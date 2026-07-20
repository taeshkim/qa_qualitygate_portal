"""Google Sheets → 담당자 마스터 동기화의 외부 연동 계층.

담당자 관리 화면(`/accounts/api-owners/`)의 마스터 데이터를 Google Sheets 와
동기화하기 위해, 대상 스프레드시트를 **Drive API `files.export(mimeType=text/csv)`**
로 CSV 로 받아온다. 받아온 CSV 는 기존 CSV 임포트 파이프라인
(`services.import_api_endpoint_owners`) 이 그대로 파싱한다 — 시트 컬럼이 CSV
헤더와 동일하기 때문(레포/카테고리/영역/Method/Endpoint/FE 담당자/BE 담당자/
매핑 앱/사용 횟수/step_ids).

인증: **서비스 계정** 또는 **Workload Identity Federation(WIF)/ADC**. 자격증명을
`.env` 로 지정한다 (slack_users.py 가 SLACK_BOT_TOKEN 을 os.getenv 로 직접 읽는 것과
같은 컨벤션). 우선순위:
  1. `GOOGLE_CREDENTIALS_FILE` — 자격증명 파일 경로. 파일의 `type` 으로 종류를 자동
     판별한다 (service_account / authorized_user / external_account). WIF(외부계정)·
     ADC 는 이 경로로 지정한다 — `google.auth.load_credentials_from_file` 가 처리.
  2. `GOOGLE_SA_JSON_B64`      — 서비스 계정 JSON 을 base64 인코딩한 문자열 (.env 인용 안전)
  3. `GOOGLE_SA_JSON`          — 서비스 계정 JSON 원문 (한 줄)
  4. `GOOGLE_SA_FILE`          — 서비스 계정 JSON 키 파일 경로

대상 시트 ID 는 `GSHEET_OWNERS_SPREADSHEET_ID`.

대상 시트를 **호출 주체(principal)에 '뷰어'로 공유**해야 export 가 동작한다 —
서비스 계정이면 `...@....iam.gserviceaccount.com`, WIF(external_account)면
임퍼소네이트하는 서비스 계정 이메일(`service_account_impersonation_url` 에 박힌 주소).
Drive export 는 **첫 번째 탭(gid=0)** 만 CSV 로 내보낸다 —
현재 마스터 시트는 단일 탭이라 충분하다. 멀티 탭에서 특정 gid 를 지정해야 하면
Google Sheets API(`spreadsheets.values.get`) 로 전환해야 한다.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# 시트 1개를 CSV 로 export 하는 데 필요한 최소 scope.
_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Drive v3 export 엔드포인트. fileId 는 format 으로 채운다.
_EXPORT_URL = "https://www.googleapis.com/drive/v3/files/{file_id}/export"

# 외부 호출 타임아웃 (Jenkins 8s / Argo 6s 와 비슷한 보수적 값).
_HTTP_TIMEOUT_SECS = 30


class GoogleSheetSyncError(Exception):
    """설정 누락 / 인증 실패 / 시트 조회 실패 등 동기화 불가 상황."""


def get_spreadsheet_id() -> str:
    """동기화 대상 스프레드시트 ID. 미설정이면 예외."""
    sid = (os.getenv("GSHEET_OWNERS_SPREADSHEET_ID") or "").strip()
    if not sid:
        raise GoogleSheetSyncError(
            "GSHEET_OWNERS_SPREADSHEET_ID 미설정 (.env 확인) — 동기화 대상 시트 ID 가 필요합니다."
        )
    return sid


def load_service_account_info() -> dict:
    """서비스 계정 JSON 을 dict 로 로드. B64 → JSON → 파일경로 순.

    어느 것도 없으면 `GoogleSheetSyncError`.
    """
    b64 = (os.getenv("GOOGLE_SA_JSON_B64") or "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64)
        except (binascii.Error, ValueError) as e:
            raise GoogleSheetSyncError(f"GOOGLE_SA_JSON_B64 base64 디코딩 실패: {e}")
        return _parse_sa_json(raw, source="GOOGLE_SA_JSON_B64")

    raw_json = (os.getenv("GOOGLE_SA_JSON") or "").strip()
    if raw_json:
        return _parse_sa_json(raw_json, source="GOOGLE_SA_JSON")

    path = (os.getenv("GOOGLE_SA_FILE") or "").strip()
    if path:
        try:
            with open(path, "rb") as f:
                return _parse_sa_json(f.read(), source=f"GOOGLE_SA_FILE({path})")
        except OSError as e:
            raise GoogleSheetSyncError(f"GOOGLE_SA_FILE 읽기 실패 ({path}): {e}")

    raise GoogleSheetSyncError(
        "서비스 계정 자격증명 미설정 — .env 에 GOOGLE_SA_JSON_B64 "
        "(또는 GOOGLE_SA_JSON / GOOGLE_SA_FILE) 중 하나를 채워주세요."
    )


def _parse_sa_json(raw, *, source: str) -> dict:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise GoogleSheetSyncError(f"{source} UTF-8 디코딩 실패: {e}")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GoogleSheetSyncError(f"{source} JSON 파싱 실패: {e}")
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise GoogleSheetSyncError(
            f"{source} 가 서비스 계정 키(JSON, type=service_account)가 아닙니다."
        )
    return info


def _resolve_credential_file() -> Optional[str]:
    """`GOOGLE_CREDENTIALS_FILE` 경로 (WIF/ADC/SA 공용). 미설정이면 None."""
    return (os.getenv("GOOGLE_CREDENTIALS_FILE") or "").strip() or None


def _impersonation_email(url: str) -> Optional[str]:
    """`service_account_impersonation_url` 에서 임퍼소네이트 SA 이메일 추출.

    예: ``.../serviceAccounts/sa@proj.iam.gserviceaccount.com:generateAccessToken``
    """
    m = re.search(r"/serviceAccounts/([^:/]+@[^:/]+)", url or "")
    return m.group(1) if m else None


def get_service_account_email() -> Optional[str]:
    """시트를 공유해야 할 주체(principal) 이메일 (공유 안내용). 미설정이면 None.

    - 서비스 계정 → ``client_email``
    - external_account(WIF) → 임퍼소네이트하는 서비스 계정 이메일
    - authorized_user 등 → None (사용자 계정이라 공유 주체가 사용자 본인)
    """
    cred_file = _resolve_credential_file()
    if cred_file:
        if not os.path.exists(cred_file):
            return None
        try:
            with open(cred_file, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(d, dict):
            return None
        if d.get("type") == "service_account":
            return d.get("client_email")
        if d.get("type") == "external_account":
            return _impersonation_email(d.get("service_account_impersonation_url"))
        return None
    try:
        return load_service_account_info().get("client_email")
    except GoogleSheetSyncError:
        return None


def _load_credentials():
    """동기화에 쓸 google-auth Credentials 를 반환.

    1. `GOOGLE_CREDENTIALS_FILE` — 파일 type 자동 판별 (service_account /
       authorized_user / external_account=WIF). load_credentials_from_file 가 처리.
    2. 인라인 서비스 계정 — `load_service_account_info()` (B64 / JSON / SA_FILE).
    """
    cred_file = _resolve_credential_file()
    if cred_file:
        if not os.path.exists(cred_file):
            raise GoogleSheetSyncError(
                f"GOOGLE_CREDENTIALS_FILE 경로에 파일이 없습니다: {cred_file}"
            )
        try:
            import google.auth
        except ImportError as e:  # pragma: no cover — requirements 에 포함됨
            raise GoogleSheetSyncError(
                f"google-auth 미설치 ({e}). `pip install -r requirements.txt` 필요."
            )
        try:
            creds, _ = google.auth.load_credentials_from_file(
                cred_file, scopes=_SCOPES
            )
        except Exception as e:  # 파일 형식/타입 오류, 외부계정 설정 오류 등
            raise GoogleSheetSyncError(f"자격증명 파일 로드 실패 ({cred_file}): {e}")
        return creds

    # 인라인 서비스 계정 (미설정이면 여기서 GoogleSheetSyncError)
    info = load_service_account_info()
    try:
        from google.oauth2 import service_account
    except ImportError as e:  # pragma: no cover — requirements 에 포함됨
        raise GoogleSheetSyncError(
            f"google-auth 미설치 ({e}). `pip install -r requirements.txt` 필요."
        )
    try:
        return service_account.Credentials.from_service_account_info(
            info, scopes=_SCOPES
        )
    except Exception as e:  # 키 형식 오류 등
        raise GoogleSheetSyncError(f"서비스 계정 자격증명 생성 실패: {e}")


def _build_session():
    """자격증명으로 토큰 자동 갱신되는 AuthorizedSession 반환."""
    # import 를 함수 안에 둬서, 동기화를 안 쓰는 환경에서 google-auth/requests 가
    # 없어도 모듈 import 자체는 깨지지 않게 한다.
    try:
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as e:  # pragma: no cover — requirements 에 포함됨
        raise GoogleSheetSyncError(
            f"google-auth/requests 미설치 ({e}). `pip install -r requirements.txt` 필요."
        )
    return AuthorizedSession(_load_credentials())


def export_sheet_csv(spreadsheet_id: Optional[str] = None) -> bytes:
    """대상 스프레드시트의 첫 번째 탭을 CSV(UTF-8 bytes)로 export.

    실패 시 `GoogleSheetSyncError` 를 던진다 (네트워크/권한/미공유 등).
    """
    file_id = (spreadsheet_id or get_spreadsheet_id()).strip()
    session = _build_session()

    url = _EXPORT_URL.format(file_id=file_id)
    try:
        resp = session.get(
            url,
            params={"mimeType": "text/csv"},
            timeout=_HTTP_TIMEOUT_SECS,
        )
    except Exception as e:  # requests 예외, 토큰 갱신 실패 등
        raise GoogleSheetSyncError(f"Google Drive export 요청 실패: {e}")

    if resp.status_code != 200:
        snippet = (resp.text or "")[:300]
        hint = ""
        if resp.status_code in (403, 404):
            hint = (
                " — 시트가 서비스 계정에 '뷰어'로 공유됐는지, "
                "Drive API 활성화 / 시트 ID 가 맞는지 확인하세요."
            )
        raise GoogleSheetSyncError(
            f"Google Drive export 실패 (HTTP {resp.status_code}){hint} :: {snippet}"
        )

    content = resp.content or b""
    if not content.strip():
        raise GoogleSheetSyncError("Google Drive export 결과가 비어있습니다 (빈 시트?).")
    logger.info("Google Sheet export OK: %s bytes (file_id=%s)", len(content), file_id)
    return content
