from django.contrib import admin
from django.http import JsonResponse, HttpResponse
from django.urls import path, include
from django.views.decorators.cache import never_cache


def health_check(request):
    return JsonResponse({"status": "ok"})


# Service Worker 는 루트 스코프로 서빙되어야 전체 사이트에서 동작하므로 /sw.js 로 노출
@never_cache
def service_worker(request):
    body = """
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
            data: { url: d.url || '/' },
        });
    }
});
self.addEventListener('notificationclick', (e) => {
    e.notification.close();
    const url = (e.notification.data && e.notification.data.url) || '/';
    e.waitUntil(clients.matchAll({type: 'window'}).then((list) => {
        for (const c of list) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
        if (clients.openWindow) return clients.openWindow(url);
    }));
});
""".strip()
    resp = HttpResponse(body, content_type="application/javascript")
    resp["Service-Worker-Allowed"] = "/"
    return resp


urlpatterns = [
    path("sw.js", service_worker, name="service_worker"),
    path("health/", health_check, name="health_check"),
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
]
