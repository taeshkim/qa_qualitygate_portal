from django.contrib import admin

from .models import RobustnessTest, RobustnessSchedule, RobustnessRun


@admin.register(RobustnessTest)
class RobustnessTestAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active", "created_at"]
    search_fields = ["name"]


@admin.register(RobustnessSchedule)
class RobustnessScheduleAdmin(admin.ModelAdmin):
    list_display = [
        "robustness_test", "test_suite", "interval_minutes",
        "is_running", "started_at", "last_triggered_at",
    ]
    list_filter = ["is_running"]


@admin.register(RobustnessRun)
class RobustnessRunAdmin(admin.ModelAdmin):
    list_display = ["schedule", "status", "triggered_at", "completed_at"]
    list_filter = ["status"]
