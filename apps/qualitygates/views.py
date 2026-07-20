import hashlib
import logging
import re
from urllib.parse import unquote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.activitylog.models import ActivityLog
from apps.integrations.jenkins import JenkinsClient, JenkinsError
from apps.integrations.argocd import ArgoCDClient, ArgoCDError, normalize_app_url, parse_app_url
from apps.integrations.log_errors import extract_error_log
from apps.notifications import events as nev
from apps.notifications.services import send_event_async
from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJobRun
from . import services
from .models import (
    QualityGate, QualityGateFolder, QualityGateSubFolder, QualityGateTestSuite,
    QualityGateRun, QualityGateRunTest,
)


def _gate_log_label(gate) -> str:
    """ActivityLog 의 target_name 으로 쓰는 '인스턴스 / 하위폴더 / 게이트명' 라벨."""
    parts = []
    if gate.subfolder_id:
        parts.append(gate.subfolder.folder.name)
        parts.append(gate.subfolder.name)
    elif gate.folder_id:
        parts.append(gate.folder.name)
    parts.append(gate.name)
    return " / ".join(parts)

logger = logging.getLogger(__name__)


def _credentials_for_gate_type(gate_type: str) -> tuple[str, str]:
    """게이트 타입별 (deploy_credential, prod_credential) 고정값."""
    if gate_type == QualityGate.GateType.STG_PRD:
        return QualityGate.CredentialSource.DEV, QualityGate.CredentialSource.PROD
    return QualityGate.CredentialSource.DEV, QualityGate.CredentialSource.DEV


def _argocd_envs_for_gate_type(gate_type: str) -> tuple[str, str]:
    """게이트 타입별 (deploy_argocd_env, prod_argocd_env) 고정값."""
    if gate_type == QualityGate.GateType.STG_PRD:
        return QualityGate.ArgoEnv.STG, QualityGate.ArgoEnv.PRD
    return QualityGate.ArgoEnv.DEV, QualityGate.ArgoEnv.STG


def _parse_jenkins_input(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if "/job/" in raw:
        match = re.search(r"/job/(.+?)(?:\?|$)", raw)
        if match:
            job_part = match.group(1).rstrip("/")
            parts = [unquote(p) for p in job_part.split("/job/")]
            return "/".join(parts)
    return raw


def _compute_section_counts(run, test_runs):
    """파이프라인 섹션별 진행 카운트 (done/total) 계산.

    image 단계는 'gate.has_image_build' 가 아니라 'run.image_stage_present' 로 판단 —
    같은 게이트라도 사용자가 '이미지 빌드 없이 실행하기' 를 고른 Run 은 이미지
    단계 카운트에서 제외해야 한다.

    test 단계 total 은 Run 시점에 fix 된 `run.total_suites` 가 아니라 현재 살아 있는
    `test_runs` 갯수로 계산 — 게이트에서 suite 가 빠지면 cascade 로 test_runs 가 줄어
    `(N-1)/N` 같은 stale 표시를 방지. 동시에 모델 필드도 동기화해 admin 표시도 일치시킴.
    """
    gate = run.gate
    has_image = run.image_stage_present
    sec1_total = (1 if has_image else 0) + 1
    sec1_done = 0
    if has_image and run.image_completed_at:
        sec1_done += 1
    if run.deploy_completed_at:
        sec1_done += 1

    sec2_total = len(test_runs)
    sec2_done = sum(1 for tr in test_runs if tr.status in ("passed", "failed"))

    sync_fields = []
    if run.total_suites != sec2_total:
        run.total_suites = sec2_total
        sync_fields.append("total_suites")
    # current_suite_index 도 게이트 suite 변경 후 stale 해질 수 있다 (병렬 모드는 done
    # 카운트, 순차 모드는 진행 중 인덱스). sec2_total 을 넘는 값이면 sec2_done 으로 보정.
    if run.current_suite_index > sec2_total or (
        gate.parallel_test and run.current_suite_index != sec2_done
    ):
        run.current_suite_index = sec2_done
        sync_fields.append("current_suite_index")
    if sync_fields:
        run.save(update_fields=sync_fields)

    sec3_total = 1 if gate.has_prod_deploy else 0
    sec3_done = 1 if gate.has_prod_deploy and run.status == "completed" else 0

    return {
        "sec1_total": sec1_total, "sec1_done": sec1_done,
        "sec2_total": sec2_total, "sec2_done": sec2_done,
        "sec3_total": sec3_total, "sec3_done": sec3_done,
    }


def _split_jenkins_url(raw: str) -> tuple[str, str]:
    """Job URL을 (base_url, job_path)로 분리. 순수 경로만 들어오면 base_url은 빈 문자열."""
    raw = raw.strip().rstrip("/")
    if not raw:
        return "", ""
    idx = raw.find("/job/")
    if idx == -1:
        return "", raw
    base = raw[:idx]
    job_part = raw[idx + len("/job/"):].split("?")[0].rstrip("/")
    parts = [unquote(p) for p in job_part.split("/job/")]
    return base, "/".join(parts)


# =============================================
# Folder CRUD
# =============================================

@login_required
def folder_list(request):
    folders = QualityGateFolder.objects.prefetch_related("gates").order_by("name")
    return render(request, "qualitygates/folder_list.html", {"folders": folders})


@login_required
def folder_register(request):
    if not request.user.is_admin:
        return redirect("qualitygates:folder_list")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        errors = []
        if not name:
            errors.append("인스턴스명을 입력하세요.")
        elif QualityGateFolder.objects.filter(name=name).exists():
            errors.append("이미 존재하는 인스턴스명입니다.")

        if errors:
            return render(request, "qualitygates/folder_form.html", {
                "errors": errors, "form_data": request.POST, "mode": "register",
            })

        folder = QualityGateFolder.objects.create(
            name=name,
            description=request.POST.get("description", "").strip(),
            dev_jenkins_url=request.POST.get("dev_jenkins_url", "").strip(),
            dev_jenkins_user=request.POST.get("dev_jenkins_user", "").strip(),
            dev_jenkins_token=request.POST.get("dev_jenkins_token", "").strip(),
            prod_jenkins_url=request.POST.get("prod_jenkins_url", "").strip(),
            prod_jenkins_user=request.POST.get("prod_jenkins_user", "").strip(),
            prod_jenkins_token=request.POST.get("prod_jenkins_token", "").strip(),
            dev_argocd_token=request.POST.get("dev_argocd_token", "").strip(),
            stg_argocd_token=request.POST.get("stg_argocd_token", "").strip(),
            prd_argocd_token=request.POST.get("prd_argocd_token", "").strip(),
        )
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.CREATE,
            target_type=ActivityLog.TargetType.GATE_FOLDER, target=folder,
        )
        return redirect("qualitygates:folder_detail", pk=folder.pk)

    return render(request, "qualitygates/folder_form.html", {"mode": "register"})


@login_required
def folder_detail(request, pk):
    folder = get_object_or_404(QualityGateFolder, pk=pk)
    subfolders = folder.subfolders.all().prefetch_related("gates")
    return render(request, "qualitygates/folder_detail.html", {
        "folder": folder, "subfolders": subfolders,
    })


@login_required
def folder_edit(request, pk):
    folder = get_object_or_404(QualityGateFolder, pk=pk)
    if not request.user.is_admin:
        return redirect("qualitygates:folder_detail", pk=folder.pk)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        errors = []
        if not name:
            errors.append("인스턴스명을 입력하세요.")
        elif QualityGateFolder.objects.exclude(pk=folder.pk).filter(name=name).exists():
            errors.append("이미 존재하는 인스턴스명입니다.")

        if errors:
            return render(request, "qualitygates/folder_form.html", {
                "folder": folder, "errors": errors, "mode": "edit",
            })

        folder.name = name
        folder.description = request.POST.get("description", "").strip()
        folder.dev_jenkins_url = request.POST.get("dev_jenkins_url", "").strip()
        folder.dev_jenkins_user = request.POST.get("dev_jenkins_user", "").strip()
        new_dev_token = request.POST.get("dev_jenkins_token", "").strip()
        if new_dev_token:
            folder.dev_jenkins_token = new_dev_token
        folder.prod_jenkins_url = request.POST.get("prod_jenkins_url", "").strip()
        folder.prod_jenkins_user = request.POST.get("prod_jenkins_user", "").strip()
        new_prod_token = request.POST.get("prod_jenkins_token", "").strip()
        if new_prod_token:
            folder.prod_jenkins_token = new_prod_token
        for env in ("dev", "stg", "prd"):
            new_token = request.POST.get(f"{env}_argocd_token", "").strip()
            if new_token:
                setattr(folder, f"{env}_argocd_token", new_token)
        folder.save()
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.GATE_FOLDER, target=folder,
        )
        return redirect("qualitygates:folder_detail", pk=folder.pk)

    return render(request, "qualitygates/folder_form.html", {
        "folder": folder, "mode": "edit",
    })


@login_required
def folder_delete(request, pk):
    folder = get_object_or_404(QualityGateFolder, pk=pk)
    if not request.user.is_admin:
        return redirect("qualitygates:folder_detail", pk=folder.pk)

    if request.method == "POST":
        confirm_name = request.POST.get("confirm_name", "").strip()
        if confirm_name != folder.name:
            return render(request, "qualitygates/folder_delete_confirm.html", {
                "folder": folder,
                "error": "입력한 인스턴스명이 일치하지 않습니다.",
            })
        folder_name = folder.name
        folder_id = str(folder.pk)
        folder.delete()
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.DELETE,
            target_type=ActivityLog.TargetType.GATE_FOLDER,
            target_name=folder_name, detail=f"id={folder_id}",
        )
        return redirect("qualitygates:folder_list")

    return render(request, "qualitygates/folder_delete_confirm.html", {"folder": folder})


# =============================================
# SubFolder CRUD
# =============================================

@login_required
def subfolder_register(request, folder_pk):
    folder = get_object_or_404(QualityGateFolder, pk=folder_pk)
    if not request.user.is_admin:
        return redirect("qualitygates:folder_detail", pk=folder.pk)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        errors = []
        if not name:
            errors.append("하위폴더명을 입력하세요.")
        elif folder.subfolders.filter(name=name).exists():
            errors.append("이미 존재하는 하위폴더명입니다.")

        if errors:
            return render(request, "qualitygates/subfolder_form.html", {
                "folder": folder, "errors": errors,
                "form_data": request.POST, "mode": "register",
            })

        sub = QualityGateSubFolder.objects.create(
            folder=folder,
            name=name,
            description=request.POST.get("description", "").strip(),
        )
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.CREATE,
            target_type=ActivityLog.TargetType.GATE_FOLDER, target=sub,
            target_name=f"{folder.name} / {sub.name}",
            detail="subfolder",
        )
        return redirect("qualitygates:subfolder_detail", pk=sub.pk)

    return render(request, "qualitygates/subfolder_form.html", {
        "folder": folder, "mode": "register",
    })


@login_required
def subfolder_detail(request, pk):
    sub = get_object_or_404(
        QualityGateSubFolder.objects.select_related("folder"), pk=pk,
    )
    gates = sub.gates.filter(is_active=True).prefetch_related("gate_suites__test_suite")
    return render(request, "qualitygates/subfolder_detail.html", {
        "subfolder": sub, "folder": sub.folder, "gates": gates,
    })


@login_required
def subfolder_edit(request, pk):
    sub = get_object_or_404(
        QualityGateSubFolder.objects.select_related("folder"), pk=pk,
    )
    if not request.user.is_admin:
        return redirect("qualitygates:subfolder_detail", pk=sub.pk)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        errors = []
        if not name:
            errors.append("하위폴더명을 입력하세요.")
        elif sub.folder.subfolders.exclude(pk=sub.pk).filter(name=name).exists():
            errors.append("이미 존재하는 하위폴더명입니다.")

        if errors:
            return render(request, "qualitygates/subfolder_form.html", {
                "folder": sub.folder, "subfolder": sub,
                "errors": errors, "mode": "edit",
            })

        sub.name = name
        sub.description = request.POST.get("description", "").strip()
        sub.save(update_fields=["name", "description", "updated_at"])
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.GATE_FOLDER, target=sub,
            target_name=f"{sub.folder.name} / {sub.name}",
            detail="subfolder",
        )
        return redirect("qualitygates:subfolder_detail", pk=sub.pk)

    return render(request, "qualitygates/subfolder_form.html", {
        "folder": sub.folder, "subfolder": sub, "mode": "edit",
    })


@login_required
def subfolder_delete(request, pk):
    sub = get_object_or_404(
        QualityGateSubFolder.objects.select_related("folder"), pk=pk,
    )
    if not request.user.is_admin:
        return redirect("qualitygates:subfolder_detail", pk=sub.pk)

    if request.method == "POST":
        confirm_name = request.POST.get("confirm_name", "").strip()
        if confirm_name != sub.name:
            return render(request, "qualitygates/subfolder_delete_confirm.html", {
                "subfolder": sub, "folder": sub.folder,
                "error": "입력한 하위폴더명이 일치하지 않습니다.",
            })
        folder_pk = sub.folder.pk
        label = f"{sub.folder.name} / {sub.name}"
        sub_id = str(sub.pk)
        sub.delete()
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.DELETE,
            target_type=ActivityLog.TargetType.GATE_FOLDER,
            target_name=label, detail=f"subfolder id={sub_id}",
        )
        return redirect("qualitygates:folder_detail", pk=folder_pk)

    return render(request, "qualitygates/subfolder_delete_confirm.html", {
        "subfolder": sub, "folder": sub.folder,
    })


# =============================================
# Gate CRUD
# =============================================

@login_required
def gate_register(request, subfolder_pk):
    subfolder = get_object_or_404(
        QualityGateSubFolder.objects.select_related("folder"), pk=subfolder_pk,
    )
    folder = subfolder.folder
    suites = TestSuite.objects.filter(is_active=True)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        deploy_job_raw = request.POST.get("deploy_job_path", "").strip()
        image_job_raw = request.POST.get("image_job_path", "").strip()
        prod_job_raw = request.POST.get("prod_job_path", "").strip()
        suite_ids = request.POST.getlist("test_suite_ids")
        gate_type = request.POST.get("gate_type", QualityGate.GateType.DEV_STG)
        if gate_type not in QualityGate.GateType.values:
            gate_type = QualityGate.GateType.DEV_STG
        deploy_cred, prod_cred = _credentials_for_gate_type(gate_type)

        deploy_base, deploy_job_path = _split_jenkins_url(deploy_job_raw) if deploy_job_raw else ("", "")
        image_base, image_job_path = _split_jenkins_url(image_job_raw) if image_job_raw else ("", "")
        prod_base, prod_job_path = _split_jenkins_url(prod_job_raw) if prod_job_raw else ("", "")

        # 이미지 Job 은 deploy 그룹과 동일한 Jenkins 서버를 가정. base 가 따로 입력됐다면 우선 사용.
        deploy_base_url = image_base or deploy_base

        # 선택한 인증키에 맞는 user/token. URL 은 입력값이 있으면 그걸, 없으면 folder 쪽으로 폴백.
        deploy_user = folder.prod_jenkins_user if deploy_cred == QualityGate.CredentialSource.PROD else folder.dev_jenkins_user
        deploy_token = folder.prod_jenkins_token if deploy_cred == QualityGate.CredentialSource.PROD else folder.dev_jenkins_token
        deploy_url_effective = deploy_base_url or (
            folder.prod_jenkins_url if deploy_cred == QualityGate.CredentialSource.PROD else folder.dev_jenkins_url
        )
        deploy_cred_label = "Prod" if deploy_cred == QualityGate.CredentialSource.PROD else "Dev"
        prod_user = folder.prod_jenkins_user if prod_cred == QualityGate.CredentialSource.PROD else folder.dev_jenkins_user
        prod_token = folder.prod_jenkins_token if prod_cred == QualityGate.CredentialSource.PROD else folder.dev_jenkins_token
        prod_url_effective = prod_base or (
            folder.prod_jenkins_url if prod_cred == QualityGate.CredentialSource.PROD else folder.dev_jenkins_url
        )
        prod_cred_label = "Prod" if prod_cred == QualityGate.CredentialSource.PROD else "Dev"

        errors = []
        if not name:
            errors.append("게이트명을 입력하세요.")
        if not deploy_job_raw:
            errors.append("검수대상 환경 배포 Job URL을 입력하세요.")
        elif not deploy_job_path:
            errors.append("올바른 Jenkins Job URL을 입력하세요.")
        if not deploy_url_effective or not deploy_token:
            errors.append(f"검수대상 Job 의 Jenkins URL/토큰 정보가 부족합니다. (인증키: {deploy_cred_label})")
        if image_job_raw and not image_job_path:
            errors.append("올바른 이미지 Job URL을 입력하세요.")
        if prod_job_raw and not prod_job_path:
            errors.append("올바른 검수통과 환경 배포 Job URL을 입력하세요.")
        if prod_job_path and (not prod_url_effective or not prod_token):
            errors.append(f"검수통과 Job 의 Jenkins URL/토큰 정보가 부족합니다. (인증키: {prod_cred_label})")
        if not suite_ids:
            errors.append("테스트 Suite를 1개 이상 선택하세요.")

        ctx_base = {
            "folder": folder, "subfolder": subfolder, "suites": suites,
            "selected_deploy_credential": deploy_cred,
            "selected_prod_credential": prod_cred,
            "selected_gate_type": gate_type,
        }

        if errors:
            return render(request, "qualitygates/gate_register.html", {**ctx_base, "errors": errors})

        try:
            deploy_client = JenkinsClient(deploy_url_effective, deploy_user, deploy_token)
            job_info = deploy_client.get_job_info(deploy_job_path)
            if not job_info.get("buildable"):
                return render(request, "qualitygates/gate_register.html", {
                    **ctx_base, "errors": [f"'{deploy_job_path}' Job이 비활성화 상태입니다."],
                })
            if image_job_path:
                image_info = deploy_client.get_job_info(image_job_path)
                if not image_info.get("buildable"):
                    return render(request, "qualitygates/gate_register.html", {
                        **ctx_base, "errors": [f"'{image_job_path}' 이미지 Job이 비활성화 상태입니다."],
                    })
        except JenkinsError as e:
            return render(request, "qualitygates/gate_register.html", {
                **ctx_base, "errors": [f"{deploy_cred_label} Jenkins 연결 실패: {e}"],
            })

        deploy_argo_env, prod_argo_env = _argocd_envs_for_gate_type(gate_type)

        gate = QualityGate.objects.create(
            folder=folder,
            subfolder=subfolder,
            name=name,
            gate_type=gate_type,
            image_job_path=image_job_path,
            deploy_job_path=deploy_job_path,
            deploy_jenkins_base_url=deploy_base_url,
            deploy_credential=deploy_cred,
            deploy_argocd_app_url=normalize_app_url(request.POST.get("deploy_argocd_app_url", "")),
            deploy_argocd_env=deploy_argo_env,
            prod_job_path=prod_job_path,
            prod_jenkins_base_url=prod_base,
            prod_credential=prod_cred,
            prod_argocd_app_url=normalize_app_url(request.POST.get("prod_argocd_app_url", "")),
            prod_argocd_env=prod_argo_env,
        )

        for i, sid in enumerate(suite_ids):
            test_suite = TestSuite.objects.get(pk=sid)
            QualityGateTestSuite.objects.create(
                gate=gate, test_suite=test_suite, order=i,
            )

        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.CREATE,
            target_type=ActivityLog.TargetType.GATE, target=gate,
            target_name=_gate_log_label(gate),
            detail=f"type={gate.get_gate_type_display()}",
        )
        return redirect("qualitygates:gate_detail", pk=gate.pk)

    return render(request, "qualitygates/gate_register.html", {
        "folder": folder, "subfolder": subfolder, "suites": suites,
    })


@login_required
def gate_detail(request, pk):
    gate = get_object_or_404(
        QualityGate.objects.prefetch_related("gate_suites__test_suite"), pk=pk,
    )

    # 페이지 로딩 속도 보호를 위해 최신 active Run 1건만 폴링 — 나머지는 run_detail 에서 갱신.
    active_run = gate.runs.filter(status__in=QualityGateRun.ACTIVE_STATUSES).order_by("-created_at").first()
    if active_run:
        _poll_gate_run(active_run)

    runs = gate.runs.prefetch_related("test_runs__gate_suite__test_suite", "test_runs__job_run__job").order_by("-created_at")[:15]
    pending_prod_run = gate.runs.filter(status=QualityGateRun.Status.QG_PASSED).order_by("-created_at").first()
    return render(request, "qualitygates/gate_detail.html", {
        "gate": gate, "runs": runs,
        "pending_prod_run": pending_prod_run,
        "active_run": active_run,
    })


@login_required
def gate_edit(request, pk):
    gate = get_object_or_404(QualityGate, pk=pk)
    suites = TestSuite.objects.filter(is_active=True)

    if not request.user.is_admin:
        return redirect("qualitygates:gate_detail", pk=gate.pk)

    current_suite_ids = list(
        gate.gate_suites.order_by("order").values_list("test_suite_id", flat=True)
    )

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        deploy_job_raw = request.POST.get("deploy_job_path", "").strip()
        image_job_raw = request.POST.get("image_job_path", "").strip()
        prod_job_raw = request.POST.get("prod_job_path", "").strip()
        suite_ids = request.POST.getlist("test_suite_ids")
        # gate_type 은 최초 생성 이후 변경 불가 — POST 값을 무시하고 기존 값을 유지한다.
        gate.deploy_credential, gate.prod_credential = _credentials_for_gate_type(gate.gate_type)

        if name:
            gate.name = name

        deploy_base_entered = ""
        if deploy_job_raw:
            deploy_base_entered, deploy_job_path = _split_jenkins_url(deploy_job_raw)
            if deploy_job_path:
                gate.deploy_job_path = deploy_job_path

        image_base_entered = ""
        if image_job_raw:
            image_base_entered, image_job_path = _split_jenkins_url(image_job_raw)
            gate.image_job_path = image_job_path
        else:
            gate.image_job_path = ""

        # deploy/image 는 같은 Jenkins 서버 공유 — 사용자가 입력한 base URL 이 있으면 그걸 저장.
        new_deploy_base = image_base_entered or deploy_base_entered
        if new_deploy_base or deploy_job_raw or image_job_raw:
            gate.deploy_jenkins_base_url = new_deploy_base

        if prod_job_raw:
            prod_base_entered, prod_job_path = _split_jenkins_url(prod_job_raw)
            gate.prod_job_path = prod_job_path
            gate.prod_jenkins_base_url = prod_base_entered
        else:
            gate.prod_job_path = ""
            gate.prod_jenkins_base_url = ""

        gate.deploy_argocd_app_url = normalize_app_url(request.POST.get("deploy_argocd_app_url", ""))
        gate.prod_argocd_app_url = normalize_app_url(request.POST.get("prod_argocd_app_url", ""))

        gate.deploy_argocd_env, gate.prod_argocd_env = _argocd_envs_for_gate_type(gate.gate_type)

        gate.save()

        if suite_ids:
            # 기존 gate_suites 와 diff 를 맞춘다 — 무조건 all().delete() 하면
            # QualityGateRunTest.gate_suite (CASCADE) 로 과거 Run 의 테스트 결과가 같이 지워짐.
            # POST 값은 문자열, DB 값은 UUID 라 str() 로 키 정규화.
            existing = {str(gs.test_suite_id): gs for gs in gate.gate_suites.all()}
            new_ids = [str(sid) for sid in suite_ids]

            for tid, gs in existing.items():
                if tid not in new_ids:
                    gs.delete()

            for i, sid in enumerate(new_ids):
                if sid in existing:
                    gs = existing[sid]
                    if gs.order != i:
                        gs.order = i
                        gs.save(update_fields=["order"])
                else:
                    test_suite = TestSuite.objects.get(pk=sid)
                    QualityGateTestSuite.objects.create(
                        gate=gate, test_suite=test_suite, order=i,
                    )

        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.UPDATE,
            target_type=ActivityLog.TargetType.GATE, target=gate,
            target_name=_gate_log_label(gate),
        )
        return redirect("qualitygates:gate_detail", pk=gate.pk)

    return render(request, "qualitygates/gate_edit.html", {
        "gate": gate, "suites": suites, "current_suite_ids": current_suite_ids,
    })


@login_required
def gate_toggle_auto(request, pk):
    """auto_mode 토글 — Dev→Stg 게이트에서만 의미 있음. POST 만 허용."""
    gate = get_object_or_404(QualityGate, pk=pk)
    if request.method != "POST":
        return redirect("qualitygates:gate_detail", pk=gate.pk)
    if not request.user.can_write:
        return redirect("qualitygates:gate_detail", pk=gate.pk)
    if gate.gate_type != QualityGate.GateType.DEV_STG:
        return redirect("qualitygates:gate_detail", pk=gate.pk)

    gate.auto_mode = not gate.auto_mode
    gate.save(update_fields=["auto_mode"])
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.UPDATE,
        target_type=ActivityLog.TargetType.GATE, target=gate,
        target_name=_gate_log_label(gate),
        detail=f"auto_mode={'on' if gate.auto_mode else 'off'}",
    )
    return redirect("qualitygates:gate_detail", pk=gate.pk)


@login_required
def gate_toggle_parallel(request, pk):
    """parallel_test 토글 — 모든 Test Suite 를 동시에 실행할지 여부. POST 만 허용."""
    gate = get_object_or_404(QualityGate, pk=pk)
    if request.method != "POST":
        return redirect("qualitygates:gate_detail", pk=gate.pk)
    if not request.user.can_write:
        return redirect("qualitygates:gate_detail", pk=gate.pk)

    gate.parallel_test = not gate.parallel_test
    gate.save(update_fields=["parallel_test"])
    ActivityLog.log(
        user=request.user, action=ActivityLog.Action.UPDATE,
        target_type=ActivityLog.TargetType.GATE, target=gate,
        target_name=_gate_log_label(gate),
        detail=f"parallel_test={'on' if gate.parallel_test else 'off'}",
    )
    return redirect("qualitygates:gate_detail", pk=gate.pk)


@login_required
def gate_delete(request, pk):
    gate = get_object_or_404(QualityGate, pk=pk)
    subfolder_pk = gate.subfolder_id
    folder_pk = gate.folder_id
    if request.method == "POST":
        gate_label = _gate_log_label(gate)
        gate_id = str(gate.pk)
        gate.delete()
        ActivityLog.log(
            user=request.user, action=ActivityLog.Action.DELETE,
            target_type=ActivityLog.TargetType.GATE,
            target_name=gate_label, detail=f"id={gate_id}",
        )
        if subfolder_pk:
            return redirect("qualitygates:subfolder_detail", pk=subfolder_pk)
        if folder_pk:
            return redirect("qualitygates:folder_detail", pk=folder_pk)
        return redirect("qualitygates:folder_list")
    return render(request, "qualitygates/gate_delete_confirm.html", {"gate": gate})


# =============================================
# Run
# =============================================

def _collect_params(post, prefix="param_"):
    """POST 에서 prefix 로 시작하는 값을 {원본이름: 값} 으로 수집."""
    return {k[len(prefix):]: v for k, v in post.items() if k.startswith(prefix) and k != prefix}


@login_required
def gate_run_params(request, pk):
    """Run 전 파라미터 조회. 이미지 Job이 있으면 이미지 Job, 없으면 Deploy Job.

    `?skip_image=1` 이 오면 이미지 빌드를 건너뛰는 모드라 Deploy Job 파라미터 반환.
    Jenkins 호출이 Git Parameter / Active Choices 대응으로 5~15초 걸릴 수 있어
    결과를 120초간 캐싱. `?refresh=1` 이면 캐시 무시하고 재조회.
    """
    gate = get_object_or_404(QualityGate, pk=pk)
    skip_image = request.GET.get("skip_image") == "1"
    use_image_job = gate.has_image_build and not skip_image
    target_job = gate.image_job_path if use_image_job else gate.deploy_job_path

    # 캐시 키는 gate.pk + target_job 조합으로 (Job 경로 바뀌면 자동 분리)
    key_raw = f"qg_params:{gate.pk}:{target_job or ''}"
    cache_key = "qgp:" + hashlib.sha1(key_raw.encode()).hexdigest()

    force_refresh = request.GET.get("refresh") == "1"
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return JsonResponse({"ok": True, "parameters": cached, "cached": True})

    client = JenkinsClient(
        gate.deploy_jenkins_url, gate.deploy_jenkins_user, gate.deploy_jenkins_token,
    )
    try:
        info = client.get_job_info(target_job)
        params = info.get("parameters", [])
        cache.set(cache_key, params, timeout=120)
        return JsonResponse({"ok": True, "parameters": params, "cached": False})
    except JenkinsError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)


@login_required
def gate_run(request, pk):
    """새 Quality Gate Run 생성 + 첫 단계 트리거. 핵심 로직은 services.trigger_gate_run."""
    gate = get_object_or_404(QualityGate, pk=pk)

    params = _collect_params(request.POST)
    skip_image_build = request.POST.get("skip_image_build") == "1"

    try:
        run = services.trigger_gate_run(
            gate=gate,
            user=request.user,
            parameters=params,
            skip_image_build=skip_image_build,
        )
    except services.GateRunError as e:
        # 진행 중 Run 등 — 기존 동작은 단순 redirect 라 messages 없이 그대로.
        if e.code == "active_run_exists":
            return redirect("qualitygates:gate_detail", pk=gate.pk)
        messages.error(request, str(e))
        return redirect("qualitygates:gate_detail", pk=gate.pk)

    return redirect("qualitygates:run_detail", pk=run.pk)


@login_required
def run_detail(request, pk):
    run = get_object_or_404(
        QualityGateRun.objects.select_related("gate")
        .prefetch_related("test_runs__gate_suite__test_suite", "test_runs__job_run__job"),
        pk=pk,
    )
    if run.is_running:
        _poll_gate_run(run)

    test_runs = run.test_runs.select_related("gate_suite__test_suite", "job_run__job").order_by("order")
    step_offset = 1 if run.image_stage_present else 0
    section_counts = _compute_section_counts(run, test_runs)
    return render(request, "qualitygates/run_detail.html", {
        "run": run, "test_runs": test_runs, "step_offset": step_offset,
        **section_counts,
    })


@login_required
def run_status_fragment(request, pk):
    run = get_object_or_404(
        QualityGateRun.objects.select_related("gate")
        .prefetch_related("test_runs__gate_suite__test_suite", "test_runs__job_run__job"),
        pk=pk,
    )
    if run.is_running:
        _poll_gate_run(run)

    test_runs = run.test_runs.select_related("gate_suite__test_suite", "job_run__job").order_by("order")
    step_offset = 1 if run.image_stage_present else 0
    section_counts = _compute_section_counts(run, test_runs)
    return render(request, "qualitygates/_run_status.html", {
        "run": run, "test_runs": test_runs, "step_offset": step_offset,
        **section_counts,
    })


@login_required
def deploy_params(request, pk):
    """검수대상 환경 배포 Job 파라미터 목록 조회 (이미지 빌드 성공 후 모달용)."""
    run = get_object_or_404(QualityGateRun.objects.select_related("gate"), pk=pk)
    gate = run.gate
    client = JenkinsClient(
        gate.deploy_jenkins_url, gate.deploy_jenkins_user, gate.deploy_jenkins_token,
    )
    try:
        info = client.get_job_info(gate.deploy_job_path)
        return JsonResponse({"ok": True, "parameters": info.get("parameters", [])})
    except JenkinsError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)


@login_required
def deploy_trigger(request, pk):
    """검수대상 환경 배포 실행 (WAITING_DEPLOY 또는 DEPLOY_FAILED 재실행).

    핵심 로직은 services.trigger_deploy — retry 시 이전 잔여 필드 정리까지 포함.
    """
    run = get_object_or_404(QualityGateRun, pk=pk)
    params = _collect_params(request.POST)
    try:
        services.trigger_deploy(run=run, user=request.user, parameters=params)
    except services.GateRunError as e:
        if e.code == "invalid_state":
            return redirect("qualitygates:run_detail", pk=run.pk)
        messages.error(request, str(e))
    return redirect("qualitygates:run_detail", pk=run.pk)


@login_required
def prod_deploy_params(request, pk):
    """검수통과 배포 Job 파라미터 목록 조회 (모달용 JSON).

    APP_IMAGE(ImageTagParameter) 는 Jenkins 쪽 default 가 비어 있어서
    <select> 가 무조건 첫 옵션(=가장 최신 태그)을 고른다. 이 Run 의 이미지
    빌드가 실제로 만든 태그를 default 로 오버라이드해서, 모달 기본 선택이
    이 Run 과 일치하도록 한다.
    """
    run = get_object_or_404(QualityGateRun.objects.select_related("gate"), pk=pk)
    gate = run.gate
    if not gate.has_prod_deploy:
        return JsonResponse({"ok": True, "parameters": []})
    client = JenkinsClient(
        gate.prod_jenkins_url, gate.prod_jenkins_user, gate.prod_jenkins_token,
    )
    try:
        info = client.get_job_info(gate.prod_job_path)
        parameters = info.get("parameters", [])

        if run.image_build_number:
            prefix = f"{run.image_build_number}-"
            for p in parameters:
                if p.get("name") != "APP_IMAGE":
                    continue
                for c in (p.get("choices") or []):
                    if isinstance(c, str) and c.startswith(prefix):
                        p["default"] = c
                        break
                break

        return JsonResponse({"ok": True, "parameters": parameters})
    except JenkinsError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)


@login_required
def prod_deploy(request, pk):
    """검수통과 배포 실행 (QG PASS 또는 PROD_DEPLOY_FAILED 재실행). 핵심은 services.trigger_prod."""
    run = get_object_or_404(QualityGateRun, pk=pk)
    params = _collect_params(request.POST)
    try:
        services.trigger_prod(run=run, user=request.user, parameters=params)
    except services.GateRunError as e:
        if e.code == "invalid_state":
            return redirect("qualitygates:run_detail", pk=run.pk)
        messages.error(request, str(e))
    return redirect("qualitygates:run_detail", pk=run.pk)


@login_required
def argocd_retry(request, pk, stage):
    """Argo CD sync 재시도 엔드포인트. 핵심은 services.argocd_retry."""
    run = get_object_or_404(QualityGateRun, pk=pk)
    try:
        services.argocd_retry(run=run, stage=stage, user=request.user)
    except services.GateRunError:
        # 잘못된 stage 등 — 기존 동작은 단순 redirect.
        pass
    return redirect("qualitygates:run_detail", pk=run.pk)


@login_required
def cancel_run(request, pk):
    """진행 중인 Gate Run 을 취소한다. 핵심 로직은 services.cancel_run."""
    run = get_object_or_404(QualityGateRun, pk=pk)
    try:
        services.cancel_run(run=run, user=request.user)
    except services.GateRunError as e:
        if e.code == "cannot_cancel":
            return redirect("qualitygates:run_detail", pk=run.pk)
        messages.error(request, str(e))
    return redirect("qualitygates:run_detail", pk=run.pk)


@login_required
def rerun_gate(request, pk):
    """검수 재실행 — 검증배포부터 다시.

    view 는 `can_rerun` 만 검사하고 `gate_run` 으로 redirect (POST 파라미터 없이 새 Run).
    실제 새 Run 생성은 services.trigger_gate_run 이 담당. service 레벨에서는
    `services.rerun_gate(run=..., user=...)` 가 직접 새 Run 을 만들어 반환.
    """
    run = get_object_or_404(QualityGateRun, pk=pk)
    gate = run.gate

    if not run.can_rerun:
        return redirect("qualitygates:run_detail", pk=run.pk)

    # 기존 동작 보존: redirect 를 통해 gate_run 으로 가서 POST 파라미터 처리가 동일하게 흐름.
    return redirect("qualitygates:gate_run", pk=gate.pk)


@login_required
def rerun_test(request, pk):
    """테스트만 재실행. 핵심 로직은 services.rerun_test."""
    run = get_object_or_404(QualityGateRun, pk=pk)

    if request.method != "POST":
        return redirect("qualitygates:run_detail", pk=run.pk)

    try:
        services.rerun_test(run=run, user=request.user)
    except services.GateRunError as e:
        if e.code == "cannot_rerun_test":
            return redirect("qualitygates:run_detail", pk=run.pk)
        messages.error(request, str(e))

    return redirect("qualitygates:run_detail", pk=run.pk)


@login_required
def image_console(request, pk):
    """이미지 생성 단계의 저장된 Jenkins 콘솔 로그 표시."""
    run = get_object_or_404(QualityGateRun.objects.select_related("gate"), pk=pk)
    return render(request, "qualitygates/console_output.html", {
        "run": run,
        "stage_label": "이미지 생성",
        "job_path": run.gate.image_job_path,
        "build_number": run.image_build_number,
        "build_url": run.image_build_url,
        "console_output": run.image_console_output,
        "duration_ms": run.image_duration_ms,
    })


@login_required
def deploy_console(request, pk):
    """검증 배포 단계의 저장된 Jenkins 콘솔 로그 표시."""
    run = get_object_or_404(QualityGateRun.objects.select_related("gate"), pk=pk)
    return render(request, "qualitygates/console_output.html", {
        "run": run,
        "stage_label": "검수대상 환경 배포",
        "job_path": run.gate.deploy_job_path,
        "build_number": run.deploy_build_number,
        "build_url": run.deploy_build_url,
        "console_output": run.deploy_console_output,
        "duration_ms": run.deploy_duration_ms,
    })


@login_required
def prod_console(request, pk):
    """검수통과 배포 단계의 저장된 Jenkins 콘솔 로그 표시."""
    run = get_object_or_404(QualityGateRun.objects.select_related("gate"), pk=pk)
    return render(request, "qualitygates/console_output.html", {
        "run": run,
        "stage_label": "검수통과 환경 배포",
        "job_path": run.gate.prod_job_path,
        "build_number": run.prod_build_number,
        "build_url": run.prod_build_url,
        "console_output": run.prod_console_output,
        "duration_ms": run.prod_duration_ms,
    })


def _render_error_log(request, run, stage_label, job_path, build_number, build_url,
                     console_output, duration_ms, console_url_name):
    """공용 — 콘솔 로그에서 @@@ 블록만 잘라 에러 로그 페이지를 렌더."""
    from apps.integrations.url_matcher import extract_urls, invalidate_match_cache
    # 담당자(`ApiEndpointOwner`) 가 admin 화면에서 변경되면 LocMem 캐시(signal 로
    # 자동 무효화) 가 stale 인 케이스를 방어 — 페이지 진입 시 명시적 무효화. 비용:
    # cache.delete + index rebuild (~ 300 row, 수십 ms). 같은 페이지 안의 여러
    # find_endpoint_owner 호출은 캐시 hit 이라 성능 영향 없음. (2026-05-20 사용자
    # 보고: 담당자 변경 후 Error Log 표에 옛 값이 그대로 노출)
    invalidate_match_cache()
    error_log = extract_error_log(console_output)
    # 표는 "추출된 에러" 카드 안에 등장한 URL 만 매칭한다. 정상 step 의 URL 까지
    # 포함되면 누가 F/U 해야 할지 한눈에 안 들어와서 의도적으로 좁힘.
    matched_urls = extract_urls(error_log or "")
    # 에러 노티 버튼용 stage 추론 — console_url_name 의 끝 segment 사용.
    stage = ""
    if console_url_name.endswith(":image_console"):
        stage = "image"
    elif console_url_name.endswith(":deploy_console"):
        stage = "deploy"
    elif console_url_name.endswith(":prod_console"):
        stage = "prod"
    return render(request, "qualitygates/error_log.html", {
        "run": run,
        "stage_label": stage_label,
        "job_path": job_path,
        "build_number": build_number,
        "build_url": build_url,
        "error_log": error_log,
        "console_url_name": console_url_name,
        "duration_ms": duration_ms,
        "matched_urls": matched_urls,
        "notify_source_type": "qg_run",
        "notify_source_id": str(run.pk),
        "notify_stage": stage,
    })


@login_required
def image_error_log(request, pk):
    """이미지 단계 콘솔 로그에서 실패 원인만 파싱해서 보여준다."""
    run = get_object_or_404(QualityGateRun.objects.select_related("gate"), pk=pk)
    return _render_error_log(
        request, run, "이미지 생성", run.gate.image_job_path,
        run.image_build_number, run.image_build_url,
        run.image_console_output, run.image_duration_ms,
        "qualitygates:image_console",
    )


@login_required
def deploy_error_log(request, pk):
    """검수대상 배포 단계 콘솔 로그에서 실패 원인만 파싱해서 보여준다."""
    run = get_object_or_404(QualityGateRun.objects.select_related("gate"), pk=pk)
    return _render_error_log(
        request, run, "검수대상 환경 배포", run.gate.deploy_job_path,
        run.deploy_build_number, run.deploy_build_url,
        run.deploy_console_output, run.deploy_duration_ms,
        "qualitygates:deploy_console",
    )


@login_required
def prod_error_log(request, pk):
    """검수통과 배포 단계 콘솔 로그에서 실패 원인만 파싱해서 보여준다."""
    run = get_object_or_404(QualityGateRun.objects.select_related("gate"), pk=pk)
    return _render_error_log(
        request, run, "검수통과 환경 배포", run.gate.prod_job_path,
        run.prod_build_number, run.prod_build_url,
        run.prod_console_output, run.prod_duration_ms,
        "qualitygates:prod_console",
    )


# =============================================
# Polling
# =============================================

def _should_auto_advance(gate) -> bool:
    """auto_mode 가 켜진 Dev→Stg 게이트에서만 중간 대기 단계를 건너뛴다."""
    return gate.auto_mode and gate.gate_type == QualityGate.GateType.DEV_STG


# 자동 모드에서 Git Parameter 의 default 를 release branch 로 오버라이드할 때 쓰는 패턴.
# Jenkins Git Parameter 의 choices 는 origin 또는 refs/remotes/origin prefix 가 붙어
# 들어올 수 있어 경계 패턴(시작/슬래시 뒤) 으로 매칭한다.
_RELEASE_BRANCH_RE = re.compile(r"(?:^|/)(release/release_[^/\s]+)$")


def _pick_release_branch(choices) -> str | None:
    """choices 안에서 `release/release_*` 형태의 항목을 골라 원본 문자열 그대로 반환.

    운영 컨벤션 — origin/release 하위에 보통 단일 release branch (`release/release_MMDD`) 만
    존재하지만, 안전을 위해 여러 개가 있을 경우 release_ 뒷부분 사전순 가장 큰 것을 선택한다
    (= 보통 가장 미래 일자). 매칭 없으면 None.
    """
    if not choices:
        return None
    matched: list[tuple[str, str]] = []
    for c in choices:
        if not isinstance(c, str):
            continue
        stripped = c.strip()
        m = _RELEASE_BRANCH_RE.search(stripped)
        if m is None:
            continue
        matched.append((m.group(1), stripped))
    if not matched:
        return None
    matched.sort(key=lambda x: x[0])
    return matched[-1][1]


def _collect_default_params_from_info(parameters: list, *, prefer_release_branch: bool = False) -> dict:
    """Job 파라미터 정의 리스트에서 default 값을 모아 {name: value} dict 로 반환.

    수동 모달의 HTML 폼 제출과 동등한 값이 되도록 맞춘다:
      - boolean:  default 가 참이면 "true", 아니면 "false"
      - choices:  default 가 choices 안에 있으면 default, 아니면 첫 번째 choice
                  (<select> 가 유효하지 않은 selected 값에 대해 첫 옵션을 채택하는 동작)
      - 그 외:    default 문자열 그대로
    이 규칙을 어기면 Git Parameter 같이 default 가 실제 브랜치 목록에 없을 때 모달이
    실제로 보내던 값과 달라져서 체크아웃이 실패한다.

    값이 빈 문자열인 파라미터는 dict 에 담지 않는다. ImageTagParameter / Active Choice
    처럼 Jenkins 측 정적 default 가 비어 있고 choices 도 못 긁어온 케이스에서, 빈 값을
    명시 전송하면 Jenkins 가 그걸 그대로 채택해 deploy 스크립트가 `image: ...:` 같은
    깨진 매니페스트를 푸시하는 사고를 일으킨다. (그냥 빼면 Jenkinsfile 에서 변수
    미바인딩으로 MissingPropertyException 이 날 수 있으니, 호출 측에서 사전에 default
    를 채우거나 트리거 자체를 보류해야 한다 — 예: `_auto_trigger_deploy`.)

    `prefer_release_branch=True` (auto_mode 진입 시) 일 때 Git Parameter 의 default 를
    `release/release_*` choice 로 오버라이드한다. release branch 가 없으면 일반 규칙대로
    fallback (보통 master).

    NOTE: String 타입의 git ref 파라미터(예: DEPLOYER_REFS=`refs/heads/master`) 는 자동 치환
    하지 않는다. 이런 파라미터가 Pipeline-from-SCM 의 Jenkinsfile ref 를 가리키는 경우, release
    branch 가 Jenkinsfile 저장소에는 없어 fetch 가 실패한다 (`fatal: couldn't find remote ref ...`).
    Jenkinsfile 저장소가 가리키는 base ref(보통 master) 는 그대로 두고, source code 체크아웃은
    Jenkinsfile 안에서 BRANCH_NAME 파라미터를 받아 release 로 진행되도록 둔다.
    """
    params: dict = {}
    for p in parameters:
        name = p.get("name")
        if not name:
            continue
        ptype = (p.get("type") or "").lower()
        choices = p.get("choices") or []
        default = p.get("default")
        default = "" if default is None else str(default)

        if "boolean" in ptype:
            params[name] = "true" if default.lower() == "true" else "false"
            continue

        # auto_mode: Git Parameter 의 choices 안에 release branch 가 있으면 그걸 default 로.
        if prefer_release_branch and "git" in ptype:
            picked = _pick_release_branch(choices)
            if picked:
                params[name] = picked
                continue

        if choices:
            value = default if default in choices else choices[0]
        else:
            value = default
        if value == "":
            # 빈 값은 보내지 않는다 — Jenkins 가 플러그인 런타임 default 로 채우게 한다.
            continue
        params[name] = value
    return params


def _collect_default_params(client, job_path: str, *, prefer_release_branch: bool = False) -> dict:
    """`_collect_default_params_from_info` 의 client 기반 래퍼."""
    info = client.get_job_info(job_path)
    return _collect_default_params_from_info(
        info.get("parameters", []), prefer_release_branch=prefer_release_branch,
    )


def _auto_trigger_deploy(run, gate):
    """이미지 빌드 성공 직후 호출 — 기본 파라미터로 검수대상 배포 Job 자동 트리거.

    APP_IMAGE(ImageTagParameter) 는 Jenkins JSON API 의 default 가 비어 있고
    choices 도 ECR 갱신 타이밍 등으로 일시적으로 비어 올 수 있다. 그 경우
    `_collect_default_params` 가 빈 값을 dict 에서 제외하면 Jenkinsfile 이
    `APP_IMAGE` 변수 미바인딩으로 `MissingPropertyException` 을 던지며 즉시
    실패한다 (Jenkins 는 buildWithParameters 에 빠진 파라미터를 plugin 의
    런타임 default 로 채워주지 않는다).

    방지책: 방금 빌드한 이미지(`run.image_build_number`) 로 시작하는 choice 를
    찾아 명시 전송. choices 자체가 비어 있어 매칭이 불가능하면 안전하게
    WAITING_DEPLOY 로 떨어뜨려 사용자 확인을 받는다.
    """
    client = JenkinsClient(
        gate.deploy_jenkins_url, gate.deploy_jenkins_user, gate.deploy_jenkins_token,
    )
    try:
        info = client.get_job_info(gate.deploy_job_path)
        parameters = info.get("parameters", [])

        # APP_IMAGE 디폴트를 이 Run 의 이미지 빌드 번호로 매칭된 choice 로 오버라이드.
        # prod_deploy_params 가 모달에 대해 하는 것과 동일.
        app_image_param = next((p for p in parameters if p.get("name") == "APP_IMAGE"), None)
        if app_image_param and run.image_build_number:
            prefix = f"{run.image_build_number}-"
            for c in (app_image_param.get("choices") or []):
                if isinstance(c, str) and c.startswith(prefix):
                    app_image_param["default"] = c
                    break

        params = _collect_default_params_from_info(parameters, prefer_release_branch=True)

        # APP_IMAGE 가 정의돼 있는데 값을 못 채웠으면 자동 진행 보류 — 사용자가 모달에서
        # 직접 선택하도록 WAITING_DEPLOY 로. 그대로 트리거하면 Jenkinsfile 이
        # MissingPropertyException 으로 실패한다.
        if app_image_param and "APP_IMAGE" not in params:
            logger.warning(
                f"Auto deploy: APP_IMAGE choice not resolvable (image_build_number={run.image_build_number}); "
                f"fallback to WAITING_DEPLOY for run {run.pk}"
            )
            run.status = QualityGateRun.Status.WAITING_DEPLOY
            run.save(update_fields=["status"])
            return

        result = client.trigger_build(gate.deploy_job_path, parameters=params or None)
        run.status = QualityGateRun.Status.DEPLOYING
        run.deploy_build_number = result.get("build_number")
        run.deploy_started_at = timezone.now()
        run.save(update_fields=["status", "deploy_build_number", "deploy_started_at"])
        send_event_async(nev.DEPLOY_STARTED, run)
    except JenkinsError as e:
        logger.error(f"Auto deploy trigger failed for run {run.pk}: {e}")
        run.status = QualityGateRun.Status.DEPLOY_FAILED
        run.save(update_fields=["status"])
        send_event_async(nev.DEPLOY_FAILED, run, error_message=str(e))
        send_event_async(nev.RUN_FAILED, run, error_message=str(e))


def _auto_trigger_prod(run, gate):
    """QG 통과 직후 호출 — 기본 파라미터로 스테이징 배포 Job 자동 트리거.

    APP_IMAGE(ImageTagParameter) 처리는 `_auto_trigger_deploy` 와 동일 — 빈 값을
    그대로 빼고 트리거하면 Jenkinsfile 이 `MissingPropertyException: APP_IMAGE`
    로 즉시 실패한다. 따라서 방금 빌드한 이미지(`run.image_build_number`) 로
    시작하는 choice 를 찾아 명시 전송. 매칭이 불가능하면 자동 트리거를 보류하고
    `QG_PASSED` 로 떨어뜨려 사용자가 모달에서 직접 선택하게 한다. (deploy 의
    fallback 은 WAITING_DEPLOY, prod 의 fallback 은 QG_PASSED — 단계 차이.)
    """
    prod_client = JenkinsClient(
        gate.prod_jenkins_url, gate.prod_jenkins_user, gate.prod_jenkins_token,
    )
    try:
        info = prod_client.get_job_info(gate.prod_job_path)
        parameters = info.get("parameters", [])

        # APP_IMAGE 디폴트를 이 Run 의 이미지 빌드 번호로 매칭된 choice 로 오버라이드.
        # _auto_trigger_deploy 와 동일.
        app_image_param = next((p for p in parameters if p.get("name") == "APP_IMAGE"), None)
        if app_image_param and run.image_build_number:
            prefix = f"{run.image_build_number}-"
            for c in (app_image_param.get("choices") or []):
                if isinstance(c, str) and c.startswith(prefix):
                    app_image_param["default"] = c
                    break

        params = _collect_default_params_from_info(parameters, prefer_release_branch=True)

        # APP_IMAGE 가 정의돼 있는데 값을 못 채웠으면 자동 진행 보류 — 사용자가 모달에서
        # 직접 선택하도록 QG_PASSED 로. 그대로 트리거하면 Jenkinsfile 이
        # MissingPropertyException 으로 실패한다 (Run #14/public-auth 2026-05-11 사고).
        if app_image_param and "APP_IMAGE" not in params:
            logger.warning(
                f"Auto prod deploy: APP_IMAGE choice not resolvable "
                f"(image_build_number={run.image_build_number}); "
                f"fallback to QG_PASSED for run {run.pk}"
            )
            run.status = QualityGateRun.Status.QG_PASSED
            run.save(update_fields=["status"])
            return

        result = prod_client.trigger_build(gate.prod_job_path, parameters=params or None)
        run.status = QualityGateRun.Status.PROD_DEPLOYING
        run.prod_build_number = result.get("build_number")
        run.prod_started_at = timezone.now()
        run.save(update_fields=["status", "prod_build_number", "prod_started_at"])
        send_event_async(nev.PROD_STARTED, run)
    except JenkinsError as e:
        logger.error(f"Auto prod deploy trigger failed for run {run.pk}: {e}")
        run.status = QualityGateRun.Status.PROD_DEPLOY_FAILED
        run.save(update_fields=["status"])
        send_event_async(nev.PROD_FAILED, run, error_message=str(e))
        send_event_async(nev.RUN_FAILED, run, error_message=str(e))


def _poll_gate_run(run):
    gate = run.gate

    if run.status == QualityGateRun.Status.BUILDING_IMAGE:
        _poll_image_build(run, gate)

    elif run.status == QualityGateRun.Status.DEPLOYING:
        _poll_deploy(run, gate)

    elif run.status == QualityGateRun.Status.WAITING_TEST:
        _advance_test(run, gate)

    elif run.status == QualityGateRun.Status.TESTING:
        _poll_current_test(run)

    elif run.status == QualityGateRun.Status.PROD_DEPLOYING:
        _poll_prod_deploy(run, gate)


def _hint_from_image_params(params: dict) -> tuple[list, list]:
    """image_params_json 의 값들을 git URL 후보 / branch 후보로 자동 분리해 build_info hint 로 사용.

    Pipeline-from-SCM 빌드는 actions 안에 BuildData 가 두 개(Jenkinsfile + source) 들어오는데,
    호출 측이 source repo URL / branch 를 hint 로 넘기면 jenkins.py 의 `_extract_git_info` 가
    source 측 BuildData 를 우선 선택해 화면 표시(branch/commit) 가 source 와 일치하게 된다.
    값에 git URL 형태(`http(s)://`, `git@`, 또는 `.git` 끝) 면 repo url, 그 외 string 은 branch
    후보. 값 type 이 dict 인 SCM_URL 이 비어 있어도 BRANCH_NAME 같은 다른 후보가 있으면 매칭됨.
    """
    repo_urls: list[str] = []
    branches: list[str] = []
    for v in (params or {}).values():
        if not isinstance(v, str) or not v.strip():
            continue
        s = v.strip()
        if (
            s.startswith(("http://", "https://", "ssh://", "git://", "git@"))
            or s.endswith(".git")
        ):
            repo_urls.append(s)
        else:
            branches.append(s)
    return repo_urls, branches


def _poll_image_build(run, gate):
    """이미지 생성 Job 빌드 폴링 → 성공 시 Deploy 자동 트리거."""
    client = JenkinsClient(
        gate.deploy_jenkins_url, gate.deploy_jenkins_user, gate.deploy_jenkins_token,
    )

    if not run.image_build_number:
        return

    prefer_urls, prefer_branches = _hint_from_image_params(run.image_params_json or {})
    try:
        build_info = client.get_build_info(
            gate.image_job_path, run.image_build_number,
            prefer_remote_urls=prefer_urls,
            prefer_branches=prefer_branches,
        )
    except JenkinsError as e:
        if hasattr(e, "status") and e.status == 404:
            return
        logger.warning(f"Image build poll failed: {e}")
        return

    run.image_build_url = build_info.get("url", "")

    git_branch = build_info.get("git_branch") or ""
    git_commit = build_info.get("git_commit") or ""
    if git_branch:
        run.image_git_branch = git_branch
    if git_commit:
        run.image_git_commit = git_commit

    stages = client.get_build_stages(gate.image_job_path, run.image_build_number)
    if stages:
        run.image_stages_json = stages
        in_progress = [s for s in stages if s["status"] == "IN_PROGRESS"]
        if in_progress:
            run.image_current_stage = in_progress[0]["name"]
        else:
            completed = [s for s in stages if s["status"] in ("SUCCESS", "FAILED")]
            if completed:
                run.image_current_stage = completed[-1]["name"]

    building = build_info.get("building", False)
    result = build_info.get("result")

    if building:
        run.save()
        return

    run.image_duration_ms = build_info.get("duration")
    run.image_completed_at = timezone.now()

    try:
        run.image_console_output = client.get_console_output(
            gate.image_job_path, run.image_build_number
        )
    except JenkinsError:
        logger.warning(f"Failed to fetch image console for run {run.pk}")

    if result != "SUCCESS":
        run.status = QualityGateRun.Status.IMAGE_BUILD_FAILED
        run.save()
        send_event_async(nev.IMAGE_FAILED, run, extra={"build_url": run.image_build_url})
        send_event_async(nev.RUN_FAILED, run, extra={"stage": "image"})
        return

    send_event_async(nev.IMAGE_COMPLETED, run, extra={"build_url": run.image_build_url})

    # auto_mode (Dev→Stg): 기본 파라미터로 배포 Job 자동 트리거, WAITING_DEPLOY 건너뜀.
    if _should_auto_advance(gate):
        run.save()
        _auto_trigger_deploy(run, gate)
        return

    # 이미지 빌드 성공 → 사용자 파라미터 입력 대기
    run.status = QualityGateRun.Status.WAITING_DEPLOY
    run.save()


_ARGOCD_POLL_COOLDOWN_SECS = 4
# 첫 sync 요청 후 Synced+Healthy 도달까지 허용할 최대 대기 시간.
# 매니페스트가 깨지거나 클러스터 상태 이상으로 영원히 sync 되지 않는 Run 이
# "Prod Deploying" 으로 무한정 남는 것을 막기 위함.
_ARGOCD_SYNC_TIMEOUT_SECS = 15 * 60
# Synced + Healthy 도달 후 grace period — 새 ReplicaSet 의 Pod 가 readiness probe 를
# 통과하기 전에 ArgoCD 가 일시적으로 Healthy 로 보고하는 케이스를 잡기 위해 추가 대기.
# 이 시간 동안 health 가 유지되어야 진짜 sync 완료로 본다. 그 사이 Progressing 으로
# 떨어지면 healthy_first_at 을 reset 하고 처음부터 다시 계산.
_ARGOCD_HEALTHY_GRACE_SECS = 30


def _check_argocd_sync(run, gate, stage: str) -> bool:
    """Argo CD 의 앱 sync/health 를 확인하고, 아직 sync 요청 전이면 sync+refresh 를 트리거한다.

    - stage: "deploy" 또는 "prod". 앱 URL 이 비어있거나 토큰이 없으면 True(패스) 반환.
    - Synced + Healthy 면 True → 호출부에서 다음 단계로 진행.
    - 그렇지 않으면 상태 필드만 갱신하고 False 반환 → DEPLOYING 상태 유지.
    - 최근 N 초 이내에 체크한 경우 Argo CD API 호출 생략(저장된 상태 기준 판정) — htmx 5s 폴링과
      외부 API 응답 지연이 겹쳐 서버가 막히는 것을 방지.
    - 첫 sync 요청 후 _ARGOCD_SYNC_TIMEOUT_SECS 초과해도 Synced+Healthy 미도달이면
      Run 을 (PROD_)DEPLOY_FAILED 로 전환해 무한 대기를 끊는다.
    """
    if stage == "deploy":
        app_url = gate.deploy_argocd_app_url
        env = gate.deploy_argocd_env
    else:
        app_url = gate.prod_argocd_app_url
        env = gate.prod_argocd_env
    token = gate.folder.argocd_token_for(env) if gate.folder else ""
    if not app_url or not token:
        return True

    # 최근 호출 쿨다운 — API 를 너무 자주 호출하지 않도록.
    last_checked = getattr(run, f"{stage}_argocd_checked_at")
    if last_checked and (timezone.now() - last_checked).total_seconds() < _ARGOCD_POLL_COOLDOWN_SECS:
        # 이미 Synced + Healthy 로 저장됐는지 검사 (다음 단계 진행 가능 여부).
        return bool(getattr(run, f"{stage}_argocd_synced_at"))

    base, app_id = parse_app_url(app_url)
    if not base or not app_id:
        logger.warning(f"Invalid Argo CD app URL for run {run.pk}: {app_url}")
        return True

    client = ArgoCDClient(base, token)

    sync_requested_field = f"{stage}_argocd_sync_requested_at"
    sync_requested_at = getattr(run, sync_requested_field)

    # 최초 진입: hard refresh + sync 만 트리거하고 같은 호출에서 polling 은 하지 않는다.
    # Argo CD 가 우리 sync 요청을 받아 새 operation 을 만들기 전에 polling 하면 직전
    # Run 에서 남은 `Synced + Healthy` 가 그대로 잡혀 다음 단계(테스트) 가 이전 revision
    # 으로 진행되는 사고가 발생한다 — 4초 쿨다운 뒤 다음 폴링에서만 상태 판정.
    if sync_requested_at is None:
        # `sync_requested_at` 은 sync_application 호출 *전* 측정 — Argo CD 가 빠르게
        # 처리해 startedAt/finishedAt 을 set 한 시점이 우리 timestamp 보다 이전으로
        # 보여 stale 로 잘못 판정되는 false-positive 를 방지.
        now = timezone.now()
        sync_error = ""
        try:
            client.get_application(app_id, refresh="hard")
        except Exception as e:
            logger.warning(f"Argo CD refresh ({stage}) failed for run {run.pk}: {e}")
            sync_error = f"refresh 실패: {e}"
        try:
            client.sync_application(app_id)
        except Exception as e:
            # 이미 동기화 중이거나 auto-sync 면 409/400 이 올 수 있다 — 무시하고 상태 폴링으로 이행.
            logger.warning(f"Argo CD sync ({stage}) trigger for run {run.pk}: {e}")
            sync_error = (sync_error + " | " if sync_error else "") + f"sync 실패: {e}"
        setattr(run, sync_requested_field, now)
        setattr(run, f"{stage}_argocd_checked_at", now)
        setattr(run, f"{stage}_argocd_sync_error", sync_error)
        run.save(update_fields=[
            sync_requested_field, f"{stage}_argocd_checked_at", f"{stage}_argocd_sync_error",
        ])
        return False
    else:
        # 두 번째 호출부터 — 데드라인 초과 검사. 매니페스트 깨짐 등으로 Argo 가 영원히
        # Synced 못 가는 Run 을 끊고 사용자에게 명확히 실패를 노출한다.
        elapsed = (timezone.now() - sync_requested_at).total_seconds()
        if elapsed > _ARGOCD_SYNC_TIMEOUT_SECS:
            last_sync = getattr(run, f"{stage}_argocd_sync_status") or "unknown"
            last_health = getattr(run, f"{stage}_argocd_health_status") or "unknown"
            last_op_msg = getattr(run, f"{stage}_argocd_operation_message") or ""
            timeout_msg = (
                f"Argo CD sync 타임아웃 ({int(elapsed // 60)}분 경과, 한도 "
                f"{_ARGOCD_SYNC_TIMEOUT_SECS // 60}분). 마지막 상태: "
                f"sync={last_sync}, health={last_health}."
            )
            if last_op_msg:
                timeout_msg += f" Argo: {last_op_msg}"
            setattr(run, f"{stage}_argocd_sync_error", timeout_msg)
            run.status = (
                QualityGateRun.Status.PROD_DEPLOY_FAILED
                if stage == "prod"
                else QualityGateRun.Status.DEPLOY_FAILED
            )
            run.save(update_fields=["status", f"{stage}_argocd_sync_error"])
            send_event_async(nev.ARGOCD_FAILED, run, error_message=timeout_msg, extra={"stage": stage})
            send_event_async(
                nev.PROD_FAILED if stage == "prod" else nev.DEPLOY_FAILED,
                run, error_message=timeout_msg,
            )
            send_event_async(nev.RUN_FAILED, run, error_message=timeout_msg)
            return False

    try:
        app = client.get_application(app_id)
    except Exception as e:
        logger.warning(f"Argo CD poll ({stage}) failed for run {run.pk}: {e}")
        setattr(run, f"{stage}_argocd_checked_at", timezone.now())
        run.save(update_fields=[f"{stage}_argocd_checked_at"])
        return False

    status = ArgoCDClient.extract_status(app)
    setattr(run, f"{stage}_argocd_sync_status", status["sync"] or "")
    setattr(run, f"{stage}_argocd_health_status", status["health"] or "")
    setattr(run, f"{stage}_argocd_operation_phase", status["operation_phase"] or "")
    setattr(run, f"{stage}_argocd_operation_message", status["operation_message"] or "")
    setattr(run, f"{stage}_argocd_checked_at", timezone.now())

    common_fields = [
        f"{stage}_argocd_sync_status",
        f"{stage}_argocd_health_status",
        f"{stage}_argocd_operation_phase",
        f"{stage}_argocd_operation_message",
        f"{stage}_argocd_checked_at",
    ]

    # 직전 Run 의 sync 결과(operationState.startedAt/finishedAt 이 우리 sync_requested_at
    # 보다 이전) 를 새 Run 의 Synced 로 오인하면 안 된다 — Argo CD 가 우리 sync 요청을 받아
    # 새 operation 을 시작/완료하기 전까지는 진행 중으로 본다.
    # Argo CD 의 timestamp 가 초 단위로 truncate 되므로 우리 sync_requested_at 의 ms
    # 부분도 잘라 동일 초 안의 비교가 false-positive 로 빠지지 않게 한다.
    sync_req_sec = sync_requested_at.replace(microsecond=0)
    op_started_raw = status.get("operation_started_at") or ""
    op_finished_raw = status.get("operation_finished_at") or ""
    op_started_at = parse_datetime(op_started_raw) if op_started_raw else None
    op_finished_at = parse_datetime(op_finished_raw) if op_finished_raw else None
    op_phase_now = status["operation_phase"] or ""
    # 새 operation 이 우리 sync 이후 시작됐으면 stale 아님 (startedAt 이 같은 초여도 OK).
    new_op_started = op_started_at is not None and op_started_at >= sync_req_sec
    if (
        not new_op_started
        and op_finished_at is not None
        and op_finished_at < sync_req_sec
        and op_phase_now not in ("Running", "Terminating", "Failed", "Error")
    ):
        # 새 operation 이 아직 보이지 않음 — stale. 다음 폴링에서 재확인.
        run.save(update_fields=common_fields)
        return False

    # Argo CD sync 작업 자체가 Failed/Error 로 종료된 경우 — 15분 타임아웃을 기다리지 않고
    # 즉시 (PROD_)DEPLOY_FAILED 로 전환해 사용자가 곧바로 재시도/원인 확인을 할 수 있게 한다.
    op_phase = status["operation_phase"] or ""
    if op_phase in ("Failed", "Error"):
        op_msg = status["operation_message"] or ""
        fail_msg = f"Argo CD sync {op_phase}." + (f" {op_msg}" if op_msg else "")
        setattr(run, f"{stage}_argocd_sync_error", fail_msg)
        run.status = (
            QualityGateRun.Status.PROD_DEPLOY_FAILED
            if stage == "prod"
            else QualityGateRun.Status.DEPLOY_FAILED
        )
        run.save(update_fields=common_fields + ["status", f"{stage}_argocd_sync_error"])
        send_event_async(nev.ARGOCD_FAILED, run, error_message=fail_msg, extra={"stage": stage})
        send_event_async(
            nev.PROD_FAILED if stage == "prod" else nev.DEPLOY_FAILED,
            run, error_message=fail_msg,
        )
        send_event_async(nev.RUN_FAILED, run, error_message=fail_msg)
        return False

    healthy_first_field = f"{stage}_argocd_healthy_first_at"

    if ArgoCDClient.is_synced_and_healthy(app):
        # (1) 자식 Pod 까지 Healthy 인지 검증 — Application 의 health.status 는 Deployment
        # level 이고 maxUnavailable 등 rolling update 설정으로 새 Pod 가 readiness probe
        # 통과 전에도 Healthy 로 보고될 수 있다. resource-tree 로 자식 Pod 들의 health 까지
        # 모두 Healthy 인지 확인.
        pods_ok = _all_argocd_pods_healthy(client, app_id, run, stage)
        if not pods_ok:
            # health 가 떨어진 것으로 간주 — grace 시작 시각 reset.
            if getattr(run, healthy_first_field) is not None:
                setattr(run, healthy_first_field, None)
                run.save(update_fields=common_fields + [healthy_first_field])
            else:
                run.save(update_fields=common_fields)
            return False

        # (3) Grace period — Healthy 가 일정 시간 유지돼야 진짜 sync 완료로 본다.
        healthy_first = getattr(run, healthy_first_field)
        now = timezone.now()
        if healthy_first is None:
            # 첫 Healthy 도달 — 시각만 기록하고 다음 폴링까지 대기.
            setattr(run, healthy_first_field, now)
            run.save(update_fields=common_fields + [healthy_first_field])
            return False
        if (now - healthy_first).total_seconds() < _ARGOCD_HEALTHY_GRACE_SECS:
            # 아직 grace 중 — 다음 폴링에서 재확인.
            run.save(update_fields=common_fields)
            return False

        # Grace 통과 — 진짜 sync 완료.
        setattr(run, f"{stage}_argocd_synced_at", now)
        # 최종 Synced + Healthy 도달 — 중간에 찍힌 일시적인 refresh/sync 트리거 에러는 더 이상 의미 없으므로 지운다.
        setattr(run, f"{stage}_argocd_sync_error", "")
        run.save(update_fields=common_fields + [
            f"{stage}_argocd_synced_at", f"{stage}_argocd_sync_error",
        ])
        return True

    # Synced + Healthy 가 아니면 healthy_first_at 도 reset.
    if getattr(run, healthy_first_field) is not None:
        setattr(run, healthy_first_field, None)
        run.save(update_fields=common_fields + [healthy_first_field])
    else:
        run.save(update_fields=common_fields)
    return False


def _all_argocd_pods_healthy(client, app_id: str, run, stage: str) -> bool:
    """resource-tree 의 모든 Pod 자식 노드 health 가 Healthy 인지.

    Pod 노드가 하나도 없는 케이스 (예: 매니페스트가 Service 만 정의) 는 True 반환 —
    Pod 단위 검증이 의미 없는 케이스. ArgoCD API 호출이 실패해도 보수적으로 False
    (다음 폴링에서 다시 시도).
    """
    try:
        tree = client.get_resource_tree(app_id)
    except Exception as e:
        logger.warning(f"Argo CD resource-tree ({stage}) failed for run {run.pk}: {e}")
        return False
    nodes = (tree or {}).get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("kind") != "Pod":
            continue
        ph = (node.get("health") or {}).get("status", "")
        if ph != "Healthy":
            return False
    # Pod 노드가 하나도 없으면 검증 skip — Pod 단위 health 가 정의되지 않은 매니페스트.
    return True


def _poll_deploy(run, gate):
    # Jenkins 는 이미 끝났고 Argo CD sync 만 기다리는 경우 — Jenkins 재폴링 생략.
    if (
        gate.has_deploy_argocd
        and run.deploy_completed_at
        and not run.deploy_argocd_synced_at
    ):
        if _check_argocd_sync(run, gate, stage="deploy"):
            run.status = QualityGateRun.Status.WAITING_TEST
            run.current_suite_index = 0
            run.save()
            send_event_async(nev.DEPLOY_COMPLETED, run, extra={"build_url": run.deploy_build_url})
            _advance_test(run, gate)
        return

    deploy_client = JenkinsClient(
        gate.deploy_jenkins_url, gate.deploy_jenkins_user, gate.deploy_jenkins_token,
    )

    if not run.deploy_build_number:
        return

    try:
        build_info = deploy_client.get_build_info(gate.deploy_job_path, run.deploy_build_number)
    except JenkinsError as e:
        if hasattr(e, "status") and e.status == 404:
            return
        logger.warning(f"Deploy poll failed: {e}")
        return

    run.deploy_build_url = build_info.get("url", "")

    stages = deploy_client.get_build_stages(gate.deploy_job_path, run.deploy_build_number)
    if stages:
        run.deploy_stages_json = stages
        in_progress = [s for s in stages if s["status"] == "IN_PROGRESS"]
        if in_progress:
            run.deploy_current_stage = in_progress[0]["name"]
        else:
            completed = [s for s in stages if s["status"] in ("SUCCESS", "FAILED")]
            if completed:
                run.deploy_current_stage = completed[-1]["name"]

    building = build_info.get("building", False)
    result = build_info.get("result")

    if building:
        run.save()
        return

    run.deploy_duration_ms = build_info.get("duration")
    run.deploy_completed_at = timezone.now()

    # 콘솔 로그 저장
    try:
        run.deploy_console_output = deploy_client.get_console_output(
            gate.deploy_job_path, run.deploy_build_number
        )
    except JenkinsError:
        logger.warning(f"Failed to fetch deploy console for run {run.pk}")

    if result != "SUCCESS":
        run.status = QualityGateRun.Status.DEPLOY_FAILED
        run.save()
        send_event_async(nev.DEPLOY_FAILED, run, extra={"build_url": run.deploy_build_url})
        send_event_async(nev.RUN_FAILED, run, extra={"stage": "deploy"})
        return

    # Jenkins 성공 — Argo CD 가 설정돼 있으면 최종 sync 가 Healthy 일 때까지 DEPLOYING 유지.
    if gate.has_deploy_argocd:
        run.save()
        if not _check_argocd_sync(run, gate, stage="deploy"):
            return

    run.status = QualityGateRun.Status.WAITING_TEST
    run.current_suite_index = 0
    run.save()
    send_event_async(nev.DEPLOY_COMPLETED, run, extra={"build_url": run.deploy_build_url})
    _advance_test(run, gate)


def _try_trigger_test(test_run, run):
    """test_run 의 Suite 에서 idle Job 을 찾아 트리거.

    성공하면 test_run 을 TESTING 으로 갱신하고 True 반환.
    Idle Job 이 없으면 WAITING 으로 두고 False 반환.
    """
    test_client = JenkinsClient()
    suite = test_run.gate_suite.test_suite
    jobs = list(suite.jobs.filter(is_active=True))

    for job in jobs:
        if test_client.is_job_building(job.job_full_path):
            continue

        try:
            result = test_client.trigger_build(job.job_full_path)

            suite_run = TestSuiteRun.objects.create(
                suite=suite,
                status=TestSuiteRun.Status.RUNNING,
                total_jobs=1,
                started_at=timezone.now(),
                triggered_by=run.triggered_by,
            )
            job_run = TestSuiteJobRun.objects.create(
                suite_run=suite_run,
                job=job,
                status=TestSuiteJobRun.Status.QUEUED,
                jenkins_build_number=result.get("build_number"),
                started_at=timezone.now(),
            )

            test_run.job_run = job_run
            test_run.status = QualityGateRunTest.Status.TESTING
            test_run.started_at = timezone.now()
            test_run.save(update_fields=["job_run", "status", "started_at"])
            send_event_async(nev.TEST_SUITE_STARTED, run, suite_name=suite.name)
            return True

        except JenkinsError as e:
            logger.error(f"Test trigger failed for {job.job_name}: {e}")
            continue

    if test_run.status != QualityGateRunTest.Status.WAITING:
        test_run.status = QualityGateRunTest.Status.WAITING
        test_run.save(update_fields=["status"])
    return False


def _advance_test(run, gate):
    """다음 테스트 단계 진행.

    - 순차 모드: 현재 Suite 순번의 테스트만 트리거. Idle Job 이 없으면 대기.
    - 병렬 모드(`gate.parallel_test`): PENDING/WAITING 인 모든 Suite 를 동시에 트리거.
    """
    test_runs = list(run.test_runs.order_by("order").select_related("gate_suite__test_suite"))

    if not test_runs:
        _finalize_run(run)
        return

    if gate.parallel_test:
        _advance_test_parallel(run, gate, test_runs)
    else:
        _advance_test_sequential(run, gate, test_runs)


def _advance_test_sequential(run, gate, test_runs):
    if run.current_suite_index >= len(test_runs):
        _finalize_run(run)
        return

    current_test = test_runs[run.current_suite_index]

    if current_test.status in (QualityGateRunTest.Status.PASSED, QualityGateRunTest.Status.FAILED):
        # 이미 완료 → 다음으로
        if current_test.status == QualityGateRunTest.Status.FAILED:
            run.status = QualityGateRun.Status.FAILED
            run.save(update_fields=["status"])
            return
        run.current_suite_index += 1
        run.save(update_fields=["current_suite_index"])
        _advance_test(run, gate)
        return

    if _try_trigger_test(current_test, run):
        run.status = QualityGateRun.Status.TESTING
        run.save(update_fields=["status"])
        return

    # 모든 Job busy → 대기
    run.status = QualityGateRun.Status.WAITING_TEST
    run.save(update_fields=["status"])


def _advance_test_parallel(run, gate, test_runs):
    """병렬 모드: PENDING/WAITING 인 모든 Suite 를 한 번에 트리거 시도.

    실패해도(idle 없음) 다른 Suite 의 트리거는 진행. 호출 직후 run.status 를
    TESTING / WAITING_TEST 중 적절한 쪽으로 맞춘다. 모두 끝났다면 finalize.
    """
    triggerable = [
        tr for tr in test_runs
        if tr.status in (QualityGateRunTest.Status.PENDING, QualityGateRunTest.Status.WAITING)
    ]
    for tr in triggerable:
        _try_trigger_test(tr, run)

    _resolve_parallel_run_status(run, test_runs)


def _resolve_parallel_run_status(run, test_runs):
    """병렬 모드에서 test_runs 상태를 보고 run.status / current_suite_index 갱신.

    - 모두 PASSED/FAILED 이면 finalize_run (실패 있으면 FAILED, 아니면 다음 단계).
    - 진행 중인 게 하나라도 있으면 TESTING.
    - 진행 중인 게 없고 WAITING/PENDING 만 남았으면 WAITING_TEST.
    """
    # DB 최신값 반영
    test_runs = list(run.test_runs.order_by("order"))
    statuses = [tr.status for tr in test_runs]
    completed = sum(
        1 for s in statuses
        if s in (QualityGateRunTest.Status.PASSED, QualityGateRunTest.Status.FAILED)
    )
    any_testing = any(s == QualityGateRunTest.Status.TESTING for s in statuses)

    if completed != run.current_suite_index:
        run.current_suite_index = completed
        run.save(update_fields=["current_suite_index"])

    if completed == len(test_runs):
        _finalize_run(run)
        return

    new_status = (
        QualityGateRun.Status.TESTING if any_testing
        else QualityGateRun.Status.WAITING_TEST
    )
    if run.status != new_status:
        run.status = new_status
        run.save(update_fields=["status"])


def _poll_one_test_build(test_run):
    """단일 test_run 의 Jenkins 빌드 상태를 폴링하고 PASSED/FAILED 로 전이.

    리턴: True 면 이 호출에서 terminal(PASSED/FAILED) 로 전이됐음.
    """
    job_run = test_run.job_run
    if not job_run or not job_run.jenkins_build_number:
        return False

    test_client = JenkinsClient()

    try:
        build_info = test_client.get_build_info(
            job_run.job.job_full_path, job_run.jenkins_build_number,
        )
    except JenkinsError as e:
        if hasattr(e, "status") and e.status == 404:
            return False
        logger.warning(f"Test poll failed: {e}")
        return False

    building = build_info.get("building", False)
    result = build_info.get("result")
    job_run.jenkins_build_url = build_info.get("url", "")

    stages = test_client.get_build_stages(job_run.job.job_full_path, job_run.jenkins_build_number)
    if stages:
        job_run.stages_json = stages
        in_progress = [s for s in stages if s["status"] == "IN_PROGRESS"]
        if in_progress:
            job_run.current_stage = in_progress[0]["name"]
        else:
            completed_stages = [s for s in stages if s["status"] in ("SUCCESS", "FAILED")]
            if completed_stages:
                job_run.current_stage = completed_stages[-1]["name"]

    if building:
        job_run.status = TestSuiteJobRun.Status.RUNNING
        job_run.save()
        return False

    # 테스트 완료
    job_run.duration_ms = build_info.get("duration")
    job_run.completed_at = timezone.now()

    try:
        job_run.console_output = test_client.get_console_output(
            job_run.job.job_full_path, job_run.jenkins_build_number
        )
    except JenkinsError:
        logger.warning(f"Failed to fetch test console for job_run {job_run.pk}")

    if result == "SUCCESS":
        job_run.status = TestSuiteJobRun.Status.SUCCESS
        test_run.status = QualityGateRunTest.Status.PASSED
    else:
        job_run.status = TestSuiteJobRun.Status.FAILURE
        test_run.status = QualityGateRunTest.Status.FAILED

    job_run.save()
    test_run.completed_at = timezone.now()
    test_run.save(update_fields=["status", "completed_at"])

    # SuiteRun 업데이트
    suite_run = job_run.suite_run
    suite_run.passed_jobs = 1 if result == "SUCCESS" else 0
    suite_run.failed_jobs = 0 if result == "SUCCESS" else 1
    suite_run.status = TestSuiteRun.Status.COMPLETED
    suite_run.completed_at = timezone.now()
    suite_run.save()

    if stages and len(stages) > job_run.job.total_stages:
        job_run.job.total_stages = len(stages)
        job_run.job.save(update_fields=["total_stages"])

    suite_name = ""
    try:
        suite_name = test_run.gate_suite.test_suite.name
    except Exception:  # noqa: BLE001
        suite_name = "Test Suite"
    if test_run.status == QualityGateRunTest.Status.PASSED:
        send_event_async(
            nev.TEST_SUITE_PASSED, test_run.gate_run, suite_name=suite_name,
        )
    else:
        send_event_async(
            nev.TEST_SUITE_FAILED, test_run.gate_run, suite_name=suite_name,
            job_run_id=str(job_run.pk),
        )

    return True


def _poll_current_test(run):
    """진행 중인 테스트의 빌드 상태 폴링.

    - 순차 모드: 현재 Suite 하나만 폴링. 실패 시 즉시 FAILED.
    - 병렬 모드: 모든 TESTING Suite 를 폴링. 마지막 Suite 까지 완료될 때 판정.
    """
    gate = run.gate
    if gate.parallel_test:
        _poll_tests_parallel(run, gate)
    else:
        _poll_test_sequential(run)


def _poll_test_sequential(run):
    test_runs = list(run.test_runs.order_by("order").select_related(
        "gate_suite__test_suite", "job_run__job",
    ))

    if run.current_suite_index >= len(test_runs):
        _finalize_run(run)
        return

    current_test = test_runs[run.current_suite_index]

    if not _poll_one_test_build(current_test):
        return

    # 실패 시 즉시 중단 (순차 모드)
    if current_test.status == QualityGateRunTest.Status.FAILED:
        run.status = QualityGateRun.Status.FAILED
        run.save(update_fields=["status"])
        send_event_async(nev.RUN_FAILED, run, extra={"stage": "test"})
        return

    run.current_suite_index += 1
    run.save(update_fields=["current_suite_index"])

    if run.current_suite_index >= len(test_runs):
        _finalize_run(run)
    else:
        run.status = QualityGateRun.Status.WAITING_TEST
        run.save(update_fields=["status"])
        _advance_test(run, run.gate)


def _poll_tests_parallel(run, gate):
    """병렬 모드: 모든 TESTING test_run 을 폴링하고, 끝까지 다 끝나야 다음 단계로.

    실패가 있어도 진행 중인 다른 Suite 가 있으면 마지막까지 기다린다 — 모두
    terminal 상태에 도달하면 finalize 가 PASS/FAIL 을 판정.
    """
    test_runs = list(run.test_runs.order_by("order").select_related(
        "gate_suite__test_suite", "job_run__job",
    ))

    # 1) TESTING 상태인 것 모두 폴링
    for tr in test_runs:
        if tr.status == QualityGateRunTest.Status.TESTING:
            _poll_one_test_build(tr)

    # 2) 아직 트리거되지 않은(PENDING/WAITING) 것이 있으면 idle Job 다시 시도
    test_runs = list(run.test_runs.order_by("order").select_related(
        "gate_suite__test_suite", "job_run__job",
    ))
    not_yet_triggered = [
        tr for tr in test_runs
        if tr.status in (QualityGateRunTest.Status.PENDING, QualityGateRunTest.Status.WAITING)
    ]
    for tr in not_yet_triggered:
        _try_trigger_test(tr, run)

    # 3) 상태 결정 (완료 카운트 갱신 + run.status 갱신)
    _resolve_parallel_run_status(run, test_runs)


def _finalize_run(run):
    """모든 Suite 테스트 완료 → 판정."""
    test_runs = list(run.test_runs.all())
    has_failed = any(tr.status == QualityGateRunTest.Status.FAILED for tr in test_runs)
    all_done = all(
        tr.status in (QualityGateRunTest.Status.PASSED, QualityGateRunTest.Status.FAILED)
        for tr in test_runs
    )

    if not all_done:
        return

    if has_failed:
        run.status = QualityGateRun.Status.FAILED
        run.save(update_fields=["status"])
        failed_count = sum(
            1 for tr in test_runs if tr.status == QualityGateRunTest.Status.FAILED
        )
        send_event_async(
            nev.RUN_FAILED, run,
            extra={"stage": "test", "failed_suites": failed_count},
        )
    elif run.gate.has_prod_deploy:
        if _should_auto_advance(run.gate):
            # auto_mode (Dev→Stg): 기본 파라미터로 스테이징 배포 자동 트리거, QG_PASSED 건너뜀.
            _auto_trigger_prod(run, run.gate)
        else:
            # 검수통과 배포가 설정됨 → QG_PASSED (다음 배포 대기)
            run.status = QualityGateRun.Status.QG_PASSED
            run.save(update_fields=["status"])
    else:
        # 검수통과 배포 없음 → 바로 완료
        run.status = QualityGateRun.Status.COMPLETED
        run.save(update_fields=["status"])
        send_event_async(nev.RUN_COMPLETED, run)


def _poll_prod_deploy(run, gate):
    """검수통과 배포 빌드 상태 폴링."""
    if (
        gate.has_prod_argocd
        and run.prod_completed_at
        and not run.prod_argocd_synced_at
    ):
        if _check_argocd_sync(run, gate, stage="prod"):
            run.status = QualityGateRun.Status.COMPLETED
            run.save()
            send_event_async(nev.PROD_COMPLETED, run, extra={"build_url": run.prod_build_url})
            send_event_async(nev.RUN_COMPLETED, run)
        return

    prod_client = JenkinsClient(
        gate.prod_jenkins_url, gate.prod_jenkins_user, gate.prod_jenkins_token,
    )

    if not run.prod_build_number:
        return

    try:
        build_info = prod_client.get_build_info(gate.prod_job_path, run.prod_build_number)
    except JenkinsError as e:
        if hasattr(e, "status") and e.status == 404:
            return
        logger.warning(f"Prod deploy poll failed: {e}")
        return

    run.prod_build_url = build_info.get("url", "")

    stages = prod_client.get_build_stages(gate.prod_job_path, run.prod_build_number)
    if stages:
        run.prod_stages_json = stages
        in_progress = [s for s in stages if s["status"] == "IN_PROGRESS"]
        if in_progress:
            run.prod_current_stage = in_progress[0]["name"]
        else:
            completed = [s for s in stages if s["status"] in ("SUCCESS", "FAILED")]
            if completed:
                run.prod_current_stage = completed[-1]["name"]

    building = build_info.get("building", False)
    result = build_info.get("result")

    if building:
        run.save()
        return

    run.prod_duration_ms = build_info.get("duration")
    run.prod_completed_at = timezone.now()

    # 콘솔 로그 저장
    try:
        run.prod_console_output = prod_client.get_console_output(
            gate.prod_job_path, run.prod_build_number
        )
    except JenkinsError:
        logger.warning(f"Failed to fetch prod console for run {run.pk}")

    if result != "SUCCESS":
        run.status = QualityGateRun.Status.PROD_DEPLOY_FAILED
        run.save()
        send_event_async(nev.PROD_FAILED, run, extra={"build_url": run.prod_build_url})
        send_event_async(nev.RUN_FAILED, run, extra={"stage": "prod"})
        return

    if gate.has_prod_argocd:
        run.save()
        if not _check_argocd_sync(run, gate, stage="prod"):
            return

    run.status = QualityGateRun.Status.COMPLETED
    run.save()
    send_event_async(nev.PROD_COMPLETED, run, extra={"build_url": run.prod_build_url})
    send_event_async(nev.RUN_COMPLETED, run)
