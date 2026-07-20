from django.contrib import admin
from .models import Escalation


@admin.register(Escalation)
class EscalationAdmin(admin.ModelAdmin):
    list_display = ["gate_run", "level", "status", "assigned_to", "is_auto_generated", "created_at"]
    list_filter = ["level", "status", "is_auto_generated"]
