"""API v2 스코프 정의.

PRD §4.3 — 단순 문자열 리스트. 와일드카드 `*` 는 admin 전용.
"""

META_READ = "meta:read"
RUNS_READ = "runs:read"
RUNS_TRIGGER = "runs:trigger"
RUNS_CONTROL = "runs:control"
WILDCARD = "*"

ALL_SCOPES = (META_READ, RUNS_READ, RUNS_TRIGGER, RUNS_CONTROL)

SCOPE_LABELS = {
    META_READ: "메타 조회 (folder/subfolder/gate/suite/job 목록, 파라미터 스키마)",
    RUNS_READ: "Run 조회 (목록/단건/콘솔)",
    RUNS_TRIGGER: "Run 트리거 (게이트 / Suite 신규 Run 생성)",
    RUNS_CONTROL: "Run 제어 (취소 / 재실행 / Argo CD 재시도)",
    WILDCARD: "전체 (admin 전용, 향후 추가 스코프 포함)",
}


def has_scope(key_scopes, required: str) -> bool:
    """`required` 를 `key_scopes` 가 커버하는지 검사. `*` 는 admin 키가 보유."""
    if not key_scopes:
        return False
    if WILDCARD in key_scopes:
        return True
    return required in key_scopes
