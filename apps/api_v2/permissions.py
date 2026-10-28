"""스코프 + 역할 매트릭스 권한 (PRD §4.4).

세션 인증으로 들어온 경우(`request.auth is None`)에는 스코프 검사를 건너뛰고 역할만 본다 —
포털 admin/일반 사용자가 같은 코드를 호출할 수 있도록.
"""

from __future__ import annotations

from rest_framework import permissions

from . import scopes as _scopes


def _has_scope(request, required: str) -> bool:
    api_key = getattr(request, "auth", None)
    if api_key is None:
        # 세션 인증 — 스코프 개념 없음. role 검사로 통과 여부 결정.
        return True
    return api_key.has_scope(required)


def _is_authenticated(request) -> bool:
    return bool(getattr(request, "user", None) and request.user.is_authenticated)


class HasScope(permissions.BasePermission):
    """뷰 단위로 `required_scope` 클래스 속성 (또는 메서드별 dict) 으로 지정."""

    message = "scope_required"

    def has_permission(self, request, view):
        if not _is_authenticated(request):
            return False
        required = self._resolve_required(request, view)
        if required is None:
            return True
        return _has_scope(request, required)

    @staticmethod
    def _resolve_required(request, view):
        # method 별 매핑 우선
        per_method = getattr(view, "required_scopes", None)
        if isinstance(per_method, dict):
            method = request.method.upper()
            value = per_method.get(method) or per_method.get("*")
            return value
        return getattr(view, "required_scope", None)


class CanWriteForControl(permissions.BasePermission):
    """trigger / control 류는 `can_write` 역할이 필요. (Viewer 거부.)"""

    message = "role_denied"

    def has_permission(self, request, view):
        if not _is_authenticated(request):
            return False
        # write 가 필요한 메서드만 검사
        if request.method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            user = request.user
            return getattr(user, "can_write", False) or getattr(user, "is_superuser", False)
        return True


class AdminOnlyForWildcard(permissions.BasePermission):
    """API Key 발급 시 `*` 스코프는 admin 만. (UI 측에서 검사하지만 안전망으로 한 번 더.)"""

    message = "wildcard_admin_only"

    def has_permission(self, request, view):
        if not _is_authenticated(request):
            return False
        api_key = getattr(request, "auth", None)
        if api_key and _scopes.WILDCARD in (api_key.scopes or []):
            return bool(getattr(request.user, "is_admin", False))
        return True


class ExportPermission(permissions.BasePermission):
    """`/api/v2/export/` 전용 — API Key 필수 + `export:read` 명시 스코프 + 키 소유자 admin.

    - 세션 인증은 허용하지 않는다 (뷰에서 ApiKeyAuthentication 만 사용).
    - `*` 와일드카드는 export:read 를 포함하지 않는다 (`scopes.has_scope` 의 EXPLICIT_ONLY_SCOPES).
    - 소유자가 admin(qpe_admin/superuser) 이 아니면 스코프가 있어도 403 role_denied.
      (요청 시점에 소유자 역할을 다시 본다 — 발급 후 강등된 키도 차단.)
    """

    message = "scope_required"

    def has_permission(self, request, view):
        if not _is_authenticated(request):
            return False
        api_key = getattr(request, "auth", None)
        if api_key is None or not api_key.has_scope(_scopes.EXPORT_READ):
            self.message = "scope_required"
            return False
        if not getattr(request.user, "is_admin", False):
            self.message = "role_denied"
            return False
        return True


class OwnersManagePermission(permissions.BasePermission):
    """`/api/v2/owners/` 전용 — API Key 필수 + `owners:manage` 명시 스코프 + 키 소유자 admin.

    UI 의 담당자 관리(`/accounts/api-owners/`)가 admin 전용이므로 API 도 같은 기준이다.
    `*` 와일드카드는 owners:manage 를 포함하지 않는다. 요청 시점에 소유자 역할을 다시 본다.
    """

    message = "scope_required"

    def has_permission(self, request, view):
        if not _is_authenticated(request):
            return False
        api_key = getattr(request, "auth", None)
        if api_key is None or not api_key.has_scope(_scopes.OWNERS_MANAGE):
            self.message = "scope_required"
            return False
        if not getattr(request.user, "is_admin", False):
            self.message = "role_denied"
            return False
        return True


# 자주 쓰는 조합
ReadPermissions = [HasScope]
WritePermissions = [HasScope, CanWriteForControl, AdminOnlyForWildcard]
