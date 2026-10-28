"""Quality Gate Run API — 조회/트리거/콘솔 (PRD §6.1, §6.2, §6.6, Phase D-2..6).

제어 액션 (cancel/rerun*/argocd retry/manual deploy) 은 Phase E 트랙. 여기에 절대 추가하지 않는다.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.shortcuts import get_object_or_404
from django.urls import reverse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers
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

# 문서 전용 상수 — @extend_schema 에서 enum/example 을 만드는 데만 사용 (런타임 무영향).
_TAGS = ["Quality Gate Runs"]
_QG_STATUS_CHOICES = [s for s, _ in QualityGateRun.Status.choices]
_QG_PHASE_CHOICES = [
    "queued", "running", "awaiting_input", "succeeded", "failed", "cancelled",
]
_QG_RESULT_CHOICES = ["PASS", "FAIL", "QG PASS", "CANCELLED"]

# 콘솔 응답 디폴트 정책 (PRD §15 의 보수적 디폴트)
_CONSOLE_FORCE_TAIL_THRESHOLD = 1_000_000  # 1MB
_CONSOLE_FORCE_TAIL_BYTES = 50_000          # 50KB

# path 파라미터 — 여러 엔드포인트가 공유하므로 한 곳에서 정의.
_GATE_ID_PATH_PARAM = OpenApiParameter(
    name="gate_id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
    required=True,
    description=(
        "(필수) 대상 게이트의 id 입니다. `GET /api/v2/gates/` 응답 목록에서 원하는 "
        "게이트를 찾아 그 항목의 `id` 필드 값을 넣습니다."
    ),
)

_RUN_ID_PATH_PARAM = OpenApiParameter(
    name="run_id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
    required=True,
    description=(
        "(필수) 대상 Run 의 id 입니다. `GET /api/v2/gates/{gate_id}/runs/` 목록 "
        "응답 각 항목의 `run_id` 필드, 또는 `POST /api/v2/gates/{gate_id}/runs/` "
        "트리거 응답의 `run_id` 필드 값을 넣습니다."
    ),
)


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

@extend_schema(
    methods=["GET"],
    tags=_TAGS,
    summary="게이트의 Quality Gate Run 목록 조회",
    description=(
        "이 게이트(Quality Gate: 배포 전 품질 검증 파이프라인 하나)에서 지금까지 "
        "실행된 Run(런 — 파이프라인을 한 번 실행한 기록)들을 최근 실행순으로 "
        "보여줍니다.\n"
        "\n"
        "**언제 쓰나요**: 이 게이트의 과거 실행 이력을 확인하고 싶을 때, 또는 새 "
        "Run 을 트리거하기 전에 지금 진행 중인 Run 이 있는지(있으면 새로 시작할 "
        "수 없습니다) 미리 확인할 때 사용합니다.\n"
        "\n"
        "`status`/`since`/`limit` 쿼리 파라미터로 필요한 것만 걸러서 받을 수 "
        "있습니다."
    ),
    parameters=[
        _GATE_ID_PATH_PARAM,
        OpenApiParameter(
            name="status",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 콤마(,)로 여러 상태를 나열하면 그 상태들만 걸러서 "
                "보여줍니다(OR 조건) — 예: `completed,failed` (완료 또는 실패 "
                "Run 만 조회). 허용값과 뜻: "
                "`pending`(대기) / `building_image`(이미지 빌드 중) / "
                "`image_build_failed`(이미지 빌드 실패) / "
                "`waiting_deploy`(검수대상 배포 승인 대기) / `deploying`(배포 중) / "
                "`deploy_failed`(배포 실패) / `waiting_test`(테스트 대기) / "
                "`testing`(테스트 진행 중) / "
                "`qg_passed`(검수 통과, 운영 반영 승인 대기) / "
                "`failed`(테스트 실패) / `prod_deploying`(운영 반영 배포 중) / "
                "`prod_deploy_failed`(운영 반영 배포 실패) / "
                "`completed`(전체 완료) / `cancelled`(취소됨). "
                "(전체 목록: " + ", ".join(_QG_STATUS_CHOICES) + ")"
            ),
        ),
        OpenApiParameter(
            name="since",
            type=OpenApiTypes.DATETIME,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 이 시각 이후에 생성된 Run 만 반환합니다 — 예: "
                "`2026-07-01T00:00:00Z` (ISO 8601 형식. 이 시각 이후 생성분만 "
                "조회). 안 넣으면 생성 시각으로 거르지 않습니다."
            ),
        ),
        OpenApiParameter(
            name="limit",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 한 번에 받을 최대 개수입니다 — 예: `50` (최근 50건만 "
                "조회). 안 넣으면 20건, 최대 200건까지 지정 가능합니다."
            ),
        ),
    ],
    responses={
        200: inline_serializer(
            name="GateRunListResponse",
            fields={
                "count": drf_serializers.IntegerField(
                    help_text="이번 응답에 담긴 결과 개수."
                ),
                "results": QualityGateRunSummarySerializer(
                    many=True,
                    help_text="조건에 맞는 Run 요약 목록 (최신순).",
                ),
            },
        ),
        404: OpenApiResponse(
            description="`not_found` — 이 gate_id 에 해당하는 게이트가 없습니다."
        ),
    },
)
@extend_schema(
    methods=["POST"],
    tags=_TAGS,
    summary="새 Quality Gate Run 트리거",
    description=(
        "이 게이트의 배포 파이프라인을 처음부터 실행합니다 — 이미지 빌드 → "
        "검수대상 환경 배포 → 자동 테스트 → (설정돼 있으면) 검수통과 환경 배포 "
        "순서로 진행됩니다.\n"
        "\n"
        "**언제 쓰나요**: CI 파이프라인에서 새 이미지가 준비됐을 때 자동으로 "
        "호출하거나, 사람이 수동으로 배포 검증을 시작하고 싶을 때 호출합니다.\n"
        "\n"
        "같은 게이트에 **이미 진행 중인 Run 이 있으면** 새 Run 을 만들지 않고 "
        "409 `active_run_exists` 를 반환합니다 — 응답의 `details.active_run_id` "
        "로 먼저 그 Run 의 상태를 확인하세요."
    ),
    parameters=[_GATE_ID_PATH_PARAM],
    request=inline_serializer(
        name="GateRunTriggerRequest",
        fields={
            "parameters": drf_serializers.DictField(
                child=drf_serializers.CharField(allow_blank=True),
                required=False,
                help_text=(
                    "Jenkins Job 에 넘길 파라미터입니다 (선택, 기본값은 빈 객체 "
                    "`{}`). 게이트에 연결된 Jenkins Job 이 별도 파라미터를 요구하지 "
                    "않으면 비워둬도 됩니다."
                ),
            ),
            "skip_image_build": drf_serializers.BooleanField(
                required=False,
                default=False,
                help_text=(
                    "true 로 보내면 1단계인 이미지 빌드를 건너뛰고 바로 검수대상 "
                    "환경 배포부터 시작합니다. 이미 만들어 둔 이미지로 배포·테스트만 "
                    "다시 검증하고 싶을 때 사용합니다."
                ),
            ),
            "auto_mode_override": drf_serializers.BooleanField(
                required=False,
                allow_null=True,
                help_text=(
                    "이 Run 한 번에 한해 게이트의 auto_mode(테스트 통과 시 다음 "
                    "단계로 사람 개입 없이 자동 진행할지 여부) 설정을 덮어씁니다. "
                    "`null` 이거나 생략하면 게이트에 저장된 기본값을 그대로 "
                    "따릅니다."
                ),
            ),
        },
    ),
    examples=[
        OpenApiExample(
            "기본 트리거",
            value={"parameters": {}, "skip_image_build": False},
            request_only=True,
            description="파라미터 없이 게이트 기본 설정 그대로 트리거하는 가장 단순한 예시입니다.",
        ),
    ],
    responses={
        201: OpenApiResponse(
            response=inline_serializer(
                name="GateRunTriggerResponse",
                fields={
                    "run_id": drf_serializers.CharField(
                        help_text="새로 생성된 Run 의 고유 id. 이후 조회/제어 API 호출에 이 값을 씁니다."
                    ),
                    "gate_id": drf_serializers.CharField(
                        help_text="이 Run 이 속한 게이트의 id."
                    ),
                    "seq_no": drf_serializers.IntegerField(
                        help_text="이 게이트 안에서 몇 번째 실행인지 나타내는 일련번호 (1부터 순증가)."
                    ),
                    "status": drf_serializers.ChoiceField(
                        choices=_QG_STATUS_CHOICES,
                        help_text="생성 직후의 세부 상태 (보통 `pending`).",
                    ),
                    "detail_url": drf_serializers.CharField(
                        help_text="포털 화면에서 이 Run 을 눈으로 확인할 수 있는 웹 페이지 경로."
                    ),
                    "self_url": drf_serializers.CharField(
                        help_text="이 Run 의 상세 조회 API 경로 (`GET` 으로 진행 상황 확인)."
                    ),
                    "created_at": drf_serializers.DateTimeField(
                        allow_null=True, help_text="Run 이 생성된 시각 (ISO 8601)."
                    ),
                },
            ),
            description="Run 이 새로 생성되어 파이프라인이 시작됐습니다. 응답의 `run_id`/`self_url` 로 이후 진행 상황을 조회하세요.",
        ),
        400: OpenApiResponse(
            description=(
                "`invalid_parameters` — `parameters` 를 객체(JSON object) 형태가 "
                "아니게 보냈거나, `auto_mode_override` 에 true/false/null 이 아닌 "
                "값을 보낸 경우입니다."
            ),
        ),
        404: OpenApiResponse(
            description="`not_found` — 이 gate_id 에 해당하는 게이트가 없습니다."
        ),
        409: OpenApiResponse(
            description=(
                "`active_run_exists` — 이 게이트에서 이미 진행 중인 Run 이 있어 "
                "새로 시작할 수 없습니다. 응답의 `details.active_run_id` 로 그 "
                "Run 의 상태를 확인하거나, 끝날 때까지 기다린 뒤 다시 시도하세요."
            ),
        ),
    },
)
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
            "detail_url": reverse("qualitygates:run_detail", kwargs={"pk": run.pk}),
            "self_url": reverse("api_v2:run_detail", kwargs={"run_id": run.pk}),
            "created_at": run.created_at.isoformat() if run.created_at else None,
        },
        status=http_status.HTTP_201_CREATED,
    )


# =============================================
# GET /api/v2/runs/<run_id>/
# =============================================

@extend_schema(
    tags=_TAGS,
    summary="Quality Gate Run 단건 상세 조회",
    description=(
        "Run 하나의 모든 정보를 보여줍니다 — 이미지 빌드/검수대상 배포/테스트/"
        "검수통과 배포 각 단계의 진행 상황, Argo CD(쿠버네티스 배포를 자동으로 "
        "동기화해 주는 도구) sync 상태, Jenkins 콘솔 로그 주소 등을 전부 포함한 "
        "전체 필드입니다.\n"
        "\n"
        "**언제 쓰나요**: `run_id` 하나를 콕 집어 자세한 진행 상황·이력을 확인하고 "
        "싶을 때 사용합니다. 상태만 가볍게 확인하면 되는 반복 폴링에는 "
        "아래 `GET /runs/{run_id}/status/`(경량 조회) 쪽이 더 가볍습니다.\n"
        "\n"
        "이 Run 이 아직 진행 중이면, 호출하는 바로 그 순간에 서버가 내부적으로 한 "
        "번 상태를 점검(폴링)해서 최신 상태로 갱신한 뒤 응답합니다 — 그래서 외부 "
        "자동화가 이 API 를 반복 호출(폴링: 끝났는지 몇 초 간격으로 반복해서 물어보는 "
        "방식)하는 것만으로도 파이프라인이 계속 앞으로 진행됩니다."
    ),
    parameters=[
        _RUN_ID_PATH_PARAM,
        OpenApiParameter(
            name="X-QGP-Poll-After",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.HEADER,
            response=True,
            description=(
                "다음 번 폴링까지 몇 초 기다리면 좋을지 알려주는 값입니다. Run 이 "
                "완전히 끝났으면(terminal — 더 이상 상태가 바뀌지 않는 최종 상태) "
                "`0`(더 조회할 필요 없음), 아직 진행 중이면 `5`(5초 후 다시 조회 "
                "권장)."
            ),
        ),
    ],
    responses={
        200: OpenApiResponse(
            response=QualityGateRunDetailSerializer,
            description="Run 상세 정보 전체.",
        ),
        404: OpenApiResponse(
            description="`not_found` — 이 run_id 에 해당하는 Run 이 없습니다."
        ),
    },
)
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


@extend_schema(
    tags=_TAGS,
    summary="Quality Gate Run 상태 폴링 조회 (경량)",
    description=(
        "Run 이 지금 어떤 상태인지 핵심 정보 4가지(`status`/`phase`/`is_terminal`/"
        "`result`)만 가볍게 반환합니다 — 전체 상세 정보가 필요 없을 때 폴링(끝났는지 "
        "몇 초 간격으로 반복해서 확인하는 방식) 비용을 줄이기 위한 전용 API 입니다.\n"
        "\n"
        "**언제 쓰나요**: CI 파이프라인이나 자동화 스크립트에서 \"아직 진행 중인가? "
        "끝났으면 성공/실패인가?\" 만 반복 확인하고 싶을 때 사용합니다. 자세한 단계별 "
        "정보(콘솔 로그 URL 등)까지 필요하면 위의 `run_detail`(상세 조회)을 대신 "
        "쓰세요.\n"
        "\n"
        "`run_detail` 과 동일하게, Run 이 아직 진행 중이면 호출 시점에 내부 상태 "
        "점검(폴링)을 한 번 수행한 뒤 응답합니다."
    ),
    parameters=[
        _RUN_ID_PATH_PARAM,
        OpenApiParameter(
            name="X-QGP-Poll-After",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.HEADER,
            response=True,
            description=(
                "다음 번 폴링까지 몇 초 기다리면 좋을지 알려주는 값입니다. terminal "
                "(더 이상 상태가 바뀌지 않는 최종 상태) 도달 시 `0`, 그 외에는 `5`."
            ),
        ),
    ],
    responses={
        200: inline_serializer(
            name="GateRunStatusResponse",
            fields={
                "run_id": drf_serializers.CharField(help_text="조회한 Run 의 id."),
                "seq_no": drf_serializers.IntegerField(
                    help_text="이 게이트 안에서 몇 번째 실행인지 나타내는 일련번호."
                ),
                "status": drf_serializers.ChoiceField(
                    choices=_QG_STATUS_CHOICES,
                    help_text="세부 상태 (14가지 중 하나). 초보자는 아래 `phase` 를 보는 걸 권장합니다.",
                ),
                "phase": drf_serializers.ChoiceField(
                    choices=_QG_PHASE_CHOICES,
                    help_text=(
                        "status 를 이해하기 쉽게 6단계로 단순화한 값입니다: "
                        "`queued`(대기 중) / `running`(진행 중) / "
                        "`awaiting_input`(사람의 승인·입력 대기 중) / "
                        "`succeeded`(성공 종료) / `failed`(실패 종료) / "
                        "`cancelled`(취소됨)."
                    ),
                ),
                "is_terminal": drf_serializers.BooleanField(
                    help_text="true 면 더 이상 상태가 바뀌지 않는 최종 상태입니다 — 폴링을 멈춰도 됩니다."
                ),
                "result": drf_serializers.ChoiceField(
                    choices=_QG_RESULT_CHOICES, allow_null=True,
                    help_text=(
                        "최종 합/불 판정입니다: `PASS`(전체 완료, 성공) / "
                        "`QG PASS`(테스트까지 통과했지만 아직 운영 반영 승인 대기 "
                        "중일 수 있음) / `FAIL`(실패) / `CANCELLED`(취소). 아직 "
                        "판정이 나지 않았으면 `null` 입니다."
                    ),
                ),
            },
        ),
        404: OpenApiResponse(
            description="`not_found` — 이 run_id 에 해당하는 Run 이 없습니다."
        ),
    },
    examples=[
        OpenApiExample(
            "진행 중",
            value={
                "run_id": "5c9b8e2e-...", "seq_no": 87, "status": "testing",
                "phase": "running", "is_terminal": False, "result": None,
            },
            response_only=True,
            description="테스트 단계를 진행 중인 Run. 아직 판정이 나지 않아 result 는 null 입니다.",
        ),
        OpenApiExample(
            "종료 (성공)",
            value={
                "run_id": "5c9b8e2e-...", "seq_no": 87, "status": "completed",
                "phase": "succeeded", "is_terminal": True, "result": "PASS",
            },
            response_only=True,
            description="모든 단계가 끝나고 성공(PASS)한 Run. is_terminal=true 이므로 폴링을 멈춰도 됩니다.",
        ),
    ],
)
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

@extend_schema(
    tags=_TAGS,
    summary="Run 단계별 Jenkins 콘솔 로그 조회",
    description=(
        "지정한 단계(`image`=이미지 빌드 / `deploy`=검수대상 배포 / `prod`=검수통과 "
        "배포)의 Jenkins 콘솔 로그(실행 중 출력된 텍스트 전체)를 그대로 반환합니다.\n"
        "\n"
        "**언제 쓰나요**: Run 이 실패했을 때 정확히 어떤 에러가 났는지 원인을 확인할 "
        "때, 또는 진행 중인 단계의 실시간 출력을 보고 싶을 때 사용합니다.\n"
        "\n"
        "로그가 너무 길면 응답이 무거워지므로 두 가지 방식으로 잘라서 받을 수 "
        "있습니다: `tail` 파라미터를 지정하면 마지막 N 글자만, `tail` 을 지정하지 "
        "않았는데 원문이 1MB 를 넘으면 서버가 자동으로 마지막 50KB 만 잘라서 주고 "
        "`truncated=true` 로 표시합니다(원본 전체 길이는 `length` 필드로 계속 알 수 "
        "있습니다)."
    ),
    parameters=[
        _RUN_ID_PATH_PARAM,
        OpenApiParameter(
            name="stage",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.PATH,
            required=True,
            enum=["image", "deploy", "prod"],
            description=(
                "(필수) 조회할 단계입니다 — API 응답에서 얻는 값이 아니라 아래 "
                "중 하나를 직접 선택해 넣습니다. `image`(이미지 빌드) / "
                "`deploy`(검수대상 환경 배포) / `prod`(검수통과 환경 배포). 예: "
                "`deploy`."
            ),
        ),
        OpenApiParameter(
            name="tail",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 로그의 마지막 N 글자(문자 수)만 잘라서 반환합니다 — 예: "
                "`2000` (로그 끝부분 2000자만, 실패 직전 메시지를 빠르게 확인할 때 "
                "유용). 안 넣으면 전체(단, 1MB 초과 시 자동으로 마지막 50KB)를 "
                "반환합니다."
            ),
        ),
    ],
    responses={
        200: inline_serializer(
            name="GateRunConsoleResponse",
            fields={
                "stage": drf_serializers.ChoiceField(
                    choices=["image", "deploy", "prod"],
                    help_text="조회한 단계.",
                ),
                "build_number": drf_serializers.IntegerField(
                    allow_null=True,
                    help_text="해당 단계의 Jenkins 빌드 번호 (아직 시작하지 않았으면 `null`).",
                ),
                "build_url": drf_serializers.CharField(
                    allow_null=True,
                    help_text="Jenkins 상의 해당 빌드 페이지 주소 (없으면 `null`).",
                ),
                "length": drf_serializers.IntegerField(
                    help_text="원본 로그 전체 길이(문자 수). 응답이 잘렸어도(`truncated=true`) 이 값은 항상 원본 전체 길이입니다."
                ),
                "truncated": drf_serializers.BooleanField(
                    help_text="true 면 로그가 잘려서 일부만 왔다는 뜻입니다 (위 설명 참고)."
                ),
                "output": drf_serializers.CharField(
                    help_text="실제 콘솔 로그 텍스트입니다."
                ),
            },
        ),
        400: OpenApiResponse(
            description=(
                "`invalid_stage` — stage 가 `image`/`deploy`/`prod` 중 하나가 아닙니다."
            ),
        ),
        404: OpenApiResponse(
            description="`not_found` — 이 run_id 에 해당하는 Run 이 없습니다."
        ),
    },
)
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
