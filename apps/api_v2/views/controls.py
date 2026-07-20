"""Phase E — Quality Gate Run 제어 API (PRD §6.3).

cancel / rerun / rerun-test / rerun-deploy / rerun-prod / deploy / prod-deploy /
argocd retry. 모든 액션이 service 함수의 얇은 래퍼 — 비즈니스 룰 위반은
`GateRunError` 가 던져지고 `apps/api_v2/exceptions.py` 의 일반화된 handler 가
표준 에러 응답으로 변환한다.

응답: 액션 후 Run 의 detail 직렬화 (`QualityGateRunDetailSerializer`).
외부 호출자가 같은 응답으로 후속 폴링을 바로 시작할 수 있다.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.qualitygates import services as qg_services
from apps.qualitygates.models import QualityGateRun

from .. import scopes as S
from ..authentication import ApiKeyAuthentication
from ..permissions import CanWriteForControl, HasScope
from ..serializers.runs import QualityGateRunDetailSerializer
from ..throttling import WriteThrottle


def _get_run(run_id):
    try:
        return (
            QualityGateRun.objects
            .select_related("gate", "gate__folder", "gate__subfolder", "triggered_by")
            .get(pk=run_id)
        )
    except QualityGateRun.DoesNotExist:
        from django.http import Http404
        raise Http404("run_not_found")


def _detail_response(run, status_code=status.HTTP_200_OK, request=None):
    fresh = (
        QualityGateRun.objects
        .select_related("gate", "gate__folder", "gate__subfolder", "triggered_by")
        .prefetch_related("test_runs__gate_suite__test_suite", "test_runs__job_run")
        .get(pk=run.pk)
    )
    serializer = QualityGateRunDetailSerializer(
        fresh, context={"request": request} if request else {}
    )
    return Response(serializer.data, status=status_code)


def _parse_parameters(request):
    """body 의 parameters dict 추출. 빈 body 는 {} 로 처리."""
    if not request.data:
        return {}
    params = request.data.get("parameters") if isinstance(request.data, dict) else None
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValidationError({"parameters": "must be an object"})
    return {k: ("" if v is None else str(v)) for k, v in params.items()}


def _common_decorators(required_scope):
    """control 류 공통 데코레이터 — POST 만, scope + writer + WriteThrottle."""

    def decorator(view_func):
        view_func = throttle_classes([WriteThrottle])(view_func)
        view_func = permission_classes([HasScope, CanWriteForControl])(view_func)
        view_func = authentication_classes([ApiKeyAuthentication, SessionAuthentication])(view_func)
        view_func = api_view(["POST"])(view_func)
        # api_view 가 만든 클래스에 required_scope 부여
        if hasattr(view_func, "view_class"):
            view_func.view_class.required_scope = required_scope
        view_func.required_scope = required_scope
        return view_func

    return decorator


# ---------------------------------------------------------------------------
# 액션 8종
# ---------------------------------------------------------------------------


@_common_decorators(S.RUNS_CONTROL)
def cancel_run(request, run_id):
    run = _get_run(run_id)
    qg_services.cancel_run(run=run, user=request.user)
    return _detail_response(run, request=request)


@_common_decorators(S.RUNS_CONTROL)
def rerun_gate(request, run_id):
    run = _get_run(run_id)
    new_run = qg_services.rerun_gate(run=run, user=request.user)
    return _detail_response(new_run, status_code=status.HTTP_201_CREATED, request=request)


@_common_decorators(S.RUNS_CONTROL)
def rerun_test(request, run_id):
    run = _get_run(run_id)
    qg_services.rerun_test(run=run, user=request.user)
    return _detail_response(run, request=request)


@_common_decorators(S.RUNS_CONTROL)
def rerun_deploy(request, run_id):
    """검수대상 배포만 재실행 — DEPLOY_FAILED 상태에서만 허용."""
    run = _get_run(run_id)
    if run.status != QualityGateRun.Status.DEPLOY_FAILED:
        raise qg_services.GateRunError(
            "run_not_actionable",
            "검수대상 배포만 재실행할 수 없는 상태입니다.",
            http_status=409,
            details={"current_status": run.status},
        )
    qg_services.trigger_deploy(
        run=run, user=request.user, parameters=_parse_parameters(request)
    )
    return _detail_response(run, request=request)


@_common_decorators(S.RUNS_CONTROL)
def rerun_prod(request, run_id):
    """검수통과 배포만 재실행 — PROD_DEPLOY_FAILED 상태에서만 허용."""
    run = _get_run(run_id)
    if run.status != QualityGateRun.Status.PROD_DEPLOY_FAILED:
        raise qg_services.GateRunError(
            "run_not_actionable",
            "검수통과 배포만 재실행할 수 없는 상태입니다.",
            http_status=409,
            details={"current_status": run.status},
        )
    qg_services.trigger_prod(
        run=run, user=request.user, parameters=_parse_parameters(request)
    )
    return _detail_response(run, request=request)


@_common_decorators(S.RUNS_CONTROL)
def trigger_deploy(request, run_id):
    """WAITING_DEPLOY 에서 수동 deploy 트리거."""
    run = _get_run(run_id)
    if run.status != QualityGateRun.Status.WAITING_DEPLOY:
        raise qg_services.GateRunError(
            "run_not_actionable",
            "이 단계에서는 배포를 수동 트리거할 수 없습니다.",
            http_status=409,
            details={"current_status": run.status},
        )
    qg_services.trigger_deploy(
        run=run, user=request.user, parameters=_parse_parameters(request)
    )
    return _detail_response(run, request=request)


@_common_decorators(S.RUNS_CONTROL)
def trigger_prod(request, run_id):
    """QG_PASSED 에서 수동 prod 트리거."""
    run = _get_run(run_id)
    if run.status != QualityGateRun.Status.QG_PASSED:
        raise qg_services.GateRunError(
            "run_not_actionable",
            "이 단계에서는 검수통과 배포를 수동 트리거할 수 없습니다.",
            http_status=409,
            details={"current_status": run.status},
        )
    qg_services.trigger_prod(
        run=run, user=request.user, parameters=_parse_parameters(request)
    )
    return _detail_response(run, request=request)


@_common_decorators(S.RUNS_CONTROL)
def argocd_retry(request, run_id, stage):
    if stage not in ("deploy", "prod"):
        raise ValidationError({"stage": "stage must be 'deploy' or 'prod'"})
    run = _get_run(run_id)
    qg_services.argocd_retry(run=run, stage=stage, user=request.user)
    return _detail_response(run, status_code=status.HTTP_202_ACCEPTED, request=request)
