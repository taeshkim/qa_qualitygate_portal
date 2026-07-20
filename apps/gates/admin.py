from django.contrib import admin
from .models import GatePolicy, GateRun, GateFailure


@admin.register(GatePolicy)
class GatePolicyAdmin(admin.ModelAdmin):
    list_display = ["service", "environment", "test_type", "blocking_mode", "pass_threshold"]
    list_filter = ["test_type", "blocking_mode"]


@admin.register(GateRun)
class GateRunAdmin(admin.ModelAdmin):
    list_display = [
        "service", "environment", "version", "result",
        "triggered_by", "duration_seconds", "created_at",
    ]
    list_filter = ["result", "triggered_by", "service", "environment"]
    search_fields = ["version", "commit_sha", "branch"]
    readonly_fields = ["created_at"]


@admin.register(GateFailure)
class GateFailureAdmin(admin.ModelAdmin):
    list_display = ["gate_run", "test_name", "failure_type", "category", "categorized_by"]
    list_filter = ["failure_type", "category"]
    search_fields = ["test_name", "error_message"]
