"""인증 불요 health + 인증된 키 식별용 me 엔드포인트 (PRD §5)."""

from __future__ import annotations

from rest_framework import permissions
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


@api_view(["GET"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def health(request):
    return Response({"status": "ok", "version": API_V2_VERSION})


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
