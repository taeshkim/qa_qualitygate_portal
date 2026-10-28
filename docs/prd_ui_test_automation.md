# PRD — Test Suites 2탭 확장: UI Test Automation (TAP 연동) + API 잡 자동 싱크

작성: 2026-07-28 · 상태: **승인 대기 → 구현 진행 (Phase 1~3 완료, §3.6 Reliability 연결 포함)** · 관련: `prd_api.md`, `maintenance.md` §3(Suites)

## 1. 배경 / 목표

- 포털의 Test Suites 는 현재 **Jenkins 기반 API 자동화**만 다룬다. UI 자동화는 별도 시스템
  **TAP(tapilot.jkintra.net, FastAPI + Playwright/TestRail)** 에서 돌고 있어 포털에서 보이지 않는다.
- 목표: Test Suites 화면을 **2탭**으로 확장해
  ① 기존 Jenkins Suites = **API Test Automation** 탭,
  ② TAP 의 Suites = **UI Test Automation** 탭으로 한 화면에서 조회·실행·진행 확인.
- 부가 목표: API 탭에도 **Jenkins 잡 자동 싱크**(이름에 `api` 포함 잡 자동 등록) 추가.

## 2. TAP API 실측 요약 (2026-07-28 검증 완료)

| 항목 | 내용 |
|---|---|
| Base | `https://tapilot.jkintra.net` (같은 호스트에서 운영 — `/home/taeshkim/pilot_ta_llm_agent`) |
| 인증 | `Authorization: Bearer tap_...` — TAP 호스트에서 `manage.py issue_api_key llm --scopes read runs:trigger` 로 발급 완료, 포털 `.env` 의 `TAPILOT_API_BASE`/`TAPILOT_API_KEY` 에 저장(평문 미기록 원칙 준수) |
| Suite 목록 | `GET /api/v2/suites/` → `[{id, name, description, owner_display, project_id/name, suite_id/name, milestone_id/…, case_ids}]` |
| Suite 실행 | `POST /api/v2/suites/{id}/run/` → `{session_id, queued, queue_position, run_env, excluded_case_ids, tap_url}` (201) |
| 진행 폴링 | `GET /api/v2/runs/{session_id}/status/` → `{status: queued\|pending\|running\|completed\|stopped\|error, terminal, total_cases, completed_cases, progress_percent, passed, failed, others}` + 헤더 `X-TAP-Poll-After`(권장 간격 초, 0=종결) |
| 케이스별 결과 | `GET /api/v2/runs/{session_id}/` → cases[]: `{result_id, case_id, title, section_name, status, duration_sec, testrail_url, raw_output_tail}` + 영상/스크린샷 다운로드 경로 |
| 취소 | `POST /api/v2/runs/{session_id}/cancel/` |
| 현재 데이터 | Suite 1건: `jk-dweb-regression-ui-dev` ([JK] Regression Test (PC), 19 cases) |

## 3. 범위

### 3.1 Test Suites 2탭 (frontend)
- `/suites/` 상단에 탭 2개: **API Test Automation**(기본, 기존 목록 그대로) / **UI Test Automation**.
- 탭 상태는 쿼리스트링 `?tab=ui` — 새로고침/딥링크 유지. 기존 URL 무변경(하위호환).
- UI 탭 목록 컬럼: Suite 명 · 프로젝트 · 케이스 수 · 담당자(owner_display) · 마지막 실행(상태/시각) ·
  진행 중이면 **진행바**(기존 Suite Run 과 동일한 시각 언어) · [실행]/[취소] 버튼 · TAP 딥링크.

### 3.2 TAP Suite 1분 자동 싱크 (backend)
- 새 모델 `TapSuite` — TAP `GET /suites/` 응답 미러 (tap_id unique, name, project, case_count,
  owner_display, milestone, raw_json, synced_at, `is_removed`(TAP 에서 사라진 경우 soft-flag)).
- 싱크 주기 **1분**: 기존 매분 cron 명령(`sweep_reliability`)에 `_sync_tap_suites()` 단계를 추가
  — **crontab 무변경**(이미 등록된 명령 재사용, 2026-07-21 단독 Suite Run 폴링 확장과 동일 패턴).
  TAP 다운 시 try/except 로 스킵(마지막 싱크본 유지, 화면에 synced_at 표시).

### 3.3 TAP Suite 실행 / 진행 / 결과 (backend + frontend)
- 새 모델 `TapRun` — `{tap_suite FK, session_id, status, total_cases, completed_cases,
  passed, failed, others, run_env, tap_url, testrail_run_id?, triggered_by, started_at, finished_at, raw_cases_json}`.
- 실행: UI 탭 [실행] → `POST /suites/{tap_id}/run/` → TapRun 생성(queued/pending).
  `excluded_case_ids` 는 결과에 표기(자동화 스크립트 없는 케이스 자동 제외 — TAP 정책).
- 진행: 매분 cron + 화면 HTMX 폴링(기존 suite run 과 동일 5~10초)이 RUNNING TapRun 을
  `GET /runs/{session_id}/status/` 로 갱신. **진행바 = completed_cases / total_cases**
  (Jenkins stage 게이지 대신 케이스 수 기준 — 요구사항 반영). `X-TAP-Poll-After` 존중.
- 종결: `terminal=true` → status 반영(completed/stopped/error) + passed/failed 뱃지.
  완료 시 `GET /runs/{session_id}/` 로 케이스별 결과 저장 → 포털 상세(모달 or 페이지)에서
  케이스 목록 + TestRail 링크 + 실패 케이스 로그 tail 노출. 영상/스크린샷은 1차에서 TAP 딥링크로 대체.
- 취소: [취소] → `POST /runs/{session_id}/cancel/`.
- 상태 매핑(포털 표기): queued/pending→대기, running→실행 중(진행바), completed→PASS/FAIL
  (failed>0 이면 FAIL), stopped→ABORTED(장애 아님 — 기존 "ABORTED=장애 아님" 정책 승계), error→FAIL.

### 3.4 API 탭 Jenkins 잡 자동 싱크 — **구현 완료 (2026-07-28)**
- 신규 서비스 `sync_api_suites()`(`apps/suites/services_sync.py`) — 내부 Jenkins 의 **등록된
  Suite 들의 부모 폴더 + 설정 가능한 루트 폴더 목록**(env `JENKINS_API_SYNC_ROOTS`)에서
  이름에 `api` 가 포함된 폴더/잡을 탐색 → 미등록이면 `TestSuite` 자동 생성 →
  기존 `suite_sync` 의 upsert 로직을 `sync_suite_jobs()` 로 추출·재사용해 서브 잡(worker) 등록.
- 트리거: API 탭 상단 **[Jenkins 싱크]** 수동 버튼(admin, `POST /suites/api-sync/`) + **10분 주기
  자동**(매분 도는 `sweep_reliability` cron 안에서 `cache.add` 10분 쿨다운으로 주기 판정 —
  1분마다 Jenkins 전체 폴더 스캔은 부하 과다이므로 기존 cron 에 편승해 crontab 은 무변경).
- 삭제는 자동 반영하지 않음(잡이 사라져도 Suite 는 유지, 기존 정책과 동일 — 수동 삭제).
- 상세: `maintenance.md` §3 "API 탭 Jenkins 잡 자동 싱크".

### 3.5 범위 제외 (2차 후보)
- TAP Run 의 Slack 알림(기존 SUITE_RUN_* 이벤트와 별도 이벤트로 2차), 대시보드 지표 합산,
  미 장애 판정 큐 편입, api_v2 로 TAP Run 재노출, 영상/스크린샷 포털 내 재생.
- ~~Reliability 스케줄에 TAP Suite 연결~~ → **1차로 앞당겨 구현 완료** (§3.6).

### 3.6 Reliability 스케줄에 TAP Suite 연결 — **1차로 앞당김, 구현 완료 (2026-07-28)**
- 원래 §3.5 의 2차 후보였으나, TAP Suite 도 Jenkins Suite 와 동일하게 N분 간격 반복 실행
  대상이 되어야 한다는 요구로 1차 범위에 포함.
- `RobustnessSchedule.test_suite` 를 nullable 로 바꾸고 `tap_suite`(`suites.TapSuite` FK,
  nullable) 를 추가 — 대상은 둘 중 **정확히 하나** (`CheckConstraint
  robustness_schedule_exactly_one_target`, DB 레벨 가드).
- 세션 통계(성공/실패/실행수/진행중/성공률)는 Jenkins=잡 단위·TAP=케이스 단위로 자동
  분기(`RobustnessSchedule._stats()`)하되 화면 컬럼 의미는 동일하게 맞춰 같은 표에 섞여 보인다.
- sweep(폴링+interval 트리거)·중지·수동실행은 Jenkins 경로와 골격을 공유하며
  `_sweep_tap_schedule`/`_claim_trigger_turn`(공용화)/`_timeout_unfinished_tap_runs`/
  `_tap_aggregate` 로 TAP 전용 분기. interval 타임아웃 마감은 TapRun 을 `error` 로,
  사용자 중지는 `stopped`(Aborted, 장애 아님)로 구분해 Jenkins 의
  FAILURE/ABORTED 의미 구분을 그대로 승계.
- "Test Suite 연동 추가" 폼이 API/UI 2탭으로 확장(`test_suite_id`/`tap_suite_id` 중 택1),
  스케줄 표의 TAP 행은 [UI] 배지, 실행 이력은 TAP 이면 세션(TapRun) 단위로 전환.
- 상세: `maintenance.md` §10 "Reliability Test 에 UI Automation(TAP) Suite 스케줄을
  연결하고 싶다" + §6.5(프론트), README.md "Reliability Test (Robustness)" 문단.

## 4. 기존 기능 연관성 점검

| 기존 기능 | 영향 | 처리 |
|---|---|---|
| Suite Run 지표/미장애 큐/대시보드 | TAP Run 은 별도 모델 → **기존 집계에 미포함** | 1차 의도적 분리(PRD §3.5) |
| ABORTED=장애 아님 정책 | TAP `stopped` 를 ABORTED 로 매핑해 승계 | §3.3 |
| RolePermissionMiddleware | 실행/취소 POST 경로에 `/run`·`/cancel` 포함해 일반 role 허용 규칙과 정합 | URL 설계 시 준수 |
| 매분 cron (`sweep_reliability`) | TAP 싱크/폴링 단계 추가(기존: 스케줄 sweep + 단독 run 폴링) | crontab 무변경 |
| Jenkins 익명 read + GWT | API 잡 싱크는 read(익명) 경로만 사용 — 트리거 무관 | 기존 클라이언트 재사용 |
| API Key 평문 금지 | TAP 키도 동일 원칙(.env 만, 로그/코드/문서에 평문 없음) | 완료 |
| 권한 | 실행/취소 = `can_write`(기존 suite run 과 동일), 싱크 버튼 = admin | 뷰 가드 |

## 5. 구현 단계

1. **Phase 1** — 모델(TapSuite/TapRun) + TAP 클라이언트(`apps/integrations/tapilot.py`) + 1분 싱크 + 2탭 UI(목록·synced_at). ✅ 완료
2. **Phase 2** — 실행/취소/진행바(HTMX)/케이스별 결과 상세 + cron 폴링. ✅ 완료
3. **Phase 3** — API 탭 Jenkins `api` 잡 자동 싱크(수동 버튼 + 10분 주기) + **(1차로 앞당김) Reliability
   스케줄에 TAP Suite 연결**(§3.6 — `RobustnessSchedule.tap_suite`/`_sweep_tap_schedule` 등). ✅ 완료
4. **Phase 4** — 테스트(모킹 기반 회귀 전체) · README/maintenance 반영 · 실브라우저 검증. 진행 중 —
   Phase 1~3 각각 커밋마다 회귀 테스트 통과 확인은 완료, 문서 반영은 이 갱신으로 마무리.

각 Phase 종료마다 테스트 통과 후 커밋. TAP 호출은 전부 timeout + 예외 격리(포털 화면이 TAP 장애에 안 막히게).
