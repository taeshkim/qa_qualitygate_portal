import uuid
from django.conf import settings
from django.db import models


class JenkinsConnection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, verbose_name="연결 이름")
    base_url = models.URLField(max_length=500, verbose_name="Jenkins URL")
    api_user = models.CharField(max_length=100, verbose_name="API User ID")
    api_token_encrypted = models.TextField(verbose_name="API Token (암호화)")
    webhook_secret_encrypted = models.TextField(
        blank=True, verbose_name="Webhook Secret (암호화)"
    )
    is_healthy = models.BooleanField(default=False)
    last_health_check_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_jenkins_connection"

    def __str__(self):
        return self.name


class JenkinsJobMapping(models.Model):
    class TestType(models.TextChoices):
        API_TEST = "api_test", "API Test"
        E2E_UI_TEST = "e2e_ui_test", "E2E UI Test"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        "services.Service", on_delete=models.CASCADE, related_name="job_mappings"
    )
    environment = models.ForeignKey(
        "services.Environment", on_delete=models.CASCADE, related_name="job_mappings"
    )
    test_type = models.CharField(max_length=30, choices=TestType.choices)
    jenkins_connection = models.ForeignKey(
        JenkinsConnection, on_delete=models.CASCADE, related_name="job_mappings"
    )
    job_path = models.CharField(max_length=500, verbose_name="Jenkins Job 경로")
    build_params = models.JSONField(default=dict, blank=True, verbose_name="빌드 파라미터")
    timeout_seconds = models.IntegerField(default=600, verbose_name="타임아웃 (초)")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "integrations_jenkins_job_mapping"

    def __str__(self):
        return f"{self.service.code}/{self.environment.name} - {self.job_path}"


class SlackChannelMapping(models.Model):
    class ChannelType(models.TextChoices):
        TRIBE_ALERT = "tribe_alert", "Tribe Alert"
        QPE_ALERT = "qpe_alert", "QPE Alert"
        PRD_APPROVAL = "prd_approval", "Prd Approval"
        OPS = "ops", "Ops"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        "services.Service", on_delete=models.CASCADE, related_name="slack_channels"
    )
    channel_type = models.CharField(max_length=30, choices=ChannelType.choices)
    channel_id = models.CharField(max_length=50, verbose_name="Slack 채널 ID")
    channel_name = models.CharField(max_length=100, blank=True, verbose_name="채널명")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "integrations_slack_channel"

    def __str__(self):
        return f"{self.channel_name} ({self.get_channel_type_display()})"


class IntegrationAuditLog(models.Model):
    class IntegrationType(models.TextChoices):
        JENKINS = "jenkins", "Jenkins"
        SLACK = "slack", "Slack"
        TAS = "tas", "TAS"
        SSO = "sso", "SSO"

    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        ROTATE_SECRET = "rotate_secret", "Rotate Secret"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    integration_type = models.CharField(max_length=30, choices=IntegrationType.choices)
    action = models.CharField(max_length=30, choices=Action.choices)
    target_id = models.UUIDField()
    changed_fields = models.JSONField(default=dict, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "integrations_audit_log"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_integration_type_display()} - {self.get_action_display()}"


class ApiEndpointOwner(models.Model):
    """API 엔드포인트별 담당자/팀 매핑.

    `(repo, method, endpoint)` 가 유니크 키. CSV 임포트로 채울 수도 있고
    Admin 화면에서 수동으로 추가/편집할 수도 있다. `is_imported=True` 인 row 만
    재임포트 시 update/delete 대상이며, 수동 row(`is_imported=False`)는 보존된다.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # 식별 (unique_together)
    repo = models.CharField(max_length=32, verbose_name="레포")
    method = models.CharField(max_length=10, verbose_name="Method")
    endpoint = models.CharField(max_length=500, verbose_name="Endpoint")

    # 분류
    category = models.CharField(max_length=100, blank=True, verbose_name="카테고리")
    area = models.CharField(max_length=100, blank=True, verbose_name="영역")
    mapped_app = models.CharField(max_length=100, blank=True, verbose_name="매핑 앱")

    # 담당자 (평면화 — 콤마 다중값을 원문 그대로 보존)
    # 비즈니스 룰: status_code 4xx → FE 담당자, 5xx → BE 담당자. 2026-05-20 컬럼명 변경
    # 및 담당팀 컬럼 제거 — 이제 담당자만 관리.
    fe_owner = models.CharField(
        max_length=200, blank=True, verbose_name="FE 담당자"
    )
    be_owner = models.CharField(
        max_length=200, blank=True, verbose_name="BE 담당자"
    )

    # 메타
    usage_count = models.PositiveIntegerField(default=0, verbose_name="사용 횟수")
    step_ids = models.TextField(blank=True, verbose_name="Step IDs (콤마)")

    # 운영
    is_imported = models.BooleanField(
        default=False, verbose_name="CSV 임포트로 들어온 row 인지"
    )
    notes = models.TextField(blank=True, verbose_name="비고 (수동 메모)")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_api_endpoint_owner"
        ordering = ["method", "endpoint", "repo"]
        constraints = [
            # repo 는 식별 키에서 제외 — 같은 (method, endpoint) 는 단 하나만 존재.
            # CSV 임포트 시 같은 endpoint 가 다른 repo 라벨로 들어와도 기존 row 를
            # 덮어쓰기만 하고 새로 만들지 않는다 (사용자 요청, 2026-05-20).
            models.UniqueConstraint(
                fields=["method", "endpoint"],
                name="uq_api_endpoint_owner_method_endpoint",
            ),
        ]
        indexes = [
            models.Index(fields=["mapped_app"]),
        ]
        verbose_name = "API 엔드포인트 담당자"
        verbose_name_plural = "API 엔드포인트 담당자"

    def __str__(self):
        return f"{self.method} {self.endpoint} ({self.repo})"


class FallbackOwners(models.Model):
    """API Endpoint 매칭이 실패한 URL 의 대체 담당자 (singleton).

    `ApiEndpointOwner` 와 매칭되지 않는 URL 의 에러 노티 발송 시점에 사용. 콤마
    다중값 (`"김태식, 김도연"`). 발송 시 각 이름을 Slack `users.list` 자동 매칭으로
    `<@U0XXX>` 멘션으로 변환 (매칭 실패는 `@이름` plain text).

    singleton — get_solo() 으로 단일 row 보장.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owners = models.CharField(
        max_length=500,
        blank=True,
        verbose_name="대체 담당자",
        help_text="콤마로 구분된 담당자 이름 (예: 김태식, 김도연). Slack 표시명과 일치해야 자동 멘션됨.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_fallback_owners"
        verbose_name = "대체 담당자"
        verbose_name_plural = "대체 담당자"

    def __str__(self):
        return self.owners or "(미설정)"

    @classmethod
    def get_solo(cls) -> "FallbackOwners":
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create(owners="")
        return obj

    def names(self) -> list[str]:
        """콤마 다중값 → 이름 리스트 (trim, 빈 토큰 제거)."""
        if not self.owners:
            return []
        return [n.strip() for n in self.owners.split(",") if n.strip()]


class UiAutomationOwners(models.Model):
    """UI Test Automation(TAP) 실패 Slack 알림 담당자 (singleton).

    TAP 케이스는 API URL 매칭 개념 밖이라 `ApiEndpointOwner` 매칭이 성립하지
    않는다 — Suite/케이스 구분 없이 UI 실패 알림 전체에 이 담당자들을 멘션한다.
    콤마 다중값 (`"김태식, 김도연"`), 발송 시 `slack_users.find_user_id_by_name`
    자동 매칭으로 `<@U0XXX>` 멘션 (매칭 실패는 `@이름` plain text).

    singleton — get_solo() 으로 단일 row 보장.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owners = models.CharField(
        max_length=500,
        blank=True,
        verbose_name="UI 알림 담당자",
        help_text="콤마로 구분된 담당자 이름 (예: 김태식, 김도연). Slack 표시명과 일치해야 자동 멘션됨.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_ui_automation_owners"
        verbose_name = "UI 자동화 알림 담당자"
        verbose_name_plural = "UI 자동화 알림 담당자"

    def __str__(self):
        return self.owners or "(미설정)"

    @classmethod
    def get_solo(cls) -> "UiAutomationOwners":
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create(owners="")
        return obj

    def names(self) -> list[str]:
        """콤마 다중값 → 이름 리스트 (trim, 빈 토큰 제거)."""
        if not self.owners:
            return []
        return [n.strip() for n in self.owners.split(",") if n.strip()]


class OwnerSyncSettings(models.Model):
    """담당자 마스터 Google Sheets 자동 동기화 주기 설정 (singleton).

    `interval_minutes` 가 자동 동기화 주기(분). **0 이면 자동 동기화 끔** (수동
    버튼/CLI 만 동작). 실제 트리거는 매분 도는 cron
    (`sync_api_owners_from_sheet --if-due`) 이며, 그 명령이 이 값과 마지막 동기화
    시각(ActivityLog API_OWNER_IMPORTED) 을 비교해 주기 도래 시에만 동기화한다 —
    즉 주기 변경은 crontab 수정 없이 이 화면에서만 한다.

    singleton — get_solo() 으로 단일 row 보장.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interval_minutes = models.PositiveIntegerField(
        default=0,
        verbose_name="자동 동기화 주기(분)",
        help_text="0 이면 자동 동기화를 끈다 (수동 동기화만). 예: 10 → 10분마다.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_owner_sync_settings"
        verbose_name = "담당자 동기화 주기"
        verbose_name_plural = "담당자 동기화 주기"

    def __str__(self):
        return "자동 동기화 꺼짐" if not self.interval_minutes else f"{self.interval_minutes}분마다"

    @classmethod
    def get_solo(cls) -> "OwnerSyncSettings":
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create(interval_minutes=0)
        return obj


class ApiHostMapping(models.Model):
    """`${BFF_URL}` → `bff-general-*.dev.albamon.com` 같은 환경변수→호스트 매핑.

    같은 변수가 환경(dev/stg/prd) 별로 다른 호스트일 수 있으므로 env 도 키에 포함.
    """

    class Env(models.TextChoices):
        DEV = "dev", "Dev"
        STG = "stg", "Staging"
        PRD = "prd", "Production"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    variable_name = models.CharField(
        max_length=64, verbose_name="환경변수명"
    )  # 'BFF_URL' (${} 제외 저장)
    env = models.CharField(
        max_length=8, choices=Env.choices, default=Env.DEV, verbose_name="환경"
    )
    host_pattern = models.CharField(max_length=255, verbose_name="호스트 패턴")
    notes = models.TextField(blank=True, verbose_name="비고")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_api_host_mapping"
        ordering = ["variable_name", "env"]
        constraints = [
            models.UniqueConstraint(
                fields=["variable_name", "env", "host_pattern"],
                name="uq_api_host_mapping_var_env_host",
            ),
        ]
        verbose_name = "API 호스트 매핑"
        verbose_name_plural = "API 호스트 매핑"

    def __str__(self):
        return f"${{{self.variable_name}}} [{self.env}] → {self.host_pattern}"


class WebhookLog(models.Model):
    class Source(models.TextChoices):
        JENKINS = "jenkins", "Jenkins"
        TAS = "tas", "TAS"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=30, choices=Source.choices)
    endpoint = models.CharField(max_length=200)
    payload_hash = models.CharField(max_length=64)
    signature_valid = models.BooleanField(default=False)
    http_status = models.IntegerField(default=200)
    processing_ms = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "integrations_webhook_log"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_source_display()} - {self.http_status} ({self.created_at:%H:%M:%S})"
