import re

from django import forms
from django.core.exceptions import ValidationError

from .models import User

SPECIAL_CHAR_RE = re.compile(r"[!@#$%^&*()\-_=+\[\]{};':\"\\|,.<>/?`~]")


class SignupForm(forms.ModelForm):
    password1 = forms.CharField(
        label="비밀번호",
        widget=forms.PasswordInput(attrs={"class": "form-input"}),
        min_length=8,
        help_text="8자 이상, 특수문자 1자 이상 포함",
    )
    password2 = forms.CharField(
        label="비밀번호 확인",
        widget=forms.PasswordInput(attrs={"class": "form-input"}),
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "email"]
        labels = {
            "username": "ID",
            "first_name": "이름",
            "email": "이메일",
        }
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-input"}),
            "first_name": forms.TextInput(attrs={"class": "form-input"}),
            "email": forms.EmailInput(attrs={"class": "form-input"}),
        }

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise ValidationError("이미 사용 중인 ID입니다.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip()
        if not email:
            raise ValidationError("이메일을 입력하세요.")
        if User.objects.filter(email=email).exists():
            raise ValidationError("이미 등록된 이메일입니다.")
        return email

    def clean_password1(self):
        pw = self.cleaned_data.get("password1", "")
        if len(pw) < 8:
            raise ValidationError("비밀번호는 8자 이상이어야 합니다.")
        if not SPECIAL_CHAR_RE.search(pw):
            raise ValidationError(
                "비밀번호에 특수문자(!@#$%^&* 등)를 1자 이상 포함해야 합니다."
            )
        return pw

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1")
        p2 = cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "비밀번호가 일치하지 않습니다.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        user.role = User.Role.VIEWER
        user.is_active = False  # 관리자 승인 전까지 비활성
        if commit:
            user.save()
        return user


class AdminUserCreateForm(SignupForm):
    """관리자가 사용자 관리 화면에서 직접 계정을 추가하는 폼.

    SignupForm 의 검증(중복 ID/이메일, 비밀번호 8자+특수문자, 확인 일치)을
    그대로 상속하고, 차이는 두 가지 — ① 역할(role)을 관리자가 지정 ②
    승인 대기 없이 **즉시 활성(is_active=True)** 로 생성.
    """

    role = forms.ChoiceField(
        label="역할",
        choices=User.Role.choices,
        initial=User.Role.VIEWER,
        widget=forms.Select(attrs={"class": "form-input"}),
    )

    # 역할을 비밀번호 입력란보다 앞에 — 상속 병합 순서상 role 이 맨 뒤로
    # 렌더되는 것을 교정 (검증/저장 로직과 무관, 표시 순서만).
    field_order = ["username", "first_name", "email", "role", "password1", "password2"]

    def save(self, commit=True):
        user = super().save(commit=False)  # 비밀번호 해시는 부모가 처리
        user.role = self.cleaned_data["role"]
        user.is_active = True  # 관리자가 직접 추가 → 승인 절차 생략
        if commit:
            user.save()
        return user
