"""API 엔드포인트 담당자 매핑용 서비스 — CSV 임포트 등.

`improvement.md` §3 의 알고리즘 그대로 구현.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Union

from django.db import transaction

from .models import ApiEndpointOwner

logger = logging.getLogger(__name__)


# --- 헤더 매핑 (CSV 한국어 헤더 → 모델 필드) ----------------------------------
# 2026-05-20: `4xx 담당자` → `FE 담당자`, `5xx 담당자` → `BE 담당자` 로 컬럼명 변경
# + 담당팀 컬럼 제거 (담당자만 관리). 옛 헤더는 alias 로 받음 — 이전 CSV 가 깨지지 않도록.
HEADER_MAP: dict[str, str] = {
    "레포": "repo",
    "카테고리": "category",
    "영역": "area",
    "Method": "method",
    "Endpoint": "endpoint",
    "FE 담당자": "fe_owner",
    "BE 담당자": "be_owner",
    "매핑 앱": "mapped_app",
    "사용 횟수": "usage_count",
    "step_ids": "step_ids",
}

# 옛 한국어 헤더 → 새 한국어 헤더. 임포트 시 입력 CSV 의 헤더를 normalize 한다.
# 옛 `*담당팀` 컬럼이 들어와도 헤더 검증 통과를 위해 새 이름으로 alias 하되, 본문은
# `_build_defaults` 가 HEADER_MAP 에 없는 컬럼은 자동으로 무시한다.
_LEGACY_HEADER_ALIASES: dict[str, str] = {
    "400에러 담당자": "FE 담당자",
    "500에러 담당자": "BE 담당자",
    # 담당팀 컬럼은 무시 — 들어와도 _build_defaults 가 모델에 매핑 안 시킴.
    "400에러 담당팀": "_legacy_team_4xx",
    "500에러 담당팀": "_legacy_team_5xx",
    "FE 담당팀": "_legacy_team_4xx",
    "BE 담당팀": "_legacy_team_5xx",
}


def _normalize_header_key(h: str) -> str:
    """헤더 비교용 키 — 모든 공백 제거. `FE 담당자` ↔ `FE담당자` 동치 처리.

    사용자 CSV 마다 띄어쓰기가 들쭉날쭉이라 (예: `FE 담당자` vs `FE담당자` vs
    `400에러 담당자` vs `400에러 담당자`) 정확 일치만 보면 헤더 누락 오인이 잦다.
    """
    if not h:
        return ""
    return "".join(h.split())


def _canonical_header(name: str) -> str | None:
    """입력 헤더를 HEADER_MAP 의 canonical 형태로 변환. 매칭 없으면 None."""
    if not name:
        return None
    nk = _normalize_header_key(name)
    for canonical in HEADER_MAP:
        if _normalize_header_key(canonical) == nk:
            return canonical
    for old, new in _LEGACY_HEADER_ALIASES.items():
        if _normalize_header_key(old) == nk:
            return new
    return None

# 식별 키 — `(method, endpoint)` 만으로 unique. repo 는 update 대상 (덮어쓰기).
# 같은 endpoint 가 다른 repo 라벨로 임포트되어도 새 row 를 만들지 않고 기존 row
# 를 덮어쓴다 (사용자 요청, 2026-05-20).
KEY_FIELDS = ("method", "endpoint")

# errors 가 이 수치를 넘으면 즉시 rollback (안전망)
MAX_ROW_ERRORS = 5


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped_manual: int = 0
    deleted_stale: int = 0
    errors: list[dict] = field(default_factory=list)


def _open_csv(csv_file: Union[str, Path, IO]) -> tuple[io.TextIOBase, bool]:
    """csv_file 이 path 면 열어주고, file-like 면 utf-8-sig 로 wrap.

    Returns (text_stream, should_close).
    """
    if isinstance(csv_file, (str, Path)):
        f = open(csv_file, "r", encoding="utf-8-sig", newline="")
        return f, True
    # file-like — 이미 text mode 일 수도, binary mode 일 수도 있음
    # binary 면 TextIOWrapper, text 면 그대로
    raw = csv_file
    # 'read' 결과로 분기하는 건 무리이므로 모드를 확인
    mode = getattr(raw, "mode", "")
    if "b" in mode:
        return io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""), False
    # mode 가 없거나 text — 그대로 사용. 단 BOM 제거를 위해 한 번 read 후 StringIO 로 감쌈.
    try:
        content = raw.read()
    except Exception:
        return raw, False
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    elif content.startswith("﻿"):
        content = content.lstrip("﻿")
    return io.StringIO(content), False


def _row_is_blank(row: dict) -> bool:
    """모든 컬럼이 공백/빈 문자열이면 True."""
    return not any((v or "").strip() for v in row.values())


def _build_defaults(row: dict, row_no: int) -> dict:
    """CSV row → ApiEndpointOwner defaults dict.

    KEY_FIELDS (repo/method/endpoint) 는 제외하고 나머지만.
    usage_count 는 int 변환 (실패 시 ValueError raise — 호출부에서 errors 누적).
    """
    defaults: dict = {}
    for ko_header, field_name in HEADER_MAP.items():
        if field_name in KEY_FIELDS:
            continue
        raw_val = (row.get(ko_header) or "").strip()
        if field_name == "usage_count":
            if raw_val == "":
                defaults[field_name] = 0
            else:
                try:
                    defaults[field_name] = int(raw_val)
                except ValueError:
                    raise ValueError(
                        f"row {row_no}: '사용 횟수' 가 정수가 아님: {raw_val!r}"
                    )
        else:
            defaults[field_name] = raw_val
    defaults["is_imported"] = True
    return defaults


def import_api_endpoint_owners(
    csv_file: Union[str, Path, IO],
    *,
    user=None,
    dry_run: bool = False,
    remove_stale: bool = True,
    source_label: str = "CSV",
) -> ImportResult:
    """CSV 한 번 읽어서 ApiEndpointOwner 일괄 import.

    `improvement.md` §3.1, §3.2, §3.3 의 사양 그대로.

    - csv_file: path 또는 file-like (binary/text 모두 허용).
    - user: ActivityLog 기록용. None 도 허용.
    - dry_run=True 면 트랜잭션 rollback.
    - remove_stale=True 면 CSV 에 없는 imported row 들을 삭제.
    - source_label: ActivityLog 표기용 출처 라벨 ("CSV" / "Google Sheet").
    """
    result = ImportResult()

    stream, should_close = _open_csv(csv_file)
    try:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames or []
        # BOM 잔여 정리 (utf-8-sig 로 못 잡힌 경우 대비)
        fieldnames = [(fn or "").lstrip("﻿") for fn in fieldnames]
        # 헤더 정규화 — 띄어쓰기 무관 (`FE 담당자` ↔ `FE담당자`) + 옛 헤더 alias
        # (`400에러 담당자` → `FE 담당자` 등) 모두 처리.
        fieldnames = [_canonical_header(fn) or fn for fn in fieldnames]
        reader.fieldnames = fieldnames

        # 헤더 검증
        missing = [h for h in HEADER_MAP.keys() if h not in fieldnames]
        if missing:
            result.errors.append(
                {
                    "row": 0,
                    "error": f"CSV 헤더 누락: {', '.join(missing)}",
                }
            )
            return result

        seen_keys: set[tuple[str, str]] = set()

        with transaction.atomic():
            for idx, row in enumerate(reader, start=2):  # 헤더가 1행
                if _row_is_blank(row):
                    continue

                repo = (row.get("레포") or "").strip()
                method = (row.get("Method") or "").strip()
                endpoint = (row.get("Endpoint") or "").strip()

                # method, endpoint 가 식별 키. repo 는 update 대상 (없어도 OK).
                if not (method and endpoint):
                    result.errors.append(
                        {
                            "row": idx,
                            "error": (
                                f"필수 키 필드 비어있음 "
                                f"(method={method!r}, endpoint={endpoint!r})"
                            ),
                        }
                    )
                    if len(result.errors) >= MAX_ROW_ERRORS:
                        transaction.set_rollback(True)
                        return result
                    continue

                try:
                    defaults = _build_defaults(row, idx)
                except ValueError as e:
                    result.errors.append({"row": idx, "error": str(e)})
                    if len(result.errors) >= MAX_ROW_ERRORS:
                        transaction.set_rollback(True)
                        return result
                    continue

                # repo 도 defaults 로 (덮어쓰기 대상)
                defaults["repo"] = repo
                seen_keys.add((method, endpoint))

                try:
                    existing = ApiEndpointOwner.objects.filter(
                        method=method, endpoint=endpoint
                    ).first()
                    if existing is not None and not existing.is_imported:
                        # 수동 row 는 import 가 건드리지 않음
                        result.skipped_manual += 1
                        continue

                    obj, created = ApiEndpointOwner.objects.update_or_create(
                        method=method,
                        endpoint=endpoint,
                        defaults=defaults,
                    )
                    if created:
                        result.created += 1
                    else:
                        result.updated += 1
                except Exception as e:  # pragma: no cover — 예외 안전망
                    result.errors.append({"row": idx, "error": str(e)})
                    if len(result.errors) >= MAX_ROW_ERRORS:
                        transaction.set_rollback(True)
                        return result
                    continue

            # remove stale — CSV 에 등장하지 않은 imported row 삭제
            if remove_stale and not result.errors:
                qs = ApiEndpointOwner.objects.filter(is_imported=True)
                stale = []
                for obj in qs.only("id", "method", "endpoint"):
                    if (obj.method, obj.endpoint) not in seen_keys:
                        stale.append(obj.id)
                if stale:
                    deleted, _ = ApiEndpointOwner.objects.filter(id__in=stale).delete()
                    result.deleted_stale = deleted

            if dry_run:
                transaction.set_rollback(True)
                return result
    finally:
        if should_close:
            try:
                stream.close()
            except Exception:
                pass

    # 성공 시 ActivityLog 기록 (dry_run 이면 건너뜀 — 위에서 이미 return 됨)
    try:
        from apps.activitylog.models import ActivityLog

        ActivityLog.log(
            user=user,
            action=ActivityLog.Action.API_OWNER_IMPORTED,
            target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
            target_name=source_label,
            detail=(
                f"생성 {result.created} · 수정 {result.updated} · "
                f"수동 보존 {result.skipped_manual} · 삭제 {result.deleted_stale} · "
                f"오류 {len(result.errors)}"
            ),
        )
    except Exception:  # pragma: no cover — 로깅 실패가 import 결과를 망치면 안 됨
        pass

    return result


def sync_owners_from_google_sheet(
    *,
    user=None,
    dry_run: bool = False,
    spreadsheet_id=None,
) -> ImportResult:
    """Google Sheets 의 담당자 마스터를 ApiEndpointOwner 로 동기화 (full mirror).

    CSV 업로드를 대체한다. 시트를 Drive export(text/csv)로 받아 기존 CSV 임포트
    파이프라인에 그대로 흘려보낸다. **항상 remove_stale=True (full mirror)** — 시트가
    단일 진실 소스이므로 시트에 없는 imported row 는 삭제한다. 수동 row
    (`is_imported=False`) 는 import 파이프라인이 보존한다.

    - 시트 조회/인증/설정 실패 시 `google_sheets.GoogleSheetSyncError` 를 그대로
      전파한다 (호출부 — view / 관리 명령 — 에서 사용자에게 메시지로 변환).
    - dry_run=True 면 DB 변경 없이 결과만 계산.
    """
    from . import google_sheets

    csv_bytes = google_sheets.export_sheet_csv(spreadsheet_id)

    result = import_api_endpoint_owners(
        io.BytesIO(csv_bytes),
        user=user,
        dry_run=dry_run,
        remove_stale=True,  # full mirror — 시트 기준 삭제
        source_label="Google Sheet",
    )

    if not dry_run:
        try:
            from .url_matcher import invalidate_match_cache

            invalidate_match_cache()
        except Exception:  # pragma: no cover
            pass
        # 담당자 시트 동기화할 때마다 신규 유저의 slack_user_id 도 자동 백필
        # (Triage 버튼 권한 매핑용, best-effort — 실패해도 owner 동기화엔 영향 없음).
        try:
            from .slack_user_sync import backfill_user_slack_ids

            sres = backfill_user_slack_ids()
            if sres.get("mapped"):
                logger.info("slack_user_id 백필: %s명 신규 매핑", sres["mapped"])
        except Exception as exc:  # pragma: no cover
            logger.warning("slack_user_id 백필 실패(무시): %s", exc)

    return result


# ---------------------------------------------------------------------------
# 역방향 기록 (write-back) — 수동 등록 담당자를 Google Sheet 에 append
# ---------------------------------------------------------------------------
# 동기화는 시트→DB 단방향(full mirror) 이지만, 포털에서 수동 등록한 담당자를 팀이
# 보는 시트에도 남기기 위해 등록 직후 best-effort 로 append 한다. 다음 동기화 때
# 그 행이 시트에서 다시 읽혀 같은 (method, endpoint) row 가 is_imported=True 로
# 흡수되므로 중복 row 는 생기지 않는다.


def is_owner_writeback_enabled() -> bool:
    """역방향 기록 on/off — 기본 off(opt-in). 운영에서 `.env` 에
    `GSHEET_OWNERS_WRITEBACK=1` 로 켠다. (시트 편집 권한 + spreadsheets 스코프 필요.)
    """
    return (os.getenv("GSHEET_OWNERS_WRITEBACK", "").strip().lower()
            in ("1", "true", "yes", "on"))


def _is_test_run() -> bool:
    """테스트 실행 중인지 — 운영 시트에 실수로 쓰지 않기 위한 안전 가드."""
    return ("test" in sys.argv) or bool(os.getenv("PYTEST_CURRENT_TEST"))


def _owner_to_sheet_values(owner: ApiEndpointOwner) -> dict:
    """ApiEndpointOwner → 시트 컬럼(모델 필드명 키) 값 dict."""
    return {
        "repo": owner.repo,
        "category": owner.category,
        "area": owner.area,
        "method": owner.method,
        "endpoint": owner.endpoint,
        "fe_owner": owner.fe_owner,
        "be_owner": owner.be_owner,
        "mapped_app": owner.mapped_app,
        "usage_count": owner.usage_count,
        "step_ids": owner.step_ids,
    }


def writeback_owner_to_sheet_async(owner: ApiEndpointOwner) -> None:
    """수동 등록 담당자 1건을 Google Sheet 에 역방향 append (비동기·best-effort).

    실패해도 등록 자체엔 영향 없음(로그 warning). off(기본) 거나 테스트 실행 중이면
    아무것도 안 한다.
    """
    if not is_owner_writeback_enabled() or _is_test_run():
        return
    values = _owner_to_sheet_values(owner)
    label = f"{owner.method} {owner.endpoint}"

    def _worker():
        try:
            from . import google_sheets
            google_sheets.append_owner_row(values)
            logger.info("담당자 시트 역방향 기록 OK: %s", label)
        except Exception as exc:  # 네트워크/권한/스코프 — 등록은 이미 끝났으므로 silent
            logger.warning("담당자 시트 역방향 기록 실패 (%s): %s", label, exc)

    threading.Thread(target=_worker, daemon=True).start()
