import uuid
from django.conf import settings
from django.db import models


class Escalation(models.Model):
    class Level(models.TextChoices):
        L1 = "L1", "L1 - 자체 해결"
        L2 = "L2", "L2 - 트라이브 + QPE"
        L3 = "L3", "L3 - QPE Ops"
        L4 = "L4", "L4 - 긴급 배포"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        RESOLVED = "resolved", "Resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gate_run = models.ForeignKey(
        "gates.GateRun", on_delete=models.CASCADE, related_name="escalations"
    )
    level = models.CharField(max_length=5, choices=Level.choices)
    trigger_reason = models.TextField(verbose_name="에스컬레이션 사유")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="assigned_escalations"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True, verbose_name="해결 내용")
    is_auto_generated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "escalations_escalation"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_level_display()} - {self.get_status_display()}"
