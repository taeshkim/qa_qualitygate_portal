"""Phase C — 메타 조회 URL 라우팅.

`apps/api_v2/urls.py` 의 include 대상 — master 에서 그쪽을 직접 수정해 통합한다.
"""

from __future__ import annotations

from django.urls import path

from .views.meta import (
    FolderDetailView,
    FolderListView,
    FolderSubFolderListView,
    GateDetailView,
    GateListView,
    GateParamsView,
    SubFolderDetailView,
    SuiteDetailView,
    SuiteListView,
)

app_name = "api_v2_meta"

urlpatterns = [
    # Folder
    path("folders/", FolderListView.as_view(), name="folder_list"),
    path("folders/<uuid:pk>/", FolderDetailView.as_view(), name="folder_detail"),
    path(
        "folders/<uuid:pk>/subfolders/",
        FolderSubFolderListView.as_view(),
        name="folder_subfolders",
    ),

    # SubFolder
    path("subfolders/<uuid:pk>/", SubFolderDetailView.as_view(), name="subfolder_detail"),

    # Gate
    path("gates/", GateListView.as_view(), name="gate_list"),
    path("gates/<uuid:pk>/", GateDetailView.as_view(), name="gate_detail"),
    path("gates/<uuid:pk>/params/", GateParamsView.as_view(), name="gate_params"),

    # Suite
    path("suites/", SuiteListView.as_view(), name="suite_list"),
    path("suites/<uuid:pk>/", SuiteDetailView.as_view(), name="suite_detail"),
]
