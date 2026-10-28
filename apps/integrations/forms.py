"""apps.integrations Admin 화면용 폼.

improvement.md §5.4 (Import 폼) 및 §5.3 (CRUD) 사양 그대로.
"""
from __future__ import annotations

from django import forms

from .models import ApiEndpointOwner, ApiHostMapping, FallbackOwners, UiAutomationOwners


class ApiEndpointOwnerForm(forms.ModelForm):
    """담당자 row 생성/수정 폼.

    `is_imported`, `created_at`, `updated_at` 는 폼에서 제외.
    수동 생성은 view 에서 `is_imported=False` 로 저장한다.

    - 레포 / 카테고리 / 영역은 Google Sheets(=DB 미러)의 기존 값 목록을 **드롭다운**
      으로 제공한다 (시트의 통제 어휘에 맞춰 일관되게 등록하기 위함). choices 는
      `__init__` 에서 DB distinct 값으로 동적 구성하며, 편집/prefill 중인 현재 값이
      목록에 없어도 보존되도록 항상 포함한다.
    - 사용 횟수는 **필수값이 아님** (비우면 0). 수동 등록 시 굳이 입력 안 해도 된다.
    """

    # 시트의 통제 어휘를 드롭다운으로 제공할 분류 필드.
    _DROPDOWN_FIELDS = ("repo", "category", "area")

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
            "method": forms.TextInput(attrs={"placeholder": "GET"}),
            "endpoint": forms.TextInput(
                attrs={"size": 80, "placeholder": "/member/users/edit-by-pop-up"}
            ),
            "fe_owner": forms.TextInput(
                attrs={"placeholder": "예: 김프론트 (여러 명은 콤마로)"}
            ),
            "be_owner": forms.TextInput(
                attrs={"placeholder": "예: 김백엔드 (여러 명은 콤마로)"}
            ),
            "mapped_app": forms.TextInput(attrs={"placeholder": "선택"}),
            "step_ids": forms.Textarea(attrs={"rows": 2, "placeholder": "선택 — 콤마 구분"}),
            "notes": forms.Textarea(attrs={"rows": 3, "placeholder": "선택"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for fname in self._DROPDOWN_FIELDS:
            model_field = ApiEndpointOwner._meta.get_field(fname)
            required = not model_field.blank  # repo 만 required (category/area 는 blank=True)

            values = (
                ApiEndpointOwner.objects.exclude(**{fname: ""})
                .values_list(fname, flat=True)
                .distinct()
            )
            opts = {(v or "").strip() for v in values if (v or "").strip()}
            # 편집/prefill 중인 현재 값은 목록에 없어도 선택 유지되도록 포함.
            for extra in (self.initial.get(fname), getattr(self.instance, fname, "")):
                extra = (extra or "").strip()
                if extra:
                    opts.add(extra)

            blank_label = "— 선택 —" if required else "— (없음) —"
            choices = [("", blank_label)] + [(o, o) for o in sorted(opts)]
            self.fields[fname] = forms.ChoiceField(
                label=model_field.verbose_name,
                required=required,
                choices=choices,
                widget=forms.Select(attrs={"class": "form-input"}),
            )

        # 사용 횟수는 선택값 — 비우면 0 으로 저장.
        self.fields["usage_count"].required = False

    def clean_usage_count(self):
        return self.cleaned_data.get("usage_count") or 0


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


class UiAutomationOwnersForm(forms.ModelForm):
    """UI Test Automation(TAP) 실패 알림 담당자 (singleton)."""

    class Meta:
        model = UiAutomationOwners
        fields = ["owners"]
