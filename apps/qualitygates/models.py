import uuid
from django.conf import settings
from django.db import models

from apps.integrations.argocd import classify_argocd_status


class QualityGateFolder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True, verbose_name="인스턴스명")
    description = models.CharField(max_length=500, blank=True, verbose_name="설명")

    # 검수대상(Dev) Jenkins
    dev_jenkins_url = models.URLField(max_length=500, blank=True, verbose_name="Dev Jenkins URL")
    dev_jenkins_user = models.CharField(max_length=200, blank=True, verbose_name="Dev Jenkins 계정 정보")
    dev_jenkins_token = models.CharField(max_length=200, blank=True, verbose_name="Dev Jenkins API Token")

    # 검수통과(Prod) Jenkins
    prod_jenkins_url = models.URLField(max_length=500, blank=True, verbose_name="Prod Jenkins URL")
    prod_jenkins_user = models.CharField(max_length=200, blank=True, verbose_name="Prod Jenkins 계정 정보")
    prod_jenkins_token = models.CharField(max_length=200, blank=True, verbose_name="Prod Jenkins API Token")

    # Argo CD (선택) — 배포 Job 완료 후 Argo CD 의 최종 sync 상태까지 확인하고 넘어간다.
    # 환경별(DEV/STG/PRD) 토큰을 따로 저장한다.
    dev_argocd_token = models.CharField(max_length=500, blank=True, default="", verbose_name="Argo CD Dev Token")
    stg_argocd_token = models.CharField(max_length=500, blank=True, default="", verbose_name="Argo CD Stg Token")
    prd_argocd_token = models.CharField(max_length=500, blank=True, default="", verbose_name="Argo CD Prd Token")

    def argocd_token_for(self, env: str) -> str:
        env = (env or "").lower()
        if env == "stg":
            return self.stg_argocd_token
        if env == "prd":
            return self.prd_argocd_token
        return self.dev_argocd_token

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "qualitygates_folder"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def gate_count(self):
        return self.gates.filter(is_active=True).count()

    @property
    def subfolder_count(self):
        return self.subfolders.count()


class QualityGateSubFolder(models.Model):
    """인스턴스(QualityGateFolder) 안에서 Gate 들을 묶는 하위 분류 폴더."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    folder = models.ForeignKey(
        QualityGateFolder, on_delete=models.CASCADE, related_name="subfolders",
        verbose_name="인스턴스",
    )
    name = models.CharField(max_length=200, verbose_name="하위폴더명")
    description = models.CharField(max_length=500, blank=True, verbose_name="설명")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "qualitygates_subfolder"
        ordering = ["name"]
        unique_together = [("folder", "name")]

    def __str__(self):
        return f"{self.folder.name} / {self.name}"

    @property
    def gate_count(self):
        return self.gates.filter(is_active=True).count()


class QualityGate(models.Model):
    class CredentialSource(models.TextChoices):
        DEV = "dev", "Dev"
        PROD = "prod", "Prod"

    class ArgoEnv(models.TextChoices):
        DEV = "dev", "Dev"
        STG = "stg", "Stg"
        PRD = "prd", "Prd"

    class GateType(models.TextChoices):
        DEV_STG = "dev_stg", "Dev → Stg"
        STG_PRD = "stg_prd", "Stg → Prd"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    folder = models.ForeignKey(
        QualityGateFolder, on_delete=models.CASCADE, related_name="gates",
        null=True, blank=True,
    )
    subfolder = models.ForeignKey(
        QualityGateSubFolder, on_delete=models.CASCADE, related_name="gates",
        null=True, blank=True,
        verbose_name="하위폴더",
    )
    name = models.CharField(max_length=200, verbose_name="게이트명")
    gate_type = models.CharField(
        max_length=10, choices=GateType.choices, default=GateType.DEV_STG,
        verbose_name="게이트 배포 구성",
    )
    # Dev → Stg 타입에서만 의미 있음. True 면 WAITING_DEPLOY / QG_PASSED 를 거치지 않고
    # 이미지 빌드 성공 → 배포, 테스트 성공 → 스테이징 배포까지 전부 기본 파라미터로 자동 진행.
    auto_mode = models.BooleanField(default=False, verbose_name="자동 진행")
    # True 면 모든 Test Suite 를 동시에 트리거하고, 모두 완료될 때까지 대기 후 다음 단계로.
    parallel_test = models.BooleanField(default=False, verbose_name="테스트 병렬 실행")

    # 이미지 생성 Jenkins (선택) — deploy 그룹 설정을 공유 (같은 base URL + credential)
    image_job_path = models.CharField(max_length=500, blank=True, verbose_name="이미지 생성 Job 경로")

    # 검증대상 배포 Job
    deploy_job_path = models.CharField(max_length=500, verbose_name="검증 배포 Job 경로")
    # 입력한 Jenkins Base URL(프로토콜+호스트). 비어 있으면 folder 의 credential 쪽 URL 을 사용.
    deploy_jenkins_base_url = models.CharField(max_length=500, blank=True, default="", verbose_name="배포 Jenkins Base URL")
    deploy_credential = models.CharField(
        max_length=10, choices=CredentialSource.choices,
        default=CredentialSource.DEV,
        verbose_name="배포 Job 인증키",
    )
    # Argo CD 앱 URL. 비어 있으면 argo 체크 건너뜀.
    deploy_argocd_app_url = models.CharField(
        max_length=500, blank=True, default="", verbose_name="검수대상 Argo CD App URL",
    )
    deploy_argocd_env = models.CharField(
        max_length=4, choices=ArgoEnv.choices, default=ArgoEnv.DEV,
        verbose_name="검수대상 Argo CD 환경",
    )

    # 검수통과 배포 Job (선택)
    prod_job_path = models.CharField(max_length=500, blank=True, verbose_name="검수통과 배포 Job 경로")
    prod_jenkins_base_url = models.CharField(max_length=500, blank=True, default="", verbose_name="Prod Jenkins Base URL")
    prod_credential = models.CharField(
        max_length=10, choices=CredentialSource.choices,
        default=CredentialSource.PROD,
        verbose_name="검수통과 Job 인증키",
    )
    prod_argocd_app_url = models.CharField(
        max_length=500, blank=True, default="", verbose_name="검수통과 Argo CD App URL",
    )
    prod_argocd_env = models.CharField(
        max_length=4, choices=ArgoEnv.choices, default=ArgoEnv.PRD,
        verbose_name="검수통과 Argo CD 환경",
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "qualitygates_gate"
        ordering = ["name"]

    def __str__(self):
        return self.name

    # --- 폴더 경유 자격증명 ---
    def _folder_url_for(self, credential: str) -> str:
        if not self.folder:
            return ""
        return self.folder.prod_jenkins_url if credential == self.CredentialSource.PROD else self.folder.dev_jenkins_url

    def _folder_user_for(self, credential: str) -> str:
        if not self.folder:
            return ""
        return self.folder.prod_jenkins_user if credential == self.CredentialSource.PROD else self.folder.dev_jenkins_user

    def _folder_token_for(self, credential: str) -> str:
        if not self.folder:
            return ""
        return self.folder.prod_jenkins_token if credential == self.CredentialSource.PROD else self.folder.dev_jenkins_token

    @property
    def deploy_jenkins_url(self):
        # 사용자가 입력한 Base URL 이 있으면 그걸 우선. 없을 때만 folder 쪽 URL 로 폴백.
        return self.deploy_jenkins_base_url or self._folder_url_for(self.deploy_credential)

    @property
    def deploy_jenkins_user(self):
        return self._folder_user_for(self.deploy_credential)

    @property
    def deploy_jenkins_token(self):
        return self._folder_token_for(self.deploy_credential)

    @property
    def prod_jenkins_url(self):
        return self.prod_jenkins_base_url or self._folder_url_for(self.prod_credential)

    @property
    def prod_jenkins_user(self):
        return self._folder_user_for(self.prod_credential)

    @property
    def prod_jenkins_token(self):
        return self._folder_token_for(self.prod_credential)

    @property
    def has_prod_deploy(self):
        return bool(self.prod_job_path)

    @property
    def has_image_build(self):
        return bool(self.image_job_path)

    @property
    def has_deploy_argocd(self):
        return bool(self.deploy_argocd_app_url) and bool(
            self.folder and self.folder.argocd_token_for(self.deploy_argocd_env)
        )

    @property
    def has_prod_argocd(self):
        return bool(self.prod_argocd_app_url) and bool(
            self.folder and self.folder.argocd_token_for(self.prod_argocd_env)
        )

    @staticmethod
    def _build_job_url(base_url: str, job_path: str) -> str:
        if not base_url or not job_path:
            return ""
        parts = [p for p in job_path.strip("/").split("/") if p]
        return base_url.rstrip("/") + "/job/" + "/job/".join(parts) + "/"

    @property
    def deploy_job_full_url(self):
        return self._build_job_url(self.deploy_jenkins_url, self.deploy_job_path)

    @property
    def image_job_full_url(self):
        return self._build_job_url(self.deploy_jenkins_url, self.image_job_path)

    @property
    def prod_job_full_url(self):
        return self._build_job_url(self.prod_jenkins_url, self.prod_job_path)

    @property
    def latest_run(self):
        return self.runs.order_by("-created_at").first()

    @property
    def test_suites_display(self):
        return ", ".join(
            gts.test_suite.name for gts in self.gate_suites.order_by("order")
        )


class QualityGateTestSuite(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gate = models.ForeignKey(QualityGate, on_delete=models.CASCADE, related_name="gate_suites")
    test_suite = models.ForeignKey(
        "suites.TestSuite", on_delete=models.CASCADE, related_name="quality_gate_links",
    )
    order = models.IntegerField(default=0)

    class Meta:
        db_table = "qualitygates_gate_suite"
        ordering = ["order"]
        unique_together = ["gate", "test_suite"]

    def __str__(self):
        return f"{self.gate.name} → {self.test_suite.name} (#{self.order + 1})"


class QualityGateRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        BUILDING_IMAGE = "building_image", "Building Image"
        IMAGE_BUILD_FAILED = "image_build_failed", "Image Build Failed"
        WAITING_DEPLOY = "waiting_deploy", "Awaiting Target Deploy"
        DEPLOYING = "deploying", "Deploying"
        DEPLOY_FAILED = "deploy_failed", "Deploy Failed"
        WAITING_TEST = "waiting_test", "Waiting for Test"
        TESTING = "testing", "Testing"
        QG_PASSED = "qg_passed", "QG Passed (Awaiting Prod Deploy)"
        FAILED = "failed", "Failed"
        PROD_DEPLOYING = "prod_deploying", "Prod Deploying"
        PROD_DEPLOY_FAILED = "prod_deploy_failed", "Prod Deploy Failed"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    # 새 Run 이 시작되면 자동으로 취소 처리되는, 사용자 개입 대기 상태.
    WAITING_USER_STATUSES = ("waiting_deploy", "qg_passed")
    # 내부 파이프라인이 능동적으로 진행 중인 상태 — 이 사이에는 새 Run 을 못 누르게 차단.
    ACTIVE_STATUSES = (
        "pending", "building_image", "deploying",
        "waiting_test", "testing", "prod_deploying",
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gate = models.ForeignKey(QualityGate, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)

    # 이미지 생성 정보 (선택)
    image_build_number = models.IntegerField(null=True, blank=True)
    image_build_url = models.URLField(max_length=500, blank=True)
    image_duration_ms = models.IntegerField(null=True, blank=True)
    image_stages_json = models.JSONField(default=list, blank=True)
    image_current_stage = models.CharField(max_length=200, blank=True)
    image_console_output = models.TextField(blank=True, default="", verbose_name="이미지 생성 콘솔 로그")
    image_started_at = models.DateTimeField(null=True, blank=True)
    image_completed_at = models.DateTimeField(null=True, blank=True)
    image_params_json = models.JSONField(default=dict, blank=True)
    image_git_branch = models.CharField(max_length=200, blank=True, default="")
    image_git_commit = models.CharField(max_length=40, blank=True, default="")

    # 검증 배포 정보
    deploy_build_number = models.IntegerField(null=True, blank=True)
    deploy_build_url = models.URLField(max_length=500, blank=True)
    deploy_duration_ms = models.IntegerField(null=True, blank=True)
    deploy_stages_json = models.JSONField(default=list, blank=True)
    deploy_current_stage = models.CharField(max_length=200, blank=True)
    deploy_console_output = models.TextField(blank=True, default="", verbose_name="검증 배포 콘솔 로그")
    deploy_started_at = models.DateTimeField(null=True, blank=True)
    deploy_completed_at = models.DateTimeField(null=True, blank=True)
    # Argo CD sync 상태 (검수대상 단계)
    deploy_argocd_sync_status = models.CharField(max_length=30, blank=True, default="")
    deploy_argocd_health_status = models.CharField(max_length=30, blank=True, default="")
    deploy_argocd_synced_at = models.DateTimeField(null=True, blank=True)
    deploy_argocd_checked_at = models.DateTimeField(null=True, blank=True)
    deploy_argocd_sync_requested_at = models.DateTimeField(null=True, blank=True)
    deploy_argocd_operation_phase = models.CharField(max_length=30, blank=True, default="")
    deploy_argocd_operation_message = models.TextField(blank=True, default="")
    deploy_argocd_sync_error = models.TextField(blank=True, default="")
    # Healthy 가 처음 잡힌 시각 — grace period 측정용. health 가 다시 떨어지면 None 으로 reset.
    deploy_argocd_healthy_first_at = models.DateTimeField(null=True, blank=True)

    # 검수통과 배포 정보
    prod_build_number = models.IntegerField(null=True, blank=True)
    prod_build_url = models.URLField(max_length=500, blank=True)
    prod_duration_ms = models.IntegerField(null=True, blank=True)
    prod_stages_json = models.JSONField(default=list, blank=True)
    prod_current_stage = models.CharField(max_length=200, blank=True)
    prod_console_output = models.TextField(blank=True, default="", verbose_name="검수통과 배포 콘솔 로그")
    prod_started_at = models.DateTimeField(null=True, blank=True)
    prod_completed_at = models.DateTimeField(null=True, blank=True)
    prod_argocd_sync_status = models.CharField(max_length=30, blank=True, default="")
    prod_argocd_health_status = models.CharField(max_length=30, blank=True, default="")
    prod_argocd_synced_at = models.DateTimeField(null=True, blank=True)
    prod_argocd_checked_at = models.DateTimeField(null=True, blank=True)
    prod_argocd_sync_requested_at = models.DateTimeField(null=True, blank=True)
    prod_argocd_operation_phase = models.CharField(max_length=30, blank=True, default="")
    prod_argocd_operation_message = models.TextField(blank=True, default="")
    prod_argocd_sync_error = models.TextField(blank=True, default="")
    prod_argocd_healthy_first_at = models.DateTimeField(null=True, blank=True)

    # 테스트 진행
    current_suite_index = models.IntegerField(default=0)
    total_suites = models.IntegerField(default=0)

    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # 게이트별 1부터 순증가하는 일련번호. 같은 application 의 여러 Run 상세 탭을 동시에
    # 띄울 때 어떤 게 최신인지 즉시 식별하기 위한 표시용 번호 — 시간 외 별도 키.
    seq_no = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "qualitygates_run"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["gate", "seq_no"],
                condition=models.Q(seq_no__gt=0),
                name="qualitygates_run_gate_seq_no_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.gate.name} - {self.get_status_display()}"

    @property
    def is_terminal(self):
        return self.status in (
            self.Status.COMPLETED, self.Status.FAILED,
            self.Status.DEPLOY_FAILED, self.Status.PROD_DEPLOY_FAILED,
            self.Status.IMAGE_BUILD_FAILED, self.Status.CANCELLED,
        )

    @property
    def is_cancelled(self):
        return self.status == self.Status.CANCELLED

    @property
    def is_running(self):
        return (
            not self.is_terminal
            and self.status != self.Status.QG_PASSED
            and self.status != self.Status.WAITING_DEPLOY
        )

    @property
    def can_deploy_now(self):
        return self.status == self.Status.WAITING_DEPLOY

    @property
    def can_prod_deploy(self):
        return self.status == self.Status.QG_PASSED and self.gate.has_prod_deploy

    @property
    def can_rerun(self):
        return self.status in (
            self.Status.FAILED, self.Status.PROD_DEPLOY_FAILED,
            self.Status.IMAGE_BUILD_FAILED, self.Status.DEPLOY_FAILED,
        )

    @property
    def can_rerun_test_only(self):
        # 테스트 단계에서 실패한 경우에만 노출 — 이미 배포된 환경을 그대로 재사용.
        return self.status == self.Status.FAILED and self.deploy_completed_at is not None

    @property
    def can_rerun_deploy_only(self):
        # 검수대상 배포 단계에서 실패한 경우에만 노출 — 이미지 빌드 결과는 그대로 재사용.
        # 이미지 빌드 단계가 없는 게이트도 deploy_failed 면 deploy 만 다시 트리거할 수 있다.
        return self.status == self.Status.DEPLOY_FAILED

    @property
    def can_rerun_prod_only(self):
        # 검수통과 배포 단계에서 실패한 경우에만 노출 — 이미지/검증배포/테스트는 그대로 유지.
        return self.status == self.Status.PROD_DEPLOY_FAILED and self.gate.has_prod_deploy

    @property
    def can_cancel(self):
        return not self.is_terminal

    @property
    def is_latest_for_gate(self):
        """이 Run 이 같은 게이트의 가장 최근(seq_no 최대) Run 인지."""
        latest = self.gate.runs.exclude(pk=self.pk).order_by("-seq_no").first()
        return latest is None or self.seq_no >= (latest.seq_no or 0)

    @property
    def newer_run_for_gate(self):
        """이 Run 보다 더 최근에 생성된 같은 게이트의 Run (seq_no 가 더 큰 것). 없으면 None.

        같은 application 의 Run 상세 탭을 여러 개 띄웠을 때, 화면에서 보고 있는 게 latest 가
        아니라는 사실을 배너로 알리기 위함.
        """
        return (
            self.gate.runs.filter(seq_no__gt=self.seq_no)
            .order_by("-seq_no").first()
        )

    @property
    def image_stage_present(self):
        """이 Run 이 실제로 이미지 빌드 단계를 거쳤는지(또는 거치고 있는지).

        게이트 자체는 image_job 을 가지고 있어도 사용자가 'skip_image_build' 옵션으로
        실행했다면 이 Run 에는 이미지 단계가 존재하지 않는다 — 그 경우 단계 카운트나
        step_offset 가 image 단계를 포함하지 않도록 Run 측 흔적으로 판단한다.
        """
        if self.image_started_at is not None:
            return True
        if self.status == self.Status.IMAGE_BUILD_FAILED:
            return True
        if self.image_build_number is not None:
            return True
        return False

    @property
    def is_success(self):
        return self.status in (self.Status.COMPLETED, self.Status.QG_PASSED)

    @property
    def status_display(self):
        """gate_type 에 따라 deploy/prod 단계의 환경명을 동적으로 매핑.

        enum 의 display name 은 `Prod Deploying` / `Deploy Failed` 같은 generic
        라벨이라 DEV_STG 게이트 (deploy=Dev, prod=Stg) 에서는 실제 환경과 어긋난다.
        예: DEV_STG 의 `PROD_DEPLOYING` 은 실제로 Stg 배포 중인데 화면에 "Prod
        Deploying" 으로 노출돼 혼란. 매핑:

        | gate_type | DEPLOY_* (검수대상) | PROD_* (검수통과) |
        |-----------|-----------------|------------------|
        | DEV_STG   | Dev             | Stg              |
        | STG_PRD   | Stg             | Prd              |
        """
        is_dev_stg = self.gate.gate_type == QualityGate.GateType.DEV_STG
        deploy_env = "Dev" if is_dev_stg else "Stg"
        prod_env = "Stg" if is_dev_stg else "Prd"

        env_labels = {
            self.Status.WAITING_DEPLOY: f"Awaiting {deploy_env} Deploy",
            self.Status.DEPLOYING: f"{deploy_env} Deploying",
            self.Status.DEPLOY_FAILED: f"{deploy_env} Deploy Failed",
            self.Status.QG_PASSED: f"QG Passed (Awaiting {prod_env} Deploy)",
            self.Status.PROD_DEPLOYING: f"{prod_env} Deploying",
            self.Status.PROD_DEPLOY_FAILED: f"{prod_env} Deploy Failed",
        }
        if self.status in env_labels:
            return env_labels[self.status]
        return self.get_status_display()

    @property
    def is_failed(self):
        return self.status in (
            self.Status.FAILED, self.Status.DEPLOY_FAILED,
            self.Status.PROD_DEPLOY_FAILED, self.Status.IMAGE_BUILD_FAILED,
        )

    @property
    def deploy_argocd_badge(self):
        """검수대상 배포 Argo CD 상태를 (kind/level/label) 로 분류해 템플릿에 노출."""
        return classify_argocd_status(
            self.deploy_argocd_sync_status,
            self.deploy_argocd_health_status,
            self.deploy_argocd_operation_phase,
            self.deploy_argocd_operation_message,
        )

    @property
    def prod_argocd_badge(self):
        """검수통과 배포 Argo CD 상태 분류."""
        return classify_argocd_status(
            self.prod_argocd_sync_status,
            self.prod_argocd_health_status,
            self.prod_argocd_operation_phase,
            self.prod_argocd_operation_message,
        )

    @property
    def show_deploy_argocd_error(self):
        """`*_argocd_sync_error` 는 최초 sync 트리거가 400/409(이미 동기화 중)을 받아도 채워져
        — 폴링 중에 빨간 글씨로 노출되면 사용자가 실제 실패로 오해한다. 최종 실패 상태일 때만
        노출하도록 좁힌다 (timeout/Failed/Error 시 sync_error 가 명확한 메시지로 덮어써짐)."""
        return bool(self.deploy_argocd_sync_error) and self.status == self.Status.DEPLOY_FAILED

    @property
    def show_prod_argocd_error(self):
        return bool(self.prod_argocd_sync_error) and self.status == self.Status.PROD_DEPLOY_FAILED

    @property
    def result_label(self):
        if self.status == self.Status.COMPLETED:
            return "PASS"
        elif self.status == self.Status.QG_PASSED:
            return "QG PASS"
        elif self.status == self.Status.CANCELLED:
            return "CANCELLED"
        elif self.status in (
            self.Status.FAILED, self.Status.DEPLOY_FAILED,
            self.Status.PROD_DEPLOY_FAILED, self.Status.IMAGE_BUILD_FAILED,
        ):
            return "FAIL"
        return None


class QualityGateRunTest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        WAITING = "waiting", "Waiting for Idle Job"
        TESTING = "testing", "Testing"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gate_run = models.ForeignKey(QualityGateRun, on_delete=models.CASCADE, related_name="test_runs")
    gate_suite = models.ForeignKey(QualityGateTestSuite, on_delete=models.CASCADE)
    order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    job_run = models.ForeignKey(
        "suites.TestSuiteJobRun", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="qg_test_runs",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "qualitygates_run_test"
        ordering = ["order"]

    def __str__(self):
        return f"{self.gate_suite.test_suite.name} - {self.status}"
