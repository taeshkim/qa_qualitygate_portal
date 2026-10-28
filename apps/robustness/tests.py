"""Robustness 서비스 단위 테스트.

외부 호출(Jenkins)은 mock 으로 차단.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.notifications import events as nev
from apps.suites.models import TestSuite, TestSuiteJob, TestSuiteRun
from .models import RobustnessRun, RobustnessSchedule, RobustnessTest
from . import services


User = get_user_model()


def _make_schedule(suite, interval=60, parallel=False, is_running=True):
    """테스트용 RobustnessSchedule 생성 헬퍼.

    start_schedule 로 세션까지 열어야 하는 테스트는 is_running=False 로 만들어
    실제 흐름(멈춘 스케줄 → 시작)대로 호출할 것 — start_schedule 은 멱등이라
    이미 실행 중이면 no-op 이다.
    """
    rt = RobustnessTest.objects.create(
        name=f"RT-{id(suite)}", parallel_jobs=parallel,
    )
    return RobustnessSchedule.objects.create(
        robustness_test=rt,
        test_suite=suite,
        interval_minutes=interval,
        is_running=is_running,
    )


class TriggerOnceRetryLimitSnapshotTests(TestCase):
    """`_trigger_once` 가 TestSuiteRun.retry_limit 에 suite.retry_count 를 스냅샷하는지."""

    def setUp(self):
        # retry_count=4 로 Suite 생성
        self.suite = TestSuite.objects.create(
            name="RobuSuite", jenkins_folder_path="rob/p", retry_count=4)
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="rob/p/j1", order=0)
        self.schedule = _make_schedule(self.suite)

    def test_trigger_once_snapshots_retry_count_into_suite_run(self):
        """`_trigger_once` 가 생성하는 TestSuiteRun.retry_limit == suite.retry_count."""
        fake = mock.MagicMock()
        fake.get_last_build_info.return_value = {"building": False}
        fake.trigger_job.return_value = {"build_number": 1}

        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake),
            mock.patch("apps.robustness.services.send_suite_run_event_async", create=True),
        ):
            rob_run = services._trigger_once(self.schedule)

        self.assertIsNotNone(rob_run)
        suite_run = rob_run.suite_run
        self.assertIsNotNone(suite_run)
        self.assertEqual(suite_run.retry_limit, 4)

    def test_trigger_once_retry_count_1_snapshot(self):
        """retry_count=1(기본) 인 suite → retry_limit=1."""
        self.suite.retry_count = 1
        self.suite.save()

        fake = mock.MagicMock()
        fake.get_last_build_info.return_value = {"building": False}
        fake.trigger_job.return_value = {"build_number": 1}

        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake),
            mock.patch("apps.robustness.services.send_suite_run_event_async"),
        ):
            rob_run = services._trigger_once(self.schedule)

        self.assertIsNotNone(rob_run)
        self.assertEqual(rob_run.suite_run.retry_limit, 1)

    def test_trigger_once_sends_suite_run_started(self):
        """Reliability Test 트리거도 단독 경로처럼 SUITE_RUN_STARTED 를 발송한다.

        (회귀: 과거 _trigger_once 가 START 를 안 보내 슬랙에 시작 노티가 안 가던 버그)
        """
        fake = mock.MagicMock()
        fake.get_last_build_info.return_value = {"building": False}
        fake.trigger_job.return_value = {"build_number": 1}
        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake),
            mock.patch("apps.robustness.services.send_suite_run_event_async") as m,
        ):
            rob_run = services._trigger_once(self.schedule)
        m.assert_any_call(nev.SUITE_RUN_STARTED, rob_run.suite_run)


# =============================================
# 결함#3 회귀: _update_run_status 병렬/순차 재트리거
# =============================================

from apps.suites.models import TestSuiteJobRun  # noqa: E402 — 테스트 편의 import


def _make_parallel_suite_run(suite, retry_limit, n_jobs):
    """suite_run + n_jobs 개의 QUEUED job_run 을 생성해 반환."""
    from django.utils import timezone
    from apps.suites.models import TestSuiteJobRun, TestSuiteRun

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


class UpdateRunStatusParallelRetriggerTests(TestCase):
    """결함#3 회귀: _update_run_status 가 parallel=True 일 때 재시도 PENDING 잡을 모두 재트리거.

    `_poll_run_status` 내부의 `_try_trigger_next` 는 1개만 재트리거하므로,
    parallel 스케줄에서 동시 실패 잡들이 폴링 사이클마다 1개씩 순차 재시도로
    강등되는 문제를 막기 위해 `_update_run_status` 가 `_trigger_all_pending` 을
    추가 호출하는 수정을 검증한다.
    """

    def setUp(self):
        from apps.suites.models import TestSuiteJob, TestSuiteRun as SR
        self.TestSuiteRun = SR
        self.suite = TestSuite.objects.create(
            name="ParallelRetrySuite", jenkins_folder_path="par/p", retry_count=2)
        # 이 테스트는 '병렬 모드가 모든 잡을 한 번에 재트리거' 만 검증 — 실행 지연
        # throttle 과 무관하게 동작하도록 delay=0(throttle off). throttle 의 순차화
        # 동작은 별도 ParallelTriggerDelayTests 에서 검증.
        self.job1 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="par/p/j1", order=0,
            is_active=True, is_target=True, trigger_delay_seconds=0)
        self.job2 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j2", job_full_path="par/p/j2", order=1,
            is_active=True, is_target=True, trigger_delay_seconds=0)
        self.job3 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j3", job_full_path="par/p/j3", order=2,
            is_active=True, is_target=True, trigger_delay_seconds=0)

    def _fake_jenkins(self):
        """trigger_build 와 get_last_build_info 를 응답하는 fake Jenkins."""
        fake = mock.MagicMock()
        fake.get_last_build_info.return_value = {"building": False}
        fake.trigger_job.return_value = {"build_number": 2}
        fake.get_build_info.return_value = {
            "building": False, "result": "FAILURE",
            "url": "http://j/1", "duration": 500,
        }
        fake.get_build_stages.return_value = []
        fake.get_console_output.return_value = "fail-log"
        return fake

    def test_parallel_all_pending_jobs_retriggered(self):
        """parallel=True 스케줄: 여러 잡이 FAILURE → PENDING 이면 _update_run_status 한 번에 모두 재트리거.

        3개 잡이 모두 QUEUED(build_number=1) 이고 Jenkins 에서 FAILURE 가 반환되면,
        retry_limit=2 이므로 3개 모두 attempt=2 PENDING 으로 전환된 후
        _trigger_all_pending 에 의해 다시 QUEUED 로 전환된다.
        trigger_build 호출 횟수 == 3 (잡 수 만큼) 인지 검증.
        """
        suite_run = _make_parallel_suite_run(self.suite, retry_limit=2, n_jobs=3)
        schedule = _make_schedule(self.suite, parallel=True)
        rob_run = RobustnessRun.objects.create(
            schedule=schedule,
            suite_run=suite_run,
            status=RobustnessRun.Status.RUNNING,
        )

        fake = self._fake_jenkins()

        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake),
            mock.patch("apps.suites.views.send_suite_run_event_async"),
            mock.patch("apps.robustness.services.send_suite_run_event_async", create=True),
        ):
            services._update_run_status(rob_run)

        # trigger_build 가 잡 수(3)만큼 호출됐는지 확인
        self.assertEqual(fake.trigger_job.call_count, 3,
            f"parallel 재시도에서 trigger_build 가 3회 호출돼야 하지만 {fake.trigger_job.call_count}회 호출됨")

        # 모든 job_run 이 QUEUED(또는 RUNNING) 상태인지 확인
        from apps.suites.models import TestSuiteJobRun as JR
        active_statuses = {JR.Status.QUEUED, JR.Status.RUNNING}
        for jr in suite_run.job_runs.all():
            jr.refresh_from_db()
            self.assertIn(jr.status, active_statuses,
                f"{jr.job.job_name} 의 status={jr.status} — QUEUED/RUNNING 이어야 함")

    def test_sequential_only_one_job_retriggered(self):
        """parallel=False(순차) 스케줄: FAILURE → PENDING 중 한 번에 1개만 재트리거.

        2개 잡이 QUEUED 이고 두 잡 모두 FAILURE 반환되어도,
        _try_trigger_next(1개만) 에 의해 trigger_build 호출 횟수 <= 1.
        (재시도가 일어난 잡이 1개뿐이면 trigger_build 는 최대 1회)
        """
        suite_run = _make_parallel_suite_run(self.suite, retry_limit=2, n_jobs=2)
        schedule = _make_schedule(self.suite, parallel=False)
        rob_run = RobustnessRun.objects.create(
            schedule=schedule,
            suite_run=suite_run,
            status=RobustnessRun.Status.RUNNING,
        )

        fake = self._fake_jenkins()

        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake),
            mock.patch("apps.suites.views.send_suite_run_event_async"),
            mock.patch("apps.robustness.services.send_suite_run_event_async", create=True),
        ):
            services._update_run_status(rob_run)

        # 순차 모드에서는 trigger_build 가 최대 1회만 호출돼야 한다
        self.assertLessEqual(fake.trigger_job.call_count, 1,
            f"sequential 재시도에서 trigger_build 가 최대 1회여야 하지만 {fake.trigger_job.call_count}회 호출됨")


# =============================================
# seq_no 회귀: _trigger_once 경로
# =============================================

class TriggerOnceSeqNoTests(TestCase):
    """`_trigger_once` 가 생성하는 TestSuiteRun 에 seq_no >= 1 이 부여되는지."""

    def setUp(self):
        self.suite = TestSuite.objects.create(
            name="SeqRobSuite", jenkins_folder_path="seq/rob/p", retry_count=1)
        self.job = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="seq/rob/p/j1", order=0)
        self.schedule = _make_schedule(self.suite)

    def _fake_jenkins(self):
        fake = mock.MagicMock()
        fake.get_last_build_info.return_value = {"building": False}
        fake.trigger_job.return_value = {"build_number": 1}
        return fake

    def test_trigger_once_assigns_seq_no_gte_1(self):
        """`_trigger_once` 가 생성하는 TestSuiteRun.seq_no >= 1."""
        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=self._fake_jenkins()),
            mock.patch("apps.robustness.services.send_suite_run_event_async", create=True),
        ):
            rob_run = services._trigger_once(self.schedule)

        self.assertIsNotNone(rob_run)
        self.assertGreaterEqual(rob_run.suite_run.seq_no, 1)
        self.assertEqual(rob_run.suite_run.seq_no, 1)

    def test_trigger_once_seq_no_increments(self):
        """두 번째 `_trigger_once` 호출에서 seq_no=2 가 부여된다."""
        from django.utils import timezone
        # 첫 번째 호출
        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=self._fake_jenkins()),
            mock.patch("apps.robustness.services.send_suite_run_event_async", create=True),
        ):
            rob_run1 = services._trigger_once(self.schedule)
        self.assertEqual(rob_run1.suite_run.seq_no, 1)

        # 두 번째 호출
        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=self._fake_jenkins()),
            mock.patch("apps.robustness.services.send_suite_run_event_async", create=True),
        ):
            rob_run2 = services._trigger_once(self.schedule)
        self.assertEqual(rob_run2.suite_run.seq_no, 2)


# =====================================================================
# 잡 실행 지연 throttle — 병렬 모드(_trigger_all_pending)에서도 순차 트리거
# =====================================================================

from datetime import timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402

from apps.suites.models import TestSuiteJobRun  # noqa: E402


class ParallelTriggerDelayTests(TestCase):
    """병렬 모드라도 한 sweep 에 이 suite 의 슬롯은 1개만 점유 → 나머지는 WAITING.

    잡들은 '동시'가 아니라 delay 간격으로 엇갈려 트리거되고, 먼저 트리거된 잡은
    계속 RUNNING 이라 병렬로 함께 돈다(순차=완료대기 아님)."""

    def setUp(self):
        self.suite = TestSuite.objects.create(
            name="ParSuite", jenkins_folder_path="par/p", retry_count=1)
        self.jobs = [
            TestSuiteJob.objects.create(
                suite=self.suite, job_name=f"j{i}", job_full_path=f"par/p/j{i}",
                order=i, trigger_delay_seconds=30,
            )
            for i in range(3)
        ]

    def _suite_run(self):
        sr = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.RUNNING,
            total_jobs=3, retry_limit=1, started_at=timezone.now(),
        )
        for job in self.jobs:
            TestSuiteJobRun.objects.create(
                suite_run=sr, job=job, status=TestSuiteJobRun.Status.PENDING,
            )
        return sr

    def _counts(self, sr):
        statuses = list(sr.job_runs.values_list("status", flat=True))
        return {
            "queued": statuses.count(TestSuiteJobRun.Status.QUEUED),
            "waiting": statuses.count(TestSuiteJobRun.Status.WAITING),
            "pending": statuses.count(TestSuiteJobRun.Status.PENDING),
        }

    def test_parallel_triggers_one_per_sweep(self):
        """첫 _trigger_all_pending → 1 QUEUED + 2 WAITING. delay 경과 후 1개 추가."""
        sr = self._suite_run()
        fake = mock.MagicMock()
        fake.get_last_build_info.return_value = {"building": False}
        fake.trigger_job.side_effect = (
            lambda path, gwt_token=None: {"build_number": 1}
        )
        with mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake):
            services._trigger_all_pending(sr)
            c1 = self._counts(sr)
            self.assertEqual(c1["queued"], 1)
            self.assertEqual(c1["waiting"], 2)

            # delay 경과 시뮬레이션(이 suite 의 마지막 트리거를 31초 전으로)
            # → 다음 sweep 에서 1개 더 트리거. 단, 먼저 트리거된 잡은 QUEUED 로 남아
            #   '완료를 기다리지 않고' 병렬로 함께 도는 상태.
            TestSuite.objects.filter(pk=self.suite.pk).update(
                last_job_triggered_at=timezone.now() - timedelta(seconds=31))
            services._trigger_all_pending(sr)
            c2 = self._counts(sr)
            self.assertEqual(c2["queued"], 2)   # 2개가 동시에 QUEUED(병렬)
            self.assertEqual(c2["waiting"], 1)
        # 트리거 호출은 총 2회 (병렬이지만 트리거 시점만 딜레이로 엇갈림)
        self.assertEqual(fake.trigger_job.call_count, 2)


# =====================================================================
# Reliability Test 화면 성능 최적화 — sweep 쿨다운 + _job_aggregate memoize
# =====================================================================

from django.core.cache import cache  # noqa: E402
from django.test import override_settings  # noqa: E402

from apps.suites.models import TestSuiteRun as _TSR  # noqa: E402


class SweepCooldownTests(TestCase):
    """sweep_active_schedules 가 쿨다운 창 안에서 중복 호출을 1회로 합치는지."""

    def setUp(self):
        # 쿨다운 키만 초기화 (cache.clear() 는 타 TestCase 캐시까지 지움 → 해당 키만).
        cache.delete(services._SWEEP_COOLDOWN_KEY)
        self.suite = TestSuite.objects.create(
            name="SweepSuite", jenkins_folder_path="sw/p")
        TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="sw/p/j1", order=0)
        self.schedule = _make_schedule(self.suite)  # is_running=True

    def tearDown(self):
        cache.delete(services._SWEEP_COOLDOWN_KEY)

    def test_cooldown_dedupes_rapid_calls(self):
        with mock.patch("apps.robustness.services.sweep_schedule") as m:
            services.sweep_active_schedules()
            self.assertEqual(m.call_count, 1)  # 첫 호출 → sweep
            services.sweep_active_schedules()
            self.assertEqual(m.call_count, 1)  # 쿨다운 창 내 → skip

    def test_force_bypasses_cooldown(self):
        with mock.patch("apps.robustness.services.sweep_schedule") as m:
            services.sweep_active_schedules()
            services.sweep_active_schedules(force=True)
            self.assertEqual(m.call_count, 2)  # force → 쿨다운 무시

    def test_cooldown_expiry_allows_next_sweep(self):
        with mock.patch("apps.robustness.services.sweep_schedule") as m:
            services.sweep_active_schedules()
            cache.delete(services._SWEEP_COOLDOWN_KEY)  # 창 만료 시뮬레이션
            services.sweep_active_schedules()
            self.assertEqual(m.call_count, 2)


class JobAggregateMemoizeTests(TestCase):
    """_job_aggregate 가 인스턴스 단위 memoize 로 행당 1쿼리만 쓰는지."""

    def setUp(self):
        self.suite = TestSuite.objects.create(
            name="AggSuite", jenkins_folder_path="ag/p")
        self.schedule = _make_schedule(self.suite, is_running=False)
        services.start_schedule(self.schedule)  # started_at 채워 session_runs 활성화
        # 완료된 suite_run 1건 + RobustnessRun 연결
        sr = _TSR.objects.create(
            suite=self.suite, status=_TSR.Status.COMPLETED,
            total_jobs=3, passed_jobs=2, failed_jobs=1, started_at=timezone.now())
        RobustnessRun.objects.create(
            schedule=self.schedule, suite_run=sr, status=RobustnessRun.Status.SUCCESS)

    @override_settings(DEBUG=True)
    def test_stats_use_shared_two_queries(self):
        """행당 stat 6개(total/passed/failed/triggers/rate/in_flight) 가 쿼리 2개만 공유하는지.

        ABORTED 잡을 total/failed 에서 제외하기 위해 Sum(suite_run 필드) 과
        job_runs 조인 Count 를 별도 aggregate 로 분리했으므로(fan-out 방지),
        memoize 가 없으면 stat 을 읽을 때마다 매번 2쿼리씩 나가야 하지만,
        memoize 덕에 행당 총 2쿼리(최초 1회분)만 나가야 한다.
        """
        from django.db import connection, reset_queries
        s = RobustnessSchedule.objects.get(pk=self.schedule.pk)
        reset_queries()
        # 화면 한 행에서 읽는 stat 들 — 모두 같은 aggregate 캐시를 공유해야 함
        total = s.total_executions
        passed = s.success_count
        failed = s.failure_count
        triggers = s.trigger_count
        rate = s.success_rate
        inflight = s.in_flight_count
        self.assertEqual(len(connection.queries), 2, connection.queries)
        self.assertEqual(total, 3)
        self.assertEqual(passed, 2)
        self.assertEqual(failed, 1)
        self.assertEqual(triggers, 1)


# =====================================================================
# 일괄 시작/중지 버튼 활성화 상태 (any_running)
# =====================================================================

class StartStopAllButtonStateTests(TestCase):
    """돌아가는 스케줄 유무에 따라 일괄 시작/중지 버튼 disabled 가 토글되는지."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="rbbtn", password="x", role="qpe_admin", is_active=True)
        self.suite = TestSuite.objects.create(name="BtnSuite", jenkins_folder_path="b/p")
        self.test = RobustnessTest.objects.create(name="BtnTest")
        self.schedule = RobustnessSchedule.objects.create(
            robustness_test=self.test, test_suite=self.suite,
            interval_minutes=60, is_running=False)
        self.client.force_login(self.user)

    def test_stopped_state_disables_stop_enables_start(self):
        resp = self.client.get(f"/robustness/{self.test.pk}/")
        self.assertFalse(resp.context["any_running"])
        body = resp.content.decode()
        self.assertIn("실행 중인 스케줄이 없습니다", body)        # 중지 disabled
        self.assertNotIn("이미 실행 중인 스케줄이 있습니다", body)  # 시작 enabled

    def test_running_state_disables_start_enables_stop(self):
        self.schedule.is_running = True
        self.schedule.save(update_fields=["is_running"])
        resp = self.client.get(f"/robustness/{self.test.pk}/")
        self.assertTrue(resp.context["any_running"])
        body = resp.content.decode()
        self.assertIn("이미 실행 중인 스케줄이 있습니다", body)     # 시작 disabled
        self.assertNotIn("실행 중인 스케줄이 없습니다", body)       # 중지 enabled


class SweepReliabilityCommandTests(TestCase):
    """cron 용 sweep_reliability 명령 — 스케줄 sweep + 단독 RUNNING run 폴링."""

    def _patch_api_sync(self):
        """API 탭 Jenkins 자동 싱크 — 실제 내부 Jenkins 로 나가지 않도록 항상 mock.

        cache.add 쿨다운 게이트가 프로세스 공유 LocMem 캐시라 테스트 실행 순서에 따라
        실호출 여부가 갈리면 안 되므로, 쿨다운 자체도 항상 통과하게 캐시 키를 비운다.
        """
        from django.core.cache import cache
        cache.delete("suites:api_jenkins_sync_cooldown")
        return mock.patch(
            "apps.suites.services_sync.sync_api_suites",
            return_value={"created": 0, "synced": 0, "candidates": []},
        )

    def test_command_calls_sweep_with_force(self):
        from django.core.management import call_command
        with mock.patch(
            "apps.robustness.management.commands.sweep_reliability.sweep_active_schedules"
        ) as m, mock.patch(
            "apps.robustness.management.commands.sweep_reliability._poll_run_status"
        ), self._patch_api_sync():
            call_command("sweep_reliability")
        m.assert_called_once_with(force=True)

    def test_command_polls_standalone_running_runs_only(self):
        """단독(비-robustness) RUNNING run 은 폴링, robustness 소속 run 은 제외.

        회귀 방지: 외부 폴러가 죽으면 단독 run 완료가 하루 뒤에야 감지되던 문제
        (2026-07-21 am-mapp-regression-api-dev seq 1284) — cron 이 매분 폴링한다.
        """
        from django.core.management import call_command
        suite = TestSuite.objects.create(name="StandaloneSuite", jenkins_folder_path="sa/p")
        standalone = _TSR.objects.create(
            suite=suite, status=_TSR.Status.RUNNING, total_jobs=1,
            started_at=timezone.now())
        done = _TSR.objects.create(
            suite=suite, status=_TSR.Status.COMPLETED, total_jobs=1,
            started_at=timezone.now())  # RUNNING 아님 → 폴링 제외
        sched = _make_schedule(suite)
        linked = _TSR.objects.create(
            suite=suite, status=_TSR.Status.RUNNING, total_jobs=1,
            started_at=timezone.now())
        RobustnessRun.objects.create(
            schedule=sched, suite_run=linked, status=RobustnessRun.Status.RUNNING)

        with mock.patch(
            "apps.robustness.management.commands.sweep_reliability.sweep_active_schedules"
        ), mock.patch(
            "apps.robustness.management.commands.sweep_reliability._poll_run_status"
        ) as poll, self._patch_api_sync():
            call_command("sweep_reliability")

        polled_pks = {c.args[0].pk for c in poll.call_args_list}
        self.assertEqual(polled_pks, {standalone.pk})  # done/linked 는 제외

    def test_command_calls_tap_sync_and_poll(self):
        """cron 이 UI Test Automation(TAP) suite 싱크 + 진행 중 run 폴링도 겸하는지.

        PRD docs/prd_ui_test_automation.md §3.2/§3.3 — crontab 무변경 재사용 회귀 방지.
        """
        from django.core.management import call_command
        with mock.patch(
            "apps.robustness.management.commands.sweep_reliability.sweep_active_schedules"
        ), mock.patch(
            "apps.robustness.management.commands.sweep_reliability._poll_run_status"
        ), mock.patch(
            "apps.suites.services_tap.sync_tap_suites", return_value=1,
        ) as mock_sync, mock.patch(
            "apps.suites.services_tap.poll_active_tap_runs", return_value=2,
        ) as mock_poll, self._patch_api_sync():
            call_command("sweep_reliability")
        mock_sync.assert_called_once_with()
        mock_poll.assert_called_once_with()

    def test_command_survives_tap_sync_exception(self):
        """TAP sync/poll 실패가 스케줄 sweep 전체를 막지 않는다(장애 격리)."""
        from django.core.management import call_command
        with mock.patch(
            "apps.robustness.management.commands.sweep_reliability.sweep_active_schedules"
        ), mock.patch(
            "apps.robustness.management.commands.sweep_reliability._poll_run_status"
        ), mock.patch(
            "apps.suites.services_tap.sync_tap_suites", side_effect=RuntimeError("tap down"),
        ), self._patch_api_sync():
            call_command("sweep_reliability")  # 예외 없이 정상 종료해야 함

    def test_command_calls_api_suite_sync_under_cooldown(self):
        """API 탭 Jenkins 자동 싱크 — 쿨다운이 풀려있으면(cache 비어있으면) 호출된다.

        PRD docs/prd_ui_test_automation.md §3.4 — 10분 주기 자동 싱크 회귀 방지.
        """
        from django.core.cache import cache
        from django.core.management import call_command
        cache.delete("suites:api_jenkins_sync_cooldown")
        with mock.patch(
            "apps.robustness.management.commands.sweep_reliability.sweep_active_schedules"
        ), mock.patch(
            "apps.robustness.management.commands.sweep_reliability._poll_run_status"
        ), mock.patch(
            "apps.suites.services_tap.sync_tap_suites", return_value=1,
        ), mock.patch(
            "apps.suites.services_tap.poll_active_tap_runs", return_value=0,
        ), mock.patch(
            "apps.suites.services_sync.sync_api_suites",
            return_value={"created": 1, "synced": 1, "candidates": ["x"]},
        ) as mock_api_sync:
            call_command("sweep_reliability")
        mock_api_sync.assert_called_once_with()

    def test_command_skips_api_suite_sync_within_cooldown_window(self):
        """직전 호출로부터 10분이 안 지났으면 이번 사이클은 건너뛴다(부하 방지)."""
        from django.core.cache import cache
        from django.core.management import call_command
        cache.set("suites:api_jenkins_sync_cooldown", 1, 600)
        with mock.patch(
            "apps.robustness.management.commands.sweep_reliability.sweep_active_schedules"
        ), mock.patch(
            "apps.robustness.management.commands.sweep_reliability._poll_run_status"
        ), mock.patch(
            "apps.suites.services_tap.sync_tap_suites", return_value=1,
        ), mock.patch(
            "apps.suites.services_tap.poll_active_tap_runs", return_value=0,
        ), mock.patch(
            "apps.suites.services_sync.sync_api_suites",
        ) as mock_api_sync:
            call_command("sweep_reliability")
        mock_api_sync.assert_not_called()
        cache.delete("suites:api_jenkins_sync_cooldown")

    def test_command_survives_api_suite_sync_exception(self):
        """API 잡 자동 싱크 실패가 스케줄 sweep 전체를 막지 않는다(장애 격리)."""
        from django.core.cache import cache
        from django.core.management import call_command
        cache.delete("suites:api_jenkins_sync_cooldown")
        with mock.patch(
            "apps.robustness.management.commands.sweep_reliability.sweep_active_schedules"
        ), mock.patch(
            "apps.robustness.management.commands.sweep_reliability._poll_run_status"
        ), mock.patch(
            "apps.suites.services_tap.sync_tap_suites", return_value=1,
        ), mock.patch(
            "apps.suites.services_tap.poll_active_tap_runs", return_value=0,
        ), mock.patch(
            "apps.suites.services_sync.sync_api_suites", side_effect=RuntimeError("jenkins down"),
        ):
            call_command("sweep_reliability")  # 예외 없이 정상 종료해야 함


class TestListOrderingTests(TestCase):
    """Reliability Test 목록이 테스트 이름 오름차순으로 노출되는지 — 회귀.

    (annotate 가 Meta.ordering 을 떨어뜨려 생성순으로 나오던 문제)
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="rlo", password="x", role="qpe_admin", is_active=True)
        self.client.force_login(self.user)
        for nm in ["Zebra Test", "Apple Test", "Mango Test"]:
            RobustnessTest.objects.create(name=nm)

    def test_list_is_ascending_by_name(self):
        resp = self.client.get("/robustness/")
        self.assertEqual(resp.status_code, 200)
        names = [t.name for t in resp.context["tests"]]
        self.assertEqual(names, ["Apple Test", "Mango Test", "Zebra Test"])


class SingleJobModeTests(TestCase):
    """단일 테스트 모드 — idle 잡 1개만 실행 + 병렬과 상호배타."""

    def setUp(self):
        self.suite = TestSuite.objects.create(
            name="SingleSuite", jenkins_folder_path="sg/p", retry_count=1)
        for i in range(5):
            TestSuiteJob.objects.create(
                suite=self.suite, job_name=f"j{i}", job_full_path=f"sg/p/j{i}",
                order=i, is_active=True, is_target=True, trigger_delay_seconds=0)
        self.rt = RobustnessTest.objects.create(name="SingleRT", single_job_mode=True)
        self.schedule = RobustnessSchedule.objects.create(
            robustness_test=self.rt, test_suite=self.suite,
            interval_minutes=60, is_running=True)

    def _fake(self, building=False):
        fake = mock.MagicMock()
        fake.get_last_build_info.return_value = {"building": building}
        fake.trigger_job.return_value = {"build_number": 1}
        return fake

    def test_single_mode_runs_one_idle_job(self):
        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=self._fake(building=False)),
            mock.patch("apps.robustness.services.send_suite_run_event_async"),
        ):
            rr = services._trigger_once(self.schedule)
        self.assertIsNotNone(rr)
        self.assertEqual(rr.suite_run.total_jobs, 1)
        self.assertEqual(rr.suite_run.job_runs.count(), 1)

    def test_single_mode_skips_when_no_idle(self):
        # 모든 잡 building=True → idle 없음 → run 생성 안 함
        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=self._fake(building=True)),
            mock.patch("apps.robustness.services.send_suite_run_event_async"),
        ):
            rr = services._trigger_once(self.schedule)
        self.assertIsNone(rr)
        self.assertEqual(RobustnessRun.objects.filter(schedule=self.schedule).count(), 0)

    def test_non_single_runs_all_jobs(self):
        self.rt.single_job_mode = False
        self.rt.save(update_fields=["single_job_mode"])
        with (
            mock.patch.object(TestSuite, "get_jenkins_client", return_value=self._fake(building=False)),
            mock.patch("apps.robustness.services.send_suite_run_event_async"),
        ):
            rr = services._trigger_once(self.schedule)
        self.assertEqual(rr.suite_run.total_jobs, 5)


class SingleModeToggleTests(TestCase):
    """토글 상호배타 — 단일 ON 시 병렬 강제 OFF + 병렬 토글 무력화."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="smt", password="x", role="qpe_admin", is_active=True)
        self.client.force_login(self.user)
        self.test = RobustnessTest.objects.create(name="ToggleRT", parallel_jobs=True)

    def test_enable_single_forces_parallel_off(self):
        self.client.post(f"/robustness/{self.test.pk}/toggle-single/")
        self.test.refresh_from_db()
        self.assertTrue(self.test.single_job_mode)
        self.assertFalse(self.test.parallel_jobs)

    def test_parallel_toggle_noop_when_single_on(self):
        self.test.single_job_mode = True
        self.test.parallel_jobs = False
        self.test.save()
        self.client.post(f"/robustness/{self.test.pk}/toggle-parallel/")
        self.test.refresh_from_db()
        self.assertFalse(self.test.parallel_jobs)  # 단일 모드 중 → 무시
        self.assertTrue(self.test.single_job_mode)


# =====================================================================
# 중지 → 재개 시 세션 통계(성공률/실행수)가 초기화되지 않고 이어지는지 회귀
# =====================================================================

from apps.suites.models import TestSuiteJobRun as _JR  # noqa: E402


class SchedulePauseResumeTests(TestCase):
    """stop_schedule → start_schedule 재개 시 세션 통계가 이어지는지 회귀.

    버그: 과거엔 start_schedule 이 매번 started_at=now 로 리셋해 session_runs
    (= runs.filter(triggered_at__gte=started_at)) 가 이전 run 을 전부 제외 →
    중지 후 재시작하면 성공률/실행수가 0 으로 초기화됐다. 이제 started_at 이
    이미 있으면 유지해 '멈췄다 이어서' 집계된다. 통계 초기화는 명시적
    reset_schedule_stats 만 한다.
    """

    def setUp(self):
        self.suite = TestSuite.objects.create(
            name="PauseResumeSuite", jenkins_folder_path="pr/p")
        self.job1 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j1", job_full_path="pr/p/j1", order=0)
        self.job2 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="j2", job_full_path="pr/p/j2", order=1)
        self.schedule = _make_schedule(self.suite, is_running=False)
        services.start_schedule(self.schedule)  # started_at 채워 세션 시작

    def _add_completed_run(self, total, passed, failed, status=RobustnessRun.Status.SUCCESS):
        sr = _TSR.objects.create(
            suite=self.suite, status=_TSR.Status.COMPLETED,
            total_jobs=total, passed_jobs=passed, failed_jobs=failed,
            started_at=timezone.now(),
        )
        return RobustnessRun.objects.create(
            schedule=self.schedule, suite_run=sr, status=status,
        )

    def test_resume_keeps_started_at_and_continues_stats(self):
        """중지 후 재개해도 started_at 유지 + 중지 전 성공/실패/성공률이 그대로 이어진다."""
        self._add_completed_run(total=3, passed=2, failed=1)
        original_started_at = RobustnessSchedule.objects.get(pk=self.schedule.pk).started_at

        # 중지: 진행 중인 run 이 없으므로 abort 대상 없음 → Jenkins mock 불필요
        services.stop_schedule(self.schedule)
        self.schedule.refresh_from_db()
        self.assertFalse(self.schedule.is_running)
        self.assertIsNotNone(self.schedule.stopped_at)

        # 재개
        services.start_schedule(self.schedule)
        self.schedule.refresh_from_db()
        self.assertTrue(self.schedule.is_running)
        self.assertIsNone(self.schedule.stopped_at)
        self.assertIsNotNone(self.schedule.started_at)
        self.assertEqual(self.schedule.started_at, original_started_at)  # 리셋되지 않음

        s = RobustnessSchedule.objects.get(pk=self.schedule.pk)  # memoize 없는 새 인스턴스
        self.assertEqual(s.total_executions, 3)
        self.assertEqual(s.success_count, 2)
        self.assertEqual(s.failure_count, 1)
        self.assertAlmostEqual(s.success_rate, 66.7, places=1)

    def test_stop_aborts_inflight_without_hurting_success_rate(self):
        """중지 시점에 진행 중이던 run 의 ABORTED 잡은 성공률/실패수/total 에서 제외된다.

        중지 전: 완료된 run(total=3, passed=2, failed=1, 성공률 66.7%) +
        진행 중인 run(2 잡, QUEUED/RUNNING). stop_schedule 후 진행 중이던 2 잡은
        모두 ABORTED 로 마킹되지만, 집계에서는 제외되어 중지 전과 동일한
        성공률/실패수/total 이 유지되고 in_flight_count 도 0 이어야 한다.
        """
        self._add_completed_run(total=3, passed=2, failed=1)

        inflight_sr = _TSR.objects.create(
            suite=self.suite, status=_TSR.Status.RUNNING,
            total_jobs=2, passed_jobs=0, failed_jobs=0,
            started_at=timezone.now(),
        )
        _JR.objects.create(
            suite_run=inflight_sr, job=self.job1,
            status=_JR.Status.QUEUED, jenkins_build_number=1,
        )
        _JR.objects.create(
            suite_run=inflight_sr, job=self.job2,
            status=_JR.Status.RUNNING, jenkins_build_number=1,
        )
        RobustnessRun.objects.create(
            schedule=self.schedule, suite_run=inflight_sr,
            status=RobustnessRun.Status.RUNNING,
        )

        fake = mock.MagicMock()
        fake.stop_build.return_value = {}
        with mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake):
            services.stop_schedule(self.schedule)

        inflight_sr.refresh_from_db()
        self.assertEqual(inflight_sr.status, _TSR.Status.COMPLETED)
        for jr in inflight_sr.job_runs.all():
            self.assertEqual(jr.status, _JR.Status.ABORTED)

        s = RobustnessSchedule.objects.get(pk=self.schedule.pk)
        self.assertEqual(s.total_executions, 3)   # ABORTED 2건은 total 에서 제외
        self.assertEqual(s.success_count, 2)
        self.assertEqual(s.failure_count, 1)       # ABORTED 는 failed 에서도 제외
        self.assertAlmostEqual(s.success_rate, 66.7, places=1)
        self.assertEqual(s.in_flight_count, 0)

    def test_jenkins_side_abort_also_excluded_from_stats(self):
        """스케줄이 도는 중 Jenkins 쪽에서 취소된(ABORTED) 잡도 장애가 아니다 — 집계 제외.

        "aborted 는 장애로 보지 않는다" 전역 정책: 중지에 의한 abort 뿐 아니라
        폴링 결과 ABORTED 로 끝난 잡(run 은 FAILURE 마감)도 성공률/실패수/total
        에서 빠진다. 미장애 판정 큐/대시보드 분포와 동일 기준.
        """
        sr = _TSR.objects.create(
            suite=self.suite, status=_TSR.Status.COMPLETED,
            total_jobs=3, passed_jobs=2, failed_jobs=1,  # failed 1 = ABORTED 잡
            started_at=timezone.now(),
        )
        _JR.objects.create(
            suite_run=sr, job=self.job1, status=_JR.Status.ABORTED,
            jenkins_build_number=1,
        )
        RobustnessRun.objects.create(
            schedule=self.schedule, suite_run=sr,
            status=RobustnessRun.Status.FAILURE,  # 중지가 아니라 폴링으로 FAILURE 마감
        )

        s = RobustnessSchedule.objects.get(pk=self.schedule.pk)
        self.assertEqual(s.total_executions, 2)   # ABORTED 1건 total 제외
        self.assertEqual(s.failure_count, 0)       # 장애 아님 — 실패 집계 제외
        self.assertEqual(s.success_count, 2)
        self.assertEqual(s.success_rate, 100.0)

    def test_aborted_with_explicit_failure_verdict_counts_as_failure(self):
        """사람이 Failure 로 명시 확정한 ABORTED 잡은 장애로 인정 — 집계에 포함."""
        sr = _TSR.objects.create(
            suite=self.suite, status=_TSR.Status.COMPLETED,
            total_jobs=2, passed_jobs=1, failed_jobs=1,
            started_at=timezone.now(),
        )
        _JR.objects.create(
            suite_run=sr, job=self.job1, status=_JR.Status.ABORTED,
            verdict="failure", jenkins_build_number=1,
        )
        RobustnessRun.objects.create(
            schedule=self.schedule, suite_run=sr,
            status=RobustnessRun.Status.FAILURE,
        )

        s = RobustnessSchedule.objects.get(pk=self.schedule.pk)
        self.assertEqual(s.total_executions, 2)   # 제외 없음
        self.assertEqual(s.failure_count, 1)       # 확정 Failure = 장애
        self.assertEqual(s.success_count, 1)
        self.assertEqual(s.success_rate, 50.0)

    def test_reset_stats_clears_session(self):
        """reset_schedule_stats 는 started_at 을 None 으로 비워 session_runs 를 빈 쿼리셋으로 만든다."""
        self._add_completed_run(total=3, passed=2, failed=1)

        services.reset_schedule_stats(self.schedule)
        self.schedule.refresh_from_db()
        self.assertIsNone(self.schedule.started_at)
        self.assertIsNone(self.schedule.last_triggered_at)
        self.assertIsNone(self.schedule.stopped_at)

        s = RobustnessSchedule.objects.get(pk=self.schedule.pk)
        self.assertEqual(s.total_executions, 0)
        self.assertEqual(s.success_count, 0)
        self.assertEqual(s.failure_count, 0)
        self.assertEqual(s.success_rate, 0)

    def test_start_after_reset_fills_started_at_again(self):
        """(중지 후) reset → start 하면 started_at 이 새로 채워진다.

        start_schedule 은 멱등이라 실행 중 스케줄엔 no-op — running 중 reset
        의 세션 재개는 다음 _trigger_once 가 채우는 별도 경로다. 여기선
        중지→초기화→시작 흐름을 검증한다.
        """
        services.stop_schedule(self.schedule)
        services.reset_schedule_stats(self.schedule)
        self.schedule.refresh_from_db()
        self.assertIsNone(self.schedule.started_at)

        self.assertTrue(services.start_schedule(self.schedule))
        self.schedule.refresh_from_db()
        self.assertIsNotNone(self.schedule.started_at)
        self.assertTrue(self.schedule.is_running)

    def test_duplicate_start_is_noop_and_keeps_last_triggered_at(self):
        """이미 실행 중인 스케줄에 start_schedule 재호출 → no-op(False) + lta 보존.

        회귀 방지(2026-07-16 seq 643 사건): 개별 '시작' 직후 '일괄 시작' 이
        같은 스케줄을 또 시작하며 last_triggered_at 을 None 으로 리셋 → 직후
        sweep 이 interval 도래로 오인해 방금 트리거된 run 을 timeout 마감
        (전 잡 FAILURE, 로그 없음) 하던 버그.
        """
        lta = timezone.now()
        self.schedule.last_triggered_at = lta
        self.schedule.save(update_fields=["last_triggered_at"])

        self.assertFalse(services.start_schedule(self.schedule))
        self.schedule.refresh_from_db()
        self.assertTrue(self.schedule.is_running)
        self.assertEqual(self.schedule.last_triggered_at, lta)  # 리셋 안 됨


class TimeoutUnfinishedRunsTests(TestCase):
    """_timeout_unfinished_runs — 나이 가드 + 증거(콘솔 사유/ActivityLog) 보존."""

    def setUp(self):
        self.suite = TestSuite.objects.create(
            name="TimeoutSuite", jenkins_folder_path="to/p")
        self.job1 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="t1", job_full_path="to/p/t1", order=0)
        self.job2 = TestSuiteJob.objects.create(
            suite=self.suite, job_name="t2", job_full_path="to/p/t2", order=1)
        self.schedule = _make_schedule(self.suite, interval=60)

    def _inflight_run(self, triggered_at):
        sr = _TSR.objects.create(
            suite=self.suite, status=_TSR.Status.RUNNING,
            total_jobs=2, started_at=triggered_at,
        )
        _JR.objects.create(
            suite_run=sr, job=self.job1,
            status=_JR.Status.RUNNING, jenkins_build_number=7,
        )
        _JR.objects.create(suite_run=sr, job=self.job2, status=_JR.Status.PENDING)
        rr = RobustnessRun.objects.create(
            schedule=self.schedule, suite_run=sr,
            status=RobustnessRun.Status.RUNNING, triggered_at=triggered_at,
        )
        return sr, rr

    def test_young_run_survives(self):
        """방금 트리거된 run(< interval)은 timeout 마감 대상이 아니다.

        회귀 방지: 중복 시작/reset 직후 sweep 이 lta=None 을 interval 도래로
        오인해도, 갓 뜬 run 이 '이전 사이클 미완료' 로 죽지 않아야 한다.
        """
        sr, rr = self._inflight_run(timezone.now())  # 방금 트리거
        affected = services._timeout_unfinished_runs(self.schedule)
        self.assertEqual(affected, 0)
        sr.refresh_from_db(); rr.refresh_from_db()
        self.assertEqual(sr.status, _TSR.Status.RUNNING)      # 살아있음
        self.assertEqual(rr.status, RobustnessRun.Status.RUNNING)

    def test_old_run_killed_with_evidence(self):
        """interval 을 넘긴 run 은 FAILURE 마감 + 콘솔 사유/로그 + ActivityLog."""
        from apps.activitylog.models import ActivityLog
        old = timezone.now() - timezone.timedelta(minutes=61)
        sr, rr = self._inflight_run(old)

        fake = mock.MagicMock()
        fake.stop_build.return_value = {}
        fake.get_console_output.return_value = "BUILD LOG LINE 1\nFinished: ABORTED"
        with mock.patch.object(TestSuite, "get_jenkins_client", return_value=fake):
            affected = services._timeout_unfinished_runs(self.schedule)

        self.assertEqual(affected, 2)
        sr.refresh_from_db(); rr.refresh_from_db()
        self.assertEqual(sr.status, _TSR.Status.COMPLETED)
        self.assertEqual(rr.status, RobustnessRun.Status.FAILURE)

        jr1 = sr.job_runs.get(job=self.job1)   # RUNNING+build → 콘솔 수집
        self.assertEqual(jr1.status, _JR.Status.FAILURE)
        self.assertIn("[interval timeout]", jr1.console_output)
        self.assertIn("BUILD LOG LINE 1", jr1.console_output)
        jr2 = sr.job_runs.get(job=self.job2)   # PENDING → 사유만
        self.assertEqual(jr2.status, _JR.Status.FAILURE)
        self.assertIn("[interval timeout]", jr2.console_output)
        self.assertIn("트리거 전", jr2.console_output)

        log = ActivityLog.objects.filter(
            action=ActivityLog.Action.CANCEL,
            target_id=str(self.schedule.pk),
        ).first()
        self.assertIsNotNone(log)
        self.assertIn("타임아웃", log.detail)


# =====================================================================
# TAP(UI Automation) 스케줄 — PRD docs/prd_ui_test_automation.md 확장.
# Jenkins(API) 경로와 같은 골격(시작/중지/reset/멱등 start 는 공유)이지만
# 트리거·폴링·타임아웃·집계는 케이스 수 기준으로 별도 분기된다. 모든 TAP
# 호출(TapClient)은 mock 으로 차단 — 실제 tapilot 호스트로 나가지 않는다.
# =====================================================================

from apps.suites.models import TapRun, TapSuite  # noqa: E402


def _make_tap_schedule(tap_suite, interval=60, is_running=True):
    """테스트용 TAP RobustnessSchedule 생성 헬퍼 (_make_schedule 의 TAP 대응)."""
    rt = RobustnessTest.objects.create(name=f"TapRT-{id(tap_suite)}")
    return RobustnessSchedule.objects.create(
        robustness_test=rt, tap_suite=tap_suite,
        interval_minutes=interval, is_running=is_running,
    )


class TapScheduleAddViewTests(TestCase):
    """schedule_add 뷰 — tap_suite_id 로 UI Automation 스케줄 추가(회귀 시나리오 ①②③)."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="tapadd", password="x", role="qpe_admin", is_active=True)
        self.client.force_login(self.user)
        self.test = RobustnessTest.objects.create(name="TapAddRT")
        self.tap_suite = TapSuite.objects.create(tap_id=1, name="Login Flow", case_count=5)

    def test_creates_tap_schedule(self):
        """tap_suite_id 로 POST → test_suite 는 비고 tap_suite 만 채운 스케줄 생성."""
        resp = self.client.post(
            f"/robustness/{self.test.pk}/schedules/add/",
            {"tap_suite_id": str(self.tap_suite.pk), "interval_minutes": "30"},
        )
        self.assertEqual(resp.status_code, 302)
        schedule = RobustnessSchedule.objects.get(robustness_test=self.test)
        self.assertEqual(schedule.tap_suite_id, self.tap_suite.pk)
        self.assertIsNone(schedule.test_suite_id)
        self.assertEqual(schedule.interval_minutes, 30)
        self.assertTrue(schedule.is_tap)

    def test_duplicate_tap_suite_updates_interval_instead_of_creating(self):
        """이미 연동된 tap_suite 를 다시 추가하면(중복) 새로 만들지 않고 간격만 갱신."""
        RobustnessSchedule.objects.create(
            robustness_test=self.test, tap_suite=self.tap_suite, interval_minutes=60)
        resp = self.client.post(
            f"/robustness/{self.test.pk}/schedules/add/",
            {"tap_suite_id": str(self.tap_suite.pk), "interval_minutes": "15"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            RobustnessSchedule.objects.filter(
                robustness_test=self.test, tap_suite=self.tap_suite).count(), 1,
        )
        schedule = RobustnessSchedule.objects.get(
            robustness_test=self.test, tap_suite=self.tap_suite)
        self.assertEqual(schedule.interval_minutes, 15)

    def test_neither_suite_nor_tap_suite_shows_error_and_creates_nothing(self):
        """test_suite_id/tap_suite_id 둘 다 없으면 에러 메시지 + 스케줄 생성 안 함."""
        resp = self.client.post(
            f"/robustness/{self.test.pk}/schedules/add/",
            {"interval_minutes": "30"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(RobustnessSchedule.objects.filter(robustness_test=self.test).count(), 0)


class SweepTapScheduleTests(TestCase):
    """_sweep_tap_schedule — 진행 폴링 + race-free interval 트리거(sweep_schedule TAP 분기)."""

    def setUp(self):
        self.tap_suite = TapSuite.objects.create(tap_id=10, name="Checkout", case_count=8)
        self.schedule = _make_tap_schedule(self.tap_suite, interval=60, is_running=True)

    def test_polls_only_active_runs(self):
        """ACTIVE_STATUSES(queued/pending/running) 인 run 만 폴링, 종결 run 은 제외."""
        active = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=1, status=TapRun.Status.RUNNING,
            robustness_schedule=self.schedule, started_at=timezone.now())
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=2, status=TapRun.Status.COMPLETED,
            robustness_schedule=self.schedule, started_at=timezone.now())
        with (
            mock.patch("apps.suites.services_tap.poll_tap_run") as mock_poll,
            mock.patch("apps.robustness.services._trigger_tap_once"),
        ):
            services._sweep_tap_schedule(self.schedule)
        polled = {c.args[0].pk for c in mock_poll.call_args_list}
        self.assertEqual(polled, {active.pk})

    def test_triggers_new_run_when_interval_reached(self):
        """last_triggered_at=None(최초) → race-free 트리거 슬롯 점유 후 _trigger_tap_once 호출."""
        with (
            mock.patch("apps.suites.services_tap.poll_tap_run"),
            mock.patch("apps.robustness.services._trigger_tap_once") as mock_trigger,
        ):
            services._sweep_tap_schedule(self.schedule)
        mock_trigger.assert_called_once_with(self.schedule)
        self.schedule.refresh_from_db()
        self.assertIsNotNone(self.schedule.last_triggered_at)

    def test_does_not_trigger_before_interval_elapses(self):
        """직전 트리거로부터 interval 이 안 지났으면 이번 sweep 은 트리거하지 않는다."""
        self.schedule.last_triggered_at = timezone.now()
        self.schedule.save(update_fields=["last_triggered_at"])
        with (
            mock.patch("apps.suites.services_tap.poll_tap_run"),
            mock.patch("apps.robustness.services._trigger_tap_once") as mock_trigger,
        ):
            services._sweep_tap_schedule(self.schedule)
        mock_trigger.assert_not_called()

    def test_stopped_schedule_polls_but_does_not_trigger(self):
        """is_running=False 인 스케줄은 진행 중 run 은 계속 폴링하되 새로 트리거하지 않는다."""
        self.schedule.is_running = False
        self.schedule.save(update_fields=["is_running"])
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=3, status=TapRun.Status.RUNNING,
            robustness_schedule=self.schedule, started_at=timezone.now())
        with (
            mock.patch("apps.suites.services_tap.poll_tap_run") as mock_poll,
            mock.patch("apps.robustness.services._trigger_tap_once") as mock_trigger,
        ):
            services._sweep_tap_schedule(self.schedule)
        mock_poll.assert_called_once()
        mock_trigger.assert_not_called()


class TimeoutUnfinishedTapRunsTests(TestCase):
    """_timeout_unfinished_tap_runs — 나이 가드 + cancel 요청 + error 마감 + ActivityLog.

    Jenkins 경로(_timeout_unfinished_runs)의 나이 가드/증거 보존 정책과 동일한
    TAP 대응 — 타임아웃은 error 로 기록해 실패 신호를 유지한다(Aborted/장애 아님과 구분).
    """

    def setUp(self):
        self.tap_suite = TapSuite.objects.create(tap_id=20, name="Search", case_count=6)
        self.schedule = _make_tap_schedule(self.tap_suite, interval=60)

    def test_young_run_survives(self):
        """방금 시작한 run(< interval)은 타임아웃 마감 대상이 아니다."""
        run = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=100, status=TapRun.Status.RUNNING,
            robustness_schedule=self.schedule, started_at=timezone.now())
        with mock.patch("apps.suites.services_tap.cancel_tap_run") as mock_cancel:
            affected = services._timeout_unfinished_tap_runs(self.schedule)
        self.assertEqual(affected, 0)
        mock_cancel.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, TapRun.Status.RUNNING)

    def test_old_run_cancelled_and_marked_error_with_activity_log(self):
        """interval 을 넘긴 run 은 cancel 요청 + error 마감(취소 응답과 무관하게 강제) + 감사 로그."""
        from apps.activitylog.models import ActivityLog

        old = timezone.now() - timezone.timedelta(minutes=61)
        run = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=101, status=TapRun.Status.RUNNING,
            robustness_schedule=self.schedule, started_at=old,
            total_cases=6, completed_cases=2, passed=1, failed=1)

        def _fake_cancel(tr):
            # TAP 이 취소를 STOPPED 로 응답했다고 가정해도, 타임아웃 마감은
            # 강제로 error 상태를 덮어써 실패 신호를 유지해야 한다.
            tr.status = TapRun.Status.STOPPED
            tr.save(update_fields=["status"])

        with mock.patch(
            "apps.suites.services_tap.cancel_tap_run", side_effect=_fake_cancel,
        ) as mock_cancel:
            affected = services._timeout_unfinished_tap_runs(self.schedule)

        mock_cancel.assert_called_once()
        self.assertEqual(affected, 1)
        run.refresh_from_db()
        self.assertEqual(run.status, TapRun.Status.ERROR)
        self.assertIsNotNone(run.finished_at)

        log = ActivityLog.objects.filter(
            action=ActivityLog.Action.CANCEL, target_id=str(self.schedule.pk),
        ).first()
        self.assertIsNotNone(log)
        self.assertIn("타임아웃", log.detail)

    def test_cancel_exception_is_isolated_and_run_still_marked_error(self):
        """TAP cancel 호출이 실패해도 로컬 상태는 error 로 강제 마감(장애 격리)."""
        old = timezone.now() - timezone.timedelta(minutes=61)
        run = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=102, status=TapRun.Status.QUEUED,
            robustness_schedule=self.schedule, started_at=old)
        with mock.patch(
            "apps.suites.services_tap.cancel_tap_run", side_effect=RuntimeError("tap down"),
        ):
            affected = services._timeout_unfinished_tap_runs(self.schedule)
        self.assertEqual(affected, 1)
        run.refresh_from_db()
        self.assertEqual(run.status, TapRun.Status.ERROR)


class StopScheduleTapBranchTests(TestCase):
    """stop_schedule TAP 분기 — 진행 중(active) TapRun 을 cancel 요청 후 스케줄 정지."""

    def setUp(self):
        self.tap_suite = TapSuite.objects.create(tap_id=30, name="Cart", case_count=4)
        self.schedule = _make_tap_schedule(self.tap_suite, is_running=True)

    def test_cancels_only_active_tap_runs_and_stops_schedule(self):
        active = TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=200, status=TapRun.Status.RUNNING,
            robustness_schedule=self.schedule, started_at=timezone.now())
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=201, status=TapRun.Status.COMPLETED,
            robustness_schedule=self.schedule, started_at=timezone.now())
        with mock.patch("apps.suites.services_tap.cancel_tap_run") as mock_cancel:
            services.stop_schedule(self.schedule)
        mock_cancel.assert_called_once_with(active)
        self.schedule.refresh_from_db()
        self.assertFalse(self.schedule.is_running)
        self.assertIsNotNone(self.schedule.stopped_at)

    def test_cancel_exception_does_not_block_stop(self):
        """TAP cancel 실패해도 스케줄 정지 자체는 정상 완료(장애 격리)."""
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=202, status=TapRun.Status.PENDING,
            robustness_schedule=self.schedule, started_at=timezone.now())
        with mock.patch(
            "apps.suites.services_tap.cancel_tap_run", side_effect=RuntimeError("tap down"),
        ):
            services.stop_schedule(self.schedule)  # 예외 없이 정상 종료
        self.schedule.refresh_from_db()
        self.assertFalse(self.schedule.is_running)


class TapAggregateStatsTests(TestCase):
    """RobustnessSchedule._tap_aggregate/_stats — 케이스 집계 + error 잔여 실패 집계 + stopped 부분 집계.

    Jenkins 잡 단위 집계(_job_aggregate)와 동일한 컬럼 의미(total/passed/failed/
    in_flight/triggers)를 케이스 수 기준으로 낸다.
    """

    def setUp(self):
        self.tap_suite = TapSuite.objects.create(tap_id=40, name="Payment", case_count=10)
        self.schedule = _make_tap_schedule(self.tap_suite, is_running=False)
        services.start_schedule(self.schedule)  # started_at 채워 session_tap_runs 활성화

    def test_mixed_run_states_aggregate_correctly(self):
        now = timezone.now()
        # 완료된 run: 10케이스 다 판정(8 pass, 2 fail).
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=1, status=TapRun.Status.COMPLETED,
            robustness_schedule=self.schedule, started_at=now,
            total_cases=10, completed_cases=10, passed=8, failed=2)
        # 진행 중(active) run: 10케이스 중 4개 판정(3 pass, 1 fail), 6개는 in_flight.
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=2, status=TapRun.Status.RUNNING,
            robustness_schedule=self.schedule, started_at=now,
            total_cases=10, completed_cases=4, passed=3, failed=1)
        # 타임아웃(error) 마감 run: 5케이스 중 3개 판정(2 pass, 1 fail), 잔여 2개는 실패로 집계.
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=3, status=TapRun.Status.ERROR,
            robustness_schedule=self.schedule, started_at=now,
            total_cases=5, completed_cases=3, passed=2, failed=1, others=0)
        # 중단(Aborted) run: 6케이스 중 3개 판정(2 pass, 1 fail) — 나머지는 부분 집계에서 제외.
        TapRun.objects.create(
            tap_suite=self.tap_suite, session_id=4, status=TapRun.Status.STOPPED,
            robustness_schedule=self.schedule, started_at=now,
            total_cases=6, completed_cases=3, passed=2, failed=1)

        s = RobustnessSchedule.objects.get(pk=self.schedule.pk)  # memoize 없는 새 인스턴스
        self.assertTrue(s.is_tap)
        # passed = 8+3+2+2 = 15, failed = 2+1+(1+잔여2)+1 = 7, in_flight = 6(진행중 run 잔여만)
        self.assertEqual(s.success_count, 15)
        self.assertEqual(s.failure_count, 7)
        self.assertEqual(s.in_flight_count, 6)
        self.assertEqual(s.total_executions, 28)  # 15+7+6
        self.assertEqual(s.trigger_count, 4)
        self.assertAlmostEqual(s.success_rate, 68.2, places=1)  # 15/(15+7)*100


class ScheduleTriggerNowTapBranchTests(TestCase):
    """schedule_trigger_now 뷰 — TAP 스케줄은 _trigger_tap_once 로 분기(Jenkins _trigger_once 아님)."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="taptrig", password="x", role="qpe_admin", is_active=True)
        self.client.force_login(self.user)
        self.tap_suite = TapSuite.objects.create(tap_id=50, name="Wishlist", case_count=3)
        self.schedule = _make_tap_schedule(self.tap_suite, is_running=False)

    def test_manual_trigger_calls_trigger_tap_once_not_jenkins_trigger(self):
        self.assertIsNone(self.schedule.started_at)
        with (
            mock.patch("apps.robustness.services._trigger_tap_once") as mock_tap_trigger,
            mock.patch("apps.robustness.views._trigger_once") as mock_trigger_once,
        ):
            resp = self.client.post(f"/robustness/schedules/{self.schedule.pk}/trigger/")
        self.assertEqual(resp.status_code, 302)
        mock_tap_trigger.assert_called_once_with(self.schedule)
        mock_trigger_once.assert_not_called()
        self.schedule.refresh_from_db()
        self.assertIsNotNone(self.schedule.started_at)  # 세션 없으면 새로 시작
