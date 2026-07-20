from django.urls import path
from . import views

app_name = "services"

urlpatterns = [
    path("", views.service_list, name="service_list"),
    path("create/", views.service_create, name="service_create"),
    path("<uuid:pk>/edit/", views.service_edit, name="service_edit"),
    path("environments/", views.environment_list, name="environment_list"),
]
