"""API v2 스코프 정의.

PRD §4.3 — 단순 문자열 리스트. 와일드카드 `*` 는 admin 전용.

`export:read` (서버 이관용 DB 스냅샷 내보내기), `owners:manage` (담당자 관리) 는 **명시 부여 전용** 스코프다 —
와일드카드 `*` 가 포함하지 않는다. 또한 키 소유자가 admin 일 때만 동작한다
(`permissions.ExportPermission` / `OwnersManagePermission`).
"""

META_READ = "meta:read"
RUNS_READ = "runs:read"
RUNS_TRIGGER = "runs:trigger"
RUNS_CONTROL = "runs:control"
EXPORT_READ = "export:read"
OWNERS_MANAGE = "owners:manage"
WILDCARD = "*"

ALL_SCOPES = (META_READ, RUNS_READ, RUNS_TRIGGER, RUNS_CONTROL)

# 와일드카드 `*` 로 암묵 부여되지 않고 키에 명시적으로 들어 있어야 하는 스코프.
# (ALL_SCOPES 에는 넣지 않는다 — /me/ available_scopes 로 일반 키 사용자에게 노출하지 않음.)
EXPLICIT_ONLY_SCOPES = (EXPORT_READ, OWNERS_MANAGE)

SCOPE_LABELS = {
    META_READ: "메타 조회 (folder/subfolder/gate/suite/job 목록, 파라미터 스키마)",
    RUNS_READ: "Run 조회 (목록/단건/콘솔)",
    RUNS_TRIGGER: "Run 트리거 (게이트 / Suite 신규 Run 생성)",
    RUNS_CONTROL: "Run 제어 (취소 / 재실행 / Argo CD 재시도)",
    EXPORT_READ: "데이터 내보내기 (DB 스냅샷 생성/다운로드 — admin 전용, 서버 이관용. config/.env/secrets 제외)",
    OWNERS_MANAGE: "담당자 관리 (API 엔드포인트 담당자 조회/등록/수정/삭제 — admin 전용, 명시 부여 필요)",
    WILDCARD: "전체 (admin 전용, 향후 추가 스코프 포함 — export:read·owners:manage 는 제외, 명시 부여 필요)",
}


def has_scope(key_scopes, required: str) -> bool:
    """`required` 를 `key_scopes` 가 커버하는지 검사. `*` 는 admin 키가 보유 (EXPLICIT_ONLY_SCOPES 제외)."""
    if not key_scopes:
        return False
    if required in EXPLICIT_ONLY_SCOPES:
        # `*` 가 있어도 통과시키지 않는다 — 반드시 키에 명시돼 있어야 함.
        return required in key_scopes
    if WILDCARD in key_scopes:
        return True
    return required in key_scopes
