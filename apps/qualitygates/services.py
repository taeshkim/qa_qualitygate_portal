"""Quality Gate 트리거/취소/재실행 비즈니스 로직.

Phase D-1 리팩토링: `views.py` 의 action 함수들을 외부 API (예: api_v2) 에서도
호출할 수 있도록 service layer 로 분리.

원칙:
- view 는 request 파싱 + messages + redirect 만 담당. 핵심 로직은 이 모듈로.
- 비즈니스 룰 위반은 `GateRunError(code=..., http_status=...)` 로 raise.
- 모든 service 함수는 (영향받은) `QualityGateRun` 인스턴스를 반환.
- ActivityLog 기록, Slack 알림 (`send_event_async`) 호출, 폴링 (`_poll_gate_run`) 은
  service 안에서 수행.

폴링 함수 (`_poll_gate_run`, `_advance_test`) 는 `views.py` 에서 lazy import —
views.py 가 이 모듈을 import 하므로 순환을 피하기 위함.
"""

import copy
import hashlib
import logging
from typing import Optional

from django.core.cache import cache
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.activitylog.models import ActivityLog
from apps.integrations.jenkins import JenkinsClient, JenkinsError
from apps.notifications import events as nev
from apps.notifications.services import send_event_async
from apps.suites.models import TestSuiteRun, TestSuiteJobRun
from .models import QualityGate, QualityGateRun, QualityGateRunTest


logger = logging.getLogger(__name__)


class GateRunError(Exception):
    """Quality Gate Run 비즈니스 룰 위반.

    code: 짧은 식별자 (api_v2 에서 그대로 응답 body 에 사용).
    http_status: REST 응답 status code 힌트 (기본 409 Conflict).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        details: Optional[dict] = None,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.details = details or {}


def _gate_log_label(gate) -> str:
    """ActivityLog 의 target_name 으로 쓰는 라벨. views.py 의 동일 헬퍼와 동기화."""
    parts = []
    if gate.subfolder_id:
        parts.append(gate.subfolder.folder.name)
        parts.append(gate.subfolder.name)
    elif gate.folder_id:
        parts.append(gate.folder.name)
    parts.append(gate.name)
    return " / ".join(parts)


# =============================================
# trigger_gate_run
# =============================================
def trigger_gate_run(
    *,
    gate: QualityGate,
    user,
    parameters: dict,
    skip_image_build: bool = False,
    auto_mode_override: Optional[bool] = None,
) -> QualityGateRun:
    """새 Quality Gate Run 을 생성하고 첫 단계 (이미지 빌드 또는 검수대상 배포) 를 트리거.

    - 진행 중 (ACTIVE_STATUSES) Run 이 있으면 `GateRunError('active_run_exists')`.
    - 사용자 입력 대기 (WAITING_USER_STATUSES) Run 은 자동 CANCELLED 처리.
    - parameters 가 비어 있고 auto-advance 조건이면 Jenkins 의 default 파라미터 수집.
    - Jenkins 트리거 실패는 raise 하지 않고 Run 의 상태를 *_FAILED 로 저장한 뒤
      Run 인스턴스 반환 (기존 view 동작과 동일).
    """
    # 진행 중 Run 차단.
    if gate.runs.filter(status__in=QualityGateRun.ACTIVE_STATUSES).exists():
        raise GateRunError(
            "active_run_exists",
            "이미 진행 중인 Run 이 있습니다.",
            http_status=409,
        )

    # 사용자 입력 대기 Run 들을 CANCELLED 처리.
    gate.runs.filter(status__in=QualityGateRun.WAITING_USER_STATUSES).update(
        status=QualityGateRun.Status.CANCELLED,
    )

    gate_suites = list(gate.gate_suites.order_by("order"))

    jenkins_client = JenkinsClient(
        gate.deploy_jenkins_url, gate.deploy_jenkins_user, gate.deploy_jenkins_token,
    )

    with transaction.atomic():
        next_seq = (
            QualityGateRun.objects.filter(gate=gate)
            .aggregate(m=Max("seq_no"))["m"] or 0
        ) + 1
        run = QualityGateRun.objects.create(
            gate=gate,
            status=QualityGateRun.Status.PENDING,
            total_suites=len(gate_suites),
            triggered_by=user,
            seq_no=next_seq,
        )

    for i, gs in enumerate(gate_suites):
        QualityGateRunTest.objects.create(
            gate_run=run, gate_suite=gs, order=i,
            retry_limit=max(1, gs.test_suite.retry_count),
        )

    ActivityLog.log(
        user=user, action=ActivityLog.Action.RUN,
        target_type=ActivityLog.TargetType.GATE_RUN, target=run,
        target_name=_gate_log_label(gate),
    )

    send_event_async(nev.RUN_STARTED, run)

    params = dict(parameters or {})

    use_image_build = gate.has_image_build and not skip_image_build

    # auto_mode 시 빈 파라미터면 Jenkins default 수집 (release branch 우선).
    if not params and _should_auto_advance(gate, auto_mode_override):
        # views 의 헬퍼를 lazy import (circular import 회피).
        from .views import _collect_default_params
        target_job = gate.image_job_path if use_image_build else gate.deploy_job_path
        try:
            params = _collect_default_params(
                jenkins_client, target_job, prefer_release_branch=True,
            )
        except JenkinsError as e:
            logger.warning(f"Auto run default params fetch failed: {e}")

    if use_image_build:
        try:
            result = jenkins_client.trigger_build(gate.image_job_path, parameters=params or None)
            run.status = QualityGateRun.Status.BUILDING_IMAGE
            run.image_build_number = result.get("build_number")
            run.image_started_at = timezone.now()
            run.image_params_json = params or {}
            run.save(update_fields=[
                "status", "image_build_number", "image_started_at", "image_params_json",
            ])
            send_event_async(nev.IMAGE_STARTED, run)
        except JenkinsError as e:
            logger.error(f"Image build trigger failed: {e}")
            run.status = QualityGateRun.Status.IMAGE_BUILD_FAILED
            run.save(update_fields=["status"])
            send_event_async(nev.IMAGE_FAILED, run, error_message=str(e))
            send_event_async(nev.RUN_FAILED, run, error_message=str(e))
    else:
        try:
            result = jenkins_client.trigger_build(gate.deploy_job_path, parameters=params or None)
            run.status = QualityGateRun.Status.DEPLOYING
            run.deploy_build_number = result.get("build_number")
            run.deploy_started_at = timezone.now()
            run.save(update_fields=["status", "deploy_build_number", "deploy_started_at"])
            send_event_async(nev.DEPLOY_STARTED, run)
        except JenkinsError as e:
            logger.error(f"Deploy trigger failed: {e}")
            run.status = QualityGateRun.Status.DEPLOY_FAILED
            run.save(update_fields=["status"])
            send_event_async(nev.DEPLOY_FAILED, run, error_message=str(e))
            send_event_async(nev.RUN_FAILED, run, error_message=str(e))

    return run


def _should_auto_advance(gate, override: Optional[bool] = None) -> bool:
    """views.py 의 `_should_auto_advance` 와 동일 — Dev→Stg + auto_mode."""
    if override is not None:
        return bool(override)
    return gate.auto_mode and gate.gate_type == QualityGate.GateType.DEV_STG


# =============================================
# cancel_run
# =============================================
def cancel_run(*, run: QualityGateRun, user) -> QualityGateRun:
    """진행 중 Run 취소 + 현재 단계 Jenkins 빌드 abort 시도."""
    gate = run.gate

    if not run.can_cancel:
        raise GateRunError(
            "cannot_cancel",
            "현재 상태에서는 취소할 수 없습니다.",
            http_status=409,
            details={"status": run.status},
        )

    status = run.status

    if status == QualityGateRun.Status.BUILDING_IMAGE and run.image_build_number:
        try:
            JenkinsClient(
                gate.deploy_jenkins_url, gate.deploy_jenkins_user, gate.deploy_jenkins_token,
            ).stop_build(gate.image_job_path, run.image_build_number)
        except Exception as e:
            logger.warning(f"Image build abort failed for run {run.pk}: {e}")

    elif status == QualityGateRun.Status.DEPLOYING and run.deploy_build_number:
        try:
            JenkinsClient(
                gate.deploy_jenkins_url, gate.deploy_jenkins_user, gate.deploy_jenkins_token,
            ).stop_build(gate.deploy_job_path, run.deploy_build_number)
        except Exception as e:
            logger.warning(f"Deploy build abort failed for run {run.pk}: {e}")

    elif status == QualityGateRun.Status.TESTING:
        running_tests = run.test_runs.filter(
            status=QualityGateRunTest.Status.TESTING,
        ).select_related("job_run__job")
        for current_test in running_tests:
            if not (current_test.job_run and current_test.job_run.jenkins_build_number):
                continue
            job_run = current_test.job_run
            try:
                job_run.job.suite.get_jenkins_client().stop_build(
                    job_run.job.job_full_path, job_run.jenkins_build_number,
                )
            except Exception as e:
                logger.warning(f"Test build abort failed for run {run.pk}: {e}")
            job_run.status = TestSuiteJobRun.Status.ABORTED
            job_run.completed_at = timezone.now()
            job_run.save(update_fields=["status", "completed_at"])
            if job_run.suite_run:
                job_run.suite_run.status = TestSuiteRun.Status.COMPLETED
                job_run.suite_run.completed_at = timezone.now()
                job_run.suite_run.save(update_fields=["status", "completed_at"])
            current_test.completed_at = timezone.now()
            current_test.save(update_fields=["completed_at"])

    elif status == QualityGateRun.Status.PROD_DEPLOYING and run.prod_build_number:
        try:
            JenkinsClient(
                gate.prod_jenkins_url, gate.prod_jenkins_user, gate.prod_jenkins_token,
            ).stop_build(gate.prod_job_path, run.prod_build_number)
        except Exception as e:
            logger.warning(f"Prod build abort failed for run {run.pk}: {e}")

    run.status = QualityGateRun.Status.CANCELLED
    run.save(update_fields=["status"])

    ActivityLog.log(
        user=user, action=ActivityLog.Action.CANCEL,
        target_type=ActivityLog.TargetType.GATE_RUN, target=run,
        target_name=_gate_log_label(gate),
        detail=f"cancelled at {status}",
    )
    send_event_async(nev.RUN_CANCELLED, run, extra={"cancelled_at": status})

    return run


# =============================================
# rerun_gate
# =============================================
def rerun_gate(*, run: QualityGateRun, user) -> QualityGateRun:
    """검수 재실행 — `can_rerun` 검사 후 새 Run 을 생성해 처음부터 다시.

    기존 view 는 단순히 `gate_run` redirect 만 했지만, service 레벨에서는 직접
    `trigger_gate_run` 을 호출해 새 Run 을 만들어 반환한다.
    """
    if not run.can_rerun:
        raise GateRunError(
            "cannot_rerun",
            "현재 상태에서는 재실행할 수 없습니다.",
            http_status=409,
            details={"status": run.status},
        )
    return trigger_gate_run(
        gate=run.gate,
        user=user,
        parameters={},
    )


# =============================================
# rerun_test
# =============================================
def rerun_test(*, run: QualityGateRun, user) -> QualityGateRun:
    """테스트만 재실행 — PASSED 가 아닌 test_run 을 PENDING 으로 리셋 후 _advance_test."""
    gate = run.gate

    if not run.can_rerun_test_only:
        raise GateRunError(
            "cannot_rerun_test",
            "현재 상태에서는 테스트 재실행이 불가합니다.",
            http_status=409,
            details={"status": run.status},
        )

    pending_tests = run.test_runs.exclude(
        status=QualityGateRunTest.Status.PASSED,
    ).order_by("order")
    reset_count = 0
    for tr in pending_tests:
        tr.status = QualityGateRunTest.Status.PENDING
        tr.job_run = None
        tr.started_at = None
        tr.completed_at = None
        tr.attempt = 1  # 수동 재실행은 새 사이클 — 자동 재시도 횟수를 초기화
        tr.save(update_fields=["status", "job_run", "started_at", "completed_at", "attempt"])
        reset_count += 1

    if gate.parallel_test:
        run.current_suite_index = run.test_runs.filter(
            status=QualityGateRunTest.Status.PASSED,
        ).count()
        run.save(update_fields=["current_suite_index"])

    run.status = QualityGateRun.Status.WAITING_TEST
    run.save(update_fields=["status"])

    ActivityLog.log(
        user=user, action=ActivityLog.Action.RETRY,
        target_type=ActivityLog.TargetType.GATE_RUN, target=run,
        target_name=_gate_log_label(gate),
        detail=f"rerun test only from suite #{run.current_suite_index + 1} ({reset_count} suites reset)",
    )

    from .views import _advance_test
    _advance_test(run, gate)

    return run


# =============================================
# trigger_deploy
# =============================================
def _validate_app_image_param(parameters: dict) -> None:
    """APP_IMAGE 파라미터가 전달됐을 때 빈값/깨진 태그를 차단한다.

    - APP_IMAGE 키 자체가 없으면 통과 (Jenkins 플러그인 default 사용 — d6652f1 의도).
    - 값이 비어있거나 공백만인 경우 → 거부.
    - 값이 콜론으로 끝나는 경우 (tag 누락 패턴, 예: "registry/img:") → 거부.
    """
    if not parameters:
        return
    if "APP_IMAGE" not in parameters:
        return
    value = parameters["APP_IMAGE"]
    stripped = (value or "").strip()
    if not stripped:
        raise GateRunError(
            "invalid_app_image",
            "APP_IMAGE 가 비어있거나 태그가 누락됐습니다. 모달에서 올바른 이미지를 선택하세요.",
            http_status=400,
        )
    if stripped.endswith(":"):
        raise GateRunError(
            "invalid_app_image",
            "APP_IMAGE 가 비어있거나 태그가 누락됐습니다. 모달에서 올바른 이미지를 선택하세요.",
            http_status=400,
        )


def trigger_deploy(*, run: QualityGateRun, user, parameters: dict) -> QualityGateRun:
    """검수대상 배포 트리거 (WAITING_DEPLOY 또는 DEPLOY_FAILED 재시도)."""
    gate = run.gate

    _validate_app_image_param(parameters)

    allowed = (QualityGateRun.Status.WAITING_DEPLOY, QualityGateRun.Status.DEPLOY_FAILED)
    if run.status not in allowed:
        raise GateRunError(
            "invalid_state",
            "현재 상태에서는 배포할 수 없습니다.",
            http_status=409,
            details={"status": run.status, "allowed": [s for s in allowed]},
        )

    is_retry = run.status == QualityGateRun.Status.DEPLOY_FAILED

    client = JenkinsClient(
        gate.deploy_jenkins_url, gate.deploy_jenkins_user, gate.deploy_jenkins_token,
    )

    params = dict(parameters or {})
    try:
        result = client.trigger_build(gate.deploy_job_path, parameters=params or None)
        run.status = QualityGateRun.Status.DEPLOYING
        run.deploy_build_number = result.get("build_number")
        run.deploy_started_at = timezone.now()
        send_event_async(nev.DEPLOY_STARTED, run)
        if is_retry:
            run.deploy_build_url = ""
            run.deploy_duration_ms = None
            run.deploy_stages_json = []
            run.deploy_current_stage = ""
            run.deploy_console_output = ""
            run.deploy_completed_at = None
            run.deploy_argocd_sync_status = ""
            run.deploy_argocd_health_status = ""
            run.deploy_argocd_synced_at = None
            run.deploy_argocd_checked_at = None
            run.deploy_argocd_sync_requested_at = None
            run.deploy_argocd_operation_phase = ""
            run.deploy_argocd_operation_message = ""
            run.deploy_argocd_sync_error = ""
            run.deploy_argocd_healthy_first_at = None
            run.save()
            ActivityLog.log(
                user=user, action=ActivityLog.Action.RETRY,
                target_type=ActivityLog.TargetType.GATE_RUN, target=run,
                target_name=_gate_log_label(gate),
                detail="rerun deploy only",
            )
        else:
            run.save(update_fields=["status", "deploy_build_number", "deploy_started_at"])
    except JenkinsError as e:
        logger.error(f"Deploy trigger failed: {e}")
        run.status = QualityGateRun.Status.DEPLOY_FAILED
        run.save(update_fields=["status"])
        send_event_async(nev.DEPLOY_FAILED, run, error_message=str(e))
        send_event_async(nev.RUN_FAILED, run, error_message=str(e))
        raise GateRunError(
            "jenkins_trigger_failed",
            f"검수대상 환경 배포 트리거에 실패했습니다: {e} "
            f"(Gate 의 '검수대상 환경 배포 Job URL' 이 실제 Jenkins Job 을 가리키는지 확인하세요.)",
            http_status=502,
            details={"error": str(e)},
        )

    return run


# =============================================
# trigger_prod
# =============================================
def trigger_prod(*, run: QualityGateRun, user, parameters: dict) -> QualityGateRun:
    """검수통과 배포 트리거 (QG_PASSED 또는 PROD_DEPLOY_FAILED 재시도)."""
    gate = run.gate

    _validate_app_image_param(parameters)

    allowed = (QualityGateRun.Status.QG_PASSED, QualityGateRun.Status.PROD_DEPLOY_FAILED)
    if run.status not in allowed or not gate.has_prod_deploy:
        raise GateRunError(
            "invalid_state",
            "현재 상태에서는 검수통과 배포를 트리거할 수 없습니다.",
            http_status=409,
            details={
                "status": run.status,
                "allowed": [s for s in allowed],
                "has_prod_deploy": gate.has_prod_deploy,
            },
        )

    is_retry = run.status == QualityGateRun.Status.PROD_DEPLOY_FAILED

    prod_client = JenkinsClient(
        gate.prod_jenkins_url, gate.prod_jenkins_user, gate.prod_jenkins_token,
    )

    params = dict(parameters or {})
    try:
        result = prod_client.trigger_build(gate.prod_job_path, parameters=params or None)
        run.status = QualityGateRun.Status.PROD_DEPLOYING
        run.prod_build_number = result.get("build_number")
        run.prod_started_at = timezone.now()
        send_event_async(nev.PROD_STARTED, run)
        if is_retry:
            run.prod_build_url = ""
            run.prod_duration_ms = None
            run.prod_stages_json = []
            run.prod_current_stage = ""
            run.prod_console_output = ""
            run.prod_completed_at = None
            run.prod_argocd_sync_status = ""
            run.prod_argocd_health_status = ""
            run.prod_argocd_synced_at = None
            run.prod_argocd_checked_at = None
            run.prod_argocd_sync_requested_at = None
            run.prod_argocd_operation_phase = ""
            run.prod_argocd_operation_message = ""
            run.prod_argocd_sync_error = ""
            run.prod_argocd_healthy_first_at = None
            run.save()
            ActivityLog.log(
                user=user, action=ActivityLog.Action.RETRY,
                target_type=ActivityLog.TargetType.GATE_RUN, target=run,
                target_name=_gate_log_label(gate),
                detail="rerun prod deploy only",
            )
        else:
            run.save(update_fields=["status", "prod_build_number", "prod_started_at"])
    except JenkinsError as e:
        logger.error(f"Prod deploy trigger failed: {e}")
        run.status = QualityGateRun.Status.PROD_DEPLOY_FAILED
        run.save(update_fields=["status"])
        send_event_async(nev.PROD_FAILED, run, error_message=str(e))
        send_event_async(nev.RUN_FAILED, run, error_message=str(e))
        raise GateRunError(
            "jenkins_trigger_failed",
            f"검수통과 배포 트리거에 실패했습니다: {e} "
            f"(Gate 의 '검수통과 환경 배포 Job URL' 이 실제 Jenkins Job 을 가리키는지 확인하세요.)",
            http_status=502,
            details={"error": str(e)},
        )

    return run


# =============================================
# argocd_retry
# =============================================
def argocd_retry(*, run: QualityGateRun, stage: str, user) -> QualityGateRun:
    """Argo CD sync 재시도 — stage 의 sync_requested_at 등을 리셋 후 폴링 한 번 수동 실행."""
    stage = (stage or "").lower()
    if stage not in ("deploy", "prod"):
        raise GateRunError(
            "invalid_stage",
            "stage 는 'deploy' 또는 'prod' 만 가능합니다.",
            http_status=400,
            details={"stage": stage},
        )

    setattr(run, f"{stage}_argocd_sync_requested_at", None)
    setattr(run, f"{stage}_argocd_sync_status", "")
    setattr(run, f"{stage}_argocd_health_status", "")
    setattr(run, f"{stage}_argocd_checked_at", None)
    setattr(run, f"{stage}_argocd_synced_at", None)
    setattr(run, f"{stage}_argocd_operation_phase", "")
    setattr(run, f"{stage}_argocd_operation_message", "")
    setattr(run, f"{stage}_argocd_sync_error", "")
    setattr(run, f"{stage}_argocd_healthy_first_at", None)

    if stage == "deploy" and run.status in (
        QualityGateRun.Status.WAITING_TEST, QualityGateRun.Status.TESTING,
        QualityGateRun.Status.FAILED, QualityGateRun.Status.QG_PASSED,
        QualityGateRun.Status.COMPLETED, QualityGateRun.Status.DEPLOY_FAILED,
    ):
        run.status = QualityGateRun.Status.DEPLOYING
    if stage == "prod" and run.status in (
        QualityGateRun.Status.COMPLETED, QualityGateRun.Status.PROD_DEPLOY_FAILED,
    ):
        run.status = QualityGateRun.Status.PROD_DEPLOYING

    run.save(update_fields=[
        f"{stage}_argocd_sync_requested_at",
        f"{stage}_argocd_sync_status",
        f"{stage}_argocd_health_status",
        f"{stage}_argocd_checked_at",
        f"{stage}_argocd_synced_at",
        f"{stage}_argocd_operation_phase",
        f"{stage}_argocd_operation_message",
        f"{stage}_argocd_sync_error",
        f"{stage}_argocd_healthy_first_at",
        "status",
    ])

    from .views import _poll_gate_run
    _poll_gate_run(run)

    return run


# =============================================
# Job 파라미터 스키마 조회 (image / deploy / prod 통합)
# =============================================

VALID_PARAM_STAGES = ("image", "deploy", "prod")


def get_job_param_schema(
    gate: QualityGate,
    stage: str,
    *,
    refresh: bool = False,
    image_build_number: Optional[int] = None,
    run=None,
):
    """게이트의 Jenkins Job 파라미터 스키마를 반환.

    - stage='image'  → image_job_path (없으면 GateRunError invalid_stage)
    - stage='deploy' → deploy_job_path
    - stage='prod'   → prod_job_path (없으면 빈 parameters)
    - 120초 캐시 (gate.pk + target_job 키), `refresh=True` 면 캐시 무시
    - run 이 주어지면 deploy/prod 단계에서 콘솔 추출 태그(resolve_and_apply_app_image)
      를 사용. run 이 None 이면 기존 prefix 방식(_apply_app_image_default) 유지.
    - api_v2 meta.py 는 run 없이 호출 → 기존 동작 완전 무파괴.

    반환: (parameters: list, cached: bool)
    Jenkins 통신 실패 → GateRunError(code='jenkins_error', http_status=502).
    """
    stage = (stage or "").lower()
    if stage not in VALID_PARAM_STAGES:
        raise GateRunError(
            "invalid_stage",
            "stage 는 'image' / 'deploy' / 'prod' 중 하나여야 합니다.",
            http_status=400,
            details={"stage": stage},
        )

    if stage == "image":
        target_job = gate.image_job_path
        if not target_job:
            raise GateRunError(
                "invalid_stage",
                "이 게이트에는 이미지 빌드 Job 이 없습니다.",
                http_status=400,
                details={"stage": stage},
            )
        base_url = gate.deploy_jenkins_url
        user = gate.deploy_jenkins_user
        token = gate.deploy_jenkins_token
    elif stage == "deploy":
        target_job = gate.deploy_job_path
        base_url = gate.deploy_jenkins_url
        user = gate.deploy_jenkins_user
        token = gate.deploy_jenkins_token
    else:  # prod
        if not gate.has_prod_deploy:
            return [], False
        target_job = gate.prod_job_path
        base_url = gate.prod_jenkins_url
        user = gate.prod_jenkins_user
        token = gate.prod_jenkins_token

    key_raw = f"qg_params:{gate.pk}:{stage}:{target_job or ''}"
    cache_key = "qgp:" + hashlib.sha1(key_raw.encode()).hexdigest()

    if not refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            # APP_IMAGE in-place 수정이 캐시 원본을 오염시키지 않도록 deep copy.
            # (LocMemCache 는 get 시 deepcopy 하지만 Redis/Memcached 백엔드 대비 명시)
            params = copy.deepcopy(cached)
            if run is not None and stage in ("deploy", "prod"):
                resolve_and_apply_app_image(params, run)
            elif stage == "prod" and image_build_number:
                _apply_app_image_default(params, image_build_number)
            return params, True

    client = JenkinsClient(base_url, user, token)
    try:
        info = client.get_job_info(target_job)
    except JenkinsError as exc:
        raise GateRunError(
            "jenkins_error", str(exc), http_status=502, details={"stage": stage}
        )
    parameters = info.get("parameters", [])
    cache.set(cache_key, parameters, timeout=120)

    if run is not None and stage in ("deploy", "prod"):
        resolve_and_apply_app_image(parameters, run)
    elif stage == "prod" and image_build_number:
        _apply_app_image_default(parameters, image_build_number)
    return parameters, False


def _apply_app_image_default(parameters: list, image_build_number) -> bool:
    """parameters 안 APP_IMAGE 의 default 를 build 번호 prefix 매칭으로 덮어쓴다.

    콘솔 추출 없이 choices 의 ``{image_build_number}-`` prefix 매칭만 하는 단순
    폴백 버전. 매칭 항목을 찾아 default 로 설정하면 True, 아니면 False.
    (prefix 매칭 로직의 단일 진실원 — resolve_and_apply_app_image 폴백/
     views._override_app_image_default 가 모두 이 함수에 위임한다.)
    """
    if not image_build_number:
        return False
    prefix = f"{image_build_number}-"
    for p in parameters:
        if p.get("name") != "APP_IMAGE":
            continue
        for c in (p.get("choices") or []):
            if isinstance(c, str) and c.startswith(prefix):
                p["default"] = c
                return True
        return False
    return False


def resolve_and_apply_app_image(
    parameters: list,
    run,
    *,
    repo_hint=None,
) -> bool:
    """콘솔 추출 태그로 APP_IMAGE default+choices 를 주입한다.

    Args:
        parameters: get_job_info 로 받은 파라미터 리스트 (in-place 수정).
        run: QualityGateRun 인스턴스.
        repo_hint: ECR repo 경로 힌트 (멀티모듈 대비, 보통 None 으로 충분).

    Returns:
        True  — APP_IMAGE 없거나 태그 주입/발견 성공.
        False — 콘솔 추출 실패 + 기존 choices prefix 매칭도 없음.
    """
    from apps.integrations.jenkins import extract_pushed_image_tag as _extract

    # APP_IMAGE 파라미터가 없으면 무관 → True
    param = next((p for p in parameters if p.get("name") == "APP_IMAGE"), None)
    if param is None:
        return True

    image_build_number = getattr(run, "image_build_number", None)

    # 콘솔에서 정확한 태그 추출
    tag = _extract(
        getattr(run, "image_console_output", None),
        image_build_number,
        repo_hint,
    )

    if tag:
        param["default"] = tag
        existing = list(param.get("choices") or [])
        if tag not in existing:
            param["choices"] = [tag] + existing
        return True

    # 폴백: choices 에 build_number prefix 항목이 있으면 그걸 default 로
    # (prefix 매칭은 _apply_app_image_default 에 위임 — 단일 진실원)
    return _apply_app_image_default(parameters, image_build_number)
