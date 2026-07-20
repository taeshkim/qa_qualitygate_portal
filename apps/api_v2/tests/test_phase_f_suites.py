"""Phase F-2..3 — 단독 Test Suite Run API.

`apps.suites.services.trigger_suite_run` / `cancel_suite_run` 는 내부에서 Jenkins
호출을 수행 (`_try_trigger_next`, `_abort_suite_run`) 하므로, 테스트에서는
``apps.suites.views`` 의 헬퍼를 monkeypatch 로 noop 처리한다.
"""

from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.api_v2 import scopes as S
from apps.api_v2.models import ApiKey
from apps.suites.models import TestSuite, TestSuiteJob, TestSuiteJobRun, TestSuiteRun


def _auth(plaintext):
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}


def _noop(*args, **kwargs):
    return False


@override_settings(ROOT_URLCONF="apps.api_v2.tests.urls_phase_f")
class _SuiteRunTestBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="ops", password="pw", role="qpe_ops",
        )
        self.viewer = User.objects.create_user(
            username="vee", password="pw", role="viewer",
        )

        self.key_read, self.read_plaintext = ApiKey.issue(
            user=self.user, name="read", scopes=[S.RUNS_READ],
        )
        self.key_trigger, self.trigger_plaintext = ApiKey.issue(
            user=self.user, name="trig", scopes=[S.RUNS_READ, S.RUNS_TRIGGER],
        )
        self.key_control, self.control_plaintext = ApiKey.issue(
            user=self.user, name="ctrl", scopes=[S.RUNS_READ, S.RUNS_CONTROL],
        )
        self.key_viewer, self.viewer_plaintext = ApiKey.issue(
            user=self.viewer, name="vk", scopes=[S.RUNS_TRIGGER],
        )

        self.suite = TestSuite.objects.create(
            name="Sample Suite",
            jenkins_folder_path="folder/path",
            jenkins_folder_url="",
        )
        self.job = TestSuiteJob.objects.create(
            suite=self.suite,
            job_name="job-a",
            job_full_path="folder/path/job-a",
            order=0,
        )
        self.other_suite = TestSuite.objects.create(
            name="Other",
            jenkins_folder_path="other/path",
        )
        self.other_job = TestSuiteJob.objects.create(
            suite=self.other_suite,
            job_name="other-job",
            job_full_path="other/path/other-job",
        )

        # Jenkins 호출 헬퍼 noop 패치 (services 가 lazy import 로 호출).
        import apps.suites.views as sv

        self._orig_try = sv._try_trigger_next
        self._orig_abort = sv._abort_suite_run
        self._orig_poll = sv._poll_run_status
        sv._try_trigger_next = _noop
        sv._abort_suite_run = self._fake_abort
        sv._poll_run_status = _noop

        self.client = Client()

    def tearDown(self):
        import apps.suites.views as sv
        sv._try_trigger_next = self._orig_try
        sv._abort_suite_run = self._orig_abort
        sv._poll_run_status = self._orig_poll

    @staticmethod
    def _fake_abort(suite_run: TestSuiteRun) -> bool:
        """services.cancel_suite_run 이 부르는 _abort_suite_run 의 테스트용 대체.

        실제 구현은 Jenkins stop_build 를 호출 — 테스트에선 단순히 SuiteRun 을
        COMPLETED 로 마무리하고 PENDING job 들을 ABORTED 로 마킹.
        """
        if suite_run.status == TestSuiteRun.Status.COMPLETED:
            return False
        for jr in suite_run.job_runs.all():
            if jr.status in (
                TestSuiteJobRun.Status.PENDING,
                TestSuiteJobRun.Status.QUEUED,
                TestSuiteJobRun.Status.RUNNING,
            ):
                jr.status = TestSuiteJobRun.Status.ABORTED
                jr.completed_at = timezone.now()
                jr.save(update_fields=["status", "completed_at"])
        suite_run.status = TestSuiteRun.Status.COMPLETED
        suite_run.completed_at = timezone.now()
        suite_run.failed_jobs = suite_run.job_runs.filter(
            status=TestSuiteJobRun.Status.ABORTED,
        ).count()
        suite_run.save()
        return True


class SuiteRunTriggerTests(_SuiteRunTestBase):
    def _url_runs(self, suite_id=None):
        return f"/api/v2/suites/{suite_id or self.suite.pk}/runs/"

    def test_trigger_returns_201_and_payload(self):
        resp = self.client.post(
            self._url_runs(),
            data={"job_id": str(self.job.pk)},
            content_type="application/json",
            **_auth(self.trigger_plaintext),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["suite_id"], str(self.suite.pk))
        self.assertEqual(body["status"], "running")
        self.assertTrue(body["self_url"].startswith("/api/v2/suite-runs/"))
        self.assertTrue(body["detail_url"].startswith("/suites/runs/"))
        # 실제 SuiteRun 이 생성됐는지 검증.
        self.assertTrue(TestSuiteRun.objects.filter(pk=body["suite_run_id"]).exists())

    def test_trigger_without_job_id_returns_400_job_required(self):
        resp = self.client.post(
            self._url_runs(),
            data={},
            content_type="application/json",
            **_auth(self.trigger_plaintext),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "job_required")

    def test_trigger_with_other_suite_job_returns_400_invalid_parameters(self):
        resp = self.client.post(
            self._url_runs(),
            data={"job_id": str(self.other_job.pk)},
            content_type="application/json",
            **_auth(self.trigger_plaintext),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_parameters")

    def test_trigger_with_malformed_uuid_returns_400_invalid_parameters(self):
        resp = self.client.post(
            self._url_runs(),
            data={"job_id": "not-a-uuid"},
            content_type="application/json",
            **_auth(self.trigger_plaintext),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_parameters")

    def test_trigger_with_read_only_key_returns_403_scope_required(self):
        resp = self.client.post(
            self._url_runs(),
            data={"job_id": str(self.job.pk)},
            content_type="application/json",
            **_auth(self.read_plaintext),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")

    def test_trigger_with_viewer_role_returns_403_role_denied(self):
        resp = self.client.post(
            self._url_runs(),
            data={"job_id": str(self.job.pk)},
            content_type="application/json",
            **_auth(self.viewer_plaintext),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "role_denied")


class SuiteRunListTests(_SuiteRunTestBase):
    def _url(self):
        return f"/api/v2/suites/{self.suite.pk}/runs/"

    def test_list_returns_recent_runs(self):
        # 3 개 run 생성
        for i in range(3):
            run = TestSuiteRun.objects.create(
                suite=self.suite,
                status=TestSuiteRun.Status.COMPLETED,
                total_jobs=1,
                started_at=timezone.now(),
                completed_at=timezone.now(),
                triggered_by=self.user,
            )
            TestSuiteJobRun.objects.create(
                suite_run=run,
                job=self.job,
                status=TestSuiteJobRun.Status.SUCCESS,
            )
        resp = self.client.get(self._url(), **_auth(self.read_plaintext))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["results"]), 3)
        self.assertIn("suite", body["results"][0])
        self.assertEqual(body["results"][0]["suite"]["id"], str(self.suite.pk))

    def test_list_filter_by_status(self):
        TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=timezone.now(),
        )
        TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.RUNNING,
            total_jobs=1, started_at=timezone.now(),
        )
        resp = self.client.get(
            self._url() + "?status=running",
            **_auth(self.read_plaintext),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["status"], "running")

    def test_list_limit_clamped(self):
        for _ in range(5):
            TestSuiteRun.objects.create(
                suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
                total_jobs=1, started_at=timezone.now(),
            )
        resp = self.client.get(
            self._url() + "?limit=2",
            **_auth(self.read_plaintext),
        )
        self.assertEqual(resp.json()["count"], 2)

    def test_list_requires_runs_read_scope(self):
        # 트리거 전용 키엔 RUNS_READ 가 있으니 통과, 아무 스코프 없는 키 만들어 비교.
        key, plaintext = ApiKey.issue(
            user=self.user, name="meta", scopes=[S.META_READ],
        )
        resp = self.client.get(self._url(), **_auth(plaintext))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")


class SuiteRunDetailTests(_SuiteRunTestBase):
    def _make_run(self, status_=TestSuiteRun.Status.COMPLETED):
        run = TestSuiteRun.objects.create(
            suite=self.suite,
            status=status_,
            total_jobs=1,
            started_at=timezone.now(),
            completed_at=timezone.now() if status_ == TestSuiteRun.Status.COMPLETED else None,
            triggered_by=self.user,
        )
        jr = TestSuiteJobRun.objects.create(
            suite_run=run, job=self.job,
            status=TestSuiteJobRun.Status.SUCCESS,
        )
        return run, jr

    def test_detail_returns_job_runs_array(self):
        run, jr = self._make_run()
        resp = self.client.get(
            f"/api/v2/suite-runs/{run.pk}/",
            **_auth(self.read_plaintext),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["id"], str(run.pk))
        self.assertEqual(len(body["job_runs"]), 1)
        self.assertEqual(body["job_runs"][0]["id"], str(jr.pk))
        self.assertIn("console_url", body["job_runs"][0])
        self.assertTrue(
            body["job_runs"][0]["console_url"].endswith(f"/{jr.pk}/console/")
        )

    def test_detail_404(self):
        resp = self.client.get(
            f"/api/v2/suite-runs/{uuid.uuid4()}/",
            **_auth(self.read_plaintext),
        )
        self.assertEqual(resp.status_code, 404)

    def test_detail_triggers_poll_when_not_completed(self):
        """폴링 함수가 호출되는지 확인."""
        run, _ = self._make_run(status_=TestSuiteRun.Status.RUNNING)
        import apps.suites.views as sv

        calls = []

        def _track(suite_run):
            calls.append(suite_run.pk)

        original = sv._poll_run_status
        sv._poll_run_status = _track
        try:
            resp = self.client.get(
                f"/api/v2/suite-runs/{run.pk}/",
                **_auth(self.read_plaintext),
            )
        finally:
            sv._poll_run_status = original

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls, [run.pk])


class SuiteRunCancelTests(_SuiteRunTestBase):
    def _make_running_run(self):
        run = TestSuiteRun.objects.create(
            suite=self.suite,
            status=TestSuiteRun.Status.RUNNING,
            total_jobs=1,
            started_at=timezone.now(),
            triggered_by=self.user,
        )
        TestSuiteJobRun.objects.create(
            suite_run=run, job=self.job,
            status=TestSuiteJobRun.Status.PENDING,
        )
        return run

    def test_cancel_running_run_returns_200(self):
        run = self._make_running_run()
        resp = self.client.post(
            f"/api/v2/suite-runs/{run.pk}/cancel/",
            **_auth(self.control_plaintext),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["status"], "completed")
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")

    def test_cancel_completed_run_returns_409_not_actionable(self):
        run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        resp = self.client.post(
            f"/api/v2/suite-runs/{run.pk}/cancel/",
            **_auth(self.control_plaintext),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "not_actionable")

    def test_cancel_with_read_key_returns_403_scope_required(self):
        run = self._make_running_run()
        resp = self.client.post(
            f"/api/v2/suite-runs/{run.pk}/cancel/",
            **_auth(self.read_plaintext),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")


class SuiteJobRunConsoleTests(_SuiteRunTestBase):
    def _make_job_run(self, console: str = "") -> TestSuiteJobRun:
        run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        return TestSuiteJobRun.objects.create(
            suite_run=run, job=self.job,
            status=TestSuiteJobRun.Status.SUCCESS,
            console_output=console,
            jenkins_build_url="https://jenkins/build/1/",
        )

    def test_console_returns_full_when_under_limit(self):
        jr = self._make_job_run(console="line1\nline2\nline3\n")
        resp = self.client.get(
            f"/api/v2/suite-job-runs/{jr.pk}/console/",
            **_auth(self.read_plaintext),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["stage"], "suite")
        self.assertEqual(body["output"], "line1\nline2\nline3\n")
        self.assertEqual(body["length"], len("line1\nline2\nline3\n"))
        self.assertFalse(body["truncated"])
        self.assertEqual(body["build_url"], "https://jenkins/build/1/")

    def test_console_tail_n(self):
        jr = self._make_job_run(console="a\nb\nc\nd\ne\n")
        resp = self.client.get(
            f"/api/v2/suite-job-runs/{jr.pk}/console/?tail=2",
            **_auth(self.read_plaintext),
        )
        body = resp.json()
        self.assertEqual(body["output"], "d\ne\n")
        self.assertTrue(body["truncated"])

    def test_console_auto_truncates_when_over_1mb(self):
        # 1MB+ 콘솔
        huge = "x" * (1_000_000 + 100)
        jr = self._make_job_run(console=huge)
        resp = self.client.get(
            f"/api/v2/suite-job-runs/{jr.pk}/console/",
            **_auth(self.read_plaintext),
        )
        body = resp.json()
        self.assertTrue(body["truncated"])
        # tail 50_000
        self.assertEqual(len(body["output"]), 50_000)
        self.assertEqual(body["length"], len(huge))
