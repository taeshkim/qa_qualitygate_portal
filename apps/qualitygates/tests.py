"""Phase D-1 service layer 단위 테스트.

외부 호출 (Jenkins / Slack) 을 patch 해서 service 함수의 상태 전이와
GateRunError 발생 조건만 검증한다.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.utils import timezone

from apps.qualitygates import services
from apps.qualitygates.models import (
    QualityGate, QualityGateFolder, QualityGateRun, QualityGateRunTest,
)  # noqa: F401 — QualityGateFolder 는 StatusDisplayTests 에서 사용
from apps.qualitygates.views import _override_app_image_default


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
            QualityGateRun.Status.QG_PASSED: "QG Passed (Awaiting Stg Approval)",
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
            QualityGateRun.Status.QG_PASSED: "QG Passed (Awaiting Prd Approval)",
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


# =============================================
# APP_IMAGE 빈값 차단 패치 회귀 테스트
# =============================================

class AppImageValidationServiceTests(TestCase):
    """services._validate_app_image_param / trigger_deploy / trigger_prod APP_IMAGE 가드.

    수동/외부 API 경로용 마지막 방어선 — UI 가드(_renderParam)와 API 응답 가드
    (_override_app_image_default)를 통과한 뒤에도 services 레이어가 빈 APP_IMAGE를
    최종 차단한다.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="u_img", password="pw", role="qpe_ops")
        self.gate = _make_gate()

    @mock.patch("apps.qualitygates.services.send_event_async")
    @mock.patch("apps.qualitygates.services.JenkinsClient")
    def test_trigger_deploy_rejects_empty_app_image(self, mock_jc, _sea):
        """APP_IMAGE="" 전달 시 GateRunError(invalid_app_image) 발생, Jenkins 미호출."""
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.WAITING_DEPLOY,
            triggered_by=self.user, seq_no=1,
        )
        with self.assertRaises(services.GateRunError) as ctx:
            services.trigger_deploy(run=run, user=self.user, parameters={"APP_IMAGE": ""})
        self.assertEqual(ctx.exception.code, "invalid_app_image")
        mock_jc.return_value.trigger_build.assert_not_called()

    @mock.patch("apps.qualitygates.services.send_event_async")
    @mock.patch("apps.qualitygates.services.JenkinsClient")
    def test_trigger_deploy_rejects_dangling_colon_app_image(self, mock_jc, _sea):
        """APP_IMAGE가 콜론으로 끝나는 깨진 태그 패턴도 거부한다."""
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.WAITING_DEPLOY,
            triggered_by=self.user, seq_no=2,
        )
        with self.assertRaises(services.GateRunError) as ctx:
            services.trigger_deploy(
                run=run, user=self.user,
                parameters={"APP_IMAGE": "registry/foo:"},
            )
        self.assertEqual(ctx.exception.code, "invalid_app_image")
        mock_jc.return_value.trigger_build.assert_not_called()

    @mock.patch("apps.qualitygates.services.send_event_async")
    @mock.patch("apps.qualitygates.services.JenkinsClient")
    def test_trigger_deploy_passes_valid_app_image(self, mock_jc, _sea):
        """올바른 APP_IMAGE 값이면 가드를 통과하고 Jenkins trigger_build 가 호출된다."""
        mock_jc.return_value.trigger_build.return_value = {"build_number": 123}
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.WAITING_DEPLOY,
            triggered_by=self.user, seq_no=3,
        )
        result = services.trigger_deploy(
            run=run, user=self.user,
            parameters={"APP_IMAGE": "registry/foo:208-20260526_093435"},
        )
        result.refresh_from_db()
        self.assertEqual(result.status, QualityGateRun.Status.DEPLOYING)
        mock_jc.return_value.trigger_build.assert_called_once()

    @mock.patch("apps.qualitygates.services.send_event_async")
    @mock.patch("apps.qualitygates.services.JenkinsClient")
    def test_trigger_prod_rejects_empty_app_image(self, mock_jc, _sea):
        """trigger_prod 도 대칭적으로 빈 APP_IMAGE 를 거부한다."""
        # prod 트리거를 허용하려면 prod_job_path 설정이 필요하다
        self.gate.prod_job_path = "folder/prod"
        self.gate.save(update_fields=["prod_job_path"])
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.QG_PASSED,
            triggered_by=self.user, seq_no=4,
        )
        with self.assertRaises(services.GateRunError) as ctx:
            services.trigger_prod(run=run, user=self.user, parameters={"APP_IMAGE": ""})
        self.assertEqual(ctx.exception.code, "invalid_app_image")
        mock_jc.return_value.trigger_build.assert_not_called()


class OverrideAppImageDefaultTests(TestCase):
    """views._override_app_image_default 헬퍼 단위 테스트."""

    def test_override_app_image_match(self):
        """choices 중 image_build_number prefix 매칭 항목이 있으면 default 로 설정, True 반환."""
        parameters = [
            {
                "name": "APP_IMAGE",
                "choices": ["713-old-tag", "714-foo", "714-bar"],
                "default": "",
            }
        ]
        result = _override_app_image_default(parameters, image_build_number=714)
        self.assertTrue(result)
        # prefix "714-" 매칭 첫 번째 항목이 default 로 설정돼야 한다
        self.assertEqual(parameters[0]["default"], "714-foo")

    def test_override_app_image_no_match(self):
        """choices 에 prefix 매칭 항목이 없으면 default 그대로, False 반환."""
        parameters = [
            {
                "name": "APP_IMAGE",
                "choices": ["713-old-tag", "712-older"],
                "default": "713-old-tag",
            }
        ]
        result = _override_app_image_default(parameters, image_build_number=714)
        self.assertFalse(result)
        # default 가 변경되지 않아야 한다
        self.assertEqual(parameters[0]["default"], "713-old-tag")

    def test_override_app_image_not_present(self):
        """parameters 에 APP_IMAGE 항목이 아예 없으면 True (해당 없음 = submit 허용)."""
        parameters = [
            {"name": "BRANCH", "default": "main"},
        ]
        result = _override_app_image_default(parameters, image_build_number=714)
        self.assertTrue(result)


class DeployParamsViewTests(TestCase):
    """deploy_params / prod_deploy_params 뷰 응답에 app_image_matched 키 포함 여부."""

    def setUp(self):
        self.user = User.objects.create_user(username="u_view", password="pw", role="qpe_ops")
        self.gate = _make_gate()
        self.client = Client()
        self.client.force_login(self.user)

    @mock.patch("apps.qualitygates.views.JenkinsClient")
    def test_deploy_params_response_has_app_image_matched(self, mock_jc):
        """deploy_params 응답 JSON 에 app_image_matched 키가 존재해야 한다."""
        mock_jc.return_value.get_job_info.return_value = {
            "parameters": [
                {"name": "APP_IMAGE", "choices": ["100-abc"], "default": ""},
            ]
        }
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.WAITING_DEPLOY,
            triggered_by=self.user, seq_no=1,
        )
        response = self.client.get(f"/qualitygates/runs/{run.pk}/deploy/params/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("app_image_matched", data)

    @mock.patch("apps.qualitygates.views.JenkinsClient")
    def test_prod_deploy_params_response_has_app_image_matched(self, mock_jc):
        """prod_deploy_params 응답 JSON 에 app_image_matched 키가 존재해야 한다."""
        mock_jc.return_value.get_job_info.return_value = {
            "parameters": [
                {"name": "APP_IMAGE", "choices": ["100-abc"], "default": ""},
            ]
        }
        self.gate.prod_job_path = "folder/prod"
        self.gate.save(update_fields=["prod_job_path"])
        run = QualityGateRun.objects.create(
            gate=self.gate, status=QualityGateRun.Status.QG_PASSED,
            triggered_by=self.user, seq_no=1,
        )
        response = self.client.get(f"/qualitygates/runs/{run.pk}/prod-deploy/params/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("app_image_matched", data)


# =============================================
# extract_pushed_image_tag 단위 테스트
# =============================================

class ExtractPushedImageTagTests(TestCase):
    """apps.integrations.jenkins.extract_pushed_image_tag 순수 함수 단위 테스트.

    실제 콘솔 형식 샘플을 사용하되, 평문 자격증명/API 키는 포함하지 않는다.
    """

    def setUp(self):
        from apps.integrations.jenkins import extract_pushed_image_tag
        self.extract = extract_pushed_image_tag

    def _make_console(self, build_number, repo, tag_suffix="20260602_153100"):
        """jib --target 라인이 포함된 현실적인 콘솔 출력 샘플 생성."""
        tag = f"{build_number}-{tag_suffix}"
        ref = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/{repo}:{tag}"
        return (
            "[INFO] --- jib-maven-plugin:3.3.1:build (default-cli) ---\n"
            f"[INFO] Executing tasks: jib --target={ref} --send-credentials-over-http\n"
            "[INFO] BUILD SUCCESS\n"
            "Finished: SUCCESS\n"
        )

    # (1) 단일 --target 정상 추출
    def test_single_target_extracts_tag(self):
        """--target= 이 1개인 정상 콘솔에서 정확한 tag 를 반환해야 한다."""
        console = self._make_console(210, "jobko/auth-admin-api")
        result = self.extract(console, build_number=210)
        self.assertEqual(result, "210-20260602_153100")

    # (2) image_build_failed: --target 없음 → None
    def test_no_target_returns_none(self):
        """--target 이 없는 콘솔(빌드 실패 케이스)은 None 을 반환해야 한다."""
        console = (
            "[INFO] Compiling sources...\n"
            "[ERROR] BUILD FAILURE\n"
            "[ERROR] Failed to execute goal\n"
        )
        result = self.extract(console, build_number=210)
        self.assertIsNone(result)

    # (3) 다른 build_number prefix 는 무시
    def test_different_build_number_tag_is_ignored(self):
        """콘솔에 다른 빌드 번호 tag 가 섞여있어도 현재 build_number prefix 것만 채택한다."""
        # bn=210 인데 콘솔에 bn=209 tag 가 있는 경우
        console = (
            "[INFO] jib --target="
            "905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/jobko/auth-admin-api:209-20260601_090000"
            " (previous build residue)\n"
            "Finished: SUCCESS\n"
        )
        result = self.extract(console, build_number=210)
        self.assertIsNone(result)

    # (4) repo_hint 로 멀티 후보 필터링
    def test_repo_hint_narrows_to_correct_repo(self):
        """repo_hint 가 주어지면 hint 에 suffix 매칭되는 후보만 채택한다."""
        # 두 repo 가 섞인 콘솔 — 멀티모듈 가상 시나리오
        tag1 = "210-20260602_153100"
        ref1 = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/jobko/auth-admin-api:{tag1}"
        tag2 = "210-20260602_153200"
        ref2 = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/jobko/auth-worker:{tag2}"
        console = (
            f"[INFO] jib --target={ref1}\n"
            f"[INFO] jib --target={ref2}\n"
            "Finished: SUCCESS\n"
        )
        # hint 없으면 후보 2개(서로 다른 tag) → None (모호)
        result_no_hint = self.extract(console, build_number=210)
        self.assertIsNone(result_no_hint)

        # hint='jobko/auth-admin-api' 이면 ref1 만 선택
        result_with_hint = self.extract(console, build_number=210, repo_hint="jobko/auth-admin-api")
        self.assertEqual(result_with_hint, tag1)

        # hint='jobko/auth-worker' 이면 ref2 만 선택
        result_worker = self.extract(console, build_number=210, repo_hint="jobko/auth-worker")
        self.assertEqual(result_worker, tag2)

    # (5) --target 없을 때 일반 ECR 참조 폴백
    def test_ecr_ref_fallback_when_no_target(self):
        """--target 이 없어도 'registry/repo:tag' 패턴에서 tag 를 추출한다."""
        tag = "395-20260511_171818"
        ref = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/jobkorea/auth-api:{tag}"
        # --target 없이 ECR 참조만 있는 콘솔
        console = (
            "[INFO] Pushing image to ECR...\n"
            f"[INFO] Pushed {ref} successfully\n"
            "Finished: SUCCESS\n"
        )
        result = self.extract(console, build_number=395)
        self.assertEqual(result, tag)

    # (6) console=None → None
    def test_none_console_returns_none(self):
        result = self.extract(None, build_number=210)
        self.assertIsNone(result)

    # (7) build_number=None → None (prefix 가드 전부 거부)
    def test_none_build_number_returns_none(self):
        console = self._make_console(210, "jobko/auth-admin-api")
        result = self.extract(console, build_number=None)
        self.assertIsNone(result)

    # (8) build_number=0 → None
    def test_zero_build_number_returns_none(self):
        console = self._make_console(210, "jobko/auth-admin-api")
        result = self.extract(console, build_number=0)
        self.assertIsNone(result)

    # (9) repo_hint 가 0건 매칭이면 전체 후보로 폴백 (단일 후보 → tag 반환)
    def test_repo_hint_zero_match_falls_back_to_all_candidates(self):
        """hint 가 어떤 후보와도 매칭되지 않으면 전체 후보로 폴백한다."""
        console = self._make_console(210, "jobko/auth-admin-api")
        # 잘못된 hint 주어도 단일 후보이므로 tag 반환
        result = self.extract(console, build_number=210, repo_hint="wrong/repo")
        self.assertEqual(result, "210-20260602_153100")

    # (10) 따옴표 포함 콘솔에도 강건 (_TARGET_RE 가 직접 처리)
    def test_target_with_quotes_handled(self):
        """--target='<ref>' / --target="<ref>" 형식(따옴표 포함)도 정상 파싱한다."""
        tag = "210-20260602_153100"
        ref = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/jobko/auth-admin-api:{tag}"
        for quoted in (f"jib --target='{ref}'", f'jib --target="{ref}"'):
            console = f"[INFO] {quoted}\nFinished: SUCCESS\n"
            result = self.extract(console, build_number=210)
            self.assertEqual(result, tag, msg=f"failed for: {quoted}")

    # (11) [회귀] 중단된 빌드: --target 은 echo 됐지만 Finished: SUCCESS 없음 → None
    def test_interrupted_build_with_echoed_target_returns_none(self):
        """shell 이 jib 명령을 실행 전 echo 하므로, 중간에 죽은 빌드(Finished: FAILURE)
        콘솔에도 --target 라인이 남는다. 이미지는 ECR 에 푸시되지 않았으므로 None 이어야 한다.
        (실제 사고 패턴: build 1070/702 가 InterruptedException 으로 중단된 케이스)"""
        tag = "1070-20260511_135004"
        ref = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/jobko/auth-internal-api:{tag}"
        console = (
            f"[INFO] + jib-0.13.0/bin/jib jar --target={ref} ...\n"
            "Cannot contact pod-xyz: java.lang.InterruptedException\n"
            "ERROR: Cannot resume build because FlowNode 52 could not be loaded\n"
            "Finished: FAILURE\n"
        )
        result = self.extract(console, build_number=1070)
        self.assertIsNone(result)

    # (12) [회귀] 동일 --target 라인이 중복 출력돼도 단일 tag 반환 (dedup)
    def test_duplicate_target_lines_returns_tag(self):
        """같은 ref:tag 가 여러 번 echo 되어도 모호로 처리하지 않고 그 tag 를 반환한다."""
        tag = "210-20260602_153100"
        ref = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/jobko/auth-admin-api:{tag}"
        console = (
            f"[INFO] jib --target={ref}\n"
            f"[INFO] jib --target={ref}\n"
            "Finished: SUCCESS\n"
        )
        result = self.extract(console, build_number=210)
        self.assertEqual(result, tag)

    # (13) [회귀] 같은 tag 를 여러 repo 에 푸시(멀티모듈)해도 tag 동일하면 반환
    def test_same_tag_multiple_repos_returns_tag(self):
        """서로 다른 repo 라도 tag 값이 모두 같으면 모호하지 않으므로 그 tag 를 반환한다."""
        tag = "210-20260602_153100"
        ref1 = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/jobko/auth-admin-api:{tag}"
        ref2 = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/jobko/auth-admin-batch:{tag}"
        console = (
            f"[INFO] jib --target={ref1}\n"
            f"[INFO] jib --target={ref2}\n"
            "Finished: SUCCESS\n"
        )
        result = self.extract(console, build_number=210)
        self.assertEqual(result, tag)


# =============================================
# resolve_and_apply_app_image 단위 테스트
# =============================================

class ResolveAndApplyAppImageTests(TestCase):
    """services.resolve_and_apply_app_image 함수 단위 테스트."""

    def setUp(self):
        self.user = User.objects.create_user(username="u_resolve", password="pw", role="qpe_ops")
        self.gate = _make_gate()

    def _make_run(self, build_number=None, console_output=""):
        return QualityGateRun.objects.create(
            gate=self.gate,
            status=QualityGateRun.Status.WAITING_DEPLOY,
            triggered_by=self.user,
            seq_no=99,
            image_build_number=build_number,
            image_console_output=console_output,
        )

    def _make_console(self, build_number, repo="jobko/auth-admin-api"):
        tag = f"{build_number}-20260602_153100"
        ref = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/{repo}:{tag}"
        return f"[INFO] jib --target={ref}\n[INFO] BUILD SUCCESS\nFinished: SUCCESS\n"

    # (1) 빈 choices + console 있는 run → default=tag, choices=[tag], True 반환
    def test_empty_choices_with_console_injects_tag(self):
        """choices=[] 이고 콘솔에 tag 있으면 default=tag, choices=[tag] 주입 후 True 반환."""
        console = self._make_console(210)
        run = self._make_run(build_number=210, console_output=console)
        parameters = [{"name": "APP_IMAGE", "choices": [], "default": ""}]
        result = services.resolve_and_apply_app_image(parameters, run)
        self.assertTrue(result)
        self.assertEqual(parameters[0]["default"], "210-20260602_153100")
        self.assertIn("210-20260602_153100", parameters[0]["choices"])

    # (2) APP_IMAGE 없으면 True (무관)
    def test_no_app_image_param_returns_true(self):
        """parameters 에 APP_IMAGE 가 없으면 True (해당 없음 — submit 허용)."""
        run = self._make_run(build_number=210)
        parameters = [{"name": "BRANCH", "default": "main"}]
        result = services.resolve_and_apply_app_image(parameters, run)
        self.assertTrue(result)

    # (3) 추출 실패 + choices 빈 → False
    def test_extraction_fails_empty_choices_returns_false(self):
        """콘솔 추출도 실패하고 choices 도 비어있으면 False 반환."""
        run = self._make_run(build_number=210, console_output="")
        parameters = [{"name": "APP_IMAGE", "choices": [], "default": ""}]
        result = services.resolve_and_apply_app_image(parameters, run)
        self.assertFalse(result)

    # (4) choices 에 prefix 항목 + console 없음 → prefix 폴백 True
    def test_prefix_fallback_when_no_console(self):
        """콘솔 없어도 choices 에 build_number prefix 항목이 있으면 폴백 True."""
        run = self._make_run(build_number=210, console_output="")
        parameters = [
            {"name": "APP_IMAGE", "choices": ["210-20260601_080000", "209-20260531_070000"], "default": ""}
        ]
        result = services.resolve_and_apply_app_image(parameters, run)
        self.assertTrue(result)
        # prefix 매칭 항목이 default 로 설정돼야 한다
        self.assertEqual(parameters[0]["default"], "210-20260601_080000")

    # (5) 콘솔 추출 tag 가 이미 choices 에 있으면 중복 prepend 하지 않음
    def test_existing_tag_in_choices_not_duplicated(self):
        """추출한 tag 가 이미 choices 에 있으면 choices 에 중복 추가하지 않는다."""
        tag = "210-20260602_153100"
        console = self._make_console(210)
        run = self._make_run(build_number=210, console_output=console)
        parameters = [{"name": "APP_IMAGE", "choices": [tag, "209-old"], "default": ""}]
        services.resolve_and_apply_app_image(parameters, run)
        # choices 에 tag 가 정확히 1개만 있어야 한다
        self.assertEqual(parameters[0]["choices"].count(tag), 1)

    # (6) build_number 없는 run + console 없음 → False
    def test_no_build_number_returns_false(self):
        """image_build_number 가 None 이고 콘솔도 없으면 False."""
        run = self._make_run(build_number=None, console_output="")
        parameters = [{"name": "APP_IMAGE", "choices": [], "default": ""}]
        result = services.resolve_and_apply_app_image(parameters, run)
        self.assertFalse(result)


# =============================================
# deploy_params / prod_deploy_params 통합 테스트
# (JenkinsClient mock + 콘솔 추출 → app_image_matched=True)
# =============================================

class DeployParamsConsoleExtractionIntegrationTests(TestCase):
    """deploy_params / prod_deploy_params 뷰가 콘솔에서 tag 를 추출해
    app_image_matched=True + default=추출태그 를 반환하는지 통합 검증.

    JenkinsClient.get_job_info 를 mock 해 APP_IMAGE choices=[] 를 반환하고,
    run.image_console_output 에 실제 콘솔 형식 샘플을 세팅한다.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="u_int", password="pw", role="qpe_ops")
        self.gate = _make_gate()
        self.client = Client()
        self.client.force_login(self.user)

    def _make_console(self, build_number, repo="jobko/auth-admin-api"):
        tag = f"{build_number}-20260602_153100"
        ref = f"905418085337.dkr.ecr.ap-northeast-2.amazonaws.com/{repo}:{tag}"
        return f"[INFO] jib --target={ref}\n[INFO] BUILD SUCCESS\nFinished: SUCCESS\n"

    @mock.patch("apps.qualitygates.views.JenkinsClient")
    def test_deploy_params_console_extraction_returns_matched_true_and_tag(self, mock_jc):
        """deploy_params: choices=[] 이지만 콘솔에 tag 있으면 app_image_matched=True, default=tag."""
        mock_jc.return_value.get_job_info.return_value = {
            "parameters": [
                {"name": "APP_IMAGE", "choices": [], "default": ""},
            ]
        }
        console = self._make_console(210)
        run = QualityGateRun.objects.create(
            gate=self.gate,
            status=QualityGateRun.Status.WAITING_DEPLOY,
            triggered_by=self.user,
            seq_no=1,
            image_build_number=210,
            image_console_output=console,
        )
        response = self.client.get(f"/qualitygates/runs/{run.pk}/deploy/params/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("app_image_matched"), msg=f"app_image_matched 가 False: {data}")
        # APP_IMAGE default 가 추출 태그로 세팅됐는지 확인
        app_image_param = next(
            (p for p in data.get("parameters", []) if p["name"] == "APP_IMAGE"), None
        )
        self.assertIsNotNone(app_image_param)
        self.assertEqual(app_image_param["default"], "210-20260602_153100")
        self.assertIn("210-20260602_153100", app_image_param["choices"])

    @mock.patch("apps.qualitygates.views.JenkinsClient")
    def test_prod_deploy_params_console_extraction_returns_matched_true_and_tag(self, mock_jc):
        """prod_deploy_params: choices=[] 이지만 콘솔에 tag 있으면 app_image_matched=True, default=tag."""
        mock_jc.return_value.get_job_info.return_value = {
            "parameters": [
                {"name": "APP_IMAGE", "choices": [], "default": ""},
            ]
        }
        self.gate.prod_job_path = "folder/prod"
        self.gate.save(update_fields=["prod_job_path"])
        console = self._make_console(210)
        run = QualityGateRun.objects.create(
            gate=self.gate,
            status=QualityGateRun.Status.QG_PASSED,
            triggered_by=self.user,
            seq_no=1,
            image_build_number=210,
            image_console_output=console,
        )
        response = self.client.get(f"/qualitygates/runs/{run.pk}/prod-deploy/params/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("app_image_matched"), msg=f"app_image_matched 가 False: {data}")
        app_image_param = next(
            (p for p in data.get("parameters", []) if p["name"] == "APP_IMAGE"), None
        )
        self.assertIsNotNone(app_image_param)
        self.assertEqual(app_image_param["default"], "210-20260602_153100")
        self.assertIn("210-20260602_153100", app_image_param["choices"])

    @mock.patch("apps.qualitygates.views.JenkinsClient")
    def test_deploy_params_no_console_no_choices_returns_matched_false(self, mock_jc):
        """deploy_params: choices=[] 이고 콘솔도 없으면 app_image_matched=False."""
        mock_jc.return_value.get_job_info.return_value = {
            "parameters": [
                {"name": "APP_IMAGE", "choices": [], "default": ""},
            ]
        }
        run = QualityGateRun.objects.create(
            gate=self.gate,
            status=QualityGateRun.Status.WAITING_DEPLOY,
            triggered_by=self.user,
            seq_no=2,
            image_build_number=210,
            image_console_output="",
        )
        response = self.client.get(f"/qualitygates/runs/{run.pk}/deploy/params/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data.get("app_image_matched"))

    @mock.patch("apps.qualitygates.views.JenkinsClient")
    def test_deploy_params_no_app_image_param_returns_matched_true(self, mock_jc):
        """deploy_params: APP_IMAGE 파라미터 자체가 없으면 app_image_matched=True (무관)."""
        mock_jc.return_value.get_job_info.return_value = {
            "parameters": [
                {"name": "BRANCH", "choices": ["main", "develop"], "default": "main"},
            ]
        }
        run = QualityGateRun.objects.create(
            gate=self.gate,
            status=QualityGateRun.Status.WAITING_DEPLOY,
            triggered_by=self.user,
            seq_no=3,
        )
        response = self.client.get(f"/qualitygates/runs/{run.pk}/deploy/params/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("app_image_matched"))


# =============================================
# QG 재시도 (retry_count) 테스트
# =============================================

def _make_qg_test_run(gate_run, gate_suite, order=0, attempt=1, retry_limit=None):
    """QualityGateRunTest 1개 생성 헬퍼.

    retry_limit 은 프로덕션(create_run)과 동일하게 suite.retry_count 를 스냅샷한다.
    명시값을 주면 그대로 — 진행 중 suite 설정을 바꿔도 Run 한도가 고정됨을 검증할 때 쓴다.
    """
    from apps.qualitygates.models import QualityGateRunTest
    if retry_limit is None:
        retry_limit = max(1, gate_suite.test_suite.retry_count)
    return QualityGateRunTest.objects.create(
        gate_run=gate_run,
        gate_suite=gate_suite,
        order=order,
        status=QualityGateRunTest.Status.TESTING,
        attempt=attempt,
        retry_limit=retry_limit,
    )


def _make_job_run_with_build(test_run, suite, job, build_number=1):
    """test_run 에 QUEUED TestSuiteJobRun 을 연결하고 suite_run 도 생성하는 헬퍼."""
    from apps.suites.models import TestSuiteRun, TestSuiteJobRun
    from django.utils import timezone

    suite_run = TestSuiteRun.objects.create(
        suite=suite,
        status=TestSuiteRun.Status.RUNNING,
        total_jobs=1,
        retry_limit=max(1, suite.retry_count),
        started_at=timezone.now(),
    )
    job_run = TestSuiteJobRun.objects.create(
        suite_run=suite_run,
        job=job,
        status=TestSuiteJobRun.Status.QUEUED,
        jenkins_build_number=build_number,
    )
    test_run.job_run = job_run
    test_run.save(update_fields=["job_run"])
    return suite_run, job_run


class PollOneTestBuildRetryTests(TestCase):
    """`_poll_one_test_build` — 재시도 로직 단위 검증.

    `apps.qualitygates.views.JenkinsClient` 를 mock 해 실제 Jenkins 호출 차단.
    `apps.qualitygates.views.send_event_async` 도 mock 해 Slack 차단.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="qg_retry_u", password="pw", role="qpe_ops")
        self.gate = _make_gate()
        self.gate_run = QualityGateRun.objects.create(
            gate=self.gate, triggered_by=self.user, seq_no=10,
            status=QualityGateRun.Status.TESTING,
        )
        # TestSuite 는 retry_count=2 (최대 2번 실행 = 1회 재시도)
        from apps.suites.models import TestSuite, TestSuiteJob
        self.suite = TestSuite.objects.create(
            name="QGRetrySuite", jenkins_folder_path="qg/p", retry_count=2)
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="qg/p/j1", order=0)
        from apps.qualitygates.models import QualityGateTestSuite
        self.gate_suite = QualityGateTestSuite.objects.create(
            gate=self.gate, test_suite=self.suite, order=0)

    def _fake_jenkins(self, result):
        """결과가 고정된 단순 fake Jenkins."""
        m = mock.MagicMock()
        m.get_build_info.return_value = {
            "building": False, "result": result,
            "url": "http://j/1", "duration": 500,
        }
        m.get_build_stages.return_value = []
        m.get_console_output.return_value = "console"
        return m

    # ------------------------------------------------------------------
    # (A) FAILURE + attempt < retry_count → PENDING, return False
    # ------------------------------------------------------------------
    def test_failure_below_limit_sets_pending_and_returns_false(self):
        """attempt=1, retry_count=2 → FAILURE 시 PENDING 으로 되돌리고 False 반환."""
        from apps.qualitygates.views import _poll_one_test_build
        from apps.qualitygates.models import QualityGateRunTest

        test_run = _make_qg_test_run(self.gate_run, self.gate_suite, attempt=1)
        _make_job_run_with_build(test_run, self.suite, self.job)

        with (
            mock.patch("apps.qualitygates.views.JenkinsClient",
                       return_value=self._fake_jenkins("FAILURE")),
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            result = _poll_one_test_build(test_run)

        self.assertFalse(result)
        test_run.refresh_from_db()
        self.assertEqual(test_run.status, QualityGateRunTest.Status.PENDING)
        self.assertEqual(test_run.attempt, 2)
        self.assertIsNone(test_run.job_run)

    # ------------------------------------------------------------------
    # (B) FAILURE + attempt >= retry_count → FAILED, return True
    # ------------------------------------------------------------------
    def test_failure_at_limit_sets_failed_and_returns_true(self):
        """attempt=2, retry_count=2 → FAILURE 시 FAILED 확정, return True."""
        from apps.qualitygates.views import _poll_one_test_build
        from apps.qualitygates.models import QualityGateRunTest

        test_run = _make_qg_test_run(self.gate_run, self.gate_suite, attempt=2)
        _make_job_run_with_build(test_run, self.suite, self.job)

        with (
            mock.patch("apps.qualitygates.views.JenkinsClient",
                       return_value=self._fake_jenkins("FAILURE")),
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            result = _poll_one_test_build(test_run)

        self.assertTrue(result)
        test_run.refresh_from_db()
        self.assertEqual(test_run.status, QualityGateRunTest.Status.FAILED)

    # ------------------------------------------------------------------
    # (C) SUCCESS → PASSED, return True (재시도 없음)
    # ------------------------------------------------------------------
    def test_success_sets_passed_and_returns_true(self):
        """SUCCESS 빌드 → PASSED, return True."""
        from apps.qualitygates.views import _poll_one_test_build
        from apps.qualitygates.models import QualityGateRunTest

        test_run = _make_qg_test_run(self.gate_run, self.gate_suite, attempt=1)
        _make_job_run_with_build(test_run, self.suite, self.job)

        with (
            mock.patch("apps.qualitygates.views.JenkinsClient",
                       return_value=self._fake_jenkins("SUCCESS")),
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            result = _poll_one_test_build(test_run)

        self.assertTrue(result)
        test_run.refresh_from_db()
        self.assertEqual(test_run.status, QualityGateRunTest.Status.PASSED)

    # ------------------------------------------------------------------
    # (D) 스냅샷 우선: 진행 중 suite.retry_count 를 바꿔도 Run 한도 불변
    # ------------------------------------------------------------------
    def test_snapshot_overrides_lowered_live_retry_count(self):
        """Run 시작 후 suite.retry_count 를 2→1 로 낮춰도, attempt=1 FAILURE 는
        스냅샷(retry_limit=2)을 따라 여전히 재시도(PENDING)된다."""
        from apps.qualitygates.views import _poll_one_test_build
        from apps.qualitygates.models import QualityGateRunTest

        test_run = _make_qg_test_run(
            self.gate_run, self.gate_suite, attempt=1, retry_limit=2)
        _make_job_run_with_build(test_run, self.suite, self.job)
        # 운영자가 진행 중 한도를 낮춤 — 이 Run 엔 영향 없어야 한다.
        self.suite.retry_count = 1
        self.suite.save(update_fields=["retry_count"])

        with (
            mock.patch("apps.qualitygates.views.JenkinsClient",
                       return_value=self._fake_jenkins("FAILURE")),
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            result = _poll_one_test_build(test_run)

        self.assertFalse(result)
        test_run.refresh_from_db()
        self.assertEqual(test_run.status, QualityGateRunTest.Status.PENDING)
        self.assertEqual(test_run.attempt, 2)

    def test_snapshot_overrides_raised_live_retry_count(self):
        """Run 시작 후 suite.retry_count 를 1→5 로 올려도, attempt=1 FAILURE 는
        스냅샷(retry_limit=1)을 따라 재시도 없이 FAILED 확정된다."""
        from apps.qualitygates.views import _poll_one_test_build
        from apps.qualitygates.models import QualityGateRunTest

        test_run = _make_qg_test_run(
            self.gate_run, self.gate_suite, attempt=1, retry_limit=1)
        _make_job_run_with_build(test_run, self.suite, self.job)
        # 운영자가 진행 중 한도를 올림 — 이 Run 엔 영향 없어야 한다.
        self.suite.retry_count = 5
        self.suite.save(update_fields=["retry_count"])

        with (
            mock.patch("apps.qualitygates.views.JenkinsClient",
                       return_value=self._fake_jenkins("FAILURE")),
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            result = _poll_one_test_build(test_run)

        self.assertTrue(result)
        test_run.refresh_from_db()
        self.assertEqual(test_run.status, QualityGateRunTest.Status.FAILED)


class QGSequentialRetryTests(TestCase):
    """순차 모드 QG — Suite FAIL→PASS(retry_count=2) 시 run 이 FAILED 로 가지 않는지."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="qg_seq_u", password="pw", role="qpe_ops")
        from apps.suites.models import TestSuite, TestSuiteJob
        from apps.qualitygates.models import QualityGateTestSuite

        self.gate = _make_gate()
        self.suite = TestSuite.objects.create(
            name="SeqRetrySuite", jenkins_folder_path="seq/p", retry_count=2)
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="seq/p/j1", order=0)
        self.gate_suite = QualityGateTestSuite.objects.create(
            gate=self.gate, test_suite=self.suite, order=0)

    def _fake_jenkins_with_results(self, results):
        """call 횟수에 따라 순서대로 결과를 돌려주는 fake."""
        call_state = {"count": 0}
        m = mock.MagicMock()

        def _get_build_info(path, build_number):
            idx = min(call_state["count"], len(results) - 1)
            call_state["count"] += 1
            return {
                "building": False, "result": results[idx],
                "url": "http://j/1", "duration": 500,
            }

        m.get_build_info.side_effect = _get_build_info
        m.get_build_stages.return_value = []
        m.get_console_output.return_value = "console"
        m.is_job_building.return_value = False
        m.trigger_build.return_value = {"build_number": 1}
        return m

    def test_fail_then_pass_does_not_fail_run(self):
        """순차 모드: 1차 FAILURE(재시도) → PENDING → 재트리거 후에도 run FAILED 안 됨."""
        from apps.qualitygates.views import _poll_test_sequential
        from apps.qualitygates.models import QualityGateRunTest

        gate_run = QualityGateRun.objects.create(
            gate=self.gate, triggered_by=self.user, seq_no=20,
            status=QualityGateRun.Status.TESTING,
            current_suite_index=0,
        )
        test_run = _make_qg_test_run(gate_run, self.gate_suite, attempt=1)
        _make_job_run_with_build(test_run, self.suite, self.job)

        fake = self._fake_jenkins_with_results(["FAILURE", "SUCCESS"])
        with (
            mock.patch("apps.qualitygates.views.JenkinsClient", return_value=fake),
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            # 1차 폴링: FAILURE, attempt=1 < retry_count=2 → PENDING → 재트리거(TESTING)
            _poll_test_sequential(gate_run)
            gate_run.refresh_from_db()
            test_run.refresh_from_db()
            # run 이 FAILED 가 아님을 확인 (재시도 트리거 후 TESTING 또는 WAITING_TEST)
            self.assertNotEqual(gate_run.status, QualityGateRun.Status.FAILED)
            # attempt 가 2 로 올라감
            self.assertEqual(test_run.attempt, 2)

    def test_exhausted_retries_fails_run(self):
        """순차 모드: retry 소진(attempt=2) FAILURE → run FAILED."""
        from apps.qualitygates.views import _poll_test_sequential
        from apps.qualitygates.models import QualityGateRunTest

        gate_run = QualityGateRun.objects.create(
            gate=self.gate, triggered_by=self.user, seq_no=21,
            status=QualityGateRun.Status.TESTING,
            current_suite_index=0,
        )
        test_run = _make_qg_test_run(gate_run, self.gate_suite, attempt=2)
        _make_job_run_with_build(test_run, self.suite, self.job)

        fake = self._fake_jenkins_with_results(["FAILURE"])
        with (
            mock.patch("apps.qualitygates.views.JenkinsClient", return_value=fake),
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            _poll_test_sequential(gate_run)
            gate_run.refresh_from_db()
            test_run.refresh_from_db()

        self.assertEqual(test_run.status, QualityGateRunTest.Status.FAILED)
        self.assertEqual(gate_run.status, QualityGateRun.Status.FAILED)


class QGParallelRetryTests(TestCase):
    """병렬 모드 QG — 한 Suite 재시도가 다른 Suite 진행을 막지 않고, 소진 시 FAILED."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="qg_par_u", password="pw", role="qpe_ops")
        from apps.suites.models import TestSuite, TestSuiteJob
        from apps.qualitygates.models import QualityGateTestSuite

        # parallel_test=True 게이트
        folder = QualityGateFolder.objects.create(
            name=f"par-fold-{id(self)}",
            dev_jenkins_url="https://j.example.com",
            dev_jenkins_user="u",
            dev_jenkins_token="t",
        )
        self.gate = QualityGate.objects.create(
            folder=folder,
            name=f"par-gate-{id(self)}",
            gate_type=QualityGate.GateType.DEV_STG,
            deploy_job_path="folder/deploy",
            parallel_test=True,
        )
        # Suite A: retry_count=2
        self.suite_a = TestSuite.objects.create(
            name="ParSuiteA", jenkins_folder_path="par/a", retry_count=2)
        self.job_a = TestSuiteJob.objects.create(
            suite=self.suite_a, job_name="a1", job_full_path="par/a/a1", order=0)
        self.gate_suite_a = QualityGateTestSuite.objects.create(
            gate=self.gate, test_suite=self.suite_a, order=0)

    def test_failure_below_limit_in_parallel_does_not_fail_run(self):
        """병렬 모드: attempt=1, retry_count=2 → FAILURE 시 run FAILED 안 됨."""
        from apps.qualitygates.views import _poll_tests_parallel
        from apps.qualitygates.models import QualityGateRunTest

        gate_run = QualityGateRun.objects.create(
            gate=self.gate, triggered_by=self.user, seq_no=30,
            status=QualityGateRun.Status.TESTING,
        )
        test_run = _make_qg_test_run(gate_run, self.gate_suite_a, attempt=1)
        _make_job_run_with_build(test_run, self.suite_a, self.job_a)

        fake = mock.MagicMock()
        fake.get_build_info.return_value = {
            "building": False, "result": "FAILURE",
            "url": "http://j/1", "duration": 500,
        }
        fake.get_build_stages.return_value = []
        fake.get_console_output.return_value = "console"
        fake.is_job_building.return_value = False
        fake.trigger_build.return_value = {"build_number": 2}

        with (
            mock.patch("apps.qualitygates.views.JenkinsClient", return_value=fake),
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            _poll_tests_parallel(gate_run, self.gate)
            gate_run.refresh_from_db()
            test_run.refresh_from_db()

        self.assertNotEqual(gate_run.status, QualityGateRun.Status.FAILED)
        self.assertEqual(test_run.attempt, 2)

    def test_exhausted_retries_in_parallel_fails_run(self):
        """병렬 모드: retry 소진 FAILURE → run FAILED."""
        from apps.qualitygates.views import _poll_tests_parallel
        from apps.qualitygates.models import QualityGateRunTest

        gate_run = QualityGateRun.objects.create(
            gate=self.gate, triggered_by=self.user, seq_no=31,
            status=QualityGateRun.Status.TESTING,
        )
        # attempt=2 == retry_count=2 → 소진
        test_run = _make_qg_test_run(gate_run, self.gate_suite_a, attempt=2)
        _make_job_run_with_build(test_run, self.suite_a, self.job_a)

        fake = mock.MagicMock()
        fake.get_build_info.return_value = {
            "building": False, "result": "FAILURE",
            "url": "http://j/1", "duration": 500,
        }
        fake.get_build_stages.return_value = []
        fake.get_console_output.return_value = "console"
        fake.is_job_building.return_value = False

        with (
            mock.patch("apps.qualitygates.views.JenkinsClient", return_value=fake),
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            _poll_tests_parallel(gate_run, self.gate)
            gate_run.refresh_from_db()
            test_run.refresh_from_db()

        self.assertEqual(test_run.status, QualityGateRunTest.Status.FAILED)
        self.assertEqual(gate_run.status, QualityGateRun.Status.FAILED)


class RerunTestAttemptResetTests(TestCase):
    """`services.rerun_test` 가 attempt 를 1 로 리셋하는지."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="qg_rerun_u", password="pw", role="qpe_ops")
        from apps.suites.models import TestSuite, TestSuiteJob
        from apps.qualitygates.models import QualityGateTestSuite

        self.gate = _make_gate()
        self.gate.prod_job_path = ""
        self.gate.save()
        self.suite = TestSuite.objects.create(
            name="RerunSuite", jenkins_folder_path="rr/p", retry_count=3)
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="rr/p/j1", order=0)
        self.gate_suite = QualityGateTestSuite.objects.create(
            gate=self.gate, test_suite=self.suite, order=0)

    def test_rerun_test_resets_attempt_to_1(self):
        """rerun_test 후 비-PASSED test_run 의 attempt 가 1 로 초기화된다."""
        from apps.qualitygates.models import QualityGateRunTest

        gate_run = QualityGateRun.objects.create(
            gate=self.gate, triggered_by=self.user, seq_no=40,
            status=QualityGateRun.Status.FAILED,
            deploy_completed_at=timezone.now(),
        )
        test_run = QualityGateRunTest.objects.create(
            gate_run=gate_run,
            gate_suite=self.gate_suite,
            order=0,
            status=QualityGateRunTest.Status.FAILED,
            attempt=3,  # 재시도 소진 상태
        )

        with (
            mock.patch("apps.qualitygates.views.JenkinsClient") as mock_jc,
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            # _advance_test → _try_trigger_test → JenkinsClient
            mock_jc.return_value.is_job_building.return_value = False
            mock_jc.return_value.trigger_build.return_value = {"build_number": 5}
            services.rerun_test(run=gate_run, user=self.user)

        test_run.refresh_from_db()
        # attempt 가 1 로 리셋됐는지 확인
        self.assertEqual(test_run.attempt, 1)


# =============================================
# seq_no 회귀: _try_trigger_test 경로
# =============================================

class TryTriggerTestSeqNoTests(TestCase):
    """`_try_trigger_test` 가 생성하는 TestSuiteRun 에 seq_no >= 1 이 부여되는지."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="qg_seq_u", password="pw", role="qpe_ops")
        from apps.suites.models import TestSuite, TestSuiteJob
        from apps.qualitygates.models import QualityGateTestSuite

        self.gate = _make_gate()
        self.gate.prod_job_path = ""
        self.gate.save()
        self.suite = TestSuite.objects.create(
            name="TriggerSeqSuite", jenkins_folder_path="ts/p", retry_count=1)
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="ts/p/j1", order=0)
        self.gate_suite = QualityGateTestSuite.objects.create(
            gate=self.gate, test_suite=self.suite, order=0)

    def test_try_trigger_test_assigns_seq_no_gte_1(self):
        """`_try_trigger_test` 에서 생성된 TestSuiteRun.seq_no >= 1."""
        from apps.qualitygates.models import QualityGateRunTest
        from apps.suites.models import TestSuiteRun

        gate_run = QualityGateRun.objects.create(
            gate=self.gate, triggered_by=self.user, seq_no=50,
            status=QualityGateRun.Status.FAILED,
            deploy_completed_at=timezone.now(),
        )
        test_run = QualityGateRunTest.objects.create(
            gate_run=gate_run,
            gate_suite=self.gate_suite,
            order=0,
            status=QualityGateRunTest.Status.WAITING,
            retry_limit=1,
        )

        with (
            mock.patch("apps.qualitygates.views.JenkinsClient") as mock_jc,
            mock.patch("apps.qualitygates.views.send_event_async"),
        ):
            mock_jc.return_value.is_job_building.return_value = False
            mock_jc.return_value.trigger_build.return_value = {"build_number": 9}
            services.rerun_test(run=gate_run, user=self.user)

        test_run.refresh_from_db()
        # rerun_test → _advance_test → _try_trigger_test 경로를 타서
        # TestSuiteRun 이 1개 생성돼 있어야 한다.
        suite_run = TestSuiteRun.objects.filter(suite=self.suite).first()
        self.assertIsNotNone(suite_run, "_try_trigger_test 가 TestSuiteRun 을 생성해야 한다")
        self.assertGreaterEqual(suite_run.seq_no, 1,
            f"seq_no 가 1 이상이어야 하지만 {suite_run.seq_no}")

    def test_try_trigger_test_seq_no_increments_across_runs(self):
        """두 번째 `_try_trigger_test` 호출에서 seq_no=2 가 부여된다."""
        from apps.qualitygates.models import QualityGateRunTest
        from apps.suites.models import TestSuiteRun

        def _make_gate_run_and_rerun(seq):
            gate_run = QualityGateRun.objects.create(
                gate=self.gate, triggered_by=self.user, seq_no=seq,
                status=QualityGateRun.Status.FAILED,
                deploy_completed_at=timezone.now(),
            )
            QualityGateRunTest.objects.create(
                gate_run=gate_run,
                gate_suite=self.gate_suite,
                order=0,
                status=QualityGateRunTest.Status.WAITING,
                retry_limit=1,
            )
            with (
                mock.patch("apps.qualitygates.views.JenkinsClient") as mock_jc,
                mock.patch("apps.qualitygates.views.send_event_async"),
            ):
                mock_jc.return_value.is_job_building.return_value = False
                mock_jc.return_value.trigger_build.return_value = {"build_number": 9}
                services.rerun_test(run=gate_run, user=self.user)

        _make_gate_run_and_rerun(51)
        _make_gate_run_and_rerun(52)

        suite_runs = list(TestSuiteRun.objects.filter(suite=self.suite).order_by("seq_no"))
        self.assertEqual(len(suite_runs), 2)
        self.assertEqual(suite_runs[0].seq_no, 1)
        self.assertEqual(suite_runs[1].seq_no, 2)
