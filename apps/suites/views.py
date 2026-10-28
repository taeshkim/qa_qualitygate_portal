import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
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
from .services_sync import sync_api_suites, sync_suite_jobs
from .throttle import claim_trigger_slot

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
    """Test Suites 목록 — 2탭: API Test Automation(기존 Jenkins) / UI Test Automation(TAP).

    탭 상태는 ``?tab=ui`` 쿼리스트링(딥링크/새로고침 유지). 기존 URL/기본 탭은
    무변경(하위호환). UI 탭 데이터는 TapSuite 미러(매분 cron 싱크) — 화면 진입이
    TAP 호출로 막히지 않는다. (PRD: docs/prd_ui_test_automation.md)
    """
    from .models import TapRun, TapSuite

    tab = "ui" if request.GET.get("tab") == "ui" else "api"
    suites = TestSuite.objects.filter(is_active=True).prefetch_related("jobs")
    tap_suites = list(TapSuite.objects.filter(is_removed=False).prefetch_related("runs"))
    tap_synced_at = max((s.synced_at for s in tap_suites if s.synced_at), default=None)
    return render(request, "suites/suite_list.html", {
        "suites": suites,
        "active_tab": tab,
        "tap_suites": tap_suites,
        "tap_synced_at": tap_synced_at,
        "tap_any_active": TapRun.objects.filter(status__in=TapRun.ACTIVE_STATUSES).exists(),
    })


@login_required
def tap_suite_table(request):
    """UI Test Automation 탭의 TAP Suite 테이블 fragment — HTMX 폴링(10초)용.

    진행 중 TapRun 이 있으면 이 fragment 요청 시점에 즉시 1회 폴링해 진행바를
    라이브 갱신(신뢰성은 매분 cron 이 보장 — 기존 Reliability fragment 패턴).
    """
    from .models import TapRun, TapSuite
    from .services_tap import poll_active_tap_runs

    poll_active_tap_runs()
    tap_suites = list(TapSuite.objects.filter(is_removed=False).prefetch_related("runs"))
    tap_synced_at = max((s.synced_at for s in tap_suites if s.synced_at), default=None)
    return render(request, "suites/_tap_suite_table.html", {
        "tap_suites": tap_suites,
        "tap_synced_at": tap_synced_at,
        "tap_any_active": TapRun.objects.filter(status__in=TapRun.ACTIVE_STATUSES).exists(),
    })


@login_required
def tap_suite_run(request, pk):
    """TAP Suite 실행 (can_write) — POST 후 UI 탭으로 복귀."""
    from apps.integrations.tapilot import TapError

    from .models import TapSuite
    from .services_tap import trigger_tap_suite

    tap_suite = get_object_or_404(TapSuite, pk=pk)
    if request.method != "POST":
        return redirect(f"{reverse('suites:suite_list')}?tab=ui")
    if not request.user.can_write:
        messages.error(request, "실행 권한이 없습니다 (Viewer).")
        return redirect(f"{reverse('suites:suite_list')}?tab=ui")
    try:
        run = trigger_tap_suite(tap_suite, user=request.user)
    except TapError as e:
        messages.error(request, f"TAP 실행 실패: {e}")
        return redirect(f"{reverse('suites:suite_list')}?tab=ui")
    ActivityLog.log(
        request.user, ActivityLog.Action.RUN, ActivityLog.TargetType.SUITE_RUN,
        target=run, target_name=f"[TAP] {tap_suite.name}",
        detail=f"UI Test Automation 실행 / session={run.session_id} / env={run.run_env}",
    )
    excluded = len(run.excluded_case_ids or [])
    msg = f"'{tap_suite.name}' UI 테스트를 시작했습니다 (케이스 {run.total_cases}개"
    if excluded:
        msg += f", 자동화 스크립트 없는 {excluded}개 자동 제외"
    messages.success(request, msg + ").")
    return redirect(f"{reverse('suites:suite_list')}?tab=ui")


@login_required
def tap_suite_edit(request, pk):
    """TAP Suite 편집 — 재시도 횟수(retry_count)만 (API Suite Edit 와 동일 정책, admin 전용).

    이름/케이스 구성 등은 TAP 이 원본이라 여기서 수정하지 않는다(매분 싱크가
    덮어씀). retry_count 는 포털 로컬 설정이라 싱크의 영향을 받지 않는다.
    """
    from .models import TapSuite

    tap_suite = get_object_or_404(TapSuite, pk=pk)
    if not request.user.is_admin:
        messages.error(request, "관리자만 수정할 수 있습니다.")
        return redirect(f"{reverse('suites:suite_list')}?tab=ui")

    if request.method == "POST":
        retry_raw = request.POST.get("retry_count", "").strip()
        try:
            tap_suite.retry_count = max(1, int(retry_raw))
        except (TypeError, ValueError):
            messages.error(request, "재시도 횟수는 1 이상의 정수여야 합니다.")
            return redirect("suites:tap_suite_edit", pk=tap_suite.pk)
        tap_suite.save(update_fields=["retry_count"])
        ActivityLog.log(
            request.user, ActivityLog.Action.UPDATE, ActivityLog.TargetType.SUITE,
            target=tap_suite, target_name=f"[TAP] {tap_suite.name}",
            detail=f"retry_count={tap_suite.retry_count}",
        )
        messages.success(
            request,
            f"'{tap_suite.name}' 재시도 횟수를 {tap_suite.retry_count}회로 설정했습니다.",
        )
        return redirect(f"{reverse('suites:suite_list')}?tab=ui")

    return render(request, "suites/tap_suite_edit.html", {"tap_suite": tap_suite})


@login_required
def tap_run_cancel(request, pk):
    """진행 중 TAP Run 취소 (can_write)."""
    from apps.integrations.tapilot import TapError

    from .models import TapRun
    from .services_tap import cancel_tap_run

    run = get_object_or_404(TapRun.objects.select_related("tap_suite"), pk=pk)
    if request.method != "POST":
        return redirect(f"{reverse('suites:suite_list')}?tab=ui")
    if not request.user.can_write:
        messages.error(request, "취소 권한이 없습니다 (Viewer).")
        return redirect(f"{reverse('suites:suite_list')}?tab=ui")
    try:
        cancel_tap_run(run)
    except TapError as e:
        messages.error(request, f"TAP 취소 실패: {e}")
        return redirect(f"{reverse('suites:suite_list')}?tab=ui")
    ActivityLog.log(
        request.user, ActivityLog.Action.CANCEL, ActivityLog.TargetType.SUITE_RUN,
        target=run, target_name=f"[TAP] {run.tap_suite.name}",
        detail=f"UI Test Automation 취소 / session={run.session_id}",
    )
    messages.info(request, f"'{run.tap_suite.name}' 실행을 취소했습니다.")
    return redirect(f"{reverse('suites:suite_list')}?tab=ui")


@login_required
def tap_run_detail(request, pk):
    """TAP Run 상세 — 케이스별 결과 + TestRail/TAP 딥링크."""
    from .models import TapRun
    from .services_tap import poll_tap_run

    run = get_object_or_404(TapRun.objects.select_related("tap_suite"), pk=pk)
    if run.is_active:
        poll_tap_run(run)  # 열람 시점 최신화 (실패해도 저장본으로 렌더)
        run.refresh_from_db()
    return render(request, "suites/tap_run_detail.html", {
        "run": run,
        "cases": run.raw_cases_json or [],
    })


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
def gwt_tokens_generate(request, pk):
    """이 Suite 의 GWT 토큰 없는 활성 워커에 토큰을 일괄 발급. admin 전용."""
    suite = get_object_or_404(TestSuite, pk=pk)
    if not request.user.is_admin:
        messages.error(request, "관리자만 접근할 수 있습니다.")
        return redirect("suites:suite_detail", pk=suite.pk)
    if request.method == "POST":
        n = 0
        for job in suite.jobs.filter(is_active=True, gwt_token=""):
            job.gwt_token = TestSuiteJob.gen_gwt_token()
            job.save(update_fields=["gwt_token"])
            n += 1
        messages.success(
            request,
            f"{n}개 워커에 GWT 토큰을 발급했습니다. 각 워커 Jenkins → Configure → Build Triggers "
            "→ Generic Webhook Trigger → Token 에 동일하게 등록하세요.",
        )
    return redirect("suites:suite_detail", pk=suite.pk)


@login_required
def gwt_token_regen(request, pk):
    """워커 1개의 GWT 토큰 (재)발급. admin 전용. (재발급 시 Jenkins 워커 토큰도 갱신 필요)"""
    job = get_object_or_404(TestSuiteJob, pk=pk)
    if not request.user.is_admin:
        messages.error(request, "관리자만 접근할 수 있습니다.")
        return redirect("suites:suite_detail", pk=job.suite.pk)
    if request.method == "POST":
        job.gwt_token = TestSuiteJob.gen_gwt_token()
        job.save(update_fields=["gwt_token"])
        messages.success(
            request,
            f"{job.job_name} GWT 토큰을 발급했습니다 — Jenkins 워커의 GWT Token 도 이 값으로 갱신하세요.",
        )
    return redirect("suites:suite_detail", pk=job.suite.pk)


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

    # 실행 이력 표가 행마다 job_runs/job 을 순회하므로 prefetch 로 N+1 방지.
    # 최근 활동(완료 시각, 없으면 시작 시각) 순 — 뒤늦게 완료된 실패도 상단 노출.
    from django.db.models.functions import Coalesce
    runs = (
        suite.runs.prefetch_related("job_runs__job")
        .annotate(_last_activity=Coalesce("completed_at", "started_at"))
        .order_by("-_last_activity")[:10]
    )

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

        # 재시도 횟수 = 최대 실행 횟수. 비정상 입력은 기존값 유지, 1 미만은 1 로 클램프.
        retry_raw = request.POST.get("retry_count", "").strip()
        try:
            suite.retry_count = max(1, int(retry_raw))
        except (TypeError, ValueError):
            pass

        # 잡별 실행 지연 시간(초). name="trigger_delay_<job_id>" 로 들어온 값만 반영.
        # 비정상/빈 입력은 그 잡의 기존값 유지, 0~3600(1시간) 으로 클램프(실수로 과도한
        # 값을 넣어 잡이 사실상 영원히 보류되는 것 방지). 화면에 노출된 활성 잡만 대상.
        for job in suite.jobs.filter(is_active=True):
            raw = request.POST.get(f"trigger_delay_{job.id}")
            if raw is None or raw.strip() == "":
                continue
            try:
                new_delay = min(3600, max(0, int(raw.strip())))
            except (TypeError, ValueError):
                continue
            if new_delay != job.trigger_delay_seconds:
                job.trigger_delay_seconds = new_delay
                job.save(update_fields=["trigger_delay_seconds"])

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

    jobs = suite.jobs.filter(is_active=True).order_by("order", "job_name")
    return render(request, "suites/suite_edit.html", {"suite": suite, "jobs": jobs})


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
        sync_suite_jobs(suite, jobs)
    except JenkinsError:
        pass
    return redirect("suites:suite_detail", pk=suite.pk)


@login_required
def suites_api_sync(request):
    """API 탭 상단 [Jenkins 싱크] 버튼 — 내부 Jenkins 'api' 폴더/잡 자동 탐색·등록.

    admin 전용(PRD docs/prd_ui_test_automation.md §3.4). 트리거/쓰기 없이 익명 read 로만
    스캔하며, 이미 등록된 Suite 는 건너뛴다.
    """
    from django.urls import reverse

    if not request.user.is_admin:
        messages.error(request, "관리자만 접근할 수 있습니다.")
        return redirect("suites:suite_list")
    if request.method == "POST":
        result = sync_api_suites()
        messages.success(
            request,
            f"Jenkins 싱크 완료 — 신규 등록 {result['created']}건 "
            f"(확인된 API Suite 총 {result['synced']}건).",
        )
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.SUITE,
            target_name="API 탭 Jenkins 자동 싱크",
            detail=f"created={result['created']} synced={result['synced']}",
        )
    return redirect(f"{reverse('suites:suite_list')}?tab=api")


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
    # _poll_run_status → _try_trigger_next 가 suite_run.suite (Jenkins client/throttle)
    # 를 역참조하므로 select_related 로 폴링마다의 추가 SELECT 제거.
    suite_run = get_object_or_404(
        TestSuiteRun.objects.select_related("suite"), pk=pk,
    )
    if suite_run.status != TestSuiteRun.Status.COMPLETED:
        _poll_run_status(suite_run)

    job_runs = suite_run.job_runs.select_related("job").all()
    return render(request, "suites/_run_status.html", {
        "suite_run": suite_run, "job_runs": job_runs,
    })


@login_required
def suite_run_list(request):
    """모든 Test Suite 의 단독 Run 을 한 곳에 모아 보는 '최근 실행' 뷰.

    대시보드는 최근 Test Suite Run 15건 요약만 보여주고, 이 화면은 필터로 전체
    이력을 조회한다. Jenkins 폴링은 하지 않고(목록 비용 보호) DB 상태를
    그대로 노출한다. 진행 중 Run 의 실시간 진행은 Detail 에서 확인.

    필터: ``suite`` / ``status`` / ``source`` / ``result``
      - source=manual    → 트리거 주체 있음 (API/UI 단독 실행)
      - source=scheduled → 트리거 주체 없음 (Reliability 스케줄 등 자동)
      - result=pass      → status=completed & failed_jobs=0
      - result=fail      → status=completed & failed_jobs>0
      (result 는 whitelist — "pass"/"fail" 이외의 값은 무시.
       result 는 완료 run 전제이므로 completed 외의 status 와 동시 지정되면
       모순 → status 쪽을 무시해 "결과 없음" 조용한 공집합을 막는다)
    """
    # 정렬 = 최근 활동(완료 시각, 없으면 시작 시각). long-running 실패가 뒤늦게 완료돼도
    # 목록 상단에 노출된다 — started_at 정렬이면 시작 시각 자리에 묻혀 방금 실패한 run 이
    # 안 보이던 문제(Slack 알림은 완료 시점에 오는데 목록은 시작 순이라 불일치).
    from django.db.models.functions import Coalesce
    qs = (
        TestSuiteRun.annotate_triage(
            TestSuiteRun.objects.select_related("suite", "triggered_by"))
        .annotate(_last_activity=Coalesce("completed_at", "started_at"))
        .order_by("-_last_activity")
    )
    f_suite = request.GET.get("suite", "")
    f_status = request.GET.get("status", "")
    f_source = request.GET.get("source", "")
    f_result = request.GET.get("result", "")
    f_date = request.GET.get("date", "")

    if f_suite:
        # 비-UUID 값이 오면 ORM 이 ValueError 를 던져 500 이 되므로 사전 검증.
        try:
            uuid.UUID(str(f_suite))
        except (ValueError, TypeError):
            f_suite = ""
        else:
            qs = qs.filter(suite_id=f_suite)
    # date 필터 (대시보드 일별 막대 드릴다운) — started_at 의 로컬(Asia/Seoul) 날짜 기준.
    # 잘못된 형식은 조용히 무시. __date 는 USE_TZ 하에서 settings.TIME_ZONE 로 변환 후 비교.
    if f_date:
        from django.utils.dateparse import parse_date
        parsed = parse_date(f_date)
        if parsed is None:
            f_date = ""
        else:
            qs = qs.filter(started_at__date=parsed)
    # result 필터: whitelist 외 값은 빈 문자열로 초기화.
    # result 는 completed 전제 — completed 외의 status 와 동시 지정 시 항상
    # 공집합이 되므로 status 필터를 무시(f_status 도 비워 UI 선택 상태와 일치).
    if f_result not in ("pass", "fail", "skip"):
        f_result = ""
    if f_result and f_status and f_status != TestSuiteRun.Status.COMPLETED:
        f_status = ""

    if f_status:
        qs = qs.filter(status=f_status)
    if f_source == "manual":
        qs = qs.filter(triggered_by__isnull=False)
    elif f_source == "scheduled":
        qs = qs.filter(triggered_by__isnull=True)

    if f_result == "pass":
        qs = qs.filter(status=TestSuiteRun.Status.COMPLETED, failed_jobs=0)
    elif f_result == "fail":
        # Skip 분류·ABORTED(중단) 는 제외 (real_failed = Skip/중단 아닌 실패 Job 수)
        qs = qs.filter(status=TestSuiteRun.Status.COMPLETED, real_failed__gt=0)
    elif f_result == "skip":
        # 전부-중단(aborted) run 이 skip 으로 새지 않게 Skip 분류 존재를 명시 조건으로.
        qs = qs.filter(
            status=TestSuiteRun.Status.COMPLETED,
            _skipped_failed__gt=0, real_failed=0,
        )

    runs = list(qs[:100])
    suites = TestSuite.objects.order_by("name")
    return render(request, "suites/run_list.html", {
        "runs": runs,
        "suites": suites,
        "f_suite": f_suite,
        "f_status": f_status,
        "f_source": f_source,
        "f_result": f_result,
        "f_date": f_date,
        "status_choices": TestSuiteRun.Status.choices,
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


def _parse_suite_filter(values):
    """suite 필터 값 목록 → 유효한 UUID 문자열 리스트 (형식 오류는 조용히 버림 —
    run_list 의 suite 필터와 동일한 관용 정책, 필터는 조회 조건일 뿐이라 400 불필요)."""
    ids = []
    for v in values:
        try:
            ids.append(str(uuid.UUID(str(v))))
        except (ValueError, TypeError):
            continue
    return ids


def _untriaged_redirect(request):
    """미판정 큐로 복귀 — POST 로 넘어온 suite 필터(filter_suite)를 보존한 redirect."""
    from urllib.parse import urlencode

    from django.urls import reverse

    ids = _parse_suite_filter(request.POST.getlist("filter_suite"))
    url = reverse("suites:untriaged_list")
    if ids:
        url += "?" + urlencode([("suite", i) for i in ids])
    return redirect(url)


@login_required
def untriaged_list(request):
    """미 장애 판정(triage) 대기 큐 — 컷오프 이후 미분류(verdict="") 실패 Job 판정 화면.

    각 행에서 Failure(실장애) / Skip(자동화 판정 오류 의심) 로 확정하면 그 Job 은
    큐에서 사라진다(verdict 채워짐). 컷오프·정렬은 ``TestSuiteJobRun.untriaged_pending``.

    **Suite 필터 (복수 선택, 즉시 적용)**: ``?suite=<uuid>&suite=<uuid>`` — 대기
    건이 있는 Suite 만 건수와 함께 체크박스 칩으로 노출하고, 칩을 누르면 별도
    적용 버튼 없이 바로 그 Suite 들의 대기분만 표시된다(onchange submit). 판정
    (단건/일괄) 후에도 필터가 유지된다(``filter_suite`` hidden →
    ``_untriaged_redirect``).
    """
    from django.db.models import Count

    selected_ids = _parse_suite_filter(request.GET.getlist("suite"))

    qs = TestSuiteJobRun.untriaged_pending()
    if selected_ids:
        qs = qs.filter(suite_run__suite_id__in=selected_ids)
    total = qs.count()
    job_runs = list(
        qs.select_related("job", "suite_run", "suite_run__suite", "suite_run__triggered_by")[:200]
    )

    # 필터 칩 — 대기 건이 있는 Suite 만, 이름순 + 건수. order_by 를 명시해 기본
    # 정렬(_evt)이 GROUP BY 에 끼어들어 행이 쪼개지는 것을 방지.
    suite_counts = [
        {"id": str(row["suite_run__suite_id"]), "name": row["suite_run__suite__name"], "n": row["n"]}
        for row in (
            TestSuiteJobRun.untriaged_pending()
            .values("suite_run__suite_id", "suite_run__suite__name")
            .annotate(n=Count("pk"))
            .order_by("suite_run__suite__name")
        )
    ]

    return render(request, "suites/untriaged_list.html", {
        "job_runs": job_runs,
        "total_count": total,
        "shown_count": len(job_runs),
        "truncated": total > len(job_runs),
        "suite_counts": suite_counts,
        "selected_suite_ids": selected_ids,
        "is_filtered": bool(selected_ids),
    })


@login_required
def job_run_triage(request, pk):
    """포털에서 실패 Job 의 verdict(Failure/Skip)를 확정한다 — Slack 버튼과 동일 코어.

    권한 = ``can_write``(Viewer 불가). 확정 후 Slack 원본 메시지 갱신·쓰레드 멘션은
    ``apply_verdict`` 가 담당(해당 run 에 Slack 메시지가 있을 때만). 판정된 Job 은
    ``untriaged_pending`` 조건에서 빠져 큐에서 자동 제거된다.
    """
    job_run = get_object_or_404(
        TestSuiteJobRun.objects.select_related("job", "suite_run", "suite_run__suite"),
        pk=pk,
    )
    back = "suites:run_detail" if request.POST.get("next") == "run_detail" else "suites:untriaged_list"
    back_kwargs = {"pk": job_run.suite_run.pk} if back == "suites:run_detail" else {}

    def _back():
        # 미판정 큐로 돌아갈 땐 suite 필터(filter_suite) 유지.
        if back == "suites:run_detail":
            return redirect(back, **back_kwargs)
        return _untriaged_redirect(request)

    if request.method != "POST":
        return _back()
    if not request.user.can_write:
        messages.error(request, "판정 권한이 없습니다 (Viewer).")
        return _back()

    verdict = request.POST.get("verdict", "")
    if verdict not in (TestSuiteJobRun.Verdict.FAILURE, TestSuiteJobRun.Verdict.SKIP):
        messages.error(request, "잘못된 판정 값입니다.")
        return _back()

    from apps.notifications.triage import apply_verdict
    apply_verdict(job_run, verdict, request.user)

    job_name = job_run.job.job_name if job_run.job else "Job"
    ActivityLog.log(
        request.user, "job_triaged", "job_run", target=job_run,
        target_name=job_name, detail=f"verdict={verdict}",
    )
    label = "장애(Failure)" if verdict == TestSuiteJobRun.Verdict.FAILURE else "Skip"
    messages.success(request, f"{job_name} → {label} 로 판정했습니다.")
    return _back()


@login_required
def untriaged_bulk_triage(request):
    """미 장애 판정 큐 — 체크박스로 선택한 실패 Job 들을 일괄 장애/Skip 확정.

    권한 = ``can_write``. 대상은 **status=FAILURE 인 Job Run 만** — 큐 표시
    기준과 동일(ABORTED 는 판정 대상 아님, SUCCESS 등 오염 id 는 무시가 아니라
    필터로 걸러짐). ``verdict=""`` 조건은 걸지 않는다 — 제출 사이에 다른
    관리자가 먼저 판정한 건이 섞여 있어도 **재분류(덮어쓰기) 허용** 정책(단건
    ``job_run_triage``/Slack 버튼과 동일)에 따라 다시 확정하며, 값이 같으면
    followup 이 나가지 않아 무해하다. verdict/id 형식이 비정상이면 400 으로
    차단. Slack 원본 메시지 갱신·쓰레드 멘션은 ``apply_verdicts_bulk`` 가
    비동기로 처리한다.
    """
    if request.method != "POST":
        return _untriaged_redirect(request)
    if not request.user.can_write:
        messages.error(request, "판정 권한이 없습니다 (Viewer).")
        return _untriaged_redirect(request)

    verdict = request.POST.get("verdict", "")
    if verdict not in (TestSuiteJobRun.Verdict.FAILURE, TestSuiteJobRun.Verdict.SKIP):
        return HttpResponseBadRequest("invalid verdict")

    raw_ids = request.POST.getlist("job_run_ids")
    if len(raw_ids) > 300:  # 큐는 최신 200건만 노출 — 그 이상은 비정상 요청
        return HttpResponseBadRequest("too many job_run_ids")
    ids = []
    for v in raw_ids:
        try:
            ids.append(uuid.UUID(str(v)))
        except (ValueError, TypeError):
            return HttpResponseBadRequest("invalid job_run id")
    if not ids:
        messages.error(request, "선택된 Job 이 없습니다.")
        return _untriaged_redirect(request)

    job_runs = list(
        TestSuiteJobRun.objects.filter(
            pk__in=ids, status=TestSuiteJobRun.Status.FAILURE,
        ).select_related("job", "suite_run", "suite_run__suite")
    )
    if not job_runs:
        messages.error(request, "처리 가능한 실패 Job 이 없습니다 (이미 삭제/변경됨).")
        return _untriaged_redirect(request)

    from apps.notifications.triage import apply_verdicts_bulk
    apply_verdicts_bulk(job_runs, verdict, request.user)

    names = ", ".join(jr.job.job_name for jr in job_runs[:10] if jr.job)
    if len(job_runs) > 10:
        names += f" 외 {len(job_runs) - 10}건"
    ActivityLog.log(
        request.user, "job_triaged", "job_run",
        target_name=f"일괄 판정 {len(job_runs)}건",
        detail=f"verdict={verdict} / {names}",
    )
    label = "장애(Failure)" if verdict == TestSuiteJobRun.Verdict.FAILURE else "Skip"
    messages.success(request, f"{len(job_runs)}건을 {label} 로 일괄 판정했습니다.")
    return _untriaged_redirect(request)


@login_required
def job_run_list(request):
    """대시보드 결과 분포 도넛 드릴다운 — Job(worker) 단위 결과 목록 (읽기 전용).

    도넛과 동일하게 **최근 14일 완료 Suite Run 의 Job Run** 만, 최신순 200건.
    ``result`` 필터 (도넛과 동일 기준 — 미분류 ABORTED 중단은 장애가 아니라 제외,
    사람이 verdict 확정한 ABORTED 는 그 판정을 따름):
      - success : status=success (Pass)
      - failure : status=failure & verdict≠skip (실장애 — 미분류 포함)
                  + status=aborted & verdict=failure (명시 확정)
      - skip    : status=failure/aborted & verdict=skip
    optional ``suite`` 필터. 판정은 여기서 안 함(판정 큐/Run 상세에서).
    """
    from datetime import timedelta
    from django.db.models.functions import Coalesce

    f_result = request.GET.get("result", "")
    if f_result not in ("success", "failure", "skip"):
        f_result = ""
    f_suite = request.GET.get("suite", "")

    window_ago = timezone.now() - timedelta(days=14)
    qs = (TestSuiteJobRun.objects
          .filter(suite_run__status=TestSuiteRun.Status.COMPLETED,
                  suite_run__started_at__gte=window_ago)
          .select_related("job", "suite_run", "suite_run__suite")
          .annotate(_evt=Coalesce("completed_at", "started_at", "suite_run__started_at"))
          .order_by("-_evt"))

    from django.db.models import Q
    if f_result == "success":
        qs = qs.filter(status=TestSuiteJobRun.Status.SUCCESS)
    elif f_result == "failure":
        qs = qs.filter(
            (Q(status=TestSuiteJobRun.Status.FAILURE) & ~Q(verdict="skip"))
            | Q(status=TestSuiteJobRun.Status.ABORTED, verdict="failure")
        )
    elif f_result == "skip":
        qs = qs.filter(
            status__in=[TestSuiteJobRun.Status.FAILURE, TestSuiteJobRun.Status.ABORTED],
            verdict="skip",
        )

    if f_suite:
        try:
            uuid.UUID(str(f_suite))
        except (ValueError, TypeError):
            f_suite = ""
        else:
            qs = qs.filter(suite_run__suite_id=f_suite)

    job_runs = list(qs[:200])
    suites = TestSuite.objects.order_by("name")
    return render(request, "suites/job_run_list.html", {
        "job_runs": job_runs,
        "suites": suites,
        "f_result": f_result,
        "f_suite": f_suite,
        "shown_count": len(job_runs),
    })


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
    from apps.integrations.forms import ApiEndpointOwnerForm
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
        # "+ 등록" 모달용 폼 (admin 전용). repo/category/area 드롭다운.
        "owner_form": ApiEndpointOwnerForm() if request.user.is_admin else None,
        "notify_source_type": "suite_job",
        "notify_source_id": str(job_run.pk),
        "notify_stage": "",
    })


def _abort_suite_run(suite_run):
    """진행 중인 SuiteRun 을 강제 중단한다.
    - QUEUED/RUNNING job 은 Jenkins stop 호출 후 ABORTED 로 마킹
      (중단 시점까지의 콘솔 로그를 best-effort 수집해 사유와 함께 저장)
    - PENDING job 은 트리거 없이 ABORTED 로 마킹 (사유만 기록)
    - SuiteRun 은 COMPLETED 로 마무리하여 더 이상 폴링/트리거되지 않도록 한다.
    """
    if suite_run.status == TestSuiteRun.Status.COMPLETED:
        return False

    client = suite_run.suite.get_jenkins_client()
    aborted_any = False
    reason = (
        f"[aborted] 사용자/스케줄 중지 요청으로 "
        f"{timezone.localtime(timezone.now()):%Y-%m-%d %H:%M:%S} 에 강제 중단되었습니다."
    )

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
            # 중단 사유 + 중단 시점까지의 콘솔을 남긴다 — 무증거 중단 방지.
            console = ""
            if jr.jenkins_build_number:
                try:
                    console = client.get_console_output(
                        jr.job.job_full_path, jr.jenkins_build_number,
                    ) or ""
                except Exception:
                    logger.exception(
                        "abort: console fetch failed for %s #%s",
                        jr.job.job_full_path, jr.jenkins_build_number,
                    )
            marker = f"{reason} (중단 전 상태: {jr.status}, build #{jr.jenkins_build_number or '-'})"
            jr.console_output = f"{marker}\n\n{console}" if console else marker
            jr.status = TestSuiteJobRun.Status.ABORTED
            jr.completed_at = timezone.now()
            jr.save(update_fields=["status", "completed_at", "console_output"])
            aborted_any = True
        elif jr.status in (
            TestSuiteJobRun.Status.PENDING,
            TestSuiteJobRun.Status.WAITING,
        ):
            # 아직 Jenkins 로 트리거되지 않은 잡(PENDING/지연 보류 WAITING) → 바로 ABORTED.
            jr.console_output = (
                f"{reason} (중단 전 상태: {jr.status} — Jenkins 트리거 전, 빌드/콘솔 없음)"
            )
            jr.status = TestSuiteJobRun.Status.ABORTED
            jr.completed_at = timezone.now()
            jr.save(update_fields=["status", "completed_at", "console_output"])
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
        # PENDING + WAITING(지연 대기) 모두 '아직 트리거 안 된 잡' → Pending 배지에 합산.
        pending = [jr for jr in job_runs if jr.status in (
            TestSuiteJobRun.Status.PENDING,
            TestSuiteJobRun.Status.WAITING,
        )]
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

def pick_idle_job(suite, jobs):
    """worker 풀에서 '실행 중이지 않은(idle)' Job 을 앞 순서부터 1개 골라 반환.

    - ``jobs``: 활성 대상 Job 목록 (호출자가 order 정렬해서 넘긴다).
    - **검증된 idle 만 선택**한다:
      · Jenkins ``lastBuild`` 조회 성공 + ``building`` 아님 → idle → 반환.
      · 빌드 이력 없음(404) → 한 번도 안 돈 worker = 사용 가능 → idle → 반환.
      · ``building=True`` → busy → 건너뜀.
      · 그 외 조회 실패(연결/인증/5xx 등 = **상태 확인 불가**) → idle 로 간주하지
        않고 건너뜀. "검증되지 않은 worker 는 실행하지 않는다."
    - 검증된 idle 가 하나도 없으면 ``None`` — 호출자가 409 로 막고 Run 을 만들지
      않는다 (전부 busy 거나 Jenkins 상태를 확인할 수 없는 비정상 상황).

    Jenkins ``lastBuild`` 조회는 요청당 최대 8초 블로킹이라 worker 수만큼 직렬로
    돌면 단일 스레드 runserver 가 8s×N 동안 멈춘다. 따라서 **병렬로 조회**하고
    (벽시계 ≈ 1회 왕복), 그 결과에서 order 가 가장 앞선 idle worker 를 고른다.
    """
    if not jobs:
        return None
    client = suite.get_jenkins_client()

    def _is_idle(job):
        """True=idle, False=busy, None=상태 확인 불가(검증 안 됨)."""
        try:
            last_build = client.get_last_build_info(job.job_full_path)
        except JenkinsError as e:
            if getattr(e, "status", None) == 404:
                return True  # 빌드 이력 없음 = 한 번도 안 돈 worker → 사용 가능.
            return None  # 연결/인증/5xx 등 → 검증 안 됨.
        return not last_build.get("building")

    with ThreadPoolExecutor(max_workers=min(len(jobs), 8)) as ex:
        idle_flags = list(ex.map(_is_idle, jobs))

    # order 정렬 입력을 그대로 유지하므로 앞 순서의 idle worker 가 우선 선택된다.
    for job, idle in zip(jobs, idle_flags):
        if idle:
            return job
    return None


def _try_trigger_next(suite_run):
    """
    PENDING/WAITING 상태인 Job 을 순서(job__order)대로 순회하면서:
    1. Jenkins에서 이미 돌고 있으면 → 그 빌드를 추적 (새로 트리거 안 함)
    2. 안 돌고 있으면, 트리거할 '다음' 잡 1개에 대해 **실행 지연 throttle 게이트** 적용:
       - 전역 마지막 트리거 후 그 잡의 ``trigger_delay_seconds`` 가 지났으면 → 트리거(QUEUED)
       - 아직이면 → WAITING 으로 보류 (다음 폴링/sweep 에서 재시도)
    한 번에 1개만 새로 트리거하며, 순서를 건너뛰지 않는다(딜레이로 보류돼도 뒤 잡을
    먼저 트리거하지 않음).
    """
    client = suite_run.suite.get_jenkins_client()
    candidates = suite_run.job_runs.filter(
        status__in=[
            TestSuiteJobRun.Status.PENDING,
            TestSuiteJobRun.Status.WAITING,
        ],
    ).select_related("job").order_by("job__order")

    triggered_new = False
    decided_next = False  # 이번 호출에서 '다음 트리거 슬롯' 을 이미 처리(트리거 또는 보류)했는지

    for job_run in candidates:
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
            continue  # 다른 잡도 추적할 수 있게 계속

        if decided_next:
            continue  # 이번 사이클의 '다음' 슬롯은 이미 처리 — 순서 건너뛰기 방지

        # 비-building 인 첫 후보 = 트리거할 '다음' 잡.
        decided_next = True

        # 실행 지연 throttle 게이트(suite 단위): 이 suite 의 마지막 트리거 후 딜레이가
        # 지나야 트리거. 안 지났으면 WAITING 으로 보류.
        if not claim_trigger_slot(suite_run.suite, job_run.job.trigger_delay_seconds):
            if job_run.status != TestSuiteJobRun.Status.WAITING:
                job_run.status = TestSuiteJobRun.Status.WAITING
                job_run.save(update_fields=["status"])
            logger.info(
                "%s: trigger deferred (delay %ss) → WAITING",
                job_run.job.job_name, job_run.job.trigger_delay_seconds,
            )
            continue

        # 안 돌고 있음 + 딜레이 통과 → 트리거
        try:
            result = client.trigger_job(job_run.job.job_full_path, gwt_token=job_run.job.gwt_token)
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

    return triggered_new


def _jenkins_build_end(build_info):
    """Jenkins build_info(timestamp=시작 epoch ms, duration=ms) → 실제 빌드 종료 시각.

    완료 마킹을 폴링(감지) 시각이 아니라 **실제 종료 시각**으로 찍기 위한 값 —
    폴링이 늦어도(예: 외부 폴러 중단으로 하루 뒤 감지) '소요 시간' 이 39초짜리
    빌드에 24시간+ 로 부풀지 않는다(2026-07-21 am-mapp-regression-api-dev
    seq 1284 사건). timestamp 가 없으면 None (호출부에서 now() 폴백).
    """
    import datetime as _dt

    ts = build_info.get("timestamp")
    if not ts:
        return None
    try:
        return _dt.datetime.fromtimestamp(
            (ts + (build_info.get("duration") or 0)) / 1000, tz=_dt.timezone.utc,
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


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
    retried = False  # 이 폴링에서 실패한 잡을 재시도용 PENDING 으로 되돌렸는지

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
            job_run.completed_at = _jenkins_build_end(build_info) or timezone.now()
            any_completed = True
            # 콘솔 로그 저장
            try:
                job_run.console_output = client.get_console_output(
                    job_run.job.job_full_path, build_number
                )
            except JenkinsError:
                logger.warning(f"Failed to fetch console for {job_run.job.job_name} #{build_number}")
        elif result == "FAILURE":
            job_run.duration_ms = build_info.get("duration")
            # 콘솔 로그 저장 (재시도 여부와 무관하게 마지막 실패본 보존)
            try:
                job_run.console_output = client.get_console_output(
                    job_run.job.job_full_path, build_number
                )
            except JenkinsError:
                logger.warning(f"Failed to fetch console for {job_run.job.job_name} #{build_number}")

            if job_run.attempt < suite_run.retry_limit:
                # 재시도: 이 잡을 PENDING 으로 되돌려 새 빌드를 트리거한다.
                # 실패로 집계하지 않고, 다음 시도가 성공하면 PASS 로 끝난다.
                ActivityLog.log(
                    user=suite_run.triggered_by,
                    action=ActivityLog.Action.RETRY,
                    target_type=ActivityLog.TargetType.SUITE_RUN,
                    target=suite_run,
                    target_name=f"{suite_run.suite.name} / {job_run.job.job_name}",
                    detail=f"auto retry {job_run.attempt + 1}/{suite_run.retry_limit} (build #{build_number} FAILURE)",
                )
                job_run.attempt += 1
                job_run.status = TestSuiteJobRun.Status.PENDING
                job_run.jenkins_build_number = None
                job_run.jenkins_build_url = ""  # 직전 실패 빌드 URL 이 남지 않게 초기화
                job_run.completed_at = None
                job_run.current_stage = ""
                job_run.stages_json = []
                retried = True
            else:
                job_run.status = TestSuiteJobRun.Status.FAILURE
                job_run.completed_at = _jenkins_build_end(build_info) or timezone.now()
                any_completed = True
        elif result == "ABORTED":
            job_run.status = TestSuiteJobRun.Status.ABORTED
            job_run.completed_at = _jenkins_build_end(build_info) or timezone.now()
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
        # 마지막 잡의 실제 종료 시각으로 마감 — 폴링이 늦어도 소요 시간이 안 부풀게.
        from django.db.models import Max
        last_end = suite_run.job_runs.aggregate(m=Max("completed_at"))["m"]
        suite_run.completed_at = last_end or timezone.now()
        suite_run.save()
        if failed > 0:
            send_suite_run_event_async(nev.SUITE_RUN_FAILED, suite_run)
        else:
            send_suite_run_event_async(nev.SUITE_RUN_PASSED, suite_run)
        return

    suite_run.save(update_fields=["passed_jobs", "failed_jobs"])

    # 3. Job이 완료됐거나(또는 실패 → 재시도용 PENDING 으로 되돌렸으면) 다음 PENDING Job 트리거
    if any_completed or retried:
        _try_trigger_next(suite_run)
    else:
        # 아무것도 QUEUED/RUNNING이 아닌데 PENDING이 있으면 트리거 시도
        has_active = suite_run.job_runs.filter(
            status__in=[TestSuiteJobRun.Status.QUEUED, TestSuiteJobRun.Status.RUNNING]
        ).exists()
        if not has_active:
            _try_trigger_next(suite_run)
