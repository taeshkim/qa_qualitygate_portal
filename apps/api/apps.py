"""AppConfig for the api app."""

from django.apps import AppConfig


class ApiConfig(AppConfig):
    """Configuration for the API application."""

    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.api"
    verbose_name: str = "API"
