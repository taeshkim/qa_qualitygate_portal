"""테스트 전용 URL conf — Phase F 의 Suite Run 라우팅을 ``/api/v2/`` 에 마운트한다.

``apps/api_v2/urls.py`` 를 직접 수정하지 못하므로, 테스트에서만 ``override_settings``
로 본 모듈을 ROOT_URLCONF 로 지정해 동일한 ``/api/v2/...`` URL 로 접근 가능하게 한다.
"""

from __future__ import annotations

from django.urls import include, path

urlpatterns = [
    path("api/v2/", include("apps.api_v2.urls")),
    path("api/v2/", include("apps.api_v2.urls_suites")),
]
