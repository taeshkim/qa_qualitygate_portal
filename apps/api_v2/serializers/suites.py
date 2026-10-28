"""apps/api_v2/serializers/suites.py — TestSuiteRun / TestSuiteJobRun 직렬화.

PRD: prd_api.md §5, §6.4. Phase F-2..3.

* `TestSuiteRunSummarySerializer` — 목록 응답용 (job_runs 포함하지 않음).
* `TestSuiteRunDetailSerializer` — 단건 응답용 (job_runs 배열 + 콘솔 URL).
* `TestSuiteJobRunSerializer` — Suite Job Run 1건.
"""

from __future__ import annotations

from django.urls import reverse
from rest_framework import serializers

from apps.suites.models import TestSuiteJobRun, TestSuiteRun

from .. import phase as _phase


# ----------------------------------------------------------------------------
# 내부 helper — 시리얼라이저에서 반복되는 nested dict 구성 로직을 한 곳에 모음.
# ----------------------------------------------------------------------------

def _suite_brief(suite):
    if suite is None:
        return None
    return {"id": str(suite.pk), "name": suite.name}


def _user_brief(user):
    if user is None:
        return None
    return {
        "id": str(user.pk),
        "username": getattr(user, "username", ""),
    }


def _job_brief(job):
    if job is None:
        return None
    return {"id": str(job.pk), "name": job.job_name}


def _job_run_console_url(job_run_id) -> str:
    return reverse("api_v2:api_v2_suites:suite_job_run_console", kwargs={"pk": job_run_id})


def _suite_run_self_url(suite_run_id) -> str:
    return reverse("api_v2:api_v2_suites:suite_run_detail", kwargs={"pk": suite_run_id})


def _suite_run_detail_url(suite_run_id) -> str:
    return reverse("suites:run_detail", kwargs={"pk": suite_run_id})


# ----------------------------------------------------------------------------
# Serializers
# ----------------------------------------------------------------------------

class TestSuiteJobRunSerializer(serializers.Serializer):
    """Suite Job Run 1건 — 단건/콘솔 응답에서 공유."""

    id = serializers.SerializerMethodField()
    suite_run_id = serializers.SerializerMethodField()
    job = serializers.SerializerMethodField()
    status = serializers.CharField()
    result = serializers.SerializerMethodField()
    build_number = serializers.SerializerMethodField()
    build_url = serializers.SerializerMethodField()
    started_at = serializers.DateTimeField()
    completed_at = serializers.DateTimeField()
    console_url = serializers.SerializerMethodField()

    def get_id(self, obj: TestSuiteJobRun) -> str:
        return str(obj.pk)

    def get_suite_run_id(self, obj: TestSuiteJobRun) -> str:
        return str(obj.suite_run_id)

    def get_job(self, obj: TestSuiteJobRun):
        return _job_brief(obj.job)

    def get_result(self, obj: TestSuiteJobRun):
        return _phase.result_for_job_run(obj.status)

    def get_build_number(self, obj: TestSuiteJobRun):
        return obj.jenkins_build_number

    def get_build_url(self, obj: TestSuiteJobRun) -> str:
        return obj.jenkins_build_url or ""

    def get_console_url(self, obj: TestSuiteJobRun) -> str:
        return _job_run_console_url(obj.pk)


class TestSuiteRunSummarySerializer(serializers.Serializer):
    """Suite Run 목록 응답용 (job_runs 미포함)."""

    id = serializers.SerializerMethodField()
    suite = serializers.SerializerMethodField()
    seq_no = serializers.IntegerField()
    status = serializers.CharField()
    phase = serializers.SerializerMethodField()
    result = serializers.SerializerMethodField()
    is_terminal = serializers.SerializerMethodField()
    total_jobs = serializers.IntegerField()
    passed_jobs = serializers.IntegerField()
    failed_jobs = serializers.IntegerField()
    started_at = serializers.DateTimeField()
    completed_at = serializers.DateTimeField()
    triggered_by = serializers.SerializerMethodField()
    self_url = serializers.SerializerMethodField()
    detail_url = serializers.SerializerMethodField()

    def get_id(self, obj: TestSuiteRun) -> str:
        return str(obj.pk)

    def get_suite(self, obj: TestSuiteRun):
        return _suite_brief(obj.suite)

    def get_phase(self, obj: TestSuiteRun) -> str:
        return _phase.phase_for_suite_run(
            obj.status,
            passed_jobs=obj.passed_jobs or 0,
            failed_jobs=obj.failed_jobs or 0,
        )

    def get_result(self, obj: TestSuiteRun):
        return _phase.result_for_suite_run(
            obj.status,
            passed_jobs=obj.passed_jobs or 0,
            failed_jobs=obj.failed_jobs or 0,
        )

    def get_is_terminal(self, obj: TestSuiteRun) -> bool:
        return obj.status == TestSuiteRun.Status.COMPLETED

    def get_triggered_by(self, obj: TestSuiteRun):
        return _user_brief(obj.triggered_by)

    def get_self_url(self, obj: TestSuiteRun) -> str:
        return _suite_run_self_url(obj.pk)

    def get_detail_url(self, obj: TestSuiteRun) -> str:
        return _suite_run_detail_url(obj.pk)


class TestSuiteRunDetailSerializer(TestSuiteRunSummarySerializer):
    """Suite Run 단건 응답용 — `job_runs` 배열 추가."""

    job_runs = serializers.SerializerMethodField()

    def get_job_runs(self, obj: TestSuiteRun):
        # prefetched 가 있으면 그대로 사용, 없으면 fetch.
        job_runs = list(obj.job_runs.all())
        return TestSuiteJobRunSerializer(job_runs, many=True).data
