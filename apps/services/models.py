import uuid
from django.db import models


class Service(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, verbose_name="서비스명")
    code = models.CharField(max_length=20, unique=True, verbose_name="서비스 코드")
    tribe_name = models.CharField(max_length=100, blank=True, verbose_name="트라이브명")
    slack_channel_id = models.CharField(max_length=50, blank=True, verbose_name="Slack 채널 ID")
    is_active = models.BooleanField(default=True, verbose_name="활성 여부")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "services_service"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Environment(models.Model):
    class TriggerType(models.TextChoices):
        JENKINS_TRIGGER = "jenkins_trigger", "Jenkins Trigger"
        SLACK_ONLY = "slack_only", "Slack Only"

    class GateMode(models.TextChoices):
        BLOCKING = "blocking", "Blocking"
        REFERENCE = "reference", "Reference"
        MONITORING = "monitoring", "Monitoring"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=20, verbose_name="환경명")
    trigger_type = models.CharField(
        max_length=30, choices=TriggerType.choices, verbose_name="트리거 방식"
    )
    gate_mode = models.CharField(
        max_length=20, choices=GateMode.choices, verbose_name="게이트 모드"
    )
    next_env_control = models.CharField(
        max_length=50, blank=True, verbose_name="다음 단계 제어"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "services_environment"
        ordering = ["name"]

    def __str__(self):
        return self.name
