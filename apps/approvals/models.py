import uuid
from django.conf import settings
from django.db import models


class ExceptionApproval(models.Model):
    class RiskLevel(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        "services.Service", on_delete=models.CASCADE, related_name="approvals"
    )
    environment = models.ForeignKey(
        "services.Environment", on_delete=models.CASCADE, related_name="approvals"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="requested_approvals"
    )
    reason = models.TextField(verbose_name="예외 사유")
    impact_scope = models.TextField(blank=True, verbose_name="영향 범위")
    risk_level = models.CharField(max_length=10, choices=RiskLevel.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="approved_approvals"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    post_gate_run = models.ForeignKey(
        "gates.GateRun", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="exception_approvals"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "approvals_exception"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.service.code} - {self.get_status_display()} ({self.created_at:%Y-%m-%d})"
