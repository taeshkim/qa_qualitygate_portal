import json

from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.views import serve as serve_static
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse, HttpResponse
from django.shortcuts import redirect
from django.urls import path, include, reverse
from urllib.parse import urlencode
from django.views.decorators.cache import never_cache


def health_check(request):
    return JsonResponse({"status": "ok"})


# Service Worker 범위는 포털 루트 (/ 또는 /qg/ 같은 배포 접두 경로).
@never_cache
def service_worker(request):
    portal_root = reverse("dashboard:index")
    body = "const PORTAL_ROOT = " + json.dumps(portal_root) + ";\n" + """
/* QG Portal Service Worker — OS 알림용 */
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener('message', (e) => {
    const d = e.data || {};
    if (d.type === 'show-notification') {
        self.registration.showNotification(d.title || 'QG Portal', {
            body: d.body || '',
            tag: d.tag || ('qg-' + Date.now()),
            requireInteraction: true,
            data: { url: d.url || PORTAL_ROOT },
        });
    }
});
self.addEventListener('notificationclick', (e) => {
    e.notification.close();
    const url = (e.notification.data && e.notification.data.url) || PORTAL_ROOT;
    e.waitUntil(clients.matchAll({type: 'window'}).then((list) => {
        for (const c of list) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
        if (clients.openWindow) return clients.openWindow(url);
    }));
});
""".strip()
    resp = HttpResponse(body, content_type="application/javascript")
    resp["Service-Worker-Allowed"] = portal_root
    return resp


def admin_login(request):
    """/admin/login/ — 자체 로그인이 닫혀 있으면(SSO 전용) 앱 로그인 화면으로 보낸다(next 보존)."""
    if not settings.LOCAL_LOGIN_ACTIVE:
        # 이미 로그인했지만 staff 가 아닌 사용자가 /admin/ 에 오면 로그인 화면(→ next 로 복귀)과 무한 왕복하므로 막는다.
        if request.user.is_authenticated:
            raise PermissionDenied
        url = reverse("accounts:login")
        nxt = request.GET.get("next", "")
        return redirect(url + ("?" + urlencode({"next": nxt}) if nxt else ""))
    return admin.site.login(request)


def admin_password_change(request):
    """SSO 전용일 때 admin 비밀번호 변경 화면을 닫는다(SSO 계정은 앱 비밀번호가 없다)."""
    if not settings.LOCAL_LOGIN_ACTIVE:
        raise Http404
    return admin.site.password_change(request)


urlpatterns = [
    path("sw.js", service_worker, name="service_worker"),
    path("health/", health_check, name="health_check"),
    path("admin/login/", admin_login),
    path("admin/password_change/", admin.site.admin_view(admin_password_change)),
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.api.urls")),
    path("api/v2/", include("apps.api_v2.urls")),
    path("", include("apps.dashboard.urls")),
    path("gates/", include("apps.gates.urls")),
    path("services/", include("apps.services.urls")),
    path("suites/", include("apps.suites.urls")),
    path("qualitygates/", include("apps.qualitygates.urls")),
    path("robustness/", include("apps.robustness.urls")),
    path("logs/", include("apps.activitylog.urls")),
    path("visualization/", include("apps.visualization.urls")),
]

# runserver의 정적 핸들러는 /qg/static/을 기대하지만 프록시 요청은 /static/이다.
# DEBUG 환경에서만 접두 경로가 제거된 요청도 동일한 staticfiles 뷰로 처리한다.
if settings.DEBUG:
    urlpatterns += [path("static/<path:path>", serve_static)]
