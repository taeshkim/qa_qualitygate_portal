from django.urls import path

from . import views

app_name = "activitylog"

urlpatterns = [
    path("", views.log_list, name="list"),
]
