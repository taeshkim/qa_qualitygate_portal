"""api_v2 URL 라우팅 (PRD §5).

세분된 라우팅은 각 Phase 별 모듈에 둔다 — 통합 충돌을 줄이기 위함.
"""

from __future__ import annotations

from django.urls import include, path

from .views.health import health, me

app_name = "api_v2"

urlpatterns = [
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
]
