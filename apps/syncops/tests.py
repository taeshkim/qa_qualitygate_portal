"""apps.syncops 스냅샷 export/import/verify/sync 테스트.

운영 DB 에는 절대 접근하지 않는다 — 모든 케이스는 tempfile 임시 디렉터리의 가짜 SQLite DB 와
가짜 BASE_DIR(.env / secrets / deploy)만 사용한다. 외부 호출(ssh / rsync / crontab)은 모킹·가짜 실행파일로 대체.
비밀 값은 모두 가짜 문자열이며 실제 값이 아니다.
"""
from __future__ import annotations

import collections
import contextlib
import errno
import glob
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test.utils import override_settings

# ---------------------------------------------------------------------------
# 공통 상수 / 헬퍼
# ---------------------------------------------------------------------------

# 가짜 비밀 값 (테스트 전용; 실제 키/토큰 아님)
FAKE_ENV_VALUE = "fake-env-secret-VALUE-0001"
FAKE_SECRET_FILE_VALUE = "fake-wif-json-VALUE-0002"
FAKE_DEPLOY_VALUE = "fake-deploy-unit-VALUE-0003"
FAKE_PASS = "fake-pass-phrase-VALUE-0004"
WRONG_PASS = "wrong-pass-phrase-VALUE-0005"
PASS_ENV = "QG_TEST_SNAPSHOT_PASS"
CRON_MARK = "FAKE_CRON_MARKER_7731"

# 현재 코드에 실제 존재하는 (app, name) — 대상 코드 마이그레이션 검사 통과용
REAL_MIGRATIONS = [("contenttypes", "0001_initial"), ("auth", "0001_initial")]
GHOST_MIGRATION = ("ghost_app_not_in_code", "0001_initial")

HAS_ZSTD = shutil.which("zstd") is not None
HAS_OPENSSL = shutil.which("openssl") is not None

# 모킹 전의 원본 (sync 테스트에서 ssh/rsync 외 호출은 그대로 위임)
_REAL_POPEN = subprocess.Popen
_REAL_RUN = subprocess.run


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_db(path, *, items=40, notes=7, migrations=None, extra_migrations=()):
    """작은 가짜 DB. django_migrations + items(BLOB/한글/NULL 포함) + notes + empty_tbl."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, app TEXT, name TEXT, applied TEXT)"
        )
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, payload BLOB, score REAL)")
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE empty_tbl (id INTEGER PRIMARY KEY, v TEXT)")
        for app, name in list(migrations or REAL_MIGRATIONS) + list(extra_migrations):
            conn.execute(
                "INSERT INTO django_migrations (app, name, applied) VALUES (?,?,?)",
                (app, name, "2026-01-01 00:00:00"),
            )
        for i in range(items):
            conn.execute(
                "INSERT INTO items (name, payload, score) VALUES (?,?,?)",
                (f"항목-{i}", os.urandom(64) if i % 3 else None, i * 1.5),
            )
        for i in range(notes):
            conn.execute("INSERT INTO notes (body) VALUES (?)", (f"노트 {i} — ünïcode",))
        conn.commit()
    finally:
        conn.close()
    return str(path)


def make_schema_only_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, payload BLOB, score REAL)")
    conn.commit()
    conn.close()
    return str(path)


def dump_db(path) -> dict:
    """{table: [rows...]} — 모든 테이블 전체 내용 (mode=ro)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY 1')] for t in tables}
    finally:
        conn.close()


def count_rows(path) -> dict:
    return {t: len(rows) for t, rows in dump_db(path).items()}


def free_port() -> int:
    """지금 아무도 listen 하지 않는 포트 (운영 호스트의 기본 8601 에 의존하지 않기 위함)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Result:
    def __init__(self, rc, out, err, exc=None):
        self.rc, self.out, self.err, self.exc = rc, out, err, exc

    @property
    def text(self):
        return f"{self.out}\n{self.err}\n{self.exc or ''}"

    @property
    def ok(self):
        return self.rc == 0


def run_cmd(name, *args) -> Result:
    """call_command 래퍼 — CommandError/SystemExit 을 종료코드로 환산."""
    out, err = io.StringIO(), io.StringIO()
    rc, exc = 0, None
    try:
        call_command(name, *[str(a) for a in args], stdout=out, stderr=err)
    except CommandError as e:
        rc = getattr(e, "returncode", 1) or 1
        exc = str(e)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return Result(rc, out.getvalue(), err.getvalue(), exc)


class SyncOpsBase(unittest.TestCase):
    """임시 디렉터리 + 가짜 BASE_DIR + 가짜 crontab 실행파일 + 패스프레이즈 환경변수."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="qg-syncops-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        # 가짜 BASE_DIR
        self.base = self.tmp / "base"
        (self.base / "secrets").mkdir(parents=True)
        (self.base / "deploy").mkdir()
        (self.base / ".env").write_text(f"FAKE_KEY={FAKE_ENV_VALUE}\n")
        (self.base / "secrets" / "qg-owners-wif.json").write_text(
            json.dumps({"fake": FAKE_SECRET_FILE_VALUE})
        )
        (self.base / "deploy" / "qualitygate.service").write_text(
            f"[Service]\nEnvironment=FAKE={FAKE_DEPLOY_VALUE}\n"
        )
        ov = override_settings(BASE_DIR=self.base)
        ov.enable()
        self.addCleanup(ov.disable)

        # 가짜 crontab — -l 이면 고정 출력, 그 외 호출(설치 시도)은 로그만 남김
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.cron_install_log = self.tmp / "crontab-install.log"
        shim = self.bin / "crontab"
        shim.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-l" ]; then\n'
            f'  echo "* * * * * echo {CRON_MARK}"\n'
            "else\n"
            f'  echo "$@" >> "{self.cron_install_log}"\n'
            "fi\n"
        )
        shim.chmod(0o755)
        p = mock.patch.dict(
            os.environ,
            {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", PASS_ENV: FAKE_PASS},
        )
        p.start()
        self.addCleanup(p.stop)

        self.src_db = make_db(self.tmp / "source.sqlite3")
        self.out_root = self.tmp / "bundles"
        self.out_root.mkdir()
        self.port = free_port()

    # --- 편의 ---
    def export(self, *extra, db=None, out=None, config=False) -> Result:
        out = out or (self.out_root / f"qg-{len(list(self.out_root.iterdir()))}")
        args = ["--out", out, "--db-path", db or self.src_db]
        if not config:
            args.append("--no-config")
        args += list(extra)
        r = run_cmd("export_snapshot", *args)
        r.bundle = Path(out)
        return r

    def make_bundle(self, *extra, config=False, **kw) -> Path:
        r = self.export(*extra, config=config, **kw)
        self.assertTrue(r.ok, r.text)
        return r.bundle

    def manifest(self, bundle) -> dict:
        return json.loads((Path(bundle) / "manifest.json").read_text())

    def write_manifest(self, bundle, m):
        (Path(bundle) / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))

    def db_file(self, bundle) -> Path:
        m = self.manifest(bundle)
        return Path(bundle) / m["db"]["file"]

    def do_import(self, bundle, target, *extra, same_host=True) -> Result:
        """기본은 --allow-same-host 를 붙인다 (이 테스트들은 한 호스트에서 export→import 왕복하므로).
        같은 호스트 가드 자체를 검증할 때만 same_host=False."""
        args = [bundle, "--target-db", target, "--service-port", self.port, "--yes"]
        if same_host:
            args.append("--allow-same-host")
        return run_cmd("import_snapshot", *args, *extra)

    def assert_no_importing_leftover(self, target):
        self.assertEqual(glob.glob(f"{target}.importing*"), [], "복원 실패 후 .importing 정리 안 됨")

    @contextlib.contextmanager
    def listening(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(5)
        try:
            yield s
        finally:
            s.close()


# ---------------------------------------------------------------------------
# ① export --dry-run
# ---------------------------------------------------------------------------


class ExportDryRunTests(SyncOpsBase):
    def _tree(self, root):
        return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))

    def test_dry_run_writes_nothing(self):
        out = self.out_root / "should-not-exist"
        before_db_hash = sha256_file(self.src_db)
        before_tree = self._tree(self.tmp)
        r = run_cmd(
            "export_snapshot", "--out", out, "--db-path", self.src_db, "--dry-run"
        )
        self.assertTrue(r.ok, r.text)
        self.assertFalse(out.exists(), "dry-run 이 번들 디렉터리를 만들었다")
        self.assertEqual(self._tree(self.tmp), before_tree, "dry-run 이 파일을 생성/삭제했다")
        self.assertEqual(sha256_file(self.src_db), before_db_hash)
        # 계획 출력: 테이블명 / config 항목 파일명 포함, 비밀 값은 없음
        text = r.text
        self.assertIn("items", text)
        self.assertIn(".env", text)
        for secret in (FAKE_ENV_VALUE, FAKE_SECRET_FILE_VALUE, FAKE_DEPLOY_VALUE):
            self.assertNotIn(secret, text)
        # C7: DB 평문 경고
        self.assertIn("DB 는 암호화되지 않습니다(세션·해시 포함)", text)
        self.assertIn("별도 암호화", text)

    def test_dry_run_default_out_does_not_create_snapshots_dir(self):
        r = run_cmd("export_snapshot", "--db-path", self.src_db, "--dry-run", "--no-config")
        self.assertTrue(r.ok, r.text)
        self.assertFalse((self.base / "snapshots").exists())

    def test_dry_run_leaves_no_journal_next_to_db(self):
        before = sorted(os.listdir(self.src_db.rsplit("/", 1)[0]))
        run_cmd("export_snapshot", "--db-path", self.src_db, "--dry-run", "--no-config",
                "--out", self.out_root / "x")
        after = sorted(os.listdir(self.src_db.rsplit("/", 1)[0]))
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# export 번들 형식
# ---------------------------------------------------------------------------


class ExportBundleTests(SyncOpsBase):
    def test_manifest_fields_and_permissions(self):
        before_hash = sha256_file(self.src_db)
        bundle = self.make_bundle("--compress", "gzip")
        m = self.manifest(bundle)
        self.assertEqual(m["schema_version"], 1)
        self.assertIn("created_at", m)
        for k in ("hostname", "base_dir", "git", "python", "django"):
            self.assertIn(k, m["source"])
        self.assertEqual(m["db"]["engine"], "sqlite")
        self.assertEqual(m["db"]["compression"], "gzip")
        self.assertEqual(m["db"]["file"], "db.sqlite3.gz")
        self.assertEqual(m["db"]["file_sha256"], sha256_file(bundle / "db.sqlite3.gz"))
        self.assertEqual(m["tables"], count_rows(self.src_db))
        self.assertEqual(
            {tuple(x) for x in m["migrations"]}, set(REAL_MIGRATIONS)
        )
        self.assertIsNone(m["config"])
        # 원본 불변, 번들에 잔여 임시 파일 없음
        self.assertEqual(sha256_file(self.src_db), before_hash)
        self.assertEqual(sorted(os.listdir(bundle)), ["db.sqlite3.gz", "manifest.json"])
        # 같은 호스트 가드용 원본 DB 경로 (C1)
        self.assertEqual(m["db"]["source_path"], str(Path(self.src_db).resolve()))
        # 권한: 디렉터리 0700, 파일 0600
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)
        for f in bundle.iterdir():
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f.name)

    def test_compression_modes(self):
        modes = [("none", "db.sqlite3"), ("gzip", "db.sqlite3.gz"), ("auto", None)]
        if HAS_ZSTD:
            modes.append(("zstd", "db.sqlite3.zst"))
        for mode, fname in modes:
            with self.subTest(mode=mode):
                bundle = self.make_bundle("--compress", mode)
                m = self.manifest(bundle)
                if fname:
                    self.assertEqual(m["db"]["file"], fname)
                if mode == "auto" and HAS_ZSTD:
                    self.assertEqual(m["db"]["compression"], "zstd")
                self.assertEqual(m["db"]["file_sha256"], sha256_file(bundle / m["db"]["file"]))
                self.assertEqual(m["tables"], count_rows(self.src_db))

    def test_export_completion_warns_db_not_encrypted(self):
        r = self.export()
        self.assertTrue(r.ok, r.text)
        self.assertIn("DB 는 암호화되지 않습니다(세션·해시 포함)", r.text)
        self.assertIn("별도 암호화", r.text)

    def test_includes_all_tables_even_empty(self):
        bundle = self.make_bundle()
        self.assertEqual(self.manifest(bundle)["tables"]["empty_tbl"], 0)


# ---------------------------------------------------------------------------
# ② export → import 왕복
# ---------------------------------------------------------------------------


class RoundTripTests(SyncOpsBase):
    def test_roundtrip_all_compressions_identical_content(self):
        modes = ["none", "gzip", "auto"] + (["zstd"] if HAS_ZSTD else [])
        for mode in modes:
            with self.subTest(mode=mode):
                bundle = self.make_bundle("--compress", mode)
                target = self.tmp / f"target-{mode}.sqlite3"
                r = self.do_import(bundle, target)
                self.assertTrue(r.ok, r.text)
                self.assertEqual(dump_db(target), dump_db(self.src_db))
                self.assert_no_importing_leftover(target)
                # 복원 결과 무결성
                conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                conn.close()
                # import 직후 verify 통과
                v = run_cmd("verify_snapshot", bundle, "--target-db", target)
                self.assertTrue(v.ok, v.text)

    def test_import_empty_existing_target_is_allowed(self):
        """스키마만 있고 사용자 테이블 행이 0 인 대상은 데이터가 있는 것이 아니다."""
        bundle = self.make_bundle()
        target = make_schema_only_db(self.tmp / "t.sqlite3")
        r = self.do_import(bundle, target)
        self.assertTrue(r.ok, r.text)
        self.assertEqual(dump_db(target), dump_db(self.src_db))

    def test_import_removes_stale_journal_files(self):
        bundle = self.make_bundle()
        target = self.tmp / "t2.sqlite3"
        for suffix in ("-journal", "-wal", "-shm"):
            Path(f"{target}{suffix}").write_bytes(b"stale")
        r = self.do_import(bundle, target)
        self.assertTrue(r.ok, r.text)
        for suffix in ("-journal", "-wal", "-shm"):
            self.assertFalse(Path(f"{target}{suffix}").exists(), suffix)

    def test_import_skips_migrate_for_non_default_target(self):
        bundle = self.make_bundle()
        target = self.tmp / "t3.sqlite3"
        with mock.patch("django.core.management.commands.migrate.Command.handle") as migrate:
            r = self.do_import(bundle, target)
        self.assertTrue(r.ok, r.text)
        migrate.assert_not_called()
        # 이유를 출력
        self.assertIn("migrate", r.text.lower())


# ---------------------------------------------------------------------------
# ③ ④ 무결성 검증 실패
# ---------------------------------------------------------------------------


class IntegrityTests(SyncOpsBase):
    def _existing_target(self):
        """이미 데이터가 있는 대상은 --force 가 필요하므로, 불변 검증은 '대상 없음' 또는 --force 로 확인."""
        t = make_db(self.tmp / "existing.sqlite3", items=3, notes=1)
        return t, sha256_file(t)

    def test_tampered_manifest_file_sha_rejected(self):
        bundle = self.make_bundle()
        m = self.manifest(bundle)
        m["db"]["file_sha256"] = "0" * 64
        self.write_manifest(bundle, m)
        target = self.tmp / "t.sqlite3"
        r = self.do_import(bundle, target)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertFalse(target.exists())
        self.assert_no_importing_leftover(target)

    def test_tampered_manifest_db_sha_rejected_keeps_existing_db(self):
        bundle = self.make_bundle()
        m = self.manifest(bundle)
        m["db"]["sha256"] = "f" * 64  # 압축 해제 후 해시와 불일치
        self.write_manifest(bundle, m)
        target, before = self._existing_target()
        r = self.do_import(bundle, target, "--force")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertEqual(sha256_file(target), before, "실패했는데 기존 DB 가 바뀌었다")
        self.assert_no_importing_leftover(target)
        self.assertEqual(glob.glob(f"{target}.pre-import-*"), [])

    def test_tampered_manifest_row_counts_rejected_keeps_existing_db(self):
        bundle = self.make_bundle()
        m = self.manifest(bundle)
        m["tables"]["items"] += 5
        self.write_manifest(bundle, m)
        target, before = self._existing_target()
        r = self.do_import(bundle, target, "--force")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertEqual(sha256_file(target), before)
        self.assert_no_importing_leftover(target)

    def test_unsupported_schema_version_rejected(self):
        bundle = self.make_bundle()
        m = self.manifest(bundle)
        m["schema_version"] = 999
        self.write_manifest(bundle, m)
        target = self.tmp / "t.sqlite3"
        r = self.do_import(bundle, target)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertFalse(target.exists())

    def test_truncated_bundle_file_rejected_keeps_existing_db(self):
        bundle = self.make_bundle("--compress", "gzip")
        f = self.db_file(bundle)
        data = f.read_bytes()
        f.write_bytes(data[: len(data) // 2])
        target, before = self._existing_target()
        r = self.do_import(bundle, target, "--force")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertEqual(sha256_file(target), before)
        self.assert_no_importing_leftover(target)

    def test_truncated_bundle_with_matching_file_sha_still_fails_cleanly(self):
        """번들 파일 sha 까지 맞춰도(=sha 검사를 통과해도) 압축 해제/압축 전 해시 단계에서 실패해야 한다."""
        bundle = self.make_bundle("--compress", "gzip")
        f = self.db_file(bundle)
        data = f.read_bytes()
        f.write_bytes(data[: len(data) // 2])
        m = self.manifest(bundle)
        m["db"]["file_sha256"] = sha256_file(f)
        self.write_manifest(bundle, m)
        target, before = self._existing_target()
        r = self.do_import(bundle, target, "--force")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertEqual(sha256_file(target), before)
        self.assert_no_importing_leftover(target)

    def test_missing_bundle_file_rejected(self):
        bundle = self.make_bundle()
        self.db_file(bundle).unlink()
        target = self.tmp / "t.sqlite3"
        r = self.do_import(bundle, target)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertFalse(target.exists())


# ---------------------------------------------------------------------------
# ⑤ 대상에 데이터 있음 / --force
# ---------------------------------------------------------------------------


class ForceTests(SyncOpsBase):
    def test_existing_data_requires_force(self):
        bundle = self.make_bundle()
        target = make_db(self.tmp / "existing.sqlite3", items=3, notes=1)
        before = sha256_file(target)
        r = self.do_import(bundle, target)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertEqual(sha256_file(target), before)
        self.assertEqual(glob.glob(f"{target}.pre-import-*"), [])
        self.assert_no_importing_leftover(target)

    def test_force_keeps_pre_import_backup(self):
        bundle = self.make_bundle()
        target = make_db(self.tmp / "existing.sqlite3", items=3, notes=1)
        before = sha256_file(target)
        before_dump = dump_db(target)
        r = self.do_import(bundle, target, "--force")
        self.assertTrue(r.ok, r.text)
        backups = glob.glob(f"{target}.pre-import-*")
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual(sha256_file(backups[0]), before, "기존 DB 가 그대로 보관돼야 한다")
        self.assertEqual(dump_db(backups[0]), before_dump)
        self.assertEqual(dump_db(target), dump_db(self.src_db))


# ---------------------------------------------------------------------------
# ⑥ 마이그레이션 ⑦ 서비스 실행 중
# ---------------------------------------------------------------------------


class PreflightTests(SyncOpsBase):
    def test_unknown_migration_in_bundle_rejected(self):
        src = make_db(self.tmp / "newer.sqlite3", extra_migrations=[GHOST_MIGRATION])
        bundle = self.make_bundle(db=src)
        target = self.tmp / "t.sqlite3"
        r = self.do_import(bundle, target)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn(GHOST_MIGRATION[0], r.text)
        self.assertFalse(target.exists())
        self.assertIn("--allow-older-code", r.text)
        # --force 로는 풀리지 않는다 (코드가 데이터보다 오래됨 ≠ 대상 덮어쓰기)
        r_force = self.do_import(bundle, target, "--force")
        self.assertNotEqual(r_force.rc, 0, r_force.text)
        self.assertIn(GHOST_MIGRATION[0], r_force.text)
        self.assertFalse(target.exists())
        # --allow-older-code 로만 통과
        r2 = self.do_import(bundle, target, "--allow-older-code")
        self.assertTrue(r2.ok, r2.text)
        self.assertTrue(target.exists())

    def test_allow_older_code_does_not_unlock_existing_data(self):
        """--allow-older-code 는 '대상에 데이터 있음' 덮어쓰기를 풀지 않는다 (--force 와 역할 분리)."""
        src = make_db(self.tmp / "newer2.sqlite3", extra_migrations=[GHOST_MIGRATION])
        bundle = self.make_bundle(db=src)
        target = make_db(self.tmp / "existing.sqlite3", items=3, notes=1)
        before = sha256_file(target)
        r = self.do_import(bundle, target, "--allow-older-code")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("--force", r.text)
        self.assertEqual(sha256_file(target), before)
        r2 = self.do_import(bundle, target, "--allow-older-code", "--force")
        self.assertTrue(r2.ok, r2.text)
        self.assertEqual(len(glob.glob(f"{target}.pre-import-*")), 1)

    def test_running_service_rejected_unless_ignored(self):
        bundle = self.make_bundle()
        target = self.tmp / "t.sqlite3"
        with self.listening():
            r = self.do_import(bundle, target)
            self.assertNotEqual(r.rc, 0, r.text)
            self.assertFalse(target.exists())
            r2 = self.do_import(bundle, target, "--ignore-running")
            self.assertTrue(r2.ok, r2.text)
        self.assertEqual(dump_db(target), dump_db(self.src_db))


# ---------------------------------------------------------------------------
# ⑫ 디스크 여유 부족
# ---------------------------------------------------------------------------


def _no_space_patches():
    """os.statvfs(shutil.disk_usage 의 하위 호출)와 shutil.disk_usage 를 여유 0 으로 가짜."""
    DU = collections.namedtuple("usage", "total used free")
    fake_stat = os.statvfs_result((4096, 4096, 1000, 0, 0, 100, 0, 0, 0, 255))
    return (
        mock.patch("os.statvfs", return_value=fake_stat),
        mock.patch("shutil.disk_usage", return_value=DU(4096 * 1000, 4096 * 1000, 0)),
    )


class DiskSpaceTests(SyncOpsBase):
    def test_export_fails_before_starting_when_disk_full(self):
        out = self.out_root / "full"
        p1, p2 = _no_space_patches()
        with p1, p2:
            r = run_cmd("export_snapshot", "--out", out, "--db-path", self.src_db, "--no-config")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertRegex(r.text, r"디스크|여유|공간|space|disk")
        self.assertFalse((out / "manifest.json").exists())
        self.assertFalse(list(out.glob("db.sqlite3*")) if out.exists() else False)

    def test_import_fails_when_disk_full_and_target_untouched(self):
        bundle = self.make_bundle()
        target = make_db(self.tmp / "existing.sqlite3", items=3, notes=1)
        before = sha256_file(target)
        p1, p2 = _no_space_patches()
        with p1, p2:
            r = self.do_import(bundle, target, "--force")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertRegex(r.text, r"디스크|여유|공간|space|disk")
        self.assertEqual(sha256_file(target), before)
        self.assert_no_importing_leftover(target)


# ---------------------------------------------------------------------------
# ⑬ 백업 시간 초과 (쓰기 경합)
# ---------------------------------------------------------------------------


class BackupContentionTests(SyncOpsBase):
    def test_backup_restarting_due_to_concurrent_writes_fails_with_guidance(self):
        big = self.tmp / "big.sqlite3"
        conn = sqlite3.connect(str(big))
        conn.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, payload BLOB)")
        conn.execute("CREATE TABLE counter (n INTEGER)")
        conn.execute("INSERT INTO counter VALUES (0)")
        conn.execute(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<12000) "
            "INSERT INTO big(payload) SELECT randomblob(4000) FROM c"
        )
        conn.commit()
        conn.close()

        stop = threading.Event()
        writes = []

        def writer():
            w = sqlite3.connect(str(big), timeout=30)
            w.execute("PRAGMA synchronous=OFF")
            try:
                while not stop.is_set():
                    try:
                        w.execute("UPDATE counter SET n = n + 1")
                        w.commit()
                        writes.append(1)
                    except sqlite3.OperationalError:
                        pass
            finally:
                w.close()

        th = threading.Thread(target=writer, daemon=True)
        th.start()
        out = self.out_root / "contended"
        try:
            r = run_cmd(
                "export_snapshot",
                "--out", out,
                "--db-path", big,
                "--no-config",
                "--compress", "none",
                "--max-backup-seconds", 2,
            )
        finally:
            stop.set()
            th.join(timeout=10)
        self.assertTrue(writes, "쓰기 스레드가 동작하지 않아 경합 시나리오가 성립하지 않음")
        self.assertNotEqual(r.rc, 0, r.text)
        # 안내: 서비스/cron 을 멈추고 다시 실행
        self.assertRegex(r.text, r"멈추|중지|정지")
        self.assertIn("cron", r.text)
        self.assertFalse((out / "manifest.json").exists(), "실패했는데 manifest 가 생성됨")
        # 임시 백업본 정리
        leftovers = [p for p in (list(out.glob("*")) if out.exists() else []) if p.name != "manifest.json"]
        self.assertEqual(leftovers, [], f"임시 파일 잔존: {leftovers}")


# ---------------------------------------------------------------------------
# ⑧ config 번들
# ---------------------------------------------------------------------------


def _read_tar_bytes(path) -> dict:
    with tarfile.open(path) as tf:
        return {m.name: (tf.extractfile(m).read() if m.isfile() else None) for m in tf.getmembers()}


class ConfigBundleTests(SyncOpsBase):
    def test_plaintext_config_bundle(self):
        bundle = self.make_bundle(config=True)
        m = self.manifest(bundle)
        cfg = m["config"]
        self.assertFalse(cfg["encrypted"])
        self.assertEqual(cfg["file"], "config.tar")
        self.assertEqual(cfg["sha256"], sha256_file(bundle / "config.tar"))
        self.assertEqual(stat.S_IMODE((bundle / "config.tar").stat().st_mode), 0o600)
        # 매니페스트: 파일명만 — 값 없음
        names = " ".join(cfg["entries"])
        self.assertIn(".env", names)
        self.assertIn("qg-owners-wif.json", names)
        self.assertIn("qualitygate.service", names)
        raw = (bundle / "manifest.json").read_text()
        for secret in (FAKE_ENV_VALUE, FAKE_SECRET_FILE_VALUE, FAKE_DEPLOY_VALUE, CRON_MARK):
            self.assertNotIn(secret, raw)
        # tar 내용에는 실제 값이 있다
        blobs = _read_tar_bytes(bundle / "config.tar")
        joined = b"".join(v for v in blobs.values() if v)
        self.assertIn(FAKE_ENV_VALUE.encode(), joined)
        self.assertIn(CRON_MARK.encode(), joined)

    @unittest.skipUnless(HAS_OPENSSL, "openssl 필요")
    def test_encrypted_config_bundle(self):
        bundle = self.make_bundle("--passphrase-env", PASS_ENV, config=True)
        m = self.manifest(bundle)
        self.assertTrue(m["config"]["encrypted"])
        self.assertEqual(m["config"]["file"], "config.tar.enc")
        self.assertTrue((bundle / "config.tar.enc").exists())
        self.assertFalse((bundle / "config.tar").exists(), "암호화 시 평문 tar 가 남으면 안 된다")
        raw = (bundle / "config.tar.enc").read_bytes()
        for secret in (FAKE_ENV_VALUE, FAKE_SECRET_FILE_VALUE, FAKE_DEPLOY_VALUE):
            self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(FAKE_PASS, (bundle / "manifest.json").read_text())
        self.assertEqual(m["config"]["sha256"], sha256_file(bundle / "config.tar.enc"))

    @unittest.skipUnless(HAS_OPENSSL, "openssl 필요")
    def test_passphrase_never_on_command_line(self):
        recorded = []

        class RecPopen(_REAL_POPEN):
            def __init__(self, args, *a, **kw):
                recorded.append(args if isinstance(args, str) else " ".join(map(str, args)))
                super().__init__(args, *a, **kw)

        with mock.patch.object(subprocess, "Popen", RecPopen):
            self.make_bundle("--passphrase-env", PASS_ENV, config=True)
        self.assertTrue(recorded, "subprocess 호출이 관찰되지 않음 (openssl 기대)")
        for cmd in recorded:
            self.assertNotIn(FAKE_PASS, cmd)

    def test_no_config_flag_omits_config(self):
        bundle = self.make_bundle(config=False)
        self.assertIsNone(self.manifest(bundle)["config"])
        self.assertFalse((bundle / "config.tar").exists())

    def test_passphrase_env_unset_fails(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("QG_TEST_UNSET_PASS", None)
            r = self.export("--passphrase-env", "QG_TEST_UNSET_PASS", config=True)
        self.assertNotEqual(r.rc, 0, r.text)

    @unittest.skipUnless(HAS_OPENSSL, "openssl 필요")
    def test_encrypted_roundtrip_and_wrong_passphrase(self):
        bundle = self.make_bundle("--passphrase-env", PASS_ENV, config=True)
        cfg_dir = self.tmp / "cfg-ok"
        target = self.tmp / "ok.sqlite3"
        r = self.do_import(
            bundle, target, "--restore-config", "--config-dir", cfg_dir, "--passphrase-env", PASS_ENV
        )
        self.assertTrue(r.ok, r.text)
        self.assertEqual((cfg_dir / ".env").read_text(), f"FAKE_KEY={FAKE_ENV_VALUE}\n")
        self.assertIn(FAKE_SECRET_FILE_VALUE, (cfg_dir / "secrets" / "qg-owners-wif.json").read_text())
        self.assertNotIn(FAKE_PASS, r.text)

        # 잘못된 패스프레이즈
        cfg_bad = self.tmp / "cfg-bad"
        target_bad = self.tmp / "bad.sqlite3"
        with mock.patch.dict(os.environ, {PASS_ENV: WRONG_PASS}):
            r2 = self.do_import(
                bundle, target_bad, "--restore-config", "--config-dir", cfg_bad,
                "--passphrase-env", PASS_ENV,
            )
        self.assertNotEqual(r2.rc, 0, r2.text)
        self.assertFalse((cfg_bad / ".env").exists(), "잘못된 패스프레이즈인데 config 가 풀림")
        self.assertNotIn(WRONG_PASS, r2.text)
        # C9: 패스프레이즈 오류는 DB 교체 전에 거부 → 대상 DB 도 만들어지지 않는다
        self.assertFalse(target_bad.exists(), "잘못된 패스프레이즈인데 DB 가 교체됨")
        self.assert_no_importing_leftover(target_bad)

    @unittest.skipUnless(HAS_OPENSSL, "openssl 필요")
    def test_encrypted_without_passphrase_rejected(self):
        bundle = self.make_bundle("--passphrase-env", PASS_ENV, config=True)
        cfg_dir = self.tmp / "cfg-nopass"
        r = self.do_import(
            bundle, self.tmp / "np.sqlite3", "--restore-config", "--config-dir", cfg_dir
        )
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertFalse((cfg_dir / ".env").exists())

    def test_plaintext_roundtrip_restore(self):
        bundle = self.make_bundle(config=True)
        cfg_dir = self.tmp / "cfg-plain"
        target = self.tmp / "plain.sqlite3"
        r = self.do_import(bundle, target, "--restore-config", "--config-dir", cfg_dir)
        self.assertTrue(r.ok, r.text)
        self.assertEqual((cfg_dir / ".env").read_text(), f"FAKE_KEY={FAKE_ENV_VALUE}\n")
        self.assertIn(FAKE_SECRET_FILE_VALUE, (cfg_dir / "secrets" / "qg-owners-wif.json").read_text())
        self.assertIn(FAKE_DEPLOY_VALUE, (cfg_dir / "deploy" / "qualitygate.service").read_text())
        self.assertEqual(dump_db(target), dump_db(self.src_db))


# ---------------------------------------------------------------------------
# ⑨ restore-config 동작
# ---------------------------------------------------------------------------


def _build_evil_config(bundle: Path, members: list, tmp: Path):
    """유효한 번들의 config.tar 를 악성 tar 로 교체하고 매니페스트 해시를 맞춘다(해시 검증은 통과시킴)."""
    tar_path = Path(bundle) / "config.tar"
    with tarfile.open(tar_path, "w") as tf:
        for info, payload in members:
            tf.addfile(info, io.BytesIO(payload) if payload is not None else None)
    m = json.loads((Path(bundle) / "manifest.json").read_text())
    m["config"]["sha256"] = sha256_file(tar_path)
    (Path(bundle) / "manifest.json").write_text(json.dumps(m))


class RestoreConfigTests(SyncOpsBase):
    def test_existing_files_not_overwritten_incoming_created(self):
        bundle = self.make_bundle(config=True)
        cfg = self.tmp / "cfg"
        cfg.mkdir()
        (cfg / ".env").write_text("OLD_LOCAL_CONTENT\n")
        r = self.do_import(bundle, self.tmp / "t.sqlite3", "--restore-config", "--config-dir", cfg)
        self.assertTrue(r.ok, r.text)
        self.assertEqual((cfg / ".env").read_text(), "OLD_LOCAL_CONTENT\n", ".env 를 덮어썼다")
        incoming = cfg / ".env.incoming"
        self.assertTrue(incoming.exists())
        self.assertEqual(incoming.read_text(), f"FAKE_KEY={FAKE_ENV_VALUE}\n")
        self.assertEqual(stat.S_IMODE(incoming.stat().st_mode), 0o600)

    def test_force_backs_up_then_replaces(self):
        bundle = self.make_bundle(config=True)
        cfg = self.tmp / "cfg"
        cfg.mkdir()
        (cfg / ".env").write_text("OLD_LOCAL_CONTENT\n")
        r = self.do_import(
            bundle, self.tmp / "t.sqlite3", "--restore-config", "--config-dir", cfg, "--force"
        )
        self.assertTrue(r.ok, r.text)
        baks = glob.glob(str(cfg / ".env.bak-*"))
        self.assertEqual(len(baks), 1, baks)
        self.assertEqual(Path(baks[0]).read_text(), "OLD_LOCAL_CONTENT\n")
        self.assertEqual((cfg / ".env").read_text(), f"FAKE_KEY={FAKE_ENV_VALUE}\n")
        self.assertFalse((cfg / ".env.incoming").exists())

    def test_permissions(self):
        bundle = self.make_bundle(config=True)
        cfg = self.tmp / "cfg"
        r = self.do_import(bundle, self.tmp / "t.sqlite3", "--restore-config", "--config-dir", cfg)
        self.assertTrue(r.ok, r.text)
        self.assertEqual(stat.S_IMODE((cfg / ".env").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((cfg / "secrets").stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((cfg / "secrets" / "qg-owners-wif.json").stat().st_mode), 0o600
        )

    def test_crontab_and_systemd_extracted_only_not_installed(self):
        bundle = self.make_bundle(config=True)
        cfg = self.tmp / "cfg"
        r = self.do_import(bundle, self.tmp / "t.sqlite3", "--restore-config", "--config-dir", cfg)
        self.assertTrue(r.ok, r.text)
        rs = cfg / "restored-system"
        self.assertTrue((rs / "crontab.txt").exists())
        self.assertIn(CRON_MARK, (rs / "crontab.txt").read_text())
        # config-dir 최상위로 새어 나오지 않는다
        self.assertFalse((cfg / "crontab.txt").exists())
        self.assertEqual(glob.glob(str(cfg / "*.service")), [])
        # crontab 은 설치되지 않았다 (가짜 crontab 이 -l 외로 호출되지 않음)
        self.assertFalse(self.cron_install_log.exists(), "crontab 을 시스템에 설치하려 했다")
        # 설치 명령 안내
        self.assertIn("crontab", r.text)
        self.assertIn("restored-system", r.text)
        if glob.glob(str(rs / "*.service")) or glob.glob(str(rs / "**" / "*.service"), recursive=True):
            self.assertIn("daemon-reload", r.text)
        # C8: 경로 치환/유닛 방식 주의 안내
        self.assertIn("#QGMIGRATE#", r.text)
        self.assertIn("경로 치환", r.text)
        self.assertIn("docs/prd_server_migration_sync.md §6.3", r.text)

    def _expect_rejected(self, members, escape_path: Path | None = None):
        bundle = self.make_bundle(config=True)
        _build_evil_config(bundle, members, self.tmp)
        cfg = self.tmp / "cfgdir" / "inner"
        r = self.do_import(
            bundle, self.tmp / "evil.sqlite3", "--restore-config", "--config-dir", cfg
        )
        self.assertNotEqual(r.rc, 0, r.text)
        if escape_path is not None:
            self.assertFalse(escape_path.exists(), f"경로 이탈 항목이 풀렸다: {escape_path}")
        return cfg

    def test_path_traversal_rejected(self):
        info = tarfile.TarInfo("../escape.txt")
        payload = b"x"
        info.size = len(payload)
        # config_dir = tmp/cfgdir/inner → ../escape.txt = tmp/cfgdir/escape.txt
        self._expect_rejected([(info, payload)], self.tmp / "cfgdir" / "escape.txt")

    def test_absolute_path_rejected(self):
        target_abs = self.tmp / "abs_escape.txt"
        info = tarfile.TarInfo(str(target_abs))
        payload = b"x"
        info.size = len(payload)
        self._expect_rejected([(info, payload)], target_abs)

    def test_symlink_member_rejected(self):
        victim = self.tmp / "victim-dir"
        victim.mkdir()
        link = tarfile.TarInfo("secrets")
        link.type = tarfile.SYMTYPE
        link.linkname = str(victim)
        f = tarfile.TarInfo("secrets/pwned.txt")
        payload = b"x"
        f.size = len(payload)
        self._expect_rejected([(link, None), (f, payload)], victim / "pwned.txt")


# ---------------------------------------------------------------------------
# ⑩ verify_snapshot
# ---------------------------------------------------------------------------


class VerifyTests(SyncOpsBase):
    def setUp(self):
        super().setUp()
        self.bundle = self.make_bundle()
        self.target = self.tmp / "verify-target.sqlite3"
        shutil.copy(self.src_db, self.target)

    def test_match_ok_with_dir_and_manifest_path(self):
        for arg in (self.bundle, self.bundle / "manifest.json"):
            with self.subTest(arg=str(arg)):
                r = run_cmd("verify_snapshot", arg, "--target-db", self.target)
                self.assertTrue(r.ok, r.text)
                for t in ("items", "notes", "empty_tbl", "django_migrations"):
                    self.assertIn(t, r.text)

    def test_missing_rows_exit_code_1(self):
        conn = sqlite3.connect(str(self.target))
        conn.execute("DELETE FROM items WHERE id <= 5")
        conn.commit()
        conn.close()
        r = run_cmd("verify_snapshot", self.bundle, "--target-db", self.target)
        self.assertEqual(r.rc, 1, r.text)
        self.assertIn("items", r.text)

    def test_newer_target_requires_allow_newer(self):
        conn = sqlite3.connect(str(self.target))
        conn.execute("INSERT INTO notes (body) VALUES ('이관 후 신규')")
        conn.commit()
        conn.close()
        r = run_cmd("verify_snapshot", self.bundle, "--target-db", self.target)
        self.assertEqual(r.rc, 1, r.text)
        r2 = run_cmd("verify_snapshot", self.bundle, "--target-db", self.target, "--allow-newer")
        self.assertTrue(r2.ok, r2.text)

    def test_allow_newer_still_fails_when_fewer(self):
        conn = sqlite3.connect(str(self.target))
        conn.execute("DELETE FROM notes")
        conn.commit()
        conn.close()
        r = run_cmd("verify_snapshot", self.bundle, "--target-db", self.target, "--allow-newer")
        self.assertEqual(r.rc, 1, r.text)

    def test_reports_migration_difference(self):
        conn = sqlite3.connect(str(self.target))
        conn.execute(
            "INSERT INTO django_migrations (app, name, applied) VALUES ('newapp', '0042_new_one', 'x')"
        )
        conn.commit()
        conn.close()
        r = run_cmd("verify_snapshot", self.bundle, "--target-db", self.target, "--allow-newer")
        self.assertIn("0042_new_one", r.text)

    def test_verify_does_not_modify_target(self):
        before = sha256_file(self.target)
        run_cmd("verify_snapshot", self.bundle, "--target-db", self.target)
        self.assertEqual(sha256_file(self.target), before)
        self.assertEqual(
            sorted(os.listdir(self.tmp)).count("verify-target.sqlite3-journal"), 0
        )

    def test_verify_output_states_its_limits(self):
        for target, expect_ok in ((self.target, True),):
            r = run_cmd("verify_snapshot", self.bundle, "--target-db", target)
            self.assertEqual(r.ok, expect_ok, r.text)
            self.assertIn("행 수·마이그레이션만 비교합니다", r.text)
            self.assertIn("내용 무결성은 import 단계의 sha256 이 보증", r.text)
            # 말미에 출력
            self.assertTrue(r.out.rstrip().splitlines()[-1].startswith("참고:"), r.out)
        conn = sqlite3.connect(str(self.target))
        conn.execute("DELETE FROM notes")
        conn.commit()
        conn.close()
        r2 = run_cmd("verify_snapshot", self.bundle, "--target-db", self.target)
        self.assertEqual(r2.rc, 1, r2.text)
        self.assertIn("행 수·마이그레이션만 비교합니다", r2.text)

    def test_missing_table_in_target_is_mismatch(self):
        conn = sqlite3.connect(str(self.target))
        conn.execute("DROP TABLE notes")
        conn.commit()
        conn.close()
        r = run_cmd("verify_snapshot", self.bundle, "--target-db", self.target, "--allow-newer")
        self.assertEqual(r.rc, 1, r.text)


# ---------------------------------------------------------------------------
# ⑪ sync_from_source (ssh / rsync 모킹)
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, text_mode):
        self._b = io.StringIO() if text_mode else io.BytesIO()

    def __iter__(self):
        return iter(())

    def read(self, *a):
        return self._b.read(*a)

    def readline(self, *a):
        return self._b.readline(*a)

    def close(self):
        pass


class _StdinSink:
    def __init__(self, sink):
        self.sink = sink

    def write(self, data):
        self.sink.append(data if isinstance(data, str) else data.decode("utf-8", "replace"))
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


class _FakeProc:
    def __init__(self, args, rc, stdin_sink, text_mode):
        self.args = args
        self.returncode = rc
        self.stdin = _StdinSink(stdin_sink)
        self.stdout = _FakeStream(text_mode)
        self.stderr = _FakeStream(text_mode)
        self._sink = stdin_sink
        self.pid = 4242

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self._sink.append(input if isinstance(input, str) else input.decode("utf-8", "replace"))
        empty = "" if isinstance(self.stdout._b, io.StringIO) else b""
        return (empty, empty)

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass

    terminate = kill

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeRemote:
    """ssh/rsync 를 흉내낸다. 그 외 명령(zstd/openssl 등)은 원본 subprocess 로 위임."""

    def __init__(self, test, bundle_to_serve: Path):
        self.test = test
        self.bundle = bundle_to_serve
        self.calls: list[list[str]] = []
        self.stdin_data: list[str] = []
        self.ssh_rc = 0
        self.ssh_fail_match = None  # 이 문자열이 명령에 있으면 실패
        self.rsync_rc = 0
        self.remote_out = None

    # --- 명령 판별 ---
    @staticmethod
    def _argv(args):
        return shlex.split(args) if isinstance(args, str) else [str(a) for a in args]

    def _handle(self, argv, input_data, text_mode):
        prog = os.path.basename(argv[0])
        self.calls.append(argv)
        if input_data is not None:
            self.stdin_data.append(input_data if isinstance(input_data, str) else input_data.decode("utf-8", "replace"))
        joined = " ".join(argv)
        if prog == "ssh":
            m = re.search(r"--out\s+['\"]?([^\s'\"]+)", joined)
            if m and "export_snapshot" in joined:
                self.remote_out = m.group(1)
            rc = self.ssh_rc
            if self.ssh_fail_match and self.ssh_fail_match in joined:
                rc = 255
            out = "/tmp/qg-sync-fake\n" if "mktemp" in joined else ""
            return rc, (out if text_mode else out.encode())
        if prog == "rsync":
            if self.rsync_rc:
                return self.rsync_rc, ("" if text_mode else b"")
            dest = Path(argv[-1])
            src = next(a for a in argv[1:-1] if re.match(r"^[^\s/-][^\s/]*:/", a) or re.match(r"^[^\s]+@[^\s]+:", a))
            remote_path = src.split(":", 1)[1]
            dest.mkdir(parents=True, exist_ok=True)
            if remote_path.endswith("/"):
                target_dir = dest  # 내용만
            else:
                target_dir = dest / os.path.basename(remote_path)
            shutil.copytree(self.bundle, target_dir, dirs_exist_ok=True)
            return 0, ("" if text_mode else b"")
        raise AssertionError("not faked")

    @staticmethod
    def _is_faked(argv):
        return os.path.basename(argv[0]) in ("ssh", "rsync")

    # --- subprocess 대체 ---
    def run(self, args, *a, **kw):
        argv = self._argv(args)
        if not self._is_faked(argv):
            return _REAL_RUN(args, *a, **kw)
        text_mode = bool(kw.get("text") or kw.get("universal_newlines") or kw.get("encoding"))
        rc, out = self._handle(argv, kw.get("input"), text_mode)
        cp = subprocess.CompletedProcess(argv, rc, out, "" if text_mode else b"")
        if kw.get("check") and rc != 0:
            raise subprocess.CalledProcessError(rc, argv, output=out)
        return cp

    def popen(self, args, *a, **kw):
        argv = self._argv(args)
        if not self._is_faked(argv):
            return _REAL_POPEN(args, *a, **kw)
        text_mode = bool(kw.get("text") or kw.get("universal_newlines") or kw.get("encoding"))
        rc, _ = self._handle(argv, None, text_mode)
        return _FakeProc(argv, rc, self.stdin_data, text_mode)

    def check_call(self, args, *a, **kw):
        kw["check"] = True
        self.run(args, *a, **kw)
        return 0

    def check_output(self, args, *a, **kw):
        kw["check"] = True
        kw["stdout"] = subprocess.PIPE
        return self.run(args, *a, **kw).stdout

    @contextlib.contextmanager
    def patched(self):
        with mock.patch.object(subprocess, "run", self.run), mock.patch.object(
            subprocess, "Popen", self.popen
        ), mock.patch.object(subprocess, "check_call", self.check_call), mock.patch.object(
            subprocess, "check_output", self.check_output
        ):
            yield self

    # --- 조회 ---
    def cmdlines(self):
        return [" ".join(c) for c in self.calls]

    def ssh_calls(self):
        return [c for c in self.cmdlines() if os.path.basename(c.split()[0]) == "ssh"]

    def rsync_calls(self):
        return [c for c in self.cmdlines() if os.path.basename(c.split()[0]) == "rsync"]


class SyncFromSourceTests(SyncOpsBase):
    SRC = "qguser@src.example.invalid"
    SRC_DIR = "/srv/fake/qa_qualitygate_portal"

    def setUp(self):
        super().setUp()
        # 원격에서 만들어졌다고 가정할 번들 (로컬 가짜 DB 로 미리 export)
        self.remote_bundle = self.make_bundle()
        self.local_dir = self.tmp / "inbox"
        self.target = self.tmp / "synced.sqlite3"

    def _args(self, *extra):
        return [
            "--source", self.SRC,
            "--source-dir", self.SRC_DIR,
            "--local-dir", self.local_dir,
            "--target-db", self.target,
            "--ignore-running",
            "--allow-same-host",
            "--yes",
            *extra,
        ]

    # --- dry-run ---
    def test_dry_run_prints_commands_without_running_or_leaking_passphrase(self):
        fake = FakeRemote(self, self.remote_bundle)
        with fake.patched():
            r = run_cmd(
                "sync_from_source", *self._args("--include-config", "--passphrase-env", PASS_ENV, "--dry-run")
            )
        self.assertTrue(r.ok, r.text)
        self.assertEqual(fake.calls, [], "dry-run 인데 ssh/rsync 가 실행됨")
        self.assertIn("ssh", r.text)
        self.assertIn("rsync", r.text)
        self.assertIn("export_snapshot", r.text)
        self.assertIn(self.SRC, r.text)
        self.assertNotIn(FAKE_PASS, r.text)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.local_dir.exists() and any(self.local_dir.iterdir()))

    def test_dry_run_batchmode_and_no_password_prompt(self):
        r = run_cmd("sync_from_source", *self._args("--dry-run"))
        self.assertTrue(r.ok, r.text)
        self.assertIn("BatchMode=yes", r.text)

    # --- 정상 경로 ---
    def test_success_path_pulls_imports_verifies_and_cleans_remote(self):
        fake = FakeRemote(self, self.remote_bundle)
        with fake.patched():
            r = run_cmd("sync_from_source", *self._args())
        self.assertTrue(r.ok, r.text)
        self.assertEqual(dump_db(self.target), dump_db(self.src_db))
        # ssh BatchMode, export 실행, rsync 진행/재개 옵션
        ssh = fake.ssh_calls()
        self.assertTrue(ssh)
        self.assertTrue(all("BatchMode=yes" in c for c in ssh), ssh)
        self.assertTrue(any("export_snapshot" in c and self.SRC_DIR in c for c in ssh), ssh)
        rs = fake.rsync_calls()
        self.assertEqual(len(rs), 1, rs)
        self.assertIn("--partial", rs[0])
        self.assertIn("-a", rs[0].split())
        # 원격 임시 번들 삭제
        self.assertTrue(any(re.search(r"\brm\b", c) for c in ssh), ssh)
        # 로컬 번들이 수신됨
        self.assertTrue(any(self.local_dir.rglob("manifest.json")))

    def test_keep_remote_does_not_delete(self):
        fake = FakeRemote(self, self.remote_bundle)
        with fake.patched():
            r = run_cmd("sync_from_source", *self._args("--keep-remote"))
        self.assertTrue(r.ok, r.text)
        self.assertFalse(any(re.search(r"\brm\b", c) for c in fake.ssh_calls()), fake.ssh_calls())

    def test_passphrase_only_via_stdin(self):
        fake = FakeRemote(self, self.remote_bundle)
        with fake.patched():
            r = run_cmd(
                "sync_from_source", *self._args("--include-config", "--passphrase-env", PASS_ENV)
            )
        # 번들은 --no-config 로 만들어 두었으므로 import 결과와 무관하게, 노출 여부만 검증
        for c in fake.cmdlines():
            self.assertNotIn(FAKE_PASS, c, "패스프레이즈가 명령행에 노출됨")
        self.assertNotIn(FAKE_PASS, r.text, "패스프레이즈가 출력/로그에 노출됨")
        self.assertTrue(
            any(FAKE_PASS in s for s in fake.stdin_data),
            "패스프레이즈가 ssh stdin 으로 전달되지 않음",
        )

    # --- 실패 경로 ---
    def test_ssh_failure_stops_with_resume_guidance(self):
        fake = FakeRemote(self, self.remote_bundle)
        fake.ssh_rc = 255
        with fake.patched():
            r = run_cmd("sync_from_source", *self._args())
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertEqual(fake.rsync_calls(), [], "ssh 실패인데 rsync 진행")
        self.assertFalse(self.target.exists())
        self.assertRegex(r.text, r"재개|다시|재실행")

    def test_remote_export_failure_stops(self):
        fake = FakeRemote(self, self.remote_bundle)
        fake.ssh_fail_match = "export_snapshot"
        with fake.patched():
            r = run_cmd("sync_from_source", *self._args())
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertEqual(fake.rsync_calls(), [])
        self.assertFalse(self.target.exists())
        self.assertRegex(r.text, r"재개|다시|재실행")

    def test_rsync_failure_reports_resume_and_keeps_remote(self):
        fake = FakeRemote(self, self.remote_bundle)
        fake.rsync_rc = 23
        with fake.patched():
            r = run_cmd("sync_from_source", *self._args())
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertFalse(self.target.exists())
        self.assertRegex(r.text, r"재개|다시|재실행")
        self.assertIn("rsync", r.text)
        # 원격 번들은 재개를 위해 지우지 않아야 한다
        self.assertFalse(any(re.search(r"\brm\b", c) for c in fake.ssh_calls()), fake.ssh_calls())

    def test_corrupted_transfer_detected_before_import(self):
        """rsync 가 성공했지만 번들이 깨진 경우: sha256 검증 단계에서 중단, 대상 DB 미생성."""
        corrupted = self.tmp / "corrupted-bundle"
        shutil.copytree(self.remote_bundle, corrupted)
        f = Path(corrupted) / self.manifest(corrupted)["db"]["file"]
        data = f.read_bytes()
        f.write_bytes(data[: max(1, len(data) // 2)])
        fake = FakeRemote(self, corrupted)
        with fake.patched():
            r = run_cmd("sync_from_source", *self._args())
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertFalse(self.target.exists())
        self.assert_no_importing_leftover(self.target)


# ---------------------------------------------------------------------------
# 오프라인 경로: 번들 디렉터리를 임의 수단으로 복사해 와도 import 만으로 동작
# ---------------------------------------------------------------------------


class OfflinePathTests(SyncOpsBase):
    def test_bundle_copied_elsewhere_imports_without_ssh(self):
        bundle = self.make_bundle(config=True)
        moved = self.tmp / "usb" / bundle.name
        shutil.copytree(bundle, moved)
        shutil.rmtree(bundle)
        target = self.tmp / "offline.sqlite3"
        fake = FakeRemote(self, moved)
        with fake.patched():
            r = self.do_import(moved, target)
        self.assertEqual(fake.calls, [], "오프라인 경로인데 ssh/rsync 가 호출됨")
        self.assertTrue(r.ok, r.text)
        self.assertEqual(dump_db(target), dump_db(self.src_db))


# ---------------------------------------------------------------------------
# C1 같은 호스트 가드
# ---------------------------------------------------------------------------


class SameHostGuardTests(SyncOpsBase):
    def test_same_host_same_base_dir_rejected_without_flag(self):
        bundle = self.make_bundle()
        target = self.tmp / "t.sqlite3"
        r = self.do_import(bundle, target, same_host=False)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("원본 서버에 원본 번들을 import 하려 합니다", r.text)
        self.assertIn("--allow-same-host", r.text)
        self.assertFalse(target.exists())
        self.assert_no_importing_leftover(target)

    def test_allow_same_host_flag_permits(self):
        bundle = self.make_bundle()
        target = self.tmp / "t.sqlite3"
        r = self.do_import(bundle, target, same_host=False)
        self.assertNotEqual(r.rc, 0, r.text)
        r2 = self.do_import(bundle, target, "--allow-same-host", same_host=False)
        self.assertTrue(r2.ok, r2.text)
        self.assertEqual(dump_db(target), dump_db(self.src_db))
        self.assertIn("--allow-same-host", r2.text)  # 경고 출력

    def test_different_hostname_needs_no_flag(self):
        bundle = self.make_bundle()
        target = self.tmp / "t.sqlite3"
        with mock.patch("socket.gethostname", return_value="brand-new-server.invalid"):
            r = self.do_import(bundle, target, same_host=False)
        self.assertTrue(r.ok, r.text)
        self.assertEqual(dump_db(target), dump_db(self.src_db))

    def test_same_host_target_equals_source_db_rejected_even_if_base_dir_differs(self):
        bundle = self.make_bundle()
        real = self.tmp / "orig.sqlite3"
        alias = self.tmp / "alias.sqlite3"
        alias.symlink_to(real)  # realpath 로 비교되어야 한다
        m = self.manifest(bundle)
        m["source"]["base_dir"] = "/somewhere/else/entirely"
        m["db"]["source_path"] = str(real)
        self.write_manifest(bundle, m)
        r = self.do_import(bundle, alias, same_host=False)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("원본 서버에 원본 번들을 import 하려 합니다", r.text)
        self.assertIn("원본 DB", r.text)
        self.assertFalse(real.exists())

    def test_same_host_other_base_dir_without_source_path_is_allowed(self):
        """source_path 가 없는 옛 번들은 hostname+base_dir 만 비교 — base_dir 이 다르면 통과."""
        bundle = self.make_bundle()
        m = self.manifest(bundle)
        m["source"]["base_dir"] = "/somewhere/else/entirely"
        m["db"].pop("source_path", None)
        self.write_manifest(bundle, m)
        target = self.tmp / "t.sqlite3"
        r = self.do_import(bundle, target, same_host=False)
        self.assertTrue(r.ok, r.text)

    def test_sync_from_source_passes_allow_same_host_through(self):
        remote_bundle = self.make_bundle()
        fake = FakeRemote(self, remote_bundle)
        args = ["--source", "qguser@src.example.invalid", "--source-dir", "/srv/fake/qg",
                "--local-dir", self.tmp / "inbox", "--target-db", self.tmp / "s.sqlite3",
                "--ignore-running", "--yes"]
        with fake.patched():
            r = run_cmd("sync_from_source", *args)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("원본 서버에 원본 번들을 import 하려 합니다", r.text)
        fake2 = FakeRemote(self, remote_bundle)
        with fake2.patched():
            r2 = run_cmd("sync_from_source", *args, "--allow-same-host")
        self.assertTrue(r2.ok, r2.text)


# ---------------------------------------------------------------------------
# C3 대상 DB 손상 / OSError·sqlite3.Error 변환
# ---------------------------------------------------------------------------


class CorruptTargetAndErrorConversionTests(SyncOpsBase):
    def _corrupt_target(self):
        t = self.tmp / "corrupt.sqlite3"
        t.write_bytes(b"this is definitely not a sqlite database " * 200)
        return t

    def test_unreadable_target_requires_force(self):
        bundle = self.make_bundle()
        target = self._corrupt_target()
        before = sha256_file(target)
        r = self.do_import(bundle, target)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("대상 DB 를 읽을 수 없음", r.text)
        self.assertIn("손상 의심", r.text)
        self.assertIn("--force", r.text)
        self.assertEqual(sha256_file(target), before)
        self.assertEqual(glob.glob(f"{target}.pre-import-*"), [])
        self.assert_no_importing_leftover(target)

    def test_unreadable_target_with_force_warns_and_replaces_keeping_backup(self):
        bundle = self.make_bundle()
        target = self._corrupt_target()
        before = sha256_file(target)
        r = self.do_import(bundle, target, "--force")
        self.assertTrue(r.ok, r.text)
        self.assertIn("대상 DB 를 읽을 수 없음", r.text)  # 경고로 출력
        self.assertEqual(dump_db(target), dump_db(self.src_db))
        backups = glob.glob(f"{target}.pre-import-*")
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual(sha256_file(backups[0]), before)

    def test_enospc_during_extract_becomes_command_error_and_keeps_db(self):
        bundle = self.make_bundle()
        target = make_db(self.tmp / "existing.sqlite3", items=3, notes=1)
        before = sha256_file(target)
        with mock.patch(
            "apps.syncops.snapshot.decompress_to",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            r = self.do_import(bundle, target, "--force")  # OSError 가 새면 run_cmd 에서 테스트가 ERROR
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("ENOSPC", r.exc or "")
        self.assertNotIn("Traceback", r.text)
        self.assertEqual(sha256_file(target), before)
        self.assert_no_importing_leftover(target)
        self.assertEqual(glob.glob(f"{target}.pre-import-*"), [])

    def test_sqlite_error_becomes_command_error_and_keeps_db(self):
        bundle = self.make_bundle()
        target = make_db(self.tmp / "existing.sqlite3", items=3, notes=1)
        before = sha256_file(target)
        with mock.patch(
            "apps.syncops.snapshot.quick_check",
            side_effect=sqlite3.DatabaseError("database disk image is malformed"),
        ):
            r = self.do_import(bundle, target, "--force")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("SQLite 오류", r.exc or "")
        self.assertEqual(sha256_file(target), before)
        self.assert_no_importing_leftover(target)

    def test_other_oserror_becomes_command_error(self):
        bundle = self.make_bundle()
        target = self.tmp / "t.sqlite3"
        with mock.patch("apps.syncops.snapshot.decompress_to", side_effect=PermissionError(errno.EACCES, "denied")):
            r = self.do_import(bundle, target)
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("파일 시스템 오류", r.exc or "")
        self.assertFalse(target.exists())
        self.assert_no_importing_leftover(target)


# ---------------------------------------------------------------------------
# C4 교체 구간 무공백화 (하드링크 보관)
# ---------------------------------------------------------------------------


class GaplessSwapTests(SyncOpsBase):
    def _existing(self):
        t = Path(make_db(self.tmp / "existing.sqlite3", items=3, notes=1))
        return t, sha256_file(t), os.stat(t).st_ino

    def test_target_path_never_empty_and_old_inode_preserved_via_hardlink(self):
        bundle = self.make_bundle()
        target, before, ino = self._existing()
        real_replace, real_rename = os.replace, os.rename
        seen = {}

        def spy_replace(src, dst, *a, **kw):
            if str(dst) == str(target):
                seen["target_exists_at_replace"] = Path(target).exists()
                pres = glob.glob(f"{target}.pre-import-*")
                seen["pre_ino_at_replace"] = os.stat(pres[0]).st_ino if pres else None
            return real_replace(src, dst, *a, **kw)

        def spy_rename(src, dst, *a, **kw):
            if str(src) == str(target):
                seen["renamed_target"] = True
            return real_rename(src, dst, *a, **kw)

        with mock.patch("os.replace", spy_replace), mock.patch("os.rename", spy_rename):
            r = self.do_import(bundle, target, "--force")
        self.assertTrue(r.ok, r.text)
        self.assertTrue(seen.get("target_exists_at_replace"), "교체 직전 대상 경로가 비어 있었다")
        self.assertEqual(seen.get("pre_ino_at_replace"), ino, "pre-import 가 기존 inode 의 하드링크가 아님")
        self.assertNotIn("renamed_target", seen, "대상 DB 를 rename 으로 옮겼다")
        backups = glob.glob(f"{target}.pre-import-*")
        self.assertEqual(len(backups), 1)
        self.assertEqual(sha256_file(backups[0]), before)
        self.assertEqual(os.stat(backups[0]).st_ino, ino)
        self.assertEqual(dump_db(target), dump_db(self.src_db))
        self.assertNotEqual(os.stat(target).st_ino, ino)

    def test_sidecars_preserved_then_removed_from_target(self):
        bundle = self.make_bundle()
        target, before, ino = self._existing()
        Path(f"{target}-wal").write_bytes(b"old-wal-bytes")
        Path(f"{target}-shm").write_bytes(b"old-shm-bytes")
        r = self.do_import(bundle, target, "--force")
        self.assertTrue(r.ok, r.text)
        (pre,) = glob.glob(f"{target}.pre-import-*[0-9Z]")
        self.assertEqual(Path(f"{pre}-wal").read_bytes(), b"old-wal-bytes")
        self.assertTrue(Path(f"{pre}-shm").exists(), "-shm 이 보관되지 않음")
        self.assertFalse(Path(f"{target}-wal").exists())
        self.assertFalse(Path(f"{target}-shm").exists())

    def test_falls_back_to_rename_with_recovery_hint_when_hardlink_unsupported(self):
        bundle = self.make_bundle()
        target, before, ino = self._existing()
        with mock.patch("os.link", side_effect=OSError(errno.EPERM, "Operation not permitted")):
            r = self.do_import(bundle, target, "--force")
        self.assertTrue(r.ok, r.text)
        self.assertIn("하드링크 불가", r.text)
        self.assertIn("대상 없음 + pre-import 존재", r.text)
        self.assertRegex(r.text, r"mv \S+\.pre-import-\S+ " + re.escape(str(target)))
        (pre,) = glob.glob(f"{target}.pre-import-*")
        self.assertEqual(sha256_file(pre), before)
        self.assertEqual(dump_db(target), dump_db(self.src_db))

    def test_replace_failure_rolls_back_link_mode(self):
        bundle = self.make_bundle()
        target, before, ino = self._existing()
        real_replace = os.replace

        def failing(src, dst, *a, **kw):
            if str(dst) == str(target):
                raise OSError(errno.EIO, "Input/output error")
            return real_replace(src, dst, *a, **kw)

        with mock.patch("os.replace", failing):
            r = self.do_import(bundle, target, "--force")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertEqual(sha256_file(target), before)
        self.assertEqual(os.stat(target).st_ino, ino)
        self.assertEqual(glob.glob(f"{target}.pre-import-*"), [], "롤백 후 보관 링크가 남음")
        self.assert_no_importing_leftover(target)

    def test_replace_failure_rolls_back_rename_fallback(self):
        bundle = self.make_bundle()
        target, before, ino = self._existing()
        real_replace = os.replace

        def failing(src, dst, *a, **kw):
            if str(dst) == str(target):
                raise OSError(errno.EIO, "Input/output error")
            return real_replace(src, dst, *a, **kw)

        with mock.patch("os.link", side_effect=OSError(errno.EPERM, "nope")), mock.patch("os.replace", failing):
            r = self.do_import(bundle, target, "--force")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertTrue(target.exists(), "rename 폴백 롤백 후 대상 DB 가 복구되지 않음")
        self.assertEqual(sha256_file(target), before)
        self.assertEqual(glob.glob(f"{target}.pre-import-*"), [])


# ---------------------------------------------------------------------------
# C9 config 패스프레이즈 사전 검증
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAS_OPENSSL, "openssl 필요")
class ConfigPassphrasePreflightTests(SyncOpsBase):
    def test_wrong_passphrase_rejected_before_db_swap_existing_db_untouched(self):
        bundle = self.make_bundle("--passphrase-env", PASS_ENV, config=True)
        target = make_db(self.tmp / "existing.sqlite3", items=3, notes=1)
        before = sha256_file(target)
        with mock.patch.dict(os.environ, {PASS_ENV: WRONG_PASS}):
            r = self.do_import(
                bundle, target, "--force", "--restore-config", "--config-dir", self.tmp / "cfg",
                "--passphrase-env", PASS_ENV,
            )
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("패스프레이즈", r.text)
        self.assertIn("DB 는 건드리지 않았습니다", r.text)
        self.assertEqual(sha256_file(target), before)
        self.assertEqual(glob.glob(f"{target}.pre-import-*"), [])
        self.assert_no_importing_leftover(target)
        self.assertNotIn(WRONG_PASS, r.text)

    def test_correct_passphrase_passes_preflight(self):
        bundle = self.make_bundle("--passphrase-env", PASS_ENV, config=True)
        target = self.tmp / "ok.sqlite3"
        r = self.do_import(
            bundle, target, "--restore-config", "--config-dir", self.tmp / "cfg", "--passphrase-env", PASS_ENV
        )
        self.assertTrue(r.ok, r.text)
        self.assertIn("패스프레이즈 검증", r.text)

    def test_truncated_encrypted_config_rejected_before_db_swap(self):
        bundle = self.make_bundle("--passphrase-env", PASS_ENV, config=True)
        enc = bundle / "config.tar.enc"
        enc.write_bytes(enc.read_bytes()[:20])  # 첫 블록도 못 채움
        m = self.manifest(bundle)
        m["config"]["sha256"] = sha256_file(enc)
        self.write_manifest(bundle, m)
        target = self.tmp / "t.sqlite3"
        r = self.do_import(
            bundle, target, "--restore-config", "--config-dir", self.tmp / "cfg", "--passphrase-env", PASS_ENV
        )
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertFalse(target.exists())


# ---------------------------------------------------------------------------
# C10 export 중 SIGHUP/SIGTERM
# ---------------------------------------------------------------------------


@unittest.skipUnless(threading.current_thread() is threading.main_thread(), "signal 은 메인 스레드에서만")
class ExportSignalTests(SyncOpsBase):
    def _run_with_signal(self, signum, out, **kw):
        """압축 단계(.partial 이 이미 존재)에서 자기 자신에게 시그널을 보낸다."""
        real = __import__("apps.syncops.snapshot", fromlist=["compress_file"]).compress_file
        seen = {}

        def sending(src, dst, method, log=None):
            seen["partial_existed"] = Path(src).exists()
            os.kill(os.getpid(), signum)
            time_left = 0
            while time_left < 50:  # 핸들러가 예외를 던질 때까지 바이트코드 실행
                time_left += 1
            return real(src, dst, method, log)

        with mock.patch("apps.syncops.snapshot.compress_file", sending):
            r = run_cmd("export_snapshot", "--out", out, "--db-path", self.src_db, "--no-config", *kw.get("args", []))
        return r, seen

    def test_sigterm_cleans_partial_and_created_out_dir_and_restores_handler(self):
        before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        out = self.out_root / "interrupted"
        r, seen = self._run_with_signal(signal.SIGTERM, out)
        self.assertTrue(seen.get("partial_existed"), "시그널 시점에 .partial 이 있어야 시나리오가 성립")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("SIGTERM", r.exc or "")
        self.assertFalse(out.exists(), "시그널 후 출력 디렉터리가 남음")
        for s_, h in before.items():
            self.assertEqual(signal.getsignal(s_), h, "명령 종료 후 시그널 핸들러가 원복되지 않음")

    def test_sighup_cleans_partial_in_preexisting_empty_out_dir(self):
        out = self.out_root / "pre-existing-empty"
        out.mkdir()
        r, seen = self._run_with_signal(signal.SIGHUP, out)
        self.assertTrue(seen.get("partial_existed"))
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertIn("SIGHUP", r.exc or "")
        self.assertTrue(out.is_dir())
        self.assertEqual(list(out.iterdir()), [], "임시 파일 잔존")

    def test_handler_restored_after_normal_export_too(self):
        before = signal.getsignal(signal.SIGTERM)
        r = self.export()
        self.assertTrue(r.ok, r.text)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_dry_run_does_not_install_handler_or_write(self):
        before = signal.getsignal(signal.SIGTERM)
        r = run_cmd("export_snapshot", "--db-path", self.src_db, "--dry-run", "--no-config", "--out", self.out_root / "d")
        self.assertTrue(r.ok, r.text)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)


# ---------------------------------------------------------------------------
# C6 .gitignore
# ---------------------------------------------------------------------------


class GitignoreTests(unittest.TestCase):
    NAMES = [
        ".env.incoming", ".env.bak-20260101T000000Z", "secrets/qg-owners-wif.json.incoming",
        "deploy/qualitygate.service.bak-20260101T000000Z", "restored-system/crontab.txt",
        "db.sqlite3.pre-import-20260101T000000Z", "db.sqlite3.importing", "db.sqlite3.git-clone",
    ]

    @unittest.skipUnless(shutil.which("git"), "git 필요")
    def test_import_artifacts_are_gitignored_but_db_is_not(self):
        repo = Path(__file__).resolve().parents[2]
        if not (repo / ".git").exists():
            self.skipTest("git 저장소 아님")
        for name in self.NAMES:
            rc = _REAL_RUN(["git", "-C", str(repo), "check-ignore", "-q", name]).returncode
            self.assertEqual(rc, 0, f"{name} 가 .gitignore 에 없음")
        rc = _REAL_RUN(["git", "-C", str(repo), "check-ignore", "-q", "db.sqlite3"]).returncode
        self.assertNotEqual(rc, 0, "db.sqlite3 (추적 파일) 는 무시되면 안 됨")


# ---------------------------------------------------------------------------
# C5 sync_from_source 강화
# ---------------------------------------------------------------------------


class SyncHardeningTests(SyncOpsBase):
    SRC = "qguser@src.example.invalid"
    SRC_DIR = "/srv/fake/qa_qualitygate_portal"

    def setUp(self):
        super().setUp()
        self.remote_bundle = self.make_bundle()
        self.local_dir = self.tmp / "inbox"
        self.target = self.tmp / "synced.sqlite3"

    def _args(self, *extra, same_host=True):
        a = ["--source", self.SRC, "--source-dir", self.SRC_DIR, "--local-dir", self.local_dir,
             "--target-db", self.target, "--ignore-running", "--yes"]
        if same_host:
            a.append("--allow-same-host")
        return [*a, *extra]

    def _sync(self, *extra, fake=None, **kw):
        fake = fake or FakeRemote(self, self.remote_bundle)
        with fake.patched():
            r = run_cmd("sync_from_source", *self._args(*extra, **kw))
        return r, fake

    # (a) 실패 메시지에 원격 번들 경로 + 정리 명령
    def test_every_early_failure_reports_remote_bundle_path_and_cleanup(self):
        # ① ssh export 실패
        fake = FakeRemote(self, self.remote_bundle)
        fake.ssh_rc = 255
        r, _ = self._sync(fake=fake)
        self.assertNotEqual(r.rc, 0)
        self.assertIn(f"{self.SRC_DIR}/snapshots/qg-", r.out)
        self.assertIn("원격에 번들이 남아 있음", r.out)
        self.assertRegex(r.out, r"ssh .*rm -rf -- ")
        # ② rsync 실패
        fake = FakeRemote(self, self.remote_bundle)
        fake.rsync_rc = 23
        r, _ = self._sync(fake=fake)
        self.assertIn("원격에 번들이 남아 있음", r.out)
        self.assertRegex(r.out, r"rm -rf -- \S*snapshots/qg-")
        # ③ 검증 실패 (손상 번들)
        corrupted = self.tmp / "corrupted"
        shutil.copytree(self.remote_bundle, corrupted)
        f = corrupted / self.manifest(corrupted)["db"]["file"]
        f.write_bytes(f.read_bytes()[:40])
        fake = FakeRemote(self, corrupted)
        shutil.rmtree(self.local_dir, ignore_errors=True)
        r, fk = self._sync(fake=fake)
        self.assertNotEqual(r.rc, 0)
        self.assertIn("원격에 번들이 남아 있음", r.out)
        self.assertFalse(any(re.search(r"\brm\b", c) for c in fk.ssh_calls()), "검증 실패인데 원격 번들을 지움")

    # (b) 원격 삭제는 ③ 직후, 이후 실패는 로컬 번들로 재개
    def test_remote_deleted_right_after_verification_before_import(self):
        make_db(self.target, items=3, notes=1)  # import 가 '대상에 데이터 있음' 으로 실패하도록
        r, fake = self._sync()
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertTrue(any(re.search(r"\brm\b", c) for c in fake.ssh_calls()),
                        "④ 실패 전에 원격 번들이 이미 삭제됐어야 한다")
        self.assertIn("이미 삭제됨", r.out)
        self.assertNotIn("원격에 번들이 남아 있음", r.out)
        m = re.search(r"python manage\.py import_snapshot (\S+)", r.out)
        self.assertIsNotNone(m, r.out)
        self.assertTrue(m.group(1).startswith(str(self.local_dir)), m.group(1))
        self.assertTrue((Path(m.group(1)) / "manifest.json").exists(), "재개 명령이 가리키는 로컬 번들이 없음")

    def test_keep_remote_preserves_and_still_reports_cleanup_after_import_failure(self):
        make_db(self.target, items=3, notes=1)
        r, fake = self._sync("--keep-remote")
        self.assertNotEqual(r.rc, 0, r.text)
        self.assertFalse(any(re.search(r"\brm\b", c) for c in fake.ssh_calls()))
        self.assertIn("원격에 번들이 남아 있음", r.out)

    # (c) 재개 명령은 사용자가 준 옵션을 재구성
    def test_resume_command_reconstructs_all_user_options(self):
        fake = FakeRemote(self, self.remote_bundle)
        fake.rsync_rc = 23
        r, _ = self._sync(
            "--ssh-opts", "-p 2222 -i /home/u/.ssh/k", "--include-config", "--passphrase-env", PASS_ENV,
            "--restore-config", "--force", "--service-port", str(self.port), "--keep-remote",
            "--max-backup-seconds", "321", "--compress", "gzip", fake=fake,
        )
        self.assertNotEqual(r.rc, 0)
        line = next(l for l in r.out.splitlines() if "재개 방법" in l)
        for tok in ("--source", self.SRC, "--source-dir", self.SRC_DIR, "--remote-bundle", "--include-config",
                    "--passphrase-env", PASS_ENV, "--restore-config", "--force", "--yes", "--ignore-running",
                    "--allow-same-host", "--keep-remote", "--target-db", str(self.target),
                    "--service-port", str(self.port), "--ssh-opts", "-p 2222 -i /home/u/.ssh/k",
                    "--local-dir", str(self.local_dir)):
            self.assertIn(tok, line)
        self.assertNotIn(FAKE_PASS, r.text)

    def test_import_resume_command_carries_import_options(self):
        make_db(self.target, items=3, notes=1)
        r, _ = self._sync("--service-port", str(self.port), "--skip-migrate")
        line = next(l for l in r.out.splitlines() if "재개 방법" in l)
        for tok in ("import_snapshot", "--target-db", str(self.target), "--yes", "--ignore-running",
                    "--allow-same-host", "--skip-migrate", "--service-port", str(self.port)):
            self.assertIn(tok, line)

    # (d) --max-backup-seconds / --compress 통과
    def test_max_backup_seconds_and_compress_forwarded_to_remote_export(self):
        r, fake = self._sync("--max-backup-seconds", "300", "--compress", "gzip")
        self.assertTrue(r.ok, r.text)
        export = next(c for c in fake.ssh_calls() if "export_snapshot" in c)
        self.assertIn("--max-backup-seconds 300", export)
        self.assertIn("--compress gzip", export)

    def test_options_not_forwarded_when_not_given(self):
        r, fake = self._sync()
        export = next(c for c in fake.ssh_calls() if "export_snapshot" in c)
        self.assertNotIn("--max-backup-seconds", export)
        self.assertNotIn("--compress", export)

    # (e) 기본 ssh keepalive
    def test_default_ssh_opts_include_keepalive(self):
        r, fake = self._sync()
        self.assertTrue(r.ok, r.text)
        for c in fake.ssh_calls():
            self.assertIn("ServerAliveInterval=30", c)
            self.assertIn("ServerAliveCountMax=20", c)
            self.assertIn("BatchMode=yes", c)
        rs = fake.rsync_calls()[0]  # rsync -e 에도 전달
        self.assertIn("ServerAliveInterval=30", rs)

    def test_user_ssh_opts_are_added_and_can_override_keepalive(self):
        r, fake = self._sync("--ssh-opts", "-p 2222 -o ServerAliveInterval=5")
        self.assertTrue(r.ok, r.text)
        export = next(c for c in fake.ssh_calls() if "export_snapshot" in c)
        self.assertIn("-p 2222", export)
        self.assertIn("ServerAliveCountMax=20", export)
        # ssh 는 먼저 나온 -o 가 우선 → 사용자 값이 기본값보다 앞에 있어야 한다
        self.assertLess(export.index("ServerAliveInterval=5"), export.index("ServerAliveInterval=30"))

    # (f) --source 검증 / '--' 구분자
    def test_invalid_source_rejected_without_running_anything(self):
        bad = ["-oProxyCommand=touch /tmp/pwn", "-x", "--help", "us er@host", "user@host;rm -rf /",
               "user@host:/x y", "$(id)@host", "", "a@b@c"]
        for src in bad:
            with self.subTest(source=src):
                fake = FakeRemote(self, self.remote_bundle)
                with fake.patched():
                    r = run_cmd("sync_from_source", "--source", src, "--source-dir", self.SRC_DIR, "--dry-run")
                self.assertNotEqual(r.rc, 0, f"{src!r} 가 통과함: {r.text}")
                self.assertIn("--source", r.exc or "")
                self.assertEqual(fake.calls, [])

    def test_valid_source_forms_accepted(self):
        for src in ("qguser@src.example.invalid", "10.0.0.5", "user@[::1]", "host-1.internal", "a_b.c@10.1.2.3"):
            with self.subTest(source=src):
                r = run_cmd("sync_from_source", "--source", src, "--source-dir", self.SRC_DIR, "--dry-run")
                self.assertTrue(r.ok, r.text)

    def test_double_dash_separator_used_for_ssh_and_rsync(self):
        r, fake = self._sync()
        self.assertTrue(r.ok, r.text)
        for argv in fake.calls:
            prog = os.path.basename(argv[0])
            if prog in ("ssh", "rsync"):
                self.assertIn("--", argv, argv)
                dd = argv.index("--")
                if prog == "ssh":
                    self.assertEqual(argv[dd + 1], self.SRC, argv)
                else:
                    self.assertTrue(argv[dd + 1].startswith(self.SRC + ":"), argv)

    def test_dry_run_shows_double_dash_and_keepalive(self):
        r = run_cmd("sync_from_source", *self._args("--dry-run"))
        self.assertTrue(r.ok, r.text)
        self.assertIn(f"-- {self.SRC}", r.out)
        self.assertIn("ServerAliveInterval=30", r.out)
