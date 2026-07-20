"""Robustness Test 스케줄 실행 로직.

각 스케줄은 자체 interval(분)을 가지며, 활성 상태에서는
interval 마다 1회씩 해당 Test Suite 를 자동 트리거한다.

트리거는 실제 Celery 같은 스케줄러를 붙이지 않고,
뷰 요청(리스트/상세/폴링) 시점에 `sweep_active_schedules()`
를 호출하여 처리한다. 상세 페이지는 HTMX 폴링으로 짧은 간격
에 재호출되므로, 페이지가 열려 있는 동안에는 근사한 주기로
실행이 유지된다.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.integrations.jenkins import JenkinsError
from apps.suites.models import TestSuiteJobRun, TestSuiteRun
from apps.suites.views import _abort_suite_run, _poll_run_status, _try_trigger_next

from .models import RobustnessRun, RobustnessSchedule

logger = logging.getLogger(__name__)


def start_schedule(schedule: RobustnessSchedule) -> None:
    """스케줄 실행 시작 — 세션 시작시간을 재설정한다."""
    now = timezone.now()
    schedule.is_running = True
    schedule.started_at = now
    schedule.stopped_at = None
    schedule.last_triggered_at = None
    schedule.save(update_fields=[
        "is_running", "started_at", "stopped_at", "last_triggered_at",
    ])


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

    반환: 실패 처리된 잡 수.
    """
    active_rrs = list(
        schedule.runs.filter(
            status__in=[RobustnessRun.Status.PENDING, RobustnessRun.Status.RUNNING],
        ).select_related("suite_run", "suite_run__suite")
    )
    affected_jobs = 0
    now = timezone.now()

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
                jr.status = TestSuiteJobRun.Status.FAILURE
                jr.completed_at = now
                jr.save(update_fields=["status", "completed_at"])
                affected_jobs += 1
            elif jr.status == TestSuiteJobRun.Status.PENDING:
                jr.status = TestSuiteJobRun.Status.FAILURE
                jr.completed_at = now
                jr.save(update_fields=["status", "completed_at"])
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
    return affected_jobs


def _trigger_all_pending(suite_run: TestSuiteRun) -> None:
    """PENDING 잡을 모두 한 번에 Jenkins 로 트리거 (병렬 모드)."""
    client = suite_run.suite.get_jenkins_client()
    pending_jobs = suite_run.job_runs.filter(
        status=TestSuiteJobRun.Status.PENDING,
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

        try:
            result = client.trigger_build(job_run.job.job_full_path)
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

    suite_run = TestSuiteRun.objects.create(
        suite=suite,
        status=TestSuiteRun.Status.RUNNING,
        total_jobs=len(jobs),
        started_at=timezone.now(),
    )
    for job in jobs:
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
        if schedule.robustness_test.parallel_jobs:
            _trigger_all_pending(suite_run)
        else:
            _try_trigger_next(suite_run)
    except Exception:
        logger.exception("Schedule %s: trigger failed", schedule.pk)

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


def sweep_schedule(schedule: RobustnessSchedule) -> None:
    """한 스케줄에 대해: 진행 중 실행을 갱신하고, 간격이 지났으면 새로 트리거.

    HTMX 5초 폴링 + 여러 워커/탭 환경에서 sweep 가 동시 호출되어 같은 schedule 에
    대해 should_trigger 가 모두 True 로 평가되면 같은 사이클에 Run 이 여러 개
    생성되는 race condition 이 발생한다. 트리거 결정을 conditional UPDATE 로
    하여 동시 호출 중 정확히 1개만 통과시킨다.
    """
    # 1. 진행 중 실행 갱신
    active = schedule.runs.filter(
        status__in=[RobustnessRun.Status.PENDING, RobustnessRun.Status.RUNNING],
    )
    for r in active:
        _update_run_status(r)

    if not schedule.is_running:
        return

    # 2. race-free 트리거 결정 — last_triggered_at 을 본 값과 같을 때만 now 로 set.
    # 동시 sweep 중 가장 먼저 UPDATE 한 1개만 1 row affected → 트리거 진행.
    # 다른 sweep 는 0 row → 그냥 return.
    now = timezone.now()
    prev_lta = schedule.last_triggered_at
    qs = RobustnessSchedule.objects.filter(pk=schedule.pk, is_running=True)
    if prev_lta is None:
        qs = qs.filter(last_triggered_at__isnull=True)
    else:
        threshold = now - timedelta(minutes=schedule.interval_minutes)
        if prev_lta > threshold:
            return  # interval 아직 안 지났음
        qs = qs.filter(last_triggered_at=prev_lta)
    updated = qs.update(last_triggered_at=now)
    if not updated:
        return  # 다른 sweep 가 먼저 트리거 — skip

    schedule.last_triggered_at = now
    _timeout_unfinished_runs(schedule)
    _trigger_once(schedule)


def sweep_active_schedules() -> None:
    """전역 sweep — 활성 스케줄 전체를 한 번씩 점검."""
    schedules = RobustnessSchedule.objects.filter(is_running=True).select_related(
        "test_suite",
    )
    for schedule in schedules:
        try:
            sweep_schedule(schedule)
        except Exception:
            logger.exception("sweep failed for schedule %s", schedule.pk)
