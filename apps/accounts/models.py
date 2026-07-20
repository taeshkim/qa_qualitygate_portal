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
