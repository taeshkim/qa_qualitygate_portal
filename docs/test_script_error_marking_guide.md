# 자동화 테스트 스크립트 에러 마킹 가이드 (초안)

> **상태**: 초안 (draft). 합의되면 Quality Gate Portal 측 파서/표시 연동을 진행한다.
> **대상**: Quality Gate / Test Suite 에서 호출되는 모든 Jenkins Job 의 테스트 스크립트 작성자.

---

## 1. 배경

현재 알바몬 API 회귀 테스트 등은 이미 풍부한 텍스트 마커로 콘솔을 찍고 있다.
사람 눈에는 충분히 구조화돼 있지만, Quality Gate Portal 이 **한 번에 실패 요약을
긁어 화면 상단에 카드/Slack 알림으로 노출** 하기엔 정규식이 다층으로 필요해 깨지기 쉽다.

이 가이드는 **두 단계** 접근을 제안한다.

- **Level 1 — 기존 텍스트 마커 그대로**: 스크립트 무수정. 포털이 기존 `[Step]` / `[FAIL]` /
  `[FAIL DETAIL]` / `[TestRail] xxx -> FAIL` / `[FAIL] SUITE FAILED — ...` 등을
  카탈로그대로 파싱한다. 기존 스크립트는 이 단계만으로도 80% 활용 가능.
- **Level 2 — 꼬리에 JSON 1블록 추가 (권장)**: 스크립트 종료 직전 `##QG-RESULT##` JSON 1개,
  필요시 실패당 `##QG-FAIL##` JSON 1개. 포털이 정규식 짜맞추기 없이 결정적으로 파싱.

Level 1 만 충족해도 동작하며, Level 2 가 있으면 안정성/세부도가 올라간다.

---

## 2. Level 1 — 기존 텍스트 마커 (스크립트 무수정)

실제 알바몬 API 회귀 테스트 로그를 기준으로, 포털이 인식할 마커 카탈로그.

### 2.1 인식 대상 마커

| 마커 패턴 | 의미 | 추출되는 값 |
|---|---|---|
| `[Step] <name>` (그 줄 또는 직후) | 케이스 시작 | 케이스명 |
| `[FAIL] Expected status N, got M` 등 `[Step]` 직후 한 줄짜리 FAIL | 케이스 실패 | 1줄 요약 message |
| `[FAIL DETAIL] <name>` … 다음 `────` 줄까지 | 실패 상세 블록 | Request/Response/Expected vs Actual |
| `[Request] Method / URL / Headers / Body` | 요청 메타 | location 후보 (URL + Method) |
| `[Response] Status / Headers / Body` | 응답 메타 | actual 후보 (status code) |
| `[Expected vs Actual] Status : expected=X, actual=Y` | 단언 비교 | expected / actual |
| `[TestRail] <case_id> -> FAIL` | TestRail 매핑 | **case_id** ✅ |
| `[STOP] Step '<name>' failed.` | 중단점 | 첫 차단 케이스 표시 |
| `✗ FAIL - <name>` (요약 표) | 결과 행 | 실패 케이스 목록 검증용 |
| `✓ PASS - <name>` (요약 표) | 결과 행 | 성공 케이스 목록 |
| `[FAIL] SUITE FAILED — suite=<file>, 실패 N건: <names>` | 스위트 종합 | suite 파일명, 실패 수, 이름 목록 |
| `[FAIL] PYTEST ASSERTION FAIL — suite=<file>, module=<m>, reason=<r>` | pytest 진입점 실패 | suite / module / reason |
| `[FAIL] STAGE: <stage> — <reason> \| <jenkins_url>` | 파이프라인 stage 실패 | stage / reason / 참고 URL |
| `테스트 시작/종료/총 소요 시간` (#### 배너) | 시간 메타 | started/ended/duration |

`@@@@…@@@@` 배너는 같은 실패가 한 번 더 반복돼 들어오는 dup 신호로 본다 (포털 측 dedup).

### 2.2 스크립트 작성자가 지켜야 할 최소 규칙 (Level 1)

이미 대부분 지키고 있는 것들. 명시화해서 합의만 맞춘다.

- **`[Step] <name>`** 는 케이스 시작 시 1회. `<name>` 은 같은 Run 내에서 일관되게.
- 실패 시 `[Step]` 직후 같은 들여쓰기 줄에 **한 줄짜리 `[FAIL] <짧은 사유>`** 를 찍는다.
  여러 줄에 걸쳐 찍지 말 것 — 첫 한 줄만 message 로 잡힘.
- 상세는 `[FAIL DETAIL] <name>` 블록 안에서만. 블록 시작/끝은 `────` 라인으로 둘러쌀 것.
- **`[TestRail] <case_id> -> FAIL`** 가 있으면 그 `<case_id>` 가 portal 의 `case_id` 가 된다.
  없으면 `[Step] <name>` 의 name 을 fallback.
- 종합 줄 (`[FAIL] SUITE FAILED — …`) 은 한 번만 출력. suite 파일명 (e.g. `test_suite_4.json`) 을 꼭 포함.
- `❌` / `✗` / `[FAIL]` 토큰은 실제 실패 외엔 절대 찍지 않을 것. (배너 장식 안에 우연히 들어가도 카운트됨)

이게 안 지켜지면 Level 1 정밀도가 떨어진다 — 그런 스크립트는 Level 2 를 같이 적용한다.

### 2.3 Level 1 만 있을 때 포털이 보여줄 수 있는 것

- 실패 케이스 표 (case_id, name, message, expected/actual, 첫 stop 표시).
- 스위트 종합 (suite 파일명, 실패 N건, 실패 케이스명 목록).
- "stage 실패 / pytest assertion / suite 실패" 어느 층에서 멈췄는지.
- TestRail Run 링크 — `https://jobkorea.testrail.io/index.php?/runs/view/<N>` 가 콘솔에 있으면 그대로 추출해 카드에 노출.

콘솔 본문 (Body / Headers raw) 은 길이가 커서 카드엔 truncate, 클릭 시 펼침.

---

## 3. Level 2 — 꼬리에 JSON 마커 추가 (권장)

기존 출력은 그대로 두고, **스크립트 종료 직전에 JSON 블록 1~N개를 추가** 한다.
포털 파서가 결정적 (deterministic) 으로 동작하게 만드는 비용 대비 효과가 가장 큰 변경.

### 3.1 두 종류

| 마커 | 언제 | 몇 번 | 용도 |
|---|---|---|---|
| `##QG-FAIL##` … `##/QG-FAIL##` | 실패 1건 확정 시 (즉시 또는 종료 직전 일괄) | 실패당 1개 | 케이스/스텝 단위 실패 상세 (구조화) |
| `##QG-RESULT##` … `##/QG-RESULT##` | 스크립트 종료 직전 | 1개 (성공이어도 권장) | 전체 합계 + 실패 id 목록 |

마커 사이엔 **단일 라인 JSON** 만 둔다. 줄바꿈/들여쓰기 없음. UTF-8. 마커는 줄 맨 앞.

### 3.2 `##QG-FAIL##` 필드

| 필드 | 필수 | 타입 | 설명 |
|---|---|---|---|
| `case_id` | ✅ | string | TestRail case 식별자(`cp_payment` 등) 또는 step 이름. 동일 Run 안에서 unique 권장. |
| `severity` | ✅ | enum | `error` \| `warning` \| `fatal`. 보통 `error`. |
| `message` | ✅ | string | 한 줄 요약 (예: `Expected status 200, got 503`). 줄바꿈 금지. |
| `step_name` | ⭕ | string | 사람용 케이스명 (`공고 결제 (캐시포인트)`). |
| `location` | ⭕ | string | `POST https://stg1-pay.albamon.com/PaymentRequestLogSave` 형태. |
| `expected` | ⭕ | string/number | 예: `200`, `success`. |
| `actual` | ⭕ | string/number | 예: `503`. |
| `request` | ⭕ | object | `{method, url, headers, body}`. body 가 크면 일부만 잘라 넣고 `truncated:true` 표시. |
| `response` | ⭕ | object | `{status, headers, body}`. 동일. |
| `hint` | ⭕ | string | 수정 힌트. |
| `tags` | ⭕ | array<string> | `["api-5xx"]`, `["payment"]`, `["flaky"]` 등. |
| `detail_url` | ⭕ | string | 스크린샷/HAR/덤프 URL. |
| `stopped_after` | ⭕ | bool | `[STOP]` 으로 후속 케이스를 끊은 케이스면 `true`. |

### 3.3 `##QG-RESULT##` 필드

| 필드 | 필수 | 타입 | 설명 |
|---|---|---|---|
| `suite` | ⭕ | string | suite 파일명 (e.g. `test_suite_4.json`). |
| `total` | ✅ | int | 시도한 케이스 총수 (PASS+FAIL+SKIP). |
| `passed` | ✅ | int |  |
| `failed` | ✅ | int | `##QG-FAIL##` 블록 개수와 일치해야 함. |
| `skipped` | ⭕ | int |  |
| `failed_ids` | ⭕ | array<string> | 실패한 `case_id` 의 순서 보존 목록. |
| `duration_sec` | ⭕ | number | wall-clock 초. |
| `testrail_run_url` | ⭕ | string | `https://jobkorea.testrail.io/index.php?/runs/view/10545` 등. |
| `env` | ⭕ | object | `{"target":"stg-4","jenkins_build":100,"branch":"release_0501"}` 등. |

### 3.4 예시 — 실제 로그를 Level 2 로 보강하면

기존 출력은 그대로 두고, **맨 끝에** 이 3블록만 더한다.

```
##QG-FAIL##
{"case_id":"cp_payment","severity":"error","step_name":"공고 결제 (캐시포인트)","message":"Expected status 200, got 503","location":"POST https://stg1-pay.albamon.com/PaymentRequestLogSave","expected":200,"actual":503,"response":{"status":503,"body":"<HTML>...Service Unavailable...</HTML>","truncated":true},"tags":["payment","api-5xx"],"stopped_after":true}
##/QG-FAIL##
##QG-RESULT##
{"suite":"test_suite_4.json","total":21,"passed":20,"failed":1,"skipped":0,"failed_ids":["cp_payment"],"duration_sec":33.0,"testrail_run_url":"https://jobkorea.testrail.io/index.php?/runs/view/10545","env":{"target":"stg-4","jenkins_build":100}}
##/QG-RESULT##
```

이 3 블록만 있으면 포털은 Level 1 정규식을 fallback 으로만 쓰고, 화면 표시/Slack/API 응답에 그대로 사용.

---

## 4. 종료 코드 / 빌드 결과와의 관계

마커는 **Jenkins 종료 코드를 대체하지 않는다.** 둘 다 맞춰야 한다.

| 상황 | exit code | `##QG-FAIL##` | `##QG-RESULT##.failed` |
|---|---|---|---|
| 전부 성공 | 0 | 없음 | 0 |
| 일부 케이스 실패 | **non-zero** | 실패 수만큼 | >0 |
| 환경 오류로 시작도 못함 | non-zero | 1개 (`severity=fatal`) | 1 이상 또는 생략 |

케이스가 깨졌는데 exit 0 으로 끝내지 말 것. Jenkins 가 SUCCESS 로 보고하면
포털은 PASSED 로 표시하고, `##QG-FAIL##` 블록은 "참고용 경고" 로만 잡힌다.

---

## 5. 언어별 헬퍼 예시 (Level 2)

기존 스크립트의 출력 함수에 5~10줄만 더하면 된다.

### Python

```python
import json, sys, time

def qg_fail(**kw):
    print("##QG-FAIL##")
    print(json.dumps(kw, ensure_ascii=False, separators=(",", ":")))
    print("##/QG-FAIL##", flush=True)

def qg_result(**kw):
    print("##QG-RESULT##")
    print(json.dumps(kw, ensure_ascii=False, separators=(",", ":")))
    print("##/QG-RESULT##", flush=True)

# 호출 예 (test runner 종료 직전)
qg_fail(
    case_id="cp_payment",
    severity="error",
    step_name="공고 결제 (캐시포인트)",
    message="Expected status 200, got 503",
    location="POST https://stg1-pay.albamon.com/PaymentRequestLogSave",
    expected=200, actual=503,
    response={"status": 503, "body": resp_body[:1024], "truncated": len(resp_body) > 1024},
    tags=["payment", "api-5xx"],
    stopped_after=True,
)
qg_result(
    suite="test_suite_4.json",
    total=21, passed=20, failed=1, skipped=0,
    failed_ids=["cp_payment"],
    duration_sec=33.0,
    testrail_run_url=testrail_url,
    env={"target": "stg-4", "jenkins_build": int(os.environ.get("BUILD_NUMBER", 0))},
)
sys.exit(1 if failed_count else 0)
```

### pytest 플러그인 (조각)

```python
# conftest.py — 케이스마다 ##QG-FAIL## 1줄 + 세션 종료 시 ##QG-RESULT## 1줄
import json, time

_failures = []   # case_id 목록
_records = []    # 풍부한 정보 (선택)
_start = time.time()

def pytest_runtest_logreport(report):
    if report.when == "call" and report.failed:
        case_id = report.nodeid.split("::")[-1]
        msg = str(report.longrepr).splitlines()[-1][:300]
        rec = {
            "case_id": case_id, "severity": "error",
            "message": msg, "location": report.location[0],
        }
        print("##QG-FAIL##")
        print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        print("##/QG-FAIL##", flush=True)
        _failures.append(case_id)
        _records.append(rec)

def pytest_sessionfinish(session, exitstatus):
    total = session.testscollected
    print("##QG-RESULT##")
    print(json.dumps({
        "total": total, "passed": total - len(_failures), "failed": len(_failures),
        "failed_ids": _failures, "duration_sec": round(time.time() - _start, 1),
    }, ensure_ascii=False, separators=(",", ":")))
    print("##/QG-RESULT##", flush=True)
```

### Bash

```bash
qg_fail() { printf '##QG-FAIL##\n%s\n##/QG-FAIL##\n' "$1"; }
qg_result() { printf '##QG-RESULT##\n%s\n##/QG-RESULT##\n' "$1"; }

qg_fail '{"case_id":"smoke-login","severity":"error","message":"로그인 실패","location":"curl https://stg.example.com/login - HTTP 502"}'
qg_result '{"total":3,"passed":2,"failed":1,"failed_ids":["smoke-login"]}'
exit 1
```

---

## 6. 권장 마이그레이션 경로

스크립트를 한 번에 다 고치지 않아도 된다.

1. **0단계 — 현행 유지**: 포털 Level 1 파서가 깔리면 별도 작업 없이도 화면/Slack 요약이 좋아진다.
2. **1단계 — 꼬리 `##QG-RESULT##` 만 추가** (~5줄): 스위트 종료부에 결과 JSON 1줄. 가장 비용 대비 효과 큼.
3. **2단계 — 실패당 `##QG-FAIL##` 추가**: 기존 `[FAIL DETAIL]` 출력 직후/직전에 JSON 1줄을 같이 찍는다. 사람용 출력은 그대로.
4. **3단계 — Level 1 의존 제거** (선택): 포털이 Level 2 만 있으면 충분하다고 판단되면 Level 1 fallback 을 끌 수 있다.

신규 스크립트는 처음부터 Level 2 로 작성 권장.

---

## 7. 작성 시 체크리스트

### Level 1 (기존 포맷)

- [ ] `[Step] <name>` 는 케이스 시작에 1회. 같은 Run 내 같은 name 사용.
- [ ] 실패 시 `[Step]` 직후 줄에 **한 줄짜리** `[FAIL] <짧은 사유>`.
- [ ] 상세는 `[FAIL DETAIL] <name>` … `────` 블록 안에서만.
- [ ] `[TestRail] <case_id> -> FAIL` 로 case_id 매핑 명시.
- [ ] 종합 `[FAIL] SUITE FAILED — suite=<file>, 실패 N건: <names>` 1회.
- [ ] `[FAIL]` / `✗` / `❌` 토큰을 장식 / 배너 안에 우연히 찍지 말 것.

### Level 2 (JSON 꼬리)

- [ ] `##QG-FAIL##` / `##QG-RESULT##` 마커는 줄 맨 앞, 좌우 공백 없음.
- [ ] 마커 사이 JSON 은 **단일 라인**, 들여쓰기 없음, UTF-8.
- [ ] `##QG-RESULT##` 는 스크립트당 1개.
- [ ] `##QG-RESULT##.failed` 가 `##QG-FAIL##` 블록 수와 일치.
- [ ] body / headers 등 큰 raw 는 1KB 이내로 자르고 `truncated:true` 또는 `detail_url`.
- [ ] 케이스가 깨졌으면 exit code != 0.
- [ ] 동일 `case_id` 가 한 Run 안에서 여러 번 나와도 됨 (재시도 등).

---

## 8. 포털 측 연동 (작업 예정 — 참고용)

이 가이드가 합의되면 포털에서 아래를 붙인다. 스크립트 작성자는 신경 안 써도 됨.

1. **콘솔 파서** — `apps/suites/parsers/qg_markers.py` 신설. Level 2 (`##QG-FAIL##` / `##QG-RESULT##`) 우선 파싱, 없으면 Level 1 텍스트 마커 fallback. 기존 `apps/suites/diagnostics.py` (LLM 브라우저용) 은 별도 분석으로 유지.
2. **Job Run 콘솔 화면** (`templates/suites/console_output.html`) 상단에 "오류 요약" 카드 — 실패 케이스 표 + suite 종합 통계 + TestRail Run 링크.
3. **Quality Gate Run 상세 화면** — Suite 별 카드에 "실패 N건 · 첫 실패: <case_id> <message>" 미리보기.
4. **Slack 알림** (`TEST_SUITE_FAILED` / `SUITE_RUN_FAILED`) 의 fields 에 첫 실패의 `case_id`/`message`/`location` 첨부. 5건 초과 시 "외 N건".
5. **API v2** (`/api/v2/suite-job-runs/<id>/`) 응답에 `failures: [...]`, `result: {...}`, `testrail_run_url` 노출 → 외부 CI 가 콘솔 파싱 없이 활용.
6. **`##QG-RESULT##` 미존재 + non-zero exit** = "스크립트가 비정상 종료" 로 간주, 별도 배지.

---

## 9. FAQ

**Q. 기존 알바몬 API 회귀 테스트 스크립트를 다 고쳐야 하나?**
A. 아니다. Level 1 만으로도 화면 요약은 동작한다. 다만 `[FAIL]` 토큰 오용 등으로
오탐이 나면 `##QG-RESULT##` 한 줄만 추가하는 게 가장 빠른 해결.

**Q. JUnit XML 안 쓰는 이유?**
A. 포털은 Jenkins API 의 콘솔 스트림만 쓴다. JUnit XML 은 artifact fetch /
archive plugin 의존이 추가로 필요. 콘솔 inline 마커는 의존성 0 이고, 기존 콘솔 뷰에
자연스럽게 보임. JUnit 을 쓰는 팀은 사이드바이사이드 같이 출력해도 무방.

**Q. `[FAIL]` 가 한 줄에 여러 의미로 쓰이는데 (`[FAIL] STAGE: ...`, `[FAIL] SUITE FAILED`, `[FAIL] Expected status ...`) 구분되나?**
A. 포털은 위계 규칙으로 분리한다.
- `[FAIL] Expected ...` / 그 외 `[Step]` 직속 한 줄 → **케이스 실패**.
- `[FAIL] SUITE FAILED — suite=...` → **스위트 종합**.
- `[FAIL] PYTEST ASSERTION FAIL` → pytest 진입점 단언.
- `[FAIL] STAGE: ...` → 파이프라인 stage 메시지.
다른 의미로 같은 토큰을 새로 만들지 말 것.

**Q. `@@@@` 배너 안에서 `[FAIL DETAIL]` 가 한 번 더 출력되는데 중복으로 안 잡히나?**
A. 포털이 dedup. 같은 `[Step] <name>` + 같은 `[FAIL DETAIL] <name>` 는 1회로 카운트.

**Q. case_id 가 같은 케이스가 한 Run 에서 PASS 도 있고 FAIL 도 있으면?**
A. 마지막 등장 결과를 기준으로 한다 (재시도 후 통과 시나리오 지원). `##QG-FAIL##`
블록도 같은 `case_id` 중복 가능 — 포털이 시간순으로 보존하고, 통계는 마지막 상태로.

**Q. 마커 토큰을 바꿔도 되나?**
A. 합의되면 한 번 픽스. 한 번 정해진 뒤엔 모든 스크립트와 포털 파서가 같이 움직여야
하므로 가벼운 변경도 PR 로 동기화.

---

## 10. 변경 이력

| 일자 | 내용 |
|---|---|
| 2026-05-15 | 초안 작성 |
| 2026-05-15 | 실제 알바몬 API 회귀 테스트 로그 포맷 반영 — Level 1 (기존 텍스트 마커) + Level 2 (JSON 꼬리) 2단 접근으로 재구성 |
