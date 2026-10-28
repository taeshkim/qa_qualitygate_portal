"""Slack 실패 알림 Triage 버튼 (Failure / Skip) — 블록 빌더 + 클릭 처리.

흐름:
  발송(services.build_suite_fail_payload) → 실패 Job 별 버튼 blocks(build_triage_blocks)
  → 사용자가 Slack 버튼 클릭 → Socket Mode 리스너(management/commands/slack_triage_listener)
  → apply_triage_action(job 의 verdict 확정 + 메시지 chat.update).

정책(2026-07-07 확정): 권한 = can_write + slack_user_id 매핑된 유저만 · 재분류(덮어쓰기) 허용 ·
다중 실패는 Job 별 버튼.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

ACTION_FAILURE = "triage_failure"
ACTION_SKIP = "triage_skip"
_ACTION_TO_VERDICT = {ACTION_FAILURE: "failure", ACTION_SKIP: "skip"}

# 스케줄 실패 알림의 '스케줄 중단 / 재활성화' 버튼 (스케줄 run 한정)
SCHEDULE_STOP = "schedule_stop"
SCHEDULE_REACTIVATE = "schedule_reactivate"
SCHEDULE_ACTIONS = (SCHEDULE_STOP, SCHEDULE_REACTIVATE)

# 스케줄 중단/재활성화 버튼은 지정 담당자만 클릭 처리 가능 (포털 User 이름 기준).
# 버튼 자체는 채널 모두에게 보이되, 이 사람들 외엔 클릭 시 거부(ephemeral 안내).
_SCHEDULE_CONTROLLER_NAMES = ["김태식", "김도연"]

_MAX_FAILED_JOBS = 5  # 메시지 길이 보호 — 버튼 노출 Job 수 상한


def triage_buttons_enabled() -> bool:
    """실패 알림에 Failure/Skip 버튼을 붙일지 (기본 off, opt-in).

    리스너 미배포 상태에서 '죽은 버튼'이 안 뜨도록 명시 활성화 전엔 기존 알림 유지.
    """
    return os.getenv("SLACK_TRIAGE_BUTTONS", "").strip().lower() in ("1", "true", "yes", "on")


def _slack_web():
    """chat.update 용 slack_sdk WebClient (지연 import — slack_sdk 미설치 환경 보호)."""
    from slack_sdk import WebClient
    from apps.notifications.services import _bot_token
    return WebClient(token=_bot_token())


# ---------------------------------------------------------------------------
# 블록 빌더 — 실패 Job 별 버튼/처리상태
# ---------------------------------------------------------------------------


def _failed_job_runs(suite_run):
    from apps.suites.models import TestSuiteJobRun
    return list(
        suite_run.job_runs.filter(
            status__in=[TestSuiteJobRun.Status.FAILURE, TestSuiteJobRun.Status.ABORTED],
        ).select_related("job", "triaged_by")[:_MAX_FAILED_JOBS]
    )


def build_triage_blocks(suite_run) -> list:
    """실패 Job 별 Triage 블록 목록. 실패 Job 이 없으면 빈 리스트.

    - 미분류 Job: 안내 + [🔴 Failure] [⏭️ Skip] 버튼(value=job_run_id).
    - 분류된 Job: "✅ 처리됨 — Failure/Skip by @이름" context.
    """
    from apps.suites.models import TestSuiteJobRun

    jrs = _failed_job_runs(suite_run)
    if not jrs:
        return []

    blocks: list = [
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "이 실패의 성격을 눌러주세요 — *🔴 Failure* = 실제 서비스 장애 · *⏭️ Skip* = 자동화 판정 오류 의심 및 자동화 코드 이슈 추정"}]},
    ]
    for jr in jrs:
        job_name = jr.job.job_name if jr.job else "?"
        if jr.verdict:
            who = str(jr.triaged_by) if jr.triaged_by else "?"
            label = "🔴 Failure (실장애)" if jr.verdict == TestSuiteJobRun.Verdict.FAILURE else "⏭️ Skip (자동화 판정 오류 의심)"
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                "text": f":arrow_right: `{job_name}` — *{label}* 로 분류됨 · by {who}"}]})
        else:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{job_name}* — 분류 대기"}})
            blocks.append({"type": "actions", "block_id": f"triage_{jr.pk}", "elements": [
                {"type": "button", "action_id": ACTION_FAILURE, "style": "danger",
                 "text": {"type": "plain_text", "text": "🔴 Failure"}, "value": str(jr.pk),
                 "confirm": {"title": {"type": "plain_text", "text": "실제 장애로 확정"},
                             "text": {"type": "mrkdwn", "text": f"`{job_name}` 실패를 *실제 서비스 장애*로 확정합니다."},
                             "confirm": {"type": "plain_text", "text": "Failure"},
                             "deny": {"type": "plain_text", "text": "취소"}}},
                {"type": "button", "action_id": ACTION_SKIP,
                 "text": {"type": "plain_text", "text": "⏭️ Skip"}, "value": str(jr.pk),
                 "confirm": {"title": {"type": "plain_text", "text": "Skip 처리"},
                             "text": {"type": "mrkdwn", "text": f"`{job_name}` 실패를 *자동화 판정 오류 의심 및 자동화 코드 이슈 추정*으로 넘깁니다."},
                             "confirm": {"type": "plain_text", "text": "Skip"},
                             "deny": {"type": "plain_text", "text": "취소"}}},
            ]})
    return blocks


# ---------------------------------------------------------------------------
# 스케줄 중단/재활성화 버튼 (스케줄 run 한정)
# ---------------------------------------------------------------------------


def _schedule_for_run(suite_run):
    """이 suite_run 을 트리거한 Reliability 스케줄(RobustnessSchedule) 또는 None.

    스케줄(자동) run 만 RobustnessRun 을 통해 스케줄과 연결된다 — 수동 run 은 None.
    """
    try:
        rr = (suite_run.robustness_runs
              .select_related("schedule", "schedule__test_suite").first())
        return rr.schedule if rr else None
    except Exception:  # noqa: BLE001
        return None


def build_schedule_action_blocks(suite_run) -> list:
    """스케줄 run 실패 알림의 '스케줄 중단 / 재활성화' 블록.

    상태(`suite_run.slack_schedule_action`) + 스케줄 `is_running` 에 따라:
      - 실행 중(미조치) → [🛑 스케줄 중단]
      - 중단됨          → 중단 안내 + [▶️ 스케줄 재활성화]
      - 재활성화됨      → 안내만(버튼 없음)
    스케줄 run 이 아니면 빈 리스트.
    """
    schedule = _schedule_for_run(suite_run)
    if schedule is None:
        return []
    suite_name = schedule.target_name if schedule.test_suite else "?"
    action = suite_run.slack_schedule_action or ""

    if action == "reactivated":
        return [{"type": "context", "elements": [{"type": "mrkdwn",
                 "text": f":arrow_forward: `{suite_name}` 스케줄 재활성화됨"}]}]

    if action == "stopped" or not schedule.is_running:
        blocks: list = []
        if action == "stopped":
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                "text": f":octagonal_sign: `{suite_name}` 스케줄 중단됨"}]})
        blocks.append({"type": "actions", "block_id": f"sched_{suite_run.pk}", "elements": [
            {"type": "button", "action_id": SCHEDULE_REACTIVATE, "style": "primary",
             "text": {"type": "plain_text", "text": "▶️ 스케줄 재활성화"}, "value": str(suite_run.pk)}]})
        return blocks

    # 실행 중 — 중단 버튼
    return [{"type": "actions", "block_id": f"sched_{suite_run.pk}", "elements": [
        {"type": "button", "action_id": SCHEDULE_STOP, "style": "danger",
         "text": {"type": "plain_text", "text": "🛑 스케줄 중단"}, "value": str(suite_run.pk),
         "confirm": {"title": {"type": "plain_text", "text": "스케줄 중단"},
                     "text": {"type": "mrkdwn", "text": f"`{suite_name}` 스위트의 스케줄(자동 반복 실행)을 중단합니다."},
                     "confirm": {"type": "plain_text", "text": "중단"},
                     "deny": {"type": "plain_text", "text": "취소"}}}]}]


# ---------------------------------------------------------------------------
# 클릭 처리
# ---------------------------------------------------------------------------


def resolve_portal_user(slack_user_id: str):
    """Slack user id → 포털 User (slack_user_id 매핑). 없으면 None."""
    if not slack_user_id:
        return None
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(slack_user_id=slack_user_id).first()


def _is_schedule_controller(user) -> bool:
    """스케줄 중단/재활성화 버튼을 쓸 수 있는 사람인지 (이름 기준 화이트리스트)."""
    if user is None:
        return False
    full_name = ((user.first_name or "") + (user.last_name or "")).strip()
    return full_name in _SCHEDULE_CONTROLLER_NAMES


def apply_verdict(job_run, verdict: str, user) -> dict:
    """verdict 확정 공통 코어 — Slack 버튼과 포털 판정 화면이 함께 쓴다.

    저장(덮어쓰기 허용) → Slack 원본 메시지 갱신(있을 때) → verdict 가 실제로 바뀐
    경우만 분기별 쓰레드 멘션(followup). 권한 검사는 **호출부 책임**(여기선 안 함).
    반환: {"ok": True, "verdict":..., "changed":..., "job_name":...}.
    """
    from django.utils import timezone

    changed = job_run.verdict != verdict  # 같은 값 재확정 시 followup 중복 방지용
    job_run.verdict = verdict
    job_run.triaged_by = user
    job_run.triaged_at = timezone.now()
    job_run.save(update_fields=["verdict", "triaged_by", "triaged_at"])
    logger.info("Triage: job_run=%s verdict=%s by=%s", job_run.pk, verdict, user)

    try:
        update_suite_fail_message(job_run.suite_run)
    except Exception as exc:  # 메시지 갱신 실패해도 verdict 는 이미 저장됨
        logger.warning("Triage chat.update 실패 (job_run=%s): %s", job_run.pk, exc)

    if changed:
        try:
            post_triage_followup(job_run.suite_run, job_run, verdict)
        except Exception as exc:
            logger.warning("Triage 쓰레드 멘션 발송 실패 (job_run=%s): %s", job_run.pk, exc)

    return {"ok": True, "verdict": verdict, "changed": changed,
            "job_name": job_run.job.job_name if job_run.job else "?"}


def apply_verdicts_bulk(job_runs, verdict: str, user) -> dict:
    """여러 job_run 의 verdict 를 일괄 확정 — 포털 미판정 큐의 일괄 판정용.

    `apply_verdict` 와 동일한 효과를 내되 규모에 맞게 조정:
    - **DB 저장은 동기** — 요청이 끝나면 큐에서 바로 사라진다.
    - **Slack 갱신/쓰레드 멘션은 데몬 스레드로 비동기** — 수십 건을 선택해도
      요청이 Slack 왕복(건당 수백 ms)에 막히지 않는다. 웹 프로세스는 상주라
      cron 처럼 join 이 필요 없다.
    - suite_run 별 chat.update 는 **1회로 dedupe** — 같은 run 의 잡 여러 개를
      골라도 원본 메시지는 한 번만 갱신(단건 loop 대비 Slack 호출 절감).
    - followup(쓰레드 멘션)은 verdict 가 **실제로 바뀐 job 만** — 재확정 시
      중복 멘션 방지(단건과 동일 정책).

    권한 검사는 호출부 책임. 반환: {"count": 처리 수, "changed": 변경 수}.
    """
    import threading

    from django.utils import timezone

    now = timezone.now()
    changed_jobs = []
    for jr in job_runs:
        if jr.verdict != verdict:
            changed_jobs.append(jr)
        jr.verdict = verdict
        jr.triaged_by = user
        jr.triaged_at = now
        jr.save(update_fields=["verdict", "triaged_by", "triaged_at"])
    logger.info(
        "Triage bulk: %d job_run(s) verdict=%s by=%s (changed=%d)",
        len(job_runs), verdict, user, len(changed_jobs),
    )

    # run 별 dedupe — dict 는 삽입 순서 유지.
    suite_runs = list({jr.suite_run_id: jr.suite_run for jr in job_runs}.values())

    def _slack_work():
        for sr in suite_runs:
            try:
                update_suite_fail_message(sr)
            except Exception as exc:
                logger.warning("Triage bulk chat.update 실패 (suite_run=%s): %s", sr.pk, exc)
        for jr in changed_jobs:
            try:
                post_triage_followup(jr.suite_run, jr, verdict)
            except Exception as exc:
                logger.warning("Triage bulk 쓰레드 멘션 실패 (job_run=%s): %s", jr.pk, exc)

    threading.Thread(target=_slack_work, daemon=True).start()
    return {"count": len(job_runs), "changed": len(changed_jobs)}


def apply_triage_action(action_id: str, job_run_id: str, slack_user_id: str) -> dict:
    """Slack 버튼 클릭 1건 처리. 권한 검사 → `apply_verdict` 공통 코어.

    반환: {"ok": bool, "reason": str, ...}. 리스너가 reason 으로 ephemeral 안내.
    """
    from apps.suites.models import TestSuiteJobRun

    verdict = _ACTION_TO_VERDICT.get(action_id)
    if verdict is None:
        return {"ok": False, "reason": "unknown_action"}

    jr = (TestSuiteJobRun.objects
          .filter(pk=job_run_id)
          .select_related("suite_run", "suite_run__suite", "job")
          .first())
    if jr is None:
        return {"ok": False, "reason": "not_found"}

    user = resolve_portal_user(slack_user_id)
    if user is None:
        return {"ok": False, "reason": "unmapped_user"}
    if not user.can_write:
        return {"ok": False, "reason": "forbidden"}

    return apply_verdict(jr, verdict, user)


def apply_schedule_action(action_id: str, suite_run_id: str, slack_user_id: str) -> dict:
    """'스케줄 중단 / 재활성화' 버튼 처리 — 해당 suite 의 Reliability 스케줄을 멈추거나 재개.

    중단: 상위 스위트 스케줄 stop(진행 run abort 포함). 재활성화: start + 즉시 sweep.
    """
    from apps.suites.models import TestSuiteRun

    if action_id not in SCHEDULE_ACTIONS:
        return {"ok": False, "reason": "unknown_action"}
    run = TestSuiteRun.objects.filter(pk=suite_run_id).select_related("suite").first()
    if run is None:
        return {"ok": False, "reason": "not_found"}
    user = resolve_portal_user(slack_user_id)
    if user is None:
        return {"ok": False, "reason": "unmapped_user"}
    if not _is_schedule_controller(user):
        # 스케줄 중단/재활성화는 지정 담당자(김태식·김도연)만 가능.
        return {"ok": False, "reason": "schedule_forbidden"}
    schedule = _schedule_for_run(run)
    if schedule is None:
        return {"ok": False, "reason": "no_schedule"}

    from apps.robustness.services import start_schedule, stop_schedule, sweep_schedule

    if action_id == SCHEDULE_STOP:
        stop_schedule(schedule)
        run.slack_schedule_action = "stopped"
    else:  # SCHEDULE_REACTIVATE
        start_schedule(schedule)
        try:
            sweep_schedule(schedule)  # 재활성화 즉시 1회 실행
        except Exception as exc:  # noqa: BLE001
            logger.warning("스케줄 재활성화 sweep 실패 (schedule=%s): %s", schedule.pk, exc)
        run.slack_schedule_action = "reactivated"
    run.save(update_fields=["slack_schedule_action"])
    logger.info("Schedule %s: suite=%s by=%s(slack=%s)",
                action_id, schedule.test_suite_id, user, slack_user_id)

    try:
        update_suite_fail_message(run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("스케줄 액션 chat.update 실패 (run=%s): %s", run.pk, exc)

    return {"ok": True, "action": action_id, "suite": run.suite.name if run.suite else "?"}


# ---------------------------------------------------------------------------
# 분기별 쓰레드 멘션 (FAILURE → 담당자 / SKIP → 김태식·김도연 고정)
# ---------------------------------------------------------------------------

# SKIP 시 고정 멘션 대상 (자동화 담당). 이름 → Slack ID 는 slack_users 로 동적 해석.
_SKIP_MENTION_NAMES = ["김태식", "김도연"]


def _owner_mentions_for_job_run(job_run) -> str:
    """실패 job 의 에러 URL 담당자 Slack 멘션 텍스트. 매칭 없으면 대체 담당자."""
    try:
        from apps.integrations.url_matcher import extract_urls
        from apps.integrations.log_errors import extract_error_log
        from apps.integrations.models import FallbackOwners
        from .owner_mentions import build_mentions, mentions_from_names, _names_for_status

        error_log = extract_error_log(job_run.console_output or "")
        if error_log:
            owners_with_status = [
                (m.owner, m.status_code)
                for m in extract_urls(error_log)
                if m.owner is not None and _names_for_status(m.owner, m.status_code)
            ]
            if owners_with_status:
                mention_text, _ = build_mentions(owners_with_status)
                if mention_text:
                    return mention_text
        names = FallbackOwners.get_solo().names()
        if names:
            mention_text, _ = mentions_from_names(names)
            return mention_text
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner 멘션 조회 실패 (job_run=%s): %s", getattr(job_run, "pk", "?"), exc)
    return ""


def _fixed_skip_mentions() -> str:
    """SKIP 고정 멘션(김태식·김도연) — 이름을 Slack ID 로 해석(실패 시 @이름 plain)."""
    from .owner_mentions import mentions_from_names
    mention_text, _ = mentions_from_names(_SKIP_MENTION_NAMES)
    return mention_text


def post_triage_followup(suite_run, job_run, verdict: str) -> bool:
    """버튼 분류 후 원본 메시지 **쓰레드에** 후속 멘션을 남긴다.

    - FAILURE → 실패 담당자 멘션 + "장애 F/U 진행이 필요합니다."
    - SKIP    → 김태식·김도연 고정 멘션 + "자동화 테스트케이스 점검 요망".
    """
    ts = getattr(suite_run, "slack_message_ts", "") or ""
    channel = getattr(suite_run, "slack_channel_id", "") or ""
    if not (ts and channel):
        return False

    if verdict == "failure":
        mentions = _owner_mentions_for_job_run(job_run)
        head = (mentions + "\n") if mentions else ""
        text = f"{head}:rotating_light: *장애 F/U 진행이 필요합니다.*"
    elif verdict == "skip":
        mentions = _fixed_skip_mentions()
        head = (mentions + "\n") if mentions else ""
        text = f"{head}:wrench: *자동화 테스트케이스 점검 요망* — 자동화 판정 오류 의심 및 자동화 코드 이슈 추정"
    else:
        return False

    _slack_web().chat_postMessage(channel=channel, thread_ts=ts, text=text)
    return True


def update_suite_fail_message(suite_run) -> bool:
    """실패 알림 메시지를 현재 verdict 상태로 다시 그려 chat.update.

    발송 시 저장한 slack_message_ts/channel 이 있어야 갱신 가능.
    """
    ts = getattr(suite_run, "slack_message_ts", "") or ""
    channel = getattr(suite_run, "slack_channel_id", "") or ""
    if not (ts and channel):
        return False
    from apps.notifications.services import build_suite_fail_payload  # 지연 import (순환 회피)
    text, blocks = build_suite_fail_payload(suite_run)
    _slack_web().chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
    return True
