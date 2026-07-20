"""메타 조회 API (Phase C) 직렬화기.

PRD §6.5 + §5 의 메타 엔드포인트용. Folder / SubFolder / Gate / Suite / Job 의
조회 응답을 만들고, 필요 시 `?include=` 파라미터로 latest_run / test_suites 등을
얇게 붙인다.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.qualitygates.models import (
    QualityGate,
    QualityGateFolder,
    QualityGateRun,
    QualityGateSubFolder,
    QualityGateTestSuite,
)
from apps.suites.models import TestSuite, TestSuiteJob


# ---------------------------------------------------------------------------
# Folder / SubFolder
# ---------------------------------------------------------------------------

class FolderListSerializer(serializers.ModelSerializer):
    """`/api/v2/folders/` 목록 응답용 — 가벼운 요약."""

    gate_count = serializers.IntegerField(read_only=True)
    subfolder_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = QualityGateFolder
        fields = (
            "id",
            "name",
            "description",
            "gate_count",
            "subfolder_count",
            "created_at",
            "updated_at",
        )


class FolderDetailSerializer(FolderListSerializer):
    """상세 — 현재는 목록과 동일 필드. 필요 시 통합 정보 추가 (예: Jenkins URL 일부)."""

    dev_jenkins_url = serializers.URLField(read_only=True)
    prod_jenkins_url = serializers.URLField(read_only=True)

    class Meta(FolderListSerializer.Meta):
        fields = FolderListSerializer.Meta.fields + (
            "dev_jenkins_url",
            "prod_jenkins_url",
        )


class SubFolderListSerializer(serializers.ModelSerializer):
    gate_count = serializers.IntegerField(read_only=True)
    folder_id = serializers.UUIDField(read_only=True)
    folder_name = serializers.CharField(source="folder.name", read_only=True)

    class Meta:
        model = QualityGateSubFolder
        fields = (
            "id",
            "folder_id",
            "folder_name",
            "name",
            "description",
            "gate_count",
            "created_at",
            "updated_at",
        )


class GateMiniSerializer(serializers.ModelSerializer):
    """SubFolder 상세 안에 묶어 보내는 가벼운 Gate 표현."""

    class Meta:
        model = QualityGate
        fields = ("id", "name", "gate_type", "is_active")


class SubFolderDetailSerializer(SubFolderListSerializer):
    gates = GateMiniSerializer(many=True, read_only=True)

    class Meta(SubFolderListSerializer.Meta):
        fields = SubFolderListSerializer.Meta.fields + ("gates",)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class GateRunSummarySerializer(serializers.ModelSerializer):
    """`?include=latest_run` 일 때 Gate 응답에 끼워 넣는 최신 Run 요약."""

    result = serializers.CharField(source="result_label", read_only=True, allow_null=True)

    class Meta:
        model = QualityGateRun
        fields = ("id", "seq_no", "status", "result", "created_at")


class GateTestSuiteLinkSerializer(serializers.ModelSerializer):
    """`?include=test_suites` 일 때 노출. 연결된 Suite 의 이름/순서 표시."""

    suite_id = serializers.UUIDField(source="test_suite.id", read_only=True)
    name = serializers.CharField(source="test_suite.name", read_only=True)

    class Meta:
        model = QualityGateTestSuite
        fields = ("suite_id", "name", "order")


class GateListSerializer(serializers.ModelSerializer):
    """`/api/v2/gates/` 목록 — 필터 결과의 한 줄짜리 표현."""

    folder_id = serializers.UUIDField(allow_null=True, read_only=True)
    subfolder_id = serializers.UUIDField(allow_null=True, read_only=True)
    folder_name = serializers.CharField(
        source="folder.name", read_only=True, allow_null=True, default=None,
    )
    subfolder_name = serializers.CharField(
        source="subfolder.name", read_only=True, allow_null=True, default=None,
    )

    class Meta:
        model = QualityGate
        fields = (
            "id",
            "name",
            "gate_type",
            "is_active",
            "auto_mode",
            "parallel_test",
            "folder_id",
            "folder_name",
            "subfolder_id",
            "subfolder_name",
            "has_image_build",
            "has_prod_deploy",
            "created_at",
            "updated_at",
        )


class GateDetailSerializer(GateListSerializer):
    """단건 + ?include=latest_run,test_suites 옵션 처리."""

    deploy_job_path = serializers.CharField(read_only=True)
    image_job_path = serializers.CharField(read_only=True)
    prod_job_path = serializers.CharField(read_only=True)
    deploy_jenkins_url = serializers.CharField(read_only=True)
    prod_jenkins_url = serializers.CharField(read_only=True)
    deploy_argocd_app_url = serializers.CharField(read_only=True)
    deploy_argocd_env = serializers.CharField(read_only=True)
    prod_argocd_app_url = serializers.CharField(read_only=True)
    prod_argocd_env = serializers.CharField(read_only=True)

    latest_run = serializers.SerializerMethodField()
    test_suites = serializers.SerializerMethodField()

    class Meta(GateListSerializer.Meta):
        fields = GateListSerializer.Meta.fields + (
            "deploy_job_path",
            "image_job_path",
            "prod_job_path",
            "deploy_jenkins_url",
            "prod_jenkins_url",
            "deploy_argocd_app_url",
            "deploy_argocd_env",
            "prod_argocd_app_url",
            "prod_argocd_env",
            "latest_run",
            "test_suites",
        )

    def _includes(self) -> set:
        request = self.context.get("request") if self.context else None
        if request is None:
            return set()
        raw = request.query_params.get("include", "") if hasattr(request, "query_params") else ""
        return {tok.strip() for tok in (raw or "").split(",") if tok.strip()}

    def get_latest_run(self, obj):
        if "latest_run" not in self._includes():
            return None
        run = obj.latest_run
        if run is None:
            return None
        return GateRunSummarySerializer(run).data

    def get_test_suites(self, obj):
        if "test_suites" not in self._includes():
            return None
        links = obj.gate_suites.select_related("test_suite").order_by("order")
        return GateTestSuiteLinkSerializer(links, many=True).data


# ---------------------------------------------------------------------------
# Params 응답 (gate_id/params/?stage=...)
# ---------------------------------------------------------------------------

class ParamsStageSerializer(serializers.Serializer):
    """`/gates/{id}/params/?stage=...` 응답 형식."""

    stage = serializers.CharField()
    cached = serializers.BooleanField()
    parameters = serializers.ListField(child=serializers.DictField())


# ---------------------------------------------------------------------------
# TestSuite
# ---------------------------------------------------------------------------

class SuiteJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestSuiteJob
        fields = (
            "id",
            "job_name",
            "job_full_path",
            "total_stages",
            "is_target",
            "is_active",
            "order",
        )


class SuiteListSerializer(serializers.ModelSerializer):
    service_id = serializers.UUIDField(allow_null=True, read_only=True)
    service_name = serializers.CharField(
        source="service.name", read_only=True, allow_null=True, default=None,
    )
    job_count = serializers.IntegerField(read_only=True)
    is_external = serializers.BooleanField(read_only=True)

    class Meta:
        model = TestSuite
        fields = (
            "id",
            "name",
            "jenkins_folder_path",
            "jenkins_folder_url",
            "is_external",
            "service_id",
            "service_name",
            "job_count",
            "is_active",
            "created_at",
            "updated_at",
        )


class SuiteDetailSerializer(SuiteListSerializer):
    jobs = SuiteJobSerializer(many=True, read_only=True)

    class Meta(SuiteListSerializer.Meta):
        fields = SuiteListSerializer.Meta.fields + ("jobs",)
