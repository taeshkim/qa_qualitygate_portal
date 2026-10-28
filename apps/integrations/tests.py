"""apps.integrations 의 CSV 임포트 서비스 + URL 매칭 엔진 단위 테스트.

`improvement.md` §9.1 의 8개 + URL 매칭 케이스.
"""
from __future__ import annotations

import os
import tempfile

from django.test import Client, TestCase

from apps.integrations.models import ApiEndpointOwner, ApiHostMapping
from apps.integrations.services import import_api_endpoint_owners
from apps.integrations.url_matcher import (
    MatchedUrl,
    extract_urls,
    find_endpoint_owner,
    invalidate_match_cache,
)


# 표준 헤더 (담당팀 제거된 신규 포맷). 옛 헤더 alias 검증은 별도 테스트.
HEADER_LINE = (
    "레포,카테고리,영역,Method,Endpoint,"
    "FE 담당자,BE 담당자,"
    "매핑 앱,사용 횟수,step_ids"
)


def _write_csv(lines: list[str], *, with_bom: bool = True) -> str:
    """임시 CSV 파일을 만들고 경로를 리턴. 호출자가 unlink 책임."""
    body = "\n".join(lines)
    if with_bom:
        body = "﻿" + body
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    return path


class ImportApiOwnersTests(TestCase):
    def setUp(self):
        self._tmp_files: list[str] = []

    def tearDown(self):
        for p in self._tmp_files:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def _make_csv(self, lines, *, with_header: bool = True, **kwargs) -> str:
        if with_header:
            lines = [HEADER_LINE] + list(lines)
        path = _write_csv(list(lines), **kwargs)
        self._tmp_files.append(path)
        return path

    # 1. 헤더 누락
    def test_import_missing_header_fails(self):
        # '레포' 헤더가 빠진 CSV
        bad_header = HEADER_LINE.replace("레포,", "")
        path = self._make_csv(
            [
                "JK PC,cat,area,GET,/foo,홍길동,홍길동,auth-api,1,s1",
            ],
            with_header=False,
        )
        # 위에서 with_header=False 라 직접 헤더만 빠진 버전 쓰기
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("﻿" + bad_header + "\n")
            f.write("cat,area,GET,/foo,홍길동,홍길동,auth-api,1,s1\n")

        result = import_api_endpoint_owners(path, user=None)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("레포", result.errors[0]["error"])
        self.assertEqual(ApiEndpointOwner.objects.count(), 0)

    # 2. 정상 생성
    def test_import_creates_rows(self):
        path = self._make_csv(
            [
                "JK PC,cat,area,GET,/foo,홍길동,홍길동,auth-api,1,s1",
                "JK PC,cat,area,POST,/bar,김철수,이영희,user-api,2,s2",
            ]
        )
        result = import_api_endpoint_owners(path, user=None)
        self.assertEqual(result.created, 2)
        self.assertEqual(result.updated, 0)
        self.assertEqual(len(result.errors), 0)
        self.assertEqual(ApiEndpointOwner.objects.count(), 2)
        obj = ApiEndpointOwner.objects.get(method="GET", endpoint="/foo")
        self.assertEqual(obj.repo, "JK PC")
        self.assertEqual(obj.fe_owner, "홍길동")
        self.assertEqual(obj.usage_count, 1)
        self.assertTrue(obj.is_imported)

    # 3. 콤마/큰따옴표 escape
    def test_import_handles_quoted_commas(self):
        path = self._make_csv(
            [
                'JK PC,cat,area,POST,/foo,장호철,"이동훈, 윤복영",svc,5,"a, b"',
            ]
        )
        result = import_api_endpoint_owners(path, user=None)
        self.assertEqual(result.created, 1)
        self.assertEqual(len(result.errors), 0)
        obj = ApiEndpointOwner.objects.get(method="POST", endpoint="/foo")
        self.assertEqual(obj.be_owner, "이동훈, 윤복영")
        self.assertEqual(obj.step_ids, "a, b")

    # 4. 중복 키 갱신
    def test_import_updates_existing(self):
        path1 = self._make_csv(
            [
                "JK PC,cat,area,GET,/foo,홍길동,홍길동,auth-api,1,s1",
            ]
        )
        import_api_endpoint_owners(path1, user=None)
        self.assertEqual(ApiEndpointOwner.objects.count(), 1)

        # 같은 (repo, method, endpoint) 인데 담당자 다른 row 로 재임포트
        path2 = self._make_csv(
            [
                "JK PC,cat,area,GET,/foo,새담당자,새담당자,auth-api,9,s1",
            ]
        )
        result = import_api_endpoint_owners(path2, user=None)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 1)
        self.assertEqual(ApiEndpointOwner.objects.count(), 1)
        obj = ApiEndpointOwner.objects.get(method="GET", endpoint="/foo")
        self.assertEqual(obj.fe_owner, "새담당자")
        self.assertEqual(obj.usage_count, 9)

    # 5. 수동 row 보호
    def test_import_skips_manual_rows(self):
        manual = ApiEndpointOwner.objects.create(
            repo="JK PC",
            method="GET",
            endpoint="/foo",
            fe_owner="수동담당자",
            is_imported=False,
        )
        path = self._make_csv(
            [
                "JK PC,cat,area,GET,/foo,임포트담당자,임포트담당자,auth-api,1,s1",
            ]
        )
        # remove_stale=False 로 — 다른 imported row 가 없어도 무관
        result = import_api_endpoint_owners(path, user=None, remove_stale=False)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 0)
        self.assertEqual(result.skipped_manual, 1)
        manual.refresh_from_db()
        # 덮어쓰지 않았는지 검증
        self.assertEqual(manual.fe_owner, "수동담당자")
        self.assertFalse(manual.is_imported)

    # 6. 빈 행 무시
    def test_import_ignores_blank_rows(self):
        path = self._make_csv(
            [
                "JK PC,cat,area,GET,/foo,홍길동,홍길동,auth-api,1,s1",
                ",,,,,,,,,",
                "",
                "JK PC,cat,area,GET,/bar,김철수,김철수,user-api,2,s2",
            ]
        )
        result = import_api_endpoint_owners(path, user=None)
        self.assertEqual(result.created, 2)
        self.assertEqual(len(result.errors), 0)

    # 7. dry_run 은 저장하지 않음
    def test_dry_run_does_not_persist(self):
        path = self._make_csv(
            [
                "JK PC,cat,area,GET,/foo,홍길동,홍길동,auth-api,1,s1",
                "JK PC,cat,area,POST,/bar,홍길동,홍길동,auth-api,1,s2",
            ]
        )
        result = import_api_endpoint_owners(path, user=None, dry_run=True)
        # 결과는 created=2 로 나오되, DB 에는 반영 X
        self.assertEqual(result.created, 2)
        self.assertEqual(ApiEndpointOwner.objects.count(), 0)

    # 8b. 헤더 띄어쓰기 변형 — `FE담당자` (공백 없음) 도 `FE 담당자` 와 동일 처리
    def test_import_accepts_header_whitespace_variants(self):
        """사용자 CSV 가 `FE담당자` / `BE담당자` (공백 없음) 으로 들어와도 인식.

        실 사고 케이스 (2026-05-20) — `api_endpoints_owners_2.csv` 가 `FE담당자` 헤더로
        들어와 '헤더 누락' 오인됐었음. 회귀 방지.
        """
        header_no_space = (
            "레포,카테고리,영역,Method,Endpoint,"
            "FE담당자,BE담당자,"
            "매핑 앱,사용 횟수,step_ids"
        )
        path = self._make_csv(
            [
                "JK PC,cat,area,GET,/foo,홍길동,김철수,auth-api,1,s1",
            ],
            with_header=False,
        )
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("﻿" + header_no_space + "\n")
            f.write("JK PC,cat,area,GET,/foo,홍길동,김철수,auth-api,1,s1\n")

        result = import_api_endpoint_owners(path, user=None)
        self.assertEqual(result.created, 1, msg=f"errors={result.errors}")
        self.assertEqual(len(result.errors), 0)
        obj = ApiEndpointOwner.objects.get(method="GET", endpoint="/foo")
        self.assertEqual(obj.fe_owner, "홍길동")
        self.assertEqual(obj.be_owner, "김철수")

    # 9. remove_stale 은 imported row 만 삭제
    def test_remove_stale_deletes_only_imported(self):
        # 이전 import 의 imported row
        stale_imported = ApiEndpointOwner.objects.create(
            repo="JK PC",
            method="GET",
            endpoint="/old-endpoint",
            is_imported=True,
        )
        # 수동 row (반드시 보존되어야 함)
        manual = ApiEndpointOwner.objects.create(
            repo="JK PC",
            method="GET",
            endpoint="/manual-endpoint",
            is_imported=False,
        )

        path = self._make_csv(
            [
                "JK PC,cat,area,GET,/foo,홍길동,홍길동,auth-api,1,s1",
            ]
        )
        result = import_api_endpoint_owners(path, user=None, remove_stale=True)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.deleted_stale, 1)
        self.assertFalse(
            ApiEndpointOwner.objects.filter(pk=stale_imported.pk).exists()
        )
        # 수동 row 는 보존
        self.assertTrue(ApiEndpointOwner.objects.filter(pk=manual.pk).exists())


# ---------------------------------------------------------------------------
# URL 매칭 엔진 테스트 (improvement.md §4)
# ---------------------------------------------------------------------------


class UrlMatcherTests(TestCase):
    """find_endpoint_owner() 의 8개 매칭 케이스."""

    def setUp(self):
        # 각 테스트 격리 — 캐시 비우기.
        invalidate_match_cache()

    # 1. AM ${BFF_URL} 환경변수 + path param
    def test_match_am_bff_url(self):
        owner = ApiEndpointOwner.objects.create(
            repo="AM Mobile",
            method="POST",
            endpoint="${BFF_URL}/apply/recruits/${RECRUIT_NO}/online-apply",
            be_owner="이동훈, 윤복영",
            is_imported=True,
        )
        result = find_endpoint_owner(
            "POST",
            "https://bff-general-01.dev.albamon.com/apply/recruits/95053137/online-apply",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, owner.pk)

    # 2. JK 절대경로
    def test_match_jk_absolute_path(self):
        owner = ApiEndpointOwner.objects.create(
            repo="JK PC",
            method="GET",
            endpoint="/Join/IDCheck",
            fe_owner="김철수",
            is_imported=True,
        )
        result = find_endpoint_owner(
            "GET", "https://www.jobkorea.co.kr/Join/IDCheck"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, owner.pk)

    # 3. ${RECRUIT_NO} 자리에 숫자/문자 모두 매칭
    def test_match_recruit_no_wildcard(self):
        owner = ApiEndpointOwner.objects.create(
            repo="AM Mobile",
            method="GET",
            endpoint="${BFF_URL}/recruit/${RECRUIT_NO}",
            is_imported=True,
        )
        for tail in ("123456", "abc-xyz_99", "RECRUIT-001"):
            r = find_endpoint_owner(
                "GET", f"https://bff-general-01.dev.albamon.com/recruit/{tail}"
            )
            self.assertIsNotNone(r, f"tail={tail!r} 매칭 실패")
            self.assertEqual(r.pk, owner.pk)

    # 4. 같은 path 가 GET/POST 다를 때 method 로 구분
    def test_match_method_strict(self):
        o_get = ApiEndpointOwner.objects.create(
            repo="JK PC",
            method="GET",
            endpoint="/member/login",
            fe_owner="GET 담당자",
            is_imported=True,
        )
        o_post = ApiEndpointOwner.objects.create(
            repo="JK PC",
            method="POST",
            endpoint="/member/login",
            fe_owner="POST 담당자",
            is_imported=True,
        )

        r_get = find_endpoint_owner(
            "GET", "https://www.jobkorea.co.kr/member/login"
        )
        r_post = find_endpoint_owner(
            "POST", "https://www.jobkorea.co.kr/member/login"
        )
        self.assertEqual(r_get.pk, o_get.pk)
        self.assertEqual(r_post.pk, o_post.pk)

    # 4b. 메서드 무관 fallback — 같은 path 가 다른 메서드로만 등록돼 있어도 매칭
    def test_match_cross_method_fallback(self):
        # 시트가 GET 으로만 등록한 endpoint.
        o_get = ApiEndpointOwner.objects.create(
            repo="AM PC",
            method="GET",
            endpoint="${BFF_PERSONAL_URL}/member/users/edit-by-pop-up",
            be_owner="담당자",
            is_imported=True,
        )
        # POST 로 호출돼도 같은 path 라 GET owner 로 fallback 매칭.
        r = find_endpoint_owner(
            "POST", "https://bff-personal.albamon.com/member/users/edit-by-pop-up"
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.pk, o_get.pk)

    # 4c. fallback 은 정확한 메서드가 있으면 양보 — 정확한 매칭이 우선
    def test_match_exact_method_wins_over_fallback(self):
        ApiEndpointOwner.objects.create(
            repo="AM PC", method="GET", endpoint="/shared/endpoint",
            fe_owner="GET 담당자", is_imported=True,
        )
        o_post = ApiEndpointOwner.objects.create(
            repo="AM PC", method="POST", endpoint="/shared/endpoint",
            be_owner="POST 담당자", is_imported=True,
        )
        r = find_endpoint_owner("POST", "https://x.example.com/shared/endpoint")
        self.assertEqual(r.pk, o_post.pk)  # GET 으로 fallback 하지 않고 POST 정확 매칭

    # 5. 여러 후보 중 specificity 큰 것 선택
    def test_match_picks_longest(self):
        general = ApiEndpointOwner.objects.create(
            repo="AM Mobile",
            method="GET",
            endpoint="${BFF_URL}/api/users/${UID}/orders/${OID}",
            is_imported=True,
        )
        specific = ApiEndpointOwner.objects.create(
            repo="AM Mobile",
            method="GET",
            endpoint="${BFF_URL}/api/users/${UID}/orders/latest",
            is_imported=True,
        )
        # /orders/latest 는 양쪽 모두 매칭 가능하지만 specific 가 리터럴 segment 1개 더 많음.
        r = find_endpoint_owner(
            "GET",
            "https://bff-general-01.dev.albamon.com/api/users/u1/orders/latest",
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.pk, specific.pk)
        # /orders/123 은 general 만 매칭.
        r2 = find_endpoint_owner(
            "GET",
            "https://bff-general-01.dev.albamon.com/api/users/u1/orders/123",
        )
        self.assertEqual(r2.pk, general.pk)

    # 6. 미등록 URL 은 None
    def test_match_no_owner_returns_none(self):
        ApiEndpointOwner.objects.create(
            repo="AM Mobile",
            method="GET",
            endpoint="/some/known/path",
            is_imported=True,
        )
        r = find_endpoint_owner("GET", "https://example.com/nope/not-here")
        self.assertIsNone(r)

    # 7. ApiHostMapping 비어있어도 path-only 로 매칭 성공
    def test_match_works_without_host_mapping(self):
        owner = ApiEndpointOwner.objects.create(
            repo="AM Mobile",
            method="POST",
            endpoint="${BFF_URL}/apply/recruits/${RECRUIT_NO}/online-apply",
            is_imported=True,
        )
        # ApiHostMapping 0건임을 명시.
        self.assertEqual(ApiHostMapping.objects.count(), 0)
        r = find_endpoint_owner(
            "POST",
            "https://random-host.example.com/apply/recruits/777/online-apply",
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.pk, owner.pk)

    # 8. tie-breaker — host_var 채워진 row > None
    def test_match_prefers_host_var_when_tie(self):
        with_host = ApiEndpointOwner.objects.create(
            repo="AM Mobile",
            method="GET",
            endpoint="${BFF_URL}/shared/path",
            is_imported=True,
        )
        without_host = ApiEndpointOwner.objects.create(
            repo="JK PC",
            method="GET",
            endpoint="/shared/path",
            is_imported=True,
        )
        # AM netloc → AM repo 가점도 함께 작동.
        r = find_endpoint_owner(
            "GET", "https://bff-general-01.dev.albamon.com/shared/path"
        )
        self.assertEqual(r.pk, with_host.pk)
        # JK netloc → JK repo 가점이 host_var 의 손을 들지 않도록 검증
        # (specificity 동일, host_var 없는 쪽이 repo bonus 받음).
        r2 = find_endpoint_owner(
            "GET", "https://www.jobkorea.co.kr/shared/path"
        )
        # 우선순위상 same_host_var(0) > has_host_var(1) 이 먼저이므로
        # ApiHostMapping 없으면 host_var 있는 쪽이 항상 우선.
        # JK netloc 이라도 with_host 가 채택됨 — improvement.md §4.2 순서 그대로.
        self.assertEqual(r2.pk, with_host.pk)
        # 의미를 잃지 않도록 without_host 도 살아있음 확인.
        self.assertTrue(
            ApiEndpointOwner.objects.filter(pk=without_host.pk).exists()
        )


class ExtractUrlsTests(TestCase):
    """extract_urls() 의 동작 — 라벨/단독 URL 인식 + dedupe + count."""

    def setUp(self):
        invalidate_match_cache()

    def test_extract_urls_dedupes_and_counts(self):
        owner = ApiEndpointOwner.objects.create(
            repo="AM Mobile",
            method="POST",
            endpoint="${BFF_URL}/apply/recruits/${RECRUIT_NO}/online-apply",
            be_owner="이동훈, 윤복영",
            is_imported=True,
        )

        text = """
============================================================
[Step] 재지원 (지원자 관리용)
============================================================
  URL: https://bff-general-01.dev.albamon.com/apply/recruits/95053137/online-apply
  Method: POST
  Status: 503
  [FAIL] Extract 'APPLY_NO' failed

@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
[FAIL] [Step] 재지원 (지원자 관리용)
  URL: https://bff-general-01.dev.albamon.com/apply/recruits/95053137/online-apply
  Method: POST
  Status: 503
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
"""
        results = extract_urls(text)
        self.assertEqual(len(results), 1)
        m: MatchedUrl = results[0]
        self.assertEqual(
            m.url,
            "https://bff-general-01.dev.albamon.com/apply/recruits/95053137/online-apply",
        )
        self.assertEqual(m.method, "POST")
        self.assertEqual(m.count, 2)
        self.assertEqual(m.status_code, 503)
        self.assertIsNotNone(m.owner)
        self.assertEqual(m.owner.pk, owner.pk)

    def test_extract_urls_label_and_bare_forms(self):
        text = (
            "URL: https://example.com/a\n"
            "URL : https://example.com/b\n"
            "log line https://example.com/c trailing\n"
        )
        results = extract_urls(text)
        urls = sorted(r.url for r in results)
        self.assertEqual(
            urls,
            [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            ],
        )

    def test_extract_urls_count_sort(self):
        text = (
            "URL: https://a.example.com/x\n"
            "\n\n\n"
            "URL: https://a.example.com/x\n"
            "\n\n\n"
            "URL: https://b.example.com/y\n"
        )
        results = extract_urls(text)
        # count 내림차순.
        self.assertEqual(results[0].url, "https://a.example.com/x")
        self.assertEqual(results[0].count, 2)
        self.assertEqual(results[1].url, "https://b.example.com/y")
        self.assertEqual(results[1].count, 1)

    def test_extract_urls_error_url_marker_real_sample(self):
        """실제 Jenkins 로그(ERROR_URL 마커) — 실패 URL 만 추출 + method/status 보강.

        host-only 변수 라인(`BFF_PERSONAL_URL=https://...`)은 무시돼야 한다.
        """
        owner = ApiEndpointOwner.objects.create(
            repo="AM Web",
            method="GET",
            endpoint="${BFF_PERSONAL_URL}/member/users/edit-by-pop-up",
            be_owner="홍길동",
            is_imported=True,
        )
        text = """
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
❌ [Step] 개인정보 수정 팝업 조회 (prd 전용)
  ERROR_URL: https://bff-personal.albamon.com/member/users/edit-by-pop-up
── 직전 흐름 (최근 2 step) ──
  ✗ 개인회원 로그인 (prd 전용)                    30s                     ← 실패
── 이 step 이 참조한 변수 ──
  BFF_PERSONAL_URL=https://bff-personal.albamon.com
────────────────────────────────────────
[Step][26-06-24 13:56:28 KST] 개인정보 수정 팝업 조회 (prd 전용)
  URL: https://bff-personal.albamon.com/member/users/edit-by-pop-up
  Method: GET
  [REQUEST ERROR] 개인정보 수정 팝업 조회 (prd 전용)
    Method : GET
    URL    : https://bff-personal.albamon.com/member/users/edit-by-pop-up
    Type   : ConnectTimeout
  [TestRail] ps_get_user_edit_info_prd -> FAIL
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
"""
        results = extract_urls(text)
        self.assertEqual(len(results), 1)
        m: MatchedUrl = results[0]
        self.assertEqual(
            m.url,
            "https://bff-personal.albamon.com/member/users/edit-by-pop-up",
        )
        self.assertEqual(m.method, "GET")
        self.assertEqual(m.count, 1)
        self.assertIsNone(m.status_code)  # ConnectTimeout — HTTP status 없음
        self.assertIsNotNone(m.owner)
        self.assertEqual(m.owner.pk, owner.pk)
        # host-only 변수 URL 은 결과에 없어야 한다.
        self.assertNotIn(
            "https://bff-personal.albamon.com",
            [r.url for r in results],
        )

    def test_extract_urls_error_url_takes_priority_over_noise(self):
        """ERROR_URL 이 있으면 로그의 다른 URL(라벨/맨몸)은 전부 무시."""
        text = (
            "  ERROR_URL: https://api.example.com/failed/endpoint\n"
            "  URL: https://api.example.com/some/other/call\n"
            "  Method: POST\n"
            "  noise line https://cdn.example.com/asset.js trailing\n"
            "  VAR_URL=https://host-only.example.com\n"
        )
        results = extract_urls(text)
        self.assertEqual(
            [r.url for r in results],
            ["https://api.example.com/failed/endpoint"],
        )

    def test_extract_urls_error_url_counts_failures(self):
        """같은 ERROR_URL 이 여러 번 = 실패 횟수 count, 정렬은 count 내림차순."""
        text = (
            "ERROR_URL: https://api.example.com/a\n\n\n"
            "ERROR_URL: https://api.example.com/a\n\n\n"
            "ERROR_URL: https://api.example.com/b\n"
        )
        results = extract_urls(text)
        self.assertEqual(results[0].url, "https://api.example.com/a")
        self.assertEqual(results[0].count, 2)
        self.assertEqual(results[1].url, "https://api.example.com/b")
        self.assertEqual(results[1].count, 1)

    def test_mentions_uses_slack_lookup_and_falls_back_to_plain(self):
        """Slack workspace 에서 매칭된 이름은 <@U...>, 매칭 못 한 이름은 @이름 plain."""
        from unittest.mock import patch
        from apps.integrations.models import ApiEndpointOwner
        from apps.notifications.owner_mentions import build_mentions

        owner = ApiEndpointOwner.objects.create(
            repo="AM",
            method="GET",
            endpoint="/api/x",
            fe_owner="이동훈, 윤복영",
            be_owner="",
        )

        def fake_lookup(name):
            return {"이동훈": "U01ABC"}.get(name)

        with patch(
            "apps.integrations.slack_users.find_user_id_by_name",
            side_effect=fake_lookup,
        ):
            mention_text, unresolved = build_mentions([(owner, 404)])

        self.assertIn("<@U01ABC>", mention_text)
        self.assertIn("@윤복영", mention_text)
        # 매칭 못 한 이름만 unresolved
        self.assertEqual(unresolved, ["윤복영"])

    def test_split_names_handles_comma_and_spaces(self):
        from apps.notifications.owner_mentions import _split_names

        self.assertEqual(
            _split_names("이동훈, 윤복영 ,  김"),
            ["이동훈", "윤복영", "김"],
        )
        self.assertEqual(_split_names(""), [])
        self.assertEqual(_split_names("  , ,  "), [])

    def test_names_for_status_uses_5xx_for_500s(self):
        from apps.integrations.models import ApiEndpointOwner
        from apps.notifications.owner_mentions import _names_for_status

        owner = ApiEndpointOwner.objects.create(
            repo="AM",
            method="GET",
            endpoint="/api/x",
            fe_owner="A",
            be_owner="B,C",
        )
        self.assertEqual(_names_for_status(owner, 503), ["B", "C"])
        self.assertEqual(_names_for_status(owner, 404), ["A"])
        # status None → union (A + B + C)
        self.assertEqual(set(_names_for_status(owner, None)), {"A", "B", "C"})

    def test_excerpt_error_truncates_long_text(self):
        from apps.notifications.owner_mentions import excerpt_error

        long_text = "x" * 4000
        result = excerpt_error(long_text, max_chars=1500)
        # 본문은 정확히 1500자
        self.assertTrue(result.startswith("x" * 1500))
        self.assertNotIn("x" * 1501, result)
        self.assertIn("(이하 ", result)
        self.assertIn("자 생략)", result)
        # 백틱 3개 escape 검증
        with_backticks = "before ```code``` after"
        escaped = excerpt_error(with_backticks, max_chars=1000)
        self.assertNotIn("```", escaped)

    def test_notify_error_endpoint_returns_redirect(self):
        """admin user 가 POST 시 302 + Slack 토큰 없는 환경에선 silent skip + ActivityLog 1건."""
        from django.contrib.auth import get_user_model
        from django.test import Client
        from apps.integrations.models import ApiEndpointOwner
        from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJob, TestSuiteJobRun
        from apps.activitylog.models import ActivityLog

        User = get_user_model()
        admin = User.objects.create_user(
            username="admin_notify_test",
            password="x",
            role="qpe_admin",
            is_staff=True,
        )
        owner = ApiEndpointOwner.objects.create(
            repo="AM",
            method="GET",
            endpoint="/api/x",
            fe_owner="이동훈",
        )
        # 최소 suite_run/job_run 픽스처
        suite = TestSuite.objects.create(name="t-suite", jenkins_folder_path="t/folder")
        suite_run = TestSuiteRun.objects.create(suite=suite, triggered_by=admin)
        job = TestSuiteJob.objects.create(suite=suite, job_name="t-job", job_full_path="t/job")
        job_run = TestSuiteJobRun.objects.create(
            suite_run=suite_run,
            job=job,
            console_output="@@@@@@@@@@\n[FAIL] error happened\n@@@@@@@@@@",
        )

        c = Client()
        c.force_login(admin)
        before = ActivityLog.objects.filter(action="owner_notified").count()
        resp = c.post(
            f"/accounts/api-owners/{owner.pk}/notify-error/",
            data={
                "url": "https://example.com/api/x",
                "method": "GET",
                "status_code": "404",
                "source_type": "suite_job",
                "source_id": str(job_run.pk),
            },
            HTTP_REFERER="/suites/job-runs/" + str(job_run.pk) + "/error-log/",
        )
        self.assertEqual(resp.status_code, 302)
        after = ActivityLog.objects.filter(action="owner_notified").count()
        self.assertEqual(after, before + 1)

    def test_slack_users_index_builds_from_members_payload(self):
        """`_build_name_index` 가 deleted/bot/app 제외 + 5개 이름 키 인덱싱."""
        from apps.integrations.slack_users import _build_name_index

        members = [
            {
                "id": "U001",
                "name": "leedong",
                "profile": {
                    "real_name": "이동훈",
                    "real_name_normalized": "이동훈",
                    "display_name": "이동훈 (Backend)",
                    "display_name_normalized": "이동훈 (backend)",
                },
            },
            {  # bot 은 제외
                "id": "B999",
                "name": "qg-bot",
                "is_bot": True,
                "profile": {"real_name": "qg-bot"},
            },
            {  # deleted 도 제외
                "id": "U002",
                "name": "old",
                "deleted": True,
                "profile": {"real_name": "구사용자"},
            },
        ]
        idx = _build_name_index(members)
        # 표시명/실명/legacy 모두 매칭 가능
        self.assertEqual(idx["이동훈"], "U001")
        self.assertEqual(idx["leedong"], "U001")
        self.assertEqual(idx["이동훈 (backend)"], "U001")
        # bot/deleted 는 빠짐
        self.assertNotIn("qg-bot", idx)
        self.assertNotIn("구사용자", idx)

    def test_slack_users_strip_status_prefix(self):
        """`휴가중_박세건` / `재택 이동훈` / `퇴근중-홍길동` 등 상태 prefix 자동 제거."""
        from apps.integrations.slack_users import _strip_status_prefix

        # 다양한 separator
        self.assertEqual(_strip_status_prefix("휴가중_박세건"), "박세건")
        self.assertEqual(_strip_status_prefix("재택 이동훈"), "이동훈")
        self.assertEqual(_strip_status_prefix("퇴근중-홍길동"), "홍길동")
        self.assertEqual(_strip_status_prefix("미팅중:김태식"), "김태식")
        self.assertEqual(_strip_status_prefix("외근중·김도연"), "김도연")
        # 긴 prefix 가 짧은 것보다 우선 (`재택근무중` vs `재택`)
        self.assertEqual(_strip_status_prefix("재택근무중_이동훈"), "이동훈")
        # generic 한글 prefix — 화이트리스트에 없는 새 상태 키워드도 자동 처리
        # (`행사_윤복영`, `교육_홍길동` 등). 화이트리스트 의존성 제거 회귀 방지.
        self.assertEqual(_strip_status_prefix("행사_윤복영"), "윤복영")
        self.assertEqual(_strip_status_prefix("교육 홍길동"), "홍길동")
        self.assertEqual(_strip_status_prefix("세미나-김태식"), "김태식")
        # 중첩 prefix — 끝까지 반복 제거
        self.assertEqual(_strip_status_prefix("재택_미팅중_홍길동"), "홍길동")
        # 상태 없음 → 입력 그대로
        self.assertEqual(_strip_status_prefix("박세건"), "박세건")
        # 영문 prefix 는 한글 패턴이 아니므로 보존 (의도)
        self.assertEqual(_strip_status_prefix("Lee Donghoon"), "Lee Donghoon")
        # 빈 입력
        self.assertEqual(_strip_status_prefix(""), "")

    def test_slack_users_index_handles_status_prefix(self):
        """Slack workspace 의 `휴가중_박세건` 표시명 → `박세건` 으로 매칭 가능 (실 사례)."""
        from apps.integrations.slack_users import _build_name_index

        members = [
            {
                "id": "U_PARK",
                "name": "parksk",
                "profile": {
                    "real_name": "박세건",
                    "display_name": "휴가중_박세건",
                    "display_name_normalized": "휴가중_박세건",
                },
            },
            {
                "id": "U_LEE",
                "name": "leedong",
                "profile": {
                    "real_name": "이동훈",
                    # 상태 + 팀 접미사 동시
                    "display_name": "재택 이동훈(engagement eng팀)",
                    "display_name_normalized": "재택 이동훈(engagement eng팀)",
                },
            },
            {
                # 화이트리스트에 없는 새 상태 키워드 — generic 패턴으로 떼어내야
                # 자동 매칭 (실 사례: `행사_윤복영`).
                "id": "U_YUN",
                "name": "yunbok",
                "profile": {
                    "real_name": "윤복영",
                    "display_name": "행사_윤복영(discovery eng팀)",
                    "display_name_normalized": "행사_윤복영(discovery eng팀)",
                },
            },
        ]
        idx = _build_name_index(members)
        # CSV 의 owner 표기 (이름만) 으로 매칭 가능해야
        self.assertEqual(idx["박세건"], "U_PARK")
        self.assertEqual(idx["이동훈"], "U_LEE")
        # 화이트리스트에 없던 prefix 도 generic 패턴으로 매칭됨
        self.assertEqual(idx["윤복영"], "U_YUN")
        # 원본 표시명도 그대로 매칭 가능 (양방향 안전망)
        self.assertEqual(idx["휴가중_박세건"], "U_PARK")

    def test_slack_users_index_strips_team_suffix(self):
        """`이동훈(engagement eng팀)` 표시명 → `이동훈` 키도 등록 (실 사례 회귀 방지)."""
        from apps.integrations.slack_users import _build_name_index, _name_head

        # _name_head 단위 검증
        self.assertEqual(_name_head("장호철(talentclient eng팀)"), "장호철")
        self.assertEqual(_name_head("이동훈(engagement eng팀)"), "이동훈")
        self.assertEqual(_name_head("이동훈 (Backend)"), "이동훈")
        self.assertEqual(_name_head("이동훈[엔지니어]"), "이동훈")
        self.assertEqual(_name_head("이동훈"), "이동훈")  # 접미사 없으면 그대로
        self.assertEqual(_name_head(""), "")

        # 실 케이스 — Slack workspace 의 display_name 에 팀명 접미사가 붙은 멤버
        members = [
            {
                "id": "U_LEE",
                "name": "tkmania",
                "profile": {
                    "real_name": "이동훈",
                    "display_name": "이동훈(engagement eng팀)",
                    "display_name_normalized": "이동훈(engagement eng팀)",
                },
            },
            {
                "id": "U_JANG",
                "name": "janghc1",
                "profile": {
                    "real_name": "장호철",
                    "display_name": "장호철(talentclient eng팀)",
                    "display_name_normalized": "장호철(talentclient eng팀)",
                },
            },
        ]
        idx = _build_name_index(members)
        # CSV 의 owner 컬럼 표기 (`이동훈`, `장호철`) 만으로 매칭 가능해야 함
        self.assertEqual(idx["이동훈"], "U_LEE")
        self.assertEqual(idx["장호철"], "U_JANG")
        # 원본 풀네임도 그대로 매칭 (양방향)
        self.assertEqual(idx["이동훈(engagement eng팀)"], "U_LEE")
        self.assertEqual(idx["장호철(talentclient eng팀)"], "U_JANG")

    def test_owner_update_reflected_in_matcher_after_save(self):
        """담당자 변경 후 find_endpoint_owner 가 변경된 fe_owner/be_owner 를 즉시 반환.

        2026-05-20 사용자 보고: 'Error Log 에 옛 담당자가 그대로' 회귀 방지.
        signal 자동 무효화 + admin save 의 명시 무효화 + page 진입 시 무효화의 3중 안전망.
        """
        from apps.integrations.models import ApiEndpointOwner
        from apps.integrations.url_matcher import find_endpoint_owner

        owner = ApiEndpointOwner.objects.create(
            repo="JK PC",
            method="GET",
            endpoint="/api/x",
            fe_owner="옛담당자",
            be_owner="옛BE",
            is_imported=True,
        )
        # 첫 매칭 → 옛 값
        r1 = find_endpoint_owner("GET", "https://example.com/api/x")
        self.assertIsNotNone(r1)
        self.assertEqual(r1.fe_owner, "옛담당자")

        # 담당자 변경 후 다시 호출 → 새 값 반영
        owner.fe_owner = "새담당자"
        owner.save()
        r2 = find_endpoint_owner("GET", "https://example.com/api/x")
        self.assertIsNotNone(r2)
        self.assertEqual(r2.fe_owner, "새담당자")

    def test_fallback_owners_singleton_and_names(self):
        """FallbackOwners.get_solo() 는 항상 단일 row, names() 가 콤마 split."""
        from apps.integrations.models import FallbackOwners

        FallbackOwners.objects.all().delete()
        a = FallbackOwners.get_solo()
        b = FallbackOwners.get_solo()
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(FallbackOwners.objects.count(), 1)

        a.owners = "김태식, 김도연 ,  ,홍길동"
        a.save()
        self.assertEqual(a.names(), ["김태식", "김도연", "홍길동"])

        a.owners = ""
        a.save()
        self.assertEqual(a.names(), [])

    def test_mentions_from_names_helper(self):
        """mentions_from_names — Slack 매칭 가능한 이름은 <@U>, 실패는 @이름."""
        from unittest.mock import patch
        from apps.notifications.owner_mentions import mentions_from_names

        with patch(
            "apps.integrations.slack_users.find_user_id_by_name",
            side_effect=lambda n: {"김태식": "U_KIM"}.get(n),
        ):
            text, unresolved = mentions_from_names(["김태식", "김도연", "김태식"])
        # 중복 제거 + 매칭 우선
        self.assertIn("<@U_KIM>", text)
        self.assertIn("@김도연", text)
        self.assertEqual(unresolved, ["김도연"])
        # 같은 이름이 두 번 들어와도 한 번만 멘션
        self.assertEqual(text.count("<@U_KIM>"), 1)

    def test_followup_uses_fallback_when_owner_matched_but_names_empty(self):
        """owner 매칭됐지만 status 별 담당자 비어있으면 fallback 사용 + 라벨 표기.

        2026-05-20 사용자 보고: ApiEndpointOwner row 가 있어도 fe_owner=""/be_owner=""
        이면 멘션 못 가는 케이스 회귀 방지.
        """
        from unittest.mock import patch
        from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJob, TestSuiteJobRun
        from apps.integrations.models import ApiEndpointOwner, FallbackOwners
        from apps.notifications.services import _build_followup_for_job_run

        # owner 매칭은 되지만 담당자 컬럼은 빈 row
        ApiEndpointOwner.objects.create(
            repo="AM",
            method="POST",
            endpoint="/member/users/sign-up",
            fe_owner="",  # 비어있음
            be_owner="",
            is_imported=True,
        )

        # 대체 담당자 등록
        FallbackOwners.objects.all().delete()
        fb = FallbackOwners.get_solo()
        fb.owners = "김태식, 김도연"
        fb.save()

        # 콘솔 — URL/Method/Status 가 들어간 에러 블록
        console = (
            "@@@@@@@@@@@@@@@@@@@@\n"
            "❌ [Step] 회원가입\n"
            "  URL: https://bff-general-01.dev.albamon.com/member/users/sign-up\n"
            "  Method: POST\n"
            "  Status: 400\n"
            "  [FAIL] Expected 200, got 400\n"
            "@@@@@@@@@@@@@@@@@@@@\n"
        )
        suite = TestSuite.objects.create(name="fb-suite", jenkins_folder_path="t/folder")
        suite_run = TestSuiteRun.objects.create(suite=suite)
        job = TestSuiteJob.objects.create(suite=suite, job_name="fb-job", job_full_path="t/job")
        jr = TestSuiteJobRun.objects.create(
            suite_run=suite_run, job=job, console_output=console,
        )

        with patch(
            "apps.integrations.slack_users.find_user_id_by_name",
            side_effect=lambda n: {"김태식": "U_KIM"}.get(n),
        ):
            text = _build_followup_for_job_run(
                str(jr.pk),
                context_label_extra="Suite: fb-suite / Job: fb-job",
            )

        # 대체 담당자 멘션 + 라벨이 메시지에 포함되어야 함
        self.assertTrue(text, "followup 이 비어있음 — fallback 진입 실패")
        self.assertIn("<@U_KIM>", text)
        self.assertIn("@김도연", text)
        self.assertIn("담당자 매칭 없음", text)
        self.assertIn("대체 담당자로 지정", text)
        self.assertIn("/member/users/sign-up", text)

    def test_manual_notify_uses_fallback_when_owner_names_empty(self):
        """수동 노티 (api_owner_notify_error): owner 매칭됐지만 fe/be 비어있으면 fallback.

        2026-05-20 사용자 보고 — 매칭된 행에서 "에러 노티" 버튼 눌렀는데 owner row 의
        fe_owner/be_owner 가 비어있어서 멘션 없이 발송된 케이스 회귀 방지.
        """
        from django.contrib.auth import get_user_model
        from django.test import Client
        from apps.integrations.models import ApiEndpointOwner, FallbackOwners
        from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJob, TestSuiteJobRun
        from apps.activitylog.models import ActivityLog

        User = get_user_model()
        admin = User.objects.create_user(
            username="admin_manual_fallback",
            password="x",
            role="qpe_admin",
            is_staff=True,
        )
        # 담당자 컬럼이 빈 owner row
        owner = ApiEndpointOwner.objects.create(
            repo="AM",
            method="POST",
            endpoint="/member/users/sign-up",
            fe_owner="",
            be_owner="",
        )
        # 대체 담당자 등록
        FallbackOwners.objects.all().delete()
        fb = FallbackOwners.get_solo()
        fb.owners = "김태식"
        fb.save()

        suite = TestSuite.objects.create(name="man-suite", jenkins_folder_path="t/folder")
        suite_run = TestSuiteRun.objects.create(suite=suite, triggered_by=admin)
        job = TestSuiteJob.objects.create(suite=suite, job_name="man-job", job_full_path="t/job")
        job_run = TestSuiteJobRun.objects.create(
            suite_run=suite_run,
            job=job,
            console_output="@@@@@@@@@@\n[FAIL] err\n@@@@@@@@@@",
        )

        c = Client()
        c.force_login(admin)
        before = ActivityLog.objects.filter(action="owner_notified").count()
        resp = c.post(
            f"/accounts/api-owners/{owner.pk}/notify-error/",
            data={
                "url": "https://x.com/member/users/sign-up",
                "method": "POST",
                "status_code": "400",
                "source_type": "suite_job",
                "source_id": str(job_run.pk),
            },
            HTTP_REFERER="/",
        )
        self.assertEqual(resp.status_code, 302)
        after = ActivityLog.objects.filter(action="owner_notified").count()
        self.assertEqual(after, before + 1)
        # ActivityLog 의 detail 에 fallback=True 가 기록되어야 한다
        last_log = ActivityLog.objects.filter(action="owner_notified").order_by("-created_at").first()
        self.assertIn("fallback=True", last_log.detail or "")

    def test_notify_fallback_owners_endpoint_sends(self):
        """대체 담당자 노티 endpoint — 매칭 안 된 URL 에 대해 fallback 사용."""
        from django.contrib.auth import get_user_model
        from django.test import Client
        from apps.integrations.models import FallbackOwners
        from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJob, TestSuiteJobRun
        from apps.activitylog.models import ActivityLog

        User = get_user_model()
        admin = User.objects.create_user(
            username="admin_fallback_test",
            password="x",
            role="qpe_admin",
            is_staff=True,
        )
        FallbackOwners.objects.all().delete()
        fb = FallbackOwners.get_solo()
        fb.owners = "김태식, 김도연"
        fb.save()

        suite = TestSuite.objects.create(name="t-suite", jenkins_folder_path="t/folder")
        suite_run = TestSuiteRun.objects.create(suite=suite, triggered_by=admin)
        job = TestSuiteJob.objects.create(suite=suite, job_name="t-job", job_full_path="t/job")
        job_run = TestSuiteJobRun.objects.create(
            suite_run=suite_run,
            job=job,
            console_output="@@@@@@@@@@\n[FAIL] error happened\n@@@@@@@@@@",
        )

        c = Client()
        c.force_login(admin)
        before = ActivityLog.objects.filter(action="owner_notified").count()
        resp = c.post(
            "/accounts/api-owners/fallback/notify/",
            data={
                "url": "https://example.com/unmatched/path",
                "method": "GET",
                "status_code": "404",
                "source_type": "suite_job",
                "source_id": str(job_run.pk),
            },
            HTTP_REFERER="/suites/job-runs/" + str(job_run.pk) + "/error-log/",
        )
        self.assertEqual(resp.status_code, 302)
        after = ActivityLog.objects.filter(action="owner_notified").count()
        self.assertEqual(after, before + 1)

    def test_notify_fallback_owners_requires_setup(self):
        """대체 담당자 미설정 시 발송 안 함 + 에러 메시지."""
        from django.contrib.auth import get_user_model
        from django.test import Client
        from apps.integrations.models import FallbackOwners
        from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJob, TestSuiteJobRun

        User = get_user_model()
        admin = User.objects.create_user(
            username="admin_no_fallback",
            password="x",
            role="qpe_admin",
            is_staff=True,
        )
        # 비워두기
        FallbackOwners.objects.all().delete()
        fb = FallbackOwners.get_solo()
        fb.owners = ""
        fb.save()

        suite = TestSuite.objects.create(name="t-suite-2", jenkins_folder_path="t/folder2")
        suite_run = TestSuiteRun.objects.create(suite=suite, triggered_by=admin)
        job = TestSuiteJob.objects.create(suite=suite, job_name="t-job-2", job_full_path="t/job2")
        job_run = TestSuiteJobRun.objects.create(
            suite_run=suite_run, job=job, console_output="@@@\n[FAIL]\n@@@",
        )

        c = Client()
        c.force_login(admin)
        resp = c.post(
            "/accounts/api-owners/fallback/notify/",
            data={
                "url": "https://x.com/p", "method": "GET", "status_code": "404",
                "source_type": "suite_job", "source_id": str(job_run.pk),
            },
            HTTP_REFERER="/",
        )
        self.assertEqual(resp.status_code, 302)
        # 에러 메시지 확인 — get_messages 로 직접 검증은 무거우니 redirect 만 확인.

    def test_extract_urls_handles_long_separator_lines_fast(self):
        """회귀 방지 — 70자 `===` / `@@@` 라인이 섞인 큰 콘솔에서도 1초 이내 완료.

        과거 `(?:=+|@+){5,}` 형태의 _BLOCK_SEP_RE 가 catastrophic backtracking 으로
        무한 대기하던 버그(2026-05-20) 의 회귀 방지. 평탄한 `(?:={5,}|@{5,})` 로
        전환되었는지 검증.
        """
        import time
        # 60자 == 라인 100개 + 60자 @@ 라인 50개 — 실 콘솔과 비슷한 양.
        big_text = ""
        for i in range(100):
            big_text += "=" * 60 + "\n"
            big_text += f"[Step {i}] URL: https://example.com/path-{i}\n"
            big_text += "  Method: POST\n"
        for i in range(50):
            big_text += "@" * 60 + "\n"
            big_text += f"❌ URL: https://example.com/fail-{i}\n"
        t0 = time.time()
        results = extract_urls(big_text)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0, f"extract_urls took {elapsed:.2f}s")
        self.assertGreaterEqual(len(results), 100)


# 동기화 테스트용 가짜 서비스 계정 JSON (실제 키 아님 — 형식만 충족).
_FAKE_SA_INFO = {
    "type": "service_account",
    "project_id": "qg-test",
    "private_key_id": "x",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "client_email": "qg-owners@qg-test.iam.gserviceaccount.com",
    "client_id": "123",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _sheet_csv_bytes(rows: list[str]) -> bytes:
    """헤더 + 본문 row 들을 UTF-8 CSV bytes 로. (Drive export 결과 모사)"""
    return ("\n".join([HEADER_LINE] + rows)).encode("utf-8")


class GoogleSheetSyncTests(TestCase):
    """Google Sheets → 담당자 마스터 동기화 (`sync_owners_from_google_sheet`) +
    자격증명 로딩 (`google_sheets`) 회귀 테스트.

    실제 Google 호출 없이 `export_sheet_csv` / `_build_session` 을 모킹한다.
    """

    def setUp(self):
        invalidate_match_cache()

    # --- full mirror 동기화 ----------------------------------------------
    def test_sync_full_mirror_create_update_delete_preserve(self):
        from unittest import mock
        from apps.integrations.services import sync_owners_from_google_sheet

        # 기존 상태:
        #  - imported 'keep' : 시트에 있음 → 갱신
        #  - imported 'stale': 시트에 없음 → 삭제 (full mirror)
        #  - manual   'man'  : 시트에 없음 → 보존 (수동 row 는 절대 삭제 안 함)
        ApiEndpointOwner.objects.create(
            repo="OLD", method="GET", endpoint="/api/keep",
            fe_owner="구담당", is_imported=True,
        )
        ApiEndpointOwner.objects.create(
            repo="OLD", method="DELETE", endpoint="/api/stale",
            fe_owner="누군가", is_imported=True,
        )
        ApiEndpointOwner.objects.create(
            repo="MAN", method="PUT", endpoint="/api/manual",
            fe_owner="수동담당", is_imported=False,
        )

        csv_bytes = _sheet_csv_bytes([
            'AM PC,공고,AM BFF,GET,/api/keep,박세건,"이동훈, 윤복영",am-bff,1,s1',
            "AM PC,공고,AM BFF,POST,/api/new,박선수,송현정,am-bff,2,s2",
        ])

        with mock.patch(
            "apps.integrations.google_sheets.export_sheet_csv",
            return_value=csv_bytes,
        ):
            result = sync_owners_from_google_sheet(user=None)

        self.assertEqual(result.created, 1)       # /api/new
        self.assertEqual(result.updated, 1)       # /api/keep
        self.assertEqual(result.deleted_stale, 1) # /api/stale
        self.assertEqual(result.skipped_manual, 0)
        self.assertEqual(result.errors, [])

        # keep 갱신됨
        keep = ApiEndpointOwner.objects.get(method="GET", endpoint="/api/keep")
        self.assertEqual(keep.fe_owner, "박세건")
        self.assertEqual(keep.be_owner, "이동훈, 윤복영")
        self.assertEqual(keep.repo, "AM PC")
        # new 생성됨
        self.assertTrue(
            ApiEndpointOwner.objects.filter(method="POST", endpoint="/api/new").exists()
        )
        # stale 삭제됨
        self.assertFalse(
            ApiEndpointOwner.objects.filter(method="DELETE", endpoint="/api/stale").exists()
        )
        # 수동 row 보존
        man = ApiEndpointOwner.objects.get(method="PUT", endpoint="/api/manual")
        self.assertEqual(man.fe_owner, "수동담당")
        self.assertFalse(man.is_imported)

    def test_sync_skips_manual_row_with_same_key(self):
        """시트 row 와 같은 (method, endpoint) 의 수동 row 는 덮어쓰지 않고 보존."""
        from unittest import mock
        from apps.integrations.services import sync_owners_from_google_sheet

        ApiEndpointOwner.objects.create(
            repo="MAN", method="GET", endpoint="/api/keep",
            fe_owner="수동담당", is_imported=False,
        )
        csv_bytes = _sheet_csv_bytes([
            "AM PC,공고,AM BFF,GET,/api/keep,박세건,송현정,am-bff,1,s1",
        ])
        with mock.patch(
            "apps.integrations.google_sheets.export_sheet_csv",
            return_value=csv_bytes,
        ):
            result = sync_owners_from_google_sheet(user=None)

        self.assertEqual(result.skipped_manual, 1)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 0)
        row = ApiEndpointOwner.objects.get(method="GET", endpoint="/api/keep")
        self.assertEqual(row.fe_owner, "수동담당")  # 수동값 유지
        self.assertFalse(row.is_imported)

    def test_sync_logs_activity_with_google_sheet_label(self):
        from unittest import mock
        from apps.activitylog.models import ActivityLog
        from apps.integrations.services import sync_owners_from_google_sheet

        csv_bytes = _sheet_csv_bytes([
            "AM PC,공고,AM BFF,GET,/api/x,박세건,송현정,am-bff,1,s1",
        ])
        before = ActivityLog.objects.filter(action="api_owner_imported").count()
        with mock.patch(
            "apps.integrations.google_sheets.export_sheet_csv",
            return_value=csv_bytes,
        ):
            sync_owners_from_google_sheet(user=None)
        log = (
            ActivityLog.objects.filter(action="api_owner_imported")
            .order_by("-created_at")
            .first()
        )
        self.assertEqual(
            ActivityLog.objects.filter(action="api_owner_imported").count(), before + 1
        )
        self.assertTrue(log.target_name.startswith("Google Sheet"))

    # --- 자격증명 / 설정 로딩 --------------------------------------------
    def test_load_service_account_from_b64(self):
        import base64
        import json
        from unittest import mock
        from apps.integrations import google_sheets

        b64 = base64.b64encode(json.dumps(_FAKE_SA_INFO).encode("utf-8")).decode("ascii")
        with mock.patch.dict(os.environ, {"GOOGLE_SA_JSON_B64": b64}, clear=False):
            # 인라인 SA 경로 격리 — GOOGLE_CREDENTIALS_FILE(WIF) 가 설정돼 있으면 그쪽이 우선.
            os.environ.pop("GOOGLE_CREDENTIALS_FILE", None)
            info = google_sheets.load_service_account_info()
            self.assertEqual(
                info["client_email"], "qg-owners@qg-test.iam.gserviceaccount.com"
            )
            self.assertEqual(
                google_sheets.get_service_account_email(),
                "qg-owners@qg-test.iam.gserviceaccount.com",
            )

    def test_load_service_account_missing_raises(self):
        from unittest import mock
        from apps.integrations import google_sheets

        with mock.patch.dict(os.environ, {}, clear=False):
            for k in (
                "GOOGLE_CREDENTIALS_FILE",
                "GOOGLE_SA_JSON_B64",
                "GOOGLE_SA_JSON",
                "GOOGLE_SA_FILE",
            ):
                os.environ.pop(k, None)
            with self.assertRaises(google_sheets.GoogleSheetSyncError):
                google_sheets.load_service_account_info()
            self.assertIsNone(google_sheets.get_service_account_email())

    def test_external_account_share_principal_is_impersonation_email(self):
        """WIF(external_account) 자격증명이면 시트 공유 주체로 임퍼소네이트 SA 이메일 노출."""
        import json
        import tempfile
        from unittest import mock
        from apps.integrations import google_sheets

        wif = {
            "type": "external_account",
            "audience": "//iam.googleapis.com/projects/1/locations/global/"
            "workloadIdentityPools/p/providers/aws",
            "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
            "token_url": "https://sts.googleapis.com/v1/token",
            "service_account_impersonation_url": (
                "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                "qg-owners@qg-test.iam.gserviceaccount.com:generateAccessToken"
            ),
            "credential_source": {"environment_id": "aws1"},
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(wif, f)
            path = f.name
        try:
            with mock.patch.dict(
                os.environ, {"GOOGLE_CREDENTIALS_FILE": path}, clear=False
            ):
                self.assertEqual(
                    google_sheets.get_service_account_email(),
                    "qg-owners@qg-test.iam.gserviceaccount.com",
                )
        finally:
            os.unlink(path)

    def test_spreadsheet_id_missing_raises(self):
        from unittest import mock
        from apps.integrations import google_sheets

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GSHEET_OWNERS_SPREADSHEET_ID", None)
            with self.assertRaises(google_sheets.GoogleSheetSyncError):
                google_sheets.get_spreadsheet_id()

    # --- export HTTP 분기 -------------------------------------------------
    def test_export_sheet_csv_returns_bytes_on_200(self):
        from unittest import mock
        from apps.integrations import google_sheets

        class _Resp:
            status_code = 200
            text = ""
            content = b"col\n1\n"

        class _Session:
            def get(self, url, params=None, timeout=None):
                return _Resp()

        with mock.patch.object(google_sheets, "_build_session", return_value=_Session()):
            out = google_sheets.export_sheet_csv("FAKEID")
        self.assertEqual(out, b"col\n1\n")

    def test_export_sheet_csv_raises_on_403(self):
        from unittest import mock
        from apps.integrations import google_sheets

        class _Resp:
            status_code = 403
            text = "The caller does not have permission"
            content = b""

        class _Session:
            def get(self, url, params=None, timeout=None):
                return _Resp()

        with mock.patch.object(google_sheets, "_build_session", return_value=_Session()):
            with self.assertRaises(google_sheets.GoogleSheetSyncError) as ctx:
                google_sheets.export_sheet_csv("FAKEID")
        self.assertIn("403", str(ctx.exception))

    # --- 수동 동기화 view (admin) ----------------------------------------
    def test_sync_view_admin_post_redirects_and_imports(self):
        from unittest import mock
        from django.contrib.auth import get_user_model
        from django.test import Client

        User = get_user_model()
        admin = User.objects.create_user(
            username="admin_sync_test", password="x", role="qpe_admin", is_staff=True
        )
        csv_bytes = _sheet_csv_bytes([
            "AM PC,공고,AM BFF,GET,/api/synced,박세건,송현정,am-bff,1,s1",
        ])
        c = Client()
        c.force_login(admin)
        with mock.patch(
            "apps.integrations.google_sheets.export_sheet_csv",
            return_value=csv_bytes,
        ):
            resp = c.post("/accounts/api-owners/sync/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            ApiEndpointOwner.objects.filter(method="GET", endpoint="/api/synced").exists()
        )

    def test_sync_view_surfaces_error_message(self):
        """자격증명/시트 미설정 등 GoogleSheetSyncError 는 302 + error 메시지로."""
        from unittest import mock
        from django.contrib.auth import get_user_model
        from django.test import Client
        from apps.integrations.google_sheets import GoogleSheetSyncError

        User = get_user_model()
        admin = User.objects.create_user(
            username="admin_sync_err", password="x", role="qpe_admin", is_staff=True
        )
        c = Client()
        c.force_login(admin)
        # 뷰가 모듈 로드 시 자기 네임스페이스로 import 하므로 views.* 를 패치.
        with mock.patch(
            "apps.integrations.views.sync_owners_from_google_sheet",
            side_effect=GoogleSheetSyncError("미설정"),
        ):
            resp = c.post("/accounts/api-owners/sync/", follow=True)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("동기화 실패", body)

    def test_sync_view_requires_post(self):
        from django.contrib.auth import get_user_model
        from django.test import Client

        User = get_user_model()
        admin = User.objects.create_user(
            username="admin_sync_get", password="x", role="qpe_admin", is_staff=True
        )
        c = Client()
        c.force_login(admin)
        resp = c.get("/accounts/api-owners/sync/")
        self.assertEqual(resp.status_code, 405)

    # --- 자동 동기화 주기 (--if-due / OwnerSyncSettings) ------------------
    def _due_status(self):
        from apps.integrations.management.commands.sync_api_owners_from_sheet import (
            Command,
        )

        return Command()._due_status()

    def _set_interval(self, minutes):
        from apps.integrations.models import OwnerSyncSettings

        OwnerSyncSettings.objects.all().delete()
        OwnerSyncSettings.objects.create(interval_minutes=minutes)

    def _log_sync_now(self):
        from apps.activitylog.models import ActivityLog

        return ActivityLog.log(
            user=None,
            action=ActivityLog.Action.API_OWNER_IMPORTED,
            target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
            target_name="Google Sheet",
            detail="x",
        )

    def test_if_due_disabled_when_interval_zero(self):
        from apps.activitylog.models import ActivityLog

        self._set_interval(0)
        ActivityLog.objects.filter(
            action=ActivityLog.Action.API_OWNER_IMPORTED
        ).delete()
        should, _ = self._due_status()
        self.assertFalse(should)

    def test_if_due_runs_when_no_prior_sync(self):
        from apps.activitylog.models import ActivityLog

        self._set_interval(10)
        ActivityLog.objects.filter(
            action=ActivityLog.Action.API_OWNER_IMPORTED
        ).delete()
        should, _ = self._due_status()
        self.assertTrue(should)

    def test_if_due_skips_when_recent(self):
        self._set_interval(60)
        self._log_sync_now()  # 방금 동기화 → 60분 주기 미도래
        should, _ = self._due_status()
        self.assertFalse(should)

    def test_if_due_runs_when_interval_elapsed(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.activitylog.models import ActivityLog

        self._set_interval(10)
        log = self._log_sync_now()
        ActivityLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(minutes=20)
        )
        should, _ = self._due_status()
        self.assertTrue(should)

    def test_sync_settings_save_view_sets_and_disables_interval(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from apps.integrations.models import OwnerSyncSettings

        User = get_user_model()
        admin = User.objects.create_user(
            username="admin_sync_interval", password="x", role="qpe_admin", is_staff=True
        )
        c = Client()
        c.force_login(admin)

        resp = c.post(
            "/accounts/api-owners/sync-settings/save/", {"interval_minutes": "15"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(OwnerSyncSettings.get_solo().interval_minutes, 15)

        # 0 → 자동 동기화 끔
        c.post("/accounts/api-owners/sync-settings/save/", {"interval_minutes": "0"})
        self.assertEqual(OwnerSyncSettings.get_solo().interval_minutes, 0)

        # 잘못된 입력은 거부 (값 유지)
        c.post(
            "/accounts/api-owners/sync-settings/save/", {"interval_minutes": "abc"}
        )
        self.assertEqual(OwnerSyncSettings.get_solo().interval_minutes, 0)

    def test_api_owner_list_renders_sync_interval_form(self):
        """담당자 관리 목록 페이지가 자동 동기화 주기 폼과 함께 렌더된다 (템플릿 회귀)."""
        from django.contrib.auth import get_user_model
        from django.test import Client
        from apps.integrations.models import OwnerSyncSettings

        self._set_interval(15)
        User = get_user_model()
        admin = User.objects.create_user(
            username="admin_owner_list", password="x", role="qpe_admin", is_staff=True
        )
        c = Client()
        c.force_login(admin)
        resp = c.get("/accounts/api-owners/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("자동 동기화 주기", body)
        self.assertIn('name="interval_minutes"', body)
        self.assertIn("15분", body)


class JenkinsInternalGwtAuthTests(TestCase):
    """내부 Jenkins = 익명 read + GWT 트리거, 외부 = Basic auth 디스패치 검증."""

    def _client_internal(self):
        from apps.integrations.jenkins import JenkinsClient
        return JenkinsClient()  # base_url 미지정 → 내부

    def _client_external(self):
        from apps.integrations.jenkins import JenkinsClient
        return JenkinsClient(base_url="https://ext-jenkins", user="u", api_token="t")

    def test_internal_is_anonymous(self):
        c = self._client_internal()
        self.assertTrue(c.is_internal)
        self.assertEqual(c._auth_header, "")  # Authorization 헤더 없음(익명)

    def test_external_uses_basic_auth(self):
        c = self._client_external()
        self.assertFalse(c.is_internal)
        self.assertTrue(c._auth_header.startswith("Basic "))

    def test_internal_request_sends_no_auth_header(self):
        from unittest import mock
        c = self._client_internal()
        with mock.patch("apps.integrations.jenkins.urlopen") as mu:
            mu.return_value.__enter__.return_value.read.return_value = b'{"ok":1}'
            c._request("/api/json")
            req = mu.call_args[0][0]
            self.assertIsNone(req.get_header("Authorization"))

    def test_trigger_job_internal_requires_token(self):
        from apps.integrations.jenkins import JenkinsError
        c = self._client_internal()
        with self.assertRaises(JenkinsError):
            c.trigger_job("folder/job_1", gwt_token="")

    def test_trigger_job_internal_dispatches_to_webhook(self):
        from unittest import mock
        c = self._client_internal()
        with mock.patch.object(c, "trigger_webhook", return_value={"build_number": 5}) as mw:
            r = c.trigger_job("folder/job_1", gwt_token="tok123")
            mw.assert_called_once_with("folder/job_1", "tok123")
            self.assertEqual(r["build_number"], 5)

    def test_trigger_job_external_dispatches_to_build(self):
        from unittest import mock
        c = self._client_external()
        with mock.patch.object(c, "trigger_build", return_value={"build_number": 9}) as mb:
            c.trigger_job("folder/job_1", gwt_token="ignored-for-external")
            mb.assert_called_once_with("folder/job_1")

    def test_trigger_webhook_invoke_request(self):
        from unittest import mock
        from apps.integrations.jenkins import JenkinsClient
        c = self._client_internal()
        with mock.patch.object(JenkinsClient, "get_next_build_number", return_value=42), \
             mock.patch("apps.integrations.jenkins.urlopen") as mu:
            mu.return_value.__enter__.return_value.read.return_value = b""  # 빈 200 = 성공
            r = c.trigger_webhook("folder/job_1", "tok123")
            self.assertEqual(r["build_number"], 42)
            req = mu.call_args[0][0]
            self.assertIn("/generic-webhook-trigger/invoke", req.full_url)
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(req.get_header("Token"), "tok123")  # 토큰은 헤더로

    def test_trigger_webhook_no_matching_job_errors(self):
        from unittest import mock
        from apps.integrations.jenkins import JenkinsClient, JenkinsError
        c = self._client_internal()
        with mock.patch.object(JenkinsClient, "get_next_build_number", return_value=1), \
             mock.patch("apps.integrations.jenkins.urlopen") as mu:
            mu.return_value.__enter__.return_value.read.return_value = (
                b'{"jobs":null,"message":"Did not find any jobs with GenericTrigger configured!"}'
            )
            with self.assertRaises(JenkinsError):
                c.trigger_webhook("folder/job_1", "wrong-token")


class ApiOwnerFormAndModalTests(TestCase):
    """담당자 등록 폼 개선(드롭다운 + 사용회수 선택값) + Error Log 모달 AJAX 등록."""

    def setUp(self):
        invalidate_match_cache()
        # 드롭다운 choices 의 소스가 될 기존 row 들 (시트 미러 가정).
        ApiEndpointOwner.objects.create(
            repo="AM PC", method="GET", endpoint="/a", category="로그인 / 로그아웃",
            area="AM BFF", is_imported=True,
        )
        ApiEndpointOwner.objects.create(
            repo="JK Mobile", method="GET", endpoint="/b", category="스크랩",
            area="JK 이력서", is_imported=True,
        )

    def _admin(self):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.create_user(
            username="owner_form_admin", password="x", role="qpe_admin", is_staff=True,
        )

    def test_dropdown_choices_from_db_distinct(self):
        from apps.integrations.forms import ApiEndpointOwnerForm
        form = ApiEndpointOwnerForm()
        repo_vals = [v for v, _ in form.fields["repo"].choices]
        self.assertIn("AM PC", repo_vals)
        self.assertIn("JK Mobile", repo_vals)
        self.assertEqual(repo_vals[0], "")  # 빈 placeholder 옵션
        cat_vals = [v for v, _ in form.fields["category"].choices]
        self.assertIn("로그인 / 로그아웃", cat_vals)
        self.assertIn("스크랩", cat_vals)
        area_vals = [v for v, _ in form.fields["area"].choices]
        self.assertIn("AM BFF", area_vals)

    def test_repo_required_category_area_optional(self):
        from apps.integrations.forms import ApiEndpointOwnerForm
        form = ApiEndpointOwnerForm()
        self.assertTrue(form.fields["repo"].required)
        self.assertFalse(form.fields["category"].required)
        self.assertFalse(form.fields["area"].required)
        self.assertFalse(form.fields["usage_count"].required)

    def test_usage_count_optional_defaults_zero(self):
        from apps.integrations.forms import ApiEndpointOwnerForm
        form = ApiEndpointOwnerForm(data={
            "repo": "AM PC", "method": "POST", "endpoint": "/new/endpoint",
            "category": "", "area": "", "usage_count": "",
        })
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        self.assertEqual(obj.usage_count, 0)

    def test_edit_preserves_value_not_in_distinct_set(self):
        """편집 중 row 의 repo 값이 다른 row 들에 없어도 choices 에 유지돼야 한다."""
        from apps.integrations.forms import ApiEndpointOwnerForm
        obj = ApiEndpointOwner.objects.create(
            repo="LEGACY REPO", method="GET", endpoint="/legacy", is_imported=False,
        )
        form = ApiEndpointOwnerForm(instance=obj)
        self.assertIn("LEGACY REPO", [v for v, _ in form.fields["repo"].choices])

    def test_ajax_create_returns_json_and_persists(self):
        c = Client()
        c.force_login(self._admin())
        resp = c.post(
            "/accounts/api-owners/new/",
            data={
                "repo": "AM PC", "method": "POST",
                "endpoint": "/member/identity/identity-verification/result-info",
                "category": "로그인 / 로그아웃", "area": "AM BFF",
                "fe_owner": "김프론트", "be_owner": "김백엔드", "usage_count": "",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertTrue(
            ApiEndpointOwner.objects.filter(
                method="POST",
                endpoint="/member/identity/identity-verification/result-info",
            ).exists()
        )

    def test_ajax_create_invalid_returns_400_with_errors(self):
        c = Client()
        c.force_login(self._admin())
        resp = c.post(
            "/accounts/api-owners/new/",
            data={"repo": "", "method": "GET", "endpoint": "/x"},  # repo 누락
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("repo", body["errors"])

    def test_non_ajax_create_still_redirects(self):
        c = Client()
        c.force_login(self._admin())
        resp = c.post(
            "/accounts/api-owners/new/",
            data={
                "repo": "AM PC", "method": "DELETE", "endpoint": "/plain/post",
                "category": "", "area": "", "usage_count": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            ApiEndpointOwner.objects.filter(method="DELETE", endpoint="/plain/post").exists()
        )

    def test_error_log_page_renders_modal_for_admin(self):
        """Suite Error Log 페이지에 admin 일 때 등록 모달이 포함된다."""
        from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJob, TestSuiteJobRun
        suite = TestSuite.objects.create(name="m-suite", jenkins_folder_path="m/folder")
        run = TestSuiteRun.objects.create(suite=suite)
        job = TestSuiteJob.objects.create(suite=suite, job_name="m-job", job_full_path="m/job")
        jr = TestSuiteJobRun.objects.create(
            suite_run=run, job=job,
            console_output="@@@@@@@@@@\n  ERROR_URL: https://x.example.com/a\n  Method: GET\n@@@@@@@@@@",
        )
        c = Client()
        c.force_login(self._admin())
        resp = c.get(f"/suites/job-runs/{jr.pk}/error-log/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "owner-modal")
        self.assertContains(resp, "openOwnerModal")

    def test_error_log_table_shows_category_and_area(self):
        """이슈 F/U 담당자 표에 카테고리·영역 컬럼이 노출되고 매칭 owner 값이 채워진다."""
        from apps.suites.models import TestSuite, TestSuiteRun, TestSuiteJob, TestSuiteJobRun
        ApiEndpointOwner.objects.create(
            repo="AM PC", method="GET", endpoint="/member/cat-area-check",
            category="회원가입 / 인증", area="AM BFF", fe_owner="김프론트", is_imported=True,
        )
        suite = TestSuite.objects.create(name="ca-suite", jenkins_folder_path="ca/f")
        run = TestSuiteRun.objects.create(suite=suite)
        job = TestSuiteJob.objects.create(suite=suite, job_name="ca-job", job_full_path="ca/job")
        jr = TestSuiteJobRun.objects.create(
            suite_run=run, job=job,
            console_output=(
                "@@@@@@@@@@\n"
                "  ERROR_URL: https://x.example.com/member/cat-area-check\n  Method: GET\n"
                "@@@@@@@@@@"
            ),
        )
        c = Client()
        c.force_login(self._admin())
        resp = c.get(f"/suites/job-runs/{jr.pk}/error-log/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "<th>카테고리</th>")
        self.assertContains(resp, "<th>영역</th>")
        self.assertContains(resp, "회원가입 / 인증")
        self.assertContains(resp, "AM BFF")


class OwnerSheetWritebackTests(TestCase):
    """담당자 수동 등록 → Google Sheet 역방향 기록(append). 네트워크는 전부 mock."""

    class _Resp:
        def __init__(self, status=200, payload=None):
            self.status_code = status
            self._p = payload or {}
            self.text = ""
        def json(self):
            return self._p

    def _fake_session(self, headers, captured):
        Resp = self._Resp
        class _Sess:
            def get(self, url, params=None, timeout=None):
                if "/values/" in url:
                    return Resp(payload={"values": [headers]})
                return Resp(payload={"sheets": [{"properties": {"title": "마스터", "sheetId": 0}}]})
            def post(self, url, params=None, json=None, timeout=None):
                captured["url"] = url
                captured["params"] = params
                captured["json"] = json
                return Resp(status=200, payload={"updates": {}})
        return _Sess()

    def test_enabled_flag_opt_in(self):
        from apps.integrations.services import is_owner_writeback_enabled
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GSHEET_OWNERS_WRITEBACK", None)
            self.assertFalse(is_owner_writeback_enabled())  # 기본 off
        with mock.patch.dict(os.environ, {"GSHEET_OWNERS_WRITEBACK": "1"}):
            self.assertTrue(is_owner_writeback_enabled())
        with mock.patch.dict(os.environ, {"GSHEET_OWNERS_WRITEBACK": "off"}):
            self.assertFalse(is_owner_writeback_enabled())

    def test_owner_to_sheet_values(self):
        from apps.integrations.services import _owner_to_sheet_values
        o = ApiEndpointOwner.objects.create(
            repo="AM PC", method="POST", endpoint="${BFF_URL}/x",
            category="기타", area="AM BFF", fe_owner="김프", be_owner="김백", usage_count=3,
        )
        v = _owner_to_sheet_values(o)
        self.assertEqual(v["endpoint"], "${BFF_URL}/x")
        self.assertEqual(v["usage_count"], 3)
        self.assertEqual(v["area"], "AM BFF")

    def test_append_builds_row_in_sheet_header_order(self):
        from apps.integrations import google_sheets as gs
        from unittest import mock
        headers = ["레포", "Method", "Endpoint", "FE 담당자", "BE 담당자",
                   "카테고리", "영역", "매핑 앱", "사용 횟수", "step_ids"]
        captured = {}
        with mock.patch.object(gs, "_build_session", return_value=self._fake_session(headers, captured)):
            gs.append_owner_row({
                "repo": "AM PC", "method": "POST", "endpoint": "${BFF_PERSONAL_URL}/member/x",
                "fe_owner": "김프", "be_owner": "김백", "category": "기타", "area": "AM BFF",
                "mapped_app": "", "usage_count": 0, "step_ids": "",
            }, spreadsheet_id="dummy")
        row = captured["json"]["values"][0]
        # 시트 헤더 순서 그대로 + ${...} 원형 보존.
        self.assertEqual(row, ["AM PC", "POST", "${BFF_PERSONAL_URL}/member/x", "김프", "김백",
                               "기타", "AM BFF", "", 0, ""])
        self.assertEqual(captured["params"]["valueInputOption"], "USER_ENTERED")

    def test_append_unmapped_sheet_column_blank(self):
        """시트에만 있는 컬럼(예: 비고)은 빈칸으로 채워 양식 유지."""
        from apps.integrations import google_sheets as gs
        from unittest import mock
        captured = {}
        with mock.patch.object(gs, "_build_session",
                               return_value=self._fake_session(["레포", "Method", "Endpoint", "비고"], captured)):
            gs.append_owner_row({"repo": "R", "method": "GET", "endpoint": "/y"}, spreadsheet_id="d")
        self.assertEqual(captured["json"]["values"][0], ["R", "GET", "/y", ""])

    def test_async_noop_when_disabled(self):
        from apps.integrations import services
        from unittest import mock
        o = ApiEndpointOwner.objects.create(repo="R", method="GET", endpoint="/z")
        with mock.patch.object(services, "_is_test_run", return_value=False), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("apps.integrations.google_sheets.append_owner_row") as m:
            os.environ.pop("GSHEET_OWNERS_WRITEBACK", None)  # disabled
            services.writeback_owner_to_sheet_async(o)
        m.assert_not_called()

    def test_async_calls_append_when_enabled(self):
        """enabled + 가드 우회 시 append 호출 (Thread 는 동기 실행으로 패치)."""
        from apps.integrations import services
        from unittest import mock
        o = ApiEndpointOwner.objects.create(repo="R", method="GET", endpoint="/q",
                                            category="기타", area="AM BFF")

        def _sync_thread(target=None, daemon=None):
            class T:
                def start(self_):
                    target()
            return T()

        with mock.patch.object(services, "_is_test_run", return_value=False), \
             mock.patch.dict(os.environ, {"GSHEET_OWNERS_WRITEBACK": "1"}), \
             mock.patch.object(services.threading, "Thread", side_effect=_sync_thread), \
             mock.patch("apps.integrations.google_sheets.append_owner_row") as m:
            services.writeback_owner_to_sheet_async(o)
        m.assert_called_once()
        self.assertEqual(m.call_args[0][0]["endpoint"], "/q")


class SlackUserIdBackfillTests(TestCase):
    """이메일 기반 User.slack_user_id 자동 매핑 — Slack 호출은 mock."""

    def _user(self, name, email, role="qpe_ops", sid="", active=True):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.create_user(
            username=name, password="x", email=email, role=role,
            is_active=active, slack_user_id=sid)

    def test_maps_empty_by_email_preserves_existing(self):
        from apps.integrations import slack_user_sync as sus
        from unittest import mock
        u1 = self._user("a", "a@x.com")
        u2 = self._user("b", "b@x.com", role="qpe_admin", sid="U_KEEP")
        u3 = self._user("c", "")  # 이메일 없음 — 대상 아님
        with mock.patch.object(sus, "build_email_index",
                               return_value={"a@x.com": "U_A", "b@x.com": "U_B"}):
            res = sus.backfill_user_slack_ids()
        u1.refresh_from_db(); u2.refresh_from_db(); u3.refresh_from_db()
        self.assertEqual(u1.slack_user_id, "U_A")       # 빈 값 → 매핑
        self.assertEqual(u2.slack_user_id, "U_KEEP")    # 기존값 보존(overwrite=False)
        self.assertEqual(u3.slack_user_id, "")
        self.assertEqual(res["mapped"], 1)

    def test_overwrite_updates_existing(self):
        from apps.integrations import slack_user_sync as sus
        from unittest import mock
        u = self._user("b", "b@x.com", sid="U_OLD")
        with mock.patch.object(sus, "build_email_index", return_value={"b@x.com": "U_NEW"}):
            res = sus.backfill_user_slack_ids(overwrite=True)
        u.refresh_from_db()
        self.assertEqual(u.slack_user_id, "U_NEW")
        self.assertEqual(res["mapped"], 1)

    def test_unmatched_reported(self):
        from apps.integrations import slack_user_sync as sus
        from unittest import mock
        self._user("nomatch", "nomatch@x.com")
        with mock.patch.object(sus, "build_email_index", return_value={"other@x.com": "U_O"}):
            res = sus.backfill_user_slack_ids()
        self.assertIn("nomatch", res["unmatched"])
        self.assertEqual(res["mapped"], 0)

    def test_empty_index_no_crash(self):
        from apps.integrations import slack_user_sync as sus
        from unittest import mock
        self._user("a", "a@x.com")
        with mock.patch.object(sus, "build_email_index", return_value={}):
            res = sus.backfill_user_slack_ids()
        self.assertEqual(res["mapped"], 0)


class UiAutomationOwnersTests(TestCase):
    """담당자 관리 > UI 탭 — UI Test Automation(TAP) 실패 Slack 알림 담당자 (singleton).

    TAP 케이스는 URL 매칭 개념 밖이라 Suite/케이스 구분 없이 통으로 멘션한다.
    """

    def setUp(self):
        from apps.notifications.models import SlackNotificationSettings
        from apps.suites.models import TapRun, TapSuite

        s = SlackNotificationSettings.get_solo()
        s.enabled = True
        s.default_channel = "#qg-test"
        s.notify_tap_run_started = True
        s.notify_tap_run_passed = True
        s.notify_tap_run_failed = True
        s.save()
        self.suite = TapSuite.objects.create(
            tap_id=7701, name="ui-owner-suite", project_name="[AM] UI", case_count=2)
        self.run = TapRun.objects.create(
            tap_suite=self.suite, session_id=77001, status=TapRun.Status.COMPLETED,
            total_cases=2, completed_cases=2, passed=1, failed=1,
            raw_cases_json=[{"title": "로그인", "status": "failed",
                             "testrail_url": "https://testrail/x/1"}],
        )

    def _set_owners(self, text):
        from apps.integrations.models import UiAutomationOwners
        obj = UiAutomationOwners.get_solo()
        obj.owners = text
        obj.save()

    def _send(self, event_type, run=None):
        """send_tap_run_event 를 실제 호출하고 _post_message 에 넘어간 (channel, title) 을 캡처."""
        from unittest import mock
        from apps.notifications.services import send_tap_run_event
        with mock.patch(
            "apps.integrations.slack_users.find_user_id_by_name",
            side_effect=lambda n: {"김태식": "U_KIM"}.get(n),
        ), mock.patch(
            "apps.notifications.services._post_message", return_value=True,
        ) as post:
            ok = send_tap_run_event(event_type, run or self.run)
        return ok, post

    def _admin(self):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.create_user(
            username="ui_owner_admin", password="x", role="qpe_admin", is_staff=True)

    def test_singleton_and_names(self):
        from apps.integrations.models import UiAutomationOwners
        a = UiAutomationOwners.get_solo()
        b = UiAutomationOwners.get_solo()
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(UiAutomationOwners.objects.count(), 1)
        a.owners = "김태식, 김도연 ,  ,홍길동"
        a.save()
        self.assertEqual(a.names(), ["김태식", "김도연", "홍길동"])

    def test_fail_alert_mentions_ui_owners(self):
        """FAIL — 매칭된 이름은 <@U>, 실패는 @이름 으로 top-level 본문에 멘션."""
        from apps.notifications import events as E
        self._set_owners("김태식, 김도연")
        ok, post = self._send(E.TAP_RUN_FAILED)
        self.assertTrue(ok)
        post.assert_called_once()
        channel, title = post.call_args.args[0], post.call_args.args[1]
        self.assertEqual(channel, "#qg-test")
        self.assertIn("UI Suite FAIL", title)
        self.assertIn("*담당자 F/U*", title)
        self.assertIn("<@U_KIM>", title)
        self.assertIn("@김도연", title)

    def test_schedule_fail_alert_also_mentions(self):
        """Reliability 스케줄 실행 FAIL 도 멘션 (UI 실패 알림 대부분이 스케줄 실행)."""
        from apps.notifications import events as E
        from apps.robustness.models import RobustnessSchedule, RobustnessTest
        rt = RobustnessTest.objects.create(name="ui-owner-rt")
        sched = RobustnessSchedule.objects.create(
            robustness_test=rt, tap_suite=self.suite, interval_minutes=60)
        self.run.robustness_schedule = sched
        self.run.save(update_fields=["robustness_schedule"])
        self._set_owners("김태식")
        _ok, post = self._send(E.TAP_RUN_FAILED)
        self.assertIn("<@U_KIM>", post.call_args.args[1])

    def test_fail_alert_without_owners_has_no_mention(self):
        """미설정 — 멘션 없이 FAIL 알림은 그대로 발송."""
        from apps.notifications import events as E
        self._set_owners("")
        ok, post = self._send(E.TAP_RUN_FAILED)
        self.assertTrue(ok)
        title = post.call_args.args[1]
        self.assertIn("UI Suite FAIL", title)
        self.assertNotIn("담당자 F/U", title)

    def test_started_and_passed_alerts_do_not_mention(self):
        from apps.notifications import events as E
        self._set_owners("김태식")
        for ev in (E.TAP_RUN_STARTED, E.TAP_RUN_PASSED):
            _ok, post = self._send(ev)
            self.assertNotIn("<@U_KIM>", post.call_args.args[1], ev)
            self.assertNotIn("담당자 F/U", post.call_args.args[1], ev)

    def test_mention_failure_does_not_block_fail_alert(self):
        """담당자 조회가 터져도(예: 배포 직후 마이그레이션 전) FAIL 알림은 발송."""
        from unittest import mock
        from apps.notifications import events as E
        with mock.patch(
            "apps.integrations.models.UiAutomationOwners.get_solo",
            side_effect=RuntimeError("no such table"),
        ):
            ok, post = self._send(E.TAP_RUN_FAILED)
        self.assertTrue(ok)
        self.assertNotIn("담당자 F/U", post.call_args.args[1])

    def test_owner_list_tabs_render(self):
        """기본 = API 탭(매핑 표), ?tab=ui = UI 담당자 폼. 두 탭 링크 모두 노출."""
        c = Client()
        c.force_login(self._admin())
        self._set_owners("김태식, 김도연")

        resp = c.get("/accounts/api-owners/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "API Endpoint 담당자 매핑")
        self.assertContains(resp, "?tab=ui")
        self.assertNotContains(resp, "/accounts/api-owners/ui/save/")

        resp = c.get("/accounts/api-owners/?tab=ui")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "UI Test Automation 실패 알림 담당자")
        self.assertContains(resp, "/accounts/api-owners/ui/save/")
        self.assertContains(resp, 'value="김태식, 김도연"')
        self.assertNotContains(resp, "API Endpoint 담당자 매핑")

    def test_save_view_persists_logs_and_redirects_to_ui_tab(self):
        from apps.activitylog.models import ActivityLog
        from apps.integrations.models import UiAutomationOwners
        c = Client()
        c.force_login(self._admin())
        resp = c.post("/accounts/api-owners/ui/save/", {"owners": "김태식, 김도연"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].endswith("/accounts/api-owners/?tab=ui"))
        self.assertEqual(UiAutomationOwners.get_solo().owners, "김태식, 김도연")
        self.assertTrue(ActivityLog.objects.filter(
            action=ActivityLog.Action.UPDATE, target_name="UI 알림 담당자").exists())

    def test_save_view_rejects_non_admin_and_get(self):
        from django.contrib.auth import get_user_model
        from apps.integrations.models import UiAutomationOwners
        self._set_owners("기존")
        c = Client()
        c.force_login(get_user_model().objects.create_user(
            username="ui_owner_ops", password="x", role="qpe_ops"))
        resp = c.post("/accounts/api-owners/ui/save/", {"owners": "침입"})
        self.assertEqual(resp.status_code, 302)  # 권한 미들웨어가 non-admin 을 돌려보냄
        self.assertEqual(UiAutomationOwners.get_solo().owners, "기존")

        c.force_login(self._admin())
        resp = c.get("/accounts/api-owners/ui/save/")
        self.assertEqual(resp.status_code, 405)
