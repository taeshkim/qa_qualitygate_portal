from django.contrib import messages
from django.shortcuts import redirect


# 모든 사용자에게 POST가 허용되어야 하는 경로
POST_ALWAYS_ALLOW = (
    "/accounts/logout/",
    # API Key 발급/회수 — 모든 인증 사용자(Viewer 포함)가 자기 키를 관리할 수 있어야 한다.
    # admin 전용 페이지(/accounts/api-keys/admin/)는 view 단의 _require_admin 으로 별도 차단.
    "/accounts/api-keys/",
)

# 일반 역할(non-admin)이 POST 가능한 경로 패턴 (실행/시작/중단 등 operational)
REGULAR_POST_ALLOW = (
    "/run",           # suite_run, gate_run
    "/start/",        # schedule_start
    "/start-all/",    # schedule_start_all
    "/stop/",         # schedule_stop
    "/stop-all/",     # schedule_stop_all, queue_stop_all
    "/trigger/",      # schedule_trigger_now
    "/status/",       # HTMX polling
    "/rerun/",        # rerun_gate
    "/deploy/",       # prod_deploy
    "/reset/",        # schedule_reset_stats
    "/triage/",       # triage_failure
)


class RolePermissionMiddleware:
    """3단계 권한 체계:
    - Admin (QPE Admin / superuser): 모든 작업 가능
    - 일반 역할: 실행/시작/중단만 가능 (등록/수정/삭제 불가)
    - Viewer: 조회만 가능 (모든 POST 차단)
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return self.get_response(request)

        # /api/v2/ 는 DRF 가 자체 권한 처리 — 미들웨어 분기 우회 (PRD §4.4)
        if request.path.startswith("/api/v2/"):
            return self.get_response(request)

        if not request.user.is_authenticated or not hasattr(request.user, "role"):
            return self.get_response(request)

        # 항상 허용되는 경로
        if any(request.path.startswith(p) for p in POST_ALWAYS_ALLOW):
            return self.get_response(request)

        # Admin → 전부 허용
        if request.user.is_admin:
            return self.get_response(request)

        # Viewer → 전부 차단
        if request.user.role == "viewer":
            messages.error(request, "Viewer 권한으로는 이 작업을 수행할 수 없습니다.")
            return redirect(request.META.get("HTTP_REFERER", "/"))

        # 일반 역할 → 실행 관련 경로만 허용
        if any(seg in request.path for seg in REGULAR_POST_ALLOW):
            return self.get_response(request)

        # 그 외 (등록/수정/삭제) → 차단
        messages.error(request, "이 작업은 관리자만 수행할 수 있습니다.")
        return redirect(request.META.get("HTTP_REFERER", "/"))
