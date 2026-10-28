"""서버 이관/전체 데이터 동기화용 스냅샷 라이브러리.

번들 형식 (디렉터리 ``snapshots/qg-<UTC yyyymmddTHHMMSSZ>/``, 권한 0700, 파일 0600)::

    manifest.json        메타데이터 (schema_version=1)
    db.sqlite3.zst       DB 백업 (또는 .gz / 무압축 db.sqlite3)
    config.tar           비밀/설정 (또는 config.tar.enc — openssl aes-256-cbc)

설계 원칙
- 운영 DB 는 항상 ``mode=ro`` 로만 연다. 백업은 sqlite3 backup API (청크 복사).
- 함수는 DB 경로/디렉터리를 인자로 받는다 (Django settings 비의존 → 테스트 용이).
- 비밀 값(.env / secrets / 패스프레이즈)은 로그·예외·매니페스트에 남기지 않는다. 파일명만.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import gzip
import hashlib
import io
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
CHUNK = 8 * 1024 * 1024
BACKUP_PAGES = 1024
BACKUP_SLEEP = 0.01
DEFAULT_MAX_BACKUP_SECONDS = 1800
DEFAULT_SERVICE_PORT = 8601

# 대상 DB 에 "사용자 데이터가 있는지" 판단할 때 무시하는 시스템 테이블
# (fresh migrate 만 해도 채워지는 테이블).
SYSTEM_TABLES = frozenset(
    {
        "django_migrations",
        "django_content_type",
        "auth_permission",
        "sqlite_sequence",
        "django_site",
    }
)

CONFIG_PASS_ENV_FIXED = "QG_SNAPSHOT_PASS"  # sync_from_source 가 원격에서 쓰는 고정 환경변수명

Log = Callable[[str], None]


class SnapshotError(Exception):
    """스냅샷 작업 실패 (사용자에게 그대로 보여줄 수 있는 메시지)."""


class SnapshotInterrupted(SnapshotError):
    """SIGHUP/SIGTERM 수신으로 중단 (기존 실패 정리 경로가 임시 산출물을 지운다)."""


@contextlib.contextmanager
def interrupt_guard(signums=(signal.SIGHUP, signal.SIGTERM)):
    """with 블록 동안 SIGHUP/SIGTERM 을 SnapshotInterrupted 로 바꿔 던진다 (ssh 단절·kill 대비).

    - 첫 시그널만 예외로 변환하고, 정리 중 도착하는 추가 시그널은 무시한다.
    - 블록이 끝나면 원래 핸들러를 복원한다. 메인 스레드가 아니면(signal 설치 불가) 아무것도 하지 않는다.
    """
    state = {"fired": False}

    def handler(signum, frame):
        if state["fired"]:
            return
        state["fired"] = True
        try:
            name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover
            name = str(signum)
        raise SnapshotInterrupted(f"{name} 수신 — 임시 파일을 정리하고 중단합니다.")

    old: dict = {}
    try:
        for sg in signums:
            old[sg] = signal.signal(sg, handler)
    except ValueError:  # 메인 스레드가 아님
        for sg, h in old.items():
            signal.signal(sg, h)
        old = {}
    try:
        yield
    finally:
        for sg, h in old.items():
            try:
                signal.signal(sg, h)
            except (ValueError, OSError):  # pragma: no cover
                pass


def _noop(msg: str) -> None:  # pragma: no cover - trivial
    pass


def _stderr(msg: str) -> None:
    sys.stderr.write(msg)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# 일반 유틸
# ---------------------------------------------------------------------------

def utc_ts() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"  # pragma: no cover


def free_bytes(path: Path) -> int:
    """path(또는 가장 가까운 존재하는 상위) 가 속한 파일시스템의 여유 공간."""
    p = Path(path).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize


def sha256_file(path: Path, chunk: int = CHUNK) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _open_private(path: Path, mode: int = 0o600):
    """0600 으로 새 파일 생성(기존 있으면 덮어씀)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    return os.fdopen(fd, "wb")


# ---------------------------------------------------------------------------
# SQLite (읽기 전용 조회)
# ---------------------------------------------------------------------------

def open_ro(path: Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise SnapshotError(f"DB 파일이 없습니다: {p}")
    uri = "file:" + str(p.resolve()).replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    return conn


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def table_counts(conn: sqlite3.Connection, log: Log = _noop) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in list_tables(conn):
        q = '"' + t.replace('"', '""') + '"'
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
    return out


def read_migrations(conn: sqlite3.Connection) -> list[list[str]]:
    try:
        rows = conn.execute(
            "SELECT app, name FROM django_migrations ORDER BY app, name"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [[a, n] for a, n in rows]


def quick_check(conn: sqlite3.Connection) -> None:
    res = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
    if res != ["ok"]:
        raise SnapshotError("PRAGMA quick_check 실패: " + "; ".join(res[:5]))


def user_data_tables(conn: sqlite3.Connection) -> dict[str, int]:
    """시스템 테이블을 제외하고 행이 1개 이상인 테이블 {이름: 행 수}."""
    out = {}
    for t, c in table_counts(conn).items():
        if t in SYSTEM_TABLES or t.startswith("sqlite_"):
            continue
        if c > 0:
            out[t] = c
    return out


# ---------------------------------------------------------------------------
# 압축 / 해제
# ---------------------------------------------------------------------------

def pick_compression(pref: str = "auto") -> str:
    pref = (pref or "auto").lower()
    if pref not in ("auto", "zstd", "gzip", "none"):
        raise SnapshotError(f"알 수 없는 압축 방식: {pref}")
    if pref == "auto":
        if have("zstd"):
            return "zstd"
        return "gzip"  # pigz 또는 python gzip
    if pref == "zstd" and not have("zstd"):
        raise SnapshotError("zstd CLI 가 없습니다 (--compress gzip|none 사용).")
    return pref


COMPRESS_SUFFIX = {"zstd": ".zst", "gzip": ".gz", "none": ""}
# 압축 후 예상 비율 (디스크 여유/번들 크기 추정용 — 로그 텍스트가 대부분이라 보수적)
EST_RATIO = {"zstd": 0.25, "gzip": 0.30, "none": 1.0}


def compress_file(src: Path, dst: Path, method: str, log: Log = _noop) -> None:
    if method == "none":
        with open(src, "rb") as fi, _open_private(dst) as fo:
            shutil.copyfileobj(fi, fo, CHUNK)
        return
    if method == "zstd":
        cmd = ["zstd", "-T0", "-3", "-q", "-f", "-o", str(dst), str(src)]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True)
        if r.returncode != 0:
            raise SnapshotError("zstd 압축 실패: " + r.stderr.decode(errors="replace")[:300])
        os.chmod(dst, 0o600)
        return
    if method == "gzip":
        if have("pigz"):
            with open(src, "rb") as fi, _open_private(dst) as fo:
                p = subprocess.run(["pigz", "-c", "-3"], stdin=fi, stdout=fo, stderr=subprocess.PIPE)
            if p.returncode != 0:
                raise SnapshotError("pigz 압축 실패: " + p.stderr.decode(errors="replace")[:300])
            return
        with open(src, "rb") as fi, _open_private(dst) as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=3) as gz:
                shutil.copyfileobj(fi, gz, CHUNK)
        return
    raise SnapshotError(f"알 수 없는 압축 방식: {method}")


def decompress_to(src: Path, dst: Path, method: str) -> str:
    """src 를 dst 로 스트리밍 해제(상수 메모리). 해제된 데이터의 sha256 hex 반환."""
    h = hashlib.sha256()
    if method == "none":
        with open(src, "rb") as fi, _open_private(dst) as fo:
            while True:
                b = fi.read(CHUNK)
                if not b:
                    break
                h.update(b)
                fo.write(b)
        return h.hexdigest()
    if method == "gzip":
        try:
            with gzip.open(src, "rb") as fi, _open_private(dst) as fo:
                while True:
                    b = fi.read(CHUNK)
                    if not b:
                        break
                    h.update(b)
                    fo.write(b)
        except (EOFError, gzip.BadGzipFile, zlib.error) as e:
            raise SnapshotError(f"gzip 해제 실패 (번들 손상/잘림): {e}") from e
        return h.hexdigest()
    if method == "zstd":
        if not have("zstd"):
            raise SnapshotError("zstd CLI 가 없습니다 — 이 서버에 zstd 를 설치한 뒤 다시 실행하세요.")
        proc = subprocess.Popen(
            ["zstd", "-d", "-c", "-q", str(src)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with _open_private(dst) as fo:
            while True:
                b = proc.stdout.read(CHUNK)
                if not b:
                    break
                h.update(b)
                fo.write(b)
        err = proc.stderr.read()
        rc = proc.wait()
        if rc != 0:
            raise SnapshotError("zstd 해제 실패: " + err.decode(errors="replace")[:300])
        return h.hexdigest()
    raise SnapshotError(f"알 수 없는 압축 방식: {method}")


# ---------------------------------------------------------------------------
# DB 백업 (sqlite3 backup API)
# ---------------------------------------------------------------------------

def backup_db(
    src_db: Path,
    dst_tmp: Path,
    max_seconds: int = DEFAULT_MAX_BACKUP_SECONDS,
    progress: Optional[Callable[[str], None]] = _stderr,
    pages: int = BACKUP_PAGES,
    sleep: float = BACKUP_SLEEP,
) -> dict:
    """원본(mode=ro)을 dst_tmp 로 청크 복사. 쓰기가 끼어들어 backup 이 재시작돼
    max_seconds 안에 못 끝나면 SnapshotError.  반환: {restarts, seconds}."""
    start = time.monotonic()
    state = {"last_remaining": None, "restarts": 0, "last_print": 0.0}

    def cb(status, remaining, total):
        now = time.monotonic()
        last = state["last_remaining"]
        if last is not None and remaining > last:
            state["restarts"] += 1  # 원본이 바뀌어 backup 이 처음부터 재시작됨
        state["last_remaining"] = remaining
        if now - start > max_seconds:
            raise SnapshotError(
                f"DB 백업이 {max_seconds}초 안에 끝나지 않았습니다 "
                f"(원본에 쓰기가 계속 끼어들어 {state['restarts']}회 재시작). "
                "서비스·cron 을 멈추고 다시 실행하세요."
            )
        if progress and now - state["last_print"] >= 2.0 and total:
            state["last_print"] = now
            pct = (total - remaining) * 100.0 / total
            progress(f"\r  DB 백업 {pct:5.1f}% ({total - remaining}/{total} pages, 재시작 {state['restarts']}회)")

    src = open_ro(src_db)
    dst = sqlite3.connect(str(dst_tmp))
    try:
        try:
            src.backup(dst, pages=pages, progress=cb, sleep=sleep)
        except SnapshotError:
            raise
        except sqlite3.Error as e:
            raise SnapshotError(f"sqlite backup 실패: {e}") from e
    finally:
        try:
            dst.close()
        finally:
            src.close()
    if progress:
        progress("\r  DB 백업 100.0%" + " " * 40 + "\n")
    try:
        os.chmod(dst_tmp, 0o600)
    except OSError:
        pass
    return {"restarts": state["restarts"], "seconds": round(time.monotonic() - start, 1)}


# ---------------------------------------------------------------------------
# Config (비밀/설정) 번들
# ---------------------------------------------------------------------------

@dataclass
class ConfigEntry:
    arcname: str                 # tar 내 이름 (예: ".env", "secrets/x.json", "crontab.txt", "systemd/u.service")
    path: Optional[Path] = None  # 디스크 파일
    data: Optional[bytes] = None # 메모리 데이터 (crontab)
    mode: int = 0o600


def _list_files(root: Path) -> list[Path]:
    out = []
    if not root.is_dir() or root.is_symlink():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.is_symlink() or not p.is_file():
                continue  # 심볼릭 링크는 따라가지 않음
            out.append(p)
    return out


def get_crontab_text() -> Optional[str]:
    if not have("crontab"):
        return None
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None  # crontab 없음
    return r.stdout


def collect_config_entries(
    base_dir: Path,
    include_systemd: bool = True,
    systemd_dir: Path = Path("/etc/systemd/system"),
    crontab_text: Optional[str] = None,
    use_crontab: bool = True,
) -> list[ConfigEntry]:
    base_dir = Path(base_dir)
    entries: list[ConfigEntry] = []
    env = base_dir / ".env"
    if env.is_file() and not env.is_symlink():
        entries.append(ConfigEntry(".env", path=env, mode=0o600))
    for p in _list_files(base_dir / "secrets"):
        rel = p.relative_to(base_dir).as_posix()
        entries.append(ConfigEntry(rel, path=p, mode=0o600))
    for p in _list_files(base_dir / "deploy"):
        rel = p.relative_to(base_dir).as_posix()
        entries.append(ConfigEntry(rel, path=p, mode=0o644))
    if use_crontab:
        text = crontab_text if crontab_text is not None else get_crontab_text()
        if text is not None:
            entries.append(ConfigEntry("crontab.txt", data=text.encode(), mode=0o600))
    if include_systemd and Path(systemd_dir).is_dir():
        for pat in ("qualitygate*.service", "qg-*.service"):
            for p in sorted(Path(systemd_dir).glob(pat)):
                try:
                    if p.is_symlink() or not p.is_file() or not os.access(p, os.R_OK):
                        continue
                except OSError:
                    continue
                arc = f"systemd/{p.name}"
                if all(e.arcname != arc for e in entries):
                    entries.append(ConfigEntry(arc, path=p, mode=0o644))
    return entries


def _write_tar(entries: list[ConfigEntry], fileobj) -> None:
    with tarfile.open(fileobj=fileobj, mode="w|", format=tarfile.PAX_FORMAT) as tf:
        for e in entries:
            ti = tarfile.TarInfo(e.arcname)
            ti.mode = e.mode
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mtime = int(time.time())
            if e.data is not None:
                ti.size = len(e.data)
                tf.addfile(ti, io.BytesIO(e.data))
            else:
                ti.size = e.path.stat().st_size
                ti.mtime = int(e.path.stat().st_mtime)
                with open(e.path, "rb") as f:
                    tf.addfile(ti, f)


def build_config_bundle(
    entries: list[ConfigEntry],
    out_dir: Path,
    passphrase_env: Optional[str] = None,
) -> dict:
    """config.tar (또는 config.tar.enc) 를 out_dir 에 만든다. 암호화 시 평문 tar 를 디스크에 쓰지 않는다."""
    out_dir = Path(out_dir)
    if passphrase_env:
        if not os.environ.get(passphrase_env):
            raise SnapshotError(f"환경변수 {passphrase_env} 가 비어 있거나 없습니다 (패스프레이즈).")
        if not have("openssl"):
            raise SnapshotError("openssl 이 없어 config 를 암호화할 수 없습니다.")
        dest = out_dir / "config.tar.enc"
        with _open_private(dest) as fo:
            proc = subprocess.Popen(
                ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"env:{passphrase_env}"],
                stdin=subprocess.PIPE, stdout=fo, stderr=subprocess.PIPE,
                env=dict(os.environ),
            )
            try:
                _write_tar(entries, proc.stdin)
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            err = proc.stderr.read()
            rc = proc.wait()
        if rc != 0:
            dest.unlink(missing_ok=True)
            raise SnapshotError("openssl 암호화 실패: " + err.decode(errors="replace")[:200])
        encrypted = True
    else:
        dest = out_dir / "config.tar"
        with _open_private(dest) as fo:
            _write_tar(entries, fo)
        encrypted = False
    os.chmod(dest, 0o600)
    return {
        "file": dest.name,
        "encrypted": encrypted,
        "sha256": sha256_file(dest),
        "entries": [e.arcname for e in entries],
    }


# ---------------------------------------------------------------------------
# Git / 소스 정보
# ---------------------------------------------------------------------------

def _git(base_dir: Path, *args: str) -> Optional[str]:
    if not have("git"):
        return None
    try:
        r = subprocess.run(["git", "-C", str(base_dir), *args], capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def gather_git_info(base_dir: Path) -> dict:
    commit = _git(base_dir, "rev-parse", "HEAD")
    branch = _git(base_dir, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(base_dir, "status", "--porcelain")
    dirty = []
    if status:
        for line in status.splitlines():
            name = line[3:].strip()
            if " -> " in name:
                name = name.split(" -> ")[-1]
            dirty.append(name.strip('"'))
    return {"commit": commit, "branch": branch, "dirty_files": dirty}


def source_info(base_dir: Path) -> dict:
    try:
        import django
        dj = django.get_version()
    except Exception:  # pragma: no cover
        dj = None
    return {
        "hostname": socket.gethostname(),
        "base_dir": str(base_dir),
        "git": gather_git_info(base_dir),
        "python": sys.version.split()[0],
        "django": dj,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def default_out_dir(base_dir: Path) -> Path:
    return Path(base_dir) / "snapshots" / f"qg-{utc_ts()}"


def _dir_has_files(p: Path) -> bool:
    return p.exists() and (not p.is_dir() or any(p.iterdir()))


def _check_export_space(db_path: Path, out_dir: Path, method: str, margin: float = 1.05) -> None:
    size = Path(db_path).stat().st_size
    need = int((size + size * EST_RATIO[method]) * margin)
    free = free_bytes(out_dir)
    if free < need:
        raise SnapshotError(
            f"디스크 여유 부족: {out_dir} 의 여유 {human(free)} < 필요 {human(need)} "
            f"(임시 백업본 {human(size)} + 압축본 예상 {human(size * EST_RATIO[method])}). "
            "--out 을 여유 있는 디스크로 지정하세요."
        )


def plan_export(
    db_path: Path,
    base_dir: Path,
    out_dir: Path,
    compress: str = "auto",
    include_config: bool = True,
    passphrase_env: Optional[str] = None,
    systemd_dir: Path = Path("/etc/systemd/system"),
    use_crontab: bool = True,
    crontab_text: Optional[str] = None,
) -> dict:
    """아무것도 쓰지 않고 계획을 계산 (DB 는 mode=ro). 운영에서 안전."""
    db_path = Path(db_path)
    size = db_path.stat().st_size if db_path.exists() else 0
    method = pick_compression(compress)
    conn = open_ro(db_path)
    try:
        counts = table_counts(conn)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    entries = []
    if include_config:
        entries = collect_config_entries(
            base_dir, systemd_dir=systemd_dir, use_crontab=use_crontab, crontab_text=crontab_text
        )
    cfg_size = sum((e.path.stat().st_size if e.path else len(e.data or b"")) for e in entries)
    free = free_bytes(out_dir)
    need = int((size + size * EST_RATIO[method]) * 1.05)
    return {
        "db_path": str(db_path),
        "db_size": size,
        "page_size": page_size,
        "journal_mode": journal,
        "compression": method,
        "tables": counts,
        "config_entries": [e.arcname for e in entries],
        "config_size": cfg_size,
        "config_encrypted": bool(passphrase_env) and include_config,
        "est_bundle_size": int(size * EST_RATIO[method]) + cfg_size,
        "free": free,
        "need": need,
        "space_ok": free >= need,
        "out_dir": str(out_dir),
    }


def export_snapshot(
    db_path: Path,
    base_dir: Path,
    out_dir: Path,
    compress: str = "auto",
    include_config: bool = True,
    passphrase_env: Optional[str] = None,
    max_backup_seconds: int = DEFAULT_MAX_BACKUP_SECONDS,
    systemd_dir: Path = Path("/etc/systemd/system"),
    use_crontab: bool = True,
    log: Log = _noop,
    progress: Optional[Callable[[str], None]] = _stderr,
    backup_pages: int = BACKUP_PAGES,
    backup_sleep: float = BACKUP_SLEEP,
) -> dict:
    """번들을 만들고 manifest(dict) 를 반환. 실패 시 만든 임시/부분 산출물을 정리."""
    db_path, base_dir, out_dir = Path(db_path), Path(base_dir), Path(out_dir)
    if not db_path.is_file():
        raise SnapshotError(f"DB 파일이 없습니다: {db_path}")
    method = pick_compression(compress)
    if _dir_has_files(out_dir):
        raise SnapshotError(f"출력 디렉터리가 이미 있고 비어 있지 않습니다: {out_dir}")
    if passphrase_env and include_config and not os.environ.get(passphrase_env):
        raise SnapshotError(f"환경변수 {passphrase_env} 가 비어 있거나 없습니다 (패스프레이즈).")

    created_dir = not out_dir.exists()
    old_umask = os.umask(0o077)
    tmp_db = out_dir / "db.sqlite3.partial"
    try:
        # mkdir 직후 시그널/예외가 와도 아래 except 가 만든 디렉터리를 정리하도록 try 안에서 만든다.
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(out_dir, 0o700)
        _check_export_space(db_path, out_dir, method)

        src = open_ro(db_path)
        try:
            page_size = src.execute("PRAGMA page_size").fetchone()[0]
            journal = src.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            src.close()
        original_size = db_path.stat().st_size

        log(f"[1/4] DB 백업 (sqlite backup API, 원본 {human(original_size)}) ...")
        info = backup_db(db_path, tmp_db, max_backup_seconds, progress, backup_pages, backup_sleep)
        log(f"      백업 완료 ({info['seconds']}s, 재시작 {info['restarts']}회)")

        log("[2/4] 백업본 무결성 검사 + 행 수/마이그레이션 수집 ...")
        conn = open_ro(tmp_db)
        try:
            quick_check(conn)
            counts = table_counts(conn)
            migrations = read_migrations(conn)
        finally:
            conn.close()
        db_sha = sha256_file(tmp_db)
        backup_size = tmp_db.stat().st_size

        log(f"[3/4] 압축 ({method}) ...")
        fname = "db.sqlite3" + COMPRESS_SUFFIX[method]
        dest = out_dir / fname
        compress_file(tmp_db, dest, method, log)
        os.chmod(dest, 0o600)
        file_sha = sha256_file(dest)
        tmp_db.unlink()

        config_meta = None
        if include_config:
            log("[4/4] config 번들 (.env, secrets, deploy, crontab, systemd 유닛) ...")
            entries = collect_config_entries(base_dir, systemd_dir=systemd_dir, use_crontab=use_crontab)
            config_meta = build_config_bundle(entries, out_dir, passphrase_env)
            if not config_meta["encrypted"]:
                log("  !! 경고: config 가 암호화 없이 포함됐습니다 (0600). 비밀(.env/secrets)이 평문입니다 — "
                    "전송 중 노출되지 않게 하거나 --passphrase-env 로 암호화하세요.")
        else:
            log("[4/4] config 제외 (--no-config)")

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_iso(),
            "source": source_info(base_dir),
            "db": {
                "engine": "sqlite",
                "original_size": backup_size,
                "source_size": original_size,
                "sha256": db_sha,
                "file": fname,
                "source_path": str(db_path.resolve()),  # 원본 DB 절대경로 (import 같은 호스트 가드용)
                "file_sha256": file_sha,
                "compression": method,
                "page_size": page_size,
                "journal_mode": journal,
            },
            "tables": counts,
            "migrations": migrations,
            "config": config_meta,
            "notes": "backup=sqlite3 backup API (mode=ro 원본). 코드는 git 으로 별도 이관.",
        }
        mf_tmp = out_dir / (MANIFEST_NAME + ".partial")
        with _open_private(mf_tmp) as f:
            f.write(json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        os.replace(mf_tmp, out_dir / MANIFEST_NAME)  # manifest 존재 = 번들 완성
        return manifest
    except BaseException:
        tmp_db.unlink(missing_ok=True)
        if created_dir:
            shutil.rmtree(out_dir, ignore_errors=True)
        else:
            for n in ("db.sqlite3", "db.sqlite3.zst", "db.sqlite3.gz", "config.tar", "config.tar.enc",
                      MANIFEST_NAME + ".partial"):
                (out_dir / n).unlink(missing_ok=True)
        raise
    finally:
        os.umask(old_umask)


# ---------------------------------------------------------------------------
# Manifest / 번들 검증
# ---------------------------------------------------------------------------

def resolve_manifest_path(manifest_or_dir: Path) -> Path:
    p = Path(manifest_or_dir)
    if p.is_dir():
        p = p / MANIFEST_NAME
    if not p.is_file():
        raise SnapshotError(f"manifest 를 찾을 수 없습니다: {p}")
    return p


def load_manifest(manifest_or_dir: Path) -> dict:
    p = resolve_manifest_path(manifest_or_dir)
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SnapshotError(f"manifest 읽기 실패: {e}") from e
    if m.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(
            f"지원하지 않는 manifest schema_version={m.get('schema_version')!r} (지원: {SCHEMA_VERSION}). "
            "이 서버의 코드를 최신으로 갱신하세요."
        )
    for key in ("db", "tables", "migrations"):
        if key not in m:
            raise SnapshotError(f"manifest 에 '{key}' 가 없습니다 (손상된 번들).")
    for key in ("file", "sha256", "file_sha256", "compression"):
        if key not in m["db"]:
            raise SnapshotError(f"manifest.db 에 '{key}' 가 없습니다.")
    if m["db"]["compression"] not in COMPRESS_SUFFIX:
        raise SnapshotError(f"알 수 없는 압축 방식: {m['db']['compression']}")
    return m


def _safe_bundle_file(bundle_dir: Path, name: str) -> Path:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise SnapshotError(f"manifest 의 파일명이 안전하지 않습니다: {name!r}")
    return Path(bundle_dir) / name


def verify_bundle_files(bundle_dir: Path, manifest: dict, log: Log = _noop, check_config: bool = True) -> None:
    """번들 안 파일(db, config)의 존재/sha256 이 manifest 와 일치하는지 확인."""
    bundle_dir = Path(bundle_dir)
    dbf = _safe_bundle_file(bundle_dir, manifest["db"]["file"])
    if not dbf.is_file():
        raise SnapshotError(f"번들에 DB 파일이 없습니다: {dbf.name}")
    log(f"  sha256 검증: {dbf.name} ({human(dbf.stat().st_size)}) ...")
    if sha256_file(dbf) != manifest["db"]["file_sha256"]:
        raise SnapshotError(f"{dbf.name} sha256 불일치 (전송 중 손상?). 번들을 다시 받으세요.")
    cfg = manifest.get("config")
    if cfg and check_config:
        cf = _safe_bundle_file(bundle_dir, cfg["file"])
        if not cf.is_file():
            raise SnapshotError(f"번들에 config 파일이 없습니다: {cf.name}")
        if sha256_file(cf) != cfg["sha256"]:
            raise SnapshotError(f"{cf.name} sha256 불일치 (전송 중 손상?).")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def service_running(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def disk_migration_keys() -> set[tuple[str, str]]:
    """현재 코드(디스크)에 있는 (app, name) 마이그레이션 집합."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, load=False)
    loader.load_disk()
    keys = set(loader.disk_migrations.keys())
    # squash 가 대체하는 원본도 "알려진" 것으로 취급
    for mig in loader.disk_migrations.values():
        for r in getattr(mig, "replaces", []) or []:
            keys.add(tuple(r))
    return keys


def unknown_migrations(manifest: dict, known: set[tuple[str, str]]) -> list[tuple[str, str]]:
    return [tuple(m) for m in manifest["migrations"] if tuple(m) not in known]


def same_host_conflict(manifest: dict, base_dir: Path, target_db: Path) -> Optional[str]:
    """번들이 이 서버(원본)에서 만든 것이고 같은 저장소/같은 DB 로 되돌리려는 경우 사유 문자열, 아니면 None.

    조건: manifest.source.hostname == 이 호스트 이고
      (manifest.source.base_dir == base_dir  또는  import 대상 DB realpath == manifest.db.source_path).
    source_path 가 없는 옛 번들은 hostname + base_dir 만 비교한다.
    """
    src = manifest.get("source") or {}
    host = src.get("hostname")
    if not host or host != socket.gethostname():
        return None
    reasons = []
    sb = src.get("base_dir")
    if sb and os.path.realpath(sb) == os.path.realpath(str(base_dir)):
        reasons.append(f"원본 저장소 경로와 같음({sb})")
    sp = (manifest.get("db") or {}).get("source_path")
    if sp and os.path.realpath(sp) == os.path.realpath(str(target_db)):
        reasons.append(f"대상 DB 가 원본 DB 와 같음({sp})")
    if not reasons:
        return None
    return (
        f"원본 서버에 원본 번들을 import 하려 합니다 (호스트 {host}: {'; '.join(reasons)}). "
        "새 서버에서 실행하세요. 의도한 리허설/복구라면 --allow-same-host."
    )


def verify_config_passphrase(bundle_dir: Path, manifest: dict, passphrase_env: str) -> None:
    """암호화된 config.tar.enc 의 첫 블록(512B)만 복호화해 tar 헤더 유효성으로 패스프레이즈를 검증.
    틀리면 SnapshotError — DB 교체 전에 호출해 DB 를 건드리지 않고 거부하기 위함.
    (암호화 안 된 config 면 아무것도 안 함.)"""
    cfg = manifest.get("config")
    if not cfg or not cfg.get("encrypted"):
        return
    if not have("openssl"):
        raise SnapshotError("openssl 이 없어 config 를 복호화할 수 없습니다.")
    cf = _safe_bundle_file(Path(bundle_dir), cfg["file"])
    proc = subprocess.Popen(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", f"env:{passphrase_env}", "-in", str(cf)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ),
    )
    try:
        block = proc.stdout.read(tarfile.BLOCKSIZE)
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    bad = SnapshotError(
        "config 패스프레이즈가 틀렸거나 config.tar.enc 가 손상됐습니다 — DB 는 건드리지 않았습니다. "
        "올바른 패스프레이즈를 환경변수에 넣고 다시 실행하세요."
    )
    if len(block) < tarfile.BLOCKSIZE:
        raise bad
    if block == tarfile.NUL * tarfile.BLOCKSIZE:
        return  # 항목 없는 빈 tar
    try:
        tarfile.TarInfo.frombuf(block, tarfile.ENCODING, "surrogateescape")
    except tarfile.TarError:
        raise bad from None


@dataclass
class ImportReport:
    target: str
    pre_import: Optional[str] = None
    tables: dict = field(default_factory=dict)
    restored_sidecars: list = field(default_factory=list)


SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def _remove_sidecars(target: Path) -> list[str]:
    removed = []
    for suf in SIDECAR_SUFFIXES:
        p = Path(str(target) + suf)
        if p.exists():
            p.unlink()
            removed.append(p.name)
    return removed


def _preserve_and_swap(importing: Path, target_db: Path, report: "ImportReport", log: Log = _noop) -> Optional[Path]:
    """기존 대상 DB(+사이드카)를 .pre-import-<ts> 로 보관하고 importing 을 target 으로 원자 교체.

    - 하드링크 우선: os.link(target, pre) 후 os.replace(importing, target) 만 수행 → 대상 경로가 비는 구간이 없다.
    - 하드링크 불가 FS 면 rename 으로 폴백(교체 직전까지 대상 경로가 비어 있음 → 복구 안내를 먼저 출력).
    - 어떤 단계든 실패하면 보관본을 원위치/제거해 기존 DB 를 그대로 남긴다.
    """
    if not target_db.exists():
        os.replace(importing, target_db)
        return None
    pre = Path(f"{target_db}.pre-import-{utc_ts()}")
    created: list[Path] = []                      # 하드링크로 만든 보관 경로 (롤백 시 제거)
    renamed: list[tuple[Path, Path]] = []         # rename 폴백으로 옮긴 (원위치, 보관위치)
    try:
        try:
            os.link(target_db, pre)
            created.append(pre)
            mode = "link"
        except OSError as e:
            if e.errno == errno.EEXIST:
                raise
            mode = "rename"
            log(f"  하드링크 불가({e.strerror}) → rename 폴백. 교체 도중 중단되어 '대상 없음 + pre-import 존재' 이면 "
                f"보관본을 원래 이름으로 되돌리세요: mv {pre} {target_db}")
            os.rename(target_db, pre)
            renamed.append((target_db, pre))
        for suf in SIDECAR_SUFFIXES:
            sp = Path(str(target_db) + suf)
            if not sp.exists():
                continue
            dp = Path(str(pre) + suf)
            if mode == "link":
                os.link(sp, dp)
                created.append(dp)
            else:
                os.rename(sp, dp)
                renamed.append((sp, dp))
        os.replace(importing, target_db)
    except BaseException:
        for orig, newp in reversed(renamed):  # 롤백
            try:
                os.rename(newp, orig)
            except OSError:
                pass
        for p in created:
            try:
                p.unlink()
            except OSError:
                pass
        raise
    report.pre_import = str(pre)
    return pre


def import_db(
    bundle_dir: Path,
    manifest: dict,
    target_db: Path,
    force: bool = False,
    log: Log = _noop,
    progress: Optional[Callable[[str], None]] = None,
) -> ImportReport:
    """번들 DB 를 target_db 로 원자적 교체. 실패 시 기존 DB 는 그대로, .importing 정리.
    (서비스 실행/마이그레이션 호환/기존 데이터 사전검증은 호출 측 preflight 에서 수행)"""
    bundle_dir, target_db = Path(bundle_dir), Path(target_db)
    dbmeta = manifest["db"]
    src = _safe_bundle_file(bundle_dir, dbmeta["file"])
    target_db.parent.mkdir(parents=True, exist_ok=True)
    importing = Path(str(target_db) + ".importing")
    importing.unlink(missing_ok=True)

    old_umask = os.umask(0o077)
    try:
        log(f"  압축 해제 → {importing.name} ({dbmeta['compression']}, 스트리밍) ...")
        got = decompress_to(src, importing, dbmeta["compression"])
        if got != dbmeta["sha256"]:
            raise SnapshotError("해제된 DB 의 sha256 이 manifest 와 다릅니다 (번들 손상).")
        log("  sha256 OK. PRAGMA quick_check ...")
        conn = open_ro(importing)
        try:
            quick_check(conn)
            log("  테이블별 행 수 검증 ...")
            counts = table_counts(conn)
        finally:
            conn.close()
        want = manifest["tables"]
        diffs = []
        for t in sorted(set(want) | set(counts)):
            if want.get(t) != counts.get(t):
                diffs.append(f"{t}: manifest={want.get(t)} 실제={counts.get(t)}")
        if diffs:
            raise SnapshotError("행 수 불일치 — " + "; ".join(diffs[:10]))

        report = ImportReport(target=str(target_db), tables=counts)
        _preserve_and_swap(importing, target_db, report, log)
        report.restored_sidecars = _remove_sidecars(target_db)
        return report
    finally:
        importing.unlink(missing_ok=True)
        os.umask(old_umask)


# ---- config 복원 -----------------------------------------------------------

def _classify_member(name: str) -> tuple[str, list[str]]:
    """tar 항목 이름 검증 + 분류. 반환 (kind, parts). 허용 밖/경로 이탈이면 SnapshotError."""
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise SnapshotError(f"config tar 에 허용되지 않는 경로: {name!r}")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise SnapshotError(f"config tar 에 경로 이탈 항목: {name!r}")
    top = parts[0]
    if name == ".env":
        return "env", parts
    if top == "secrets" and len(parts) >= 2:
        return "secrets", parts
    if top == "deploy" and len(parts) >= 2:
        return "deploy", parts
    if name == "crontab.txt":
        return "crontab", parts
    if top == "systemd" and len(parts) == 2:
        return "systemd", parts
    raise SnapshotError(f"config tar 의 예상 밖 항목: {name!r}")


def _dest_for(kind: str, parts: list[str], config_dir: Path) -> Path:
    if kind == "env":
        return config_dir / ".env"
    if kind in ("secrets", "deploy"):
        return config_dir.joinpath(*parts)
    return config_dir / "restored-system" / Path(*parts)


def _ensure_dir(path: Path, mode: int, root: Path) -> None:
    """path 를 만들고(없으면) 심볼릭 링크를 거치지 않았는지 확인."""
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    resolved = path.resolve()
    if not (resolved == root or root in resolved.parents):
        raise SnapshotError(f"복원 경로가 config-dir 밖으로 나갑니다: {path}")
    if path.is_symlink():
        raise SnapshotError(f"복원 경로가 심볼릭 링크입니다: {path}")


def restore_config(
    bundle_dir: Path,
    manifest: dict,
    config_dir: Path,
    passphrase_env: Optional[str] = None,
    force: bool = False,
    log: Log = _noop,
) -> dict:
    """config 번들을 config_dir 에 푼다. 기존 파일은 덮어쓰지 않고 <name>.incoming 으로 저장
    (force 면 <name>.bak-<ts> 보관 후 교체). 시스템(crontab/systemd)에는 설치하지 않는다."""
    cfg = manifest.get("config")
    if not cfg:
        raise SnapshotError("이 번들에는 config 가 없습니다 (--no-config 로 만들어짐).")
    bundle_dir = Path(bundle_dir)
    cf = _safe_bundle_file(bundle_dir, cfg["file"])
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    root = config_dir.resolve()

    proc = None
    fobj = None
    if cfg["encrypted"]:
        if not passphrase_env or not os.environ.get(passphrase_env):
            raise SnapshotError("config 가 암호화돼 있습니다 — --passphrase-env VAR (값이 들어 있는 환경변수) 가 필요합니다.")
        if not have("openssl"):
            raise SnapshotError("openssl 이 없어 config 를 복호화할 수 없습니다.")
        proc = subprocess.Popen(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", f"env:{passphrase_env}", "-in", str(cf)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ),
        )
        stream = proc.stdout
    else:
        fobj = open(cf, "rb")
        stream = fobj

    ts = utc_ts()
    result = {"written": [], "incoming": [], "backed_up": [], "system": []}
    old_umask = os.umask(0o077)
    try:
        try:
            tf = tarfile.open(fileobj=stream, mode="r|")
            for ti in tf:
                if ti.isdir():
                    continue
                if not ti.isfile():
                    raise SnapshotError(f"config tar 에 일반 파일이 아닌 항목(링크/장치 등)이 있습니다: {ti.name!r}")
                kind, parts = _classify_member(ti.name)
                dest = _dest_for(kind, parts, config_dir)
                _ensure_dir(dest.parent, 0o700 if kind in ("secrets", "crontab", "systemd") else 0o755, root)
                if kind in ("secrets", "crontab", "systemd"):
                    os.chmod(dest.parent, 0o700)
                final = dest
                if dest.exists() or dest.is_symlink():
                    if force:
                        bak = Path(f"{dest}.bak-{ts}")
                        os.replace(dest, bak)
                        result["backed_up"].append(bak.name)
                    else:
                        final = Path(f"{dest}.incoming")
                        if final.is_symlink():
                            final.unlink()
                mode = 0o600 if kind in ("env", "secrets", "crontab", "systemd") else (ti.mode & 0o755) or 0o644
                tmp = Path(f"{final}.tmp-{os.getpid()}")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                with os.fdopen(fd, "wb") as out:
                    src = tf.extractfile(ti)
                    shutil.copyfileobj(src, out, 1024 * 1024)
                os.chmod(tmp, mode)
                os.replace(tmp, final)
                rel = final.relative_to(config_dir).as_posix()
                (result["incoming"] if final != dest else result["written"]).append(rel)
                if kind in ("crontab", "systemd"):
                    result["system"].append(rel)
            tf.close()
        except tarfile.TarError as e:
            raise SnapshotError(
                "config tar 를 읽을 수 없습니다 — 패스프레이즈가 틀렸거나 파일이 손상됐습니다."
            ) from e
        if proc is not None:
            err = proc.stderr.read()
            rc = proc.wait()
            if rc != 0:
                raise SnapshotError("openssl 복호화 실패 (패스프레이즈 확인): " + err.decode(errors="replace")[:120])
    finally:
        os.umask(old_umask)
        if fobj:
            fobj.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
    return result


SYSTEM_INSTALL_NOTE = (
    "주의: crontab.txt 는 #QGMIGRATE# 접두(주석)·원본 경로가 들어 있을 수 있어 경로 치환 후 설치, "
    "유닛은 run.sh 가 아닌 새 서버 방식 — docs/prd_server_migration_sync.md §6.3 참조"
)


def system_install_hints(config_dir: Path, result: dict) -> list[str]:
    """crontab/systemd 는 설치하지 않고 명령만 안내."""
    hints = []
    rs = Path(config_dir) / "restored-system"
    if any(r.endswith("crontab.txt") for r in result.get("system", [])):
        hints.append(f"crontab {rs}/crontab.txt")
    units = sorted(r for r in result.get("system", []) if "/systemd/" in r)
    if units:
        for u in units:
            hints.append(f"sudo cp {Path(config_dir) / u} /etc/systemd/system/")
        hints.append("sudo systemctl daemon-reload")
    return hints


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def compare_counts(manifest: dict, target_db: Path, allow_newer: bool = False) -> dict:
    """target_db(mode=ro) 와 manifest 비교. 반환 dict: rows=[(table, want, got, status)], ok, mig_missing, mig_extra."""
    conn = open_ro(target_db)
    try:
        got = table_counts(conn)
        migs = {tuple(m) for m in read_migrations(conn)}
    finally:
        conn.close()
    want = manifest["tables"]
    rows = []
    ok = True
    for t in sorted(want):
        w, g = want[t], got.get(t)
        if g is None:
            status = "MISSING"
            ok = False
        elif g == w:
            status = "ok"
        elif g > w and allow_newer:
            status = f"newer(+{g - w})"
        else:
            status = "MISMATCH"
            ok = False
        rows.append((t, w, g, status))
    for t in sorted(set(got) - set(want)):
        rows.append((t, None, got[t], "extra(매니페스트에 없음)"))
    mset = {tuple(m) for m in manifest["migrations"]}
    mig_missing = sorted(mset - migs)
    mig_extra = sorted(migs - mset)
    if mig_missing:
        ok = False
    return {"rows": rows, "ok": ok, "mig_missing": mig_missing, "mig_extra": mig_extra}


def format_table(rows: Iterable[tuple], headers: tuple[str, ...]) -> list[str]:
    rows = [tuple("-" if c is None else str(c) for c in r) for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    def fmt(r, right=()):
        return "  ".join(
            (c.rjust(widths[i]) if i in right else c.ljust(widths[i])) for i, c in enumerate(r)
        )
    out = [fmt(headers, right=(1, 2)), "  ".join("-" * w for w in widths)]
    out += [fmt(r, right=(1, 2)) for r in rows]
    return out
