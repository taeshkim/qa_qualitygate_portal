from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from .models import ActivityLog


@login_required
def log_list(request):
    logs = ActivityLog.objects.select_related("user")

    target_type = request.GET.get("type", "").strip()
    action = request.GET.get("action", "").strip()
    q = request.GET.get("q", "").strip()

    if target_type and target_type in ActivityLog.TargetType.values:
        logs = logs.filter(target_type=target_type)
    if action and action in ActivityLog.Action.values:
        logs = logs.filter(action=action)
    if q:
        logs = logs.filter(target_name__icontains=q)

    paginator = Paginator(logs, 50)
    page = paginator.get_page(request.GET.get("page"))

    return render(request, "activitylog/log_list.html", {
        "page": page,
        "target_types": ActivityLog.TargetType.choices,
        "actions": ActivityLog.Action.choices,
        "selected_type": target_type,
        "selected_action": action,
        "q": q,
    })
