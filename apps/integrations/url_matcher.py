"""URL → ApiEndpointOwner 매칭 엔진.

`improvement.md` §4 의 알고리즘 그대로 구현.

핵심 원칙(§4.0):
- 매칭은 path-only. host(`${BFF_URL}` / `bff-general-01...`)는 떼어내고 path만 본다.
- `ApiHostMapping` 은 동률 후보를 가리는 tie-breaker. 비어있어도 매칭은 동작.

공개 API:
- :func:`find_endpoint_owner` — 단일 (method, url) → ApiEndpointOwner | None
- :func:`extract_urls`        — Error Log 텍스트에서 URL/Method/Status 묶음 추출
- :func:`invalidate_match_cache` — 캐시 무효화 (signal에서 호출)
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import ApiEndpointOwner, ApiHostMapping


# ---------------------------------------------------------------------------
# 공개 데이터 클래스
# ---------------------------------------------------------------------------


@dataclass
class MatchedUrl:
    url: str
    method: Optional[str]
    count: int
    owner: Optional[ApiEndpointOwner]
    status_code: Optional[int] = None


# ---------------------------------------------------------------------------
# 캐시 키 / TTL
# ---------------------------------------------------------------------------

_OWNER_CACHE_KEY = "qgp:api_owner_patterns:v1"
_HOST_CACHE_KEY = "qgp:api_host_mappings:v1"
# LocMemCache 는 TTL 없어도 OK 지만 안전망으로 1시간.
_CACHE_TTL = 60 * 60


# ---------------------------------------------------------------------------
# 컴파일 / 인덱싱
# ---------------------------------------------------------------------------

# `${HOST_VAR}` prefix 추출 (endpoint 가 ${...} 로 시작하는 경우).
_HOST_VAR_PREFIX_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# path param `${PARAM}` 자리표시자 (호스트 변수 제거 후 path 안에서만 등장).
_PATH_PARAM_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _normalize_path(path: str) -> str:
    """path 정규화: 앞에 `/` 보장, 끝 `/` 제거 (단 path 가 `/` 단독이면 유지)."""
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return path


def _compile_endpoint(endpoint: str) -> tuple[re.Pattern, Optional[str], str, int]:
    """`endpoint` 문자열을 (pattern, host_var, raw_path, specificity) 로 변환.

    - host_var: `${BFF_URL}/foo` 같은 형식이면 'BFF_URL', 아니면 None.
    - raw_path: host 변수 제거 + query string 제거 + 끝 슬래시 정규화한 path.
    - specificity: raw_path 의 segment 중 `${...}` 가 없는(=리터럴) 개수.
    """
    raw = (endpoint or "").strip()
    host_var: Optional[str] = None

    # host 변수 prefix 떼어내기.
    m = _HOST_VAR_PREFIX_RE.match(raw)
    if m:
        host_var = m.group(1)
        raw = raw[m.end():]
        # `${HOST}` 만 있고 그 뒤가 없는 경우 raw 가 빈 문자열.

    # query string 제거.
    q_idx = raw.find("?")
    if q_idx >= 0:
        raw = raw[:q_idx]

    # fragment 제거 (혹시).
    h_idx = raw.find("#")
    if h_idx >= 0:
        raw = raw[:h_idx]

    raw_path = _normalize_path(raw)

    # ${PARAM} → [^/]+ 치환 (escape 전에 자리만 마킹 후 escape 후 복원).
    SENTINEL = "\x00PARAM\x00"
    template = _PATH_PARAM_RE.sub(SENTINEL, raw_path)
    escaped = re.escape(template).replace(re.escape(SENTINEL), r"[^/]+")
    pattern = re.compile(r"^" + escaped + r"$")

    # specificity = 리터럴 segment 수.
    segments = [s for s in raw_path.split("/") if s]
    specificity = sum(1 for s in segments if "${" not in s)

    return pattern, host_var, raw_path, specificity


def _build_owner_index() -> dict[str, list[dict]]:
    """모든 ApiEndpointOwner row 를 method 별 인덱스로 컴파일."""
    index: dict[str, list[dict]] = {}
    qs = ApiEndpointOwner.objects.all().only(
        "id", "method", "endpoint", "repo", "usage_count"
    )
    for owner in qs:
        method = (owner.method or "").upper().strip()
        try:
            pattern, host_var, raw_path, specificity = _compile_endpoint(owner.endpoint)
        except re.error:
            # 잘못된 endpoint 는 조용히 건너뜀 (운영 데이터 손상에 대한 방어).
            continue
        index.setdefault(method, []).append(
            {
                "pk": owner.pk,
                "pattern": pattern,
                "host_var": host_var,
                "raw_path": raw_path,
                "specificity": specificity,
                "repo": owner.repo or "",
                "usage_count": owner.usage_count or 0,
            }
        )
    return index


def _build_host_index() -> list[dict]:
    """모든 ApiHostMapping row 를 list 로 반환 (netloc 매칭에 사용)."""
    rows: list[dict] = []
    for h in ApiHostMapping.objects.all().only(
        "variable_name", "env", "host_pattern"
    ):
        pattern = (h.host_pattern or "").strip()
        rows.append(
            {
                "variable_name": (h.variable_name or "").strip(),
                "env": h.env,
                "host_pattern": pattern,
                "wildcards": pattern.count("*"),
            }
        )
    return rows


def _get_owner_index() -> dict[str, list[dict]]:
    cached = cache.get(_OWNER_CACHE_KEY)
    if cached is not None:
        return cached
    built = _build_owner_index()
    cache.set(_OWNER_CACHE_KEY, built, _CACHE_TTL)
    return built


def _get_host_index() -> list[dict]:
    cached = cache.get(_HOST_CACHE_KEY)
    if cached is not None:
        return cached
    built = _build_host_index()
    cache.set(_HOST_CACHE_KEY, built, _CACHE_TTL)
    return built


def invalidate_match_cache() -> None:
    """ApiEndpointOwner / ApiHostMapping 변경 시 호출."""
    cache.delete(_OWNER_CACHE_KEY)
    cache.delete(_HOST_CACHE_KEY)


# ---------------------------------------------------------------------------
# host_var 추론 (tie-breaker)
# ---------------------------------------------------------------------------


def _infer_host_var(netloc: str) -> Optional[str]:
    """netloc 으로부터 ApiHostMapping 의 variable_name 을 추론.

    매칭이 여러 개면 wildcards 가 적은(=더 정확한) 것 우선.
    아무것도 매칭 안 되면 None.
    """
    if not netloc:
        return None
    host_idx = _get_host_index()
    if not host_idx:
        return None
    best: Optional[dict] = None
    for row in host_idx:
        pat = row["host_pattern"]
        if not pat:
            continue
        if fnmatch.fnmatchcase(netloc, pat):
            if best is None or row["wildcards"] < best["wildcards"]:
                best = row
    return best["variable_name"] if best else None


# ---------------------------------------------------------------------------
# 동률 정렬 (improvement.md §4.2 단계 4)
# ---------------------------------------------------------------------------


def _repo_netloc_bonus(repo: str, netloc: str) -> int:
    """repo↔netloc 키워드 가점. 양쪽 다 있을 때만 +1."""
    if not repo or not netloc:
        return 0
    repo_u = repo.upper()
    netloc_l = netloc.lower()
    if "AM" in repo_u and "albamon" in netloc_l:
        return 1
    if "JK" in repo_u and "jobkorea" in netloc_l:
        return 1
    return 0


def _rank_key(cand: dict, inferred_host_var: Optional[str], netloc: str) -> tuple:
    """후보를 정렬할 키. tuple 작은게 우선이 되도록 음수/역순으로 구성.

    우선순위:
      1. specificity 큰 row 우선
      2. inferred_host_var 와 같은 host_var 우선
      3. host_var 채워진 row > None
      4. repo↔netloc 키워드 가점 큰 row
      5. usage_count 큰 row
      6. pk 안정 정렬
    """
    same_host_var = (
        1 if (inferred_host_var and cand["host_var"] == inferred_host_var) else 0
    )
    has_host_var = 1 if cand["host_var"] else 0
    bonus = _repo_netloc_bonus(cand["repo"], netloc)
    return (
        -cand["specificity"],
        -same_host_var,
        -has_host_var,
        -bonus,
        -cand["usage_count"],
        str(cand["pk"]),
    )


# ---------------------------------------------------------------------------
# 공개 함수 — find_endpoint_owner
# ---------------------------------------------------------------------------


def find_endpoint_owner(method: str, url: str) -> Optional[ApiEndpointOwner]:
    """단일 (method, url) → 매칭된 ApiEndpointOwner 인스턴스 or None.

    - method 가 빈 문자열/None 이면 GET 으로 가정.
    - url 은 `http(s)://...` 절대 URL 이거나 `/...` 절대 path.
    """
    method_n = (method or "GET").upper().strip() or "GET"

    parts = urlsplit(url or "")
    netloc = parts.netloc or ""
    path = parts.path or url or ""
    if "://" not in (url or "") and path == (url or ""):
        # urlsplit 이 `/foo` 같은 절대 path 를 path 로 잡아주지만
        # 일부 케이스(스킴 없는 host 같이 모호한 입력) 대비 보강.
        if path and not path.startswith("/"):
            path = "/" + path

    target_path = _normalize_path(path)

    index = _get_owner_index()
    pool = index.get(method_n, [])
    if not pool:
        return None

    candidates = [c for c in pool if c["pattern"].fullmatch(target_path)]
    if not candidates:
        return None

    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        inferred = _infer_host_var(netloc)
        candidates.sort(key=lambda c: _rank_key(c, inferred, netloc))
        chosen = candidates[0]

    try:
        return ApiEndpointOwner.objects.get(pk=chosen["pk"])
    except ApiEndpointOwner.DoesNotExist:
        # 캐시와 DB 가 어긋난 경우 — 캐시 비우고 None.
        invalidate_match_cache()
        return None


# ---------------------------------------------------------------------------
# extract_urls — Error Log 텍스트에서 URL/Method/Status 추출
# ---------------------------------------------------------------------------

# step 블록 구분 휴리스틱: `====...` / `@@@...` / 빈 줄 2개 연속.
# 주의: 반복 그룹 안에 또 + 를 두면 (예: (=+|@+){5,}) catastrophic backtracking 발생.
# 70자짜리 `===...` 라인을 만나면 무한 대기에 빠지므로 평탄한 형태로 작성.
_BLOCK_SEP_RE = re.compile(
    r"(?:^|\n)(?:={5,}|@{5,})\s*\n|\n\s*\n\s*\n", re.MULTILINE
)

# URL 라벨 형식 ("URL:" / "URL :")
_URL_LABEL_RE = re.compile(r"URL\s*:\s*(https?://\S+)", re.IGNORECASE)
# 라벨 없는 단독 URL — `://` 가 있어야 매칭.
_BARE_URL_RE = re.compile(r"(?<![\w:/])(https?://\S+)")

_METHOD_RE = re.compile(
    r"Method\s*:\s*([A-Za-z]+)", re.IGNORECASE
)
_STATUS_RE = re.compile(
    r"Status(?:\s*Code)?\s*:\s*(\d{3})", re.IGNORECASE
)


def _clean_url(raw: str) -> str:
    """URL 끝의 구두점/괄호/따옴표 제거."""
    # `https://foo.com/bar.` 같은 케이스에서 trailing punctuation 잘라냄.
    return raw.rstrip(".,;:!?)\"']”’")


def _extract_from_block(block: str) -> list[tuple[str, Optional[str], Optional[int]]]:
    """한 블록에서 (url, method, status) 튜플 목록을 추출.

    같은 블록 안의 첫 Method/Status 를 그 블록의 모든 URL 에 공유.
    """
    method: Optional[str] = None
    status: Optional[int] = None

    m = _METHOD_RE.search(block)
    if m:
        method = m.group(1).upper()
    s = _STATUS_RE.search(block)
    if s:
        try:
            status = int(s.group(1))
        except ValueError:
            status = None

    urls: list[str] = []
    seen: set[str] = set()

    for um in _URL_LABEL_RE.finditer(block):
        u = _clean_url(um.group(1))
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    # 라벨 없는 URL — 라벨로 이미 잡힌 것 제외.
    for bm in _BARE_URL_RE.finditer(block):
        u = _clean_url(bm.group(1))
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    return [(u, method, status) for u in urls]


def extract_urls(text: str) -> list[MatchedUrl]:
    """Error Log 텍스트에서 URL 라인을 추출하고 step 블록의 Method/Status 와 묶는다.

    - 같은 (method, url) 은 dedupe 하면서 count 증가.
    - 같은 url 의 status 가 여러 번 나오면 마지막 값 사용.
    - 결과는 count 내림차순 → url 사전순.
    - 각 MatchedUrl 에 owner 매칭 결과도 채워준다 (method 가 없으면 GET 가정).
    """
    if not text:
        return []

    # 블록 분리. 결과가 0 이면 텍스트 전체를 한 블록으로 처리.
    blocks = _BLOCK_SEP_RE.split(text)
    if not blocks:
        blocks = [text]

    # key = (method_or_None, url) → record
    records: dict[tuple[Optional[str], str], dict] = {}

    for block in blocks:
        if not block or not block.strip():
            continue
        for url, method, status in _extract_from_block(block):
            key = (method, url)
            rec = records.get(key)
            if rec is None:
                rec = {
                    "url": url,
                    "method": method,
                    "count": 0,
                    "status_code": None,
                }
                records[key] = rec
            rec["count"] += 1
            if status is not None:
                rec["status_code"] = status

    results: list[MatchedUrl] = []
    for rec in records.values():
        method_for_match = rec["method"] or "GET"
        owner = find_endpoint_owner(method_for_match, rec["url"])
        results.append(
            MatchedUrl(
                url=rec["url"],
                method=rec["method"],
                count=rec["count"],
                owner=owner,
                status_code=rec["status_code"],
            )
        )

    results.sort(key=lambda r: (-r.count, r.url))
    return results


# ---------------------------------------------------------------------------
# 시그널 — 캐시 무효화
# ---------------------------------------------------------------------------


@receiver(post_save, sender=ApiEndpointOwner)
@receiver(post_delete, sender=ApiEndpointOwner)
@receiver(post_save, sender=ApiHostMapping)
@receiver(post_delete, sender=ApiHostMapping)
def _invalidate_owner_cache(sender, **kwargs):  # pragma: no cover - 통합 검증
    invalidate_match_cache()
