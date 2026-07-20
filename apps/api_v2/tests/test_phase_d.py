"""Phase D-2..6 — Quality Gate Run 조회/트리거/콘솔 API.

서비스 레이어 (`apps.qualitygates.services.trigger_gate_run`) 까지 들어가면 Jenkins 호출이
일어나므로 `JenkinsClient.trigger_build` 를 패치한다. 폴링 (`_poll_gate_run`) 도 외부 호출 발생
가능성이 있어 단위 테스트에서는 no-op 으로 패치.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.api_v2 import scopes as S
from apps.api_v2.models import ApiKey
from apps.qualitygates.models import (
    QualityGate,
    QualityGateFolder,
    QualityGateRun,
    QualityGateSubFolder,
)


def _auth(plaintext: str):
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}


def _make_gate(
    *,
    name="gate-A",
    auto_mode=False,
    image_job_path="",
    prod_job_path="",
) -> QualityGate:
    folder = QualityGateFolder.objects.create(
        name=f"folder-{name}",
        dev_jenkins_url="https://jenkins.local",
        dev_jenkins_user="dev@example.com",
        dev_jenkins_token="x",
    )
    subfolder = QualityGateSubFolder.objects.create(folder=folder, name="sub")
    return QualityGate.objects.create(
        folder=folder,
        subfolder=subfolder,
        name=name,
        gate_type=QualityGate.GateType.DEV_STG,
        auto_mode=auto_mode,
        deploy_job_path="folder/deploy-job",
        image_job_path=image_job_path,
        prod_job_path=prod_job_path,
    )


class ListRunsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="alice", password="pw", role="qpe_ops")
        self.key, self.plaintext = ApiKey.issue(
            user=self.user, name="ci", scopes=[S.RUNS_READ],
        )
        self.gate = _make_gate(name="gate-list")
        # 두 개의 Run — 하나는 진행 중, 하나는 완료.
        self.r1 = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.TESTING, seq_no=1,
            triggered_by=self.user,
        )
        self.r2 = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.COMPLETED, seq_no=2,
            triggered_by=self.user,
        )
        self.client = Client()

    def test_list_requires_scope(self):
        # 키 발급은 RUNS_READ 만 — 이 케이스는 통과해야 한다.
        resp = self.client.get(
            f"/api/v2/gates/{self.gate.pk}/runs/", **_auth(self.plaintext),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 2)
        ids = {r["run_id"] for r in body["results"]}
        self.assertSetEqual(ids, {str(self.r1.pk), str(self.r2.pk)})

    def test_list_rejects_missing_scope(self):
        key2, plain2 = ApiKey.issue(
            user=self.user, name="meta-only", scopes=[S.META_READ],
        )
        resp = self.client.get(
            f"/api/v2/gates/{self.gate.pk}/runs/", **_auth(plain2),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")

    def test_list_filters_status(self):
        resp = self.client.get(
            f"/api/v2/gates/{self.gate.pk}/runs/?status=completed",
            **_auth(self.plaintext),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["run_id"], str(self.r2.pk))

    def test_list_limit(self):
        resp = self.client.get(
            f"/api/v2/gates/{self.gate.pk}/runs/?limit=1",
            **_auth(self.plaintext),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)

    def test_list_404_for_unknown_gate(self):
        import uuid
        resp = self.client.get(
            f"/api/v2/gates/{uuid.uuid4()}/runs/", **_auth(self.plaintext),
        )
        self.assertEqual(resp.status_code, 404)


class TriggerRunTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="bob", password="pw", role="qpe_ops")
        self.viewer = User.objects.create_user(username="vee", password="pw", role="viewer")
        self.gate = _make_gate(name="gate-trigger")
        self.client = Client()

    def _trigger_key(self):
        _, plain = ApiKey.issue(
            user=self.user, name="trig", scopes=[S.RUNS_TRIGGER, S.RUNS_READ],
        )
        return plain

    @patch("apps.qualitygates.services.JenkinsClient")
    def test_trigger_creates_run(self, mock_jc):
        mock_jc.return_value.trigger_build.return_value = {"build_number": 42}
        plain = self._trigger_key()
        resp = self.client.post(
            f"/api/v2/gates/{self.gate.pk}/runs/",
            data={"parameters": {"BRANCH_NAME": "release/release_0511"}},
            content_type="application/json",
            **_auth(plain),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertIn("run_id", body)
        self.assertEqual(body["gate_id"], str(self.gate.pk))
        self.assertEqual(body["status"], "deploying")  # image_job 없으므로 바로 deploying
        self.assertTrue(body["self_url"].startswith("/api/v2/runs/"))

    @patch("apps.qualitygates.services.JenkinsClient")
    def test_trigger_409_when_active_run_exists(self, mock_jc):
        QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.TESTING, seq_no=1,
        )
        plain = self._trigger_key()
        resp = self.client.post(
            f"/api/v2/gates/{self.gate.pk}/runs/",
            data={"parameters": {}},
            content_type="application/json",
            **_auth(plain),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "active_run_exists")

    @patch("apps.qualitygates.services.JenkinsClient")
    def test_trigger_invalid_parameters_type(self, mock_jc):
        plain = self._trigger_key()
        resp = self.client.post(
            f"/api/v2/gates/{self.gate.pk}/runs/",
            data={"parameters": "not-a-dict"},
            content_type="application/json",
            **_auth(plain),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_parameters")

    def test_trigger_rejects_missing_trigger_scope(self):
        _, plain = ApiKey.issue(
            user=self.user, name="read", scopes=[S.RUNS_READ],
        )
        resp = self.client.post(
            f"/api/v2/gates/{self.gate.pk}/runs/",
            data={"parameters": {}},
            content_type="application/json",
            **_auth(plain),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")

    def test_trigger_rejects_viewer_role(self):
        _, plain = ApiKey.issue(
            user=self.viewer, name="vkey", scopes=[S.RUNS_TRIGGER],
        )
        resp = self.client.post(
            f"/api/v2/gates/{self.gate.pk}/runs/",
            data={"parameters": {}},
            content_type="application/json",
            **_auth(plain),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "role_denied")


class RunDetailTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="carol", password="pw", role="qpe_ops")
        self.key, self.plain = ApiKey.issue(
            user=self.user, name="r", scopes=[S.RUNS_READ],
        )
        self.gate = _make_gate(name="gate-detail")
        self.run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.COMPLETED, seq_no=1,
            triggered_by=self.user,
            deploy_build_number=88,
            deploy_console_output="hello world\n",
        )
        self.client = Client()

    def test_detail_returns_run(self):
        resp = self.client.get(
            f"/api/v2/runs/{self.run.pk}/", **_auth(self.plain),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["run_id"], str(self.run.pk))
        self.assertEqual(body["gate"]["id"], str(self.gate.pk))
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["is_terminal"])
        self.assertEqual(body["result_label"], "PASS")
        self.assertIn("deploy", body)
        self.assertEqual(body["deploy"]["build_number"], 88)
        self.assertEqual(body["console_urls"]["deploy"], f"/api/v2/runs/{self.run.pk}/console/deploy/")

    def test_detail_polls_when_running(self):
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.TESTING, seq_no=2,
            triggered_by=self.user,
        )
        with patch("apps.qualitygates.views._poll_gate_run") as mock_poll:
            resp = self.client.get(
                f"/api/v2/runs/{run.pk}/", **_auth(self.plain),
            )
        self.assertEqual(resp.status_code, 200)
        mock_poll.assert_called_once()

    def test_detail_404(self):
        import uuid
        resp = self.client.get(
            f"/api/v2/runs/{uuid.uuid4()}/", **_auth(self.plain),
        )
        self.assertEqual(resp.status_code, 404)


class ConsoleTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="dan", password="pw", role="qpe_ops")
        self.key, self.plain = ApiKey.issue(
            user=self.user, name="c", scopes=[S.RUNS_READ],
        )
        self.gate = _make_gate(name="gate-console")
        self.run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.COMPLETED, seq_no=1,
            deploy_build_number=10,
            deploy_build_url="https://jenkins.local/job/foo/10/",
            deploy_console_output="A" * 200,
        )
        self.client = Client()

    def test_console_full(self):
        resp = self.client.get(
            f"/api/v2/runs/{self.run.pk}/console/deploy/", **_auth(self.plain),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["stage"], "deploy")
        self.assertEqual(body["length"], 200)
        self.assertFalse(body["truncated"])
        self.assertEqual(len(body["output"]), 200)
        self.assertEqual(body["build_number"], 10)
        self.assertTrue(body["build_url"].startswith("https://"))

    def test_console_tail(self):
        resp = self.client.get(
            f"/api/v2/runs/{self.run.pk}/console/deploy/?tail=50",
            **_auth(self.plain),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["length"], 200)
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["output"]), 50)

    def test_console_invalid_stage(self):
        resp = self.client.get(
            f"/api/v2/runs/{self.run.pk}/console/bogus/", **_auth(self.plain),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_stage")

    def test_console_force_tail_when_over_1mb(self):
        # 1MB 초과 + tail 미지정 → 마지막 50KB 만 + truncated=True.
        self.run.deploy_console_output = "B" * 1_500_000
        self.run.save(update_fields=["deploy_console_output"])
        resp = self.client.get(
            f"/api/v2/runs/{self.run.pk}/console/deploy/", **_auth(self.plain),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["truncated"])
        self.assertEqual(body["length"], 1_500_000)
        self.assertEqual(len(body["output"]), 50_000)
