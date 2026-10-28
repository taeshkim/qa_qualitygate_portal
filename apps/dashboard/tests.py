"""apps.dashboard 단위/통합 테스트.

커버 시나리오:
  1. 로그인 후 대시보드 index 200 응답 + "최근 Test Suites 실행 내역" 렌더링
  2. recent_suite_runs 컨텍스트에 최근 Run 포함 여부
  3. DORA 문자열이 응답에 없음 (회귀 방지)
  4. metrics 집계 — pass_rate, total_runs, running_count, today_count
  5. dist 집계 — pass/fail/running 카운트
  6. daily 14일 길이 + 빈 날짜 0 채움
  7. 비로그인 → 로그인 페이지 리다이렉트
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJob, TestSuiteJobRun

from apps.dashboard.views import (
    _compute_metrics,
    _compute_daily,
    _compute_result_dist,
    _compute_suite_stats,
)


def _make_user(username="dashops"):
    User = get_user_model()
    return User.objects.create_user(
        username=username, password="pw", role="qpe_ops"
    )


def _make_suite(name="DashSuite", path="d/p"):
    return TestSuite.objects.create(name=name, jenkins_folder_path=path)


_jr_counter = {"n": 0}


def _add_job_run(suite_run, suite, *, status, verdict=""):
    """suite_run 에 Job Run 1건 추가 (job 단위 분포 집계 테스트용)."""
    _jr_counter["n"] += 1
    n = _jr_counter["n"]
    job = TestSuiteJob.objects.create(
        suite=suite, job_name=f"job{n}", job_full_path=f"{suite.jenkins_folder_path}/j{n}")
    return TestSuiteJobRun.objects.create(
        suite_run=suite_run, job=job, status=status, verdict=verdict,
        started_at=suite_run.started_at, completed_at=suite_run.completed_at)


def _completed_run(suite, *, failed_jobs=0, offset_days=0, user=None):
    """completed status 의 TestSuiteRun 생성 헬퍼."""
    now = timezone.now() - timedelta(days=offset_days)
    return TestSuiteRun.objects.create(
        suite=suite,
        status=TestSuiteRun.Status.COMPLETED,
        total_jobs=1,
        passed_jobs=(0 if failed_jobs > 0 else 1),
        failed_jobs=failed_jobs,
        started_at=now,
        completed_at=now + timedelta(seconds=30),
        triggered_by=user,
    )


def _running_run(suite, *, offset_days=0):
    """running status 의 TestSuiteRun 생성 헬퍼."""
    now = timezone.now() - timedelta(days=offset_days)
    return TestSuiteRun.objects.create(
        suite=suite,
        status=TestSuiteRun.Status.RUNNING,
        total_jobs=1,
        started_at=now,
    )


# ---------------------------------------------------------------------------
# 1~3: 뷰 HTTP 응답 / 컨텍스트 / DORA 회귀
# ---------------------------------------------------------------------------

class DashboardIndexViewTests(TestCase):
    """대시보드 index 뷰 기본 동작."""

    def setUp(self):
        self.user = _make_user()
        self.client.force_login(self.user)
        self.suite = _make_suite()

    def test_index_returns_200_when_logged_in(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_index_renders_recent_suite_runs_heading(self):
        resp = self.client.get("/")
        self.assertContains(resp, "최근 Test Suites 실행 내역")

    def test_index_context_has_recent_suite_runs(self):
        run = _completed_run(self.suite, user=self.user)
        resp = self.client.get("/")
        self.assertIn("recent_suite_runs", resp.context)
        runs = list(resp.context["recent_suite_runs"])
        self.assertIn(run, runs)

    def test_index_does_not_contain_dora_string(self):
        """DORA Metrics 섹션이 완전히 제거됐는지 회귀 확인."""
        resp = self.client.get("/")
        self.assertNotContains(resp, "DORA Metrics")
        self.assertNotContains(resp, "DORA")

    def test_index_context_has_metrics_key(self):
        resp = self.client.get("/")
        self.assertIn("metrics", resp.context)
        m = resp.context["metrics"]
        for key in ("total_runs", "passed_count", "failed_count", "pass_rate",
                    "running_count", "today_count"):
            self.assertIn(key, m, f"metrics 에 '{key}' 키가 없음")

    def test_index_context_has_daily_dist_suite_stats(self):
        resp = self.client.get("/")
        for key in ("daily", "dist_quality", "dist_skip", "untriaged_count", "suite_stats"):
            self.assertIn(key, resp.context, f"context 에 '{key}' 키가 없음")

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        resp = self.client.get("/")
        self.assertIn(resp.status_code, (302, 301))
        self.assertIn("/accounts/login", resp["Location"])

    def test_untriaged_count_excludes_aborted(self):
        """untriaged_count 카드는 untriaged_pending() 재사용 — ABORTED(중단)는 제외된 count."""
        run = _completed_run(self.suite, failed_jobs=1, offset_days=0)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.ABORTED)
        resp = self.client.get("/")
        self.assertEqual(resp.context["untriaged_count"], 0)

    def test_untriaged_count_includes_failure(self):
        """대조군 — 같은 상황에서 FAILURE(미분류)는 그대로 카운트된다."""
        run = _completed_run(self.suite, failed_jobs=1, offset_days=0)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.FAILURE)
        resp = self.client.get("/")
        self.assertEqual(resp.context["untriaged_count"], 1)


# ---------------------------------------------------------------------------
# 4: _compute_metrics 단위 테스트
# ---------------------------------------------------------------------------

class ComputeMetricsTests(TestCase):
    """_compute_metrics 집계 정확성."""

    def setUp(self):
        self.suite = _make_suite("MetricsSuite", "m/p")

    def test_empty_db_returns_zero_metrics(self):
        now = timezone.now()
        m = _compute_metrics(now)
        self.assertEqual(m["total_runs"], 0)
        self.assertEqual(m["passed_count"], 0)
        self.assertEqual(m["failed_count"], 0)
        self.assertEqual(m["pass_rate"], 0.0)
        self.assertEqual(m["running_count"], 0)

    def test_counts_within_14_days_only(self):
        now = timezone.now()
        # 최근 14일 내: pass 3, fail 1 (8일 전도 이제 포함)
        for _ in range(3):
            _completed_run(self.suite, failed_jobs=0, offset_days=2)
        _completed_run(self.suite, failed_jobs=1, offset_days=8)
        # 15일 전 — 14일 창 밖이라 포함 안 됨
        _completed_run(self.suite, failed_jobs=0, offset_days=15)

        m = _compute_metrics(now)
        self.assertEqual(m["total_runs"], 4)
        self.assertEqual(m["passed_count"], 3)
        self.assertEqual(m["failed_count"], 1)
        self.assertAlmostEqual(m["pass_rate"], 75.0, places=1)

    def test_running_count_all_regardless_of_date(self):
        """running_count 는 기간 필터 없이 현재 RUNNING 전체."""
        now = timezone.now()
        _running_run(self.suite, offset_days=0)
        _running_run(self.suite, offset_days=0)
        m = _compute_metrics(now)
        self.assertEqual(m["running_count"], 2)

    def test_today_count_includes_running(self):
        """today_count 는 오늘 started_at 인 Run 전체 (상태 무관)."""
        now = timezone.now()
        _completed_run(self.suite, offset_days=0)   # 오늘
        _running_run(self.suite, offset_days=0)      # 오늘 running
        _completed_run(self.suite, offset_days=2)    # 어제 이전
        m = _compute_metrics(now)
        self.assertGreaterEqual(m["today_count"], 2)


# ---------------------------------------------------------------------------
# 5: _compute_result_dist 단위 테스트 (Job 단위 2분포)
# ---------------------------------------------------------------------------

class ComputeResultDistTests(TestCase):
    """_compute_result_dist — 14일 완료 run 의 Job 단위 Pass/Failure, Pass/Skip."""

    def setUp(self):
        self.suite = _make_suite("DistSuite", "dist/p")

    def test_empty_db_all_zeros(self):
        dq, ds = _compute_result_dist(timezone.now())
        self.assertEqual(dq, {"pass": 0, "fail": 0})
        self.assertEqual(ds, {"pass": 0, "skip": 0})

    def test_job_level_split(self):
        """성공/실패(미분류·failure)/skip 이 두 도넛에 올바로 나뉜다."""
        run = _completed_run(self.suite, failed_jobs=3, offset_days=5)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.SUCCESS)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.SUCCESS)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.FAILURE)                 # 미분류 → 장애
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.FAILURE, verdict="failure")  # 장애
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.FAILURE, verdict="skip")     # skip

        dq, ds = _compute_result_dist(timezone.now())
        self.assertEqual(dq, {"pass": 2, "fail": 2})   # 미분류 실패도 장애로 셈
        self.assertEqual(ds, {"pass": 2, "skip": 1})   # 두 도넛의 패스는 동일

    def test_runs_older_than_14_days_excluded(self):
        old = _completed_run(self.suite, failed_jobs=0, offset_days=15)  # 14일 밖 → 제외
        _add_job_run(old, self.suite, status=TestSuiteJobRun.Status.SUCCESS)
        new = _completed_run(self.suite, failed_jobs=0, offset_days=0)   # 포함
        _add_job_run(new, self.suite, status=TestSuiteJobRun.Status.SUCCESS)
        dq, _ = _compute_result_dist(timezone.now())
        self.assertEqual(dq["pass"], 1)

    def test_running_run_jobs_excluded(self):
        """진행 중(running) suite run 의 Job 은 완료 분포에서 제외."""
        run = _running_run(self.suite, offset_days=1)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.FAILURE)
        dq, _ = _compute_result_dist(timezone.now())
        self.assertEqual(dq["fail"], 0)

    def test_aborted_job_excluded_from_both_donuts(self):
        """ABORTED(중단) Job 은 fail 도넛·skip 도넛 어느 쪽에도 세지 않는다."""
        run = _completed_run(self.suite, failed_jobs=2, offset_days=1)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.SUCCESS)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.FAILURE)  # 실장애 → fail
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.ABORTED)  # 중단 → 어디도 안 셈

        dq, ds = _compute_result_dist(timezone.now())
        self.assertEqual(dq, {"pass": 1, "fail": 1})
        self.assertEqual(ds, {"pass": 1, "skip": 0})

    def test_aborted_with_explicit_verdict_follows_verdict(self):
        """사람이 verdict 를 명시 확정한 ABORTED 는 그 판정을 따른다 — failure→장애 도넛, skip→Skip 도넛."""
        run = _completed_run(self.suite, failed_jobs=2, offset_days=1)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.SUCCESS)
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.ABORTED, verdict="failure")
        _add_job_run(run, self.suite, status=TestSuiteJobRun.Status.ABORTED, verdict="skip")

        dq, ds = _compute_result_dist(timezone.now())
        self.assertEqual(dq, {"pass": 1, "fail": 1})   # 확정 Failure ABORTED = 장애
        self.assertEqual(ds, {"pass": 1, "skip": 1})   # 확정 Skip ABORTED = Skip


# ---------------------------------------------------------------------------
# 6: _compute_daily 단위 테스트
# ---------------------------------------------------------------------------

class ComputeDailyTests(TestCase):
    """_compute_daily — 14일 길이, 빈 날짜 0 채움."""

    def setUp(self):
        self.suite = _make_suite("DailySuite", "daily/p")

    def test_returns_exactly_14_entries(self):
        now = timezone.now()
        result = _compute_daily(now)
        self.assertEqual(len(result), 14)

    def test_each_entry_has_label_pass_fail(self):
        now = timezone.now()
        result = _compute_daily(now)
        for entry in result:
            self.assertIn("label", entry)
            self.assertIn("pass", entry)
            self.assertIn("fail", entry)

    def test_empty_days_filled_with_zeros(self):
        """Run 이 없으면 14일 모두 pass=0, fail=0."""
        now = timezone.now()
        result = _compute_daily(now)
        for entry in result:
            self.assertEqual(entry["pass"], 0)
            self.assertEqual(entry["fail"], 0)

    def test_run_appears_in_correct_day_slot(self):
        """3일 전 run 이 정확한 날짜 슬롯에 집계된다."""
        now = timezone.now()
        _completed_run(self.suite, failed_jobs=0, offset_days=3)

        result = _compute_daily(now)
        # 인덱스 10 = 오늘(13) - 3 = 10번째 (0-indexed, 0=oldest)
        # result 는 오래된 날짜 → 최근 순, 마지막이 오늘(idx 13)
        target = result[10]
        self.assertEqual(target["pass"], 1)
        self.assertEqual(target["fail"], 0)

    def test_fail_run_in_correct_day_slot(self):
        now = timezone.now()
        _completed_run(self.suite, failed_jobs=1, offset_days=1)

        result = _compute_daily(now)
        yesterday = result[12]  # 인덱스 12 = 오늘-1
        self.assertEqual(yesterday["fail"], 1)
        self.assertEqual(yesterday["pass"], 0)

    def test_runs_outside_14_days_not_counted(self):
        now = timezone.now()
        _completed_run(self.suite, failed_jobs=0, offset_days=14)  # 경계 밖
        _completed_run(self.suite, failed_jobs=0, offset_days=0)   # 오늘

        result = _compute_daily(now)
        total_pass = sum(e["pass"] for e in result)
        # 오늘 1개만 집계돼야 함
        self.assertEqual(total_pass, 1)


# ---------------------------------------------------------------------------
# 추가: recent_suite_runs 최대 15건 제한 확인
# ---------------------------------------------------------------------------

class RecentSuiteRunsLimitTests(TestCase):
    """대시보드 recent_suite_runs 는 최신 15건만 노출된다."""

    def setUp(self):
        self.user = _make_user("limitops")
        self.client.force_login(self.user)
        self.suite = _make_suite("LimitSuite", "lim/p")

    def test_at_most_15_runs_in_context(self):
        for i in range(20):
            _completed_run(self.suite, failed_jobs=0, offset_days=i)
        resp = self.client.get("/")
        runs = list(resp.context["recent_suite_runs"])
        self.assertLessEqual(len(runs), 15)


class ChartDrilldownDataTests(TestCase):
    """차트 클릭 드릴다운용 데이터 — daily[].date(ISO), suite_stats[].suite_id."""

    def setUp(self):
        self.suite = _make_suite("DrillSuite", "drill/p")

    def test_daily_entries_have_iso_date(self):
        result = _compute_daily(timezone.now())
        self.assertEqual(len(result), 14)
        for e in result:
            self.assertIn("date", e)
            self.assertRegex(e["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_suite_stats_have_suite_id(self):
        _completed_run(self.suite, failed_jobs=0, offset_days=1)
        stats = _compute_suite_stats(timezone.now())
        self.assertTrue(stats)
        self.assertEqual(stats[0]["suite_id"], str(self.suite.pk))
