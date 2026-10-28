import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        QPE_ADMIN = "qpe_admin", "QPE Admin"
        QPE_OPS = "qpe_ops", "QPE Ops"
        TRIBE_LEAD = "tribe_lead", "Tribe Lead"
        TRIBE_DEV = "tribe_dev", "Tribe Developer"
        APPROVER = "approver", "Approver"
        VIEWER = "viewer", "Viewer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.VIEWER,
    )
    slack_user_id = models.CharField(max_length=50, blank=True)

    class Meta:
        db_table = "accounts_user"

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"

    @property
    def is_qpe(self):
        return self.role in (self.Role.QPE_ADMIN, self.Role.QPE_OPS)

    @property
    def is_admin(self):
        return self.role == self.Role.QPE_ADMIN or self.is_superuser

    @property
    def can_write(self):
        """Viewer가 아닌 모든 역할은 쓰기(등록/실행/수정/삭제) 가능."""
        return self.role != self.Role.VIEWER

    @property
    def is_viewer(self):
        """Viewer 역할 — 조회 전용 + 접근 가능 메뉴가 Dashboard/Suite Runs
        로 제한된다 (Visualization 은 QPE Admin 전용) (사이드바 숨김 + RolePermissionMiddleware 가 GET 페이지 차단)."""
        return self.role == self.Role.VIEWER


class PortalSettings(models.Model):
    """포털 UI 설정 (singleton). 현재는 좌측 사이드바 메뉴 표시 여부.

    모든 페이지(base.html)가 context processor(`apps.accounts.context_processors`)
    로 읽는다. 메뉴 토글을 늘리려면 BooleanField 추가 + base.html 분기 + 포털 설정
    화면 폼만 늘리면 된다. singleton — get_solo() 으로 단일 row 보장.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    show_quality_gate = models.BooleanField(
        default=True,
        verbose_name="Quality Gate 메뉴 표시",
        help_text="끄면 좌측 사이드바에서 Quality Gate 메뉴가 숨겨진다 (URL 직접 접근은 가능).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_portal_settings"
        verbose_name = "포털 설정"
        verbose_name_plural = "포털 설정"

    def __str__(self):
        return "포털 설정"

    @classmethod
    def get_solo(cls) -> "PortalSettings":
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create()
        return obj


class ExternalIdentity(models.Model):
    """외부 로그인(Keycloak OIDC) 계정 ↔ 포털 User 연결.

    사람은 (issuer, subject) 로만 식별한다 — 이메일은 바뀔 수 있어 식별자로 쓰지 않는다.
    email 은 마지막 로그인 시점의 값(게이트웨이 세션 정합 확인용).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="external_identities",
    )
    provider = models.CharField(max_length=30)
    issuer = models.CharField(max_length=255)
    subject = models.CharField(max_length=255)
    email = models.CharField(max_length=254, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "accounts_external_identity"
        constraints = [
            models.UniqueConstraint(fields=["issuer", "subject"], name="uniq_external_identity_issuer_subject"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.user_id}"
