"""대시보드 뷰 — 메트릭 카드 + DORA 4지표 + 최근 실행 + 게이트 현황.

DORA Metrics (DevOps Research and Assessment) — 표준 4지표:
- Deployment Frequency (DF)     : 단위 기간당 성공 배포 수
- Lead Time for Changes (LT)    : 변경 시작 → 배포 완료 까지 걸린 시간 (중앙값)
- Change Failure Rate (CFR)     : 배포 시도 중 실패 비율
- Mean Time to Recovery (MTTR)  : 실패 → 다음 성공 까지 걸린 시간 (중앙값)

산정 기간은 30일이 표준 (`Accelerate State of DevOps` 기준).
"""

import json
import statistics
from collections import defaultdict
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from apps.qualitygates.models import QualityGate, QualityGateRun


# Terminal 분류 ----------------------------------------------------------------
SUCCESS_STATUSES = (
    QualityGateRun.Status.COMPLETED,
    QualityGateRun.Status.QG_PASSED,
)
FAILURE_STATUSES = (
    QualityGateRun.Status.FAILED,
    QualityGateRun.Status.DEPLOY_FAILED,
    QualityGateRun.Status.PROD_DEPLOY_FAILED,
    QualityGateRun.Status.IMAGE_BUILD_FAILED,
)
TERMINAL_STATUSES = SUCCESS_STATUSES + FAILURE_STATUSES + (
    QualityGateRun.Status.CANCELLED,
)


# 사람이 읽기 좋은 짧은 duration ------------------------------------------------
def _fmt_duration_short(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return "N/A"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# DORA 등급 (Accelerate 2022/2023 기준) -----------------------------------------
def _dora_grade_df(per_day: float) -> tuple[str, str]:
    """Deployment Frequency 등급 + CSS level (green/blue/yellow/red)."""
    if per_day >= 1:
        return "Elite", "elite"
    if per_day >= 1 / 7:
        return "High", "high"
    if per_day >= 1 / 30:
        return "Medium", "medium"
    return "Low", "low"


def _dora_grade_lt(median_seconds: float | None) -> tuple[str, str]:
    if median_seconds is None:
        return "—", "na"
    day = 86400
    if median_seconds < day:
        return "Elite", "elite"
    if median_seconds < 7 * day:
        return "High", "high"
    if median_seconds < 30 * day:
        return "Medium", "medium"
    return "Low", "low"


def _dora_grade_cfr(pct: float) -> tuple[str, str]:
    if pct <= 15:
        return "Elite", "elite"
    if pct <= 30:
        return "High", "high"
    if pct <= 45:
        return "Medium", "medium"
    return "Low", "low"


def _dora_grade_mttr(median_seconds: float | None) -> tuple[str, str]:
    if median_seconds is None:
        return "—", "na"
    hour = 3600
    day = 86400
    if median_seconds < hour:
        return "Elite", "elite"
    if median_seconds < day:
        return "High", "high"
    if median_seconds < 7 * day:
        return "Medium", "medium"
    return "Low", "low"


def _compute_dora(now, runs_30d) -> dict:
    """30일 윈도우의 DORA 4지표 계산."""
    # 1) Deployment Frequency — 성공한 terminal Run 수 / 30일.
    successes = [r for r in runs_30d if r.status in SUCCESS_STATUSES]
    df_count = len(successes)
    df_per_day = df_count / 30.0

    if df_per_day >= 1:
        df_value = f"{df_per_day:.1f}/day"
    elif df_per_day > 0:
        df_value = f"{df_count}/30d"
    else:
        df_value = "0/30d"
    df_grade, df_level = _dora_grade_df(df_per_day)

    # 2) Lead Time — created_at → 마지막 단계 완료 시각 (prod 가 있으면 prod, 없으면 deploy)
    lt_samples: list[float] = []
    for r in successes:
        end = r.prod_completed_at or r.deploy_completed_at
        if end and r.created_at:
            diff = (end - r.created_at).total_seconds()
            if diff > 0:
                lt_samples.append(diff)
    lt_median = statistics.median(lt_samples) if lt_samples else None
    lt_value = _fmt_duration_short(lt_median) if lt_median else "—"
    lt_grade, lt_level = _dora_grade_lt(lt_median)

    # 3) Change Failure Rate — failures / (failures + successes). cancelled 는 제외.
    failures = [r for r in runs_30d if r.status in FAILURE_STATUSES]
    denom = len(successes) + len(failures)
    cfr_pct = (len(failures) / denom * 100) if denom else 0.0
    cfr_value = f"{cfr_pct:.1f}%"
    cfr_grade, cfr_level = _dora_grade_cfr(cfr_pct)

    # 4) MTTR — 같은 게이트의 FAIL → 다음 성공까지의 시간 중앙값.
    by_gate: dict = defaultdict(list)
    for r in runs_30d:
        if r.status in TERMINAL_STATUSES and r.status != QualityGateRun.Status.CANCELLED:
            by_gate[r.gate_id].append(r)

    mttr_samples: list[float] = []
    for gate_runs in by_gate.values():
        # 시간 오름차순. FAIL 만나면 fail_at 마킹, 다음 PASS 만나면 diff.
        gate_runs.sort(key=lambda r: r.created_at)
        fail_at = None
        for r in gate_runs:
            if r.status in FAILURE_STATUSES and fail_at is None:
                fail_at = r.created_at
            elif r.status in SUCCESS_STATUSES and fail_at is not None:
                diff = (r.created_at - fail_at).total_seconds()
                if diff > 0:
                    mttr_samples.append(diff)
                fail_at = None
    mttr_median = statistics.median(mttr_samples) if mttr_samples else None
    mttr_value = _fmt_duration_short(mttr_median) if mttr_median else "—"
    mttr_grade, mttr_level = _dora_grade_mttr(mttr_median)

    return {
        "df": {
            "value": df_value, "grade": df_grade, "level": df_level,
            "count": df_count, "per_day": df_per_day,
        },
        "lt": {
            "value": lt_value, "grade": lt_grade, "level": lt_level,
            "samples": len(lt_samples),
        },
        "cfr": {
            "value": cfr_value, "grade": cfr_grade, "level": cfr_level,
            "failures": len(failures), "successes": len(successes),
        },
        "mttr": {
            "value": mttr_value, "grade": mttr_grade, "level": mttr_level,
            "samples": len(mttr_samples),
        },
    }


def _compute_gate_lifetime_stats() -> dict:
    """게이트별 누적 통계 (전 기간) — 최근 실행 표의 컬럼에 사용.

    Returns:
        {gate_pk_str: {"total": int, "success": int, "rate": int}}
    """
    stats: dict = {}
    qs = (
        QualityGateRun.objects
        .filter(status__in=TERMINAL_STATUSES)
        .values("gate_id", "status")
    )
    by_gate: dict = defaultdict(lambda: {"total": 0, "success": 0})
    for row in qs:
        rec = by_gate[row["gate_id"]]
        rec["total"] += 1
        if row["status"] in SUCCESS_STATUSES:
            rec["success"] += 1
    for gate_id, rec in by_gate.items():
        rate = round(rec["success"] / rec["total"] * 100) if rec["total"] else 0
        stats[str(gate_id)] = {
            "total": rec["total"],
            "success": rec["success"],
            "rate": rate,
        }
    return stats


@login_required
def index(request):
    now = timezone.now()
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    # ===== 메트릭 카드 (최근 7일) =====
    runs_7d = list(QualityGateRun.objects.filter(
        created_at__gte=seven_days_ago,
        status__in=TERMINAL_STATUSES,
    ))
    total_runs = len([r for r in runs_7d if r.status != QualityGateRun.Status.CANCELLED])
    passed_count = len([r for r in runs_7d if r.status in SUCCESS_STATUSES])
    failed_count = len([r for r in runs_7d if r.status in FAILURE_STATUSES])
    pass_rate = round(passed_count / total_runs * 100, 1) if total_runs > 0 else 0

    running_count = QualityGateRun.objects.filter(
        status__in=[
            QualityGateRun.Status.PENDING,
            QualityGateRun.Status.DEPLOYING,
            QualityGateRun.Status.WAITING_TEST,
            QualityGateRun.Status.TESTING,
            QualityGateRun.Status.PROD_DEPLOYING,
        ]
    ).count()

    # ===== DORA Metrics (최근 30일) =====
    runs_30d = list(
        QualityGateRun.objects
        .filter(created_at__gte=thirty_days_ago, status__in=TERMINAL_STATUSES)
        .select_related("gate")
    )
    dora = _compute_dora(now, runs_30d)

    # ===== 최근 Quality Gate 실행 내역 + 게이트 누적 통계 부착 =====
    recent_runs = list(
        QualityGateRun.objects
        .select_related("gate", "triggered_by")
        .prefetch_related("test_runs__gate_suite__test_suite")
        .order_by("-created_at")[:15]
    )
    gate_stats = _compute_gate_lifetime_stats()
    for r in recent_runs:
        r.gate_lifetime_stats = gate_stats.get(str(r.gate_id))

    # ===== Quality Gate별 현황 (사이드 패널) =====
    gates = QualityGate.objects.filter(is_active=True).prefetch_related("gate_suites__test_suite")

    context = {
        "metrics": {
            "pass_rate": pass_rate,
            "total_runs": total_runs,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "running_count": running_count,
        },
        "dora": dora,
        "recent_runs": recent_runs,
        "gate_stats": gate_stats,
        "gate_stats_json": json.dumps(gate_stats),  # 디버그용
        "gates": gates,
    }
    return render(request, "dashboard/index.html", context)
