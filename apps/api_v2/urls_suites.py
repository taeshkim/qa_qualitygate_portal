"""apps/api_v2/urls_suites.py — 단독 Test Suite Run 엔드포인트 라우팅 (Phase F-2..3).

본 모듈은 의도적으로 ``apps/api_v2/urls.py`` 와 분리되어 있다. Phase C (메타 조회)
가 Suite 목록/상세 (`GET /api/v2/suites/`, `GET /api/v2/suites/<id>/`) 를 같이
include 할 예정이라, 합치는 시점에 충돌을 피하려는 목적이다.

추후 ``apps/api_v2/urls.py`` 에서 다음과 같이 include 한다::

    urlpatterns += [
        path("", include("apps.api_v2.urls_suites")),
    ]

PRD: prd_api.md §5 의 Suite 라우팅.
"""

from __future__ import annotations

from django.urls import path

from .views import suites_v2

app_name = "api_v2_suites"

urlpatterns = [
    path(
        "suites/<uuid:pk>/runs/",
        suites_v2.suite_run_collection,
        name="suite_runs",
    ),
    path(
        "suite-runs/<uuid:pk>/",
        suites_v2.suite_run_detail,
        name="suite_run_detail",
    ),
    path(
        "suite-runs/<uuid:pk>/status/",
        suites_v2.suite_run_status,
        name="suite_run_status",
    ),
    path(
        "suite-runs/<uuid:pk>/cancel/",
        suites_v2.suite_run_cancel,
        name="suite_run_cancel",
    ),
    path(
        "suite-job-runs/<uuid:pk>/console/",
        suites_v2.suite_job_run_console,
        name="suite_job_run_console",
    ),
]
