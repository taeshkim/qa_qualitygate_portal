"""GET /runs/<id>/status/ + GET /suite-runs/<id>/status/ — 가벼운 폴링 엔드포인트."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.api_v2 import phase as _phase
from apps.api_v2 import scopes as S
from apps.api_v2.models import ApiKey
from apps.qualitygates.models import (
    QualityGate,
    QualityGateFolder,
    QualityGateRun,
    QualityGateSubFolder,
)
from apps.suites.models import TestSuite, TestSuiteRun


def _auth(plaintext):
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}


@override_settings(ALLOWED_HOSTS=["*"])
class RunStatusEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="ls", password="pw", role="qpe_ops")
        folder = QualityGateFolder.objects.create(name=f"fls-{uuid.uuid4().hex[:6]}")
        sub = QualityGateSubFolder.objects.create(folder=folder, name="auth")
        self.gate = QualityGate.objects.create(
            subfolder=sub, folder=folder, name=f"gls-{uuid.uuid4().hex[:6]}",
            gate_type="dev_stg", deploy_job_path="job/x",
        )
        _, self.plaintext = ApiKey.issue(user=self.user, name="ls", scopes=[S.RUNS_READ])

    def _run(self, status):
        return QualityGateRun.objects.create(
            gate=self.gate, triggered_by=self.user, seq_no=1, status=status,
        )

    def test_terminal_run_returns_phase_succeeded(self):
        run = self._run(QualityGateRun.Status.COMPLETED)
        resp = self.client.get(f"/api/v2/runs/{run.pk}/status/", **_auth(self.plaintext))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["phase"], _phase.SUCCEEDED)
        self.assertTrue(body["is_terminal"])
        self.assertEqual(body["result"], "PASS")
        self.assertEqual(resp["X-QGP-Poll-After"], "0")

    def test_failed_run_returns_phase_failed(self):
        run = self._run(QualityGateRun.Status.DEPLOY_FAILED)
        resp = self.client.get(f"/api/v2/runs/{run.pk}/status/", **_auth(self.plaintext))
        body = resp.json()
        self.assertEqual(body["phase"], _phase.FAILED)
        self.assertEqual(body["result"], "FAIL")
        self.assertEqual(resp["X-QGP-Poll-After"], "0")

    def test_running_run_returns_phase_running_and_poll_5(self):
        run = self._run(QualityGateRun.Status.TESTING)
        with patch("apps.qualitygates.views._poll_gate_run") as m:
            m.return_value = None
            resp = self.client.get(f"/api/v2/runs/{run.pk}/status/", **_auth(self.plaintext))
        body = resp.json()
        self.assertEqual(body["phase"], _phase.RUNNING)
        self.assertFalse(body["is_terminal"])
        self.assertEqual(resp["X-QGP-Poll-After"], "5")

    def test_awaiting_input_phase(self):
        run = self._run(QualityGateRun.Status.QG_PASSED)
        resp = self.client.get(f"/api/v2/runs/{run.pk}/status/", **_auth(self.plaintext))
        body = resp.json()
        self.assertEqual(body["phase"], _phase.AWAITING_INPUT)
        self.assertFalse(body["is_terminal"])

    def test_cancelled_run_returns_phase_cancelled(self):
        run = self._run(QualityGateRun.Status.CANCELLED)
        resp = self.client.get(f"/api/v2/runs/{run.pk}/status/", **_auth(self.plaintext))
        body = resp.json()
        self.assertEqual(body["phase"], _phase.CANCELLED)
        self.assertEqual(body["result"], "CANCELLED")

    def test_status_404(self):
        resp = self.client.get(
            f"/api/v2/runs/{uuid.uuid4()}/status/", **_auth(self.plaintext)
        )
        self.assertEqual(resp.status_code, 404)

    def test_status_scope_check(self):
        _, plain_only_meta = ApiKey.issue(user=self.user, name="ls-meta", scopes=[S.META_READ])
        run = self._run(QualityGateRun.Status.COMPLETED)
        resp = self.client.get(f"/api/v2/runs/{run.pk}/status/", **_auth(plain_only_meta))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")


@override_settings(ALLOWED_HOSTS=["*"])
class SuiteRunStatusEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="srs", password="pw", role="qpe_ops")
        self.suite = TestSuite.objects.create(
            name=f"s-{uuid.uuid4().hex[:6]}",
            jenkins_folder_path="ATS/sample",
        )
        _, self.plaintext = ApiKey.issue(user=self.user, name="srs", scopes=[S.RUNS_READ])

    def _suite_run(self, **kwargs):
        return TestSuiteRun.objects.create(
            suite=self.suite, triggered_by=self.user, total_jobs=1, **kwargs,
        )

    def test_running_suite(self):
        sr = self._suite_run(status=TestSuiteRun.Status.RUNNING)
        with patch("apps.suites.views._poll_run_status") as m:
            m.return_value = None
            resp = self.client.get(f"/api/v2/suite-runs/{sr.pk}/status/", **_auth(self.plaintext))
        body = resp.json()
        self.assertEqual(body["phase"], _phase.RUNNING)
        self.assertFalse(body["is_terminal"])
        self.assertEqual(resp["X-QGP-Poll-After"], "5")

    def test_completed_all_passed_returns_succeeded(self):
        sr = self._suite_run(
            status=TestSuiteRun.Status.COMPLETED, passed_jobs=1, failed_jobs=0,
        )
        resp = self.client.get(f"/api/v2/suite-runs/{sr.pk}/status/", **_auth(self.plaintext))
        body = resp.json()
        self.assertEqual(body["phase"], _phase.SUCCEEDED)
        self.assertTrue(body["is_terminal"])
        self.assertEqual(resp["X-QGP-Poll-After"], "0")

    def test_completed_any_failed_returns_failed(self):
        sr = self._suite_run(
            status=TestSuiteRun.Status.COMPLETED, passed_jobs=0, failed_jobs=1,
        )
        resp = self.client.get(f"/api/v2/suite-runs/{sr.pk}/status/", **_auth(self.plaintext))
        body = resp.json()
        self.assertEqual(body["phase"], _phase.FAILED)
        self.assertTrue(body["is_terminal"])
