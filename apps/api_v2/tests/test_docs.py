"""drf-spectacular 문서 엔드포인트 (`/api/v2/schema/` · `/docs/` · `/redoc/`).

문서 생성 전용 배선 검증 — 기존 API 뷰의 동작/응답은 건드리지 않는다.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import resolve_url
from django.test import Client, TestCase


class SchemaEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="docs-viewer", password="pw", role="qpe_ops",
        )
        self.client = Client()

    def test_schema_requires_auth(self):
        # 세션 로그인 없이 접근하면 인증 실패 (401 또는 403 — DRF 인증 클래스 조합에 따라 다름).
        resp = self.client.get("/api/v2/schema/")
        self.assertIn(resp.status_code, (401, 403))

    def test_schema_authenticated_returns_openapi_document(self):
        self.client.login(username="docs-viewer", password="pw")
        # 기본 렌더러는 YAML — JSON 은 Accept 헤더(or ?format=json)로 명시 요청.
        resp = self.client.get(
            "/api/v2/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        # OpenAPI 문서 최상위 키
        self.assertIn("openapi", body)
        self.assertTrue(str(body["openapi"]).startswith("3."))

        paths = body.get("paths", {})
        self.assertIn("/ci-api/health/", paths)  # 문서 표기는 프로그램 호출 주소(/ci-api/)

        # /api/v2/ 외 경로(legacy apps.api 등)는 스키마에서 제외되어야 한다.
        non_v2_paths = [p for p in paths if not p.startswith("/ci-api")]
        self.assertEqual(non_v2_paths, [])

    def test_schema_authenticated_default_format_is_yaml(self):
        self.client.login(username="docs-viewer", password="pw")
        resp = self.client.get("/api/v2/schema/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/vnd.oai.openapi", resp["Content-Type"])
        self.assertIn(b"openapi:", resp.content)

    def test_schema_security_scheme_is_api_key_only(self):
        # AUTHENTICATION_WHITELIST 회귀 방지 — 스키마에 세션/기본 인증(cookieAuth/basicAuth)이
        # 노출되면 Swagger "Authorize" 가 API Key 외의 방식도 요구하는 것처럼 오해를 준다.
        self.client.login(username="docs-viewer", password="pw")
        resp = self.client.get(
            "/api/v2/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        security_schemes = body.get("components", {}).get("securitySchemes", {})
        self.assertEqual(list(security_schemes), ["ApiKeyAuth"])
        self.assertNotIn("cookieAuth", security_schemes)
        self.assertNotIn("basicAuth", security_schemes)

    def test_schema_operation_security_has_no_cookie_auth_reference(self):
        self.client.login(username="docs-viewer", password="pw")
        resp = self.client.get(
            "/api/v2/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        me_get = body["paths"]["/ci-api/me/"]["get"]
        referenced_schemes = {
            scheme_name
            for requirement in me_get.get("security", [])
            for scheme_name in requirement
        }
        self.assertNotIn("cookieAuth", referenced_schemes)
        self.assertIn("ApiKeyAuth", referenced_schemes)


class SwaggerUIEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="docs-viewer2", password="pw", role="qpe_ops",
        )
        self.client = Client()

    def test_docs_redirects_to_login_when_not_authenticated(self):
        resp = self.client.get("/api/v2/docs/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(resolve_url(settings.LOGIN_URL), resp["Location"])
        self.assertIn("/api/v2/docs/", resp["Location"])

    def test_docs_authenticated_returns_swagger_ui_html(self):
        self.client.login(username="docs-viewer2", password="pw")
        resp = self.client.get("/api/v2/docs/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp["Content-Type"])
        self.assertIn(b"swagger-ui", resp.content.lower())


class RedocEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="docs-viewer3", password="pw", role="qpe_ops",
        )
        self.client = Client()

    def test_redoc_redirects_to_login_when_not_authenticated(self):
        resp = self.client.get("/api/v2/redoc/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(resolve_url(settings.LOGIN_URL), resp["Location"])
        self.assertIn("/api/v2/redoc/", resp["Location"])

    def test_redoc_authenticated_returns_redoc_html(self):
        self.client.login(username="docs-viewer3", password="pw")
        resp = self.client.get("/api/v2/redoc/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp["Content-Type"])
        self.assertIn(b"redoc", resp.content.lower())


class SchemaGenerationTests(TestCase):
    """다른 에이전트들이 views 에 @extend_schema 를 계속 붙이는 중이므로,
    스키마 생성 자체가 예외 없이 도는지 직접 SchemaGenerator 로도 확인한다
    (엔드포인트 200 만으로는 잡히지 않는 generator 레벨 예외 대비)."""

    def test_generator_builds_schema_without_raising(self):
        from drf_spectacular.generators import SchemaGenerator

        generator = SchemaGenerator()
        schema = generator.get_schema(request=None, public=True)

        self.assertIsInstance(schema, dict)
        self.assertIn("paths", schema)
        self.assertIn("/ci-api/health/", schema["paths"])
        non_v2_paths = [p for p in schema["paths"] if not p.startswith("/ci-api")]
        self.assertEqual(non_v2_paths, [])


class SchemaCiApiNotationTests(TestCase):
    """Swagger/ReDoc 에 보이는 경로·설명·예시가 /ci-api/ 로 표기되고, 실제 라우팅은 그대로인지 확인."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="docs-ci", password="pw", role="qpe_ops")
        self.client = Client()
        self.client.login(username="docs-ci", password="pw")

    def _schema(self):
        resp = self.client.get("/api/v2/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_no_api_v2_path_remains_in_paths(self):
        import json

        paths = self._schema()["paths"]
        self.assertTrue(paths)
        self.assertEqual([p for p in paths if "/api/v2/" in p], [])
        # 경로 하위(설명/예시)의 호출 안내도 /api/v2/ 가 남지 않는다.
        self.assertNotIn("/api/v2/", json.dumps(paths, ensure_ascii=False))

    def test_description_explains_ci_api_base_and_auth(self):
        info = self._schema()["info"]["description"]
        self.assertIn("/ci-api/", info)
        self.assertIn("Bearer", info)
        self.assertIn("Basic", info)

    def test_export_and_docs_stay_out_of_schema(self):
        paths = self._schema()["paths"]
        self.assertEqual([p for p in paths if "export" in p or "schema" in p or "docs" in p or "redoc" in p], [])

    def test_runtime_routes_are_unchanged(self):
        # 문서 표기만 바뀌었다 — /api/v2/ 는 여전히 동작한다.
        self.assertEqual(self.client.get("/api/v2/health/").status_code, 200)

    def test_swagger_page_validates_key_against_ci_api(self):
        resp = self.client.get("/api/v2/docs/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'.replace("/api/v2/", "/ci-api/")', resp.content)
