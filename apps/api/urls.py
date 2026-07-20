from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r"services", views.ServiceViewSet)
router.register(r"environments", views.EnvironmentViewSet)
router.register(r"gates/runs", views.GateRunViewSet, basename="gate-run")
router.register(r"policies", views.GatePolicyViewSet)

urlpatterns = [
    path("", include(router.urls)),
    path(
        "gates/runs/<uuid:run_id>/failures/<uuid:failure_id>/categorize/",
        views.categorize_failure,
        name="categorize-failure",
    ),
    path("dashboard/overview/", views.dashboard_overview, name="dashboard-overview"),
    path("dashboard/metrics/", views.dashboard_metrics, name="dashboard-metrics"),
    # Webhooks
    path("webhooks/jenkins/", views.webhook_jenkins, name="webhook-jenkins"),
    path("webhooks/tas/", views.webhook_tas, name="webhook-tas"),
    # Jenkins (조회 전용)
    path("jenkins/test-connection/", views.jenkins_test_connection, name="jenkins-test-connection"),
    path("jenkins/job-info/", views.jenkins_job_info, name="jenkins-job-info"),
    path("jenkins/build-info/", views.jenkins_build_info, name="jenkins-build-info"),
]
