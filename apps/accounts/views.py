import os

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.activitylog.models import ActivityLog
from apps.api_v2 import scopes as api_scopes
from apps.api_v2.models import ApiKey
from apps.notifications import events as nev
from apps.notifications.models import SlackNotificationSettings
from apps.notifications.services import send_test_message

from .forms import AdminUserCreateForm, SignupForm
from .models import PortalSettings, User


# =============================================
# 회원가입 (가입 신청 → 관리자 승인 대기)
# =============================================

def signup(request):
    # SSO 전환 후(LOCAL_LOGIN_ACTIVE=False)에는 자체 가입 경로를 닫는다 — 계정은 SSO 첫 로그인으로 생성된다.
    if not settings.LOCAL_LOGIN_ACTIVE:
        raise Http404
    if request.user.is_authenticated:
        return redirect("dashboard:index")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            form.save()  # is_active=False 로 생성됨
            return render(request, "accounts/signup_pending.html")
    else:
        form = SignupForm()
    return render(request, "accounts/signup.html", {"form": form})


# =============================================
# 관리자 - 사용자 관리
# =============================================

def _require_admin(user):
    if not user.is_authenticated or not user.is_admin:
        raise PermissionDenied("관리자만 접근할 수 있습니다.")


@login_required
def user_list(request):
    _require_admin(request.user)
    pending_users = User.objects.filter(is_active=False).order_by("-date_joined")
    active_users = User.objects.filter(is_active=True).order_by("-date_joined")
    return render(request, "accounts/user_list.html", {
        "pending_users": pending_users,
        "active_users": active_users,
        "role_choices": User.Role.choices,
        "admin_active_tab": "users",
    })


@login_required
def user_create(request):
    """관리자가 사용자 관리 화면에서 직접 계정을 추가한다.

    가입 신청/승인 절차 없이 즉시 활성 계정으로 생성 — 역할도 이 자리에서
    지정한다. 검증(중복 ID/이메일, 비밀번호 규칙)은 가입 폼과 동일
    (`AdminUserCreateForm` 이 `SignupForm` 상속). 생성 이력은 ActivityLog 에
    남긴다(비밀번호는 어디에도 기록하지 않음).
    """
    _require_admin(request.user)

    if request.method == "POST":
        form = AdminUserCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            ActivityLog.log(
                request.user, ActivityLog.Action.CREATE,
                ActivityLog.TargetType.USER, target=user,
                target_name=user.username,
                detail=f"관리자 직접 추가 / role={user.role} / 즉시 활성",
            )
            messages.success(
                request,
                f"'{user.username}' 계정을 추가했습니다 (역할: {user.get_role_display()}, 즉시 사용 가능).",
            )
            return redirect("accounts:user_list")
    else:
        form = AdminUserCreateForm()
    return render(request, "accounts/user_form.html", {
        "form": form,
        "admin_active_tab": "users",
    })


@login_required
@require_POST
def user_update(request, pk):
    _require_admin(request.user)
    target = get_object_or_404(User, pk=pk)

    role = request.POST.get("role")
    if role and role in User.Role.values:
        target.role = role

    target.is_active = request.POST.get("is_active") == "on"
    target.save(update_fields=["role", "is_active"])
    messages.success(request, f"'{target.username}' 정보를 변경했습니다.")
    return redirect("accounts:user_list")


@login_required
@require_POST
def user_toggle_active(request, pk):
    _require_admin(request.user)
    target = get_object_or_404(User, pk=pk)

    if target == request.user:
        messages.error(request, "본인 계정은 비활성화할 수 없습니다.")
        return redirect("accounts:user_list")

    target.is_active = not target.is_active
    target.save(update_fields=["is_active"])
    messages.success(
        request,
        f"'{target.username}' 을(를) {'활성화' if target.is_active else '비활성화'}했습니다.",
    )
    return redirect("accounts:user_list")


@login_required
@require_POST
def user_delete(request, pk):
    _require_admin(request.user)
    target = get_object_or_404(User, pk=pk)

    if target == request.user:
        messages.error(request, "본인 계정은 삭제할 수 없습니다.")
        return redirect("accounts:user_list")

    username = target.username
    target.delete()
    messages.info(request, f"'{username}' 계정을 삭제했습니다.")
    return redirect("accounts:user_list")


# =============================================
# 관리자 - Slack 알림 설정
# =============================================

@login_required
def notification_settings(request):
    """Slack 알림 설정 화면. 토큰은 .env 의 SLACK_BOT_TOKEN, 채널/이벤트 토글은 DB."""
    _require_admin(request.user)
    settings_obj = SlackNotificationSettings.get_solo()

    if request.method == "POST":
        settings_obj.enabled = request.POST.get("enabled") == "on"
        settings_obj.default_channel = (request.POST.get("default_channel") or "").strip()
        for code in nev.ALL_EVENTS:
            field = f"notify_{code}"
            if hasattr(settings_obj, field):
                setattr(settings_obj, field, request.POST.get(field) == "on")
        settings_obj.save()
        messages.success(request, "Slack 알림 설정을 저장했습니다.")
        return redirect("accounts:notification_settings")

    bot_token_present = bool((os.getenv("SLACK_BOT_TOKEN") or "").strip())
    env_default_channel = (os.getenv("SLACK_DEFAULT_CHANNEL") or "").strip()
    return render(request, "accounts/notification_settings.html", {
        "settings_obj": settings_obj,
        "event_tabs": nev.EVENT_TABS,
        "bot_token_present": bot_token_present,
        "env_default_channel": env_default_channel,
        "admin_active_tab": "notifications",
    })


@login_required
@require_POST
def notification_settings_test(request):
    """테스트 메시지 전송 버튼."""
    _require_admin(request.user)
    channel = (request.POST.get("channel") or "").strip()
    ok, msg = send_test_message(channel or None)
    if ok:
        messages.success(request, msg)
    else:
        messages.error(request, msg)
    return redirect("accounts:notification_settings")


# =============================================
# 포털 설정 (메뉴 표시 등)
# =============================================

@login_required
def portal_settings(request):
    """포털 UI 설정 — 좌측 사이드바 메뉴 표시 토글 (admin 전용)."""
    _require_admin(request.user)
    settings_obj = PortalSettings.get_solo()

    if request.method == "POST":
        settings_obj.show_quality_gate = request.POST.get("show_quality_gate") == "on"
        settings_obj.save(update_fields=["show_quality_gate", "updated_at"])
        messages.success(request, "포털 설정을 저장했습니다.")
        return redirect("accounts:portal_settings")

    return render(request, "accounts/portal_settings.html", {
        "settings_obj": settings_obj,
        "admin_active_tab": "portal_settings",
    })


# =============================================
# API Key 관리 (prd_api.md §4.5)
# =============================================
#
# - /accounts/api-keys/                 → 본인 키 목록
# - /accounts/api-keys/new/             → 발급 폼 + 평문 1회 노출
# - /accounts/api-keys/<uuid>/revoke/   → 본인 키 회수
# - /accounts/api-keys/admin/           → admin 전체 키 조회/회수
#
# RolePermissionMiddleware 의 POST_ALWAYS_ALLOW 에 "/accounts/api-keys/" 가 포함돼 있어
# Viewer 를 포함한 모든 인증 사용자가 자기 키를 발급/회수할 수 있다.

# 만료일 옵션 (일 단위). 0 = 무제한 (None).
_EXPIRY_CHOICES = [
    ("30", "30일"),
    ("90", "90일 (기본)"),
    ("180", "180일"),
    ("365", "365일"),
    ("0", "무제한"),
]


def _scope_options_for(user) -> list[tuple[str, str, bool]]:
    """발급 폼에 노출할 스코프 옵션 목록. (값, 설명, admin 전용 여부)."""
    base = [
        (api_scopes.META_READ, api_scopes.SCOPE_LABELS[api_scopes.META_READ], False),
        (api_scopes.RUNS_READ, api_scopes.SCOPE_LABELS[api_scopes.RUNS_READ], False),
        (api_scopes.RUNS_TRIGGER, api_scopes.SCOPE_LABELS[api_scopes.RUNS_TRIGGER], False),
        (api_scopes.RUNS_CONTROL, api_scopes.SCOPE_LABELS[api_scopes.RUNS_CONTROL], False),
        (api_scopes.EXPORT_READ, api_scopes.SCOPE_LABELS[api_scopes.EXPORT_READ], True),
        (api_scopes.OWNERS_MANAGE, api_scopes.SCOPE_LABELS[api_scopes.OWNERS_MANAGE], True),
        (api_scopes.WILDCARD, api_scopes.SCOPE_LABELS[api_scopes.WILDCARD], True),
    ]
    return base


@login_required
def api_key_list(request):
    """본인 키 목록."""
    keys = ApiKey.objects.filter(user=request.user).order_by("revoked_at", "-created_at")
    return render(request, "accounts/api_key_list.html", {
        "keys": keys,
    })


@login_required
def api_key_create(request):
    """발급 폼 (GET) + 발급 처리 (POST)."""
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        selected = request.POST.getlist("scopes")
        expiry_raw = (request.POST.get("expires_in_days") or "90").strip()

        # 입력 검증
        if not name:
            messages.error(request, "이름을 입력하세요.")
            return redirect("accounts:api_key_create")

        # 와일드카드는 admin 만
        if api_scopes.WILDCARD in selected and not request.user.is_admin:
            raise PermissionDenied("`*` 스코프는 관리자만 부여할 수 있습니다.")

        # 알 수 없는 스코프 거부
        allowed = {s for s, _label, admin_only in _scope_options_for(request.user)
                   if not admin_only or request.user.is_admin}
        cleaned_scopes = [s for s in selected if s in allowed]
        if not cleaned_scopes:
            messages.error(request, "스코프를 1개 이상 선택하세요.")
            return redirect("accounts:api_key_create")

        # 만료일
        try:
            expiry_days_int = int(expiry_raw)
        except ValueError:
            expiry_days_int = 90
        expires_in_days = None if expiry_days_int == 0 else expiry_days_int

        try:
            key, plaintext = ApiKey.issue(
                user=request.user,
                name=name,
                scopes=cleaned_scopes,
                expires_in_days=expires_in_days,
            )
        except IntegrityError:
            messages.error(request, f"이미 같은 이름의 키가 있습니다 — '{name}'. 다른 이름을 사용하세요.")
            return redirect("accounts:api_key_create")

        ActivityLog.log(
            user=request.user,
            action=ActivityLog.Action.API_KEY_CREATED,
            target_type=ActivityLog.TargetType.USER,
            target=request.user,
            target_name=f"{request.user.username}/{key.name}",
            detail=f"prefix={key.key_prefix} scopes={','.join(cleaned_scopes)} expires_in_days={expires_in_days}",
        )

        return render(request, "accounts/api_key_created.html", {
            "key": key,
            "plaintext": plaintext,
        })

    # GET
    return render(request, "accounts/api_key_form.html", {
        "scope_options": _scope_options_for(request.user),
        "expiry_choices": _EXPIRY_CHOICES,
        "default_expiry": "90",
        "is_admin": request.user.is_admin,
    })


@login_required
@require_POST
def api_key_revoke(request, pk):
    """본인 키 회수 (admin 이면 어떤 키든 회수 가능)."""
    if request.user.is_admin:
        key = get_object_or_404(ApiKey, pk=pk)
    else:
        key = get_object_or_404(ApiKey, pk=pk, user=request.user)

    if key.is_revoked:
        messages.info(request, f"키 '{key.name}' 은 이미 회수된 상태입니다.")
        return redirect("accounts:api_key_list")

    key.revoke()
    ActivityLog.log(
        user=request.user,
        action=ActivityLog.Action.API_KEY_REVOKED,
        target_type=ActivityLog.TargetType.USER,
        target=key.user,
        target_name=f"{key.user.username}/{key.name}",
        detail=f"prefix={key.key_prefix}",
    )
    messages.success(request, f"키 '{key.name}' 을(를) 회수했습니다.")
    return redirect("accounts:api_key_list")


@login_required
def api_key_admin_list(request):
    """admin 전체 키 조회."""
    _require_admin(request.user)
    keys = (ApiKey.objects
            .select_related("user")
            .order_by("revoked_at", "-created_at"))
    return render(request, "accounts/api_key_admin_list.html", {
        "keys": keys,
        "admin_active_tab": "api_keys",
    })


@login_required
@require_POST
def api_key_admin_revoke(request, pk):
    """admin 이 임의의 키를 회수."""
    _require_admin(request.user)
    key = get_object_or_404(ApiKey.objects.select_related("user"), pk=pk)
    if key.is_revoked:
        messages.info(request, f"키 '{key.name}' 은 이미 회수된 상태입니다.")
        return redirect("accounts:api_key_admin_list")

    key.revoke()
    ActivityLog.log(
        user=request.user,
        action=ActivityLog.Action.API_KEY_REVOKED,
        target_type=ActivityLog.TargetType.USER,
        target=key.user,
        target_name=f"{key.user.username}/{key.name}",
        detail=f"prefix={key.key_prefix} by_admin={request.user.username}",
    )
    messages.success(request, f"'{key.user.username}' 의 키 '{key.name}' 을(를) 회수했습니다.")
    return redirect("accounts:api_key_admin_list")
