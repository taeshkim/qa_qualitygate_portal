from django.test import SimpleTestCase

from .diagnostics import analyze_console_output


class ConsoleDiagnosticsTests(SimpleTestCase):
    def test_resume_browser_failure_is_summarized(self):
        log = """
[12:07:17] INFO [Step 14] tool: click args={"uid": "7_74", "includeSnapshot": false}
[12:07:22] INFO [Step 14] click 실패 (5.0초): Timed out while waiting for response to ClientRequest. Waited 5.0 seconds.
[12:08:40] INFO [Step 21] tool: click args={"uid": "7_74"}
[12:08:46] INFO [Step 21] click 실패 (5.0초): Timed out while waiting for response to ClientRequest. Waited 5.0 seconds.
[12:12:59] INFO [C17508] FAILED: 스텝 12: '고등학교 미만 졸업' 체크박스 클릭이 수행되지 않았음.; 스텝 13~15: 희망근무지 '추가하기' 클릭 및 팝업에서 '서울 > 서초구' 선택 후 닫기 동작이 수행되지 않음.; 스텝 16(기대결과): '이력서 작성 완료 페이지로 이동'하지 않음. 현재 페이지 URL이 여전히 https://jts.jobkorea.co.kr/User/Resume/Write 이며, 화면에 '저장 완료' 메시지는 보이지만 완료 페이지로 리다이렉트되지 않았음.
"""

        result = analyze_console_output(log)

        self.assertTrue(result["has_findings"])
        self.assertEqual(result["case_id"], "C17508")
        self.assertEqual(result["suspect_uids"][0]["uid"], "7_74")
        self.assertEqual(result["suspect_uids"][0]["count"], 2)
        self.assertEqual(result["stuck_url"], "https://jts.jobkorea.co.kr/User/Resume/Write")
        self.assertTrue(any("체크박스" in item for item in result["summary"]))
        self.assertTrue(any("희망근무지" in item for item in result["summary"]))
        self.assertTrue(any("저장 메시지" in item for item in result["summary"]))

    def test_no_console_output_returns_empty_diagnostics(self):
        result = analyze_console_output("")

        self.assertFalse(result["has_findings"])
        self.assertEqual(result["summary"], [])
        self.assertEqual(result["actions"], [])
