from django.urls import path

from . import views

app_name = "robustness"

urlpatterns = [
    path("", views.test_list, name="test_list"),
    path("register/", views.test_register, name="test_register"),
    path("<uuid:pk>/", views.test_detail, name="test_detail"),
    path("<uuid:pk>/edit/", views.test_edit, name="test_edit"),
    path("<uuid:pk>/delete/", views.test_delete, name="test_delete"),
    path("<uuid:pk>/fragment/", views.test_detail_fragment, name="test_detail_fragment"),
    path("<uuid:pk>/schedules/add/", views.schedule_add, name="schedule_add"),
    path("<uuid:pk>/schedules/add-all/", views.schedule_add_all, name="schedule_add_all"),
    path("<uuid:pk>/schedules/start-all/", views.schedule_start_all, name="schedule_start_all"),
    path("<uuid:pk>/schedules/stop-all/", views.schedule_stop_all, name="schedule_stop_all"),
    path("<uuid:pk>/schedules/reset-all/", views.schedule_reset_all_stats, name="schedule_reset_all"),
    path("<uuid:pk>/toggle-parallel/", views.test_toggle_parallel, name="test_toggle_parallel"),

    path("schedules/<uuid:pk>/start/", views.schedule_start, name="schedule_start"),
    path("schedules/<uuid:pk>/stop/", views.schedule_stop, name="schedule_stop"),
    path("schedules/<uuid:pk>/trigger/", views.schedule_trigger_now, name="schedule_trigger"),
    path("schedules/<uuid:pk>/interval/", views.schedule_update_interval, name="schedule_update_interval"),
    path("schedules/<uuid:pk>/delete/", views.schedule_delete, name="schedule_delete"),
    path("schedules/<uuid:pk>/reset/", views.schedule_reset_stats, name="schedule_reset"),
    path("schedules/<uuid:pk>/runs/", views.schedule_runs, name="schedule_runs"),
]
