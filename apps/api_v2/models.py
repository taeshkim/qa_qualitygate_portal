"""API v2 인증용 모델.

ApiKey — 외부 시스템(Jenkins / GitHub Actions / 백오피스 등) 이 `/api/v2/` 에 접근할 때
사용하는 다중 발급/회수 가능한 API Key. 평문은 발급 시점에만 1회 응답으로 노출되고
DB 에는 SHA-256 해시만 저장한다.

PRD §4.1 의 필드 스키마를 그대로 따른다.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta
from typing import Optional, Tuple

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import scopes as _scopes

KEY_PREFIX_LENGTH = 8
KEY_SECRET_LENGTH = 32  # hex chars
KEY_PLAINTEXT_FORMAT = "qgp_{prefix}_{secret}"
DEFAULT_EXPIRY_DAYS = 90
LAST_USED_WRITE_THROTTLE_SECS = 60


def _generate_secret() -> str:
    return secrets.token_hex(KEY_SECRET_LENGTH // 2)


def _generate_prefix() -> str:
    # 16진수 8자리 — URL/Authorization 헤더에서 식별용
    return secrets.token_hex(KEY_PREFIX_LENGTH // 2)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ApiKey(models.Model):
    """외부 호출용 API 키. 평문은 1회만 노출되고 해시만 보관."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=80,
        help_text="식별 라벨 (사용자별 unique). 예: Jenkins-CI, Backoffice-Dashboard",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_keys",
    )
    key_prefix = models.CharField(max_length=KEY_PREFIX_LENGTH, db_index=True, unique=True)
    key_hash = models.CharField(max_length=64)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "api_v2_apikey"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="api_v2_apikey_user_name_uniq"),
        ]
        indexes = [
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.key_prefix}…) — {self.user_id}"

    # ---- 상태 헬퍼 ----

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())

    @property
    def is_active(self) -> bool:
        return not self.is_revoked and not self.is_expired

    # ---- 발급 / 검증 ----

    @classmethod
    def issue(
        cls,
        *,
        user,
        name: str,
        scopes: list[str],
        expires_in_days: Optional[int] = DEFAULT_EXPIRY_DAYS,
    ) -> Tuple["ApiKey", str]:
        """새 키 발급. (ApiKey 인스턴스, 평문 문자열) 반환.

        평문은 호출자가 즉시 사용자에게 응답으로 돌려주고 폐기해야 한다 (DB 에는 해시만).
        """
        prefix = _generate_prefix()
        # prefix 충돌 회피 (확률은 매우 낮지만 unique 제약 있음)
        while cls.objects.filter(key_prefix=prefix).exists():
            prefix = _generate_prefix()
        secret = _generate_secret()
        plaintext = KEY_PLAINTEXT_FORMAT.format(prefix=prefix, secret=secret)
        expires_at = None
        if expires_in_days:
            expires_at = timezone.now() + timedelta(days=expires_in_days)
        obj = cls.objects.create(
            user=user,
            name=name[:80],
            key_prefix=prefix,
            key_hash=hash_secret(secret),
            scopes=list(scopes or []),
            expires_at=expires_at,
        )
        return obj, plaintext

    @classmethod
    def find_by_plaintext(cls, plaintext: str) -> Optional["ApiKey"]:
        """`qgp_{prefix}_{secret}` 형식의 평문 → DB 의 ApiKey 매칭 (constant-time)."""
        parts = (plaintext or "").split("_", 2)
        if len(parts) != 3 or parts[0] != "qgp":
            return None
        prefix, secret = parts[1], parts[2]
        if len(prefix) != KEY_PREFIX_LENGTH or not secret:
            return None
        try:
            key = cls.objects.select_related("user").get(key_prefix=prefix)
        except cls.DoesNotExist:
            return None
        expected = hash_secret(secret)
        if not hmac.compare_digest(expected, key.key_hash):
            return None
        return key

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at", "updated_at"])

    def touch_last_used(self) -> None:
        """과도한 write 를 막기 위해 60초 throttle."""
        now = timezone.now()
        if (
            self.last_used_at is None
            or (now - self.last_used_at).total_seconds() >= LAST_USED_WRITE_THROTTLE_SECS
        ):
            self.last_used_at = now
            type(self).objects.filter(pk=self.pk).update(last_used_at=now)

    # ---- 스코프 ----

    def has_scope(self, required: str) -> bool:
        return _scopes.has_scope(self.scopes or [], required)
