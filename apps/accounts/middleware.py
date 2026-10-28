import logging
from ipaddress import ip_address, ip_network

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse

# 공용 게이트웨이(oauth2-proxy)가 붙이는 사용자 식별 헤더의 META 접두어. 게이트웨이 밖에서 온 값은 버린다.
GATEWAY_HEADER_PREFIX = "HTTP_X_AUTH_"


logger = logging.getLogger(__name__)

# Viewer(조회 전용)가 GET 으로 열람 가능한 페이지 — 그 외 portal 페이지는 차단.
# 허용 메뉴: Dashboard(/) · Suite Runs(실행 조회). Visualization 은 QPE Admin 전용(2026-10-21).
# "Suite Runs" 열람에는 실행 목록/상세/상태폴링(/suites/runs/) + 콘솔/에러로그
# (/suites/job-runs/) 가 포함된다. Test Suites 설정(/suites/, /suites/<pk>/), Queue,
# Reliability, Quality Gate, Activity Log, Admin 은 막는다.
_VIEWER_GET_ALLOW_EXACT = ("/", "/health/", "/sw.js", "/favicon.ico", "/favicon.svg")
_VIEWER_GET_ALLOW_PREFIX = (
    "/suites/runs/",       # Suite Run 목록·상세·상태 폴링
    "/suites/job-runs/",   # Job Run 콘솔·에러로그
    "/static/",            # 정적 파일
    "/media/",
    "/api/",               # 프로그램 API — 자체 인증 처리, enforcement 제외
    "/ci-api/",            # 게이트웨이 로그인 예외 API — API Key 자체 인증
    "/accounts/login",     # 로그인
    "/accounts/oidc/",     # SSO 로그인 시작·콜백
    "/accounts/gateway-login",  # 게이트웨이 진입 로그인(viewer 첫 로그인도 next 로 가야 한다)
    "/accounts/logout",    # 로그아웃
)


def _viewer_page_allowed(path: str) -> bool:
    """Viewer 가 GET 으로 접근 가능한 경로면 True."""
    if path in _VIEWER_GET_ALLOW_EXACT:
        return True
    return path.startswith(_VIEWER_GET_ALLOW_PREFIX)


# 모든 사용자에게 POST가 허용되어야 하는 경로
POST_ALWAYS_ALLOW = (
    "/accounts/logout/",
    # API Key 발급/회수 — 모든 인증 사용자(Viewer 포함)가 자기 키를 관리할 수 있어야 한다.
    # admin 전용 페이지(/accounts/api-keys/admin/)는 view 단의 _require_admin 으로 별도 차단.
    "/accounts/api-keys/",
)

# 일반 역할(non-admin)이 POST 가능한 경로 패턴 (실행/시작/중단 등 operational)
REGULAR_POST_ALLOW = (
    "/run",           # suite_run, gate_run
    "/start/",        # schedule_start
    "/start-all/",    # schedule_start_all
    "/stop/",         # schedule_stop
    "/stop-all/",     # schedule_stop_all, queue_stop_all
    "/trigger/",      # schedule_trigger_now
    "/status/",       # HTMX polling
    "/rerun/",        # rerun_gate
    "/deploy/",       # prod_deploy
    "/reset/",        # schedule_reset_stats
    "/triage/",       # triage_failure
    "/cancel/",       # tap_run_cancel (TAP UI 테스트 취소) — 기존 취소 URL 들은
                      # "/run" 부분일치로 우연히 통과했지만 /suites/tap-runs/<pk>/cancel/
                      # 은 하이픈 구조라 명시 필요. 뷰 단에서 can_write 재검증.
)


class ProxyBoundaryMiddleware:
    """게이트웨이(X-Auth-*) 헤더 신뢰 경계.

    REMOTE_ADDR 이 settings.TRUSTED_PROXY_CIDRS 안일 때만 X-Auth-* 헤더를 남긴다. 그 밖(또는 CIDR 이 비어 있거나
    출발지를 해석할 수 없는 경우)에는 모두 삭제해, 게이트웨이를 거치지 않은 요청이 헤더로 다른 사람 행세를
    하거나 SSO 자동시작을 유도하지 못하게 한다. 요청 시점에 settings 를 읽는다(override_settings 반영).
    ALB 의 X-Forwarded-Proto 처리(SECURE_PROXY_SSL_HEADER)는 건드리지 않는다.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _trusted(request) -> bool:
        try:
            peer = ip_address(request.META.get("REMOTE_ADDR", ""))
        except ValueError:
            return False
        # IPv4-mapped IPv6(::ffff:a.b.c.d)는 IPv4 로 정규화해 IPv4 CIDR 과 비교한다(듀얼스택 소켓 대응).
        peer = getattr(peer, "ipv4_mapped", None) or peer
        for cidr in settings.TRUSTED_PROXY_CIDRS:
            try:
                if peer in ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
        return False

    def __call__(self, request):
        if not self._trusted(request):
            dropped = [k for k in request.META if k.startswith(GATEWAY_HEADER_PREFIX)]
            for key in dropped:
                del request.META[key]
            if dropped:
                # 게이트웨이 출발지가 TRUSTED_PROXY_CIDRS 에 없으면 SSO 가 조용히 안 되므로 출발지를 남긴다(값은 남기지 않음).
                logger.warning(
                    "신뢰 밖 출발지의 X-Auth-* 헤더 제거 peer=%s headers=%s — 게이트웨이 출발지면 TRUSTED_PROXY_CIDRS 에 추가",
                    request.META.get("REMOTE_ADDR", ""), ",".join(sorted(k[5:] for k in dropped)),
                )
        return self.get_response(request)


# 게이트웨이 로그인 예외 경로 — 게이트웨이(nginx)가 SSO 쿠키 없이도 통과시키므로 앱이 API Key 로만 인증한다.
CI_API_PREFIX = "/ci-api/"


class CiApiBoundaryMiddleware:
    """`/ci-api/` 요청은 쿠키·SSO 헤더를 모두 무시하고 API Key(Bearer/Basic)로만 인증한다.

    이 경로는 게이트웨이가 로그인 검사를 하지 않는다. 그래서 (1) 세션 쿠키로 로그인한 사용자 행세(CSRF 우회 포함),
    (2) 클라이언트가 보낸 X-Auth-* 헤더로 GatewaySessionMiddleware 가 자동 로그인시키는 것을 막아야 한다.
    SessionMiddleware 가 쿠키를 읽기 전에 Cookie 헤더와 X-Auth-* 를 제거하고(반드시 SessionMiddleware 앞),
    request.urlconf 를 config.urls_ci 로 바꿔 `/ci-api/` 아래 reverse() 결과도 `/ci-api/...` 가 되게 한다.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info.startswith(CI_API_PREFIX):
            request.META.pop("HTTP_COOKIE", None)
            for key in [k for k in request.META if k.startswith(GATEWAY_HEADER_PREFIX)]:
                del request.META[key]
            request.urlconf = "config.urls_ci"
            request.is_ci_api = True
        return self.get_response(request)


class GatewaySessionMiddleware:
    """게이트웨이(oauth2-proxy)가 확인한 사용자로 앱 세션을 맞춘다 (docs/service-sso-migration.md §3.1·§4).

    - 게이트웨이를 통과한 요청에는 X-Auth-Request-Email / X-Auth-Realm 이 붙는다(신뢰 출발지 것만 남음 — ProxyBoundaryMiddleware).
    - 세션 사용자 ≠ 헤더 사용자이면 세션을 폐기한다(공용 PC 에서 A 로그아웃 후 B 로그인 시 A 세션 잔존 방지).
    - 비로그인이고 realm 이 world(사내)이면 헤더 이메일로 앱 사용자를 찾아(없으면 viewer 로 생성) 바로 로그인시킨다.
      Keycloak client 정보·비밀번호가 필요 없다. 헤더가 없으면(게이트웨이 밖) 아무것도 하지 않고 기존 로그인 화면으로 폴백한다.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    ENTRY_PATH = "/accounts/gateway-login"

    def __call__(self, request):
        if settings.GATEWAY_ENTRY_ENABLED and request.path_info.rstrip("/") != self.ENTRY_PATH:
            # 진입 모드: 게이트웨이가 보호하는 경로는 진입 경로 하나뿐이다. 다른 경로에서 온 X-Auth-* 는 게이트웨이가 안 붙인
            # 것이므로(신뢰 출발지를 거쳤어도 클라이언트가 보낸 값일 수 있다 — 게이트웨이 설정 실수 방어) 전부 버린다.
            for key in [k for k in request.META if k.startswith("HTTP_X_AUTH_")]:
                del request.META[key]
        email = request.META.get("HTTP_X_AUTH_REQUEST_EMAIL", "").strip().lower()
        if email:
            if request.user.is_authenticated and not self._matches(request.user, email):
                logout(request)
            if not request.user.is_authenticated:
                from .sso import login_by_gateway_header  # 순환 import 방지

                login_by_gateway_header(request, email)
        return self.get_response(request)

    @staticmethod
    def _matches(user, email: str) -> bool:
        if (user.email or "").strip().lower() == email:
            return True
        return any(
            (linked or "").strip().lower() == email
            for linked in user.external_identities.values_list("email", flat=True)
        )


class RolePermissionMiddleware:
    """3단계 권한 체계:
    - Admin (QPE Admin / superuser): 모든 작업 가능
    - 일반 역할: 실행/시작/중단만 가능 (등록/수정/삭제 불가)
    - Viewer: 조회만 가능 (모든 POST 차단) + 열람 페이지도 Dashboard/
      Suite Runs 영역으로 제한 (Visualization 은 QPE Admin 전용) (그 외 GET 페이지는 대시보드로 리다이렉트)
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # SCRIPT_NAME을 제외한 앱 내부 경로로 권한을 검사한다.
        path = request.path_info
        # Viewer 페이지 접근 제한 (GET/HEAD) — 허용 영역 외 portal 페이지는 차단.
        if request.method in ("GET", "HEAD"):
            user = request.user
            if (
                user.is_authenticated
                and getattr(user, "role", None) == "viewer"
                and not _viewer_page_allowed(path)
            ):
                # XHR(전역 알림 폴링 등)은 메시지 없이 조용히 리다이렉트 — 메시지 누적 방지.
                if request.headers.get("x-requested-with") != "XMLHttpRequest":
                    messages.error(request, "Viewer 권한으로는 접근할 수 없는 페이지입니다.")
                return redirect("dashboard:index")
            return self.get_response(request)

        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return self.get_response(request)

        # /api/v2/ 는 DRF 가 자체 권한 처리 — 미들웨어 분기 우회 (PRD §4.4)
        if path.startswith(("/api/v2/", CI_API_PREFIX)):
            return self.get_response(request)

        if not request.user.is_authenticated or not hasattr(request.user, "role"):
            return self.get_response(request)

        # 항상 허용되는 경로
        if any(path.startswith(p) for p in POST_ALWAYS_ALLOW):
            return self.get_response(request)

        # Admin → 전부 허용
        if request.user.is_admin:
            return self.get_response(request)

        # Viewer → 전부 차단
        if request.user.role == "viewer":
            messages.error(request, "Viewer 권한으로는 이 작업을 수행할 수 없습니다.")
            return redirect(request.META.get("HTTP_REFERER") or reverse("dashboard:index"))

        # 일반 역할 → 실행 관련 경로만 허용
        if any(seg in path for seg in REGULAR_POST_ALLOW):
            return self.get_response(request)

        # 그 외 (등록/수정/삭제) → 차단
        messages.error(request, "이 작업은 관리자만 수행할 수 있습니다.")
        return redirect(request.META.get("HTTP_REFERER") or reverse("dashboard:index"))
