"""Phase E — Quality Gate Run 제어 API."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.api_v2 import scopes as S
from apps.api_v2.models import ApiKey
from apps.qualitygates import services as qg_services
from apps.qualitygates.models import (
    QualityGate,
    QualityGateFolder,
    QualityGateRun,
    QualityGateSubFolder,
)


def _auth(plaintext):
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}


def _make_world(role="qpe_ops", scopes=None):
    User = get_user_model()
    user = User.objects.create_user(username=f"u-{uuid.uuid4().hex[:6]}", password="pw", role=role)
    folder = QualityGateFolder.objects.create(name=f"f-{uuid.uuid4().hex[:6]}")
    sub = QualityGateSubFolder.objects.create(folder=folder, name="auth")
    gate = QualityGate.objects.create(
        subfolder=sub,
        folder=folder,
        name=f"g-{uuid.uuid4().hex[:6]}",
        gate_type="dev_stg",
        deploy_job_path="job/x",
    )
    run = QualityGateRun.objects.create(gate=gate, triggered_by=user, seq_no=1)
    key, plaintext = ApiKey.issue(
        user=user, name="e-test", scopes=scopes or [S.RUNS_CONTROL]
    )
    return user, gate, run, key, plaintext


@override_settings(ALLOWED_HOSTS=["*"])
class CancelRunTests(TestCase):
    def test_cancel_invokes_service_and_returns_detail(self):
        user, gate, run, key, plaintext = _make_world()
        run.status = QualityGateRun.Status.TESTING
        run.save(update_fields=["status"])

        with patch.object(qg_services, "cancel_run") as m:
            def fake_cancel(*, run, user):
                run.status = QualityGateRun.Status.CANCELLED
                run.save(update_fields=["status"])
                return run
            m.side_effect = fake_cancel
            resp = self.client.post(
                f"/api/v2/runs/{run.pk}/cancel/", **_auth(plaintext)
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "cancelled")
        self.assertTrue(m.called)

    def test_cancel_without_control_scope_403(self):
        user, _, run, _, plaintext = _make_world(scopes=[S.RUNS_READ])
        resp = self.client.post(f"/api/v2/runs/{run.pk}/cancel/", **_auth(plaintext))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")

    def test_cancel_viewer_denied(self):
        user, _, run, _, plaintext = _make_world(role="viewer")
        resp = self.client.post(f"/api/v2/runs/{run.pk}/cancel/", **_auth(plaintext))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "role_denied")

    def test_cancel_404(self):
        _, _, _, _, plaintext = _make_world()
        bogus = uuid.uuid4()
        resp = self.client.post(f"/api/v2/runs/{bogus}/cancel/", **_auth(plaintext))
        self.assertEqual(resp.status_code, 404)


@override_settings(ALLOWED_HOSTS=["*"])
class ArgocdRetryTests(TestCase):
    def test_retry_deploy_calls_service(self):
        _, _, run, _, plaintext = _make_world()
        run.status = QualityGateRun.Status.DEPLOY_FAILED
        run.save(update_fields=["status"])
        with patch.object(qg_services, "argocd_retry") as m:
            m.side_effect = lambda **kw: kw["run"]
            resp = self.client.post(
                f"/api/v2/runs/{run.pk}/argocd/deploy/retry/", **_auth(plaintext)
            )
        self.assertEqual(resp.status_code, 202)
        m.assert_called_once()
        args = m.call_args.kwargs
        self.assertEqual(args["stage"], "deploy")

    def test_retry_invalid_stage_400(self):
        _, _, run, _, plaintext = _make_world()
        resp = self.client.post(
            f"/api/v2/runs/{run.pk}/argocd/bogus/retry/", **_auth(plaintext)
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_parameters")


@override_settings(ALLOWED_HOSTS=["*"])
class DeployTriggerTests(TestCase):
    def test_trigger_deploy_requires_waiting_deploy(self):
        _, _, run, _, plaintext = _make_world()
        run.status = QualityGateRun.Status.TESTING  # 잘못된 상태
        run.save(update_fields=["status"])
        resp = self.client.post(f"/api/v2/runs/{run.pk}/deploy/", **_auth(plaintext))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "run_not_actionable")

    def test_trigger_deploy_passes_parameters(self):
        _, _, run, _, plaintext = _make_world()
        run.status = QualityGateRun.Status.WAITING_DEPLOY
        run.save(update_fields=["status"])
        with patch.object(qg_services, "trigger_deploy") as m:
            m.side_effect = lambda **kw: kw["run"]
            resp = self.client.post(
                f"/api/v2/runs/{run.pk}/deploy/",
                data={"parameters": {"BRANCH_NAME": "release/release_0511"}},
                content_type="application/json",
                **_auth(plaintext),
            )
        self.assertEqual(resp.status_code, 200)
        args = m.call_args.kwargs
        self.assertEqual(args["parameters"], {"BRANCH_NAME": "release/release_0511"})


@override_settings(ALLOWED_HOSTS=["*"])
class RerunDeployTests(TestCase):
    def test_rerun_deploy_requires_deploy_failed(self):
        _, _, run, _, plaintext = _make_world()
        run.status = QualityGateRun.Status.WAITING_DEPLOY  # 잘못된 상태
        run.save(update_fields=["status"])
        resp = self.client.post(
            f"/api/v2/runs/{run.pk}/rerun-deploy/", **_auth(plaintext)
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "run_not_actionable")
        self.assertEqual(
            resp.json()["error"]["details"]["current_status"], "waiting_deploy"
        )

    def test_rerun_deploy_ok(self):
        _, _, run, _, plaintext = _make_world()
        run.status = QualityGateRun.Status.DEPLOY_FAILED
        run.save(update_fields=["status"])
        with patch.object(qg_services, "trigger_deploy") as m:
            m.side_effect = lambda **kw: kw["run"]
            resp = self.client.post(
                f"/api/v2/runs/{run.pk}/rerun-deploy/", **_auth(plaintext)
            )
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()


@override_settings(ALLOWED_HOSTS=["*"])
class RerunPathsTests(TestCase):
    def test_rerun_gate_creates_new_run(self):
        _, _, run, _, plaintext = _make_world()
        run.status = QualityGateRun.Status.FAILED
        run.save(update_fields=["status"])

        new_run = QualityGateRun.objects.create(
            gate=run.gate, triggered_by=run.triggered_by, seq_no=2
        )
        with patch.object(qg_services, "rerun_gate") as m:
            m.return_value = new_run
            resp = self.client.post(
                f"/api/v2/runs/{run.pk}/rerun/", **_auth(plaintext)
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["run_id"], str(new_run.pk))

    def test_rerun_test_calls_service(self):
        _, _, run, _, plaintext = _make_world()
        with patch.object(qg_services, "rerun_test") as m:
            m.side_effect = lambda **kw: kw["run"]
            resp = self.client.post(
                f"/api/v2/runs/{run.pk}/rerun-test/", **_auth(plaintext)
            )
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()
