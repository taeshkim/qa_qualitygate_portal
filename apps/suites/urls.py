from django.urls import path
from . import views

app_name = "suites"

urlpatterns = [
    path("", views.suite_list, name="suite_list"),
    path("register/", views.suite_register, name="suite_register"),
    path("credentials/", views.credentials_manage, name="credentials"),
    path("queue/", views.queue_view, name="queue"),
    path("queue/stop-all/", views.queue_stop_all, name="queue_stop_all"),
    path("<uuid:pk>/", views.suite_detail, name="suite_detail"),
    path("<uuid:pk>/edit/", views.suite_edit, name="suite_edit"),
    path("<uuid:pk>/sync/", views.suite_sync, name="suite_sync"),
    path("<uuid:pk>/delete/", views.suite_delete, name="suite_delete"),
    path("<uuid:pk>/run/", views.suite_run, name="suite_run"),
    path("runs/<uuid:pk>/", views.run_detail, name="run_detail"),
    path("runs/<uuid:pk>/status/", views.run_status_fragment, name="run_status_fragment"),
    path("runs/<uuid:pk>/delete/", views.run_delete, name="run_delete"),
    path("<uuid:pk>/runs/clear/", views.runs_clear, name="runs_clear"),
    path("job-runs/<uuid:pk>/console/", views.job_run_console, name="job_run_console"),
    path("job-runs/<uuid:pk>/error-log/", views.job_run_error_log, name="job_run_error_log"),
]
