from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from .models import Service, Environment
from .forms import ServiceForm


@login_required
def service_list(request):
    services = Service.objects.all()
    return render(request, "services/service_list.html", {"services": services})


@login_required
def service_create(request):
    if request.method == "POST":
        form = ServiceForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("services:service_list")
    else:
        form = ServiceForm()
    return render(request, "services/service_form.html", {"form": form, "title": "서비스 등록"})


@login_required
def service_edit(request, pk):
    service = get_object_or_404(Service, pk=pk)
    if request.method == "POST":
        form = ServiceForm(request.POST, instance=service)
        if form.is_valid():
            form.save()
            return redirect("services:service_list")
    else:
        form = ServiceForm(instance=service)
    return render(request, "services/service_form.html", {"form": form, "title": "서비스 수정"})


@login_required
def environment_list(request):
    environments = Environment.objects.all()
    return render(request, "services/environment_list.html", {"environments": environments})
