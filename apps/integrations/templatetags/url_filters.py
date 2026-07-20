"""Error Log 화면의 "+ 등록" 동선에서 URL 을 path/host 로 쪼개기 위한 템플릿 필터.

improvement.md §6.3 - 매칭 실패 URL 의 등록 prefill 시 endpoint 는 path 부분만,
host 는 netloc 부분으로 분리해 전달한다.
"""
from urllib.parse import urlsplit

from django import template

register = template.Library()


@register.filter
def url_path(value):
    """URL 에서 path 부분만 추출. 실패 시 원본 그대로 반환."""
    try:
        parts = urlsplit(value or "")
        return parts.path or value
    except Exception:
        return value


@register.filter
def url_host(value):
    """URL 에서 netloc(host) 부분만 추출. 실패 시 빈 문자열."""
    try:
        return urlsplit(value or "").netloc
    except Exception:
        return ""
