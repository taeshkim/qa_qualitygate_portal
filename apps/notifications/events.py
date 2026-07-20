"""알림 이벤트 타입 상수.

각 상수는 SlackNotificationSettings 의 토글 필드명 (`notify_*`) 과 1:1 매핑된다.
"""

# Run 단위
RUN_STARTED = "run_started"
RUN_COMPLETED = "run_completed"
RUN_FAILED = "run_failed"
RUN_CANCELLED = "run_cancelled"

# Image
IMAGE_STARTED = "image_started"
IMAGE_COMPLETED = "image_completed"
IMAGE_FAILED = "image_failed"

# Deploy (검수대상)
DEPLOY_STARTED = "deploy_started"
DEPLOY_COMPLETED = "deploy_completed"
DEPLOY_FAILED = "deploy_failed"

# Prod (검수통과)
PROD_STARTED = "prod_started"
PROD_COMPLETED = "prod_completed"
PROD_FAILED = "prod_failed"

# Test Suite — Quality Gate 파이프라인 내부에서 실행되는 Suite
TEST_SUITE_STARTED = "test_suite_started"
TEST_SUITE_PASSED = "test_suite_passed"
TEST_SUITE_FAILED = "test_suite_failed"

# Test Suite — 단독 실행 (apps/suites 의 Suite Run)
SUITE_RUN_STARTED = "suite_run_started"
SUITE_RUN_PASSED = "suite_run_passed"
SUITE_RUN_FAILED = "suite_run_failed"

# 공통
ARGOCD_FAILED = "argocd_failed"


# UI 표시 — 탭 → 그룹 → 이벤트 2단 구조.
# 어드민 화면에서 'Quality Gate' 와 'Test Suite' 영역을 시각적으로 분리하기 위함.
EVENT_TABS = [
    ("Quality Gate", [
        ("Run 단위", [
            (RUN_STARTED, "Run 시작"),
            (RUN_COMPLETED, "Run 성공 종료"),
            (RUN_FAILED, "Run 실패"),
            (RUN_CANCELLED, "Run 취소"),
        ]),
        ("이미지 빌드", [
            (IMAGE_STARTED, "이미지 빌드 시작"),
            (IMAGE_COMPLETED, "이미지 빌드 완료"),
            (IMAGE_FAILED, "이미지 빌드 실패"),
        ]),
        ("검수대상 배포", [
            (DEPLOY_STARTED, "검수대상 배포 시작"),
            (DEPLOY_COMPLETED, "검수대상 배포 완료"),
            (DEPLOY_FAILED, "검수대상 배포 실패"),
        ]),
        ("검수통과 배포", [
            (PROD_STARTED, "검수통과 배포 시작"),
            (PROD_COMPLETED, "검수통과 배포 완료"),
            (PROD_FAILED, "검수통과 배포 실패"),
        ]),
        ("파이프라인 내부 Test Suite", [
            (TEST_SUITE_STARTED, "Test Suite 시작"),
            (TEST_SUITE_PASSED, "Test Suite PASS"),
            (TEST_SUITE_FAILED, "Test Suite FAIL"),
        ]),
        ("Argo CD", [
            (ARGOCD_FAILED, "Argo CD Sync 실패"),
        ]),
    ]),
    ("Test Suite", [
        ("단독 Suite Run", [
            (SUITE_RUN_STARTED, "Suite Run 시작"),
            (SUITE_RUN_PASSED, "Suite Run PASS"),
            (SUITE_RUN_FAILED, "Suite Run FAIL"),
        ]),
    ]),
]

# 기존 호환 — flat 그룹 리스트.
EVENT_GROUPS = [g for _, groups in EVENT_TABS for g in groups]

ALL_EVENTS = [code for _, items in EVENT_GROUPS for code, _ in items]
