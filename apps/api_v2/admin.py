from __future__ import annotations

from django.contrib import admin

from .models import ApiKey


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "key_prefix", "scopes", "expires_at", "revoked_at", "last_used_at")
    list_filter = ("revoked_at", "expires_at")
    search_fields = ("name", "user__username", "key_prefix")
    readonly_fields = (
        "id",
        "key_prefix",
        "key_hash",
        "last_used_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("id", "name", "user", "scopes", "expires_at", "revoked_at")}),
        ("키 (해시만)", {"fields": ("key_prefix", "key_hash"), "classes": ("collapse",)}),
        ("운영 메타", {"fields": ("last_used_at", "created_at", "updated_at"), "classes": ("collapse",)}),
    )
