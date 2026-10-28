"""/ci-api/ — 게이트웨이 로그인 예외 경로: API Key(Bearer/Basic)로만 인증, 쿠키·X-Auth-* 무시."""

from __future__ import annotations

import base64

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import Resolver404, resolve, reverse

from apps.api_v2 import scopes as S
from apps.api_v2.models import ApiKey


def _bearer(key):
    return {"HTTP_AUTHORIZATION": f"Bearer {key}"}


def _basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"HTTP_AUTHORIZATION": f"Basic {token}"}


class CiApiAuthTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="ci-bot", password="pw", role="qpe_ops", email="ci-bot@worxphere.ai",
        )
        self.key, self.plaintext = ApiKey.issue(
            user=self.user, name="ci", scopes=[S.META_READ, S.RUNS_READ],
        )
        self.client = Client()

    def test_health_is_public_and_json(self):
        resp = self.client.get("/ci-api/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_bearer_key_authenticates(self):
        resp = self.client.get("/ci-api/me/", **_bearer(self.plaintext))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["api_key"]["key_prefix"], self.key.key_prefix)

    def test_basic_key_in_password_or_username_authenticates(self):
        for auth in (_basic("anything", self.plaintext), _basic(self.plaintext, "")):
            with self.subTest(auth=auth["HTTP_AUTHORIZATION"][:12]):
                resp = self.client.get("/ci-api/me/", **auth)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["user"]["username"], "ci-bot")

    def test_basic_with_real_password_is_not_accepted(self):
        # 아이디/비밀번호 Basic 은 ci-api 에서 통하지 않는다(키 전용).
        resp = self.client.get("/ci-api/me/", **_basic("ci-bot", "pw"))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "not_authenticated")

    def test_no_credentials_is_json_401(self):
        resp = self.client.get("/ci-api/me/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "not_authenticated")

    def test_invalid_key_is_401(self):
        resp = self.client.get("/ci-api/me/", **_bearer("qgp_deadbeef_" + "0" * 32))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "api_key_invalid")

    def test_session_cookie_is_ignored(self):
        # 같은 클라이언트가 로그인한 세션 쿠키를 들고 있어도 /ci-api/ 는 키 없이는 401.
        self.assertTrue(self.client.login(username="ci-bot", password="pw"))
        self.assertEqual(self.client.get("/api/v2/me/").status_code, 200)
        self.assertEqual(self.client.get("/ci-api/me/").status_code, 401)

    @override_settings(TRUSTED_PROXY_CIDRS=["127.0.0.1/32"], GATEWAY_ENTRY_ENABLED=False)
    def test_gateway_identity_headers_are_ignored(self):
        # 신뢰 프록시 출발지라도 /ci-api/ 의 X-Auth-* 는 자동 로그인에 쓰이지 않는다.
        headers = {"HTTP_X_AUTH_REQUEST_EMAIL": "ci-bot@worxphere.ai", "HTTP_X_AUTH_REALM": "world"}
        resp = self.client.get("/ci-api/me/", REMOTE_ADDR="127.0.0.1", **headers)
        self.assertEqual(resp.status_code, 401)

    def test_key_wins_over_session_user(self):
        # 세션 사용자(다른 사람)와 키 주인이 달라도 키 주인으로 처리된다(쿠키 무시).
        other = get_user_model().objects.create_user(username="other", password="pw", role="qpe_ops")
        self.client.login(username="other", password="pw")
        resp = self.client.get("/ci-api/me/", **_bearer(self.plaintext))
        self.assertEqual(resp.json()["user"]["username"], "ci-bot")
        self.assertNotEqual(other.username, "ci-bot")

    def test_csrf_checks_do_not_block_key_requests(self):
        client = Client(enforce_csrf_checks=True)
        resp = client.get("/ci-api/gates/", **_bearer(self.plaintext))
        self.assertEqual(resp.status_code, 200)


class CiApiUrlconfTests(TestCase):
    URLCONF = "config.urls_ci"

    def test_reverse_stays_under_ci_api(self):
        # 응답 본문 링크(self_url 등)가 게이트웨이 예외 경로 안에서 따라갈 수 있어야 한다.
        rid = "11111111-1111-1111-1111-111111111111"
        self.assertEqual(
            reverse("api_v2:run_detail", kwargs={"run_id": rid}, urlconf=self.URLCONF),
            f"/ci-api/runs/{rid}/",
        )
        self.assertEqual(
            reverse("api_v2:run_detail", kwargs={"run_id": rid}), f"/api/v2/runs/{rid}/",
        )

    def test_export_and_docs_are_not_exposed(self):
        for path in ("/ci-api/export/snapshots/", "/ci-api/schema/", "/ci-api/docs/"):
            with self.subTest(path=path):
                with self.assertRaises(Resolver404):
                    resolve(path, urlconf=self.URLCONF)
        # 일반 경로에는 그대로 존재한다.
        self.assertEqual(resolve("/api/v2/schema/").url_name, "schema")

    def test_export_via_ci_api_is_404(self):
        resp = Client().get("/ci-api/export/snapshots/")
        self.assertEqual(resp.status_code, 404)
