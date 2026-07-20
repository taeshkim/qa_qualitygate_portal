"""AppConfig for the services app."""

from django.apps import AppConfig


class ServicesConfig(AppConfig):
    """Configuration for the Services application."""

    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.services"
    verbose_name: str = "Services"
