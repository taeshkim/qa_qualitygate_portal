"""apps/api_v2/views/suites_v2.py — 단독 Test Suite Run API (Phase F-2..3).

PRD: prd_api.md §6.4 (트리거), §5 (URL 라우팅), §6.6 (콘솔 로그).

본 파일은 Phase C 의 ``views/meta.py`` (Suite 메타 조회) 와 별도이며, Run/Cancel/Console
만 책임진다. 비즈니스 로직은 ``apps.suites.services`` 의 ``trigger_suite_run`` /
``cancel_suite_run`` 을 그대로 호출하고, 폴링은 ``apps.suites.views._poll_run_status``
를 lazy import 로 호출한다.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.suites.models import TestSuite, TestSuiteJobRun, TestSuiteRun
from apps.suites.services import SuiteRunError, cancel_suite_run, trigger_suite_run

from .. import scopes as _scopes
from ..authentication import ApiKeyAuthentication
from ..permissions import CanWriteForControl, HasScope
from ..serializers.suites import (
    TestSuiteJobRunSerializer,
    TestSuiteRunDetailSerializer,
    TestSuiteRunSummarySerializer,
    _suite_run_detail_url,
    _suite_run_self_url,
)
from ..throttling import ReadThrottle, WriteThrottle

# 1MB. console 응답에서 ``tail`` 파라미터가 없고 전체 크기가 이를 넘으면 자동으로
# 마지막 50KB 만 잘라 반환한다. (PRD §6.6 가이드 + 본 Phase 의 요구사항.)
_CONSOLE_AUTO_TRUNCATE_BYTES = 1_000_000
_CONSOLE_AUTO_TAIL_BYTES = 50_000

# drf-spectacular 문서 전용 — 이 아래 상수/데코레이터는 API 동작에 영향을 주지 않는다.
_TAG = "Test Suite Runs"

# `GET /suite-runs/<id>/` 및 `.../status/` 응답에 공통으로 붙는 폴링 힌트 헤더.
_POLL_AFTER_HEADER_PARAM = OpenApiParameter(
    name="X-QGP-Poll-After",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.HEADER,
    response=True,
    description="다음 폴링까지 대기할 초. 진행 중이면 `5`, 종료(`is_terminal=true`)면 `0`.",
)

# `GET/POST /suites/<id>/runs/` 의 경로 파라미터 — 대상 Suite 의 id.
_SUITE_ID_PATH_PARAM = OpenApiParameter(
    name="id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
    required=True,
    description=(
        "(필수) 대상 Suite(테스트 묶음)의 id. `GET /api/v2/suites/` 응답 목록 각 항목의 "
        "`id` 값을 사용합니다."
    ),
)

# `GET/POST /suite-runs/<id>/...` 의 경로 파라미터 — 대상 Suite Run 의 id.
_SUITE_RUN_ID_PATH_PARAM = OpenApiParameter(
    name="id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
    required=True,
    description=(
        "(필수) 대상 Suite Run(실행)의 id. `POST /api/v2/suites/{id}/runs/` 트리거 응답의 "
        "`suite_run_id`, 또는 `GET /api/v2/suites/{id}/runs/` 목록 응답 각 항목의 `id` "
        "값입니다."
    ),
)

# `GET /suite-job-runs/<id>/console/` 의 경로 파라미터 — 대상 Job Run 의 id.
_JOB_RUN_ID_PATH_PARAM = OpenApiParameter(
    name="id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
    required=True,
    description=(
        "(필수) 콘솔 로그를 조회할 Job Run(스위트 안 개별 Job 의 실행 기록) 의 id. Suite Run "
        "이 아니라 그 안의 Job 실행 단위입니다 — `GET /api/v2/suite-runs/{id}/` 응답의 "
        "`job_runs[].id` 값을 사용합니다."
    ),
)


# ----------------------------------------------------------------------------
# 공통 helper
# ----------------------------------------------------------------------------

def _service_error_response(exc: SuiteRunError) -> Response:
    body = {"error": {"code": exc.code, "message": str(exc)}}
    if exc.details:
        body["error"]["details"] = exc.details
    return Response(body, status=exc.http_status)


def _coerce_bool(raw) -> bool:
    """body 의 truthy 표현을 bool 로. (true/1/yes/on, 대소문자 무관)"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return False


def _parse_positive_int(raw, default=None):
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return value


def _tail_lines(text: str, n: int) -> str:
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    return "".join(lines[-n:])


# ----------------------------------------------------------------------------
# GET  /api/v2/suites/<suite_id>/runs/
# POST /api/v2/suites/<suite_id>/runs/
# ----------------------------------------------------------------------------

def _list_runs(request, suite: TestSuite) -> Response:
    qs = (
        suite.runs
        .select_related("suite", "triggered_by")
        .prefetch_related("job_runs__job")
        .order_by("-started_at")
    )

    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    since = request.query_params.get("since")
    if since:
        qs = qs.filter(started_at__gte=since)

    limit = _parse_positive_int(request.query_params.get("limit"), default=50)
    if limit > 200:
        limit = 200
    runs = list(qs[:limit])

    data = TestSuiteRunSummarySerializer(runs, many=True).data
    return Response({"count": len(data), "results": data})


def _trigger_run(request, suite: TestSuite) -> Response:
    # 트리거 모드 (trigger_suite_run 이 분기):
    #   - job_id 지정          → 그 Job(worker) 1개만 실행 (하위호환)
    #   - job_id 생략 (기본)   → 활성 대상 Job 중 '실행 중이지 않은' worker 1개만
    #   - run_all=true         → 활성 대상 Job 전체 순차 체인
    body = request.data if isinstance(request.data, dict) else {}
    raw_job_id = body.get("job_id")
    if isinstance(raw_job_id, str):
        job_id = raw_job_id.strip()
    elif raw_job_id is None:
        job_id = ""
    else:
        job_id = str(raw_job_id)

    run_all = _coerce_bool(body.get("run_all"))

    try:
        suite_run = trigger_suite_run(
            suite=suite, user=request.user, job_id=job_id, run_all=run_all,
        )
    except SuiteRunError as e:
        return _service_error_response(e)
    except (Http404, DjangoValidationError, ValueError):
        err_body = {
            "error": {
                "code": "invalid_parameters",
                "message": "job_id is not a job of this suite.",
                "details": {"job_id": str(job_id)},
            }
        }
        return Response(err_body, status=status.HTTP_400_BAD_REQUEST)

    payload = {
        "suite_run_id": str(suite_run.pk),
        "suite_id": str(suite.pk),
        "seq_no": suite_run.seq_no,
        "status": suite_run.status,
        "total_jobs": suite_run.total_jobs,
        "self_url": _suite_run_self_url(suite_run.pk),
        "detail_url": _suite_run_detail_url(suite_run.pk),
    }
    return Response(payload, status=status.HTTP_201_CREATED)


@extend_schema(
    methods=["GET"],
    summary="Suite Run 목록 조회 (이 스위트의 실행 이력 보기)",
    description=(
        "이 테스트 묶음(Suite)이 지금까지 실행된 기록(Run)들을 목록으로 조회합니다.\n\n"
        "**언제 쓰나요** — 과거에 실행했던 Run 들의 이력을 훑어보거나, 최근 몇 건의 실행 "
        "결과를 한 번에 확인하고 싶을 때 사용합니다. 특정 Run 하나의 상세 결과가 필요하면 "
        "`GET /api/v2/suite-runs/{id}/` 를 대신 사용해 주세요.\n\n"
        "`status`/`since`/`limit` 쿼리 파라미터로 원하는 조건만 골라 볼 수 있습니다. 응답은 "
        "`count`(조건에 맞는 전체 건수 — `limit` 과 무관하게 실제 반환된 건수)와 `results`"
        "(Run 요약 목록, 최신 시작 순)로 구성됩니다."
    ),
    tags=[_TAG],
    parameters=[
        _SUITE_ID_PATH_PARAM,
        OpenApiParameter(
            name="status",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            enum=["pending", "running", "completed"],
            description=(
                "(선택) 이 상태인 Run 만 골라서 보여줍니다 — 예: `status=running` "
                "(`pending`=대기 중·아직 시작 전 / `running`=진행 중 / `completed`=종료됨, "
                "성공·실패 모두 포함). 생략하면 전체."
            ),
        ),
        OpenApiParameter(
            name="since",
            type=OpenApiTypes.DATETIME,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 이 시각 이후에 시작된 Run 만 보여줍니다 — 예: "
                "`since=2026-07-01T00:00:00Z` (이 시각 이후 시작분만). "
                "\"최근 하루치만 보고 싶다\" 같은 경우에 씁니다. 생략하면 기간 제한 없음."
            ),
        ),
        OpenApiParameter(
            name="limit",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 한 번에 몇 건까지 받을지 지정합니다 — 예: `limit=10` (최근 10건만). "
                "기본값 50, 최대 200 — 200 보다 크게 지정해도 200 건까지만 반환됩니다."
            ),
        ),
    ],
    responses={
        200: inline_serializer(
            name="SuiteRunListResponse",
            fields={
                "count": serializers.IntegerField(),
                "results": TestSuiteRunSummarySerializer(many=True),
            },
        ),
    },
)
@extend_schema(
    methods=["POST"],
    summary="Suite Run 트리거 (테스트 묶음 실행 시작)",
    description=(
        "테스트 묶음(Suite) 하나를 실제로 실행시킵니다 — 이 API 를 호출하는 순간 Jenkins 에 "
        "테스트 빌드가 트리거됩니다.\n\n"
        "**언제 쓰나요** — 코드를 배포한 뒤 자동으로 회귀 테스트를 돌리고 싶을 때, 또는 "
        "외부 스케줄러/CI 파이프라인에서 정기적으로 테스트를 실행시키고 싶을 때 이 API 를 "
        "호출합니다.\n\n"
        "**보내는 것(body)** — 아래 3가지 모드 중 하나로 요청 body 를 구성합니다. 여기서 "
        "말하는 Job(잡, worker) 은 스위트 안에 등록된 개별 테스트 실행 단위이며, 스위트 "
        "하나에 여러 개가 있을 수 있습니다.\n\n"
        "| 모드 | 보낼 body | 무슨 일이 일어나나요 |\n"
        "|---|---|---|\n"
        "| **기본** (자동 선택) | body 없음 또는 `{}` (`job_id` 생략) | 실행 대상으로 설정된 "
        "Job 들 중 Jenkins 에서 지금 놀고 있는 것으로 확인된(idle) worker **1개만** 골라 "
        "실행합니다. 놀고 있는 worker 가 하나도 확인되지 않으면 아무것도 실행하지 않고 "
        "`409 all_workers_busy` 를 돌려줍니다.\n"
        "| **특정 Job 지정** | `{\"job_id\": \"<uuid>\"}` | 지정한 Job **1개만** 실행합니다 "
        "(`total_jobs=1`, 예전부터 있던 호출 방식과 호환). 지정한 `job_id` 가 이 스위트 소속이 "
        "아니면 `400 invalid_parameters`.\n"
        "| **전체 순차 실행** | `{\"run_all\": true}` | 실행 대상으로 설정된 Job **전체**를 "
        "등록 순서대로 하나씩 차례로 실행합니다 (한 Job 이 끝나야 다음 Job 이 자동으로 "
        "시작 — 포털 화면에서 '전체 실행' 눌렀을 때와 동일한 동작). 이미 진행 중인 Run 이 "
        "있으면 중복 실행하지 않고 `409 run_already_active`.\n\n"
        "모드와 무관하게 이 스위트에 실행 대상 Job 이 하나도 없으면 `409 no_active_jobs` 가 "
        "돌아옵니다.\n\n"
        "**받는 것 / 이후 흐름** — 3가지 모드 모두 성공하면 동일한 형태로 201 을 응답합니다. "
        "다음 순서로 진행하세요:\n"
        "1. 이 API 응답의 `suite_run_id`(이번 실행 번호표)를 기억해 둡니다.\n"
        "2. `GET /api/v2/suite-runs/{suite_run_id}/status/` 를 몇 초 간격으로 반복 호출"
        "(폴링 — 끝났는지 반복해서 확인하는 방식)합니다.\n"
        "3. 응답의 `is_terminal` 이 `true` 가 되거나 `result` 가 `passed`(통과)/`failed`"
        "(실패)로 바뀌면 종료된 것입니다 — 더 이상 폴링하지 않아도 됩니다.\n\n"
        "**에러가 나면** — `error.code` 로 원인과 대처를 확인하세요.\n"
        "- `409 all_workers_busy` : 지금 놀고 있는 Job 이 하나도 없어 실행을 시작하지 "
        "못했습니다(Run 자체가 생성되지 않음). 잠시 후 다시 시도하거나, 실행할 Job 을 "
        "알고 있다면 `job_id` 를 직접 지정해 보세요.\n"
        "- `409 no_active_jobs` : 이 스위트에 실행 대상으로 설정된 Job 이 하나도 없습니다. "
        "스위트의 Job 설정(활성 여부/실행 대상 여부)을 확인해 주세요.\n"
        "- `409 run_already_active` : (`run_all=true` 모드에서만) 이미 진행 중인 Run 이 있어 "
        "새로 시작할 수 없습니다. 기존 Run 이 끝난 뒤 다시 시도해 주세요.\n"
        "- `400 invalid_parameters` : 보낸 `job_id` 가 이 스위트에 속한 Job 이 아닙니다. "
        "`GET /api/v2/suites/{suite_id}/` 로 올바른 job id 를 다시 확인해 주세요."
    ),
    tags=[_TAG],
    parameters=[_SUITE_ID_PATH_PARAM],
    request=inline_serializer(
        name="SuiteRunTriggerRequest",
        fields={
            "job_id": serializers.CharField(
                required=False,
                allow_blank=True,
                help_text=(
                    "(선택) 특정 Job(잡, 스위트 안의 개별 테스트 실행 단위) 1개만 콕 집어 "
                    "실행하고 싶을 때 그 Job 의 UUID — 예: "
                    "`3f6b1e9e-1234-4a9b-9a11-abcdef012345`. 생략(또는 빈 문자열)하면 놀고 "
                    "있는(idle) worker 1개를 자동으로 골라 실행하는 기본 모드로 동작합니다."
                ),
            ),
            "run_all": serializers.BooleanField(
                required=False,
                default=False,
                help_text=(
                    "(선택) true 로 보내면 실행 대상 Job 전체를 등록 순서대로 하나씩 순차 "
                    "실행합니다 — 예: `\"run_all\": true`. 생략하면 기본값 false 로 동작합니다."
                ),
            ),
        },
    ),
    responses={
        201: inline_serializer(
            name="SuiteRunTriggerResponse",
            fields={
                "suite_run_id": serializers.CharField(),
                "suite_id": serializers.CharField(),
                "seq_no": serializers.IntegerField(),
                "status": serializers.CharField(),
                "total_jobs": serializers.IntegerField(),
                "self_url": serializers.CharField(),
                "detail_url": serializers.CharField(),
            },
        ),
        400: OpenApiResponse(
            description=(
                "`invalid_parameters` — 보낸 `job_id` 가 이 스위트에 속한 Job 이 아닙니다 "
                "(`details.job_id` 에 문제가 된 값이 담겨 옵니다). 올바른 job id 를 다시 "
                "확인해 재시도해 주세요."
            ),
        ),
        409: OpenApiResponse(
            description=(
                "지금 상태에서는 실행을 시작할 수 없는 3가지 경우입니다 — "
                "`all_workers_busy`(기본 모드에서 놀고 있는 Job 이 확인되지 않음, Run 은 "
                "생성되지 않으며 `details.candidate_jobs` 에 후보 목록이 담겨 옵니다) / "
                "`no_active_jobs`(이 스위트에 실행 대상 Job 이 하나도 없음) / "
                "`run_already_active`(`run_all=true` 인데 이미 진행 중인 Run 이 있음, "
                "`details.suite_run_id` 에 그 Run 의 id 가 담겨 옵니다)."
            ),
        ),
    },
    examples=[
        OpenApiExample(
            "기본 — 놀고 있는(idle) worker 1개 자동 선택",
            value={},
            request_only=True,
        ),
        OpenApiExample(
            "특정 Job 지정 (하위호환)",
            value={"job_id": "3f6b1e9e-1234-4a9b-9a11-abcdef012345"},
            request_only=True,
        ),
        OpenApiExample(
            "run_all — 실행 대상 Job 전체 순차 실행",
            value={"run_all": True},
            request_only=True,
        ),
        OpenApiExample(
            "트리거 성공",
            value={
                "suite_run_id": "b6b61e9e-1234-4a9b-9a11-abcdef012345",
                "suite_id": "a1a11e9e-1234-4a9b-9a11-abcdef012345",
                "seq_no": 12,
                "status": "running",
                "total_jobs": 1,
                "self_url": "/api/v2/suite-runs/b6b61e9e-1234-4a9b-9a11-abcdef012345/",
                "detail_url": "/suites/runs/b6b61e9e-1234-4a9b-9a11-abcdef012345/",
            },
            response_only=True,
            status_codes=["201"],
        ),
        OpenApiExample(
            "409 — 놀고 있는(idle) worker 없음",
            value={
                "error": {
                    "code": "all_workers_busy",
                    "message": (
                        "No idle worker available (all busy or Jenkins state "
                        "unverifiable). Retry later or pass job_id."
                    ),
                    "details": {"candidate_jobs": ["3f6b1e9e-1234-4a9b-9a11-abcdef012345"]},
                }
            },
            response_only=True,
            status_codes=["409"],
        ),
        OpenApiExample(
            "409 — run_all 인데 이미 진행 중",
            value={
                "error": {
                    "code": "run_already_active",
                    "message": "A run is already active for this suite.",
                    "details": {"suite_run_id": "b6b61e9e-1234-4a9b-9a11-abcdef012345"},
                }
            },
            response_only=True,
            status_codes=["409"],
        ),
    ],
)
@api_view(["GET", "POST"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope, CanWriteForControl])
def suite_run_collection(request, pk):
    """GET=목록 / POST=트리거 — method 별 scope/throttle 을 inline 으로 분기."""
    # method-specific throttle 을 직접 검사: DRF 데코레이터는 클래스 1개만 받으니
    # 우리가 직접 wrap. (api_v2.throttling.*Throttle 은 SimpleRateThrottle 기반.)
    method = request.method.upper()
    if method == "POST":
        throttle = WriteThrottle()
    else:
        throttle = ReadThrottle()
    if not throttle.allow_request(request, suite_run_collection):
        from rest_framework.exceptions import Throttled
        raise Throttled(wait=throttle.wait())

    suite = get_object_or_404(TestSuite, pk=pk)
    if method == "GET":
        return _list_runs(request, suite)
    return _trigger_run(request, suite)


# method 별 required_scope. HasScope 가 dict 면 method 별 적용.
# `@api_view` 가 만든 클래스 (`.cls`) 에 attribute 를 붙여야 permission 검사 시
# `getattr(view, "required_scopes", None)` 가 잡힌다.
suite_run_collection.required_scopes = {
    "GET": _scopes.RUNS_READ,
    "POST": _scopes.RUNS_TRIGGER,
}
suite_run_collection.cls.required_scopes = suite_run_collection.required_scopes


# ----------------------------------------------------------------------------
# GET  /api/v2/suite-runs/<id>/
# POST /api/v2/suite-runs/<id>/cancel/
# ----------------------------------------------------------------------------

def _load_suite_run(pk) -> TestSuiteRun:
    return get_object_or_404(
        TestSuiteRun.objects
        .select_related("suite", "triggered_by")
        .prefetch_related(
            Prefetch(
                "job_runs",
                queryset=TestSuiteJobRun.objects.select_related("job"),
            )
        ),
        pk=pk,
    )


@extend_schema(
    summary="Suite Run 단건 상세 조회 (Job 별 결과 포함)",
    description=(
        "Run(실행) 하나의 상세 정보를 조회합니다 — 스위트 안의 각 Job(잡)이 어떻게 "
        "실행됐는지까지 `job_runs` 목록으로 함께 보여줍니다.\n\n"
        "**언제 쓰나요** — 실행 결과를 자세히(Job 별 상태, 콘솔 로그 URL 등) 확인하고 싶을 "
        "때 사용합니다. 단순히 \"끝났는지, 합/불인지\"만 자주 확인하려면 응답이 더 가벼운 "
        "`GET /api/v2/suite-runs/{id}/status/` 를 대신 쓰는 것을 권장합니다.\n\n"
        "아직 끝나지 않은(`status != completed`) Run 이면, 이 API 를 호출하는 시점에 내부적으로 "
        "Jenkins 최신 진행 상태를 확인해 업데이트한 뒤 응답합니다 — 즉 외부 자동화가 이 "
        "엔드포인트를 적당한 간격으로 반복 호출(폴링)만 해도 파이프라인 진행이 자동으로 "
        "이어집니다. Jenkins 호출이 실패해도 조용히 무시하고 응답은 정상적으로 돌려줍니다."
    ),
    tags=[_TAG],
    parameters=[_SUITE_RUN_ID_PATH_PARAM, _POLL_AFTER_HEADER_PARAM],
    responses={200: TestSuiteRunDetailSerializer},
)
@api_view(["GET"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope])
@throttle_classes([ReadThrottle])
def suite_run_detail(request, pk):
    """Suite Run 단건 + 폴링 트리거. PRD §6.4."""
    suite_run = _load_suite_run(pk)

    if suite_run.status != TestSuiteRun.Status.COMPLETED:
        # 순환 import 회피 + 본 Phase 가 _poll_run_status 자체는 건드리지 못함.
        from apps.suites.views import _poll_run_status

        try:
            _poll_run_status(suite_run)
        except Exception:  # Jenkins 외부 호출 실패는 응답을 막지 않는다.
            pass
        suite_run.refresh_from_db()
        # prefetch 갱신을 위해 재조회.
        suite_run = _load_suite_run(pk)

    data = TestSuiteRunDetailSerializer(suite_run).data
    response = Response(data)
    # 외부 폴링 힌트 — COMPLETED 도달 시 0 (stop), 그 외 5 초.
    response["X-QGP-Poll-After"] = (
        "0" if suite_run.status == TestSuiteRun.Status.COMPLETED else "5"
    )
    return response


suite_run_detail.required_scope = _scopes.RUNS_READ
suite_run_detail.cls.required_scope = _scopes.RUNS_READ


# =============================================
# GET /api/v2/suite-runs/<pk>/status/  — 가벼운 폴링용
# =============================================


@extend_schema(
    summary="Suite Run 가벼운 상태 조회 (폴링 전용, 합/불 즉시 확인)",
    description=(
        "Run(실행) 하나가 **끝났는지, 끝났다면 합격(pass)인지 불합격(fail)인지**를 가볍고 "
        "빠르게 확인하는 전용 엔드포인트입니다.\n\n"
        "**언제 쓰나요** — Run 을 트리거한 뒤 완료될 때까지 반복 조회(폴링)할 때는 이 "
        "엔드포인트를 쓰세요. `suite_run_detail`(Job 별 상세) 보다 응답이 훨씬 가벼워 짧은 "
        "간격으로 자주 호출해도 부담이 적습니다.\n\n"
        "**받는 것** — `result` 필드 하나로 합/불을 바로 읽을 수 있습니다: 아직 진행 중"
        "(`is_terminal=false`)이면 `null`, 종료 후에는 실패한 Job 이 하나라도 있으면 "
        "`\"failed\"`, 하나도 없으면 `\"passed\"` 입니다.\n\n"
        "**주의 — 재시도(`retry_count`) 설정이 있는 Suite** : 실패해도 곧바로 종료가 아닐 수 "
        "있습니다. Suite 에 `retry_count > 1` 이 설정돼 있으면, 설정된 횟수를 모두 실패해야 "
        "비로소 `is_terminal=true` 로 바뀝니다 — 그 전까지는 중간에 실패가 있어도 "
        "`status=running`/`result=null` 상태가 유지됩니다(자동으로 재시도가 이어지는 중). "
        "그래서 폴링하는 쪽은 반드시 **`is_terminal`(또는 `result != null`) 값으로만 종료 "
        "여부를 판정**해야 하며, 중간 `status` 값만 보고 끝났다고 단정하면 안 됩니다.\n\n"
        "호출 시 아직 끝나지 않은 Run 이면 `suite_run_detail` 과 마찬가지로 내부적으로 "
        "Jenkins 최신 진행 상태를 확인해 업데이트한 뒤 응답합니다."
    ),
    tags=[_TAG],
    parameters=[_SUITE_RUN_ID_PATH_PARAM, _POLL_AFTER_HEADER_PARAM],
    responses={
        200: inline_serializer(
            name="SuiteRunStatusResponse",
            fields={
                "suite_run_id": serializers.CharField(
                    help_text="이 Run(실행)의 고유 id.",
                ),
                "seq_no": serializers.IntegerField(
                    help_text="이 스위트 안에서 몇 번째 실행인지 나타내는 순번.",
                ),
                "status": serializers.CharField(
                    help_text="Run 의 현재 상태(pending/running/completed).",
                ),
                "phase": serializers.CharField(
                    help_text="지금 파이프라인이 어느 단계(phase)를 지나고 있는지.",
                ),
                "result": serializers.ChoiceField(
                    choices=["passed", "failed"], allow_null=True,
                    help_text=(
                        "합/불 최종 판정. 진행 중이면 null, 종료되면 passed(통과) 또는 "
                        "failed(실패)."
                    ),
                ),
                "is_terminal": serializers.BooleanField(
                    help_text="true 면 Run 이 종료된 것 — 폴링을 멈춰도 됩니다.",
                ),
                "passed_jobs": serializers.IntegerField(
                    help_text="지금까지 통과(passed)로 끝난 Job 개수.",
                ),
                "failed_jobs": serializers.IntegerField(
                    help_text="지금까지 실패(failed)로 끝난 Job 개수.",
                ),
                "total_jobs": serializers.IntegerField(
                    help_text="이번 Run 에서 실행되는 전체 Job 개수.",
                ),
            },
        ),
    },
    examples=[
        OpenApiExample(
            "진행 중",
            value={
                "suite_run_id": "b6b61e9e-1234-4a9b-9a11-abcdef012345",
                "seq_no": 12,
                "status": "running",
                "phase": "running",
                "result": None,
                "is_terminal": False,
                "passed_jobs": 0,
                "failed_jobs": 0,
                "total_jobs": 5,
            },
            response_only=True,
        ),
        OpenApiExample(
            "종료 — 실패 Job 존재",
            value={
                "suite_run_id": "b6b61e9e-1234-4a9b-9a11-abcdef012345",
                "seq_no": 12,
                "status": "completed",
                "phase": "failed",
                "result": "failed",
                "is_terminal": True,
                "passed_jobs": 4,
                "failed_jobs": 1,
                "total_jobs": 5,
            },
            response_only=True,
        ),
    ],
)
@api_view(["GET"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope])
@throttle_classes([ReadThrottle])
def suite_run_status(request, pk):
    """Suite Run 의 status / phase 만 가볍게 반환 — 폴링 비용 최소화."""
    from .. import phase as _phase_mod

    try:
        suite_run = TestSuiteRun.objects.only(
            "pk", "status", "passed_jobs", "failed_jobs", "total_jobs", "seq_no",
        ).get(pk=pk)
    except TestSuiteRun.DoesNotExist:
        from django.http import Http404
        raise Http404("suite_run_not_found")

    if suite_run.status != TestSuiteRun.Status.COMPLETED:
        from apps.suites.views import _poll_run_status

        try:
            _poll_run_status(suite_run)
        except Exception:  # noqa: BLE001
            pass
        suite_run.refresh_from_db()

    is_terminal = suite_run.status == TestSuiteRun.Status.COMPLETED
    body = {
        "suite_run_id": str(suite_run.pk),
        "seq_no": suite_run.seq_no,
        "status": suite_run.status,
        "phase": _phase_mod.phase_for_suite_run(
            suite_run.status,
            passed_jobs=suite_run.passed_jobs or 0,
            failed_jobs=suite_run.failed_jobs or 0,
        ),
        # 외부 시스템이 폴링으로 합/불을 바로 읽는 명시 필드.
        # 종료 전에는 null, 종료 후 "passed" / "failed".
        "result": _phase_mod.result_for_suite_run(
            suite_run.status,
            passed_jobs=suite_run.passed_jobs or 0,
            failed_jobs=suite_run.failed_jobs or 0,
        ),
        "is_terminal": is_terminal,
        "passed_jobs": suite_run.passed_jobs or 0,
        "failed_jobs": suite_run.failed_jobs or 0,
        "total_jobs": suite_run.total_jobs or 0,
    }
    response = Response(body)
    response["X-QGP-Poll-After"] = "0" if is_terminal else "5"
    return response


suite_run_status.required_scope = _scopes.RUNS_READ
suite_run_status.cls.required_scope = _scopes.RUNS_READ


@extend_schema(
    summary="Suite Run 취소 (실행 중인 테스트 중단)",
    description=(
        "진행 중인 Run(실행)을 강제로 중단시킵니다.\n\n"
        "**언제 쓰나요** — 실행이 잘못 트리거됐거나, 너무 오래 걸리거나, 더 이상 결과가 "
        "필요 없어졌을 때 즉시 멈추고 싶으면 호출합니다.\n\n"
        "**무슨 일이 일어나나요** — 아직 실행 중(RUNNING)이거나 대기 중(QUEUED/PENDING, "
        "`waiting` 포함)인 Job 을 모두 ABORTED(강제 중단됨)로 표시하고, Suite Run 자체는 "
        "COMPLETED(종료됨)로 마무리합니다. 이 요청은 body 가 필요 없습니다(빈 POST).\n\n"
        "이미 종료(`completed`)된 Run 을 다시 취소하려 하면 `409 not_actionable` 을 돌려줍니다 "
        "— 이미 끝난 실행은 취소할 대상이 없다는 뜻이니, 다시 실행하고 싶다면 새로 트리거해 "
        "주세요."
    ),
    tags=[_TAG],
    parameters=[_SUITE_RUN_ID_PATH_PARAM],
    request=None,
    responses={
        200: inline_serializer(
            name="SuiteRunCancelResponse",
            fields={
                "suite_run_id": serializers.CharField(),
                "suite_id": serializers.CharField(),
                "status": serializers.CharField(
                    help_text="취소 처리 직후의 상태 — 정상 처리되면 completed 로 반환됩니다.",
                ),
                "self_url": serializers.CharField(),
                "detail_url": serializers.CharField(),
            },
        ),
        409: OpenApiResponse(
            description=(
                "`not_actionable` — 이미 종료(completed)된 Suite Run 을 취소하려고 했습니다 "
                "(`details.status` 에 현재 상태가 담겨 옵니다). 이미 끝난 실행이라 취소할 "
                "필요가 없는 상태입니다."
            ),
        ),
    },
    examples=[
        OpenApiExample(
            "취소 성공",
            value={
                "suite_run_id": "b6b61e9e-1234-4a9b-9a11-abcdef012345",
                "suite_id": "a1a11e9e-1234-4a9b-9a11-abcdef012345",
                "status": "completed",
                "self_url": "/api/v2/suite-runs/b6b61e9e-1234-4a9b-9a11-abcdef012345/",
                "detail_url": "/suites/runs/b6b61e9e-1234-4a9b-9a11-abcdef012345/",
            },
            response_only=True,
            status_codes=["200"],
        ),
        OpenApiExample(
            "409 — 이미 종료된 Run",
            value={
                "error": {
                    "code": "not_actionable",
                    "message": "Suite Run is already completed.",
                    "details": {"status": "completed"},
                }
            },
            response_only=True,
            status_codes=["409"],
        ),
    ],
)
@api_view(["POST"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope, CanWriteForControl])
@throttle_classes([WriteThrottle])
def suite_run_cancel(request, pk):
    """Suite Run 취소."""
    suite_run = _load_suite_run(pk)
    try:
        suite_run = cancel_suite_run(suite_run=suite_run, user=request.user)
    except SuiteRunError as e:
        return _service_error_response(e)

    # 응답: cancel 직후 상태 그대로 (COMPLETED).
    payload = {
        "suite_run_id": str(suite_run.pk),
        "suite_id": str(suite_run.suite_id),
        "status": suite_run.status,
        "self_url": _suite_run_self_url(suite_run.pk),
        "detail_url": _suite_run_detail_url(suite_run.pk),
    }
    return Response(payload)


suite_run_cancel.required_scope = _scopes.RUNS_CONTROL
suite_run_cancel.cls.required_scope = _scopes.RUNS_CONTROL


# ----------------------------------------------------------------------------
# GET /api/v2/suite-job-runs/<id>/console/?tail=N
# ----------------------------------------------------------------------------

@extend_schema(
    summary="Suite Job 콘솔 로그 조회 (실행 결과 상세 확인)",
    description=(
        "Jenkins 가 테스트를 실행하면서 찍은 콘솔 로그(실행 과정이 그대로 출력된 텍스트)를 "
        "그대로 가져옵니다.\n\n"
        "**언제 쓰나요** — 테스트가 실패했을 때 어디서/왜 실패했는지 자세히 들여다보고 싶을 "
        "때 사용합니다. 이 `id` 는 Suite Run 이 아니라 그 안의 개별 Job 실행 기록(job run) "
        "id 이며, `GET /api/v2/suite-runs/{id}/` 응답의 `job_runs[].id` 값에서 얻을 수 "
        "있습니다.\n\n"
        "**받는 것** — `output` 은 실제 로그 텍스트, `length` 는 원본 전체 길이(문자 수), "
        "`truncated` 는 응답이 일부만 잘려서 온 것인지 여부입니다. `tail` 파라미터를 주지 "
        "않았는데 원본이 1MB 를 넘으면 자동으로 마지막 50KB 만 반환하고 `truncated=true` 로 "
        "표시합니다 — 로그가 너무 커서 응답이 느려지는 것을 막기 위한 안전장치입니다."
    ),
    tags=[_TAG],
    parameters=[
        _JOB_RUN_ID_PATH_PARAM,
        OpenApiParameter(
            name="tail",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 로그가 길 때 마지막 N 줄(개행 기준)만 보고 싶으면 지정합니다 — 예: "
                "`tail=200` 이면 마지막 200줄만 반환. 지정하지 않으면 전체(1MB 이하일 때) "
                "또는 자동으로 잘린 마지막 부분을 반환합니다."
            ),
        ),
    ],
    responses={
        200: inline_serializer(
            name="SuiteJobRunConsoleResponse",
            fields={
                "stage": serializers.CharField(
                    help_text="로그 출처 구분값. 현재는 항상 \"suite\" 입니다.",
                ),
                "output": serializers.CharField(
                    help_text="실제 콘솔 로그 텍스트.",
                ),
                "length": serializers.IntegerField(
                    help_text="잘리기 전 원본 로그의 전체 길이(문자 수).",
                ),
                "truncated": serializers.BooleanField(
                    help_text="true 면 원본 전체가 아니라 일부만 잘려서 반환된 것입니다.",
                ),
                "build_url": serializers.CharField(
                    help_text="이 로그에 해당하는 Jenkins 빌드의 콘솔 페이지 URL.",
                ),
            },
        ),
    },
    examples=[
        OpenApiExample(
            "응답 예시",
            value={
                "stage": "suite",
                "output": "...(콘솔 출력)...",
                "length": 12345,
                "truncated": False,
                "build_url": "https://jenkins/.../188/console",
            },
            response_only=True,
        ),
    ],
)
@api_view(["GET"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope])
@throttle_classes([ReadThrottle])
def suite_job_run_console(request, pk):
    job_run = get_object_or_404(
        TestSuiteJobRun.objects.select_related("job", "suite_run", "suite_run__suite"),
        pk=pk,
    )
    raw = job_run.console_output or ""
    full_length = len(raw)
    truncated = False

    tail = _parse_positive_int(request.query_params.get("tail"))
    if tail is not None:
        output = _tail_lines(raw, tail)
        if len(output) < full_length:
            truncated = True
    elif full_length > _CONSOLE_AUTO_TRUNCATE_BYTES:
        # 1MB 초과 → 자동 50KB tail.
        output = raw[-_CONSOLE_AUTO_TAIL_BYTES:]
        truncated = True
    else:
        output = raw

    payload = {
        "stage": "suite",
        "output": output,
        "length": full_length,
        "truncated": truncated,
        "build_url": job_run.jenkins_build_url or "",
    }
    return Response(payload)


suite_job_run_console.required_scope = _scopes.RUNS_READ
suite_job_run_console.cls.required_scope = _scopes.RUNS_READ
