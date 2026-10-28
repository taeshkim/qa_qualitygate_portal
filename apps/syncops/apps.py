from django.apps import AppConfig


class SyncopsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.syncops"
    verbose_name = "서버 이관/동기화 (모델 없음)"
