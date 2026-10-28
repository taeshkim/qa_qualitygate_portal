"""`/ci-api/` URL 라우팅 — 게이트웨이 로그인 예외 경로 전용 (config/urls_ci.py 에서 마운트).

`/api/v2/` 의 키 인증 업무 엔드포인트(meta/runs/controls/suites + health/me)와 동일한 뷰·이름을 쓴다.
문서(schema/docs/redoc)와 DB 내보내기(export)는 로그인 없이 열리는 경로에 둘 수 없어 제외한다.
"""

from __future__ import annotations

from .urls import ci_api_patterns

app_name = "api_v2"

urlpatterns = ci_api_patterns
