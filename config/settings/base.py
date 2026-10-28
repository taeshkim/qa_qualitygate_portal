import os
import re
from pathlib import Path
from urllib.parse import urlparse
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")

DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# Nginx가 이 접두 경로를 제거해 전달하는 하위 경로 배포용 (미설정 = 루트).
_script_name = os.getenv("QGP_SCRIPT_NAME", "").strip().rstrip("/")
if _script_name and not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", _script_name):
    raise ImproperlyConfigured("QGP_SCRIPT_NAME must be an absolute path such as /qg.")
FORCE_SCRIPT_NAME = _script_name or None

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        "https://qualitygate.jkintra.net",
    ).split(",")
    if o.strip()
]

# ALB(HTTPS) → EC2(HTTP) 프록시 구조에서 Django가 원 요청을 HTTPS로 인식하게 함
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "django_filters",
    "corsheaders",
    "django_celery_beat",
    "drf_spectacular",          # /api/v2/ OpenAPI 스키마 생성
    "drf_spectacular_sidecar",  # Swagger UI/ReDoc 정적 에셋 로컬 서빙 (인트라넷 — CDN 미의존)
    # Local apps
    "apps.accounts",
    "apps.services",
    "apps.gates",
    "apps.approvals",
    "apps.escalations",
    "apps.integrations",
    "apps.dashboard",
    "apps.suites",
    "apps.qualitygates",
    "apps.robustness",
    "apps.api",
    "apps.api_v2",
    "apps.activitylog",
    "apps.notifications",
    "apps.visualization",
    "apps.syncops",
]

MIDDLEWARE = [
    # 게이트웨이 신뢰 경계 — X-Auth-* 헤더는 TRUSTED_PROXY_CIDRS 출발지 것만 남긴다(반드시 맨 앞).
    "apps.accounts.middleware.ProxyBoundaryMiddleware",
    # /ci-api/ (게이트웨이 로그인 예외 경로)는 쿠키·X-Auth-* 를 버리고 API Key 로만 인증 — SessionMiddleware 보다 앞.
    "apps.accounts.middleware.CiApiBoundaryMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # 게이트웨이가 확인한 사용자와 앱 세션 사용자가 다르면 세션 폐기(AuthenticationMiddleware 뒤).
    "apps.accounts.middleware.GatewaySessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.accounts.middleware.RolePermissionMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.portal_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "quality_gate_portal"),
        "USER": os.getenv("DB_USER", "qgp_user"),
        "PASSWORD": os.getenv("DB_PASSWORD", "qgp_password"),
        "HOST": os.getenv("DB_HOST", "db"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Custom User Model
AUTH_USER_MODEL = "accounts.User"

# Session — 로그인 후 24시간이 지나면 만료. 매 요청마다 갱신하지 않음(절대 만료).
SESSION_COOKIE_AGE = 60 * 60 * 24  # 24h in seconds
SESSION_SAVE_EVERY_REQUEST = False
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_PATH = f"{_script_name}/"
CSRF_COOKIE_PATH = SESSION_COOKIE_PATH

# Internationalization
LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

# Static files
# runserver 초기 검사에서 SCRIPT_NAME 설정 전 캐시돼도 올바른 외부 URL을 유지한다.
STATIC_URL = f"{_script_name}/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    # /api/v2/ 전용 — 뷰에서 명시 지정하지 않으면 영향 없음.
    "EXCEPTION_HANDLER": "apps.api_v2.exceptions.exception_handler",
    "DEFAULT_THROTTLE_RATES": {
        "api_v2_read": "120/min",
        "api_v2_write": "30/min",
    },
    # 스키마 생성 전용 — 런타임 API 동작(인증/응답/직렬화)에는 영향 없음.
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# /api/v2/export/ (서버 이관용 DB 스냅샷 내보내기) — 기본값(None)은 각각
# DATABASES["default"]["NAME"] / BASE_DIR/"snapshots". 테스트에서 임시 DB·임시 디렉터리를 지정하기 위한 옵션.
SYNCOPS_EXPORT_DB_PATH = None
SYNCOPS_EXPORT_DIR = None

# API 문서에 안내하는 호출 주소. 게이트웨이 로그인 예외 경로 `/ci-api/`(쿠키 없이 API Key 로만 인증).
# QGP_BASE_URL(외부 주소, 예: https://qw.jkintra.net/qg) 이 없으면 운영 주소로 안내한다.
CI_API_BASE = (os.getenv("QGP_BASE_URL", "").strip().rstrip("/") or "https://qw.jkintra.net/qg") + "/ci-api"

# drf-spectacular — /api/v2/ Swagger 문서 (기존 API 명세/동작 불변, 문서만 추가)
SPECTACULAR_SETTINGS = {
    "TITLE": "Quality Gate Portal API",
    "DESCRIPTION": (
        "화면에서 하던 일(테스트 실행, 결과 확인)을 **코드/스크립트로 자동화**할 수 있는 REST API 입니다.\n"
        "\n"
        "### 🚀 빠른 시작 (3단계)\n"
        "\n"
        "| 단계 | 하는 일 |\n"
        "|---|---|\n"
        "| **1. 키 발급** | 포털 [API Keys](/accounts/api-keys/) 에서 발급 — `qgp_...` 키는 **발급 직후 1회만 표시**되니 그 자리에서 복사 |\n"
        "| **2. 키 등록** | 우측 상단 **Authorize** 버튼에 키 입력 → ✅ 초록 배지가 뜨면 준비 완료 (틀린 키는 ❌ 후 자동 해제 — 다시 입력) |\n"
        "| **3. 호출 연습** | 아래 목록에서 항목 펼치기 → **Try it out** → **Execute**. 첫 연습은 `GET /me/` 추천 |\n"
        "\n"
        "> **Parameters 는 빨간 `required` 표시만 필수**입니다. 아무것도 안 넣고 Execute 해도 기본값으로 동작하며, 각 칸 설명의 `예:` 가 넣을 수 있는 값입니다.\n"
        "\n"
        "### 🌐 호출 주소와 인증 방법 (Jenkins · 스크립트 · CI)\n"
        "\n"
        "프로그램에서 호출할 때는 아래 **전용 주소**를 쓰세요. 이 주소는 포털 로그인(SSO) 없이 **API Key 만으로** 인증합니다 (쿠키·SSO 헤더는 무시).\n"
        "\n"
        "| 항목 | 값 |\n"
        "|---|---|\n"
        f"| **토큰 사용 주소 (Base URL)** | `{CI_API_BASE}/` — 아래 목록의 경로가 이미 이 주소 기준이며, **Try it out 도 이 주소로 호출**됩니다 |\n"
        "| **인증 (Bearer)** | `Authorization: Bearer qgp_...` |\n"
        "| **인증 (Basic)** | `curl -u x:qgp_...` 처럼 **비밀번호 자리에 키** (Jenkins 자격증명 등 Bearer 를 못 쓸 때). 아이디/비밀번호 로그인은 안 됩니다 |\n"
        f"| **연결 확인** | `GET {CI_API_BASE}/health/` (키 불필요) → 키 확인은 `GET {CI_API_BASE}/me/` |\n"
        "\n"
        f"예: `curl -H \"Authorization: Bearer $QGP_KEY\" {CI_API_BASE}/suites/`\n"
        "\n"
        "> 브라우저 주소(`/qg/api/v2/…`)는 SSO 로그인이 필요해, 키만 보내면 **HTML 401(nginx)** 로 막힙니다. 프로그램은 반드시 `/ci-api/` 를 쓰세요(이 문서 자체와 DB 내보내기는 브라우저 `/api/v2/` 에서만 제공). 응답에 담긴 링크(`self_url` 등)도 `/ci-api/…` 로 나옵니다.\n"
        "\n"
        "### 🔁 대표 흐름 — 테스트 묶음 실행하고 결과 받기\n"
        "\n"
        "`GET /suites/` 에서 id 확인 → `POST /suites/{id}/runs/` 로 실행 시작 → `GET /suite-runs/{id}/status/` 를 몇 초 간격으로 반복 조회(폴링) — `result` 가 `passed`(통과) 또는 `failed`(실패)로 바뀌면 종료입니다 (`null` = 진행 중).\n"
        "\n"
        "### 📖 용어 사전\n"
        "\n"
        "| 용어 | 뜻 |\n"
        "|---|---|\n"
        "| **Test Suite** | 함께 실행되는 자동화 테스트 묶음 (Jenkins 폴더 1개) |\n"
        "| **Job** (worker) | 스위트 안의 개별 테스트 실행 단위 |\n"
        "| **Run** | \"1회 실행 기록\" — Suite Run(스위트 1회), Quality Gate Run(배포 파이프라인 1회) |\n"
        "| **Quality Gate** | 배포 전 품질 검증 파이프라인 — 이미지 빌드 → 배포 → 테스트 → 운영 반영 |\n"
        "| **Scope** | API Key 의 권한 — `suites:read` 조회, `runs:trigger` 실행 시작 등. 부족하면 403 |\n"
        "| **폴링** | 끝났는지 반복 조회 — 응답 헤더 `X-QGP-Poll-After` 가 권장 간격(초) |\n"
        "\n"
        "### ⚠️ 오류 코드 읽는 법\n"
        "\n"
        "| HTTP | 의미 | 대처 |\n"
        "|---|---|---|\n"
        "| 401 | 키가 없거나 잘못됨 | Authorize 에 올바른 `qgp_` 키 재입력 |\n"
        "| 403 | 권한(scope) 부족 | 키의 scope 확인 (`GET /me/`) |\n"
        "| 404 | 대상 없음 | id 를 목록 API 에서 다시 확인 |\n"
        "| 409 | 지금 상태에선 불가 (예: `all_workers_busy`) | 응답 `code` 확인 후 잠시 뒤 재시도 |\n"
        "| 429 | 호출이 너무 잦음 | 잠시 후 재시도 |\n"
        "\n"
        "코드 예제가 포함된 상세 연동 가이드는 저장소의 `docs/api_guide.md` 를 참고해 주세요."
    ),
    "VERSION": "v2.0",
    # 그룹(태그)별 설명 — 문서 목록의 섹션 제목 아래에 노출.
    "TAGS": [
        {"name": "Auth & Health", "description": "연결 확인용 기본 엔드포인트입니다. `health` 는 키 없이 서버 살아있는지 확인, `me` 는 지금 쓰는 키가 유효한지·어떤 권한(scope)이 있는지 확인합니다."},
        {"name": "Meta (폴더/게이트/스위트 조회)", "description": "실행 대상을 찾기 위한 조회 전용 목록입니다. 폴더 → 하위폴더 → Quality Gate, 그리고 Test Suite 목록에서 실행에 필요한 id 를 얻습니다."},
        {"name": "Quality Gate Runs", "description": "배포 파이프라인(Quality Gate) 실행을 시작하고 진행 상황을 조회합니다. 실행 후에는 status 를 폴링해 단계(phase)별 진행과 최종 합/불(result)을 확인합니다."},
        {"name": "Quality Gate Run 제어", "description": "진행 중이거나 끝난 Quality Gate Run 에 개입하는 동작입니다 — 취소, 단계별 재실행, 배포 승인 트리거 등. 쓰기 권한(scope)이 필요합니다."},
        {"name": "Test Suite Runs", "description": "테스트 묶음(Suite) 실행을 시작하고 결과를 조회/취소합니다. 가장 많이 쓰는 흐름: 트리거 → status 폴링 → result 확인. 콘솔 로그로 실패 원인도 볼 수 있습니다."},
        {"name": "담당자 관리", "description": "담당자 관리 화면(`/accounts/api-owners/`)의 API 엔드포인트별 담당자(FE/BE) 매핑을 조회·등록·수정·삭제합니다. `owners:manage` 스코프가 명시된 **관리자 소유 키**만 호출할 수 있습니다(`*` 로는 부여되지 않음)."},
    ],
    "SERVE_INCLUDE_SCHEMA": False,
    # 브라우저 세션 로그인 사용자만 문서/스키마 열람 (인트라넷 포털 계정).
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
    "SERVE_AUTHENTICATION": ["rest_framework.authentication.SessionAuthentication"],
    # /api/v2/ 외 경로(legacy apps.api 등)는 스키마에서 제외.
    "PREPROCESSING_HOOKS": ["apps.api_v2.schema.preprocess_only_api_v2"],
    # 문서에 보이는 호출 경로를 프로그램 호출 주소(/ci-api/)로 바꾼다 — 브라우저 /api/v2/ 는 SSO 로그인이 필요해 키만으로는 못 쓴다.
    "POSTPROCESSING_HOOKS": ["apps.api_v2.schema.rewrite_paths_to_ci_api"],
    # Authorize 에 API Key(Bearer) 만 노출 — 세션 쿠키/Basic 은 문서에 표기하지
    # 않는다(외부 소비자용 문서. 실제 세션 병행 인증 동작은 불변).
    "AUTHENTICATION_WHITELIST": ["apps.api_v2.authentication.ApiKeyAuthentication"],
    "SCHEMA_PATH_PREFIX": r"/api/v2",
    "SCHEMA_PATH_PREFIX_INSERT": FORCE_SCRIPT_NAME or "",
    # Swagger UI / ReDoc 에셋을 sidecar 패키지에서 서빙 (인트라넷 — 외부 CDN 미의존)
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
}

# Celery
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/1")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/2")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "Asia/Seoul"

# Redis Cache
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.getenv("REDIS_URL", "redis://redis:6379/0"),
    }
}

# Login
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:index"
LOGOUT_REDIRECT_URL = "accounts:login"

# ── SSO (Keycloak OIDC) ─────────────────────────────────────────────
# 공급자 'internal'(사내 Keycloak)은 ISSUER/CLIENT_ID/CLIENT_SECRET 셋이 모두 있을 때만 켠다.
# 일부만 설정하면 오타·누락이므로 기동 시점에 바로 실패시킨다.
OIDC_PROVIDERS = {}
_oidc_internal = {
    key: os.getenv(f"OIDC_INTERNAL_{key.upper()}", "").strip()
    for key in ("issuer", "client_id", "client_secret")
}
if any(_oidc_internal.values()):
    if not all(_oidc_internal.values()):
        raise ImproperlyConfigured(
            "OIDC_INTERNAL_ISSUER / OIDC_INTERNAL_CLIENT_ID / OIDC_INTERNAL_CLIENT_SECRET 은 모두 설정해야 합니다."
        )
    _oidc_internal["issuer"] = _oidc_internal["issuer"].rstrip("/")
    _issuer_url = urlparse(_oidc_internal["issuer"])
    if _issuer_url.scheme != "https" and not (
        _issuer_url.scheme == "http" and _issuer_url.hostname in ("localhost", "127.0.0.1")
    ):
        raise ImproperlyConfigured("OIDC_INTERNAL_ISSUER 는 https:// 주소여야 합니다(로컬 개발용 localhost 제외).")
    # Keycloak 로그아웃(end-session) 연동. Keycloak client 에 '로그아웃 후 이동 주소'(= 포털 /accounts/login/)가 등록돼
    # 있어야 하므로 기본 꺼짐 — 미등록 상태로 켜면 Keycloak 이 오류 화면을 띄운다.
    _oidc_internal["post_logout_redirect"] = os.getenv("OIDC_INTERNAL_POST_LOGOUT_REDIRECT", "False").strip().lower() in (
        "true", "1", "yes",
    )
    OIDC_PROVIDERS["internal"] = _oidc_internal
OIDC_PROVIDER_KEYS = ["internal"]

# 이 도메인 이메일은 Keycloak 의 email_verified 없이 신뢰한다(회사가 발급·관리하는 도메인).
# 그 외 도메인은 email_verified=true 일 때만 신뢰한다.
OIDC_INTERNAL_EMAIL_DOMAINS = frozenset(
    d.strip().lstrip("@").lower()
    for d in os.getenv("OIDC_INTERNAL_EMAIL_DOMAINS", "worxphere.ai").split(",")
    if d.strip()
)

# 아이디/비밀번호 로그인·회원가입 화면 사용 여부. QG 운영은 DEBUG=True 라 DEBUG 로 판단하지 않는다.
# LOCAL_LOGIN_ACTIVE 가 실제 적용값이다: OIDC 설정(.env)이 없으면 로그인이 막히지 않도록
# LOCAL_LOGIN_ENABLED 와 무관하게 자체 로그인을 유지한다(= 안전한 배포 순서: 코드 먼저, OIDC 값은 나중).
LOCAL_LOGIN_ENABLED = os.getenv("LOCAL_LOGIN_ENABLED", "False").lower() in ("true", "1", "yes")
# 게이트웨이 진입 로그인: 앱 자체 로그인 화면(게이트웨이 비보호 경로)에 "Keycloak 로그인" 버튼을 두고, 버튼은 게이트웨이가
# 보호하는 진입 경로(/accounts/gateway-login/)로 간다 — 게이트웨이가 Keycloak 로그인을 시키고 붙여 준 X-Auth-* 헤더로 앱
# 세션을 만든다(client secret 불필요). 게이트웨이 쪽에 진입 경로만 internal 로 보호하는 라우팅이 먼저 있어야 켠다
# (docs/handoff_sso_integration.md §10). 켜면 자체 로그인(아이디/비밀번호)은 기본 꺼진다 — 비상 시 LOCAL_LOGIN_ENABLED=True.
GATEWAY_ENTRY_ENABLED = os.getenv("GATEWAY_ENTRY_ENABLED", "False").lower() in ("true", "1", "yes")
LOCAL_LOGIN_ACTIVE = LOCAL_LOGIN_ENABLED or not (OIDC_PROVIDERS or GATEWAY_ENTRY_ENABLED)

# 게이트웨이(oauth2-proxy)가 붙이는 X-Auth-* 헤더를 신뢰할 직전 프록시 CIDR(콤마). 비우면 아무도 신뢰하지 않는다.
# 미들웨어가 요청 시점에 읽는다(테스트의 override_settings 반영).
TRUSTED_PROXY_CIDRS = [c.strip() for c in os.getenv("TRUSTED_PROXY_CIDRS", "172.20.0.222/32").split(",") if c.strip()]

# 게이트웨이 뒤에서 로그아웃할 때 이동할 게이트웨이 로그아웃 URL(게이트웨이·Keycloak 세션도 함께 종료). 비우면 앱 로그인 화면.
GATEWAY_LOGOUT_URL = os.getenv("GATEWAY_LOGOUT_URL", "https://qw.jkintra.net/_qw/logout").strip()

# ── 미 장애 판정(triage) 대기 큐 집계 시작 시각 ──
# 대시보드 '미 장애 판정 분류건' 카운트/리스트는 이 시각 **이후** 발생한 미분류
# 실패 Job 만 포함한다. 기능 도입 이전의 과거 대량 미분류(수천 건)를 판정 대상에서
# 제외하기 위한 컷오프 — env 로 덮어쓸 수 있다(ISO8601, tz 포함 권장).
UNTRIAGED_QUEUE_SINCE = os.getenv("UNTRIAGED_QUEUE_SINCE", "2026-07-08T05:58:00+00:00")
