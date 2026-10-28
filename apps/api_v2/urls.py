"""api_v2 URL 라우팅 (PRD §5).

세분된 라우팅은 각 Phase 별 모듈에 둔다 — 통합 충돌을 줄이기 위함.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from .views.health import health, me

app_name = "api_v2"

# API Key 로만 인증하는 업무 엔드포인트 묶음. `/ci-api/`(urls_ci.py — 게이트웨이 로그인 예외 경로)와 공유한다.
# health/me · 문서(schema/docs/redoc) · 내보내기(export)는 포함하지 않는다.
ci_api_patterns = [
    path("health/", health, name="health"),
    path("me/", me, name="me"),
    # 메타 조회 (folders/subfolders/gates/suites/params) — Phase C
    path("", include("apps.api_v2.urls_meta")),
    # Quality Gate Run 조회/트리거/콘솔 — Phase D-2..6
    path("", include("apps.api_v2.urls_runs")),
    # Quality Gate Run 제어 (cancel/rerun*/deploy/prod-deploy/argocd retry) — Phase E
    path("", include("apps.api_v2.urls_controls")),
    # Test Suite Run — Phase F-2..3
    path("", include("apps.api_v2.urls_suites")),
    # 담당자 관리 (owners:manage + admin 소유 키)
    path("", include("apps.api_v2.urls_owners")),
]


urlpatterns = [
    # ── OpenAPI 문서 (문서 전용 신규 엔드포인트 — 기존 API 명세/동작 불변) ──
    # schema JSON 은 세션 로그인 필요(SPECTACULAR_SETTINGS.SERVE_PERMISSIONS),
    # 문서 페이지는 login_required 로 감싸 미로그인 브라우저를 로그인 화면으로 유도.
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    # 커스텀 템플릿: Authorize 입력 키를 /api/v2/me/ 로 실검증해 유효/무효 배지 표시.
    path("docs/", login_required(
        SpectacularSwaggerView.as_view(
            url_name="api_v2:schema",
            template_name="api_v2/swagger_ui.html",
        )), name="swagger-ui"),
    path("redoc/", login_required(
        SpectacularRedocView.as_view(url_name="api_v2:schema")), name="redoc"),
    # 메타 조회·Run·제어·Suite — ci-api(게이트웨이 로그인 예외 경로)와 공유하는 키 인증 API
    *ci_api_patterns,
    # 서버 이관용 DB 스냅샷 내보내기 (admin + export:read 명시 스코프) — 문서(schema)에는 노출하지 않음
    path("", include("apps.api_v2.urls_export")),
]
