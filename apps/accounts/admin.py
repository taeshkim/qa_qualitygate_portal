from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import ExternalIdentity, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["username", "email", "role", "is_active", "is_staff"]
    list_filter = ["role", "is_active", "is_staff"]
    fieldsets = BaseUserAdmin.fieldsets + (
        ("QGP", {"fields": ("role", "slack_user_id")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("QGP", {"fields": ("role",)}),
    )


@admin.register(ExternalIdentity)
class ExternalIdentityAdmin(admin.ModelAdmin):
    """SSO 연결 내역 — 읽기 전용(삭제는 superuser 만: 연결을 끊으면 다음 SSO 로그인 때 이메일로 재연결/신규 생성)."""

    list_display = ["user", "provider", "email", "created_at", "last_login_at"]
    list_filter = ["provider"]
    search_fields = ["user__username", "email", "subject"]
    readonly_fields = ["user", "provider", "issuer", "subject", "email", "created_at", "last_login_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
