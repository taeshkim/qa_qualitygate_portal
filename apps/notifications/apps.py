"""AppConfig for the notifications app."""

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """Configuration for the Notifications application."""

    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.notifications"
    verbose_name: str = "Notifications"
