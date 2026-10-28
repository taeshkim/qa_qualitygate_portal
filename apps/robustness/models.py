import uuid

from django.conf import settings
from django.db import models
from django.db.models import Count, Sum
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
    single_job_mode = models.BooleanField(
        default=False, verbose_name="단일 테스트 모드",
        help_text="ON 이면 한 번 트리거 시 Suite 의 잡 전체가 아니라 '비어있는(idle)' 잡 1개만 실행한다. ON 시 잡 병렬 실행은 강제 OFF. 이 테스트의 모든 스케줄에 적용.",
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
    # 대상은 둘 중 정확히 하나 — Jenkins Suite(API Test Automation) 또는
    # TAP Suite(UI Test Automation). PRD docs/prd_ui_test_automation.md 확장.
    test_suite = models.ForeignKey(
        "suites.TestSuite", on_delete=models.CASCADE, related_name="robustness_schedules",
        null=True, blank=True,
    )
    tap_suite = models.ForeignKey(
        "suites.TapSuite", on_delete=models.CASCADE, related_name="robustness_schedules",
        null=True, blank=True, verbose_name="TAP Suite (UI Test Automation)",
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
        constraints = [
            # 대상은 Jenkins Suite / TAP Suite 중 정확히 하나.
            models.CheckConstraint(
                name="robustness_schedule_exactly_one_target",
                check=(
                    (models.Q(test_suite__isnull=False) & models.Q(tap_suite__isnull=True))
                    | (models.Q(test_suite__isnull=True) & models.Q(tap_suite__isnull=False))
                ),
            ),
            models.UniqueConstraint(
                fields=["robustness_test", "tap_suite"],
                name="uniq_robustness_test_tap_suite",
            ),
        ]

    def __str__(self):
        return f"{self.robustness_test.name} → {self.target_name}"

    # ── 대상 종류/표시 헬퍼 — Jenkins(API)/TAP(UI) 공용 화면·로그용 ──
    @property
    def is_tap(self) -> bool:
        return self.tap_suite_id is not None

    @property
    def target_name(self) -> str:
        if self.tap_suite_id:
            return self.tap_suite.name
        return self.test_suite.name if self.test_suite_id else "?"

    @property
    def latest_run(self):
        """마지막으로 트리거된 실행 객체 — Jenkins: TestSuiteRun, TAP: TapRun.

        '마지막 실행' 링크(스케줄 표/실행 이력)가 대상 종류에 따라 다른 상세
        화면(suites:run_detail / suites:tap_run_detail)으로 가리키기 위한 헬퍼.
        트리거는 됐지만 suite_run 생성에 실패한 경우 등은 None.
        """
        if self.is_tap:
            return self.tap_runs.order_by("-started_at").first()
        run = self.runs.select_related("suite_run").order_by("-triggered_at").first()
        return run.suite_run if run else None

    @property
    def session_runs(self):
        """현재(또는 마지막) 세션의 시작 이후 트리거된 run 만 카운트."""
        if not self.started_at:
            return self.runs.none()
        return self.runs.filter(triggered_at__gte=self.started_at)

    @property
    def session_tap_runs(self):
        """TAP 스케줄의 세션 run (TapRun.robustness_schedule 역참조)."""
        if not self.started_at:
            return self.tap_runs.none()
        return self.tap_runs.filter(started_at__gte=self.started_at)

    def _tap_aggregate(self):
        """TAP 스케줄 세션 통계 — 케이스 수 기준 (Jenkins 잡 수 집계와 동일한 컬럼 의미).

        - passed/failed: 각 run 의 케이스 합/불 합계
        - in_flight: 진행 중 run 의 남은 케이스 (terminal run 은 0 — 취소/에러로
          안 돈 케이스는 세지 않아 유령 '진행중' 이 남지 않게)
        - stopped(Aborted) run 도 그 시점까지의 passed/failed 는 집계 — 중단
          자체는 실패로 세지 않는다("ABORTED=장애 아님" 정책, Jenkins 경로와 동일).
          interval 타임아웃 마감은 status=error 로 기록돼 실패 신호가 유지된다.
        """
        cached = getattr(self, "_tap_agg_cache", None)
        if cached is not None:
            return cached
        passed = failed = in_flight = triggers = 0
        # 재시도된 run(뒤에 재시도 시도가 있는 run)은 집계에서 제외 — 최종 시도만
        # 반영 (Jenkins 경로의 '중간 시도 실패는 집계 안 함' 과 동일 의미).
        for r in self.session_tap_runs.exclude(retries__isnull=False):
            triggers += 1
            passed += r.passed or 0
            failed += r.failed or 0
            if r.is_active:
                in_flight += max((r.total_cases or 0) - (r.completed_cases or 0), 0)
            elif r.status == r.Status.ERROR:
                # 타임아웃/에러 마감 — 판정 못 받은 잔여 케이스는 실패로 집계(신호 유지)
                failed += max((r.total_cases or 0) - (r.passed or 0) - (r.failed or 0) - (r.others or 0), 0)
        result = {
            "total": passed + failed + in_flight,
            "passed": passed,
            "failed": failed,
            "in_flight": in_flight,
            "triggers": triggers,
        }
        self._tap_agg_cache = result
        return result

    def _job_aggregate(self):
        """현재 세션의 RobustnessRun 들이 가리키는 SuiteRun 으로부터
        잡 단위 합계를 계산한다.

        화면 한 행에서 total_executions/success_count/failure_count/in_flight_count/
        success_rate/trigger_count 가 모두 이 결과를 읽으므로, 인스턴스 단위로
        memoize 해 aggregate 쿼리를 공유한다(행당 6쿼리 → 2쿼리). 스케줄
        인스턴스는 요청마다 새로 만들어지므로 요청 간 stale 우려 없음.

        **ABORTED 잡은 장애가 아니다** — 통계에서 **제외**한다. 스케줄 중지로
        강제 중단됐든 Jenkins 쪽에서 빌드가 취소됐든, 중단은 제품 실패가 아니므로
        성공률/실패수를 깎지 않게 total/failed 양쪽에서 뺀다(중지→재개 시 수치가
        멈췄다 이어지는 semantics + "aborted 는 장애로 보지 않는다" 전역 정책 —
        미장애 판정 큐/대시보드 분포와 동일 기준). 단 사람이 Slack/포털 버튼으로
        verdict=failure 를 명시 확정한 ABORTED 는 장애로 인정해 빼지 않는다.
        interval 타임아웃 마감(_timeout_unfinished_runs)은 FAILURE 마킹이라
        여전히 실패로 집계된다.

        suite_run.failed_jobs 에는 FAILURE+ABORTED 가 합산돼 있어 aborted 를 별도
        카운트해 빼는데, Sum(suite_run 필드)과 job_runs 조인 Count 를 한 aggregate
        에 섞으면 조인 fan-out 으로 Sum 이 부풀므로 쿼리를 둘로 나눈다. 두 쿼리의
        정합은 RobustnessRun↔TestSuiteRun 이 1:1 (_trigger_once 에서만 생성) 이라는
        불변조건에 의존한다.
        """
        from apps.suites.models import TestSuiteJobRun

        cached = getattr(self, "_job_agg_cache", None)
        if cached is not None:
            return cached
        agg = self.session_runs.aggregate(
            total=Sum("suite_run__total_jobs"),
            passed=Sum("suite_run__passed_jobs"),
            failed=Sum("suite_run__failed_jobs"),
            triggers=Count("id"),
        )
        aborted = self.session_runs.aggregate(
            aborted=Count(
                "suite_run__job_runs",
                filter=models.Q(
                    suite_run__job_runs__status=TestSuiteJobRun.Status.ABORTED,
                ) & ~models.Q(suite_run__job_runs__verdict="failure"),
            ),
        )["aborted"] or 0
        result = {
            "total": max((agg["total"] or 0) - aborted, 0),
            "passed": agg["passed"] or 0,
            "failed": max((agg["failed"] or 0) - aborted, 0),
            "triggers": agg["triggers"] or 0,
        }
        self._job_agg_cache = result
        return result

    def _stats(self):
        """대상 종류에 맞는 세션 집계 — Jenkins 는 잡 단위, TAP 은 케이스 단위.

        컬럼 의미는 동일(실행 수/성공/실패/진행중/트리거)해서 같은 테이블에 섞여 나온다.
        """
        return self._tap_aggregate() if self.is_tap else self._job_aggregate()

    @property
    def total_executions(self):
        """세션 실행 총 수 — Jenkins: 잡 수 합, TAP: 케이스 수 합."""
        return self._stats()["total"]

    @property
    def success_count(self):
        return self._stats()["passed"]

    @property
    def failure_count(self):
        return self._stats()["failed"]

    @property
    def in_flight_count(self):
        agg = self._stats()
        if self.is_tap:
            return agg["in_flight"]
        return max(agg["total"] - agg["passed"] - agg["failed"], 0)

    @property
    def trigger_count(self):
        """세션 동안 자동/수동 트리거가 발생한 횟수 (Run 단위)."""
        return self._stats()["triggers"]

    @property
    def success_rate(self):
        agg = self._stats()
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
