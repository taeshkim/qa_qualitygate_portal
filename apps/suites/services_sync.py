"""API Test Automation 탭 — 내부 Jenkins 'api' 폴더/잡 자동 싱크.

PRD: `docs/prd_ui_test_automation.md` §3.4.

``sync_api_suites()`` 가 하는 일:

1. 탐색 루트를 정한다 — 이미 등록된 **내부**(jenkins_base_url="") ``TestSuite`` 들의
   ``jenkins_folder_path`` 부모 폴더 전부 + env ``JENKINS_API_SYNC_ROOTS``(콤마 구분,
   선택) 를 합쳐 중복 제거. 실측 컨벤션상 대부분의 Suite 폴더가 Jenkins 루트 바로
   아래에 있어 부모 = 루트("") 인 경우가 흔하다.
2. 각 루트에서 깊이 제한(``_MAX_DEPTH``) 안으로 폴더 트리를 내려가며, 이름에
   ``api`` (대소문자 무관) 가 포함된 폴더/잡을 찾는다.
   - 후보를 만나면 ``JenkinsClient.list_folder_jobs`` 로 그 경로의 실제 워커 Job
     목록을 가져와 본다. **비어있지 않으면** 그 경로를 Suite 로 확정한다 — 이미
     등록(같은 base_url+folder_path) 이면 워커만 재동기화, 아니면 ``TestSuite`` 를
     새로 만들고 워커를 등록한다(기존 `apps.suites.views.suite_sync` 의 upsert 로직을
     `sync_suite_jobs` 로 추출해 재사용). 워커 폴더 확정 후에는 그 아래로 더
     내려가지 않는다(자식은 폴더가 아니라 워커 Job 이라는 전제).
   - 비어있으면(폴더인데 자식이 없거나 애초에 leaf job 이라 ``jobs`` 키가 없는 경우)
     그 아이템이 폴더면 더 깊이 탐색을 계속한다(예: "api" 가 포함된 상위 그룹 폴더
     아래 실제 Suite 폴더가 한 단계 더 있는 경우 대비).
   - ``api`` 가 없는 폴더도 계속 하위로 내려가며 탐색한다(깊이 제한까지).
3. 모든 Jenkins 호출은 익명 read 전용(``JenkinsClient()`` 기본 = 내부 Jenkins) —
   트리거/쓰기 없음. 개별 폴더 조회 실패는 ``JenkinsError`` 로 격리해 그 가지만
   건너뛰고 나머지 탐색을 막지 않는다.
4. 폭주 방지 — 전체 탐색에 폴더/잡 조회 호출 예산(``_MAX_NODES``)을 두어, 아무리
   깊거나 넓은 트리라도 한도 안에서만 스캔한다.

반환: ``{"created": int, "synced": int, "candidates": [folder_path, ...]}``.
``dry_run=True`` 면 실제 생성/등록 없이 ``candidates`` 만 채워 반환한다(사전 점검용).
"""

from __future__ import annotations

import logging
import os

from apps.integrations.jenkins import JenkinsClient, JenkinsError

from .models import TestSuite, TestSuiteJob

logger = logging.getLogger(__name__)

_MATCH_KEYWORD = "api"
_MAX_DEPTH = 4       # 루트로부터 최대 폴더 깊이(실측 컨벤션은 깊이 0~1 이면 충분 — 여유만 둠)
_MAX_NODES = 300     # 전체 스캔에서 쓸 수 있는 Jenkins 폴더/잡 목록 조회 호출 예산(폭주 방지)


def sync_suite_jobs(suite: TestSuite, jobs: list) -> int:
    """워커 Job 목록으로 ``TestSuiteJob`` 을 upsert. (기존 `views.suite_sync` 본문을 추출)

    수동 재싱크(`apps.suites.views.suite_sync`)와 자동 싱크(`sync_api_suites`) 양쪽이
    공유하는 단일 로직 — 여기 한 곳만 고치면 두 경로 모두 반영된다.
    """
    suite.jobs.all().update(is_active=False)
    for i, job_data in enumerate(jobs):
        TestSuiteJob.objects.update_or_create(
            suite=suite,
            job_full_path=job_data["full_path"],
            defaults={
                "job_name": job_data["name"],
                "total_stages": job_data.get("total_stages", 0),
                "is_active": True,
                "order": i,
            },
        )
    return len(jobs)


def _parent_folder(path: str) -> str:
    path = (path or "").strip("/")
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def _scan_roots() -> list:
    roots = {""}
    for folder_path in TestSuite.objects.filter(jenkins_base_url="").values_list(
        "jenkins_folder_path", flat=True,
    ):
        roots.add(_parent_folder(folder_path))
    for token in os.getenv("JENKINS_API_SYNC_ROOTS", "").split(","):
        token = token.strip().strip("/")
        if token:
            roots.add(token)
    return sorted(roots)


class _Budget:
    """폴더/잡 목록 조회 호출 예산 — 폭주 방지용 단순 카운터."""

    def __init__(self, total: int):
        self.left = total

    def spend(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def _walk(client, folder_path, depth, budget, registered, candidates, dry_run, counters):
    if depth > _MAX_DEPTH or not budget.spend():
        return
    try:
        children = client.list_children(folder_path)
    except JenkinsError as e:
        logger.warning(
            "sync_api_suites: 폴더 조회 실패 folder=%s err=%s", folder_path or "(root)", e,
        )
        return
    except Exception:
        logger.exception(
            "sync_api_suites: 폴더 조회 중 예상치 못한 오류 folder=%s", folder_path or "(root)",
        )
        return

    for child in children:
        if budget.left <= 0:
            return
        name = child["name"]
        full_path = child["full_path"]

        if ("", full_path) in registered:
            # 이미 등록된 Suite — 그 아래는 워커 Job 뿐이므로 더 볼 필요 없음.
            continue

        if _MATCH_KEYWORD in name.lower():
            if not budget.spend():
                return
            try:
                jobs = client.list_folder_jobs(full_path)
            except JenkinsError as e:
                logger.warning(
                    "sync_api_suites: job 목록 조회 실패 path=%s err=%s", full_path, e,
                )
                jobs = []
            except Exception:
                logger.exception(
                    "sync_api_suites: job 목록 조회 중 예상치 못한 오류 path=%s", full_path,
                )
                jobs = []

            if jobs:
                candidates.append(full_path)
                if not dry_run:
                    try:
                        suite, created = TestSuite.objects.get_or_create(
                            jenkins_base_url="",
                            jenkins_folder_path=full_path,
                            defaults={
                                "name": name,
                                "jenkins_folder_url": child.get("url", ""),
                            },
                        )
                        sync_suite_jobs(suite, jobs)
                        if created:
                            counters["created"] += 1
                        counters["synced"] += 1
                    except Exception:
                        logger.exception(
                            "sync_api_suites: Suite 생성/동기화 실패 path=%s", full_path,
                        )
                # 워커 폴더로 확정 — 자식은 워커 Job 이지 폴더가 아니므로 더 내려가지 않는다.
                continue

        if child.get("is_folder") and depth < _MAX_DEPTH:
            _walk(client, full_path, depth + 1, budget, registered, candidates, dry_run, counters)


def sync_api_suites(client: JenkinsClient | None = None, *, dry_run: bool = False) -> dict:
    """내부 Jenkins 를 스캔해 이름에 'api' 가 포함된 미등록 폴더/잡을 TestSuite 로 등록.

    ``dry_run=True`` 면 아무것도 쓰지 않고 후보 목록만 반환(사전 점검용).
    반환: ``{"created": int, "synced": int, "candidates": [str, ...]}``.
    """
    client = client or JenkinsClient()
    registered = set(
        TestSuite.objects.filter(jenkins_base_url="").values_list(
            "jenkins_base_url", "jenkins_folder_path",
        )
    )
    candidates: list = []
    counters = {"created": 0, "synced": 0}
    budget = _Budget(_MAX_NODES)

    for root in _scan_roots():
        if budget.left <= 0:
            break
        _walk(client, root, 0, budget, registered, candidates, dry_run, counters)

    return {"created": counters["created"], "synced": counters["synced"], "candidates": candidates}
