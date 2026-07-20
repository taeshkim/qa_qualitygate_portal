"""api_v2 메타 조회 FilterSet (Phase C).

PRD §5 의 URL 라우팅 필터 컬럼을 그대로 매핑:
- gates: folder, subfolder, gate_type, q
- suites: q, service
"""

from __future__ import annotations

import django_filters

from apps.qualitygates.models import QualityGate
from apps.suites.models import TestSuite


class GateFilter(django_filters.FilterSet):
    folder = django_filters.UUIDFilter(field_name="folder_id")
    subfolder = django_filters.UUIDFilter(field_name="subfolder_id")
    gate_type = django_filters.ChoiceFilter(choices=QualityGate.GateType.choices)
    q = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = QualityGate
        fields = ("folder", "subfolder", "gate_type", "q", "is_active")


class SuiteFilter(django_filters.FilterSet):
    service = django_filters.UUIDFilter(field_name="service_id")
    q = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = TestSuite
        fields = ("service", "q", "is_active")
