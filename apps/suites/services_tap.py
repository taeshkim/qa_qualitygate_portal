"""UI Test Automation (TAP 연동) 서비스 — 싱크 / 실행 / 폴링 / 취소.

- `sync_tap_suites()` : TAP Suite 목록 → TapSuite 미러 (매분 cron + 화면 수동)
- `trigger_tap_suite()` : TAP Suite 실행 → TapRun 생성
- `poll_active_tap_runs()` : 진행 중 TapRun 폴링(케이스 수 기준 진행) — cron + HTMX
- `cancel_tap_run()` : 실행 취소

모든 TAP 호출은 TapError 로 격리 — TAP 장애가 포털 화면/cron 을 막지 않는다.
설계 근거: docs/prd_ui_test_automation.md
"""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.integrations.tapilot import TapClient, TapError

from .models import TapRun, TapSuite

logger = logging.getLogger(__name__)


def sync_tap_suites(client: TapClient | None = None) -> int:
    """TAP GET /suites/ → TapSuite upsert. 반환: 싱크된 suite 수 (-1 = TAP 미설정/실패).

    TAP 에서 사라진 suite 는 is_removed=True (이력 보존, 목록에서 숨김).
    """
    client = client or TapClient()
    if not client.configured:
        return -1
    try:
        items = client.list_suites()
    except TapError as e:
        logger.warning("TAP suite sync 실패: %s", e)
        return -1

    now = timezone.now()
    seen_ids = []
    for item in items:
        tap_id = item.get("id")
        if tap_id is None:
            continue
        seen_ids.append(tap_id)
        TapSuite.objects.update_or_create(
            tap_id=tap_id,
            defaults={
                "name": item.get("name", ""),
                "description": item.get("description", "") or "",
                "owner_display": item.get("owner_display", "") or "",
                "project_id": item.get("project_id"),
                "project_name": item.get("project_name", "") or "",
                "milestone_name": item.get("milestone_name", "") or "",
                "case_count": len(item.get("case_ids") or []) or item.get("case_count", 0) or 0,
                "raw_json": item,
                "is_removed": False,
                "synced_at": now,
            },
        )
    # TAP 에서 사라진 것 표시 (삭제하지 않음 — 실행 이력 보존)
    TapSuite.objects.exclude(tap_id__in=seen_ids).filter(is_removed=False).update(is_removed=True)
    return len(seen_ids)


def trigger_tap_suite(
    tap_suite: TapSuite, user=None, schedule=None,
    attempt: int = 1, retry_origin: TapRun | None = None, notify: bool = True,
) -> TapRun:
    """TAP Suite 실행 → TapRun 생성. 실패 시 TapError 전파(호출부에서 메시지 처리).

    - ``schedule``: Reliability(UI Automation) 스케줄이 트리거한 경우 그 스케줄 —
      세션 통계 집계(RobustnessSchedule._tap_aggregate) 의 연결고리.
    - ``attempt``/``retry_origin``: 자동 재시도 회차/원본 run (poll_tap_run 이 설정).
    - ``notify=False``: 재시도 트리거 — 시작 알림을 다시 보내지 않는다(스팸 방지,
      Jenkins retry 와 동일 정책).
    """
    client = TapClient()
    data = client.run_suite(tap_suite.tap_id)
    run = TapRun.objects.create(
        tap_suite=tap_suite,
        session_id=data["session_id"],
        status=TapRun.Status.QUEUED if data.get("queued") else TapRun.Status.PENDING,
        run_env=data.get("run_env", "") or "",
        excluded_case_ids=data.get("excluded_case_ids") or [],
        tap_url=data.get("tap_url", "") or "",
        total_cases=tap_suite.case_count,
        triggered_by=user,
        robustness_schedule=schedule,
        attempt=attempt,
        retry_origin=retry_origin,
    )
    logger.info("TAP suite %s 실행 → session %s (attempt %d)", tap_suite.name, run.session_id, attempt)
    if notify:
        # Slack 시작 알림 (토글/채널 미설정은 send 내부에서 silent 처리)
        from apps.notifications import events as nev
        from apps.notifications.services import send_tap_run_event_async
        send_tap_run_event_async(nev.TAP_RUN_STARTED, run)
    return run


def _apply_status(run: TapRun, st: dict) -> None:
    run.status = st.get("status", run.status)
    run.total_cases = st.get("total_cases", run.total_cases) or run.total_cases
    run.completed_cases = st.get("completed_cases", run.completed_cases) or 0
    run.passed = st.get("passed", run.passed) or 0
    run.failed = st.get("failed", run.failed) or 0
    run.others = st.get("others", run.others) or 0
    if st.get("testrail_run_id"):
        run.testrail_run_id = st["testrail_run_id"]
    if st.get("terminal") and not run.finished_at:
        run.finished_at = timezone.now()
    run.save()


def poll_tap_run(run: TapRun, client: TapClient | None = None) -> None:
    """TapRun 1건 폴링 — 종결 시 케이스별 결과 저장 + Slack PASS/FAIL 알림.

    알림은 **active → terminal 전이 시 1회만** 발송된다(폴링은 active run 만
    대상이라 전이는 한 번). completed → PASS/FAIL(failed>0), error → FAIL,
    stopped(취소) → 발송 안 함(명시적 사용자 액션 — Jenkins abort 와 동일 정책).
    """
    client = client or TapClient()
    was_active = run.is_active
    try:
        st, _poll_after = client.run_status(run.session_id)
    except TapError as e:
        logger.warning("TAP run %s 폴링 실패: %s", run.session_id, e)
        return
    _apply_status(run, st)
    if st.get("terminal"):
        try:
            detail = client.run_detail(run.session_id)
            run.raw_cases_json = detail.get("cases") or []
            run.save(update_fields=["raw_cases_json"])
        except TapError as e:
            logger.warning("TAP run %s 상세 저장 실패: %s", run.session_id, e)
        if was_active:
            from apps.notifications import events as nev
            from apps.notifications.services import send_tap_run_event_async
            if run.status == TapRun.Status.COMPLETED and run.failed > 0:
                # 자동 재시도 — completed+실패 케이스만 대상 (error/stopped 미대상,
                # Jenkins '순수 FAILURE 만 재시도' 정책 승계). 중간 시도 실패는
                # Slack 미발송 + ActivityLog RETRY 기록, 최종 시도만 FAIL 발송.
                max_attempts = max(1, run.tap_suite.retry_count or 1)
                if run.attempt < max_attempts and _retry_tap_run(run, max_attempts):
                    return
                send_tap_run_event_async(nev.TAP_RUN_FAILED, run)
            elif run.status == TapRun.Status.COMPLETED:
                send_tap_run_event_async(nev.TAP_RUN_PASSED, run)
            elif run.status == TapRun.Status.ERROR:
                send_tap_run_event_async(nev.TAP_RUN_FAILED, run)


def _retry_tap_run(run: TapRun, max_attempts: int) -> bool:
    """실패한 TapRun 의 자동 재시도 트리거. 성공 시 True (원본 FAIL 알림 억제).

    재시도 트리거 자체가 실패하면 False — 호출부가 원본을 최종 실패로 알림.
    """
    from apps.activitylog.models import ActivityLog

    try:
        new_run = trigger_tap_suite(
            run.tap_suite,
            user=run.triggered_by,
            schedule=run.robustness_schedule,
            attempt=run.attempt + 1,
            retry_origin=run,
            notify=False,
        )
    except TapError as e:
        logger.warning("TAP run %s 재시도 트리거 실패: %s", run.session_id, e)
        return False
    try:
        ActivityLog.log(
            run.triggered_by, ActivityLog.Action.RETRY,
            ActivityLog.TargetType.SUITE_RUN, target=new_run,
            target_name=f"[TAP] {run.tap_suite.name}",
            detail=(f"auto retry {new_run.attempt}/{max_attempts} "
                    f"(session {run.session_id} FAIL {run.failed}건)"),
        )
    except Exception:
        logger.exception("TAP retry activity log 실패")
    return True


def poll_active_tap_runs() -> int:
    """진행 중(queued/pending/running) TapRun 전부 폴링. 반환: 폴링한 수."""
    client = TapClient()
    if not client.configured:
        return 0
    active = list(TapRun.objects.filter(status__in=TapRun.ACTIVE_STATUSES).select_related("tap_suite"))
    for run in active:
        try:
            poll_tap_run(run, client)
        except Exception:
            logger.exception("TAP run %s 폴링 중 오류", run.session_id)
    return len(active)


def cancel_tap_run(run: TapRun) -> None:
    """실행 취소 — TAP cancel 호출 후 즉시 1회 폴링해 상태 반영."""
    client = TapClient()
    client.cancel_run(run.session_id)
    poll_tap_run(run, client)
