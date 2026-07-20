import uuid
from django.conf import settings
from django.db import models


class GatePolicy(models.Model):
    class TestType(models.TextChoices):
        API_TEST = "api_test", "API Test"
        E2E_UI_TEST = "e2e_ui_test", "E2E UI Test"

    class BlockingMode(models.TextChoices):
        BLOCKING = "blocking", "Blocking"
        REFERENCE = "reference", "Reference"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        "services.Service", on_delete=models.CASCADE, related_name="gate_policies"
    )
    environment = models.ForeignKey(
        "services.Environment", on_delete=models.CASCADE, related_name="gate_policies"
    )
    test_type = models.CharField(max_length=30, choices=TestType.choices)
    blocking_mode = models.CharField(max_length=20, choices=BlockingMode.choices)
    pass_threshold = models.DecimalField(
        max_digits=5, decimal_places=2, default=100.00, verbose_name="통과 임계치 (%)"
    )
    latency_threshold_ms = models.IntegerField(
        null=True, blank=True, verbose_name="응답시간 임계치 (ms)"
    )
    error_rate_threshold = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        verbose_name="에러율 허용 임계치 (%)"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "gates_policy"
        unique_together = ["service", "environment", "test_type"]
        verbose_name_plural = "Gate Policies"

    def __str__(self):
        return f"{self.service.code} / {self.environment.name} / {self.get_test_type_display()}"


class GateRun(models.Model):
    class Result(models.TextChoices):
        PASS = "PASS", "PASS"
        FAIL = "FAIL", "FAIL"
        RUNNING = "RUNNING", "Running"

    class TriggerType(models.TextChoices):
        WEBHOOK = "webhook", "Webhook"
        MANUAL = "manual", "Manual"
        RERUN = "rerun", "Re-run"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        "services.Service", on_delete=models.CASCADE, related_name="gate_runs"
    )
    environment = models.ForeignKey(
        "services.Environment", on_delete=models.CASCADE, related_name="gate_runs"
    )
    version = models.CharField(max_length=50, blank=True, verbose_name="배포 버전")
    commit_sha = models.CharField(max_length=40, blank=True, verbose_name="커밋 SHA")
    branch = models.CharField(max_length=100, blank=True, verbose_name="브랜치명")
    result = models.CharField(
        max_length=10, choices=Result.choices, default=Result.RUNNING
    )
    api_test_result = models.CharField(
        max_length=10, choices=Result.choices, null=True, blank=True
    )
    e2e_test_result = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        verbose_name="E2E PASS Rate (%)"
    )
    duration_seconds = models.IntegerField(null=True, blank=True, verbose_name="소요시간 (초)")
    jenkins_build_url = models.URLField(max_length=500, blank=True)
    report_url = models.URLField(max_length=500, blank=True)
    triggered_by = models.CharField(
        max_length=50, choices=TriggerType.choices, default=TriggerType.WEBHOOK
    )
    triggered_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "gates_run"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.service.code}/{self.environment.name} - {self.result} ({self.created_at:%Y-%m-%d %H:%M})"


class GateFailure(models.Model):
    class FailureType(models.TextChoices):
        ASSERTION = "assertion", "Assertion"
        TIMEOUT = "timeout", "Timeout"
        ERROR = "error", "Error"
        SCHEMA = "schema", "Schema"

    class Category(models.TextChoices):
        BUG = "bug", "Bug"
        ENVIRONMENT = "environment", "Environment Issue"
        FLAKY = "flaky", "Flaky Test"
        CONFIGURATION = "configuration", "Configuration"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gate_run = models.ForeignKey(
        GateRun, on_delete=models.CASCADE, related_name="failures"
    )
    test_name = models.CharField(max_length=200, verbose_name="테스트명")
    failure_type = models.CharField(
        max_length=30, choices=FailureType.choices, verbose_name="실패 유형"
    )
    error_message = models.TextField(blank=True, verbose_name="에러 메시지")
    category = models.CharField(
        max_length=30, choices=Category.choices, null=True, blank=True,
        verbose_name="분류"
    )
    categorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="categorized_failures"
    )
    categorized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "gates_failure"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.test_name} - {self.get_failure_type_display()}"
