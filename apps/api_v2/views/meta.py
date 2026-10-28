"""메타 조회 API (Phase C).

이 파일의 API 는 모두 "무언가를 실행하기 전에, 실행할 대상의 id 를 찾기 위한 조회" 용도다.
구조는 인스턴스(Folder, 폴더) → 하위폴더(SubFolder) → Quality Gate(게이트) 순서로 좁혀지고,
Test Suite(테스트 묶음)는 이 계층과 별도로 존재하는 목록이다(게이트가 실행 대상으로 Suite 를
연결해 둔 관계). 이 id 들을 찾은 뒤에는 `Quality Gate Runs` / `Test Suite Runs` 태그의
실행 API 로 넘어가면 된다.

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
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
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
    ParamsStageSerializer,
    SubFolderDetailSerializer,
    SubFolderListSerializer,
    SuiteDetailSerializer,
    SuiteListSerializer,
)
from ..throttling import ReadThrottle

_META_TAG = "Meta (폴더/게이트/스위트 조회)"


class _MetaReadMixin:
    """Phase C 의 모든 메타 조회 뷰가 공유하는 DRF 설정."""

    authentication_classes = [ApiKeyAuthentication, SessionAuthentication]
    permission_classes = [HasScope]
    required_scope = S.META_READ
    throttle_classes = [ReadThrottle]


# ---------------------------------------------------------------------------
# Folder
# ---------------------------------------------------------------------------

@extend_schema(
    summary="인스턴스(Folder, 폴더) 목록 조회 — 게이트를 찾는 첫 단계",
    description=(
        "등록된 QA 환경(인스턴스, 이 API 에서는 \"Folder(폴더)\"라고 부릅니다) 목록을 조회합니다. "
        "보통 서비스나 조직 단위(예: AM, JK) 하나가 인스턴스 하나에 해당하고, 그 안에 Jenkins/"
        "Argo CD 접속 정보가 같이 묶여 있습니다.\n"
        "\n"
        "**언제 쓰나요**: 원하는 Quality Gate(게이트)를 실행하려면 그 게이트가 속한 "
        "**인스턴스 → 하위폴더(SubFolder) → 게이트** 순서로 계층을 따라 내려가며 id 를 찾아야 "
        "합니다. 이 API 가 그 첫 번째 단계입니다. 처음 연동을 시작할 때 전체 구조를 파악하는 "
        "용도로도 사용합니다.\n"
        "\n"
        "**보내는 것**: 전부 선택 입력 — 필터 파라미터는 없고, `page`(페이지 번호)로 결과 "
        "페이지만 넘길 수 있습니다. 아무것도 안 보내면 전체 목록의 1페이지를 반환합니다.\n"
        "\n"
        "**받는 것**: 목록의 각 항목은 `id`(다음 단계인 하위폴더 조회에 쓸 식별자), `name`"
        "(인스턴스 이름), `gate_count`(그 안에 있는 활성화된 게이트 수), `subfolder_count`"
        "(하위폴더 개수) 를 담고 있습니다."
    ),
    tags=[_META_TAG],
    parameters=[
        OpenApiParameter(
            name="page",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 결과가 많을 때 페이지 번호. 예: `2` — 두 번째 페이지. 안 넣으면 "
                "1페이지(기본 20건)입니다."
            ),
        ),
        OpenApiParameter(name="search", exclude=True),
        OpenApiParameter(name="ordering", exclude=True),
    ],
)
class FolderListView(_MetaReadMixin, generics.ListAPIView):
    serializer_class = FolderListSerializer
    queryset = QualityGateFolder.objects.all().order_by("name")


@extend_schema(
    summary="인스턴스(Folder, 폴더) 상세 조회",
    description=(
        "인스턴스(폴더) 하나의 상세 정보를 조회합니다. `GET /api/v2/folders/` 목록에서 확인한 "
        "`id` 를 경로에 넣어 호출합니다.\n"
        "\n"
        "**언제 쓰나요**: 목록에서는 안 보이는 세부 정보(Jenkins 접속 주소 등)까지 확인하고 "
        "싶을 때 사용합니다. 다음 단계로 넘어가려면(하위폴더 찾기) 목록 API 만으로도 충분하니, "
        "이 API 는 꼭 필요할 때만 호출하면 됩니다.\n"
        "\n"
        "**받는 것**: 목록 응답과 동일한 필드에 더해 `dev_jenkins_url`(개발 환경 Jenkins 주소), "
        "`prod_jenkins_url`(운영 환경 Jenkins 주소) 이 추가로 포함됩니다."
    ),
    tags=[_META_TAG],
    parameters=[
        OpenApiParameter(
            name="id",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.PATH,
            required=True,
            description="(필수) 조회할 인스턴스(폴더)의 id. `GET /api/v2/folders/` 응답의 id 입니다.",
        ),
    ],
)
class FolderDetailView(_MetaReadMixin, generics.RetrieveAPIView):
    serializer_class = FolderDetailSerializer
    queryset = QualityGateFolder.objects.all()
    lookup_field = "pk"


@extend_schema(
    summary="인스턴스 안의 하위폴더(SubFolder) 목록 조회 — 게이트를 찾는 두 번째 단계",
    description=(
        "인스턴스(폴더) 하나에 속한 하위폴더 목록을 조회합니다. 하위폴더는 인스턴스 안에서 "
        "게이트들을 다시 한 번 기능 단위로 분류해 놓은 폴더입니다(예: `auth`, `payment`).\n"
        "\n"
        "**언제 쓰나요**: `GET /api/v2/folders/` 로 얻은 인스턴스 `id` 를 경로에 넣어 호출하면, "
        "그 인스턴스 안에 있는 하위폴더들의 `id` 를 받을 수 있습니다. 그 다음 "
        "`GET /api/v2/subfolders/{id}/` 로 실제 게이트 목록까지 내려가면 됩니다.\n"
        "\n"
        "**보내는 것**: 경로 파라미터로 인스턴스 `id` 가 필수이고, 그 외 선택 입력으로 "
        "`page`(페이지 번호)를 쓸 수 있습니다.\n"
        "\n"
        "**받는 것**: 각 하위폴더의 `id`(다음 단계에 사용), `name`(하위폴더명), `folder_id`/"
        "`folder_name`(소속 인스턴스), `gate_count`(그 하위폴더 안의 활성 게이트 수)."
    ),
    tags=[_META_TAG],
    parameters=[
        OpenApiParameter(
            name="id",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.PATH,
            required=True,
            description="(필수) 하위폴더를 조회할 인스턴스(폴더)의 id. `GET /api/v2/folders/` 응답의 id 입니다.",
        ),
        OpenApiParameter(
            name="page",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 결과가 많을 때 페이지 번호. 예: `2` — 두 번째 페이지. 안 넣으면 "
                "1페이지(기본 20건)입니다."
            ),
        ),
        OpenApiParameter(name="search", exclude=True),
        OpenApiParameter(name="ordering", exclude=True),
    ],
)
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

@extend_schema(
    summary="하위폴더(SubFolder) 상세 조회 — 그 안의 게이트 목록까지 한 번에",
    description=(
        "하위폴더 하나의 상세 정보를 조회합니다. 응답의 `gates` 배열에 그 하위폴더에 속한 "
        "게이트들의 요약 정보(`id`, `name`, `gate_type`, `is_active`)가 함께 담겨 있어, "
        "실행할 게이트의 `id` 를 바로 여기서 찾을 수 있습니다.\n"
        "\n"
        "**언제 쓰나요**: `GET /api/v2/folders/{id}/subfolders/` 로 얻은 하위폴더 `id` 를 "
        "경로에 넣어 호출하면, 계층 구조를 따라 내려온 마지막 단계로서 실행 가능한 게이트 "
        "목록을 확인할 수 있습니다. 원하는 게이트의 `id` 를 찾았다면, 그 게이트를 실행하기 위해 "
        "`POST /api/v2/gates/{gate_id}/runs/` 를 호출하면 됩니다.\n"
        "\n"
        "**보내는 것**: 경로 파라미터로 하위폴더 `id` 만 필요합니다.\n"
        "\n"
        "**받는 것**: 하위폴더 자체 정보(`id`, `name`, `folder_id`/`folder_name`, "
        "`gate_count`) 에 더해 `gates`(그 안에 속한 게이트 요약 목록)."
    ),
    tags=[_META_TAG],
    parameters=[
        OpenApiParameter(
            name="id",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.PATH,
            required=True,
            description="(필수) 조회할 하위폴더의 id. `GET /api/v2/folders/{id}/subfolders/` 응답의 id 입니다.",
        ),
    ],
)
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


@extend_schema(
    summary="실행 가능한 Quality Gate(게이트) 목록 조회",
    description=(
        "실행할 수 있는 Quality Gate(게이트) 목록을 조회합니다. 게이트는 배포 전 품질을 검증하는 "
        "파이프라인 하나하나를 말하며(이미지 빌드 → 배포 → 테스트 → 운영 반영 순서로 진행), 이 "
        "목록에서 원하는 게이트의 `id` 를 찾으면 바로 `POST /api/v2/gates/{id}/runs/` 로 "
        "실행할 수 있습니다.\n"
        "\n"
        "**언제 쓰나요**: 이미 실행하려는 게이트의 이름이나 소속 인스턴스를 알고 있어서 폴더/"
        "하위폴더를 하나씩 타고 내려가지 않고 바로 검색하고 싶을 때 사용합니다. 아래 쿼리 "
        "파라미터로 원하는 게이트만 걸러 볼 수 있습니다.\n"
        "\n"
        "**받는 것**: 각 게이트의 `id`(실행 시 사용할 식별자), `name`, `gate_type`(`dev_stg`="
        "개발→스테이징 검증 / `stg_prd`=스테이징→운영 검증), `is_active`(비활성 게이트는 실행 "
        "불가), `auto_mode`(테스트 통과 시 다음 단계 자동 진행 여부), `parallel_test`(테스트 "
        "묶음들을 동시에 실행하는지), 소속 인스턴스/하위폴더 이름 등."
    ),
    tags=[_META_TAG],
    parameters=[
        OpenApiParameter(
            name="folder",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 이 인스턴스(폴더) id 에 속한 게이트만 봅니다. 예: "
                "`3fa85f64-5717-4562-b3fc-2c963f66afa6` — `GET /api/v2/folders/` 응답의 id."
            ),
        ),
        OpenApiParameter(
            name="subfolder",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 이 하위폴더 id 에 속한 게이트만 봅니다. 예: "
                "`3fa85f64-5717-4562-b3fc-2c963f66afa6` — `GET /api/v2/folders/{id}/subfolders/` 응답의 id."
            ),
        ),
        OpenApiParameter(
            name="gate_type",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            enum=["dev_stg", "stg_prd"],
            description=(
                "(선택) 게이트 종류로 좁혀 봅니다. 예: `dev_stg` — `dev_stg`=개발 환경에서 "
                "검증해 스테이징으로 배포, `stg_prd`=스테이징에서 검증해 운영으로 배포."
            ),
        ),
        OpenApiParameter(
            name="q",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 게이트 이름에 이 문자열이 포함된 것만 검색합니다(부분 일치, 대소문자 "
                "구분 없음). 예: `login` — 이름에 `login` 이 들어간 게이트만."
            ),
        ),
        OpenApiParameter(
            name="is_active",
            type=OpenApiTypes.BOOL,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 예: `true` — 활성화된 게이트만, `false` — 비활성화된 게이트만. "
                "생략하면 둘 다 포함됩니다."
            ),
        ),
        OpenApiParameter(
            name="page",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 결과가 많을 때 페이지 번호. 예: `2` — 두 번째 페이지. 안 넣으면 "
                "1페이지(기본 20건)입니다."
            ),
        ),
        OpenApiParameter(name="search", exclude=True),
        OpenApiParameter(name="ordering", exclude=True),
    ],
)
class GateListView(_MetaReadMixin, generics.ListAPIView):
    serializer_class = GateListSerializer
    queryset = _GATE_BASE_QS
    filterset_class = GateFilter


@extend_schema(
    summary="게이트 상세 조회 — 실행에 필요한 설정값 확인",
    description=(
        "게이트 하나의 상세 정보를 조회합니다. Jenkins Job 경로, Argo CD 연동 정보 등 실제로 "
        "실행할 때 쓰이는 설정값이 모두 담겨 있습니다.\n"
        "\n"
        "**언제 쓰나요**: `GET /api/v2/gates/` 목록에서 찾은 `id` 로 그 게이트의 세부 구성을 "
        "확인하고 싶을 때, 또는 최근 실행 이력이나 연결된 테스트 묶음까지 한 번에 보고 싶을 때 "
        "`include` 파라미터와 함께 사용합니다.\n"
        "\n"
        "**받는 것**: 목록 응답의 필드에 더해 각 단계(stage)별 Jenkins Job 경로/URL, Argo CD "
        "앱 URL·환경 등이 포함됩니다. `?include=` 를 주면 아래 부가 정보도 함께 옵니다."
    ),
    tags=[_META_TAG],
    parameters=[
        OpenApiParameter(
            name="id",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.PATH,
            required=True,
            description="(필수) 조회할 게이트의 id. `GET /api/v2/gates/` 응답의 id 입니다.",
        ),
        OpenApiParameter(
            name="include",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 응답에 추가로 포함할 부가 정보를 콤마(,)로 구분해 지정합니다. 예: "
                "`latest_run,test_suites`. 지정하지 않은 항목은 응답에서 아예 빠집니다(=`null` "
                "이 아니라 필드 자체가 없음). `latest_run`(이 게이트를 가장 최근에 실행한 Run 의 "
                "요약 — 상태/합불/실행 번호) / `test_suites`(이 게이트가 실행 대상으로 연결해 둔 "
                "Test Suite 목록과 실행 순서)."
            ),
            examples=[
                OpenApiExample("최신 Run + 연결 Suite 둘 다 포함", value="latest_run,test_suites"),
                OpenApiExample("최신 Run 만 포함", value="latest_run"),
            ],
        ),
    ],
)
class GateDetailView(_MetaReadMixin, generics.RetrieveAPIView):
    serializer_class = GateDetailSerializer
    queryset = _GATE_BASE_QS
    lookup_field = "pk"


class GateParamsView(_MetaReadMixin, APIView):
    """`/api/v2/gates/<uuid>/params/?stage=image|deploy|prod&refresh=1`.

    기존 `gate_run_params` / `deploy_params` / `prod_deploy_params` 와 동일하게
    동작 — services.get_job_param_schema 로 통합.
    """

    @extend_schema(
        summary="게이트 실행 전, 단계별로 입력 가능한 옵션(파라미터) 목록 미리보기",
        description=(
            "Quality Gate(게이트)를 실제로 실행(`POST /api/v2/gates/{id}/runs/`)하기 **전에**, "
            "이미지 빌드/배포/운영배포 각 단계(stage, 파이프라인의 한 구간)에서 어떤 값을 입력할 "
            "수 있는지 미리 확인하는 API 입니다. 여기 나오는 파라미터들은 그 단계에 연결된 Jenkins "
            "Job 이 원래 가지고 있는 빌드 파라미터를 그대로 가져온 것입니다(예: 배포할 브랜치명, "
            "이미지 태그 등).\n"
            "\n"
            "**언제 쓰나요**: 게이트를 실행할 때 기본값을 그대로 쓰지 않고 `parameters` 값을 직접 "
            "지정하고 싶다면, 먼저 이 API 로 어떤 이름의 파라미터가 있고 기본값이 무엇인지 확인한 "
            "뒤 그 이름 그대로 실행 API 의 `parameters` 객체에 담아 보내면 됩니다.\n"
            "\n"
            "**한 번에 한 단계씩 조회**: 게이트 하나는 최대 3단계(`image`=이미지 빌드, `deploy`="
            "검수 대상 환경 배포, `prod`=검수 통과 후 운영 배포)로 이루어져 있고, 단계마다 연결된 "
            "Jenkins Job 과 파라미터 구성이 다를 수 있어 `stage` 파라미터로 매번 하나씩만 조회합니다. "
            "게이트에 그 단계가 아예 설정돼 있지 않으면(예: 이미지 빌드를 생략하는 게이트) "
            "`parameters` 가 빈 배열로 옵니다.\n"
            "\n"
            "**캐시**: 매번 Jenkins 에 직접 물어보면 느리므로 120초 동안 결과를 저장해 두고 "
            "재사용합니다 — 응답의 `cached` 값이 `true` 면 저장해 둔 값을 준 것입니다. 방금 "
            "Jenkins Job 설정을 바꿔서 최신 값이 꼭 필요하면 `refresh=1` 을 붙여 캐시를 건너뛸 "
            "수 있습니다(평소에는 생략해도 충분합니다)."
        ),
        tags=[_META_TAG],
        parameters=[
            OpenApiParameter(
                name="id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                required=True,
                description="(필수) 파라미터를 조회할 게이트의 id. `GET /api/v2/gates/` 응답의 id 입니다.",
            ),
            OpenApiParameter(
                name="stage",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=True,
                enum=["image", "deploy", "prod"],
                description=(
                    "(필수) 조회할 파이프라인 단계입니다. 예: `deploy`. `image`=이미지 빌드, "
                    "`deploy`=검수 대상(스테이징 등) 배포, `prod`=검수 통과 후 운영 배포."
                ),
            ),
            OpenApiParameter(
                name="refresh",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "(선택) `1` 을 보내면 120초 캐시를 무시하고 Jenkins 에서 방금 값을 다시 "
                    "가져옵니다. 예: `1`. 평소에는 생략해도 됩니다."
                ),
            ),
        ],
        responses={
            200: ParamsStageSerializer,
            400: OpenApiResponse(
                description=(
                    "`invalid_parameters` — `stage` 파라미터가 없거나 `image`/`deploy`/`prod` "
                    "중 하나가 아닙니다. `stage` 값을 확인해 다시 요청해 주세요."
                ),
            ),
            502: OpenApiResponse(
                description=(
                    "`jenkins_error` — Jenkins 에 파라미터 스키마를 물어보는 데 실패했습니다 "
                    "(Jenkins 서버 접속 불가, 인증 실패 등). 어느 단계에서 실패했는지는 "
                    "`error.details.stage` 로 알 수 있습니다. Jenkins 연결 상태를 확인한 뒤 "
                    "다시 시도해 주세요."
                ),
            ),
        },
        examples=[
            OpenApiExample(
                "응답 예시 — deploy 단계",
                value={
                    "stage": "deploy",
                    "cached": True,
                    "parameters": [
                        {
                            "name": "BRANCH_NAME",
                            "type": "GitParameterDefinition",
                            "default": "main",
                        },
                    ],
                },
                response_only=True,
            ),
        ],
    )
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


@extend_schema(
    summary="Test Suite(테스트 묶음) 목록 조회",
    description=(
        "실행할 수 있는 Test Suite(테스트 묶음) 목록을 조회합니다. Test Suite 는 함께 실행되는 "
        "자동화 테스트 묶음 하나를 말하며(Jenkins 폴더 하나에 해당), 그 안에는 Job(잡, 개별 "
        "실행 단위)이 여러 개 들어있을 수 있습니다. Quality Gate 계층(인스턴스/하위폴더/게이트)과는 "
        "별개의 독립된 목록입니다 — 게이트가 실행 대상으로 특정 Suite 를 연결해 두는 관계입니다.\n"
        "\n"
        "**언제 쓰나요**: 게이트 파이프라인과 무관하게 테스트 묶음 자체를 단독으로 실행하고 "
        "싶을 때(`POST /api/v2/suites/{id}/runs/`) 그 대상의 `id` 를 이 목록에서 찾습니다.\n"
        "\n"
        "**받는 것**: 각 Suite 의 `id`(실행 시 사용할 식별자), `name`, `is_active`(비활성이면 "
        "실행 불가), `job_count`(그 안에 있는 Job 개수), `service_id`/`service_name`(연결된 "
        "서비스), `is_external`(외부 Jenkins 를 쓰는 Suite 인지 여부)."
    ),
    tags=[_META_TAG],
    parameters=[
        OpenApiParameter(
            name="q",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) Suite 이름에 이 문자열이 포함된 것만 검색합니다(부분 일치, 대소문자 "
                "구분 없음). 예: `smoke` — 이름에 `smoke` 가 들어간 Suite 만."
            ),
        ),
        OpenApiParameter(
            name="service",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 이 서비스 id 에 연결된 Suite 만 봅니다. 예: "
                "`3fa85f64-5717-4562-b3fc-2c963f66afa6`."
            ),
        ),
        OpenApiParameter(
            name="is_active",
            type=OpenApiTypes.BOOL,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 예: `true` — 활성화된 Suite 만, `false` — 비활성화된 Suite 만. "
                "생략하면 둘 다 포함됩니다."
            ),
        ),
        OpenApiParameter(
            name="page",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                "(선택) 결과가 많을 때 페이지 번호. 예: `2` — 두 번째 페이지. 안 넣으면 "
                "1페이지(기본 20건)입니다."
            ),
        ),
        OpenApiParameter(name="search", exclude=True),
        OpenApiParameter(name="ordering", exclude=True),
    ],
)
class SuiteListView(_MetaReadMixin, generics.ListAPIView):
    serializer_class = SuiteListSerializer
    queryset = _SUITE_BASE_QS
    filterset_class = SuiteFilter


@extend_schema(
    summary="Test Suite(테스트 묶음) 상세 조회 — 그 안의 Job(잡) 목록 포함",
    description=(
        "Test Suite 하나의 상세 정보를 조회합니다. 응답의 `jobs` 배열에 그 Suite 안에 있는 "
        "Job(잡, 개별 테스트 실행 단위) 목록이 함께 담겨 있습니다.\n"
        "\n"
        "**언제 쓰나요**: `GET /api/v2/suites/` 목록에서 찾은 `id` 로 이 Suite 를 실행하기 전에 "
        "구성(어떤 Job 들이 있는지, 몇 개가 활성 상태인지)을 확인하고 싶을 때 사용합니다. Suite 를 "
        "실행할 때는 보통 `job_id` 없이 idle(지금 놀고 있는) Job 을 자동 선택하도록 두지만, 특정 "
        "Job 하나만 지정해서 실행하고 싶다면 여기서 그 Job 의 `id` 를 확인해 사용하세요.\n"
        "\n"
        "**받는 것**: 목록 응답의 필드에 더해 `jobs`(각 Job 의 `id`, `job_name`, "
        "`job_full_path`, `is_active`, `is_target`=실행 대상 여부, `order`=실행 순서)."
    ),
    tags=[_META_TAG],
    parameters=[
        OpenApiParameter(
            name="id",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.PATH,
            required=True,
            description="(필수) 조회할 Test Suite 의 id. `GET /api/v2/suites/` 응답의 id 입니다.",
        ),
    ],
)
class SuiteDetailView(_MetaReadMixin, generics.RetrieveAPIView):
    serializer_class = SuiteDetailSerializer
    queryset = _SUITE_BASE_QS.prefetch_related("jobs")
    lookup_field = "pk"
