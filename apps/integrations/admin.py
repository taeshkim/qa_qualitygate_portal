from django.contrib import admin
from .models import (
    JenkinsConnection, JenkinsJobMapping, SlackChannelMapping,
    IntegrationAuditLog, WebhookLog,
)


@admin.register(JenkinsConnection)
class JenkinsConnectionAdmin(admin.ModelAdmin):
    list_display = ["name", "base_url", "api_user", "is_healthy", "is_active", "updated_at"]
    list_filter = ["is_healthy", "is_active"]


@admin.register(JenkinsJobMapping)
class JenkinsJobMappingAdmin(admin.ModelAdmin):
    list_display = ["service", "environment", "test_type", "jenkins_connection", "job_path", "is_active"]
    list_filter = ["test_type", "is_active"]


@admin.register(SlackChannelMapping)
class SlackChannelMappingAdmin(admin.ModelAdmin):
    list_display = ["service", "channel_type", "channel_id", "channel_name", "is_active"]
    list_filter = ["channel_type", "is_active"]


@admin.register(IntegrationAuditLog)
class IntegrationAuditLogAdmin(admin.ModelAdmin):
    list_display = ["integration_type", "action", "target_id", "changed_by", "created_at"]
    list_filter = ["integration_type", "action"]
    readonly_fields = ["created_at"]


@admin.register(WebhookLog)
class WebhookLogAdmin(admin.ModelAdmin):
    list_display = ["source", "endpoint", "signature_valid", "http_status", "processing_ms", "created_at"]
    list_filter = ["source", "signature_valid", "http_status"]
    readonly_fields = ["created_at"]
