"""포털 User → Slack member ID 자동 매핑 (`User.slack_user_id` 백필).

Slack Triage 버튼(Failure/Skip) 권한 검사가 `slack_user_id` 매핑을 요구하므로,
`users.list` 의 profile.email 로 포털 User(이메일 일치)를 Slack ID 에 연결한다.

- 매칭 키 = **이메일**(소문자). 봇에 `users:read.email` scope 필요 (있음).
- 기본은 비어있는 유저만 채운다(overwrite=False) — 수동 지정값 보존.
- `sync_owners_from_google_sheet` 끝에서 best-effort 호출 → 동기화 때마다 신규 유저 자동 반영.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model

from . import slack_users

logger = logging.getLogger(__name__)


def build_email_index() -> dict:
    """Slack workspace 의 email(lower) → member_id 인덱스 (deleted/bot 제외)."""
    token = slack_users._bot_token()
    if not token:
        return {}
    index: dict = {}
    cursor = ""
    for _ in range(50):  # 페이지 안전 상한
        body = slack_users._fetch_page(token, cursor)
        if not body.get("ok"):
            logger.warning("users.list 실패: %s", body.get("error"))
            break
        for m in body.get("members") or []:
            if m.get("deleted") or m.get("is_bot"):
                continue
            email = ((m.get("profile") or {}).get("email") or "").strip().lower()
            if email and m.get("id"):
                index[email] = m["id"]
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return index


def backfill_user_slack_ids(*, overwrite: bool = False) -> dict:
    """활성 User 중 이메일이 있고 slack_user_id 가 비어있는(또는 overwrite) 유저를 매핑.

    반환: {"mapped": n, "already": n, "unmatched": [username...], "index_size": n}
    """
    User = get_user_model()
    index = build_email_index()
    if not index:
        return {"mapped": 0, "already": 0, "unmatched": [], "index_size": 0, "reason": "no_index"}

    mapped = 0
    already = 0
    unmatched: list = []
    for u in User.objects.filter(is_active=True).exclude(email=""):
        if u.slack_user_id and not overwrite:
            already += 1
            continue
        sid = index.get((u.email or "").strip().lower())
        if not sid:
            unmatched.append(u.username)
            continue
        if u.slack_user_id != sid:
            u.slack_user_id = sid
            u.save(update_fields=["slack_user_id"])
            mapped += 1
            logger.info("slack_user_id 매핑: %s(%s) -> %s", u.username, u.email, sid)
        else:
            already += 1
    return {"mapped": mapped, "already": already, "unmatched": unmatched, "index_size": len(index)}
