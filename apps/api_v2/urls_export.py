"""서버 이관용 DB 스냅샷 내보내기 라우팅 (`/api/v2/export/…`). 명세는 views_export 모듈 docstring 참조."""

from __future__ import annotations

from django.urls import path

from . import views_export

urlpatterns = [
    path("export/snapshots/", views_export.SnapshotCreateView.as_view(), name="export_snapshot_create"),
    # id 는 str 로 받아 뷰에서 uuid 정규형 검증 (잘못된 값은 표준 404 JSON)
    path("export/snapshots/<str:snapshot_id>/", views_export.SnapshotDetailView.as_view(),
         name="export_snapshot_detail"),
    path("export/snapshots/<str:snapshot_id>/download/<str:name>/",
         views_export.SnapshotDownloadView.as_view(), name="export_snapshot_download"),
]
