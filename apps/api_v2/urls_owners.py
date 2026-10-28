"""담당자 관리 API 라우팅 (`/api/v2/owners/` — `/ci-api/owners/` 와 공유). 명세는 views/owners.py 참조."""

from __future__ import annotations

from django.urls import path

from .views import owners

app_name = "api_v2_owners"

urlpatterns = [
    path("owners/", owners.OwnerCollectionView.as_view(), name="owner_list"),
    path("owners/<uuid:pk>/", owners.OwnerDetailView.as_view(), name="owner_detail"),
]
