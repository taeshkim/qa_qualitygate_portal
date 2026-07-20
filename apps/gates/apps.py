"""AppConfig for the gates app."""

from django.apps import AppConfig


class GatesConfig(AppConfig):
    """Configuration for the Gates application."""

    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.gates"
    verbose_name: str = "Gates"
