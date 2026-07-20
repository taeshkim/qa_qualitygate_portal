"""Slack `users.list` 로 이름 → User ID 자동 매핑.

CSV 의 owner 이름(`이동훈, 윤복영` 등)을 Slack workspace 의 실 사용자 정보와
정확 일치로 매칭해 `<@U0XXX>` 멘션 문자열로 변환한다. 별도 매핑 테이블 없이
workspace 사용자 정보를 그대로 사용하므로 사람 입사/입사 시 별도 등록 불필요.

필요 scope: SLACK_BOT_TOKEN 에 `users:read` 추가 (기본 `chat:write` 외에).
없으면 빈 매핑이 반환되고, 호출 측은 `@이름` plain text 로 fallback.

캐싱:
- LocMem 1시간. workspace 수백~수천명 ÷ users.list 1회 호출 / 1h 정도면
  Slack rate limit (Tier 2, ~20 req/min) 부담 없다.
- 매칭 실패 시(token 없음/scope 부족/network)에는 캐시에 빈 dict 저장하지 않고
  바로 빈 dict 반환 — 토큰 추가 직후 갱신을 위해.
- 강제 갱신: `invalidate_user_cache()` (Admin 화면 버튼이 호출 가능).

매칭 키 (등록 순서 = 약 → 강, 충돌 시 마지막 등록이 이긴다):
  1. name (legacy username, `@username` 의 username 부분)
  2. profile.real_name (예: "장호철")
  3. profile.display_name (사용자가 설정한 표시명)
  4. profile.real_name_normalized
  5. profile.display_name_normalized

5개 모두 같은 U_ID 로 인덱싱돼 어느 필드 기준이든 매칭 가능. 동명이인은 첫
매치 사용 (workspace 멤버 순서 의존). 매칭 실패한 이름은 호출 측이 `@이름`
plain text 로 fallback.
"""
from __future__ import annotations

import json
import logging
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.cache import cache

logger = logging.getLogger(__name__)

SLACK_USERS_LIST_URL = "https://slack.com/api/users.list"
SLACK_TIMEOUT = 8
CACHE_KEY = "qgp:slack_users:v1"
LAST_ERROR_KEY = "qgp:slack_users:last_error:v1"
CACHE_TTL = 3600  # 1h
PAGE_LIMIT = 200  # users.list 최대 limit (Slack 권장)
MAX_PAGES = 20  # 4000명 workspace 까지 대응 (안전 상한)


def _set_last_error(msg: str) -> None:
    """마지막 호출 실패 사유를 캐시에 기록 (디버그 페이지가 surfaced 하기 위함)."""
    if msg:
        cache.set(LAST_ERROR_KEY, msg, timeout=CACHE_TTL)
    else:
        cache.delete(LAST_ERROR_KEY)


def get_last_error() -> str:
    return cache.get(LAST_ERROR_KEY) or ""


def _bot_token() -> str:
    return (os.getenv("SLACK_BOT_TOKEN") or "").strip()


def _fetch_page(token: str, cursor: str = "") -> dict:
    params = {"limit": str(PAGE_LIMIT)}
    if cursor:
        params["cursor"] = cursor
    url = f"{SLACK_USERS_LIST_URL}?{urlencode(params)}"
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=SLACK_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


# 표시명에서 `이름(팀명)` / `이름 (Backend)` / `이름[부서]` 형태의 접미사 제거.
# Slack 워크스페이스 관습 — display_name 끝에 `(엔지니어팀)` 같은 식별 정보가
# 붙은 케이스가 흔하다 (실 사례: "장호철(talentclient eng팀)"). CSV 의 owner
# 컬럼은 보통 이름만 가지므로 앞부분도 별도 키로 등록해 자동 매칭이 동작하게 한다.
_SUFFIX_TRIM_RE = re.compile(r"\s*[\(\[\{<].*$")


def _name_head(raw: str) -> str:
    """`이름(팀명)` → `이름`. 접미사가 없거나 잘리는 게 없으면 입력과 동일.

    잘릴 부분이 처음부터 시작하면 (예: `(팀) 이름`) 빈 문자열 반환 — 호출 측이
    drop 한다.
    """
    if not raw:
        return ""
    return _SUFFIX_TRIM_RE.sub("", raw).strip()


# Slack 표시명 prefix 로 흔히 붙는 상태/근태 키워드 처리.
# 운영 관습: `휴가중_박세건`, `재택 이동훈`, `행사_윤복영`, `교육중-홍길동` 처럼
# 상태를 이름 앞에 표기하는 케이스가 흔하다. CSV 의 owner 컬럼은 보통 이름만
# 가지므로 prefix 를 떼어낸 변형도 키로 등록해 자동 매칭이 동작하게 한다.
#
# 화이트리스트 (예: {"휴가중", "재택", ...}) 방식은 새 상태 키워드가 등장할
# 때마다 코드 수정이 필요해 깨지기 쉽다. 대신 **한글 prefix + separator +
# 한글 이름** 패턴을 generic 으로 처리한다 — 어떤 한글 단어든 prefix 로
# 인식되어 새 키워드 추가 없이 자동 매칭이 동작한다.
#
# 패턴: `^[가-힣]+[\s_·\-:|]+([가-힣].+)$`
# - 시작에 한글 1+ 자 prefix (상태/근태 키워드)
# - separator 1+ 자 (공백 / `_` / `·` / `-` / `:` / `|`)
# - 그 뒤 한글로 시작하는 본문 (실제 이름)
#
# 영문 이름 (`Lee Donghoon`) 은 prefix 가 한글이 아니므로 매칭 안 됨 — 의도된
# 동작. 본명에 공백/언더스코어가 들어간 한글 이름 (`홍 길동`) 은 운영상 거의
# 없어 무시. prefix 가 중첩된 케이스 (`재택_미팅중_홍길동`) 는 while 루프로
# 반복 적용해 끝까지 떼어낸다.
_STATUS_PREFIX_RE = re.compile(r"^[가-힣]+[\s_·\-:|]+([가-힣].+)$")


def _strip_status_prefix(raw: str) -> str:
    """`휴가중_박세건` / `재택 이동훈` / `행사_윤복영` 형태에서 상태 prefix 제거.

    한글 prefix + separator + 한글 이름 패턴을 generic 으로 처리 — 어떤
    한글 단어든 prefix 로 인식. 중첩 prefix (`재택_미팅중_홍길동`) 까지
    반복 제거. 매칭 없으면 입력 그대로 반환. 빈 입력은 빈 문자열.
    """
    if not raw:
        return ""
    s = raw.strip()
    while True:
        m = _STATUS_PREFIX_RE.match(s)
        if not m:
            break
        s = m.group(1).strip()
    return s


def _build_name_index(members: list) -> dict[str, str]:
    """`members` 리스트 → {name_key.lower(): U_ID} 매핑.

    deleted / bot / app 계정 제외. 각 멤버당 다음 5 × 4 변형 모두 키로 등록:
    - 5 개 raw 후보 (name / real_name / display_name + normalized 변형)
    - 각 raw 에 대해 4 가지 변형: 원본 / 접미사 (`(팀)`) 제거 / 상태 prefix
      (`휴가중_`) 제거 / 둘 다 제거.
    """
    mapping: dict[str, str] = {}
    for m in members:
        if m.get("deleted") or m.get("is_bot") or m.get("is_app_user"):
            continue
        uid = m.get("id")
        if not uid:
            continue
        profile = m.get("profile") or {}
        for raw in (
            m.get("name") or "",
            profile.get("real_name") or "",
            profile.get("real_name_normalized") or "",
            profile.get("display_name") or "",
            profile.get("display_name_normalized") or "",
        ):
            head = _name_head(raw)
            for candidate in (
                raw,
                head,
                _strip_status_prefix(raw),
                _strip_status_prefix(head),
            ):
                k = (candidate or "").strip().lower()
                if k:
                    mapping[k] = uid
    return mapping


def _fetch_all_users() -> dict[str, str]:
    """Workspace 전체 사용자를 페이지네이션 돌며 수집 → 이름 인덱스.

    실패 사유는 `LAST_ERROR_KEY` 캐시에 기록 — 디버그 페이지에서 surface.
    """
    token = _bot_token()
    if not token:
        msg = "SLACK_BOT_TOKEN 미설정 (.env 확인)"
        logger.debug("Slack users.list 스킵 — %s", msg)
        _set_last_error(msg)
        return {}

    mapping: dict[str, str] = {}
    cursor = ""
    for _ in range(MAX_PAGES):
        try:
            body = _fetch_page(token, cursor)
        except (HTTPError, URLError, ValueError, TimeoutError) as exc:
            msg = f"HTTP/네트워크 오류: {exc}"
            logger.warning("Slack users.list 호출 실패: %s", exc)
            _set_last_error(msg)
            return mapping

        if not body.get("ok"):
            err = body.get("error") or "unknown"
            if err == "missing_scope":
                msg = (
                    "missing_scope — SLACK_BOT_TOKEN 에 `users:read` 권한이 없습니다. "
                    "Slack 앱 설정 → OAuth & Permissions 에서 추가한 뒤 반드시 "
                    "'Reinstall to Workspace' 1회 클릭해야 토큰에 반영됩니다."
                )
            elif err in ("not_authed", "invalid_auth", "token_revoked"):
                msg = f"인증 오류 ({err}) — SLACK_BOT_TOKEN 유효성/재설치 확인 필요."
            else:
                msg = f"Slack API 응답 오류: {err}"
            logger.warning("Slack users.list 응답 오류: %s", err)
            _set_last_error(msg)
            return mapping

        members = body.get("members") or []
        page_map = _build_name_index(members)
        # 충돌 시 page 후순위가 이기도록 update — _build_name_index 의 우선순위는
        # member 내부 후보 순서로 처리됐고, 페이지 간에는 보통 영향 없음.
        mapping.update(page_map)

        cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break

    _set_last_error("")  # 성공 → 이전 에러 클리어
    return mapping


def get_user_index(*, force_refresh: bool = False) -> dict[str, str]:
    """이름 인덱스 조회. 캐시 hit 면 즉시 반환."""
    if not force_refresh:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached
    mapping = _fetch_all_users()
    if mapping:
        cache.set(CACHE_KEY, mapping, timeout=CACHE_TTL)
    return mapping


def find_user_id_by_name(name: str) -> str | None:
    """이름으로 U_ID 검색. 매칭 없으면 None.

    trim + lower 한 키로 비교. workspace 의 display_name / real_name / legacy
    username 5개 후보를 모두 키로 인덱싱했으므로 사용자가 어느 필드에 실명을
    설정해도 매칭 가능.
    """
    if not name:
        return None
    return get_user_index().get(name.strip().lower())


def invalidate_user_cache() -> None:
    """수동 캐시 무효화 (사람 입퇴사 직후 갱신용)."""
    cache.delete(CACHE_KEY)
