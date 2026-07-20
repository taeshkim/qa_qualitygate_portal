from django import forms
from .models import Service


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ["name", "code", "tribe_name", "slack_channel_id", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input"}),
            "code": forms.TextInput(attrs={"class": "form-input"}),
            "tribe_name": forms.TextInput(attrs={"class": "form-input"}),
            "slack_channel_id": forms.TextInput(attrs={"class": "form-input"}),
        }
