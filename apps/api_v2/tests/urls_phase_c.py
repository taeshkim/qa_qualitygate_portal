"""Phase C 테스트용 URLConf — 기존 `/api/v2/` 라우팅 + 메타 라우팅을 합쳐 사용.

`apps.api_v2.urls` 는 health/me 만 포함하므로, Phase C 메타 라우팅을 같이 노출하기
위해 테스트에서 `ROOT_URLCONF` 를 이 모듈로 override 한다. 운영 통합은 master 에서
별도로 진행.
"""

from __future__ import annotations

from django.urls import include, path

urlpatterns = [
    path("api/v2/", include("apps.api_v2.urls")),
    # Phase C — 같은 prefix 에 메타 라우팅 덧붙임.
    path("api/v2/", include("apps.api_v2.urls_meta")),
]
