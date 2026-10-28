"""Keycloak(OIDC) SSO 로그인 테스트.

실제 Keycloak/네트워크는 호출하지 않는다. `apps.accounts.sso._client` 를 patch 해 가짜 Authlib client 를 주입하고,
secret/토큰/API Key 는 전부 가짜(또는 런타임 생성) 값만 쓴다.

시나리오 매핑: 1 로그인 화면 / 2 자체 로그인 닫힘 / 3 게이트웨이 자동시작 / 4 헤더 신뢰 경계 / 5 세션 불일치 /
6 로그아웃 / 7 콜백 사용자 결정 / 8 콜백 실패 / 9 next / 10 설정 검증 / 11 401 리로드 / 12 Viewer 허용 prefix /
13 기존 회귀.
"""
import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.http import HttpResponse, HttpResponseRedirect
from django.contrib.messages import get_messages
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.middleware import (
    GatewaySessionMiddleware, ProxyBoundaryMiddleware, _viewer_page_allowed,
)
from apps.accounts.models import ExternalIdentity
from apps.activitylog.models import ActivityLog
from apps.api_v2.models import ApiKey

User = get_user_model()

ISSUER = "https://sso.example.test/realms/world"
FAKE_CLIENT_SECRET = "fake-client-secret-for-test"
FAKE_ID_TOKEN = "fake-id-token-for-test"
PROVIDERS = {
    "internal": {
        "issuer": ISSUER,
        "client_id": "qg-portal-test",
        "client_secret": FAKE_CLIENT_SECRET,
        "post_logout_redirect": False,
    },
}
PROVIDERS_END_SESSION = {"internal": {**PROVIDERS["internal"], "post_logout_redirect": True}}
END_SESSION_ENDPOINT = "https://sso.example.test/realms/world/protocol/openid-connect/logout"
PROXY_IP = "10.1.2.3"
TRUSTED = ["10.0.0.0/8"]
GATEWAY_LOGOUT = "https://gateway.example.test/oauth2/sign_out"


def assert_gateway_logout_page(test, resp, url=None):
    """게이트웨이 로그아웃은 302(Location 은 게이트웨이가 고쳐 쓸 수 있음)가 아니라 자동 이동 HTML 페이지여야 한다."""
    url = url or GATEWAY_LOGOUT
    test.assertEqual(resp.status_code, 200)
    test.assertFalse(resp.has_header("Location"))
    body = resp.content.decode()
    test.assertIn(f'<meta http-equiv="refresh" content="0;url={url}">', body)
    test.assertIn(f'<a href="{url}">', body)
    test.assertIn(f'location.replace("{url}")', body)

LOGIN_URL = "/accounts/login/"
SSO_LOGIN_URL = "/accounts/oidc/internal/login/"
SSO_CALLBACK_URL = "/accounts/oidc/internal/callback/"
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
BASE_DIR = CONFIG_DIR.parent

# OIDC 가 켜진 SSO 전용 운영 상태.
SSO_ONLY = dict(
    OIDC_PROVIDERS=PROVIDERS, OIDC_PROVIDER_KEYS=["internal"],
    LOCAL_LOGIN_ACTIVE=False, TRUSTED_PROXY_CIDRS=TRUSTED,
    GATEWAY_LOGOUT_URL=GATEWAY_LOGOUT, OIDC_INTERNAL_EMAIL_DOMAINS=frozenset({"worxphere.ai"}),
)


def _load_base_settings():
    spec = importlib.util.spec_from_file_location("_qgp_sso_test_settings", CONFIG_DIR / "settings" / "base.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOAuthClient:
    """Authlib client 대역. 실제 토큰 교환 없이 미리 정한 결과를 돌려준다."""

    def __init__(self, token=None, error=None):
        self.token = token
        self.error = error
        self.redirect_uris = []
        self.logout_calls = []
        self.logout_error = None

    def authorize_redirect(self, request, redirect_uri):
        self.redirect_uris.append(redirect_uri)
        return HttpResponseRedirect("https://sso.example.test/realms/world/protocol/openid-connect/auth?state=fake")

    def authorize_access_token(self, request):
        if self.error is not None:
            raise self.error
        return self.token

    def create_logout_url(self, post_logout_redirect_uri=None, id_token_hint=None, state=None, **kwargs):
        """Authlib create_logout_url 대역: 메타데이터 조회 없이 end-session URL 을 조립한다."""
        self.logout_calls.append({"post_logout_redirect_uri": post_logout_redirect_uri,
                                  "id_token_hint": id_token_hint, **kwargs})
        if self.logout_error is not None:
            raise self.logout_error
        from urllib.parse import urlencode
        params = {"id_token_hint": id_token_hint, "post_logout_redirect_uri": post_logout_redirect_uri, **kwargs}
        return {"url": END_SESSION_ENDPOINT + "?" + urlencode(params), "state": state}


def claims_for(**overrides):
    claims = {
        "iss": ISSUER, "sub": "kc-sub-1", "email": "hong@worxphere.ai", "email_verified": True,
        "name": "홍길동", "preferred_username": "hong",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


class SSOTestBase(TestCase):
    """SSO 전용 설정 + 가짜 client 주입 헬퍼."""

    settings_overrides = SSO_ONLY

    def setUp(self):
        self.enterContext(override_settings(**self.settings_overrides))
        self.fake = FakeOAuthClient()
        patcher = patch("apps.accounts.sso._client", side_effect=lambda provider: (self.fake, settings.OIDC_PROVIDERS[provider]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_token(self, claims=None, error=None):
        self.fake.token = {"userinfo": claims if claims is not None else claims_for(), "id_token": FAKE_ID_TOKEN}
        self.fake.error = error

    def sso_roundtrip(self, claims=None, next_url=None, error=None, **extra):
        """SSO 시작 → 콜백. 콜백 응답을 돌려준다."""
        self.set_token(claims, error)
        params = {"next": next_url} if next_url is not None else {}
        start = self.client.get(SSO_LOGIN_URL, params, **extra)
        self.assertEqual(start.status_code, 302)
        return self.client.get(SSO_CALLBACK_URL, {"code": "fake-code", "state": "fake"}, **extra)

    def logged_in_user(self):
        user_id = self.client.session.get("_auth_user_id")
        return User.objects.get(pk=user_id) if user_id else None


# ─────────────────────────────────────────────
# 1 로그인 화면
# ─────────────────────────────────────────────

class LoginPageTests(TestCase):
    def test_sso_only_shows_sso_button_and_hides_local_form_and_signup(self):
        with override_settings(**SSO_ONLY):
            resp = self.client.get(LOGIN_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, SSO_LOGIN_URL)
        self.assertContains(resp, "사내 계정으로 로그인 (SSO)")
        self.assertNotContains(resp, 'name="password"')
        self.assertNotContains(resp, "/accounts/signup/")
        self.assertEqual(resp.context["local_login"], False)
        self.assertEqual([p["key"] for p in resp.context["providers"]], ["internal"])

    def test_logged_out_context_follows_loggedout_query(self):
        with override_settings(**SSO_ONLY):
            self.assertEqual(self.client.get(LOGIN_URL).context["logged_out"], False)
            self.assertEqual(self.client.get(LOGIN_URL, {"loggedout": "1"}).context["logged_out"], True)
            # Keycloak 에서 쿼리 없이 돌아온 경우에도 자동시작 없이 평범한 로그인 화면
            self.assertEqual(self.client.get(LOGIN_URL, {"state": "x"}).context["logged_out"], False)

    def test_login_view_masks_password_post_parameters(self):
        """기존 Django LoginView 와 동일하게 비밀번호 POST 가 오류 보고서에 마스킹된다."""
        with override_settings(OIDC_PROVIDERS={}, LOCAL_LOGIN_ACTIVE=True):
            resp = self.client.post(LOGIN_URL, {"username": "nobody", "password": "x"})
        self.assertEqual(resp.wsgi_request.sensitive_post_parameters, "__ALL__")

    def test_sso_button_carries_encoded_next(self):
        with override_settings(**SSO_ONLY):
            resp = self.client.get(LOGIN_URL, {"next": "/suites/runs/?a=1&b=2"})
        self.assertContains(resp, SSO_LOGIN_URL + "?next=/suites/runs/%3Fa%3D1%26b%3D2")

    def test_without_oidc_config_local_login_stays_open(self):
        with override_settings(OIDC_PROVIDERS={}, OIDC_PROVIDER_KEYS=["internal"],
                               LOCAL_LOGIN_ACTIVE=True):
            resp = self.client.get(LOGIN_URL)
        self.assertContains(resp, 'name="password"')
        self.assertContains(resp, "/accounts/signup/")
        self.assertNotContains(resp, SSO_LOGIN_URL)
        self.assertEqual(resp.context["providers"], [])
        self.assertEqual(resp.context["local_login"], True)

    def test_local_login_enabled_with_oidc_shows_both(self):
        with override_settings(**{**SSO_ONLY, "LOCAL_LOGIN_ACTIVE": True}):
            resp = self.client.get(LOGIN_URL)
        self.assertContains(resp, SSO_LOGIN_URL)
        self.assertContains(resp, 'name="password"')
        self.assertContains(resp, "/accounts/signup/")

    def test_no_login_method_shows_notice_only(self):
        with override_settings(OIDC_PROVIDERS={}, LOCAL_LOGIN_ACTIVE=False):
            resp = self.client.get(LOGIN_URL)
        self.assertNotContains(resp, 'name="password"')
        self.assertNotContains(resp, "/accounts/signup/")
        self.assertContains(resp, "관리자에게 문의")

    def test_error_messages_are_korean_and_hide_internal_terms(self):
        cases = {
            "sso_failed": "로그인을 완료하지 못했습니다. 다시 시도해 주세요.",
            "sso_unavailable": "지금은 로그인 화면을 열 수 없습니다. 잠시 후 다시 시도해 주세요.",
            "sso_inactive": "비활성화되었거나 승인 대기 중인 계정입니다. 관리자에게 문의하세요.",
            "local_disabled": "아이디/비밀번호 로그인은 사용할 수 없습니다. 사내 계정(SSO)으로 로그인해 주세요.",
        }
        from django.template.loader import render_to_string
        from django.contrib.auth.forms import AuthenticationForm
        for error, text in cases.items():
            with self.subTest(error=error), override_settings(**SSO_ONLY):
                request = RequestFactory().get(LOGIN_URL)
                html = render_to_string("accounts/login.html", {
                    "form": AuthenticationForm(request), "next": "", "providers": [], "local_login": False,
                    "error": error,
                }, request=request)
                self.assertIn(text, html)
                self.assertNotIn("OIDC", html)
                self.assertNotIn("issuer", html.lower())


# ─────────────────────────────────────────────
# 2 자체 로그인 닫힘 / 폴백
# ─────────────────────────────────────────────

class LocalLoginClosedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("localu", password="pw-local-1", role="viewer", email="l@worxphere.ai")

    def test_password_post_is_forbidden_when_closed(self):
        with override_settings(**SSO_ONLY):
            resp = self.client.post(LOGIN_URL, {"username": "localu", "password": "pw-local-1"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.context["error"], "local_disabled")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signup_is_404_when_closed_and_open_otherwise(self):
        with override_settings(**SSO_ONLY):
            self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)
            self.assertEqual(self.client.post("/accounts/signup/", {"username": "x"}).status_code, 404)
        with override_settings(OIDC_PROVIDERS={}, LOCAL_LOGIN_ACTIVE=True):
            self.assertEqual(self.client.get("/accounts/signup/").status_code, 200)

    def test_admin_login_redirects_to_app_login_with_next_when_closed(self):
        with override_settings(**SSO_ONLY):
            resp = self.client.get("/admin/login/", {"next": "/admin/"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(LOGIN_URL))
        self.assertIn("next=%2Fadmin%2F", resp["Location"])

    def test_admin_for_logged_in_non_staff_does_not_loop_when_closed(self):
        """로그인했지만 staff 가 아닌 사용자가 /admin/ 에 와도 로그인 화면과 무한 왕복하지 않는다(403)."""
        dev = User.objects.create_user("devu", password="pw-dev-1", role="tribe_dev", email="d@worxphere.ai")
        self.client.force_login(dev)
        with override_settings(**SSO_ONLY):
            resp = self.client.get("/admin/", follow=True)
        self.assertEqual(resp.status_code, 403)
        self.assertLessEqual(len(resp.redirect_chain), 2)

    def test_admin_login_is_the_normal_admin_form_when_open(self):
        with override_settings(OIDC_PROVIDERS={}, LOCAL_LOGIN_ACTIVE=True):
            resp = self.client.get("/admin/login/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "csrfmiddlewaretoken")

    def test_fallback_without_oidc_keeps_password_login(self):
        """OIDC 값이 없으면 로그인이 막히지 않는다(안전한 배포 순서)."""
        with override_settings(OIDC_PROVIDERS={}, LOCAL_LOGIN_ACTIVE=True):
            resp = self.client.post(LOGIN_URL, {"username": "localu", "password": "pw-local-1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))


# ─────────────────────────────────────────────
# 3 게이트웨이 자동시작
# ─────────────────────────────────────────────

class GatewayAutoStartTests(TestCase):
    def setUp(self):
        self.enterContext(override_settings(**SSO_ONLY))

    def get(self, path=LOGIN_URL, realm="world", addr=PROXY_IP, **kw):
        headers = {"X-Auth-Realm": realm} if realm else {}
        return self.client.get(path, REMOTE_ADDR=addr, headers=headers, **kw)

    def test_world_realm_from_trusted_proxy_starts_sso(self):
        resp = self.get()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], SSO_LOGIN_URL)

    def test_realm_header_is_case_insensitive(self):
        self.assertEqual(self.get(realm="World")["Location"], SSO_LOGIN_URL)

    def test_next_is_preserved_on_auto_start(self):
        resp = self.get(data={"next": "/suites/runs/?x=1"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(SSO_LOGIN_URL + "?next="))
        self.assertIn("%2Fsuites%2Fruns%2F", resp["Location"])

    def test_no_header_renders_login_page(self):
        resp = self.get(realm=None)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "사내 계정으로 로그인 (SSO)")

    def test_unknown_or_ext_realm_does_not_auto_start(self):
        for realm in ("ext", "unknown"):
            with self.subTest(realm=realm):
                self.assertEqual(self.get(realm=realm).status_code, 200)

    def test_post_never_auto_starts(self):
        resp = self.client.post(LOGIN_URL, {"username": "a", "password": "b"},
                                REMOTE_ADDR=PROXY_IP, headers={"X-Auth-Realm": "world"})
        self.assertEqual(resp.status_code, 403)  # SSO 전용이라 자체 로그인 POST 는 차단, 리다이렉트 아님

    def test_loggedout_flag_blocks_auto_start(self):
        resp = self.get(data={"loggedout": "1"})
        self.assertEqual(resp.status_code, 200)

    def test_no_auto_start_when_provider_not_configured(self):
        with override_settings(OIDC_PROVIDERS={}, LOCAL_LOGIN_ACTIVE=True):
            self.assertEqual(self.get().status_code, 200)

    def test_anonymous_page_request_chains_into_sso(self):
        """비로그인 보호 페이지 → 로그인 화면(next) → 자동시작으로 SSO 로 이어진다."""
        first = self.get(path="/")
        self.assertEqual(first.status_code, 302)
        second = self.client.get(first["Location"], REMOTE_ADDR=PROXY_IP, headers={"X-Auth-Realm": "world"})
        self.assertEqual(second.status_code, 302)
        self.assertTrue(second["Location"].startswith(SSO_LOGIN_URL))


# ─────────────────────────────────────────────
# 4 헤더 신뢰 경계
# ─────────────────────────────────────────────

class ProxyBoundaryTests(TestCase):
    def _run(self, addr, cidrs):
        seen = {}

        def view(request):
            seen.update({k: v for k, v in request.META.items() if k.startswith("HTTP_X_AUTH_")})
            return HttpResponse("ok")

        request = RequestFactory().get(
            "/", REMOTE_ADDR=addr, HTTP_X_AUTH_REALM="world", HTTP_X_AUTH_REQUEST_EMAIL="a@worxphere.ai",
            HTTP_X_AUTH_REQUEST_USER="a", HTTP_X_OTHER="keep",
        )
        with override_settings(TRUSTED_PROXY_CIDRS=cidrs):
            ProxyBoundaryMiddleware(view)(request)
        return seen, request

    def test_trusted_source_keeps_headers(self):
        seen, _ = self._run(PROXY_IP, TRUSTED)
        self.assertEqual(seen["HTTP_X_AUTH_REALM"], "world")
        self.assertEqual(seen["HTTP_X_AUTH_REQUEST_EMAIL"], "a@worxphere.ai")

    def test_untrusted_source_headers_are_removed(self):
        seen, request = self._run("203.0.113.9", TRUSTED)
        self.assertEqual(seen, {})
        self.assertEqual(request.META["HTTP_X_OTHER"], "keep")  # X-Auth-* 외 헤더는 그대로

    def test_empty_cidrs_always_remove(self):
        for cidrs in ([], [""]):
            with self.subTest(cidrs=cidrs):
                seen, _ = self._run(PROXY_IP, cidrs)
                self.assertEqual(seen, {})

    def test_ipv4_mapped_ipv6_source_is_normalized_and_trusted(self):
        seen, _ = self._run("::ffff:10.1.2.3", TRUSTED)
        self.assertEqual(seen["HTTP_X_AUTH_REALM"], "world")
        seen, _ = self._run("::ffff:203.0.113.9", TRUSTED)
        self.assertEqual(seen, {})

    def test_unparseable_source_removes_headers(self):
        seen, _ = self._run("not-an-ip", TRUSTED)
        self.assertEqual(seen, {})

    def test_settings_are_read_per_request_not_cached(self):
        mw = ProxyBoundaryMiddleware(lambda r: HttpResponse("ok"))
        for cidrs, expect_kept in ((TRUSTED, True), ([], False), (TRUSTED, True)):
            request = RequestFactory().get("/", REMOTE_ADDR=PROXY_IP, HTTP_X_AUTH_REALM="world")
            with override_settings(TRUSTED_PROXY_CIDRS=cidrs):
                mw(request)
            self.assertEqual("HTTP_X_AUTH_REALM" in request.META, expect_kept)


class ForgedHeaderTests(SSOTestBase):
    def setUp(self):
        super().setUp()
        self.victim = User.objects.create_user("victim", password="pw-victim-1", email="victim@worxphere.ai",
                                               role="qpe_admin")

    def test_untrusted_realm_header_does_not_auto_start(self):
        resp = self.client.get(LOGIN_URL, REMOTE_ADDR="203.0.113.9", headers={"X-Auth-Realm": "world"})
        self.assertEqual(resp.status_code, 200)

    def test_untrusted_empty_cidrs_does_not_auto_start(self):
        with override_settings(TRUSTED_PROXY_CIDRS=[]):
            resp = self.client.get(LOGIN_URL, REMOTE_ADDR=PROXY_IP, headers={"X-Auth-Realm": "world"})
        self.assertEqual(resp.status_code, 200)

    def test_forged_email_header_from_untrusted_peer_never_logs_anyone_in(self):
        """게이트웨이가 아닌 출발지가 위조한 X-Auth-* 는 제거되어 누구도 로그인되지 않는다."""
        for addr in ("203.0.113.9", "192.0.2.50"):
            with self.subTest(addr=addr):
                resp = self.client.get("/", REMOTE_ADDR=addr, headers={
                    "X-Auth-Request-Email": self.victim.email, "X-Auth-Realm": "world",
                    "X-Auth-Request-User": self.victim.username,
                })
                self.assertEqual(resp.status_code, 302)
                self.assertIn("/accounts/login/", resp["Location"])
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_trusted_gateway_header_logs_in_that_user(self):
        """신뢰 출발지(게이트웨이)가 붙인 헤더는 게이트웨이가 인증한 값이므로 그 사용자로 로그인한다."""
        resp = self.client.get("/", REMOTE_ADDR=PROXY_IP, headers={
            "X-Auth-Request-Email": self.victim.email, "X-Auth-Realm": "world",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.victim.pk))

    def test_untrusted_forged_email_cannot_evict_a_real_session(self):
        """신뢰 밖 출발지의 X-Auth-Request-Email 은 제거되므로 세션 불일치 처리도 일어나지 않는다."""
        self.client.force_login(self.victim)
        resp = self.client.get("/", REMOTE_ADDR="203.0.113.9", headers={"X-Auth-Request-Email": "other@worxphere.ai"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.victim.pk))


# ─────────────────────────────────────────────
# 5 세션 불일치
# ─────────────────────────────────────────────

class GatewaySessionMismatchTests(TestCase):
    def setUp(self):
        self.enterContext(override_settings(**SSO_ONLY))
        self.user = User.objects.create_user("alice", password="pw-alice-1", email="Alice@Worxphere.ai", role="viewer")
        self.client.force_login(self.user)

    def get(self, email=None, addr=PROXY_IP):
        headers = {"X-Auth-Request-Email": email} if email is not None else {}
        return self.client.get("/", REMOTE_ADDR=addr, headers=headers)

    def test_different_email_discards_session(self):
        resp = self.get("bob@worxphere.ai")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_same_email_keeps_session_case_insensitive(self):
        resp = self.get("ALICE@worxphere.ai")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))

    def test_external_identity_email_keeps_session(self):
        ExternalIdentity.objects.create(user=self.user, provider="internal", issuer=ISSUER, subject="s-a",
                                        email="alice.alt@worxphere.ai")
        resp = self.get("Alice.Alt@worxphere.ai")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("_auth_user_id", self.client.session)

    def test_no_header_keeps_session(self):
        resp = self.get(None)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("_auth_user_id", self.client.session)

    def test_middleware_unit_logs_out_only_on_mismatch(self):
        factory = RequestFactory()
        mw = GatewaySessionMiddleware(lambda r: HttpResponse("ok"))
        for header, expect_logout in (("alice@worxphere.ai", False), ("zed@worxphere.ai", True), ("", False)):
            with self.subTest(header=header):
                request = factory.get("/", HTTP_X_AUTH_REQUEST_EMAIL=header)
                request.user = self.user
                request.session = self.client.session
                with patch("apps.accounts.middleware.logout") as fake_logout:
                    mw(request)
                self.assertEqual(fake_logout.called, expect_logout)


# ─────────────────────────────────────────────
# 6 로그아웃
# ─────────────────────────────────────────────

class LogoutTests(TestCase):
    def setUp(self):
        self.enterContext(override_settings(**SSO_ONLY))
        self.user = User.objects.create_user("bye", password="pw-bye-1", role="viewer", email="bye@worxphere.ai")
        self.client.force_login(self.user)

    def _logout(self, method, headers=None, addr=PROXY_IP):
        self.client.force_login(self.user)
        return getattr(self.client, method)("/accounts/logout/", REMOTE_ADDR=addr, headers=headers or {})

    def test_behind_gateway_redirects_to_gateway_logout_get_and_post(self):
        for method in ("get", "post"):
            with self.subTest(method=method):
                resp = self._logout(method, {"X-Auth-Realm": "world"})
                assert_gateway_logout_page(self, resp)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_gateway_email_header_alone_counts_as_behind_gateway(self):
        resp = self._logout("get", {"X-Auth-Request-Email": self.user.email})
        assert_gateway_logout_page(self, resp)

    def test_outside_gateway_goes_to_login_with_loggedout_flag(self):
        for method in ("get", "post"):
            with self.subTest(method=method):
                resp = self._logout(method)
                self.assertRedirects(resp, LOGIN_URL + "?loggedout=1", fetch_redirect_response=False)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_untrusted_gateway_headers_do_not_trigger_gateway_logout(self):
        resp = self._logout("get", {"X-Auth-Realm": "world"}, addr="203.0.113.9")
        self.assertRedirects(resp, LOGIN_URL + "?loggedout=1", fetch_redirect_response=False)

    def test_behind_gateway_without_logout_url_uses_login_page(self):
        with override_settings(GATEWAY_LOGOUT_URL=""):
            resp = self._logout("get", {"X-Auth-Realm": "world"})
        self.assertRedirects(resp, LOGIN_URL + "?loggedout=1", fetch_redirect_response=False)

    def test_after_logout_login_page_does_not_auto_start(self):
        self._logout("get", {"X-Auth-Realm": "world"}).close()
        with override_settings(GATEWAY_LOGOUT_URL=""):
            resp = self.client.get(LOGIN_URL + "?loggedout=1", REMOTE_ADDR=PROXY_IP,
                                   headers={"X-Auth-Realm": "world"})
        self.assertEqual(resp.status_code, 200)


class SSOLogoutTests(SSOTestBase):
    """로그아웃 우선순위 (a) 게이트웨이 (b) Keycloak end-session (c) 앱 로그인 화면 + id_token 보관."""

    settings_overrides = {**SSO_ONLY, "OIDC_PROVIDERS": PROVIDERS_END_SESSION}

    def login_via_sso(self, **extra):
        resp = self.sso_roundtrip(**extra)
        self.assertEqual(resp.status_code, 302)
        self.assertIsNotNone(self.logged_in_user())
        return resp

    def test_id_token_is_kept_in_session_after_login_and_not_exposed(self):
        resp = self.login_via_sso()
        self.assertEqual(self.client.session["sso_id_token"], FAKE_ID_TOKEN)
        self.assertEqual(self.client.session["sso_provider"], "internal")
        self.assertNotIn(FAKE_ID_TOKEN, resp["Location"])
        page = self.client.get("/")
        self.assertNotContains(page, FAKE_ID_TOKEN)

    def test_missing_id_token_is_not_stored(self):
        self.set_token()
        self.fake.token.pop("id_token")
        self.client.get(SSO_LOGIN_URL)
        self.client.get(SSO_CALLBACK_URL, {"code": "c", "state": "s"})
        self.assertIsNotNone(self.logged_in_user())
        self.assertNotIn("sso_id_token", self.client.session)

    def test_b_end_session_logout_when_flag_on_and_id_token_present(self):
        self.login_via_sso()
        for method in ("get", "post"):
            with self.subTest(method=method):
                self.login_via_sso()
                resp = getattr(self.client, method)("/accounts/logout/")
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(resp["Location"].startswith(END_SESSION_ENDPOINT + "?"))
                call = self.fake.logout_calls[-1]
                # 등록된 URI 와 정확히 일치해야 하므로 쿼리 없는 로그인 URL, 테스트 호스트는 localhost 가 아니라 https 강제
                self.assertEqual(call["post_logout_redirect_uri"], "https://testserver" + LOGIN_URL)
                self.assertNotIn("?", call["post_logout_redirect_uri"])
                self.assertEqual(call["id_token_hint"], FAKE_ID_TOKEN)
                self.assertEqual(call["client_id"], "qg-portal-test")
                self.assertIsNone(self.logged_in_user())
                self.assertNotIn("sso_id_token", self.client.session)

    def test_b_post_logout_uri_keeps_http_for_localhost(self):
        self.login_via_sso()
        self.client.get("/accounts/logout/", HTTP_HOST="localhost:8000")
        self.assertEqual(self.fake.logout_calls[-1]["post_logout_redirect_uri"], "http://localhost:8000" + LOGIN_URL)

    def test_id_token_not_leaked_after_logout(self):
        """로그아웃 후 세션·로그인 화면 어디에도 id_token 이 남지 않는다(end-session URL 의 id_token_hint 제외)."""
        self.login_via_sso()
        resp = self.client.get("/accounts/logout/")
        self.client.get(LOGIN_URL)
        self.assertNotIn(FAKE_ID_TOKEN, str(dict(self.client.session)))
        page = self.client.get(LOGIN_URL)
        self.assertNotContains(page, FAKE_ID_TOKEN)
        self.assertEqual(resp.status_code, 302)

    def test_a_gateway_takes_priority_over_end_session(self):
        self.login_via_sso()
        resp = self.client.get("/accounts/logout/", REMOTE_ADDR=PROXY_IP, headers={"X-Auth-Realm": "world"})
        assert_gateway_logout_page(self, resp)
        self.assertEqual(self.fake.logout_calls, [])
        self.assertIsNone(self.logged_in_user())

    def test_gateway_headers_without_logout_url_fall_to_end_session(self):
        self.login_via_sso()
        with override_settings(GATEWAY_LOGOUT_URL=""):
            resp = self.client.get("/accounts/logout/", REMOTE_ADDR=PROXY_IP, headers={"X-Auth-Realm": "world"})
        self.assertTrue(resp["Location"].startswith(END_SESSION_ENDPOINT))

    def test_c_metadata_failure_falls_back_to_login_page_and_logs_warning(self):
        self.login_via_sso()
        self.fake.logout_error = RuntimeError(FAKE_ID_TOKEN)  # 예외 메시지에 값이 있어도 로그에는 타입만
        with self.assertLogs("apps.accounts.sso", level="WARNING") as logs:
            resp = self.client.get("/accounts/logout/")
        self.assertRedirects(resp, LOGIN_URL + "?loggedout=1", fetch_redirect_response=False)
        self.assertIsNone(self.logged_in_user())
        joined = "\n".join(logs.output)
        self.assertIn("provider=internal", joined)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn(FAKE_ID_TOKEN, joined)

    def test_c_when_post_logout_redirect_flag_is_off(self):
        with override_settings(OIDC_PROVIDERS=PROVIDERS):
            self.login_via_sso()
            resp = self.client.get("/accounts/logout/")
        self.assertRedirects(resp, LOGIN_URL + "?loggedout=1", fetch_redirect_response=False)
        self.assertEqual(self.fake.logout_calls, [])

    def test_c_when_logged_in_by_password_without_id_token(self):
        user = User.objects.create_user("pwu", password="pw-pwu-1", role="viewer", email="pwu@worxphere.ai")
        self.client.force_login(user)
        resp = self.client.get("/accounts/logout/")
        self.assertRedirects(resp, LOGIN_URL + "?loggedout=1", fetch_redirect_response=False)
        self.assertEqual(self.fake.logout_calls, [])


# ─────────────────────────────────────────────
# 7 SSO 콜백 — 사용자 결정
# ─────────────────────────────────────────────

class SSOCallbackUserTests(SSOTestBase):
    def test_sso_login_redirects_to_provider_with_https_callback(self):
        resp = self.client.get(SSO_LOGIN_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.fake.redirect_uris, ["https://testserver" + SSO_CALLBACK_URL])

    def test_sso_login_get_only_and_unknown_provider_404(self):
        self.assertEqual(self.client.post(SSO_LOGIN_URL).status_code, 405)
        self.assertEqual(self.client.get("/accounts/oidc/unknown/login/").status_code, 404)
        self.assertEqual(self.client.get("/accounts/oidc/unknown/callback/").status_code, 404)
        with override_settings(OIDC_PROVIDERS={}):
            self.assertEqual(self.client.get(SSO_LOGIN_URL).status_code, 404)

    def test_sso_login_start_failure_renders_unavailable(self):
        with patch("apps.accounts.sso._client", side_effect=RuntimeError("boom")):
            resp = self.client.get(SSO_LOGIN_URL)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.context["error"], "sso_unavailable")

    def test_existing_account_matched_by_email_is_linked_keeping_role_and_password(self):
        existing = User.objects.create_user("legacy", password="pw-legacy-1", email="Hong@Worxphere.AI",
                                            role="qpe_admin")
        resp = self.sso_roundtrip(claims_for(email_verified=None))  # 사내 도메인은 email_verified 불필요
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.logged_in_user(), existing)
        existing.refresh_from_db()
        self.assertEqual(existing.role, "qpe_admin")
        self.assertTrue(existing.check_password("pw-legacy-1"))
        self.assertEqual(User.objects.count(), 1)
        identity = ExternalIdentity.objects.get(user=existing)
        self.assertEqual((identity.provider, identity.issuer, identity.subject), ("internal", ISSUER, "kc-sub-1"))
        self.assertIsNotNone(identity.last_login_at)
        log = ActivityLog.objects.get(user=existing, action=ActivityLog.Action.CREATE)
        self.assertIn("SSO 계정 연결(이메일 일치)", log.detail)

    def test_login_event_is_logged_once_by_signal(self):
        User.objects.create_user("legacy", password="pw-legacy-1", email="hong@worxphere.ai")
        self.sso_roundtrip()
        self.assertEqual(ActivityLog.objects.filter(action=ActivityLog.Action.LOGIN).count(), 1)

    def test_relogin_uses_issuer_and_subject_even_if_email_changed(self):
        self.sso_roundtrip()
        user = self.logged_in_user()
        self.client.logout()
        resp = self.sso_roundtrip(claims_for(email="renamed@worxphere.ai"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.logged_in_user(), user)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ExternalIdentity.objects.count(), 1)
        self.assertEqual(ExternalIdentity.objects.get().email, "renamed@worxphere.ai")

    def test_no_matching_account_creates_active_viewer_without_password(self):
        resp = self.sso_roundtrip()
        self.assertEqual(resp.status_code, 302)
        user = self.logged_in_user()
        self.assertEqual((user.username, user.email, user.first_name), ("hong", "hong@worxphere.ai", "홍길동"))
        self.assertTrue(user.is_active)
        self.assertEqual(user.role, User.Role.VIEWER)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertEqual(ExternalIdentity.objects.get(user=user).subject, "kc-sub-1")
        log = ActivityLog.objects.get(user=user, action=ActivityLog.Action.CREATE)
        self.assertIn("SSO 첫 로그인 자동 생성", log.detail)
        self.assertIn("role=viewer", log.detail)

    def test_username_collision_gets_numeric_suffix(self):
        User.objects.create_user("hong", password="pw-other-1", email="other@worxphere.ai")
        User.objects.create_user("hong-2", password="pw-other-2", email="other2@worxphere.ai")
        self.sso_roundtrip()
        self.assertEqual(self.logged_in_user().username, "hong-3")

    def test_username_falls_back_to_email_local_part_and_is_sanitized(self):
        self.sso_roundtrip(claims_for(preferred_username=None, email="kim chulsu@worxphere.ai"))
        self.assertEqual(self.logged_in_user().username, "kimchulsu")

    def test_ambiguous_email_does_not_link_and_creates_new_user(self):
        a = User.objects.create_user("dup1", password="pw-dup-1", email="hong@worxphere.ai", role="qpe_admin")
        b = User.objects.create_user("dup2", password="pw-dup-2", email="HONG@worxphere.ai", role="viewer")
        resp = self.sso_roundtrip()
        self.assertEqual(resp.status_code, 302)
        user = self.logged_in_user()
        self.assertNotIn(user.pk, (a.pk, b.pk))
        self.assertEqual(user.role, User.Role.VIEWER)
        self.assertFalse(a.external_identities.exists() or b.external_identities.exists())
        self.assertEqual(User.objects.count(), 3)

    def test_email_match_with_other_sub_already_linked_is_rejected_without_duplicate(self):
        """이메일이 정확히 1명과 일치하지만 그 계정에 같은 issuer 의 다른 sub 연결이 있으면 로그인 거부(중복 계정 금지)."""
        owner = User.objects.create_user("owner", password="pw-owner-1", email="hong@worxphere.ai", role="qpe_admin")
        ExternalIdentity.objects.create(user=owner, provider="internal", issuer=ISSUER, subject="old-sub",
                                        email="hong@worxphere.ai")
        with self.assertLogs("apps.accounts.sso", level="WARNING") as logs:
            resp = self.sso_roundtrip(claims_for(sub="new-sub"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.context["error"], "sso_failed")
        self.assertIsNone(self.logged_in_user())
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(owner.external_identities.count(), 1)
        self.assertFalse(ExternalIdentity.objects.filter(subject="new-sub").exists())
        joined = "\n".join(logs.output)
        self.assertIn("기존 계정에 다른 SSO 연결이 있어 연결하지 않음", joined)
        self.assertNotIn("hong@worxphere.ai", joined)
        self.assertNotIn("new-sub", joined)

    def test_inactive_link_target_is_rejected_and_not_activated(self):
        pending = User.objects.create_user("pending", password="pw-pend-1", email="hong@worxphere.ai",
                                           is_active=False)
        resp = self.sso_roundtrip()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.context["error"], "sso_inactive")
        pending.refresh_from_db()
        self.assertFalse(pending.is_active)
        self.assertFalse(pending.external_identities.exists())
        self.assertIsNone(self.logged_in_user())
        self.assertEqual(User.objects.count(), 1)

    def test_already_linked_but_inactive_user_is_rejected(self):
        self.sso_roundtrip()
        user = self.logged_in_user()
        self.client.logout()
        User.objects.filter(pk=user.pk).update(is_active=False)
        resp = self.sso_roundtrip()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.context["error"], "sso_inactive")
        self.assertIsNone(self.logged_in_user())
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_outside_domain_without_email_verified_creates_nothing(self):
        existing = User.objects.create_user("gm", password="pw-gm-1", email="someone@gmail.com", role="qpe_admin")
        for verified in (False, None):
            with self.subTest(email_verified=verified):
                claims = claims_for(email="someone@gmail.com", email_verified=verified, sub="outside-sub")
                resp = self.sso_roundtrip(claims)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.context["error"], "sso_failed")
                self.assertIsNone(self.logged_in_user())
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(existing.external_identities.exists())
        self.assertEqual(ExternalIdentity.objects.count(), 0)

    def test_outside_domain_with_email_verified_is_allowed(self):
        resp = self.sso_roundtrip(claims_for(email="someone@gmail.com", email_verified=True, sub="outside-sub",
                                             preferred_username="someone"))
        self.assertEqual(resp.status_code, 302)
        user = self.logged_in_user()
        self.assertEqual((user.email, user.role, user.is_active), ("someone@gmail.com", "viewer", True))

    def test_missing_email_cannot_create_account(self):
        resp = self.sso_roundtrip(claims_for(email=None, email_verified=None))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(User.objects.count(), 0)

    def test_concurrent_first_login_race_retries_once(self):
        original_save = User.save
        calls = {"n": 0}

        def flaky_save(user, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("simulated concurrent insert")
            return original_save(user, *args, **kwargs)

        with patch.object(User, "save", flaky_save), patch("apps.accounts.sso.time.sleep"):
            resp = self.sso_roundtrip()
        self.assertEqual(resp.status_code, 302)
        self.assertGreaterEqual(calls["n"], 2)  # 첫 시도 실패 + 재시도 성공(이후 로그인 시각 갱신 save 포함)
        self.assertEqual(User.objects.count(), 1)

    def test_persistent_race_failure_ends_as_sso_failed(self):
        with patch.object(User, "save", side_effect=IntegrityError("always")), \
                patch("apps.accounts.sso.time.sleep"):
            resp = self.sso_roundtrip()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.context["error"], "sso_failed")
        self.assertIsNone(self.logged_in_user())


# ─────────────────────────────────────────────
# 8 콜백 실패
# ─────────────────────────────────────────────

class SSOCallbackFailureTests(SSOTestBase):
    def assert_failed_page(self, resp):
        self.assertEqual(resp.status_code, 400)  # 리다이렉트가 아니어야 자동시작 무한루프가 없다
        self.assertFalse(resp.has_header("Location"))
        self.assertEqual(resp.context["error"], "sso_failed")
        self.assertContains(resp, "로그인을 완료하지 못했습니다", status_code=400)
        self.assertIsNone(self.logged_in_user())
        self.assertEqual(User.objects.count(), 0)

    def test_token_exchange_exception(self):
        self.assert_failed_page(self.sso_roundtrip(error=RuntimeError("token exchange failed")))

    def test_issuer_mismatch(self):
        self.assert_failed_page(self.sso_roundtrip(claims_for(iss="https://evil.example.test/realms/world")))

    def test_missing_subject(self):
        self.assert_failed_page(self.sso_roundtrip(claims_for(sub=None)))

    def test_missing_userinfo(self):
        self.set_token()
        self.fake.token = {"id_token": FAKE_ID_TOKEN}
        self.client.get(SSO_LOGIN_URL)
        self.assert_failed_page(self.client.get(SSO_CALLBACK_URL, {"code": "c", "state": "s"}))

    def test_callback_without_prior_start_fails_cleanly(self):
        self.set_token(error=RuntimeError("state mismatch"))
        self.assert_failed_page(self.client.get(SSO_CALLBACK_URL, {"code": "c", "state": "s"}))

    def test_failure_behind_gateway_does_not_loop_back_into_sso(self):
        resp = self.sso_roundtrip(error=RuntimeError("fail"), REMOTE_ADDR=PROXY_IP,
                                  headers={"X-Auth-Realm": "world"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.has_header("Location"))

    def test_callback_is_get_only(self):
        self.assertEqual(self.client.post(SSO_CALLBACK_URL).status_code, 405)

    def test_no_secret_or_token_leaks_into_response_or_activity_log(self):
        self.sso_roundtrip()  # 성공 경로
        failure = self.sso_roundtrip(claims_for(sub=None))
        for leaked in (FAKE_CLIENT_SECRET, FAKE_ID_TOKEN):
            self.assertNotContains(failure, leaked, status_code=400)
            for log in ActivityLog.objects.all():
                self.assertNotIn(leaked, f"{log.detail} {log.target_name} {log.target_id}")


# ─────────────────────────────────────────────
# 9 next
# ─────────────────────────────────────────────

class SSONextTests(SSOTestBase):
    def test_internal_next_survives_sso_roundtrip(self):
        resp = self.sso_roundtrip(next_url="/suites/runs/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/suites/runs/")

    def test_external_next_is_ignored(self):
        for bad in ("https://evil.example/", "//evil.example/x", "javascript:alert(1)"):
            with self.subTest(next=bad):
                self.client.logout()
                resp = self.sso_roundtrip(next_url=bad)
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp["Location"], reverse("dashboard:index"))

    def test_missing_next_goes_to_dashboard(self):
        self.assertEqual(self.sso_roundtrip()["Location"], reverse("dashboard:index"))

    def test_already_authenticated_sso_login_goes_to_next(self):
        user = User.objects.create_user("already", password="pw-already-1", email="a@worxphere.ai")
        self.client.force_login(user)
        resp = self.client.get(SSO_LOGIN_URL, {"next": "/suites/runs/"})
        self.assertRedirects(resp, "/suites/runs/", fetch_redirect_response=False)
        resp = self.client.get(SSO_LOGIN_URL, {"next": "https://evil.example/"})
        self.assertRedirects(resp, reverse("dashboard:index"), fetch_redirect_response=False)

    def test_login_page_drops_external_next_from_sso_button(self):
        resp = self.client.get(LOGIN_URL, {"next": "https://evil.example/"})
        self.assertNotContains(resp, "evil.example")

    def test_http_host_callback_is_forced_to_https_except_localhost(self):
        self.client.get(SSO_LOGIN_URL)
        self.client.get(SSO_LOGIN_URL, HTTP_HOST="localhost:8000")
        self.client.get(SSO_LOGIN_URL, HTTP_HOST="127.0.0.1:8000")
        with override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"]):
            self.client.get(SSO_LOGIN_URL, HTTP_HOST="localhost:8000")
            self.client.get(SSO_LOGIN_URL, HTTP_HOST="127.0.0.1:8000")
        self.assertEqual(self.fake.redirect_uris[0], "https://testserver" + SSO_CALLBACK_URL)
        self.assertEqual(self.fake.redirect_uris[-2], "http://localhost:8000" + SSO_CALLBACK_URL)
        self.assertEqual(self.fake.redirect_uris[-1], "http://127.0.0.1:8000" + SSO_CALLBACK_URL)


# ─────────────────────────────────────────────
# 10 설정 검증
# ─────────────────────────────────────────────

class OidcSettingsTests(SimpleTestCase):
    ENV_KEYS = (
        "OIDC_INTERNAL_ISSUER", "OIDC_INTERNAL_CLIENT_ID", "OIDC_INTERNAL_CLIENT_SECRET",
        "OIDC_INTERNAL_EMAIL_DOMAINS", "OIDC_INTERNAL_POST_LOGOUT_REDIRECT", "LOCAL_LOGIN_ENABLED", "TRUSTED_PROXY_CIDRS", "GATEWAY_LOGOUT_URL",
    )

    def load(self, **env):
        base = {k: "" for k in self.ENV_KEYS}
        base["LOCAL_LOGIN_ENABLED"] = "False"
        base["OIDC_INTERNAL_EMAIL_DOMAINS"] = "worxphere.ai"
        base.update(env)
        with patch.dict(os.environ, base):
            return _load_base_settings()

    def test_no_oidc_values_disables_provider_and_keeps_local_login(self):
        cfg = self.load()
        self.assertEqual(cfg.OIDC_PROVIDERS, {})
        self.assertEqual(cfg.OIDC_PROVIDER_KEYS, ["internal"])
        self.assertTrue(cfg.LOCAL_LOGIN_ACTIVE)
        self.assertFalse(cfg.LOCAL_LOGIN_ENABLED)  # DEBUG 와 무관하게 기본 False
        self.assertEqual(cfg.TRUSTED_PROXY_CIDRS, [])
        self.assertEqual(cfg.GATEWAY_LOGOUT_URL, "")

    def test_all_three_values_enable_provider_and_close_local_login(self):
        cfg = self.load(OIDC_INTERNAL_ISSUER="https://sso.example.test/realms/world/",
                        OIDC_INTERNAL_CLIENT_ID="cid", OIDC_INTERNAL_CLIENT_SECRET="fake-secret")
        self.assertEqual(set(cfg.OIDC_PROVIDERS), {"internal"})
        self.assertEqual(cfg.OIDC_PROVIDERS["internal"]["client_id"], "cid")
        self.assertEqual(cfg.OIDC_PROVIDERS["internal"]["issuer"], "https://sso.example.test/realms/world")
        self.assertFalse(cfg.LOCAL_LOGIN_ACTIVE)

    def test_post_logout_redirect_flag_defaults_off_and_parses_bool(self):
        common = dict(OIDC_INTERNAL_ISSUER="https://sso.example.test/realms/world",
                      OIDC_INTERNAL_CLIENT_ID="cid", OIDC_INTERNAL_CLIENT_SECRET="fake-secret")
        self.assertIs(self.load(**common).OIDC_PROVIDERS["internal"]["post_logout_redirect"], False)
        for raw, expect in (("true", True), ("1", True), ("YES", True), ("false", False), ("", False)):
            with self.subTest(raw=raw):
                cfg = self.load(OIDC_INTERNAL_POST_LOGOUT_REDIRECT=raw, **common)
                self.assertIs(cfg.OIDC_PROVIDERS["internal"]["post_logout_redirect"], expect)

    def test_env_example_keeps_oidc_values_commented_out(self):
        lines = (BASE_DIR / ".env.example").read_text(encoding="utf-8").splitlines()
        for key in ("OIDC_INTERNAL_ISSUER", "OIDC_INTERNAL_CLIENT_ID", "OIDC_INTERNAL_CLIENT_SECRET",
                    "OIDC_INTERNAL_POST_LOGOUT_REDIRECT"):
            with self.subTest(key=key):
                self.assertFalse([l for l in lines if l.startswith(key + "=")], "주석 처리되어야 함")
                self.assertTrue([l for l in lines if l.startswith("# " + key + "=")])

    def test_local_login_enabled_keeps_both_open(self):
        cfg = self.load(OIDC_INTERNAL_ISSUER="https://sso.example.test/realms/world",
                        OIDC_INTERNAL_CLIENT_ID="cid", OIDC_INTERNAL_CLIENT_SECRET="fake-secret",
                        LOCAL_LOGIN_ENABLED="true")
        self.assertTrue(cfg.LOCAL_LOGIN_ACTIVE)
        self.assertEqual(set(cfg.OIDC_PROVIDERS), {"internal"})

    def test_partial_oidc_values_are_rejected(self):
        full = {"OIDC_INTERNAL_ISSUER": "https://sso.example.test/realms/world",
                "OIDC_INTERNAL_CLIENT_ID": "cid", "OIDC_INTERNAL_CLIENT_SECRET": "fake-secret"}
        for missing in full:
            with self.subTest(missing=missing):
                env = {k: v for k, v in full.items() if k != missing}
                with self.assertRaises(ImproperlyConfigured):
                    self.load(**env)

    def test_http_issuer_is_rejected_except_localhost(self):
        common = {"OIDC_INTERNAL_CLIENT_ID": "cid", "OIDC_INTERNAL_CLIENT_SECRET": "fake-secret"}
        with self.assertRaises(ImproperlyConfigured):
            self.load(OIDC_INTERNAL_ISSUER="http://sso.example.test/realms/world", **common)
        for host in ("localhost:8080", "127.0.0.1:8080"):
            with self.subTest(host=host):
                cfg = self.load(OIDC_INTERNAL_ISSUER=f"http://{host}/realms/dev", **common)
                self.assertIn("internal", cfg.OIDC_PROVIDERS)

    def test_email_domains_trusted_proxies_and_gateway_logout_parsing(self):
        cfg = self.load(OIDC_INTERNAL_EMAIL_DOMAINS=" WorxPhere.ai , @Example.com ,",
                        TRUSTED_PROXY_CIDRS="10.0.0.0/8, 172.16.0.0/12 ,",
                        GATEWAY_LOGOUT_URL=" https://gw.example.test/oauth2/sign_out ")
        self.assertEqual(set(cfg.OIDC_INTERNAL_EMAIL_DOMAINS), {"worxphere.ai", "example.com"})
        self.assertEqual(list(cfg.TRUSTED_PROXY_CIDRS), ["10.0.0.0/8", "172.16.0.0/12"])
        self.assertEqual(cfg.GATEWAY_LOGOUT_URL, "https://gw.example.test/oauth2/sign_out")

    def test_middleware_order(self):
        mw = list(settings.MIDDLEWARE)
        self.assertEqual(mw[0], "apps.accounts.middleware.ProxyBoundaryMiddleware")
        self.assertLess(mw.index("apps.accounts.middleware.ProxyBoundaryMiddleware"),
                        mw.index("django.middleware.security.SecurityMiddleware"))
        self.assertEqual(
            mw.index("apps.accounts.middleware.GatewaySessionMiddleware"),
            mw.index("django.contrib.auth.middleware.AuthenticationMiddleware") + 1,
        )


# ─────────────────────────────────────────────
# 11 401 리로드 스크립트
# ─────────────────────────────────────────────

class SessionExpiredReloadScriptTests(TestCase):
    JS = BASE_DIR / "static" / "js" / "qg_notify.js"

    def test_rendered_page_loads_the_reload_script(self):
        user = User.objects.create_user("jsu", password="pw-js-1", role="viewer")
        self.client.force_login(user)
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "js/qg_notify.js?v=")

    def test_reload_logic_exists_exactly_once_in_the_shared_script(self):
        src = self.JS.read_text(encoding="utf-8")
        self.assertEqual(src.count("window.location.reload()"), 1)
        self.assertEqual(src.count("function reloadOnSessionExpired"), 1)
        self.assertIn("resp.status === 401", src)           # fetch 래퍼
        self.assertIn("htmx:responseError", src)             # htmx 처리
        self.assertIn("xhr.status === 401", src)
        self.assertIn("sessionStorage", src)                 # 무한 새로고침 방지
        self.assertIn("10000", src)
        self.assertIn("isSameOrigin", src)                   # 같은 출처만

    def test_no_second_implementation_elsewhere(self):
        needles = ("reloadOnSessionExpired", "qg-401-reload-at")
        offenders = []
        for root in (BASE_DIR / "static" / "js", BASE_DIR / "templates"):
            for path in root.rglob("*"):
                if path.suffix not in (".js", ".html") or path == self.JS:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if any(n in text for n in needles):
                    offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_base_template_has_no_inline_401_reload(self):
        base = (BASE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertNotIn("status === 401", base)
        self.assertNotIn("location.reload", base)


# ─────────────────────────────────────────────
# 12 Viewer 허용 prefix
# ─────────────────────────────────────────────

class ViewerOidcPrefixTests(SSOTestBase):
    def test_oidc_paths_are_in_viewer_allowlist(self):
        for path in (SSO_LOGIN_URL, SSO_CALLBACK_URL, "/accounts/login/", "/accounts/logout/"):
            self.assertTrue(_viewer_page_allowed(path), path)
        self.assertFalse(_viewer_page_allowed("/accounts/users/"))

    def test_viewer_is_not_bounced_to_dashboard_on_callback_path(self):
        viewer = User.objects.create_user("vw", password="pw-vw-1", role="viewer", email="vw@worxphere.ai")
        self.client.force_login(viewer)
        self.set_token(error=RuntimeError("fail"))
        resp = self.client.get(SSO_CALLBACK_URL, {"code": "c", "state": "s"})
        # 미들웨어가 막았다면 대시보드로 302 — 뷰까지 도달하면 로그인 실패 화면(400)이 렌더된다.
        self.assertEqual(resp.status_code, 400)

    def test_viewer_still_blocked_from_other_account_pages(self):
        viewer = User.objects.create_user("vw2", password="pw-vw-2", role="viewer")
        self.client.force_login(viewer)
        resp = self.client.get("/accounts/users/")
        self.assertRedirects(resp, reverse("dashboard:index"), fetch_redirect_response=False)


# ─────────────────────────────────────────────
# 13 기존 회귀
# ─────────────────────────────────────────────

class ExistingBehaviorRegressionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("reg", password="pw-reg-1", role="qpe_ops", email="reg@worxphere.ai")

    def test_password_login_and_logout_still_work_when_local_login_active(self):
        with override_settings(OIDC_PROVIDERS={}, LOCAL_LOGIN_ACTIVE=True):
            resp = self.client.post(LOGIN_URL, {"username": "reg", "password": "pw-reg-1", "next": "/suites/runs/"})
            self.assertRedirects(resp, "/suites/runs/", fetch_redirect_response=False)
            self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
            self.assertTrue(ActivityLog.objects.filter(user=self.user, action=ActivityLog.Action.LOGIN).exists())
            resp = self.client.post("/accounts/logout/")
            self.assertRedirects(resp, LOGIN_URL + "?loggedout=1", fetch_redirect_response=False)
            self.assertNotIn("_auth_user_id", self.client.session)

    def test_wrong_password_and_pending_account_errors(self):
        User.objects.create_user("wait", password="pw-wait-1", is_active=False)
        with override_settings(OIDC_PROVIDERS={}, LOCAL_LOGIN_ACTIVE=True):
            resp = self.client.post(LOGIN_URL, {"username": "reg", "password": "wrong"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.context["error"], "invalid")
            resp = self.client.post(LOGIN_URL, {"username": "wait", "password": "pw-wait-1"})
            self.assertEqual(resp.context["error"], "pending")
            self.assertNotIn("_auth_user_id", self.client.session)

    def test_api_key_and_basic_auth_unaffected_when_local_login_closed(self):
        with override_settings(**SSO_ONLY):
            key, plaintext = ApiKey.issue(user=self.user, name="sso-regress", scopes=["runs:read"])
            self.assertEqual(self.client.get("/api/v2/health/").status_code, 200)
            resp = self.client.get("/api/v2/me/", headers={"Authorization": f"Bearer {plaintext}"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["api_key"]["key_prefix"], key.key_prefix)
            # 평문은 응답·로그 어디에도 남지 않는다 — 해시/prefix 만 검증.
            self.assertNotIn(plaintext, resp.content.decode())
            self.assertNotEqual(key.key_hash, plaintext)
            import base64
            token = base64.b64encode(b"reg:pw-reg-1").decode()
            resp = self.client.get("/api/v1/services/", headers={"Authorization": f"Basic {token}"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(self.client.get("/api/v1/services/").status_code, 403)

    def test_api_paths_are_not_redirected_to_login_for_anonymous(self):
        with override_settings(**SSO_ONLY):
            resp = self.client.get("/api/v2/me/")
        self.assertIn(resp.status_code, (401, 403))
        self.assertFalse(resp.has_header("Location"))


# ─────────────────────────────────────────────
# 게이트웨이 헤더 로그인 (Keycloak client 불필요 — docs/service-sso-migration.md §4)
# ─────────────────────────────────────────────

GW_PEER = "172.20.0.222"
GW_HEADERS = {"HTTP_X_AUTH_REQUEST_EMAIL": "hong@worxphere.ai", "HTTP_X_AUTH_REALM": "world"}


@override_settings(TRUSTED_PROXY_CIDRS=[f"{GW_PEER}/32"], OIDC_PROVIDERS={})
class GatewayHeaderLoginTests(TestCase):
    def _get(self, path="/", peer=GW_PEER, **extra):
        headers = {**GW_HEADERS, **extra}
        return self.client.get(path, REMOTE_ADDR=peer, **headers)

    def test_new_internal_user_is_created_as_active_viewer_and_logged_in(self):
        resp = self._get("/")
        self.assertEqual(resp.status_code, 200)
        user = User.objects.get(email="hong@worxphere.ai")
        self.assertEqual(user.role, User.Role.VIEWER)
        self.assertTrue(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_existing_account_is_matched_by_email_and_keeps_role(self):
        admin = User.objects.create_user("hong", email="Hong@Worxphere.ai", password="x", role=User.Role.QPE_ADMIN)
        self._get("/")
        self.assertEqual(self.client.session["_auth_user_id"], str(admin.pk))
        self.assertEqual(User.objects.filter(email__iexact="hong@worxphere.ai").count(), 1)

    def test_inactive_account_is_not_logged_in(self):
        User.objects.create_user("hong", email="hong@worxphere.ai", password="x", is_active=False)
        resp = self._get("/accounts/login/")
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_ambiguous_email_is_not_guessed(self):
        User.objects.create_user("a", email="hong@worxphere.ai", password="x")
        User.objects.create_user("b", email="hong@worxphere.ai", password="x")
        resp = self._get("/accounts/login/")
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(User.objects.filter(email="hong@worxphere.ai").count(), 2)

    def test_partner_realm_and_untrusted_peer_do_not_log_in(self):
        self._get("/", HTTP_X_AUTH_REALM="ext")
        self.assertNotIn("_auth_user_id", self.client.session)
        self._get("/", peer="10.44.32.6")  # ALB 등 게이트웨이가 아닌 출발지가 위조한 헤더
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(User.objects.filter(email="hong@worxphere.ai").exists())

    def test_without_headers_falls_back_to_login_page(self):
        resp = self.client.get("/", REMOTE_ADDR=GW_PEER)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_session_of_other_user_is_replaced_by_header_user(self):
        other = User.objects.create_user("other", email="other@worxphere.ai", password="x")
        self.client.force_login(other)
        self._get("/")
        user = User.objects.get(email="hong@worxphere.ai")
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_logout_goes_to_gateway_logout_by_default(self):
        self._get("/")
        resp = self.client.get("/accounts/logout/", REMOTE_ADDR=GW_PEER, **GW_HEADERS)
        assert_gateway_logout_page(self, resp, "https://qw.jkintra.net/_qw/logout")

    def test_login_page_redirects_logged_in_gateway_user_to_dashboard(self):
        resp = self._get("/accounts/login/")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/accounts/login/", resp["Location"])


class GatewayLogoutPageTests(TestCase):
    """게이트웨이 로그아웃 이동 페이지 — URL 이 HTML/JS 에 안전하게 들어가는지."""

    def test_url_is_escaped_in_html_and_js(self):
        from apps.accounts.sso import _gateway_logout_page
        url = 'https://gw.example.test/_qw/logout?a=1&b="x"</script><script>alert(1)</script>'
        body = _gateway_logout_page(url).content.decode()
        self.assertNotIn("</script><script>alert(1)", body)
        self.assertNotIn('b="x"', body)
        self.assertIn("&amp;b=&quot;x&quot;", body)
        self.assertIn("\\u003c/script>", body)

    def test_response_is_not_cacheable(self):
        user = User.objects.create_user("bye2", password="pw-bye-2", role="viewer", email="bye2@worxphere.ai")
        with override_settings(**SSO_ONLY):
            self.client.force_login(user)
            resp = self.client.get("/accounts/logout/", REMOTE_ADDR=PROXY_IP, headers={"X-Auth-Realm": "world"})
        self.assertIn("no-cache", resp["Cache-Control"])
        self.assertNotIn("_auth_user_id", self.client.session)


# ─────────────────────────────────────────────
# 게이트웨이 진입 로그인 (앱 자체 로그인 화면 + "Keycloak 로그인" 버튼 → 보호된 진입 경로)
# ─────────────────────────────────────────────

ENTRY_URL = "/accounts/gateway-login/"
ENTRY_SETTINGS = dict(SSO_ONLY, GATEWAY_ENTRY_ENABLED=True, LOCAL_LOGIN_ACTIVE=False)


class GatewayEntryTests(TestCase):
    def setUp(self):
        self.enterContext(override_settings(**ENTRY_SETTINGS))
        self.user = User.objects.create_user("eve", password="pw-eve-1", email="eve@worxphere.ai", role="qpe_admin")

    def test_login_page_shows_keycloak_button_to_protected_entry(self):
        resp = self.client.get(LOGIN_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Keycloak 로그인")
        self.assertContains(resp, f'href="{ENTRY_URL}"')
        self.assertNotContains(resp, 'name="password"')  # 자체 로그인 폼 없음

    def test_login_page_button_keeps_next(self):
        resp = self.client.get(LOGIN_URL + "?next=/suites/")
        self.assertContains(resp, f'href="{ENTRY_URL}?next=/suites/"')

    def test_no_button_when_flag_off(self):
        with override_settings(GATEWAY_ENTRY_ENABLED=False, LOCAL_LOGIN_ACTIVE=True):
            resp = self.client.get(LOGIN_URL)
        self.assertNotContains(resp, "Keycloak 로그인")
        self.assertNotContains(resp, ENTRY_URL)

    def test_entry_logs_in_with_gateway_header_and_goes_to_dashboard(self):
        resp = self.client.get(ENTRY_URL, REMOTE_ADDR=PROXY_IP,
                               headers={"X-Auth-Realm": "world", "X-Auth-Request-Email": "eve@worxphere.ai"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/")
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))

    def test_entry_honours_safe_next_only(self):
        hdr = {"X-Auth-Realm": "world", "X-Auth-Request-Email": "eve@worxphere.ai"}
        ok = self.client.get(ENTRY_URL + "?next=/suites/", REMOTE_ADDR=PROXY_IP, headers=hdr)
        self.assertEqual(ok["Location"], "/suites/")
        self.client.logout()
        evil = self.client.get(ENTRY_URL + "?next=https://evil.example.test/", REMOTE_ADDR=PROXY_IP, headers=hdr)
        self.assertEqual(evil["Location"], "/")

    def test_entry_without_gateway_header_renders_login_with_error_no_redirect_loop(self):
        resp = self.client.get(ENTRY_URL, REMOTE_ADDR="127.0.0.1")
        self.assertEqual(resp.status_code, 503)
        self.assertContains(resp, "alert-error", status_code=503)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_entry_with_forged_header_from_untrusted_peer_does_not_login(self):
        resp = self.client.get(ENTRY_URL, REMOTE_ADDR="203.0.113.9",
                               headers={"X-Auth-Realm": "world", "X-Auth-Request-Email": "eve@worxphere.ai"})
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_entry_inactive_account_is_denied_403(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        resp = self.client.get(ENTRY_URL, REMOTE_ADDR=PROXY_IP,
                               headers={"X-Auth-Realm": "world", "X-Auth-Request-Email": "eve@worxphere.ai"})
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_page_does_not_auto_start_or_auto_login_without_header(self):
        resp = self.client.get(LOGIN_URL, REMOTE_ADDR=PROXY_IP)
        self.assertEqual(resp.status_code, 200)

    def test_logout_without_headers_kills_gateway_session_then_returns_to_login_page(self):
        self.client.force_login(self.user)
        resp = self.client.get("/accounts/logout/", REMOTE_ADDR=PROXY_IP)  # 앱 경로는 게이트웨이 비보호 → 헤더 없음
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.has_header("Location"))
        body = resp.content.decode()
        self.assertIn(f'fetch("{GATEWAY_LOGOUT}"', body)
        self.assertIn('location.replace("/accounts/login/?loggedout=1")', body)
        self.assertIn('content="4;url=/accounts/login/?loggedout=1"', body)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_without_gateway_logout_url_goes_to_login(self):
        self.client.force_login(self.user)
        with override_settings(GATEWAY_LOGOUT_URL=""):
            resp = self.client.get("/accounts/logout/", REMOTE_ADDR=PROXY_IP)
        self.assertRedirects(resp, LOGIN_URL + "?loggedout=1", fetch_redirect_response=False)


class GatewayEntrySettingsTests(SimpleTestCase):
    """LOCAL_LOGIN_ACTIVE 계산 — 진입 모드를 켜면 자체 로그인은 기본 꺼지고, 비상 플래그로만 다시 열린다."""

    def load(self, **env):
        base = {"OIDC_INTERNAL_ISSUER": "", "OIDC_INTERNAL_CLIENT_ID": "", "OIDC_INTERNAL_CLIENT_SECRET": "",
                "LOCAL_LOGIN_ENABLED": "False", "GATEWAY_ENTRY_ENABLED": ""}
        base.update(env)
        with patch.dict(os.environ, base):
            return _load_base_settings()

    def test_entry_flag_defaults_off_and_keeps_local_login_when_no_oidc(self):
        cfg = self.load()
        self.assertFalse(cfg.GATEWAY_ENTRY_ENABLED)
        self.assertTrue(cfg.LOCAL_LOGIN_ACTIVE)

    def test_entry_flag_closes_local_login(self):
        cfg = self.load(GATEWAY_ENTRY_ENABLED="true")
        self.assertTrue(cfg.GATEWAY_ENTRY_ENABLED)
        self.assertFalse(cfg.LOCAL_LOGIN_ACTIVE)

    def test_emergency_flag_reopens_local_login(self):
        cfg = self.load(GATEWAY_ENTRY_ENABLED="true", LOCAL_LOGIN_ENABLED="true")
        self.assertTrue(cfg.LOCAL_LOGIN_ACTIVE)


class GatewayEntryHardeningTests(TestCase):
    """코드 리뷰(2026-10-09) 지적 사항 — 진입 모드의 노출 면 축소와 viewer 첫 로그인."""

    def setUp(self):
        self.enterContext(override_settings(**ENTRY_SETTINGS))
        self.admin = User.objects.create_user("adm", password="pw-adm-1", email="adm@worxphere.ai", role="qpe_admin")
        self.hdr = {"X-Auth-Realm": "world", "X-Auth-Request-Email": "adm@worxphere.ai"}

    def test_trusted_peer_header_on_non_entry_path_does_not_login(self):
        """게이트웨이가 비보호 경로에서 헤더를 안 덮어써도(설정 실수) 이메일만으로 로그인되지 않는다."""
        for path in ("/", "/suites/", "/accounts/login/", "/accounts/logout/"):
            with self.subTest(path=path):
                self.client.get(path, REMOTE_ADDR=PROXY_IP, headers=self.hdr)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_flag_off_keeps_header_login_on_any_path(self):
        with override_settings(GATEWAY_ENTRY_ENABLED=False):
            resp = self.client.get("/", REMOTE_ADDR=PROXY_IP, headers=self.hdr)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.admin.pk))

    def test_non_entry_path_headers_do_not_discard_existing_session(self):
        self.client.force_login(self.admin)
        self.client.get("/", REMOTE_ADDR=PROXY_IP,
                        headers={"X-Auth-Realm": "world", "X-Auth-Request-Email": "someone-else@worxphere.ai"})
        self.assertEqual(self.client.session["_auth_user_id"], str(self.admin.pk))

    def test_first_login_viewer_follows_next_without_error_flash(self):
        resp = self.client.get(
            ENTRY_URL + "?next=/suites/runs/", REMOTE_ADDR=PROXY_IP,
            headers={"X-Auth-Realm": "world", "X-Auth-Request-Email": "newbie@worxphere.ai"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/suites/runs/")
        viewer = User.objects.get(email="newbie@worxphere.ai")
        self.assertEqual(viewer.role, "viewer")
        self.assertEqual(self.client.session["_auth_user_id"], str(viewer.pk))
        self.assertEqual([str(m) for m in get_messages(resp.wsgi_request)], [])

    def test_ext_realm_cannot_use_entry(self):
        resp = self.client.get(ENTRY_URL, REMOTE_ADDR=PROXY_IP,
                               headers={"X-Auth-Realm": "ext", "X-Auth-Request-Email": "partner@vendor.example"})
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_page_then_url_is_escaped(self):
        from apps.accounts.sso import _gateway_logout_page
        body = _gateway_logout_page("https://gw.example.test/_qw/logout",
                                    then='/x"</script><script>alert(1)</script>').content.decode()
        self.assertNotIn("</script><script>alert(1)", body)
        self.assertIn("\\u003c/script>", body)
        self.assertIn("&quot;&lt;/script&gt;", body)

    def test_direct_oidc_user_keeps_keycloak_end_session_branch(self):
        """id_token 이 있는 직접 OIDC 로그인 사용자는 게이트웨이 fetch 페이지가 아니라 기존 분기를 탄다."""
        self.client.force_login(self.admin)
        session = self.client.session
        session["sso_id_token"] = "x"  # 값 자체는 쓰이지 않는다(공급자 미설정 → (c) 로그인 화면)
        session.save()
        resp = self.client.get("/accounts/logout/", REMOTE_ADDR=PROXY_IP)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("loggedout=1", resp["Location"])

    def test_emergency_local_login_shows_form_and_button_and_logs_in(self):
        with override_settings(LOCAL_LOGIN_ACTIVE=True):
            page = self.client.get(LOGIN_URL)
            self.assertContains(page, 'name="password"')
            self.assertContains(page, "Keycloak 로그인")
            resp = self.client.post(LOGIN_URL, {"username": "adm", "password": "pw-adm-1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.admin.pk))
