"""`python manage.py fetch_snapshot_via_api` — 원본 서버의 Export API 로 DB 스냅샷을 내려받는다 (ssh/rsync 불가 시).

흐름: POST /api/v2/export/snapshots/ (또는 --existing-id) → GET 상태 폴링 → manifest.json · DB 파일 다운로드
(``.part`` + Range 이어받기, sha256 검증) → ``<out-dir>/qg-<UTC ts>/`` 에 export_snapshot 과 같은 번들 형식으로 배치 →
원격 DELETE → ``import_snapshot`` / ``verify_snapshot`` 안내.

- API Key 는 ``--key-env`` 가 가리키는 환경변수에서만 읽는다. 값은 인자·로그·예외 메시지·출력에 남기지 않는다.
- config(.env/secrets) 는 이 API 로 내보내지 않으므로 이 명령도 다루지 않는다 (필요하면 별도 경로로 옮길 것).
- HTTPS 인증서 검증은 항상 켠다(끄는 옵션 없음). 호스트가 바뀌는 리다이렉트는 따라가지 않고 거부한다
  (Authorization 이 다른 호스트로 새지 않게 requests 의 자동 리다이렉트는 쓰지 않는다).
- 실패 메시지에는 항상 원격 snapshot id 와 재개 방법(--existing-id)을 포함한다. 부분 다운로드는
  ``<out-dir>/.fetch-<id>/`` 에 ``.part`` 로 남아 재개 시 Range 로 이어받는다.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
import requests.auth
from django.core.management.base import BaseCommand, CommandError

from apps.syncops import snapshot as sn

API_PATH = "/api/v2/export/snapshots/"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# 서버가 내려줄 수 있는 파일 이름 허용 목록 (경로 조작 방지 — 서버 응답도 신뢰하지 않는다)
ALLOWED_NAME_RE = re.compile(r"(manifest\.json|db\.sqlite3(\.zst|\.gz)?)")
TS_FORMAT = "%Y%m%dT%H%M%SZ"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
MAX_RETRIES = 5  # 5xx/네트워크 오류 연속 재시도 횟수
RETRY_BACKOFF = 2.0  # 시도 n 번째 대기 = RETRY_BACKOFF * n 초 (테스트에서 0 으로 패치)
MAX_REDIRECTS = 3
CHUNK = 64 * 1024  # 중단 시 버려지는 마지막 청크를 작게 (Range 재개 손실 최소화)
PROGRESS_EVERY = 5.0  # 다운로드 진행률 출력 간격(초)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
RETRYABLE_EXC = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)


class FetchError(Exception):
    """사용자에게 그대로 보여줄 수 있는 실패 (키 값은 포함하지 않는다)."""


def validate_url(url: str) -> str:
    """https:// 만 허용 (테스트용 http://127.0.0.1 / localhost 예외). 끝 슬래시 제거한 base URL 반환."""
    parts = urlsplit(url or "")
    host = parts.hostname
    if parts.scheme not in ("https", "http") or not host:
        raise CommandError("--url 은 https://host[/prefix] 형식이어야 합니다.")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise CommandError("--url 은 https:// 만 허용합니다 (http:// 는 127.0.0.1/localhost 테스트용만).")
    if parts.username is not None or parts.password is not None:
        raise CommandError("--url 에 사용자/비밀번호를 넣지 마세요 (키는 --key-env 로만 전달).")
    if parts.query or parts.fragment:
        raise CommandError("--url 에 query/fragment 는 쓸 수 없습니다.")
    return url.rstrip("/")


def _origin(url: str) -> tuple:
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    return (p.scheme, (p.hostname or "").lower(), port)


class _BearerAuth(requests.auth.AuthBase):
    def __init__(self, key: str):
        self._key = key

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {self._key}"
        return request


class Client:
    """Authorization 을 붙인 requests 래퍼. 리다이렉트는 같은 origin 만 수동으로 따라간다."""

    def __init__(self, base_url: str, key: str):
        self.base_url = base_url
        self.key = key
        self.origin = _origin(base_url)
        self.session = requests.Session()
        self.session.trust_env = True  # 사내 프록시 환경변수는 따른다
        # Authorization 헤더를 직접 박으면 requests 가 ~/.netrc 의 Basic 자격으로 덮어쓸 수 있다(trust_env=True 일 때).
        # session.auth 에 Bearer 를 두면 netrc 조회를 건너뛴다.
        self.session.auth = _BearerAuth(key)
        self.session.headers["Accept"] = "application/json"

    def scrub(self, text) -> str:
        return str(text).replace(self.key, "***")

    def request(self, method, url, *, stream=False, headers=None, json=None) -> requests.Response:
        for _ in range(MAX_REDIRECTS + 1):
            resp = self.session.request(
                method, url, stream=stream, headers=headers, json=json,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=False,
            )
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            loc = resp.headers.get("Location")
            resp.close()
            if not loc:
                raise FetchError(f"리다이렉트 응답(HTTP {resp.status_code})에 Location 이 없습니다.")
            nxt = urljoin(url, loc)
            if _origin(nxt) != self.origin:
                # Authorization 이 다른 호스트/스킴으로 새는 것을 막는다 (https→http 다운그레이드 포함).
                raise FetchError("다른 호스트/스킴으로의 리다이렉트는 거부합니다 (Authorization 유출 방지).")
            if resp.status_code == 303:
                method, json = "GET", None
            url = nxt
        raise FetchError("리다이렉트가 너무 많습니다.")


def error_detail(resp: requests.Response) -> tuple[str, str]:
    """v2 오류 형식 {"error":{"code","message"}} 에서 (code, message). 파싱 실패 시 빈 문자열."""
    try:
        err = resp.json().get("error") or {}
        return str(err.get("code") or ""), str(err.get("message") or "")[:200]
    except (ValueError, AttributeError):
        return "", ""


class Command(BaseCommand):
    help = ("원본 서버의 Export API(/api/v2/export/snapshots/)로 DB 스냅샷 번들을 내려받습니다 "
            "(config 는 제외). 키는 --key-env 환경변수에서만 읽습니다.")

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True, help="원본 base URL (https://host[/prefix]; http 는 127.0.0.1/localhost 만)")
        parser.add_argument("--key-env", required=True, metavar="VAR",
                            help="export:read 스코프 API Key 가 들어있는 환경변수명 (값은 명령행으로 넘기지 않음)")
        parser.add_argument("--out-dir", default="snapshots", help="번들 저장 디렉터리 (기본 snapshots)")
        parser.add_argument("--compress", choices=["auto", "gzip", "zstd"], default="auto", help="원격 DB 압축 방식")
        parser.add_argument("--poll-interval", type=float, default=10, help="상태 폴링 간격(초, 기본 10)")
        parser.add_argument("--timeout", type=float, default=14400,
                            help="스냅샷이 ready 가 될 때까지 기다리는 최대 시간(초, 기본 14400)")
        parser.add_argument("--existing-id", help="이미 만들어진 원격 snapshot id 로 재개 (POST 생략)")
        parser.add_argument("--keep-remote", action="store_true", help="성공해도 원격 스냅샷을 DELETE 하지 않음")
        parser.add_argument("--dry-run", action="store_true", help="요청을 보내지 않고 계획만 출력")

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        self._opts = opts
        self._remote_id = opts.get("existing_id")
        self._client: Client | None = None
        try:
            self._run(opts)
        except FetchError as e:
            raise CommandError(self._fail_message(str(e)))
        except requests.RequestException as e:  # 예상 밖 네트워크 오류도 키 없이 보고
            raise CommandError(self._fail_message(f"네트워크 오류: {type(e).__name__}"))
        except OSError as e:
            raise CommandError(self._fail_message(f"파일 시스템 오류: {e.strerror or type(e).__name__}"))

    def _fail_message(self, msg: str) -> str:
        if self._client:
            msg = self._client.scrub(msg)
        if not self._remote_id:
            return msg
        o = self._opts
        resume = (f"manage.py fetch_snapshot_via_api --url {shlex.quote(o['url'])} --key-env {shlex.quote(o['key_env'])} "
                  f"--out-dir {shlex.quote(str(o['out_dir']))} --existing-id {self._remote_id}")
        return (f"{msg}\n  원격 snapshot id: {self._remote_id} (원격에 남아 있음, 24시간 뒤 자동 만료)"
                f"\n  재개: {resume}")

    # ------------------------------------------------------------------
    def _run(self, opts):
        w = self.stdout.write
        base_url = validate_url(opts["url"])
        key_env = opts["key_env"]
        key = (os.environ.get(key_env) or "").strip()
        if not key:
            raise CommandError(f"환경변수 {key_env} 가 비어 있거나 없습니다 (API Key).")
        if opts["poll_interval"] <= 0 or opts["timeout"] <= 0:
            raise CommandError("--poll-interval, --timeout 은 0 보다 커야 합니다.")
        existing = opts.get("existing_id")
        if existing:
            try:
                existing = str(uuid.UUID(existing))
            except ValueError:
                raise CommandError("--existing-id 는 uuid 형식이어야 합니다.")
            self._remote_id = existing
        out_dir = Path(opts["out_dir"])
        api = base_url + API_PATH

        if opts["dry_run"]:
            w(self.style.WARNING("[dry-run] 요청을 보내지 않고 아무것도 쓰지 않습니다."))
            w(f"원본          : {base_url}")
            w(f"API Key       : 환경변수 {key_env} (설정됨)")
            w(f"동작          : {'기존 스냅샷 ' + existing + ' 이어받기' if existing else 'POST 로 새 스냅샷 생성 (압축=' + opts['compress'] + ')'}")
            w(f"저장 위치     : {out_dir}/qg-<manifest.created_at>/ (임시 {out_dir}/.fetch-<id>/)")
            w(f"완료 후 원격  : {'보존 (--keep-remote)' if opts['keep_remote'] else 'DELETE'}")
            w("config(.env/secrets) 는 이 API 로 내보내지 않으므로 다루지 않습니다.")
            return

        self._client = client = Client(base_url, key)

        # 1) 생성 (또는 기존 id 재개)
        if not existing:
            w("[fetch 1/5] 원격 스냅샷 생성 요청 ...")
            existing = self._create(client, api, opts["compress"])
            self._remote_id = existing
            w(f"  snapshot id: {existing}")
        else:
            w(f"[fetch 1/5] 기존 스냅샷 재개: {existing}")
        snap_url = f"{api}{existing}/"

        # 2) ready 까지 폴링
        w("[fetch 2/5] 스냅샷 준비 대기 ...")
        status = self._wait_ready(client, snap_url, opts["poll_interval"], opts["timeout"])
        files = {f["name"]: f for f in status.get("files") or [] if ALLOWED_NAME_RE.fullmatch(str(f.get("name", "")))}
        if "manifest.json" not in files:
            raise FetchError("ready 응답에 manifest.json 이 없습니다.")

        # 3) 다운로드 (.fetch-<id>/ 스테이징)
        out_dir.mkdir(parents=True, exist_ok=True)
        stage = out_dir / f".fetch-{existing}"
        old_umask = os.umask(0o077)
        try:
            stage.mkdir(exist_ok=True)
            os.chmod(stage, 0o700)
            w("[fetch 3/5] manifest.json 다운로드 ...")
            self._download(client, snap_url, "manifest.json", files["manifest.json"], stage, expected_sha=None)
            manifest = self._load_manifest(stage)
            dbname = manifest["db"]["file"]
            if not ALLOWED_NAME_RE.fullmatch(dbname) or dbname == "manifest.json" or dbname not in files:
                raise FetchError(f"manifest 의 DB 파일명이 허용 목록/파일 목록에 없습니다: {dbname!r}")
            if manifest.get("config"):
                raise FetchError("원격 번들에 config 가 포함돼 있습니다 — 이 API 는 config 를 내보내지 않아야 합니다. 거부합니다.")
            size = files[dbname].get("size")
            w(f"[fetch 3/5] {dbname} 다운로드 ({sn.human(size) if isinstance(size, int) else '크기 미상'}) ...")
            self._download(client, snap_url, dbname, files[dbname], stage, expected_sha=manifest["db"]["file_sha256"])

            # 4) 최종 번들 배치
            w("[fetch 4/5] 번들 배치 ...")
            bundle = self._place_bundle(stage, out_dir, manifest, dbname)
        finally:
            os.umask(old_umask)
        w(self.style.SUCCESS(f"  번들 완료: {bundle}"))

        # 5) 원격 정리
        if opts["keep_remote"]:
            w(f"[fetch 5/5] 원격 스냅샷 보존 (--keep-remote): id={existing}")
        else:
            w("[fetch 5/5] 원격 스냅샷 삭제 ...")
            self._delete_remote(client, snap_url)

        w("\n다음 단계 (이 서버에서):")
        w(f"  python manage.py import_snapshot {shlex.quote(str(bundle))} --yes")
        w(f"  python manage.py verify_snapshot {shlex.quote(str(bundle))} --allow-newer")
        w("참고: config(.env/secrets/crontab 등)는 이 API 로 내보내지 않으므로 별도 경로로 옮겨야 합니다.")

    # --- API 호출 ------------------------------------------------------
    def _api_error(self, resp: requests.Response, what: str) -> FetchError:
        code, msg = error_detail(resp)
        hint = ""
        if resp.status_code in (401, 403):
            hint = " — 키가 유효하지 않거나 export:read 스코프가 없습니다 ('*' 스코프에는 포함되지 않으므로 명시 부여 필요)."
        elif resp.status_code == 409 and code == "export_in_progress":
            hint = " — 원격에서 다른 export 가 실행 중입니다. 끝난 뒤 다시 시도하거나 --existing-id 로 재개하세요."
        elif resp.status_code == 400 and code == "unsupported":
            hint = " — 서버가 지원하지 않는 요청입니다."
        detail = f" code={code}" if code else ""
        detail += f" message={msg}" if msg else ""
        return FetchError(f"{what} 실패: HTTP {resp.status_code}{detail}{hint}")

    def _create(self, client, api, compress) -> str:
        try:
            resp = client.request("POST", api, json={"compress": compress})
        except RETRYABLE_EXC as e:
            # POST 는 재시도하지 않는다 (중복 생성 방지). 서버에 이미 만들어졌을 수 있다.
            raise FetchError(f"스냅샷 생성 요청 중 네트워크 오류({type(e).__name__}) — 원격에 생성됐을 수 있으니 "
                             "잠시 후 다시 실행하세요 (이미 실행 중이면 409 export_in_progress).")
        with resp:
            if resp.status_code != 202:
                raise self._api_error(resp, "스냅샷 생성")
            try:
                sid = str(uuid.UUID(str(resp.json().get("id"))))
            except (ValueError, AttributeError):
                raise FetchError("생성 응답에 올바른 id 가 없습니다.")
            return sid

    def _get_status(self, client, snap_url) -> dict:
        """GET 상태. 5xx/네트워크 오류는 MAX_RETRIES 회까지 재시도."""
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                resp = client.request("GET", snap_url)
            except RETRYABLE_EXC as e:
                err = f"네트워크 오류({type(e).__name__})"
            else:
                with resp:
                    if resp.status_code == 200:
                        try:
                            return resp.json()
                        except ValueError:
                            err = "응답이 JSON 이 아님"
                    elif resp.status_code in RETRYABLE_STATUS:
                        err = f"HTTP {resp.status_code}"
                    elif resp.status_code == 404:
                        raise FetchError("원격 스냅샷이 없습니다 (만료/삭제됨 — 새로 생성하려면 --existing-id 없이 실행).")
                    else:
                        raise self._api_error(resp, "상태 조회")
            if attempt > MAX_RETRIES:
                raise FetchError(f"상태 조회 재시도 {MAX_RETRIES}회 실패 ({err}).")
            self.stdout.write(f"  상태 조회 일시 오류 ({err}) — 재시도 {attempt}/{MAX_RETRIES}")
            time.sleep(RETRY_BACKOFF * attempt)

    def _wait_ready(self, client, snap_url, interval, timeout) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            st = self._get_status(client, snap_url)
            state = st.get("status")
            if state == "ready":
                return st
            if state == "failed":
                raise FetchError(f"원격 export 실패: {str(st.get('error'))[:200]}")
            if state == "expired":
                raise FetchError("원격 스냅샷이 만료됐습니다 — --existing-id 없이 새로 실행하세요.")
            if state != "running":
                raise FetchError(f"알 수 없는 상태: {state!r}")
            pct = st.get("progress_pct")
            self.stdout.write(f"  [poll] running {pct if pct is not None else '?'}%")
            if time.monotonic() + interval > deadline:
                raise FetchError(f"{timeout:g}초 안에 ready 가 되지 않았습니다 (--timeout).")
            time.sleep(interval)

    def _delete_remote(self, client, snap_url):
        try:
            resp = client.request("DELETE", snap_url)
            with resp:
                if resp.status_code not in (204, 404):
                    code, _ = error_detail(resp)
                    self.stdout.write(self.style.WARNING(
                        f"  원격 삭제 실패(HTTP {resp.status_code} {code}) — 24시간 뒤 자동 만료됩니다. id={self._remote_id}"))
                    return
        except (requests.RequestException, FetchError) as e:
            self.stdout.write(self.style.WARNING(
                f"  원격 삭제 실패({type(e).__name__}) — 24시간 뒤 자동 만료됩니다. id={self._remote_id}"))
            return
        self.stdout.write("  원격 삭제 완료")

    # --- 다운로드 ------------------------------------------------------
    def _download(self, client, snap_url, name, meta, stage: Path, expected_sha):
        """name 을 stage/<name> 으로 받는다 (.part + Range 이어받기). sha256 검증 후 .part → 최종 이름으로 rename."""
        if not ALLOWED_NAME_RE.fullmatch(name):  # 방어: 호출부가 이미 걸렀지만 경로 조작은 여기서도 차단
            raise FetchError(f"허용되지 않은 파일명: {name!r}")
        final, part = stage / name, stage / (name + ".part")
        total = meta.get("size") if isinstance(meta.get("size"), int) else None
        want_shas = {s for s in (meta.get("sha256"), expected_sha) if s}
        header_sha = None
        url = f"{snap_url}download/{name}/"
        if total is not None:
            need = total - (part.stat().st_size if part.exists() else 0)
            if sn.free_bytes(stage) < need * 1.05:
                raise FetchError(f"디스크 여유 공간 부족: {name} 에 약 {sn.human(need)} 필요 (--out-dir 변경).")

        for attempt in range(1, MAX_RETRIES + 2):
            have_bytes = part.stat().st_size if part.exists() else 0
            if total is not None and have_bytes > total:
                part.unlink()
                have_bytes = 0
            headers = {"Range": f"bytes={have_bytes}-"} if have_bytes else None
            try:
                resp = client.request("GET", url, stream=True, headers=headers)
            except RETRYABLE_EXC as e:
                err = f"네트워크 오류({type(e).__name__})"
            else:
                try:
                    err = self._consume(resp, part, have_bytes, total, name)
                    if err is None:
                        h = resp.headers.get("X-Content-SHA256")
                        header_sha = h.lower() if h else header_sha
                        total = total if total is not None else self._resp_total
                    elif err == "done":  # 416 이지만 이미 완전한 .part
                        h = resp.headers.get("X-Content-SHA256")
                        header_sha = h.lower() if h else header_sha
                        err = None
                except RETRYABLE_EXC as e:
                    err = f"전송 중단({type(e).__name__})"
                finally:
                    resp.close()
            cur = part.stat().st_size if part.exists() else 0
            if err is None and (total is None or cur == total):
                break
            if err is None:
                err = f"받은 크기 {cur} < 기대 {total}"
            if attempt > MAX_RETRIES:
                raise FetchError(f"{name} 다운로드 재시도 {MAX_RETRIES}회 실패 ({err}). 받은 {sn.human(cur)} 는 .part 로 남아 재개 시 이어받습니다.")
            self.stdout.write(f"  {name}: {err} — {sn.human(cur)} 받음, 이어받기 {attempt}/{MAX_RETRIES}")
            time.sleep(RETRY_BACKOFF * attempt)
        else:  # pragma: no cover
            raise FetchError(f"{name} 다운로드 실패")

        digest = sn.sha256_file(part)
        if header_sha:
            want_shas.add(header_sha)
        if not want_shas:
            part.unlink(missing_ok=True)
            raise FetchError(f"{name}: 서버가 sha256 을 알려주지 않아 검증할 수 없습니다 — 거부합니다.")
        if any(digest != s.lower() for s in want_shas):
            part.unlink(missing_ok=True)
            raise FetchError(f"{name}: sha256 불일치 — 손상/변조 의심. 받은 파일을 삭제했습니다. (다시 실행하면 처음부터 받습니다)")
        os.chmod(part, 0o600)
        os.replace(part, final)
        self.stdout.write(f"  {name}: {sn.human(final.stat().st_size)} sha256 OK")

    _resp_total = None

    def _consume(self, resp, part: Path, have_bytes: int, total, name):
        """응답 하나를 .part 에 기록. 반환: None(정상 수신) | "done"(416 + 이미 완전) | 재시도 사유 문자열."""
        sc = resp.status_code
        self._resp_total = None
        if sc == 206:
            m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", resp.headers.get("Content-Range", ""))
            if not m or int(m.group(1)) != have_bytes or not have_bytes:
                part.unlink(missing_ok=True)  # 엉뚱한 구간 — 처음부터 다시
                return "Content-Range 불일치"
            self._resp_total = int(m.group(3)) if m.group(3) != "*" else None
            mode = "ab"
        elif sc == 200:
            mode = "wb"  # 서버가 Range 를 무시하고 전체를 줌 → 처음부터
            cl = resp.headers.get("Content-Length")
            self._resp_total = int(cl) if cl and cl.isdigit() else None
        elif sc == 416:
            if total is not None and have_bytes == total:
                return "done"
            part.unlink(missing_ok=True)
            return "Range 거부(416) — 처음부터 다시"
        elif sc in RETRYABLE_STATUS:
            return f"HTTP {sc}"
        elif sc == 409:
            raise FetchError(f"{name}: 스냅샷이 ready 상태가 아닙니다 (HTTP 409).")
        elif sc == 404:
            raise FetchError(f"{name}: 원격에 파일/스냅샷이 없습니다 (HTTP 404, 만료됐을 수 있음).")
        else:
            raise self._api_error(resp, f"{name} 다운로드")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "ab" else os.O_TRUNC), 0o600)
        written = 0
        last = time.monotonic()
        with os.fdopen(fd, "ab" if mode == "ab" else "wb") as f:
            for chunk in resp.iter_content(CHUNK):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                now = time.monotonic()
                if now - last >= PROGRESS_EVERY:
                    last = now
                    cur = (have_bytes if mode == "ab" else 0) + written
                    t = total or self._resp_total
                    self.stdout.write(f"  {name}: {sn.human(cur)}" + (f" / {sn.human(t)} ({cur * 100 // t}%)" if t else ""))
        if mode == "wb" and self._resp_total is not None and written < self._resp_total:
            return f"받은 크기 {written} < Content-Length {self._resp_total}"
        return None

    # --- manifest / 배치 -------------------------------------------------
    def _load_manifest(self, stage: Path) -> dict:
        try:
            return sn.load_manifest(stage)
        except sn.SnapshotError as e:
            raise FetchError(f"manifest.json 이 올바르지 않습니다: {e}")

    def _place_bundle(self, stage: Path, out_dir: Path, manifest: dict, dbname: str) -> Path:
        try:
            ts = _dt.datetime.strptime(str(manifest.get("created_at")), "%Y-%m-%dT%H:%M:%SZ").strftime(TS_FORMAT)
        except ValueError:
            raise FetchError(f"manifest.created_at 을 해석할 수 없습니다: {manifest.get('created_at')!r}")
        bundle = out_dir / f"qg-{ts}"
        if bundle.exists() and any(bundle.iterdir()):
            raise FetchError(f"이미 같은 번들 디렉터리가 있습니다: {bundle} — 이미 받은 번들이면 그대로 import 하세요 "
                             "(받은 파일은 스테이징에 남겨 둡니다).")
        bundle.mkdir(exist_ok=True)
        os.chmod(bundle, 0o700)
        try:
            os.replace(stage / dbname, bundle / dbname)
            os.replace(stage / "manifest.json", bundle / "manifest.json")  # manifest 존재 = 번들 완성
            for n in (dbname, "manifest.json"):
                os.chmod(bundle / n, 0o600)
        except BaseException:
            shutil.rmtree(bundle, ignore_errors=True)
            raise
        shutil.rmtree(stage, ignore_errors=True)
        return bundle
