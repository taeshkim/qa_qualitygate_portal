import json
import logging
import os
import time

from django.db.models import Avg, Count, Q
from django.utils import timezone
from datetime import timedelta
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.services.models import Service, Environment
from apps.gates.models import GateRun, GateFailure, GatePolicy
from apps.integrations.webhooks import (
    verify_hmac_signature, log_webhook,
    process_jenkins_webhook, process_tas_webhook,
)
from apps.integrations.jenkins import JenkinsClient, JenkinsError

logger = logging.getLogger(__name__)

from .serializers import (
    ServiceSerializer,
    EnvironmentSerializer,
    GateRunListSerializer,
    GateRunDetailSerializer,
    GatePolicySerializer,
    FailureCategorizeSerializer,
)


class ServiceViewSet(viewsets.ModelViewSet):
    queryset = Service.objects.all()
    serializer_class = ServiceSerializer
    filterset_fields = ["is_active", "code"]
    search_fields = ["name", "code", "tribe_name"]


class EnvironmentViewSet(viewsets.ModelViewSet):
    queryset = Environment.objects.all()
    serializer_class = EnvironmentSerializer


class GateRunViewSet(viewsets.ReadOnlyModelViewSet):
    filterset_fields = ["service", "environment", "result", "triggered_by"]
    search_fields = ["version", "commit_sha", "branch"]
    ordering_fields = ["created_at", "duration_seconds"]

    def get_queryset(self):
        return (
            GateRun.objects.select_related("service", "environment")
            .annotate(failure_count=Count("failures"))
        )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return GateRunDetailSerializer
        return GateRunListSerializer

    # Re-run은 Jenkins에서 직접 실행 — Portal은 결과 조회만 담당


class GatePolicyViewSet(viewsets.ModelViewSet):
    queryset = GatePolicy.objects.select_related("service", "environment").all()
    serializer_class = GatePolicySerializer
    filterset_fields = ["service", "environment", "test_type", "blocking_mode"]
    http_method_names = ["get", "put", "patch", "head", "options"]

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def categorize_failure(request, run_id, failure_id):
    try:
        failure = GateFailure.objects.get(pk=failure_id, gate_run_id=run_id)
    except GateFailure.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)

    serializer = FailureCategorizeSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    failure.category = serializer.validated_data["category"]
    failure.categorized_by = request.user
    failure.categorized_at = timezone.now()
    failure.save(update_fields=["category", "categorized_by", "categorized_at"])

    return Response({"status": "categorized", "category": failure.category})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dashboard_overview(request):
    services = Service.objects.filter(is_active=True)
    environments = Environment.objects.all()

    matrix = []
    for service in services:
        row = {"service": ServiceSerializer(service).data, "environments": []}
        for env in environments:
            latest_run = (
                GateRun.objects.filter(service=service, environment=env)
                .order_by("-created_at")
                .first()
            )
            row["environments"].append({
                "environment": EnvironmentSerializer(env).data,
                "latest_run": GateRunListSerializer(latest_run).data if latest_run else None,
            })
        matrix.append(row)

    return Response({"matrix": matrix})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dashboard_metrics(request):
    now = timezone.now()
    seven_days_ago = now - timedelta(days=7)

    runs_7d = GateRun.objects.filter(created_at__gte=seven_days_ago)
    total_runs = runs_7d.count()

    pass_count = runs_7d.filter(result=GateRun.Result.PASS).count()
    pass_rate = round((pass_count / total_runs * 100), 1) if total_runs > 0 else 0

    avg_duration = runs_7d.filter(
        duration_seconds__isnull=False
    ).aggregate(avg=Avg("duration_seconds"))["avg"] or 0

    fail_count = runs_7d.filter(result=GateRun.Result.FAIL).count()

    flaky_count = GateFailure.objects.filter(
        gate_run__created_at__gte=seven_days_ago,
        category="flaky",
    ).count()
    total_failures = GateFailure.objects.filter(
        gate_run__created_at__gte=seven_days_ago,
    ).count()
    flake_rate = round((flaky_count / total_failures * 100), 1) if total_failures > 0 else 0

    return Response({
        "period": "7d",
        "total_runs": total_runs,
        "pass_rate": pass_rate,
        "fail_count": fail_count,
        "avg_duration_seconds": round(avg_duration, 1),
        "flake_rate": flake_rate,
    })


# ---- Webhook Endpoints ----

@api_view(["POST"])
@permission_classes([AllowAny])
def webhook_jenkins(request):
    start = time.time()
    payload_body = request.body
    signature = request.headers.get("X-QGP-Signature", "")
    secret = os.getenv("JENKINS_WEBHOOK_SECRET", "")

    sig_valid = verify_hmac_signature(payload_body, signature, secret) if secret else True

    if secret and not sig_valid:
        log_webhook("jenkins", "/api/v1/webhooks/jenkins/", payload_body, False, 403, "Invalid signature")
        return Response({"error": "Invalid signature"}, status=status.HTTP_403_FORBIDDEN)

    try:
        payload = json.loads(payload_body)
        gate_run = process_jenkins_webhook(payload)
        processing_ms = int((time.time() - start) * 1000)
        log_webhook("jenkins", "/api/v1/webhooks/jenkins/", payload_body, sig_valid, 201, processing_ms=processing_ms)

        # Trigger Slack notification asynchronously
        try:
            from apps.integrations.tasks import send_gate_result_notification
            send_gate_result_notification.delay(str(gate_run.pk))
        except Exception:
            logger.warning("Failed to queue Slack notification", exc_info=True)

        return Response(
            {"status": "accepted", "gate_run_id": str(gate_run.pk)},
            status=status.HTTP_201_CREATED,
        )
    except Exception as e:
        processing_ms = int((time.time() - start) * 1000)
        log_webhook("jenkins", "/api/v1/webhooks/jenkins/", payload_body, sig_valid, 400, str(e), processing_ms)
        logger.exception("Jenkins webhook processing failed")
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([AllowAny])
def webhook_tas(request):
    start = time.time()
    payload_body = request.body
    signature = request.headers.get("X-QGP-Signature", "")
    secret = os.getenv("TAS_WEBHOOK_SECRET", "")

    sig_valid = verify_hmac_signature(payload_body, signature, secret) if secret else True

    if secret and not sig_valid:
        log_webhook("tas", "/api/v1/webhooks/tas/", payload_body, False, 403, "Invalid signature")
        return Response({"error": "Invalid signature"}, status=status.HTTP_403_FORBIDDEN)

    try:
        payload = json.loads(payload_body)
        gate_run = process_tas_webhook(payload)
        processing_ms = int((time.time() - start) * 1000)
        log_webhook("tas", "/api/v1/webhooks/tas/", payload_body, sig_valid, 201, processing_ms=processing_ms)

        return Response(
            {"status": "accepted", "gate_run_id": str(gate_run.pk)},
            status=status.HTTP_201_CREATED,
        )
    except Exception as e:
        processing_ms = int((time.time() - start) * 1000)
        log_webhook("tas", "/api/v1/webhooks/tas/", payload_body, sig_valid, 400, str(e), processing_ms)
        logger.exception("TAS webhook processing failed")
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ---- Jenkins API ----

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def jenkins_test_connection(request):
    try:
        client = JenkinsClient()
        result = client.test_connection()
        return Response(result)
    except JenkinsError as e:
        return Response({"ok": False, "error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def jenkins_job_info(request):
    job_path = request.query_params.get("job_path", "")
    if not job_path:
        return Response({"error": "job_path parameter required"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        client = JenkinsClient()
        result = client.get_job_info(job_path)
        return Response(result)
    except JenkinsError as e:
        return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def jenkins_build_info(request):
    job_path = request.query_params.get("job_path", "")
    build_number = request.query_params.get("build_number", "")
    if not job_path:
        return Response({"error": "job_path parameter required"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        client = JenkinsClient()
        if build_number:
            result = client.get_build_info(job_path, int(build_number))
        else:
            result = client.get_last_build_info(job_path)
        return Response(result)
    except JenkinsError as e:
        return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
