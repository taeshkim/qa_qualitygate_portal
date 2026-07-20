"""AppConfig for the integrations app."""

from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    """Configuration for the Integrations application."""

    default_auto_field: str = "django.db.models.BigAutoField"
    name: str = "apps.integrations"
    verbose_name: str = "Integrations"

    def ready(self) -> None:  # noqa: D401 — Django hook
        # url_matcher 모듈을 import 해 post_save / post_delete 시그널 receiver 등록.
        from . import url_matcher  # noqa: F401
