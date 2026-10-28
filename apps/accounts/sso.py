"""Keycloak(OIDC) SSO 로그인.

흐름: 로그인 버튼(또는 게이트웨이 헤더 자동시작) → Keycloak → 콜백에서 코드 교환·ID 토큰 검증 → 포털 User 연결 → 세션 로그인.

보안 규칙:
- 사람은 (issuer, sub) 로만 식별한다(ExternalIdentity). 이메일은 최초 연결 때만 쓴다.
- 이메일은 OIDC_INTERNAL_EMAIL_DOMAINS(회사 관리 도메인)이거나 email_verified=true 일 때만 믿는다.
  믿을 수 없는 이메일이면 계정 생성/연결을 모두 하지 않는다.
- 이메일이 정확히 1명의 기존 계정과 일치하면 그 계정에 연결한다(역할·비밀번호 유지). 비활성 계정은 활성화하지 않고 거부한다.
  2명 이상과 일치(모호)하면 추측해서 연결하지 않는다.
- 연결할 계정이 없으면 즉시 활성 + 기본 권한(viewer) 으로 새 계정을 만든다(사용자 결정).
- 로그에는 공급자·예외 타입명만 남긴다(토큰/claims/secret 금지).

게이트웨이(qw Nginx + oauth2-proxy) 뒤에서는 X-Auth-Realm(world|ext)·X-Auth-Request-Email 헤더가 붙는다.
비로그인 GET /accounts/login/ 에 world realm 헤더가 있으면 로그인 화면 없이 SSO 를 바로 시작한다.
헤더는 TRUSTED_PROXY_CIDRS 출발지에서 온 것만 남는다(ProxyBoundaryMiddleware).
자동시작 무한루프 방지: 콜백 실패는 리다이렉트하지 않고 로그인 템플릿을 직접 렌더하며, 로그아웃은 ?loggedout=1 을 붙인다.

로그아웃(logout_view 우선순위): (a) 게이트웨이 뒤 + GATEWAY_LOGOUT_URL → 게이트웨이가 Keycloak 세션까지 종료.
(b) 로그인 때 보관한 id_token + 공급자 post_logout_redirect=True → Keycloak end_session_endpoint(RP-Initiated Logout) 로
앱·Keycloak 세션을 함께 끝내고 로그인 화면으로 복귀. (c) 그 외 앱 세션만 끝내고 /accounts/login/?loggedout=1.
id_token 은 세션에만 두고 로그/응답/템플릿에 노출하지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlencode

from authlib.integrations.django_client import OAuth
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.db import IntegrityError, OperationalError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET

from apps.activitylog.models import ActivityLog

from .models import ExternalIdentity, User

logger = logging.getLogger(__name__)

PROVIDER_LABELS = {
    "internal": "사내 계정으로 로그인 (SSO)",
}
# 게이트웨이 X-Auth-Realm → 이 앱의 공급자. ext(협력사)는 QG 접근 대상이 아니라 매핑하지 않는다(자동시작 안 함).
GATEWAY_REALM_PROVIDERS = {"world": "internal"}

SESSION_NEXT = "sso_next"
# 로그아웃에서만 쓰는 값(Keycloak end-session 의 id_token_hint). 로그/응답/템플릿 노출 금지.
SESSION_ID_TOKEN = "sso_id_token"
SESSION_PROVIDER = "sso_provider"

_LOGIN_BACKEND = "django.contrib.auth.backends.ModelBackend"
_USERNAME_UNSAFE = re.compile(r"[^\w.@+-]")
_LOCAL_HOSTS = ("localhost", "127.0.0.1")
_clients: dict[tuple, object] = {}


# ─────────────────────────────────────────────
# 공급자·게이트웨이 헬퍼
# ─────────────────────────────────────────────

def enabled_providers() -> list[dict]:
    """로그인 화면에 표시할 공급자 목록 [{key, label, url}]."""
    return [
        {
            "key": key,
            "label": PROVIDER_LABELS.get(key, key),
            "url": reverse("accounts:sso_login", args=[key]),
        }
        for key in settings.OIDC_PROVIDER_KEYS
        if key in settings.OIDC_PROVIDERS
    ]


def gateway_realm(request) -> str:
    """게이트웨이가 확인한 realm(소문자). 없으면 "". 신뢰할 수 없는 출발지의 헤더는 미들웨어가 이미 제거했다."""
    return request.META.get("HTTP_X_AUTH_REALM", "").strip().lower()


def gateway_provider(request) -> str | None:
    """게이트웨이 realm 에 대응하고 이 앱에 설정된 공급자. 없으면 None(일반 로그인 화면)."""
    provider = GATEWAY_REALM_PROVIDERS.get(gateway_realm(request))
    return provider if provider in settings.OIDC_PROVIDERS else None


def behind_gateway(request) -> bool:
    """게이트웨이 SSO 를 통과해 들어온 요청인가."""
    return bool(gateway_realm(request) or request.META.get("HTTP_X_AUTH_REQUEST_EMAIL", "").strip())


def _client(provider: str):
    """공급자별 Authlib client. 메타데이터·공개키 캐시를 위해 프로세스 안에서 재사용한다."""
    cfg = settings.OIDC_PROVIDERS.get(provider)
    if not cfg:
        raise Http404
    key = (provider, cfg["issuer"], cfg["client_id"], cfg["client_secret"])
    client = _clients.get(key)
    if client is None:
        client = OAuth().register(
            name=provider,
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            server_metadata_url=f"{cfg['issuer']}/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid email profile",
                "code_challenge_method": "S256",
                # Keycloak 이 응답하지 않을 때 웹 워커를 오래 붙잡지 않도록 짧게 둔다.
                "default_timeout": 5,
            },
        )
        _clients[key] = client
    return client, cfg


def _safe_next(request, raw: str | None = None) -> str:
    """로그인 후 이동할 주소. 같은 호스트의 상대/절대 URL 만 허용(open redirect 방지), 기본 대시보드."""
    if raw is None:
        raw = request.POST.get("next") or request.GET.get("next") or ""
    if raw and url_has_allowed_host_and_scheme(
        raw, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return raw
    return reverse("dashboard:index")


# ─────────────────────────────────────────────
# 로그인 화면
# ─────────────────────────────────────────────

def _render_login(request, form=None, error: str = "", status: int = 200, next_url: str | None = None):
    """로그인 템플릿 렌더(컨텍스트 계약: form/next/providers/local_login/error/logged_out)."""
    if next_url is None:
        raw = request.POST.get("next") or request.GET.get("next") or ""
        next_url = raw if raw and _safe_next(request, raw) == raw else ""
    return render(request, "accounts/login.html", {
        "form": form if form is not None else AuthenticationForm(request),
        "next": next_url,
        "providers": enabled_providers(),
        "local_login": settings.LOCAL_LOGIN_ACTIVE,
        "error": error,
        "logged_out": "loggedout" in request.GET,
        "gateway_entry_url": reverse("accounts:gateway_login") if settings.GATEWAY_ENTRY_ENABLED else "",
    }, status=status)


def _render_sso_error(request, error: str, status: int = 400) -> HttpResponse:
    # 다시 시도 버튼이 원래 가려던 페이지를 유지하도록 시작 시 저장한 next 를 넘긴다.
    # 리다이렉트하지 않고 직접 렌더한다(게이트웨이 자동시작 무한루프 방지).
    return _render_login(request, error=error, status=status, next_url=request.session.get(SESSION_NEXT, ""))


@never_cache
@require_GET
def gateway_entry_view(request):
    """로그인 화면의 "Keycloak 로그인" 버튼 목적지. 게이트웨이가 이 경로만 보호(internal)한다.

    게이트웨이를 통과하면 GatewaySessionMiddleware 가 X-Auth-Request-Email 로 앱 세션을 이미 만들었다 → 원래 가려던 곳으로.
    세션이 없으면 게이트웨이가 헤더를 안 붙인 것(라우팅 미설정·신뢰 밖 출발지) 또는 계정이 거부된 것이다. 리다이렉트하지 않고
    로그인 화면을 직접 렌더한다(무한 루프 방지, 사유 표시).
    """
    if request.user.is_authenticated:
        return redirect(_safe_next(request))
    denied = getattr(request, "gateway_denied", "")
    if denied:
        return _render_login(request, error=denied, status=403)
    logger.warning("게이트웨이 진입 경로에 인증 헤더가 없음 — 게이트웨이 라우팅(진입 경로 보호)·TRUSTED_PROXY_CIDRS 확인")
    return _render_login(request, error="sso_unavailable", status=503)


@sensitive_post_parameters()
@never_cache
def login_view(request):
    if request.user.is_authenticated:
        return redirect(_safe_next(request))

    if request.method == "POST":
        if not settings.LOCAL_LOGIN_ACTIVE:
            return _render_login(request, error="local_disabled", status=403)
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            return redirect(_safe_next(request))
        # 비활성(승인 대기) 계정은 ModelBackend 가 거부해 일반 오류와 구분되지 않는다.
        # 비밀번호까지 맞을 때만 승인 대기로 안내한다(계정 존재 여부 노출 방지).
        error = "invalid"
        username = request.POST.get("username", "")
        pending = User.objects.filter(username__iexact=username, is_active=False).first() if username else None
        if pending is not None and pending.check_password(request.POST.get("password", "")):
            error = "pending"
        return _render_login(request, form=form, error=error)

    # GET — 게이트웨이가 확인한 사용자인데 로그인이 거부됐으면(비활성·모호) 사유를 보여 준다(자동시작 루프 방지).
    denied = getattr(request, "gateway_denied", "")
    if denied:
        return _render_login(request, error=denied, status=403)
    # 게이트웨이가 확인한 사용자는 로그인 화면 없이 SSO 를 바로 시작한다.
    provider = gateway_provider(request)
    if provider and "loggedout" not in request.GET:
        url = reverse("accounts:sso_login", args=[provider])
        nxt = request.GET.get("next", "")
        return redirect(url + ("?" + urlencode({"next": nxt}) if nxt else ""))
    return _render_login(request)


def _gateway_logout_page(url: str, then: str = "") -> HttpResponse:
    """게이트웨이 로그아웃 주소로 브라우저를 보낸다 — 302 가 아니라 HTML 로.

    게이트웨이(Nginx)는 하위 경로(/qg/) 응답의 Location 을 자기 경로 기준으로 고쳐 쓸 수 있어, 절대 주소 302 가
    https://qw.../qg/_qw/logout 으로 바뀌어 앱에 404 로 돌아온 적이 있다(2026-10-09). Location 헤더가 없으면
    고쳐 쓸 대상이 없다. JS 가 막혀도 meta refresh·링크로 이동한다.

    then 이 있으면(게이트웨이 진입 모드) 게이트웨이 로그아웃을 백그라운드 요청(fetch)으로 호출해 세션 쿠키만 지우고
    then(앱 로그인 화면)으로 돌아온다. 3초 안에 응답이 없어도 이동한다. then 이 없으면 게이트웨이 로그아웃 화면으로 이동한다.
    """
    safe = escape(url)
    js_url = json.dumps(url).replace("<", chr(92) + "u003c")
    if then:
        safe_then = escape(then)
        js_then = json.dumps(then).replace("<", chr(92) + "u003c")
        refresh = f'<meta http-equiv="refresh" content="4;url={safe_then}">'
        link = f'<a href="{safe_then}">여기</a>'
        script = (
            f"<script>var go=function(){{location.replace({js_then});}};setTimeout(go,3000);"
            f"try{{fetch({js_url},{{credentials:'include',redirect:'manual',cache:'no-store'}}).then(go,go);}}catch(e){{go();}}</script>"
        )
    else:
        refresh = f'<meta http-equiv="refresh" content="0;url={safe}">'
        link = f'<a href="{safe}">여기</a>'
        script = f"<script>location.replace({js_url});</script>"
    body = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        f"{refresh}"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>로그아웃</title></head><body>"
        f"<p>로그아웃 중입니다… 자동으로 이동하지 않으면 {link}를 누르세요.</p>"
        f"{script}</body></html>"
    )
    return HttpResponse(body)


@never_cache
def logout_view(request):
    """앱 세션을 끝낸다(GET/POST 모두 — 사이드바가 GET 링크).

    우선순위: (a) 게이트웨이 뒤 + GATEWAY_LOGOUT_URL → 게이트웨이가 자기 세션과 Keycloak 세션까지 끝내도록 넘긴다.
    (b) 로그인 때 보관한 id_token 이 있고 공급자 post_logout_redirect=True → Keycloak end-session 으로 보내
    Keycloak 세션도 끝낸다(앱 세션만 끊으면 버튼이 비밀번호 없이 바로 재로그인된다).
    (c) 그 외 로그인 화면(?loggedout=1 로 게이트웨이 자동시작 방지).
    id_token 은 logout() 이 세션을 비우기 전에 읽는다.
    """
    id_token = request.session.get(SESSION_ID_TOKEN)
    provider = request.session.get(SESSION_PROVIDER)
    logout(request)

    if settings.GATEWAY_LOGOUT_URL and settings.GATEWAY_ENTRY_ENABLED and not id_token:
        # 앱 경로는 게이트웨이 비보호라 헤더가 없다. 게이트웨이 세션을 끊어야 버튼이 비밀번호 없이 재로그인되지 않는다.
        return _gateway_logout_page(
            settings.GATEWAY_LOGOUT_URL, then=reverse("accounts:login") + "?loggedout=1",
        )
    if settings.GATEWAY_LOGOUT_URL and behind_gateway(request):
        return _gateway_logout_page(settings.GATEWAY_LOGOUT_URL)

    cfg = settings.OIDC_PROVIDERS.get(provider) if provider else None
    if id_token and cfg and cfg.get("post_logout_redirect"):
        try:
            client, cfg = _client(provider)
            result = client.create_logout_url(
                # Keycloak 은 등록된 URI 와 정확히 일치해야 하므로 쿼리(?loggedout=1)를 붙이지 않는다.
                post_logout_redirect_uri=_absolute_url(request, reverse("accounts:login")),
                id_token_hint=id_token,
                client_id=cfg["client_id"],
            )
            return redirect(result["url"])
        except Exception as exc:
            logger.warning("SSO 로그아웃 주소 생성 실패 provider=%s error=%s", provider, type(exc).__name__)
    return redirect(reverse("accounts:login") + "?loggedout=1")


# ─────────────────────────────────────────────
# SSO 시작 / 콜백
# ─────────────────────────────────────────────

def _redirect_uri(request, provider: str) -> str:
    return _absolute_url(request, reverse("accounts:sso_callback", args=[provider]))


def _absolute_url(request, path: str) -> str:
    """Keycloak 에 등록한 주소와 맞추기 위한 절대 URL(localhost 외에는 https 강제)."""
    uri = request.build_absolute_uri(path)
    # 운영은 TLS 종단 프록시 뒤라 Django 가 http 로 볼 수 있다 — Keycloak 에 등록한 https 콜백과 맞춘다.
    host = request.get_host()
    if not host.endswith("]"):  # IPv6 리터럴이 아니면 포트 제거
        host = host.rsplit(":", 1)[0]
    if host not in _LOCAL_HOSTS and uri.startswith("http://"):
        uri = "https://" + uri[len("http://"):]
    return uri


@never_cache
@require_GET
def sso_login(request, provider: str) -> HttpResponse:
    """Keycloak 로그인 화면으로 보낸다."""
    if provider not in settings.OIDC_PROVIDERS:
        raise Http404
    if request.user.is_authenticated:
        return redirect(_safe_next(request))

    request.session[SESSION_NEXT] = _safe_next(request)
    try:
        client, _cfg = _client(provider)
        return client.authorize_redirect(request, _redirect_uri(request, provider))
    except Http404:
        raise
    except Exception as exc:
        logger.warning("SSO 시작 실패 provider=%s error=%s", provider, type(exc).__name__)
        return _render_sso_error(request, "sso_unavailable")


@never_cache
@require_GET
def sso_callback(request, provider: str) -> HttpResponse:
    """Keycloak 에서 돌아온 사용자를 확인해 로그인시킨다."""
    if provider not in settings.OIDC_PROVIDERS:
        raise Http404
    try:
        client, cfg = _client(provider)
        token = client.authorize_access_token(request)
    except Http404:
        raise
    except Exception as exc:
        # 상태값 불일치(세션 만료·다른 브라우저), 사용자 취소, 토큰 교환·검증 실패 모두 로그인하지 않는다.
        logger.warning("SSO 콜백 실패 provider=%s error=%s", provider, type(exc).__name__)
        return _render_sso_error(request, "sso_failed")

    claims = (token or {}).get("userinfo")
    subject = str((claims or {}).get("sub") or "")
    if not claims or not subject or claims.get("iss") != cfg["issuer"]:
        logger.warning("SSO 토큰 검증 실패 provider=%s", provider)
        return _render_sso_error(request, "sso_failed")

    user, error = _resolve_with_retry(provider, cfg["issuer"], subject, claims)
    if error:
        return _render_sso_error(request, error, status=403 if error == "sso_inactive" else 400)

    next_url = request.session.pop(SESSION_NEXT, "") or reverse("dashboard:index")
    login(request, user, backend=_LOGIN_BACKEND)
    # login() 이 세션 키를 순환/비운 뒤에 보관한다. 로그아웃의 id_token_hint 로만 쓴다.
    id_token = (token or {}).get("id_token")
    if isinstance(id_token, str) and id_token:
        request.session[SESSION_ID_TOKEN] = id_token
        request.session[SESSION_PROVIDER] = provider
    # 세션에 저장된 next 도 다시 한 번 같은 호스트인지 확인한다.
    return redirect(_safe_next(request, next_url))


# ─────────────────────────────────────────────
# 게이트웨이 헤더 로그인 (Keycloak client 불필요)
# ─────────────────────────────────────────────

def login_by_gateway_header(request, email: str) -> None:
    """게이트웨이가 확인한 사내(world) 이메일로 앱 로그인. 못 하면 request.gateway_denied 에 사유를 남긴다.

    헤더는 ProxyBoundaryMiddleware 가 신뢰 출발지 것만 남긴 뒤이므로 게이트웨이가 인증한 값이다.
    User.email 이 정확히 1명과 일치하면 그 계정(역할 유지), 없으면 즉시 활성 + viewer 로 생성한다.
    비활성 계정·여러 명과 일치(모호)는 로그인시키지 않는다. 협력사(ext) realm 은 QG 대상이 아니라 무시한다.
    """
    if gateway_realm(request) != "world" or "@" not in email:
        return
    user, error = None, "sso_failed"
    for attempt in range(2):
        try:
            with transaction.atomic():
                user, error = _gateway_user(email, request.META.get("HTTP_X_AUTH_REQUEST_PREFERRED_USERNAME", ""))
            break
        except (IntegrityError, OperationalError) as exc:
            logger.warning("게이트웨이 사용자 처리 실패 error=%s attempt=%d", type(exc).__name__, attempt + 1)
            if not attempt:
                time.sleep(0.2)
    if user is None:
        request.gateway_denied = error
        return
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")


def _gateway_user(email: str, preferred_username: str) -> tuple[User | None, str]:
    matches = list(User.objects.filter(email__iexact=email)[:2])
    if len(matches) == 1:
        user = matches[0]
        return (user, "") if user.is_active else (None, "sso_inactive")
    if matches:
        logger.warning("게이트웨이 이메일이 여러 계정과 일치해 로그인시키지 않음")
        return None, "sso_failed"
    user = User(
        username=_unique_username({"preferred_username": preferred_username}, email),
        email=email,
        is_active=True,
        role=User.Role.VIEWER,
    )
    user.set_unusable_password()
    user.save()
    ActivityLog.log(
        user, ActivityLog.Action.CREATE, ActivityLog.TargetType.USER,
        target=user, target_name=user.username, detail="게이트웨이 SSO 첫 로그인 자동 생성 / role=viewer / 즉시 활성",
    )
    return user, ""


# ─────────────────────────────────────────────
# 사용자 결정
# ─────────────────────────────────────────────

def _resolve_with_retry(provider: str, issuer: str, subject: str, claims) -> tuple[User | None, str]:
    """사용자 결정. 동시 첫 로그인 경합·DB 잠금은 1회 재시도. (user, error) — error 는 "" | sso_inactive | sso_failed."""
    for attempt in range(2):
        try:
            with transaction.atomic():
                return _resolve_user(provider, issuer, subject, claims)
        except (IntegrityError, OperationalError) as exc:
            logger.warning("SSO 사용자 처리 실패 provider=%s error=%s attempt=%d", provider, type(exc).__name__, attempt + 1)
            if not attempt:
                time.sleep(0.2)
    return None, "sso_failed"


def _resolve_user(provider: str, issuer: str, subject: str, claims) -> tuple[User | None, str]:
    email = str(claims.get("email") or "").strip()

    # 1) 이미 연결된 사용자
    identity = ExternalIdentity.objects.select_related("user").filter(issuer=issuer, subject=subject).first()
    if identity:
        if email:
            identity.email = email
        identity.last_login_at = timezone.now()
        identity.save(update_fields=["email", "last_login_at"])
        if not identity.user.is_active:
            return None, "sso_inactive"
        return identity.user, ""

    # 신뢰할 수 없는 이메일이면 연결·생성 모두 하지 않는다.
    if not email or not _email_trusted(email, claims):
        logger.warning("SSO 이메일을 신뢰할 수 없어 계정을 만들지 않음 provider=%s", provider)
        return None, "sso_failed"

    # 2) 이메일이 정확히 1명과 일치하면 그 계정에 연결(모호하면 연결하지 않는다)
    matches = list(User.objects.filter(email__iexact=email)[:2])
    if len(matches) == 1:
        user = matches[0]
        # 같은 issuer 의 다른 sub 가 이미 붙은 계정이면(이메일 재사용 의심) 붙이지 않고, 중복 계정도 만들지 않는다.
        # 복구: 운영자가 Django admin 에서 그 계정의 기존 ExternalIdentity 를 삭제하면 다음 로그인에 다시 연결된다.
        if user.external_identities.filter(issuer=issuer).exists():
            logger.warning("기존 계정에 다른 SSO 연결이 있어 연결하지 않음 provider=%s", provider)
            return None, "sso_failed"
        if not user.is_active:
            return None, "sso_inactive"
        _create_identity(user, provider, issuer, subject, email)
        ActivityLog.log(
            user, ActivityLog.Action.CREATE, ActivityLog.TargetType.USER,
            target=user, target_name=user.username, detail="SSO 계정 연결(이메일 일치)",
        )
        return user, ""
    elif matches:
        logger.warning("SSO 이메일이 여러 계정과 일치해 연결하지 않음 provider=%s", provider)

    # 3) 연결할 계정이 없으면 새 계정을 즉시 활성 + viewer 로 생성
    user = User(
        username=_unique_username(claims, email),
        email=email,
        first_name=_display_name(claims)[:150],
        is_active=True,
        role=User.Role.VIEWER,
    )
    user.set_unusable_password()
    user.save()
    _create_identity(user, provider, issuer, subject, email)
    ActivityLog.log(
        user, ActivityLog.Action.CREATE, ActivityLog.TargetType.USER,
        target=user, target_name=user.username, detail="SSO 첫 로그인 자동 생성 / role=viewer / 즉시 활성",
    )
    return user, ""


def _create_identity(user: User, provider: str, issuer: str, subject: str, email: str) -> None:
    ExternalIdentity.objects.create(
        user=user, provider=provider, issuer=issuer, subject=subject,
        email=email, last_login_at=timezone.now(),
    )


def _email_trusted(email: str, claims) -> bool:
    """이메일을 본인 것으로 믿을 수 있는가. 회사 관리 도메인이거나 Keycloak 이 확인했다고 알린 경우."""
    if "@" in email and email.rsplit("@", 1)[1].lower() in settings.OIDC_INTERNAL_EMAIL_DOMAINS:
        return True
    return claims.get("email_verified") is True


def _display_name(claims) -> str:
    return str(claims.get("name") or claims.get("given_name") or "").strip()


def _unique_username(claims, email: str) -> str:
    raw = str(claims.get("preferred_username") or "").strip() or email.split("@")[0]
    base = _USERNAME_UNSAFE.sub("", User.normalize_username(raw))[:120] or "sso"
    taken = {u.lower() for u in User.objects.filter(username__istartswith=base).values_list("username", flat=True)}
    if base.lower() not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if candidate.lower() not in taken:
            return candidate
    raise IntegrityError("사용 가능한 아이디를 만들지 못했습니다")
