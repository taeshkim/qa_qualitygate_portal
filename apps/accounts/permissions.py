from rest_framework.permissions import BasePermission


class IsQPEAdmin(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == "qpe_admin"


class IsQPE(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in ("qpe_admin", "qpe_ops")


class IsQPEOrTribeLead(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in (
            "qpe_admin", "qpe_ops", "tribe_lead",
        )


class ReadOnlyOrQPE(BasePermission):
    def has_permission(self, request, view):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return request.user.is_authenticated
        return request.user.is_authenticated and request.user.role in ("qpe_admin", "qpe_ops")
