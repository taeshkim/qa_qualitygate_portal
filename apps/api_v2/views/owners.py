"""담당자 관리 API — API 엔드포인트 담당자 매핑 조회/등록/수정/삭제 (`/api/v2/owners/`).

UI `담당자 관리 > API` 탭(`/accounts/api-owners/`)과 같은 데이터(`ApiEndpointOwner`)·같은 부수효과를 쓴다:
수동 row(`is_imported=False`) 로 저장, URL 매칭 캐시 무효화, ActivityLog 기록, (옵션) Google Sheet 역방향 기록.
인증은 API Key 만 — `owners:manage` 명시 스코프 + 키 소유자 admin (`OwnersManagePermission`).
"""

from __future__ import annotations

from django.db.models import Q
from django.http import Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.activitylog.models import ActivityLog
from apps.integrations.models import ApiEndpointOwner

from ..authentication import ApiKeyAuthentication
from ..permissions import OwnersManagePermission
from ..serializers.owners import ApiEndpointOwnerSerializer
from ..throttling import ReadThrottle, WriteThrottle

_TAGS = ["담당자 관리"]

_EXAMPLE_BODY = {
    "repo": "JK PC",
    "method": "POST",
    "endpoint": "/member/users/edit-by-pop-up",
    "category": "회원",
    "area": "개인회원",
    "fe_owner": "김프론트",
    "be_owner": "김백엔드, 이백엔드",
    "notes": "",
}


class DuplicateOwner(Exception):
    """`(method, endpoint)` 가 이미 있음 — 409 `already_exists` (exceptions._code_for 의 code/http_status 규약)."""

    code = "already_exists"
    http_status = status.HTTP_409_CONFLICT

    def __init__(self, existing: ApiEndpointOwner):
        super().__init__(f"이미 등록된 (method, endpoint) 입니다. existing_id={existing.pk}")
        self.details = {"existing_id": str(existing.pk)}


class _OwnersPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


def _invalidate_cache():
    from apps.integrations.url_matcher import invalidate_match_cache

    invalidate_match_cache()


def _log(request, action, owner, label=None):
    key = getattr(request, "auth", None)
    ActivityLog.log(
        user=request.user,
        action=action,
        target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
        target=owner,
        target_name=label or f"{owner.method} {owner.endpoint}",
        detail=f"repo={owner.repo} via=api key={getattr(key, 'key_prefix', '')}",
    )


def _assert_unique(method: str, endpoint: str, exclude_pk=None):
    qs = ApiEndpointOwner.objects.filter(method=method, endpoint=endpoint)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    existing = qs.first()
    if existing is not None:
        raise DuplicateOwner(existing)


class _OwnersBase(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [OwnersManagePermission]


class OwnerCollectionView(_OwnersBase):
    def get_throttles(self):
        return [(WriteThrottle if self.request.method == "POST" else ReadThrottle)()]

    @extend_schema(
        summary="담당자 매핑 목록 조회 (검색·필터·페이지)",
        description=(
            "담당자 관리 화면(`/accounts/api-owners/`)에 보이는 API 엔드포인트별 담당자 매핑을 조회합니다.\n"
            "\n"
            "**필요 권한**: `owners:manage` 스코프가 명시된 API Key + 키 소유자가 관리자(admin). "
            "`*` 와일드카드로는 이 스코프가 부여되지 않습니다.\n"
            "\n"
            "**필터**: `q`(repo/카테고리/endpoint/영역/FE·BE 담당자 부분일치), `repo`, `method`, "
            "`mapped_app`, `is_imported`(`true`/`false`). 페이지는 `page`, `page_size`(기본 50, 최대 200)."
        ),
        tags=_TAGS,
        parameters=[
            OpenApiParameter("q", OpenApiTypes.STR, description="통합 검색어(부분일치)."),
            OpenApiParameter("repo", OpenApiTypes.STR, description="레포 정확히 일치. 예: `JK PC`."),
            OpenApiParameter("method", OpenApiTypes.STR, description="HTTP Method (대소문자 무관)."),
            OpenApiParameter("mapped_app", OpenApiTypes.STR, description="매핑 앱 정확히 일치."),
            OpenApiParameter("is_imported", OpenApiTypes.BOOL, description="Sheet 동기화 row 만(true) / 수동 등록 row 만(false)."),
            OpenApiParameter("page", OpenApiTypes.INT, description="페이지 번호(1부터)."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="페이지 크기(기본 50, 최대 200)."),
        ],
        responses={200: ApiEndpointOwnerSerializer(many=True)},
    )
    def get(self, request):
        qs = ApiEndpointOwner.objects.all()
        p = request.query_params
        q = (p.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(repo__icontains=q) | Q(category__icontains=q) | Q(endpoint__icontains=q)
                | Q(area__icontains=q) | Q(fe_owner__icontains=q) | Q(be_owner__icontains=q)
            )
        if (repo := (p.get("repo") or "").strip()):
            qs = qs.filter(repo=repo)
        if (method := (p.get("method") or "").strip()):
            qs = qs.filter(method=method.upper())
        if (mapped_app := (p.get("mapped_app") or "").strip()):
            qs = qs.filter(mapped_app=mapped_app)
        imported = (p.get("is_imported") or "").strip().lower()
        if imported in ("true", "1", "yes"):
            qs = qs.filter(is_imported=True)
        elif imported in ("false", "0", "no"):
            qs = qs.filter(is_imported=False)

        paginator = _OwnersPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(ApiEndpointOwnerSerializer(page, many=True).data)

    @extend_schema(
        summary="담당자 매핑 등록",
        description=(
            "담당자 관리 > API 탭의 **추가** 와 같은 동작입니다. 수동 row(`is_imported=false`)로 저장되어 "
            "Google Sheet 동기화가 덮어쓰지 않으며, 저장 즉시 Slack 자동 멘션 매칭에 반영됩니다.\n"
            "\n"
            "**필수**: `repo`, `method`, `endpoint`. 나머지는 선택입니다. "
            "`(method, endpoint)` 가 이미 있으면 `409 already_exists` 와 `details.existing_id` 를 돌려주니 "
            "그 id 로 `PATCH` 하세요.\n"
            "\n"
            "**필요 권한**: `owners:manage` 스코프 + 관리자 소유 키."
        ),
        tags=_TAGS,
        request=ApiEndpointOwnerSerializer,
        examples=[OpenApiExample("등록 예시", value=_EXAMPLE_BODY, request_only=True)],
        responses={
            201: ApiEndpointOwnerSerializer,
            400: OpenApiResponse(description="`invalid_parameters` — 필수값 누락/형식 오류."),
            409: OpenApiResponse(description="`already_exists` — 같은 (method, endpoint) 가 이미 있음."),
        },
    )
    def post(self, request):
        ser = ApiEndpointOwnerSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        _assert_unique(data["method"], data["endpoint"])
        owner = ApiEndpointOwner(**data)
        owner.is_imported = False
        owner.save()
        _invalidate_cache()
        from apps.integrations.services import writeback_owner_to_sheet_async

        writeback_owner_to_sheet_async(owner)
        _log(request, ActivityLog.Action.API_OWNER_CREATED, owner)
        return Response(ApiEndpointOwnerSerializer(owner).data, status=status.HTTP_201_CREATED)


_ID_PARAM = OpenApiParameter(
    "id", OpenApiTypes.UUID, location=OpenApiParameter.PATH,
    description="담당자 row id. 목록 조회/등록 응답의 `id`.",
)


class OwnerDetailView(_OwnersBase):
    def get_throttles(self):
        return [(ReadThrottle if self.request.method == "GET" else WriteThrottle)()]

    @staticmethod
    def _get(pk):
        try:
            return ApiEndpointOwner.objects.get(pk=pk)
        except ApiEndpointOwner.DoesNotExist:
            raise Http404("owner_not_found")

    @extend_schema(summary="담당자 매핑 단건 조회", tags=_TAGS, parameters=[_ID_PARAM],
                   responses={200: ApiEndpointOwnerSerializer, 404: OpenApiResponse(description="`not_found`")})
    def get(self, request, pk):
        return Response(ApiEndpointOwnerSerializer(self._get(pk)).data)

    def _update(self, request, pk, partial):
        owner = self._get(pk)
        ser = ApiEndpointOwnerSerializer(owner, data=request.data, partial=partial)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        _assert_unique(data.get("method", owner.method), data.get("endpoint", owner.endpoint), exclude_pk=owner.pk)
        saved = ser.save()
        _invalidate_cache()
        _log(request, ActivityLog.Action.API_OWNER_UPDATED, saved)
        return Response(ApiEndpointOwnerSerializer(saved).data)

    @extend_schema(
        summary="담당자 매핑 부분 수정 (보낸 필드만 변경)",
        description="보낸 필드만 바꿉니다. 예: 담당자만 교체하려면 `{\"fe_owner\": \"홍길동\"}`. "
                    "`method`/`endpoint` 를 바꿔 다른 row 와 겹치면 `409 already_exists`.",
        tags=_TAGS, parameters=[_ID_PARAM], request=ApiEndpointOwnerSerializer,
        examples=[OpenApiExample("담당자만 변경", value={"fe_owner": "홍길동", "be_owner": "김백엔드"}, request_only=True)],
        responses={200: ApiEndpointOwnerSerializer, 404: OpenApiResponse(description="`not_found`"),
                   409: OpenApiResponse(description="`already_exists`")},
    )
    def patch(self, request, pk):
        return self._update(request, pk, partial=True)

    @extend_schema(
        summary="담당자 매핑 전체 수정 (필수 필드 모두 필요)",
        tags=_TAGS, parameters=[_ID_PARAM], request=ApiEndpointOwnerSerializer,
        responses={200: ApiEndpointOwnerSerializer, 404: OpenApiResponse(description="`not_found`"),
                   409: OpenApiResponse(description="`already_exists`")},
    )
    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    @extend_schema(summary="담당자 매핑 삭제", tags=_TAGS, parameters=[_ID_PARAM],
                   responses={204: OpenApiResponse(description="삭제됨"),
                              404: OpenApiResponse(description="`not_found`")})
    def delete(self, request, pk):
        owner = self._get(pk)
        label = f"{owner.method} {owner.endpoint}"
        snapshot = ApiEndpointOwner(pk=owner.pk, repo=owner.repo, method=owner.method, endpoint=owner.endpoint)
        owner.delete()
        _invalidate_cache()
        _log(request, ActivityLog.Action.API_OWNER_DELETED, snapshot, label=label)
        return Response(status=status.HTTP_204_NO_CONTENT)
