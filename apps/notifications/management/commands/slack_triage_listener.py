"""Slack Socket Mode 리스너 — 실패 알림 Failure/Skip 버튼 클릭 수신·처리.

인트라넷이라 인바운드 공개 URL 대신 **Socket Mode**(앱→Slack 아웃바운드 WebSocket)로
버튼 클릭(block_actions)을 받는다. 상시 프로세스로 띄워야 함(systemd 권장):

    .venv/bin/python manage.py slack_triage_listener

필요 env: SLACK_BOT_TOKEN(xoxb-, chat:write) + SLACK_APP_TOKEN(xapp-, connections:write).
Slack 앱: Socket Mode ON + Interactivity ON.
"""
import json
import logging
import os
import time
import urllib.request

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

_REASON_MSG = {
    "forbidden": ":no_entry: 쓰기 권한이 없어 처리할 수 없습니다 (Viewer).",
    "schedule_forbidden": ":no_entry: 스케줄 중단/재활성화는 김태식·김도연 만 사용할 수 있습니다.",
    "unmapped_user": ":warning: Slack 계정이 포털 사용자와 연결돼 있지 않습니다. 관리자에게 `slack_user_id` 등록을 요청하세요.",
    "not_found": ":question: 대상 Job Run 을 찾을 수 없습니다 (오래된 메시지일 수 있음).",
    "no_schedule": ":information_source: 이 실행은 스케줄과 연결돼 있지 않습니다 (수동 실행).",
    "unknown_action": ":question: 알 수 없는 동작입니다.",
}


def _ephemeral(response_url: str, text: str) -> None:
    """response_url 로 클릭한 사람에게만 보이는 임시 메시지."""
    if not response_url:
        return
    try:
        data = json.dumps({"response_type": "ephemeral", "replace_original": False, "text": text}).encode()
        req = urllib.request.Request(response_url, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        logger.debug("ephemeral 응답 실패: %s", exc)


def dispatch_interaction(payload: dict) -> None:
    """block_actions payload 1건 처리 — 테스트에서 직접 호출 가능."""
    from apps.notifications.triage import (
        apply_triage_action, apply_schedule_action,
        ACTION_FAILURE, ACTION_SKIP, SCHEDULE_ACTIONS,
    )
    actions = payload.get("actions") or []
    slack_user = (payload.get("user") or {}).get("id", "")
    response_url = payload.get("response_url", "")
    for a in actions:
        aid = a.get("action_id")
        if aid in (ACTION_FAILURE, ACTION_SKIP):
            res = apply_triage_action(aid, a.get("value", ""), slack_user)
        elif aid in SCHEDULE_ACTIONS:
            res = apply_schedule_action(aid, a.get("value", ""), slack_user)
        else:
            continue
        if not res.get("ok"):
            _ephemeral(response_url, _REASON_MSG.get(res.get("reason"), ":x: 처리 실패."))


class Command(BaseCommand):
    help = "Slack Socket Mode 리스너 — 실패 알림 Failure/Skip 버튼 클릭 수신·처리 (상시 실행)."

    def handle(self, *args, **options):
        from apps.notifications.services import _bot_token
        app_token = (os.getenv("SLACK_APP_TOKEN") or "").strip()
        bot_token = _bot_token()
        if not app_token.startswith("xapp-"):
            raise CommandError("SLACK_APP_TOKEN(xapp-) 미설정 — Socket Mode 리스너를 켤 수 없습니다.")
        if not bot_token:
            raise CommandError("SLACK_BOT_TOKEN 미설정.")

        try:
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
            from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: F401
            from slack_sdk.socket_mode.response import SocketModeResponse
        except ImportError as e:
            raise CommandError(f"slack_sdk 미설치 ({e}). `pip install -r requirements.txt`.")

        client = SocketModeClient(app_token=app_token, web_client=WebClient(token=bot_token))

        def _on_request(cli, req):
            # 3초 내 ack 필수 — 먼저 응답하고 처리.
            try:
                cli.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("ack 실패: %s", exc)
            if req.type != "interactive":
                return
            try:
                dispatch_interaction(req.payload or {})
            except Exception as exc:  # noqa: BLE001
                logger.exception("triage 처리 오류: %s", exc)

        client.socket_mode_request_listeners.append(_on_request)
        self.stdout.write("Slack Triage 리스너 연결 중...")
        client.connect()
        self.stdout.write(self.style.SUCCESS("연결됨 — Failure/Skip 버튼 클릭 대기 (Ctrl+C 종료)"))
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            self.stdout.write("종료합니다.")
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
