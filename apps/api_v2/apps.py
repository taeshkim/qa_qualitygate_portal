from django.apps import AppConfig


class ApiV2Config(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.api_v2"
    verbose_name = "QGP API v2"

    def ready(self):
        # drf-spectacular 인증 스킴 확장 등록 (import 부수효과) — 문서 생성 전용,
        # API 런타임 동작에는 영향 없음.
        from . import schema  # noqa: F401
