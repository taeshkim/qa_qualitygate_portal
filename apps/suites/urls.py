from django.urls import path
from . import views

app_name = "suites"

urlpatterns = [
    path("", views.suite_list, name="suite_list"),
    path("register/", views.suite_register, name="suite_register"),
    path("api-sync/", views.suites_api_sync, name="suites_api_sync"),
    path("credentials/", views.credentials_manage, name="credentials"),
    path("queue/", views.queue_view, name="queue"),
    path("queue/stop-all/", views.queue_stop_all, name="queue_stop_all"),
    path("<uuid:pk>/", views.suite_detail, name="suite_detail"),
    path("<uuid:pk>/edit/", views.suite_edit, name="suite_edit"),
    path("<uuid:pk>/sync/", views.suite_sync, name="suite_sync"),
    path("<uuid:pk>/delete/", views.suite_delete, name="suite_delete"),
    path("<uuid:pk>/run/", views.suite_run, name="suite_run"),
    path("<uuid:pk>/gwt-tokens/generate/", views.gwt_tokens_generate, name="gwt_tokens_generate"),
    path("jobs/<uuid:pk>/gwt-token/regen/", views.gwt_token_regen, name="gwt_token_regen"),
    path("runs/", views.suite_run_list, name="run_list"),
    path("runs/untriaged/", views.untriaged_list, name="untriaged_list"),
    path("runs/untriaged/bulk-triage/", views.untriaged_bulk_triage, name="untriaged_bulk_triage"),
    path("runs/<uuid:pk>/", views.run_detail, name="run_detail"),
    path("runs/<uuid:pk>/status/", views.run_status_fragment, name="run_status_fragment"),
    path("runs/<uuid:pk>/delete/", views.run_delete, name="run_delete"),
    path("<uuid:pk>/runs/clear/", views.runs_clear, name="runs_clear"),
    # UI Test Automation (TAP) — PRD docs/prd_ui_test_automation.md
    path("tap/table/", views.tap_suite_table, name="tap_suite_table"),
    path("tap/<uuid:pk>/run/", views.tap_suite_run, name="tap_suite_run"),
    path("tap/<uuid:pk>/edit/", views.tap_suite_edit, name="tap_suite_edit"),
    path("tap-runs/<uuid:pk>/cancel/", views.tap_run_cancel, name="tap_run_cancel"),
    path("tap-runs/<uuid:pk>/", views.tap_run_detail, name="tap_run_detail"),
    path("job-runs/", views.job_run_list, name="job_run_list"),
    path("job-runs/<uuid:pk>/console/", views.job_run_console, name="job_run_console"),
    path("job-runs/<uuid:pk>/error-log/", views.job_run_error_log, name="job_run_error_log"),
    path("job-runs/<uuid:pk>/triage/", views.job_run_triage, name="job_run_triage"),
]
