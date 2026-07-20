"""AppConfig for the dashboard app."""

from django.apps import AppConfig


class DashboardConfig(AppConfig):
    """Configuration for the Dashboard application."""

    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.dashboard"
    verbose_name: str = "Dashboard"
