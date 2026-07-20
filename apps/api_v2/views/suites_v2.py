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
from rest_framework import status
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
)
from ..throttling import ReadThrottle, WriteThrottle

# 1MB. console 응답에서 ``tail`` 파라미터가 없고 전체 크기가 이를 넘으면 자동으로
# 마지막 50KB 만 잘라 반환한다. (PRD §6.6 가이드 + 본 Phase 의 요구사항.)
_CONSOLE_AUTO_TRUNCATE_BYTES = 1_000_000
_CONSOLE_AUTO_TAIL_BYTES = 50_000


# ----------------------------------------------------------------------------
# 공통 helper
# ----------------------------------------------------------------------------

def _service_error_response(exc: SuiteRunError) -> Response:
    body = {"error": {"code": exc.code, "message": str(exc)}}
    if exc.details:
        body["error"]["details"] = exc.details
    return Response(body, status=exc.http_status)


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
    body = request.data if isinstance(request.data, dict) else {}
    raw_job_id = body.get("job_id")
    if isinstance(raw_job_id, str):
        job_id = raw_job_id.strip()
    elif raw_job_id is None:
        job_id = ""
    else:
        job_id = str(raw_job_id)

    try:
        suite_run = trigger_suite_run(suite=suite, user=request.user, job_id=job_id)
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
        "status": suite_run.status,
        "self_url": f"/api/v2/suite-runs/{suite_run.pk}/",
        "detail_url": f"/suites/runs/{suite_run.pk}/",
    }
    return Response(payload, status=status.HTTP_201_CREATED)


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


@api_view(["GET"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, HasScope])
@throttle_classes([ReadThrottle])
def suite_run_status(request, pk):
    """Suite Run 의 status / phase 만 가볍게 반환 — 폴링 비용 최소화."""
    from .. import phase as _phase_mod

    try:
        suite_run = TestSuiteRun.objects.only(
            "pk", "status", "passed_jobs", "failed_jobs", "total_jobs",
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
        "status": suite_run.status,
        "phase": _phase_mod.phase_for_suite_run(
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
        "self_url": f"/api/v2/suite-runs/{suite_run.pk}/",
        "detail_url": f"/suites/runs/{suite_run.pk}/",
    }
    return Response(payload)


suite_run_cancel.required_scope = _scopes.RUNS_CONTROL
suite_run_cancel.cls.required_scope = _scopes.RUNS_CONTROL


# ----------------------------------------------------------------------------
# GET /api/v2/suite-job-runs/<id>/console/?tail=N
# ----------------------------------------------------------------------------

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
