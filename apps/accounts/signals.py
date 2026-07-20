from django.contrib.auth.signals import (
    user_logged_in, user_logged_out, user_login_failed,
)
from django.dispatch import receiver

from apps.activitylog.models import ActivityLog


def _client_ip(request):
    if request is None:
        return ""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


@receiver(user_logged_in)
def log_login(sender, request, user, **kwargs):
    ActivityLog.log(
        user=user, action=ActivityLog.Action.LOGIN,
        target_type=ActivityLog.TargetType.USER, target=user,
        target_name=user.get_username(),
        detail=f"ip={_client_ip(request)}",
    )


@receiver(user_logged_out)
def log_logout(sender, request, user, **kwargs):
    if user is None:
        return
    ActivityLog.log(
        user=user, action=ActivityLog.Action.LOGOUT,
        target_type=ActivityLog.TargetType.USER, target=user,
        target_name=user.get_username(),
        detail=f"ip={_client_ip(request)}",
    )


@receiver(user_login_failed)
def log_login_failed(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get("username", "") or "?"
    ActivityLog.log(
        user=None, action=ActivityLog.Action.LOGIN_FAILED,
        target_type=ActivityLog.TargetType.USER,
        target_name=str(username)[:300],
        detail=f"ip={_client_ip(request)}",
    )
