import logging
import os
from celery import shared_task
from django.utils import timezone
from apps.gates.models import GateRun
from apps.integrations.slack import send_slack_message, build_gate_result_blocks

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_gate_result_notification(self, gate_run_id: str):
    try:
        gate_run = GateRun.objects.select_related(
            "service", "environment"
        ).prefetch_related("failures").get(pk=gate_run_id)
    except GateRun.DoesNotExist:
        logger.error(f"GateRun {gate_run_id} not found")
        return

    blocks = build_gate_result_blocks(gate_run)
    text = f"QG {gate_run.result}: {gate_run.service.name} / {gate_run.environment.name}"

    # Send to service's Slack channel
    service_channel = gate_run.service.slack_channel_id
    qg_alerts_channel = os.getenv("SLACK_QG_ALERTS_CHANNEL", "")

    channels = set()
    if service_channel:
        channels.add(service_channel)
    if qg_alerts_channel:
        channels.add(qg_alerts_channel)

    if not channels:
        logger.info(f"No Slack channels configured for {gate_run.service.code}, skipping")
        return

    for channel in channels:
        success = send_slack_message(channel, blocks, text)
        if not success:
            logger.warning(f"Failed to send to channel {channel}")


@shared_task(bind=True, max_retries=60)
def poll_suite_run_status(self, suite_run_id: str):
    """Jenkins 빌드 상태를 폴링하여 TestSuiteJobRun 업데이트."""
    from apps.suites.models import TestSuiteRun, TestSuiteJobRun
    from apps.integrations.jenkins import JenkinsClient, JenkinsError

    try:
        suite_run = TestSuiteRun.objects.get(pk=suite_run_id)
    except TestSuiteRun.DoesNotExist:
        return

    if suite_run.status == TestSuiteRun.Status.COMPLETED:
        return

    client = JenkinsClient()
    pending_runs = suite_run.job_runs.filter(
        status__in=[
            TestSuiteJobRun.Status.PENDING,
            TestSuiteJobRun.Status.QUEUED,
            TestSuiteJobRun.Status.RUNNING,
        ]
    ).select_related("job")

    for job_run in pending_runs:
        try:
            build_info = client.get_last_build_info(job_run.job.job_full_path)
            build_number = build_info.get("number")
            building = build_info.get("building", False)
            result = build_info.get("result")

            if build_number and (
                job_run.jenkins_build_number is None
                or build_number >= job_run.jenkins_build_number
            ):
                job_run.jenkins_build_number = build_number
                job_run.jenkins_build_url = build_info.get("url", "")

                if building:
                    job_run.status = TestSuiteJobRun.Status.RUNNING
                elif result == "SUCCESS":
                    job_run.status = TestSuiteJobRun.Status.SUCCESS
                    job_run.duration_ms = build_info.get("duration")
                    job_run.completed_at = timezone.now()
                elif result == "FAILURE":
                    job_run.status = TestSuiteJobRun.Status.FAILURE
                    job_run.duration_ms = build_info.get("duration")
                    job_run.completed_at = timezone.now()
                elif result == "ABORTED":
                    job_run.status = TestSuiteJobRun.Status.ABORTED
                    job_run.completed_at = timezone.now()

                job_run.save()
        except JenkinsError as e:
            logger.warning(f"Failed to poll {job_run.job.job_name}: {e}")

    # 집계 업데이트
    all_job_runs = suite_run.job_runs.all()
    passed = all_job_runs.filter(status=TestSuiteJobRun.Status.SUCCESS).count()
    failed = all_job_runs.filter(
        status__in=[TestSuiteJobRun.Status.FAILURE, TestSuiteJobRun.Status.ABORTED]
    ).count()

    suite_run.passed_jobs = passed
    suite_run.failed_jobs = failed

    if passed + failed >= suite_run.total_jobs:
        suite_run.status = TestSuiteRun.Status.COMPLETED
        suite_run.completed_at = timezone.now()
        suite_run.save()
        logger.info(f"Suite run {suite_run_id} completed: {passed}/{suite_run.total_jobs} passed")
    else:
        suite_run.save(update_fields=["passed_jobs", "failed_jobs"])
        # 아직 진행 중 → 15초 후 재폴링
        self.retry(countdown=15)
