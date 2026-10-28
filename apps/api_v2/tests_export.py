"""`/api/v2/export/` (서버 이관용 DB 스냅샷 내보내기) 테스트.

- 운영 DB(db.sqlite3) 는 건드리지 않는다. 내보내기 대상 DB / 번들 디렉터리는 모두 tempfile 임시 경로이며
  `SYNCOPS_EXPORT_DB_PATH` / `SYNCOPS_EXPORT_DIR` 설정으로 주입한다. 원본 서버 요청은 없다.
- API Key 는 `ApiKey.issue` 로 그때그때 발급하고, 평문은 변수로만 쓰며 출력/하드코딩하지 않는다.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.activitylog.models import ActivityLog
from apps.api_v2 import scopes as S
from apps.api_v2 import views_export as VE
from apps.api_v2.models import ApiKey
from apps.syncops import snapshot as snap

BASE = "/api/v2/export/snapshots/"


def _auth(plaintext):
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}


def _make_source_db(path: Path, rows: int = 2000) -> None:
    """내보내기 대상이 될 가짜 SQLite DB (Django 마이그레이션 테이블 포함)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, app TEXT, name TEXT, applied TEXT)")
        conn.execute("INSERT INTO django_migrations(app,name,applied) VALUES ('auth','0001_initial','2026-01-01')")
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO sample(body) VALUES (?)",
                         [(f"row-{i}-" + "x" * 200,) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def _wait_done(snap_id: str, timeout: float = 60.0) -> None:
    """워커 스레드 종료 대기 (HTTP 폴링 대신 — throttle 영향 없이 결정적)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = VE._ACTIVE.get(snap_id)
        if t is None or not t.is_alive():
            return
        time.sleep(0.02)
    raise AssertionError("export 워커가 제한 시간 안에 끝나지 않았습니다.")


class ExportTestBase(TestCase):
    def setUp(self):
        cache.clear()  # throttle 카운터 초기화
        self.tmp = Path(tempfile.mkdtemp(prefix="qgp-export-test-"))
        self.src_db = self.tmp / "source.sqlite3"
        _make_source_db(self.src_db)
        self.out_root = self.tmp / "snapshots"
        ov = override_settings(
            ALLOWED_HOSTS=["*"],
            SYNCOPS_EXPORT_DB_PATH=str(self.src_db),
            SYNCOPS_EXPORT_DIR=str(self.out_root),
        )
        ov.enable()
        self.addCleanup(ov.disable)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._join_workers)

        User = get_user_model()
        self.admin = User.objects.create_user(username="exp_admin", password="pw", role="qpe_admin")
        self.ops = User.objects.create_user(username="exp_ops", password="pw", role="qpe_ops")
        self.superuser = User.objects.create_superuser(username="exp_super", password="pw")
        _, self.admin_key = ApiKey.issue(user=self.admin, name="exp-admin", scopes=[S.EXPORT_READ])
        self.plaintexts = [self.admin_key]

    def _join_workers(self):
        for sid, t in list(VE._ACTIVE.items()):
            t.join(timeout=30)

    # -- 헬퍼 --
    def _issue(self, user, scopes, name="k"):
        _, pt = ApiKey.issue(user=user, name=name, scopes=scopes)
        self.plaintexts.append(pt)
        return pt

    def _create(self, key=None, body=None):
        return self.client.post(
            BASE, data=json.dumps(body if body is not None else {"compress": "gzip"}),
            content_type="application/json", **_auth(key or self.admin_key))

    def _create_ready(self, compress="gzip") -> str:
        resp = self._create(body={"compress": compress})
        self.assertEqual(resp.status_code, 202, resp.content)
        sid = resp.json()["id"]
        _wait_done(sid)
        st = self.client.get(f"{BASE}{sid}/", **_auth(self.admin_key)).json()
        self.assertEqual(st["status"], "ready", st)
        return sid

    def _get(self, sid, key=None):
        return self.client.get(f"{BASE}{sid}/", **_auth(key or self.admin_key))

    def _download(self, sid, name, key=None, **extra):
        return self.client.get(f"{BASE}{sid}/download/{name}/", **_auth(key or self.admin_key), **extra)

    def _bundle(self, sid) -> Path:
        return self.out_root / f"api-{sid}"


class ScopeAndPermissionTests(ExportTestBase):
    def test_no_credentials_is_401(self):
        self.assertEqual(self.client.post(BASE, data="{}", content_type="application/json").status_code, 401)
        self.assertEqual(self.client.get(f"{BASE}{uuid.uuid4()}/").status_code, 401)

    def test_key_without_export_scope_403(self):
        key = self._issue(self.admin, [S.RUNS_READ, S.META_READ], name="norole")
        resp = self._create(key=key)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")
        self.assertEqual(self._get(uuid.uuid4(), key=key).status_code, 403)

    def test_wildcard_does_not_include_export_read(self):
        key = self._issue(self.admin, [S.WILDCARD], name="wild")
        resp = self._create(key=key)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "scope_required")
        self.assertFalse(S.has_scope([S.WILDCARD], S.EXPORT_READ))
        self.assertTrue(S.has_scope([S.WILDCARD], S.RUNS_READ))  # 기존 동작 불변
        self.assertTrue(S.has_scope([S.WILDCARD, S.EXPORT_READ], S.EXPORT_READ))

    def test_non_admin_owner_with_scope_is_403(self):
        key = self._issue(self.ops, [S.EXPORT_READ], name="ops-exp")
        resp = self._create(key=key)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "role_denied")
        self.assertFalse(self.out_root.exists() and any(self.out_root.iterdir()))

    def test_owner_demoted_after_issue_is_403(self):
        self.admin.role = "qpe_ops"
        self.admin.save()
        resp = self._create()
        self.assertEqual(resp.status_code, 403)

    def test_superuser_owner_allowed(self):
        key = self._issue(self.superuser, [S.EXPORT_READ], name="su")
        resp = self._create(key=key)
        self.assertEqual(resp.status_code, 202)
        _wait_done(resp.json()["id"])

    def test_session_auth_not_accepted(self):
        self.client.force_login(self.admin)
        resp = self.client.post(BASE, data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    def test_key_form_hides_export_scope_from_non_admin(self):
        from apps.accounts.views import _scope_options_for
        opts = {v: admin_only for v, _l, admin_only in _scope_options_for(self.ops)}
        self.assertTrue(opts[S.EXPORT_READ])  # admin 전용 표시
        self.client.force_login(self.ops)
        resp = self.client.post("/accounts/api-keys/new/", {
            "name": "sneaky", "scopes": [S.EXPORT_READ], "expires_in_days": "30"})
        self.assertFalse(ApiKey.objects.filter(user=self.ops, name="sneaky").exists(), resp.status_code)


class ExportFlowTests(ExportTestBase):
    def test_full_flow_create_poll_download_delete(self):
        resp = self._create(body={"compress": "gzip"})
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(set(body), {"id", "status", "created_at"})
        self.assertEqual(body["status"], "running")
        sid = body["id"]
        self.assertEqual(str(uuid.UUID(sid)), sid)
        _wait_done(sid)

        st = self._get(sid).json()
        self.assertEqual(st["status"], "ready")
        self.assertEqual(st["progress_pct"], 100)
        self.assertIsNone(st["error"])
        self.assertTrue(st["finished_at"])
        manifest = st["manifest"]
        self.assertEqual(manifest["schema_version"], 1)
        self.assertIsNone(manifest["config"])  # config 는 절대 포함되지 않는다
        self.assertEqual(manifest["db"]["file"], "db.sqlite3.gz")
        names = {f["name"] for f in st["files"]}
        self.assertEqual(names, {"manifest.json", "db.sqlite3.gz"})

        # 번들 디렉터리/파일 권한, config 부재, work 디렉터리 정리
        bundle = self._bundle(sid)
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)
        for child in bundle.iterdir():
            self.assertTrue(child.is_file(), child)
            self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o600, child.name)
        self.assertEqual({p.name for p in bundle.iterdir()}, {"manifest.json", "db.sqlite3.gz", "state.json"})

        # manifest.json 다운로드 + sha256
        r = self._download(sid, "manifest.json")
        self.assertEqual(r.status_code, 200)
        mbytes = b"".join(r.streaming_content)
        self.assertEqual(json.loads(mbytes), manifest)
        mf = next(f for f in st["files"] if f["name"] == "manifest.json")
        self.assertEqual(hashlib.sha256(mbytes).hexdigest(), mf["sha256"])
        self.assertEqual(r["X-Content-SHA256"], mf["sha256"])

        # DB 전체 다운로드
        r = self._download(sid, "db.sqlite3.gz")
        self.assertEqual(r.status_code, 200)
        data = b"".join(r.streaming_content)
        dbf = next(f for f in st["files"] if f["name"] == "db.sqlite3.gz")
        self.assertEqual(r["Content-Length"], str(len(data)))
        self.assertEqual(len(data), dbf["size"])
        self.assertEqual(r["Accept-Ranges"], "bytes")
        self.assertEqual(r["X-Content-SHA256"], dbf["sha256"])
        self.assertEqual(r["ETag"], f'"{dbf["sha256"]}"')
        self.assertEqual(hashlib.sha256(data).hexdigest(), dbf["sha256"])
        self.assertEqual(dbf["sha256"], manifest["db"]["file_sha256"])
        # 풀어서 sqlite 로 열린다 + 행 수 일치
        raw = gzip.decompress(data)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest["db"]["sha256"])
        out = self.tmp / "restored.sqlite3"
        out.write_bytes(raw)
        conn = sqlite3.connect(str(out))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 2000)
        finally:
            conn.close()

        # 삭제
        d = self.client.delete(f"{BASE}{sid}/", **_auth(self.admin_key))
        self.assertEqual(d.status_code, 204)
        self.assertFalse(bundle.exists())
        self.assertEqual(self._get(sid).status_code, 404)
        self.assertEqual(self.client.delete(f"{BASE}{sid}/", **_auth(self.admin_key)).status_code, 404)

    def test_default_compress_auto_and_empty_body(self):
        resp = self.client.post(BASE, **_auth(self.admin_key))
        self.assertEqual(resp.status_code, 202)
        _wait_done(resp.json()["id"])
        st = self._get(resp.json()["id"]).json()
        self.assertEqual(st["status"], "ready")
        self.assertIn(st["manifest"]["db"]["compression"], ("zstd", "gzip"))

    def test_invalid_compress_400(self):
        for val in ("bzip2", "none", 5):
            resp = self._create(body={"compress": val})
            self.assertEqual(resp.status_code, 400, val)
            self.assertEqual(resp.json()["error"]["code"], "invalid_parameters")

    def test_range_requests(self):
        sid = self._create_ready()
        full = b"".join(self._download(sid, "db.sqlite3.gz").streaming_content)
        size = len(full)
        self.assertGreater(size, 100)

        r = self._download(sid, "db.sqlite3.gz", HTTP_RANGE="bytes=10-99")
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r["Content-Range"], f"bytes 10-99/{size}")
        self.assertEqual(r["Content-Length"], "90")
        self.assertEqual(b"".join(r.streaming_content), full[10:100])

        r = self._download(sid, "db.sqlite3.gz", HTTP_RANGE="bytes=50-")
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r["Content-Range"], f"bytes 50-{size - 1}/{size}")
        self.assertEqual(b"".join(r.streaming_content), full[50:])

        # 끝이 파일보다 크면 잘라서 응답
        r = self._download(sid, "db.sqlite3.gz", HTTP_RANGE=f"bytes={size - 5}-{size + 1000}")
        self.assertEqual(r.status_code, 206)
        self.assertEqual(b"".join(r.streaming_content), full[-5:])

        # 분할 다운로드를 이어 붙이면 원본과 동일 (재개 다운로드 시나리오)
        half = size // 2
        a = b"".join(self._download(sid, "db.sqlite3.gz", HTTP_RANGE=f"bytes=0-{half - 1}").streaming_content)
        b = b"".join(self._download(sid, "db.sqlite3.gz", HTTP_RANGE=f"bytes={half}-").streaming_content)
        self.assertEqual(a + b, full)

        for bad in (f"bytes={size}-", f"bytes={size + 10}-{size + 20}", "bytes=9-3", "bytes=-",
                    "items=0-5", "bytes=0-1,3-4", "bytes=abc-", "garbage"):
            r = self._download(sid, "db.sqlite3.gz", HTTP_RANGE=bad)
            self.assertEqual(r.status_code, 416, bad)
            self.assertEqual(r["Content-Range"], f"bytes */{size}", bad)

    def test_download_streams_in_chunks_not_whole_file(self):
        sid = self._create_ready()
        r = self._download(sid, "db.sqlite3.gz")
        self.assertTrue(r.streaming)
        with mock.patch.object(VE, "STREAM_CHUNK", 64):
            r = self._download(sid, "db.sqlite3.gz")
            chunks = list(r.streaming_content)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 64 for c in chunks))

    def test_download_not_ready_is_409(self):
        gate = threading.Event()

        def slow(*a, **kw):
            gate.wait(30)
            raise snap.SnapshotError("중단")

        with mock.patch.object(VE._snap, "export_snapshot", slow):
            sid = self._create().json()["id"]
            r = self._download(sid, "manifest.json")
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.json()["error"]["code"], "export_not_ready")
            st = self._get(sid).json()
            self.assertEqual(st["status"], "running")
            self.assertEqual(st["files"], [])
            self.assertIsNone(st["manifest"])
            gate.set()
            _wait_done(sid)

    def test_missing_source_db_503(self):
        with override_settings(SYNCOPS_EXPORT_DB_PATH=str(self.tmp / "nope.sqlite3")):
            resp = self._create()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "export_unavailable")


class ConcurrencyAndFailureTests(ExportTestBase):
    def test_second_create_while_running_is_409(self):
        gate = threading.Event()

        def slow(db_path, base_dir, out_dir, **kw):
            gate.wait(30)
            raise snap.SnapshotError("테스트용 실패")

        with mock.patch.object(VE._snap, "export_snapshot", slow):
            first = self._create()
            self.assertEqual(first.status_code, 202)
            sid = first.json()["id"]
            second = self._create()
            self.assertEqual(second.status_code, 409)
            self.assertEqual(second.json()["error"]["code"], "export_in_progress")
            # 실행 중 삭제도 거부
            d = self.client.delete(f"{BASE}{sid}/", **_auth(self.admin_key))
            self.assertEqual(d.status_code, 409)
            gate.set()
            _wait_done(sid)

        st = self._get(sid).json()
        self.assertEqual(st["status"], "failed")
        self.assertIn("테스트용 실패", st["error"])
        self.assertEqual(st["files"], [])
        # 실패 후에는 새 스냅샷을 만들 수 있다
        third = self._create()
        self.assertEqual(third.status_code, 202)
        _wait_done(third.json()["id"])

    def test_include_config_and_unknown_fields_400(self):
        for body in ({"include_config": True}, {"include_config": False}, {"config": True},
                     {"compress": "gzip", "passphrase": "x"}, {"anything": 1}):
            resp = self._create(body=body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertEqual(resp.json()["error"]["code"], "unsupported", body)
        # 아무 번들도 만들어지지 않았다
        self.assertFalse(self.out_root.exists() and any(self.out_root.iterdir()))

    def test_non_object_body_400(self):
        resp = self.client.post(BASE, data="[1,2]", content_type="application/json", **_auth(self.admin_key))
        self.assertEqual(resp.status_code, 400)

    def test_config_never_in_bundle(self):
        # 환경에 .env/secrets 가 있어도 번들에 config 가 들어가지 않는다.
        sid = self._create_ready()
        self.assertFalse(any(p.name.startswith("config") for p in self._bundle(sid).iterdir()))

    def test_stale_running_state_becomes_failed(self):
        sid = str(uuid.uuid4())
        bundle = self._bundle(sid)
        bundle.mkdir(parents=True, mode=0o700)
        (bundle / "work").mkdir()
        VE._write_state(bundle, {
            "id": sid, "status": "running", "progress_pct": 40,
            "created_at": VE._iso(VE._now()), "finished_at": None, "error": None,
            "pid": os.getpid(), "heartbeat": time.time()})  # 같은 프로세스인데 워커 스레드 없음
        st = self._get(sid).json()
        self.assertEqual(st["status"], "failed")
        self.assertIn("중단", st["error"])
        self.assertFalse((bundle / "work").exists())
        # stale 은 동시 실행 제한에 걸리지 않는다
        r = self._create()
        self.assertEqual(r.status_code, 202)
        _wait_done(r.json()["id"])

    def test_dead_pid_and_old_heartbeat_are_stale(self):
        for pid, hb in ((2 ** 31 - 2, time.time()), (1, time.time() - 10_000)):
            sid = str(uuid.uuid4())
            bundle = self._bundle(sid)
            bundle.mkdir(parents=True, mode=0o700)
            VE._write_state(bundle, {
                "id": sid, "status": "running", "progress_pct": 1,
                "created_at": VE._iso(VE._now()), "pid": pid, "heartbeat": hb})
            self.assertEqual(self._get(sid).json()["status"], "failed", pid)


class ExpiryTests(ExportTestBase):
    def _backdate(self, sid, hours):
        bundle = self._bundle(sid)
        state = VE._read_state(bundle)
        state["created_at"] = VE._iso(VE._now() - __import__("datetime").timedelta(hours=hours))
        VE._write_state(bundle, state)

    def test_expires_after_24h_then_removed(self):
        sid = self._create_ready()
        self._backdate(sid, 23)
        self.assertEqual(self._get(sid).json()["status"], "ready")

        self._backdate(sid, 25)
        st = self._get(sid).json()
        self.assertEqual(st["status"], "expired")
        self.assertEqual(st["files"], [])
        self.assertIsNone(st["manifest"])
        self.assertEqual({p.name for p in self._bundle(sid).iterdir()}, {"state.json"})
        r = self._download(sid, "db.sqlite3.gz")
        self.assertEqual(r.status_code, 409)

        self._backdate(sid, 49)
        self.assertEqual(self._get(sid).status_code, 404)
        self.assertFalse(self._bundle(sid).exists())

    def test_create_sweeps_expired_bundles(self):
        sid = self._create_ready()
        self._backdate(sid, 80)
        new = self._create()
        self.assertEqual(new.status_code, 202)
        _wait_done(new.json()["id"])
        self.assertFalse(self._bundle(sid).exists())

    def test_expired_can_be_deleted(self):
        sid = self._create_ready()
        self._backdate(sid, 30)
        self.assertEqual(self._get(sid).json()["status"], "expired")
        self.assertEqual(self.client.delete(f"{BASE}{sid}/", **_auth(self.admin_key)).status_code, 204)
        self.assertFalse(self._bundle(sid).exists())


class PathTraversalTests(ExportTestBase):
    def test_bad_ids_404(self):
        sid = self._create_ready()
        for bad in ("..", "not-a-uuid", "%2e%2e%2f%2e%2e%2fetc", sid.upper(), "{" + sid + "}", sid.replace("-", "")):
            self.assertEqual(self._get(bad).status_code, 404, bad)
            self.assertEqual(self._download(bad, "manifest.json").status_code, 404, bad)
            self.assertEqual(self.client.delete(f"{BASE}{bad}/", **_auth(self.admin_key)).status_code, 404, bad)
        self.assertTrue(self._bundle(sid).exists())  # 정상 번들은 영향 없음
        with self.assertRaises(VE.ExportError):
            VE._parse_id("../x")

    def test_download_name_allowlist(self):
        sid = self._create_ready()
        for bad in ("state.json", "config.tar", "config.tar.enc", "work", "..", "db.sqlite3",
                    "db.sqlite3.zst", "%2e%2e%2fstate.json", "..%2fstate.json", "manifest.json.partial",
                    "MANIFEST.JSON"):
            r = self._download(sid, bad)
            self.assertEqual(r.status_code, 404, bad)
        # 슬래시 포함 경로 (URL 단계에서 매칭 실패)
        r = self.client.get(f"{BASE}{sid}/download/../state.json/", **_auth(self.admin_key))
        self.assertEqual(r.status_code, 404)
        r = self.client.get(f"{BASE}{sid}/download/a/b/", **_auth(self.admin_key))
        self.assertEqual(r.status_code, 404)

    def test_symlink_in_bundle_not_served(self):
        sid = self._create_ready()
        secret = self.tmp / "outside.txt"
        secret.write_text("outside")
        target = self._bundle(sid) / "manifest.json"
        target.unlink()
        target.symlink_to(secret)
        self.assertEqual(self._download(sid, "manifest.json").status_code, 404)


class DownloadAuditTests(ExportTestBase):
    """Range 구간 요청(start != 0)도 감사에 남아야 한다 (감사 누락 회귀 방지)."""

    def _dl_logs(self):
        return ActivityLog.objects.filter(target_name__contains="download db.sqlite3.gz")

    def test_non_zero_range_is_audited_once_per_ttl(self):
        sid = self._create_ready()
        self.assertEqual(self._dl_logs().count(), 0)
        with self.assertLogs("apps.api_v2.export", level="INFO") as cm:
            r1 = self._download(sid, "db.sqlite3.gz", HTTP_RANGE="bytes=1-")
            b"".join(r1.streaming_content)
            r2 = self._download(sid, "db.sqlite3.gz", HTTP_RANGE="bytes=5-9")
            b"".join(r2.streaming_content)
        self.assertEqual(r1.status_code, 206)
        # ActivityLog: 시작 오프셋이 0 이 아니어도 최초 1회 기록, 같은 TTL 안의 반복 요청은 중복 기록 없음
        self.assertEqual(self._dl_logs().count(), 1)
        self.assertIn("range=1-", self._dl_logs().first().detail)
        # logger: 모든 구간 요청이 기록
        lines = [l for l in cm.output if "download db.sqlite3.gz" in l]
        self.assertEqual(len(lines), 2)
        self.assertIn("range=5-9/", lines[1])

    def test_audit_again_after_ttl_and_for_other_key(self):
        sid = self._create_ready()
        b"".join(self._download(sid, "db.sqlite3.gz", HTTP_RANGE="bytes=1-").streaming_content)
        self.assertEqual(self._dl_logs().count(), 1)
        cache.clear()  # TTL 만료 모사
        b"".join(self._download(sid, "db.sqlite3.gz", HTTP_RANGE="bytes=2-").streaming_content)
        self.assertEqual(self._dl_logs().count(), 2)
        # start==0 요청은 항상 기록
        b"".join(self._download(sid, "db.sqlite3.gz", HTTP_RANGE="bytes=0-9").streaming_content)
        b"".join(self._download(sid, "db.sqlite3.gz").streaming_content)
        self.assertEqual(self._dl_logs().count(), 4)


class NoPlaintextLeakTests(ExportTestBase):
    def test_key_plaintext_not_exposed_anywhere(self):
        with self.assertLogs("apps.api_v2.export", level="DEBUG") as cm:
            sid = self._create_ready()
            r1 = self._get(sid)
            r2 = self._download(sid, "manifest.json")
            mbytes = b"".join(r2.streaming_content)
            r3 = self.client.delete(f"{BASE}{sid}/", **_auth(self.admin_key))
            bad = self._create(key=self._issue(self.ops, [S.EXPORT_READ], name="ops-leak"))
        corpus = [r1.content, mbytes, r3.content, bad.content, "\n".join(cm.output).encode()]
        corpus += [f"{a.target_name} {a.detail}".encode() for a in ActivityLog.objects.all()]
        self.assertTrue(ActivityLog.objects.filter(target_name__contains="DB 내보내기 API").exists())
        for pt in self.plaintexts:
            secret_part = pt.split("_", 2)[2]  # 평문의 비밀 부분
            for blob in corpus:
                self.assertNotIn(pt.encode(), blob)
                self.assertNotIn(secret_part.encode(), blob)
        # 번들 안에도 없다
        for name in ("manifest.json", "state.json"):
            p = self._bundle(sid) / name
            if p.exists():
                for pt in self.plaintexts:
                    self.assertNotIn(pt.encode(), p.read_bytes())


class MigrationSafetyTests(TestCase):
    def test_no_model_changes_pending(self):
        out = io.StringIO()
        try:
            call_command("makemigrations", check=True, dry_run=True, stdout=out, stderr=out)
        except SystemExit as e:  # 변경이 있으면 SystemExit(1)
            self.fail(f"새 마이그레이션이 필요합니다 — 모델/choices 변경 금지: {out.getvalue()}")


class RangeParseHardeningTests(TestCase):
    """리뷰(2026-10-09): 수천 자리 숫자 Range 가 500 이 아니라 416(invalid) 이어야 한다."""

    def test_huge_digit_range_is_invalid_not_error(self):
        for header in ("bytes=" + "9" * 5000 + "-", "bytes=0-" + "9" * 5000, "bytes=-" + "9" * 5000):
            with self.subTest(len=len(header)):
                self.assertEqual(VE._parse_range(header, 10), "invalid")

    def test_normal_ranges_still_parse(self):
        self.assertEqual(VE._parse_range("bytes=0-3", 10), (0, 3))
        self.assertEqual(VE._parse_range("bytes=5-", 10), (5, 9))
        self.assertEqual(VE._parse_range("bytes=-4", 10), (6, 9))
