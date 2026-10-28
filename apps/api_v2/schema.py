"""drf-spectacular 스키마 커스터마이즈 — 문서 생성 전용 (API 동작 불변).

- `ApiKeyAuthScheme`: 커스텀 `ApiKeyAuthentication` 을 OpenAPI security scheme
  (`Authorization: Bearer qgp_...`) 으로 노출. Swagger UI 의 Authorize 버튼에서
  키를 넣고 Try-it-out 을 쓸 수 있게 한다. 모듈 import 시 자동 등록되며,
  등록 트리거는 `apps.api_v2.apps.ApiV2Config.ready()`.
- `preprocess_only_api_v2`: 스키마 대상 엔드포인트를 `/api/v2/` 로 한정
  (legacy `apps.api` 등 제외) — settings.SPECTACULAR_SETTINGS 의
  PREPROCESSING_HOOKS 에서 참조.
- `rewrite_paths_to_ci_api`: 생성된 스키마의 `paths`(경로 키·설명·예시)에서 `/api/v2/` 를 `/ci-api/` 로 바꾼다.
  Swagger/ReDoc 에 보이는 주소와 Try-it-out 호출이 게이트웨이 로그인 예외 경로(키 전용)가 되게 한다.
  실제 URL 구성(`/api/v2/` 마운트)과 뷰 코드는 그대로다 — 문서 표기만 바뀐다.
Authorize 대상은 settings 의 ``AUTHENTICATION_WHITELIST`` 로 API Key 만
노출한다 — 세션 쿠키/Basic 은 문서에 표기하지 않는다(실제 API 의 세션 병행
인증 동작은 불변, 문서 표기만 제외).
"""

from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ApiKeyAuthScheme(OpenApiAuthenticationExtension):
    target_class = "apps.api_v2.authentication.ApiKeyAuthentication"
    name = "ApiKeyAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "qgp_{prefix}_{secret}",
            "description": (
                "포털 `/accounts/api-keys/` 에서 발급한 API Key 를 "
                "`Authorization: Bearer qgp_...` 헤더로 전달해 주세요 "
                "(Jenkins 등은 Basic — 비밀번호 자리에 키 — 도 가능). "
                "프로그램 호출 주소는 `/ci-api/` (로그인 없이 키만 인증). "
                "키에 부여된 Scope 에 따라 접근 가능한 엔드포인트가 달라집니다."
            ),
        }


def preprocess_only_api_v2(endpoints):
    """스키마에 /api/v2/ 경로만 포함 (문서/스키마 자체 엔드포인트는 SERVE_INCLUDE_SCHEMA
    False 로 이미 제외)."""
    return [
        (path, path_regex, method, callback)
        for (path, path_regex, method, callback) in endpoints
        if path.startswith("/api/v2/")
    ]


def _to_ci_api(value):
    """스키마 조각(dict/list/str)의 `/api/v2/` 표기를 `/ci-api/` 로 재귀 치환한다."""
    if isinstance(value, str):
        return value.replace("/api/v2/", "/ci-api/")
    if isinstance(value, list):
        return [_to_ci_api(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_ci_api(v) for k, v in value.items()}
    return value


def rewrite_paths_to_ci_api(result, generator, request, public):
    """POSTPROCESSING_HOOK — `paths` 아래만 치환한다(상단 소개문 `info.description` 은 두 주소를 비교 설명하므로 제외)."""
    paths = result.get("paths") or {}
    result["paths"] = {
        key.replace("/api/v2/", "/ci-api/", 1): _to_ci_api(item) for key, item in paths.items()
    }
    return result
