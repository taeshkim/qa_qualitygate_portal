from .base import *  # noqa: F401,F403

DEBUG = True

CORS_ALLOW_ALL_ORIGINS = True

# 로컬 개발: SQLite 사용 (PostgreSQL 불필요)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# 로컬 개발: 파일 기반 캐시 (Redis 불필요)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Celery 없이 동작 (task를 동기 실행)
CELERY_TASK_ALWAYS_EAGER = True
