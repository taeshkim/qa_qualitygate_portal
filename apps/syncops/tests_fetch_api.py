"""apps.syncops `fetch_snapshot_via_api` 테스트.

원본 서버(qualitygate.jkintra.net)에는 어떤 요청도 보내지 않는다 — 표준 라이브러리 http.server 로 만든
**로컬 가짜 Export API 서버**(127.0.0.1, 스레드)만 상대한다. DB 는 tempfile 임시 DB 뿐(운영 db.sqlite3 미접근).
키는 테스트마다 런타임에 무작위로 만들고 어디에도 출력/저장하지 않는다 (실제 키 아님).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import requests
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from apps.syncops import tests as base
from apps.syncops.management.commands import fetch_snapshot_via_api as fetch

KEY_ENV = "QG_TEST_FETCH_KEY"
API = "/api/v2/export/snapshots/"


class FakeState:
    """가짜 서버의 동작 스위치 + 수신 요청 기록."""

    def __init__(self, files: dict[str, bytes], key: str, prefix: str = ""):
        self.files = files
        self.key = key
        self.prefix = prefix
        self.sid = str(uuid.uuid4())
        self.running_polls = 1          # ready 가 되기 전 'running' 으로 응답할 상태 조회 횟수
        self.status_5xx = 0             # 처음 N 번 상태 조회를 503 으로
        self.final_status = "ready"     # ready | failed | expired
        self.post_error = None          # (status, code, message)
        self.download_error = None      # (status, code, message) — 다운로드 요청에 강제
        self.truncate_once = None       # 이 파일의 첫 전체 다운로드를 절반만 보내고 연결 종료
        self.ignore_range = False       # Range 무시하고 항상 200 전체
        self.sha_header = None          # X-Content-SHA256 헤더를 이 값으로 덮어씀 (dict name->sha 또는 str)
        self.corrupt = None             # 이 파일 본문의 첫 바이트를 바꿔서 보냄 (헤더 sha 는 원본 기준)
        self.redirect_from = None       # 이 prefix 로 오는 요청을 302 로 redirect_to 에 보냄
        self.redirect_to = None
        self.polls = 0
        self.truncated = False
        self.deleted = False
        self.log: list[dict] = []

    def reset_log(self):
        self.log.clear()


def _handler_for(state: FakeState):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # 조용히
            pass

        # --- 공통 -----------------------------------------------------
        def _record(self):
            state.log.append({
                "method": self.command, "path": self.path, "range": self.headers.get("Range"),
                "auth_ok": self.headers.get("Authorization") == f"Bearer {state.key}",
                "host": self.headers.get("Host"),
            })

        def _send(self, status, body=b"", headers=None, ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _err(self, status, code, message):
            self._send(status, json.dumps({"error": {"code": code, "message": message}}).encode())

        def _route(self):
            """prefix 를 벗기고 (kind, name) 반환. 못 찾으면 None."""
            path = self.path.split("?")[0]
            if state.redirect_from and path.startswith(state.redirect_from):
                self._send(302, b"", {"Location": state.redirect_to + path[len(state.redirect_from):]})
                return "redirected"
            if not path.startswith(state.prefix + API):
                return None
            rest = path[len(state.prefix + API):]
            if rest == "":
                return ("collection", None)
            m = re.fullmatch(r"([0-9a-f-]{36})/", rest)
            if m:
                return ("snapshot", m.group(1))
            m = re.fullmatch(r"([0-9a-f-]{36})/download/([^/]+)/", rest)
            if m:
                return ("download", (m.group(1), m.group(2)))
            return None

        def _auth(self):
            if self.headers.get("Authorization") != f"Bearer {state.key}":
                self._err(401, "unauthorized", "invalid key")
                return False
            return True

        # --- 메서드 ----------------------------------------------------
        def do_POST(self):
            self._record()
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            route = self._route()
            if route == "redirected":
                return
            if route != ("collection", None):
                return self._err(404, "not_found", "nope")
            if state.post_error:
                return self._err(*state.post_error)
            if not self._auth():
                return
            self._send(202, json.dumps({"id": state.sid, "status": "running",
                                        "created_at": "2026-01-01T00:00:00Z"}).encode())

        def do_GET(self):
            self._record()
            route = self._route()
            if route == "redirected":
                return
            if not route:
                return self._err(404, "not_found", "nope")
            if not self._auth():
                return
            kind, val = route
            if kind == "snapshot":
                return self._status(val)
            if kind == "download":
                return self._download(*val)
            self._err(404, "not_found", "nope")

        def do_DELETE(self):
            self._record()
            route = self._route()
            if route == "redirected":
                return
            if not route or route[0] != "snapshot" or not self._auth():
                return self._err(404, "not_found", "nope")
            state.deleted = True
            self._send(204)

        # --- 구현 ------------------------------------------------------
        def _status(self, sid):
            if sid != state.sid:
                return self._err(404, "not_found", "no such snapshot")
            if state.status_5xx > 0:
                state.status_5xx -= 1
                return self._err(503, "unavailable", "try later")
            state.polls += 1
            created = "2026-01-01T00:00:00Z"
            if state.final_status == "failed":
                return self._send(200, json.dumps({
                    "id": sid, "status": "failed", "progress_pct": None, "created_at": created,
                    "finished_at": created, "error": "disk full", "manifest": None, "files": []}).encode())
            if state.final_status == "expired":
                return self._send(200, json.dumps({
                    "id": sid, "status": "expired", "progress_pct": None, "created_at": created,
                    "finished_at": created, "error": None, "manifest": None, "files": []}).encode())
            if state.polls <= state.running_polls:
                return self._send(200, json.dumps({
                    "id": sid, "status": "running", "progress_pct": 40, "created_at": created,
                    "finished_at": None, "error": None, "manifest": None, "files": []}).encode())
            files = [{"name": n, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                     for n, b in state.files.items()]
            self._send(200, json.dumps({
                "id": sid, "status": "ready", "progress_pct": 100, "created_at": created,
                "finished_at": created, "error": None,
                "manifest": json.loads(state.files["manifest.json"]), "files": files}).encode())

        def _download(self, sid, name):
            if sid != state.sid or name not in state.files:
                return self._err(404, "not_found", "no such file")
            if state.download_error:
                return self._err(*state.download_error)
            data = state.files[name]
            sha = hashlib.sha256(data).hexdigest()
            if isinstance(state.sha_header, dict):
                sha = state.sha_header.get(name, sha)
            elif state.sha_header:
                sha = state.sha_header
            body_src = data
            if state.corrupt == name:
                body_src = bytes([data[0] ^ 0xFF]) + data[1:]
            start, end, status = 0, len(data) - 1, 200
            rng = self.headers.get("Range")
            hdr = {"Accept-Ranges": "bytes", "ETag": f'"{hashlib.md5(data).hexdigest()}"',
                   "X-Content-SHA256": sha}
            if rng and not state.ignore_range:
                m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
                if not m or int(m.group(1)) >= len(data):
                    return self._send(416, b"", {"Content-Range": f"bytes */{len(data)}", **hdr})
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else len(data) - 1
                status = 206
                hdr["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            body = body_src[start:end + 1]
            if state.truncate_once == name and not state.truncated and start == 0:
                state.truncated = True
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for k, v in hdr.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body[: len(body) // 2])
                self.wfile.flush()
                self.close_connection = True  # 본문 절반만 보내고 연결 종료 → 클라이언트는 IncompleteRead
                return
            self._send(status, body, hdr, ctype="application/octet-stream")

    return H


class FetchApiBase(base.SyncOpsBase):
    """SyncOpsBase(임시 가짜 DB/BASE_DIR) + 로컬 가짜 서버."""

    def setUp(self):
        super().setUp()
        self.key = "qgp_test_" + secrets.token_hex(16)  # 런타임 생성, 출력 금지
        p = mock.patch.dict(os.environ, {
            KEY_ENV: self.key, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        })
        p.start()
        self.addCleanup(p.stop)
        for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            os.environ.pop(var, None)
        p2 = mock.patch.object(fetch, "RETRY_BACKOFF", 0)
        p2.start()
        self.addCleanup(p2.stop)
        self.dest = self.tmp / "fetched"

    def serve(self, bundle: Path, prefix: str = "") -> FakeState:
        m = self.manifest(bundle)
        files = {"manifest.json": (bundle / "manifest.json").read_bytes(),
                 m["db"]["file"]: (bundle / m["db"]["file"]).read_bytes()}
        return self.serve_files(files, prefix)

    def serve_files(self, files, prefix: str = "") -> FakeState:
        state = FakeState(files, self.key, prefix)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(state))
        srv.daemon_threads = True
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        def stop():
            srv.shutdown()
            srv.server_close()
            t.join(5)
        self.addCleanup(stop)
        state.port = srv.server_address[1]
        state.url = f"http://127.0.0.1:{state.port}{prefix}"
        return state

    def fetch(self, state, *extra, key_env=KEY_ENV, url=None):
        return base.run_cmd(
            "fetch_snapshot_via_api", "--url", url or state.url, "--key-env", key_env,
            "--out-dir", self.dest, "--poll-interval", "0.01", *extra,
        )

    def assert_no_key(self, r):
        self.assertNotIn(self.key, r.text)

    def bundles(self):
        return sorted(p for p in self.dest.glob("qg-*")) if self.dest.exists() else []


# ---------------------------------------------------------------------------
# ① 정상 흐름 + 번들 형식 + import/verify 왕복
# ---------------------------------------------------------------------------


class FetchHappyPathTests(FetchApiBase):
    def test_fetch_roundtrip_with_import_and_verify(self):
        modes = ["gzip"] + (["zstd"] if base.HAS_ZSTD else [])
        for mode in modes:
            with self.subTest(mode=mode):
                if self.dest.exists():
                    shutil.rmtree(self.dest)
                bundle = self.make_bundle("--compress", mode)
                state = self.serve(bundle, prefix="/qg")
                r = self.fetch(state, "--compress", mode)
                self.assertTrue(r.ok, r.text)
                self.assert_no_key(r)

                [got] = self.bundles()
                m = self.manifest(bundle)
                ts = m["created_at"].replace("-", "").replace(":", "")
                self.assertEqual(got.name, f"qg-{ts}")
                self.assertEqual(sorted(p.name for p in got.iterdir()), sorted(["manifest.json", m["db"]["file"]]))
                self.assertFalse((got / "config.tar").exists())
                self.assertEqual(stat.S_IMODE(got.stat().st_mode), 0o700)
                for p in got.iterdir():
                    self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600, p.name)
                self.assertEqual(base.sha256_file(got / m["db"]["file"]), m["db"]["file_sha256"])
                self.assertFalse((self.dest / f".fetch-{state.sid}").exists(), "스테이징 정리")

                # 요청 순서/인증/원격 삭제
                self.assertTrue(all(e["auth_ok"] for e in state.log))
                self.assertEqual(state.log[0]["method"], "POST")
                self.assertTrue(state.deleted)
                self.assertIn("[poll] running", r.out)
                # 안내 문구 (경로·명령만)
                self.assertIn(f"import_snapshot {got} --yes", r.out)
                self.assertIn(f"verify_snapshot {got} --allow-newer", r.out)
                self.assertIn("config", r.out)

                # 기존 import/verify 로 통과
                target = self.tmp / f"target-{mode}.sqlite3"
                ri = self.do_import(got, target)
                self.assertTrue(ri.ok, ri.text)
                self.assertEqual(base.dump_db(target), base.dump_db(self.src_db))
                rv = base.run_cmd("verify_snapshot", got, "--target-db", target, "--allow-newer")
                self.assertTrue(rv.ok, rv.text)

    def test_keep_remote_skips_delete(self):
        state = self.serve(self.make_bundle("--compress", "gzip"))
        r = self.fetch(state, "--keep-remote")
        self.assertTrue(r.ok, r.text)
        self.assertFalse(state.deleted)
        self.assertNotIn("DELETE", [e["method"] for e in state.log])

    def test_status_5xx_is_retried(self):
        state = self.serve(self.make_bundle("--compress", "gzip"))
        state.status_5xx = 2
        r = self.fetch(state)
        self.assertTrue(r.ok, r.text)
        self.assertIn("재시도", r.out)

    def test_status_5xx_exhausted_fails_with_resume_hint(self):
        state = self.serve(self.make_bundle("--compress", "gzip"))
        state.status_5xx = 999
        r = self.fetch(state)
        self.assertFalse(r.ok)
        self.assertIn(state.sid, r.text)
        self.assertIn(f"--existing-id {state.sid}", r.text)
        self.assert_no_key(r)

    def test_same_origin_redirect_is_followed(self):
        state = self.serve(self.make_bundle("--compress", "gzip"), prefix="/qg")
        state.redirect_from = "/old"
        state.redirect_to = "/qg"
        r = self.fetch(state, url=f"http://127.0.0.1:{state.port}/old")
        self.assertTrue(r.ok, r.text)
        self.assertTrue(all(e["auth_ok"] for e in state.log))


# ---------------------------------------------------------------------------
# ② 중단 후 Range 재개
# ---------------------------------------------------------------------------


class FetchResumeTests(FetchApiBase):
    def setUp(self):
        super().setUp()
        self.bundle = self.make_bundle("--compress", "gzip")
        self.dbname = self.manifest(self.bundle)["db"]["file"]
        self.state = self.serve(self.bundle)
        # 작은 가짜 DB 에서도 중단 전에 일부가 .part 에 기록되도록 청크를 작게
        p = mock.patch.object(fetch, "CHUNK", 256)
        p.start()
        self.addCleanup(p.stop)

    def test_connection_drop_resumes_with_range(self):
        self.state.truncate_once = self.dbname
        r = self.fetch(self.state)
        self.assertTrue(r.ok, r.text)
        self.assertTrue(self.state.truncated)
        dl = [e for e in self.state.log if f"/download/{self.dbname}/" in e["path"]]
        self.assertEqual(len(dl), 2, dl)
        self.assertIsNone(dl[0]["range"])
        self.assertRegex(dl[1]["range"], r"^bytes=\d+-$")
        self.assertGreater(int(dl[1]["range"][6:-1]), 0)
        [got] = self.bundles()
        self.assertEqual((got / self.dbname).read_bytes(), (self.bundle / self.dbname).read_bytes())

    def test_existing_part_is_resumed_without_post(self):
        data = (self.bundle / self.dbname).read_bytes()
        stage = self.dest / f".fetch-{self.state.sid}"
        stage.mkdir(parents=True)
        (stage / f"{self.dbname}.part").write_bytes(data[: len(data) // 3])
        r = self.fetch(self.state, "--existing-id", self.state.sid)
        self.assertTrue(r.ok, r.text)
        self.assertNotIn("POST", [e["method"] for e in self.state.log])
        dl = [e for e in self.state.log if f"/download/{self.dbname}/" in e["path"]]
        self.assertEqual(dl[0]["range"], f"bytes={len(data) // 3}-")
        [got] = self.bundles()
        self.assertEqual((got / self.dbname).read_bytes(), data)

    def test_server_ignoring_range_restarts_from_zero(self):
        data = (self.bundle / self.dbname).read_bytes()
        stage = self.dest / f".fetch-{self.state.sid}"
        stage.mkdir(parents=True)
        (stage / f"{self.dbname}.part").write_bytes(b"garbage-prefix" * 3)
        self.state.ignore_range = True
        r = self.fetch(self.state, "--existing-id", self.state.sid)
        self.assertTrue(r.ok, r.text)
        [got] = self.bundles()
        self.assertEqual((got / self.dbname).read_bytes(), data)

    def test_complete_part_gets_416_and_is_accepted(self):
        data = (self.bundle / self.dbname).read_bytes()
        stage = self.dest / f".fetch-{self.state.sid}"
        stage.mkdir(parents=True)
        (stage / f"{self.dbname}.part").write_bytes(data)
        r = self.fetch(self.state, "--existing-id", self.state.sid)
        self.assertTrue(r.ok, r.text)
        [got] = self.bundles()
        self.assertEqual((got / self.dbname).read_bytes(), data)


# ---------------------------------------------------------------------------
# ③ sha256 불일치 거부
# ---------------------------------------------------------------------------


class FetchIntegrityTests(FetchApiBase):
    def setUp(self):
        super().setUp()
        self.bundle = self.make_bundle("--compress", "gzip")
        self.dbname = self.manifest(self.bundle)["db"]["file"]
        self.state = self.serve(self.bundle)

    def _assert_rejected(self, r):
        self.assertFalse(r.ok, r.text)
        self.assertIn("sha256", r.text)
        self.assertIn(f"--existing-id {self.state.sid}", r.text)
        self.assertEqual(self.bundles(), [])
        stage = self.dest / f".fetch-{self.state.sid}"
        self.assertFalse((stage / f"{self.dbname}.part").exists(), "불일치 파일 삭제")
        self.assertFalse((stage / self.dbname).exists())
        self.assert_no_key(r)

    def test_header_sha_mismatch_rejected(self):
        self.state.sha_header = {self.dbname: "0" * 64}
        self._assert_rejected(self.fetch(self.state))

    def test_corrupted_body_rejected(self):
        self.state.corrupt = self.dbname
        self._assert_rejected(self.fetch(self.state))

    def test_manifest_file_sha_mismatch_rejected(self):
        """서버 헤더/목록 sha 는 받은 파일과 맞지만 manifest.db.file_sha256 이 다르면 거부."""
        m = self.manifest(self.bundle)
        m["db"]["file_sha256"] = "f" * 64
        files = {"manifest.json": json.dumps(m).encode(),
                 self.dbname: (self.bundle / self.dbname).read_bytes()}
        self.state = self.serve_files(files)
        self._assert_rejected(self.fetch(self.state))

    def test_manifest_with_config_rejected(self):
        m = self.manifest(self.bundle)
        m["config"] = {"file": "config.tar", "encrypted": False, "sha256": "0" * 64, "entries": []}
        files = {"manifest.json": json.dumps(m).encode(),
                 self.dbname: (self.bundle / self.dbname).read_bytes()}
        self.state = self.serve_files(files)
        r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertIn("config", r.text)
        self.assertEqual(self.bundles(), [])

    def test_manifest_db_name_traversal_rejected(self):
        m = self.manifest(self.bundle)
        m["db"]["file"] = "../evil"
        files = {"manifest.json": json.dumps(m).encode(),
                 self.dbname: (self.bundle / self.dbname).read_bytes()}
        self.state = self.serve_files(files)
        r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertEqual(self.bundles(), [])
        self.assertFalse((self.tmp / "evil").exists())


# ---------------------------------------------------------------------------
# ④ 오류 응답 (키 미노출)
# ---------------------------------------------------------------------------


class FetchErrorTests(FetchApiBase):
    def setUp(self):
        super().setUp()
        self.state = self.serve(self.make_bundle("--compress", "gzip"))

    def test_409_export_in_progress(self):
        self.state.post_error = (409, "export_in_progress", "다른 export 실행 중")
        r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertIn("409", r.text)
        self.assertIn("export_in_progress", r.text)
        self.assert_no_key(r)
        self.assertEqual(self.bundles(), [])

    def test_403_mentions_scope_and_hides_key(self):
        # 서버가 (악의적/실수로) 오류 메시지에 키를 되돌려 보내도 출력에는 남지 않는다.
        self.state.post_error = (403, "forbidden", f"scope export:read required for {self.key}")
        r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertIn("403", r.text)
        self.assertIn("export:read", r.text)
        self.assert_no_key(r)

    def test_400_unsupported(self):
        self.state.post_error = (400, "unsupported", "config 는 내보낼 수 없음")
        r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertIn("unsupported", r.text)

    def test_wrong_key_401_hides_key(self):
        with mock.patch.dict(os.environ, {KEY_ENV: "qgp_wrong_" + secrets.token_hex(8)}):
            wrong = os.environ[KEY_ENV]
            r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertIn("401", r.text)
        self.assertNotIn(wrong, r.text)
        self.assert_no_key(r)

    def test_download_409_not_ready_includes_resume_hint(self):
        self.state.download_error = (409, "not_ready", "아직 준비 안 됨")
        r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertIn("409", r.text)
        self.assertIn(f"--existing-id {self.state.sid}", r.text)
        self.assert_no_key(r)

    def test_remote_failed_status(self):
        self.state.final_status = "failed"
        r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertIn("disk full", r.text)
        self.assertIn(self.state.sid, r.text)

    def test_remote_expired_status(self):
        self.state.final_status = "expired"
        r = self.fetch(self.state, "--existing-id", self.state.sid)
        self.assertFalse(r.ok)
        self.assertIn("만료", r.text)

    def test_unknown_existing_id_404(self):
        r = self.fetch(self.state, "--existing-id", str(uuid.uuid4()))
        self.assertFalse(r.ok)
        self.assertIn("없습니다", r.text)

    def test_poll_timeout(self):
        self.state.running_polls = 10 ** 6
        r = self.fetch(self.state, "--timeout", "0.3")
        self.assertFalse(r.ok)
        self.assertIn("--timeout", r.text)
        self.assertIn(f"--existing-id {self.state.sid}", r.text)

    def test_cross_host_redirect_refused(self):
        """Authorization 이 다른 호스트(여기서는 hostname 만 다른 localhost)로 새지 않는다."""
        self.state.redirect_from = API
        self.state.redirect_to = f"http://localhost:{self.state.port}{API}"
        r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertIn("리다이렉트", r.text)
        self.assertEqual(len(self.state.log), 1, "리다이렉트 대상으로는 요청을 보내지 않아야 한다")

    def test_existing_bundle_dir_collision(self):
        r1 = self.fetch(self.state)
        self.assertTrue(r1.ok, r1.text)
        self.state.polls = 0
        self.state.deleted = False
        r2 = self.fetch(self.state, "--existing-id", self.state.sid)
        self.assertFalse(r2.ok)
        self.assertIn("이미", r2.text)


# ---------------------------------------------------------------------------
# ⑤ ⑥ ⑦ 입력 검증 / dry-run
# ---------------------------------------------------------------------------


class FetchInputTests(FetchApiBase):
    def setUp(self):
        super().setUp()
        self.state = self.serve(self.make_bundle("--compress", "gzip"))

    def test_missing_key_env(self):
        r = self.fetch(self.state, key_env="QG_TEST_FETCH_KEY_NOT_SET")
        self.assertFalse(r.ok)
        self.assertIn("QG_TEST_FETCH_KEY_NOT_SET", r.text)
        self.assertEqual(self.state.log, [])

    def test_empty_key_env(self):
        with mock.patch.dict(os.environ, {KEY_ENV: "  "}):
            r = self.fetch(self.state)
        self.assertFalse(r.ok)
        self.assertEqual(self.state.log, [])

    def test_non_local_http_rejected(self):
        for url in ("http://example.com", "http://10.0.0.5:8080/qg", "ftp://example.com", "example.com", "https://"):
            with self.subTest(url=url):
                r = self.fetch(self.state, url=url)
                self.assertFalse(r.ok)
                self.assertIn("--url", r.text)
        self.assertEqual(self.state.log, [])

    def test_url_with_credentials_rejected(self):
        r = self.fetch(self.state, url="https://user:pw@example.com")
        self.assertFalse(r.ok)
        self.assertNotIn("pw@", r.text)

    def test_validate_url_accepts_https_and_local_http(self):
        self.assertEqual(fetch.validate_url("https://qualitygate.example/qg/"), "https://qualitygate.example/qg")
        self.assertEqual(fetch.validate_url("http://localhost:8000"), "http://localhost:8000")
        self.assertEqual(fetch.validate_url("http://127.0.0.1:1/x"), "http://127.0.0.1:1/x")
        with self.assertRaises(CommandError):
            fetch.validate_url("http://qualitygate.example")

    def test_bad_existing_id(self):
        r = self.fetch(self.state, "--existing-id", "../../etc")
        self.assertFalse(r.ok)
        self.assertEqual(self.state.log, [])

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        r = self.fetch(self.state, "--dry-run")
        self.assertTrue(r.ok, r.text)
        self.assertEqual(self.state.log, [])
        self.assertFalse(self.dest.exists())
        self.assertIn("dry-run", r.out)
        self.assertIn(KEY_ENV, r.out)
        self.assert_no_key(r)

    def test_dry_run_still_requires_key(self):
        r = self.fetch(self.state, "--dry-run", key_env="QG_TEST_FETCH_KEY_NOT_SET")
        self.assertFalse(r.ok)

    def test_no_insecure_option(self):
        """인증서 검증을 끄는 옵션은 없다."""
        for flag in ("--insecure", "--no-verify", "--verify"):
            r = self.fetch(self.state, flag)
            self.assertFalse(r.ok, flag)
        self.assertEqual(self.state.log, [])


class FetchAuthNetrcTests(SimpleTestCase):
    """리뷰(2026-10-09): ~/.netrc 에 원본 호스트가 있어도 Bearer 키가 Basic 으로 덮이지 않는다."""

    def test_netrc_does_not_override_bearer(self):
        import netrc as _netrc  # noqa: F401  (존재 확인)
        import tempfile
        from apps.syncops.management.commands.fetch_snapshot_via_api import Client
        with tempfile.TemporaryDirectory() as d:
            rc = os.path.join(d, "netrc")
            with open(rc, "w") as f:
                f.write("machine 127.0.0.1 login someone password not-a-real-secret\n")
            os.chmod(rc, 0o600)
            with mock.patch.dict(os.environ, {"NETRC": rc}):
                c = Client("http://127.0.0.1:9", "qgp_testprefix_" + "0" * 32)
                req = requests.Request("GET", "http://127.0.0.1:9/x")
                prepared = c.session.prepare_request(req)
        self.assertTrue(prepared.headers["Authorization"].startswith("Bearer "))
        self.assertNotIn("Basic", prepared.headers["Authorization"])
