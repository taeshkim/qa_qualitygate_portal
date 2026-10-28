"""TAP(tapilot) API 클라이언트 — UI Test Automation 탭 연동.

TAP 은 같은 호스트에서 운영되는 FastAPI 앱(https://tapilot.jkintra.net)으로,
Playwright 기반 UI 자동화 실행을 담당한다. 포털은 이 클라이언트로
Suite 목록 싱크 / 실행 / 진행 폴링 / 취소 / 케이스별 결과 조회를 한다.
(스펙 실측: docs/prd_ui_test_automation.md §2, openapi: /api/v2/openapi.json)

인증: `Authorization: Bearer tap_...` — TAP 호스트 `manage.py issue_api_key` 로
발급한 키를 포털 `.env` 의 `TAPILOT_API_KEY` 에 저장(평문은 .env 에만).

모든 호출은 짧은 timeout + `TapError` 로 격리 — TAP 이 죽어도 포털 화면/
cron 이 막히지 않게 한다.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10  # TAP 은 동일 호스트라 왕복이 짧다 — 지연 시 빠르게 포기


class TapError(Exception):
    """TAP API 호출 실패 (연결/HTTP/응답 형식). status 속성에 HTTP 코드(있으면)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TapClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.getenv("TAPILOT_API_BASE", "https://tapilot.jkintra.net")).rstrip("/")
        self.api_key = api_key or os.getenv("TAPILOT_API_KEY", "")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _request(self, method: str, path: str, **kw):
        url = f"{self.base_url}/api/v2{path}"
        headers = kw.pop("headers", {})
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = requests.request(
                method, url, headers=headers, timeout=kw.pop("timeout", DEFAULT_TIMEOUT), **kw,
            )
        except requests.RequestException as e:
            raise TapError(f"TAP 연결 실패: {e}") from e
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str(resp.json())[:200]
            except Exception:
                detail = resp.text[:200]
            raise TapError(f"TAP HTTP {resp.status_code}: {detail}", status=resp.status_code)
        return resp

    # ── Suites ──
    def list_suites(self) -> list[dict]:
        return self._request("GET", "/suites/").json()

    def suite_detail(self, tap_suite_id: int) -> dict:
        return self._request("GET", f"/suites/{tap_suite_id}/").json()

    def run_suite(self, tap_suite_id: int) -> dict:
        """Suite 실행 → {session_id, queued, queue_position, run_env, excluded_case_ids, tap_url}

        TAP 이 실행 준비(TestRail 런 시트 생성 등)를 동기로 하느라 10초를 넘길 수
        있어 트리거만 넉넉한 timeout — 실측 2026-07-28: 기본 10초에서 타임아웃 났고
        서버는 세션을 정상 생성(402)했음.
        """
        return self._request("POST", f"/suites/{tap_suite_id}/run/", timeout=60).json()

    # ── Runs ──
    def run_status(self, session_id: int) -> tuple[dict, int]:
        """진행 폴링 → (RunStatusOut, poll_after_seconds).

        RunStatusOut: {status, terminal, total_cases, completed_cases,
        progress_percent, passed, failed, others, ...}
        """
        resp = self._request("GET", f"/runs/{session_id}/status/")
        try:
            poll_after = int(resp.headers.get("X-TAP-Poll-After", "5"))
        except ValueError:
            poll_after = 5
        return resp.json(), poll_after

    def run_detail(self, session_id: int) -> dict:
        """케이스별 결과 상세 (cases[]: result_id/title/status/duration/testrail_url/로그 tail)."""
        return self._request("GET", f"/runs/{session_id}/").json()

    def cancel_run(self, session_id: int) -> dict:
        return self._request("POST", f"/runs/{session_id}/cancel/", timeout=30).json()
