"""AppConfig for the escalations app."""

from django.apps import AppConfig


class EscalationsConfig(AppConfig):
    """Configuration for the Escalations application."""

    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.escalations"
    verbose_name: str = "Escalations"
