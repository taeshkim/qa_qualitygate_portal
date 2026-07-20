import logging
import re
from urllib.parse import unquote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone

from apps.activitylog.models import ActivityLog
from apps.integrations.jenkins import JenkinsClient, JenkinsError
from apps.integrations.log_errors import extract_error_log
from apps.notifications import events as nev
from apps.notifications.services import send_suite_run_event_async
from . import services
from .diagnostics import analyze_console_output
from .models import (
    SuiteJenkinsCredential,
    TestSuite, TestSuiteJob, TestSuiteRun, TestSuiteJobRun,
)

logger = logging.getLogger(__name__)


def _parse_jenkins_input(raw: str) -> str:
    """Jenkins URL 또는 폴더 경로를 정규화된 폴더 경로로 변환."""
    raw = raw.strip().rstrip("/")
    if "/job/" in raw:
        match = re.search(r"/job/(.+?)(?:\?|$)", raw)
        if match:
            job_part = match.group(1).rstrip("/")
            parts = [unquote(p) for p in job_part.split("/job/")]
            return "/".join(parts)
    return raw


# =============================================
# Suite CRUD
# =============================================

@login_required
def suite_list(request):
    suites = TestSuite.objects.filter(is_active=True).prefetch_related("jobs")
    return render(request, "suites/suite_list.html", {"suites": suites})


# =============================================
# Jenkins 자격증명 관리 (내부 / 외부 default)
# =============================================

@login_required
def credentials_manage(request):
    """Test Suite 용 Jenkins 자격증명(내부/외부) 중앙 관리. admin 전용.

    - 저장된 token 평문은 화면/응답 어디에도 노출하지 않는다 (마스킹만 표시).
    - 입력칸을 비운 채 저장하면 기존 token 유지. 새 값 입력 시에만 갱신.
    """
    if not request.user.is_admin:
        messages.error(request, "관리자만 접근할 수 있습니다.")
        return redirect("suites:suite_list")

    internal, _ = SuiteJenkinsCredential.objects.get_or_create(
        kind=SuiteJenkinsCredential.Kind.INTERNAL,
    )
    external, _ = SuiteJenkinsCredential.objects.get_or_create(
        kind=SuiteJenkinsCredential.Kind.EXTERNAL,
    )

    if request.method == "POST":
        kind = request.POST.get("kind", "")
        target = {"internal": internal, "external": external}.get(kind)
        if target is None:
            messages.error(request, "잘못된 요청입니다.")
            return redirect("suites:credentials")

        target.base_url = request.POST.get("base_url", "").strip().rstrip("/")
        target.user = request.POST.get("user", "").strip()
        new_token = request.POST.get("api_token", "").strip()
        if new_token:
            target.api_token = new_token
        target.updated_by = request.user
        target.save()

        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.SUITE,
            target_name=f"Jenkins 자격증명 ({target.get_kind_display()})",
            detail=f"base_url={target.base_url or '(empty)'}",
        )
        messages.success(request, f"{target.get_kind_display()} 자격증명을 저장했습니다.")
        return redirect("suites:credentials")

    return render(request, "suites/credentials.html", {
        "credentials": [internal, external],
    })


@login_required
def suite_register(request):
    if request.method == "POST":
        source_type = request.POST.get("source_type", "default")
        raw_input = request.POST.get("jenkins_folder_path", "").strip().strip("/")
        name = request.POST.get("name", "").strip()
        ext_url = request.POST.get("jenkins_base_url", "").strip().rstrip("/")
        ext_user = request.POST.get("jenkins_user", "").strip()
        ext_token = request.POST.get("jenkins_api_token", "").strip()

        form_ctx = {
            "source_type": source_type,
            "form": {
                "jenkins_folder_path": raw_input,
                "name": name,
                "jenkins_base_url": ext_url,
                "jenkins_user": ext_user,
                "jenkins_api_token": ext_token,
            },
        }

        if not raw_input:
            return render(request, "suites/suite_register.html", {**form_ctx, "error": "Jenkins 폴더 경로를 입력하세요."})

        if source_type == "external":
            # 비어있으면 default 외부 자격증명으로 폴백 — 평문은 form 에 다시 채우지 않는다.
            ext_default = SuiteJenkinsCredential.objects.filter(
                kind=SuiteJenkinsCredential.Kind.EXTERNAL,
            ).first()
            if ext_default:
                ext_url = ext_url or ext_default.base_url
                ext_user = ext_user or ext_default.user
                ext_token = ext_token or ext_default.api_token
            if not (ext_url and ext_user and ext_token):
                return render(request, "suites/suite_register.html", {
                    **form_ctx, "error": "외부 Jenkins URL / User / API Token을 모두 입력하세요. (default 자격증명 미설정)",
                })
            client = JenkinsClient(base_url=ext_url, user=ext_user, api_token=ext_token)
            suite_base_url, suite_user, suite_token = ext_url, ext_user, ext_token
        else:
            client = JenkinsClient()
            suite_base_url, suite_user, suite_token = "", "", ""

        folder_path = _parse_jenkins_input(raw_input)

        try:
            jobs = client.list_folder_jobs(folder_path)
        except JenkinsError as e:
            return render(request, "suites/suite_register.html", {**form_ctx, "error": f"Jenkins 연결 실패: {e}"})

        if not jobs:
            return render(request, "suites/suite_register.html", {**form_ctx, "error": "해당 폴더에 Job이 없습니다."})

        folder_info = client.get_job_info(folder_path)
        if not name:
            name = folder_info.get("name", folder_path.split("/")[-1])

        suite, created = TestSuite.objects.get_or_create(
            jenkins_base_url=suite_base_url,
            jenkins_folder_path=folder_path,
            defaults={
                "name": name,
                "jenkins_folder_url": folder_info.get("url", ""),
                "jenkins_user": suite_user,
                "jenkins_api_token": suite_token,
            },
        )

        if not created:
            suite.name = name
            suite.jenkins_user = suite_user
            suite.jenkins_api_token = suite_token
            suite.jenkins_folder_url = folder_info.get("url", suite.jenkins_folder_url)
            suite.save(update_fields=[
                "name", "jenkins_user", "jenkins_api_token",
                "jenkins_folder_url", "updated_at",
            ])
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

        ActivityLog.log(
            user=request.user,
            action=ActivityLog.Action.CREATE if created else ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.SUITE,
            target=suite,
            detail=f"Jobs: {len(jobs)}",
        )

        return redirect("suites:suite_detail", pk=suite.pk)

    ext_default = SuiteJenkinsCredential.objects.filter(
        kind=SuiteJenkinsCredential.Kind.EXTERNAL,
    ).first()
    return render(request, "suites/suite_register.html", {
        "source_type": "default",
        "form": {
            "jenkins_base_url": ext_default.base_url if ext_default else "",
            "jenkins_user": ext_default.user if ext_default else "",
            "jenkins_api_token": "",
        },
        "has_external_default_token": bool(ext_default and ext_default.api_token),
    })


@login_required
def suite_detail(request, pk):
    suite = get_object_or_404(TestSuite, pk=pk)
    jobs = list(suite.jobs.filter(is_active=True))

    # 실행 중인 Run 상태 업데이트
    for run in suite.runs.filter(status=TestSuiteRun.Status.RUNNING):
        _poll_run_status(run)

    runs = suite.runs.order_by("-started_at")[:10]

    # 각 Job의 Jenkins 실시간 상태 조회
    client = suite.get_jenkins_client()
    job_status_list = []
    for job in jobs:
        is_building = False
        try:
            is_building = client.is_job_building(job.job_full_path)
        except JenkinsError:
            pass
        job_status_list.append({
            "job": job,
            "is_building": is_building,
        })

    # Idle 먼저, Running 나중에 정렬
    job_status_list.sort(key=lambda x: x["is_building"])

    active_run = (
        suite.runs.filter(status=TestSuiteRun.Status.RUNNING)
        .order_by("-started_at")
        .first()
    )

    return render(request, "suites/suite_detail.html", {
        "suite": suite, "job_status_list": job_status_list, "runs": runs,
        "active_run": active_run,
    })


@login_required
def suite_edit(request, pk):
    suite = get_object_or_404(TestSuite, pk=pk)
    if not request.user.is_admin:
        return render(request, "suites/suite_detail.html", {
            "suite": suite, "error": "관리자만 수정할 수 있습니다.",
        })

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        folder_path_raw = request.POST.get("jenkins_folder_path", "").strip()
        ext_url = request.POST.get("jenkins_base_url", "").strip().rstrip("/")
        ext_user = request.POST.get("jenkins_user", "").strip()
        ext_token = request.POST.get("jenkins_api_token", "").strip()

        if name:
            suite.name = name
        if folder_path_raw:
            suite.jenkins_folder_path = _parse_jenkins_input(folder_path_raw)

        if suite.is_external or ext_url:
            suite.jenkins_base_url = ext_url
            suite.jenkins_user = ext_user
            if ext_token:
                suite.jenkins_api_token = ext_token

        try:
            folder_info = suite.get_jenkins_client().get_job_info(suite.jenkins_folder_path)
            suite.jenkins_folder_url = folder_info.get("url", "")
        except JenkinsError:
            pass

        suite.save()
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.SUITE, target=suite,
        )
        return redirect("suites:suite_detail", pk=suite.pk)

    return render(request, "suites/suite_edit.html", {"suite": suite})


@login_required
def suite_delete(request, pk):
    suite = get_object_or_404(TestSuite, pk=pk)
    if request.method == "POST":
        suite_name = suite.name
        suite_id = str(suite.pk)
        suite.delete()
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.DELETE,
            target_type=ActivityLog.TargetType.SUITE,
            target_name=suite_name, detail=f"id={suite_id}",
        )
        return redirect("suites:suite_list")
    return render(request, "suites/suite_delete_confirm.html", {"suite": suite})


@login_required
def suite_sync(request, pk):
    suite = get_object_or_404(TestSuite, pk=pk)
    try:
        client = suite.get_jenkins_client()
        jobs = client.list_folder_jobs(suite.jenkins_folder_path)
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
    except JenkinsError:
        pass
    return redirect("suites:suite_detail", pk=suite.pk)


# =============================================
# Suite Run (순차 실행 + 대기열)
# =============================================

@login_required
def suite_run(request, pk):
    """Run: 선택한 1개 Job 실행. (service 의 얇은 래퍼 — PRD §12 F-1)"""
    suite = get_object_or_404(TestSuite, pk=pk)
    job_id = request.POST.get("job_id", "")

    if not job_id:
        return redirect("suites:suite_detail", pk=suite.pk)

    try:
        suite_run = services.trigger_suite_run(
            suite=suite, user=request.user, job_id=job_id,
        )
    except services.SuiteRunError as e:
        messages.error(request, str(e))
        return redirect("suites:suite_detail", pk=suite.pk)

    return redirect("suites:run_detail", pk=suite_run.pk)


@login_required
def run_detail(request, pk):
    suite_run = get_object_or_404(
        TestSuiteRun.objects.select_related("suite"), pk=pk,
    )
    if suite_run.status != TestSuiteRun.Status.COMPLETED:
        _poll_run_status(suite_run)

    job_runs = suite_run.job_runs.select_related("job").all()
    return render(request, "suites/run_detail.html", {
        "suite_run": suite_run, "job_runs": job_runs,
    })


@login_required
def run_status_fragment(request, pk):
    """HTMX polling용."""
    suite_run = get_object_or_404(TestSuiteRun, pk=pk)
    if suite_run.status != TestSuiteRun.Status.COMPLETED:
        _poll_run_status(suite_run)

    job_runs = suite_run.job_runs.select_related("job").all()
    return render(request, "suites/_run_status.html", {
        "suite_run": suite_run, "job_runs": job_runs,
    })


@login_required
def run_delete(request, pk):
    """개별 실행 이력 삭제 (QPE_ADMIN 전용). 실행 중인 Run은 삭제 불가."""
    run = get_object_or_404(TestSuiteRun.objects.select_related("suite"), pk=pk)
    suite_pk = run.suite.pk
    if not request.user.is_admin:
        messages.error(request, "실행 이력 삭제는 관리자(QPE Admin)만 가능합니다.")
        return redirect("suites:suite_detail", pk=suite_pk)
    if request.method != "POST":
        return redirect("suites:suite_detail", pk=suite_pk)
    if run.status == TestSuiteRun.Status.RUNNING:
        messages.error(request, "실행 중인 Run은 삭제할 수 없습니다. 먼저 중단 후 다시 시도하세요.")
        return redirect("suites:suite_detail", pk=suite_pk)
    run.delete()
    messages.success(request, "실행 이력 1건을 삭제했습니다.")
    return redirect("suites:suite_detail", pk=suite_pk)


@login_required
def runs_clear(request, pk):
    """Suite의 전체 실행 이력 초기화 (QPE_ADMIN 전용). 실행 중인 Run은 제외."""
    suite = get_object_or_404(TestSuite, pk=pk)
    if not request.user.is_admin:
        messages.error(request, "실행 이력 초기화는 관리자(QPE Admin)만 가능합니다.")
        return redirect("suites:suite_detail", pk=suite.pk)
    if request.method != "POST":
        return redirect("suites:suite_detail", pk=suite.pk)
    deletable = suite.runs.exclude(status=TestSuiteRun.Status.RUNNING)
    count = deletable.count()
    deletable.delete()
    remaining = suite.runs.filter(status=TestSuiteRun.Status.RUNNING).count()
    msg = f"실행 이력 {count}건을 삭제했습니다."
    if remaining:
        msg += f" (실행 중 {remaining}건은 제외)"
    messages.success(request, msg)
    return redirect("suites:suite_detail", pk=suite.pk)


@login_required
def job_run_console(request, pk):
    """Job Run의 저장된 Jenkins 콘솔 로그 표시."""
    job_run = get_object_or_404(
        TestSuiteJobRun.objects.select_related("job", "suite_run", "suite_run__suite"),
        pk=pk,
    )
    return render(request, "suites/console_output.html", {
        "job_run": job_run,
        "console_diagnostics": analyze_console_output(job_run.console_output),
    })


@login_required
def job_run_error_log(request, pk):
    """Job Run 의 콘솔 로그에서 실패 원인 부분만 파싱해서 보여준다."""
    from apps.integrations.url_matcher import extract_urls, invalidate_match_cache
    # 담당자 변경이 즉시 반영되도록 페이지 진입 시 캐시 명시 무효화 (signal 보강).
    invalidate_match_cache()
    job_run = get_object_or_404(
        TestSuiteJobRun.objects.select_related("job", "suite_run", "suite_run__suite"),
        pk=pk,
    )
    error_log = extract_error_log(job_run.console_output)
    return render(request, "suites/error_log.html", {
        "job_run": job_run,
        "error_log": error_log,
        # 표는 "추출된 에러" 카드 안의 URL 만 매칭. 정상 step URL 은 제외.
        "matched_urls": extract_urls(error_log or ""),
        "notify_source_type": "suite_job",
        "notify_source_id": str(job_run.pk),
        "notify_stage": "",
    })


def _abort_suite_run(suite_run):
    """진행 중인 SuiteRun 을 강제 중단한다.
    - QUEUED/RUNNING job 은 Jenkins stop 호출 후 ABORTED 로 마킹
    - PENDING job 은 트리거 없이 ABORTED 로 마킹
    - SuiteRun 은 COMPLETED 로 마무리하여 더 이상 폴링/트리거되지 않도록 한다.
    """
    if suite_run.status == TestSuiteRun.Status.COMPLETED:
        return False

    client = suite_run.suite.get_jenkins_client()
    aborted_any = False

    job_runs = list(suite_run.job_runs.select_related("job").all())
    for jr in job_runs:
        if jr.status in (TestSuiteJobRun.Status.QUEUED, TestSuiteJobRun.Status.RUNNING):
            if jr.jenkins_build_number:
                try:
                    client.stop_build(jr.job.job_full_path, jr.jenkins_build_number)
                except Exception:
                    logger.exception(
                        "stop_build failed for %s #%s",
                        jr.job.job_full_path, jr.jenkins_build_number,
                    )
            jr.status = TestSuiteJobRun.Status.ABORTED
            jr.completed_at = timezone.now()
            jr.save(update_fields=["status", "completed_at"])
            aborted_any = True
        elif jr.status == TestSuiteJobRun.Status.PENDING:
            jr.status = TestSuiteJobRun.Status.ABORTED
            jr.completed_at = timezone.now()
            jr.save(update_fields=["status", "completed_at"])
            aborted_any = True

    # 집계 갱신
    all_jrs = suite_run.job_runs.all()
    suite_run.passed_jobs = all_jrs.filter(
        status=TestSuiteJobRun.Status.SUCCESS
    ).count()
    suite_run.failed_jobs = all_jrs.filter(
        status__in=[
            TestSuiteJobRun.Status.FAILURE,
            TestSuiteJobRun.Status.ABORTED,
        ]
    ).count()
    suite_run.status = TestSuiteRun.Status.COMPLETED
    suite_run.completed_at = timezone.now()
    suite_run.save()
    return aborted_any


@login_required
def queue_view(request):
    """대기열 현황: 실행 중인 모든 Suite Run + 대기 중인 Job 목록."""
    # Running 상태 Run 전부 폴링 업데이트
    for run in TestSuiteRun.objects.filter(status=TestSuiteRun.Status.RUNNING):
        _poll_run_status(run)

    active_runs = (
        TestSuiteRun.objects.filter(status=TestSuiteRun.Status.RUNNING)
        .select_related("suite", "triggered_by")
        .order_by("-started_at")
    )

    queue_data = []
    for run in active_runs:
        job_runs = run.job_runs.select_related("job").all()
        pending = [jr for jr in job_runs if jr.status == TestSuiteJobRun.Status.PENDING]
        queued = [jr for jr in job_runs if jr.status == TestSuiteJobRun.Status.QUEUED]
        running = [jr for jr in job_runs if jr.status == TestSuiteJobRun.Status.RUNNING]
        done = [jr for jr in job_runs if jr.status in (
            TestSuiteJobRun.Status.SUCCESS,
            TestSuiteJobRun.Status.FAILURE,
            TestSuiteJobRun.Status.ABORTED,
        )]
        queue_data.append({
            "run": run,
            "job_runs": job_runs,
            "pending_count": len(pending),
            "queued_count": len(queued),
            "running_count": len(running),
            "done_count": len(done),
        })

    return render(request, "suites/queue.html", {"queue_data": queue_data})


@login_required
def queue_stop_all(request):
    """대기열의 모든 실행 중인 항목(Robustness 스케줄 + Suite Run + Job)을 중단한다."""
    if request.method != "POST":
        return redirect("suites:queue")

    # Robustness 스케줄을 먼저 정지 — stop_schedule 이 자체적으로 자기 SuiteRun 도 abort
    from apps.robustness.models import RobustnessRun, RobustnessSchedule
    from apps.robustness.services import stop_schedule

    schedule_count = 0
    for sched in RobustnessSchedule.objects.filter(is_running=True):
        try:
            stop_schedule(sched)
            schedule_count += 1
        except Exception:
            logger.exception("Failed to stop schedule %s", sched.pk)

    # 남아있는 모든 RUNNING SuiteRun 강제 중단
    run_count = 0
    for run in TestSuiteRun.objects.filter(status=TestSuiteRun.Status.RUNNING):
        try:
            if _abort_suite_run(run):
                run_count += 1
        except Exception:
            logger.exception("Failed to abort suite_run %s", run.pk)

    # 혹시 남은 진행중 RobustnessRun(고아) 도 ABORTED 처리
    RobustnessRun.objects.filter(
        status__in=[RobustnessRun.Status.PENDING, RobustnessRun.Status.RUNNING]
    ).update(
        status=RobustnessRun.Status.ABORTED,
        completed_at=timezone.now(),
    )

    messages.success(
        request,
        f"실행을 중단했습니다. (스케줄 {schedule_count}개, Suite Run {run_count}개)",
    )
    return redirect("suites:queue")


# =============================================
# Internal: 순차 트리거 + 폴링
# =============================================

def _try_trigger_next(suite_run):
    """
    PENDING 상태인 Job을 순회하면서:
    1. Jenkins에서 이미 돌고 있으면 → 그 빌드를 추적 (새로 트리거 안 함)
    2. 안 돌고 있으면 → 트리거
    한 번에 1개만 새로 트리거.
    """
    client = suite_run.suite.get_jenkins_client()
    pending_jobs = suite_run.job_runs.filter(
        status=TestSuiteJobRun.Status.PENDING,
    ).select_related("job").order_by("job__order")

    triggered_new = False

    for job_run in pending_jobs:
        try:
            last_build = client.get_last_build_info(job_run.job.job_full_path)
        except JenkinsError:
            last_build = {}

        is_building = last_build.get("building", False)

        if is_building:
            # 이미 Jenkins에서 돌고 있음 → 그 빌드를 추적 (트리거 안 함)
            job_run.status = TestSuiteJobRun.Status.RUNNING
            job_run.jenkins_build_number = last_build.get("number")
            job_run.jenkins_build_url = last_build.get("url", "")
            job_run.started_at = timezone.now()
            job_run.save(update_fields=[
                "status", "jenkins_build_number", "jenkins_build_url", "started_at",
            ])
            logger.info(f"{job_run.job.job_name}: already running #{last_build.get('number')}, tracking it")
            continue  # 다음 Job도 확인

        if not triggered_new:
            # 안 돌고 있음 → 트리거
            try:
                result = client.trigger_build(job_run.job.job_full_path)
                job_run.status = TestSuiteJobRun.Status.QUEUED
                job_run.jenkins_build_number = result.get("build_number")
                job_run.started_at = timezone.now()
                job_run.save(update_fields=["status", "jenkins_build_number", "started_at"])
                logger.info(f"Triggered {job_run.job.job_name} #{job_run.jenkins_build_number}")
                triggered_new = True  # 1개만 새로 트리거
            except JenkinsError as e:
                logger.error(f"Failed to trigger {job_run.job.job_name}: {e}")
                job_run.status = TestSuiteJobRun.Status.FAILURE
                job_run.completed_at = timezone.now()
                job_run.console_output = f"[trigger failed] {e}"
                job_run.save(update_fields=["status", "completed_at", "console_output"])
                continue

    return triggered_new


def _poll_run_status(suite_run):
    """트리거한 빌드 상태를 조회 + 완료 시 다음 Job 트리거."""
    client = suite_run.suite.get_jenkins_client()

    # 1. QUEUED/RUNNING 상태인 Job의 빌드 결과 업데이트
    active_runs = suite_run.job_runs.filter(
        status__in=[
            TestSuiteJobRun.Status.QUEUED,
            TestSuiteJobRun.Status.RUNNING,
        ]
    ).select_related("job")

    any_completed = False

    for job_run in active_runs:
        build_number = job_run.jenkins_build_number
        if not build_number:
            continue

        try:
            build_info = client.get_build_info(
                job_run.job.job_full_path, build_number
            )
        except JenkinsError as e:
            if hasattr(e, "status") and e.status == 404:
                continue
            logger.warning(f"Failed to poll {job_run.job.job_name} #{build_number}: {e}")
            continue

        building = build_info.get("building", False)
        result = build_info.get("result")
        job_run.jenkins_build_url = build_info.get("url", "")

        # 스테이지 정보
        stages = client.get_build_stages(job_run.job.job_full_path, build_number)
        if stages:
            job_run.stages_json = stages
            in_progress = [s for s in stages if s["status"] == "IN_PROGRESS"]
            if in_progress:
                job_run.current_stage = in_progress[0]["name"]
            else:
                completed_stages = [s for s in stages if s["status"] in ("SUCCESS", "FAILED")]
                if completed_stages:
                    job_run.current_stage = completed_stages[-1]["name"]

        if building:
            job_run.status = TestSuiteJobRun.Status.RUNNING
        elif result == "SUCCESS":
            job_run.status = TestSuiteJobRun.Status.SUCCESS
            job_run.duration_ms = build_info.get("duration")
            job_run.completed_at = timezone.now()
            any_completed = True
            # 콘솔 로그 저장
            try:
                job_run.console_output = client.get_console_output(
                    job_run.job.job_full_path, build_number
                )
            except JenkinsError:
                logger.warning(f"Failed to fetch console for {job_run.job.job_name} #{build_number}")
        elif result == "FAILURE":
            job_run.status = TestSuiteJobRun.Status.FAILURE
            job_run.duration_ms = build_info.get("duration")
            job_run.completed_at = timezone.now()
            any_completed = True
            # 콘솔 로그 저장
            try:
                job_run.console_output = client.get_console_output(
                    job_run.job.job_full_path, build_number
                )
            except JenkinsError:
                logger.warning(f"Failed to fetch console for {job_run.job.job_name} #{build_number}")
        elif result == "ABORTED":
            job_run.status = TestSuiteJobRun.Status.ABORTED
            job_run.completed_at = timezone.now()
            any_completed = True
            # 콘솔 로그 저장
            try:
                job_run.console_output = client.get_console_output(
                    job_run.job.job_full_path, build_number
                )
            except JenkinsError:
                logger.warning(f"Failed to fetch console for {job_run.job.job_name} #{build_number}")

        # 빌드 완료 시 전체 스테이지 수 업데이트
        if not building and stages and len(stages) > job_run.job.total_stages:
            job_run.job.total_stages = len(stages)
            job_run.job.save(update_fields=["total_stages"])

        job_run.save()

    # 2. 집계 업데이트
    all_job_runs = suite_run.job_runs.all()
    passed = all_job_runs.filter(status=TestSuiteJobRun.Status.SUCCESS).count()
    failed = all_job_runs.filter(
        status__in=[TestSuiteJobRun.Status.FAILURE, TestSuiteJobRun.Status.ABORTED]
    ).count()

    suite_run.passed_jobs = passed
    suite_run.failed_jobs = failed

    if passed + failed >= suite_run.total_jobs:
        suite_run.status = TestSuiteRun.Status.COMPLETED
        suite_run.completed_at = timezone.now()
        suite_run.save()
        if failed > 0:
            send_suite_run_event_async(nev.SUITE_RUN_FAILED, suite_run)
        else:
            send_suite_run_event_async(nev.SUITE_RUN_PASSED, suite_run)
        return

    suite_run.save(update_fields=["passed_jobs", "failed_jobs"])

    # 3. Job이 완료됐으면 다음 PENDING Job 트리거
    if any_completed:
        _try_trigger_next(suite_run)
    else:
        # 아무것도 QUEUED/RUNNING이 아닌데 PENDING이 있으면 트리거 시도
        has_active = suite_run.job_runs.filter(
            status__in=[TestSuiteJobRun.Status.QUEUED, TestSuiteJobRun.Status.RUNNING]
        ).exists()
        if not has_active:
            _try_trigger_next(suite_run)
