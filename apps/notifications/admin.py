"""Django admin (백오피스용 — 메인 UI 는 /accounts/notifications/ 에 있음)."""

from django.contrib import admin

from .models import SlackNotificationSettings


@admin.register(SlackNotificationSettings)
class SlackNotificationSettingsAdmin(admin.ModelAdmin):
    list_display = ("enabled", "default_channel", "updated_at")

    def has_add_permission(self, request):
        return not SlackNotificationSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
