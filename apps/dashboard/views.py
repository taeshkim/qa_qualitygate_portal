"""대시보드 뷰 — Suite 중심 메트릭 카드 + 일별/Suite별 차트 + 최근 실행 내역.

집계 기간은 **최근 14일(`WINDOW_DAYS`)로 일원화**한다:
- 메트릭 카드 : 최근 14일 (started_at 기준, 완료 run)
- 일별 추이 (daily) : 최근 14일 (오늘 포함, 완료 run 의 pass/fail)
- Suite별 통계 (suite_stats) : 최근 14일 (완료 run, 상위 10개)
- 결과 분포 (dist_quality/dist_skip) : 최근 14일 Job 단위 (Pass/Failure, Pass/Skip)
- 미 장애 판정 대기 (untriaged_count) : 컷오프 이후 미분류 실패 Job 수 (기간 무관)

날짜 귀속은 timezone.localtime 기준 (Asia/Seoul).
"""

from collections import defaultdict
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from apps.suites.models import TestSuiteRun

# 대시보드 전 지표 공통 집계 기간(일). 카드·차트·분포 모두 이 값으로 통일.
WINDOW_DAYS = 14


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _date_label(d: date) -> str:
    return f"{d.month}/{d.day}"


# ---------------------------------------------------------------------------
# 집계 함수들
# ---------------------------------------------------------------------------

def _compute_metrics(now) -> dict:
    """최근 14일(WINDOW_DAYS) 완료 Run 기반 메트릭 카드."""
    window_ago = now - timedelta(days=WINDOW_DAYS)
    today_local = timezone.localtime(now).date()

    completed_qs = TestSuiteRun.annotate_triage(TestSuiteRun.objects.filter(
        started_at__gte=window_ago,
        status=TestSuiteRun.Status.COMPLETED,
    ))
    total_runs = completed_qs.count()
    # verdict=skip 로 분류된 실패는 실패로 세지 않는다 (real_failed = Skip 제외 실패 Job 수).
    failed_count = completed_qs.filter(real_failed__gt=0).count()
    passed_count = total_runs - failed_count  # 순수 PASS + '전부 Skip'/'전부 중단(ABORTED)' 실패
    pass_rate = round(passed_count / total_runs * 100, 1) if total_runs > 0 else 0.0

    running_count = TestSuiteRun.objects.filter(
        status=TestSuiteRun.Status.RUNNING,
    ).count()

    # 오늘 시작 run (로컬 날짜 기준)
    today_count = TestSuiteRun.objects.filter(
        started_at__date=today_local,
    ).count()

    return {
        "total_runs": total_runs,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "pass_rate": pass_rate,
        "running_count": running_count,
        "today_count": today_count,
    }


def _compute_daily(now) -> list:
    """최근 14일 날짜별 pass/fail 집계 (완료 run 만).

    Returns list of dicts: [{label, pass, fail}, ...] 오래된 날짜 → 최근 날짜 순.
    """
    fourteen_days_ago = now - timedelta(days=13)  # 오늘 포함 14일
    qs = TestSuiteRun.annotate_triage(TestSuiteRun.objects.filter(
        started_at__gte=fourteen_days_ago,
        status=TestSuiteRun.Status.COMPLETED,
    )).values("started_at", "real_failed")

    # 날짜별 집계 (real_failed=0 → 실장애 없음 = pass 쪽; Skip 은 실패로 안 셈)
    by_date: dict = defaultdict(lambda: {"pass": 0, "fail": 0})
    for row in qs:
        local_dt = timezone.localtime(row["started_at"])
        d = local_dt.date()
        if row["real_failed"] == 0:
            by_date[d]["pass"] += 1
        else:
            by_date[d]["fail"] += 1

    today_local = timezone.localtime(now).date()
    result = []
    for i in range(13, -1, -1):
        d = today_local - timedelta(days=i)
        rec = by_date.get(d, {"pass": 0, "fail": 0})
        # date(ISO) 는 대시보드 막대 클릭 → Run 목록 날짜 필터 드릴다운용.
        result.append({"label": _date_label(d), "date": d.isoformat(),
                       "pass": rec["pass"], "fail": rec["fail"]})
    return result


def _compute_suite_stats(now) -> list:
    """최근 14일(WINDOW_DAYS) 완료 run 을 suite 별로 집계, run 수 내림차순 상위 10개.

    avg_duration_sec: completed_at - started_at 평균 (둘 다 있는 row 만).
    """
    window_ago = now - timedelta(days=WINDOW_DAYS)
    qs = TestSuiteRun.annotate_triage(TestSuiteRun.objects.filter(
        started_at__gte=window_ago,
        status=TestSuiteRun.Status.COMPLETED,
    )).values(
        "suite_id", "suite__name", "real_failed",
        "started_at", "completed_at",
    )

    by_suite: dict = defaultdict(lambda: {
        "suite_id": "", "name": "", "total": 0, "pass": 0, "fail": 0, "duration_samples": [],
    })
    for row in qs:
        key = row["suite_id"]
        rec = by_suite[key]
        rec["suite_id"] = str(key)  # 막대 클릭 → Run 목록 suite 필터 드릴다운용
        rec["name"] = rec["name"] or row["suite__name"]
        rec["total"] += 1
        if row["real_failed"] == 0:  # Skip 은 실패로 안 셈
            rec["pass"] += 1
        else:
            rec["fail"] += 1
        if row["started_at"] and row["completed_at"]:
            diff = (row["completed_at"] - row["started_at"]).total_seconds()
            if diff > 0:
                rec["duration_samples"].append(diff)

    result = []
    for rec in by_suite.values():
        samples = rec["duration_samples"]
        avg_dur = round(sum(samples) / len(samples), 1) if samples else None
        pass_rate = round(rec["pass"] / rec["total"] * 100, 1) if rec["total"] else 0.0
        result.append({
            "suite_id": rec["suite_id"],
            "name": rec["name"],
            "total": rec["total"],
            "pass": rec["pass"],
            "fail": rec["fail"],
            "pass_rate": pass_rate,
            "avg_duration_sec": avg_dur,
        })

    result.sort(key=lambda x: x["total"], reverse=True)
    return result[:10]


def _compute_result_dist(now) -> tuple:
    """최근 14일(WINDOW_DAYS) 완료 Suite Run 의 **Job(worker) 단위** 결과 분포 2종.

    도넛을 둘로 나눈다 — 공통 기준(패스)을 두고 각각 head-to-head:
    - dist_quality (서비스 품질 지표) : 패스 vs 장애(실장애 = 실패 중 Skip 아닌 것,
      미분류 포함). Skip 은 서비스 장애가 아니므로 이 도넛에선 제외.
    - dist_skip                        : 패스 vs Skip(자동화 판정 오류 의심).
      실장애는 이 도넛에서 제외. 두 도넛의 '패스' 는 동일(성공 Job 수).

    미분류 ABORTED(중단) Job 은 장애가 아니므로 **어느 도넛에도 세지 않는다**
    — 스케줄 정지·수동 취소가 품질 지표를 왜곡하지 않게. 단 사람이 verdict 를
    명시 확정한 ABORTED 는 그 판정을 따른다(failure→장애 도넛, skip→Skip 도넛).
    """
    from django.db.models import Count, Q
    from apps.suites.models import TestSuiteJobRun

    window_ago = now - timedelta(days=WINDOW_DAYS)
    agg = TestSuiteJobRun.objects.filter(
        suite_run__status=TestSuiteRun.Status.COMPLETED,
        suite_run__started_at__gte=window_ago,
    ).aggregate(
        pass_jobs=Count("pk", filter=Q(status=TestSuiteJobRun.Status.SUCCESS)),
        fail_jobs=Count("pk", filter=(
            Q(status=TestSuiteJobRun.Status.FAILURE) & ~Q(verdict="skip")
        ) | Q(status=TestSuiteJobRun.Status.ABORTED, verdict="failure")),
        skip_jobs=Count("pk", filter=Q(
            status__in=[TestSuiteJobRun.Status.FAILURE, TestSuiteJobRun.Status.ABORTED],
            verdict="skip",
        )),
    )
    p = agg["pass_jobs"] or 0
    dist_quality = {"pass": p, "fail": agg["fail_jobs"] or 0}
    dist_skip = {"pass": p, "skip": agg["skip_jobs"] or 0}
    return dist_quality, dist_skip


# ---------------------------------------------------------------------------
# 뷰
# ---------------------------------------------------------------------------

@login_required
def index(request):
    now = timezone.now()

    metrics = _compute_metrics(now)
    daily = _compute_daily(now)
    suite_stats = _compute_suite_stats(now)
    dist_quality, dist_skip = _compute_result_dist(now)

    # 미 장애 판정 대기 큐 (컷오프 이후 미분류 실패 Job 수) — 카드 + 판정 화면 링크.
    from apps.suites.models import TestSuiteJobRun
    untriaged_count = TestSuiteJobRun.untriaged_pending().count()

    # 정렬 = 최근 활동(완료 시각, 없으면 시작 시각). long-running 실패가 뒤늦게 완료돼도
    # 최근 항목으로 노출된다 — started_at 정렬이면 시작 시각 자리에 묻혀(그 사이 다른 run
    # 이 많이 시작되면 최근 15 에서 밀려남) 방금 실패한 run 이 대시보드에 안 보이던 문제.
    from django.db.models.functions import Coalesce
    recent_suite_runs = (
        TestSuiteRun.annotate_triage(
            TestSuiteRun.objects.select_related("suite", "triggered_by"))
        .annotate(_last_activity=Coalesce("completed_at", "started_at"))
        .order_by("-_last_activity")[:15]
    )

    # 차트 데이터는 템플릿에서 |json_script 로 직접 직렬화한다 (XSS 안전).
    context = {
        "metrics": metrics,
        "daily": daily,
        "suite_stats": suite_stats,
        "dist_quality": dist_quality,
        "dist_skip": dist_skip,
        "untriaged_count": untriaged_count,
        "recent_suite_runs": recent_suite_runs,
    }
    return render(request, "dashboard/index.html", context)
