"""Quality Gate Run API — 조회/트리거/콘솔 (PRD §6.1, §6.2, §6.6, Phase D-2..6).

제어 액션 (cancel/rerun*/argocd retry/manual deploy) 은 Phase E 트랙. 여기에 절대 추가하지 않는다.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.shortcuts import get_object_or_404
from rest_framework import status as http_status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.qualitygates.models import QualityGate, QualityGateRun
from apps.qualitygates.services import GateRunError, trigger_gate_run

from .. import scopes as _scopes
from ..authentication import ApiKeyAuthentication
from ..permissions import CanWriteForControl, HasScope
from ..serializers.runs import (
    QualityGateRunDetailSerializer,
    QualityGateRunSummarySerializer,
)
from ..throttling import ReadThrottle, WriteThrottle


logger = logging.getLogger(__name__)

# 콘솔 응답 디폴트 정책 (PRD §15 의 보수적 디폴트)
_CONSOLE_FORCE_TAIL_THRESHOLD = 1_000_000  # 1MB
_CONSOLE_FORCE_TAIL_BYTES = 50_000          # 50KB


# =============================================
# 헬퍼
# =============================================

def _err(code: str, message: str, *, status_code: int, details: Optional[dict] = None):
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return Response(body, status=status_code)


def _gate_list_queryset(gate: QualityGate):
    qs = gate.runs.select_related(
        "gate", "gate__folder", "gate__subfolder", "triggered_by",
    ).order_by("-created_at")
    return qs


def _run_detail_queryset():
    return QualityGateRun.objects.select_related(
        "gate", "gate__folder", "gate__subfolder", "triggered_by",
    ).prefetch_related(
        "test_runs__gate_suite__test_suite",
        "test_runs__job_run",
    )


def _parse_int(value, *, default=None, minimum=None, maximum=None):
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and n < minimum:
        n = minimum
    if maximum is not None and n > maximum:
        n = maximum
    return n


# =============================================
# GET /api/v2/gates/<gate_id>/runs/  +  POST 트리거
# =============================================

@api_view(["GET", "POST"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope, CanWriteForControl])
@throttle_classes([ReadThrottle])  # method 분기로 write 는 별도 적용
def gate_runs(request, gate_id):
    """게이트의 Run 목록 + 새 Run 트리거.

    - GET: scope `runs:read`
    - POST: scope `runs:trigger`, role `can_write`
    """
    gate = get_object_or_404(
        QualityGate.objects.select_related("folder", "subfolder"), pk=gate_id,
    )
    if request.method == "GET":
        return _gate_runs_list(request, gate)
    return _gate_runs_trigger(request, gate)


# 메서드별 scope/throttle 매핑 — HasScope 가 검사한다.
# @api_view 는 함수를 WrappedAPIView 클래스로 감싸므로 permission 측 `view` 매개변수에서
# 보이려면 함수가 아닌 `view_class` 에 attr 를 설정해야 한다.
gate_runs.view_class.required_scopes = {
    "GET": _scopes.RUNS_READ,
    "POST": _scopes.RUNS_TRIGGER,
}


def _gate_runs_list(request, gate: QualityGate) -> Response:
    qs = _gate_list_queryset(gate)

    status_filter = request.query_params.get("status")
    if status_filter:
        valid = {s for s, _ in QualityGateRun.Status.choices}
        statuses = [s for s in status_filter.split(",") if s in valid]
        if statuses:
            qs = qs.filter(status__in=statuses)

    since = request.query_params.get("since")
    if since:
        from django.utils.dateparse import parse_datetime
        dt = parse_datetime(since)
        if dt is not None:
            qs = qs.filter(created_at__gte=dt)

    limit = _parse_int(
        request.query_params.get("limit"), default=20, minimum=1, maximum=200,
    )
    qs = qs[:limit]

    data = QualityGateRunSummarySerializer(qs, many=True).data
    return Response({
        "count": len(data),
        "results": list(data),
    })


def _gate_runs_trigger(request, gate: QualityGate) -> Response:
    # WriteThrottle 을 수동 적용 — @throttle_classes 는 클래스 자체가 method 분기를 모르므로.
    write_throttle = WriteThrottle()
    if not write_throttle.allow_request(request, None):
        wait = write_throttle.wait()
        resp = _err(
            "rate_limited", "요청 제한에 도달했습니다.",
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
        )
        if wait is not None:
            resp["Retry-After"] = str(int(wait))
        return resp

    body = request.data if isinstance(request.data, dict) else {}
    try:
        parameters = body.get("parameters") or {}
        if not isinstance(parameters, dict):
            return _err(
                "invalid_parameters", "parameters 는 객체여야 합니다.",
                status_code=http_status.HTTP_400_BAD_REQUEST,
                details={"got": type(parameters).__name__},
            )
        skip_image_build = bool(body.get("skip_image_build") or False)
        auto_mode_override = body.get("auto_mode_override", None)
        if auto_mode_override is not None and not isinstance(auto_mode_override, bool):
            return _err(
                "invalid_parameters", "auto_mode_override 는 boolean 또는 null 이어야 합니다.",
                status_code=http_status.HTTP_400_BAD_REQUEST,
            )
    except Exception as e:  # noqa: BLE001
        return _err(
            "invalid_parameters", f"요청 본문을 파싱할 수 없습니다: {e}",
            status_code=http_status.HTTP_400_BAD_REQUEST,
        )

    try:
        run = trigger_gate_run(
            gate=gate,
            user=request.user,
            parameters=parameters,
            skip_image_build=skip_image_build,
            auto_mode_override=auto_mode_override,
        )
    except GateRunError:
        # exception_handler 가 표준 에러 응답으로 변환.
        raise

    return Response(
        {
            "run_id": str(run.pk),
            "gate_id": str(gate.pk),
            "seq_no": run.seq_no,
            "status": run.status,
            "detail_url": f"/qualitygates/runs/{run.pk}/",
            "self_url": f"/api/v2/runs/{run.pk}/",
            "created_at": run.created_at.isoformat() if run.created_at else None,
        },
        status=http_status.HTTP_201_CREATED,
    )


# =============================================
# GET /api/v2/runs/<run_id>/
# =============================================

@api_view(["GET"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope])
@throttle_classes([ReadThrottle])
def run_detail(request, run_id):
    """Run 단건 — 호출 시점에 진행 중이면 _poll_gate_run 실행 후 직렬화."""
    run = get_object_or_404(_run_detail_queryset(), pk=run_id)

    # 외부 자동화가 폴링으로 진행을 동기화 — 진행 중일 때만.
    if run.is_running:
        try:
            from apps.qualitygates.views import _poll_gate_run  # lazy
            _poll_gate_run(run)
        except Exception as e:  # noqa: BLE001
            logger.warning("poll_gate_run failed for run=%s: %s", run.pk, e)
        # 폴링 후 상태가 바뀌었을 가능성 — prefetch 와 함께 다시 로드.
        run = _run_detail_queryset().get(pk=run.pk)

    data = QualityGateRunDetailSerializer(run).data
    response = Response(data)
    # 외부 폴링 힌트 — terminal 도달 시 0 (stop), 그 외 5 초.
    response["X-QGP-Poll-After"] = "0" if run.is_terminal else "5"
    return response


run_detail.view_class.required_scope = _scopes.RUNS_READ


# =============================================
# GET /api/v2/runs/<run_id>/status/  — 가벼운 폴링용
# =============================================


@api_view(["GET"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope])
@throttle_classes([ReadThrottle])
def run_status(request, run_id):
    """Run 의 status / phase / terminal 만 가볍게 반환 — 폴링 비용 최소화.

    detail 과 동일하게 진행 중이면 `_poll_gate_run` 을 한 번 돌려 외부 진행을
    이어준다. detail 의 full payload 가 필요 없는 호출자(예: 단순 "끝났니?"
    체크) 가 사용한다. X-QGP-Poll-After 헤더는 detail 과 동일.
    """
    from .. import phase as _phase_mod

    try:
        # result_label/is_terminal/is_running 은 status 에서 derive 되는 property —
        # .only("status", "seq_no") 만으로 충분.
        run = QualityGateRun.objects.only("pk", "status", "seq_no").get(pk=run_id)
    except QualityGateRun.DoesNotExist:
        from django.http import Http404
        raise Http404("run_not_found")

    if run.is_running:
        try:
            from apps.qualitygates.views import _poll_gate_run  # lazy

            _poll_gate_run(run)
        except Exception as e:  # noqa: BLE001
            logger.warning("poll_gate_run failed for run=%s: %s", run.pk, e)
        run.refresh_from_db()

    body = {
        "run_id": str(run.pk),
        "seq_no": run.seq_no,
        "status": run.status,
        "phase": _phase_mod.phase_for_run(run.status),
        "is_terminal": run.is_terminal,
        "result": run.result_label,
    }
    response = Response(body)
    response["X-QGP-Poll-After"] = "0" if run.is_terminal else "5"
    return response


run_status.view_class.required_scope = _scopes.RUNS_READ


# =============================================
# GET /api/v2/runs/<run_id>/console/<stage>/
# =============================================

@api_view(["GET"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope])
@throttle_classes([ReadThrottle])
def run_console(request, run_id, stage):
    """단계별 Jenkins 콘솔 로그. ?tail=N (마지막 N 문자) 지원."""
    stage = (stage or "").lower()
    if stage not in ("image", "deploy", "prod"):
        return _err(
            "invalid_stage",
            "stage 는 'image' / 'deploy' / 'prod' 만 가능합니다.",
            status_code=http_status.HTTP_400_BAD_REQUEST,
            details={"stage": stage},
        )

    run = get_object_or_404(
        QualityGateRun.objects.select_related("gate"), pk=run_id,
    )

    output = getattr(run, f"{stage}_console_output", "") or ""
    build_number = getattr(run, f"{stage}_build_number", None)
    build_url = getattr(run, f"{stage}_build_url", "") or None

    total_length = len(output)
    truncated = False

    tail_raw = request.query_params.get("tail")
    tail = _parse_int(tail_raw, default=None, minimum=1)

    if tail is not None:
        if total_length > tail:
            output = output[-tail:]
            truncated = True
    else:
        # tail 미지정 + 1MB 초과 → 마지막 50KB 만 (PRD §15 보수적 디폴트).
        if total_length > _CONSOLE_FORCE_TAIL_THRESHOLD:
            output = output[-_CONSOLE_FORCE_TAIL_BYTES:]
            truncated = True

    return Response({
        "stage": stage,
        "build_number": build_number,
        "build_url": build_url,
        "length": total_length,
        "truncated": truncated,
        "output": output,
    })


run_console.view_class.required_scope = _scopes.RUNS_READ
