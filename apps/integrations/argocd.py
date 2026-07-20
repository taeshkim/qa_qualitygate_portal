import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class ArgoCDError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def parse_app_url(app_url: str) -> tuple[str, str]:
    """Argo CD Application URL 에서 (base_url, app_identifier) 를 뽑는다.

    허용 형태:
      https://argocd.example.com/applications/my-app
      https://argocd.example.com/applications/argocd/my-app            (project/app 형태)
      https://argocd.example.com/applications/argocd/my-app?view=tree  (쿼리/프래그먼트 제거)
      https://argocd.example.com/applications/my-app#resource=...      (프래그먼트 제거)
    """
    if not app_url:
        return "", ""
    parsed = urlparse(app_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return "", ""
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    marker = "/applications/"
    idx = path.find(marker)
    if idx == -1:
        return base, ""
    app_part = path[idx + len(marker):]
    # /applications/ns/app 형태는 그대로 반환. Argo CD API 가 `ns/app` 를 해석.
    app_id = "/".join(unquote(p) for p in app_part.split("/") if p)
    return base, app_id


def normalize_app_url(app_url: str) -> str:
    """사용자가 붙여넣은 Argo CD URL 을 표준 형태로 정리한다.

    - 쿼리 스트링(`?view=tree&resource=...`) 과 프래그먼트(`#...`) 제거
    - 마지막 슬래시 제거
    - 파싱 불가능한 값은 공백 제거만 해서 그대로 반환(사용자가 입력한 값 존중).
    """
    if not app_url:
        return ""
    raw = app_url.strip()
    base, app_id = parse_app_url(raw)
    if base and app_id:
        return f"{base}/applications/{app_id}"
    return raw


class ArgoCDClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""

    def _request(self, path: str, method: str = "GET", body: dict | None = None, timeout: float = 6.0) -> dict:
        url = f"{self.base_url}{path}"
        data = None
        req = Request(url, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, data=data, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            body_text = e.read().decode(errors="replace")[:500]
            logger.warning(f"Argo CD API {e.code} {e.reason}: {body_text}")
            raise ArgoCDError(f"HTTP {e.code}: {e.reason}", status=e.code) from e
        except URLError as e:
            logger.warning(f"Argo CD connection error: {e.reason}")
            raise ArgoCDError(f"Connection failed: {e.reason}") from e

    @staticmethod
    def _app_parts(app_identifier: str) -> tuple[str, str]:
        """app_identifier (`name` 또는 `namespace/name`) 를 (app_name, app_namespace) 로 분해.

        Argo CD UI 의 `/applications/<ns>/<name>` 구조에서 앞 세그먼트는 **namespace** 이다
        (project 가 아님). API 호출 시 `appNamespace` 쿼리/바디로 넘겨야 올바르게 조회된다.
        """
        parts = [p for p in app_identifier.split("/") if p]
        if not parts:
            raise ArgoCDError("empty app identifier")
        if len(parts) == 1:
            return parts[0], ""
        return parts[-1], parts[0]

    def get_application(self, app_identifier: str, refresh: str = "") -> dict:
        """Argo CD Application 상태 조회. refresh='normal'|'hard' 로 재동기화 트리거."""
        name, namespace = self._app_parts(app_identifier)
        extras = {}
        if namespace:
            extras["appNamespace"] = namespace
        if refresh:
            extras["refresh"] = refresh
        qs = ("?" + urlencode(extras)) if extras else ""
        return self._request(f"/api/v1/applications/{name}{qs}")

    def sync_application(self, app_identifier: str, prune: bool = False) -> dict:
        """Argo CD Application 에 sync 요청."""
        name, namespace = self._app_parts(app_identifier)
        body = {"name": name, "prune": prune}
        if namespace:
            body["appNamespace"] = namespace
        qs = f"?appNamespace={namespace}" if namespace else ""
        return self._request(f"/api/v1/applications/{name}/sync{qs}", method="POST", body=body)

    def get_resource_tree(self, app_identifier: str) -> dict:
        """Application 의 자식 리소스 트리 조회 — Pod 까지 health 검증할 때 사용.

        Application JSON 의 `status.resources` 는 directly-managed resource (Deployment 등)
        만 노출하고 그 자식인 Pod/ReplicaSet 은 안 들어온다. resource-tree 가 자식까지 펼쳐
        반환 — `nodes[]` 의 각 항목에 kind/name/health.status 가 들어있다.
        """
        name, namespace = self._app_parts(app_identifier)
        qs = f"?appNamespace={namespace}" if namespace else ""
        return self._request(f"/api/v1/applications/{name}/resource-tree{qs}")

    @staticmethod
    def extract_status(app_json: dict) -> dict:
        status = (app_json or {}).get("status") or {}
        sync = (status.get("sync") or {}).get("status", "")
        health = (status.get("health") or {}).get("status", "")
        operation = status.get("operationState") or {}
        op_phase = operation.get("phase", "")
        op_message = operation.get("message", "") or ""
        # startedAt / finishedAt — Synced + Healthy 가 우리 sync 트리거 이전의 stale
        # 결과인지 판별하기 위한 timestamp. ISO 8601 (예: "2026-05-04T01:23:45Z").
        # finishedAt 만 보면 Argo CD 가 초 단위로 truncate 한 값이 우리 sync_requested_at
        # 보다 ms 단위로 약간 작아 false-positive 가 발생 — startedAt 도 같이 본다.
        op_started_at = operation.get("startedAt", "") or ""
        op_finished_at = operation.get("finishedAt", "") or ""
        return {
            "sync": sync, "health": health,
            "operation_phase": op_phase, "operation_message": op_message,
            "operation_started_at": op_started_at,
            "operation_finished_at": op_finished_at,
        }

    @staticmethod
    def is_synced_and_healthy(app_json: dict) -> bool:
        s = ArgoCDClient.extract_status(app_json)
        # 진행 중인 operation 이 있으면 아직 완료 전.
        if s["operation_phase"] in ("Running", "Terminating"):
            return False
        return s["sync"] == "Synced" and s["health"] == "Healthy"


def classify_argocd_status(sync: str, health: str, op_phase: str, op_message: str = "") -> dict:
    """Argo CD 의 sync / health / operation phase 조합을 사용자 친화적인 배지 정보로 분류.

    UI 의 "Syncing..." 한 가지 표현으로는 사용자가 ``Operation: Succeeded`` 가 떠 있어도
    왜 게이트가 안 풀리는지 알기 어려워서 추가한 헬퍼. 반환 dict:

      - ``kind``: ``synced`` / ``rollout`` / ``syncing`` / ``outofsync_pending``
        / ``failed`` / ``pending``
      - ``level``: 배지 색 — ``pass`` / ``running`` / ``fail`` (CSS ``status-*`` 와 매칭)
      - ``label``: 화면에 그대로 노출할 한글 문자열

    분기 우선순위:
      1. ``health`` ∈ Degraded/Missing → 영구 실패(빨강).
      2. ``operation_phase`` ∈ Failed/Error → "Sync 실패"(빨강). _check_argocd_sync 가
         이걸 보면 곧바로 (PROD_)DEPLOY_FAILED 로 전환하므로, 사용자가 새 매니페스트를
         못 푸시한 경우 15분 타임아웃 없이 즉시 실패로 끊긴다.
      3. ``operation_phase`` ∈ Running/Terminating → "동기화중"(노랑).
      4. Synced + Healthy → "Synced"(초록) — 게이트 통과.
      5. Synced + Progressing(또는 그 외 health) → "롤아웃중"(노랑).
      6. OutOfSync → "OutOfSync — 재동기화 대기"(노랑) — 직전 sync 작업이 끝났는데도
         매니페스트와 클러스터가 다르면, Argo CD 의 다음 자동 sync 또는 사용자의 수동
         Retry 를 기다리는 상태.
      7. 그 외(상태 미수신 등) → "Syncing..."(노랑).
    """
    op = (op_phase or "").strip()
    s = (sync or "").strip()
    h = (health or "").strip()

    if h in ("Degraded", "Missing"):
        return {"kind": "failed", "level": "fail", "label": h}
    if op in ("Failed", "Error"):
        return {"kind": "failed", "level": "fail", "label": "Sync 실패"}
    if op in ("Running", "Terminating"):
        return {"kind": "syncing", "level": "running", "label": "동기화중"}
    if s == "Synced" and h == "Healthy":
        return {"kind": "synced", "level": "pass", "label": "Synced"}
    if s == "Synced":
        # health 가 Progressing 이거나 정보가 비어 있으면 롤아웃 진행 중으로 본다.
        return {"kind": "rollout", "level": "running", "label": "롤아웃중"}
    if s and s != "Synced":
        return {
            "kind": "outofsync_pending",
            "level": "running",
            "label": "OutOfSync — 재동기화 대기",
        }
    return {"kind": "pending", "level": "running", "label": "Syncing..."}
