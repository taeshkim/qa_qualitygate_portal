from django.conf import settings
from django.db import models


class ActivityLog(models.Model):
    class Action(models.TextChoices):
        CREATE = "create", "생성"
        UPDATE = "update", "수정"
        DELETE = "delete", "삭제"
        RUN = "run", "실행"
        CANCEL = "cancel", "취소"
        RETRY = "retry", "재시도"
        LOGIN = "login", "로그인"
        LOGOUT = "logout", "로그아웃"
        LOGIN_FAILED = "login_failed", "로그인 실패"
        API_KEY_CREATED = "api_key_created", "API 키 발급"
        API_KEY_REVOKED = "api_key_revoked", "API 키 회수"
        API_RUN_TRIGGERED = "api_run_triggered", "API 로 Run 트리거"
        API_OWNER_CREATED = "api_owner_created", "담당자 생성"
        API_OWNER_UPDATED = "api_owner_updated", "담당자 수정"
        API_OWNER_DELETED = "api_owner_deleted", "담당자 삭제"
        API_OWNER_IMPORTED = "api_owner_imported", "담당자 CSV 임포트"
        API_HOST_CREATED = "api_host_created", "호스트 매핑 생성"
        API_HOST_UPDATED = "api_host_updated", "호스트 매핑 수정"
        API_HOST_DELETED = "api_host_deleted", "호스트 매핑 삭제"
        OWNER_NOTIFIED = "owner_notified", "담당자 Slack 노티 발송"

    class TargetType(models.TextChoices):
        GATE_FOLDER = "gate_folder", "Quality Gate 인스턴스"
        GATE = "gate", "Quality Gate"
        GATE_RUN = "gate_run", "Quality Gate Run"
        SUITE = "suite", "Test Suite"
        SUITE_RUN = "suite_run", "Test Suite Run"
        RELIABILITY_TEST = "reliability_test", "Reliability Test"
        RELIABILITY_SCHEDULE = "reliability_schedule", "Reliability Test 스케줄"
        USER = "user", "사용자"
        API_ENDPOINT_OWNER = "api_endpoint_owner", "API Endpoint Owner"
        API_HOST_MAPPING = "api_host_mapping", "API Host Mapping"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="activity_logs",
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    target_type = models.CharField(max_length=30, choices=TargetType.choices)
    target_id = models.CharField(max_length=40, blank=True, default="")
    target_name = models.CharField(max_length=300, blank=True, default="")
    detail = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "activity_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["target_type", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.user or 'system'} {self.action} {self.target_type}:{self.target_name}"

    @classmethod
    def log(cls, user, action, target_type, target=None, target_name="", detail=""):
        """간편 기록 헬퍼. target 이 객체면 pk/str 자동 추출."""
        target_id = ""
        if target is not None:
            target_id = str(getattr(target, "pk", "") or "")
            if not target_name:
                target_name = str(target)[:300]
        return cls.objects.create(
            user=user if getattr(user, "is_authenticated", False) else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name[:300],
            detail=detail[:500],
        )
