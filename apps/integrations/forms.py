"""apps.integrations Admin 화면용 폼.

improvement.md §5.4 (Import 폼) 및 §5.3 (CRUD) 사양 그대로.
"""
from __future__ import annotations

from django import forms

from .models import ApiEndpointOwner, ApiHostMapping, FallbackOwners


class ApiEndpointOwnerForm(forms.ModelForm):
    """담당자 row 생성/수정 폼.

    `is_imported`, `created_at`, `updated_at` 는 폼에서 제외.
    수동 생성은 view 에서 `is_imported=False` 로 저장한다.
    """

    class Meta:
        model = ApiEndpointOwner
        fields = [
            "repo",
            "method",
            "endpoint",
            "category",
            "area",
            "mapped_app",
            "fe_owner",
            "be_owner",
            "usage_count",
            "step_ids",
            "notes",
        ]
        widgets = {
            "endpoint": forms.TextInput(attrs={"size": 80}),
            "step_ids": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class ApiHostMappingForm(forms.ModelForm):
    class Meta:
        model = ApiHostMapping
        fields = ["variable_name", "env", "host_pattern", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class FallbackOwnersForm(forms.ModelForm):
    """API Endpoint 매칭 실패 시 알림 받을 대체 담당자 (singleton)."""

    class Meta:
        model = FallbackOwners
        fields = ["owners"]
        widgets = {
            "owners": forms.TextInput(
                attrs={
                    "placeholder": "예: 김태식, 김도연",
                    "style": "width:100%;max-width:520px",
                }
            ),
        }
