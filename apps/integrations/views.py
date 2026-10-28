"""API Endpoint Owner / Host Mapping 의 Admin CRUD + CSV import/export.

improvement.md §5 (Admin UI) / §7 (ActivityLog) 사양에 맞춰 구현.
URL 라우팅은 `apps/accounts/urls.py` 에서 `int_views.*` 로 import 해 등록.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.activitylog.models import ActivityLog

from .forms import (
    ApiEndpointOwnerForm,
    ApiHostMappingForm,
    FallbackOwnersForm,
    UiAutomationOwnersForm,
)
from .google_sheets import (
    GoogleSheetSyncError,
    get_service_account_email,
    get_spreadsheet_id,
)
from .models import (
    ApiEndpointOwner,
    ApiHostMapping,
    FallbackOwners,
    OwnerSyncSettings,
    UiAutomationOwners,
)
from .services import sync_owners_from_google_sheet


# 한국어 CSV 헤더 (services.HEADER_MAP 의 키와 일치). 담당팀 컬럼은 2026-05-20 제거.
CSV_EXPORT_HEADERS = [
    "레포",
    "카테고리",
    "영역",
    "Method",
    "Endpoint",
    "FE 담당자",
    "BE 담당자",
    "매핑 앱",
    "사용 횟수",
    "step_ids",
]


def _require_admin(user):
    """apps.accounts.views._require_admin 과 동일 (의존 최소화 위해 재정의)."""
    if not user.is_authenticated or not user.is_admin:
        raise PermissionDenied("관리자만 접근할 수 있습니다.")


# =========================================================
# ApiEndpointOwner — list / create / edit / delete
# =========================================================


@login_required
def api_owner_list(request):
    """담당자 관리 화면 — 내부 탭 2개 (`?tab=` 로 전환).

    - `api` (기본): API Endpoint 담당자 매핑 + 자동 동기화 주기 + 대체 담당자
    - `ui`: UI Test Automation(TAP) 실패 Slack 알림 담당자 (구분 없이 통 설정)
    """
    _require_admin(request.user)

    if request.GET.get("tab") == "ui":
        return render(
            request,
            "accounts/api_owner_list.html",
            {
                "admin_active_tab": "api_owners",
                "owner_tab": "ui",
                "ui_owners_text": UiAutomationOwners.get_solo().owners,
            },
        )

    qs = ApiEndpointOwner.objects.all()

    q = (request.GET.get("q") or "").strip()
    repo = (request.GET.get("repo") or "").strip()
    mapped_app = (request.GET.get("mapped_app") or "").strip()
    is_imported = (request.GET.get("is_imported") or "").strip()  # 'yes'/'no'/''

    if q:
        qs = qs.filter(
            Q(repo__icontains=q)
            | Q(category__icontains=q)
            | Q(endpoint__icontains=q)
            | Q(area__icontains=q)
            | Q(fe_owner__icontains=q)
            | Q(be_owner__icontains=q)
        )
    if repo:
        qs = qs.filter(repo=repo)
    if mapped_app:
        qs = qs.filter(mapped_app=mapped_app)
    if is_imported == "yes":
        qs = qs.filter(is_imported=True)
    elif is_imported == "no":
        qs = qs.filter(is_imported=False)

    total_count = qs.count()

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get("page"))

    repos = list(
        ApiEndpointOwner.objects.exclude(repo="")
        .values_list("repo", flat=True)
        .distinct()
        .order_by("repo")
    )
    mapped_apps = list(
        ApiEndpointOwner.objects.exclude(mapped_app="")
        .values_list("mapped_app", flat=True)
        .distinct()
        .order_by("mapped_app")
    )

    # 마지막 Google Sheets 동기화 (수동/cron 공통 — ActivityLog 에 기록됨)
    last_sync = (
        ActivityLog.objects.filter(action=ActivityLog.Action.API_OWNER_IMPORTED)
        .order_by("-created_at")
        .first()
    )
    try:
        sheet_id = get_spreadsheet_id()
        sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    except GoogleSheetSyncError:
        sheet_url = ""
    sa_email = get_service_account_email()

    sync_settings = OwnerSyncSettings.get_solo()
    fallback = FallbackOwners.get_solo()
    return render(
        request,
        "accounts/api_owner_list.html",
        {
            "paginator_page": page,
            "repos": repos,
            "mapped_apps": mapped_apps,
            "total_count": total_count,
            "admin_active_tab": "api_owners",
            "owner_tab": "api",
            # echo back filter values
            "filter_q": q,
            "filter_repo": repo,
            "filter_mapped_app": mapped_app,
            "filter_is_imported": is_imported,
            # Google Sheets 동기화 정보
            "last_sync": last_sync,
            "sheet_url": sheet_url,
            "sa_email": sa_email,
            "sync_interval_minutes": sync_settings.interval_minutes,
            # 대체 담당자 (singleton)
            "fallback_form": FallbackOwnersForm(instance=fallback),
            "fallback_owners_text": fallback.owners,
        },
    )


@login_required
def api_owner_create(request):
    _require_admin(request.user)

    # Error Log 화면 모달에서 fetch 로 호출되면 JSON 으로 응답 (페이지 이동 없이 등록).
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    prefill_host = ""
    if request.method == "POST":
        form = ApiEndpointOwnerForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.is_imported = False  # 수동 row 는 import 가 덮어쓰지 않음
            obj.save()
            from .url_matcher import invalidate_match_cache
            invalidate_match_cache()
            # Google Sheet 역방향 기록 (비동기·best-effort, opt-in). 등록 흐름엔 영향 없음.
            from .services import writeback_owner_to_sheet_async
            writeback_owner_to_sheet_async(obj)
            ActivityLog.log(
                user=request.user,
                action=ActivityLog.Action.API_OWNER_CREATED,
                target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
                target=obj,
                target_name=f"{obj.method} {obj.endpoint}",
                detail=f"repo={obj.repo}",
            )
            if is_ajax:
                return JsonResponse(
                    {"ok": True, "id": str(obj.id), "label": f"{obj.method} {obj.endpoint}"}
                )
            messages.success(request, f"담당자 매핑을 추가했습니다: {obj.method} {obj.endpoint}")
            return redirect("accounts:api_owner_list")
        if is_ajax:
            return JsonResponse(
                {"ok": False, "errors": form.errors.get_json_data()}, status=400
            )
    else:
        initial = {}
        # Error Log 의 "+ 등록" 동선에서 prefill — improvement.md §6.3
        prefill_method = (request.GET.get("method") or "").strip().upper()
        prefill_endpoint = (request.GET.get("endpoint") or "").strip()
        prefill_host = (request.GET.get("host") or "").strip()
        if prefill_method:
            initial["method"] = prefill_method
        if prefill_endpoint:
            initial["endpoint"] = prefill_endpoint
        # host 는 ApiEndpointOwner 모델에 직접 필드가 없어 notes 에 단서로 prefill.
        if prefill_host:
            initial["notes"] = f"host={prefill_host}"
        form = ApiEndpointOwnerForm(initial=initial)

    return render(
        request,
        "accounts/api_owner_form.html",
        {
            "form": form,
            "owner": None,
            "admin_active_tab": "api_owners",
            "prefill_host": prefill_host,
        },
    )


@login_required
def api_owner_edit(request, pk):
    _require_admin(request.user)
    obj = get_object_or_404(ApiEndpointOwner, pk=pk)

    if request.method == "POST":
        form = ApiEndpointOwnerForm(request.POST, instance=obj)
        if form.is_valid():
            saved = form.save()
            # signal 등록이 누락된 환경(multi-worker 등) 대비 명시 무효화
            from .url_matcher import invalidate_match_cache
            invalidate_match_cache()
            ActivityLog.log(
                user=request.user,
                action=ActivityLog.Action.API_OWNER_UPDATED,
                target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
                target=saved,
                target_name=f"{saved.method} {saved.endpoint}",
                detail=f"repo={saved.repo}",
            )
            messages.success(request, f"수정했습니다: {saved.method} {saved.endpoint}")
            return redirect("accounts:api_owner_list")
    else:
        form = ApiEndpointOwnerForm(instance=obj)

    return render(
        request,
        "accounts/api_owner_form.html",
        {
            "form": form,
            "owner": obj,
            "admin_active_tab": "api_owners",
        },
    )


@login_required
@require_POST
def api_owner_delete(request, pk):
    _require_admin(request.user)
    obj = get_object_or_404(ApiEndpointOwner, pk=pk)
    label = f"{obj.method} {obj.endpoint}"
    repo = obj.repo
    obj.delete()
    from .url_matcher import invalidate_match_cache
    invalidate_match_cache()
    ActivityLog.log(
        user=request.user,
        action=ActivityLog.Action.API_OWNER_DELETED,
        target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
        target_name=label,
        detail=f"repo={repo}",
    )
    messages.info(request, f"삭제했습니다: {label}")
    return redirect("accounts:api_owner_list")


@login_required
@require_POST
def api_owner_sync(request):
    """Google Sheets 의 담당자 마스터를 즉시 동기화 (수동 트리거).

    cron(`manage.py sync_api_owners_from_sheet`) 과 동일한 서비스를 호출하는
    on-demand 버튼. 항상 full mirror (시트 기준 삭제, 수동 row 보존).
    """
    _require_admin(request.user)

    try:
        result = sync_owners_from_google_sheet(user=request.user)
    except GoogleSheetSyncError as e:
        messages.error(request, f"Google Sheets 동기화 실패: {e}")
        return redirect("accounts:api_owner_list")

    summary = (
        f"created={result.created} updated={result.updated} "
        f"skipped_manual={result.skipped_manual} deleted_stale={result.deleted_stale}"
    )
    if result.errors:
        messages.warning(
            request, f"동기화 완료 (errors {len(result.errors)}건). {summary}"
        )
    else:
        messages.success(request, f"Google Sheets 동기화 완료. {summary}")

    return redirect("accounts:api_owner_list")


@login_required
def api_owner_export(request):
    """현재 필터된 queryset 을 CSV 로 stream. improvement.md §5.3 + §8.3."""
    _require_admin(request.user)

    qs = ApiEndpointOwner.objects.all()

    q = (request.GET.get("q") or "").strip()
    repo = (request.GET.get("repo") or "").strip()
    mapped_app = (request.GET.get("mapped_app") or "").strip()
    is_imported = (request.GET.get("is_imported") or "").strip()

    if q:
        qs = qs.filter(
            Q(repo__icontains=q)
            | Q(category__icontains=q)
            | Q(endpoint__icontains=q)
            | Q(area__icontains=q)
            | Q(fe_owner__icontains=q)
            | Q(be_owner__icontains=q)
        )
    if repo:
        qs = qs.filter(repo=repo)
    if mapped_app:
        qs = qs.filter(mapped_app=mapped_app)
    if is_imported == "yes":
        qs = qs.filter(is_imported=True)
    elif is_imported == "no":
        qs = qs.filter(is_imported=False)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # text/csv 로 보내되 Excel 한국어 깨짐 방지를 위해 BOM 한 번만 prepend.
    # bytes 로 만들어서 HttpResponse 가 추가 인코딩하지 않도록 한다.
    import io as _io
    sio = _io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(CSV_EXPORT_HEADERS)
    for obj in qs.iterator():
        writer.writerow([
            obj.repo,
            obj.category,
            obj.area,
            obj.method,
            obj.endpoint,
            obj.fe_owner,
            obj.be_owner,
            obj.mapped_app,
            obj.usage_count,
            obj.step_ids,
        ])
    body_bytes = b"\xef\xbb\xbf" + sio.getvalue().encode("utf-8")

    response = HttpResponse(body_bytes, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="api_endpoint_owners_{ts}.csv"'
    )
    return response


# =========================================================
# ApiHostMapping — list / create / edit / delete
# =========================================================


@login_required
def api_host_list(request):
    _require_admin(request.user)

    qs = ApiHostMapping.objects.all()

    q = (request.GET.get("q") or "").strip()
    env = (request.GET.get("env") or "").strip()

    if q:
        qs = qs.filter(
            Q(variable_name__icontains=q)
            | Q(host_pattern__icontains=q)
            | Q(notes__icontains=q)
        )
    if env and env in dict(ApiHostMapping.Env.choices):
        qs = qs.filter(env=env)

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "accounts/api_host_list.html",
        {
            "paginator_page": page,
            "envs": ApiHostMapping.Env.choices,
            "admin_active_tab": "api_hosts",
            "filter_q": q,
            "filter_env": env,
            "total_count": qs.count(),
        },
    )


@login_required
def api_host_create(request):
    _require_admin(request.user)

    if request.method == "POST":
        form = ApiHostMappingForm(request.POST)
        if form.is_valid():
            obj = form.save()
            ActivityLog.log(
                user=request.user,
                action=ActivityLog.Action.API_HOST_CREATED,
                target_type=ActivityLog.TargetType.API_HOST_MAPPING,
                target=obj,
                target_name=f"${{{obj.variable_name}}} [{obj.env}] → {obj.host_pattern}",
            )
            messages.success(request, "호스트 매핑을 추가했습니다.")
            return redirect("accounts:api_host_list")
    else:
        initial = {}
        prefill_var = (request.GET.get("variable_name") or "").strip()
        prefill_host = (request.GET.get("host_pattern") or "").strip()
        if prefill_var:
            initial["variable_name"] = prefill_var
        if prefill_host:
            initial["host_pattern"] = prefill_host
        form = ApiHostMappingForm(initial=initial)

    return render(
        request,
        "accounts/api_host_form.html",
        {
            "form": form,
            "mapping": None,
            "admin_active_tab": "api_hosts",
        },
    )


@login_required
def api_host_edit(request, pk):
    _require_admin(request.user)
    obj = get_object_or_404(ApiHostMapping, pk=pk)

    if request.method == "POST":
        form = ApiHostMappingForm(request.POST, instance=obj)
        if form.is_valid():
            saved = form.save()
            ActivityLog.log(
                user=request.user,
                action=ActivityLog.Action.API_HOST_UPDATED,
                target_type=ActivityLog.TargetType.API_HOST_MAPPING,
                target=saved,
                target_name=f"${{{saved.variable_name}}} [{saved.env}] → {saved.host_pattern}",
            )
            messages.success(request, "호스트 매핑을 수정했습니다.")
            return redirect("accounts:api_host_list")
    else:
        form = ApiHostMappingForm(instance=obj)

    return render(
        request,
        "accounts/api_host_form.html",
        {
            "form": form,
            "mapping": obj,
            "admin_active_tab": "api_hosts",
        },
    )


@login_required
@require_POST
def api_host_delete(request, pk):
    _require_admin(request.user)
    obj = get_object_or_404(ApiHostMapping, pk=pk)
    label = f"${{{obj.variable_name}}} [{obj.env}] → {obj.host_pattern}"
    obj.delete()
    ActivityLog.log(
        user=request.user,
        action=ActivityLog.Action.API_HOST_DELETED,
        target_type=ActivityLog.TargetType.API_HOST_MAPPING,
        target_name=label,
    )
    messages.info(request, f"삭제했습니다: {label}")
    return redirect("accounts:api_host_list")


# =========================================================
# FallbackOwners — singleton 저장 + 매칭 실패 URL 용 노티
# =========================================================


@login_required
@require_POST
def fallback_owners_save(request):
    """담당자 관리 화면 하단의 대체 담당자 입력 폼 저장."""
    _require_admin(request.user)
    instance = FallbackOwners.get_solo()
    form = FallbackOwnersForm(request.POST, instance=instance)
    if form.is_valid():
        saved = form.save()
        ActivityLog.log(
            user=request.user,
            action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
            target=saved,
            target_name="대체 담당자",
            detail=f"owners={saved.owners}",
        )
        messages.success(
            request, f"대체 담당자를 저장했습니다: {saved.owners or '(미설정)'}"
        )
    else:
        messages.error(request, f"저장 실패: {form.errors}")
    return redirect("accounts:api_owner_list")


@login_required
@require_POST
def ui_owners_save(request):
    """담당자 관리 > UI 탭의 UI Test Automation(TAP) 실패 알림 담당자 저장."""
    _require_admin(request.user)
    instance = UiAutomationOwners.get_solo()
    form = UiAutomationOwnersForm(request.POST, instance=instance)
    if form.is_valid():
        saved = form.save()
        ActivityLog.log(
            user=request.user,
            action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
            target=saved,
            target_name="UI 알림 담당자",
            detail=f"owners={saved.owners}",
        )
        messages.success(
            request, f"UI 알림 담당자를 저장했습니다: {saved.owners or '(미설정)'}"
        )
    else:
        messages.error(request, f"저장 실패: {form.errors}")
    return redirect(f"{reverse('accounts:api_owner_list')}?tab=ui")


@login_required
@require_POST
def owner_sync_settings_save(request):
    """담당자 관리 화면 하단의 '자동 동기화 주기(분)' 저장. 0 = 자동 동기화 끔.

    저장값은 매분 도는 cron(`sync_api_owners_from_sheet --if-due`) 이 읽어 주기
    도래 시에만 동기화한다 — crontab 수정 없이 여기서만 주기를 바꾼다.
    """
    _require_admin(request.user)
    raw = (request.POST.get("interval_minutes") or "").strip()
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        messages.error(request, "주기는 숫자(분)로 입력하세요. 예: 10 (0 이면 자동 동기화 끔)")
        return redirect("accounts:api_owner_list")
    if minutes < 0:
        minutes = 0

    obj = OwnerSyncSettings.get_solo()
    obj.interval_minutes = minutes
    obj.save(update_fields=["interval_minutes", "updated_at"])
    ActivityLog.log(
        user=request.user,
        action=ActivityLog.Action.UPDATE,
        target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
        target=obj,
        target_name="담당자 자동 동기화 주기",
        detail=f"interval_minutes={minutes}",
    )
    if minutes == 0:
        messages.success(request, "자동 동기화를 껐습니다 (수동 동기화만 동작).")
    else:
        messages.success(request, f"자동 동기화 주기를 {minutes}분으로 설정했습니다.")
    return redirect("accounts:api_owner_list")


@login_required
@require_POST
def notify_fallback_owners(request):
    """Error Log 페이지의 매칭 실패 URL 에 대해 대체 담당자에게 Slack 노티 발송.

    POST body 는 `api_owner_notify_error` 와 동일 (url, method, status_code,
    source_type, source_id, stage) — 단 owner pk 가 없으므로 메시지는 "매칭된
    담당자 없음 — 대체 담당자로 지정" 라벨이 추가되고 멘션은 FallbackOwners 의
    이름들로 구성된다.
    """
    if not getattr(request.user, "can_write", False):
        raise PermissionDenied("쓰기 권한이 필요합니다.")

    from apps.notifications.models import SlackNotificationSettings
    from apps.notifications.owner_mentions import (
        build_error_followup_text,
        excerpt_error,
        mentions_from_names,
    )
    from apps.notifications import services as nsvc
    from .log_errors import extract_error_log

    url = (request.POST.get("url") or "").strip()
    method = (request.POST.get("method") or "").strip().upper()
    status_raw = (request.POST.get("status_code") or "").strip()
    try:
        status_code: int | None = int(status_raw) if status_raw else None
    except ValueError:
        status_code = None
    source_type = (request.POST.get("source_type") or "").strip()
    source_id = (request.POST.get("source_id") or "").strip()
    stage = (request.POST.get("stage") or "").strip()

    referer = request.META.get("HTTP_REFERER") or "/"

    # source fetch & console_output (api_owner_notify_error 와 동일 로직 — 추후
    # 공용 헬퍼로 추출 가능하나 일단 인라인 유지)
    console_output = ""
    context_label = ""
    portal_log_url = ""
    if source_type == "suite_job":
        from apps.suites.models import TestSuiteJobRun
        try:
            jr = TestSuiteJobRun.objects.select_related(
                "job", "suite_run", "suite_run__suite"
            ).get(pk=source_id)
        except (TestSuiteJobRun.DoesNotExist, ValueError):
            messages.error(request, "원본 Job Run 을 찾을 수 없습니다.")
            return redirect(referer)
        console_output = jr.console_output or ""
        suite_name = jr.suite_run.suite.name if jr.suite_run and jr.suite_run.suite else "?"
        job_name = jr.job.job_name if jr.job else "?"
        context_label = f"Suite: {suite_name} / Job: {job_name}"
        portal_log_url = request.build_absolute_uri(
            reverse("suites:job_run_error_log", kwargs={"pk": jr.pk})
        )
    elif source_type == "qg_run":
        from apps.qualitygates.models import QualityGateRun
        try:
            run = QualityGateRun.objects.select_related(
                "gate", "gate__folder", "gate__subfolder"
            ).get(pk=source_id)
        except (QualityGateRun.DoesNotExist, ValueError):
            messages.error(request, "원본 Quality Gate Run 을 찾을 수 없습니다.")
            return redirect(referer)
        if stage == "image":
            console_output = run.image_console_output or ""
            stage_label = "이미지 빌드"
            url_name = "qualitygates:image_error_log"
        elif stage == "deploy":
            console_output = run.deploy_console_output or ""
            stage_label = "검수대상 배포"
            url_name = "qualitygates:deploy_error_log"
        elif stage == "prod":
            console_output = run.prod_console_output or ""
            stage_label = "검수통과 배포"
            url_name = "qualitygates:prod_error_log"
        else:
            messages.error(request, "잘못된 stage 입니다.")
            return redirect(referer)
        gate_name = run.gate.name if run.gate else "?"
        context_label = f"Quality Gate: {gate_name} / {stage_label}"
        portal_log_url = request.build_absolute_uri(
            reverse(url_name, kwargs={"pk": run.pk})
        )
    else:
        messages.error(request, "잘못된 source_type 입니다.")
        return redirect(referer)

    error_log = extract_error_log(console_output)
    error_excerpt = excerpt_error(error_log or "", max_chars=1500)

    fallback = FallbackOwners.get_solo()
    names = fallback.names()
    if not names:
        messages.error(
            request,
            "대체 담당자가 설정되어 있지 않습니다. 담당자 관리 화면 하단에서 먼저 등록해주세요.",
        )
        return redirect(referer)

    mention_text, unresolved = mentions_from_names(names)
    # 매칭된 담당자가 없는 URL 임을 명시
    label = context_label + " — 담당자 매칭 없음, 대체 담당자로 지정"
    matches = [
        {
            "url": url,
            "method": method,
            "status_code": status_code,
            "owner": None,
        }
    ]
    text = build_error_followup_text(
        matches=matches,
        error_excerpt=error_excerpt,
        portal_log_url=portal_log_url,
        context_label=label,
        mention_text=mention_text,
    )

    settings_obj = SlackNotificationSettings.get_solo()
    channel = (settings_obj.default_channel or "").strip() or (
        os.getenv("SLACK_DEFAULT_CHANNEL") or ""
    ).strip()
    token = (os.getenv("SLACK_BOT_TOKEN") or "").strip()

    sent = False
    if not token:
        messages.error(request, "Slack 토큰(.env SLACK_BOT_TOKEN) 미설정 — 발송 실패.")
    elif not channel:
        messages.error(request, "Slack 채널 미설정 — 발송 실패.")
    else:
        sent = nsvc._post_message(channel, text)
        if sent:
            base_msg = f"Slack 채널 {channel} 으로 대체 담당자 노티를 발송했습니다."
            if unresolved:
                base_msg += (
                    f" (Slack workspace 자동 매칭 실패 — @이름 평문: {', '.join(unresolved)})"
                )
            messages.success(request, base_msg)
        else:
            messages.error(request, "Slack 발송 실패 (서버 로그 확인).")

    ActivityLog.log(
        user=request.user,
        action=ActivityLog.Action.OWNER_NOTIFIED,
        target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
        target_name="대체 담당자 노티",
        detail=(
            f"url={url} method={method} status={status_code} "
            f"source={source_type}:{source_id} fallback={fallback.owners} sent={sent}"
        )[:500],
    )
    return redirect(referer)


# =========================================================
# Slack 사용자 자동 매칭 — 디버그 페이지
# =========================================================


@login_required
def slack_users_debug(request):
    """`users.list` 결과 확인 + 이름 검색 + 캐시 갱신 — 매칭 실패 트러블슈팅."""
    _require_admin(request.user)
    from apps.integrations.slack_users import (
        find_user_id_by_name,
        get_last_error,
        get_user_index,
        invalidate_user_cache,
    )

    if request.method == "POST" and request.POST.get("refresh"):
        invalidate_user_cache()
        get_user_index(force_refresh=True)
        messages.info(request, "Slack users.list 캐시를 다시 채웠습니다.")
        return redirect("accounts:slack_users_debug")

    index = get_user_index()  # 캐시 hit 면 즉시
    last_error = get_last_error()

    q = (request.GET.get("q") or "").strip()
    lookup = None
    if q:
        uid = find_user_id_by_name(q)
        ql = q.lower()
        candidates = sorted(
            (k for k in index if ql in k),
            key=lambda s: (len(s), s),
        )[:15]
        lookup = {"query": q, "uid": uid, "candidates": candidates}

    sample = sorted(index.keys())[:200]

    return render(
        request,
        "accounts/slack_users_debug.html",
        {
            "total_count": len(index),
            "sample": sample,
            "sample_truncated": len(index) > 200,
            "last_error": last_error,
            "q": q,
            "lookup": lookup,
            "admin_active_tab": "slack_users",
        },
    )


# =========================================================
# 수동 발송 endpoint — Error Log 페이지의 "에러 노티" 버튼
# =========================================================


@login_required
@require_POST
def api_owner_notify_error(request, pk):
    """이 owner 의 담당자에게 Slack 으로 즉시 에러 노티를 보낸다.

    POST body:
        url, method, status_code (int), source_type (suite_job|qg_run),
        source_id (uuid), stage (image|deploy|prod, qg_run 일 때만)

    동작:
        1. owner / source 객체 fetch
        2. 해당 source 의 console_output → extract_error_log → 발췌
        3. portal 에러 로그 URL 빌드
        4. 멘션 + 메시지 본문 빌드 → Slack 발송
        5. ActivityLog 기록
    """
    if not getattr(request.user, "can_write", False):
        raise PermissionDenied("쓰기 권한이 필요합니다.")

    from apps.notifications.models import SlackNotificationSettings
    from apps.notifications.owner_mentions import (
        _names_for_status,
        build_error_followup_text,
        build_mentions,
        excerpt_error,
        mentions_from_names,
    )
    from apps.notifications import services as nsvc
    from .log_errors import extract_error_log

    owner = get_object_or_404(ApiEndpointOwner, pk=pk)

    url = (request.POST.get("url") or "").strip()
    method = (request.POST.get("method") or "").strip().upper()
    status_raw = (request.POST.get("status_code") or "").strip()
    try:
        status_code: int | None = int(status_raw) if status_raw else None
    except ValueError:
        status_code = None
    source_type = (request.POST.get("source_type") or "").strip()
    source_id = (request.POST.get("source_id") or "").strip()
    stage = (request.POST.get("stage") or "").strip()

    referer = request.META.get("HTTP_REFERER") or "/"

    # source fetch & console_output
    console_output = ""
    context_label = ""
    portal_log_url = ""
    if source_type == "suite_job":
        from apps.suites.models import TestSuiteJobRun
        try:
            jr = TestSuiteJobRun.objects.select_related(
                "job", "suite_run", "suite_run__suite"
            ).get(pk=source_id)
        except (TestSuiteJobRun.DoesNotExist, ValueError):
            messages.error(request, "원본 Job Run 을 찾을 수 없습니다.")
            return redirect(referer)
        console_output = jr.console_output or ""
        suite_name = jr.suite_run.suite.name if jr.suite_run and jr.suite_run.suite else "?"
        job_name = jr.job.job_name if jr.job else "?"
        context_label = f"Suite: {suite_name} / Job: {job_name}"
        portal_log_url = request.build_absolute_uri(
            reverse("suites:job_run_error_log", kwargs={"pk": jr.pk})
        )
    elif source_type == "qg_run":
        from apps.qualitygates.models import QualityGateRun
        try:
            run = QualityGateRun.objects.select_related(
                "gate", "gate__folder", "gate__subfolder"
            ).get(pk=source_id)
        except (QualityGateRun.DoesNotExist, ValueError):
            messages.error(request, "원본 Quality Gate Run 을 찾을 수 없습니다.")
            return redirect(referer)
        if stage == "image":
            console_output = run.image_console_output or ""
            stage_label = "이미지 빌드"
            url_name = "qualitygates:image_error_log"
        elif stage == "deploy":
            console_output = run.deploy_console_output or ""
            stage_label = "검수대상 배포"
            url_name = "qualitygates:deploy_error_log"
        elif stage == "prod":
            console_output = run.prod_console_output or ""
            stage_label = "검수통과 배포"
            url_name = "qualitygates:prod_error_log"
        else:
            messages.error(request, "잘못된 stage 입니다.")
            return redirect(referer)
        gate_name = run.gate.name if run.gate else "?"
        context_label = f"Quality Gate: {gate_name} / {stage_label}"
        portal_log_url = request.build_absolute_uri(
            reverse(url_name, kwargs={"pk": run.pk})
        )
    else:
        messages.error(request, "잘못된 source_type 입니다.")
        return redirect(referer)

    error_log = extract_error_log(console_output)
    error_excerpt = excerpt_error(error_log or "", max_chars=1500)

    # 매칭된 owner row 의 fe_owner / be_owner 가 status 별로 실제 값을 가지는지 확인.
    # 빈 string 이면 owner 객체는 있어도 사실상 "담당자 없음" 과 동일 — FallbackOwners
    # 로 대체 (자동 발송 분기와 동일 처리, 2026-05-20 사용자 보고).
    owner_names = _names_for_status(owner, status_code)
    used_fallback = False
    fallback_label_suffix = ""
    if owner_names:
        # 정상 경로
        mention_text, unresolved = build_mentions([(owner, status_code)])
        matches = [
            {
                "url": url,
                "method": method,
                "status_code": status_code,
                "owner": owner,
            }
        ]
    else:
        # owner 매칭은 됐지만 담당자 컬럼이 비어있음 → FallbackOwners 사용
        fallback = FallbackOwners.get_solo()
        fallback_names = fallback.names()
        if not fallback_names:
            messages.error(
                request,
                "이 URL 의 owner row 에 담당자 컬럼이 비어있고 대체 담당자도 설정되어 있지 않습니다. "
                "담당자 관리 화면 하단에서 대체 담당자를 먼저 등록해주세요.",
            )
            return redirect(referer)
        mention_text, unresolved = mentions_from_names(fallback_names)
        used_fallback = True
        fallback_label_suffix = " — 담당자 매칭 없음, 대체 담당자로 지정"
        # matches 는 owner=None 으로 표시 (담당자 라벨 안 나오도록)
        matches = [
            {
                "url": url,
                "method": method,
                "status_code": status_code,
                "owner": None,
            }
        ]

    text = build_error_followup_text(
        matches=matches,
        error_excerpt=error_excerpt,
        portal_log_url=portal_log_url,
        context_label=(context_label + fallback_label_suffix).strip(" —"),
        mention_text=mention_text,
    )

    # 토큰/채널 확인 후 발송 (silent skip 대신 사용자에게 명시적으로 알린다)
    settings_obj = SlackNotificationSettings.get_solo()
    channel = (settings_obj.default_channel or "").strip() or (
        os.getenv("SLACK_DEFAULT_CHANNEL") or ""
    ).strip()
    token = (os.getenv("SLACK_BOT_TOKEN") or "").strip()

    detail = (
        f"url={url} method={method} status={status_code} "
        f"source={source_type}:{source_id} "
        f"fallback={used_fallback} unresolved={','.join(unresolved)}"
    )

    sent = False
    if not token:
        messages.error(request, "Slack 토큰(.env SLACK_BOT_TOKEN) 이 설정되어 있지 않아 발송하지 못했습니다.")
    elif not channel:
        messages.error(request, "Slack 채널(default_channel 또는 SLACK_DEFAULT_CHANNEL) 이 비어 있어 발송하지 못했습니다.")
    else:
        sent = nsvc._post_message(channel, text)
        if sent:
            base_msg = f"Slack 채널 {channel} 으로 에러 노티를 발송했습니다."
            if used_fallback:
                base_msg += " (담당자 컬럼이 비어있어 대체 담당자로 발송)"
            if unresolved:
                base_msg += (
                    f" (Slack 워크스페이스 자동 매칭 실패 — @이름 평문: "
                    f"{', '.join(unresolved)})"
                )
            messages.success(request, base_msg)
        else:
            messages.error(request, "Slack 발송에 실패했습니다 (서버 로그 확인).")

    ActivityLog.log(
        user=request.user,
        action=ActivityLog.Action.OWNER_NOTIFIED,
        target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
        target=owner,
        target_name=f"{owner.method} {owner.endpoint}",
        detail=(detail + f" sent={sent}")[:500],
    )
    return redirect(referer)
