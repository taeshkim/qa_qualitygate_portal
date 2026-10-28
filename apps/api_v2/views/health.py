"""인증 불요 health + 인증된 키 식별용 me 엔드포인트 (PRD §5)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiExample, extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from .. import scopes as _scopes
from ..authentication import ApiKeyAuthentication

API_V2_VERSION = "v2.0"


@extend_schema(
    summary="서버가 살아있는지 확인 (인증 불요)",
    description=(
        "이 API 서버(`/api/v2/`)가 정상적으로 응답하는지 확인하는 가장 단순한 엔드포인트입니다. "
        "API Key 나 로그인 없이 **누구나** 호출할 수 있습니다.\n"
        "\n"
        "**언제 쓰나요**: 처음 이 API 와 연동을 시작할 때 가장 먼저 호출해서 \"서버에 접속은 된다\" "
        "를 확인하는 용도로 씁니다. 또는 모니터링 시스템에서 주기적으로 호출해 서버 다운을 감지할 때 "
        "사용합니다. 특정 기능이 되는지(내 API Key 가 유효한지 등)는 확인해 주지 않으니, "
        "그 다음 단계로는 `GET /api/v2/me/` 를 호출해 보세요.\n"
        "\n"
        "**보내는 것**: 없음 (파라미터 없이 그냥 GET 요청).\n"
        "\n"
        "**받는 것**: `status`(항상 `\"ok\"`), `version`(현재 API 버전 문자열)."
    ),
    tags=["Auth & Health"],
    responses={
        200: inline_serializer(
            name="HealthResponse",
            fields={
                "status": serializers.CharField(help_text="항상 \"ok\" 입니다. 이 값이 보이면 서버가 정상 동작 중입니다."),
                "version": serializers.CharField(help_text="현재 배포된 API 버전 (예: \"v2.0\")."),
            },
        ),
    },
    examples=[
        OpenApiExample(
            "정상",
            value={"status": "ok", "version": API_V2_VERSION},
            response_only=True,
        ),
    ],
)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def health(request):
    return Response({"status": "ok", "version": API_V2_VERSION})


@extend_schema(
    summary="내 API Key 가 정상인지, 어떤 권한(scope)을 가졌는지 확인",
    description=(
        "지금 요청에 사용한 인증 수단(API Key 또는 포털 로그인 세션)이 유효한지, "
        "그리고 어떤 사용자로 인식되고 있는지, 이 키가 가진 권한 범위(scope, 스코프)는 "
        "무엇인지 확인하는 API 입니다.\n"
        "\n"
        "**언제 쓰나요**: API Key 를 새로 발급받은 직후 \"이 키가 실제로 동작하는지\" 가장 먼저 "
        "확인할 때, 또는 다른 API 호출이 `403 scope_required` 로 거절됐을 때 \"내 키에 어떤 "
        "권한이 있길래 막힌 걸까\" 를 점검할 때 사용합니다.\n"
        "\n"
        "**보내는 것**: 없음. `Authorization: Bearer qgp_...` 헤더(또는 포털 로그인 세션 쿠키)만 "
        "있으면 됩니다.\n"
        "\n"
        "**받는 것**:\n"
        "- `user`: 이 요청을 보낸 사용자 정보(`id`, `username`, `role`=포털 내 역할, "
        "`is_admin`=관리자 여부).\n"
        "- `api_key`: API Key 로 인증한 경우에만 채워집니다(포털 로그인 세션으로 호출하면 `null`). "
        "`key_prefix`(키 앞부분 몇 글자 — 어떤 키인지 구별용, 전체 키 값은 발급 시 1회만 노출되고 "
        "다시 볼 수 없습니다), `scopes`(**이 키가 실제로 가진 권한 목록** — 다른 API 호출 가능 "
        "여부는 이 값으로 판단하세요), `expires_at`(만료 예정 시각, 없으면 `null`=무제한).\n"
        "- `available_scopes`: 이 시스템 전체에 존재하는 스코프 종류 목록입니다. **내 키의 권한이 "
        "아니라 참고용 전체 목록**이니 주의하세요 — 내가 실제로 쓸 수 있는 권한은 위 `api_key.scopes` "
        "쪽을 봐야 합니다."
    ),
    tags=["Auth & Health"],
    responses={
        200: inline_serializer(
            name="MeResponse",
            fields={
                "user": inline_serializer(
                    name="MeUser",
                    fields={
                        "id": serializers.CharField(help_text="사용자 고유 id"),
                        "username": serializers.CharField(help_text="포털 로그인 아이디"),
                        "role": serializers.CharField(
                            allow_null=True,
                            help_text="포털 내 역할 (예: qpe_admin, viewer 등). 세션 인증이 아니면 API Key 소유자의 역할입니다.",
                        ),
                        "is_admin": serializers.BooleanField(help_text="관리자 권한 보유 여부"),
                    },
                ),
                "api_key": inline_serializer(
                    name="MeApiKey",
                    fields={
                        "id": serializers.CharField(help_text="이 API Key 의 고유 id"),
                        "name": serializers.CharField(help_text="발급 시 붙인 키 이름 (용도 구분용)"),
                        "key_prefix": serializers.CharField(help_text="키 앞부분 일부. 전체 평문 값은 발급 직후 1회만 노출되므로 이후에는 이 prefix 로만 어떤 키인지 구별합니다."),
                        "scopes": serializers.ListField(
                            child=serializers.CharField(),
                            help_text="이 키에 실제로 부여된 권한(스코프) 목록. 예: [\"meta:read\", \"runs:read\"]",
                        ),
                        "expires_at": serializers.DateTimeField(
                            allow_null=True,
                            help_text="키 만료 예정 시각(ISO 8601). null 이면 만료 기한 없음.",
                        ),
                    },
                    required=False,
                    allow_null=True,
                    help_text="포털 로그인 세션으로 호출한 경우(API Key 미사용) null 입니다.",
                ),
                "available_scopes": serializers.ListField(
                    child=serializers.CharField(),
                    help_text="시스템에 존재하는 전체 스코프 종류(참고용). 내가 가진 권한은 api_key.scopes 를 보세요.",
                ),
            },
        ),
    },
    examples=[
        OpenApiExample(
            "API Key 로 인증",
            value={
                "user": {
                    "id": "1",
                    "username": "taeshkim",
                    "role": "qpe_admin",
                    "is_admin": True,
                },
                "api_key": {
                    "id": "3f6b1e9e-1234-4a9b-9a11-abcdef012345",
                    "name": "jenkins-ci",
                    "key_prefix": "ab12cd34",
                    "scopes": ["meta:read", "runs:read"],
                    "expires_at": "2026-10-01T00:00:00+09:00",
                },
                "available_scopes": [
                    "meta:read", "runs:read", "runs:trigger", "runs:control",
                ],
            },
            response_only=True,
        ),
        OpenApiExample(
            "포털 세션 로그인 (API Key 없음)",
            value={
                "user": {
                    "id": "1",
                    "username": "taeshkim",
                    "role": "qpe_admin",
                    "is_admin": True,
                },
                "api_key": None,
                "available_scopes": [
                    "meta:read", "runs:read", "runs:trigger", "runs:control",
                ],
            },
            response_only=True,
        ),
    ],
)
@api_view(["GET"])
@authentication_classes([ApiKeyAuthentication, SessionAuthentication])
@permission_classes([permissions.IsAuthenticated])
def me(request):
    api_key = getattr(request, "auth", None)
    payload = {
        "user": {
            "id": str(request.user.pk),
            "username": request.user.username,
            "role": getattr(request.user, "role", None),
            "is_admin": getattr(request.user, "is_admin", False),
        },
        "api_key": None,
    }
    if api_key is not None and hasattr(api_key, "pk"):
        payload["api_key"] = {
            "id": str(api_key.pk),
            "name": api_key.name,
            "key_prefix": api_key.key_prefix,
            "scopes": list(api_key.scopes or []),
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
        }
    payload["available_scopes"] = list(_scopes.ALL_SCOPES)
    return Response(payload)
