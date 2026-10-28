"""Slack 실패 알림 Triage 버튼(Failure/Skip) 테스트 — 네트워크는 전부 mock."""
import json
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJob, TestSuiteJobRun
from apps.notifications import triage

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        self.suite = TestSuite.objects.create(name="am-x", jenkins_folder_path="a/f")
        self.run = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, failed_jobs=1)
        self.job = TestSuiteJob.objects.create(suite=self.suite, job_name="am-x_1", job_full_path="a/x_1")
        self.jr = TestSuiteJobRun.objects.create(
            suite_run=self.run, job=self.job, status=TestSuiteJobRun.Status.FAILURE)
        self.ops = User.objects.create_user(
            username="ops1", password="x", role="qpe_ops", slack_user_id="U_OPS")
        self.viewer = User.objects.create_user(
            username="v1", password="x", role="viewer", slack_user_id="U_VIEW")


class ApplyTriageTests(_Base):
    def test_failure_sets_verdict(self):
        res = triage.apply_triage_action(triage.ACTION_FAILURE, str(self.jr.pk), "U_OPS")
        self.assertTrue(res["ok"])
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "failure")
        self.assertEqual(self.jr.triaged_by, self.ops)
        self.assertIsNotNone(self.jr.triaged_at)

    def test_skip_sets_verdict(self):
        res = triage.apply_triage_action(triage.ACTION_SKIP, str(self.jr.pk), "U_OPS")
        self.assertTrue(res["ok"])
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "skip")

    def test_retriage_overwrites(self):
        triage.apply_triage_action(triage.ACTION_FAILURE, str(self.jr.pk), "U_OPS")
        triage.apply_triage_action(triage.ACTION_SKIP, str(self.jr.pk), "U_OPS")
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "skip")  # 최신값

    def test_viewer_forbidden(self):
        res = triage.apply_triage_action(triage.ACTION_FAILURE, str(self.jr.pk), "U_VIEW")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "forbidden")
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "")

    def test_unmapped_user(self):
        res = triage.apply_triage_action(triage.ACTION_FAILURE, str(self.jr.pk), "U_UNKNOWN")
        self.assertEqual(res["reason"], "unmapped_user")

    def test_not_found(self):
        res = triage.apply_triage_action(
            triage.ACTION_FAILURE, "11111111-1111-1111-1111-111111111111", "U_OPS")
        self.assertEqual(res["reason"], "not_found")

    def test_unknown_action(self):
        res = triage.apply_triage_action("bogus", str(self.jr.pk), "U_OPS")
        self.assertEqual(res["reason"], "unknown_action")

    def test_chat_update_called_when_ts_stored(self):
        self.run.slack_message_ts = "123.45"
        self.run.slack_channel_id = "C1"
        self.run.save(update_fields=["slack_message_ts", "slack_channel_id"])
        with mock.patch.object(triage, "_slack_web") as m:
            res = triage.apply_triage_action(triage.ACTION_FAILURE, str(self.jr.pk), "U_OPS")
        self.assertTrue(res["ok"])
        m.return_value.chat_update.assert_called_once()
        _, kwargs = m.return_value.chat_update.call_args
        self.assertEqual(kwargs["channel"], "C1")
        self.assertEqual(kwargs["ts"], "123.45")


class TriageBlocksTests(_Base):
    def test_untriaged_has_buttons_with_job_run_value(self):
        blocks = triage.build_triage_blocks(self.run)
        dumped = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("triage_failure", dumped)
        self.assertIn("triage_skip", dumped)
        self.assertIn(str(self.jr.pk), dumped)  # value = job_run_id
        self.assertTrue(any(b.get("type") == "actions" for b in blocks))

    def test_triaged_shows_done_no_buttons(self):
        self.jr.verdict = "skip"
        self.jr.triaged_by = self.ops
        self.jr.save(update_fields=["verdict", "triaged_by"])
        blocks = triage.build_triage_blocks(self.run)
        self.assertFalse(any(b.get("type") == "actions" for b in blocks))
        self.assertIn("분류됨", json.dumps(blocks, ensure_ascii=False))

    def test_no_failed_jobs_empty(self):
        self.jr.status = TestSuiteJobRun.Status.SUCCESS
        self.jr.save(update_fields=["status"])
        self.assertEqual(triage.build_triage_blocks(self.run), [])


class FailPayloadTests(_Base):
    def test_fail_payload_contains_buttons(self):
        from apps.notifications.services import build_suite_fail_payload
        text, blocks = build_suite_fail_payload(self.run)
        d = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("triage_failure", d)
        self.assertIn("Suite Run FAIL", text)


class SendGatingTests(_Base):
    def _enable_slack(self):
        from apps.notifications.models import SlackNotificationSettings
        s = SlackNotificationSettings.get_solo()
        s.enabled = True
        s.notify_suite_run_failed = True
        s.save()

    def test_flag_on_sends_blocks_and_stores_ts(self):
        from apps.notifications import services
        from apps.notifications import events as E
        self._enable_slack()
        with mock.patch.dict(os.environ, {"SLACK_TRIAGE_BUTTONS": "1"}), \
             mock.patch.object(services, "_resolve_channel", return_value="#c"), \
             mock.patch.object(services, "_do_post",
                               return_value={"ok": True, "ts": "9.9", "channel": "CZ"}) as m:
            ok = services.send_suite_run_event(E.SUITE_RUN_FAILED, self.run)
        self.assertTrue(ok)
        # blocks 로 발송됐는지
        _, kwargs = m.call_args
        self.assertIn("blocks", kwargs)
        # ts/channel 저장됐는지
        self.run.refresh_from_db()
        self.assertEqual(self.run.slack_message_ts, "9.9")
        self.assertEqual(self.run.slack_channel_id, "CZ")

    def test_flag_off_uses_legacy_path_no_ts(self):
        from apps.notifications import services
        from apps.notifications import events as E
        self._enable_slack()
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(services, "_resolve_channel", return_value="#c"), \
             mock.patch.object(services, "_post_message", return_value=True) as m:
            os.environ.pop("SLACK_TRIAGE_BUTTONS", None)
            ok = services.send_suite_run_event(E.SUITE_RUN_FAILED, self.run)
        self.assertTrue(ok)
        m.assert_called_once()  # 기존 attachment 경로
        self.run.refresh_from_db()
        self.assertEqual(self.run.slack_message_ts, "")  # ts 저장 안 함


class DispatchTests(_Base):
    def test_dispatch_routes_to_apply(self):
        from apps.notifications.management.commands.slack_triage_listener import dispatch_interaction
        payload = {
            "user": {"id": "U_OPS"},
            "response_url": "",
            "actions": [{"action_id": "triage_failure", "value": str(self.jr.pk)}],
        }
        with mock.patch.object(triage, "update_suite_fail_message", return_value=False):
            dispatch_interaction(payload)
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "failure")

    def test_dispatch_ignores_non_triage_action(self):
        from apps.notifications.management.commands.slack_triage_listener import dispatch_interaction
        payload = {"user": {"id": "U_OPS"}, "actions": [{"action_id": "open_report", "value": "x"}]}
        dispatch_interaction(payload)  # 아무 것도 안 함
        self.jr.refresh_from_db()
        self.assertEqual(self.jr.verdict, "")


class ResultLabelTests(_Base):
    def test_result_label(self):
        self.assertEqual(self.jr.result_label, "Fail")  # 미분류
        self.jr.verdict = "failure"
        self.assertEqual(self.jr.result_label, "Failure")
        self.jr.verdict = "skip"
        self.assertEqual(self.jr.result_label, "Skip")
        self.jr.status = TestSuiteJobRun.Status.SUCCESS
        self.jr.verdict = ""
        self.assertEqual(self.jr.result_label, "Pass")


class FlagTests(TestCase):
    def test_triage_buttons_enabled_flag(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SLACK_TRIAGE_BUTTONS", None)
            self.assertFalse(triage.triage_buttons_enabled())
        with mock.patch.dict(os.environ, {"SLACK_TRIAGE_BUTTONS": "1"}):
            self.assertTrue(triage.triage_buttons_enabled())


class TriageFollowupTests(_Base):
    """분류 후 쓰레드 멘션 — FAILURE=담당자 / SKIP=김태식·김도연 고정. Slack mock."""

    def setUp(self):
        super().setUp()
        self.run.slack_message_ts = "111.22"
        self.run.slack_channel_id = "C9"
        self.run.save(update_fields=["slack_message_ts", "slack_channel_id"])

    def test_failure_followup_mentions_owners(self):
        with mock.patch.object(triage, "_owner_mentions_for_job_run", return_value="<@U1> <@U2>"), \
             mock.patch.object(triage, "_slack_web") as web:
            ok = triage.post_triage_followup(self.run, self.jr, "failure")
        self.assertTrue(ok)
        _, kw = web.return_value.chat_postMessage.call_args
        self.assertEqual(kw["thread_ts"], "111.22")
        self.assertEqual(kw["channel"], "C9")
        self.assertIn("장애 F/U 진행이 필요합니다", kw["text"])
        self.assertIn("<@U1>", kw["text"])

    def test_skip_followup_fixed_mentions(self):
        with mock.patch.object(triage, "_fixed_skip_mentions", return_value="<@UTS> <@UDY>"), \
             mock.patch.object(triage, "_slack_web") as web:
            triage.post_triage_followup(self.run, self.jr, "skip")
        _, kw = web.return_value.chat_postMessage.call_args
        self.assertIn("자동화 테스트케이스 점검 요망", kw["text"])
        self.assertIn("자동화 판정 오류 의심 및 자동화 코드 이슈 추정", kw["text"])
        self.assertIn("<@UTS>", kw["text"])

    def test_no_followup_without_ts(self):
        self.run.slack_message_ts = ""
        self.run.save(update_fields=["slack_message_ts"])
        with mock.patch.object(triage, "_slack_web") as web:
            self.assertFalse(triage.post_triage_followup(self.run, self.jr, "skip"))
        web.return_value.chat_postMessage.assert_not_called()

    def test_apply_triage_posts_followup_on_change(self):
        with mock.patch.object(triage, "update_suite_fail_message"), \
             mock.patch.object(triage, "post_triage_followup") as fu:
            triage.apply_triage_action(triage.ACTION_SKIP, str(self.jr.pk), "U_OPS")
        fu.assert_called_once()

    def test_apply_triage_no_followup_when_unchanged(self):
        self.jr.verdict = "skip"
        self.jr.save(update_fields=["verdict"])
        with mock.patch.object(triage, "update_suite_fail_message"), \
             mock.patch.object(triage, "post_triage_followup") as fu:
            triage.apply_triage_action(triage.ACTION_SKIP, str(self.jr.pk), "U_OPS")
        fu.assert_not_called()


class OwnerFuScheduledTests(_Base):
    """스케줄 run(triggered_by 없음) 알림은 담당자 F/U(멘션·배정) 제외, URL·발췌는 유지."""

    def setUp(self):
        super().setUp()
        self.jr.console_output = (
            "@@@@@@@@@@\n  ERROR_URL: https://x.example.com/pay\n"
            "  Method: POST\n  Status: 500\n@@@@@@@@@@")
        self.jr.save(update_fields=["console_output"])

    def test_owner_fu_false_removes_mentions_keeps_url_excerpt(self):
        from apps.notifications.services import _build_followup_for_job_run
        fu = _build_followup_for_job_run(
            str(self.jr.pk), context_label_extra="Suite: x / Job: y", owner_fu=False)
        self.assertIn("x.example.com/pay", fu)  # 실패 URL 유지
        self.assertIn("에러 발췌", fu)            # 발췌 유지
        self.assertNotIn("<@", fu)              # 담당자 멘션 없음
        self.assertNotIn("→", fu.split("에러 발췌")[0])  # 실패호출에 담당자 배정(→) 없음


class ScheduleButtonTests(_Base):
    """스케줄 실패 알림의 '스케줄 중단/재활성화' 버튼. 스케줄 서비스는 mock."""

    def setUp(self):
        super().setUp()
        # 스케줄 버튼 권한자(김태식·김도연) — 이름 기준 화이트리스트.
        self.ctrl = User.objects.create_user(
            username="taeshkim", password="x", role="qpe_admin",
            first_name="김태식", slack_user_id="U_TS")

    def _schedule(self, running=True):
        from apps.robustness.models import RobustnessTest, RobustnessSchedule, RobustnessRun
        rt = RobustnessTest.objects.create(name="rt")
        sched = RobustnessSchedule.objects.create(
            robustness_test=rt, test_suite=self.suite, interval_minutes=10, is_running=running)
        RobustnessRun.objects.create(schedule=sched, suite_run=self.run)
        return sched

    def test_no_blocks_for_manual_run(self):
        self.assertEqual(triage.build_schedule_action_blocks(self.run), [])

    def test_stop_button_when_running(self):
        self._schedule(running=True)
        d = json.dumps(triage.build_schedule_action_blocks(self.run), ensure_ascii=False)
        self.assertIn("schedule_stop", d)
        self.assertIn(str(self.run.pk), d)

    def test_reactivate_button_when_stopped(self):
        self._schedule(running=False)
        self.run.slack_schedule_action = "stopped"
        d = json.dumps(triage.build_schedule_action_blocks(self.run), ensure_ascii=False)
        self.assertIn("schedule_reactivate", d)
        self.assertIn("중단됨", d)

    def test_reactivated_context_no_button(self):
        self._schedule(running=True)
        self.run.slack_schedule_action = "reactivated"
        blocks = triage.build_schedule_action_blocks(self.run)
        self.assertFalse(any(b.get("type") == "actions" for b in blocks))
        self.assertIn("재활성화됨", json.dumps(blocks, ensure_ascii=False))

    def test_apply_stop_sets_state(self):
        self._schedule(running=True)
        with mock.patch("apps.robustness.services.stop_schedule") as stop, \
             mock.patch.object(triage, "update_suite_fail_message"):
            res = triage.apply_schedule_action(triage.SCHEDULE_STOP, str(self.run.pk), "U_TS")
        self.assertTrue(res["ok"])
        stop.assert_called_once()
        self.run.refresh_from_db()
        self.assertEqual(self.run.slack_schedule_action, "stopped")

    def test_apply_reactivate_sets_state(self):
        self._schedule(running=False)
        with mock.patch("apps.robustness.services.start_schedule") as start, \
             mock.patch("apps.robustness.services.sweep_schedule"), \
             mock.patch.object(triage, "update_suite_fail_message"):
            res = triage.apply_schedule_action(triage.SCHEDULE_REACTIVATE, str(self.run.pk), "U_TS")
        self.assertTrue(res["ok"])
        start.assert_called_once()
        self.run.refresh_from_db()
        self.assertEqual(self.run.slack_schedule_action, "reactivated")

    def test_apply_viewer_forbidden(self):
        self._schedule()
        res = triage.apply_schedule_action(triage.SCHEDULE_STOP, str(self.run.pk), "U_VIEW")
        self.assertEqual(res["reason"], "schedule_forbidden")

    def test_apply_ops_forbidden(self):
        # 쓰기 권한이 있어도 김태식·김도연 이 아니면 스케줄 버튼은 거부.
        self._schedule()
        res = triage.apply_schedule_action(triage.SCHEDULE_STOP, str(self.run.pk), "U_OPS")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "schedule_forbidden")

    def test_apply_controller_by_name_allowed(self):
        # 김도연(다른 이름)도 이름 화이트리스트로 허용.
        User.objects.create_user(
            username="kdy7854", password="x", role="qpe_admin",
            first_name="김도연", slack_user_id="U_DY")
        self._schedule(running=True)
        with mock.patch("apps.robustness.services.stop_schedule"), \
             mock.patch.object(triage, "update_suite_fail_message"):
            res = triage.apply_schedule_action(triage.SCHEDULE_STOP, str(self.run.pk), "U_DY")
        self.assertTrue(res["ok"])

    def test_apply_no_schedule(self):
        res = triage.apply_schedule_action(triage.SCHEDULE_STOP, str(self.run.pk), "U_TS")
        self.assertEqual(res["reason"], "no_schedule")


class _SyncThread:
    """threading.Thread stand-in — target 을 즉시 동기 실행(백그라운드 스레드 flakiness 방지)."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class ApplyVerdictsBulkTests(TestCase):
    """apply_verdicts_bulk — 포털 미판정 큐 일괄 판정 코어. suite_run 별 chat.update
    dedupe + verdict 가 실제로 바뀐 job 만 쓰레드 followup 발송을 검증한다.
    """

    def setUp(self):
        self.ops = User.objects.create_user(username="bulk_ops", password="x", role="qpe_ops")
        self.suite = TestSuite.objects.create(name="bulk-x", jenkins_folder_path="b/f")
        self.run1 = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=2, failed_jobs=2,
            slack_message_ts="1.1", slack_channel_id="C1")
        self.run2 = TestSuiteRun.objects.create(
            suite=self.suite, status=TestSuiteRun.Status.COMPLETED,
            total_jobs=1, failed_jobs=1,
            slack_message_ts="2.2", slack_channel_id="C2")
        job1 = TestSuiteJob.objects.create(suite=self.suite, job_name="b1", job_full_path="b/1")
        job2 = TestSuiteJob.objects.create(suite=self.suite, job_name="b2", job_full_path="b/2")
        job3 = TestSuiteJob.objects.create(suite=self.suite, job_name="b3", job_full_path="b/3")
        # run1 의 잡 2개(미분류 → 새 verdict), run2 의 잡 1개(이미 같은 verdict → 재확정만)
        self.jr1 = TestSuiteJobRun.objects.create(
            suite_run=self.run1, job=job1, status=TestSuiteJobRun.Status.FAILURE, verdict="")
        self.jr2 = TestSuiteJobRun.objects.create(
            suite_run=self.run1, job=job2, status=TestSuiteJobRun.Status.FAILURE, verdict="")
        self.jr3 = TestSuiteJobRun.objects.create(
            suite_run=self.run2, job=job3, status=TestSuiteJobRun.Status.FAILURE, verdict="skip")

    def test_dedupes_chat_update_per_run_and_followup_only_changed(self):
        with mock.patch("threading.Thread", _SyncThread), \
             mock.patch.object(triage, "update_suite_fail_message") as upd, \
             mock.patch.object(triage, "post_triage_followup") as fu:
            result = triage.apply_verdicts_bulk([self.jr1, self.jr2, self.jr3], "skip", self.ops)

        self.assertEqual(result, {"count": 3, "changed": 2})

        # run1 의 잡 2개 + run2 의 잡 1개를 섞어 선택해도 run 별로는 2회만(dedupe).
        self.assertEqual(upd.call_count, 2)
        updated_run_ids = {call.args[0].pk for call in upd.call_args_list}
        self.assertEqual(updated_run_ids, {self.run1.pk, self.run2.pk})

        # followup 은 verdict 가 실제로 바뀐 jr1·jr2 만(jr3 는 이미 skip → 재확정 제외).
        self.assertEqual(fu.call_count, 2)
        followed_up_ids = {call.args[1].pk for call in fu.call_args_list}
        self.assertEqual(followed_up_ids, {self.jr1.pk, self.jr2.pk})
        for call in fu.call_args_list:
            self.assertEqual(call.args[2], "skip")

        for jr in (self.jr1, self.jr2, self.jr3):
            jr.refresh_from_db()
            self.assertEqual(jr.verdict, "skip")
            self.assertEqual(jr.triaged_by, self.ops)
            self.assertIsNotNone(jr.triaged_at)

    def test_no_followup_when_all_unchanged(self):
        # jr1·jr2 를 미리 같은 verdict(failure)로 만들어두고 재확정 → followup 없음.
        self.jr1.verdict = "failure"
        self.jr1.save(update_fields=["verdict"])
        self.jr2.verdict = "failure"
        self.jr2.save(update_fields=["verdict"])
        with mock.patch("threading.Thread", _SyncThread), \
             mock.patch.object(triage, "update_suite_fail_message") as upd, \
             mock.patch.object(triage, "post_triage_followup") as fu:
            result = triage.apply_verdicts_bulk([self.jr1, self.jr2], "failure", self.ops)
        self.assertEqual(result, {"count": 2, "changed": 0})
        self.assertEqual(upd.call_count, 1)  # 둘 다 run1 → 1회
        fu.assert_not_called()

    def test_thread_started_as_daemon_without_blocking_caller(self):
        # 웹 요청 스레드를 막지 않도록 daemon=True 로 기동하는지 확인(스레드 자체는 목).
        with mock.patch("threading.Thread") as thread_cls, \
             mock.patch.object(triage, "update_suite_fail_message"), \
             mock.patch.object(triage, "post_triage_followup"):
            triage.apply_verdicts_bulk([self.jr1], "skip", self.ops)
        _, kwargs = thread_cls.call_args
        self.assertTrue(kwargs.get("daemon"))
        thread_cls.return_value.start.assert_called_once()
