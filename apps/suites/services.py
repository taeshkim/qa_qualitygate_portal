"""apps/suites/services.py — Suite Run 트리거/취소 서비스.

기존 ``apps/suites/views.py`` 의 ``suite_run`` 뷰 핵심 로직을 외부 API (Phase F-2)
에서도 호출할 수 있도록 추출한 service 레이어. 동작 변경 없음.

PRD: prd_api.md §6.4, §12 Phase F-1.
"""

from __future__ import annotations

from typing import Optional

from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.activitylog.models import ActivityLog
from apps.notifications import events as nev
from apps.notifications.services import send_suite_run_event_async

from .models import TestSuite, TestSuiteJob, TestSuiteJobRun, TestSuiteRun


class SuiteRunError(Exception):
    """Service 레벨 사용자 에러. View / API 어느 쪽에서도 잡아 사용."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        details: Optional[dict] = None,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.details = details or {}


def trigger_suite_run(*, suite: TestSuite, user, job_id: str) -> TestSuiteRun:
    """단독 Suite Run 1개를 트리거한다.

    PRD §6.4 — ``job_id`` 가 비어있거나 None 이면 ``SuiteRunError('job_required')``
    를 raise. 그 외 흐름은 기존 ``suite_run`` 뷰와 동일하다.
    """
    if not job_id:
        raise SuiteRunError(
            "job_required",
            "job_id is required.",
            http_status=400,
        )

    job = get_object_or_404(TestSuiteJob, pk=job_id, suite=suite)

    suite_run = TestSuiteRun.objects.create(
        suite=suite,
        status=TestSuiteRun.Status.RUNNING,
        total_jobs=1,
        started_at=timezone.now(),
        triggered_by=user,
    )

    TestSuiteJobRun.objects.create(
        suite_run=suite_run,
        job=job,
        status=TestSuiteJobRun.Status.PENDING,
    )

    # 순환 import 회피: views 모듈의 내부 헬퍼를 lazy import.
    from .views import _try_trigger_next

    _try_trigger_next(suite_run)

    ActivityLog.log(
        user=user,
        action=ActivityLog.Action.RUN,
        target_type=ActivityLog.TargetType.SUITE_RUN,
        target=suite_run,
        target_name=f"{suite.name} / {job.job_name}",
    )

    send_suite_run_event_async(nev.SUITE_RUN_STARTED, suite_run)

    return suite_run


def cancel_suite_run(*, suite_run: TestSuiteRun, user) -> TestSuiteRun:
    """진행 중인 Suite Run 의 RUNNING/QUEUED/PENDING Job 을 모두 ABORTED 로 마킹하고
    Suite Run 자체도 COMPLETED 로 마무리한다.

    이미 종료된 Run 이면 ``SuiteRunError('not_actionable')`` raise.

    내부 구현은 기존 ``_abort_suite_run`` 헬퍼를 재사용한다.
    """
    if suite_run.status == TestSuiteRun.Status.COMPLETED:
        raise SuiteRunError(
            "not_actionable",
            "Suite Run is already completed.",
            http_status=409,
            details={"status": suite_run.status},
        )

    # 순환 import 회피: views 의 abort 헬퍼를 lazy import.
    from .views import _abort_suite_run

    _abort_suite_run(suite_run)

    ActivityLog.log(
        user=user,
        action=ActivityLog.Action.CANCEL,
        target_type=ActivityLog.TargetType.SUITE_RUN,
        target=suite_run,
        target_name=f"{suite_run.suite.name}",
    )

    suite_run.refresh_from_db()
    return suite_run
