from rest_framework import serializers
from apps.services.models import Service, Environment
from apps.gates.models import GateRun, GateFailure, GatePolicy


class ServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = [
            "id", "name", "code", "tribe_name",
            "slack_channel_id", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class EnvironmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Environment
        fields = [
            "id", "name", "trigger_type", "gate_mode", "next_env_control",
        ]


class GateFailureSerializer(serializers.ModelSerializer):
    categorized_by_username = serializers.CharField(
        source="categorized_by.username", read_only=True, default=None,
    )

    class Meta:
        model = GateFailure
        fields = [
            "id", "test_name", "failure_type", "error_message",
            "category", "categorized_by_username", "categorized_at", "created_at",
        ]


class GateRunListSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source="service.name", read_only=True)
    service_code = serializers.CharField(source="service.code", read_only=True)
    environment_name = serializers.CharField(source="environment.name", read_only=True)
    failure_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = GateRun
        fields = [
            "id", "service_name", "service_code", "environment_name",
            "version", "commit_sha", "branch", "result",
            "api_test_result", "e2e_test_result", "duration_seconds",
            "triggered_by", "triggered_at", "completed_at", "created_at",
            "failure_count",
        ]


class GateRunDetailSerializer(serializers.ModelSerializer):
    service = ServiceSerializer(read_only=True)
    environment = EnvironmentSerializer(read_only=True)
    failures = GateFailureSerializer(many=True, read_only=True)

    class Meta:
        model = GateRun
        fields = [
            "id", "service", "environment",
            "version", "commit_sha", "branch", "result",
            "api_test_result", "e2e_test_result", "duration_seconds",
            "jenkins_build_url", "report_url",
            "triggered_by", "triggered_at", "completed_at", "created_at",
            "failures",
        ]


class GatePolicySerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source="service.name", read_only=True)
    environment_name = serializers.CharField(source="environment.name", read_only=True)

    class Meta:
        model = GatePolicy
        fields = [
            "id", "service", "service_name", "environment", "environment_name",
            "test_type", "blocking_mode", "pass_threshold",
            "latency_threshold_ms", "error_rate_threshold",
            "updated_at",
        ]
        read_only_fields = ["id", "updated_at"]


class FailureCategorizeSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=GateFailure.Category.choices)
