"""모든 템플릿(base.html)에 포털 UI 설정을 주입하는 context processor.

`portal_settings.show_quality_gate` 로 좌측 사이드바의 Quality Gate 메뉴 표시 여부를
결정한다. base.py TEMPLATES 의 context_processors 에 등록되어 있다.

- 렌더 경로이므로 **읽기만** 한다(`.first()`). row 가 없으면(설정 저장 전) 기본 노출.
- 테이블 미존재(마이그레이션 전) 등 예외에도 기본 노출로 안전하게 폴백.
- dict 로 노출해 row 부재 시에도 `{% if portal_settings.show_quality_gate %}` 가
  안전하게 동작(기본 True)하도록 한다.
"""

from __future__ import annotations

from django.conf import settings


def portal_settings(request):
    show_quality_gate = True
    try:
        from .models import PortalSettings
        obj = PortalSettings.objects.first()
        if obj is not None:
            show_quality_gate = obj.show_quality_gate
    except Exception:
        # 마이그레이션 전/DB 오류 등 — 메뉴는 기본 노출.
        pass
    return {
        "portal_settings": {"show_quality_gate": show_quality_gate},
        # API Key 화면의 호출 예시용 — 프로그램 호출 주소(게이트웨이 로그인 예외 경로).
        "ci_api_base": settings.CI_API_BASE,
    }
