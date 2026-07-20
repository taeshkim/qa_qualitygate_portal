from django.contrib import admin
from .models import ExceptionApproval


@admin.register(ExceptionApproval)
class ExceptionApprovalAdmin(admin.ModelAdmin):
    list_display = [
        "service", "environment", "requested_by", "risk_level",
        "status", "approved_by", "created_at",
    ]
    list_filter = ["status", "risk_level"]
    search_fields = ["reason"]
