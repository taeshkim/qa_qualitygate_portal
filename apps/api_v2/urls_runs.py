"""Quality Gate Run 라우팅 (Phase D-2..6).

`apps/api_v2/urls.py` 는 이 모듈을 include 한다. Phase E (control 액션) 는 별도 `urls_control.py`
에서 추가될 예정 — 이 파일에는 조회/트리거/콘솔만.
"""

from __future__ import annotations

from django.urls import path

from .views.runs import gate_runs, run_console, run_detail, run_status


urlpatterns = [
    path("gates/<uuid:gate_id>/runs/", gate_runs, name="gate_runs"),
    path("runs/<uuid:run_id>/", run_detail, name="run_detail"),
    path("runs/<uuid:run_id>/status/", run_status, name="run_status"),
    path("runs/<uuid:run_id>/console/<str:stage>/", run_console, name="run_console"),
]
