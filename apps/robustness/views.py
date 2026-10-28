import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
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
    # 목록은 스케줄/잡 상태(라이브 데이터)를 보여주지 않고 개수만 보여주므로 Jenkins
    # 폴링(sweep)을 하지 않는다 — "메뉴 들어가면 느린" 주범 제거. 스케줄 진행/트리거는
    # 상세 페이지의 HTMX fragment 가 담당한다. 개수는 행당 2쿼리(N+1) 대신 annotate 로
    # 한 번에 집계.
    # annotate(집계) 가 모델 Meta.ordering(name) 을 떨어뜨려 생성순으로 나오므로
    # 테스트 이름 오름차순을 명시한다.
    tests = RobustnessTest.objects.filter(is_active=True).annotate(
        n_schedules=Count("schedules", distinct=True),
        n_running=Count(
            "schedules", filter=Q(schedules__is_running=True), distinct=True,
        ),
    ).order_by("name")
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
    # 초기 진입은 DB 상태로 즉시 렌더한다 — 동기 sweep(Jenkins 직렬 폴링)으로 페이지
    # 로드를 막지 않는다. 라이브 갱신은 _schedule_table 의 HTMX fragment(hx-trigger
    # "load, every 10s") 가 비동기로 sweep+swap 하므로 첫 페인트 직후 최신값으로 채워진다.
    test = get_object_or_404(RobustnessTest, pk=pk)

    from apps.suites.models import TapSuite

    schedules = list(
        test.schedules.select_related("test_suite", "tap_suite").order_by("created_at")
    )
    available_suites = TestSuite.objects.filter(is_active=True).exclude(
        pk__in=[s.test_suite_id for s in schedules if s.test_suite_id],
    )
    # UI Automation 탭 — TAP Suite 중 이 테스트에 아직 연동 안 된 것
    available_tap_suites = TapSuite.objects.filter(is_removed=False).exclude(
        pk__in=[s.tap_suite_id for s in schedules if s.tap_suite_id],
    )
    return render(request, "robustness/test_detail.html", {
        "test": test,
        "schedules": schedules,
        "available_suites": available_suites,
        "available_tap_suites": available_tap_suites,
        # 일괄 시작/중지 버튼 활성화 판정 — 이미 로드한 schedules 로 계산(추가 쿼리 없음).
        "any_running": any(s.is_running for s in schedules),
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
    tap_suite_id = request.POST.get("tap_suite_id")
    interval_raw = request.POST.get("interval_minutes", "60")

    if not suite_id and not tap_suite_id:
        messages.error(request, "Test Suite 를 선택하세요.")
        return redirect("robustness:test_detail", pk=test.pk)

    try:
        interval = int(interval_raw)
    except ValueError:
        return HttpResponseBadRequest("interval_minutes must be an integer")

    if interval < 1:
        messages.error(request, "실행 간격은 1분 이상이어야 합니다.")
        return redirect("robustness:test_detail", pk=test.pk)

    if tap_suite_id:
        # UI Automation(TAP) 스케줄 — PRD docs/prd_ui_test_automation.md 확장
        from apps.suites.models import TapSuite
        target = get_object_or_404(TapSuite, pk=tap_suite_id)
        schedule, created = RobustnessSchedule.objects.get_or_create(
            robustness_test=test,
            tap_suite=target,
            defaults={"interval_minutes": interval},
        )
    else:
        target = get_object_or_404(TestSuite, pk=suite_id)
        schedule, created = RobustnessSchedule.objects.get_or_create(
            robustness_test=test,
            test_suite=target,
            defaults={"interval_minutes": interval},
        )
    if not created:
        schedule.interval_minutes = interval
        schedule.save(update_fields=["interval_minutes"])
        messages.info(request, f"'{target.name}' 스케줄의 간격을 {interval}분으로 갱신했습니다.")
    else:
        messages.success(request, f"'{target.name}' 를 {interval}분 간격으로 추가했습니다.")

    ActivityLog.log(
        user=request.user,
        action=ActivityLog.Action.CREATE if created else ActivityLog.Action.UPDATE,
        target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=schedule,
        target_name=f"{test.name} / {target.name}",
        detail=f"interval={interval}분" + (" / UI Automation(TAP)" if tap_suite_id else ""),
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
        # start_schedule 은 race-free 멱등 — 목록 조회 후 다른 요청(개별 시작 등)이
        # 먼저 시작했으면 False 를 반환하고 아무것도 바꾸지 않는다. 그 경우 sweep 도
        # 건너뛴다(진행 중 사이클을 timeout 마감으로 죽이지 않게 — 2026-07-16 사건).
        if not start_schedule(schedule):
            continue
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
            target_name=f"{schedule.robustness_test.name} / {schedule.target_name}",
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
    if not start_schedule(schedule):
        # 이미 실행 중(중복 클릭/일괄 시작과 경합) — 진행 중 사이클을 건드리지 않음.
        messages.info(request, f"'{schedule.target_name}' 스케줄은 이미 실행 중입니다.")
        return redirect("robustness:test_detail", pk=schedule.robustness_test_id)
    sweep_schedule(schedule)
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.RUN,
        target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=schedule,
        target_name=f"{schedule.robustness_test.name} / {schedule.target_name}",
        detail=f"시작 / interval={schedule.interval_minutes}분",
    )
    messages.success(request, f"'{schedule.target_name}' 스케줄을 시작했습니다.")
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
        target_name=f"{schedule.robustness_test.name} / {schedule.target_name}",
        detail="중지",
    )
    messages.info(request, f"'{schedule.target_name}' 스케줄을 정지했습니다.")
    return redirect("robustness:test_detail", pk=schedule.robustness_test_id)


@login_required
def schedule_trigger_now(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=schedule.robustness_test_id)
    # 세션이 없으면 새로 시작
    if not schedule.started_at:
        start_schedule(schedule)
    if schedule.tap_suite_id:
        from .services import _trigger_tap_once
        _trigger_tap_once(schedule)
    else:
        _trigger_once(schedule)
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.RUN,
        target_type=ActivityLog.TargetType.RELIABILITY_SCHEDULE, target=schedule,
        target_name=f"{schedule.robustness_test.name} / {schedule.target_name}",
        detail="수동 실행",
    )
    messages.success(request, "수동 실행을 시작했습니다.")
    return redirect("robustness:test_detail", pk=schedule.robustness_test_id)


@login_required
def schedule_delete(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    test_pk = schedule.robustness_test_id
    if request.method == "POST":
        label = f"{schedule.robustness_test.name} / {schedule.target_name}"
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
    # 단일 테스트 모드 중에는 병렬 토글 비활성(UI 버튼도 disabled) — 방어적으로 무시.
    if test.single_job_mode:
        if request.headers.get("HX-Request") == "true":
            return HttpResponse(status=204)
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
def test_toggle_single(request, pk):
    """단일 테스트 모드 토글 — ON 시 Suite 의 idle 잡 1개만 실행하고, 잡 병렬 실행은
    강제 OFF 한다(상호 배타)."""
    test = get_object_or_404(RobustnessTest, pk=pk)
    if request.method != "POST":
        return redirect("robustness:test_detail", pk=test.pk)
    test.single_job_mode = not test.single_job_mode
    fields = ["single_job_mode"]
    if test.single_job_mode and test.parallel_jobs:
        test.parallel_jobs = False  # 단일 모드 켜면 병렬 강제 OFF
        fields.append("parallel_jobs")
    test.save(update_fields=fields)
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.UPDATE,
        target_type=ActivityLog.TargetType.RELIABILITY_TEST, target=test,
        detail=f"single_job_mode={'on' if test.single_job_mode else 'off'}",
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
        target_name=f"{schedule.robustness_test.name} / {schedule.target_name}",
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
        test.schedules.select_related("test_suite", "tap_suite").order_by("created_at")
    )
    return render(request, "robustness/_schedule_table.html", {
        "test": test,
        "schedules": schedules,
        "any_running": any(s.is_running for s in schedules),
    })


@login_required
def schedule_runs(request, pk):
    schedule = get_object_or_404(RobustnessSchedule, pk=pk)
    sweep_schedule(schedule)  # 내부에서 Jenkins/TAP 자동 분기 (services.sweep_schedule)
    if schedule.is_tap:
        # UI Automation(TAP) 스케줄 — 세션 이력은 TapRun 역참조.
        # PRD docs/prd_ui_test_automation.md 확장.
        tap_runs = schedule.tap_runs.order_by("-started_at")[:100]
        return render(request, "robustness/schedule_runs.html", {
            "schedule": schedule,
            "tap_runs": tap_runs,
        })
    runs = schedule.runs.select_related("suite_run").order_by("-triggered_at")[:100]
    return render(request, "robustness/schedule_runs.html", {
        "schedule": schedule,
        "runs": runs,
    })
