"""메타 조회 API (Phase C).

엔드포인트 (모두 인증=ApiKey+Session, 스코프=meta:read, throttle=ReadThrottle):

- GET /api/v2/folders/
- GET /api/v2/folders/<uuid>/
- GET /api/v2/folders/<uuid>/subfolders/
- GET /api/v2/subfolders/<uuid>/
- GET /api/v2/gates/                 ?folder=&subfolder=&gate_type=&q=
- GET /api/v2/gates/<uuid>/          ?include=latest_run,test_suites
- GET /api/v2/gates/<uuid>/params/   ?stage=image|deploy|prod&refresh=1
- GET /api/v2/suites/                ?q=&service=
- GET /api/v2/suites/<uuid>/

`gates/<id>/params/` 는 `apps.qualitygates.services.get_job_param_schema` 를
재사용하여 기존 뷰 (`gate_run_params` / `deploy_params` / `prod_deploy_params`) 의
캐시 + APP_IMAGE override 정책을 그대로 따른다.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.qualitygates import services as qg_services
from apps.qualitygates.models import (
    QualityGate,
    QualityGateFolder,
    QualityGateSubFolder,
)
from apps.suites.models import TestSuite

from .. import scopes as S
from ..authentication import ApiKeyAuthentication
from ..filters import GateFilter, SuiteFilter
from ..permissions import HasScope
from ..serializers.meta import (
    FolderDetailSerializer,
    FolderListSerializer,
    GateDetailSerializer,
    GateListSerializer,
    SubFolderDetailSerializer,
    SubFolderListSerializer,
    SuiteDetailSerializer,
    SuiteListSerializer,
)
from ..throttling import ReadThrottle


class _MetaReadMixin:
    """Phase C 의 모든 메타 조회 뷰가 공유하는 DRF 설정."""

    authentication_classes = [ApiKeyAuthentication, SessionAuthentication]
    permission_classes = [HasScope]
    required_scope = S.META_READ
    throttle_classes = [ReadThrottle]


# ---------------------------------------------------------------------------
# Folder
# ---------------------------------------------------------------------------

class FolderListView(_MetaReadMixin, generics.ListAPIView):
    serializer_class = FolderListSerializer
    queryset = QualityGateFolder.objects.all().order_by("name")


class FolderDetailView(_MetaReadMixin, generics.RetrieveAPIView):
    serializer_class = FolderDetailSerializer
    queryset = QualityGateFolder.objects.all()
    lookup_field = "pk"


class FolderSubFolderListView(_MetaReadMixin, generics.ListAPIView):
    """`/api/v2/folders/<uuid>/subfolders/`."""

    serializer_class = SubFolderListSerializer

    def get_queryset(self):
        folder = get_object_or_404(QualityGateFolder, pk=self.kwargs["pk"])
        return (
            QualityGateSubFolder.objects
            .filter(folder=folder)
            .select_related("folder")
            .order_by("name")
        )


# ---------------------------------------------------------------------------
# SubFolder
# ---------------------------------------------------------------------------

class SubFolderDetailView(_MetaReadMixin, generics.RetrieveAPIView):
    serializer_class = SubFolderDetailSerializer
    queryset = (
        QualityGateSubFolder.objects
        .select_related("folder")
        .prefetch_related("gates")
    )
    lookup_field = "pk"


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

_GATE_BASE_QS = (
    QualityGate.objects
    .select_related("folder", "subfolder")
    .prefetch_related("gate_suites__test_suite")
    .order_by("name")
)


class GateListView(_MetaReadMixin, generics.ListAPIView):
    serializer_class = GateListSerializer
    queryset = _GATE_BASE_QS
    filterset_class = GateFilter


class GateDetailView(_MetaReadMixin, generics.RetrieveAPIView):
    serializer_class = GateDetailSerializer
    queryset = _GATE_BASE_QS
    lookup_field = "pk"


class GateParamsView(_MetaReadMixin, APIView):
    """`/api/v2/gates/<uuid>/params/?stage=image|deploy|prod&refresh=1`.

    기존 `gate_run_params` / `deploy_params` / `prod_deploy_params` 와 동일하게
    동작 — services.get_job_param_schema 로 통합.
    """

    def get(self, request, pk):
        gate = get_object_or_404(QualityGate, pk=pk)
        stage = (request.query_params.get("stage") or "").lower()
        if stage not in qg_services.VALID_PARAM_STAGES:
            raise ValidationError({
                "stage": (
                    "stage 는 'image', 'deploy', 'prod' 중 하나여야 합니다."
                ),
            })
        refresh = request.query_params.get("refresh") == "1"

        image_build_number = None
        if stage == "prod":
            # 캐시된 prod 응답에 가장 최근 Run 의 image_build_number 를 default override
            # 로 적용. 기존 prod_deploy_params 가 run 단위로 했던 것을, 메타 조회에서는
            # gate 의 latest_run 기준으로 한다 (아직 Run 컨텍스트가 없는 메타 API 이므로).
            latest = gate.latest_run
            if latest is not None:
                image_build_number = latest.image_build_number

        try:
            parameters, cached = qg_services.get_job_param_schema(
                gate,
                stage,
                refresh=refresh,
                image_build_number=image_build_number,
            )
        except qg_services.GateRunError as exc:
            body = {"error": {"code": exc.code, "message": str(exc)}}
            if exc.details:
                body["error"]["details"] = exc.details
            return Response(body, status=exc.http_status)

        return Response({
            "stage": stage,
            "cached": cached,
            "parameters": parameters,
        }, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

_SUITE_BASE_QS = (
    TestSuite.objects
    .select_related("service")
    .order_by("name")
)


class SuiteListView(_MetaReadMixin, generics.ListAPIView):
    serializer_class = SuiteListSerializer
    queryset = _SUITE_BASE_QS
    filterset_class = SuiteFilter


class SuiteDetailView(_MetaReadMixin, generics.RetrieveAPIView):
    serializer_class = SuiteDetailSerializer
    queryset = _SUITE_BASE_QS.prefetch_related("jobs")
    lookup_field = "pk"
