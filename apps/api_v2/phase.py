"""세분 status → 외부 친화적 5단 phase 매핑.

DB 의 14개 (Quality Gate Run) / 3개 (Test Suite Run) 세분 상태를 외부 호출자가
쉽게 분기할 수 있도록 단순화한 enum. 원본 `status` 는 그대로 유지하고 `phase`
를 함께 노출.

Phase:
- `queued`         — 시작 직후 대기 (pending)
- `running`        — 능동 진행 중 (building/deploying/testing/prod_deploying)
- `awaiting_input` — 사용자 입력 대기 (waiting_deploy, qg_passed)
- `succeeded`      — terminal, 성공 (completed)
- `failed`         — terminal, 실패 (image/deploy/prod_failed, failed)
- `cancelled`      — terminal, 사용자 취소
"""

from __future__ import annotations

QUEUED = "queued"
RUNNING = "running"
AWAITING_INPUT = "awaiting_input"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"


# QualityGateRun.Status (14개) → phase
_QG_RUN_PHASE = {
    "pending": QUEUED,
    "building_image": RUNNING,
    "deploying": RUNNING,
    "waiting_test": RUNNING,
    "testing": RUNNING,
    "prod_deploying": RUNNING,
    "waiting_deploy": AWAITING_INPUT,
    "qg_passed": AWAITING_INPUT,
    "completed": SUCCEEDED,
    "failed": FAILED,
    "image_build_failed": FAILED,
    "deploy_failed": FAILED,
    "prod_deploy_failed": FAILED,
    "cancelled": CANCELLED,
}

# TestSuiteRun.Status (3개) — passed_jobs/failed_jobs 로 추가 분기 필요
_SUITE_RUN_PHASE_BASE = {
    "pending": QUEUED,
    "running": RUNNING,
    "completed": None,  # passed_jobs/failed_jobs 보고 결정
}


def phase_for_run(status: str) -> str:
    """QualityGateRun 의 status → phase. 매핑 없는 값은 그대로 반환."""
    return _QG_RUN_PHASE.get(status, status or QUEUED)


def phase_for_suite_run(status: str, *, passed_jobs: int = 0, failed_jobs: int = 0) -> str:
    """TestSuiteRun 의 status → phase. completed 면 failed_jobs 로 분기."""
    base = _SUITE_RUN_PHASE_BASE.get(status)
    if base is not None:
        return base
    # completed → 결과로 분기
    if failed_jobs > 0:
        return FAILED
    if passed_jobs > 0:
        return SUCCEEDED
    # 둘 다 0 — 비정상이지만 보수적으로 succeeded
    return SUCCEEDED
