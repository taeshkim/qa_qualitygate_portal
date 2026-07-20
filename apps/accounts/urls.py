from django.contrib.auth import views as auth_views
from django.urls import path

from apps.integrations import views as int_views

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="accounts/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("signup/", views.signup, name="signup"),
    # 관리자 - 사용자 관리
    path("users/", views.user_list, name="user_list"),
    path("users/<uuid:pk>/update/", views.user_update, name="user_update"),
    path("users/<uuid:pk>/toggle-active/", views.user_toggle_active, name="user_toggle_active"),
    path("users/<uuid:pk>/delete/", views.user_delete, name="user_delete"),
    # 관리자 - Slack 알림 설정
    path("notifications/", views.notification_settings, name="notification_settings"),
    path("notifications/test/", views.notification_settings_test, name="notification_settings_test"),
    # API Key 관리 (본인)
    path("api-keys/", views.api_key_list, name="api_key_list"),
    path("api-keys/new/", views.api_key_create, name="api_key_create"),
    path("api-keys/<uuid:pk>/revoke/", views.api_key_revoke, name="api_key_revoke"),
    # API Key 관리 (admin 전체 보기)
    path("api-keys/admin/", views.api_key_admin_list, name="api_key_admin_list"),
    path("api-keys/admin/<uuid:pk>/revoke/", views.api_key_admin_revoke, name="api_key_admin_revoke"),
    # API Endpoint Owner (담당자 매핑) — view 는 apps/integrations/views.py 에
    path("api-owners/", int_views.api_owner_list, name="api_owner_list"),
    path("api-owners/new/", int_views.api_owner_create, name="api_owner_create"),
    path("api-owners/sync/", int_views.api_owner_sync, name="api_owner_sync"),
    path("api-owners/export.csv", int_views.api_owner_export, name="api_owner_export"),
    path("api-owners/<uuid:pk>/edit/", int_views.api_owner_edit, name="api_owner_edit"),
    path("api-owners/<uuid:pk>/delete/", int_views.api_owner_delete, name="api_owner_delete"),
    # API Host Mapping (호스트 매핑)
    path("api-hosts/", int_views.api_host_list, name="api_host_list"),
    path("api-hosts/new/", int_views.api_host_create, name="api_host_create"),
    path("api-hosts/<uuid:pk>/edit/", int_views.api_host_edit, name="api_host_edit"),
    path("api-hosts/<uuid:pk>/delete/", int_views.api_host_delete, name="api_host_delete"),
    # Owner 에게 수동 에러 노티 발송
    path("api-owners/<uuid:pk>/notify-error/", int_views.api_owner_notify_error, name="api_owner_notify_error"),
    # 자동 동기화 주기 (singleton) 저장
    path("api-owners/sync-settings/save/", int_views.owner_sync_settings_save, name="owner_sync_settings_save"),
    # 대체 담당자 (singleton) 저장 + 매칭 실패 URL 의 노티
    path("api-owners/fallback/save/", int_views.fallback_owners_save, name="fallback_owners_save"),
    path("api-owners/fallback/notify/", int_views.notify_fallback_owners, name="notify_fallback_owners"),
    # Slack 자동 매칭 디버그 페이지
    path("slack-users/", int_views.slack_users_debug, name="slack_users_debug"),
]
