"""Phase E — Quality Gate Run 제어 API (PRD §6.3).

cancel / rerun / rerun-test / rerun-deploy / rerun-prod / deploy / prod-deploy /
argocd retry. 모든 액션이 service 함수의 얇은 래퍼 — 비즈니스 룰 위반은
`GateRunError` 가 던져지고 `apps/api_v2/exceptions.py` 의 일반화된 handler 가
표준 에러 응답으로 변환한다.

응답: 액션 후 Run 의 detail 직렬화 (`QualityGateRunDetailSerializer`).
외부 호출자가 같은 응답으로 후속 폴링을 바로 시작할 수 있다.
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers
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


# 문서 전용 상수/헬퍼 — @extend_schema 에서만 쓰인다 (런타임 무영향).
_TAGS = ["Quality Gate Run 제어"]

# rerun-deploy / rerun-prod / deploy / prod-deploy 4개 엔드포인트가 공유하는 선택적
# body — 모두 동일 스키마이므로 같은 이름의 inline_serializer 를 재사용해 문서에서도
# 하나의 컴포넌트로 묶인다.
_PARAMETERS_REQUEST = inline_serializer(
    name="ControlActionParametersRequest",
    fields={
        "parameters": drf_serializers.DictField(
            child=drf_serializers.CharField(allow_blank=True),
            required=False,
            help_text=(
                "(선택) Jenkins Job 에 넘길 파라미터입니다 — 예: "
                "`{\"APP_IMAGE\": \"registry/foo:208-20260526_093435\"}` (배포에 "
                "쓸 이미지:태그를 직접 지정). 기본값은 빈 객체 `{}` 이며, 보통 이 "
                "Run 을 처음 트리거할 때 썼던 파라미터를 그대로 다시 보내면 됩니다."
            ),
        ),
    },
)

# 4개 control 엔드포인트가 공유하는 body 예시 — 명세 하나로 재사용.
_PARAMETERS_REQUEST_EXAMPLES = [
    OpenApiExample(
        "기본값 그대로 재시도",
        value={"parameters": {}},
        request_only=True,
        description="parameters 를 비워두면 이 Run 에 저장된 기존 파라미터를 그대로 사용합니다.",
    ),
    OpenApiExample(
        "이미지 태그 직접 지정",
        value={"parameters": {"APP_IMAGE": "registry/foo:208-20260526_093435"}},
        request_only=True,
        description="배포에 쓸 이미지:태그(APP_IMAGE)를 이번 호출에 한해 직접 지정하는 예시입니다.",
    ),
]

# run_id path 파라미터 — 이 파일의 모든 control 엔드포인트가 공유.
_RUN_ID_PATH_PARAM = OpenApiParameter(
    name="run_id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
    required=True,
    description=(
        "(필수) 제어할 Run 의 id 입니다. `GET /api/v2/gates/{gate_id}/runs/` 목록 "
        "응답 각 항목의 `run_id` 필드, 또는 `POST /api/v2/gates/{gate_id}/runs/` "
        "트리거 응답의 `run_id` 필드 값을 넣습니다."
    ),
)

_RUN_NOT_FOUND = OpenApiResponse(
    description="`not_found` — 이 run_id 에 해당하는 Run 이 없습니다. run_id 를 다시 확인해 주세요."
)


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


@extend_schema(
    tags=_TAGS,
    summary="Quality Gate Run 취소",
    description=(
        "지금 진행 중인 Run 을 중간에 멈춥니다. 현재 실행 중인 단계(이미지 빌드/"
        "배포/테스트)의 Jenkins 빌드도 함께 중단(abort)을 시도합니다.\n"
        "\n"
        "**언제 쓰나요**: 잘못된 조건으로 실행을 시작했거나, 급하게 중단하고 다시 "
        "시작해야 할 때 사용합니다. 한 번 취소된 Run 은 되돌릴 수 없습니다 — 다시 "
        "실행하려면 아래 '처음부터 재실행'으로 새 Run 을 시작하세요."
    ),
    parameters=[_RUN_ID_PATH_PARAM],
    request=None,
    responses={
        200: OpenApiResponse(
            response=QualityGateRunDetailSerializer,
            description="취소가 반영된 Run 상세 정보.",
        ),
        404: _RUN_NOT_FOUND,
        409: OpenApiResponse(
            description=(
                "`cannot_cancel` — 이미 끝난(성공/실패/취소) Run 이라 취소할 게 "
                "없습니다. 응답의 `details.status` 로 현재 상태를 확인하세요."
            ),
        ),
    },
)
@_common_decorators(S.RUNS_CONTROL)
def cancel_run(request, run_id):
    run = _get_run(run_id)
    qg_services.cancel_run(run=run, user=request.user)
    return _detail_response(run, request=request)


@extend_schema(
    tags=_TAGS,
    summary="Quality Gate Run 처음부터 재실행",
    description=(
        "실패한 Run 과 같은 게이트에 **완전히 새로운 Run 을 하나 만들어** 이미지 "
        "빌드부터 처음부터 다시 시작합니다. (기존 Run 을 고치는 게 아니라 새 Run 이 "
        "생성되는 것이라 응답이 201 입니다.)\n"
        "\n"
        "**언제 쓰나요**: 실패 원인이 이미지·설정처럼 처음부터 다시 해야 해결되는 "
        "경우, 또는 어느 단계에서 실패했든 상관없이 통째로 다시 검증받고 싶을 때 "
        "사용합니다. 아래 상태(모두 '실패'로 끝난 상태)에서만 가능합니다: "
        "`FAILED`(테스트 실패) / `IMAGE_BUILD_FAILED`(이미지 빌드 실패) / "
        "`DEPLOY_FAILED`(검수대상 배포 실패) / `PROD_DEPLOY_FAILED`(검수통과 배포 "
        "실패). 아직 진행 중이거나 이미 성공한 Run 은 재실행할 수 없습니다."
    ),
    parameters=[_RUN_ID_PATH_PARAM],
    request=None,
    responses={
        201: OpenApiResponse(
            response=QualityGateRunDetailSerializer,
            description="새로 생성된 Run 상세 정보.",
        ),
        404: _RUN_NOT_FOUND,
        409: OpenApiResponse(
            description=(
                "`cannot_rerun` — 실패 상태가 아니라서(예: 아직 진행 중이거나 이미 "
                "성공) 재실행할 수 없습니다. 응답의 `details.status` 로 현재 상태를 "
                "확인하세요."
            ),
        ),
    },
)
@_common_decorators(S.RUNS_CONTROL)
def rerun_gate(request, run_id):
    run = _get_run(run_id)
    new_run = qg_services.rerun_gate(run=run, user=request.user)
    return _detail_response(new_run, status_code=status.HTTP_201_CREATED, request=request)


@extend_schema(
    tags=_TAGS,
    summary="Quality Gate Run 테스트만 재실행",
    description=(
        "**같은 Run 을 그대로 유지**한 채, 통과(PASSED)하지 못한 테스트만 골라 다시 "
        "실행합니다 — 이미 통과한 테스트는 다시 돌리지 않습니다. 배포는 이미 끝난 "
        "상태이므로 배포 단계는 건드리지 않습니다.\n"
        "\n"
        "**언제 쓰나요**: 검수대상 배포는 성공했는데 테스트 일부만 실패(예: 일시적 "
        "flaky 실패, 네트워크 지연 등)했을 때, 배포는 그대로 두고 실패한 테스트만 "
        "재시도하고 싶을 때 사용합니다. `FAILED`(배포까지는 완료됐고 테스트가 실패한 "
        "상태)에서만 가능합니다. 새 Run 을 만들지 않고 같은 Run 을 갱신해 200 으로 "
        "반환합니다."
    ),
    parameters=[_RUN_ID_PATH_PARAM],
    request=None,
    responses={
        200: OpenApiResponse(
            response=QualityGateRunDetailSerializer,
            description="테스트 재실행이 반영된 Run 상세 정보.",
        ),
        404: _RUN_NOT_FOUND,
        409: OpenApiResponse(
            description=(
                "`cannot_rerun_test` — 배포가 끝나지 않았거나 테스트 실패 상태가 "
                "아니라서 테스트만 재실행할 수 없습니다. 응답의 `details.status` 로 "
                "현재 상태를 확인하세요."
            ),
        ),
    },
)
@_common_decorators(S.RUNS_CONTROL)
def rerun_test(request, run_id):
    run = _get_run(run_id)
    qg_services.rerun_test(run=run, user=request.user)
    return _detail_response(run, request=request)


@extend_schema(
    tags=_TAGS,
    summary="검수대상 배포만 재실행",
    description=(
        "검수대상 환경(개발/스테이징) 배포만 다시 시도합니다. 이미지 빌드나 테스트 "
        "단계는 건드리지 않습니다.\n"
        "\n"
        "**언제 쓰나요**: 이미지 빌드까지는 성공했는데 배포 단계에서만 실패(예: "
        "일시적인 배포 서버 오류)했을 때, 배포만 다시 시도하고 싶을 때 사용합니다. "
        "`DEPLOY_FAILED`(검수대상 배포 실패) 상태에서만 가능합니다."
    ),
    parameters=[_RUN_ID_PATH_PARAM],
    request=_PARAMETERS_REQUEST,
    examples=_PARAMETERS_REQUEST_EXAMPLES,
    responses={
        200: OpenApiResponse(
            response=QualityGateRunDetailSerializer,
            description="배포 재실행이 반영된 Run 상세 정보.",
        ),
        400: OpenApiResponse(
            description=(
                "`invalid_app_image` — 배포에 쓸 이미지 태그(`APP_IMAGE` 파라미터) "
                "가 비어있거나 태그 부분이 빠져 있습니다. 올바른 `이미지:태그` 값을 "
                "지정해 주세요."
            ),
        ),
        404: _RUN_NOT_FOUND,
        409: OpenApiResponse(
            description=(
                "`run_not_actionable` — 지금은 `DEPLOY_FAILED` 상태가 아니라서 "
                "배포만 재실행할 수 없습니다. 응답의 `details.current_status` 로 "
                "현재 상태를 확인하세요."
            ),
        ),
        502: OpenApiResponse(
            description=(
                "`jenkins_trigger_failed` — Jenkins 에 배포 Job 실행을 요청했지만 "
                "실패했습니다 (Jenkins 연결/설정 문제일 수 있습니다). 잠시 후 다시 "
                "시도하거나 게이트 설정(Job 경로/자격증명)을 확인하세요."
            ),
        ),
    },
)
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


@extend_schema(
    tags=_TAGS,
    summary="검수통과 배포만 재실행",
    description=(
        "검수통과 환경(운영/상용) 배포만 다시 시도합니다. 이미지 빌드·테스트·"
        "검수대상 배포는 이미 끝난 상태이므로 건드리지 않습니다.\n"
        "\n"
        "**언제 쓰나요**: 테스트까지는 모두 통과했는데 운영 반영(prod 배포) 단계에서만 "
        "실패했을 때, 그 배포만 다시 시도하고 싶을 때 사용합니다. "
        "`PROD_DEPLOY_FAILED`(검수통과 배포 실패) 상태에서만 가능합니다."
    ),
    parameters=[_RUN_ID_PATH_PARAM],
    request=_PARAMETERS_REQUEST,
    examples=_PARAMETERS_REQUEST_EXAMPLES,
    responses={
        200: OpenApiResponse(
            response=QualityGateRunDetailSerializer,
            description="배포 재실행이 반영된 Run 상세 정보.",
        ),
        400: OpenApiResponse(
            description=(
                "`invalid_app_image` — 배포에 쓸 이미지 태그(`APP_IMAGE` 파라미터) "
                "가 비어있거나 태그 부분이 빠져 있습니다. 올바른 `이미지:태그` 값을 "
                "지정해 주세요."
            ),
        ),
        404: _RUN_NOT_FOUND,
        409: OpenApiResponse(
            description=(
                "`run_not_actionable` — 지금은 `PROD_DEPLOY_FAILED` 상태가 아니라서 "
                "배포만 재실행할 수 없습니다. 응답의 `details.current_status` 로 "
                "현재 상태를 확인하세요."
            ),
        ),
        502: OpenApiResponse(
            description=(
                "`jenkins_trigger_failed` — Jenkins 에 배포 Job 실행을 요청했지만 "
                "실패했습니다 (Jenkins 연결/설정 문제일 수 있습니다). 잠시 후 다시 "
                "시도하거나 게이트 설정(Job 경로/자격증명)을 확인하세요."
            ),
        ),
    },
)
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


@extend_schema(
    tags=_TAGS,
    summary="검수대상 환경 수동 배포 트리거",
    description=(
        "사람이 배포 시점을 직접 정하는 게이트(`auto_mode`=off, 자동 진행이 꺼진 "
        "게이트)에서, 검수대상 환경 배포를 사람이 직접 시작시킵니다.\n"
        "\n"
        "**언제 쓰나요**: 이미지 빌드까지 끝나고 배포 승인 대기 중인 "
        "`WAITING_DEPLOY`(검수대상 배포 대기) 상태일 때, 담당자가 확인 후 이 API 로 "
        "배포를 진행시킵니다. `auto_mode` 가 켜져 있는 게이트는 이 단계가 자동으로 "
        "진행되므로 이 API 를 호출할 필요가 없습니다."
    ),
    parameters=[_RUN_ID_PATH_PARAM],
    request=_PARAMETERS_REQUEST,
    examples=_PARAMETERS_REQUEST_EXAMPLES,
    responses={
        200: OpenApiResponse(
            response=QualityGateRunDetailSerializer,
            description="배포 트리거가 반영된 Run 상세 정보.",
        ),
        400: OpenApiResponse(
            description=(
                "`invalid_app_image` — 배포에 쓸 이미지 태그(`APP_IMAGE` 파라미터) "
                "가 비어있거나 태그 부분이 빠져 있습니다. 올바른 `이미지:태그` 값을 "
                "지정해 주세요."
            ),
        ),
        404: _RUN_NOT_FOUND,
        409: OpenApiResponse(
            description=(
                "`run_not_actionable` — 지금은 `WAITING_DEPLOY`(검수대상 배포 대기) "
                "상태가 아니라서 트리거할 수 없습니다. 응답의 "
                "`details.current_status` 로 현재 상태를 확인하세요."
            ),
        ),
        502: OpenApiResponse(
            description=(
                "`jenkins_trigger_failed` — Jenkins 에 배포 Job 실행을 요청했지만 "
                "실패했습니다 (Jenkins 연결/설정 문제일 수 있습니다). 잠시 후 다시 "
                "시도하거나 게이트 설정(Job 경로/자격증명)을 확인하세요."
            ),
        ),
    },
)
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


@extend_schema(
    tags=_TAGS,
    summary="검수통과 환경 수동 배포 트리거",
    description=(
        "테스트를 모두 통과해 검수(Quality Gate)를 통과한 Run 을, 실제 운영(prod) "
        "환경에 반영할지 사람이 최종 승인하는 API 입니다.\n"
        "\n"
        "**언제 쓰나요**: 테스트가 모두 통과해 `QG_PASSED`(검수 통과, 운영 반영 승인 "
        "대기) 상태가 된 뒤, 담당자가 운영 반영을 최종 확인·승인할 때 호출합니다. "
        "이 게이트에 검수통과 배포 자체가 설정돼 있지 않으면(운영 반영 단계가 없는 "
        "게이트) 테스트 통과 즉시 `completed` 로 끝나므로 이 상태에 도달하지 "
        "않습니다."
    ),
    parameters=[_RUN_ID_PATH_PARAM],
    request=_PARAMETERS_REQUEST,
    examples=_PARAMETERS_REQUEST_EXAMPLES,
    responses={
        200: OpenApiResponse(
            response=QualityGateRunDetailSerializer,
            description="배포 트리거가 반영된 Run 상세 정보.",
        ),
        400: OpenApiResponse(
            description=(
                "`invalid_app_image` — 배포에 쓸 이미지 태그(`APP_IMAGE` 파라미터) "
                "가 비어있거나 태그 부분이 빠져 있습니다. 올바른 `이미지:태그` 값을 "
                "지정해 주세요."
            ),
        ),
        404: _RUN_NOT_FOUND,
        409: OpenApiResponse(
            description=(
                "`run_not_actionable` — 지금은 `QG_PASSED`(검수 통과, 승인 대기) "
                "상태가 아니라서 트리거할 수 없습니다 (`details.current_status` 로 "
                "현재 상태 확인). `invalid_state` — 이 게이트에는 검수통과 배포 "
                "자체가 설정돼 있지 않습니다 (`details.has_prod_deploy`)."
            ),
        ),
        502: OpenApiResponse(
            description=(
                "`jenkins_trigger_failed` — Jenkins 에 배포 Job 실행을 요청했지만 "
                "실패했습니다 (Jenkins 연결/설정 문제일 수 있습니다). 잠시 후 다시 "
                "시도하거나 게이트 설정(Job 경로/자격증명)을 확인하세요."
            ),
        ),
    },
)
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


@extend_schema(
    tags=_TAGS,
    summary="Argo CD sync 재시도",
    description=(
        "지정한 단계의 Argo CD(쿠버네티스 배포 상태를 자동으로 동기화해 주는 도구) "
        "동기화(sync) 상태를 리셋하고 다시 시도합니다.\n"
        "\n"
        "**언제 쓰나요**: Jenkins 배포는 끝났는데 Argo CD 동기화가 실패하거나 멈춰서 "
        "최종 배포 상태가 반영되지 않을 때, 동기화만 다시 시도하고 싶을 때 "
        "사용합니다. 이 API 는 재시도를 접수만 하고 바로 202(접수됨)로 응답합니다 — "
        "실제로 동기화가 끝났는지는 이후 `GET /runs/{id}/` 등으로 다시 조회(폴링)해서 "
        "확인하세요."
    ),
    parameters=[
        _RUN_ID_PATH_PARAM,
        OpenApiParameter(
            name="stage",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.PATH,
            required=True,
            enum=["deploy", "prod"],
            description=(
                "(필수) 재시도할 단계입니다 — API 응답에서 얻는 값이 아니라 "
                "아래 중 하나를 직접 선택해 넣습니다. `deploy`(검수대상 환경 "
                "배포) 또는 `prod`(검수통과 환경 배포). 예: `deploy`."
            ),
        ),
    ],
    request=None,
    responses={
        202: OpenApiResponse(
            response=QualityGateRunDetailSerializer,
            description="재시도 요청이 접수된 Run 상세 정보 (동기화가 실제로 끝났다는 뜻은 아닙니다).",
        ),
        400: OpenApiResponse(
            description="`invalid_parameters` — stage 가 `deploy`/`prod` 중 하나가 아닙니다."
        ),
        404: _RUN_NOT_FOUND,
    },
)
@_common_decorators(S.RUNS_CONTROL)
def argocd_retry(request, run_id, stage):
    if stage not in ("deploy", "prod"):
        raise ValidationError({"stage": "stage must be 'deploy' or 'prod'"})
    run = _get_run(run_id)
    qg_services.argocd_retry(run=run, stage=stage, user=request.user)
    return _detail_response(run, status_code=status.HTTP_202_ACCEPTED, request=request)
