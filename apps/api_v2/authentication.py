"""DRF Authentication — Authorization: Bearer qgp_{prefix}_{secret}.

세션 인증과 병행 가능하도록 BaseAuthentication 만 구현. 인증 후 `request.auth` 에 ApiKey 인스턴스.
"""

from __future__ import annotations

from rest_framework import authentication, exceptions

from .models import ApiKey


class ApiKeyAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION") or ""
        if not header:
            return None
        parts = header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            return None
        plaintext = parts[1]
        if not plaintext.startswith("qgp_"):
            return None
        key = ApiKey.find_by_plaintext(plaintext)
        if key is None:
            raise exceptions.AuthenticationFailed("api_key_invalid")
        if key.is_revoked:
            raise exceptions.AuthenticationFailed("api_key_revoked")
        if key.is_expired:
            raise exceptions.AuthenticationFailed("api_key_expired")
        if not key.user.is_active:
            raise exceptions.AuthenticationFailed("user_inactive")
        key.touch_last_used()
        return (key.user, key)

    def authenticate_header(self, request):
        return self.keyword
