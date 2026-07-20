"""Jenkins 콘솔 로그에서 실패 원인 부분만 추출하는 유틸.

Quality Gate (`image_console_output`, `deploy_console_output`, `prod_console_output`) 와
Test Suite job (`TestSuiteJobRun.console_output`) 양쪽에서 동일한 추출 규칙을 쓰기 위해
공용 모듈로 둔다.

추출 우선순위 (자동화 스크립트의 마킹 → 일반 pytest 출력 → 파이프라인 키워드 순):

1. `@@@...@@@` (10자 이상 연속된 `@`) 라인 사이 블록 — am-mapp-regression-api 등
   사내 자동화 스크립트가 최종 실패 step 을 의도적으로 감싸 둔 영역.
2. pytest `=== FAILURES ===` 블록 + `short test summary info`.
3. `[FAIL]` / `[STOP]` / `❌ [Step]` 등 키워드 라인 주변 컨텍스트.

전부 못 찾으면 빈 문자열을 반환한다 (호출 측이 "에러 로그를 찾지 못함" 안내).
"""

from __future__ import annotations

import re

_AT_BLOCK_LINE = re.compile(r"^\s*@{10,}\s*$")
_PYTEST_FAILURES_HDR = re.compile(r"^=+\s*FAILURES\s*=+\s*$")
_PYTEST_SHORT_SUMMARY_HDR = re.compile(r"^=+\s*short test summary info\s*=+\s*$")
_PYTEST_END_HDR = re.compile(r"^=+\s*(passed|failed|error|warnings).*=+\s*$", re.IGNORECASE)
_FAIL_KEYWORDS = (
    "❌ [Step]",
    "[FAIL]",
    "[STOP]",
    "FAILED ",
    "AssertionError",
)


def extract_error_log(console_output: str | None) -> str:
    """콘솔 로그에서 실패 원인 부분만 추출해 문자열로 돌려준다.

    추출 가능한 마커가 하나도 없으면 빈 문자열.
    """
    if not console_output:
        return ""
    text = console_output.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    blocks = _extract_at_blocks(lines)
    if blocks:
        return "\n\n".join(blocks).rstrip()

    pytest_block = _extract_pytest_failures(lines)
    if pytest_block:
        return pytest_block.rstrip()

    keyword_block = _extract_keyword_context(lines)
    if keyword_block:
        return keyword_block.rstrip()

    return ""


def _extract_at_blocks(lines: list[str]) -> list[str]:
    """`@@@...@@@` 로 둘러싸인 블록들을 모두 수집.

    중첩이나 짝이 안 맞는 경우(시작 `@@@` 만 있고 닫는 줄이 없는 경우)도
    파일 끝까지 포함해 가능한 정보는 모두 보여준다.
    """
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _AT_BLOCK_LINE.match(lines[i]):
            buf: list[str] = []
            j = i + 1
            while j < n and not _AT_BLOCK_LINE.match(lines[j]):
                buf.append(lines[j])
                j += 1
            if buf:
                content = "\n".join(buf).strip("\n")
                if content.strip():
                    blocks.append(content)
            i = j + 1
        else:
            i += 1
    return blocks


def _extract_pytest_failures(lines: list[str]) -> str:
    """pytest 의 `=== FAILURES ===` 부터 다음 `=== ... ===` 헤더 전까지 추출.

    `short test summary info` 가 별도로 있으면 그것도 함께 붙인다.
    """
    out_parts: list[str] = []

    failures_idx = _find_first(lines, _PYTEST_FAILURES_HDR)
    if failures_idx is not None:
        end_idx = _find_next_section(lines, failures_idx + 1)
        out_parts.append("\n".join(lines[failures_idx:end_idx]).rstrip())

    summary_idx = _find_first(lines, _PYTEST_SHORT_SUMMARY_HDR)
    if summary_idx is not None:
        end_idx = _find_next_section(lines, summary_idx + 1)
        block = "\n".join(lines[summary_idx:end_idx]).rstrip()
        if block:
            out_parts.append(block)

    return "\n\n".join(out_parts)


def _extract_keyword_context(lines: list[str], context: int = 2) -> str:
    """`[FAIL]` 등 키워드 라인 주변 컨텍스트를 모아 반환.

    같은 키워드가 반복되면 라인 번호 기준 ±`context` 라인을 묶고,
    묶인 영역끼리 겹치면 하나로 합친다.
    """
    hits = [
        idx
        for idx, line in enumerate(lines)
        if any(kw in line for kw in _FAIL_KEYWORDS)
    ]
    if not hits:
        return ""

    ranges: list[tuple[int, int]] = []
    for idx in hits:
        start = max(0, idx - context)
        end = min(len(lines), idx + context + 1)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    parts = ["\n".join(lines[s:e]).rstrip() for s, e in ranges]
    return "\n…\n".join(p for p in parts if p.strip())


def _find_first(lines: list[str], pattern: re.Pattern[str]) -> int | None:
    for idx, line in enumerate(lines):
        if pattern.match(line):
            return idx
    return None


def _find_next_section(lines: list[str], start: int) -> int:
    for idx in range(start, len(lines)):
        if _PYTEST_END_HDR.match(lines[idx]) or _PYTEST_SHORT_SUMMARY_HDR.match(lines[idx]):
            return idx
    return len(lines)
