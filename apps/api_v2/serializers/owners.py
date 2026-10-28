"""담당자 관리(API 엔드포인트 담당자 매핑) 직렬화 — `/api/v2/owners/`."""

from __future__ import annotations

from rest_framework import serializers

from apps.integrations.models import ApiEndpointOwner

ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


class ApiEndpointOwnerSerializer(serializers.ModelSerializer):
    """담당자 row. `(method, endpoint)` 중복은 뷰에서 409 로 처리하므로 기본 unique validator 는 끈다."""

    method = serializers.CharField(
        max_length=10,
        help_text="HTTP Method. 대소문자 무관(저장 시 대문자로 통일). GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS.",
    )
    endpoint = serializers.CharField(
        max_length=500,
        help_text="엔드포인트 경로. 예: `/member/users/edit-by-pop-up` 또는 `${BFF_COMPANY_URL}/recruit/list`. "
        "`(method, endpoint)` 조합은 유일해야 합니다.",
    )
    repo = serializers.CharField(max_length=32, help_text="레포 구분. 예: `JK PC`, `JK Mobile`, `AM PC`, `AM Mobile`.")

    class Meta:
        model = ApiEndpointOwner
        fields = [
            "id", "repo", "method", "endpoint", "category", "area", "mapped_app",
            "fe_owner", "be_owner", "usage_count", "step_ids", "notes",
            "is_imported", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "is_imported", "created_at", "updated_at"]
        # 선택 문자열 필드는 생략·빈 문자열·null 모두 허용(null 은 빈 값으로 저장) — 담당자 미정 등록용.
        extra_kwargs = {
            "category": {"required": False, "allow_null": True, "help_text": "카테고리(선택)."},
            "area": {"required": False, "allow_null": True, "help_text": "영역(선택)."},
            "mapped_app": {"required": False, "allow_null": True, "help_text": "매핑 앱(선택)."},
            "fe_owner": {
                "required": False, "allow_null": True,
                "help_text": "FE 담당자 (4xx 실패 시 멘션, **선택 — 미정이면 생략/빈 값**). 여러 명은 콤마로 구분. Slack 표시명과 매칭됩니다.",
            },
            "be_owner": {
                "required": False, "allow_null": True,
                "help_text": "BE 담당자 (5xx 실패 시 멘션, **선택 — 미정이면 생략/빈 값**). 여러 명은 콤마로 구분.",
            },
            "usage_count": {"required": False, "help_text": "사용 횟수(선택, 기본 0)."},
            "step_ids": {"required": False, "allow_null": True, "help_text": "Step ID 목록(선택, 콤마 구분)."},
            "notes": {"required": False, "allow_null": True, "help_text": "비고(선택)."},
        }
        validators = []

    def validate_method(self, value):
        value = (value or "").strip().upper()
        if value not in ALLOWED_METHODS:
            raise serializers.ValidationError(f"method 는 {', '.join(ALLOWED_METHODS)} 중 하나여야 합니다.")
        return value

    def validate_endpoint(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("endpoint 는 비울 수 없습니다.")
        return value

    def validate_repo(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("repo 는 비울 수 없습니다.")
        return value

    def validate_usage_count(self, value):
        return value or 0

    def to_internal_value(self, data):
        out = super().to_internal_value(data)
        # null → 빈 문자열 (모델 CharField/TextField 는 NULL 불가)
        for f in ("category", "area", "mapped_app", "fe_owner", "be_owner", "step_ids", "notes"):
            if f in out and out[f] is None:
                out[f] = ""
        return out
