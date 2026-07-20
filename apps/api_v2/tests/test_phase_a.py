"""Phase A — ApiKey 모델 + Authentication + 미들웨어 bypass + 표준 에러 응답."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.api_v2 import scopes as S
from apps.api_v2.models import ApiKey, hash_secret


def _auth(plaintext):
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}


class ApiKeyModelTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="alice", password="pw", role="qpe_ops")

    def test_issue_returns_plaintext_and_hash(self):
        key, plaintext = ApiKey.issue(user=self.user, name="k1", scopes=[S.META_READ])
        self.assertTrue(plaintext.startswith("qgp_"))
        # plaintext = qgp_{prefix}_{secret}
        _, prefix, secret = plaintext.split("_", 2)
        self.assertEqual(prefix, key.key_prefix)
        self.assertEqual(hash_secret(secret), key.key_hash)
        self.assertTrue(key.is_active)
        self.assertIsNotNone(key.expires_at)  # 디폴트 90일

    def test_find_by_plaintext_matches_only_correct_secret(self):
        key, plaintext = ApiKey.issue(user=self.user, name="k2", scopes=[S.RUNS_READ])
        self.assertEqual(ApiKey.find_by_plaintext(plaintext), key)
        # 같은 prefix + 잘못된 secret
        bogus = f"qgp_{key.key_prefix}_" + "0" * 32
        self.assertIsNone(ApiKey.find_by_plaintext(bogus))

    def test_revoke_and_expiry(self):
        k1, _ = ApiKey.issue(user=self.user, name="r1", scopes=[S.META_READ])
        k1.revoke()
        self.assertTrue(k1.is_revoked)
        self.assertFalse(k1.is_active)

        k2, _ = ApiKey.issue(user=self.user, name="r2", scopes=[S.META_READ])
        k2.expires_at = timezone.now() - timedelta(seconds=1)
        k2.save(update_fields=["expires_at"])
        self.assertTrue(k2.is_expired)
        self.assertFalse(k2.is_active)

    def test_scope_matching(self):
        k, _ = ApiKey.issue(user=self.user, name="s", scopes=[S.META_READ, S.RUNS_READ])
        self.assertTrue(k.has_scope(S.META_READ))
        self.assertFalse(k.has_scope(S.RUNS_TRIGGER))
        k.scopes = [S.WILDCARD]
        k.save(update_fields=["scopes"])
        self.assertTrue(k.has_scope(S.RUNS_CONTROL))  # 와일드카드 커버


class ApiKeyAuthEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="bob", password="pw", role="qpe_ops")
        self.key, self.plaintext = ApiKey.issue(
            user=self.user,
            name="bob-ci",
            scopes=[S.META_READ, S.RUNS_READ],
        )
        self.client = Client()

    def test_health_is_public(self):
        resp = self.client.get("/api/v2/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_me_requires_auth(self):
        resp = self.client.get("/api/v2/me/")
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertIn("error", body)
        self.assertEqual(body["error"]["code"], "not_authenticated")

    def test_me_with_valid_key(self):
        resp = self.client.get("/api/v2/me/", **_auth(self.plaintext))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["user"]["username"], "bob")
        self.assertEqual(body["api_key"]["key_prefix"], self.key.key_prefix)
        self.assertEqual(sorted(body["api_key"]["scopes"]), [S.META_READ, S.RUNS_READ])

    def test_me_with_invalid_key(self):
        resp = self.client.get("/api/v2/me/", **_auth("qgp_deadbeef_" + "0" * 32))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "api_key_invalid")

    def test_me_with_revoked_key(self):
        self.key.revoke()
        resp = self.client.get("/api/v2/me/", **_auth(self.plaintext))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "api_key_revoked")

    def test_me_with_expired_key(self):
        self.key.expires_at = timezone.now() - timedelta(seconds=1)
        self.key.save(update_fields=["expires_at"])
        resp = self.client.get("/api/v2/me/", **_auth(self.plaintext))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "api_key_expired")

    def test_me_with_malformed_authorization(self):
        # Bearer 스킴이 아니면 401
        resp = self.client.get("/api/v2/me/", HTTP_AUTHORIZATION="Token foo")
        self.assertEqual(resp.status_code, 401)

    def test_session_auth_also_works(self):
        # API Key 없이 세션 로그인 → /me 성공
        self.client.login(username="bob", password="pw")
        resp = self.client.get("/api/v2/me/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNone(body["api_key"])  # 세션 경로엔 api_key 정보 없음


class MiddlewareBypassTests(TestCase):
    """RolePermissionMiddleware 가 /api/v2/ 경로를 우회하는지 확인."""

    def setUp(self):
        User = get_user_model()
        self.viewer = User.objects.create_user(username="vee", password="pw", role="viewer")
        self.client = Client()

    def test_api_v2_post_not_blocked_by_middleware_for_viewer(self):
        # Viewer 가 다른 경로에 POST 하면 미들웨어가 redirect (302). /api/v2/health/ 는
        # GET 만 허용이라 POST 시 미들웨어가 아닌 DRF 가 405 를 돌려준다. 즉 미들웨어를
        # 통과했다는 의미.
        self.client.login(username="vee", password="pw")
        resp = self.client.post("/api/v2/health/")
        # 미들웨어가 가로채면 302, 통과하면 DRF 가 405.
        self.assertEqual(resp.status_code, 405)
