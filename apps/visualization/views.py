"""Quality Highway 시각화 페이지 — index 렌더 + JSON 데이터 피드.

data 엔드포인트는 최근 TestSuiteRun 을 차(car) 한 대씩의 JSON 으로 반환한다.
(Quality Gate Run 은 시각화에서 제외 — Test Suite 실행만 표시.)

상태 매핑:
  TestSuiteRun  running  → "running"
                completed & real_failed > 0 → "fail"
                completed & failed_jobs > 0 & real_failed == 0 → "skip"
                  (실패 Job 이 전부 Skip 분류 또는 전부 ABORTED(중단) — 둘 다
                   장애가 아니므로 JS 가 아는 "skip"(피트 진입) 으로 표현)
                completed & failed_jobs == 0 → "pass"
                pending → 제외
"""

import datetime
import time
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Subquery
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.timezone import utc

from apps.suites.models import TestSuiteJobRun, TestSuiteRun


_SEED_LIMIT = 40
_STALE_CURSOR_SECONDS = 15 * 60


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _ts(run, *fields):
    """run 에서 순서대로 datetime 필드를 확인해 첫 번째 non-None 의 epoch int 를 반환.
    모두 None 이면 None 반환.
    """
    for f in fields:
        dt = getattr(run, f, None)
        if dt is not None:
            return int(dt.timestamp())
    return None


def _suite_run_to_dict(run):
    """TestSuiteRun → dict. pending 이면 None 반환(제외 신호)."""
    if run.status == TestSuiteRun.Status.PENDING:
        return None

    if run.status == TestSuiteRun.Status.RUNNING:
        status = "running"
    elif run.status == TestSuiteRun.Status.COMPLETED:
        # annotate_triage 로 계산된 real_failed 를 재사용한다. 실패 Job이 모두
        # verdict=skip 이면 갓길 사고가 아닌 별도 skip 상태로 표현한다.
        status = run.triage_result
        # 전부-중단(aborted) run 은 장애가 아니다 — JS 는 pass/fail/skip/running 만
        # 인지하므로 skip(피트 진입)으로 표현한다.
        if status == "aborted":
            status = "skip"
    else:
        # 혹시 모를 미래 상태는 running 으로 fallback
        status = "running"

    ts = _ts(run, "completed_at", "started_at")
    if ts is None:
        return None

    suite = run.suite
    if run.seq_no:
        label = f"{suite.short_label} · #{run.seq_no}"
    else:
        label = suite.name

    detail = f"{run.passed_jobs}/{run.total_jobs} jobs"

    return {
        "id": str(run.id),
        "kind": "suite",
        "label": label,
        "status": status,
        "ts": ts,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# 뷰
# ---------------------------------------------------------------------------

def _admin_only(view):
    """QPE Admin(`is_admin` — qpe_admin/superuser) 전용. 그 외 역할은 페이지는 대시보드로, JSON 피드는 403."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_admin:
            if request.path_info.rstrip("/").endswith("/data"):
                return JsonResponse({"error": {"code": "role_denied", "message": "QPE Admin 전용입니다."}}, status=403)
            if request.headers.get("x-requested-with") != "XMLHttpRequest":
                messages.error(request, "Visualization 은 QPE Admin 만 볼 수 있습니다.")
            return redirect("dashboard:index")
        return view(request, *args, **kwargs)

    return wrapper


@login_required
@_admin_only
def index(request):
    return render(request, "visualization/index.html", {})


@login_required
@_admin_only
def data(request):
    """JSON 데이터 피드 (Test Suite Run 만).

    쿼리파라미터:
      after  int (epoch seconds, optional) — 증분 폴링용. 주어지면 ts > after 인 run,
             진행률 갱신이 필요한 현재 RUNNING run, 이후 triage된 run 을 반환.

    응답:
      {"runs": [...], "server_ts": <query-start epoch>, "reset": <bool>}
    """
    # 쿼리 완료 시각이 아니라 시작 시각을 다음 cursor로 보낸다. 쿼리 도중 commit된
    # 변경은 클라이언트의 짧은 replay 구간에서 다음 요청에 다시 포착된다.
    server_ts = int(time.time())
    after_raw = request.GET.get("after")
    after_dt = None
    reset = False
    if after_raw is not None:
        try:
            after_epoch = int(after_raw)
            if (
                after_epoch <= 0
                or after_epoch < server_ts - _STALE_CURSOR_SECONDS
            ):
                # 오랫동안 숨겨졌던 탭이 전체 이력을 한 번에 읽지 않도록 최신
                # seed로 재동기화한다. 클라이언트는 reset 신호로 기존 pool을 교체한다.
                reset = True
            else:
                after_dt = datetime.datetime.fromtimestamp(after_epoch, tz=utc)
        except (ValueError, TypeError, OverflowError, OSError):
            # 숫자로 보이더라도 datetime 이 표현할 수 없는 epoch 면 잘못된
            # 문자열과 동일하게 초기(seed) 피드로 폴백한다.
            reset = True

    candidate_qs = (
        TestSuiteRun.objects
        .exclude(status=TestSuiteRun.Status.PENDING)
        .annotate(_activity_at=Coalesce("completed_at", "started_at"))
        .filter(_activity_at__isnull=False)
    )

    if after_dt is None:
        # 시드 모드: 시작 시각이 오래됐어도 방금 완료된 run 을 놓치지 않도록
        # 실제 응답 ts 와 같은 completed_at → started_at 기준 후보를 먼저 제한한다.
        candidate_ids = (
            candidate_qs
            .order_by("-_activity_at", "-id")
            .values("pk")[:_SEED_LIMIT]
        )
    else:
        # 증분 모드: 새 활동뿐 아니라 현재 RUNNING과 after 이후 triage된 완료 run도
        # 재전송한다. triage 대상 id를 비상관 서브쿼리로 한 번만 스캔하므로
        # job_runs 추가 JOIN/결과 중복/Run별 상관 서브쿼리 비용이 없다.
        recently_triaged_ids = (
            TestSuiteJobRun.objects
            .filter(triaged_at__gt=after_dt)
            .order_by()  # TestSuiteJobRun 기본 job 정렬 및 불필요한 JOIN 제거
            .values("suite_run_id")
        )
        candidate_ids = (
            candidate_qs
            .filter(
                Q(_activity_at__gt=after_dt)
                | Q(status=TestSuiteRun.Status.RUNNING)
                | Q(pk__in=Subquery(recently_triaged_ids))
            )
            .order_by("-_activity_at", "-id")
            .values("pk")
        )

    # triage Count는 후보 id를 제한한 뒤에만 계산한다. 응답 한도와 무관하게 전체
    # 이력의 job_runs를 집계하던 비용을 피하면서도 단일 SQL/N+1 없음은 유지한다.
    suite_qs = (
        TestSuiteRun.annotate_triage(
            TestSuiteRun.objects
            .select_related("suite")
            .filter(pk__in=candidate_ids)
            .annotate(_activity_at=Coalesce("completed_at", "started_at"))
        )
        .order_by("-_activity_at", "-id")
    )
    items = [d for d in (_suite_run_to_dict(r) for r in suite_qs) if d is not None]

    if after_dt is not None:
        # 오래된 순(오름차순) — 차가 등장하는 시간 순서로
        items.sort(key=lambda x: (x["ts"], x["id"]))

    return JsonResponse({"runs": items, "server_ts": server_ts, "reset": reset})
