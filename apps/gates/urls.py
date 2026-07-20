from django.urls import path
from . import views

app_name = "gates"

urlpatterns = [
    path("", views.gate_run_list, name="run_list"),
    path("<uuid:pk>/", views.gate_run_detail, name="run_detail"),
    path("<uuid:run_pk>/failures/<uuid:failure_pk>/triage/", views.triage_failure, name="triage_failure"),
]
