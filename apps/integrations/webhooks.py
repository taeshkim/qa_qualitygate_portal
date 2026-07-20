import hashlib
import hmac
import json
import logging
import time
from django.conf import settings
from django.utils import timezone
from apps.gates.models import GateRun, GateFailure
from apps.services.models import Service, Environment
from apps.integrations.models import WebhookLog

logger = logging.getLogger(__name__)


def verify_hmac_signature(payload_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode("utf-8"), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def log_webhook(source, endpoint, payload_body, signature_valid, http_status, error_message="", processing_ms=0):
    WebhookLog.objects.create(
        source=source,
        endpoint=endpoint,
        payload_hash=hashlib.sha256(payload_body).hexdigest(),
        signature_valid=signature_valid,
        http_status=http_status,
        processing_ms=processing_ms,
        error_message=error_message,
    )


def process_jenkins_webhook(payload: dict) -> GateRun:
    """
    Jenkins Webhook Payload 예시:
    {
        "service_code": "JK",
        "environment": "Dev",
        "version": "1.2.3",
        "commit_sha": "abc123...",
        "branch": "main",
        "result": "PASS" | "FAIL",
        "api_test_result": "PASS" | "FAIL",
        "e2e_test_result": 95.5,
        "duration_seconds": 120,
        "jenkins_build_url": "https://jenkins.../build/123",
        "report_url": "https://jenkins.../report",
        "failures": [
            {
                "test_name": "test_login_api",
                "failure_type": "assertion",
                "error_message": "Expected 200, got 500"
            }
        ]
    }
    """
    service = Service.objects.get(code=payload["service_code"])
    environment = Environment.objects.get(name=payload["environment"])

    gate_run = GateRun.objects.create(
        service=service,
        environment=environment,
        version=payload.get("version", ""),
        commit_sha=payload.get("commit_sha", ""),
        branch=payload.get("branch", ""),
        result=payload.get("result", GateRun.Result.RUNNING),
        api_test_result=payload.get("api_test_result"),
        e2e_test_result=payload.get("e2e_test_result"),
        duration_seconds=payload.get("duration_seconds"),
        jenkins_build_url=payload.get("jenkins_build_url", ""),
        report_url=payload.get("report_url", ""),
        triggered_by=GateRun.TriggerType.WEBHOOK,
        triggered_at=timezone.now(),
        completed_at=timezone.now() if payload.get("result") in ("PASS", "FAIL") else None,
    )

    for failure_data in payload.get("failures", []):
        GateFailure.objects.create(
            gate_run=gate_run,
            test_name=failure_data["test_name"],
            failure_type=failure_data.get("failure_type", "error"),
            error_message=failure_data.get("error_message", ""),
        )

    return gate_run


def process_tas_webhook(payload: dict) -> GateRun:
    """
    TAS Webhook Payload 예시:
    {
        "service_code": "JK",
        "environment": "Dev",
        "gate_run_id": "uuid",  (optional - 기존 GateRun 업데이트)
        "e2e_test_result": 98.5,
        "report_url": "https://tas.../report/123",
        "failures": [...]
    }
    """
    service = Service.objects.get(code=payload["service_code"])
    environment = Environment.objects.get(name=payload["environment"])

    gate_run_id = payload.get("gate_run_id")

    if gate_run_id:
        gate_run = GateRun.objects.get(pk=gate_run_id)
        gate_run.e2e_test_result = payload.get("e2e_test_result")
        if payload.get("report_url"):
            gate_run.report_url = payload["report_url"]
        gate_run.save(update_fields=["e2e_test_result", "report_url"])
    else:
        gate_run = GateRun.objects.create(
            service=service,
            environment=environment,
            e2e_test_result=payload.get("e2e_test_result"),
            report_url=payload.get("report_url", ""),
            triggered_by=GateRun.TriggerType.WEBHOOK,
            triggered_at=timezone.now(),
            result=GateRun.Result.RUNNING,
        )

    for failure_data in payload.get("failures", []):
        GateFailure.objects.create(
            gate_run=gate_run,
            test_name=failure_data["test_name"],
            failure_type=failure_data.get("failure_type", "error"),
            error_message=failure_data.get("error_message", ""),
        )

    return gate_run
