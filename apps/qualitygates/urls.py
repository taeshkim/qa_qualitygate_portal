from django.urls import path
from . import views

app_name = "qualitygates"

urlpatterns = [
    # 폴더 (인스턴스)
    path("", views.folder_list, name="folder_list"),
    path("folders/register/", views.folder_register, name="folder_register"),
    path("folders/<uuid:pk>/", views.folder_detail, name="folder_detail"),
    path("folders/<uuid:pk>/edit/", views.folder_edit, name="folder_edit"),
    path("folders/<uuid:pk>/delete/", views.folder_delete, name="folder_delete"),

    # 하위폴더 (SubFolder)
    path("folders/<uuid:folder_pk>/subfolders/register/", views.subfolder_register, name="subfolder_register"),
    path("subfolders/<uuid:pk>/", views.subfolder_detail, name="subfolder_detail"),
    path("subfolders/<uuid:pk>/edit/", views.subfolder_edit, name="subfolder_edit"),
    path("subfolders/<uuid:pk>/delete/", views.subfolder_delete, name="subfolder_delete"),

    # Gate
    path("subfolders/<uuid:subfolder_pk>/gates/register/", views.gate_register, name="gate_register"),
    path("<uuid:pk>/", views.gate_detail, name="gate_detail"),
    path("<uuid:pk>/edit/", views.gate_edit, name="gate_edit"),
    path("<uuid:pk>/toggle-auto/", views.gate_toggle_auto, name="gate_toggle_auto"),
    path("<uuid:pk>/toggle-parallel/", views.gate_toggle_parallel, name="gate_toggle_parallel"),
    path("<uuid:pk>/delete/", views.gate_delete, name="gate_delete"),
    path("<uuid:pk>/run/", views.gate_run, name="gate_run"),
    path("<uuid:pk>/run/params/", views.gate_run_params, name="gate_run_params"),

    # Run 상세 / 액션
    path("runs/<uuid:pk>/", views.run_detail, name="run_detail"),
    path("runs/<uuid:pk>/status/", views.run_status_fragment, name="run_status_fragment"),
    path("runs/<uuid:pk>/deploy/", views.deploy_trigger, name="deploy_trigger"),
    path("runs/<uuid:pk>/deploy/params/", views.deploy_params, name="deploy_params"),
    path("runs/<uuid:pk>/prod-deploy/", views.prod_deploy, name="prod_deploy"),
    path("runs/<uuid:pk>/prod-deploy/params/", views.prod_deploy_params, name="prod_deploy_params"),
    path("runs/<uuid:pk>/rerun/", views.rerun_gate, name="rerun_gate"),
    path("runs/<uuid:pk>/rerun-test/", views.rerun_test, name="rerun_test"),
    path("runs/<uuid:pk>/cancel/", views.cancel_run, name="cancel_run"),
    path("runs/<uuid:pk>/console/image/", views.image_console, name="image_console"),
    path("runs/<uuid:pk>/console/deploy/", views.deploy_console, name="deploy_console"),
    path("runs/<uuid:pk>/console/prod/", views.prod_console, name="prod_console"),
    path("runs/<uuid:pk>/error-log/image/", views.image_error_log, name="image_error_log"),
    path("runs/<uuid:pk>/error-log/deploy/", views.deploy_error_log, name="deploy_error_log"),
    path("runs/<uuid:pk>/error-log/prod/", views.prod_error_log, name="prod_error_log"),
    path("runs/<uuid:pk>/argocd/<str:stage>/retry/", views.argocd_retry, name="argocd_retry"),
]
