"""Phase G — 마무리. X-QGP-Poll-After 헤더 + 에러 코드 카탈로그 검증."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.api_v2 import scopes as S
from apps.api_v2.models import ApiKey
from apps.qualitygates.models import (
    QualityGate,
    QualityGateFolder,
    QualityGateRun,
    QualityGateSubFolder,
)


def _auth(plaintext):
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}


@override_settings(ALLOWED_HOSTS=["*"])
class PollAfterHeaderTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="poller", password="pw", role="qpe_ops")
        folder = QualityGateFolder.objects.create(name=f"fp-{uuid.uuid4().hex[:6]}")
        sub = QualityGateSubFolder.objects.create(folder=folder, name="auth")
        self.gate = QualityGate.objects.create(
            subfolder=sub, folder=folder, name=f"gp-{uuid.uuid4().hex[:6]}",
            gate_type="dev_stg", deploy_job_path="job/x",
        )
        _, self.plaintext = ApiKey.issue(user=self.user, name="poll", scopes=[S.RUNS_READ])

    def _make_run(self, status):
        return QualityGateRun.objects.create(
            gate=self.gate, triggered_by=self.user, seq_no=1, status=status,
        )

    def test_run_detail_running_returns_poll_after_5(self):
        run = self._make_run(QualityGateRun.Status.TESTING)
        # _poll_gate_run 가 Jenkins 미설정으로 상태를 망가뜨릴 수 있으니 no-op.
        with patch("apps.qualitygates.views._poll_gate_run") as m:
            m.return_value = None
            resp = self.client.get(f"/api/v2/runs/{run.pk}/", **_auth(self.plaintext))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-QGP-Poll-After"], "5")

    def test_run_detail_terminal_returns_poll_after_0(self):
        run = self._make_run(QualityGateRun.Status.COMPLETED)
        resp = self.client.get(f"/api/v2/runs/{run.pk}/", **_auth(self.plaintext))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-QGP-Poll-After"], "0")

    def test_run_detail_cancelled_is_terminal(self):
        run = self._make_run(QualityGateRun.Status.CANCELLED)
        resp = self.client.get(f"/api/v2/runs/{run.pk}/", **_auth(self.plaintext))
        self.assertEqual(resp["X-QGP-Poll-After"], "0")


@override_settings(ALLOWED_HOSTS=["*"])
class ErrorCatalogTests(TestCase):
    """PRD §7 의 표준 에러 카탈로그가 일관된 포맷으로 노출되는지."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="cat", password="pw", role="qpe_ops")

    def test_not_authenticated(self):
        resp = self.client.get("/api/v2/me/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "not_authenticated")

    def test_api_key_invalid(self):
        resp = self.client.get(
            "/api/v2/me/", HTTP_AUTHORIZATION="Bearer qgp_deadbeef_" + "0" * 32
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "api_key_invalid")

    def test_scope_required(self):
        _, plaintext = ApiKey.issue(user=self.user, name="cat-meta", scopes=[S.RUNS_READ])
        resp = self.client.get("/api/v2/gates/", **_auth(plaintext))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")

    def test_not_found(self):
        _, plaintext = ApiKey.issue(user=self.user, name="cat-nf", scopes=[S.RUNS_READ])
        resp = self.client.get(
            f"/api/v2/runs/{uuid.uuid4()}/", **_auth(plaintext)
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "not_found")


@override_settings(ALLOWED_HOSTS=["*"])
class ApiKeyPlaintextDoesNotLeakTests(TestCase):
    """CLAUDE.md 룰: 평문은 DB / 응답 (발급 외) / 로그 어디에도 남지 않음."""

    def test_plaintext_not_stored_in_db(self):
        User = get_user_model()
        user = User.objects.create_user(username="leak", password="pw")
        key, plaintext = ApiKey.issue(user=user, name="leak", scopes=[S.META_READ])
        # 평문 secret 부분이 DB row 의 어떤 필드에도 그대로 들어 있으면 안 됨.
        _, prefix, secret = plaintext.split("_", 2)
        self.assertNotIn(secret, key.key_hash)
        self.assertNotIn(secret, key.key_prefix)
        # repr/str 에도 평문이 노출되면 안 됨.
        self.assertNotIn(secret, str(key))
        self.assertNotIn(secret, repr(key))

    def test_me_response_does_not_echo_plaintext(self):
        User = get_user_model()
        user = User.objects.create_user(username="leak2", password="pw")
        _, plaintext = ApiKey.issue(user=user, name="leak2", scopes=[S.META_READ])
        resp = self.client.get("/api/v2/me/", **_auth(plaintext))
        body = resp.content.decode()
        _, prefix, secret = plaintext.split("_", 2)
        # 응답에 prefix 는 있어도 secret 은 절대 없어야.
        self.assertIn(prefix, body)
        self.assertNotIn(secret, body)
