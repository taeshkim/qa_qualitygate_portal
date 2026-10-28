"""서버(`/api/v2/export/`) ↔ 클라이언트(`fetch_snapshot_via_api`) 실구현 왕복 통합 테스트.

가짜 서버(tests_fetch_api.py)가 아니라 **실제 Django 뷰**(apps.api_v2.views_export)를 `LiveServerTestCase`
의 실제 HTTP 서버로 띄우고, 클라이언트 명령을 거기에 붙인다.

    create(POST) → 폴링 → manifest/DB 다운로드(Range 재개) → sha256 → 번들 배치
      → import_snapshot --target-db <임시> --yes → verify_snapshot (종료코드 0)

- 운영 DB(db.sqlite3)·원본 서버(qualitygate.jkintra.net)는 건드리지 않는다. 내보내기 대상 DB / 번들 디렉터리 /
  import 대상 DB 는 모두 mktemp 임시 경로이며 `SYNCOPS_EXPORT_DB_PATH` / `SYNCOPS_EXPORT_DIR` 설정으로 주입한다.
- API Key 는 `ApiKey.issue` 로 그때그때 발급하고 평문은 변수/환경변수로만 쓴다 (출력·하드코딩 없음).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import LiveServerTestCase, override_settings

from apps.activitylog.models import ActivityLog
from apps.api_v2 import scopes as S
from apps.api_v2 import views_export as VE
from apps.api_v2.models import ApiKey
from apps.syncops import snapshot as sn
from apps.syncops import tests as base
from apps.syncops.management.commands import fetch_snapshot_via_api as fetch

skip_no_zstd = unittest.skipUnless(base.HAS_ZSTD, "zstd 바이너리 없음")

KEY_ENV = "QG_TEST_ROUNDTRIP_KEY"
API = "/api/v2/export/snapshots/"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BLOB_BYTES = 4 * 1024 * 1024  # 압축이 안 되는 랜덤 블롭 — 다운로드가 여러 청크/여러 번의 Range 로 나뉘게 한다


def _add_blob_table(path: str, nbytes: int = BLOB_BYTES) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, data BLOB)")
        step = 256 * 1024
        for _ in range(nbytes // step):
            conn.execute("INSERT INTO blobs (data) VALUES (?)", (os.urandom(step),))
        conn.commit()
    finally:
        conn.close()


def _wait_workers(timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(t.is_alive() for t in list(VE._ACTIVE.values())):
            return
        time.sleep(0.02)
    raise AssertionError("export 워커가 제한 시간 안에 끝나지 않았습니다.")


class RoundTripBase(LiveServerTestCase):
    """실제 HTTP 서버 + 임시 원본 DB/번들 디렉터리 + export:read 키 (admin 소유)."""

    @classmethod
    def setUpClass(cls):
        # 라이브 서버는 localhost 로 접근한다 — Host 검증 통과용 (tests_export 와 동일 방식)
        cls._ah = override_settings(ALLOWED_HOSTS=["*"])
        cls._ah.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._ah.disable()

    def setUp(self):
        cache.clear()  # throttle 카운터 초기화
        self.tmp = Path(tempfile.mkdtemp(prefix="qgp-roundtrip-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src_db = Path(base.make_db(self.tmp / "source.sqlite3"))  # items 40 / notes 7 / empty_tbl 0 (BLOB·한글·NULL)
        _add_blob_table(str(self.src_db))
        self.srv_root = self.tmp / "server-snapshots"      # 서버 쪽 번들 위치 (SYNCOPS_EXPORT_DIR)
        self.client_out = self.tmp / "client-out"          # 클라이언트 --out-dir
        ov = override_settings(SYNCOPS_EXPORT_DB_PATH=str(self.src_db), SYNCOPS_EXPORT_DIR=str(self.srv_root))
        ov.enable()
        self.addCleanup(ov.disable)
        self.addCleanup(self._join_workers)

        User = get_user_model()
        self.admin = User.objects.create_user(username="rt_admin", password="pw", role="qpe_admin")
        self.ops = User.objects.create_user(username="rt_ops", password="pw", role="qpe_ops")
        _, self.key = ApiKey.issue(user=self.admin, name="rt-export", scopes=[S.EXPORT_READ])
        self.plaintexts = [self.key]

        # 클라이언트 재시도 대기 제거 (테스트 속도)
        p = mock.patch.object(fetch, "RETRY_BACKOFF", 0)
        p.start()
        self.addCleanup(p.stop)

    def _join_workers(self):
        for t in list(VE._ACTIVE.values()):
            t.join(timeout=30)

    # --- 헬퍼 ----------------------------------------------------------
    def fetch(self, *extra, key=None, out=None, poll="0.05", timeout="60", compress="gzip") -> base.Result:
        """fetch_snapshot_via_api 실행. 키는 환경변수로만 전달한다."""
        args = ["--url", self.live_server_url, "--key-env", KEY_ENV,
                "--out-dir", str(out or self.client_out),
                "--compress", compress, "--poll-interval", poll, "--timeout", timeout, *extra]
        with mock.patch.dict(os.environ, {KEY_ENV: key or self.key}):
            return base.run_cmd("fetch_snapshot_via_api", *args)

    def bundles(self) -> list[Path]:
        if not self.client_out.is_dir():
            return []
        return sorted(p for p in self.client_out.iterdir() if p.name.startswith("qg-"))

    def only_bundle(self) -> Path:
        b = self.bundles()
        self.assertEqual(len(b), 1, f"번들 디렉터리 수: {b}")
        return b[0]

    def import_and_verify(self, bundle: Path) -> Path:
        """import_snapshot --target-db <임시> --yes → verify_snapshot. 복원 DB 경로 반환."""
        target = self.tmp / "restored.sqlite3"
        # 클라이언트 서버가 '다른 서버' 인 것처럼 — 같은 호스트/저장소 가드(same_host_conflict) 우회가 아니라
        # 새 서버 상황을 모사한다. 서비스 포트 점검은 비어 있는 임시 포트로.
        with mock.patch("apps.syncops.snapshot.socket.gethostname", return_value="new-server.example"):
            r = base.run_cmd("import_snapshot", bundle, "--target-db", target, "--yes",
                             "--service-port", base.free_port())
        self.assertEqual(r.rc, 0, r.text)
        self.assertIn("migrate 자동 생략", r.text)  # 임시 target → migrate 는 자동으로 생략
        v = base.run_cmd("verify_snapshot", bundle, "--target-db", target)
        self.assertEqual(v.rc, 0, v.text)
        self.assertIn("검증 OK", v.out)
        return target

    def assert_no_plaintext(self, *texts):
        """키 평문/비밀 부분이 어디에도 없어야 한다 (출력, 클라이언트 번들, 서버 번들, 활동 로그)."""
        needles = []
        for pt in self.plaintexts:
            needles.append(pt)
            needles.append(pt.rsplit("_", 1)[-1])  # secret(hex) 부분
        needles = [n for n in needles if len(n) >= 16]
        for t in texts:
            for n in needles:
                self.assertNotIn(n, t or "")
        for root in (self.client_out, self.srv_root):
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.stat().st_size < 1024 * 1024:  # 작은 파일(manifest/state/.json)만 본다
                    data = p.read_bytes()
                    for n in needles:
                        self.assertNotIn(n.encode(), data, f"{p.name} 에 키가 남아 있음")
        dump = " ".join(f"{a.target_name} {a.detail}" for a in ActivityLog.objects.all())
        for n in needles:
            self.assertNotIn(n, dump)

    def req(self, method, path, key=None, **kw):
        return requests.request(method, f"{self.live_server_url}{path}",
                                headers={"Authorization": f"Bearer {key or self.key}", **kw.pop("headers", {})},
                                timeout=30, **kw)


# ---------------------------------------------------------------------------
# 정상 흐름
# ---------------------------------------------------------------------------

class RoundTripHappyPathTests(RoundTripBase):
    def test_full_roundtrip_gzip(self):
        r = self.fetch()
        self.assertEqual(r.rc, 0, r.text)
        self.assertIn("[fetch 5/5]", r.out)
        self.assertIn("sha256 OK", r.out)

        bundle = self.only_bundle()
        self.assertEqual(sorted(p.name for p in bundle.iterdir()), ["db.sqlite3.gz", "manifest.json"])  # config 없음
        self.assertFalse(list(self.client_out.glob(".fetch-*")))  # 스테이징 정리됨
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)
        for p in bundle.iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600, p.name)

        manifest = sn.load_manifest(bundle)
        self.assertIsNone(manifest.get("config"))
        self.assertEqual(manifest["db"]["file"], "db.sqlite3.gz")
        self.assertEqual(base.sha256_file(bundle / "db.sqlite3.gz"), manifest["db"]["file_sha256"])
        self.assertEqual(manifest["tables"]["items"], 40)
        self.assertEqual(manifest["tables"]["notes"], 7)
        self.assertEqual(manifest["tables"]["empty_tbl"], 0)
        self.assertEqual(manifest["tables"]["blobs"], BLOB_BYTES // (256 * 1024))

        # 원격은 기본적으로 DELETE — 서버 번들 디렉터리가 사라지고 목록에 없어야 한다
        self.assertEqual(list(self.srv_root.glob("api-*")), [])

        # import → verify (종료코드 0) + 내용 동일
        target = self.import_and_verify(bundle)
        self.assertEqual(base.dump_db(target), base.dump_db(self.src_db))
        self.assert_no_plaintext(r.text)

    def test_cli_api_bundle_served_by_api_and_fetched(self):
        """서비스를 멈춘 채 `export_snapshot --api-bundle` 로 만든 번들을 API 가 그대로 내려준다 (쓰기 간섭 회피 경로)."""
        r = base.run_cmd("export_snapshot", "--api-bundle", "--compress", "gzip", "--db-path", self.src_db)
        self.assertEqual(r.rc, 0, r.text)
        ids = UUID_RE.findall(r.out)
        self.assertTrue(ids, r.out)
        bundle = self.srv_root / f"api-{ids[0]}"
        self.assertEqual(sorted(p.name for p in bundle.iterdir()), ["db.sqlite3.gz", "manifest.json", "state.json"])
        self.assertIsNone(sn.load_manifest(bundle).get("config"))  # config 는 절대 포함 안 됨
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)

        r = self.fetch("--existing-id", ids[0])
        self.assertEqual(r.rc, 0, r.text)
        self.assertIn("sha256 OK", r.out)
        got = self.only_bundle()
        target = self.import_and_verify(got)
        self.assertEqual(base.dump_db(target), base.dump_db(self.src_db))

    def test_cli_api_bundle_rejects_incompatible_options(self):
        for extra in (("--out", str(self.tmp / "x")), ("--passphrase-env", "X"), ("--compress", "none")):
            r = base.run_cmd("export_snapshot", "--api-bundle", "--db-path", self.src_db, *extra)
            self.assertNotEqual(r.rc, 0, extra)
        self.assertEqual(list(self.srv_root.glob("api-*")), [])

    @skip_no_zstd
    def test_full_roundtrip_zstd(self):
        r = self.fetch(compress="zstd")
        self.assertEqual(r.rc, 0, r.text)
        bundle = self.only_bundle()
        self.assertTrue((bundle / "db.sqlite3.zst").is_file())
        target = self.import_and_verify(bundle)
        self.assertEqual(base.count_rows(target), base.count_rows(self.src_db))

    def test_compress_auto_default_roundtrip(self):
        r = self.fetch(compress="auto")
        self.assertEqual(r.rc, 0, r.text)
        bundle = self.only_bundle()
        self.import_and_verify(bundle)

    def test_keep_remote_then_manual_delete(self):
        r = self.fetch("--keep-remote")
        self.assertEqual(r.rc, 0, r.text)
        sid = UUID_RE.search(r.out).group(0)
        self.assertTrue((self.srv_root / f"api-{sid}").is_dir())
        st = self.req("GET", f"{API}{sid}/")
        self.assertEqual(st.status_code, 200)
        self.assertEqual(st.json()["status"], "ready")
        self.assertEqual([f["name"] for f in st.json()["files"]], ["manifest.json", "db.sqlite3.gz"])
        self.assertEqual(self.req("DELETE", f"{API}{sid}/").status_code, 204)
        self.assertEqual(self.req("GET", f"{API}{sid}/").status_code, 404)

    def test_dry_run_sends_nothing(self):
        before = len(list(self.srv_root.glob("api-*"))) if self.srv_root.is_dir() else 0
        r = self.fetch("--dry-run")
        self.assertEqual(r.rc, 0, r.text)
        after = len(list(self.srv_root.glob("api-*"))) if self.srv_root.is_dir() else 0
        self.assertEqual(before, after)
        self.assertFalse(self.client_out.exists())

    def test_existing_bundle_not_clobbered(self):
        """같은 원격 스냅샷(=같은 manifest.created_at)을 다시 받으면 기존 번들을 덮어쓰지 않고 실패."""
        r1 = self.fetch("--keep-remote")
        self.assertEqual(r1.rc, 0, r1.text)
        sid = UUID_RE.search(r1.out).group(0)
        bundle = self.only_bundle()
        before = base.sha256_file(bundle / "manifest.json")
        r2 = self.fetch("--existing-id", sid, "--keep-remote")
        self.assertNotEqual(r2.rc, 0)
        self.assertIn("이미 같은 번들 디렉터리", r2.text)
        self.assertEqual(base.sha256_file(bundle / "manifest.json"), before)


# ---------------------------------------------------------------------------
# 실제 HTTP 계약 (클라이언트가 의존하는 헤더/상태 코드) — 실서버 스택(WSGI) 기준
# ---------------------------------------------------------------------------

class RoundTripHttpContractTests(RoundTripBase):
    def _ready(self) -> str:
        resp = self.req("POST", API, json={"compress": "gzip"})
        self.assertEqual(resp.status_code, 202, resp.text)
        sid = resp.json()["id"]
        _wait_workers()
        st = self.req("GET", f"{API}{sid}/").json()
        self.assertEqual(st["status"], "ready", st)
        return sid

    def test_range_and_headers_over_real_http(self):
        sid = self._ready()
        path = self.srv_root / f"api-{sid}" / "db.sqlite3.gz"
        data = path.read_bytes()
        url = f"{API}{sid}/download/db.sqlite3.gz/"

        full = self.req("GET", url)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, data)
        self.assertEqual(full.headers["Content-Length"], str(len(data)))
        self.assertEqual(full.headers["Accept-Ranges"], "bytes")
        self.assertEqual(full.headers["X-Content-SHA256"], base.sha256_file(path))
        self.assertTrue(full.headers["ETag"])

        part = self.req("GET", url, headers={"Range": "bytes=10-19"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.content, data[10:20])
        self.assertEqual(part.headers["Content-Range"], f"bytes 10-19/{len(data)}")
        self.assertEqual(part.headers["Content-Length"], "10")

        tail = self.req("GET", url, headers={"Range": f"bytes={len(data) - 5}-"})
        self.assertEqual(tail.status_code, 206)
        self.assertEqual(tail.content, data[-5:])

        bad = self.req("GET", url, headers={"Range": f"bytes={len(data)}-"})
        self.assertEqual(bad.status_code, 416)
        self.assertEqual(bad.headers["Content-Range"], f"bytes */{len(data)}")
        self.assertEqual(self.req("GET", url, headers={"Range": "bytes=0-1,5-6"}).status_code, 416)

        # manifest.json 의 sha 헤더 == 상태 응답 files[].sha256 == 실제 파일 sha
        mf = self.req("GET", f"{API}{sid}/download/manifest.json/")
        self.assertEqual(mf.status_code, 200)
        files = {f["name"]: f for f in self.req("GET", f"{API}{sid}/").json()["files"]}
        self.assertEqual(mf.headers["X-Content-SHA256"], files["manifest.json"]["sha256"])
        self.assertEqual(files["db.sqlite3.gz"]["sha256"], base.sha256_file(path))
        self.assertEqual(files["db.sqlite3.gz"]["size"], len(data))

    def test_download_not_allowed_names_404_and_not_ready_409(self):
        sid = self._ready()
        for name in ("state.json", "config.tar", "..%2Fstate.json", "db.sqlite3"):
            self.assertEqual(self.req("GET", f"{API}{sid}/download/{name}/").status_code, 404, name)
        # 허용 이름이지만 이 번들엔 없는 파일(.zst) 도 404
        self.assertEqual(self.req("GET", f"{API}{sid}/download/db.sqlite3.zst/").status_code, 404)

    def test_config_field_rejected_400_unsupported(self):
        for body in ({"include_config": True}, {"config": True}, {"compress": "gzip", "foo": 1}):
            resp = self.req("POST", API, json=body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertEqual(resp.json()["error"]["code"], "unsupported", body)
        self.assertFalse(self.srv_root.is_dir() and list(self.srv_root.glob("api-*")))


# ---------------------------------------------------------------------------
# 인증/권한 실패 — 클라이언트가 실서버 오류를 올바로 보고하는가
# ---------------------------------------------------------------------------

class RoundTripAuthFailureTests(RoundTripBase):
    def _assert_failed_clean(self, r, *needles):
        self.assertNotEqual(r.rc, 0)
        for n in needles:
            self.assertIn(n, r.text)
        self.assertEqual(self.bundles(), [])
        self.assertFalse(self.srv_root.is_dir() and list(self.srv_root.glob("api-*")))

    def test_wildcard_scope_key_rejected_with_scope_hint(self):
        _, wild = ApiKey.issue(user=self.admin, name="rt-wild", scopes=[S.WILDCARD])
        self.plaintexts.append(wild)
        r = self.fetch(key=wild)
        self._assert_failed_clean(r, "HTTP 403", "scope_required", "export:read")
        self.assert_no_plaintext(r.text)

    def test_non_admin_owner_rejected_role_denied(self):
        _, k = ApiKey.issue(user=self.ops, name="rt-ops", scopes=[S.EXPORT_READ])
        self.plaintexts.append(k)
        r = self.fetch(key=k)
        self._assert_failed_clean(r, "HTTP 403", "role_denied")
        self.assert_no_plaintext(r.text)

    def test_demoted_owner_rejected(self):
        self.admin.role = "qpe_ops"
        self.admin.save()
        r = self.fetch()
        self._assert_failed_clean(r, "HTTP 403", "role_denied")

    def test_garbage_key_401(self):
        r = self.fetch(key="qgp_notakey_" + "0" * 40)
        self._assert_failed_clean(r, "HTTP 401")


# ---------------------------------------------------------------------------
# 진행 중 / 동시성 / --existing-id 재개
# ---------------------------------------------------------------------------

class RoundTripResumeTests(RoundTripBase):
    def _failing_cut(self, limit: int):
        """DB 파일 응답을 요청당 limit 바이트만 보내고 연결을 끊는다 (WSGI 이터레이터 조기 종료 = 끊김).

        반환: (patcher, calls) — calls 는 (이름, start, length) 기록. limit=None 이면 정상 동작.
        """
        real = VE._iter_file
        calls: list[tuple[str, int, int]] = []
        state = {"limit": limit}

        def flaky(path, start, length):
            calls.append((path.name, start, length))
            if state["limit"] and path.name.startswith("db.sqlite3"):
                sent = 0
                for chunk in real(path, start, length):
                    room = state["limit"] - sent
                    if len(chunk) >= room:
                        yield chunk[:room]
                        return  # Content-Length 보다 적게 보내고 멈춘다 → 클라이언트는 READ_TIMEOUT 뒤 불완전 수신으로 본다
                    sent += len(chunk)
                    yield chunk
            else:
                yield from real(path, start, length)

        # Django 라이브 서버는 HTTP/1.1 keep-alive 라 조기 종료해도 소켓을 닫지 않는다 — 클라이언트의 읽기
        # 타임아웃(기본 60초)을 줄여 '멈춘 연결 → 재시도(Range)' 경로를 빠르게 태운다.
        p = mock.patch.multiple(VE, _iter_file=flaky)
        t = mock.patch.object(fetch, "READ_TIMEOUT", 1)
        self.addCleanup(t.stop)
        t.start()
        return p, calls, state

    def test_resume_with_existing_id_after_interrupted_download(self):
        p, calls, state = self._failing_cut(limit=400 * 1024)
        start_spy = mock.patch.object(VE, "_start_export", wraps=VE._start_export)
        with p, start_spy as starts:
            # 1) 요청당 400KiB 만 주고 끊기는 서버 — 재시도(최대 5회)를 소진하고 실패해야 한다
            r1 = self.fetch("--keep-remote")
            self.assertNotEqual(r1.rc, 0, r1.text)
            sid = UUID_RE.search(r1.text).group(0)
            self.assertIn("--existing-id " + sid, r1.text)
            self.assertIn("원격 snapshot id", r1.text)
            self.assertEqual(self.bundles(), [])  # 미완성 번들은 만들지 않는다

            stage = self.client_out / f".fetch-{sid}"
            self.assertTrue((stage / "manifest.json").is_file())  # manifest 는 이미 받음
            part = stage / "db.sqlite3.gz.part"
            self.assertTrue(part.is_file())
            have = part.stat().st_size
            total = (self.srv_root / f"api-{sid}" / "db.sqlite3.gz").stat().st_size
            self.assertTrue(0 < have < total, (have, total))
            # 원격은 보존되어 있어야 한다
            self.assertEqual(self.req("GET", f"{API}{sid}/").json()["status"], "ready")
            self.assertEqual(starts.call_count, 1)

            # 한 번의 run 안에서도 Range 로 이어받았는지 (첫 요청 start=0, 이후 start>0 이 늘어남)
            starts_seen = [c[1] for c in calls if c[0].startswith("db.sqlite3")]
            self.assertEqual(starts_seen[0], 0)
            self.assertEqual(starts_seen, sorted(starts_seen))
            self.assertTrue(all(s > 0 for s in starts_seen[1:]))

            # 2) 끊김 해소 후 --existing-id 로 재개 — POST 없이 .part 에서 이어받아야 한다
            state["limit"] = None
            calls.clear()
            r2 = self.fetch("--existing-id", sid)
            self.assertEqual(r2.rc, 0, r2.text)
            self.assertEqual(starts.call_count, 1)  # 새 스냅샷을 만들지 않았다
            db_calls = [c for c in calls if c[0].startswith("db.sqlite3")]
            self.assertEqual(db_calls[0][1], have)  # 이어받기 시작 오프셋 == 남아 있던 .part 크기
            self.assertEqual(db_calls[0][1] + db_calls[0][2], total)

        bundle = self.only_bundle()
        self.assertEqual(base.sha256_file(bundle / "db.sqlite3.gz"), sn.load_manifest(bundle)["db"]["file_sha256"])
        self.assertFalse(list(self.client_out.glob(".fetch-*")))
        self.assertEqual(list(self.srv_root.glob("api-*")), [])  # 성공 후 원격 DELETE
        target = self.import_and_verify(bundle)
        self.assertEqual(base.dump_db(target), base.dump_db(self.src_db))
        self.assert_no_plaintext(r1.text, r2.text)

    def test_transient_cut_recovers_within_single_run(self):
        """요청당 끊기지만 재시도 한도 안에서 끝나는 크기 — 한 번의 실행으로 Range 이어받기로 완주."""
        p, calls, state = self._failing_cut(limit=1536 * 1024)
        with p:
            r = self.fetch()
            self.assertEqual(r.rc, 0, r.text)
        db_starts = [c[1] for c in calls if c[0].startswith("db.sqlite3")]
        self.assertGreaterEqual(len(db_starts), 2)  # 최소 한 번은 Range 재요청
        self.assertEqual(db_starts[0], 0)
        self.assertTrue(all(s > 0 for s in db_starts[1:]))
        bundle = self.only_bundle()
        target = self.import_and_verify(bundle)
        self.assertEqual(base.dump_db(target), base.dump_db(self.src_db))

    def test_resume_running_snapshot_after_timeout_and_conflict_while_running(self):
        """원격 export 가 아직 running 일 때: 타임아웃 실패 → 새 생성은 409 → 끝난 뒤 --existing-id 로 완주."""
        gate = threading.Event()
        real_export = sn.export_snapshot

        def gated(*a, **kw):
            gate.wait(30)
            return real_export(*a, **kw)

        self.addCleanup(gate.set)
        with mock.patch.object(sn, "export_snapshot", gated):
            r1 = self.fetch(poll="0.05", timeout="0.4")
            self.assertNotEqual(r1.rc, 0, r1.text)
            self.assertIn("[poll] running", r1.out)
            self.assertIn("--timeout", r1.text)
            sid = UUID_RE.search(r1.text).group(0)
            self.assertIn("--existing-id " + sid, r1.text)
            self.assertEqual(self.req("GET", f"{API}{sid}/").json()["status"], "running")

            # 실행 중에는 두 번째 생성이 409 export_in_progress — 클라이언트가 안내와 함께 실패
            r2 = self.fetch(timeout="5")
            self.assertNotEqual(r2.rc, 0)
            self.assertIn("export_in_progress", r2.text)
            self.assertEqual(len(list(self.srv_root.glob("api-*"))), 1)

            # 실행 중 DELETE 는 409
            self.assertEqual(self.req("DELETE", f"{API}{sid}/").status_code, 409)

            gate.set()
            _wait_workers()

        r3 = self.fetch("--existing-id", sid)
        self.assertEqual(r3.rc, 0, r3.text)
        bundle = self.only_bundle()
        self.import_and_verify(bundle)
        self.assert_no_plaintext(r1.text, r2.text, r3.text)

    def test_resume_existing_id_not_found_404(self):
        r = self.fetch("--existing-id", "00000000-0000-4000-8000-000000000000")
        self.assertNotEqual(r.rc, 0)
        self.assertIn("원격 스냅샷이 없습니다", r.text)
        self.assertEqual(self.bundles(), [])

    def test_failed_export_reported(self):
        """서버 export 가 실패하면(failed) 클라이언트가 원격 error 를 보고하고 번들을 만들지 않는다."""
        self.src_db.write_bytes(b"this is not a sqlite database" * 100)
        with self.assertLogs("apps.api_v2.export", level="ERROR"):  # 서버의 예상된 실패 로그 (콘솔 노이즈 방지)
            r = self.fetch(timeout="20")
        self.assertNotEqual(r.rc, 0)
        self.assertEqual(self.bundles(), [])
        self.assertIn("원격 export 실패", r.text)
        sid = UUID_RE.search(r.text).group(0)
        self.assertIn("--existing-id " + sid, r.text)

    def test_source_db_missing_503(self):
        self.src_db.unlink()
        r = self.fetch()
        self.assertNotEqual(r.rc, 0)
        self.assertIn("HTTP 503", r.text)
        self.assertIn("export_unavailable", r.text)
