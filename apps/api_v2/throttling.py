"""per-API-Key rate limit (PRD §4.6).

- 메타/조회: 120 req/min
- trigger/control: 30 req/min

세션 인증은 별도 throttle 적용하지 않음 (기존 portal UI 동작 보존).
"""

from __future__ import annotations

from rest_framework.throttling import SimpleRateThrottle


class _BaseApiKeyThrottle(SimpleRateThrottle):
    scope = "api_v2_default"

    def get_cache_key(self, request, view):
        api_key = getattr(request, "auth", None)
        if api_key is None:
            return None  # 세션 인증은 throttle skip
        return self.cache_format % {"scope": self.scope, "ident": str(api_key.pk)}


class ReadThrottle(_BaseApiKeyThrottle):
    scope = "api_v2_read"
    rate = "120/min"


class WriteThrottle(_BaseApiKeyThrottle):
    scope = "api_v2_write"
    rate = "30/min"
