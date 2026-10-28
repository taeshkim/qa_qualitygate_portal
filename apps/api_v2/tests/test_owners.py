"""담당자 관리 API (`/api/v2/owners/`, `/ci-api/owners/`) — owners:manage 스코프 + admin 소유 키."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.activitylog.models import ActivityLog
from apps.api_v2 import scopes as S
from apps.api_v2.models import ApiKey
from apps.integrations.models import ApiEndpointOwner

BODY = {
    "repo": "JK PC", "method": "post", "endpoint": "/member/users/edit",
    "category": "회원", "fe_owner": "김프론트", "be_owner": "김백엔드",
}


def _h(key):
    return {"HTTP_AUTHORIZATION": f"Bearer {key}"}


class OwnersApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="adm", password="pw", role="qpe_admin", email="adm@worxphere.ai")
        self.ops = User.objects.create_user(username="ops", password="pw", role="qpe_ops", email="ops@worxphere.ai")
        _, self.key = ApiKey.issue(user=self.admin, name="own", scopes=[S.OWNERS_MANAGE])
        _, self.wild = ApiKey.issue(user=self.admin, name="wild", scopes=[S.WILDCARD])
        _, self.ops_key = ApiKey.issue(user=self.ops, name="ops", scopes=[S.OWNERS_MANAGE])
        self.client = Client()

    def _create(self, **over):
        return self.client.post("/api/v2/owners/", {**BODY, **over}, content_type="application/json", **_h(self.key))

    def test_create_normalizes_and_logs(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()
        self.assertEqual(data["method"], "POST")
        self.assertFalse(data["is_imported"])
        row = ApiEndpointOwner.objects.get(pk=data["id"])
        self.assertEqual((row.fe_owner, row.be_owner), ("김프론트", "김백엔드"))
        log = ActivityLog.objects.get(action=ActivityLog.Action.API_OWNER_CREATED)
        self.assertIn("via=api", log.detail)
        self.assertNotIn(self.key, log.detail)

    def test_create_without_owners_is_allowed(self):
        """담당자 미정 — fe/be_owner 생략·빈 문자열·null 모두 등록 가능(빈 값으로 저장)."""
        cases = [
            ("/a", {}),
            ("/b", {"fe_owner": "", "be_owner": ""}),
            ("/c", {"fe_owner": None, "be_owner": None}),
        ]
        for endpoint, extra in cases:
            with self.subTest(endpoint=endpoint):
                body = {"repo": "JK PC", "method": "GET", "endpoint": endpoint, **extra}
                resp = self.client.post("/api/v2/owners/", body, content_type="application/json", **_h(self.key))
                self.assertEqual(resp.status_code, 201, resp.content)
                self.assertEqual((resp.json()["fe_owner"], resp.json()["be_owner"]), ("", ""))
        # 나중에 담당자 지정
        pk = ApiEndpointOwner.objects.get(endpoint="/a").pk
        resp = self.client.patch(f"/api/v2/owners/{pk}/", {"be_owner": "김백엔드"}, content_type="application/json", **_h(self.key))
        self.assertEqual(resp.json()["be_owner"], "김백엔드")

    def test_duplicate_is_409_with_existing_id(self):
        first = self._create().json()["id"]
        resp = self._create(method="POST", be_owner="다른사람")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "already_exists")
        self.assertEqual(resp.json()["error"]["details"]["existing_id"], first)
        self.assertEqual(ApiEndpointOwner.objects.count(), 1)

    def test_validation_errors(self):
        for bad in ({"method": "FETCH"}, {"endpoint": "  "}, {"repo": ""}):
            with self.subTest(bad=bad):
                resp = self._create(**bad)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["error"]["code"], "invalid_parameters")

    def test_list_filter_and_paging(self):
        self._create()
        self._create(endpoint="/other", repo="AM PC", fe_owner="이프론트")
        resp = self.client.get("/api/v2/owners/?q=이프론트", **_h(self.key))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["endpoint"] for r in resp.json()["results"]], ["/other"])
        resp = self.client.get("/api/v2/owners/?repo=JK PC&method=post&is_imported=false", **_h(self.key))
        self.assertEqual(resp.json()["count"], 1)

    def test_patch_put_delete(self):
        pk = self._create().json()["id"]
        url = f"/api/v2/owners/{pk}/"
        resp = self.client.patch(url, {"fe_owner": "홍길동"}, content_type="application/json", **_h(self.key))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["fe_owner"], "홍길동")
        self.assertEqual(resp.json()["be_owner"], "김백엔드")  # 안 보낸 필드는 유지
        # PUT 은 필수 필드 누락 시 400
        resp = self.client.put(url, {"fe_owner": "x"}, content_type="application/json", **_h(self.key))
        self.assertEqual(resp.status_code, 400)
        # 다른 row 와 (method, endpoint) 충돌 → 409
        other = self._create(endpoint="/o2").json()["id"]
        resp = self.client.patch(f"/api/v2/owners/{other}/", {"endpoint": "/member/users/edit"},
                                 content_type="application/json", **_h(self.key))
        self.assertEqual(resp.status_code, 409)
        # 자기 자신 값 그대로 PATCH 는 OK
        resp = self.client.patch(url, {"endpoint": "/member/users/edit"}, content_type="application/json", **_h(self.key))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.delete(url, **_h(self.key)).status_code, 204)
        self.assertEqual(self.client.get(url, **_h(self.key)).status_code, 404)
        self.assertTrue(ActivityLog.objects.filter(action=ActivityLog.Action.API_OWNER_DELETED).exists())

    def test_wildcard_scope_is_not_enough(self):
        resp = self.client.get("/api/v2/owners/", **_h(self.wild))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")

    def test_non_admin_owner_denied(self):
        resp = self.client.get("/api/v2/owners/", **_h(self.ops_key))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "role_denied")

    def test_no_auth_and_session_not_accepted(self):
        self.assertEqual(self.client.get("/api/v2/owners/").status_code, 401)
        self.client.force_login(self.admin)
        self.assertIn(self.client.get("/api/v2/owners/").status_code, (401, 403))

    def test_ci_api_path_works_and_ignores_session(self):
        resp = self.client.post("/ci-api/owners/", BODY, content_type="application/json", **_h(self.key))
        self.assertEqual(resp.status_code, 201, resp.content)
        c = Client()
        c.force_login(self.admin)
        self.assertEqual(c.get("/ci-api/owners/").status_code, 401)
