from django.contrib import admin
from .models import Service, Environment


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "tribe_name", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "code", "tribe_name"]


@admin.register(Environment)
class EnvironmentAdmin(admin.ModelAdmin):
    list_display = ["name", "trigger_type", "gate_mode", "next_env_control"]
