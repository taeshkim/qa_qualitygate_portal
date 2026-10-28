"""DRF Authentication — Authorization: Bearer qgp_{prefix}_{secret} (또는 Basic).

Basic 은 Jenkins/curl `-u` 처럼 Bearer 를 못 쓰는 클라이언트용 — 비밀번호(비어 있으면 사용자명) 자리에 `qgp_` 키를
넣는다. 사용자명은 무시하며, `qgp_` 로 시작하지 않는 Basic 자격증명(아이디/비밀번호)은 인증하지 않는다.

세션 인증과 병행 가능하도록 BaseAuthentication 만 구현. 인증 후 `request.auth` 에 ApiKey 인스턴스.
"""

from __future__ import annotations

import base64
import binascii

from rest_framework import authentication, exceptions

from .models import ApiKey


class ApiKeyAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION") or ""
        if not header:
            return None
        parts = header.split()
        if len(parts) != 2:
            return None
        if parts[0] == self.keyword:
            plaintext = parts[1]
        elif parts[0] == "Basic":
            plaintext = self._key_from_basic(parts[1])
        else:
            return None
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

    @staticmethod
    def _key_from_basic(token: str) -> str:
        """Basic base64(user:password) 에서 qgp_ 키를 꺼낸다(없으면 빈 문자열)."""
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return ""
        user, _, password = decoded.partition(":")
        for candidate in (password, user):
            if candidate.startswith("qgp_"):
                return candidate
        return ""

    def authenticate_header(self, request):
        return self.keyword
