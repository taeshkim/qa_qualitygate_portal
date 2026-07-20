import uuid
from django.conf import settings
from django.db import models


class SuiteJenkinsCredential(models.Model):
    """Test Suite 가 사용하는 Jenkins 자격증명(내부/외부) 중앙 저장소.

    - internal: `JenkinsClient()` 가 (env 변수보다 우선) 사용하는 전용 Jenkins.
    - external: 새 Suite 등록 시 폼에 미리 채워줄 default 외부 Jenkins.
    """

    class Kind(models.TextChoices):
        INTERNAL = "internal", "내부 Jenkins"
        EXTERNAL = "external", "외부 Jenkins (default)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20, choices=Kind.choices, unique=True)
    base_url = models.CharField(max_length=500, blank=True, default="")
    user = models.CharField(max_length=200, blank=True, default="")
    api_token = models.CharField(max_length=500, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )

    class Meta:
        db_table = "suites_jenkins_credential"

    def __str__(self):
        return f"{self.get_kind_display()} ({self.base_url or '-'})"

    @property
    def has_token(self) -> bool:
        return bool(self.api_token)

    @property
    def token_masked(self) -> str:
        """저장된 token 의 마스킹 표시. 평문은 절대 노출하지 않는다."""
        if not self.api_token:
            return ""
        prefix = self.api_token[:4]
        return f"{prefix}{'*' * 8}"


class TestSuite(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, verbose_name="스위트명")
    jenkins_folder_path = models.CharField(
        max_length=500, verbose_name="Jenkins 폴더 경로",
        help_text="예: 100_jk_regression_test_group/PC/dev_jk_regression_api_test",
    )
    jenkins_folder_url = models.URLField(max_length=500, blank=True)
    # 외부 Jenkins 연결 정보. 비어있으면 기본(전용) Jenkins 환경변수를 사용한다.
    jenkins_base_url = models.CharField(
        max_length=500, blank=True, default="",
        verbose_name="Jenkins Base URL",
        help_text="외부 Jenkins를 사용할 경우의 서버 URL. 비우면 전용 Jenkins 사용.",
    )
    jenkins_user = models.CharField(max_length=200, blank=True, default="", verbose_name="Jenkins User")
    jenkins_api_token = models.CharField(max_length=500, blank=True, default="", verbose_name="Jenkins API Token")
    service = models.ForeignKey(
        "services.Service", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="test_suites",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "suites_test_suite"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["jenkins_base_url", "jenkins_folder_path"],
                name="uniq_suite_base_url_folder_path",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_external(self) -> bool:
        return bool(self.jenkins_base_url)

    def get_jenkins_client(self):
        """Suite에 저장된 외부 자격증명이 있으면 그 설정으로, 없으면 기본 Jenkins Client."""
        from apps.integrations.jenkins import JenkinsClient
        if self.is_external:
            return JenkinsClient(
                base_url=self.jenkins_base_url,
                user=self.jenkins_user,
                api_token=self.jenkins_api_token,
            )
        return JenkinsClient()

    @property
    def job_count(self):
        return self.jobs.filter(is_active=True).count()

    @property
    def latest_run(self):
        return self.runs.order_by("-started_at").first()

    @property
    def overall_pass_rate(self):
        """전체 완료된 실행 이력 기준 Pass Rate."""
        completed_runs = self.runs.filter(status="completed")
        total = sum(r.total_jobs for r in completed_runs)
        passed = sum(r.passed_jobs for r in completed_runs)
        if total == 0:
            return 0
        return round(passed / total * 100, 1)

    @property
    def overall_stats(self):
        """전체 완료 이력의 passed/total."""
        completed_runs = self.runs.filter(status="completed")
        total = sum(r.total_jobs for r in completed_runs)
        passed = sum(r.passed_jobs for r in completed_runs)
        return {"passed": passed, "total": total}


class TestSuiteJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    suite = models.ForeignKey(TestSuite, on_delete=models.CASCADE, related_name="jobs")
    job_name = models.CharField(max_length=200, verbose_name="Job 이름")
    job_full_path = models.CharField(max_length=500, verbose_name="Jenkins Full Path")
    total_stages = models.IntegerField(default=0, verbose_name="전체 스테이지 수")
    is_target = models.BooleanField(default=True, verbose_name="실행 대상")
    is_active = models.BooleanField(default=True)
    order = models.IntegerField(default=0)

    class Meta:
        db_table = "suites_test_suite_job"
        ordering = ["order", "job_name"]

    def __str__(self):
        return self.job_name


class TestSuiteRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    suite = models.ForeignKey(TestSuite, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    total_jobs = models.IntegerField(default=0)
    passed_jobs = models.IntegerField(default=0)
    failed_jobs = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )

    class Meta:
        db_table = "suites_test_suite_run"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.suite.name} - {self.status} ({self.started_at})"

    @property
    def pass_rate(self):
        if self.total_jobs == 0:
            return 0
        return round(self.passed_jobs / self.total_jobs * 100, 1)

    @property
    def completed_jobs(self):
        return self.passed_jobs + self.failed_jobs

    @property
    def progress_percent(self):
        if self.total_jobs == 0:
            return 0
        return round(self.completed_jobs / self.total_jobs * 100, 1)

    @property
    def passed_percent(self):
        if self.total_jobs == 0:
            return 0
        return round(self.passed_jobs / self.total_jobs * 100, 1)

    @property
    def failed_percent(self):
        if self.total_jobs == 0:
            return 0
        return round(self.failed_jobs / self.total_jobs * 100, 1)


class TestSuiteJobRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILURE = "failure", "Failure"
        ABORTED = "aborted", "Aborted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    suite_run = models.ForeignKey(
        TestSuiteRun, on_delete=models.CASCADE, related_name="job_runs",
    )
    job = models.ForeignKey(TestSuiteJob, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    jenkins_build_number = models.IntegerField(null=True, blank=True)
    jenkins_build_url = models.URLField(max_length=500, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    current_stage = models.CharField(max_length=200, blank=True, verbose_name="현재 스테이지")
    stages_json = models.JSONField(default=list, blank=True, verbose_name="스테이지 목록")
    console_output = models.TextField(blank=True, default="", verbose_name="Jenkins 콘솔 로그")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "suites_test_suite_job_run"
        ordering = ["job__order", "job__job_name"]

    def __str__(self):
        return f"{self.job.job_name} - {self.status}"

    @property
    def duration_seconds(self):
        if self.duration_ms:
            return round(self.duration_ms / 1000, 1)
        return None

    @property
    def display_stages_json(self):
        """화면 표시용 stages — Jenkins 가 NOT_EXECUTED 로 보고한 stage(예: when{...}
        조건 미충족으로 skip) 는 제외해 사용자 화면의 dot 게이지와 카운트가 '실제 실행된
        stage 수' 기준이 되게 한다. 7 SUCCESS + 2 NOT_EXECUTED 빌드는 7/7 로 표시.
        """
        return [s for s in (self.stages_json or [])
                if s.get("status") != "NOT_EXECUTED"]

    @property
    def stages_total(self):
        """실제 실행된 전체 스테이지 수 (NOT_EXECUTED 제외)."""
        return len(self.display_stages_json)

    @property
    def stages_done(self):
        """완료(SUCCESS/FAILED) 스테이지 수."""
        return sum(1 for s in self.display_stages_json
                   if s.get("status") in ("SUCCESS", "FAILED"))

    @property
    def stages_running(self):
        """현재 진행 중인 스테이지 수."""
        return sum(1 for s in self.display_stages_json
                   if s.get("status") == "IN_PROGRESS")

    @property
    def stages_current_number(self):
        """현재 몇 번째 스테이지인지 (1-based). 완료 + 진행중."""
        return self.stages_done + self.stages_running
