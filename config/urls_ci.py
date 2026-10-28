"""`/ci-api/` 요청 전용 URLconf — CiApiBoundaryMiddleware 가 request.urlconf 로 지정한다.

기본 URLconf 에서 `api/v2/` 마운트만 `ci-api/` 로 바꾼 것이다. 이 URLconf 아래에서는 `reverse("api_v2:...")`
(응답 본문의 self_url·console_url 등)가 `/ci-api/...` 로 만들어져, 게이트웨이 로그인 예외 경로 안에서
링크를 그대로 따라갈 수 있다. 그 밖의 포털 경로(reverse 대상으로만 존재)는 `/ci-api/` 요청으로 도달할 수 없다.
"""

from django.urls import include, path

from config.urls import urlpatterns as _base_patterns

urlpatterns = [
    pattern for pattern in _base_patterns if str(pattern.pattern) != "api/v2/"
] + [
    path("ci-api/", include("apps.api_v2.urls_ci")),
]
