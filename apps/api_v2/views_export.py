"""서버 이관용 DB 스냅샷 내보내기 API (`/api/v2/export/`).

- POST   /export/snapshots/                      스냅샷 생성 시작 (백그라운드 스레드, 202)
- GET    /export/snapshots/<id>/                 상태/진행률/manifest/파일 목록
- GET    /export/snapshots/<id>/download/<name>/ 파일 스트리밍 (Range 지원)
- DELETE /export/snapshots/<id>/                 번들 삭제

설계 요점
- 인증은 API Key 만(세션 불가) + `export:read` 명시 스코프 + 키 소유자 admin (`ExportPermission`).
- 번들은 `<EXPORT_DIR>/api-<uuid>/` — 기존 syncops 번들 형식(manifest.json + db.sqlite3.*).
  **config.tar 는 만들지 않는다**(include_config=False 고정, config/.env/secrets 는 이 API 로 내보내지 않음).
- 상태는 DB 가 아니라 번들 디렉터리 안의 `state.json` (새 모델/마이그레이션 없음).
- `export_snapshot` 은 비어 있지 않은 out_dir 을 거부하므로, 작업은 `api-<id>/work/` 에서 하고
  완료 시 manifest.json + DB 파일만 `api-<id>/` 로 옮긴다(같은 파일시스템 rename, 즉시).
- 동시에 1개만 실행. 프로세스가 죽어 'running' 으로 남은 상태는 조회 시 failed 로 정리,
  24시간 지난 번들은 파일 삭제 후 `expired` 표식만 남기고 다시 24시간 뒤 디렉터리째 제거.
- API Key 평문은 어디에도 남기지 않는다 (로그/감사에는 소유자 username 과 key_prefix 만).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.http import StreamingHttpResponse
from rest_framework import status as http
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema

from apps.syncops import snapshot as _snap

from .authentication import ApiKeyAuthentication
from .permissions import ExportPermission
from .throttling import ReadThrottle, WriteThrottle

logger = logging.getLogger("apps.api_v2.export")

# ── 상수 ──
TTL_SECONDS = 24 * 3600            # 스냅샷 만료
TOMBSTONE_SECONDS = 24 * 3600      # 만료 후 `expired` 표식을 더 유지하는 시간
STALE_HEARTBEAT_SECONDS = 300      # 다른 프로세스가 쓴 running 상태의 하트비트 허용 한도
STREAM_CHUNK = 1024 * 1024
STATE_NAME = "state.json"
MANIFEST_NAME = _snap.MANIFEST_NAME
DB_FILE_NAMES = ("db.sqlite3.zst", "db.sqlite3.gz", "db.sqlite3")
# 다운로드 허용 이름 (경로 구분자/.. 가 들어갈 여지가 없는 고정 목록과만 비교)
ALLOWED_DOWNLOAD_NAMES = frozenset((MANIFEST_NAME,) + DB_FILE_NAMES)
ALLOWED_COMPRESS = ("auto", "gzip", "zstd")
_DIR_PREFIX = "api-"
_STAGE_PCT = {"[1/4]": 5, "[2/4]": 75, "[3/4]": 85, "[4/4]": 95}
_PCT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")

_LOCK = threading.RLock()
_ACTIVE: dict[str, threading.Thread] = {}  # snapshot_id -> 실행 중 스레드 (이 프로세스)


class ExportError(Exception):
    """표준 에러 응답(`{"error":{"code","message"}}`)으로 변환되는 도메인 예외 (exceptions._code_for 참조)."""

    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


# ---------------------------------------------------------------------------
# 경로 / 상태 파일
# ---------------------------------------------------------------------------

def _root() -> Path:
    custom = getattr(settings, "SYNCOPS_EXPORT_DIR", None)
    return Path(custom) if custom else Path(settings.BASE_DIR) / "snapshots"


def _db_path() -> Path:
    custom = getattr(settings, "SYNCOPS_EXPORT_DB_PATH", None)
    return Path(custom) if custom else Path(str(settings.DATABASES["default"]["NAME"]))


def _parse_id(raw: str) -> str:
    """uuid 형식 검증 후 정규화된(소문자 하이픈) 문자열 반환. 아니면 404 — 경로 조작 방지."""
    try:
        u = uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise ExportError("not_found", "스냅샷을 찾을 수 없습니다.", http.HTTP_404_NOT_FOUND)
    if str(u) != str(raw):  # 대문자/중괄호/하이픈 없음 등 비정규 표기는 거부
        raise ExportError("not_found", "스냅샷을 찾을 수 없습니다.", http.HTTP_404_NOT_FOUND)
    return str(u)


def _bundle_dir(snap_id: str) -> Path:
    return _root() / f"{_DIR_PREFIX}{snap_id}"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _read_state(bundle: Path) -> dict | None:
    try:
        with open(bundle / STATE_NAME, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(bundle: Path, state: dict) -> None:
    """state.json 원자적 교체 (0600)."""
    tmp = bundle / (STATE_NAME + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, bundle / STATE_NAME)


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, ValueError, TypeError):
        return True  # 확인 불가 — 살아있다고 보고 하트비트로 판단
    return True


def _is_stale_running(snap_id: str, state: dict) -> bool:
    """'running' 으로 남았지만 실제로는 아무도 돌리지 않는 상태인가."""
    t = _ACTIVE.get(snap_id)
    if t is not None and t.is_alive():
        return False
    pid = state.get("pid")
    if pid == os.getpid():
        return True  # 같은 프로세스인데 작업 스레드가 없음 → 고아
    if pid and not _pid_alive(pid):
        return True
    hb = state.get("heartbeat")
    try:
        return (time.time() - float(hb)) > STALE_HEARTBEAT_SECONDS
    except (TypeError, ValueError):
        return True


def _purge_files(bundle: Path) -> None:
    """state.json 만 남기고 번들 내용을 모두 제거."""
    try:
        for child in bundle.iterdir():
            if child.name == STATE_NAME:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    except OSError:
        pass


def _reconcile(snap_id: str) -> dict | None:
    """디스크 상태를 읽어 stale/만료를 정리한 뒤 최신 state 반환 (없으면 None)."""
    with _LOCK:
        bundle = _bundle_dir(snap_id)
        if not bundle.is_dir():
            return None
        state = _read_state(bundle)
        if state is None:
            # state.json 이 없거나 깨진 디렉터리 — 쓸모없는 잔재. 활성 작업이 없으면 제거.
            t = _ACTIVE.get(snap_id)
            if t is None or not t.is_alive():
                shutil.rmtree(bundle, ignore_errors=True)
            return None
        status = state.get("status")
        created = _parse_iso(state.get("created_at"))
        age = (_now() - created).total_seconds() if created else TTL_SECONDS + 1

        if status == "running" and _is_stale_running(snap_id, state):
            shutil.rmtree(bundle / "work", ignore_errors=True)
            state.update(status="failed", finished_at=_iso(_now()),
                         error="서버 프로세스가 중단되어 스냅샷 생성이 실패했습니다.")
            _write_state(bundle, state)
            status = "failed"

        if status in ("ready", "failed") and age > TTL_SECONDS:
            _purge_files(bundle)
            state.update(status="expired", progress_pct=None)
            _write_state(bundle, state)
            logger.info("export snapshot expired id=%s", snap_id)
            return state
        if status == "expired" and age > TTL_SECONDS + TOMBSTONE_SECONDS:
            shutil.rmtree(bundle, ignore_errors=True)
            return None
        return state


def _sweep() -> None:
    """모든 api-* 번들에 대해 stale/만료 정리를 수행 (생성/조회 시 호출)."""
    root = _root()
    try:
        names = [p.name for p in root.iterdir() if p.is_dir() and p.name.startswith(_DIR_PREFIX)]
    except OSError:
        return
    for name in names:
        try:
            _reconcile(str(uuid.UUID(name[len(_DIR_PREFIX):])))
        except ValueError:
            continue  # 형식이 다른 디렉터리는 건드리지 않는다


def _running_id() -> str | None:
    """현재 실행 중(=stale 아님)인 스냅샷 id. 없으면 None."""
    root = _root()
    try:
        names = [p.name for p in root.iterdir() if p.is_dir() and p.name.startswith(_DIR_PREFIX)]
    except OSError:
        names = []
    for name in names:
        try:
            sid = str(uuid.UUID(name[len(_DIR_PREFIX):]))
        except ValueError:
            continue
        state = _reconcile(sid)
        if state and state.get("status") == "running":
            return sid
    with _LOCK:
        for sid, t in _ACTIVE.items():
            if t.is_alive():
                return sid
    return None


# ---------------------------------------------------------------------------
# 백그라운드 작업
# ---------------------------------------------------------------------------

def _run_export(snap_id: str, db_path: Path, method: str) -> None:
    bundle = _bundle_dir(snap_id)
    work = bundle / "work"
    state = _read_state(bundle) or {}

    def save(**kw):
        state.update(kw)
        state["heartbeat"] = time.time()
        try:
            _write_state(bundle, state)
        except OSError:  # pragma: no cover - 디렉터리가 외부에서 삭제됨
            pass

    def on_log(msg: str) -> None:
        stripped = msg.strip()
        for tag, pct in _STAGE_PCT.items():
            if stripped.startswith(tag):
                save(progress_pct=pct)
                return

    def on_progress(msg: str) -> None:
        m = _PCT_RE.search(msg or "")
        if m:
            backup_pct = float(m.group(1))
            save(progress_pct=min(75, 5 + int(backup_pct * 0.70)))

    try:
        manifest = _snap.export_snapshot(
            db_path=db_path,
            base_dir=Path(settings.BASE_DIR),
            out_dir=work,
            compress=method,
            include_config=False,  # config/.env/secrets 는 절대 내보내지 않는다
            log=on_log,
            progress=on_progress,
        )
        db_file = manifest["db"]["file"]
        # 허용된 두 종류의 파일만 최종 번들로 옮긴다 (rename — 같은 파일시스템).
        os.replace(work / db_file, bundle / db_file)
        os.chmod(bundle / db_file, 0o600)
        os.replace(work / MANIFEST_NAME, bundle / MANIFEST_NAME)  # manifest 존재 = 번들 완성
        os.chmod(bundle / MANIFEST_NAME, 0o600)
        shutil.rmtree(work, ignore_errors=True)
        save(status="ready", progress_pct=100, finished_at=_iso(_now()), error=None)
        logger.info("export snapshot ready id=%s", snap_id)
    except BaseException as e:  # noqa: BLE001 — 스레드 종료 전 반드시 상태 기록
        shutil.rmtree(work, ignore_errors=True)
        for n in DB_FILE_NAMES + (MANIFEST_NAME,):
            (bundle / n).unlink(missing_ok=True)
        if isinstance(e, _snap.SnapshotError):
            msg = str(e)
        else:
            logger.exception("export snapshot failed id=%s", snap_id)
            msg = "내부 오류로 스냅샷 생성에 실패했습니다."
        if bundle.is_dir():
            save(status="failed", finished_at=_iso(_now()), error=msg)
        if not isinstance(e, Exception):
            raise
    finally:
        with _LOCK:
            _ACTIVE.pop(snap_id, None)


def _start_export(method: str) -> dict:
    with _LOCK:
        _sweep()
        running = _running_id()
        if running is not None:
            raise ExportError("export_in_progress",
                              "이미 실행 중인 스냅샷이 있습니다.", http.HTTP_409_CONFLICT)
        db_path = _db_path()
        if not db_path.is_file():
            raise ExportError("export_unavailable",
                              "내보낼 DB 파일을 찾을 수 없습니다.", http.HTTP_503_SERVICE_UNAVAILABLE)
        snap_id = str(uuid.uuid4())
        root = _root()
        old_umask = os.umask(0o077)
        try:
            root.mkdir(parents=True, exist_ok=True)
            bundle = _bundle_dir(snap_id)
            bundle.mkdir(mode=0o700)
            os.chmod(bundle, 0o700)
        finally:
            os.umask(old_umask)
        created_at = _iso(_now())
        state = {
            "id": snap_id,
            "status": "running",
            "progress_pct": 0,
            "created_at": created_at,
            "finished_at": None,
            "error": None,
            "compress": method,
            "pid": os.getpid(),
            "heartbeat": time.time(),
        }
        _write_state(bundle, state)
        t = threading.Thread(target=_run_export, args=(snap_id, db_path, method),
                             name=f"export-{snap_id[:8]}", daemon=True)
        _ACTIVE[snap_id] = t
        t.start()
    return {"id": snap_id, "status": "running", "created_at": created_at}


# ---------------------------------------------------------------------------
# 응답 조립
# ---------------------------------------------------------------------------

def _load_manifest(bundle: Path) -> dict | None:
    try:
        with open(bundle / MANIFEST_NAME, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _file_entries(bundle: Path, manifest: dict) -> list[dict]:
    """다운로드 가능한 파일 목록 — manifest.json + DB 파일 (이름/크기/sha256)."""
    out = []
    try:
        mf = bundle / MANIFEST_NAME
        out.append({"name": MANIFEST_NAME, "size": mf.stat().st_size, "sha256": _snap.sha256_file(mf)})
    except OSError:
        pass
    db = manifest.get("db") or {}
    name = db.get("file")
    if name in DB_FILE_NAMES:
        try:
            out.append({"name": name, "size": (bundle / name).stat().st_size,
                        "sha256": db.get("file_sha256")})
        except OSError:
            pass
    return out


def _status_payload(snap_id: str, state: dict) -> dict:
    status = state.get("status")
    manifest = None
    files: list[dict] = []
    if status == "ready":
        bundle = _bundle_dir(snap_id)
        manifest = _load_manifest(bundle)
        if manifest:
            files = _file_entries(bundle, manifest)
    pct = state.get("progress_pct")
    return {
        "id": snap_id,
        "status": status,
        "progress_pct": int(pct) if isinstance(pct, (int, float)) else None,
        "created_at": state.get("created_at"),
        "finished_at": state.get("finished_at"),
        "error": state.get("error"),
        "manifest": manifest,
        "files": files,
    }


# 구간(Range) 다운로드 ActivityLog 의 중복 억제 TTL — (key, snapshot, name) 당 이 시간 안에 최초 1회 기록
DOWNLOAD_AUDIT_TTL = 3600


def _audit(request, action_label: str, snap_id: str, db_action: str,
           extra: str = "", to_activity_log: bool = True) -> None:
    """감사 — logger(항상) + 기존 ActivityLog (새 enum 값 없이 기존 값 재사용). 평문 키는 기록하지 않는다."""
    api_key = getattr(request, "auth", None)
    prefix = getattr(api_key, "key_prefix", "")
    logger.info("export %s id=%s user=%s key_prefix=%s%s", action_label, snap_id,
                getattr(request.user, "username", "?"), prefix, f" {extra}" if extra else "")
    if not to_activity_log:
        return
    try:
        from apps.activitylog.models import ActivityLog
        ActivityLog.log(
            user=request.user,
            action=db_action,
            target_type=ActivityLog.TargetType.USER,
            target_name=f"[DB 내보내기 API] {action_label} api-{snap_id}",
            detail=f"key_prefix={prefix}" + (f" {extra}" if extra else ""),
        )
    except Exception:  # noqa: BLE001 — 감사 실패가 본 요청을 깨뜨리지 않게
        logger.warning("export audit log failed id=%s", snap_id)


# ---------------------------------------------------------------------------
# Range / 스트리밍
# ---------------------------------------------------------------------------

# 자릿수 상한(18): 수천 자리 정수는 Python 의 int 변환 한도(4300자리)에서 ValueError → 500 이 되므로 정규식 단계에서 416 으로 거른다.
_RANGE_RE = re.compile(r"^bytes=(\d{0,18})-(\d{0,18})$")


def _parse_range(header: str, size: int):
    """None: Range 헤더 없음 / (start, end): 유효(양끝 포함) / "invalid": 416."""
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return "invalid"  # 다중 구간(콤마)·다른 단위·형식 오류
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return "invalid"
    if a == "":  # 접미 구간: 마지막 n 바이트
        n = int(b)
        if n == 0 or size == 0:
            return "invalid"
        return (max(0, size - n), size - 1)
    start = int(a)
    end = int(b) if b != "" else size - 1
    if start >= size or end < start:
        return "invalid"
    return (start, min(end, size - 1))


def _iter_file(path: Path, start: int, length: int):
    """청크 단위로 파일 일부를 읽어 내보낸다 (메모리에 전체를 올리지 않음)."""
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


class _IgnoreClientContentNegotiation(DefaultContentNegotiation):
    """Accept 헤더와 무관하게 JSON 렌더러 사용 (다운로드 클라이언트가 octet-stream 을 요구해도 406 방지)."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return (renderers[0], renderers[0].media_type)


# ---------------------------------------------------------------------------
# 뷰
# ---------------------------------------------------------------------------

class _ExportBase(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [ExportPermission]
    renderer_classes = [JSONRenderer]
    content_negotiation_class = _IgnoreClientContentNegotiation


@extend_schema(exclude=True)
class SnapshotCreateView(_ExportBase):
    throttle_classes = [WriteThrottle]

    def post(self, request):
        data = request.data
        if data in (None, ""):
            data = {}
        if not isinstance(data, Mapping):
            raise ExportError("invalid_parameters", "요청 본문은 JSON 객체여야 합니다.",
                              http.HTTP_400_BAD_REQUEST)
        extra = [k for k in data.keys() if k != "compress"]
        if extra:
            # config/.env/secrets 등은 이 API 로 내보내지 않는다 — 알 수 없는 필드도 모두 거부.
            raise ExportError(
                "unsupported",
                "지원하지 않는 필드입니다 (허용: compress). config/.env/secrets 는 이 API 로 내보낼 수 없습니다.",
                http.HTTP_400_BAD_REQUEST)
        compress = data.get("compress", "auto")
        if not isinstance(compress, str) or compress.lower() not in ALLOWED_COMPRESS:
            raise ExportError("invalid_parameters", "compress 는 gzip|zstd|auto 중 하나여야 합니다.",
                              http.HTTP_400_BAD_REQUEST)
        try:
            method = _snap.pick_compression(compress.lower())
        except _snap.SnapshotError as e:
            raise ExportError("invalid_parameters", str(e), http.HTTP_400_BAD_REQUEST)

        payload = _start_export(method)
        _audit(request, "create", payload["id"], "create")
        return Response(payload, status=http.HTTP_202_ACCEPTED)


@extend_schema(exclude=True)
class SnapshotDetailView(_ExportBase):
    def get_throttles(self):
        cls = WriteThrottle if self.request.method == "DELETE" else ReadThrottle
        return [cls()]

    def get(self, request, snapshot_id):
        snap_id = _parse_id(snapshot_id)
        _sweep()
        state = _reconcile(snap_id)
        if state is None:
            raise ExportError("not_found", "스냅샷을 찾을 수 없습니다.", http.HTTP_404_NOT_FOUND)
        return Response(_status_payload(snap_id, state))

    def delete(self, request, snapshot_id):
        snap_id = _parse_id(snapshot_id)
        with _LOCK:
            state = _reconcile(snap_id)
            if state is None:
                raise ExportError("not_found", "스냅샷을 찾을 수 없습니다.", http.HTTP_404_NOT_FOUND)
            if state.get("status") == "running":
                raise ExportError("export_in_progress",
                                  "실행 중인 스냅샷은 삭제할 수 없습니다. 완료/실패 후 다시 시도하세요.",
                                  http.HTTP_409_CONFLICT)
            shutil.rmtree(_bundle_dir(snap_id), ignore_errors=True)
        _audit(request, "delete", snap_id, "delete")
        return Response(status=http.HTTP_204_NO_CONTENT)


@extend_schema(exclude=True)
class SnapshotDownloadView(_ExportBase):
    # 대용량 Range 재개 다운로드가 분당 요청 한도에 걸리지 않도록 throttle 은 적용하지 않는다
    # (admin + export:read 명시 키만 접근 가능).
    throttle_classes: list = []

    def get(self, request, snapshot_id, name):
        snap_id = _parse_id(snapshot_id)
        if name not in ALLOWED_DOWNLOAD_NAMES:  # allowlist 비교 — 경로 구분자/.. 는 여기서 탈락
            raise ExportError("not_found", "파일을 찾을 수 없습니다.", http.HTTP_404_NOT_FOUND)
        _sweep()
        state = _reconcile(snap_id)
        if state is None:
            raise ExportError("not_found", "스냅샷을 찾을 수 없습니다.", http.HTTP_404_NOT_FOUND)
        if state.get("status") != "ready":
            raise ExportError("export_not_ready", "스냅샷이 아직 준비되지 않았거나 사용할 수 없습니다.",
                              http.HTTP_409_CONFLICT)
        bundle = _bundle_dir(snap_id)
        path = bundle / name
        if path.is_symlink() or not path.is_file():
            raise ExportError("not_found", "파일을 찾을 수 없습니다.", http.HTTP_404_NOT_FOUND)

        size = path.stat().st_size
        if name == MANIFEST_NAME:
            sha = _snap.sha256_file(path)
            ctype = "application/json"
        else:
            manifest = _load_manifest(bundle) or {}
            sha = (manifest.get("db") or {}).get("file_sha256")
            ctype = "application/octet-stream"

        rng = _parse_range(request.META.get("HTTP_RANGE", ""), size)
        if rng == "invalid":
            resp = Response(
                {"error": {"code": "range_not_satisfiable", "message": "요청한 Range 를 처리할 수 없습니다."}},
                status=http.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE)
            resp["Content-Range"] = f"bytes */{size}"
            resp["Accept-Ranges"] = "bytes"
            return resp

        if rng is None:
            start, end, code = 0, size - 1, 200
        else:
            (start, end), code = rng, 206
        length = max(0, end - start + 1)

        # 감사: 모든 요청을 logger 에 남기고(구간 포함), ActivityLog 는 처음부터 받는 요청(start==0)은 매번,
        # 그 외 구간/재개 요청은 (key, 스냅샷, 파일) 당 TTL 안에 최초 1회만 남긴다 (start!=0 요청의 감사 누락 방지).
        range_info = f"range={start}-{end}/{size}"
        to_db = start == 0
        if not to_db:
            key_id = getattr(getattr(request, "auth", None), "pk", None) or request.user.pk
            to_db = cache.add(f"export-dl-audit:{key_id}:{snap_id}:{name}", 1, DOWNLOAD_AUDIT_TTL)
        _audit(request, f"download {name}", snap_id, "run", extra=range_info, to_activity_log=to_db)

        resp = StreamingHttpResponse(_iter_file(path, start, length), status=code, content_type=ctype)
        resp["Content-Length"] = str(length)
        resp["Accept-Ranges"] = "bytes"
        if code == 206:
            resp["Content-Range"] = f"bytes {start}-{end}/{size}"
        if sha:
            resp["ETag"] = f'"{sha}"'
            resp["X-Content-SHA256"] = sha
        resp["Content-Disposition"] = f'attachment; filename="{name}"'
        resp["Cache-Control"] = "private, no-store"
        return resp
