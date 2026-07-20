"""Phase C — 메타 조회 API (folders / subfolders / gates / suites).

기존 phase_a 테스트와 동일한 키 발급 패턴을 따른다. URLConf 는 master 통합 전이므로
테스트 안에서 `apps.api_v2.tests.urls_phase_c` 로 override.
"""

from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from apps.api_v2 import scopes as S
from apps.api_v2.models import ApiKey
from apps.qualitygates.models import (
    QualityGate,
    QualityGateFolder,
    QualityGateRun,
    QualityGateSubFolder,
    QualityGateTestSuite,
)
from apps.suites.models import TestSuite


URLCONF = "apps.api_v2.tests.urls_phase_c"


def _auth(plaintext):
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}


@override_settings(ROOT_URLCONF=URLCONF)
class PhaseCMetaTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="phasec", password="pw", role="qpe_ops",
        )
        self.key_meta, self.token_meta = ApiKey.issue(
            user=self.user, name="phasec-meta", scopes=[S.META_READ],
        )
        self.key_runs, self.token_runs = ApiKey.issue(
            user=self.user, name="phasec-runs", scopes=[S.RUNS_READ],
        )
        self.client = Client()

        # 도메인 데이터
        self.folder = QualityGateFolder.objects.create(name="auth-service")
        self.subfolder = QualityGateSubFolder.objects.create(
            folder=self.folder, name="admin",
        )
        self.gate_dev = QualityGate.objects.create(
            folder=self.folder,
            subfolder=self.subfolder,
            name="auth-admin",
            gate_type=QualityGate.GateType.DEV_STG,
            deploy_job_path="auth/deploy",
        )
        self.gate_prd = QualityGate.objects.create(
            folder=self.folder,
            name="auth-admin-prd",
            gate_type=QualityGate.GateType.STG_PRD,
            deploy_job_path="auth/prd",
        )
        self.suite = TestSuite.objects.create(
            name="auth-regression",
            jenkins_folder_path="100_jk/auth_regression",
        )
        QualityGateTestSuite.objects.create(
            gate=self.gate_dev, test_suite=self.suite, order=0,
        )
        # 최신 Run (latest_run include 검증용)
        self.run = QualityGateRun.objects.create(
            gate=self.gate_dev,
            status=QualityGateRun.Status.COMPLETED,
            seq_no=1,
        )

    # ------------------------------------------------------------------
    # 인증 / 스코프
    # ------------------------------------------------------------------

    def test_anonymous_is_rejected(self):
        resp = self.client.get("/api/v2/gates/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "not_authenticated")

    def test_key_without_meta_scope_is_forbidden(self):
        resp = self.client.get("/api/v2/gates/", **_auth(self.token_runs))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")

    # ------------------------------------------------------------------
    # 게이트 목록 / 필터
    # ------------------------------------------------------------------

    def test_gate_list_returns_paginated_count(self):
        resp = self.client.get("/api/v2/gates/", **_auth(self.token_meta))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("count", body)
        self.assertIn("results", body)
        self.assertEqual(body["count"], 2)
        names = {row["name"] for row in body["results"]}
        self.assertEqual(names, {"auth-admin", "auth-admin-prd"})

    def test_gate_list_filter_by_gate_type(self):
        resp = self.client.get(
            "/api/v2/gates/?gate_type=dev_stg", **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["name"], "auth-admin")

    def test_gate_list_filter_by_folder(self):
        resp = self.client.get(
            f"/api/v2/gates/?folder={self.folder.id}", **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 2)

    def test_gate_list_filter_by_q(self):
        resp = self.client.get(
            "/api/v2/gates/?q=prd", **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["name"], "auth-admin-prd")

    # ------------------------------------------------------------------
    # 게이트 상세 + include
    # ------------------------------------------------------------------

    def test_gate_detail_without_include(self):
        resp = self.client.get(
            f"/api/v2/gates/{self.gate_dev.id}/", **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["name"], "auth-admin")
        self.assertIsNone(body["latest_run"])
        self.assertIsNone(body["test_suites"])

    def test_gate_detail_with_include_latest_run(self):
        resp = self.client.get(
            f"/api/v2/gates/{self.gate_dev.id}/?include=latest_run",
            **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNotNone(body["latest_run"])
        self.assertEqual(body["latest_run"]["id"], str(self.run.id))
        self.assertEqual(body["latest_run"]["status"], "completed")
        self.assertEqual(body["latest_run"]["result"], "PASS")

    def test_gate_detail_with_include_test_suites(self):
        resp = self.client.get(
            f"/api/v2/gates/{self.gate_dev.id}/?include=latest_run,test_suites",
            **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNotNone(body["test_suites"])
        self.assertEqual(len(body["test_suites"]), 1)
        self.assertEqual(body["test_suites"][0]["name"], "auth-regression")

    def test_gate_detail_404(self):
        resp = self.client.get(
            f"/api/v2/gates/{uuid.uuid4()}/", **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "not_found")

    # ------------------------------------------------------------------
    # Folder / SubFolder
    # ------------------------------------------------------------------

    def test_folder_list(self):
        resp = self.client.get("/api/v2/folders/", **_auth(self.token_meta))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        row = body["results"][0]
        self.assertEqual(row["name"], "auth-service")
        self.assertEqual(row["subfolder_count"], 1)

    def test_folder_subfolders(self):
        resp = self.client.get(
            f"/api/v2/folders/{self.folder.id}/subfolders/",
            **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["name"], "admin")

    def test_subfolder_detail_includes_gates(self):
        resp = self.client.get(
            f"/api/v2/subfolders/{self.subfolder.id}/",
            **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["name"], "admin")
        gate_names = {g["name"] for g in body["gates"]}
        self.assertIn("auth-admin", gate_names)

    # ------------------------------------------------------------------
    # Suite
    # ------------------------------------------------------------------

    def test_suite_list(self):
        resp = self.client.get("/api/v2/suites/", **_auth(self.token_meta))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["name"], "auth-regression")

    def test_suite_list_filter_q(self):
        resp = self.client.get(
            "/api/v2/suites/?q=regression", **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)

    def test_suite_detail_includes_jobs(self):
        resp = self.client.get(
            f"/api/v2/suites/{self.suite.id}/", **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["name"], "auth-regression")
        self.assertEqual(body["jobs"], [])

    # ------------------------------------------------------------------
    # Params
    # ------------------------------------------------------------------

    def test_params_requires_stage(self):
        resp = self.client.get(
            f"/api/v2/gates/{self.gate_dev.id}/params/",
            **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_parameters")

    def test_params_rejects_bad_stage(self):
        resp = self.client.get(
            f"/api/v2/gates/{self.gate_dev.id}/params/?stage=bogus",
            **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 400)

    def test_params_prod_returns_empty_when_no_prod_job(self):
        # gate_dev 는 prod_job_path 가 비어있으므로 prod stage 는 빈 parameters 반환.
        resp = self.client.get(
            f"/api/v2/gates/{self.gate_dev.id}/params/?stage=prod",
            **_auth(self.token_meta),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["stage"], "prod")
        self.assertEqual(body["parameters"], [])
