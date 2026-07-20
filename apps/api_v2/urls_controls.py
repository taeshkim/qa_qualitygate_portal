"""Phase E — Quality Gate Run 제어 라우팅 (PRD §5)."""

from __future__ import annotations

from django.urls import path

from .views import controls

urlpatterns = [
    path("runs/<uuid:run_id>/cancel/", controls.cancel_run, name="run_cancel"),
    path("runs/<uuid:run_id>/rerun/", controls.rerun_gate, name="run_rerun"),
    path("runs/<uuid:run_id>/rerun-test/", controls.rerun_test, name="run_rerun_test"),
    path("runs/<uuid:run_id>/rerun-deploy/", controls.rerun_deploy, name="run_rerun_deploy"),
    path("runs/<uuid:run_id>/rerun-prod/", controls.rerun_prod, name="run_rerun_prod"),
    path("runs/<uuid:run_id>/deploy/", controls.trigger_deploy, name="run_deploy"),
    path("runs/<uuid:run_id>/prod-deploy/", controls.trigger_prod, name="run_prod_deploy"),
    path("runs/<uuid:run_id>/argocd/<str:stage>/retry/", controls.argocd_retry, name="run_argocd_retry"),
]
