"""담당자 멘션 / 에러 발췌 / Slack 메시지 본문 빌더 헬퍼.

CSV 의 `fe_owner` / `be_owner` 가 "이동훈, 윤복영" 같은 콤마
다중값이라 발송 시점에 split → `apps.integrations.slack_users.find_user_id_by_name`
으로 Slack workspace 에서 자동 조회 → `<@U0XXX>` 멘션 문자열로 변환한다.
Slack 에서 매칭 못 한 이름은 `@이름` plain text 로 fallback (알림은 안 가지만
메시지 본문에는 등장).

`users.list` 호출은 1시간 LocMem 캐시.
"""

from __future__ import annotations

from typing import Iterable, Optional


SLACK_TEXT_LIMIT = 4000  # Slack message text 안전 한도


def _split_names(raw: str) -> list[str]:
    '''"이동훈, 윤복영" → ["이동훈", "윤복영"]. 공백/빈 토큰 정리.'''
    if not raw:
        return []
    parts = [p.strip() for p in str(raw).split(",")]
    return [p for p in parts if p]


def _names_for_status(owner, status_code: int | None) -> list[str]:
    """status_code 가 5xx 면 BE 담당자, 4xx 면 FE 담당자, 그 외엔 둘 다 union.

    비즈니스 룰: Status 가 4로 시작 → FE 담당자, 5로 시작 → BE 담당자.
    """
    if owner is None:
        return []
    fe = getattr(owner, "fe_owner", "") or ""
    be = getattr(owner, "be_owner", "") or ""

    if status_code is None:
        # union, dedupe 보존 순서
        seen: set[str] = set()
        out: list[str] = []
        for n in _split_names(fe) + _split_names(be):
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out
    if 500 <= status_code <= 599:
        return _split_names(be)
    if 400 <= status_code <= 499:
        return _split_names(fe)
    # 그 외 (2xx/3xx/None 외 비정상) — union
    seen2: set[str] = set()
    out2: list[str] = []
    for n in _split_names(fe) + _split_names(be):
        if n not in seen2:
            seen2.add(n)
            out2.append(n)
    return out2


def build_mentions(
    owners_with_status: Iterable[tuple[object, int | None]],
) -> tuple[str, list[str]]:
    """여러 (owner, status_code) 쌍에서 이름 모음 → Slack 자동 조회 → 멘션 문자열.

    Returns:
        (mention_text, unresolved_names)
        mention_text: "<@U01> <@U02> @이동훈" 같은 형태.
            Slack workspace 에서 매칭된 사람은 진짜 멘션(`<@U..>`),
            매칭 못 한 사람은 `@이름` plain text (메시지에는 보이지만 알림 X).
        unresolved_names: 매칭 못 한 이름 (로깅/디버깅용).
    """
    # 1) 이름 union (보존 순서)
    all_names: list[str] = []
    seen: set[str] = set()
    for owner, status in owners_with_status or []:
        for n in _names_for_status(owner, status):
            if n not in seen:
                seen.add(n)
                all_names.append(n)

    if not all_names:
        return "", []

    # 2) Slack workspace 에서 이름 → U_ID 자동 조회 (1시간 캐시)
    from apps.integrations.slack_users import find_user_id_by_name

    tokens: list[str] = []
    unresolved: list[str] = []
    for n in all_names:
        uid = find_user_id_by_name(n)
        if uid:
            tokens.append(f"<@{uid}>")
        else:
            tokens.append(f"@{n}")
            unresolved.append(n)
    return " ".join(tokens), unresolved


def mentions_from_names(names: Iterable[str]) -> tuple[str, list[str]]:
    """이름 리스트 → Slack 자동 조회 → 멘션 문자열.

    `build_mentions` 의 status-기반 분기 없이 단순히 주어진 이름들에 대해 멘션
    생성. 대체 담당자 (FallbackOwners) 처럼 status 무관하게 전부 호출하는 경우용.

    Returns:
        (mention_text, unresolved_names) — `build_mentions` 와 동일 인터페이스.
    """
    seen: set[str] = set()
    dedup: list[str] = []
    for n in names or []:
        if n and n not in seen:
            seen.add(n)
            dedup.append(n)
    if not dedup:
        return "", []

    from apps.integrations.slack_users import find_user_id_by_name

    tokens: list[str] = []
    unresolved: list[str] = []
    for n in dedup:
        uid = find_user_id_by_name(n)
        if uid:
            tokens.append(f"<@{uid}>")
        else:
            tokens.append(f"@{n}")
            unresolved.append(n)
    return " ".join(tokens), unresolved


def excerpt_error(text: str, *, max_chars: int = 1500) -> str:
    """긴 에러 본문 잘라내고 마지막에 "... (이하 N자 생략)" 안내.

    Slack code block (``` ... ```) 안전 처리: 본문에 백틱 3개 (```) 가 포함된
    경우 백틱을 zero-width-space + 백틱으로 escape 하여 code block 이 깨지지
    않도록 한다.
    """
    if not text:
        return ""
    s = str(text)
    truncated = False
    if len(s) > max_chars:
        omitted = len(s) - max_chars
        s = s[:max_chars]
        truncated = True
    # 백틱 3개 (```) escape — zero-width-space 삽입
    s = s.replace("```", "`​`​`")
    if truncated:
        s = s + f"\n... (이하 {omitted}자 생략)"
    return s


def build_error_followup_text(
    *,
    matches: list[dict],
    error_excerpt: str,
    portal_log_url: str,
    context_label: str,
    mention_text: str = "",
) -> str:
    """Slack 메시지 본문 (mrkdwn) 빌드 — 멘션 + 컨텍스트 + URL 표 + 에러 발췌 + 로그 링크.

    Args:
        matches: [{"url":..., "method":..., "status_code":..., "owner":<ApiEndpointOwner|None>}, ...]
        error_excerpt: 이미 `excerpt_error` 로 잘려 있는 본문.
        portal_log_url: 포털 에러 로그 페이지의 절대 URL.
        context_label: "Suite: 결제 회귀 / Job: foo" 같은 라벨.
        mention_text: 메시지 첫 라인 멘션 (없으면 생략).

    Returns:
        Slack mrkdwn 텍스트. 4000자 한도 내로 안전 절단.
    """
    lines: list[str] = []
    if mention_text:
        lines.append(mention_text)
    lines.append(":rotating_light: *이슈 F/U 요청* — " + (context_label or ""))

    # URL 표 (최대 5개)
    if matches:
        lines.append("")
        lines.append("*실패 호출*")
        for m in matches[:5]:
            method = (m.get("method") or "?").upper()
            url = m.get("url") or ""
            status = m.get("status_code")
            status_s = f"{status}" if status else "?"
            owner = m.get("owner")
            owner_label = ""
            if owner is not None:
                if 400 <= int(status or 0) < 500:
                    owner_label = getattr(owner, "fe_owner", "") or ""
                elif 500 <= int(status or 0) < 600:
                    owner_label = getattr(owner, "be_owner", "") or ""
                else:
                    fe = getattr(owner, "fe_owner", "") or ""
                    be = getattr(owner, "be_owner", "") or ""
                    owner_label = ", ".join([x for x in [fe, be] if x])
            owner_part = f" → {owner_label}" if owner_label else ""
            lines.append(f"• `{method}` `{url}` [{status_s}]{owner_part}")
        if len(matches) > 5:
            lines.append(f"  ... 외 {len(matches) - 5}건")

    if error_excerpt:
        lines.append("")
        lines.append("*에러 발췌*")
        lines.append("```")
        lines.append(error_excerpt)
        lines.append("```")

    if portal_log_url:
        lines.append("")
        lines.append(f":mag: <{portal_log_url}|포털 로그에서 보기>")

    text = "\n".join(lines)
    if len(text) > SLACK_TEXT_LIMIT:
        # code block 안전 종료를 위해 자르고 ``` 보강
        head = text[: SLACK_TEXT_LIMIT - 200]
        return head + "\n```\n... (메시지 길이 초과로 잘림)"
    return text
