"""apps.visualization 단위 테스트.

검증 항목:
  1. /visualization/ 로그인 렌더링 + Canvas/JSON feed/정적 JS 연결
  2. /visualization/data/ GET → 200, JSON 에 "runs" 키 존재
  3. suite pass/fail/skip/running 상태 매핑 정확성
  4. Quality Gate Run 은 피드에서 제외됨 (Test Suite 만 표시)
  5. pending 인 run 은 결과에 포함되지 않음
  6. `after` 파라미터와 completed_at 기준 증분 폴링
  7. 응답 각 항목이 요구 스키마 키(id/kind/label/status/ts/detail)를 가짐
"""

import datetime

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.db import connection
from django.templatetags.static import static
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.timezone import utc

from apps.suites.models import (
    TestSuite,
    TestSuiteJob,
    TestSuiteJobRun,
    TestSuiteRun,
)
from apps.qualitygates.models import QualityGate, QualityGateRun

User = get_user_model()


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _make_suite(name="test-suite"):
    return TestSuite.objects.create(
        name=name,
        jenkins_folder_path=f"folder/{name}",
    )


def _make_suite_run(suite, status, failed_jobs=0, passed_jobs=1, total_jobs=1,
                    started_at=None, seq_no=1):
    now = datetime.datetime.now(tz=utc)
    started = started_at or now
    # 완료 run 의 completed_at 은 started_at 과 동일하게 — ts(=completed_at) 와 증분 필터를 일관되게.
    completed = started if status == TestSuiteRun.Status.COMPLETED else None
    return TestSuiteRun.objects.create(
        suite=suite,
        status=status,
        failed_jobs=failed_jobs,
        passed_jobs=passed_jobs,
        total_jobs=total_jobs,
        started_at=started,
        completed_at=completed,
        seq_no=seq_no,
    )


def _make_job_run(suite_run, *, verdict="", suffix="1", status=TestSuiteJobRun.Status.FAILURE):
    job = TestSuiteJob.objects.create(
        suite=suite_run.suite,
        job_name=f"triage-job-{suffix}",
        job_full_path=f"{suite_run.suite.jenkins_folder_path}/triage-job-{suffix}",
    )
    return TestSuiteJobRun.objects.create(
        suite_run=suite_run,
        job=job,
        status=status,
        verdict=verdict,
        started_at=suite_run.started_at,
        completed_at=suite_run.completed_at,
    )


def _make_gate(name="test-gate"):
    return QualityGate.objects.create(
        name=name,
        deploy_job_path="deploy/job",
    )


def _make_qg_run(gate, status, created_at=None, seq_no=1):
    now = datetime.datetime.now(tz=utc)
    run = QualityGateRun(
        gate=gate,
        status=status,
        seq_no=seq_no,
    )
    run.save()
    if created_at is not None:
        QualityGateRun.objects.filter(pk=run.pk).update(created_at=created_at)
        run.refresh_from_db()
    return run


class DataEndpointBasicTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", password="pass", is_active=True, role="qpe_admin"
        )
        self.client.force_login(self.user)
        self.url = reverse("visualization:data")

    def test_returns_200_with_runs_key(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Content-Type"], "application/json")
        data = resp.json()
        self.assertIn("runs", data)
        self.assertIn("server_ts", data)
        self.assertIn("reset", data)
        self.assertIsInstance(data["runs"], list)
        self.assertIsInstance(data["server_ts"], int)
        self.assertIs(data["reset"], False)

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get(self.url)
        # 로그인 필요 → 302 리다이렉트
        self.assertEqual(resp.status_code, 302)

    def test_non_admin_forbidden(self):
        """Visualization 은 QPE Admin 전용 — ops 는 피드 403 / 페이지 대시보드 리다이렉트."""
        ops = User.objects.create_user(username="ops-v", password="pass", role="qpe_ops", is_active=True)
        self.client.force_login(ops)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        resp = self.client.get(reverse("visualization:index"))
        self.assertEqual((resp.status_code, resp["Location"]), (302, reverse("dashboard:index")))

    def test_index_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("visualization:index"))
        self.assertEqual(resp.status_code, 302)


class IndexPageRenderingTest(TestCase):
    """전면 UI 개편 시에도 유지되어야 할 프런트-백엔드 연결 계약."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="visualization-page-user", password="pass", is_active=True, role="qpe_admin"
        )
        self.client.force_login(self.user)
        self.url = reverse("visualization:index")

    def test_authenticated_index_renders_visualization_template(self):
        resp = self.client.get(self.url)

        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "visualization/index.html")

    def test_index_wires_canvas_runtime_and_data_feed(self):
        resp = self.client.get(self.url)

        # 주변 HUD/CSS 구조는 자유롭게 바꿔도, 런타임이 찾는 ID와
        # data endpoint 및 초기화 호출은 꾸준히 연결되어야 한다.
        self.assertContains(resp, 'id="qg-highway"')
        self.assertContains(resp, 'id="hw-tip"')
        self.assertContains(resp, 'id="hw-empty"')
        self.assertContains(resp, reverse("visualization:data"))
        self.assertContains(resp, "QGHighway.init")
        self.assertContains(
            resp,
            reverse(
                "suites:run_detail",
                kwargs={"pk": "00000000-0000-0000-0000-000000000000"},
            ),
        )

    def test_index_loads_discoverable_frontend_assets(self):
        resp = self.client.get(self.url)
        for asset_path in (
            "css/qg_highway.css",
            "js/qg_highway.js",
            "images/visualization/highway-command-bg.webp",
        ):
            with self.subTest(asset_path=asset_path):
                self.assertIsNotNone(finders.find(asset_path))
                # cache-busting query string은 변경 가능하므로 정적 URL 본체만 검증한다.
                self.assertContains(resp, static(asset_path))


class SuiteRunMappingTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="u2", password="pass", is_active=True, role="qpe_admin"
        )
        self.client.force_login(self.user)
        self.url = reverse("visualization:data")
        self.suite = _make_suite("am-dweb-regression-api-prd")

    def _get_runs(self):
        return self.client.get(self.url).json()["runs"]

    def test_suite_pass_mapping(self):
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=0, passed_jobs=3, total_jobs=3, seq_no=1)
        runs = self._get_runs()
        suite_runs = [r for r in runs if r["kind"] == "suite"]
        self.assertEqual(len(suite_runs), 1)
        self.assertEqual(suite_runs[0]["status"], "pass")

    def test_suite_fail_mapping(self):
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=1, passed_jobs=2, total_jobs=3, seq_no=2)
        runs = self._get_runs()
        suite_runs = [r for r in runs if r["kind"] == "suite"]
        self.assertEqual(suite_runs[0]["status"], "fail")

    def test_suite_all_failed_jobs_triaged_as_skip_maps_to_skip(self):
        run = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.COMPLETED,
            failed_jobs=2,
            passed_jobs=1,
            total_jobs=3,
            seq_no=9,
        )
        _make_job_run(
            run,
            verdict=TestSuiteJobRun.Verdict.SKIP,
            suffix="all-skip-1",
        )
        _make_job_run(
            run,
            verdict=TestSuiteJobRun.Verdict.SKIP,
            suffix="all-skip-2",
        )

        by_id = {item["id"]: item for item in self._get_runs()}

        self.assertEqual(by_id[str(run.pk)]["status"], "skip")

    def test_suite_all_failed_jobs_aborted_maps_to_skip(self):
        """전부-중단(ABORTED) 완료 run 은 장애가 아니므로 JS 가 아는 'skip'(피트 진입)으로 매핑된다."""
        run = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.COMPLETED,
            failed_jobs=1,
            passed_jobs=1,
            total_jobs=2,
            seq_no=20,
        )
        _make_job_run(
            run,
            status=TestSuiteJobRun.Status.ABORTED,
            suffix="all-aborted-1",
        )

        by_id = {item["id"]: item for item in self._get_runs()}

        self.assertEqual(by_id[str(run.pk)]["status"], "skip")

    def test_suite_aborted_plus_untriaged_failure_maps_to_fail(self):
        """ABORTED + 미분류 FAILURE 혼재 — 실장애가 있으므로 'fail' 로 매핑된다."""
        run = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.COMPLETED,
            failed_jobs=2,
            passed_jobs=0,
            total_jobs=2,
            seq_no=21,
        )
        _make_job_run(
            run,
            status=TestSuiteJobRun.Status.ABORTED,
            suffix="mixed-aborted-1",
        )
        _make_job_run(
            run,
            status=TestSuiteJobRun.Status.FAILURE,
            verdict=TestSuiteJobRun.Verdict.UNTRIAGED,
            suffix="mixed-aborted-2",
        )

        by_id = {item["id"]: item for item in self._get_runs()}

        self.assertEqual(by_id[str(run.pk)]["status"], "fail")

    def test_suite_any_real_or_untriaged_failure_maps_to_fail(self):
        runs = []
        for seq_no, verdict in (
            (10, TestSuiteJobRun.Verdict.FAILURE),
            (11, TestSuiteJobRun.Verdict.UNTRIAGED),
        ):
            run = _make_suite_run(
                self.suite,
                TestSuiteRun.Status.COMPLETED,
                failed_jobs=2,
                passed_jobs=0,
                total_jobs=2,
                seq_no=seq_no,
            )
            _make_job_run(
                run,
                verdict=TestSuiteJobRun.Verdict.SKIP,
                suffix=f"mixed-skip-{seq_no}",
            )
            _make_job_run(
                run,
                verdict=verdict,
                suffix=f"mixed-fail-{seq_no}",
            )
            runs.append(run)

        by_id = {item["id"]: item for item in self._get_runs()}

        for run in runs:
            with self.subTest(run_id=run.pk):
                self.assertEqual(by_id[str(run.pk)]["status"], "fail")

    def test_suite_running_mapping(self):
        _make_suite_run(self.suite, TestSuiteRun.Status.RUNNING,
                        failed_jobs=0, passed_jobs=0, total_jobs=3, seq_no=3)
        runs = self._get_runs()
        suite_runs = [r for r in runs if r["kind"] == "suite"]
        self.assertEqual(suite_runs[0]["status"], "running")

    def test_suite_pending_excluded(self):
        _make_suite_run(self.suite, TestSuiteRun.Status.PENDING, seq_no=4)
        runs = self._get_runs()
        suite_runs = [r for r in runs if r["kind"] == "suite"]
        self.assertEqual(len(suite_runs), 0)

    def test_suite_schema_keys(self):
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=0, passed_jobs=2, total_jobs=2, seq_no=5)
        runs = self._get_runs()
        suite_runs = [r for r in runs if r["kind"] == "suite"]
        item = suite_runs[0]
        for key in ("id", "kind", "label", "status", "ts", "detail"):
            self.assertIn(key, item, f"key '{key}' missing from suite run dict")

    def test_suite_label_with_short_label_and_seq(self):
        """short_label + seq_no 가 있으면 '{short_label} · #{seq_no}' 형식."""
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=0, passed_jobs=1, total_jobs=1, seq_no=7)
        runs = self._get_runs()
        suite_runs = [r for r in runs if r["kind"] == "suite"]
        # am-dweb-regression-api-prd → regression/api 제거 → am-dweb-prd
        self.assertIn("·", suite_runs[0]["label"])
        self.assertIn("#7", suite_runs[0]["label"])

    def test_suite_detail_format(self):
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=1, passed_jobs=4, total_jobs=5, seq_no=8)
        runs = self._get_runs()
        suite_runs = [r for r in runs if r["kind"] == "suite"]
        self.assertEqual(suite_runs[0]["detail"], "4/5 jobs")


class QGExclusionTest(TestCase):
    """Quality Gate Run 은 시각화 피드에서 제외된다 (Test Suite 만 표시)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="u3", password="pass", is_active=True, role="qpe_admin"
        )
        self.client.force_login(self.user)
        self.url = reverse("visualization:data")
        self.gate = _make_gate("my-gate")
        self.suite = _make_suite("vis-suite")

    def _get_runs(self):
        return self.client.get(self.url).json()["runs"]

    def test_qg_runs_not_in_feed(self):
        # QG run 여러 상태를 만들어도 피드에는 하나도 없어야 함
        _make_qg_run(self.gate, QualityGateRun.Status.COMPLETED, seq_no=1)
        _make_qg_run(self.gate, QualityGateRun.Status.FAILED, seq_no=2)
        _make_qg_run(self.gate, QualityGateRun.Status.TESTING, seq_no=3)
        runs = self._get_runs()
        self.assertEqual([r for r in runs if r["kind"] == "qg"], [])

    def test_only_suite_runs_returned(self):
        _make_qg_run(self.gate, QualityGateRun.Status.COMPLETED, seq_no=1)
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=0, passed_jobs=2, total_jobs=2, seq_no=1)
        runs = self._get_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["kind"], "suite")

    def test_after_mode_also_excludes_qg(self):
        _make_qg_run(self.gate, QualityGateRun.Status.FAILED, seq_no=1)
        after_epoch = int(datetime.datetime(2000, 1, 1, tzinfo=utc).timestamp())
        runs = self.client.get(self.url, {"after": after_epoch}).json()["runs"]
        self.assertEqual([r for r in runs if r["kind"] == "qg"], [])


class AfterParamTest(TestCase):
    """after 파라미터로 과거 run 이 걸러지는지 검증."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="u4", password="pass", is_active=True, role="qpe_admin"
        )
        self.client.force_login(self.user)
        self.url = reverse("visualization:data")
        self.suite = _make_suite("filter-suite")

    def test_after_filters_old_runs(self):
        now = datetime.datetime.now(tz=utc)
        old_time = now - datetime.timedelta(minutes=10)
        recent_time = now - datetime.timedelta(minutes=1)

        # 오래된 run
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=0, passed_jobs=1, total_jobs=1,
                        started_at=old_time, seq_no=1)
        # 최근 run
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=0, passed_jobs=1, total_jobs=1,
                        started_at=recent_time, seq_no=2)

        # 15분 stale-reset 구간 안의 after → 그보다 오래된 run만 걸러짐
        after_epoch = int((now - datetime.timedelta(minutes=5)).timestamp())
        resp = self.client.get(self.url, {"after": after_epoch})
        runs = resp.json()["runs"]
        suite_runs = [r for r in runs if r["kind"] == "suite"]

        # 10분 전 run은 after보다 이전 → 제외, 최근 run만 포함
        self.assertEqual(len(suite_runs), 1)
        self.assertGreater(suite_runs[0]["ts"], after_epoch)

    def test_after_no_new_runs_returns_empty(self):
        old_time = datetime.datetime(2020, 1, 1, tzinfo=utc)
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=0, passed_jobs=1, total_jobs=1,
                        started_at=old_time, seq_no=1)

        # after = 현재 시각 → 아무것도 없음
        after_epoch = int(datetime.datetime.now(tz=utc).timestamp()) + 3600
        resp = self.client.get(self.url, {"after": after_epoch})
        runs = resp.json()["runs"]
        self.assertEqual(runs, [])

    def test_after_ascending_order(self):
        """증분 모드는 ts 오름차순 반환."""
        now = datetime.datetime.now(tz=utc)
        t1 = now - datetime.timedelta(minutes=4)
        t2 = now - datetime.timedelta(minutes=2)
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=0, passed_jobs=1, total_jobs=1,
                        started_at=t1, seq_no=1)
        _make_suite_run(self.suite, TestSuiteRun.Status.COMPLETED,
                        failed_jobs=0, passed_jobs=1, total_jobs=1,
                        started_at=t2, seq_no=2)

        after_epoch = int((now - datetime.timedelta(minutes=5)).timestamp())
        resp = self.client.get(self.url, {"after": after_epoch})
        runs = [r for r in resp.json()["runs"] if r["kind"] == "suite"]

        self.assertEqual(len(runs), 2)
        # 오름차순 확인
        self.assertLessEqual(runs[0]["ts"], runs[1]["ts"])

    def test_run_started_before_cutoff_is_returned_when_completed_after_it(self):
        """시드 이후 running→completed 전환이 증분 피드에서 누락되지 않음."""
        cutoff = datetime.datetime.now(tz=utc) - datetime.timedelta(minutes=1)
        run = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.RUNNING,
            failed_jobs=0,
            passed_jobs=0,
            total_jobs=2,
            started_at=cutoff - datetime.timedelta(hours=1),
            seq_no=3,
        )
        completed_at = cutoff + datetime.timedelta(seconds=10)
        TestSuiteRun.objects.filter(pk=run.pk).update(
            status=TestSuiteRun.Status.COMPLETED,
            passed_jobs=2,
            completed_at=completed_at,
        )

        runs = self.client.get(
            self.url, {"after": int(cutoff.timestamp())}
        ).json()["runs"]

        self.assertEqual([item["id"] for item in runs], [str(run.pk)])
        self.assertEqual(runs[0]["status"], "pass")
        self.assertEqual(runs[0]["ts"], int(completed_at.timestamp()))

    def test_running_started_before_cutoff_is_reemitted_with_latest_progress(self):
        """RUNNING은 활동 timestamp가 안 바뀌어도 진행률 갱신을 위해 재전송."""
        cutoff = datetime.datetime.now(tz=utc) - datetime.timedelta(minutes=1)
        run = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.RUNNING,
            passed_jobs=0,
            total_jobs=3,
            started_at=cutoff - datetime.timedelta(hours=2),
            seq_no=4,
        )
        TestSuiteRun.objects.filter(pk=run.pk).update(passed_jobs=2)

        runs = self.client.get(
            self.url, {"after": int(cutoff.timestamp())}
        ).json()["runs"]

        self.assertEqual([item["id"] for item in runs], [str(run.pk)])
        self.assertEqual(runs[0]["status"], "running")
        self.assertEqual(runs[0]["detail"], "2/3 jobs")

    def test_running_matching_both_incremental_conditions_is_not_duplicated(self):
        """새로 시작한 RUNNING은 activity/상태 조건을 모두 만족해도 한 번만 반환."""
        cutoff = datetime.datetime.now(tz=utc) - datetime.timedelta(minutes=1)
        run = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.RUNNING,
            passed_jobs=1,
            total_jobs=2,
            started_at=cutoff + datetime.timedelta(seconds=10),
            seq_no=5,
        )

        runs = self.client.get(
            self.url, {"after": int(cutoff.timestamp())}
        ).json()["runs"]
        ids = [item["id"] for item in runs]

        self.assertEqual(ids.count(str(run.pk)), 1)
        self.assertEqual(len(ids), len(set(ids)))

    def test_run_triaged_after_cutoff_is_reemitted_with_skip_status(self):
        """완료시각이 오래됐어도 after 이후 triage되면 최신 상태를 재전송."""
        cutoff = datetime.datetime.now(tz=utc) - datetime.timedelta(minutes=1)
        run = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.COMPLETED,
            failed_jobs=1,
            passed_jobs=0,
            total_jobs=1,
            started_at=cutoff - datetime.timedelta(hours=2),
            seq_no=7,
        )
        job_run = _make_job_run(
            run,
            verdict=TestSuiteJobRun.Verdict.UNTRIAGED,
            suffix="triaged-after-cutoff",
        )
        TestSuiteJobRun.objects.filter(pk=job_run.pk).update(
            verdict=TestSuiteJobRun.Verdict.SKIP,
            triaged_at=cutoff + datetime.timedelta(seconds=10),
        )

        with CaptureQueriesContext(connection) as captured:
            resp = self.client.get(
                self.url, {"after": int(cutoff.timestamp())}
            )
        runs = resp.json()["runs"]
        feed_queries = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "suites_test_suite_run"' in query["sql"]
        ]

        self.assertEqual([item["id"] for item in runs], [str(run.pk)])
        self.assertEqual(runs[0]["status"], "skip")
        self.assertLess(runs[0]["ts"], int(cutoff.timestamp()))
        # feed 본문은 1 query이며 서브쿼리가 triage 변경을 찾고, Python N+1은 없다.
        self.assertEqual(len(feed_queries), 1)
        self.assertIn(" IN (SELECT ", feed_queries[0].upper())

    def test_invalid_after_falls_back_to_seed_feed(self):
        run = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.COMPLETED,
            failed_jobs=0,
            passed_jobs=1,
            total_jobs=1,
            seq_no=4,
        )

        resp = self.client.get(self.url, {"after": "not-an-epoch"})

        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["reset"], True)
        self.assertEqual(
            [item["id"] for item in resp.json()["runs"]],
            [str(run.pk)],
        )

    def test_out_of_range_numeric_after_falls_back_to_seed_feed(self):
        """int 변환은 되지만 datetime 범위 밖인 epoch도 500 대신 seed 처리."""
        run = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.COMPLETED,
            passed_jobs=1,
            total_jobs=1,
            seq_no=6,
        )

        resp = self.client.get(self.url, {"after": "9" * 100})

        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["reset"], True)
        self.assertEqual(
            [item["id"] for item in resp.json()["runs"]],
            [str(run.pk)],
        )

    def test_after_feed_returns_entire_burst_in_ascending_order(self):
        """60건을 넘는 증분 burst도 자르지 않고 오래된 순으로 모두 반환."""
        cutoff = datetime.datetime.now(tz=utc) - datetime.timedelta(minutes=10)
        created = [
            _make_suite_run(
                self.suite,
                TestSuiteRun.Status.COMPLETED,
                started_at=cutoff + datetime.timedelta(seconds=seq_no),
                seq_no=seq_no,
            )
            for seq_no in range(10, 75)
        ]

        runs = self.client.get(
            self.url, {"after": int(cutoff.timestamp())}
        ).json()["runs"]
        ids = [item["id"] for item in runs]

        self.assertEqual(len(runs), len(created))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            [item["ts"] for item in runs],
            sorted(item["ts"] for item in runs),
        )
        self.assertEqual(ids[0], str(created[0].pk))
        self.assertEqual(ids[-1], str(created[-1].pk))

    def test_stale_after_resets_to_latest_seed(self):
        """오래 숨겨둔 탭은 전체 backlog 대신 최신 40건 snapshot으로 재시드."""
        now = datetime.datetime.now(tz=utc)
        created = [
            _make_suite_run(
                self.suite,
                TestSuiteRun.Status.COMPLETED,
                started_at=now - datetime.timedelta(seconds=45 - seq_no),
                seq_no=seq_no,
            )
            for seq_no in range(1, 46)
        ]

        resp = self.client.get(
            self.url,
            {"after": int((now - datetime.timedelta(minutes=16)).timestamp())},
        )
        data = resp.json()
        ids = [item["id"] for item in data["runs"]]

        self.assertIs(data["reset"], True)
        self.assertEqual(len(ids), 40)
        self.assertEqual(ids[0], str(created[-1].pk))
        self.assertNotIn(str(created[0].pk), ids)


class SeedFeedOrderingTest(TestCase):
    """초기 피드는 최근 Test Suite Run 순서로 반환한다."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="seed-feed-user", password="pass", is_active=True, role="qpe_admin"
        )
        self.client.force_login(self.user)
        self.url = reverse("visualization:data")
        self.suite = _make_suite("seed-order-suite")

    def test_seed_feed_is_newest_first(self):
        older = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.COMPLETED,
            started_at=datetime.datetime(2025, 1, 1, tzinfo=utc),
            seq_no=1,
        )
        newer = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.RUNNING,
            started_at=datetime.datetime(2025, 1, 2, tzinfo=utc),
            seq_no=2,
        )

        runs = self.client.get(self.url).json()["runs"]

        self.assertEqual(
            [item["id"] for item in runs],
            [str(newer.pk), str(older.pk)],
        )

    def test_seed_feed_is_bounded_to_recent_runs(self):
        base = datetime.datetime(2025, 1, 1, tzinfo=utc)
        created = [
            _make_suite_run(
                self.suite,
                TestSuiteRun.Status.COMPLETED,
                started_at=base + datetime.timedelta(minutes=seq_no),
                seq_no=seq_no,
            )
            for seq_no in range(1, 46)
        ]

        runs = self.client.get(self.url).json()["runs"]
        returned_ids = [item["id"] for item in runs]

        # 문서상 최대 40건 이내의 최근 실행만 유지되는지 검증한다.
        self.assertLessEqual(len(runs), 40)
        self.assertEqual(returned_ids[0], str(created[-1].pk))
        self.assertNotIn(str(created[0].pk), returned_ids)

    def test_seed_feed_uses_recent_completion_instead_of_recent_start(self):
        """오래전에 시작했어도 가장 최근 완료된 run은 seed 선두에 포함된다."""
        target = _make_suite_run(
            self.suite,
            TestSuiteRun.Status.COMPLETED,
            started_at=datetime.datetime(2020, 1, 1, tzinfo=utc),
            seq_no=1,
        )
        completed_at = datetime.datetime(2026, 1, 1, tzinfo=utc)
        TestSuiteRun.objects.filter(pk=target.pk).update(completed_at=completed_at)

        for seq_no in range(2, 47):
            _make_suite_run(
                self.suite,
                TestSuiteRun.Status.COMPLETED,
                started_at=(
                    datetime.datetime(2025, 1, 1, tzinfo=utc)
                    + datetime.timedelta(minutes=seq_no)
                ),
                seq_no=seq_no,
            )

        runs = self.client.get(self.url).json()["runs"]

        self.assertEqual(len(runs), 40)
        self.assertEqual(runs[0]["id"], str(target.pk))
        self.assertEqual(runs[0]["ts"], int(completed_at.timestamp()))
