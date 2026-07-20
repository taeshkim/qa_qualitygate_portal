"""Phase D-1 service layer 단위 테스트.

외부 호출 (Jenkins / Slack) 을 patch 해서 service 함수의 상태 전이와
GateRunError 발생 조건만 검증한다.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.qualitygates import services
from apps.qualitygates.models import (
    QualityGate, QualityGateFolder, QualityGateRun, QualityGateRunTest,
)  # noqa: F401 — QualityGateFolder 는 StatusDisplayTests 에서 사용


User = get_user_model()


def _make_gate(**overrides) -> QualityGate:
    folder = QualityGateFolder.objects.create(
        name="folder1",
        dev_jenkins_url="https://j.example.com",
        dev_jenkins_user="u",
        dev_jenkins_token="t",
    )
    defaults = dict(
        folder=folder,
        name="gate1",
        gate_type=QualityGate.GateType.DEV_STG,
        deploy_job_path="folder/deploy",
    )
    defaults.update(overrides)
    return QualityGate.objects.create(**defaults)


class TriggerGateRunTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pw", role="qpe_ops")
        self.gate = _make_gate()

    @mock.patch("apps.qualitygates.services.send_event_async")
    @mock.patch("apps.qualitygates.services.JenkinsClient")
    def test_creates_run_and_triggers_deploy_when_no_image_job(self, mock_jc, _sea):
        mock_jc.return_value.trigger_build.return_value = {"build_number": 42}
        run = services.trigger_gate_run(
            gate=self.gate, user=self.user, parameters={"FOO": "bar"},
        )
        run.refresh_from_db()
        self.assertEqual(run.status, QualityGateRun.Status.DEPLOYING)
        self.assertEqual(run.deploy_build_number, 42)
        self.assertEqual(run.triggered_by, self.user)
        self.assertEqual(run.seq_no, 1)
        # parameters 전달 확인
        mock_jc.return_value.trigger_build.assert_called_once()
        _args, kwargs = mock_jc.return_value.trigger_build.call_args
        self.assertEqual(kwargs.get("parameters"), {"FOO": "bar"})

    @mock.patch("apps.qualitygates.services.send_event_async")
    @mock.patch("apps.qualitygates.services.JenkinsClient")
    def test_blocks_when_active_run_exists(self, mock_jc, _sea):
        mock_jc.return_value.trigger_build.return_value = {"build_number": 1}
        services.trigger_gate_run(gate=self.gate, user=self.user, parameters={})
        with self.assertRaises(services.GateRunError) as ctx:
            services.trigger_gate_run(gate=self.gate, user=self.user, parameters={})
        self.assertEqual(ctx.exception.code, "active_run_exists")
        self.assertEqual(ctx.exception.http_status, 409)

    @mock.patch("apps.qualitygates.services.send_event_async")
    @mock.patch("apps.qualitygates.services.JenkinsClient")
    def test_cancels_waiting_user_runs(self, mock_jc, _sea):
        mock_jc.return_value.trigger_build.return_value = {"build_number": 1}
        old = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.WAITING_DEPLOY,
            triggered_by=self.user, seq_no=1,
        )
        services.trigger_gate_run(gate=self.gate, user=self.user, parameters={})
        old.refresh_from_db()
        self.assertEqual(old.status, QualityGateRun.Status.CANCELLED)


class CancelRunTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u2", password="pw", role="qpe_ops")
        self.gate = _make_gate()

    def test_cancel_completed_run_raises(self):
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.COMPLETED,
            triggered_by=self.user, seq_no=1,
        )
        with self.assertRaises(services.GateRunError) as ctx:
            services.cancel_run(run=run, user=self.user)
        self.assertEqual(ctx.exception.code, "cannot_cancel")

    @mock.patch("apps.qualitygates.services.send_event_async")
    @mock.patch("apps.qualitygates.services.JenkinsClient")
    def test_cancel_deploying_aborts_build_and_marks_cancelled(self, mock_jc, _sea):
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.DEPLOYING,
            deploy_build_number=7,
            triggered_by=self.user, seq_no=1,
        )
        result = services.cancel_run(run=run, user=self.user)
        self.assertEqual(result.status, QualityGateRun.Status.CANCELLED)
        mock_jc.return_value.stop_build.assert_called_once_with(
            self.gate.deploy_job_path, 7,
        )


class TriggerDeployTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u3", password="pw", role="qpe_ops")
        self.gate = _make_gate()

    def test_invalid_state_raises(self):
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.PENDING,
            triggered_by=self.user, seq_no=1,
        )
        with self.assertRaises(services.GateRunError) as ctx:
            services.trigger_deploy(run=run, user=self.user, parameters={})
        self.assertEqual(ctx.exception.code, "invalid_state")

    @mock.patch("apps.qualitygates.services.send_event_async")
    @mock.patch("apps.qualitygates.services.JenkinsClient")
    def test_retry_resets_argocd_fields(self, mock_jc, _sea):
        mock_jc.return_value.trigger_build.return_value = {"build_number": 99}
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.DEPLOY_FAILED,
            deploy_build_url="https://stale.example.com",
            deploy_argocd_sync_status="OutOfSync",
            triggered_by=self.user, seq_no=1,
        )
        result = services.trigger_deploy(run=run, user=self.user, parameters={})
        result.refresh_from_db()
        self.assertEqual(result.status, QualityGateRun.Status.DEPLOYING)
        self.assertEqual(result.deploy_build_number, 99)
        self.assertEqual(result.deploy_build_url, "")
        self.assertEqual(result.deploy_argocd_sync_status, "")


class ArgocdRetryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u4", password="pw", role="qpe_ops")
        self.gate = _make_gate()

    def test_invalid_stage_raises(self):
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.DEPLOYING,
            triggered_by=self.user, seq_no=1,
        )
        with self.assertRaises(services.GateRunError) as ctx:
            services.argocd_retry(run=run, stage="bogus", user=self.user)
        self.assertEqual(ctx.exception.code, "invalid_stage")

    @mock.patch("apps.qualitygates.views._poll_gate_run")
    def test_deploy_stage_resets_fields_and_calls_poll(self, mock_poll):
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.COMPLETED,
            deploy_argocd_sync_status="Synced",
            deploy_argocd_health_status="Healthy",
            triggered_by=self.user, seq_no=1,
        )
        result = services.argocd_retry(run=run, stage="deploy", user=self.user)
        result.refresh_from_db()
        self.assertEqual(result.deploy_argocd_sync_status, "")
        self.assertEqual(result.deploy_argocd_health_status, "")
        # COMPLETED 였으니 DEPLOYING 으로 되돌려야 한다.
        self.assertEqual(result.status, QualityGateRun.Status.DEPLOYING)
        mock_poll.assert_called_once()


class StatusDisplayTests(TestCase):
    """status_display property — gate_type 에 따른 환경명 매핑 회귀 방지."""

    def setUp(self):
        # 각 게이트 타입별 1개씩만 생성, 같은 인스턴스 재사용해 UNIQUE 위반 회피.
        self.dev_stg_gate = _make_gate(
            gate_type=QualityGate.GateType.DEV_STG,
        )
        # 별도 folder 로 stg_prd 게이트 생성
        folder2 = QualityGateFolder.objects.create(
            name="folder2",
            dev_jenkins_url="https://j.example.com",
            dev_jenkins_user="u",
            dev_jenkins_token="t",
        )
        self.stg_prd_gate = QualityGate.objects.create(
            folder=folder2,
            name="gate-stg-prd",
            gate_type=QualityGate.GateType.STG_PRD,
            deploy_job_path="folder/deploy2",
        )

    def test_dev_stg_labels_use_dev_and_stg(self):
        cases = {
            QualityGateRun.Status.WAITING_DEPLOY: "Awaiting Dev Deploy",
            QualityGateRun.Status.DEPLOYING: "Dev Deploying",
            QualityGateRun.Status.DEPLOY_FAILED: "Dev Deploy Failed",
            QualityGateRun.Status.QG_PASSED: "QG Passed (Awaiting Stg Deploy)",
            QualityGateRun.Status.PROD_DEPLOYING: "Stg Deploying",
            QualityGateRun.Status.PROD_DEPLOY_FAILED: "Stg Deploy Failed",
        }
        for status, expected in cases.items():
            run = QualityGateRun(gate=self.dev_stg_gate, status=status)
            self.assertEqual(
                run.status_display, expected,
                msg=f"DEV_STG / {status} expected {expected!r}",
            )

    def test_stg_prd_labels_use_stg_and_prd(self):
        cases = {
            QualityGateRun.Status.WAITING_DEPLOY: "Awaiting Stg Deploy",
            QualityGateRun.Status.DEPLOYING: "Stg Deploying",
            QualityGateRun.Status.DEPLOY_FAILED: "Stg Deploy Failed",
            QualityGateRun.Status.QG_PASSED: "QG Passed (Awaiting Prd Deploy)",
            QualityGateRun.Status.PROD_DEPLOYING: "Prd Deploying",
            QualityGateRun.Status.PROD_DEPLOY_FAILED: "Prd Deploy Failed",
        }
        for status, expected in cases.items():
            run = QualityGateRun(gate=self.stg_prd_gate, status=status)
            self.assertEqual(
                run.status_display, expected,
                msg=f"STG_PRD / {status} expected {expected!r}",
            )

    def test_non_env_labels_fall_through_to_enum_display(self):
        """이미지 빌드/테스트/완료 등 환경 무관 상태는 enum 의 display name 그대로."""
        for status, expected in [
            (QualityGateRun.Status.PENDING, "Pending"),
            (QualityGateRun.Status.BUILDING_IMAGE, "Building Image"),
            (QualityGateRun.Status.TESTING, "Testing"),
            (QualityGateRun.Status.COMPLETED, "Completed"),
            (QualityGateRun.Status.CANCELLED, "Cancelled"),
        ]:
            run = QualityGateRun(gate=self.dev_stg_gate, status=status)
            self.assertEqual(run.status_display, expected)
