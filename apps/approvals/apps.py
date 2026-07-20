"""AppConfig for the approvals app."""

from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    """Configuration for the Approvals application."""

    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.approvals"
    verbose_name: str = "Approvals"
