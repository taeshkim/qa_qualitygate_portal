import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render

from apps.activitylog.models import ActivityLog
from apps.suites.models import TestSuite

from .models import RobustnessRun, RobustnessSchedule, RobustnessTest
from .services import (
    _trigger_once,
    reset_schedule_stats,
    start_schedule,
    stop_schedule,
    sweep_active_schedules,
    sweep_schedule,
)

logger = logging.getLogger(__name__)


# =============================================
# Robustness Test CRUD
# =============================================

@login_required
def test_list(request):
    sweep_active_schedules()
    tests = RobustnessTest.objects.filter(is_active=True).prefetch_related(
        "schedules__test_suite",
    )
    return render(request, "robustness/test_list.html", {"tests": tests})


@login_required
def test_register(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        if not name:
            return render(request, "robustness/test_register.html", {
                "error": "Robustness Test 이름을 입력하세요.",
            })
        test = RobustnessTest.objects.create(
            name=name, description=description, created_by=request.user,
        )
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.CREATE,
            target_type=ActivityLog.TargetType.RELIABILITY_TEST, target=test,
        )
        return redirect("robustness:test_detail", pk=test.pk)
    return render(request, "robustness/test_register.html")


@login_required
def test_detail(request, pk):
    test = get_object_or_404(
        RobustnessTest.objects.prefetch_related("schedules__test_suite"), pk=pk,
    )
    sweep_active_schedules()
    test.refresh_from_db()

    schedules = list(
        test.schedules.select_related("test_suite").order_by("created_at")
    )
    available_suites = TestSuite.objects.filter(is_active=True).exclude(
        pk__in=[s.test_suite_id for s in schedules],
    )
    return render(request, "robustness/test_detail.html", {
        "test": test,
        "schedules": schedules,
        "available_suites": available_suites,
    })


@login_required
def test_edit(request, pk):
    test = get_object_or_404(RobustnessTest, pk=pk)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        if name:
            test.name = name
        test.description = description
        test.save()
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.RELIABILITY_TEST, target=test,
        )
        return redirect("robustness:test_detail", pk=test.pk)
    return render(request, "robustness/test_edit.html", {"test": test})


@login_required
def test_delete(request, pk):
    test = get_object_or_404(RobustnessTest, pk=pk)
    if request.method == "POST":
        name = test.name
        test_id = str(test.pk)
        test.delete()
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.DELETE,
            target_type=ActivityLog.TargetType.RELIABILITY_TEST,
            target_name=name, detail=f"id={test_id}",
        )
        return redirect("robustness:test_list")
    return render(request, "robustness/test_delete_confirm.html", {"test": test})


# =============================================
# Schedule CRUD / Control
# =============================================

@login_required
def schedule_add(request, pk):
    test = get_object_or_404(RobustnessTest, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=test.pk)

    suite_id = request.POST.get("test_suite_id")
    interval_raw = request.POST.get("interval_minutes", "60")

    if not suite_id:
        messages.error(request, "Test Suite 를 선택하세요.")
        return redirect("robustness:test_detail", pk=test.pk)

    try:
        interval = int(interval_raw)
    except ValueError:
        return HttpResponseBadRequest("interval_minutes must be an integer")

    if interval < 1:
        messages.error(request, "실행 간격은 1분 이상이어야 합니다.")
        return redirect("robustness:test_detail", pk=test.pk)

    suite = get_object_or_404(TestSuite, pk=suite_id)
    schedule, created = RobustnessSchedule.objects.get_or_create(
        robustness_test=test,
        test_suite=suite,
        defaults={"interval_minutes": interval},
    )
    if not created:
        schedule.interval_minutes = interval
        schedule.save(update_fields=["interval_minutes"])
        messages.info(request, f"'{suite.name}' 스케줄의 간격을 {interval}분으로 갱신했습니다.")
    else:
        messages.success(request, f"'{suite.name}' 를 {interval}분 간격으로 추가했습니다.")

    ActivityLog.log(
        user=request.user,
        action=ActivityLog.Action.CREATE if created else ActivityLog.Action.UPDATE,
        target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=schedule,
        target_name=f"{test.name} / {suite.name}",
        detail=f"interval={interval}분",
    )
    return redirect("robustness:test_detail", pk=test.pk)


@login_required
def schedule_add_all(request, pk):
    """미연동 Test Suite 를 일괄 추가한다."""
    test = get_object_or_404(RobustnessTest, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=test.pk)

    try:
        interval = int(request.POST.get("interval_minutes", "60"))
    except ValueError:
        interval = 60
    if interval < 1:
        interval = 60

    existing_ids = set(
        test.schedules.values_list("test_suite_id", flat=True)
    )
    available = TestSuite.objects.filter(is_active=True).exclude(pk__in=existing_ids)
    count = 0
    for suite in available:
        RobustnessSchedule.objects.create(
            robustness_test=test,
            test_suite=suite,
            interval_minutes=interval,
        )
        count += 1

    if count:
        messages.success(request, f"{count}개 Test Suite 를 {interval}분 간격으로 일괄 추가했습니다.")
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.CREATE,
            target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=test,
            target_name=test.name,
            detail=f"일괄 추가 {count}개 / interval={interval}분",
        )
    else:
        messages.info(request, "추가할 Test Suite 가 없습니다.")
    return redirect("robustness:test_detail", pk=test.pk)


@login_required
def schedule_start_all(request, pk):
    """해당 테스트의 모든 스케줄을 일괄 시작한다."""
    test = get_object_or_404(RobustnessTest, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=test.pk)

    schedules = test.schedules.filter(is_running=False)
    count = 0
    for schedule in schedules:
        start_schedule(schedule)
        sweep_schedule(schedule)
        count += 1

    if count:
        messages.success(request, f"{count}개 스케줄을 일괄 시작했습니다.")
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.RUN,
            target_type=ActivityLog.TargetType.RELIABILITY_TEST, target=test,
            detail=f"일괄 시작 {count}개",
        )
    else:
        messages.info(request, "시작할 스케줄이 없습니다.")
    return redirect("robustness:test_detail", pk=test.pk)


@login_required
def schedule_stop_all(request, pk):
    """해당 테스트의 모든 스케줄을 일괄 중지한다."""
    test = get_object_or_404(RobustnessTest, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=test.pk)

    schedules = test.schedules.filter(is_running=True)
    count = 0
    for schedule in schedules:
        stop_schedule(schedule)
        count += 1

    if count:
        messages.info(request, f"{count}개 스케줄을 일괄 중지했습니다.")
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.CANCEL,
            target_type=ActivityLog.TargetType.RELIABILITY_TEST, target=test,
            detail=f"일괄 중지 {count}개",
        )
    else:
        messages.info(request, "실행 중인 스케줄이 없습니다.")
    return redirect("robustness:test_detail", pk=test.pk)


@login_required
def schedule_update_interval(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=schedule.robustness_test_id)

    is_htmx = request.headers.get("HX-Request") == "true"

    try:
        interval = int(request.POST.get("interval_minutes", "0"))
    except ValueError:
        if is_htmx:
            return HttpResponseBadRequest("interval_minutes must be an integer")
        return HttpResponseBadRequest("interval_minutes must be an integer")

    if interval < 1:
        if is_htmx:
            return HttpResponseBadRequest("실행 간격은 1분 이상이어야 합니다.")
        messages.error(request, "실행 간격은 1분 이상이어야 합니다.")
    else:
        schedule.interval_minutes = interval
        schedule.save(update_fields=["interval_minutes"])
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=schedule,
            target_name=f"{schedule.robustness_test.name} / {schedule.test_suite.name}",
            detail=f"interval={interval}분",
        )
        if is_htmx:
            return HttpResponse(status=204)
        messages.success(request, f"실행 간격을 {interval}분으로 변경했습니다.")

    return redirect("robustness:test_detail", pk=schedule.robustness_test_id)


@login_required
def schedule_start(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=schedule.robustness_test_id)
    start_schedule(schedule)
    sweep_schedule(schedule)
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.RUN,
        target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=schedule,
        target_name=f"{schedule.robustness_test.name} / {schedule.test_suite.name}",
        detail=f"시작 / interval={schedule.interval_minutes}분",
    )
    messages.success(request, f"'{schedule.test_suite.name}' 스케줄을 시작했습니다.")
    return redirect("robustness:test_detail", pk=schedule.robustness_test_id)


@login_required
def schedule_stop(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=schedule.robustness_test_id)
    stop_schedule(schedule)
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.CANCEL,
        target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=schedule,
        target_name=f"{schedule.robustness_test.name} / {schedule.test_suite.name}",
        detail="중지",
    )
    messages.info(request, f"'{schedule.test_suite.name}' 스케줄을 정지했습니다.")
    return redirect("robustness:test_detail", pk=schedule.robustness_test_id)


@login_required
def schedule_trigger_now(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=schedule.robustness_test_id)
    # 세션이 없으면 새로 시작
    if not schedule.started_at:
        start_schedule(schedule)
    _trigger_once(schedule)
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.RUN,
        target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=schedule,
        target_name=f"{schedule.robustness_test.name} / {schedule.test_suite.name}",
        detail="수동 실행",
    )
    messages.success(request, "수동 실행을 시작했습니다.")
    return redirect("robustness:test_detail", pk=schedule.robustness_test_id)


@login_required
def schedule_delete(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    test_pk = schedule.robustness_test_id
    if request.method == "POST":
        label = f"{schedule.robustness_test.name} / {schedule.test_suite.name}"
        schedule_id = str(schedule.pk)
        schedule.delete()
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.DELETE,
            target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE,
            target_name=label, detail=f"id={schedule_id}",
        )
        messages.info(request, "스케줄을 삭제했습니다.")
    return redirect("robustness:test_detail", pk=test_pk)


@login_required
def test_toggle_parallel(request, pk):
    """잡 병렬 실행 토글 — 테스트 전체에 적용."""
    test = get_object_or_404(RobustnessTest, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=test.pk)
    test.parallel_jobs = not test.parallel_jobs
    test.save(update_fields=["parallel_jobs"])
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.UPDATE,
        target_type=ActivityLog.TargetType.RELIABILITY_TEST, target=test,
        detail=f"parallel_jobs={'on' if test.parallel_jobs else 'off'}",
    )
    if request.headers.get("HX-Request") == "true":
        return HttpResponse(status=204)
    return redirect("robustness:test_detail", pk=test.pk)


@login_required
def schedule_reset_stats(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=schedule.robustness_test_id)
    reset_schedule_stats(schedule)
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.UPDATE,
        target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=schedule,
        target_name=f"{schedule.robustness_test.name} / {schedule.test_suite.name}",
        detail="통계 초기화",
    )
    messages.info(request, "통계를 초기화했습니다.")
    return redirect("robustness:test_detail", pk=schedule.robustness_test_id)


@login_required
def schedule_reset_all_stats(request, pk):
    """해당 테스트의 모든 스케줄 통계를 일괄 초기화한다."""
    test = get_object_or_404(RobustnessTest, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=test.pk)

    schedules = list(test.schedules.all())
    count = 0
    for schedule in schedules:
        reset_schedule_stats(schedule)
        count += 1

    if count:
        messages.info(request, f"{count}개 스케줄의 통계를 초기화했습니다.")
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.RELIABILITY_TEST, target=test,
            detail=f"통계 일괄 초기화 {count}개",
        )
    else:
        messages.info(request, "초기화할 스케줄이 없습니다.")
    return redirect("robustness:test_detail", pk=test.pk)


# =============================================
# HTMX fragments
# =============================================

@login_required
def test_detail_fragment(request, pk):
    """test_detail 페이지의 스케줄 테이블을 주기적으로 갱신."""
    test = get_object_or_404(RobustnessTest, pk=pk)
    sweep_active_schedules()
    schedules = list(
        test.schedules.select_related("test_suite").order_by("created_at")
    )
    return render(request, "robustness/_schedule_table.html", {
        "test": test,
        "schedules": schedules,
    })


@login_required
def schedule_runs(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    sweep_schedule(schedule)
    runs = schedule.runs.select_related("suite_run").order_by("-triggered_at")[:100]
    return render(request, "robustness/schedule_runs.html", {
        "schedule": schedule,
        "runs": runs,
    })
