import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.activitylog.models import ActivityLog
from apps.integrations.jenkins import JenkinsError
from apps.integrations.tapilot import TapError

from .diagnostics import analyze_console_output
from .models import TapRun, TapSuite, TestSuite, TestSuiteJob, TestSuiteJobRun, TestSuiteRun
from .services_sync import sync_api_suites
from .services_tap import (
    cancel_tap_run,
    poll_active_tap_runs,
    poll_tap_run,
    sync_tap_suites,
    trigger_tap_suite,
)
from .views import pick_idle_job


class _FakeJenkinsClient:
    """get_last_build_info 만 흉내내는 fake — behavior 로 job 별 상태 지정.

    behavior[path] ∈ {"idle","busy","404","error"}; 기본은 "idle".
    """

    def __init__(self, behavior):
        self.behavior = behavior

    def get_last_build_info(self, path):
        kind = self.behavior.get(path, "idle")
        if kind == "idle":
            return {"building": False}
        if kind == "busy":
            return {"building": True}
        if kind == "404":
            raise JenkinsError("no build", status=404)
        raise JenkinsError("connection failed")  # status=None → 검증 불가


class PickIdleJobTests(TestCase):
    """pick_idle_job — 검증된 idle worker 만 선택, 조회 실패는 실행하지 않음."""

    def setUp(self):
        self.suite = TestSuite.objects.create(name="S", jenkins_folder_path="f/p")
        self.j1 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="w1", job_full_path="f/p/w1", order=0)
        self.j2 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="w2", job_full_path="f/p/w2", order=1)
        self.j3 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="w3", job_full_path="f/p/w3", order=2)

    def _jobs(self):
        return list(self.suite.jobs.order_by("order"))

    def _patch(self, behavior):
        return mock.patch.object(
            self.suite, "get_jenkins_client",
            return_value=_FakeJenkinsClient(behavior),
        )

    def test_returns_first_idle(self):
        with self._patch({"f/p/w1": "idle"}):
            self.assertEqual(pick_idle_job(self.suite, self._jobs()), self.j1)

    def test_skips_busy_returns_next_idle(self):
        with self._patch({"f/p/w1": "busy", "f/p/w2": "idle"}):
            self.assertEqual(pick_idle_job(self.suite, self._jobs()), self.j2)

    def test_never_built_404_counts_as_idle(self):
        with self._patch({"f/p/w1": "busy", "f/p/w2": "404"}):
            self.assertEqual(pick_idle_job(self.suite, self._jobs()), self.j2)

    def test_all_busy_returns_none(self):
        with self._patch({"f/p/w1": "busy", "f/p/w2": "busy", "f/p/w3": "busy"}):
            self.assertIsNone(pick_idle_job(self.suite, self._jobs()))

    def test_unverifiable_error_is_not_run(self):
        # 조회 실패(상태 확인 불가)는 idle 로 보지 않고 건너뜀 → 전부 실패면 None.
        with self._patch({"f/p/w1": "error", "f/p/w2": "error", "f/p/w3": "error"}):
            self.assertIsNone(pick_idle_job(self.suite, self._jobs()))

    def test_busy_then_error_then_idle(self):
        with self._patch({"f/p/w1": "busy", "f/p/w2": "error", "f/p/w3": "idle"}):
            self.assertEqual(pick_idle_job(self.suite, self._jobs()), self.j3)


class SuiteRunListViewTests(TestCase):
    """단독 Test Suite Run 전용 '최근 실행' 뷰 — 노출/필터 회귀 방지."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="ops", password="pw", role="qpe_ops")
        self.client.force_login(self.user)
        self.suite_a = TestSuite.objects.create(name="Suite-Alpha", jenkins_folder_path="a/p")
        self.suite_b = TestSuite.objects.create(name="Suite-Bravo", jenkins_folder_path="b/p")
        # 단독(API/UI) — triggered_by 있음
        self.manual = TestSuiteRun.objects.create(
            suite=self.suite_a, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, passed_jobs=1, started_at=timezone.now(),
            completed_at=timezone.now(), triggered_by=self.user)
        # 스케줄/자동 — triggered_by 없음
        self.scheduled = TestSuiteRun.objects.create(
            suite=self.suite_b, status=TestSuiteRun.Status.RUNNING,
            total_jobs=5, started_at=timezone.now())

    def test_list_shows_both_runs(self):
        resp = self.client.get("/suites/runs/")
        self.assertEqual(resp.status_code, 200)
        runs = list(resp.context["runs"])
        self.assertIn(self.manual, runs)
        self.assertIn(self.scheduled, runs)
        self.assertContains(resp, "Suite-Alpha")
        self.assertContains(resp, "Suite-Bravo")

    def test_filter_source_manual(self):
        resp = self.client.get("/suites/runs/?source=manual")
        runs = list(resp.context["runs"])
        self.assertIn(self.manual, runs)
        self.assertNotIn(self.scheduled, runs)

    def test_filter_source_scheduled(self):
        resp = self.client.get("/suites/runs/?source=scheduled")
        runs = list(resp.context["runs"])
        self.assertIn(self.scheduled, runs)
        self.assertNotIn(self.manual, runs)

    def test_filter_by_suite(self):
        resp = self.client.get(f"/suites/runs/?suite={self.suite_a.pk}")
        self.assertEqual(list(resp.context["runs"]), [self.manual])

    def test_invalid_suite_uuid_does_not_500(self):
        # 비-UUID suite 필터가 와도 500 이 아니라 200 (필터 무시).
        resp = self.client.get("/suites/runs/?suite=not-a-uuid")
        self.assertEqual(resp.status_code, 200)

    def test_orders_by_last_activity_not_started_at(self):
        """먼저 시작했지만 늦게 완료된 run(long-running FAIL)이 나중에 시작해 일찍
        끝난 run 보다 위에 나와야 한다 — Slack 알림(완료 시점)과 목록 순서 일치."""
        from datetime import timedelta
        base = timezone.now() - timedelta(hours=5)
        # A: 먼저 시작(base) + 늦게 완료(+3h) — 뒤늦게 완료된 실패
        run_a = TestSuiteRun.objects.create(
            suite=self.suite_a, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, failed_jobs=1, started_at=base,
            completed_at=base + timedelta(hours=3), triggered_by=self.user)
        # B: 나중에 시작(+1h) + 일찍 완료(+1h5m)
        run_b = TestSuiteRun.objects.create(
            suite=self.suite_b, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, failed_jobs=0, started_at=base + timedelta(hours=1),
            completed_at=base + timedelta(hours=1, minutes=5), triggered_by=self.user)
        runs = list(self.client.get("/suites/runs/").context["runs"])
        self.assertLess(runs.index(run_a), runs.index(run_b),
                        "늦게 완료된 A(FAIL)가 B보다 위에 있어야 함")

    def test_filter_by_date(self):
        # 대시보드 일별 막대 드릴다운 — started_at 로컬 날짜로 필터.
        today = timezone.localtime(self.manual.started_at).date().isoformat()
        resp = self.client.get("/suites/runs/", {"date": today})
        self.assertIn(self.manual, list(resp.context["runs"]))
        resp2 = self.client.get("/suites/runs/", {"date": "2020-01-01"})
        self.assertNotIn(self.manual, list(resp2.context["runs"]))

    def test_filter_by_invalid_date_ignored(self):
        resp = self.client.get("/suites/runs/", {"date": "not-a-date"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["f_date"], "")


class TriggerSingleIdleRunTests(TestCase):
    """_trigger_single_idle_run — 이미 진행 중인 worker 는 후보에서 제외(HIGH-3)."""

    def setUp(self):
        from . import services as _services
        self.services = _services
        User = get_user_model()
        self.user = User.objects.create_user(username="ops", password="pw", role="qpe_ops")
        self.suite = TestSuite.objects.create(name="S", jenkins_folder_path="f/p")
        self.j1 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="w1", job_full_path="f/p/w1", order=0)
        self.j2 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="w2", job_full_path="f/p/w2", order=1)

    def test_active_worker_excluded_from_candidates(self):
        # j1 이 진행 중인 SuiteRun 에 active job_run 으로 잡혀 있으면 후보에서 빠진다.
        running = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.RUNNING,
            total_jobs=1, started_at=timezone.now())
        TestSuiteJobRun.objects.create(
            suite_run=running, job=self.j1, status=TestSuiteJobRun.Status.RUNNING)

        with mock.patch(
            "apps.suites.views.pick_idle_job",
            side_effect=lambda s, cands: cands[0] if cands else None,
        ), mock.patch("apps.suites.views._try_trigger_next", return_value=False):
            run = self.services.trigger_suite_run(suite=self.suite, user=self.user)

        # 후보는 [j2] 뿐 → j2 로 Run 생성, total_jobs=1.
        self.assertEqual(run.total_jobs, 1)
        self.assertEqual(run.job_runs.first().job_id, self.j2.id)

    def test_all_workers_busy_raises_409_without_creating_run(self):
        # 모든 worker 가 진행 중 → 후보 0개 → 409, Run 생성 안 함.
        running = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.RUNNING,
            total_jobs=2, started_at=timezone.now())
        for j in (self.j1, self.j2):
            TestSuiteJobRun.objects.create(
                suite_run=running, job=j, status=TestSuiteJobRun.Status.RUNNING)

        before = TestSuiteRun.objects.count()
        with mock.patch("apps.suites.views._try_trigger_next", return_value=False):
            with self.assertRaises(self.services.SuiteRunError) as ctx:
                self.services.trigger_suite_run(suite=self.suite, user=self.user)
        self.assertEqual(ctx.exception.code, "all_workers_busy")
        self.assertEqual(ctx.exception.http_status, 409)
        # 새 Run 은 만들어지지 않았다.
        self.assertEqual(TestSuiteRun.objects.count(), before)


# =============================================
# 재시도 (retry_count / retry_limit) 테스트
# =============================================

class _StatefulFakeJenkins:
    """여러 빌드 결과를 순차로 돌려주는 stateful fake Jenkins Client.

    results: [("SUCCESS"|"FAILURE"|"ABORTED"), ...]  — 폴링마다 순서대로 사용.
    마지막 결과 이후는 마지막 값을 반복.
    get_build_info 는 단일 잡(build_number=1) 기준으로만 사용한다고 가정.
    """

    def __init__(self, results):
        self._results = list(results)
        self._call_count = 0  # get_build_info 호출 횟수

    def get_last_build_info(self, path):
        return {"building": False}

    def get_build_info(self, path, build_number):
        idx = min(self._call_count, len(self._results) - 1)
        result = self._results[idx]
        self._call_count += 1
        return {"building": False, "result": result, "url": f"http://j/{build_number}", "duration": 1000}

    def get_build_stages(self, path, build_number):
        return []

    def get_console_output(self, path, build_number):
        return "console-log"

    def trigger_build(self, path, parameters=None):
        return {"build_number": 1}

    def trigger_job(self, path, gwt_token=None):
        return self.trigger_build(path)


def _make_suite_run(suite, retry_limit, n_jobs=1):
    """suite_run + n_jobs 개의 QUEUED job_run 을 만들어 반환."""
    from django.utils import timezone
    suite_run = TestSuiteRun.objects.create(
        suite=suite,
        status=TestSuiteRun.Status.RUNNING,
        total_jobs=n_jobs,
        retry_limit=retry_limit,
        started_at=timezone.now(),
    )
    jobs = list(suite.jobs.order_by("order")[:n_jobs])
    for job in jobs:
        TestSuiteJobRun.objects.create(
            suite_run=suite_run,
            job=job,
            status=TestSuiteJobRun.Status.QUEUED,
            jenkins_build_number=1,
        )
    return suite_run


class PollRunStatusRetryTests(TestCase):
    """`_poll_run_status` 재시도 로직 회귀 테스트.

    `send_suite_run_event_async` 는 데몬 스레드에서 DB 락을 유발할 수 있으므로 전부 mock.
    Jenkins Client 는 stateful fake 로 교체해 실제 네트워크 호출 없이 결과를 시뮬레이션.
    """

    def setUp(self):
        from .views import _poll_run_status
        self._poll = _poll_run_status
        self.suite = TestSuite.objects.create(
            name="RetryTestSuite", jenkins_folder_path="r/p")
        # 이 테스트는 재시도 로직만 검증 — 실행 지연 throttle 과 무관하게 즉시 재트리거
        # 되도록 delay=0(throttle off). throttle 동작은 별도 ClaimTriggerSlotTests 등에서.
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="w1", job_full_path="r/p/w1", order=0,
            trigger_delay_seconds=0)

    def _patch(self, results):
        """stateful fake Jenkins 를 TestSuite 클래스 레벨로 패치해 모든 인스턴스에 적용."""
        fake = _StatefulFakeJenkins(results)
        return mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake)

    # -----------------------------------------------------------
    # (1) retry_limit=1: FAILURE 1회 → COMPLETED, failed_jobs=1, attempt=1
    # -----------------------------------------------------------
    def test_retry_limit_1_failure_once_completes(self):
        suite_run = _make_suite_run(self.suite, retry_limit=1)
        with (
            self._patch(["FAILURE"]),
            mock.patch("apps.suites.views.send_suite_run_event_async"),
        ):
            self._poll(suite_run)
        suite_run.refresh_from_db()
        self.assertEqual(suite_run.status, TestSuiteRun.Status.COMPLETED)
        self.assertEqual(suite_run.failed_jobs, 1)
        job_run = suite_run.job_runs.first()
        self.assertEqual(job_run.attempt, 1)
        self.assertEqual(job_run.status, TestSuiteJobRun.Status.FAILURE)

    # -----------------------------------------------------------
    # (2) retry_limit=3: FAIL→FAIL→PASS → COMPLETED, passed=1, failed=0, attempt=3
    # -----------------------------------------------------------
    def test_retry_limit_3_fail_fail_pass(self):
        suite_run = _make_suite_run(self.suite, retry_limit=3)
        fake = _StatefulFakeJenkins(["FAILURE", "FAILURE", "SUCCESS"])
        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake),
            mock.patch("apps.suites.views.send_suite_run_event_async"),
        ):
            # 1차 폴링: FAILURE → PENDING(attempt=2), retried=True → _try_trigger_next
            self._poll(suite_run)
            suite_run.refresh_from_db()
            # 아직 RUNNING (재시도 중)
            self.assertEqual(suite_run.status, TestSuiteRun.Status.RUNNING)
            self.assertEqual(suite_run.failed_jobs, 0)

            # job_run 을 다시 QUEUED 로 올리기 (실제 _try_trigger_next 가 하는 일을 수동 반영)
            jr = suite_run.job_runs.first()
            jr.refresh_from_db()
            if jr.status == TestSuiteJobRun.Status.PENDING:
                jr.status = TestSuiteJobRun.Status.QUEUED
                jr.jenkins_build_number = 1
                jr.save()

            # 2차 폴링: FAILURE → PENDING(attempt=3), retried=True → _try_trigger_next
            self._poll(suite_run)
            suite_run.refresh_from_db()
            self.assertEqual(suite_run.status, TestSuiteRun.Status.RUNNING)
            self.assertEqual(suite_run.failed_jobs, 0)

            jr.refresh_from_db()
            if jr.status == TestSuiteJobRun.Status.PENDING:
                jr.status = TestSuiteJobRun.Status.QUEUED
                jr.jenkins_build_number = 1
                jr.save()

            # 3차 폴링: SUCCESS → COMPLETED, passed=1
            self._poll(suite_run)
            suite_run.refresh_from_db()

        self.assertEqual(suite_run.status, TestSuiteRun.Status.COMPLETED)
        self.assertEqual(suite_run.passed_jobs, 1)
        self.assertEqual(suite_run.failed_jobs, 0)
        jr.refresh_from_db()
        self.assertEqual(jr.attempt, 3)

    # -----------------------------------------------------------
    # (3) retry_limit=3: FAIL×3 → COMPLETED, failed=1, attempt=3
    # -----------------------------------------------------------
    def test_retry_limit_3_fail_three_times(self):
        suite_run = _make_suite_run(self.suite, retry_limit=3)
        fake = _StatefulFakeJenkins(["FAILURE", "FAILURE", "FAILURE"])
        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake),
            mock.patch("apps.suites.views.send_suite_run_event_async"),
        ):
            # 1차 폴링: FAILURE → PENDING(attempt=2)
            self._poll(suite_run)
            jr = suite_run.job_runs.first()
            jr.refresh_from_db()
            if jr.status == TestSuiteJobRun.Status.PENDING:
                jr.status = TestSuiteJobRun.Status.QUEUED
                jr.jenkins_build_number = 1
                jr.save()

            # 2차 폴링: FAILURE → PENDING(attempt=3)
            self._poll(suite_run)
            jr.refresh_from_db()
            if jr.status == TestSuiteJobRun.Status.PENDING:
                jr.status = TestSuiteJobRun.Status.QUEUED
                jr.jenkins_build_number = 1
                jr.save()

            # 3차 폴링: FAILURE, attempt==retry_limit → COMPLETED, failed=1
            self._poll(suite_run)
            suite_run.refresh_from_db()

        self.assertEqual(suite_run.status, TestSuiteRun.Status.COMPLETED)
        self.assertEqual(suite_run.failed_jobs, 1)
        jr.refresh_from_db()
        self.assertEqual(jr.attempt, 3)
        self.assertEqual(jr.status, TestSuiteJobRun.Status.FAILURE)

    # -----------------------------------------------------------
    # (4) retry_limit=2: 첫판 SUCCESS → passed=1, attempt=1 (재시도 없음)
    # -----------------------------------------------------------
    def test_retry_limit_2_first_success_no_retry(self):
        suite_run = _make_suite_run(self.suite, retry_limit=2)
        with (
            self._patch(["SUCCESS"]),
            mock.patch("apps.suites.views.send_suite_run_event_async"),
        ):
            self._poll(suite_run)
        suite_run.refresh_from_db()
        self.assertEqual(suite_run.status, TestSuiteRun.Status.COMPLETED)
        self.assertEqual(suite_run.passed_jobs, 1)
        self.assertEqual(suite_run.failed_jobs, 0)
        jr = suite_run.job_runs.first()
        self.assertEqual(jr.attempt, 1)

    # -----------------------------------------------------------
    # (5) ABORTED 는 재시도 안 함 (retry_limit=3 이어도 즉시 집계)
    # -----------------------------------------------------------
    def test_aborted_does_not_retry(self):
        suite_run = _make_suite_run(self.suite, retry_limit=3)
        with (
            self._patch(["ABORTED"]),
            mock.patch("apps.suites.views.send_suite_run_event_async"),
        ):
            self._poll(suite_run)
        suite_run.refresh_from_db()
        self.assertEqual(suite_run.status, TestSuiteRun.Status.COMPLETED)
        # ABORTED 는 failed_jobs 에 집계
        self.assertEqual(suite_run.failed_jobs, 1)
        jr = suite_run.job_runs.first()
        # attempt 는 1 그대로
        self.assertEqual(jr.attempt, 1)
        self.assertEqual(jr.status, TestSuiteJobRun.Status.ABORTED)

    # -----------------------------------------------------------
    # (6) 결함#2 회귀: 재시도 reset 시 jenkins_build_url 이 "" 로 초기화
    # -----------------------------------------------------------
    def test_retry_resets_jenkins_build_url(self):
        """FAILURE 재시도 전환 시 jenkins_build_url 이 빈 문자열로 초기화된다 (결함#2 회귀).

        재시도 전 job_run 에 jenkins_build_url="http://old/build/1" 을 세팅해두고,
        폴링 후 PENDING 저장 → _try_trigger_next 가 QUEUED 로 전환하더라도
        jenkins_build_url 은 "" 그대로여야 한다.
        (_try_trigger_next trigger_build 성공 경로의 update_fields 에 jenkins_build_url 이 없으므로
        PENDING 단계에서 "" 로 저장된 값이 유지된다.)

        retry_limit=2 → FAILURE 1번째에서 재시도 전환이 일어난다.
        """
        suite_run = _make_suite_run(self.suite, retry_limit=2)
        jr = suite_run.job_runs.first()
        # 직전 실패 빌드 URL 을 의도적으로 채워둔다
        jr.jenkins_build_url = "http://old/build/1"
        jr.save()

        with (
            self._patch(["FAILURE"]),
            mock.patch("apps.suites.views.send_suite_run_event_async"),
        ):
            self._poll(suite_run)

        jr.refresh_from_db()
        # _try_trigger_next 가 QUEUED 로 바꿨을 수 있으므로 PENDING or QUEUED 허용
        self.assertIn(jr.status, [
            TestSuiteJobRun.Status.PENDING, TestSuiteJobRun.Status.QUEUED,
        ], f"재시도 후 PENDING 또는 QUEUED 여야 하지만 {jr.status}")
        self.assertEqual(jr.attempt, 2)
        # 핵심: 직전 실패 빌드 URL 이 "" 로 초기화돼 남지 않아야 한다.
        # _try_trigger_next 의 update_fields 에 jenkins_build_url 이 없으므로
        # PENDING 단계에서 "" 로 저장된 값이 DB에 그대로 유지된다.
        self.assertEqual(jr.jenkins_build_url, "",
            f"재시도 후 jenkins_build_url 은 '' 이어야 하지만 '{jr.jenkins_build_url}'")
        # suite_run 은 아직 RUNNING (재시도 진행 중)
        suite_run.refresh_from_db()
        self.assertEqual(suite_run.status, TestSuiteRun.Status.RUNNING)


class PollBackdatesCompletionTests(TestCase):
    """완료 시각을 폴링(감지) 시각이 아닌 Jenkins 실제 종료 시각으로 기록하는지 회귀.

    외부 폴러 중단으로 39초짜리 빌드가 하루 뒤에야 감지된 사건(2026-07-21,
    am-mapp-regression-api-dev seq 1284)에서 Slack '소요 시간' 이 24시간 43분으로
    표기되던 문제 — completed_at 은 build_info 의 timestamp+duration 으로 backdate.
    """

    def setUp(self):
        from .views import _poll_run_status
        self._poll = _poll_run_status
        self.suite = TestSuite.objects.create(name="BackdateSuite", jenkins_folder_path="bd/p")
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="b1", job_full_path="bd/p/b1", order=0)

    def _fake(self, result, *, start, duration_ms):
        fake = _StatefulFakeJenkins([result])
        ts_ms = int(start.timestamp() * 1000)
        orig = fake.get_build_info
        def get_build_info(path, build_number):
            info = orig(path, build_number)
            info["timestamp"] = ts_ms
            info["duration"] = duration_ms
            return info
        fake.get_build_info = get_build_info
        return fake

    def test_late_poll_backdates_job_and_suite_completion(self):
        """하루 늦게 폴링해도 completed_at = 실제 빌드 종료(시작+39초), 소요 시간 정상."""
        started = timezone.now() - timezone.timedelta(days=1)
        suite_run = _make_suite_run(self.suite, retry_limit=1)
        suite_run.started_at = started
        suite_run.save(update_fields=["started_at"])
        fake = self._fake("FAILURE", start=started, duration_ms=39000)
        with mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake), \
             mock.patch("apps.suites.views.send_suite_run_event_async"):
            self._poll(suite_run)
        suite_run.refresh_from_db()
        jr = suite_run.job_runs.first()
        expected_end = started + timezone.timedelta(seconds=39)
        self.assertEqual(suite_run.status, TestSuiteRun.Status.COMPLETED)
        self.assertAlmostEqual(
            jr.completed_at.timestamp(), expected_end.timestamp(), delta=1)
        self.assertEqual(suite_run.completed_at, jr.completed_at)  # 마지막 잡 종료로 마감
        self.assertLess(
            (suite_run.completed_at - suite_run.started_at).total_seconds(), 60)

    def test_success_backdated_too(self):
        started = timezone.now() - timezone.timedelta(hours=3)
        suite_run = _make_suite_run(self.suite, retry_limit=1)
        fake = self._fake("SUCCESS", start=started, duration_ms=25000)
        with mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake), \
             mock.patch("apps.suites.views.send_suite_run_event_async"):
            self._poll(suite_run)
        jr = suite_run.job_runs.first()
        jr.refresh_from_db()
        expected_end = started + timezone.timedelta(seconds=25)
        self.assertEqual(jr.status, TestSuiteJobRun.Status.SUCCESS)
        self.assertAlmostEqual(
            jr.completed_at.timestamp(), expected_end.timestamp(), delta=1)

    def test_missing_timestamp_falls_back_to_now(self):
        """Jenkins 가 timestamp 를 안 주면 기존처럼 now() 폴백."""
        suite_run = _make_suite_run(self.suite, retry_limit=1)
        fake = _StatefulFakeJenkins(["FAILURE"])  # timestamp 없음
        before = timezone.now()
        with mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake), \
             mock.patch("apps.suites.views.send_suite_run_event_async"):
            self._poll(suite_run)
        jr = suite_run.job_runs.first()
        jr.refresh_from_db()
        self.assertGreaterEqual(jr.completed_at, before)


class RetryLimitSnapshotTests(TestCase):
    """trigger_suite_run 이 TestSuiteRun.retry_limit 에 suite.retry_count 를 스냅샷하는지."""

    def setUp(self):
        from . import services as _services
        self.services = _services
        User = get_user_model()
        self.user = User.objects.create_user(username="snap_ops", password="pw", role="qpe_ops")
        self.suite = TestSuite.objects.create(
            name="SnapSuite", jenkins_folder_path="s/p", retry_count=5)
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="w1", job_full_path="s/p/w1", order=0)

    def test_trigger_suite_run_snapshots_retry_count(self):
        """trigger_suite_run 이 생성하는 TestSuiteRun.retry_limit == suite.retry_count."""
        with (
            mock.patch("apps.suites.views.pick_idle_job", return_value=self.job),
            mock.patch("apps.suites.views._try_trigger_next", return_value=False),
            mock.patch("apps.suites.services.send_suite_run_event_async"),
        ):
            run = self.services.trigger_suite_run(suite=self.suite, user=self.user)
        self.assertEqual(run.retry_limit, 5)

    def test_retry_count_1_becomes_retry_limit_1(self):
        """retry_count=1(기본) → retry_limit=1."""
        self.suite.retry_count = 1
        self.suite.save()
        with (
            mock.patch("apps.suites.views.pick_idle_job", return_value=self.job),
            mock.patch("apps.suites.views._try_trigger_next", return_value=False),
            mock.patch("apps.suites.services.send_suite_run_event_async"),
        ):
            run = self.services.trigger_suite_run(suite=self.suite, user=self.user)
        self.assertEqual(run.retry_limit, 1)


class ConsoleDiagnosticsTests(SimpleTestCase):
    def test_resume_browser_failure_is_summarized(self):
        log = """
[12:07:17] INFO [Step 14] tool: click args={"uid": "7_74", "includeSnapshot": false}
[12:07:22] INFO [Step 14] click 실패 (5.0초): Timed out while waiting for response to ClientRequest. Waited 5.0 seconds.
[12:08:40] INFO [Step 21] tool: click args={"uid": "7_74"}
[12:08:46] INFO [Step 21] click 실패 (5.0초): Timed out while waiting for response to ClientRequest. Waited 5.0 seconds.
[12:12:59] INFO [C17508] FAILED: 스텝 12: '고등학교 미만 졸업' 체크박스 클릭이 수행되지 않았음.; 스텝 13~15: 희망근무지 '추가하기' 클릭 및 팝업에서 '서울 > 서초구' 선택 후 닫기 동작이 수행되지 않음.; 스텝 16(기대결과): '이력서 작성 완료 페이지로 이동'하지 않음. 현재 페이지 URL이 여전히 https://jts.jobkorea.co.kr/User/Resume/Write 이며, 화면에 '저장 완료' 메시지는 보이지만 완료 페이지로 리다이렉트되지 않았음.
"""

        result = analyze_console_output(log)

        self.assertTrue(result["has_findings"])
        self.assertEqual(result["case_id"], "C17508")
        self.assertEqual(result["suspect_uids"][0]["uid"], "7_74")
        self.assertEqual(result["suspect_uids"][0]["count"], 2)
        self.assertEqual(result["stuck_url"], "https://jts.jobkorea.co.kr/User/Resume/Write")
        self.assertTrue(any("체크박스" in item for item in result["summary"]))
        self.assertTrue(any("희망근무지" in item for item in result["summary"]))
        self.assertTrue(any("저장 메시지" in item for item in result["summary"]))

    def test_no_console_output_returns_empty_diagnostics(self):
        result = analyze_console_output("")

        self.assertFalse(result["has_findings"])
        self.assertEqual(result["summary"], [])
        self.assertEqual(result["actions"], [])


# =============================================
# seq_no 회귀 테스트
# =============================================

class NextSeqNoTests(TestCase):
    """TestSuiteRun.next_seq_no — suite 별 1부터 순증가, 다른 suite 와 독립."""

    def setUp(self):
        self.suite_a = TestSuite.objects.create(name="SeqSuiteA", jenkins_folder_path="seq/a")
        self.suite_b = TestSuite.objects.create(name="SeqSuiteB", jenkins_folder_path="seq/b")

    def test_first_run_gets_seq_no_1(self):
        """Run 이 하나도 없으면 next_seq_no == 1."""
        self.assertEqual(TestSuiteRun.next_seq_no(self.suite_a), 1)

    def test_seq_no_increments_within_same_suite(self):
        """같은 suite 안에서 1, 2, 3 으로 순증가한다."""
        from django.utils import timezone
        r1 = TestSuiteRun.objects.create(
            suite=self.suite_a, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=timezone.now(),
            seq_no=TestSuiteRun.next_seq_no(self.suite_a),
        )
        self.assertEqual(r1.seq_no, 1)

        r2 = TestSuiteRun.objects.create(
            suite=self.suite_a, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=timezone.now(),
            seq_no=TestSuiteRun.next_seq_no(self.suite_a),
        )
        self.assertEqual(r2.seq_no, 2)

        r3 = TestSuiteRun.objects.create(
            suite=self.suite_a, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=timezone.now(),
            seq_no=TestSuiteRun.next_seq_no(self.suite_a),
        )
        self.assertEqual(r3.seq_no, 3)

    def test_seq_no_independent_across_suites(self):
        """suite_a 와 suite_b 의 seq_no 는 서로 독립적으로 카운트된다."""
        from django.utils import timezone
        # suite_a 에 2개 생성
        for _ in range(2):
            TestSuiteRun.objects.create(
                suite=self.suite_a, status=TestSuiteRun.Status.COMPLETED,
                total_jobs=1, started_at=timezone.now(),
                seq_no=TestSuiteRun.next_seq_no(self.suite_a),
            )
        # suite_b 에는 0개 → next_seq_no 는 1 이어야 함
        self.assertEqual(TestSuiteRun.next_seq_no(self.suite_b), 1)
        # suite_a 의 next_seq_no 는 3
        self.assertEqual(TestSuiteRun.next_seq_no(self.suite_a), 3)

    def test_seq_no_0_runs_do_not_affect_counter(self):
        """seq_no=0(기본값, 백필 전 레거시 행) 이 있어도 next_seq_no == 1."""
        from django.utils import timezone
        TestSuiteRun.objects.create(
            suite=self.suite_a, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=timezone.now(),
            seq_no=0,  # 레거시 — 0 이 max 에 반영되면 1+1=2 가 되어선 안 된다
        )
        # Max(seq_no)=0 인 suite 의 다음 번호는 1 이어야 한다.
        self.assertEqual(TestSuiteRun.next_seq_no(self.suite_a), 1)

    def test_seq_no_mixed_legacy_and_assigned(self):
        """seq_no=0 레거시 행과 양수 행이 혼재하면 Max(양수) 기준으로 +1."""
        from django.utils import timezone
        TestSuiteRun.objects.create(
            suite=self.suite_a, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=timezone.now(), seq_no=0,
        )
        TestSuiteRun.objects.create(
            suite=self.suite_a, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=timezone.now(), seq_no=1,
        )
        self.assertEqual(TestSuiteRun.next_seq_no(self.suite_a), 2)


class SuiteRunListResultFilterTests(TestCase):
    """suite_run_list — result 필터(pass/fail/무효값) 회귀 방지."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="rfops", password="pw", role="qpe_ops")
        self.client.force_login(self.user)
        self.suite = TestSuite.objects.create(
            name="FilterSuite", jenkins_folder_path="rf/p")
        # pass run — completed, failed_jobs=0
        self.pass_run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, passed_jobs=1, failed_jobs=0,
            started_at=timezone.now(), completed_at=timezone.now())
        # fail run — completed, failed_jobs>0
        self.fail_run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, passed_jobs=0, failed_jobs=1,
            started_at=timezone.now(), completed_at=timezone.now())
        # running run — not completed
        self.running_run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.RUNNING,
            total_jobs=1, started_at=timezone.now())

    def test_result_pass_shows_only_pass_runs(self):
        resp = self.client.get("/suites/runs/?result=pass")
        self.assertEqual(resp.status_code, 200)
        runs = list(resp.context["runs"])
        self.assertIn(self.pass_run, runs)
        self.assertNotIn(self.fail_run, runs)
        self.assertNotIn(self.running_run, runs)

    def test_result_fail_shows_only_fail_runs(self):
        resp = self.client.get("/suites/runs/?result=fail")
        self.assertEqual(resp.status_code, 200)
        runs = list(resp.context["runs"])
        self.assertIn(self.fail_run, runs)
        self.assertNotIn(self.pass_run, runs)
        self.assertNotIn(self.running_run, runs)

    def test_invalid_result_value_shows_all(self):
        """result=xx 같은 whitelist 외 값은 무시 — 전체 노출."""
        resp = self.client.get("/suites/runs/?result=xx")
        self.assertEqual(resp.status_code, 200)
        runs = list(resp.context["runs"])
        self.assertIn(self.pass_run, runs)
        self.assertIn(self.fail_run, runs)
        self.assertIn(self.running_run, runs)
        # whitelist 외 값은 f_result 가 빈 문자열로 초기화돼야 함
        self.assertEqual(resp.context["f_result"], "")

    def test_result_pass_combined_with_suite_filter(self):
        """result=pass + suite 필터 조합 — 해당 suite 의 pass run 만 노출."""
        suite_b = TestSuite.objects.create(
            name="OtherSuite", jenkins_folder_path="other/p")
        other_pass = TestSuiteRun.objects.create(
            suite=suite_b, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, passed_jobs=1, failed_jobs=0,
            started_at=timezone.now(), completed_at=timezone.now())

        resp = self.client.get(
            f"/suites/runs/?result=pass&suite={self.suite.pk}")
        self.assertEqual(resp.status_code, 200)
        runs = list(resp.context["runs"])
        self.assertIn(self.pass_run, runs)
        self.assertNotIn(other_pass, runs)
        self.assertNotIn(self.fail_run, runs)

    def test_f_result_context_value_preserved(self):
        """context 의 f_result 값이 요청 파라미터 값과 일치한다."""
        resp = self.client.get("/suites/runs/?result=fail")
        self.assertEqual(resp.context["f_result"], "fail")

        resp = self.client.get("/suites/runs/?result=pass")
        self.assertEqual(resp.context["f_result"], "pass")

    def test_result_with_conflicting_status_ignores_status(self):
        """result=pass + status=running 처럼 모순되는 조합은 status 를 무시한다.

        result 는 completed 전제 — 그대로 AND 하면 항상 공집합("결과 없음")이
        되는 조용한 버그라 status 쪽을 버리고 f_status 도 비워 UI 와 일치시킨다.
        """
        resp = self.client.get("/suites/runs/?result=pass&status=running")
        self.assertEqual(resp.status_code, 200)
        runs = list(resp.context["runs"])
        self.assertIn(self.pass_run, runs)
        self.assertNotIn(self.running_run, runs)
        self.assertEqual(resp.context["f_status"], "")
        self.assertEqual(resp.context["f_result"], "pass")

    def test_result_with_completed_status_is_compatible(self):
        """result=fail + status=completed 는 모순이 아니므로 둘 다 유지."""
        resp = self.client.get("/suites/runs/?result=fail&status=completed")
        self.assertEqual(resp.status_code, 200)
        runs = list(resp.context["runs"])
        self.assertIn(self.fail_run, runs)
        self.assertNotIn(self.pass_run, runs)
        self.assertEqual(resp.context["f_status"], "completed")

        resp = self.client.get("/suites/runs/")
        self.assertEqual(resp.context["f_result"], "")


class SuiteRunListSeqNoRenderTests(TestCase):
    """suite_run_list 뷰에서 seq_no 가 #N 으로 렌더링되는지 확인."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="seqops", password="pw", role="qpe_ops")
        self.client.force_login(self.user)
        self.suite = TestSuite.objects.create(name="SeqRenderSuite", jenkins_folder_path="sr/p")

    def test_seq_no_rendered_as_hash_n(self):
        """seq_no >= 1 인 Run 은 목록에서 '#7' 형태로 노출된다."""
        from django.utils import timezone
        TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, passed_jobs=1, started_at=timezone.now(),
            completed_at=timezone.now(), triggered_by=self.user,
            seq_no=7,
        )
        resp = self.client.get("/suites/runs/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "#7")

    def test_seq_no_zero_renders_dash(self):
        """seq_no=0 인 Run(레거시/백필 전)은 '-'로 표시된다."""
        from django.utils import timezone
        TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.RUNNING,
            total_jobs=1, started_at=timezone.now(),
            seq_no=0,
        )
        resp = self.client.get("/suites/runs/")
        self.assertEqual(resp.status_code, 200)
        # seq_no=0 행은 '-' 가 있어야 한다 (템플릿: run.seq_no|default:"-")
        self.assertContains(resp, "-")

    def test_multiple_seq_no_all_rendered(self):
        """여러 seq_no 가 한 뷰에서 모두 렌더링된다."""
        from django.utils import timezone
        for n in (3, 5, 11):
            TestSuiteRun.objects.create(
                suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
                total_jobs=1, started_at=timezone.now(),
                seq_no=n,
            )
        resp = self.client.get("/suites/runs/")
        self.assertEqual(resp.status_code, 200)
        for n in (3, 5, 11):
            self.assertContains(resp, f"#{n}")


class TriggerSeqNoAssignmentTests(TestCase):
    """trigger_suite_run 경로에서 생성된 Run 에 seq_no >= 1 이 부여되는지."""

    def setUp(self):
        from . import services as _services
        self.services = _services
        User = get_user_model()
        self.user = User.objects.create_user(
            username="seqtrig", password="pw", role="qpe_ops")
        self.suite = TestSuite.objects.create(
            name="SeqTrigSuite", jenkins_folder_path="st/p")
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="w1", job_full_path="st/p/w1", order=0)

    def test_idle_run_gets_seq_no_1_on_first_trigger(self):
        """첫 번째 트리거(idle worker 경로) 에서 seq_no=1 이 부여된다."""
        with (
            mock.patch("apps.suites.views.pick_idle_job", return_value=self.job),
            mock.patch("apps.suites.views._try_trigger_next", return_value=False),
            mock.patch("apps.suites.services.send_suite_run_event_async"),
        ):
            run = self.services.trigger_suite_run(suite=self.suite, user=self.user)
        self.assertGreaterEqual(run.seq_no, 1)
        self.assertEqual(run.seq_no, 1)

    def test_second_trigger_gets_seq_no_2(self):
        """두 번째 트리거에서 seq_no=2 가 부여된다."""
        with (
            mock.patch("apps.suites.views.pick_idle_job", return_value=self.job),
            mock.patch("apps.suites.views._try_trigger_next", return_value=False),
            mock.patch("apps.suites.services.send_suite_run_event_async"),
        ):
            run1 = self.services.trigger_suite_run(suite=self.suite, user=self.user)
        self.assertEqual(run1.seq_no, 1)
        # 첫 run 이 RUNNING 상태로 남아 있으면 같은 worker 가 busy 로 잡혀
        # 두 번째 호출이 all_workers_busy 로 거절된다.
        # COMPLETED 로 만든 뒤 두 번째 트리거를 호출한다.
        run1.status = TestSuiteRun.Status.COMPLETED
        run1.save(update_fields=["status"])
        run1.job_runs.update(status=TestSuiteJobRun.Status.SUCCESS)

        with (
            mock.patch("apps.suites.views.pick_idle_job", return_value=self.job),
            mock.patch("apps.suites.views._try_trigger_next", return_value=False),
            mock.patch("apps.suites.services.send_suite_run_event_async"),
        ):
            run2 = self.services.trigger_suite_run(suite=self.suite, user=self.user)
        self.assertEqual(run2.seq_no, 2)

    def test_job_id_path_gets_seq_no(self):
        """job_id 지정 경로(_trigger_single_job_run)에서도 seq_no 가 부여된다."""
        with (
            mock.patch("apps.suites.views._try_trigger_next", return_value=False),
            mock.patch("apps.suites.services.send_suite_run_event_async"),
        ):
            run = self.services.trigger_suite_run(
                suite=self.suite, user=self.user, job_id=str(self.job.pk),
            )
        self.assertGreaterEqual(run.seq_no, 1)

    def test_run_all_path_gets_seq_no(self):
        """run_all=True 경로(_trigger_chain_run)에서도 seq_no 가 부여된다."""
        with (
            mock.patch("apps.suites.views._try_trigger_next", return_value=False),
            mock.patch("apps.suites.services.send_suite_run_event_async"),
        ):
            run = self.services.trigger_suite_run(
                suite=self.suite, user=self.user, run_all=True,
            )
        self.assertGreaterEqual(run.seq_no, 1)


# =====================================================================
# 잡 실행 지연 throttle (apps/suites/throttle.py) + WAITING 전이
# =====================================================================

from datetime import timedelta  # noqa: E402

from .throttle import claim_trigger_slot, seconds_until_slot  # noqa: E402


class ClaimTriggerSlotTests(TestCase):
    """suite 단위 마지막 트리거 시각 기준 delay 경과 여부를 원자적으로 판정/점유."""

    def setUp(self):
        self.suite = TestSuite.objects.create(name="ClaimSuite", jenkins_folder_path="c/p")

    def _last(self):
        return TestSuite.objects.values_list(
            "last_job_triggered_at", flat=True,
        ).get(pk=self.suite.pk)

    def _set_last(self, secs_ago):
        TestSuite.objects.filter(pk=self.suite.pk).update(
            last_job_triggered_at=timezone.now() - timedelta(seconds=secs_ago),
        )

    def test_first_claim_succeeds_and_records(self):
        """최초(마지막 트리거 없음) → True + last_job_triggered_at 기록."""
        self.assertTrue(claim_trigger_slot(self.suite, 30))
        self.assertIsNotNone(self._last())

    def test_second_claim_within_window_fails(self):
        """방금 점유했으면 delay 안에는 False (갱신 안 함)."""
        self.assertTrue(claim_trigger_slot(self.suite, 30))
        first = self._last()
        self.assertFalse(claim_trigger_slot(self.suite, 30))
        self.assertEqual(self._last(), first)

    def test_claim_after_delay_elapsed_succeeds(self):
        """delay 가 지난 뒤(마지막 트리거를 31초 전으로) → True."""
        self.assertTrue(claim_trigger_slot(self.suite, 30))
        self._set_last(31)
        self.assertTrue(claim_trigger_slot(self.suite, 30))

    def test_delay_zero_always_passes(self):
        """delay=0 = throttle 끔 — 연속 호출 모두 통과(타임스탬프는 갱신)."""
        self.assertTrue(claim_trigger_slot(self.suite, 0))
        self.assertTrue(claim_trigger_slot(self.suite, 0))
        self.assertIsNotNone(self._last())

    def test_per_job_delay_respected(self):
        """짧은 delay 잡은 먼저 통과 — 24초 전 기준, delay 10 통과 / 30 보류."""
        self._set_last(24)
        self.assertFalse(claim_trigger_slot(self.suite, 30))  # 24 < 30
        self.assertTrue(claim_trigger_slot(self.suite, 10))   # 24 >= 10

    def test_suites_are_isolated(self):
        """**핵심**: 서로 다른 suite 는 슬롯을 공유하지 않는다(병렬 starvation 방지).

        suite A 가 방금 트리거해 슬롯을 점유해도, suite B 의 잡은 영향 없이 즉시 트리거.
        """
        other = TestSuite.objects.create(name="OtherSuite", jenkins_folder_path="o/p")
        self.assertTrue(claim_trigger_slot(self.suite, 30))   # A 점유
        self.assertFalse(claim_trigger_slot(self.suite, 30))  # A 는 보류
        self.assertTrue(claim_trigger_slot(other, 30))        # B 는 무관하게 통과

    def test_seconds_until_slot(self):
        self._set_last(10)
        remaining = seconds_until_slot(self.suite, 30)
        self.assertTrue(18 <= remaining <= 20, remaining)
        self.assertEqual(seconds_until_slot(self.suite, 0), 0)


class _IdleTriggerFakeJenkins:
    """get_last_build_info(=idle) + trigger_build 만 흉내내는 fake."""

    def __init__(self):
        self.triggered = []

    def get_last_build_info(self, path):
        return {"building": False}

    def trigger_build(self, path, parameters=None):
        self.triggered.append(path)
        return {"build_number": len(self.triggered)}

    def trigger_job(self, path, gwt_token=None):
        return self.trigger_build(path)


class TriggerDelayGateTests(TestCase):
    """`_try_trigger_next` 가 throttle 게이트로 WAITING / 트리거를 분기하는지."""

    def setUp(self):
        from .views import _try_trigger_next
        self._trigger = _try_trigger_next
        self.suite = TestSuite.objects.create(name="GateSuite", jenkins_folder_path="g/p")
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="g1", job_full_path="g/p/g1",
            order=0, trigger_delay_seconds=30,
        )

    def _suite_run(self):
        sr = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.RUNNING,
            total_jobs=1, retry_limit=1, started_at=timezone.now(),
        )
        TestSuiteJobRun.objects.create(
            suite_run=sr, job=self.job, status=TestSuiteJobRun.Status.PENDING,
        )
        return sr

    def test_free_slot_triggers(self):
        """슬롯이 비어 있으면(최초) → 트리거되어 QUEUED."""
        sr = self._suite_run()
        fake = _IdleTriggerFakeJenkins()
        with mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake):
            self._trigger(sr)
        jr = sr.job_runs.first()
        self.assertEqual(jr.status, TestSuiteJobRun.Status.QUEUED)
        self.assertEqual(fake.triggered, ["g/p/g1"])

    def test_busy_slot_defers_to_waiting(self):
        """직전 트리거 직후(이 suite 슬롯 점유 중) → 트리거 없이 WAITING 보류."""
        # 방금 같은 suite 의 다른 잡이 트리거된 상태로 만든다.
        claim_trigger_slot(self.suite, 30)
        sr = self._suite_run()
        fake = _IdleTriggerFakeJenkins()
        with mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake):
            self._trigger(sr)
        jr = sr.job_runs.first()
        self.assertEqual(jr.status, TestSuiteJobRun.Status.WAITING)
        self.assertEqual(fake.triggered, [])  # 트리거 안 됨

    def test_waiting_job_triggers_after_delay(self):
        """WAITING 잡도 delay 가 지나면 다음 호출에서 트리거된다."""
        claim_trigger_slot(self.suite, 30)
        sr = self._suite_run()
        fake = _IdleTriggerFakeJenkins()
        with mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake):
            self._trigger(sr)  # → WAITING
            self.assertEqual(sr.job_runs.first().status, TestSuiteJobRun.Status.WAITING)
            # delay 경과 시뮬레이션 (이 suite 의 마지막 트리거를 31초 전으로)
            TestSuite.objects.filter(pk=self.suite.pk).update(
                last_job_triggered_at=timezone.now() - timedelta(seconds=31))
            self._trigger(sr)  # → QUEUED
        jr = sr.job_runs.first()
        self.assertEqual(jr.status, TestSuiteJobRun.Status.QUEUED)
        self.assertEqual(fake.triggered, ["g/p/g1"])


class SuiteEditTriggerDelayTests(TestCase):
    """suite_edit 화면 POST 로 잡별 trigger_delay_seconds 저장."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="admin", password="x", role="qpe_admin", is_active=True,
        )
        self.suite = TestSuite.objects.create(name="EditSuite", jenkins_folder_path="e/p")
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="e1", job_full_path="e/p/e1",
            order=0, trigger_delay_seconds=30,
        )

    def test_post_updates_per_job_delay(self):
        self.client.force_login(self.user)
        with mock.patch.object(TestSuite, "get_jenkins_client") as gc:
            gc.return_value.get_job_info.side_effect = JenkinsError("skip")
            self.client.post(f"/suites/{self.suite.pk}/edit/", {
                "name": "EditSuite",
                "jenkins_folder_path": "e/p",
                "retry_count": "1",
                f"trigger_delay_{self.job.id}": "5",
            })
        self.job.refresh_from_db()
        self.assertEqual(self.job.trigger_delay_seconds, 5)

    def test_post_blank_keeps_existing(self):
        self.client.force_login(self.user)
        with mock.patch.object(TestSuite, "get_jenkins_client") as gc:
            gc.return_value.get_job_info.side_effect = JenkinsError("skip")
            self.client.post(f"/suites/{self.suite.pk}/edit/", {
                "name": "EditSuite",
                "jenkins_folder_path": "e/p",
                "retry_count": "1",
                f"trigger_delay_{self.job.id}": "",
            })
        self.job.refresh_from_db()
        self.assertEqual(self.job.trigger_delay_seconds, 30)

    def test_get_renders_per_job_delay_inputs(self):
        """편집 화면 GET 이 잡별 지연 입력(name=trigger_delay_<id>)을 렌더한다."""
        self.client.force_login(self.user)
        resp = self.client.get(f"/suites/{self.suite.pk}/edit/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(f"trigger_delay_{self.job.id}", body)
        self.assertIn("잡별 실행 지연 시간", body)


class StageDotRenderTests(TestCase):
    """stage 동그라미(dot) 갯수 = stages_total(NOT_EXECUTED 제외)과 일치 — 회귀.

    버그: dot 루프가 raw stages_json(NOT_EXECUTED 포함)을 돌아 회색 빈 동그라미가
    카운트(Y)보다 많이 생겼다. display_stages_json 으로 통일해 일치시킴.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="sd", password="x", role="qpe_admin", is_active=True)
        self.client.force_login(self.user)
        self.suite = TestSuite.objects.create(name="DotSuite", jenkins_folder_path="d/p")
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="d/p/j1", order=0)
        self.sr = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, passed_jobs=1, retry_limit=1, started_at=timezone.now())
        self.jr = TestSuiteJobRun.objects.create(
            suite_run=self.sr, job=self.job, status=TestSuiteJobRun.Status.SUCCESS,
            jenkins_build_number=1,
            stages_json=[
                {"name": "Build", "status": "SUCCESS"},
                {"name": "Test", "status": "SUCCESS"},
                {"name": "Deploy", "status": "NOT_EXECUTED"},  # when{} skip → dot 에서 제외돼야
            ])

    def test_dot_count_matches_displayed_total(self):
        # COMPLETED 라 _poll_run_status(Jenkins) 안 탐 — 저장된 stages_json 그대로 렌더
        resp = self.client.get(f"/suites/runs/{self.sr.pk}/status/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertEqual(body.count("stage-dot"), 2, "NOT_EXECUTED 제외 후 dot 은 2개여야 함")
        self.assertNotIn("stage-not_executed", body, "NOT_EXECUTED dot 이 렌더되면 안 됨")
        self.assertIn("2/2", body, "카운트 라벨도 2/2 (dot 갯수와 일치)")


class SuiteShortLabelTests(TestCase):
    """목록 # 컬럼 앞 구분자용 short_label — 공통 토큰(regression/api) 제거."""

    def test_strips_generic_tokens(self):
        s = TestSuite.objects.create(
            name="am-dweb-regression-api-prd", jenkins_folder_path="sl/1")
        self.assertEqual(s.short_label, "am-dweb-prd")
        s2 = TestSuite.objects.create(
            name="jk-mapp-regression-api-stg_103", jenkins_folder_path="sl/2")
        self.assertEqual(s2.short_label, "jk-mapp-stg_103")

    def test_fallback_when_no_convention(self):
        s = TestSuite.objects.create(name="MySuite", jenkins_folder_path="sl/3")
        self.assertEqual(s.short_label, "MySuite")

    def test_run_list_shows_prefix(self):
        user = get_user_model().objects.create_user(
            username="rl", password="x", role="qpe_admin", is_active=True)
        self.client.force_login(user)
        s = TestSuite.objects.create(
            name="am-dweb-regression-api-prd", jenkins_folder_path="sl/4")
        TestSuiteRun.objects.create(
            suite=s, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, passed_jobs=1, seq_no=5, started_at=timezone.now())
        resp = self.client.get("/suites/runs/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "am-dweb-prd")   # 구분자
        self.assertContains(resp, "#5")            # seq_no


class GwtTokenTests(TestCase):
    """워커별 GWT 토큰 발급 — 모델 헬퍼 + 발급/재발급 뷰 + 권한."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="gwt_admin", password="pw", role="qpe_admin")
        self.ops = User.objects.create_user(username="gwt_ops", password="pw", role="qpe_ops")
        self.suite = TestSuite.objects.create(name="GwtSuite", jenkins_folder_path="g/p")
        self.j1 = TestSuiteJob.objects.create(suite=self.suite, job_name="w1", job_full_path="g/p/w1", order=0)
        self.j2 = TestSuiteJob.objects.create(suite=self.suite, job_name="w2", job_full_path="g/p/w2", order=1)

    def test_gen_gwt_token_unique_and_prefixed(self):
        a, b = TestSuiteJob.gen_gwt_token(), TestSuiteJob.gen_gwt_token()
        self.assertTrue(a.startswith("qgwt_"))
        self.assertNotEqual(a, b)

    def test_generate_all_assigns_to_empty_only(self):
        from django.urls import reverse
        self.j1.gwt_token = "existing"
        self.j1.save(update_fields=["gwt_token"])
        self.client.force_login(self.admin)
        self.client.post(reverse("suites:gwt_tokens_generate", args=[self.suite.pk]))
        self.j1.refresh_from_db(); self.j2.refresh_from_db()
        self.assertEqual(self.j1.gwt_token, "existing")           # 기존 토큰 보존
        self.assertTrue(self.j2.gwt_token.startswith("qgwt_"))    # 빈 워커만 발급

    def test_regen_sets_new_token(self):
        from django.urls import reverse
        self.j1.gwt_token = "old"
        self.j1.save(update_fields=["gwt_token"])
        self.client.force_login(self.admin)
        self.client.post(reverse("suites:gwt_token_regen", args=[self.j1.pk]))
        self.j1.refresh_from_db()
        self.assertNotEqual(self.j1.gwt_token, "old")
        self.assertTrue(self.j1.gwt_token.startswith("qgwt_"))

    def test_non_admin_cannot_generate(self):
        from django.urls import reverse
        self.client.force_login(self.ops)
        self.client.post(reverse("suites:gwt_tokens_generate", args=[self.suite.pk]))
        self.j2.refresh_from_db()
        self.assertEqual(self.j2.gwt_token, "")                   # ops 는 발급 불가


class TriageResultTests(TestCase):
    """verdict(Skip) 반영 결과 — triage_result 프로퍼티 + annotate + 대시보드/목록."""

    _n = 0

    def _run(self, failed=1, verdict=""):
        TriageResultTests._n += 1
        n = TriageResultTests._n
        suite = TestSuite.objects.create(name=f"tr-{n}", jenkins_folder_path=f"t/p{n}")
        run = TestSuiteRun.objects.create(
            suite=suite, status=TestSuiteRun.Status.COMPLETED, total_jobs=1, failed_jobs=failed,
            started_at=timezone.now(), completed_at=timezone.now())
        job = TestSuiteJob.objects.create(suite=suite, job_name="j1", job_full_path=f"t/j{n}")
        st = TestSuiteJobRun.Status.FAILURE if failed else TestSuiteJobRun.Status.SUCCESS
        TestSuiteJobRun.objects.create(suite_run=run, job=job, status=st, verdict=verdict)
        return run

    def test_property_skip_fail_pass(self):
        self.assertEqual(self._run(1, "skip").triage_result, "skip")
        self.assertEqual(self._run(1, "").triage_result, "fail")       # 미분류
        self.assertEqual(self._run(1, "failure").triage_result, "fail")
        self.assertEqual(self._run(0, "").triage_result, "pass")

    def test_annotate_matches_property(self):
        run = self._run(1, "skip")
        ann = TestSuiteRun.annotate_triage(TestSuiteRun.objects.filter(pk=run.pk)).first()
        self.assertEqual(ann.real_failed, 0)
        self.assertEqual(ann.triage_result, "skip")
        run2 = self._run(1, "failure")
        ann2 = TestSuiteRun.annotate_triage(TestSuiteRun.objects.filter(pk=run2.pk)).first()
        self.assertEqual(ann2.real_failed, 1)
        self.assertEqual(ann2.triage_result, "fail")

    def test_dashboard_metrics_exclude_skip(self):
        from apps.dashboard.views import _compute_metrics
        from django.utils import timezone
        self._run(1, "skip")      # 실패지만 Skip → 실패로 안 셈
        self._run(1, "failure")   # 실장애
        self._run(0, "")          # pass
        m = _compute_metrics(timezone.now())
        self.assertEqual(m["failed_count"], 1)   # skip 제외, 실장애 1건만
        self.assertEqual(m["passed_count"], 2)   # pass + skip

    def test_run_list_skip_filter(self):
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.create_user(username="ops9", password="x", role="qpe_ops")
        self.client.force_login(u)
        skip_run = self._run(1, "skip")
        fail_run = self._run(1, "failure")
        # result=fail 은 skip 제외
        fail_ids = [r.pk for r in self.client.get("/suites/runs/?result=fail").context["runs"]]
        self.assertIn(fail_run.pk, fail_ids)
        self.assertNotIn(skip_run.pk, fail_ids)
        # result=skip 은 skip 만
        skip_ids = [r.pk for r in self.client.get("/suites/runs/?result=skip").context["runs"]]
        self.assertIn(skip_run.pk, skip_ids)
        self.assertNotIn(fail_run.pk, skip_ids)


from django.test import override_settings  # noqa: E402


@override_settings(UNTRIAGED_QUEUE_SINCE="2000-01-01T00:00:00+00:00")
class UntriagedQueueTests(TestCase):
    """'미 장애 판정' 대기 큐 — 컷오프 필터 + 포털 판정(job_run_triage) 회귀.

    apply_verdict 은 slack_message_ts 가 없으면 Slack 을 타지 않으므로 mock 불필요.
    """

    def setUp(self):
        User = get_user_model()
        self.ops = User.objects.create_user(username="uq_ops", password="pw", role="qpe_ops")
        self.viewer = User.objects.create_user(username="uq_view", password="pw", role="viewer")
        self.suite = TestSuite.objects.create(name="UQ", jenkins_folder_path="uq/p")
        now = timezone.now()
        self.run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=2, passed_jobs=1, failed_jobs=1,
            started_at=now, completed_at=now)  # slack_message_ts 없음 → 네트워크 미발생
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="uqj1", job_full_path="uq/j1")
        self.jr = TestSuiteJobRun.objects.create(
            suite_run=self.run, job=self.job, status=TestSuiteJobRun.Status.FAILURE,
            started_at=now, completed_at=now)

    def _other_job_run(self, *, status, verdict=""):
        n = TestSuiteJob.objects.count()
        job = TestSuiteJob.objects.create(
            suite=self.suite, job_name=f"uqj{n+9}", job_full_path=f"uq/j{n+9}")
        return TestSuiteJobRun.objects.create(
            suite_run=self.run, job=job, status=status, verdict=verdict,
            started_at=self.run.started_at, completed_at=self.run.completed_at)

    # ── untriaged_pending 쿼리 ──
    def test_pending_includes_untriaged_failure(self):
        self.assertIn(self.jr, list(TestSuiteJobRun.untriaged_pending()))

    def test_pending_excludes_triaged(self):
        self.jr.verdict = "skip"
        self.jr.save(update_fields=["verdict"])
        self.assertNotIn(self.jr, list(TestSuiteJobRun.untriaged_pending()))

    def test_pending_excludes_success(self):
        ok = self._other_job_run(status=TestSuiteJobRun.Status.SUCCESS)
        self.assertNotIn(ok, list(TestSuiteJobRun.untriaged_pending()))

    def test_pending_excludes_aborted(self):
        """ABORTED(중단)는 장애가 아니므로 미 장애 판정 큐에서 제외된다."""
        aborted = self._other_job_run(status=TestSuiteJobRun.Status.ABORTED)
        self.assertNotIn(aborted, list(TestSuiteJobRun.untriaged_pending()))
        # FAILURE 는 그대로 포함(회귀 방지 — ABORTED 제외가 FAILURE 까지 걸러내면 안 됨).
        self.assertIn(self.jr, list(TestSuiteJobRun.untriaged_pending()))

    @override_settings(UNTRIAGED_QUEUE_SINCE="2999-01-01T00:00:00+00:00")
    def test_cutoff_excludes_before(self):
        # 컷오프가 미래면 지금 발생한 실패는 큐에서 제외.
        self.assertEqual(TestSuiteJobRun.untriaged_pending().count(), 0)

    # ── 화면 ──
    def test_list_view_200_and_lists_pending(self):
        self.client.force_login(self.ops)
        resp = self.client.get("/suites/runs/untriaged/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.jr, list(resp.context["job_runs"]))
        self.assertContains(resp, "uqj1")

    # ── 판정(job_run_triage) ──
    def test_triage_sets_verdict_and_removes_from_queue(self):
        self.client.force_login(self.ops)
        resp = self.client.post(f"/suites/job-runs/{self.jr.pk}/triage/", {"verdict": "skip"})
        self.assertEqual(resp.status_code, 302)
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "skip")
        self.assertEqual(self.jr.triaged_by, self.ops)
        self.assertNotIn(self.jr, list(TestSuiteJobRun.untriaged_pending()))

    def test_triage_failure_verdict(self):
        self.client.force_login(self.ops)
        self.client.post(f"/suites/job-runs/{self.jr.pk}/triage/", {"verdict": "failure"})
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "failure")

    def test_triage_viewer_forbidden(self):
        self.client.force_login(self.viewer)
        self.client.post(f"/suites/job-runs/{self.jr.pk}/triage/", {"verdict": "skip"})
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "")  # 뷰어는 판정 불가

    def test_triage_invalid_verdict_ignored(self):
        self.client.force_login(self.ops)
        self.client.post(f"/suites/job-runs/{self.jr.pk}/triage/", {"verdict": "bogus"})
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "")

    def test_triage_get_does_not_mutate(self):
        self.client.force_login(self.ops)
        self.client.get(f"/suites/job-runs/{self.jr.pk}/triage/")
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "")


@override_settings(UNTRIAGED_QUEUE_SINCE="2000-01-01T00:00:00+00:00")
class UntriagedBulkTriageTests(TestCase):
    """미 장애 판정 큐 — 체크박스 일괄 판정(untriaged_bulk_triage) 회귀.

    apply_verdicts_bulk 는 slack_message_ts 가 없으면 백그라운드 스레드가 즉시
    반환하므로(Slack 미호출) mock 불필요 — DB/ActivityLog 반영은 응답 전에 동기 처리된다.
    """

    URL = "/suites/runs/untriaged/bulk-triage/"

    def setUp(self):
        User = get_user_model()
        self.ops = User.objects.create_user(username="ubt_ops", password="pw", role="qpe_ops")
        self.viewer = User.objects.create_user(username="ubt_view", password="pw", role="viewer")
        self.suite = TestSuite.objects.create(name="UBT", jenkins_folder_path="ubt/p")
        now = timezone.now()
        self.run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=3, passed_jobs=0, failed_jobs=3,
            started_at=now, completed_at=now)  # slack_message_ts 없음 → 네트워크 미발생
        self.jrs = [
            self._job_run(f"ubtj{i}", status=TestSuiteJobRun.Status.FAILURE) for i in range(3)
        ]

    def _job_run(self, name, *, status, verdict=""):
        job = TestSuiteJob.objects.create(
            suite=self.suite, job_name=name, job_full_path=f"ubt/{name}")
        return TestSuiteJobRun.objects.create(
            suite_run=self.run, job=job, status=status, verdict=verdict,
            started_at=self.run.started_at, completed_at=self.run.completed_at)

    def _post(self, user, job_runs, verdict, extra=None):
        self.client.force_login(user)
        data = {"verdict": verdict, "job_run_ids": [str(jr.pk) for jr in job_runs]}
        if extra:
            data.update(extra)
        return self.client.post(self.URL, data)

    def _refresh(self):
        for jr in self.jrs:
            jr.refresh_from_db()

    def test_bulk_failure_only_selected_updated(self):
        resp = self._post(self.ops, self.jrs[:2], "failure")
        self.assertEqual(resp.status_code, 302)
        self._refresh()
        self.assertEqual(self.jrs[0].verdict, "failure")
        self.assertEqual(self.jrs[0].triaged_by, self.ops)
        self.assertIsNotNone(self.jrs[0].triaged_at)
        self.assertEqual(self.jrs[1].verdict, "failure")
        self.assertEqual(self.jrs[1].triaged_by, self.ops)
        self.assertEqual(self.jrs[2].verdict, "")  # 미선택 → 미분류 유지
        self.assertIsNone(self.jrs[2].triaged_by)

        pending = list(TestSuiteJobRun.untriaged_pending())
        self.assertNotIn(self.jrs[0], pending)
        self.assertNotIn(self.jrs[1], pending)
        self.assertIn(self.jrs[2], pending)

        log = ActivityLog.objects.filter(action="job_triaged").latest("created_at")
        self.assertEqual(log.target_name, "일괄 판정 2건")
        self.assertIn("verdict=failure", log.detail)
        self.assertEqual(log.user, self.ops)

    def test_bulk_skip_verdict(self):
        resp = self._post(self.ops, self.jrs[:2], "skip")
        self.assertEqual(resp.status_code, 302)
        self._refresh()
        self.assertEqual(self.jrs[0].verdict, "skip")
        self.assertEqual(self.jrs[1].verdict, "skip")
        self.assertEqual(self.jrs[2].verdict, "")
        log = ActivityLog.objects.filter(action="job_triaged").latest("created_at")
        self.assertEqual(log.target_name, "일괄 판정 2건")
        self.assertIn("verdict=skip", log.detail)

    def test_bulk_viewer_forbidden(self):
        resp = self._post(self.viewer, self.jrs[:2], "failure")
        self.assertEqual(resp.status_code, 302)
        self._refresh()
        for jr in self.jrs:
            self.assertEqual(jr.verdict, "")
        self.assertFalse(ActivityLog.objects.filter(action="job_triaged").exists())

    def test_bulk_invalid_verdict_400(self):
        resp = self._post(self.ops, self.jrs[:2], "bogus")
        self.assertEqual(resp.status_code, 400)
        self._refresh()
        for jr in self.jrs:
            self.assertEqual(jr.verdict, "")
        self.assertFalse(ActivityLog.objects.filter(action="job_triaged").exists())

    def test_bulk_invalid_uuid_400(self):
        self.client.force_login(self.ops)
        resp = self.client.post(self.URL, {
            "verdict": "failure", "job_run_ids": ["not-a-uuid"],
        })
        self.assertEqual(resp.status_code, 400)
        self._refresh()
        for jr in self.jrs:
            self.assertEqual(jr.verdict, "")

    def test_bulk_too_many_ids_400(self):
        self.client.force_login(self.ops)
        many_ids = [str(uuid.uuid4()) for _ in range(301)]
        resp = self.client.post(self.URL, {"verdict": "failure", "job_run_ids": many_ids})
        self.assertEqual(resp.status_code, 400)
        self._refresh()
        for jr in self.jrs:
            self.assertEqual(jr.verdict, "")

    def test_bulk_empty_selection_redirect(self):
        resp = self._post(self.ops, [], "failure")
        self.assertEqual(resp.status_code, 302)
        self._refresh()
        for jr in self.jrs:
            self.assertEqual(jr.verdict, "")
        self.assertFalse(ActivityLog.objects.filter(action="job_triaged").exists())

    def test_bulk_mixed_status_only_failure_processed(self):
        aborted = self._job_run("ubt_ab", status=TestSuiteJobRun.Status.ABORTED)
        success = self._job_run("ubt_ok", status=TestSuiteJobRun.Status.SUCCESS)
        resp = self._post(self.ops, [self.jrs[0], aborted, success], "failure")
        self.assertEqual(resp.status_code, 302)
        self.jrs[0].refresh_from_db()
        aborted.refresh_from_db()
        success.refresh_from_db()
        self.assertEqual(self.jrs[0].verdict, "failure")  # FAILURE 만 처리
        self.assertEqual(aborted.verdict, "")
        self.assertEqual(success.verdict, "")
        log = ActivityLog.objects.filter(action="job_triaged").latest("created_at")
        self.assertEqual(log.target_name, "일괄 판정 1건")  # 걸러진 뒤 실제 처리 건수만 기록

    def test_bulk_get_does_not_mutate(self):
        self.client.force_login(self.ops)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 302)
        self._refresh()
        for jr in self.jrs:
            self.assertEqual(jr.verdict, "")


@override_settings(UNTRIAGED_QUEUE_SINCE="2000-01-01T00:00:00+00:00")
class UntriagedSuiteFilterTests(TestCase):
    """미 장애 판정 큐 — Suite 필터(복수 선택, 즉시 적용) + 판정 후 필터 유지."""

    URL = "/suites/runs/untriaged/"

    def setUp(self):
        User = get_user_model()
        self.ops = User.objects.create_user(username="usf_ops", password="pw", role="qpe_ops")
        self.client.force_login(self.ops)
        now = timezone.now()
        self.suites, self.jrs = [], []
        for i, n_fails in enumerate([2, 1, 1]):
            suite = TestSuite.objects.create(name=f"USF{i}", jenkins_folder_path=f"usf/p{i}")
            run = TestSuiteRun.objects.create(
                suite=suite, status=TestSuiteRun.Status.COMPLETED,
                total_jobs=n_fails, failed_jobs=n_fails,
                started_at=now, completed_at=now)  # slack_message_ts 없음
            self.suites.append(suite)
            for j in range(n_fails):
                job = TestSuiteJob.objects.create(
                    suite=suite, job_name=f"usf{i}-{j}", job_full_path=f"usf/{i}/{j}")
                self.jrs.append(TestSuiteJobRun.objects.create(
                    suite_run=run, job=job, status=TestSuiteJobRun.Status.FAILURE,
                    started_at=now, completed_at=now))

    def test_no_filter_shows_all_and_counts(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 4)
        self.assertFalse(resp.context["is_filtered"])
        counts = {c["name"]: c["n"] for c in resp.context["suite_counts"]}
        self.assertEqual(counts, {"USF0": 2, "USF1": 1, "USF2": 1})

    def test_single_suite_filter(self):
        resp = self.client.get(self.URL, {"suite": str(self.suites[0].pk)})
        self.assertEqual(resp.context["total_count"], 2)
        self.assertTrue(resp.context["is_filtered"])
        shown_suites = {jr.suite_run.suite_id for jr in resp.context["job_runs"]}
        self.assertEqual(shown_suites, {self.suites[0].pk})

    def test_multi_suite_filter(self):
        resp = self.client.get(self.URL, {
            "suite": [str(self.suites[0].pk), str(self.suites[2].pk)],
        })
        self.assertEqual(resp.context["total_count"], 3)
        shown_suites = {jr.suite_run.suite_id for jr in resp.context["job_runs"]}
        self.assertEqual(shown_suites, {self.suites[0].pk, self.suites[2].pk})
        # 칩 체크 상태 유지용 컨텍스트
        self.assertCountEqual(
            resp.context["selected_suite_ids"],
            [str(self.suites[0].pk), str(self.suites[2].pk)],
        )

    def test_invalid_suite_id_silently_ignored(self):
        resp = self.client.get(self.URL, {"suite": "not-a-uuid"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 4)  # 필터 미적용
        self.assertFalse(resp.context["is_filtered"])

    def test_suite_counts_ignore_filter(self):
        """칩 건수는 필터와 무관하게 전체 대기 기준 — 다른 Suite 로 갈아탈 수 있게."""
        resp = self.client.get(self.URL, {"suite": str(self.suites[1].pk)})
        counts = {c["name"]: c["n"] for c in resp.context["suite_counts"]}
        self.assertEqual(counts, {"USF0": 2, "USF1": 1, "USF2": 1})

    def test_bulk_triage_preserves_filter_on_redirect(self):
        sid = str(self.suites[0].pk)
        resp = self.client.post("/suites/runs/untriaged/bulk-triage/", {
            "verdict": "skip",
            "job_run_ids": [str(self.jrs[0].pk)],
            "filter_suite": [sid],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"suite={sid}", resp["Location"])

    def test_single_triage_preserves_filter_on_redirect(self):
        sid = str(self.suites[0].pk)
        resp = self.client.post(f"/suites/job-runs/{self.jrs[0].pk}/triage/", {
            "verdict": "skip",
            "filter_suite": [sid],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"suite={sid}", resp["Location"])

    def test_redirect_drops_invalid_filter_values(self):
        resp = self.client.post("/suites/runs/untriaged/bulk-triage/", {
            "verdict": "skip",
            "job_run_ids": [str(self.jrs[0].pk)],
            "filter_suite": ["not-a-uuid"],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("suite=", resp["Location"])


class JobRunListTests(TestCase):
    """대시보드 결과 분포 도넛 드릴다운 — Job 단위 결과 목록(result 필터)."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="jrl", password="pw", role="qpe_ops")
        self.client.force_login(self.user)
        self.suite = TestSuite.objects.create(name="JRL", jenkins_folder_path="jrl/p")
        now = timezone.now()
        self.run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=4, started_at=now, completed_at=now)
        self.ok = self._jr("success")
        self.fail_untriaged = self._jr("failure")            # verdict="" → 장애(미분류)
        self.fail_confirmed = self._jr("failure", "failure")  # 장애
        self.skip = self._jr("failure", "skip")               # skip

    def _jr(self, status, verdict="", run=None):
        run = run or self.run
        n = TestSuiteJob.objects.count()
        job = TestSuiteJob.objects.create(
            suite=self.suite, job_name=f"jrlj{n}", job_full_path=f"jrl/j{n}")
        st = {
            "success": TestSuiteJobRun.Status.SUCCESS,
            "aborted": TestSuiteJobRun.Status.ABORTED,
        }.get(status, TestSuiteJobRun.Status.FAILURE)
        return TestSuiteJobRun.objects.create(
            suite_run=run, job=job, status=st, verdict=verdict,
            started_at=run.started_at, completed_at=run.completed_at)

    def _get(self, **params):
        return list(self.client.get("/suites/job-runs/", params).context["job_runs"])

    def test_result_success(self):
        rows = self._get(result="success")
        self.assertIn(self.ok, rows)
        for x in (self.fail_untriaged, self.fail_confirmed, self.skip):
            self.assertNotIn(x, rows)

    def test_result_failure_includes_untriaged_and_confirmed_excludes_skip(self):
        rows = self._get(result="failure")
        self.assertIn(self.fail_untriaged, rows)
        self.assertIn(self.fail_confirmed, rows)
        self.assertNotIn(self.skip, rows)
        self.assertNotIn(self.ok, rows)

    def test_result_skip_only(self):
        rows = self._get(result="skip")
        self.assertEqual(rows, [self.skip])

    def test_no_result_returns_all(self):
        rows = self._get()
        self.assertEqual(len(rows), 4)

    def test_suite_filter(self):
        other = TestSuite.objects.create(name="Other", jenkins_folder_path="oth/p")
        orun = TestSuiteRun.objects.create(
            suite=other, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=self.run.started_at, completed_at=self.run.completed_at)
        ojob = TestSuiteJob.objects.create(suite=other, job_name="oj", job_full_path="oth/j")
        ojr = TestSuiteJobRun.objects.create(
            suite_run=orun, job=ojob, status=TestSuiteJobRun.Status.SUCCESS,
            started_at=orun.started_at, completed_at=orun.completed_at)
        rows = self._get(suite=str(self.suite.pk))
        self.assertIn(self.ok, rows)
        self.assertNotIn(ojr, rows)

    def test_older_than_14_days_excluded(self):
        from datetime import timedelta
        old = timezone.now() - timedelta(days=15)
        orun = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=old, completed_at=old)
        ojr = self._jr("success", run=orun)
        self.assertNotIn(ojr, self._get(result="success"))

    def test_result_failure_excludes_aborted(self):
        """ABORTED(중단)는 장애가 아니므로 result=failure 목록에서 제외된다."""
        aborted = self._jr("aborted")
        rows = self._get(result="failure")
        self.assertNotIn(aborted, rows)
        self.assertIn(self.fail_untriaged, rows)  # FAILURE 는 그대로 포함

    def test_result_skip_excludes_aborted(self):
        aborted = self._jr("aborted")
        rows = self._get(result="skip")
        self.assertNotIn(aborted, rows)
        self.assertIn(self.skip, rows)


# =====================================================================
# ABORTED(중단) 은 장애가 아니다 — triage_result / annotate_triage 회귀
# =====================================================================

class AbortedTriageResultTests(TestCase):
    """triage_result·annotate_triage — 실패 Job 전부 ABORTED(중단)는 장애로 안 본다.

    - 실패 Job 전부 ABORTED(미분류) → 'aborted' (장애 아님)
    - ABORTED + FAILURE(미분류/failure) 혼재 → 'fail' (실장애 있음)
    - ABORTED + Skip 분류 FAILURE → 'skip' (실장애 없고 Skip 만)
    property(비주석 fallback 경로)·annotate_triage(주석 경로) 양쪽 모두 검증.
    """

    _n = 0

    def _run(self, job_specs):
        """job_specs: [(status, verdict), ...] — failed_jobs 는 FAILURE/ABORTED 개수 합."""
        AbortedTriageResultTests._n += 1
        n = AbortedTriageResultTests._n
        suite = TestSuite.objects.create(name=f"atr-{n}", jenkins_folder_path=f"atr/p{n}")
        failed = sum(
            1 for st, _ in job_specs
            if st in (TestSuiteJobRun.Status.FAILURE, TestSuiteJobRun.Status.ABORTED)
        )
        run = TestSuiteRun.objects.create(
            suite=suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=len(job_specs), failed_jobs=failed,
            started_at=timezone.now(), completed_at=timezone.now())
        for i, (st, verdict) in enumerate(job_specs):
            job = TestSuiteJob.objects.create(
                suite=suite, job_name=f"j{i}", job_full_path=f"atr/j{n}-{i}")
            TestSuiteJobRun.objects.create(suite_run=run, job=job, status=st, verdict=verdict)
        return run

    # ── 전부 ABORTED(미분류) → 'aborted' ──
    def test_all_aborted_is_aborted_property(self):
        run = self._run([(TestSuiteJobRun.Status.ABORTED, "")])
        self.assertEqual(run.triage_result, "aborted")

    def test_all_aborted_is_aborted_annotated(self):
        run = self._run([(TestSuiteJobRun.Status.ABORTED, "")])
        ann = TestSuiteRun.annotate_triage(TestSuiteRun.objects.filter(pk=run.pk)).first()
        self.assertEqual(ann.real_failed, 0)
        self.assertEqual(ann.triage_result, "aborted")

    # ── ABORTED + FAILURE 혼재 → 'fail' ──
    def test_aborted_plus_failure_is_fail_property(self):
        run = self._run([
            (TestSuiteJobRun.Status.ABORTED, ""),
            (TestSuiteJobRun.Status.FAILURE, ""),
        ])
        self.assertEqual(run.triage_result, "fail")

    def test_aborted_plus_failure_is_fail_annotated(self):
        run = self._run([
            (TestSuiteJobRun.Status.ABORTED, ""),
            (TestSuiteJobRun.Status.FAILURE, ""),
        ])
        ann = TestSuiteRun.annotate_triage(TestSuiteRun.objects.filter(pk=run.pk)).first()
        self.assertEqual(ann.real_failed, 1)
        self.assertEqual(ann.triage_result, "fail")

    # ── ABORTED + Skip 분류 FAILURE → 'skip' ──
    def test_aborted_plus_skip_is_skip_property(self):
        run = self._run([
            (TestSuiteJobRun.Status.ABORTED, ""),
            (TestSuiteJobRun.Status.FAILURE, "skip"),
        ])
        self.assertEqual(run.triage_result, "skip")

    def test_aborted_plus_skip_is_skip_annotated(self):
        run = self._run([
            (TestSuiteJobRun.Status.ABORTED, ""),
            (TestSuiteJobRun.Status.FAILURE, "skip"),
        ])
        ann = TestSuiteRun.annotate_triage(TestSuiteRun.objects.filter(pk=run.pk)).first()
        self.assertEqual(ann.real_failed, 0)
        self.assertEqual(ann.triage_result, "skip")

    # ── ABORTED 인데 사람이 Failure 로 명시 확정 → 장애('fail') ──
    # Run 상세의 '🔴 FAILURE — 실제 장애로 분류' 배지와 집계가 모순되지 않아야 한다.
    def test_aborted_with_failure_verdict_is_fail_property(self):
        run = self._run([(TestSuiteJobRun.Status.ABORTED, "failure")])
        self.assertEqual(run.triage_result, "fail")

    def test_aborted_with_failure_verdict_is_fail_annotated(self):
        run = self._run([(TestSuiteJobRun.Status.ABORTED, "failure")])
        ann = TestSuiteRun.annotate_triage(TestSuiteRun.objects.filter(pk=run.pk)).first()
        self.assertEqual(ann.real_failed, 1)
        self.assertEqual(ann.triage_result, "fail")

    # ── 미분류 ABORTED 의 job 단위 라벨은 'Fail(판정 대기)' 가 아니라 'Aborted' ──
    def test_untriaged_aborted_result_label_is_aborted(self):
        run = self._run([(TestSuiteJobRun.Status.ABORTED, "")])
        self.assertEqual(run.job_runs.first().result_label, "Aborted")


class RunListAbortedExclusionTests(TestCase):
    """suite_run_list f_result=fail/skip — 전부-중단(aborted) run 은 어느 필터에도 안 걸린다."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="abt_ops", password="pw", role="qpe_ops")
        self.client.force_login(self.user)
        self.suite = TestSuite.objects.create(name="AbtSuite", jenkins_folder_path="abt/p")
        self._n = 0

    def _run(self, job_specs):
        self._n += 1
        n = self._n
        failed = sum(
            1 for st, _ in job_specs
            if st in (TestSuiteJobRun.Status.FAILURE, TestSuiteJobRun.Status.ABORTED)
        )
        run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=len(job_specs), failed_jobs=failed,
            started_at=timezone.now(), completed_at=timezone.now())
        for i, (st, verdict) in enumerate(job_specs):
            job = TestSuiteJob.objects.create(
                suite=self.suite, job_name=f"abtj{n}-{i}", job_full_path=f"abt/j{n}-{i}")
            TestSuiteJobRun.objects.create(suite_run=run, job=job, status=st, verdict=verdict)
        return run

    def test_all_aborted_run_excluded_from_fail_filter(self):
        aborted_run = self._run([(TestSuiteJobRun.Status.ABORTED, "")])
        ids = [r.pk for r in self.client.get("/suites/runs/?result=fail").context["runs"]]
        self.assertNotIn(aborted_run.pk, ids)

    def test_all_aborted_run_excluded_from_skip_filter(self):
        aborted_run = self._run([(TestSuiteJobRun.Status.ABORTED, "")])
        ids = [r.pk for r in self.client.get("/suites/runs/?result=skip").context["runs"]]
        self.assertNotIn(aborted_run.pk, ids)

    def test_mixed_aborted_and_failure_included_in_fail_filter(self):
        run = self._run([
            (TestSuiteJobRun.Status.ABORTED, ""),
            (TestSuiteJobRun.Status.FAILURE, ""),
        ])
        ids = [r.pk for r in self.client.get("/suites/runs/?result=fail").context["runs"]]
        self.assertIn(run.pk, ids)


# =====================================================================
# UI Test Automation (TAP 연동) — docs/prd_ui_test_automation.md
# 모든 TAP 호출은 mock. 실제 TAP(tapilot.jkintra.net) 호출 금지.
# =====================================================================

class SyncTapSuitesTests(TestCase):
    """services_tap.sync_tap_suites — upsert / 제거 감지 / 장애·미설정 격리."""

    @staticmethod
    def _client(items=None, configured=True, error=None):
        c = mock.Mock()
        c.configured = configured
        if error is not None:
            c.list_suites.side_effect = error
        else:
            c.list_suites.return_value = items or []
        return c

    def test_creates_new_suite(self):
        client = self._client(items=[{
            "id": 101, "name": "Suite A", "description": "d",
            "owner_display": "홍길동", "project_id": 5, "project_name": "JK",
            "milestone_name": "M1", "case_ids": [1, 2, 3],
        }])
        n = sync_tap_suites(client)
        self.assertEqual(n, 1)
        suite = TapSuite.objects.get(tap_id=101)
        self.assertEqual(suite.name, "Suite A")
        self.assertEqual(suite.case_count, 3)
        self.assertFalse(suite.is_removed)
        self.assertIsNotNone(suite.synced_at)

    def test_updates_existing_suite(self):
        TapSuite.objects.create(tap_id=101, name="Old", case_count=1)
        client = self._client(items=[{"id": 101, "name": "New", "case_ids": [1, 2]}])
        n = sync_tap_suites(client)
        self.assertEqual(n, 1)
        suite = TapSuite.objects.get(tap_id=101)
        self.assertEqual(suite.name, "New")
        self.assertEqual(suite.case_count, 2)

    def test_marks_missing_suite_as_removed_without_deleting(self):
        stays = TapSuite.objects.create(tap_id=101, name="Stays", case_count=1)
        gone = TapSuite.objects.create(tap_id=202, name="Gone", case_count=1)
        client = self._client(items=[{"id": 101, "name": "Stays", "case_ids": [1]}])
        sync_tap_suites(client)
        gone.refresh_from_db()
        stays.refresh_from_db()
        self.assertTrue(gone.is_removed)
        self.assertFalse(stays.is_removed)
        self.assertTrue(TapSuite.objects.filter(pk=gone.pk).exists())  # 삭제 아님 — 이력 보존

    def test_tap_error_returns_neg_one_and_keeps_existing_data(self):
        existing = TapSuite.objects.create(tap_id=101, name="Keep", case_count=5)
        client = self._client(error=TapError("boom"))
        n = sync_tap_suites(client)
        self.assertEqual(n, -1)
        existing.refresh_from_db()
        self.assertEqual(existing.name, "Keep")
        self.assertFalse(existing.is_removed)

    def test_not_configured_returns_neg_one_without_calling_tap(self):
        client = self._client(configured=False)
        n = sync_tap_suites(client)
        self.assertEqual(n, -1)
        client.list_suites.assert_not_called()


class TriggerTapSuiteTests(TestCase):
    """services_tap.trigger_tap_suite — TapRun 생성(session_id/status/excluded/total_cases)."""

    def setUp(self):
        self.tap_suite = TapSuite.objects.create(tap_id=1, name="S", case_count=19)
        self.user = get_user_model().objects.create_user(
            username="tapper", password="pw", role="qpe_ops")

    def _trigger(self, data):
        with mock.patch("apps.suites.services_tap.TapClient") as MockClient:
            MockClient.return_value.run_suite.return_value = data
            return trigger_tap_suite(self.tap_suite, user=self.user)

    def test_creates_run_queued(self):
        run = self._trigger({
            "session_id": 555, "queued": True, "run_env": "dev",
            "excluded_case_ids": [3], "tap_url": "https://tapilot.example/x",
        })
        self.assertEqual(run.session_id, 555)
        self.assertEqual(run.status, TapRun.Status.QUEUED)
        self.assertEqual(run.run_env, "dev")
        self.assertEqual(run.excluded_case_ids, [3])
        self.assertEqual(run.total_cases, 19)  # suite.case_count 스냅샷
        self.assertEqual(run.triggered_by, self.user)

    def test_not_queued_is_pending(self):
        run = self._trigger({"session_id": 556, "queued": False})
        self.assertEqual(run.status, TapRun.Status.PENDING)


class PollTapRunTests(TestCase):
    """services_tap.poll_tap_run / _apply_status — 진행 갱신·종결 저장·장애 격리."""

    def setUp(self):
        self.tap_suite = TapSuite.objects.create(tap_id=1, name="S", case_count=19)
        self.run = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=900, status=TapRun.Status.RUNNING,
            total_cases=19)

    @staticmethod
    def _client(status_payload=None, detail_payload=None, status_error=None, detail_error=None):
        c = mock.Mock()
        if status_error is not None:
            c.run_status.side_effect = status_error
        else:
            c.run_status.return_value = (status_payload, 5)
        if detail_error is not None:
            c.run_detail.side_effect = detail_error
        elif detail_payload is not None:
            c.run_detail.return_value = detail_payload
        return c

    def test_running_updates_progress_without_finishing(self):
        client = self._client({
            "status": "running", "terminal": False,
            "completed_cases": 5, "passed": 4, "failed": 1, "others": 0,
        })
        poll_tap_run(self.run, client)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")
        self.assertEqual(self.run.completed_cases, 5)
        self.assertEqual(self.run.passed, 4)
        self.assertIsNone(self.run.finished_at)
        client.run_detail.assert_not_called()

    def test_terminal_sets_finished_at_and_saves_cases(self):
        client = self._client(
            {"status": "completed", "terminal": True, "completed_cases": 19,
             "passed": 18, "failed": 1, "others": 0},
            detail_payload={"cases": [{"result_id": 1, "title": "t1", "status": "passed"}]},
        )
        poll_tap_run(self.run, client)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "completed")
        self.assertIsNotNone(self.run.finished_at)
        self.assertEqual(
            self.run.raw_cases_json,
            [{"result_id": 1, "title": "t1", "status": "passed"}],
        )

    def test_status_tap_error_leaves_run_unchanged(self):
        client = self._client(status_error=TapError("down"))
        before_status = self.run.status
        poll_tap_run(self.run, client)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, before_status)
        self.assertIsNone(self.run.finished_at)

    def test_detail_tap_error_keeps_status_update_but_no_cases(self):
        client = self._client(
            {"status": "completed", "terminal": True, "completed_cases": 19},
            detail_error=TapError("down"),
        )
        poll_tap_run(self.run, client)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "completed")  # 상태 갱신은 반영됨
        self.assertEqual(self.run.raw_cases_json, [])  # 상세 조회 실패 → 케이스는 비어있음


class PollActiveTapRunsTests(TestCase):
    """services_tap.poll_active_tap_runs — 진행 중 run 만 폴링, 개별 예외 격리."""

    def setUp(self):
        self.tap_suite = TapSuite.objects.create(tap_id=1, name="S", case_count=5)

    def test_not_configured_returns_zero(self):
        with mock.patch("apps.suites.services_tap.TapClient") as MockClient:
            MockClient.return_value.configured = False
            n = poll_active_tap_runs()
        self.assertEqual(n, 0)

    def test_polls_only_active_statuses(self):
        active = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=1, status=TapRun.Status.RUNNING)
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=2, status=TapRun.Status.COMPLETED)
        with mock.patch("apps.suites.services_tap.TapClient") as MockClient, \
                mock.patch("apps.suites.services_tap.poll_tap_run") as mock_poll:
            MockClient.return_value.configured = True
            n = poll_active_tap_runs()
        self.assertEqual(n, 1)
        polled_pks = [c.args[0].pk for c in mock_poll.call_args_list]
        self.assertEqual(polled_pks, [active.pk])

    def test_one_run_exception_does_not_block_others(self):
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=1, status=TapRun.Status.RUNNING)
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=2, status=TapRun.Status.QUEUED)
        with mock.patch("apps.suites.services_tap.TapClient") as MockClient, \
                mock.patch(
                    "apps.suites.services_tap.poll_tap_run",
                    side_effect=[Exception("boom"), None],
                ) as mock_poll:
            MockClient.return_value.configured = True
            n = poll_active_tap_runs()
        self.assertEqual(n, 2)  # 하나가 예외여도 나머지는 계속 폴링
        self.assertEqual(mock_poll.call_count, 2)


class CancelTapRunTests(TestCase):
    """services_tap.cancel_tap_run — TAP 취소 호출 후 즉시 1회 폴링해 상태 반영."""

    def test_cancel_calls_client_and_polls(self):
        tap_suite = TapSuite.objects.create(tap_id=1, name="S", case_count=1)
        run = TapRun.objects.create(
            tap_suite=tap_suite, session_id=42, status=TapRun.Status.RUNNING)
        with mock.patch("apps.suites.services_tap.TapClient") as MockClient:
            instance = MockClient.return_value
            instance.cancel_run.return_value = {"status": "stopped"}
            instance.run_status.return_value = ({"status": "stopped", "terminal": True}, 5)
            instance.run_detail.return_value = {"cases": []}
            cancel_tap_run(run)
        instance.cancel_run.assert_called_once_with(42)
        run.refresh_from_db()
        self.assertEqual(run.status, "stopped")
        self.assertIsNotNone(run.finished_at)


class TapRunPropertiesTests(SimpleTestCase):
    """TapRun.result_label / progress_percent / is_active — 단위 케이스(미저장 인스턴스)."""

    @staticmethod
    def _run(status, total=0, completed=0, failed=0):
        return TapRun(status=status, total_cases=total, completed_cases=completed, failed=failed)

    def test_queued_label_is_waiting(self):
        self.assertEqual(self._run(TapRun.Status.QUEUED).result_label, "대기")

    def test_pending_label_is_waiting(self):
        self.assertEqual(self._run(TapRun.Status.PENDING).result_label, "대기")

    def test_running_label(self):
        self.assertEqual(self._run(TapRun.Status.RUNNING).result_label, "실행 중")

    def test_stopped_label_is_aborted(self):
        self.assertEqual(self._run(TapRun.Status.STOPPED).result_label, "Aborted")

    def test_error_label_is_fail(self):
        self.assertEqual(self._run(TapRun.Status.ERROR).result_label, "Fail")

    def test_completed_no_failure_is_pass(self):
        r = self._run(TapRun.Status.COMPLETED, total=10, completed=10, failed=0)
        self.assertEqual(r.result_label, "Pass")

    def test_completed_with_failure_is_fail(self):
        r = self._run(TapRun.Status.COMPLETED, total=10, completed=10, failed=1)
        self.assertEqual(r.result_label, "Fail")

    def test_progress_percent_zero_total(self):
        self.assertEqual(self._run(TapRun.Status.QUEUED, total=0, completed=0).progress_percent, 0)

    def test_progress_percent_partial(self):
        r = self._run(TapRun.Status.RUNNING, total=19, completed=5)
        self.assertEqual(r.progress_percent, round(5 / 19 * 100))

    def test_progress_percent_capped_at_100(self):
        r = self._run(TapRun.Status.RUNNING, total=10, completed=15)  # 방어적 케이스(초과 보고)
        self.assertEqual(r.progress_percent, 100)

    def test_is_active_true_for_queued_pending_running(self):
        for st in (TapRun.Status.QUEUED, TapRun.Status.PENDING, TapRun.Status.RUNNING):
            self.assertTrue(self._run(st).is_active, msg=st)

    def test_is_active_false_for_terminal_statuses(self):
        for st in (TapRun.Status.COMPLETED, TapRun.Status.STOPPED, TapRun.Status.ERROR):
            self.assertFalse(self._run(st).is_active, msg=st)


class SuiteListTapTabTests(TestCase):
    """suite_list — ?tab=ui 컨텍스트(active_tab/tap_suites/tap_any_active)."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="tabviewer", password="pw", role="qpe_ops")
        self.client.force_login(self.user)
        self.tap_suite = TapSuite.objects.create(
            tap_id=1, name="[JK] Regression Test (PC)", case_count=19,
            synced_at=timezone.now())
        self.removed = TapSuite.objects.create(
            tap_id=2, name="Removed", case_count=1, is_removed=True)

    def test_default_tab_is_api(self):
        resp = self.client.get("/suites/")
        self.assertEqual(resp.context["active_tab"], "api")

    def test_tab_ui_sets_active_tab_and_context(self):
        resp = self.client.get("/suites/?tab=ui")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["active_tab"], "ui")
        tap_suites = list(resp.context["tap_suites"])
        self.assertIn(self.tap_suite, tap_suites)
        self.assertNotIn(self.removed, tap_suites)  # is_removed 는 숨김

    def test_tap_any_active_reflects_running_run(self):
        resp = self.client.get("/suites/?tab=ui")
        self.assertFalse(resp.context["tap_any_active"])
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=1, status=TapRun.Status.RUNNING)
        resp = self.client.get("/suites/?tab=ui")
        self.assertTrue(resp.context["tap_any_active"])


class TapSuiteRunViewTests(TestCase):
    """tap_suite_run — POST 전용, can_write 필요, 성공 시 TapRun 생성+ActivityLog."""

    def setUp(self):
        self.tap_suite = TapSuite.objects.create(tap_id=1, name="S", case_count=19)
        self.viewer = get_user_model().objects.create_user(
            username="viewerx", password="pw", role="viewer")
        self.writer = get_user_model().objects.create_user(
            username="writerx", password="pw", role="qpe_ops")

    def test_viewer_cannot_trigger(self):
        self.client.force_login(self.viewer)
        resp = self.client.post(f"/suites/tap/{self.tap_suite.pk}/run/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(TapRun.objects.filter(tap_suite=self.tap_suite).exists())

    def test_writer_trigger_creates_run_and_activity_log(self):
        self.client.force_login(self.writer)
        with mock.patch("apps.suites.services_tap.TapClient") as MockClient:
            MockClient.return_value.run_suite.return_value = {
                "session_id": 777, "queued": True, "run_env": "dev",
                "excluded_case_ids": [], "tap_url": "https://x",
            }
            resp = self.client.post(f"/suites/tap/{self.tap_suite.pk}/run/")
        self.assertEqual(resp.status_code, 302)
        run = TapRun.objects.get(tap_suite=self.tap_suite)
        self.assertEqual(run.session_id, 777)
        self.assertTrue(
            ActivityLog.objects.filter(
                action=ActivityLog.Action.RUN,
                target_type=ActivityLog.TargetType.SUITE_RUN,
                target_id=str(run.pk),
            ).exists()
        )

    def test_tap_error_shows_message_and_no_run_created(self):
        self.client.force_login(self.writer)
        with mock.patch("apps.suites.services_tap.TapClient") as MockClient:
            MockClient.return_value.run_suite.side_effect = TapError("연결 실패")
            resp = self.client.post(f"/suites/tap/{self.tap_suite.pk}/run/", follow=True)
        self.assertFalse(TapRun.objects.filter(tap_suite=self.tap_suite).exists())
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("TAP 실행 실패" in m for m in msgs))

    def test_get_is_not_allowed_and_does_not_trigger(self):
        self.client.force_login(self.writer)
        resp = self.client.get(f"/suites/tap/{self.tap_suite.pk}/run/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(TapRun.objects.filter(tap_suite=self.tap_suite).exists())


class TapRunCancelViewTests(TestCase):
    """tap_run_cancel — can_write 필요, 성공 시 상태 반영+ActivityLog."""

    def setUp(self):
        self.tap_suite = TapSuite.objects.create(tap_id=1, name="S", case_count=1)
        self.viewer = get_user_model().objects.create_user(
            username="viewerc", password="pw", role="viewer")
        self.writer = get_user_model().objects.create_user(
            username="writerc", password="pw", role="qpe_ops")
        self.run = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=10, status=TapRun.Status.RUNNING)

    def test_viewer_cannot_cancel(self):
        self.client.force_login(self.viewer)
        resp = self.client.post(f"/suites/tap-runs/{self.run.pk}/cancel/")
        self.assertEqual(resp.status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, TapRun.Status.RUNNING)

    def test_writer_cancel_updates_status_and_logs(self):
        self.client.force_login(self.writer)
        with mock.patch("apps.suites.services_tap.TapClient") as MockClient:
            instance = MockClient.return_value
            instance.cancel_run.return_value = {}
            instance.run_status.return_value = ({"status": "stopped", "terminal": True}, 5)
            instance.run_detail.return_value = {"cases": []}
            resp = self.client.post(f"/suites/tap-runs/{self.run.pk}/cancel/")
        self.assertEqual(resp.status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "stopped")
        self.assertTrue(
            ActivityLog.objects.filter(
                action=ActivityLog.Action.CANCEL,
                target_type=ActivityLog.TargetType.SUITE_RUN,
                target_id=str(self.run.pk),
            ).exists()
        )


class TapRunDetailViewTests(TestCase):
    """tap_run_detail — 활성 run 은 열람 시점에 1회 폴링, 응답 200 + cases 컨텍스트."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="detailv", password="pw", role="qpe_ops")
        self.client.force_login(self.user)
        self.tap_suite = TapSuite.objects.create(tap_id=1, name="S", case_count=1)

    def test_active_run_polls_before_render(self):
        run = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=1, status=TapRun.Status.RUNNING)
        with mock.patch("apps.suites.services_tap.poll_tap_run") as mock_poll:
            resp = self.client.get(f"/suites/tap-runs/{run.pk}/")
        mock_poll.assert_called_once()
        self.assertEqual(resp.status_code, 200)

    def test_inactive_run_skips_poll(self):
        run = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=2, status=TapRun.Status.COMPLETED,
            raw_cases_json=[{"result_id": 1, "title": "t"}])
        with mock.patch("apps.suites.services_tap.poll_tap_run") as mock_poll:
            resp = self.client.get(f"/suites/tap-runs/{run.pk}/")
        mock_poll.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["cases"], [{"result_id": 1, "title": "t"}])


class _FakeSyncJenkinsClient:
    """services_sync.sync_api_suites 용 fake — 폴더 트리(list_children)와 워커 목록
    (list_folder_jobs)을 딕셔너리로 흉내낸다. 실패를 흉내내려면 값 대신 예외 인스턴스를 둔다.
    """

    def __init__(self, children_by_folder=None, jobs_by_path=None):
        self.children_by_folder = children_by_folder or {}
        self.jobs_by_path = jobs_by_path or {}
        self.list_children_calls = []
        self.list_folder_jobs_calls = []

    def list_children(self, folder_full_name=""):
        self.list_children_calls.append(folder_full_name)
        result = self.children_by_folder.get(folder_full_name, [])
        if isinstance(result, Exception):
            raise result
        return result

    def list_folder_jobs(self, folder_full_name):
        self.list_folder_jobs_calls.append(folder_full_name)
        result = self.jobs_by_path.get(folder_full_name, [])
        if isinstance(result, Exception):
            raise result
        return result


def _job(name, path):
    return {"name": name, "full_path": path, "url": "", "color": "blue",
            "buildable": True, "last_build_number": 1, "last_build_result": "SUCCESS",
            "last_build_duration": 100, "total_stages": 3}


class SyncApiSuitesTests(TestCase):
    """services_sync.sync_api_suites — 'api' 폴더/잡 탐색 → 미등록 Suite 자동 생성."""

    def test_creates_new_suite_for_matching_root_folder(self):
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "": [{"name": "foo-api-dev", "full_path": "foo-api-dev", "url": "u", "is_folder": True}],
            },
            jobs_by_path={"foo-api-dev": [_job("worker1", "foo-api-dev/worker1")]},
        )
        result = sync_api_suites(client)
        self.assertEqual(result, {"created": 1, "synced": 1, "candidates": ["foo-api-dev"]})
        suite = TestSuite.objects.get(jenkins_folder_path="foo-api-dev")
        self.assertEqual(suite.jenkins_base_url, "")
        self.assertEqual(suite.jenkins_folder_url, "u")
        self.assertEqual(suite.jobs.count(), 1)
        self.assertEqual(suite.jobs.first().job_full_path, "foo-api-dev/worker1")

    def test_skips_already_registered_suite_without_calling_list_folder_jobs(self):
        TestSuite.objects.create(name="foo-api-dev", jenkins_folder_path="foo-api-dev")
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "": [{"name": "foo-api-dev", "full_path": "foo-api-dev", "url": "u", "is_folder": True}],
            },
        )
        result = sync_api_suites(client)
        self.assertEqual(result, {"created": 0, "synced": 0, "candidates": []})
        self.assertEqual(TestSuite.objects.filter(jenkins_folder_path="foo-api-dev").count(), 1)
        self.assertEqual(client.list_folder_jobs_calls, [])

    def test_ignores_non_matching_folder_names(self):
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "": [{"name": "foo-ui-dev", "full_path": "foo-ui-dev", "url": "", "is_folder": True}],
            },
        )
        result = sync_api_suites(client)
        self.assertEqual(result["created"], 0)
        self.assertFalse(TestSuite.objects.filter(jenkins_folder_path="foo-ui-dev").exists())

    def test_recurses_into_non_matching_folder_to_find_nested_match(self):
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "": [{"name": "group", "full_path": "group", "url": "", "is_folder": True}],
                "group": [{"name": "bar-api-dev", "full_path": "group/bar-api-dev", "url": "", "is_folder": True}],
            },
            jobs_by_path={"group/bar-api-dev": [_job("w", "group/bar-api-dev/w")]},
        )
        result = sync_api_suites(client)
        self.assertEqual(result["created"], 1)
        self.assertTrue(TestSuite.objects.filter(jenkins_folder_path="group/bar-api-dev").exists())

    def test_matching_folder_with_no_jobs_recurses_deeper_instead_of_creating(self):
        # 'api' 가 들어간 그룹 폴더인데 자체는 워커가 없고, 한 단계 더 안에 실제 Suite 가 있는 경우.
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "": [{"name": "api-group", "full_path": "api-group", "url": "", "is_folder": True}],
                "api-group": [{"name": "real-api-suite", "full_path": "api-group/real-api-suite",
                                "url": "", "is_folder": True}],
            },
            jobs_by_path={
                "api-group": [],  # 그룹 폴더 자체는 워커 없음
                "api-group/real-api-suite": [_job("w", "api-group/real-api-suite/w")],
            },
        )
        result = sync_api_suites(client)
        self.assertEqual(result["created"], 1)
        self.assertFalse(TestSuite.objects.filter(jenkins_folder_path="api-group").exists())
        self.assertTrue(TestSuite.objects.filter(jenkins_folder_path="api-group/real-api-suite").exists())

    def test_leaf_job_with_no_children_is_not_created_and_not_recursed(self):
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "": [{"name": "lone-api-job", "full_path": "lone-api-job", "url": "", "is_folder": False}],
            },
            jobs_by_path={"lone-api-job": []},
        )
        result = sync_api_suites(client)
        self.assertEqual(result["created"], 0)
        self.assertEqual(client.list_children_calls, [""])  # 재귀 안 함(폴더 아님)

    def test_dry_run_reports_candidates_without_writing(self):
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "": [{"name": "foo-api-dev", "full_path": "foo-api-dev", "url": "", "is_folder": True}],
            },
            jobs_by_path={"foo-api-dev": [_job("worker1", "foo-api-dev/worker1")]},
        )
        result = sync_api_suites(client, dry_run=True)
        self.assertEqual(result, {"created": 0, "synced": 0, "candidates": ["foo-api-dev"]})
        self.assertFalse(TestSuite.objects.filter(jenkins_folder_path="foo-api-dev").exists())

    def test_jenkins_error_on_one_branch_does_not_abort_whole_scan(self):
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "": [
                    {"name": "broken", "full_path": "broken", "url": "", "is_folder": True},
                    {"name": "foo-api-dev", "full_path": "foo-api-dev", "url": "", "is_folder": True},
                ],
                "broken": JenkinsError("connection failed"),
            },
            jobs_by_path={"foo-api-dev": [_job("w", "foo-api-dev/w")]},
        )
        result = sync_api_suites(client)
        self.assertEqual(result["created"], 1)
        self.assertTrue(TestSuite.objects.filter(jenkins_folder_path="foo-api-dev").exists())

    def test_env_roots_are_scanned_in_addition_to_registered_parents(self):
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "external_group": [
                    {"name": "ext-api-dev", "full_path": "external_group/ext-api-dev",
                     "url": "", "is_folder": True},
                ],
            },
            jobs_by_path={"external_group/ext-api-dev": [_job("w", "external_group/ext-api-dev/w")]},
        )
        with mock.patch.dict("os.environ", {"JENKINS_API_SYNC_ROOTS": "external_group"}):
            result = sync_api_suites(client)
        self.assertEqual(result["created"], 1)
        self.assertTrue(
            TestSuite.objects.filter(jenkins_folder_path="external_group/ext-api-dev").exists()
        )

    def test_second_run_is_idempotent(self):
        client = _FakeSyncJenkinsClient(
            children_by_folder={
                "": [{"name": "foo-api-dev", "full_path": "foo-api-dev", "url": "", "is_folder": True}],
            },
            jobs_by_path={"foo-api-dev": [_job("worker1", "foo-api-dev/worker1")]},
        )
        first = sync_api_suites(client)
        second = sync_api_suites(client)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(TestSuite.objects.filter(jenkins_folder_path="foo-api-dev").count(), 1)


class SuitesApiSyncViewTests(TestCase):
    """views.suites_api_sync — admin 전용 [Jenkins 싱크] 버튼. 결과 메시지 + ActivityLog."""

    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="syncadmin", password="pw", role="qpe_admin")
        self.regular = get_user_model().objects.create_user(
            username="syncuser", password="pw", role="qpe_ops")

    def test_non_admin_is_rejected_without_calling_sync(self):
        self.client.force_login(self.regular)
        with mock.patch("apps.suites.views.sync_api_suites") as mock_sync:
            resp = self.client.post("/suites/api-sync/")
        mock_sync.assert_not_called()
        self.assertEqual(resp.status_code, 302)

    def test_admin_post_triggers_sync_and_logs_activity(self):
        self.client.force_login(self.admin)
        with mock.patch(
            "apps.suites.views.sync_api_suites",
            return_value={"created": 2, "synced": 2, "candidates": ["a", "b"]},
        ) as mock_sync:
            resp = self.client.post("/suites/api-sync/", follow=True)
        mock_sync.assert_called_once()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            ActivityLog.objects.filter(
                action=ActivityLog.Action.UPDATE,
                target_type=ActivityLog.TargetType.SUITE,
                target_name="API 탭 Jenkins 자동 싱크",
            ).exists()
        )

    def test_admin_get_does_not_trigger_sync(self):
        self.client.force_login(self.admin)
        with mock.patch("apps.suites.views.sync_api_suites") as mock_sync:
            resp = self.client.get("/suites/api-sync/")
        mock_sync.assert_not_called()
        self.assertEqual(resp.status_code, 302)


class TapRunSlackNotifyTests(TestCase):
    """TAP Run Slack 알림 훅 — 시작/종결 전이 시에만 발송, stopped 는 silent."""

    def setUp(self):
        self.suite = TapSuite.objects.create(
            tap_id=901, name="notify-ui-suite", case_count=3)
        self.run = TapRun.objects.create(
            tap_suite=self.suite, session_id=9001,
            status=TapRun.Status.RUNNING, total_cases=3)

    def _poll_with(self, status_payload):
        fake = mock.MagicMock()
        fake.configured = True
        fake.run_status.return_value = (status_payload, 0)
        fake.run_detail.return_value = {"cases": []}
        from .services_tap import poll_tap_run
        with mock.patch("apps.notifications.services.send_tap_run_event_async") as sent:
            poll_tap_run(self.run, fake)
        return sent

    def test_completed_all_pass_sends_passed(self):
        sent = self._poll_with({"status": "completed", "terminal": True,
                                "total_cases": 3, "completed_cases": 3,
                                "passed": 3, "failed": 0, "others": 0})
        from apps.notifications import events as nev
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[0], nev.TAP_RUN_PASSED)

    def test_completed_with_fail_sends_failed(self):
        sent = self._poll_with({"status": "completed", "terminal": True,
                                "total_cases": 3, "completed_cases": 3,
                                "passed": 2, "failed": 1, "others": 0})
        from apps.notifications import events as nev
        self.assertEqual(sent.call_args.args[0], nev.TAP_RUN_FAILED)

    def test_error_sends_failed(self):
        sent = self._poll_with({"status": "error", "terminal": True,
                                "total_cases": 3, "completed_cases": 1,
                                "passed": 1, "failed": 0, "others": 0})
        from apps.notifications import events as nev
        self.assertEqual(sent.call_args.args[0], nev.TAP_RUN_FAILED)

    def test_stopped_is_silent(self):
        sent = self._poll_with({"status": "stopped", "terminal": True,
                                "total_cases": 3, "completed_cases": 1,
                                "passed": 1, "failed": 0, "others": 0})
        sent.assert_not_called()

    def test_already_terminal_no_resend(self):
        """이미 terminal 인 run 재폴링 시 알림 재발송 없음 (전이 1회 원칙)."""
        self.run.status = TapRun.Status.COMPLETED
        self.run.save(update_fields=["status"])
        sent = self._poll_with({"status": "completed", "terminal": True,
                                "total_cases": 3, "completed_cases": 3,
                                "passed": 3, "failed": 0, "others": 0})
        sent.assert_not_called()

    def test_trigger_sends_started(self):
        fake_client = mock.MagicMock()
        fake_client.run_suite.return_value = {
            "session_id": 9002, "queued": False, "run_env": "DEV",
            "excluded_case_ids": [], "tap_url": "https://tap/x"}
        from .services_tap import trigger_tap_suite
        with mock.patch("apps.suites.services_tap.TapClient", return_value=fake_client), \
             mock.patch("apps.notifications.services.send_tap_run_event_async") as sent:
            trigger_tap_suite(self.suite)
        from apps.notifications import events as nev
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[0], nev.TAP_RUN_STARTED)


class TapRetryTests(TestCase):
    """TAP Suite 재시도 — completed+실패만 재시도, 중간 실패 미발송, 최종만 집계/알림."""

    def setUp(self):
        self.suite = TapSuite.objects.create(
            tap_id=902, name="retry-ui-suite", case_count=3, retry_count=2)
        self.run = TapRun.objects.create(
            tap_suite=self.suite, session_id=9101,
            status=TapRun.Status.RUNNING, total_cases=3, attempt=1)

    def _poll_terminal(self, status, passed, failed, retrigger_ok=True):
        fake = mock.MagicMock()
        fake.configured = True
        fake.run_status.return_value = ({
            "status": status, "terminal": True, "total_cases": 3,
            "completed_cases": 3, "passed": passed, "failed": failed, "others": 0,
        }, 0)
        fake.run_detail.return_value = {"cases": []}
        trigger_client = mock.MagicMock()
        if retrigger_ok:
            trigger_client.run_suite.return_value = {
                "session_id": 9102, "queued": False, "run_env": "DEV",
                "excluded_case_ids": [], "tap_url": ""}
        else:
            from apps.integrations.tapilot import TapError
            trigger_client.run_suite.side_effect = TapError("down")
        from .services_tap import poll_tap_run
        with mock.patch("apps.suites.services_tap.TapClient", return_value=trigger_client), \
             mock.patch("apps.notifications.services.send_tap_run_event_async") as sent:
            poll_tap_run(self.run, fake)
        return sent

    def test_failed_run_retried_without_fail_notify(self):
        sent = self._poll_terminal("completed", passed=2, failed=1)
        retry = TapRun.objects.get(session_id=9102)
        self.assertEqual(retry.attempt, 2)
        self.assertEqual(retry.retry_origin_id, self.run.pk)
        sent.assert_not_called()  # 중간 실패 FAIL 미발송 + 재시도 STARTED 미발송
        self.assertTrue(ActivityLog.objects.filter(
            action=ActivityLog.Action.RETRY, target_name="[TAP] retry-ui-suite").exists())

    def test_final_attempt_failure_notifies(self):
        self.run.attempt = 2  # == retry_count → 더 재시도 없음
        self.run.save(update_fields=["attempt"])
        sent = self._poll_terminal("completed", passed=2, failed=1)
        self.assertFalse(TapRun.objects.filter(session_id=9102).exists())
        from apps.notifications import events as nev
        self.assertEqual(sent.call_args.args[0], nev.TAP_RUN_FAILED)

    def test_error_run_not_retried(self):
        sent = self._poll_terminal("error", passed=1, failed=0)
        self.assertFalse(TapRun.objects.filter(session_id=9102).exists())
        from apps.notifications import events as nev
        self.assertEqual(sent.call_args.args[0], nev.TAP_RUN_FAILED)

    def test_retry_trigger_failure_falls_back_to_notify(self):
        sent = self._poll_terminal("completed", passed=2, failed=1, retrigger_ok=False)
        from apps.notifications import events as nev
        self.assertEqual(sent.call_args.args[0], nev.TAP_RUN_FAILED)

    def test_schedule_stats_count_final_attempt_only(self):
        from apps.robustness.models import RobustnessSchedule, RobustnessTest
        rt = RobustnessTest.objects.create(name="RT-tapretry")
        sched = RobustnessSchedule.objects.create(
            robustness_test=rt, tap_suite=self.suite, interval_minutes=60,
            is_running=True, started_at=timezone.now() - timezone.timedelta(hours=1))
        first = TapRun.objects.create(
            tap_suite=self.suite, session_id=9201, status=TapRun.Status.COMPLETED,
            total_cases=3, completed_cases=3, passed=2, failed=1, attempt=1,
            robustness_schedule=sched)
        TapRun.objects.create(
            tap_suite=self.suite, session_id=9202, status=TapRun.Status.COMPLETED,
            total_cases=3, completed_cases=3, passed=3, failed=0, attempt=2,
            retry_origin=first, robustness_schedule=sched)
        s = RobustnessSchedule.objects.get(pk=sched.pk)
        self.assertEqual(s.success_count, 3)   # 최종 시도만
        self.assertEqual(s.failure_count, 0)   # 중간 시도 실패 미집계
        self.assertEqual(s.success_rate, 100.0)


class TapSuiteEditViewTests(TestCase):
    """TAP Suite Edit(재시도 횟수) — admin 전용, 클램프/검증."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="tapedit_admin", password="pw", role="qpe_admin", is_active=True)
        self.ops = User.objects.create_user(
            username="tapedit_ops", password="pw", role="qpe_ops", is_active=True)
        self.suite = TapSuite.objects.create(tap_id=903, name="edit-ui-suite", case_count=5)

    def test_admin_get_renders_form(self):
        self.client.force_login(self.admin)
        resp = self.client.get(f"/suites/tap/{self.suite.pk}/edit/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "재시도 횟수")

    def test_admin_post_saves(self):
        self.client.force_login(self.admin)
        resp = self.client.post(f"/suites/tap/{self.suite.pk}/edit/", {"retry_count": "3"})
        self.assertEqual(resp.status_code, 302)
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.retry_count, 3)
        self.assertTrue(ActivityLog.objects.filter(
            action=ActivityLog.Action.UPDATE, detail="retry_count=3").exists())

    def test_zero_clamped_to_one(self):
        self.client.force_login(self.admin)
        self.client.post(f"/suites/tap/{self.suite.pk}/edit/", {"retry_count": "0"})
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.retry_count, 1)

    def test_invalid_keeps_value_with_error(self):
        self.suite.retry_count = 2
        self.suite.save(update_fields=["retry_count"])
        self.client.force_login(self.admin)
        self.client.post(f"/suites/tap/{self.suite.pk}/edit/", {"retry_count": "abc"})
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.retry_count, 2)

    def test_non_admin_blocked(self):
        self.client.force_login(self.ops)
        resp = self.client.post(f"/suites/tap/{self.suite.pk}/edit/", {"retry_count": "5"})
        self.assertEqual(resp.status_code, 302)
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.retry_count, 1)
