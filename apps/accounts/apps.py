"""AppConfig for the accounts app."""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Configuration for the Accounts application."""

    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.accounts"
    verbose_name: str = "Accounts"

    def ready(self):
        from . import signals  # noqa: F401
