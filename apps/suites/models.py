import uuid
from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Max, Q
from django.utils import timezone


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
    # 재시도 횟수 = 잡(worker) 1개의 '최대 실행 횟수'. 1=재시도 없음(1번 실패하면 오류),
    # 3=실패 시 최대 3번까지 실행하고 3번 모두 실패해야 오류로 판정. 통과한 잡은 재실행 안 함.
    # 단독 Suite Run / Quality Gate / Reliability Test 모든 실행 경로에 적용된다.
    retry_count = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name="재시도 횟수",
        help_text="잡 1개의 최대 실행 횟수. 1=재시도 없음, 3=실패 시 최대 3번까지 실행.",
    )
    # 잡 트리거 throttle — 이 suite 의 '마지막 잡 트리거 시각'. 잡을 Jenkins 로
    # 트리거하기 직전, 이 값으로부터 잡의 trigger_delay_seconds 가 지났는지 보고
    # 안 지났으면 WAITING 으로 보류한다(apps/suites/throttle.py:claim_trigger_slot).
    # **suite 단위** 라 서로 다른 suite/스케줄은 서로 간섭하지 않고, 한 suite 안에서만
    # (병렬 run 의 잡들 · 동시 idle 호출) 딜레이 간격으로 트리거가 분산된다 — 병렬
    # 실행 시 잡들이 '동시'가 아니라 딜레이만큼 '엇갈려' 시작/완료되게 한다.
    last_job_triggered_at = models.DateTimeField(null=True, blank=True)
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

    # 목록(여러 suite 혼합)에서 # 컬럼 앞 구분자로 쓰는 짧은 식별자.
    # suite 이름이 보통 '<org>-<plat>-regression-api-<env>' 컨벤션이라 공통 토큰
    # (regression/api/test)을 떼어 'am-dweb-prd' 처럼 압축한다. 컨벤션과 다른
    # 이름이면 (떼어낼 토큰이 없거나 dash 가 없으면) 거의 원본 그대로 — graceful.
    _SHORT_LABEL_GENERIC = {"regression", "api", "test"}

    @property
    def short_label(self) -> str:
        parts = [p for p in self.name.split("-") if p.strip()]
        kept = [p for p in parts if p.lower() not in self._SHORT_LABEL_GENERIC]
        return "-".join(kept) if kept else self.name

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
    # 이 잡을 Jenkins 로 트리거하기 직전, 전역 '마지막 잡 트리거 시각' 으로부터 이
    # 시간(초) 이 지나야 실제로 트리거한다. 그 전이면 잡을 WAITING 으로 보류하고
    # 다음 폴링/sweep 에서 재시도. 동시에 여러 API 를 쏠 때 Jenkins 가 꼬이는 이슈를
    # 막기 위한 잡별 순차 throttle. 0 = 지연 없음. 단독 실행/Reliability Test(병렬
    # 포함) 전 경로에 적용 — single source of truth 는 apps/suites/throttle.py.
    trigger_delay_seconds = models.PositiveIntegerField(
        default=30,
        verbose_name="실행 지연 시간(초)",
        help_text="직전에 트리거된 잡으로부터 이 시간이 지나야 실행. 0=지연 없음. 기본 30초.",
    )
    # 내부 Jenkins 가 Keycloak SSO 뒤라 UserAPIToken 으로 트리거가 안 돼서, 이 워커(잡)는
    # Jenkins Generic Webhook Trigger(GWT) 토큰으로 트리거한다. 워커마다 '고유' 토큰이어야
    # sequential(특정 워커만 트리거)이 된다 — 같은 토큰을 여러 워커에 걸면 invoke 1번에 다
    # 같이 돈다. 이 값을 Jenkins 워커의 GWT Token 과 동일하게 넣어야 한다. (외부 Jenkins
    # suite 는 사용 안 함 — 기존 user:api_token Basic auth 로 트리거)
    gwt_token = models.CharField(
        max_length=128, blank=True, default="",
        verbose_name="GWT 토큰",
        help_text="이 워커를 트리거할 Generic Webhook Trigger 토큰(워커별 고유). Jenkins 워커의 GWT Token 과 동일하게 등록.",
    )

    @staticmethod
    def gen_gwt_token() -> str:
        """워커별 고유 GWT 토큰 값 생성(추측 어려운 랜덤)."""
        import secrets
        return "qgwt_" + secrets.token_hex(20)

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
    # Run 시작 시점의 suite.retry_count 스냅샷 — 진행 중 suite 설정을 바꿔도 이 Run 에는
    # 영향 없게 한다. _poll_run_status 의 잡별 재시도 판정이 이 값을 읽는다.
    retry_limit = models.PositiveIntegerField(default=1)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    # suite 별 1부터 순증가하는 표시용 일련번호. 같은 suite 의 여러 Run 을 동시에
    # 비교할 때 어떤 게 최신인지 즉시 식별하기 위한 번호 — 시간 외 별도 식별 키.
    seq_no = models.PositiveIntegerField(default=0)
    # 실패 알림 Slack 메시지 참조 — 버튼(Failure/Skip) 클릭 시 chat.update 로 이 메시지를
    # 갱신(처리됨 표시)하기 위해 발송 시 ts/channel 을 저장한다.
    slack_message_ts = models.CharField(max_length=32, blank=True, default="")
    slack_channel_id = models.CharField(max_length=32, blank=True, default="")
    # 스케줄 실패 알림의 '스케줄 중단/재활성화' 버튼 상태(메시지별) — "" / "stopped" / "reactivated".
    slack_schedule_action = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        db_table = "suites_test_suite_run"
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["suite", "seq_no"],
                condition=Q(seq_no__gt=0),
                name="suites_test_suite_run_suite_seq_no_uniq",
            ),
        ]

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

    @classmethod
    def annotate_triage(cls, qs):
        """queryset 에 `real_failed`(= failed_jobs − Skip 처리 − ABORTED 실패 Job 수) 주석 추가.

        대시보드/목록 집계에서 verdict=skip(자동화 판정 오류)과 **미분류
        ABORTED(중단)** 를 실패(장애)로 세지 않기 위해 사용 — 중단은 사용자/
        스케줄 정지·취소의 결과라 기본적으로 장애가 아니다. 단 **사람이 Slack/
        포털 버튼으로 verdict=failure 를 명시 확정한 ABORTED 는 장애로 인정**해
        빼지 않는다(Run 상세의 '🔴 FAILURE — 실제 장애로 분류' 배지와 집계가
        모순되지 않게). 저장된 `failed_jobs`(FAILURE+ABORTED 합산) 카운트에서
        해당 Job 수만 빼므로, job_run 레코드가 없는 레거시 run 은 기존처럼
        failed_jobs 그대로 실패로 남는다. run 단위 `triage_result` 도 이 주석이
        있으면 재사용.
        """
        from django.db.models import Count, F, Q
        return qs.annotate(
            _skipped_failed=Count(
                "job_runs",
                filter=Q(job_runs__status__in=["failure", "aborted"])
                & Q(job_runs__verdict="skip"),
                distinct=True,
            ),
            _aborted_untriaged=Count(
                "job_runs",
                filter=Q(job_runs__status="aborted")
                & Q(job_runs__verdict=""),
                distinct=True,
            ),
        ).annotate(
            real_failed=F("failed_jobs") - F("_skipped_failed") - F("_aborted_untriaged"),
        )

    @property
    def triage_result(self):
        """triage(verdict)·중단 반영 결과 — 완료 run 은 'pass'|'fail'|'skip'|'aborted', 그 외 status.

        - pass: 실패 Job 없음
        - fail: 실장애 Job 존재 — 미분류/Failure 확정 FAILURE, 또는 사람이
          Failure 로 명시 확정한 ABORTED
        - skip: 실장애는 없고 Skip 분류된 실패 Job 이 있음 (자동화 판정 오류 의심)
        - aborted: 실패 Job 이 전부 미분류 ABORTED (중단 — 장애로 보지 않음)
        """
        if self.status != self.Status.COMPLETED:
            return self.status
        if (self.failed_jobs or 0) == 0:
            return "pass"
        real = getattr(self, "real_failed", None)
        skipped = getattr(self, "_skipped_failed", None)
        if real is None or skipped is None:
            skipped = (self.job_runs
                       .filter(status__in=["failure", "aborted"], verdict="skip").count())
            aborted = (self.job_runs
                       .filter(status="aborted", verdict="").count())
            real = (self.failed_jobs or 0) - skipped - aborted
        if real > 0:
            return "fail"
        return "skip" if skipped > 0 else "aborted"

    @staticmethod
    def next_seq_no(suite) -> int:
        """해당 suite 의 다음 seq_no 를 반환한다 (Max + 1).

        호출부는 반드시 transaction.atomic() 으로 감싸야 한다 — aggregate/create
        사이에 경쟁이 없도록. QualityGateRun 쪽(apps/qualitygates/services.py:109-113)
        과 동일한 패턴.
        """
        return (
            TestSuiteRun.objects.filter(suite=suite)
            .aggregate(m=Max("seq_no"))["m"] or 0
        ) + 1


class TestSuiteJobRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        # WAITING = 실행 지연 throttle 로 보류된 상태. PENDING 과 마찬가지로 '아직
        # 트리거되지 않음' 이지만, 전역 마지막 트리거 후 잡의 trigger_delay_seconds
        # 가 지나지 않아 다음 폴링/sweep 으로 미뤄둔 상태. 딜레이가 지나면 QUEUED 로
        # 전이. (PENDING 과 분리해 화면에서 '지연 대기' 로 구분 표시)
        WAITING = "waiting", "Waiting"
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILURE = "failure", "Failure"
        ABORTED = "aborted", "Aborted"

    class Verdict(models.TextChoices):
        # 실패 알림 Slack 버튼(Failure/Skip)으로 확정하는 '실패의 성격'.
        UNTRIAGED = "", "미분류"
        FAILURE = "failure", "Failure (실장애)"   # 실제 서비스 장애 — F/U 필요
        SKIP = "skip", "Skip (자동화 판정 오류 의심)"        # 테스트 코드/환경 문제로 넘김

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    suite_run = models.ForeignKey(
        TestSuiteRun, on_delete=models.CASCADE, related_name="job_runs",
    )
    job = models.ForeignKey(TestSuiteJob, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # 이 잡의 현재 시도 회차 (1-based). retry_limit 까지 재시도되며, FAILURE 가 나도
    # attempt < suite_run.retry_limit 이면 PENDING 으로 되돌려 다시 트리거한다.
    attempt = models.PositiveIntegerField(default=1)
    jenkins_build_number = models.IntegerField(null=True, blank=True)
    jenkins_build_url = models.URLField(max_length=500, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    current_stage = models.CharField(max_length=200, blank=True, verbose_name="현재 스테이지")
    stages_json = models.JSONField(default=list, blank=True, verbose_name="스테이지 목록")
    console_output = models.TextField(blank=True, default="", verbose_name="Jenkins 콘솔 로그")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # 실패 분류(triage) — Slack Failure/Skip 버튼 또는 포털에서 확정. 덮어쓰기 허용(최신값).
    verdict = models.CharField(
        max_length=10, choices=Verdict.choices, blank=True, default="",
        verbose_name="실패 분류",
    )
    triaged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="triaged_job_runs", verbose_name="분류자",
    )
    triaged_at = models.DateTimeField(null=True, blank=True, verbose_name="분류 시각")

    class Meta:
        db_table = "suites_test_suite_job_run"
        ordering = ["job__order", "job__job_name"]

    def __str__(self):
        return f"{self.job.job_name} - {self.status}"

    @property
    def is_failure_status(self):
        return self.status in (self.Status.FAILURE, self.Status.ABORTED)

    @property
    def result_label(self):
        """화면/알림 표시용 결과 — verdict 반영. 실패건은 분류에 따라 Failure/Skip/Fail(미분류).

        미분류 ABORTED 는 'Fail(판정 대기)' 가 아니라 'Aborted' — 중단은 장애가
        아니고 판정 큐(untriaged_pending) 대상도 아니므로 분류 대기처럼 보이면 안 됨.
        """
        if self.status == self.Status.SUCCESS:
            return "Pass"
        if self.is_failure_status:
            if self.verdict == self.Verdict.FAILURE:
                return "Failure"
            if self.verdict == self.Verdict.SKIP:
                return "Skip"
            if self.status == self.Status.ABORTED:
                return "Aborted"
            return "Fail"
        return self.get_status_display()

    @property
    def duration_seconds(self):
        if self.duration_ms:
            return round(self.duration_ms / 1000, 1)
        return None

    @classmethod
    def untriaged_pending(cls):
        """'미 장애 판정' 대기 큐 — 아직 verdict 미확정인 **실패(FAILURE)** Job Run.

        ABORTED(중단)는 장애가 아니므로 판정 대상에서 제외한다 — 스케줄 정지·
        수동 취소로 중단된 Job 이 판정 큐에 쌓이지 않게.

        `settings.UNTRIAGED_QUEUE_SINCE`(컷오프) **이후** 발생한 것만 포함한다
        (기능 도입 이전 과거 대량 미분류를 판정 대상에서 제외). 발생 시각은
        완료→시작→상위 run 시작 순으로 Coalesce. 최신순 정렬.
        """
        from django.conf import settings
        from django.db.models.functions import Coalesce
        from django.utils.dateparse import parse_datetime

        cutoff = parse_datetime(settings.UNTRIAGED_QUEUE_SINCE)
        qs = (cls.objects
              .filter(status=cls.Status.FAILURE, verdict="")
              .annotate(_evt=Coalesce("completed_at", "started_at", "suite_run__started_at")))
        if cutoff is not None:
            qs = qs.filter(_evt__gte=cutoff)
        return qs.order_by("-_evt")

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


# 잡 트리거 throttle 의 상태(마지막 트리거 시각)는 ``TestSuite.last_job_triggered_at``
# (suite 단위) 에 둔다. 과거의 전역 singleton ``JobTriggerThrottle`` 은 서로 다른
# 스케줄/suite 의 잡까지 한 슬롯으로 묶어 병렬 실행이 서로 굶주리고(starve) interval
# 타임아웃으로 실패하는 문제가 있어 제거했다. 판정/갱신 로직 single source of truth 는
# ``apps/suites/throttle.py:claim_trigger_slot(suite, delay_seconds)``.


# =====================================================================
# UI Test Automation (TAP 연동) — docs/prd_ui_test_automation.md
# =====================================================================

class TapSuite(models.Model):
    """TAP(tapilot) 의 Test Suite 미러 — 매분 cron 이 GET /api/v2/suites/ 로 싱크.

    포털은 이 미러를 목록으로 보여주고, 실행은 tap_id 로 TAP API 를 호출한다.
    TAP 쪽에서 사라진 Suite 는 삭제하지 않고 is_removed=True 로 표시(실행 이력 보존).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tap_id = models.IntegerField(unique=True, verbose_name="TAP Suite ID")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    owner_display = models.CharField(max_length=100, blank=True, default="")
    project_id = models.IntegerField(null=True, blank=True)
    project_name = models.CharField(max_length=200, blank=True, default="")
    milestone_name = models.CharField(max_length=100, blank=True, default="")
    case_count = models.IntegerField(default=0)
    raw_json = models.JSONField(default=dict, blank=True)  # TAP 응답 원본 (필드 추가 대비)
    # 재시도 횟수 = 최대 실행 횟수 (API Suite 의 retry_count 와 동일 의미).
    # 1=재시도 없음, 3=완료 결과에 실패 케이스가 있으면 전체를 최대 3번까지 재실행.
    # 재시도 대상은 completed+failed>0 만 — error(타임아웃)/stopped(취소)는 재시도 안 함
    # (Jenkins 경로의 '순수 FAILURE 만 재시도' 정책 승계). 포털 로컬 설정 — TAP 싱크가 덮어쓰지 않음.
    retry_count = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name="재시도 횟수",
        help_text="최대 실행 횟수. 1=재시도 없음, 3=실패 시 최대 3번까지 실행.",
    )
    is_removed = models.BooleanField(default=False, verbose_name="TAP 에서 제거됨")
    synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "suites_tap_suite"
        ordering = ["name"]

    def __str__(self):
        return f"[TAP] {self.name}"

    @property
    def latest_run(self):
        return self.runs.order_by("-started_at").first()


class TapRun(models.Model):
    """TAP Suite 1회 실행 기록 — TAP session 을 미러링.

    진행바는 Jenkins stage 가 아니라 **케이스 수 기준**(completed_cases/total_cases).
    상태 매핑(포털 표기): queued/pending→대기, running→실행 중,
    completed→PASS/FAIL(failed>0), stopped→ABORTED(장애 아님 정책 승계), error→FAIL.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        STOPPED = "stopped", "Stopped"
        ERROR = "error", "Error"

    ACTIVE_STATUSES = ("queued", "pending", "running")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tap_suite = models.ForeignKey(TapSuite, on_delete=models.CASCADE, related_name="runs")
    session_id = models.IntegerField(unique=True, verbose_name="TAP 세션 ID")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    run_env = models.CharField(max_length=20, blank=True, default="")
    total_cases = models.IntegerField(default=0)
    completed_cases = models.IntegerField(default=0)
    passed = models.IntegerField(default=0)
    failed = models.IntegerField(default=0)
    others = models.IntegerField(default=0)  # skipped/stopped/error 등
    excluded_case_ids = models.JSONField(default=list, blank=True)  # 자동화 스크립트 없어 제외
    tap_url = models.URLField(max_length=500, blank=True, default="")
    testrail_run_id = models.IntegerField(null=True, blank=True)
    raw_cases_json = models.JSONField(default=list, blank=True)  # 종결 시 케이스별 결과 저장
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    # 이 run 의 시도 회차 (1-based). retry_count 까지 자동 재시도되며, 재시도 run 은
    # retry_origin 으로 이전 시도를 가리킨다. 통계/최종 판정은 최종 시도만 사용.
    attempt = models.PositiveIntegerField(default=1)
    retry_origin = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="retries", verbose_name="재시도 원본 run",
    )
    # Reliability(UI Automation) 스케줄이 트리거한 run 이면 그 스케줄 — 세션 통계
    # (RobustnessSchedule._tap_aggregate) 의 집계 기준. 수동 실행은 None.
    robustness_schedule = models.ForeignKey(
        "robustness.RobustnessSchedule", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="tap_runs",
    )
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "suites_tap_run"
        ordering = ["-started_at"]

    def __str__(self):
        return f"[TAP] {self.tap_suite.name} #{self.session_id}"

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def progress_percent(self):
        if not self.total_cases:
            return 0
        return min(100, round(self.completed_cases / self.total_cases * 100))

    @property
    def result_label(self):
        """포털 표기 결과 — 기존 Suite Run 배지와 동일한 시각 언어."""
        if self.status in (self.Status.QUEUED, self.Status.PENDING):
            return "대기"
        if self.status == self.Status.RUNNING:
            return "실행 중"
        if self.status == self.Status.STOPPED:
            return "Aborted"  # 중단 — 장애로 보지 않음 (전역 정책)
        if self.status == self.Status.ERROR:
            return "Fail"
        return "Fail" if self.failed > 0 else "Pass"
