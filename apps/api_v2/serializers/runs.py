"""Quality Gate Run 직렬화기 — PRD §6.1 / §6.2.

원칙:
- 직렬화기 안에서 추가 쿼리를 발생시키지 않는다. view 가 select_related/prefetch_related 로
  필요한 관계를 한 번에 끌어온다.
- 시간은 ISO 8601 (Django 의 DateTimeField 직렬화 — `TIME_ZONE=Asia/Seoul, USE_TZ=True` 이므로
  `+09:00` 표기가 그대로 붙는다).
"""

from __future__ import annotations

from typing import Optional

from django.urls import reverse
from rest_framework import serializers

from apps.qualitygates.models import QualityGateRun, QualityGateRunTest

from .. import phase as _phase


# =============================================
# 공통 헬퍼 — Run 내부 값 → dict
# =============================================

def _gate_dict(gate) -> dict:
    folder = getattr(gate, "folder", None)
    subfolder = getattr(gate, "subfolder", None)
    return {
        "id": str(gate.pk),
        "name": gate.name,
        "gate_type": gate.gate_type,
        "folder": folder.name if folder else None,
        "subfolder": subfolder.name if subfolder else None,
    }


def _user_dict(user) -> Optional[dict]:
    if user is None:
        return None
    return {
        "id": str(user.pk),
        "username": getattr(user, "username", ""),
    }


def _argocd_dict(run, stage: str) -> Optional[dict]:
    """stage ∈ ('deploy', 'prod'). Argo CD 사용 안 하면 None."""
    sync = getattr(run, f"{stage}_argocd_sync_status", "") or ""
    health = getattr(run, f"{stage}_argocd_health_status", "") or ""
    phase = getattr(run, f"{stage}_argocd_operation_phase", "") or ""
    synced_at = getattr(run, f"{stage}_argocd_synced_at", None)
    if not (sync or health or phase or synced_at):
        return None
    badge = (
        run.deploy_argocd_badge if stage == "deploy" else run.prod_argocd_badge
    )
    badge_dict = None
    if badge:
        # classify_argocd_status() returns a named-tuple-ish or dict; normalize.
        if hasattr(badge, "_asdict"):
            badge_dict = dict(badge._asdict())
        elif isinstance(badge, dict):
            badge_dict = dict(badge)
        else:
            badge_dict = {
                "kind": getattr(badge, "kind", None),
                "level": getattr(badge, "level", None),
                "label": getattr(badge, "label", None),
            }
    return {
        "sync": sync or None,
        "health": health or None,
        "phase": phase or None,
        "synced_at": synced_at.isoformat() if synced_at else None,
        "badge": badge_dict,
    }


def _image_dict(run) -> dict:
    present = run.image_stage_present
    if not present:
        return {"present": False}
    duration = run.image_duration_ms
    return {
        "present": True,
        "build_number": run.image_build_number,
        "build_url": run.image_build_url or None,
        "started_at": run.image_started_at.isoformat() if run.image_started_at else None,
        "completed_at": run.image_completed_at.isoformat() if run.image_completed_at else None,
        "duration_ms": duration,
        "git_branch": run.image_git_branch or None,
        "git_commit": run.image_git_commit or None,
        "current_stage": run.image_current_stage or None,
    }


def _deploy_dict(run) -> Optional[dict]:
    if not (run.deploy_started_at or run.deploy_build_number):
        return None
    return {
        "build_number": run.deploy_build_number,
        "build_url": run.deploy_build_url or None,
        "started_at": run.deploy_started_at.isoformat() if run.deploy_started_at else None,
        "completed_at": run.deploy_completed_at.isoformat() if run.deploy_completed_at else None,
        "duration_ms": run.deploy_duration_ms,
        "current_stage": run.deploy_current_stage or None,
        "argocd": _argocd_dict(run, "deploy"),
    }


def _prod_dict(run) -> Optional[dict]:
    if not (run.prod_started_at or run.prod_build_number):
        return None
    return {
        "build_number": run.prod_build_number,
        "build_url": run.prod_build_url or None,
        "started_at": run.prod_started_at.isoformat() if run.prod_started_at else None,
        "completed_at": run.prod_completed_at.isoformat() if run.prod_completed_at else None,
        "duration_ms": run.prod_duration_ms,
        "current_stage": run.prod_current_stage or None,
        "argocd": _argocd_dict(run, "prod"),
    }


def _test_run_dict(tr: QualityGateRunTest) -> dict:
    # gate_suite__test_suite 는 view 에서 prefetch.
    gate_suite = tr.gate_suite
    suite = gate_suite.test_suite if gate_suite else None
    job_run = tr.job_run
    return {
        "order": tr.order,
        "suite_id": str(suite.pk) if suite else None,
        "suite_name": suite.name if suite else None,
        "status": tr.status,
        "job_run_id": str(job_run.pk) if job_run else None,
        "started_at": tr.started_at.isoformat() if tr.started_at else None,
        "completed_at": tr.completed_at.isoformat() if tr.completed_at else None,
    }


def _console_urls(run) -> dict:
    """이 Run 에 콘솔 데이터가 있는 stage 들의 API 상대 경로. 없는 stage 는 키 자체를 빠뜨림."""
    urls = {}
    if run.image_stage_present or run.image_console_output:
        urls["image"] = reverse("api_v2:run_console", kwargs={"run_id": run.pk, "stage": "image"})
    if run.deploy_build_number or run.deploy_console_output:
        urls["deploy"] = reverse("api_v2:run_console", kwargs={"run_id": run.pk, "stage": "deploy"})
    if run.prod_build_number or run.prod_console_output:
        urls["prod"] = reverse("api_v2:run_console", kwargs={"run_id": run.pk, "stage": "prod"})
    return urls


# =============================================
# Serializer 클래스
# =============================================

class QualityGateRunSummarySerializer(serializers.Serializer):
    """목록용 — 가벼운 필드만 (PRD §6 게이트별 Run 목록)."""

    run_id = serializers.SerializerMethodField()
    seq_no = serializers.IntegerField()
    status = serializers.CharField()
    phase = serializers.SerializerMethodField()
    result_label = serializers.CharField(allow_null=True)
    gate = serializers.SerializerMethodField()
    is_terminal = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    started_at = serializers.SerializerMethodField()
    completed_at = serializers.SerializerMethodField()
    triggered_by = serializers.SerializerMethodField()
    self_url = serializers.SerializerMethodField()

    def get_run_id(self, obj):
        return str(obj.pk)

    def get_phase(self, obj):
        return _phase.phase_for_run(obj.status)

    def get_gate(self, obj):
        return _gate_dict(obj.gate)

    def get_started_at(self, obj):
        # 첫 단계 시작 시각 = image 또는 deploy 중 먼저.
        first = obj.image_started_at or obj.deploy_started_at
        return first.isoformat() if first else None

    def get_completed_at(self, obj):
        # terminal 일 때만 의미. prod_completed_at → deploy_completed_at → image_completed_at 순.
        if not obj.is_terminal:
            return None
        last = obj.prod_completed_at or obj.deploy_completed_at or obj.image_completed_at
        return last.isoformat() if last else None

    def get_triggered_by(self, obj):
        return _user_dict(obj.triggered_by)

    def get_self_url(self, obj):
        return reverse("api_v2:run_detail", kwargs={"run_id": obj.pk})


class QualityGateRunDetailSerializer(serializers.Serializer):
    """단건용 — PRD §6.2 응답 스키마."""

    run_id = serializers.SerializerMethodField()
    gate = serializers.SerializerMethodField()
    seq_no = serializers.IntegerField()
    status = serializers.CharField()
    phase = serializers.SerializerMethodField()
    result_label = serializers.CharField(allow_null=True)
    is_terminal = serializers.BooleanField()
    auto_mode = serializers.SerializerMethodField()
    parallel_test = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    deploy = serializers.SerializerMethodField()
    test_runs = serializers.SerializerMethodField()
    prod = serializers.SerializerMethodField()
    console_urls = serializers.SerializerMethodField()
    triggered_by = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    detail_url = serializers.SerializerMethodField()
    self_url = serializers.SerializerMethodField()

    def get_run_id(self, obj):
        return str(obj.pk)

    def get_gate(self, obj):
        return _gate_dict(obj.gate)

    def get_phase(self, obj):
        return _phase.phase_for_run(obj.status)

    def get_auto_mode(self, obj):
        return bool(obj.gate.auto_mode)

    def get_parallel_test(self, obj):
        return bool(obj.gate.parallel_test)

    def get_image(self, obj):
        return _image_dict(obj)

    def get_deploy(self, obj):
        return _deploy_dict(obj)

    def get_test_runs(self, obj):
        # view 가 prefetch_related("test_runs__gate_suite__test_suite") 함.
        return [_test_run_dict(tr) for tr in obj.test_runs.all()]

    def get_prod(self, obj):
        return _prod_dict(obj)

    def get_console_urls(self, obj):
        return _console_urls(obj)

    def get_triggered_by(self, obj):
        return _user_dict(obj.triggered_by)

    def get_detail_url(self, obj):
        return reverse("qualitygates:run_detail", kwargs={"pk": obj.pk})

    def get_self_url(self, obj):
        return reverse("api_v2:run_detail", kwargs={"run_id": obj.pk})
