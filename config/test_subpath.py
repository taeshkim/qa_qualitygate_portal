"""Nginx가 /qg 접두 경로를 제거해 전달하는 배포와 기존 루트 배포 회귀 검증."""

import hashlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.exceptions import ImproperlyConfigured
from django.core.handlers.wsgi import WSGIHandler
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import get_script_prefix, set_script_prefix

from apps.api_v2.models import ApiKey


def _load_config_module(relative_path):
    """설정별 URLconf를 격리해 다른 테스트의 import cache를 변경하지 않는다."""
    spec = importlib.util.spec_from_file_location(
        "_qgp_subpath_test_config", Path(__file__).parent / relative_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScriptNameSettingsTests(SimpleTestCase):
    def test_root_is_the_default_and_slashes_are_normalized(self):
        for value, expected in (("", None), ("/", None), ("/qg", "/qg"),
                                (" /qg/ ", "/qg"), ("/tools/qg", "/tools/qg")):
            with self.subTest(value=value), patch.dict(os.environ, {"QGP_SCRIPT_NAME": value}):
                config = _load_config_module("settings/base.py")
                self.assertEqual(config.FORCE_SCRIPT_NAME, expected)
                self.assertEqual(config.STATIC_URL, (expected or "") + "/static/")
                self.assertEqual(
                    config.SPECTACULAR_SETTINGS["SCHEMA_PATH_PREFIX_INSERT"],
                    expected or "",
                )

    def test_invalid_mount_paths_are_rejected(self):
        for value in ("qg", "//qg", "/qg?next=/", "/qg#fragment", "/qg/../admin"):
            with self.subTest(value=value), patch.dict(os.environ, {"QGP_SCRIPT_NAME": value}):
                with self.assertRaises(ImproperlyConfigured):
                    _load_config_module("settings/base.py")

    def test_static_url_is_correct_even_when_read_before_django_setup(self):
        # runserver 초기화 중 STATIC_URL이 script prefix 설정보다 먼저 읽혀도
        # 잘못된 /static/ 값이 settings cache에 남아서는 안 된다.
        script = """
import json
import django
from django.conf import settings
before_setup = settings.STATIC_URL
django.setup()
from django.templatetags.static import static
print(json.dumps([before_setup, settings.STATIC_URL, static('css/style.css')]))
"""
        for prefix in ("", "/qg"):
            with self.subTest(prefix=prefix):
                process = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=Path(__file__).parent.parent,
                    env={**os.environ, "QGP_SCRIPT_NAME": prefix,
                         "DJANGO_SETTINGS_MODULE": "config.settings.dev"},
                    capture_output=True, text=True, check=True, timeout=30,
                )
                self.assertEqual(json.loads(process.stdout), [
                    prefix + "/static/", prefix + "/static/",
                    prefix + "/static/css/style.css",
                ])


class _DeploymentChecks:
    """Client에 전달하는 URL은 항상 Nginx가 접두 경로를 제거한 PATH_INFO다."""

    @classmethod
    def setUpTestData(cls):
        cls.viewer = get_user_model().objects.create_user(
            username="subpath-viewer", password="subpath-test-password", role="viewer",
        )

    def setUp(self):
        super().setUp()
        previous_prefix = get_script_prefix()
        set_script_prefix(self.prefix or "/")
        self.addCleanup(set_script_prefix, previous_prefix)
        self.enterContext(override_settings(
            FORCE_SCRIPT_NAME=self.prefix or None,
            STATIC_URL=self.prefix + "/static/",
            DEBUG=True,
        ))
        self.enterContext(override_settings(ROOT_URLCONF=_load_config_module("urls.py")))
        external_request = self.enterContext(patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("배포 회귀 테스트에서 외부 API를 호출할 수 없습니다."),
        ))
        self.addCleanup(external_request.assert_not_called)

    def test_anonymous_dashboard_redirect_preserves_next(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        location = urlsplit(response["Location"])
        self.assertEqual(location.path, self.prefix + "/accounts/login/")
        self.assertEqual(parse_qs(location.query), {"next": [self.prefix + "/"]})

    def test_login_page_links_keep_the_mount_path(self):
        response = self.client.get("/accounts/login/")
        # style.css 는 캐시버스트 토큰(?v=...)이 붙으므로 마운트 경로 접두만 확인한다.
        self.assertContains(response, 'href="' + self.prefix + '/static/css/style.css?v=')
        self.assertContains(response, 'href="' + self.prefix + '/accounts/signup/"')

    def test_login_and_logout_default_redirects(self):
        response = self.client.post("/accounts/login/", {
            "username": self.viewer.username,
            "password": "subpath-test-password",
        })
        self.assertRedirects(response, self.prefix + "/", fetch_redirect_response=False)
        response = self.client.post("/accounts/logout/")
        self.assertRedirects(
            response, self.prefix + "/accounts/login/?loggedout=1", fetch_redirect_response=False,
        )

    def test_viewer_dashboard_does_not_redirect_to_itself(self):
        self.client.force_login(self.viewer)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.path_info, "/")
        self.assertEqual(response.wsgi_request.path, self.prefix + "/")
        self.assertContains(response, 'href="' + self.prefix + '/suites/runs/"')

    def test_viewer_forbidden_pages_still_redirect_inside_portal(self):
        self.client.force_login(self.viewer)
        for method in (self.client.get, self.client.post):
            with self.subTest(method=method.__name__):
                response = method("/suites/")
                self.assertRedirects(
                    response, self.prefix + "/", fetch_redirect_response=False,
                )

    def test_viewer_can_revoke_own_api_key(self):
        self.client.force_login(self.viewer)
        # 회수 검증에 평문 키를 발급/저장할 필요가 없다.
        key = ApiKey.objects.create(
            user=self.viewer, name="mount-revoke", scopes=["runs:read"],
            key_prefix=secrets.token_hex(4),
            key_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
        )
        response = self.client.post(f"/accounts/api-keys/{key.pk}/revoke/")
        self.assertRedirects(
            response, self.prefix + "/accounts/api-keys/", fetch_redirect_response=False,
        )
        key.refresh_from_db()
        self.assertTrue(key.is_revoked)

    def test_viewer_api_post_reaches_drf_method_validation(self):
        self.client.force_login(self.viewer)
        response = self.client.post("/api/v2/health/")
        # GET 전용 API의 405는 portal 권한 미들웨어를 통과했다는 의미다.
        self.assertEqual(response.status_code, 405)
        self.assertIn("application/json", response["Content-Type"])

    def test_service_worker_scope_and_fallback_url_use_portal_root(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response["Service-Worker-Allowed"], self.prefix + "/")
        self.assertContains(response, 'const PORTAL_ROOT = "' + self.prefix + '/";')

    def test_runserver_serves_static_file_after_proxy_strips_prefix(self):
        # runserver의 실제 WSGI static wrapper를 거친다. Client만으로는 이 경로를
        # 검증할 수 없고, /qg/static/을 /static/으로 전달하면 wrapper를 우회한다.
        application = StaticFilesHandler(WSGIHandler())
        environ = RequestFactory().get("/static/css/style.css").environ
        start_response = Mock()
        response = application(environ, start_response)
        try:
            content = b"".join(response)
        finally:
            response.close()
        self.assertEqual(start_response.call_args.args[0], "200 OK")
        self.assertTrue(content)
        headers = dict(start_response.call_args.args[1])
        self.assertIn("text/css", headers["Content-Type"])


class SubpathDeploymentTests(_DeploymentChecks, TestCase):
    prefix = "/qg"


class RootDeploymentTests(_DeploymentChecks, TestCase):
    prefix = ""
