import re
from collections import Counter, defaultdict


CLICK_TOOL_RE = re.compile(
    r"\[Step (?P<step>\d+)\]\s+tool:\s+click\s+args=.*?\"uid\"\s*:\s*\"(?P<uid>[^\"]+)\""
)
CLICK_FAIL_RE = re.compile(r"\[Step (?P<step>\d+)\]\s+click 실패.*?:\s*(?P<reason>.+)$")
FINAL_FAIL_RE = re.compile(r"\[(?P<case_id>C\d+)\]\s+FAILED:\s*(?P<message>.+)$")
STILL_URL_RE = re.compile(r"현재 페이지 URL이 여전히\s+(?P<url>https?://\S+)")


def analyze_console_output(console_output: str) -> dict:
    """Jenkins 콘솔에서 LLM 브라우저 자동화 실패 패턴을 요약한다.

    포털은 테스트 러너를 직접 실행하지 않으므로, 여기서는 원문 로그를 바탕으로
    수정자가 바로 볼 수 있는 selector/prompt 보강 힌트를 계산한다.
    """
    text = console_output or ""
    step_uids = {}
    click_failures = []
    final_case_id = ""
    final_failure = ""
    stuck_url = ""

    for line in text.splitlines():
        click_tool = CLICK_TOOL_RE.search(line)
        if click_tool:
            step_uids[click_tool.group("step")] = click_tool.group("uid")
            continue

        click_fail = CLICK_FAIL_RE.search(line)
        if click_fail:
            step = click_fail.group("step")
            click_failures.append({
                "step": step,
                "uid": step_uids.get(step, ""),
                "reason": click_fail.group("reason").strip(),
            })
            continue

        final_fail = FINAL_FAIL_RE.search(line)
        if final_fail:
            final_case_id = final_fail.group("case_id")
            final_failure = final_fail.group("message").strip()

        still_url = STILL_URL_RE.search(line)
        if still_url:
            stuck_url = still_url.group("url")

    uid_counts = Counter(f["uid"] for f in click_failures if f["uid"])
    uid_steps = defaultdict(list)
    for failure in click_failures:
        if failure["uid"]:
            uid_steps[failure["uid"]].append(failure["step"])

    suspect_uids = [
        {
            "uid": uid,
            "count": count,
            "steps": ", ".join(uid_steps[uid]),
        }
        for uid, count in uid_counts.most_common()
        if count >= 2
    ]

    summary = []
    actions = []

    for suspect in suspect_uids[:3]:
        summary.append(
            f"동일 요소 uid={suspect['uid']} 클릭이 {suspect['count']}회 실패했습니다 "
            f"(steps {suspect['steps']}). UID snapshot이 오래됐거나 숨김/styled control일 가능성이 큽니다."
        )

    if "고등학교 미만 졸업" in text:
        summary.append("'고등학교 미만 졸업' 체크박스가 체크 완료 상태로 검증되지 않았습니다.")
        actions.append(
            "체크박스는 라벨 텍스트로 input을 다시 찾고, 클릭 후 checked 상태를 검증하세요. "
            "styled/hidden input이면 JS로 checked=true 설정 후 input/change 이벤트를 dispatch하는 fallback을 사용하세요."
        )

    if "희망근무지" in text:
        summary.append("희망근무지 추가/지역 선택 흐름이 완료되지 않았습니다.")
        actions.append(
            "'추가하기' 버튼은 페이지 전체 UID가 아니라 '희망근무지' 섹션 내부로 scope를 제한해 찾고, "
            "팝업에서는 '서울 > 서초구' 선택 결과가 본문에 반영됐는지 확인한 뒤 닫으세요."
        )

    if "저장 완료" in text and ("완료 페이지" in text or stuck_url):
        summary.append("저장 메시지는 확인됐지만 완료 페이지 이동 검증은 실패했습니다.")
        actions.append(
            "'저장 완료' toast를 pass 조건으로 보지 말고, 최종 완료 버튼 클릭 후 URL이 작성 화면을 벗어났는지 "
            "또는 완료 페이지 고유 문구가 보이는지 검증하세요."
        )

    if final_failure and not summary:
        summary.append(final_failure)

    if suspect_uids:
        actions.insert(
            0,
            "반복 실패한 UID는 재사용하지 말고 최신 snapshot/evaluate_script로 대상 DOM을 다시 찾으세요.",
        )

    return {
        "has_findings": bool(summary or actions or suspect_uids or final_failure),
        "case_id": final_case_id,
        "summary": summary,
        "actions": actions,
        "suspect_uids": suspect_uids,
        "stuck_url": stuck_url,
        "final_failure": final_failure,
    }
