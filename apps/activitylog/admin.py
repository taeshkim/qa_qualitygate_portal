from django.contrib import admin

from .models import ActivityLog


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "target_type", "target_name", "detail")
    list_filter = ("action", "target_type")
    search_fields = ("target_name", "detail")
    readonly_fields = ("created_at",)
