"""Slack 알림 발송 서비스.

호출 측에서는 `send_event(event_type, run, **ctx)` 만 사용한다.

토큰: `.env` 의 `SLACK_BOT_TOKEN`
기본 채널: DB 의 SlackNotificationSettings.default_channel — 비어있으면 `.env` 의 `SLACK_DEFAULT_CHANNEL`
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import events as E
from .models import SlackNotificationSettings

logger = logging.getLogger(__name__)

SLACK_API_URL = "https://slack.com/api/chat.postMessage"
SLACK_TIMEOUT = 6  # seconds


# event_type → (settings 토글 필드, 색상, 이모지, 한글 라벨)
_EVENT_META = {
    E.RUN_STARTED: ("notify_run_started", "#3aa0ff", ":rocket:", "Run 시작"),
    E.RUN_COMPLETED: ("notify_run_completed", "#36a64f", ":white_check_mark:", "Run 완료"),
    E.RUN_FAILED: ("notify_run_failed", "#e01e5a", ":x:", "Run 실패"),
    E.RUN_CANCELLED: ("notify_run_cancelled", "#999999", ":no_entry_sign:", "Run 취소"),
    E.IMAGE_STARTED: ("notify_image_started", "#3aa0ff", ":hammer:", "이미지 빌드 시작"),
    E.IMAGE_COMPLETED: ("notify_image_completed", "#36a64f", ":package:", "이미지 빌드 완료"),
    E.IMAGE_FAILED: ("notify_image_failed", "#e01e5a", ":x:", "이미지 빌드 실패"),
    E.DEPLOY_STARTED: ("notify_deploy_started", "#3aa0ff", ":truck:", "검수대상 배포 시작"),
    E.DEPLOY_COMPLETED: ("notify_deploy_completed", "#36a64f", ":truck:", "검수대상 배포 완료"),
    E.DEPLOY_FAILED: ("notify_deploy_failed", "#e01e5a", ":x:", "검수대상 배포 실패"),
    E.PROD_STARTED: ("notify_prod_started", "#3aa0ff", ":ship:", "검수통과 배포 시작"),
    E.PROD_COMPLETED: ("notify_prod_completed", "#36a64f", ":ship:", "검수통과 배포 완료"),
    E.PROD_FAILED: ("notify_prod_failed", "#e01e5a", ":x:", "검수통과 배포 실패"),
    E.TEST_SUITE_STARTED: ("notify_test_suite_started", "#3aa0ff", ":test_tube:", "Test Suite 시작"),
    E.TEST_SUITE_PASSED: ("notify_test_suite_passed", "#36a64f", ":white_check_mark:", "Test Suite PASS"),
    E.TEST_SUITE_FAILED: ("notify_test_suite_failed", "#e01e5a", ":x:", "Test Suite FAIL"),
    E.SUITE_RUN_STARTED: ("notify_suite_run_started", "#3aa0ff", ":test_tube:", "Suite Run 시작"),
    E.SUITE_RUN_PASSED: ("notify_suite_run_passed", "#36a64f", ":white_check_mark:", "Suite Run PASS"),
    E.SUITE_RUN_FAILED: ("notify_suite_run_failed", "#e01e5a", ":x:", "Suite Run FAIL"),
    E.ARGOCD_FAILED: ("notify_argocd_failed", "#e01e5a", ":warning:", "Argo CD Sync 실패"),
}


def _bot_token() -> str:
    return (os.getenv("SLACK_BOT_TOKEN") or "").strip()


def _resolve_channel(settings: SlackNotificationSettings) -> str:
    if settings.default_channel:
        return settings.default_channel.strip()
    return (os.getenv("SLACK_DEFAULT_CHANNEL") or "").strip()


def _gate_label(gate) -> str:
    folder = getattr(gate, "folder", None)
    sub = getattr(gate, "subfolder", None)
    parts = []
    if folder:
        parts.append(folder.name)
    if sub:
        parts.append(sub.name)
    parts.append(gate.name)
    return " / ".join(parts)


def _base_url() -> str:
    return (os.getenv("QGP_BASE_URL") or "").rstrip("/")


def _run_url(run) -> Optional[str]:
    base = _base_url()
    if not base:
        return None
    return f"{base}/qualitygates/runs/{run.pk}/"


def _fmt_duration(seconds) -> str:
    """초 단위 숫자를 사람이 읽기 좋은 한국어 포맷으로."""
    if seconds is None:
        return ""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 0:
        return ""
    if s < 60:
        return f"{s}초"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}분 {sec}초" if sec else f"{m}분"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}시간 {m}분" if m else f"{h}시간"


def _fmt_ms(ms) -> str:
    if not ms:
        return ""
    return _fmt_duration(int(ms) / 1000)


def _stage_console_field(run, stage: str, base: str) -> Optional[dict]:
    """이미지 / 검수대상 / 검수통과 단계 콘솔 로그 링크 필드.

    포털의 단계 콘솔 뷰만 노출 (원본 Jenkins URL 은 의도적으로 제외 — 사용자가
    QG 포털 안에서 로그를 보도록 UX 통일).
    """
    if not base:
        return None
    portal = f"{base}/qualitygates/runs/{run.pk}/console/{stage}/"
    return {"title": "로그", "value": f"<{portal}|콘솔 로그 보기>", "short": False}


def _qg_event_fields(event_type: str, run, ctx: dict) -> list:
    """QualityGateRun 이벤트별 필드 빌더. 직접 모델 필드를 읽어 메시지를 풍부하게."""
    fields: list = []
    base = _base_url()
    extra = ctx.get("extra") if isinstance(ctx.get("extra"), dict) else {}
    gate = getattr(run, "gate", None)

    triggered_by = getattr(run, "triggered_by", None)
    if triggered_by and event_type in (E.RUN_STARTED, E.RUN_CANCELLED):
        fields.append({"title": "실행자", "value": str(triggered_by), "short": True})

    # ---- Run 단위 ----
    if event_type == E.RUN_STARTED:
        if gate:
            opts = []
            if getattr(gate, "has_image_build", False):
                opts.append("이미지 빌드")
            if getattr(gate, "parallel_test", False):
                opts.append("병렬 테스트")
            if getattr(gate, "has_prod_deploy", False):
                opts.append("검수통과 배포")
            if opts:
                fields.append({"title": "옵션", "value": " · ".join(opts), "short": True})

    elif event_type == E.RUN_COMPLETED:
        if run.created_at:
            from django.utils import timezone
            elapsed = (timezone.now() - run.created_at).total_seconds()
            fields.append({"title": "총 소요 시간", "value": _fmt_duration(elapsed), "short": True})
        # 실행 모드
        if gate and getattr(gate, "has_prod_deploy", False):
            fields.append({"title": "Prod 배포", "value": "포함", "short": True})

    elif event_type == E.RUN_FAILED:
        stage = extra.get("stage")
        if stage:
            stage_label = {
                "image": "이미지 빌드",
                "deploy": "검수대상 배포",
                "test": "테스트",
                "prod": "검수통과 배포",
            }.get(stage, stage)
            fields.append({"title": "실패 단계", "value": stage_label, "short": True})
            failed_suites = extra.get("failed_suites")
            if stage == "test" and failed_suites:
                fields.append({"title": "실패 Suite 수", "value": str(failed_suites), "short": True})
        if run.created_at:
            from django.utils import timezone
            elapsed = (timezone.now() - run.created_at).total_seconds()
            fields.append({"title": "경과 시간", "value": _fmt_duration(elapsed), "short": True})

    elif event_type == E.RUN_CANCELLED:
        cancelled_at = extra.get("cancelled_at")
        if cancelled_at:
            fields.append({"title": "취소 시점 상태", "value": str(cancelled_at), "short": True})

    # ---- 이미지 빌드 ----
    elif event_type == E.IMAGE_STARTED:
        if run.image_git_branch:
            fields.append({"title": "Branch", "value": run.image_git_branch, "short": True})
        if run.image_git_commit:
            fields.append({"title": "Commit", "value": run.image_git_commit[:8], "short": True})

    elif event_type == E.IMAGE_COMPLETED:
        if run.image_git_branch:
            fields.append({"title": "Branch", "value": run.image_git_branch, "short": True})
        if run.image_git_commit:
            fields.append({"title": "Commit", "value": run.image_git_commit[:8], "short": True})
        if run.image_duration_ms:
            fields.append({"title": "소요 시간", "value": _fmt_ms(run.image_duration_ms), "short": True})
        link = _stage_console_field(run, "image", base)
        if link:
            fields.append(link)

    elif event_type == E.IMAGE_FAILED:
        if run.image_git_branch:
            fields.append({"title": "Branch", "value": run.image_git_branch, "short": True})
        if run.image_git_commit:
            fields.append({"title": "Commit", "value": run.image_git_commit[:8], "short": True})
        link = _stage_console_field(run, "image", base)
        if link:
            fields.append(link)

    # ---- 검수대상 배포 ----
    elif event_type == E.DEPLOY_STARTED:
        if run.image_git_branch:
            fields.append({"title": "Branch", "value": run.image_git_branch, "short": True})
        if run.image_git_commit:
            fields.append({"title": "Commit", "value": run.image_git_commit[:8], "short": True})

    elif event_type == E.DEPLOY_COMPLETED:
        if run.deploy_duration_ms:
            fields.append({"title": "소요 시간", "value": _fmt_ms(run.deploy_duration_ms), "short": True})
        if run.deploy_argocd_health_status:
            fields.append({"title": "Argo CD", "value": run.deploy_argocd_health_status, "short": True})

    elif event_type == E.DEPLOY_FAILED:
        link = _stage_console_field(run, "deploy", base)
        if link:
            fields.append(link)
        if run.deploy_argocd_sync_error:
            snippet = run.deploy_argocd_sync_error[:300]
            fields.append({"title": "Argo CD 오류", "value": snippet, "short": False})

    # ---- 검수통과 배포 ----
    elif event_type == E.PROD_STARTED:
        if run.image_git_branch:
            fields.append({"title": "Branch", "value": run.image_git_branch, "short": True})
        if run.image_git_commit:
            fields.append({"title": "Commit", "value": run.image_git_commit[:8], "short": True})

    elif event_type == E.PROD_COMPLETED:
        if run.prod_duration_ms:
            fields.append({"title": "소요 시간", "value": _fmt_ms(run.prod_duration_ms), "short": True})
        if run.prod_argocd_health_status:
            fields.append({"title": "Argo CD", "value": run.prod_argocd_health_status, "short": True})

    elif event_type == E.PROD_FAILED:
        link = _stage_console_field(run, "prod", base)
        if link:
            fields.append(link)
        if run.prod_argocd_sync_error:
            snippet = run.prod_argocd_sync_error[:300]
            fields.append({"title": "Argo CD 오류", "value": snippet, "short": False})

    # ---- 파이프라인 내부 Test Suite ----
    elif event_type in (E.TEST_SUITE_STARTED, E.TEST_SUITE_PASSED, E.TEST_SUITE_FAILED):
        suite_name = ctx.get("suite_name")
        if suite_name:
            fields.append({"title": "Test Suite", "value": str(suite_name), "short": True})
        if event_type == E.TEST_SUITE_FAILED:
            job_run_id = ctx.get("job_run_id")
            if job_run_id and base:
                portal = f"{base}/suites/job-runs/{job_run_id}/console/"
                fields.append({
                    "title": "로그",
                    "value": f"<{portal}|콘솔 로그 보기>",
                    "short": False,
                })
            # 담당자 멘션 + 에러 발췌 + 포털 에러 로그 링크 (silent on failure)
            followup = _build_followup_for_job_run(
                job_run_id=ctx.get("job_run_id"),
                context_label_extra=f"Test Suite: {suite_name}" if suite_name else "",
            )
            if followup:
                fields.append({"title": "이슈 F/U", "value": followup, "short": False})

    # ---- Argo CD ----
    elif event_type == E.ARGOCD_FAILED:
        stage = extra.get("stage")
        if stage:
            stage_label = {"deploy": "검수대상", "prod": "검수통과"}.get(stage, stage)
            fields.append({"title": "단계", "value": stage_label, "short": True})

    return fields


def _suite_event_fields(event_type: str, suite_run, ctx: dict) -> list:
    """단독 TestSuiteRun 이벤트별 필드 빌더."""
    fields: list = []
    base = _base_url()

    suite = getattr(suite_run, "suite", None)
    if suite:
        fields.append({"title": "Suite", "value": str(suite.name), "short": True})

    triggered_by = getattr(suite_run, "triggered_by", None)
    if triggered_by:
        fields.append({"title": "실행자", "value": str(triggered_by), "short": True})

    total = suite_run.total_jobs or 0
    passed = suite_run.passed_jobs or 0
    failed = suite_run.failed_jobs or 0

    if event_type == E.SUITE_RUN_STARTED:
        if total:
            fields.append({"title": "Job 수", "value": str(total), "short": True})

    elif event_type == E.SUITE_RUN_PASSED:
        fields.append({"title": "결과", "value": f"{passed}/{total} PASS", "short": True})
        if suite_run.completed_at and suite_run.started_at:
            elapsed = (suite_run.completed_at - suite_run.started_at).total_seconds()
            fields.append({"title": "소요 시간", "value": _fmt_duration(elapsed), "short": True})

    elif event_type == E.SUITE_RUN_FAILED:
        fields.append({
            "title": "결과",
            "value": f"PASS {passed} / FAIL {failed} / 총 {total}",
            "short": True,
        })
        if suite_run.completed_at and suite_run.started_at:
            elapsed = (suite_run.completed_at - suite_run.started_at).total_seconds()
            fields.append({"title": "소요 시간", "value": _fmt_duration(elapsed), "short": True})
        failed_jrs: list = []
        if base:
            try:
                from apps.suites.models import TestSuiteJobRun
                failed_jrs = list(suite_run.job_runs.filter(
                    status__in=[TestSuiteJobRun.Status.FAILURE, TestSuiteJobRun.Status.ABORTED],
                ).select_related("job")[:5])
                if failed_jrs:
                    lines = []
                    for jr in failed_jrs:
                        name = jr.job.job_name if jr.job else "?"
                        portal = f"{base}/suites/job-runs/{jr.pk}/console/"
                        lines.append(f"• <{portal}|{name}>")
                    fields.append({
                        "title": "실패 Job 콘솔",
                        "value": "\n".join(lines),
                        "short": False,
                    })
            except Exception as exc:  # noqa: BLE001
                logger.debug("실패 job 목록 조회 실패: %s", exc)
        # 각 실패 job 별 담당자 멘션 + 에러 발췌 (1번째 job 만 — 메시지 길이 보호)
        for jr in failed_jrs[:1]:
            followup = _build_followup_for_job_run(
                job_run_id=str(jr.pk),
                context_label_extra=f"Suite: {suite.name if suite else '?'} / Job: {jr.job.job_name if jr.job else '?'}",
            )
            if followup:
                fields.append({"title": "이슈 F/U", "value": followup, "short": False})

    return fields


def _build_followup_for_job_run(job_run_id: Optional[str], context_label_extra: str = "") -> str:
    """TestSuiteJobRun 의 console_output 으로 owner 매칭 + 에러 발췌 + 멘션 텍스트 생성.

    실패해도 silent — 메시지에 안 들어가도 자동 알림 자체는 진행돼야 한다.
    Slack attachment field `value` 안에 들어가도 적당하도록 텍스트 길이를 보수적으로 잡는다.
    """
    if not job_run_id:
        return ""
    try:
        from apps.suites.models import TestSuiteJobRun
        from apps.integrations.url_matcher import extract_urls
        from apps.integrations.log_errors import extract_error_log
        from .owner_mentions import (
            build_error_followup_text,
            build_mentions,
            excerpt_error,
        )

        jr = TestSuiteJobRun.objects.select_related(
            "job", "suite_run", "suite_run__suite"
        ).get(pk=job_run_id)
        error_log = extract_error_log(jr.console_output or "")
        if not error_log:
            return ""
        matched = extract_urls(error_log)
        # owner 매칭 + 해당 status 의 담당자 이름이 실제로 있는 URL 만 정상 경로로.
        # 매칭은 됐지만 fe_owner/be_owner 가 빈 문자열인 owner row 는 사실상 "담당자
        # 없음" 과 동일하므로 fallback 처리한다 (2026-05-20 사용자 보고).
        from .owner_mentions import _names_for_status
        owners_with_status = [
            (m.owner, m.status_code)
            for m in matched
            if m.owner is not None and _names_for_status(m.owner, m.status_code)
        ]

        excerpt = excerpt_error(error_log, max_chars=1000)
        base = _base_url()
        portal_log_url = (
            f"{base}/suites/job-runs/{jr.pk}/error-log/" if base else ""
        )

        if owners_with_status:
            # 정상 경로 — 매칭된 owner 에게 멘션
            mention_text, _unresolved = build_mentions(owners_with_status)
            matches_for_text = [
                {
                    "url": m.url,
                    "method": m.method,
                    "status_code": m.status_code,
                    "owner": m.owner,
                }
                for m in matched
                if m.owner is not None and _names_for_status(m.owner, m.status_code)
            ][:5]
            return build_error_followup_text(
                matches=matches_for_text,
                error_excerpt=excerpt,
                portal_log_url=portal_log_url,
                context_label=context_label_extra,
                mention_text=mention_text,
            )

        # Fallback — 매칭된 owner 가 0건이거나 매칭은 됐지만 status 별 담당자가
        # 비어있음 → 대체 담당자(FallbackOwners) 사용.
        from apps.integrations.models import FallbackOwners
        from .owner_mentions import mentions_from_names

        names = FallbackOwners.get_solo().names()
        if not names:
            return ""  # 대체 담당자도 없으면 followup 생략

        mention_text, _ = mentions_from_names(names)
        # 매칭 안 된 URL 일부도 본문에 노출 (owner=None)
        matches_for_text = [
            {
                "url": m.url,
                "method": m.method,
                "status_code": m.status_code,
                "owner": None,
            }
            for m in matched[:5]
        ]
        label = (
            (context_label_extra or "")
            + " — 담당자 매칭 없음, 대체 담당자로 지정"
        ).strip(" —")
        return build_error_followup_text(
            matches=matches_for_text,
            error_excerpt=excerpt,
            portal_log_url=portal_log_url,
            context_label=label,
            mention_text=mention_text,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Slack followup 빌드 실패 (job_run=%s): %s", job_run_id, exc)
        return ""


def _post_message(channel: str, text: str, attachment: Optional[dict] = None) -> bool:
    token = _bot_token()
    if not token:
        logger.warning("Slack 알림 전송 스킵 — SLACK_BOT_TOKEN 미설정")
        return False
    if not channel:
        logger.warning("Slack 알림 전송 스킵 — channel 미지정")
        return False

    payload = {"channel": channel, "text": text}
    if attachment is not None:
        payload["attachments"] = [attachment]

    body_bytes = json.dumps(payload).encode("utf-8")
    req = Request(
        SLACK_API_URL,
        data=body_bytes,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=SLACK_TIMEOUT) as resp:
            raw = resp.read()
    except HTTPError as exc:
        logger.warning("Slack 알림 HTTP 오류: %s %s", exc.code, exc.reason)
        return False
    except URLError as exc:
        logger.warning("Slack 알림 전송 실패 (network): %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Slack 알림 전송 실패: %s", exc)
        return False

    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("Slack 알림 응답 JSON 파싱 실패: %s", raw[:200])
        return False

    if not body.get("ok"):
        logger.warning("Slack 알림 응답 오류: %s", body.get("error") or body)
        return False
    return True


def send_test_message(channel: Optional[str] = None) -> tuple[bool, str]:
    """관리 화면의 '테스트 메시지 전송' 버튼용. 토글 무시하고 강제 발송."""
    token = _bot_token()
    if not token:
        return False, ".env 의 SLACK_BOT_TOKEN 이 설정되지 않았습니다."

    settings_obj = SlackNotificationSettings.get_solo()
    target = (channel or "").strip() or _resolve_channel(settings_obj)
    if not target:
        return False, "기본 채널이 비어 있습니다 (.env 의 SLACK_DEFAULT_CHANNEL 또는 어드민 화면에서 설정)."

    text = ":white_check_mark: QualityGate Portal — Slack 알림 테스트 메시지입니다."
    if _post_message(target, text):
        return True, f"채널 {target} 으로 테스트 메시지 전송 완료."
    return False, "전송에 실패했습니다. 서버 로그를 확인해주세요 (토큰 권한 / 채널명 확인)."


def send_event(event_type: str, run, **ctx) -> bool:
    """파이프라인 상태 전이 시점에 호출.

    Args:
        event_type: events.py 의 상수 중 하나
        run: QualityGateRun 인스턴스
        **ctx: 메시지에 포함할 추가 컨텍스트 (예: suite_name, error_message 등)

    Returns:
        실제 전송 성공 여부 (False 면 비활성/토글 OFF/전송 실패 — 호출 측은 무시)
    """
    meta = _EVENT_META.get(event_type)
    if meta is None:
        logger.debug("Slack 알림 — 알 수 없는 event_type: %s", event_type)
        return False
    toggle_field, color, emoji, label = meta

    settings_obj = SlackNotificationSettings.get_solo()
    if not settings_obj.enabled:
        return False
    if not getattr(settings_obj, toggle_field, False):
        return False

    channel = _resolve_channel(settings_obj)
    if not channel:
        return False

    gate = getattr(run, "gate", None)
    if gate is None:
        return False
    gate_label = _gate_label(gate)
    seq = getattr(run, "seq_no", "?")
    title = f"{emoji} *{label}* — `{gate_label}` #{seq}"

    fields = _qg_event_fields(event_type, run, ctx)

    error_message = ctx.get("error_message")
    if error_message:
        snippet = str(error_message)
        if len(snippet) > 500:
            snippet = snippet[:500] + "..."
        fields.append({"title": "오류", "value": snippet, "short": False})

    run_url = _run_url(run)
    attachment = {
        "color": color,
        "fields": fields,
        "fallback": f"{label} — {gate_label} #{seq}",
        "mrkdwn_in": ["fields"],
    }
    if run_url:
        attachment["title"] = f"Run #{seq}"
        attachment["title_link"] = run_url

    return _post_message(channel, title, attachment)


def send_event_async(event_type: str, run, **ctx) -> None:
    """폴링/요청 흐름을 막지 않도록 데몬 스레드에서 발송한다.

    Slack API 호출은 보통 100~300ms 지만 네트워크 지연 시 timeout(6s) 까지
    호출자가 대기할 수 있다 — 파이프라인 폴링 함수에서는 이 wrapper 를 사용.
    """
    run_pk = getattr(run, "pk", None)
    if run_pk is None:
        return

    def _worker():
        try:
            from .models import SlackNotificationSettings  # noqa: F401  (django ready)
            from apps.qualitygates.models import QualityGateRun
            fresh_run = QualityGateRun.objects.select_related(
                "gate", "gate__folder", "gate__subfolder",
            ).get(pk=run_pk)
            send_event(event_type, fresh_run, **ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack 비동기 알림 실패 (%s): %s", event_type, exc)

    threading.Thread(target=_worker, daemon=True).start()


def send_suite_run_event(event_type: str, suite_run, **ctx) -> bool:
    """단독 Test Suite Run (apps/suites) 용 알림.

    QualityGateRun 이 아니라 TestSuiteRun 을 받는다. 토글/채널/토큰 처리는
    `send_event` 와 동일하지만 메시지 본문은 suite_run 컨텍스트로 구성한다.
    """
    meta = _EVENT_META.get(event_type)
    if meta is None:
        logger.debug("Slack 알림 — 알 수 없는 event_type (suite): %s", event_type)
        return False
    toggle_field, color, emoji, label = meta

    settings_obj = SlackNotificationSettings.get_solo()
    if not settings_obj.enabled:
        return False
    if not getattr(settings_obj, toggle_field, False):
        return False

    channel = _resolve_channel(settings_obj)
    if not channel:
        return False

    suite = getattr(suite_run, "suite", None)
    suite_name = suite.name if suite else "?"
    title = f"{emoji} *{label}* — `{suite_name}`"

    fields = _suite_event_fields(event_type, suite_run, ctx)

    error_message = ctx.get("error_message")
    if error_message:
        snippet = str(error_message)
        if len(snippet) > 500:
            snippet = snippet[:500] + "..."
        fields.append({"title": "오류", "value": snippet, "short": False})

    base = _base_url()
    run_url = f"{base}/suites/runs/{suite_run.pk}/" if base else None
    attachment = {
        "color": color,
        "fields": fields,
        "fallback": f"{label} — {suite_name}",
        "mrkdwn_in": ["fields"],
    }
    if run_url:
        attachment["title"] = "Run 상세"
        attachment["title_link"] = run_url

    return _post_message(channel, title, attachment)


def send_suite_run_event_async(event_type: str, suite_run, **ctx) -> None:
    """`send_suite_run_event` 의 비동기 wrapper."""
    suite_run_pk = getattr(suite_run, "pk", None)
    if suite_run_pk is None:
        return

    def _worker():
        try:
            from apps.suites.models import TestSuiteRun
            fresh = TestSuiteRun.objects.select_related(
                "suite", "triggered_by",
            ).get(pk=suite_run_pk)
            send_suite_run_event(event_type, fresh, **ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack 비동기 알림 (suite) 실패 (%s): %s", event_type, exc)

    threading.Thread(target=_worker, daemon=True).start()
