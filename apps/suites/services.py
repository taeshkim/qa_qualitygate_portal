"""apps/suites/services.py — Suite Run 트리거/취소 서비스.

기존 ``apps/suites/views.py`` 의 ``suite_run`` 뷰 핵심 로직을 외부 API (Phase F-2)
에서도 호출할 수 있도록 추출한 service 레이어. 동작 변경 없음.

PRD: prd_api.md §6.4, §12 Phase F-1.
"""

from __future__ import annotations

from typing import Optional

from django.db import transaction
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


def trigger_suite_run(
    *, suite: TestSuite, user, job_id: str = "", run_all: bool = False,
) -> TestSuiteRun:
    """Suite Run 1건을 트리거한다.

    - ``job_id`` 지정 → 해당 Job **1개만** 실행 (하위호환, 기존 동작 그대로).
    - ``job_id`` 생략 + ``run_all=False`` (**기본**) → suite 의 활성 대상 Job
      (``is_active=True, is_target=True``) 중 **'실행 중이지 않은(idle)' worker
      1개만** 골라 실행한다 (worker 풀 모델). 앞 순서(order)의 idle worker 가
      우선 선택되고, 모든 worker 가 busy 면 ``all_workers_busy`` (409).
    - ``job_id`` 생략 + ``run_all=True`` → 활성 대상 Job **전체**를 ``job__order``
      순으로 묶은 **순차 체인 Run** 1건 생성 (이전 기본동작 — 명시 요청 시에만).

    worker 1개 실행 / 체인 실행 모두 동시에 여러 Jenkins 빌드를 돌리지 않으므로,
    호출자는 1건의 ``suite_run_id`` 만 폴링하면 된다.
    """
    if isinstance(job_id, str):
        job_id = job_id.strip()
    elif job_id is not None:
        job_id = str(job_id)
    else:
        job_id = ""

    if job_id:
        return _trigger_single_job_run(suite=suite, user=user, job_id=job_id)
    if run_all:
        return _trigger_chain_run(suite=suite, user=user)
    return _trigger_single_idle_run(suite=suite, user=user)


def _trigger_single_idle_run(*, suite: TestSuite, user) -> TestSuiteRun:
    """활성 대상 Job 중 '실행 중이지 않은(idle)' worker **1개만** 골라 실행한다.

    worker 풀 모델 — 1 요청 = worker 1개. 클라우드인프라가 동일 suite 를 여러 번
    호출해도 idle worker 가 분산 선택되어 5개가 한꺼번에 돌지 않는다.

    - 활성 대상 Job 이 하나도 없으면 ``SuiteRunError('no_active_jobs', 409)``.
    - **검증된 idle worker 가 없으면**(전부 busy 거나 Jenkins 상태 확인 불가)
      ``SuiteRunError('all_workers_busy', 409)`` — Run 을 만들지 않는다. 잠시 후
      재시도하거나 ``job_id`` 를 지정해 특정 worker 를 강제 실행한다.
    - 전체 worker 를 한 번에 돌리려면 ``run_all=True`` (→ ``_trigger_chain_run``).
    """
    jobs = list(
        suite.jobs.filter(is_active=True, is_target=True).order_by("order", "job_name")
    )
    if not jobs:
        raise SuiteRunError(
            "no_active_jobs",
            "Suite has no active target jobs to run.",
            http_status=409,
        )

    # 포털이 이미 트리거해 진행 중인 worker(job) 는 후보에서 제외 — 같은 worker 가
    # 동시에 두 번 트리거되는 것을 막는다. Jenkins 가 아직 building=True 를 반영하기
    # 전(방금 트리거됨)의 TOCTOU 도 DB 상태로 한 번 더 차단(best-effort).
    busy_job_ids = set(
        TestSuiteJobRun.objects.filter(
            suite_run__suite=suite,
            suite_run__status=TestSuiteRun.Status.RUNNING,
            status__in=[
                TestSuiteJobRun.Status.PENDING,
                TestSuiteJobRun.Status.WAITING,
                TestSuiteJobRun.Status.QUEUED,
                TestSuiteJobRun.Status.RUNNING,
            ],
        ).values_list("job_id", flat=True)
    )
    candidates = [j for j in jobs if j.id not in busy_job_ids]

    # 순환 import 회피: views 의 idle 선택 헬퍼를 lazy import.
    from .views import pick_idle_job

    job = pick_idle_job(suite, candidates) if candidates else None
    if job is None:
        raise SuiteRunError(
            "all_workers_busy",
            "No idle worker available (all busy or Jenkins state unverifiable). "
            "Retry later or pass job_id.",
            http_status=409,
            details={"candidate_jobs": [str(j.pk) for j in jobs]},
        )

    # suite_run 과 job_run 생성을 한 트랜잭션으로 묶는다 — job_run 생성이 실패하면
    # RUNNING 상태로 영원히 남는 고아 suite_run 이 생기는 것을 방지 (seq_no 집계 포함).
    with transaction.atomic():
        suite_run = TestSuiteRun.objects.create(
            suite=suite,
            status=TestSuiteRun.Status.RUNNING,
            total_jobs=1,
            retry_limit=max(1, suite.retry_count),
            started_at=timezone.now(),
            triggered_by=user,
            seq_no=TestSuiteRun.next_seq_no(suite),
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


def _trigger_single_job_run(*, suite: TestSuite, user, job_id: str) -> TestSuiteRun:
    """``job_id`` 로 지정된 Job 1개만 실행하는 Run 을 생성한다 (하위호환 경로)."""
    job = get_object_or_404(TestSuiteJob, pk=job_id, suite=suite)

    # 고아 suite_run 방지 — job_run 까지 한 트랜잭션으로 묶는다.
    with transaction.atomic():
        suite_run = TestSuiteRun.objects.create(
            suite=suite,
            status=TestSuiteRun.Status.RUNNING,
            total_jobs=1,
            retry_limit=max(1, suite.retry_count),
            started_at=timezone.now(),
            triggered_by=user,
            seq_no=TestSuiteRun.next_seq_no(suite),
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


def _trigger_chain_run(*, suite: TestSuite, user) -> TestSuiteRun:
    """suite 의 활성 대상 Job 전체를 ``job__order`` 순차 체인으로 실행한다.

    - 실행할 활성 대상 Job 이 하나도 없으면 ``SuiteRunError('no_active_jobs', 409)``.
    - 진행 중(RUNNING) Run 이 이미 있으면 ``SuiteRunError('run_already_active', 409)``
      로 차단해 동시 체인 실행에 따른 Jenkins 빌드 충돌을 막는다.
    """
    jobs = list(
        suite.jobs.filter(is_active=True, is_target=True).order_by("order", "job_name")
    )
    if not jobs:
        raise SuiteRunError(
            "no_active_jobs",
            "Suite has no active target jobs to run.",
            http_status=409,
        )

    # best-effort 가드: 체크와 create 사이에 원자성은 없다(TOCTOU). runserver/SQLite
    # 환경에선 동시 체인 트리거 확률이 낮아 실용상 충분하며, 설령 둘이 통과해도
    # _try_trigger_next 가 "이미 building 이면 추적만" 하므로 같은 Job 을 중복
    # 트리거하지는 않는다. 단독(job_id 지정) 경로는 하위호환 위해 가드하지 않는다.
    active = suite.runs.filter(status=TestSuiteRun.Status.RUNNING).first()
    if active is not None:
        raise SuiteRunError(
            "run_already_active",
            "A run is already active for this suite.",
            http_status=409,
            details={"suite_run_id": str(active.pk)},
        )

    # 고아 suite_run 방지 — job_run 들까지 한 트랜잭션으로 묶는다.
    with transaction.atomic():
        suite_run = TestSuiteRun.objects.create(
            suite=suite,
            status=TestSuiteRun.Status.RUNNING,
            total_jobs=len(jobs),
            retry_limit=max(1, suite.retry_count),
            started_at=timezone.now(),
            triggered_by=user,
            seq_no=TestSuiteRun.next_seq_no(suite),
        )
        TestSuiteJobRun.objects.bulk_create([
            TestSuiteJobRun(
                suite_run=suite_run,
                job=job,
                status=TestSuiteJobRun.Status.PENDING,
            )
            for job in jobs
        ])

    # 순환 import 회피: views 모듈의 내부 헬퍼를 lazy import.
    from .views import _try_trigger_next

    _try_trigger_next(suite_run)

    ActivityLog.log(
        user=user,
        action=ActivityLog.Action.RUN,
        target_type=ActivityLog.TargetType.SUITE_RUN,
        target=suite_run,
        target_name=f"{suite.name} / 전체 {len(jobs)}개 Job",
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
