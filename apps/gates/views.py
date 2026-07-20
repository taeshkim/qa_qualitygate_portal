import json
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from .models import GateRun, GateFailure


@login_required
def gate_run_list(request):
    runs = GateRun.objects.select_related("service", "environment").all()

    service_filter = request.GET.get("service")
    env_filter = request.GET.get("environment")
    result_filter = request.GET.get("result")

    if service_filter:
        runs = runs.filter(service_id=service_filter)
    if env_filter:
        runs = runs.filter(environment_id=env_filter)
    if result_filter:
        runs = runs.filter(result=result_filter)

    return render(request, "gates/run_list.html", {"runs": runs[:50]})


@login_required
def gate_run_detail(request, pk):
    run = get_object_or_404(
        GateRun.objects.select_related("service", "environment"),
        pk=pk,
    )
    failures = run.failures.select_related("categorized_by").all()
    return render(request, "gates/run_detail.html", {"run": run, "failures": failures})


@login_required
@require_http_methods(["PATCH"])
def triage_failure(request, run_pk, failure_pk):
    failure = get_object_or_404(GateFailure, pk=failure_pk, gate_run_id=run_pk)
    data = json.loads(request.body)
    category = data.get("category", "")

    if category in dict(GateFailure.Category.choices):
        failure.category = category
        failure.categorized_by = request.user
        failure.categorized_at = timezone.now()
        failure.save(update_fields=["category", "categorized_by", "categorized_at"])

        label = failure.get_category_display()
        return HttpResponse(
            f'<span class="category-badge category-{category}">{label}</span>'
        )

    return HttpResponse("Invalid category", status=400)
