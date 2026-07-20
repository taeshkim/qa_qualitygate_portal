"""표준 에러 응답 (PRD §7.2).

{ "error": { "code": "...", "message": "...", "details": {...} } }

세부 매핑은 DRF 기본 예외 + Django Http404 + service layer 의 도메인 예외
(`GateRunError`, `SuiteRunError` 등 — 공통적으로 `code`/`http_status`/`details`
속성을 가짐) 를 모두 정규화한다.
"""

from __future__ import annotations

from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


_KNOWN_DETAIL_CODES = {
    "api_key_invalid": status.HTTP_401_UNAUTHORIZED,
    "api_key_revoked": status.HTTP_401_UNAUTHORIZED,
    "api_key_expired": status.HTTP_401_UNAUTHORIZED,
    "user_inactive": status.HTTP_403_FORBIDDEN,
    "scope_required": status.HTTP_403_FORBIDDEN,
    "role_denied": status.HTTP_403_FORBIDDEN,
    "wildcard_admin_only": status.HTTP_403_FORBIDDEN,
}


def _code_for(exc):
    # 서비스 레이어 예외 — `code`/`http_status` 속성을 가진 non-APIException 은
    # 그대로 사용한다 (GateRunError, SuiteRunError 등).
    custom_code = getattr(exc, "code", None)
    custom_http = getattr(exc, "http_status", None)
    if (
        isinstance(custom_code, str)
        and isinstance(custom_http, int)
        and not isinstance(exc, exceptions.APIException)
    ):
        return custom_code, custom_http

    detail = getattr(exc, "detail", None)
    if isinstance(detail, str) and detail in _KNOWN_DETAIL_CODES:
        return detail, _KNOWN_DETAIL_CODES[detail]
    if isinstance(exc, exceptions.NotAuthenticated):
        return "not_authenticated", status.HTTP_401_UNAUTHORIZED
    if isinstance(exc, exceptions.AuthenticationFailed):
        return "authentication_failed", status.HTTP_401_UNAUTHORIZED
    if isinstance(exc, exceptions.PermissionDenied):
        return "permission_denied", status.HTTP_403_FORBIDDEN
    if isinstance(exc, exceptions.NotFound) or isinstance(exc, Http404):
        return "not_found", status.HTTP_404_NOT_FOUND
    if isinstance(exc, exceptions.MethodNotAllowed):
        return "method_not_allowed", status.HTTP_405_METHOD_NOT_ALLOWED
    if isinstance(exc, exceptions.Throttled):
        return "rate_limited", status.HTTP_429_TOO_MANY_REQUESTS
    if isinstance(exc, exceptions.ValidationError):
        return "invalid_parameters", status.HTTP_400_BAD_REQUEST
    return "internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    code, http_status = _code_for(exc)
    detail = getattr(exc, "detail", None)
    message = None
    details = None
    if isinstance(detail, str):
        message = detail
    elif isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail") or code
        details = {k: v for k, v in detail.items() if k not in ("message", "detail")} or None
    elif isinstance(detail, list):
        message = "; ".join(str(d) for d in detail) or code
    else:
        message = str(exc) or code

    # 서비스 레이어 예외에 details 가 붙어 있으면 응답에 합쳐 노출.
    extra_details = getattr(exc, "details", None)
    if isinstance(extra_details, dict) and extra_details:
        merged = dict(details or {})
        merged.update(extra_details)
        details = merged

    body = {"error": {"code": code, "message": str(message)}}
    if details:
        body["error"]["details"] = details

    if isinstance(exc, exceptions.Throttled) and exc.wait is not None:
        if response is None:
            response = Response(body, status=http_status)
        response["Retry-After"] = str(int(exc.wait))
        response.data = body
        return response
    if response is None:
        # 서비스 레이어 예외 / Http404 등 DRF 가 response 를 만들지 않는 케이스.
        return Response(body, status=http_status)
    response.data = body
    return response
