"""Robustness Test 스케줄 실행 로직.

각 스케줄은 자체 interval(분)을 가지며, 활성 상태에서는
interval 마다 1회씩 해당 Test Suite 를 자동 트리거한다.

트리거는 실제 Celery 같은 스케줄러를 붙이지 않고,
**상세 페이지의 HTMX fragment**(`test_detail_fragment`, hx-trigger
`load, every 10s`)가 호출하는 `sweep_active_schedules()` 시점에
처리한다. 목록/상세 진입(GET) 자체는 sweep 하지 않고 DB 상태로
즉시 렌더한다(페이지 로드를 Jenkins 직렬 폴링으로 막지 않기 위함).
따라서 어떤 상세 페이지가 열려 있는 동안에는 근사한 주기로 전체
스케줄 실행이 유지되며, 목록만 열어둔 경우엔 sweep 이 돌지 않는다.
중복 호출은 `sweep_active_schedules` 의 5초 쿨다운으로 1회로 합쳐진다.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.integrations.jenkins import JenkinsError
from apps.notifications import events as nev
from apps.notifications.services import send_suite_run_event_async, send_tap_run_event_async
from apps.suites.models import TestSuiteJobRun, TestSuiteRun
from apps.suites.throttle import claim_trigger_slot
from apps.suites.views import _abort_suite_run, _poll_run_status, _try_trigger_next, pick_idle_job

from .models import RobustnessRun, RobustnessSchedule

logger = logging.getLogger(__name__)


def start_schedule(schedule: RobustnessSchedule) -> bool:
    """스케줄 실행 시작(또는 재개). **이미 실행 중이면 no-op — False 반환.**

    conditional UPDATE(`is_running=False → True`)로 race-free 하게 판정한다.
    개별 '시작' 직후 '일괄 시작' 이 같은 스케줄을 또 잡는 중복 시작이 발생해도
    두 번째 호출이 아무것도 바꾸지 않는다 — 과거엔 무조건 `last_triggered_at=None`
    으로 리셋해 직후 sweep 이 interval 도래로 오인, **방금 트리거된 run 을
    '이전 사이클 미완료' 로 timeout 마감(전 잡 FAILURE)** 해버렸다
    (2026-07-16 am-mapp-regression-api-stg seq 643 사건).

    기존 세션(`started_at`)이 있으면 **유지**한다 — 중지→재개 시 성공률/실행수
    등 세션 통계가 초기화되지 않고 이어서 집계된다. 통계를 비우는 것은 명시적
    '통계 초기화'(`reset_schedule_stats`) 만 한다. `last_triggered_at` 은 비워
    재개 직후 sweep 에서 즉시 1회 트리거되게 한다(기존 시작 동작과 동일).
    """
    updated = RobustnessSchedule.objects.filter(
        pk=schedule.pk, is_running=False,
    ).update(is_running=True, stopped_at=None, last_triggered_at=None)
    if updated:
        # 세션이 없을 때만(최초 시작/reset 후) 시작점을 지금으로.
        RobustnessSchedule.objects.filter(
            pk=schedule.pk, started_at__isnull=True,
        ).update(started_at=timezone.now())
    schedule.refresh_from_db(fields=[
        "is_running", "started_at", "stopped_at", "last_triggered_at",
    ])
    return bool(updated)


def reset_schedule_stats(schedule: RobustnessSchedule) -> None:
    """스케줄의 세션 통계를 초기화한다.

    `started_at`, `last_triggered_at`, `stopped_at` 을 모두 None 으로 비운다.
    `session_runs = runs.filter(triggered_at__gte=started_at)` 이고 started_at
    이 None 이면 빈 쿼리셋이라 성공/실패/실행수/성공률/마지막 실행/세션 시작
    표시가 모두 깨끗하게 비워진다. `is_running` 은 손대지 않는다 — 멈춰 있는
    스케줄을 reset 한다고 시작되지 않도록.

    running 인 스케줄을 reset 한 경우 다음 sweep 트리거에서 `_trigger_once`
    가 started_at 을 now 로 다시 채워 그 이후 run 들이 정상 카운트된다.
    """
    schedule.started_at = None
    schedule.last_triggered_at = None
    schedule.stopped_at = None
    schedule.save(update_fields=["started_at", "last_triggered_at", "stopped_at"])


def stop_schedule(schedule: RobustnessSchedule) -> None:
    """스케줄을 정지하고, 진행 중인 실행을 강제로 중단한다."""
    # 0. TAP(UI Automation) 스케줄 — 진행 중 TapRun 을 TAP 에 취소 요청
    if schedule.tap_suite_id:
        from apps.suites.models import TapRun
        from apps.suites.services_tap import cancel_tap_run
        for tr in schedule.tap_runs.filter(status__in=TapRun.ACTIVE_STATUSES):
            try:
                cancel_tap_run(tr)
            except Exception:
                logger.exception("TAP run %s 취소 실패", tr.session_id)
        schedule.is_running = False
        schedule.stopped_at = timezone.now()
        schedule.save(update_fields=["is_running", "stopped_at"])
        return

    # 1. 진행 중인 RobustnessRun 들의 SuiteRun 을 abort
    active_runs = list(
        schedule.runs.filter(
            status__in=[RobustnessRun.Status.PENDING, RobustnessRun.Status.RUNNING],
        ).select_related("suite_run")
    )
    for r in active_runs:
        if r.suite_run is not None:
            try:
                _abort_suite_run(r.suite_run)
            except Exception:
                logger.exception("abort suite_run %s failed", r.suite_run_id)
        r.status = RobustnessRun.Status.ABORTED
        r.completed_at = timezone.now()
        r.save(update_fields=["status", "completed_at"])

    # 2. 스케줄 자체를 정지 상태로 표시
    schedule.is_running = False
    schedule.stopped_at = timezone.now()
    schedule.save(update_fields=["is_running", "stopped_at"])


def _timeout_unfinished_runs(schedule: RobustnessSchedule) -> int:
    """interval 도달 시 아직 끝나지 않은 이전 사이클의 잡을 FAILURE 로 마감.

    PENDING/QUEUED/RUNNING 잡 모두 FAILURE 로 마킹하고, QUEUED/RUNNING 은
    가능한 한 Jenkins 측 빌드도 stop_build 호출로 종료한다. SuiteRun 은
    COMPLETED 로 닫고 RobustnessRun 은 FAILURE 로 마무리한다.

    **나이 가드**: 트리거된 지 한 interval 이상 지난 run 만 대상 — '이전
    사이클' 판정을 시간으로 강제한다. start 중복/reset 직후 sweep 등으로
    `last_triggered_at` 만 초기화된 경우, 방금 트리거된 멀쩡한 run 을
    미완료 사이클로 오인해 죽이지 않게 (2026-07-16 seq 643 사건 재발 방지
    2차 방어선 — 1차는 start_schedule 의 멱등화).

    **증거 보존**: 마감된 잡은 console_output 에 사유([interval timeout])를
    남기고, Jenkins 빌드가 있으면 콘솔 로그를 best-effort 로 수집해 함께
    저장한다. 스케줄 단위로 ActivityLog(CANCEL) 1건 기록 — "로그가 아무것도
    안 남는" 무증거 실패 방지.

    반환: 실패 처리된 잡 수.
    """
    from apps.activitylog.models import ActivityLog

    now = timezone.now()
    cutoff = now - timedelta(minutes=schedule.interval_minutes)
    active_rrs = list(
        schedule.runs.filter(
            status__in=[RobustnessRun.Status.PENDING, RobustnessRun.Status.RUNNING],
            triggered_at__lte=cutoff,
        ).select_related("suite_run", "suite_run__suite")
    )
    affected_jobs = 0
    reason = (
        f"[interval timeout] 스케줄 '{schedule.robustness_test.name} / "
        f"{schedule.test_suite.name}' 의 실행 간격({schedule.interval_minutes}분) 이 "
        f"도래할 때까지 완료되지 않아 {timezone.localtime(now):%Y-%m-%d %H:%M:%S} 에 "
        f"FAILURE 로 강제 마감되었습니다."
    )

    for rr in active_rrs:
        suite_run = rr.suite_run
        if suite_run is None:
            rr.status = RobustnessRun.Status.FAILURE
            rr.completed_at = now
            rr.save(update_fields=["status", "completed_at"])
            continue

        client = suite_run.suite.get_jenkins_client()
        job_runs = list(suite_run.job_runs.select_related("job").all())
        for jr in job_runs:
            if jr.status in (TestSuiteJobRun.Status.QUEUED, TestSuiteJobRun.Status.RUNNING):
                if jr.jenkins_build_number:
                    try:
                        client.stop_build(jr.job.job_full_path, jr.jenkins_build_number)
                    except Exception:
                        logger.exception(
                            "interval timeout: stop_build failed for %s #%s",
                            jr.job.job_full_path, jr.jenkins_build_number,
                        )
                # 마감 사유 + 중단 시점까지의 Jenkins 콘솔을 best-effort 수집.
                console = ""
                if jr.jenkins_build_number:
                    try:
                        console = client.get_console_output(
                            jr.job.job_full_path, jr.jenkins_build_number,
                        ) or ""
                    except Exception:
                        logger.exception(
                            "interval timeout: console fetch failed for %s #%s",
                            jr.job.job_full_path, jr.jenkins_build_number,
                        )
                marker = f"{reason} (마감 전 상태: {jr.status}, build #{jr.jenkins_build_number or '-'})"
                jr.console_output = f"{marker}\n\n{console}" if console else marker
                jr.status = TestSuiteJobRun.Status.FAILURE
                jr.completed_at = now
                jr.save(update_fields=["status", "completed_at", "console_output"])
                affected_jobs += 1
            elif jr.status in (
                TestSuiteJobRun.Status.PENDING,
                TestSuiteJobRun.Status.WAITING,
            ):
                # 아직 트리거 안 된 잡(지연 보류 WAITING 포함) → 빌드 stop 없이 FAILURE.
                jr.console_output = (
                    f"{reason} (마감 전 상태: {jr.status} — 앞 잡이 끝나지 않아 "
                    f"Jenkins 트리거 전이었음, 빌드/콘솔 없음)"
                )
                jr.status = TestSuiteJobRun.Status.FAILURE
                jr.completed_at = now
                jr.save(update_fields=["status", "completed_at", "console_output"])
                affected_jobs += 1

        all_jrs = suite_run.job_runs.all()
        suite_run.passed_jobs = all_jrs.filter(
            status=TestSuiteJobRun.Status.SUCCESS,
        ).count()
        suite_run.failed_jobs = all_jrs.filter(
            status__in=[
                TestSuiteJobRun.Status.FAILURE,
                TestSuiteJobRun.Status.ABORTED,
            ],
        ).count()
        suite_run.status = TestSuiteRun.Status.COMPLETED
        suite_run.completed_at = now
        suite_run.save()

        rr.status = RobustnessRun.Status.FAILURE
        rr.completed_at = now
        rr.save(update_fields=["status", "completed_at"])

    if affected_jobs:
        logger.info(
            "Schedule %s: interval timeout — %d job(s) failed and stopped",
            schedule.pk, affected_jobs,
        )
        # 감사 추적 — cron/sweep 이 죽인 것이므로 user 없음(None).
        try:
            ActivityLog.log(
                None, ActivityLog.Action.CANCEL,
                ActivityLog.TargetType.RELIABILITY_SCHEDULE,
                target=schedule,
                target_name=f"{schedule.robustness_test.name} / {schedule.test_suite.name}",
                detail=(
                    f"interval({schedule.interval_minutes}분) 타임아웃 마감 — "
                    f"미완료 run {len(active_rrs)}건 / 잡 {affected_jobs}개 FAILURE 처리"
                ),
            )
        except Exception:
            logger.exception("interval timeout: activity log failed for %s", schedule.pk)
    return affected_jobs


def _trigger_all_pending(suite_run: TestSuiteRun) -> None:
    """PENDING/WAITING 잡을 트리거 시도 (병렬 모드).

    병렬 모드라도 **실행 지연 throttle 게이트**(``claim_trigger_slot``, suite 단위) 를
    거치므로, 한 sweep 에서 그 suite 의 슬롯을 점유하는 잡은 1개뿐이고 나머지는 WAITING
    으로 보류된다. 이후 sweep 마다 딜레이가 지난 잡이 1개씩 추가로 트리거된다.

    **'순차(완료 대기)'가 아니라 '엇갈린 병렬'이다**: 먼저 트리거된 잡은 계속 RUNNING
    이고 다음 잡은 그 잡의 완료를 기다리지 않고 ``trigger_delay_seconds`` 만 지나면
    트리거되어 **함께 병렬로 돈다** — 잡들이 동시에 시작/완료되지 않고 딜레이만큼
    엇갈려 시작 → 병렬 → 엇갈려 완료. throttle 이 suite 단위라 서로 다른 스케줄/suite
    는 슬롯을 공유하지 않아 starvation 이 없다.
    """
    client = suite_run.suite.get_jenkins_client()
    pending_jobs = suite_run.job_runs.filter(
        status__in=[
            TestSuiteJobRun.Status.PENDING,
            TestSuiteJobRun.Status.WAITING,
        ],
    ).select_related("job").order_by("job__order")

    for job_run in pending_jobs:
        try:
            last_build = client.get_last_build_info(job_run.job.job_full_path)
        except JenkinsError:
            last_build = {}

        if last_build.get("building", False):
            job_run.status = TestSuiteJobRun.Status.RUNNING
            job_run.jenkins_build_number = last_build.get("number")
            job_run.jenkins_build_url = last_build.get("url", "")
            job_run.started_at = timezone.now()
            job_run.save(update_fields=[
                "status", "jenkins_build_number", "jenkins_build_url", "started_at",
            ])
            continue

        # 실행 지연 throttle 게이트(suite 단위) — 슬롯 점유 실패 시 WAITING 으로 보류.
        # 병렬 모드라도 한 sweep 에 이 suite 의 슬롯은 1개만 점유 → 잡들이 delay 간격으로
        # 엇갈려 트리거되고, 먼저 트리거된 잡은 계속 RUNNING 이라 병렬로 함께 돈다.
        if not claim_trigger_slot(suite_run.suite, job_run.job.trigger_delay_seconds):
            if job_run.status != TestSuiteJobRun.Status.WAITING:
                job_run.status = TestSuiteJobRun.Status.WAITING
                job_run.save(update_fields=["status"])
            continue

        try:
            result = client.trigger_job(job_run.job.job_full_path, gwt_token=job_run.job.gwt_token)
            job_run.status = TestSuiteJobRun.Status.QUEUED
            job_run.jenkins_build_number = result.get("build_number")
            job_run.started_at = timezone.now()
            job_run.save(update_fields=["status", "jenkins_build_number", "started_at"])
        except JenkinsError as e:
            logger.error("Failed to trigger %s: %s", job_run.job.job_name, e)
            job_run.status = TestSuiteJobRun.Status.FAILURE
            job_run.completed_at = timezone.now()
            job_run.console_output = f"[trigger failed] {e}"
            job_run.save(update_fields=["status", "completed_at", "console_output"])


def _trigger_once(schedule: RobustnessSchedule) -> RobustnessRun | None:
    """해당 스케줄의 Test Suite 를 1회 실행한다."""
    suite = schedule.test_suite
    jobs = list(suite.jobs.filter(is_active=True, is_target=True).order_by("order"))
    if not jobs:
        logger.warning("Schedule %s: no active target jobs on suite %s", schedule.pk, suite.pk)
        return None

    # reset 직후 처음 트리거되는 경우 — 세션 시작점을 지금으로 잡아 이 트리거
    # 이후 RobustnessRun 이 session_runs 에 정상 카운트되도록.
    if schedule.started_at is None:
        schedule.started_at = timezone.now()
        schedule.save(update_fields=["started_at"])

    test = schedule.robustness_test
    single = test.single_job_mode

    # 단일 테스트 모드: Suite 잡 전체가 아니라 '비어있는(idle)' worker 1개만 실행
    # (단독 Suite Run 의 idle 경로와 동일한 선택 로직). idle 이 없으면 이번 사이클 skip.
    if single:
        busy_job_ids = set(
            TestSuiteJobRun.objects.filter(
                suite_run__suite=suite,
                suite_run__status=TestSuiteRun.Status.RUNNING,
                status__in=[
                    TestSuiteJobRun.Status.PENDING,
                    TestSuiteJobRun.Status.WAITING,
                    TestSuiteJobRun.Status.QUEUED,
                    TestSuiteJobRun.Status.RUNNING,
                ],
            ).values_list("job_id", flat=True)
        )
        candidates = [j for j in jobs if j.id not in busy_job_ids]
        picked = pick_idle_job(suite, candidates) if candidates else None
        if picked is None:
            logger.info(
                "Schedule %s: 단일 테스트 모드 — idle worker 없음(전부 busy/확인불가), 이번 사이클 skip",
                schedule.pk,
            )
            return None
        run_jobs = [picked]
    else:
        run_jobs = jobs

    # 고아 suite_run 방지 — job_run 들까지 한 트랜잭션으로 묶는다 (seq_no 집계 포함).
    with transaction.atomic():
        suite_run = TestSuiteRun.objects.create(
            suite=suite,
            status=TestSuiteRun.Status.RUNNING,
            total_jobs=len(run_jobs),
            retry_limit=max(1, suite.retry_count),
            started_at=timezone.now(),
            seq_no=TestSuiteRun.next_seq_no(suite),
        )
        for job in run_jobs:
            TestSuiteJobRun.objects.create(
                suite_run=suite_run,
                job=job,
                status=TestSuiteJobRun.Status.PENDING,
            )

    robustness_run = RobustnessRun.objects.create(
        schedule=schedule,
        suite_run=suite_run,
        status=RobustnessRun.Status.RUNNING,
    )

    try:
        # 단일 모드는 잡 1개라 순차 트리거. parallel 은 단일 모드가 아닐 때만 의미.
        if (not single) and test.parallel_jobs:
            _trigger_all_pending(suite_run)
        else:
            _try_trigger_next(suite_run)
    except Exception:
        logger.exception("Schedule %s: trigger failed", schedule.pk)

    # Suite Run 시작 알림 — 단독 경로(suites/services.py 의 trigger 헬퍼)는 START 를
    # 보내지만 Reliability 경로는 _trigger_once 가 suite_run 을 직접 만들므로 여기서
    # 같이 보낸다. (PASS/FAIL 은 공용 _poll_run_status 가 발송하므로 기존에도 정상)
    # 토글(SUITE_RUN_STARTED)·토큰/채널 미설정은 send_* 내부에서 silent 처리.
    send_suite_run_event_async(nev.SUITE_RUN_STARTED, suite_run)

    now = timezone.now()
    schedule.last_triggered_at = now
    schedule.save(update_fields=["last_triggered_at"])
    return robustness_run


def _update_run_status(robustness_run: RobustnessRun) -> None:
    suite_run = robustness_run.suite_run
    if suite_run is None:
        return

    if suite_run.status != TestSuiteRun.Status.COMPLETED:
        try:
            _poll_run_status(suite_run)
            suite_run.refresh_from_db()
            # 병렬 스케줄: 재시도로 PENDING 으로 되돌아온 잡들을 한 번에 다시 트리거한다.
            # _poll_run_status 내부의 _try_trigger_next 는 1개만 재트리거하므로, 그대로 두면
            # 병렬 실행이 폴링 사이클마다 1개씩 순차 재시도로 강등된다.
            if (
                suite_run.status == TestSuiteRun.Status.RUNNING
                and robustness_run.schedule.robustness_test.parallel_jobs
            ):
                _trigger_all_pending(suite_run)
        except Exception:
            logger.exception("poll failed for suite_run %s", suite_run.pk)
            return

    if suite_run.status == TestSuiteRun.Status.COMPLETED:
        if suite_run.failed_jobs == 0 and suite_run.passed_jobs == suite_run.total_jobs:
            robustness_run.status = RobustnessRun.Status.SUCCESS
        else:
            robustness_run.status = RobustnessRun.Status.FAILURE
        robustness_run.completed_at = suite_run.completed_at or timezone.now()
        robustness_run.save(update_fields=["status", "completed_at"])


def _claim_trigger_turn(schedule: RobustnessSchedule) -> bool:
    """interval 도래 판정 + race-free 트리거 슬롯 점유 (conditional UPDATE).

    Jenkins/TAP 스케줄 공용 — 동시 sweep 중 정확히 1개만 True 를 받는다.
    True 반환 시 schedule.last_triggered_at 이 now 로 갱신돼 있다.
    """
    now = timezone.now()
    prev_lta = schedule.last_triggered_at
    qs = RobustnessSchedule.objects.filter(pk=schedule.pk, is_running=True)
    if prev_lta is None:
        qs = qs.filter(last_triggered_at__isnull=True)
    else:
        threshold = now - timedelta(minutes=schedule.interval_minutes)
        if prev_lta > threshold:
            return False  # interval 아직 안 지났음
        qs = qs.filter(last_triggered_at=prev_lta)
    if not qs.update(last_triggered_at=now):
        return False  # 다른 sweep 가 먼저 트리거
    schedule.last_triggered_at = now
    return True


def _timeout_unfinished_tap_runs(schedule: RobustnessSchedule) -> int:
    """TAP 스케줄 interval 도래 시 — 한 interval 이상 지난 미완료 TapRun 을 마감.

    TAP 에 취소를 요청한 뒤 로컬 상태를 **error** 로 기록한다(케이스 잔여분이
    실패로 집계돼 'interval 안에 못 끝남' 신호 유지 — Jenkins 경로의 FAILURE
    마킹과 동일한 의미). ActivityLog(CANCEL) 로 감사 추적. 나이 가드/증거 보존
    정책은 Jenkins 경로(_timeout_unfinished_runs)와 동일.
    """
    from apps.activitylog.models import ActivityLog
    from apps.suites.models import TapRun
    from apps.suites.services_tap import cancel_tap_run

    now = timezone.now()
    cutoff = now - timedelta(minutes=schedule.interval_minutes)
    stale = list(schedule.tap_runs.filter(
        status__in=TapRun.ACTIVE_STATUSES, started_at__lte=cutoff,
    ))
    for tr in stale:
        try:
            cancel_tap_run(tr)
        except Exception:
            logger.exception("TAP timeout: run %s 취소 실패", tr.session_id)
        tr.refresh_from_db()
        tr.status = TapRun.Status.ERROR  # 타임아웃 = 실패 신호 (Aborted 아님)
        if not tr.finished_at:
            tr.finished_at = now
        tr.save(update_fields=["status", "finished_at"])
        # 타임아웃 마감은 실패 신호 — Slack FAIL 알림 (취소 자체(stopped)는 silent).
        send_tap_run_event_async(nev.TAP_RUN_FAILED, tr)
    if stale:
        try:
            ActivityLog.log(
                None, ActivityLog.Action.CANCEL,
                ActivityLog.TargetType.RELIABILITY_SCHEDULE,
                target=schedule,
                target_name=f"{schedule.robustness_test.name} / {schedule.target_name}",
                detail=f"interval({schedule.interval_minutes}분) 타임아웃 — TAP run {len(stale)}건 error 마감",
            )
        except Exception:
            logger.exception("TAP timeout: activity log 실패")
    return len(stale)


def _trigger_tap_once(schedule: RobustnessSchedule):
    """TAP 스케줄 1회 트리거 — 세션 시작점 보정 포함 (Jenkins _trigger_once 대응)."""
    from apps.integrations.tapilot import TapError
    from apps.suites.services_tap import trigger_tap_suite

    if schedule.started_at is None:
        schedule.started_at = timezone.now()
        schedule.save(update_fields=["started_at"])
    try:
        return trigger_tap_suite(schedule.tap_suite, schedule=schedule)
    except TapError as e:
        logger.warning("TAP 스케줄 %s 트리거 실패: %s", schedule.pk, e)
        return None


def _sweep_tap_schedule(schedule: RobustnessSchedule) -> None:
    """TAP(UI Automation) 스케줄 sweep — 진행 폴링 + interval 트리거.

    Jenkins 경로와 동일한 골격: ① 진행 중 run 폴링 ② race-free interval 판정
    ③ 타임아웃 마감(나이 가드) ④ 새 트리거. 병렬/단일 모드는 TAP 에 해당 없음
    (Suite 실행 = 세션 1개).
    """
    from apps.suites.models import TapRun
    from apps.suites.services_tap import poll_tap_run

    for tr in schedule.tap_runs.filter(status__in=TapRun.ACTIVE_STATUSES):
        try:
            poll_tap_run(tr)
        except Exception:
            logger.exception("TAP run %s 폴링 실패", tr.session_id)

    if not schedule.is_running:
        return
    if not _claim_trigger_turn(schedule):
        return
    _timeout_unfinished_tap_runs(schedule)
    _trigger_tap_once(schedule)


def sweep_schedule(schedule: RobustnessSchedule) -> None:
    """한 스케줄에 대해: 진행 중 실행을 갱신하고, 간격이 지났으면 새로 트리거.

    HTMX 5초 폴링 + 여러 워커/탭 환경에서 sweep 가 동시 호출되어 같은 schedule 에
    대해 should_trigger 가 모두 True 로 평가되면 같은 사이클에 Run 이 여러 개
    생성되는 race condition 이 발생한다. 트리거 결정을 conditional UPDATE 로
    하여 동시 호출 중 정확히 1개만 통과시킨다.

    TAP(UI Automation) 스케줄은 `_sweep_tap_schedule` 로 분기 — 케이스 수 기준
    진행/트리거이며 나머지 상태기계(시작/중지/reset/멱등 start)는 공유한다.
    """
    if schedule.tap_suite_id:
        _sweep_tap_schedule(schedule)
        return
    # 1. 진행 중 실행 갱신. _update_run_status → _poll_run_status/_trigger_all_pending
    # 가 suite_run.suite (Jenkins client, throttle) 와 schedule.robustness_test
    # (parallel_jobs) 를 역참조하므로 select_related 로 N+1 폴링 쿼리 제거.
    active = schedule.runs.filter(
        status__in=[RobustnessRun.Status.PENDING, RobustnessRun.Status.RUNNING],
    ).select_related("suite_run", "suite_run__suite", "schedule__robustness_test")
    for r in active:
        _update_run_status(r)

    if not schedule.is_running:
        return

    # 2. race-free 트리거 결정 — 공용 _claim_trigger_turn (TAP sweep 과 공유).
    if not _claim_trigger_turn(schedule):
        return
    _timeout_unfinished_runs(schedule)
    _trigger_once(schedule)


# 전역 sweep 쿨다운 — 화면 폴링(HTMX 10초) 이 다중 탭/유저로 동시에 들어와도
# 짧은 창 안에서는 1회만 실제 sweep 하도록 묶는다. sweep 은 각 running 스케줄의
# in-flight Run 을 Jenkins 로 폴링(직렬, 호출당 최대 8초)하므로, 동시 다발 sweep 이
# SQLite 쓰기 락을 물고 늘어져 포털 전체가 느려지는 것을 막는다(README §10).
# cron 대체 역할이라 최대 이 시간만큼 트리거가 늦어질 수 있으나 interval(분) 단위라 무해.
_SWEEP_COOLDOWN_SECS = 5
_SWEEP_COOLDOWN_KEY = "robustness:sweep_active_cooldown"


def sweep_active_schedules(*, force: bool = False) -> None:
    """전역 sweep — 활성 스케줄 전체를 한 번씩 점검.

    ``force=False`` (기본) 이면 ``_SWEEP_COOLDOWN_SECS`` 쿨다운을 적용해 짧은 창 안의
    중복 호출을 1회로 합친다. 명시적 사용자 액션(시작/수동 트리거)은 ``sweep_schedule``
    /``_trigger_once`` 를 직접 호출하므로 이 쿨다운의 영향을 받지 않는다.
    """
    from django.core.cache import cache

    # cache.add: 키가 없을 때만 set+True. 최근 창 안에 이미 sweep 했으면 False → skip.
    if not force and not cache.add(_SWEEP_COOLDOWN_KEY, 1, _SWEEP_COOLDOWN_SECS):
        return

    schedules = RobustnessSchedule.objects.filter(is_running=True).select_related(
        "test_suite", "tap_suite",
    )
    for schedule in schedules:
        try:
            sweep_schedule(schedule)
        except Exception:
            logger.exception("sweep failed for schedule %s", schedule.pk)
