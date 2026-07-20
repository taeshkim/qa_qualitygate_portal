import uuid

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class RobustnessTest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, verbose_name="Robustness Test 이름")
    description = models.TextField(blank=True, default="", verbose_name="설명")
    is_active = models.BooleanField(default=True)
    parallel_jobs = models.BooleanField(
        default=False, verbose_name="잡 병렬 실행",
        help_text="ON 이면 한 번 트리거 시 Suite 의 모든 잡을 동시에 실행. OFF 면 한 잡씩 순차 실행. 이 테스트의 모든 스케줄에 적용.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )

    class Meta:
        db_table = "robustness_test"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def schedule_count(self):
        return self.schedules.count()

    @property
    def running_count(self):
        return self.schedules.filter(is_running=True).count()


class RobustnessSchedule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    robustness_test = models.ForeignKey(
        RobustnessTest, on_delete=models.CASCADE, related_name="schedules",
    )
    test_suite = models.ForeignKey(
        "suites.TestSuite", on_delete=models.CASCADE, related_name="robustness_schedules",
    )
    interval_minutes = models.PositiveIntegerField(
        default=60, verbose_name="실행 간격(분)",
        help_text="이 간격마다 자동으로 1회 실행됩니다.",
    )
    is_running = models.BooleanField(default=False, verbose_name="스케줄 실행 중")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="현재 세션 시작 시각")
    stopped_at = models.DateTimeField(null=True, blank=True, verbose_name="마지막 정지 시각")
    last_triggered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "robustness_schedule"
        ordering = ["created_at"]
        unique_together = [("robustness_test", "test_suite")]

    def __str__(self):
        return f"{self.robustness_test.name} → {self.test_suite.name}"

    @property
    def session_runs(self):
        """현재(또는 마지막) 세션의 시작 이후 트리거된 run 만 카운트."""
        if not self.started_at:
            return self.runs.none()
        return self.runs.filter(triggered_at__gte=self.started_at)

    def _job_aggregate(self):
        """현재 세션의 RobustnessRun 들이 가리키는 SuiteRun 으로부터
        잡 단위 합계를 계산한다."""
        agg = self.session_runs.aggregate(
            total=Sum("suite_run__total_jobs"),
            passed=Sum("suite_run__passed_jobs"),
            failed=Sum("suite_run__failed_jobs"),
        )
        return {
            "total": agg["total"] or 0,
            "passed": agg["passed"] or 0,
            "failed": agg["failed"] or 0,
        }

    @property
    def total_executions(self):
        """세션 동안 트리거된 잡 실행 총 수 (Suite Run × jobs/Suite)."""
        return self._job_aggregate()["total"]

    @property
    def success_count(self):
        return self._job_aggregate()["passed"]

    @property
    def failure_count(self):
        return self._job_aggregate()["failed"]

    @property
    def in_flight_count(self):
        agg = self._job_aggregate()
        return max(agg["total"] - agg["passed"] - agg["failed"], 0)

    @property
    def trigger_count(self):
        """세션 동안 자동/수동 트리거가 발생한 횟수 (Suite Run 단위)."""
        return self.session_runs.count()

    @property
    def success_rate(self):
        agg = self._job_aggregate()
        finished = agg["passed"] + agg["failed"]
        if finished == 0:
            return 0
        return round(agg["passed"] / finished * 100, 1)

    @property
    def next_trigger_at(self):
        if not self.is_running:
            return None
        if not self.last_triggered_at:
            return self.started_at or timezone.now()
        return self.last_triggered_at + timezone.timedelta(minutes=self.interval_minutes)

    @property
    def seconds_until_next(self):
        nxt = self.next_trigger_at
        if not nxt:
            return None
        delta = (nxt - timezone.now()).total_seconds()
        return max(int(delta), 0)


class RobustnessRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILURE = "failure", "Failure"
        ABORTED = "aborted", "Aborted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schedule = models.ForeignKey(
        RobustnessSchedule, on_delete=models.CASCADE, related_name="runs",
    )
    suite_run = models.ForeignKey(
        "suites.TestSuiteRun", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="robustness_runs",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )
    triggered_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "robustness_run"
        ordering = ["-triggered_at"]

    def __str__(self):
        return f"{self.schedule} @ {self.triggered_at:%Y-%m-%d %H:%M}"
