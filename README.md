# QA Quality Gate Portal

Jenkins · Argo CD · Test Suite 를 하나의 검수 파이프라인으로 묶어 운영하는 Django 기반 품질 게이트 포털.

접속 주소: <https://qw.jkintra.net/qg/> (게이트웨이, 아래 "Nginx `/qg` 경로로 접속") · 프로그램(API Key) 호출 전용: <https://qw.jkintra.net/qg/ci-api/>. 이전 운영 도메인 `https://qualitygate.jkintra.net`(AWS 원본)은 2026-10 서버 이관(IDC `wx-qms221`)으로 **종료**됐다(502).

> 시스템 전체 소개 / 구조도(다이어그램) 는 [`intro.md`](./intro.md) 를 참고.
> 유지보수 / 코드 변경 가이드는 [`maintenance.md`](./maintenance.md) 를 참고.

---

## 1. 무엇을 하는가

하나의 **Quality Gate Run** 은 다음 4단계를 순차로 진행한다.

```
(1) 이미지 빌드        → image_job (선택)
(2) 검수대상 배포      → deploy_job + Argo CD sync (선택)
(3) Test Suite 순차    → 각 Suite 의 Idle Job 1개 트리거
(4) 검수통과 배포      → prod_job + Argo CD sync (선택)
```

- **이미지 / 검수통과 배포 / Argo CD** 는 게이트별로 켜고 끌 수 있다.
- 게이트는 두 가지 타입 — **Dev → Stg** (개발 인스턴스에서 검증 후 스테이징 배포), **Stg → Prd** (스테이징에서 검증 후 운영 배포). 게이트 타입에 따라 사용할 Jenkins 자격증명과 Argo CD 환경(dev/stg/prd) 이 자동으로 결정된다.
- 새 Run 을 시작하면 같은 게이트의 사용자 입력 대기 Run 은 자동 `CANCELLED`. 이미 진행 중인 Run 이 있으면 새 Run 자체가 차단된다.

### 자동 진행 모드 (`auto_mode`)

**Dev → Stg 게이트 한정** — 토글을 켜면 중간 사용자 입력 단계 (`WAITING_DEPLOY` / `QG_PASSED`) 를 건너뛰고 **각 Job 의 기본 파라미터로 다음 단계를 자동 트리거**. 이미지 빌드 → 배포 → 테스트 → 스테이징 배포까지 한 번에 흐른다.

자동 모드 진입 시 **Git Parameter** (Jenkins type 에 `git` 포함) 의 default 가 운영 정기 배포 흐름과 어긋나는 master 로 잡히는 문제를 막기 위해, choices 에 `release/release_MMDD` 형태의 release branch 가 보이면 그 값을 우선 default 로 사용한다 (`origin/release/release_0501`, `refs/remotes/origin/release/release_0501` 같은 prefix variants 도 인식). 매칭되는 release branch 가 여러 개면 사전순 가장 큰 (= 가장 미래 일자) 항목을 선택. 매칭이 없으면 기존 규칙대로 default → 첫 choice 순 fallback.

String 타입의 git ref 파라미터(예: `DEPLOYER_REFS=refs/heads/master`) 는 의도적으로 손대지 않는다. 이런 파라미터는 보통 Pipeline-from-SCM 의 **Jenkinsfile 저장소 ref** 를 가리키며, Jenkinsfile 저장소엔 release branch 가 없는 경우가 많아 자동 치환 시 `fatal: couldn't find remote ref ...` 로 실패한다. Jenkinsfile 자체는 master 에서 가져오고, 그 안의 source code 체크아웃이 `BRANCH_NAME` 파라미터(=release) 를 받도록 두는 게 안전하다.

게이트 상세 화면 토글로만 변경할 수 있고, 활동 로그에 `auto_mode=on|off` 로 남는다.

### 테스트 병렬 실행 (`parallel_test`)

게이트 타입과 무관하게 토글 가능. 자동 토글 옆 **병렬 ON/OFF** 버튼. **기본 Off**.

- **Off (순차)**: Suite 를 등록 순서대로 하나씩 실행. 한 Suite 가 PASS 해야 다음 Suite 로 넘어가고, 어느 하나라도 FAIL 하면 즉시 `FAILED` 로 중단.
- **On (병렬)**: 검증 배포 완료 직후 등록된 모든 Test Suite 를 동시에 트리거. **모든 Suite 가 PASS/FAIL 로 끝날 때까지 다음 단계로 가지 않는다.** 마지막 Suite 까지 종료된 뒤 — 하나라도 실패면 `FAILED`, 전부 통과면 `QG_PASSED` (또는 prod 배포 단계로). 활동 로그에 `parallel_test=on|off` 로 기록.

### 테스트만 재실행 (`rerun_test`)

테스트 단계에서 실패한 Run 은 **이미지/배포는 그대로 유지한 채 테스트만** 다시 돌릴 수 있다. 이미 PASS 한 Suite 는 유지되고, 실패한 Suite 부터 PENDING 으로 리셋 → `WAITING_TEST` → 자동 재진행. 이미지나 배포부터 다시 돌려야 한다면 일반 **재실행** (= 처음부터) 을 사용.

### 검수대상 배포만 재실행

검수대상 환경 배포 단계 (`DEPLOY_FAILED`) 에서 실패한 Run 은 **이미지 빌드는 그대로 재사용한 채 deploy Job 만** 다시 트리거할 수 있다. 모달에서 파라미터를 다시 선택하면 이전 시도의 빌드/Argo CD 잔여 정보는 정리되고 폴링이 새 빌드 결과로 채운다. 이미지부터 다시 돌리고 싶다면 같은 카드의 **처음부터 재실행** 을 사용.

### 검수통과 배포만 재실행

검수통과 배포 단계에서 실패한 Run 은 prod 배포만 다시 트리거할 수 있다. 이미지/검증배포/테스트 결과는 그대로 보존.

---

## 2. 핵심 화면

| 화면 | 경로 | 역할 |
|---|---|---|
| 로그인 (Keycloak 전용) | `/accounts/login/` | **Keycloak(사내 계정) 전용 로그인 페이지.** 처음 접속하면(미로그인) 이 화면이 열리고, 큰 **SSO 버튼**(문구 "사내 계정으로 로그인 (SSO)") 하나가 중심이다. SSO 가 활성이고 자체 로그인이 닫혀 있으면 아이디/비밀번호 폼·회원가입 링크 없이 버튼 + 안내 문구만 보인다. **로그아웃하면 앱 세션과 Keycloak 세션을 모두 끝내고 이 화면으로 돌아오며**(상단에 "로그아웃되었습니다." 알림, `?loggedout=1`), 다시 버튼을 누르면 Keycloak 비밀번호를 요구한다(공용 PC 대응 — 아래 §4 "SSO 로그인"·§10 참고). 로그인 실패 시 이 화면에 한국어 오류 안내**게이트웨이 진입 모드**(`GATEWAY_ENTRY_ENABLED=True`)에서는 SSO 버튼 대신 **"Keycloak 로그인" 버튼**(`/accounts/gateway-login/` 로 이동)이 이 화면의 중심이 되고, 로그아웃하면 게이트웨이 세션을 끊은 뒤 이 화면으로 돌아온다(§4 "SSO 로그인"). |
| 대시보드 | `/` | Suite 기준 요약 카드(14일: Pass Rate·Runs·Failures·Running·Today) + **Chart.js 차트 2행** (1행: 일별 stacked bar 14일·Suite 별 Pass Rate horizontal bar 14일 / 2행: 도넛 2개 — 서비스 품질 지표(Pass/Failure)·퀄리티게이트 품질 지표(Pass/Skip), 가운데 Pass Rate % — 소수점 3자리 표기) + **미 장애 판정 분류건 카드** (미분류 0건=초록, 1건 이상=판정 화면 링크) + 최근 **TestSuiteRun 15건 표**. **모든 차트 요소가 클릭 드릴다운** — 일별 막대(Pass/Fail)→Run 목록(날짜·결과 필터), Suite 막대→Run 목록(suite 필터), 도넛 슬라이스→Job 결과 목록(`/suites/job-runs/?result=`). **ABORTED(중단) Job 은 장애가 아니므로 두 도넛 어디에도 집계되지 않는다**(스케줄 정지·수동 취소·Jenkins 취소 전부 제외). 단 사람이 Slack/포털 버튼으로 verdict 를 명시 확정한 ABORTED 는 그 판정을 따른다(Failure→장애 도넛, Skip→Skip 도넛) |
| Visualization | `/visualization/` | **QPE Admin 전용 (2026-10-21)** — 사이드바 메뉴도 admin 에게만 보이고, ops 등 다른 역할은 페이지 접근 시 대시보드로 리다이렉트·`/visualization/data/` 는 403, Viewer 는 Viewer 제한으로 차단. **"Quality Highway / Live Race Control"** — 최근 **Test Suite 실행**(Quality Gate Run 제외)을 상용 레이싱 게임풍 Canvas 서킷과 Race Control HUD로 표현. PASS=완주, RUNNING=라이브 점멸, FAIL=사고·리커버리, SKIP=사고 없는 피트 이탈. 상태별 고유 Run 통계와 최근 전송 피드를 제공하며 차량을 클릭·탭하거나 키보드로 선택하면 Run 상세로 이동. 6초마다 최신 진행·판정 상태를 동기화 |
| Quality Gates | `/qualitygates/` | 인스턴스(폴더) 목록 |
| 인스턴스 상세 | `/qualitygates/folders/<pk>/` | 하위폴더 카드 목록 + Jenkins/Argo CD 자격증명 관리 진입 |
| 하위폴더 상세 | `/qualitygates/subfolders/<pk>/` | 그 하위폴더에 속한 Quality Gate 목록 |
| 게이트 상세 | `/qualitygates/<pk>/` | Run 시작, auto_mode 토글, 최근 Run 15건 |
| Run 상세 | `/qualitygates/runs/<pk>/` | 5초 폴링되는 단계별 진행 + 콘솔 로그 + Argo CD 상태 |
| Test Suite | `/suites/` | **2탭 구성 (2026-07-28)**: **API Test Automation**(기본 탭, 기존 Jenkins 폴더 단위 테스트 묶음 — 편집 화면(`/suites/<pk>/edit/`)에서 재시도 횟수(`retry_count`) 및 **잡별 실행 지연 시간(`trigger_delay_seconds`)** 설정 가능. 상단 **[Jenkins 싱크]** 버튼(admin 전용, `/suites/api-sync/` POST)으로 이름에 `api` 가 포함된 내부 Jenkins 폴더/잡을 자동 탐색·등록 — 10분 주기 자동(cron)도 병행) / **UI Test Automation**(`?tab=ui` — TAP(tapilot) Playwright 자동화 연동. 매분 cron 이 TAP Suite 목록을 미러링(`TapSuite`)해 케이스 수·담당자·마지막 실행 결과·진행 중이면 **케이스 수 기준 진행바**를 보여주는 표. 진행 중인 실행이 있을 때만 10초 HTMX 폴링, 없으면 1회 로드 후 정지). [Run](`can_write`, confirm) 또는 진행 중이면 [Cancel], [Edit](admin — **재시도 횟수** 설정, `/suites/tap/<pk>/edit/`), [Detail], TAP 딥링크 아이콘. **재시도(2026-07-30)**: API Suite 와 동일하게 완료 결과에 실패 케이스가 있으면 `retry_count` 회까지 전체 자동 재실행 — 중간 시도 실패는 Slack 미발송(ActivityLog RETRY 기록), 통계/알림은 최종 시도만 반영. error(타임아웃)/Aborted(취소)는 재시도 안 함. 하단에 마지막 싱크 시각 표시 |
| UI Test Automation Run 상세 | `/suites/tap-runs/<uuid:pk>/` | TAP Run 1건의 진행 상태(케이스 수 기준 진행바) + passed/failed/others + TestRail·TAP 딥링크 + 제외 케이스 수(자동화 스크립트 없어 자동 제외) + 케이스별 결과 표(제목/섹션/상태/소요시간/TestRail 링크). 실패·에러 케이스는 콘솔 발췌(`raw_output_tail`)를 접이식(`<details>`)으로 노출. 진행 중이면 10초마다 자기 자신을 다시 불러와(HTMX `hx-select`) 갱신, 종결되면 자동으로 폴링 중지 |
| Suite Runs (Run 전체 이력) | `/suites/runs/` | 모든 Suite 의 단독 Run(API/UI/스케줄)을 한 곳에서 — 대시보드는 최근 15건 요약, 이 화면에서 필터(suite/status/source/**result**=PASS·FAIL·SKIP)로 전체 이력 조회. result 필터: `pass` → `completed & failed_jobs=0`, `fail` → `completed & real_failed>0`(Skip 분류·**ABORTED(중단)** 제외한 실장애 기준), `skip` → `completed & Skip 분류 있음 & real_failed=0`. 행 배지는 PASS/FAIL/SKIP 외에 **실패 Job 이 전부 ABORTED 인 run 은 별도 ABORTED 배지**(장애로 집계하지 않음). suite 별 순증가 일련번호 첫 컬럼 — 여러 suite 가 섞여 `#N`(suite별 1..N)이 중복돼 보이므로 **suite 구분자(`TestSuite.short_label`)를 앞에 붙여 `am-dweb-prd · #5`** 형태로 표시. 사이드바 **"Suite Runs"** (fa-list-ul) 메뉴 진입 |
| 미 장애 판정(triage) 큐 | `/suites/runs/untriaged/` | 컷오프(`settings.UNTRIAGED_QUEUE_SINCE`, 기본 2026-07-08T05:58:00+00:00) 이후 발생한 **실패(FAILURE)** Job Run 중 verdict 미확정(`verdict=""`) 목록 — **ABORTED(중단)는 장애가 아니므로 큐에서 제외**(스케줄 정지·수동 취소로 중단된 Job 이 판정 대기로 쌓이지 않음). 최신 200건. 행마다 [🔴 장애][⏭️ Skip] 버튼(POST, `can_write` 필요) — 판정하면 큐에서 사라짐. 판정 시 Slack 원본 메시지 갱신 + 담당자 F/U 멘션(slack_message_ts 있을 때). **체크박스로 여러 건을 골라 일괄 판정도 가능**(`can_write` 한정) — 행별 체크박스 + 헤더 전체선택, 상단 액션 바 [🔴 선택 일괄 장애][⏭️ 선택 일괄 Skip](선택 0건이면 비활성, 클릭 시 선택 건수 confirm). **Suite 필터(복수 선택, 즉시 적용)** — 대기 건이 있는 Suite 만 건수와 함께 체크박스 칩으로 노출(`?suite=<uuid>` 반복), 칩을 누르면 적용 버튼 없이 바로 그 Suite 들의 대기분만 표시되고 "전체 보기"로 해제. 단건/일괄 판정 후에도 필터 유지. |
| Job 결과 목록 (도넛 드릴다운) | `/suites/job-runs/` | 대시보드 결과 분포 도넛의 드릴다운 — 최근 14일 완료 Suite Run 의 **Job 단위** 결과(최신 200건, 읽기 전용). 필터 `result`=success/failure(장애=Skip 아닌 실패, 미분류 포함)/skip + `suite`. **미분류 ABORTED(중단) Job 은 도넛과 동일하게 어느 필터에도 잡히지 않고**(배지도 FAIL 이 아닌 ABORTED 로 표시), verdict 명시 확정된 ABORTED 는 그 판정(failure/skip)을 따라 잡힌다. |
| Test Suite — Jenkins 자격증명 | `/suites/credentials/` | (admin) 내부 Jenkins · 외부 Jenkins default 자격증명 관리. API token 평문은 비노출 |
| Queue | `/suites/queue/` | 진행 중 빌드 모음 |
| Reliability Tests | `/robustness/` | 스케줄 기반 반복 실행. 상세 화면(`/robustness/<pk>/`)의 "Test Suite 연동 추가" 폼은 **2탭**(API Test Automation=기존 Jenkins Suite / UI Automation=TAP Suite, 케이스 수 병기, JS 토글로 페이지 리로드 없이 전환, interval 입력은 공용) — TAP Suite 를 골라 스케줄로 등록하면 UI Automation Suite 도 동일한 간격 반복 실행 대상이 된다(PRD `docs/prd_ui_test_automation.md`). 스케줄 표의 TAP 행은 **[UI] 배지**로 구분되고 실행 수 단위가 "잡"→"케이스"로 바뀌며, "마지막 실행" 은 TAP 이면 UI Automation Run 상세(`/suites/tap-runs/<pk>/`)로 링크. 실행 이력(`/robustness/schedules/<pk>/runs/`)도 TAP 스케줄이면 세션(TapRun) 단위 이력 — 세션 ID/상태/케이스 진행/Pass·Fail/시작·종료/상세 링크 |
| Activity Log | `/logs/` | 등록/실행/취소/재시도 로그 |
| Admin — 사용자 관리 | `/accounts/users/` | 가입 승인 + 권한 부여. **사용자 직접 추가** (`/accounts/users/new/`) — 관리자가 승인 절차 없이 즉시 활성 계정을 생성하며 역할(role)도 이 자리에서 지정 |
| Admin — Slack 알림 | `/accounts/notifications/` | 이벤트별 토글 + 채널 설정 + 테스트 메시지 |
| Admin — 포털 설정 | `/accounts/portal-settings/` | 좌측 사이드바 메뉴 표시 토글 (현재 **Quality Gate** 메뉴 on/off — 끄면 사이드바에서 숨김, URL 직접 접근은 가능). `PortalSettings` singleton + context processor 로 전 페이지 반영 |
| API Keys (본인) | `/accounts/api-keys/` | 외부 호출용 API 키 발급/회수 — 평문은 발급 직후 1회만 노출. **사이드바 메뉴에서는 제거됨 (2026-05-27)** — URL 로만 접근, 메뉴상 API Keys 진입은 Admin 탭에만 노출 |
| API Keys (admin) | `/accounts/api-keys/admin/` | 전체 사용자 키 조회/회수 + **'새 키 발급' 버튼(본인=관리자 키 발급)**. **메뉴상 API Keys 의 유일한 진입점 (Admin → API Keys)** |
| Admin — 담당자 관리 | `/accounts/api-owners/` | **내부 탭 2개 (2026-10-02)** — **API Endpoint 담당자**(기본) / **UI 담당자**(`?tab=ui` — UI Test Automation(TAP) 실패 Slack 알림에 멘션할 담당자를 Suite/케이스 구분 없이 콤마 다중값으로 통 설정, `UiAutomationOwners`). 아래는 API 탭 내용: API 엔드포인트별 담당자 마스터. **Google Sheets 동기화 (full mirror — 시트가 단일 진실 소스)** / 수동 추가/수정/삭제 / 필터 검색 / CSV 다운로드. 동기화는 **`(Method, Endpoint)` 키로 `update_or_create`** — 같은 endpoint 가 다른 repo 라벨로 들어와도 기존 row 를 덮어쓰기만 하고 새 row 는 만들지 않는다 (2026-05-20). 시트에 없는 imported row 는 삭제, 수동 row(`is_imported=False`) 는 보존. **자동 동기화 주기(분)는 화면 하단에서 설정** (`0`=끔) — 매분 도는 cron `sync_api_owners_from_sheet --if-due` 이 그 주기에 맞춰 실행하고, 화면의 **'Google Sheets 동기화'** 버튼은 수동 즉시 동기화 (CSV 업로드 대체, 2026-05-27). |
| Admin — 호스트 매핑 | `/accounts/api-hosts/` | `${BFF_URL}` 같은 환경변수를 실제 도메인(`bff-general-*.dev.albamon.com`) 과 연결. 매칭의 필수 조건이 아니라 같은 path 가 AM/JK 양쪽에 등록된 경우 disambiguation 용. |
| API 문서 (Swagger / ReDoc) | `/api/v2/docs/` · `/api/v2/redoc/` · `/api/v2/schema/` | `/api/v2/` REST API 의 OpenAPI 문서(drf-spectacular). **포털 세션 로그인 필요** — 로그인 안 된 브라우저는 로그인 화면으로 이동. Swagger UI 우상단 **Authorize** 버튼에 발급받은 API Key(`Bearer qgp_...`)를 입력하면 화면에서 바로 Try-it-out 호출 가능 — **키 입력 즉시 서버 실검증**(**`/ci-api/me/`** 호출, 세션 쿠키 제외 — `/api/v2/` 는 게이트웨이가 쿠키 없는 요청을 HTML 401 로 막기 때문)해 우상단에 ✅ 유효(키 이름·scope 표시)/❌ 무효 배지를 띄운다(`templates/api_v2/swagger_ui.html` 커스텀). Authorize 대상은 **API Key 하나만 노출**(세션 쿠키/Basic 은 문서에 미표기 — `AUTHENTICATION_WHITELIST`, 실제 세션 병행 인증 동작은 불변). 문서 JS/CSS 에셋은 외부 CDN 대신 **sidecar 로 로컬 서빙**(인트라넷 환경). 엔드포인트별 상세 시나리오·에러 카탈로그는 [`docs/api_guide.md`](./docs/api_guide.md) 참고. **Swagger/ReDoc 에 표시되는 엔드포인트 경로·설명·예시는 모두 `/ci-api/` 기준**이고 Try it out 도 `/qg/ci-api/…` 로 호출된다(`apps/api_v2/schema.py:rewrite_paths_to_ci_api` 가 스키마 `paths` 의 `/api/v2/` 를 치환 — 문서 표기만 바뀌고 실제 라우팅·뷰는 불변, 문서 페이지 자체와 DB export 는 계속 브라우저용 `/api/v2/` 에만 존재). **게이트웨이 뒤(`qw.jkintra.net/qg`)에서 프로그램이 호출할 때는 `/ci-api/` 주소 사용**(문서 페이지가 아니라 API 호출 주소 — `https://qw.jkintra.net/qg/ci-api/…`, 인증은 API Key 만: `Authorization: Bearer qgp_...` 또는 Basic `curl -u x:$KEY`, 2026-10-12). |

---

## 3. 도메인 모델 요약

| 모델 | 설명 |
|---|---|
| `QualityGateFolder` | **인스턴스** 단위. Jenkins **Dev/Prod 자격증명** 2쌍 + Argo CD **Dev/Stg/Prd 토큰** 3개를 보관. 같은 인스턴스 안의 게이트가 자격증명을 공유. |
| `QualityGateSubFolder` | 인스턴스 안에서 게이트들을 묶는 **하위폴더**. `folder` (인스턴스) + `name` (인스턴스 안에서 unique). 기존 게이트는 마이그레이션 시 자동 생성된 `auth` 하위폴더에 배치. |
| `QualityGate` | 개별 게이트. `subfolder` (하위폴더) + `folder` (인스턴스), `gate_type`(`dev_stg`/`stg_prd`), `auto_mode`, image/deploy/prod 각 Job 경로, Argo CD App URL, `deploy_jenkins_base_url` (입력 우선) 등. |
| `QualityGateRun` | 단일 실행 인스턴스. 단계별 Jenkins build 정보 + Argo CD sync 정보(7개 필드 세트) 보유. `is_running`, `can_rerun_test_only` 등 다수 프로퍼티. |
| `QualityGateRunTest` | Run 안에서 Suite 별 진행 상태(PENDING/WAITING/TESTING/PASSED/FAILED). |
| `TestSuite` / `TestSuiteJob` | Jenkins 폴더 단위 테스트 묶음. `jenkins_base_url` 채우면 외부 Jenkins 도 지원. `TestSuiteJob.trigger_delay_seconds` (기본 30초) — 잡별 실행 지연시간. `TestSuiteJob.gwt_token` — 내부 Jenkins GWT(Generic Webhook Trigger) 트리거용 워커별 고유 토큰. Suite 상세 화면(admin 전용)의 "GWT 토큰" 카드에서 발급/재발급. |
| `TestSuiteRun` | Suite 단독 실행 인스턴스. suite 별 1부터 순증가하는 `seq_no` (표시용 일련번호) 를 가진다. Run 목록/상세/API 응답에 `#N` 으로 표시. |
| `TestSuite.last_job_triggered_at` | **suite 단위 throttle** 필드 (DateTimeField, null=True). 잡 트리거마다 해당 suite row 를 원자적 조건부 UPDATE 로 갱신해 Jenkins API 동시 충돌을 방지. 서로 다른 suite 는 독립 슬롯이므로 간섭 없음. |
| `RobustnessTest` / `RobustnessSchedule` / `RobustnessRun` | 일정 간격으로 Suite 를 반복 실행하는 스케줄러 (Reliability Test). |
| `ActivityLog` | 주요 행위 추적 (생성/수정/삭제/실행/취소/재시도/로그인). |

---

## 4. 주요 기능

- **SSO 로그인 (Keycloak OIDC, 공급자 `internal`)** — `/accounts/login/` 이 **Keycloak 전용 로그인 페이지**다. `OIDC_INTERNAL_ISSUER`/`CLIENT_ID`/`CLIENT_SECRET` 셋이 모두 있으면 SSO 가 켜지고 **자체 아이디/비밀번호 로그인·회원가입(`/accounts/signup/` 404)이 닫힌다**(`LOCAL_LOGIN_ENABLED=True` 로 병행 가능, OIDC 값이 없으면 자체 로그인 유지 — 코드 먼저·OIDC 값은 나중 순서로 안전하게 배포). 흐름: 버튼 → Keycloak → `/accounts/oidc/internal/callback/` 에서 코드 교환·ID 토큰 검증 → 포털 세션 로그인(Authlib, PKCE S256). **계정 연결 규칙**: 사람은 `(issuer, sub)` 로만 식별(`ExternalIdentity`)하고 이메일은 **최초 연결에만** 쓴다. 이메일이 정확히 1명의 기존 계정과 일치하면 그 계정에 연결(역할·비밀번호 유지), 일치하는 계정이 없으면 **즉시 활성 + `viewer` 권한으로 자동 생성**(역할은 관리자가 사용자 관리에서 승격). 이메일이 `OIDC_INTERNAL_EMAIL_DOMAINS`(기본 `worxphere.ai`) 도메인이거나 `email_verified=true` 일 때만 신뢰 — 아니면 계정 생성/연결 없이 거부. 이메일 없음·2명 이상과 일치(모호)·비활성 계정·**같은 issuer 의 다른 SSO 연결이 이미 있는 계정**은 연결하지 않고 거부(중복 계정도 만들지 않음). **로그아웃**: ① 게이트웨이(qw) 뒤에서 `GATEWAY_LOGOUT_URL` 이 있으면 게이트웨이 로그아웃으로 이동(게이트웨이가 Keycloak 세션까지 종료) ② 아니고 `OIDC_INTERNAL_POST_LOGOUT_REDIRECT=True` 면 **Keycloak end-session** 으로 보내 Keycloak 세션까지 끝내고 로그인 페이지로 복귀 ③ 그 외에는 앱 세션만 끝내고 `/accounts/login/?loggedout=1` (이 경우 Keycloak 세션이 남아 버튼이 비밀번호 없이 바로 재로그인될 수 있다). ①의 게이트웨이 이동은 302 가 아니라 **HTML 페이지(meta refresh + JS + 링크)** 로 한다 — 게이트웨이가 `/qg/` 하위 응답의 `Location` 을 고쳐 써 `/qg/_qw/logout` 404 가 난 적이 있어(2026-10-09) `Location` 헤더 자체를 쓰지 않는다. 게이트웨이가 Keycloak 세션까지 끊는지는 게이트웨이의 `SSO_*_ISSUER` 설정에 달려 있어 **미확인**. **게이트웨이 진입 로그인(`GATEWAY_ENTRY_ENABLED`, 기본 `False`)**: 앱 자체 로그인 화면을 첫 화면으로 두고 거기 **"Keycloak 로그인" 버튼**으로 로그인하는 방식. 게이트웨이는 `/qg/` 를 비보호로 두고 **진입 경로 `/qg/accounts/gateway-login/` 하나만 internal 로 보호**해야 한다(게이트웨이 라우팅은 QA 인프라팀 소유 — 요청 문안은 `docs/handoff_sso_integration.md` §10). 버튼 → 게이트웨이 Keycloak 로그인 → 게이트웨이가 붙인 `X-Auth-*` 헤더로 앱 세션 생성 → 대시보드. 헤더가 없으면(라우팅 미설정·신뢰 밖 출발지) 503 `sso_unavailable` 로그인 화면을 직접 렌더한다(루프 없음). 켜면 자체 아이디/비밀번호 로그인은 기본 꺼지고(`LOCAL_LOGIN_ENABLED=True` + 재시작으로 비상 복구), **게이트웨이 라우팅이 적용·검증되기 전에는 켜지 않는다**(안 켜진 라우팅에서 켜면 로그인 잠금). 로그아웃은 게이트웨이 로그아웃을 백그라운드로 호출해 세션 쿠키를 지운 뒤 로그인 화면(`?loggedout=1`)으로 돌아온다. **게이트웨이 헤더 로그인**: 게이트웨이(`qw.jkintra.net`, oauth2-proxy)를 통과한 world realm 요청은 `X-Auth-Request-Email` 로 바로 로그인된다(Keycloak client 불필요, 없으면 viewer 로 생성). 앱 직접 OIDC 가 켜져 있으면 로그인 화면 없이 SSO 를 바로 시작하기도 하며, 이 헤더(`X-Auth-*`)는 `TRUSTED_PROXY_CIDRS` 출발지 것만 신뢰한다. 세션이 끝난 뒤 게이트웨이가 돌려주는 401 은 화면이 자동 새로고침해 다시 로그인으로 보낸다(`qg_notify.js`). `/admin/login/` 은 자체 로그인이 닫혀 있으면 앱 로그인 화면으로 이동. 설계·운영 절차는 `maintenance.md` §7 "SSO 로그인", 게이트웨이 구조는 `docs/qw-sso-gateway.md`.
- **서버 이관 / 전체 데이터 싱크 (`apps/syncops`, 2026-10-09)** — 이 저장소를 다른 서버(IDC 게이트웨이)로 통째로 옮길 때 DB 전 테이블(5GB+) + 비밀/설정(`.env`·`secrets/`·`deploy/`·crontab·systemd 유닛) + 마이그레이션 상태를 **검증 가능한 스냅샷 번들**(`snapshots/qg-<UTC>/`: `manifest.json` + `db.sqlite3.zst` + `config.tar[.enc]`)로 옮기는 관리 명령 4종 — `export_snapshot`(원본: sqlite3 backup API 로 일관 스냅샷, `--dry-run` 은 운영에서 안전) / `import_snapshot`(새 서버: sha256·`PRAGMA quick_check`·테이블 행 수 검증 후 `os.replace` 원자 교체, 기존 DB 는 하드링크로 `.pre-import-<ts>` 보존, 같은 호스트 가드 `--allow-same-host`, 코드가 오래된 번들은 `--allow-older-code` 로만 허용·`--force` 는 덮어쓰기 전용) / `verify_snapshot`(행 수·마이그레이션 비교) / `sync_from_source`(새 서버에서 ssh→rsync→import→verify 를 한 번에 하는 pull 오케스트레이터). ssh 가 막혀 있으면 번들 디렉터리를 아무 수단으로 옮겨 `import_snapshot` 만 쓰면 된다. **HTTPS 만 열린 환경(경로 C)** 은 원본의 **DB 스냅샷 내보내기 API** `/api/v2/export/snapshots/`(스코프 `export:read`) + 새 서버 명령 `fetch_snapshot_via_api`(키는 `--key-env` 환경변수로만, `.part` Range 이어받기·sha256 검증·번들 배치·원격 DELETE) — API 는 **admin 소유 키 + `export:read` 명시 스코프**(`*` 는 포함하지 않음)만 허용, **config(`.env`·`secrets/`·crontab)는 내보내지 않고**(별도 전달), 번들은 `snapshots/api-<id>/` 에 만들어져 24시간 뒤 자동 만료·동시 1개·상태는 번들 안 `state.json`(DB 모델/마이그레이션 없음). 원본에 이 코드가 배포돼야 쓸 수 있다. 엔드포인트·오류 코드: [`docs/api_guide.md`](./docs/api_guide.md) §4.5, 요구사항·구현 현황: [`docs/api_requirements_data_export.md`](./docs/api_requirements_data_export.md). 절차·AI 런북: [`docs/prd_server_migration_sync.md`](./docs/prd_server_migration_sync.md) (새 서버에서 AI 에게 "docs/prd_server_migration_sync.md 보고 싱크해줘"), SSO 이어하기: [`docs/handoff_sso_integration.md`](./docs/handoff_sso_integration.md). 번들에는 비밀이 들어 있고 **번들 DB 는 암호화되지 않으므로**(세션·해시 포함) 외부 매체 전송 시 별도 암호화가 필요하다. `snapshots/`·`*.incoming`·`*.bak-*`·`db.sqlite3.pre-import-*` 등은 `.gitignore` 대상이다.
- **반응형 (모바일/태블릿)** — 폭에 따라 자동 적응. **데스크톱(>1024px)** 은 240px 고정 사이드바, **태블릿(≤1024px)** 은 사이드바 200px 로 축소, **모바일(≤768px)** 은 사이드바가 **off-canvas 드로어**로 전환되고 상단에 햄버거 바가 노출된다(햄버거/오버레이/ESC/메뉴 클릭/데스크톱 폭 복귀 시 자동 닫힘 — `QGNav`). 좁은 화면에서 페이지 헤더는 세로로 쌓이고, 액션 버튼 툴바는 줄바꿈, 탭 스트립(`.admin-tabs`/`.event-tabs`)·넓은 테이블은 가로 스크롤, 상세 그리드는 1열, QG 파이프라인 3섹션은 세로로 쌓인다. 초소형(≤480px)에서 폰트/패딩을 한 단계 더 축소. (구현: `static/css/style.css` 의 `@media` 블록 + `templates/base.html` 의 `.mobile-topbar`·`.sidebar-overlay`·`QGNav`)
- **Visualization (Quality Highway / Live Race Control)** — 좌측 사이드바 **'Visualization'** (fa-road) 메뉴. 생성형 메가시티 WebP와 절차적 폴백 위에 최근 Test Suite Run을 레이싱 머신으로 렌더링하고, Race Control HUD에서 PASS/RUNNING/FAIL/SKIP(PIT)별 고유 Run 수·전체 수·최근 4건을 확인한다. PASS는 완주, RUNNING은 점멸 주행, FAIL은 사고 후 견인 리커버리, 실패 Job이 모두 Skip 판정된 Run은 사고 없이 피트로 이탈한다. 차량의 툴팁과 클릭·탭, 또는 Canvas 포커스 후 방향키 + Enter/Space로 Run 상세를 열 수 있다. 로딩·빈 데이터·연결 끊김·stale 링크 상태를 구분하고, 6초 폴링의 10초 replay와 오래된 탭 재시드로 burst·경계 이벤트를 놓치지 않으면서 RUNNING 진행률과 사후 triage 변경까지 반영한다. 일시정지, OS 모션 축소, 모바일 3차선, 라이트/다크 테마를 지원한다. (구현: `apps/visualization` + `templates/visualization/index.html` + `static/css/qg_highway.css` + `static/js/qg_highway.js`)
- **다크 / 라이트 모드** — 좌측 사이드바 **'알림' 버튼 바로 아래 토글** 로 전환. CSS 변수(`[data-theme="dark"]`) 기반이라 **전 페이지** 에 일관 적용, `localStorage`(`qg-theme`) 저장, 첫 페인트 전 `<head>` 스크립트로 적용해 **깜빡임 없음**. 라이트 = 대시보드 톤(인디고/시안 강조 + 소프트 배경), 다크 = 짙은 슬레이트 + 인디고/시안 네온 강조(첨단). 색은 semantic 변수(`--surface`/`--text`/`--border`/`--glass-bg`/`--warn-bg` 등)로 관리하며 다크에선 gray scale + semantic 을 함께 override. (구현: `static/css/style.css` 의 `:root`·`[data-theme=dark]`, 토글 `templates/base.html` 의 `QGTheme`)
- **4단계 파이프라인** — 이미지 → 검수대상 배포 → 테스트 → 검수통과 배포. 각 단계 선택 가능. 테스트 통과 후 `QG_PASSED` 는 운영 배포 **승인 대기** 단계이며, "검수통과 배포" 버튼이 곧 승인+실행이다.
- **`auto_mode` 자동 진행** — Dev→Stg 게이트에서 중간 대기 없이 기본 파라미터로 끝까지 진행.
- **`parallel_test` 테스트 병렬 실행** — 모든 Test Suite 를 동시에 트리거하고, 마지막 Suite 까지 끝나야 다음 단계로 진행. 기본 Off.
- **`rerun_test` / 검수대상 재실행 / 검수통과 재실행** — 실패 단계만 재시도. 이미지/검증배포/테스트 결과는 가능한 한 그대로 재사용.
- **재시도 횟수 (`retry_count`)** — Test Suite 편집 화면(`/suites/<pk>/edit/`)에서 잡(worker)의 최대 실행 횟수를 설정. 기본 1(= 재시도 없음). 3 이면 순수 FAILURE 가 3회 모두 발생해야 Suite Run 실패로 확정되고, 중간 시도 실패는 Slack 미발송(ActivityLog `RETRY` 기록). QG 파이프라인 테스트 단계·단독 Suite Run·Reliability Test 전 경로에 동일 적용. **Run 시작 시점의 값을 스냅샷**해 진행 중 설정 변경이 in-flight Run 에 영향 없음 — 단독/Reliability 경로는 `TestSuiteRun.retry_limit`, QG 파이프라인 경로는 `QualityGateRunTest.retry_limit` 에 각각 고정.
- **잡 실행 지연 (`trigger_delay_seconds`)** — Test Suite 편집 화면(`/suites/<pk>/edit/`)에서 잡(worker)별로 실행 지연 시간(초)을 설정. 기본 30초, 0 이면 즉시 트리거. 잡이 트리거될 때마다 **각 suite 의 `TestSuite.last_job_triggered_at`(suite 단위 throttle)** 과 비교해 지연시간이 지났을 때만 Jenkins API 를 호출하고, 그 전이면 해당 잡을 **"지연 대기(WAITING)"** 상태로 보류한 뒤 다음 폴링에서 재시도 — 동시 Jenkins API 충돌 방지. Queue 화면과 Run 상세에 "지연 대기" 배지로 표시. Suite 상세(`/suites/<pk>/`) Job 목록 표의 "실행 지연" 컬럼에서도 잡별 지연값 확인 가능(admin 은 컬럼 헤더 연필 아이콘으로 Edit 진입). 단독 Suite Run, Reliability Test (순차·병렬 모두) 에 적용. 병렬 모드에서는 잡들이 delay 간격으로 엇갈려 시작되어 **병렬로 함께 실행**된다(앞 잡 완료를 기다리지 않음). 서로 다른 suite/스케줄은 각자의 슬롯을 가지므로 starvation 없음. QG 파이프라인 테스트 단계는 현재 이 throttle 의 적용 범위 밖이다.
- **UI Test Automation 탭 (TAP 연동, 2026-07-28)** — Test Suites 화면(`/suites/`)이 **API Test Automation**(기존 Jenkins, 기본) / **UI Test Automation**(`?tab=ui`) 2탭으로 확장. UI 탭은 TAP(tapilot, Playwright 기반)에 등록된 Suite 를 매분 cron 이 미러링(`TapSuite`)한 목록 — Suite 명/프로젝트/케이스 수/담당자/마지막 실행 결과(대기·실행 중·Pass·Fail·Aborted 배지)/**진행 중이면 케이스 수 기준 진행바**(`completed_cases`/`total_cases`)를 보여준다. `can_write` 이면 [▶ 실행] 버튼(confirm 후 TAP 세션 생성)이, 진행 중이면 [취소] 버튼이 노출되고, 실행 이력이 있으면 [상세]→Run 상세, TAP 딥링크 아이콘이 함께 노출된다. 화면 진입은 TAP 호출로 막히지 않고(DB 미러 즉시 렌더), 진행 중인 Run 이 있을 때만 10초 HTMX 폴링(`suites:tap_suite_table` fragment, 없으면 로드 1회). Run 상세(`/suites/tap-runs/<uuid:pk>/`)는 요약 카드(상태/진행바/passed·failed·others/시작·종료 시각) + 실행 정보(세션 ID/환경/제외 케이스 수/TestRail·TAP 링크) + 케이스별 결과 표(제목/섹션/상태/소요시간/TestRail 링크, 실패·에러 케이스는 콘솔 발췌를 접이식으로) — 진행 중이면 10초마다 자기 자신을 다시 조회해 갱신하고 종결 시 자동으로 멈춘다(전용 fragment 엔드포인트 없이 `hx-select` 로 같은 페이지에서 부분만 추출). **Slack 알림 연동(2026-07-29)** — 실행 시작/PASS/FAIL 이 기존 Suite Run 알림과 같은 채널로 발송된다(알림 설정 화면의 'UI Test Automation (TAP)' 토글 3종 — 기본값: FAIL 만 ON). FAIL 메시지에는 케이스 합/불 집계·실패 케이스 목록(TestRail 링크, 최대 5건)·소요 시간·포털/TAP 링크가 담기고, **담당자 관리 > UI 탭의 UI 알림 담당자가 메시지 본문에 `<@U..>` 멘션된다(2026-10-02, Reliability 스케줄 실행 포함 — 아래 'UI 알림 담당자')**, 취소(stopped=Aborted)는 발송하지 않으며 interval 타임아웃(error) 마감은 FAIL 로 발송된다. TAP 호출은 `.env` 의 `TAPILOT_API_BASE`/`TAPILOT_API_KEY` 가 필요하며 미설정/장애 시 UI 탭은 DB 미러만으로 조용히 렌더된다(§8). 설계 배경/스펙: [`docs/prd_ui_test_automation.md`](./docs/prd_ui_test_automation.md). 실데이터/TAP 실행은 실제 Playwright 자동화가 도는 실행 부작용이 있어 자동 검증 대상에서 제외.
- **API 탭 Jenkins 잡 자동 싱크 (2026-07-28)** — API Test Automation 탭 상단의 **[Jenkins 싱크]** 버튼(admin 전용, `/suites/api-sync/`)과 **10분 주기 자동**(매분 cron `sweep_reliability` 안에서 캐시 쿨다운으로 10분에 1회만) 이 내부 Jenkins 를 스캔해, 이름에 `api` 가 포함된 폴더/잡 중 **아직 등록 안 된 것을 `TestSuite` 로 자동 생성**하고 워커 Job 까지 등록한다(`apps.suites.services_sync.sync_api_suites`). 탐색 루트는 이미 등록된 내부 Suite 들의 상위 폴더(대부분 Jenkins 루트) + `.env` `JENKINS_API_SYNC_ROOTS`(콤마 구분, 선택). 익명 read 전용(트리거/쓰기 없음), 이미 등록된 Suite 는 건너뛰고, 삭제는 자동 반영 안 함(잡이 사라져도 Suite 유지 — 기존 정책과 동일, 수동 삭제). 설계 배경/스펙: [`docs/prd_ui_test_automation.md`](./docs/prd_ui_test_automation.md) §3.4.
- **내부 Jenkins 익명 read + GWT 트리거** — 내부 Jenkins(Keycloak SSO 뒤)에서 `user:api_token` Basic auth 가 SSO 세션 만료 시 거부되는 문제를 근본 해결. **읽기(상태/스테이지/콘솔/idle 조회)는 익명(Anonymous)** 으로 수행하고(Jenkins Role-Based Strategy 에서 anonymous 에 Overall/Read + Job/Read 부여), **트리거는 워커별 고유 Generic Webhook Trigger(GWT) 토큰**을 사용한다. Suite 상세 화면(admin + 내부 suite 한정) 의 "GWT 토큰" 카드에서 토큰 발급("전체 발급") 및 워커별 "재발급" 가능 — 발급 후 해당 워커의 Jenkins → Configure → Build Triggers → Generic Webhook Trigger → Token 에 동일한 값을 등록해야 한다. **외부 Jenkins**(suite 가 `jenkins_base_url` 지정)는 SSO 가 없으므로 기존 Basic auth(user:api_token) 방식 그대로 유지. **Quality Gate 파이프라인의 트리거는 아직 Basic auth** — GWT 전환은 별도 작업.
- **Jenkins 파라미터 모달** — Active Choices / Git Parameter 까지 4가지 경로 (HTML 스크래프, scriptText Active Choices, Git Parameter HTTP, scriptText GitParameter) **병렬** 으로 시도. 결과는 120초 LocMem 캐싱, `?refresh=1` 로 강제 재조회. **APP_IMAGE (luxair `ImageTagParameterDefinition`)** 는 Jenkins `/api/json` 응답에 `choices=[]` 로 비어 오므로, `create-image` 빌드 콘솔 로그에서 jib `--target=<registry>/<repo>:<tag>` 패턴으로 실제 푸시된 이미지 태그를 직접 추출(`extract_pushed_image_tag`)하여 `default` + `choices` 에 주입한다 — AWS 자격증명/ECR 직접 조회 없이 이미 DB 에 저장된 콘솔(`image_console_output`)만 사용. deploy / prod 모달 양쪽에서 적용되며 자동 트리거(`_auto_trigger_deploy` / `_auto_trigger_prod`) 에서도 매칭 불가능하면 사용자 확인 단계로 떨어진다. 수동 모달에서 추출+매칭 모두 실패하면 submit 버튼이 비활성화되고 경고 배너가 표시되며, 빈 태그/콜론으로 끝나는 값은 브라우저와 서버 양쪽에서 차단된다.
- **Image Build git 정보 표시** — Image Build 단계에 사용된 source repository 의 **branch 명과 commit hash(앞 8자리)** 를 Run 상세 화면에 노출. release/feature/hotfix 어느 브랜치로 빌드했는지 한눈에 확인. Jenkins build_info 의 `actions[BuildData].lastBuiltRevision` 에서 추출하며, Pipeline-from-SCM 빌드처럼 BuildData 가 두 개(Jenkinsfile 저장소 + source 저장소) 들어오는 경우 `image_params_json` 의 값에서 자동 추출한 source repo URL/branch 를 hint 로 넘겨 source 측 BuildData 를 우선 선택. git plugin 정보가 없으면 `GIT_BRANCH`/`BRANCH` 등 트리거 파라미터를 폴백으로 사용. 표시 시 `origin/`, `refs/heads/`, `refs/remotes/origin/` prefix 는 자동 제거.
- **Image Build 이미지 태그 표시 (`image_app_tag`)** — Image Build 단계가 ECR 에 실제 푸시한 이미지 태그를 Run 상세 화면의 Image Build 스텝에 `fa-tag` 아이콘과 함께 표시. 빌드 콘솔에서 jib `--target=` 토큰을 파싱해 추출(`QualityGateRun.image_app_tag` 프로퍼티, DB 컬럼 없음). 이 태그가 deploy/prod 모달의 APP_IMAGE 선택 항목으로 자동 주입되어 Run 이 빌드한 이미지 그대로 배포됨을 보장한다.
- **Argo CD 연동** — 배포 Job 성공 후 hard refresh + sync 트리거 → *Synced + Healthy* 도달까지 폴링. Dev/Stg/Prd 토큰 분리, 4초 쿨다운으로 외부 API 보호.
- **Argo CD 상태 배지** — Run 상세 화면의 Argo CD 표시는 sync/health/operation 조합으로 분기:
  *Synced & Healthy* (초록) / *동기화중* / *롤아웃중* / *OutOfSync — 재동기화 대기* (노랑) / *Sync 실패* / *Degraded* / *Missing* (빨강). `Sync 실패` 또는 health=`Degraded`/`Missing` 인 경우 Retry Sync 버튼이 즉시 노출되고, sync 작업이 `Failed`/`Error` 로 종료되면 15분 타임아웃을 기다리지 않고 곧바로 `(PROD_)DEPLOY_FAILED` 로 전환.
- **수동 Argo CD 재시도** — `argocd_retry(run, stage)` 엔드포인트로 sync 상태 리셋 + 즉시 재시도. `DEPLOY_FAILED` / `PROD_DEPLOY_FAILED` 모두 다시 `*_DEPLOYING` 으로 되돌려 폴링 재개.
- **htmx 5초 폴링** — Run 상세 화면이 부분 갱신. 외부 호출은 4초 쿨다운, Jenkins 8s / Argo CD 6s 타임아웃.
- **Error Log 버튼 (실패 단계만 노출)** — 이미지/검수대상 배포/검수통과 배포/Test Suite Job 어느 단계든 실패하면 `Console` 옆에 빨간 **Error Log** 버튼이 자동 노출. 클릭하면 Jenkins 콘솔 로그에서 자동화 스크립트가 마킹한 `@@@...@@@` 블록(또는 pytest `=== FAILURES ===` 섹션, `[FAIL]`/`[STOP]` 라인 주변) 만 추출해 보여준다. 전체 콘솔로 점프하는 *Full Console* 버튼도 같이 노출. 파싱 규칙은 `apps/integrations/log_errors.py`.
- **Error Log 상단의 "이슈 F/U 담당자" 표** — 추출된 에러 위에 콘솔에 등장한 URL / Method / 호출횟수 / Status / **카테고리** / **영역** / **FE 담당자** / **BE 담당자** 표가 자동 노출 (카테고리·영역은 매칭된 `ApiEndpointOwner` 의 분류값, 미매칭 행은 `-`). URL→담당자 매핑은 `/accounts/api-owners/` 의 Google Sheets 동기화 마스터(`ApiEndpointOwner`) 와 **path-only 정규식 매칭** (호스트는 떼어내고 path 만 비교, `${PARAM}` 자리는 와일드카드). 행의 Status 가 4xx (400~499) 면 FE 담당자 칸이 노랑 음영, 5xx 면 BE 담당자 칸이 빨강 음영으로 강조. 매칭 실패한 URL 은 admin 에게 "+ 등록" 버튼이 노출되며, 클릭하면 **그 자리 모달**로 담당자를 등록할 수 있다 (페이지 이동 없이 뒤의 에러 로그를 보면서 작성 — Method/Endpoint 는 그 URL 로 자동 prefill, **레포/카테고리/영역은 Google Sheets 기준 드롭다운**, **사용 횟수는 선택값**). 등록 즉시 표에 반영. 호스트 매핑(`/accounts/api-hosts/`) 은 같은 path 가 AM/JK 양쪽에 등록된 경우의 disambiguation 용이라 비어있어도 매칭은 동작. **컬럼 변경 이력 (2026-05-20)**: `4xx 담당자` → `FE 담당자`, `5xx 담당자` → `BE 담당자`. `*담당팀` 컬럼은 삭제 (담당자만 관리). 동기화/CSV 다운로드 파서는 옛 헤더 (`400에러 담당자` 등) 도 alias 로 호환.
- **담당자 마스터 Google Sheets 동기화 (CSV 업로드 대체, 2026-05-27)** — `/accounts/api-owners/` 의 담당자 마스터를 Google Sheets 와 동기화한다. 시트 컬럼이 기존 CSV 헤더와 동일해 기존 임포트 파서(`(Method, Endpoint)` 키 `update_or_create`)를 그대로 재사용. **full mirror** — 시트가 단일 진실 소스이며 시트에 없는 imported row 는 삭제, 수동 row(`is_imported=False`)는 보존. 인증은 **서비스 계정 또는 Workload Identity Federation(WIF)** — `.env` 의 `GOOGLE_CREDENTIALS_FILE`(파일 type 자동 판별, 운영은 AWS WIF) 또는 `GOOGLE_SA_JSON_B64` 등에 자격증명 지정, 대상 시트를 호출 주체(SA / 임퍼소네이트 SA)에 '뷰어' 공유, 대상 시트 ID 는 `GSHEET_OWNERS_SPREADSHEET_ID`. 조회는 **Drive API `files.export(text/csv)`** (첫 탭 gid=0). 트리거 = ① 매분 도는 cron `python manage.py sync_api_owners_from_sheet --if-due` — 화면 하단에서 설정한 주기(분, `OwnerSyncSettings`, 0=끔)가 도래했을 때만 실행 ② 화면의 'Google Sheets 동기화' 버튼 (수동 즉시). 모듈: `apps/integrations/google_sheets.py`, 서비스: `services.sync_owners_from_google_sheet`, 주기 설정: `accounts:owner_sync_settings_save`.
- **역방향 기록 (write-back, opt-in)** — Error Log 모달 "+ 등록"으로 만든 수동 담당자(`is_imported=False`)를 등록 직후 **시트에도 1행 append** 한다(비동기·best-effort, 기존 컬럼 순서·헤더·`${BFF_URL}` 값 원형 보존). 쓰기는 **Sheets API(`spreadsheets` 스코프)** + SA 의 시트 **'편집자'** 권한 필요. 기본 off — `.env` 에 `GSHEET_OWNERS_WRITEBACK=1` 설정 + GCP 프로젝트에 **Google Sheets API 활성화** 시 동작. `services.writeback_owner_to_sheet_async` → `google_sheets.append_owner_row`.
- **"에러 노티" 버튼 (Error Log 페이지)** — 이슈 F/U 담당자 표의 각 행 우측에 빨간 종 모양의 **에러 노티** 버튼이 노출된다 (`can_write` 권한 필요). 클릭 → confirm → 해당 URL 의 4xx/5xx 담당자에게 Slack 으로 즉시 알림 발송. 입력값 없이 버튼 한 번이면 끝나고, 메시지에는 ① Slack `users.list` API 로 자동 조회한 `<@U0XXX>` 멘션 (실패 시 `@이름` plain text) ② Suite/Job 또는 Gate/Stage 컨텍스트 라벨 ③ URL/Method/Status ④ 콘솔에서 추출된 에러 발췌 (최대 1500자) ⑤ 포털 에러 로그 페이지 절대 URL 이 포함된다. `ActivityLog (owner_notified)` 에 기록.
- **Slack 자동 에러 알림 — 멘션 + 에러 발췌 + 로그 링크 (Suite/QG Run 실패 시)** — 단독 Test Suite Run 이 FAIL 하거나 파이프라인 내부 Test Suite 가 FAIL 하면 기존 Slack 메시지에 *이슈 F/U* 필드가 자동으로 추가된다. 콘솔에서 자동 추출한 URL 의 FE/BE 담당자 이름을 Slack workspace 의 `users.list` 와 자동 매칭 → `<@U0XXX>` 멘션. 매칭 실패한 이름은 `@이름` plain text 로 노출(알림은 안 가지만 메시지엔 등장). URL/Method/Status 표, 발췌된 에러 본문, 포털 에러 로그 페이지 링크를 한 번에 포함. 단독 Suite Run 의 FAIL 알림에서는 실패 job 중 첫 1건만 followup 으로 첨부 (메시지 길이 보호). 토큰/채널 미설정 등은 silent.
- **대체 담당자 (FallbackOwners)** — Error Log 의 URL 이 `ApiEndpointOwner` 마스터와 매칭되지 않은 경우 자동으로 노티를 받을 담당자. **담당자 관리 화면 (`/accounts/api-owners/`) 하단** 의 콤마 다중값 입력(예: `김태식, 김도연`) 으로 설정 (singleton, `FallbackOwners` 모델). Suite 자동 알림에서 매칭된 owner 가 0건이면 fallback 으로 자동 발송 + 메시지 본문에 "담당자 매칭 없음, 대체 담당자로 지정" 라벨. Error Log 페이지의 매칭 실패 행도 "에러 노티" 버튼이 노출되어 수동 발송 가능. 미설정 상태에서는 매칭 실패 URL 의 알림이 발송되지 않음.
- **UI 알림 담당자 (UiAutomationOwners, 2026-10-02)** — UI Test Automation(TAP) Suite 실패(FAIL) Slack 알림에 멘션할 담당자. TAP 케이스는 API URL 매칭 개념 밖이라 `ApiEndpointOwner` 매칭이 성립하지 않으므로, **담당자 관리 화면의 'UI 담당자' 탭(`/accounts/api-owners/?tab=ui`)** 에서 Suite/케이스 구분 없이 콤마 다중값(예: `김태식, 김도연`)으로 통 설정한다 (singleton). FAIL 알림(최종 시도·interval 타임아웃 error 포함, **Reliability 스케줄 실행 포함** — UI 실패 알림 대부분이 스케줄 실행이라 API Suite 의 '스케줄 run 멘션 제외' 정책을 따르지 않음) 의 메시지 본문 첫 줄 아래에 `*담당자 F/U* <@U..> ...` 로 멘션. 시작/PASS 알림엔 멘션 없음. 미설정이면 멘션 없이 기존처럼 발송. 이름→멘션 변환은 API 경로와 동일한 `slack_users.find_user_id_by_name` (매칭 실패는 `@이름` plain text).
- **Slack 사용자 자동 매칭** — `apps/integrations/slack_users.py:find_user_id_by_name(name)` 가 Slack `users.list` API 로 workspace 사용자 (id / name / real_name / display_name + normalized 변형) 를 LocMem 1시간 캐시에 인덱싱해서 owner 이름을 U_ID 로 변환. SLACK_BOT_TOKEN 에 **`users:read` scope 필수** (Slack 앱 설정 → OAuth & Permissions 에서 추가 후 워크스페이스 재설치 1회). scope 가 없으면 멘션이 평문(`@이름`) 으로만 나가고 알림은 안 간다. 별도 수동 매핑 테이블/어드민 메뉴는 없음 — workspace 멤버 정보가 단일 진실 소스.
- **Slack 매칭 디버그 페이지** — `/accounts/slack-users/` (admin only). 현재 인덱싱된 멤버 수, 마지막 `users.list` 호출 실패 사유 (missing_scope / 인증 오류 등), 이름 검색 (부분 일치 후보 노출), 캐시 강제 갱신 버튼. 자동 매칭이 안 될 때 1) scope/재설치 문제 vs 2) Slack 표시명/실명과 CSV 표기 불일치 를 즉시 구분 가능.
- **Run 일련번호(`seq_no`) + 최신 Run 배너** — 게이트별로 1부터 순증가하는 `#N` 번호를 모든 Run 에 부여. 상세 헤더, 탭 title (`auth-admin #42 - QG Portal`), 게이트 상세의 실행 이력 표, 대시보드 최근 실행에서 모두 `#N` 노출. 같은 application 의 Run 상세 탭을 여러 개 띄웠을 때 보고 있는 화면이 latest 가 아니면 상단에 노란 배너 + "최신 Run 으로 이동" 버튼이 자동 노출돼 휴먼 에러 방지.
- **OS 알림 + 글로벌 워처** — Service Worker 기반 데스크톱 알림. 실행 중 Run 은 `localStorage` 워처로 등록되어 **페이지 이동/새로고침해도** 5초 주기로 추적. 터미널 상태 도달 시 토스트 + 삐 + OS 알림. 사이드바 ON/OFF 토글, 우하단 진단 버튼/가이드 제공.
- **Slack 알림** — Quality Gate 파이프라인과 단독 Test Suite Run 양쪽 모두 상태 전이 시점에 Slack 채널로 알림 발송. Run 시작/종료, 각 단계(image/deploy/test/prod) 시작·종료, 파이프라인 내부 Test Suite, **단독 Suite Run (시작/PASS/FAIL)**, Argo CD Sync 실패 등 20개 이벤트를 어드민 화면에서 개별 토글. 파이프라인 내부 Suite (`TEST_SUITE_*`) 와 단독 Suite Run (`SUITE_RUN_*`) 토글은 분리돼 있어 각각 끄고 켤 수 있다. Bot 토큰은 `.env` 의 `SLACK_BOT_TOKEN`, 기본 채널은 어드민 화면(또는 `.env` 의 `SLACK_DEFAULT_CHANNEL`) 에서 관리. Run 상세 링크를 메시지에 포함하려면 `QGP_BASE_URL` 도 채울 것. 발송은 데몬 스레드로 비동기 처리되어 폴링 흐름을 막지 않으며, 토큰/채널 미설정 시 silent skip.
- **Suite Run 실패 알림 Triage 버튼 (Failure / Skip, opt-in)** — 단독 Suite Run FAIL 알림에 실패 Job 별 **[🔴 Failure] [⏭️ Skip]** 버튼을 달아, 클릭으로 그 실패가 *실제 장애(Failure)* 인지 *자동화 코드/환경 문제(Skip)* 인지 확정한다. 확정 결과는 포털 Run 상세의 실패 Job 배지로 반영되고, Slack 원본 메시지는 "처리됨" 으로 자동 갱신된다. 인트라넷이라 인바운드 공개 URL 대신 **Socket Mode**(앱→Slack 아웃바운드 WebSocket)로 클릭을 수신 — 상시 프로세스 `manage.py slack_triage_listener` (systemd 상주). 권한은 **`can_write` + `User.slack_user_id` 매핑된 유저만**, 재분류(덮어쓰기) 허용. 활성화: `.env` `SLACK_TRIAGE_BUTTONS=1` + `SLACK_APP_TOKEN=xapp-` + Slack 앱 Socket Mode·Interactivity ON + 리스너 상주. **스케줄(Reliability) 실행의 FAIL 알림**에는 추가로 **[🛑 스케줄 중단] / [▶️ 스케줄 재활성화]** 버튼이 붙어 상위 스위트 스케줄을 즉시 멈추거나 재개할 수 있는데, 이 버튼만은 **김태식·김도연 두 사람만** 클릭 처리되고(그 외 클릭 시 "김태식·김도연 만 사용 가능" ephemeral 안내) 나머지에겐 무시된다.
- **3단계 권한 (RolePermissionMiddleware)**
  - Admin (`qpe_admin` / superuser): 전부 허용
  - 일반 역할: 실행/시작/중단/재시도/상태 폴링 등 operational POST 만 허용
  - Viewer: 모든 POST 차단 + **열람 페이지도 Dashboard · Suite Runs 영역으로 제한** (그 외 페이지 GET 시 대시보드로 리다이렉트, 사이드바에서도 해당 메뉴 숨김)
- **회원가입 승인 대기** — `is_active=False` 로 가입 → 관리자가 승인 + 역할 부여. 관리자가 승인 절차 없이 계정을 바로 만들어야 하면 **사용자 직접 추가**(`/accounts/users/new/`)로 즉시 활성 상태 + 역할 지정까지 한 화면에서 처리.
- **Reliability Test (Robustness)** — Suite 별로 N분 간격 반복 트리거. **스케줄 트리거는 cron(매분 `python manage.py sweep_reliability`)이 구동한다** — Celery beat 같은 별도 스케줄러가 없어, 과거엔 트리거가 화면 폴링 시점에만 진행돼 **아무도 Reliability Test 화면을 안 열어두면 interval 이 한참 지나도 안 도는**(=설정 간격대로 안 도는) 문제가 있었고, 이를 브라우저와 무관한 cron 으로 해결했다(crontab 등록 필요 — §6·§10). 각 스케줄 interval 도래 여부는 sweep 내부에서 race-free 하게 판정하므로 매분 호출해도 각 스케줄은 자기 interval 에만 1회 트리거된다. **목록/상세 진입은 DB 상태로 즉시 렌더**(Jenkins 폴링으로 페이지 로드를 막지 않음)하고, 상세 화면의 HTMX fragment(`load, 10초마다`)도 `sweep_active_schedules()` 를 돌리지만 이는 **보고 있을 때의 라이브 갱신용**이다(스케줄링 신뢰성은 cron 이 보장). sweep 은 5초 쿨다운(`_SWEEP_COOLDOWN_SECS`, cron 은 `force=True` 로 우회)으로 다중 탭/유저의 중복 호출을 1회로 합쳐 SQLite 락 경합을 막는다 — 과거 목록/상세 진입마다 동기 sweep(Jenkins 직렬 폴링) 으로 페이지가 느리던 문제도 함께 해소. 테스트별 **잡 병렬 실행** 마스터 토글 (스케줄 목록의 *일괄 시작* 옆) — OFF (기본) 면 모든 스케줄이 Suite 의 잡을 한 잡씩 순차 실행, ON 이면 한 번 트리거 시 Suite 의 모든 잡을 동시에 트리거. **병렬 모드에서도 잡 실행 지연 throttle(`trigger_delay_seconds`) 가 적용**돼 한 sweep 에서 해당 suite 의 슬롯을 점유하는 잡은 1개뿐이고 나머지는 WAITING 으로 보류 → 이후 sweep 마다 delay 지난 잡 1개씩 추가 트리거. 먼저 트리거된 잡은 계속 RUNNING 이므로 잡들은 **트리거 시점만 delay 간격으로 엇갈린 채 병렬로 함께 실행**된다. 서로 다른 스케줄/suite 는 독립 슬롯이라 starvation 없음. 토글은 그 테스트에 속한 모든 스케줄에 일괄 적용. 테스트별 **단일 테스트 모드**(`single_job_mode`) 토글 (병렬 버튼 옆) — ON 이면 한 번 트리거 시 Suite 의 모든 잡 대신 **비어있는(idle) 잡 1개만** 실행한다(idle 판정은 단독 실행과 동일한 `pick_idle_job` — 다른 RUNNING suite_run 이 점유 중인 잡 제외 + Jenkins building 체크, idle 이 없으면 그 사이클은 skip). **단일과 병렬은 상호배타** — 단일 ON 시 병렬은 자동으로 OFF 되고 병렬 버튼은 비활성화(단일을 끄면 다시 사용 가능)된다. **일괄 시작** 버튼은 실행 중인 스케줄이 하나라도 있으면, **일괄 중지** 버튼은 실행 중인 스케줄이 하나도 없으면 각각 비활성화된다(`any_running` 판정). *일괄 시작/중지* 옆 **일괄 초기화** 버튼은 그 테스트의 모든 스케줄의 세션 통계(성공/실패/실행수/세션 시작/마지막 실행)를 빈 상태로 리셋한다 — `started_at` 까지 `None` 으로 비워 "세션 시작" 칼럼도 함께 비워지고, running 인 스케줄은 다음 트리거 시점에 `started_at` 이 자동으로 다시 채워져 카운트가 재개된다. `is_running` 은 손대지 않으므로 멈춰 있는 스케줄을 reset 한다고 자동 시작되지 않는다. **스케줄을 중지했다가 다시 시작(재개)해도 세션 통계는 리셋되지 않는다** — 시작 시 기존 `started_at` 이 있으면 그대로 유지해(신규 스케줄의 최초 시작만 새로 채움) 성공/실패/실행수/성공률이 멈췄다가 이어서 집계된다. 통계를 비우는 것은 위 (개별/일괄) **초기화** 버튼뿐이다. 또한 **ABORTED(중단) 잡은 실패로 집계되지 않는다** — 스케줄 중지로 강제 종료됐든, 스케줄이 도는 중 Jenkins 쪽에서 빌드가 취소됐든(모두 ABORTED) 성공률·실패수·실행수(total) 계산에서 제외되어 중단이 성공률을 깎지 않는다(대시보드 결과 분포·미 장애 판정 큐와 동일한 "ABORTED=장애 아님" 전역 기준). 단 사람이 Slack/포털 버튼으로 **Failure(실장애)를 명시 확정한 ABORTED 잡은 장애로 인정**해 실패로 집계된다. 반면 아래 interval 타임아웃 마감은 미완료 잡을 ABORTED 가 아닌 **FAILURE** 로 마킹하므로 여전히 실패로 집계된다(Reliability 이상 신호 유지). interval 도달 시점에 이전 사이클이 끝나지 않았다면 **미완료 잡(PENDING/QUEUED/RUNNING)을 FAILURE 로 마감**하고 Jenkins 측 빌드는 `stop_build` 로 종료한 뒤 새 사이클로 진행한다 — 즉, 60분 interval 인데 이전 잡이 60분 안에 안 끝나면 그 잡은 실패로 처리되고 다음 사이클이 시작된다. 이때 **마감 사유와 Jenkins 콘솔 로그가 잡별 콘솔에 자동 저장**되고 ActivityLog 에도 남는다(무증거 실패 방지, 2026-07-16). 마감 대상은 **트리거된 지 한 interval 이상 지난 run 만**이며, '시작' 버튼은 **race-free 멱등**이라 개별 시작+일괄 시작이 겹쳐도(중복 시작) 방금 트리거된 run 이 죽지 않는다. **UI Automation(TAP) Suite 도 동일 스케줄러로 반복 실행 가능** (`RobustnessSchedule.tap_suite`, 대상은 `test_suite`/`tap_suite` 중 정확히 하나 — PRD `docs/prd_ui_test_automation.md`) — 시작/중지/수동실행/초기화/삭제 버튼과 병렬·단일 토글의 백엔드 분기는 공용, 세션 통계는 Jenkins=잡 단위·TAP=케이스 단위로 자동 집계되지만 화면 컬럼(실행 수/성공·실패/성공률)은 동일한 의미로 함께 보인다. TAP 스케줄 행은 **[UI] 배지**로 구분되고, 실행 이력 화면은 TAP 이면 세션(TapRun) 단위 이력으로 전환된다. TAP 스케줄도 Jenkins 와 동일한 실패 신호 규칙을 따른다 — **사용자가 중지하면 진행 중이던 TapRun 은 stopped(Aborted, 장애 아님)**로, **interval 안에 못 끝나 타임아웃으로 마감되면 error(장애로 집계)**로 구분 기록된다.
- **Activity Log** — 게이트/폴더/Run/Suite/사용자 대상 주요 행위 기록.

---

## 5. 디렉터리 구조

```
apps/
  qualitygates/   # ★ 핵심 — Folder/Gate/Run, 파이프라인 폴링
  suites/         # Test Suite + Suite Run + Queue
  robustness/     # Reliability Test (스케줄 sweep)
  accounts/       # User, 역할 미들웨어, 회원가입 승인, Slack 알림 설정 화면
  notifications/  # Slack 알림 (모델 + 발송 서비스 + 이벤트 상수)
  activitylog/    # 활동 로그
  dashboard/      # Suite 기준 7일 요약 카드 + Chart.js 차트 3종 + 최근 TestSuiteRun 15건
  visualization/  # "Quality Highway / Race Control" Canvas 페이지 + Test Suite 실행 피드 JSON (모델 없음, 읽기 전용)
  syncops/        # 서버 이관용 스냅샷 export/import/verify/sync/fetch_snapshot_via_api 관리 명령 (모델 없음, snapshot.py 가 라이브러리)
  integrations/
    jenkins.py        # JenkinsClient (Active Choices/Git Parameter 4-path)
    argocd.py         # ArgoCDClient (appNamespace 규약)
    google_sheets.py  # Google Sheets 담당자 마스터 동기화 (Drive files.export)
    log_errors.py     # Jenkins 콘솔 로그 에러 발췌 파서 (@@@/pytest FAILURES)
    slack_users.py    # Slack users.list → User ID 자동 매칭 + 멘션
    url_matcher.py    # Error Log URL → ApiEndpointOwner 담당자 매칭
    slack.py / webhooks.py / tasks.py  ← 레거시
  api_v2/         # DRF API v2 — 외부 시스템용 REST API (API Key + Scope, §9 참고). urls_ci.py = /ci-api/ 용 라우팅(문서·export 제외)
  api/            # DRF API (구 모델 기반, 신 파이프라인과 무관)
  gates/, services/, approvals/, escalations/  ← 레거시 placeholder

config/
  settings/
    base.py        # 공통 (PostgreSQL/Redis 기본값은 사용 안 함)
    dev.py         # SQLite + LocMem + EAGER celery — 운영도 이걸 사용
    production.py  # 미사용
  urls.py          # /sw.js 인라인 서빙, /health/, 앱 include
  urls_ci.py       # /ci-api/ 요청 전용 URLconf (api/v2/ 마운트만 ci-api/ 로 교체 — CiApiBoundaryMiddleware 가 지정)
  celery.py / wsgi.py

static/js/qg_notify.js   # 토스트 + OS 알림 + 글로벌 Run 워처
templates/               # Django 템플릿 (base.html 에 알림 인프라 포함)

docs/
  api_guide.md                      # ★ 외부 호출자용 단일 가이드 (api_v2_quickstart 흡수·삭제)
  prd_server_migration_sync.md      # 서버 이관 + 전체 데이터 싱크 PRD / AI 런북 (apps/syncops 사용법)
  api_requirements_data_export.md   # HTTPS 만 열린 환경에서 원본 데이터를 내려받기 위한 API 요구사항 + 구현 현황 (스냅샷 내보내기 API, P0 구현됨·config 제외)
  handoff_sso_integration.md        # SSO(Keycloak/게이트웨이) 연동 인수인계 — 새 서버에서 이어서 작업
  qw-sso-gateway.md / service-sso-migration.md  # 게이트웨이(qw) 구조 · 서비스 SSO 전환 가이드
  llm_browser_automation_policy.md  # LLM 브라우저 테스트 러너 정책
  test_script_error_marking_guide.md # 자동화 스크립트 에러 마킹 가이드
```

파이프라인 진행 로직은 모두 `apps/qualitygates/views.py` 에 모여 있다 — `_poll_gate_run`, `_poll_image_build`, `_poll_deploy`, `_check_argocd_sync`, `_advance_test`, `_poll_current_test`, `_poll_prod_deploy`, `_finalize_run`, `_should_auto_advance`, `_auto_trigger_deploy`, `_auto_trigger_prod`, `argocd_retry`, `rerun_test`, `cancel_run`.

---

## 6. 실행 방법

### Linux 서비스 (`172.20.0.221`)

`qualitygate.service` 가 부팅 시 자동 시작하며 `/home/taeshkim/qa-qg` 에서 `taeshkim` 사용자로 8601 서버를 유지한다. 비정상 종료 시 5초 후 재시작한다 (60초 안에 시작 5회 제한). 버전 관리 원본은 `deploy/qualitygate.service`, 설치 위치는 `/etc/systemd/system/qualitygate.service` 이다.

```bash
sudo install -m 0644 deploy/qualitygate.service /etc/systemd/system/qualitygate.service
sudo systemctl daemon-reload
sudo systemctl enable --now qualitygate
```

운영 시 필요한 명령을 선택해 실행한다.

```bash
systemctl status qualitygate          # 상태 확인
sudo systemctl restart qualitygate    # 재시작
sudo systemctl stop qualitygate       # 의도적으로 서버 중지
sudo journalctl -u qualitygate -f      # 로그 확인
```

서비스는 `.venv/bin/python manage.py runserver 0.0.0.0:8601 --noreload` 를 실행하고 앱이 `.env` 를 읽는다. **코드·`.env` 변경 후 `restart` 가 필요**하며 Git 동기화·의존성 설치·마이그레이션은 별도 배포 작업이다. 아래 `run.sh` 는 수동 설치·개발 기동용으로 계속 사용할 수 있으나, 서비스 실행 중 함께 띄우면 8601 포트가 충돌한다.

### Linux / macOS

```bash
./run.sh
```

### Windows

```powershell
./run.ps1
```

`run.sh` / `run.ps1` / `run.bat` 는 실행 시마다 **먼저 `origin/master` 를 fast-forward(ff-only)로 당겨와 코드를 최신화**한다 (현재 브랜치가 `master` 가 아니거나 git 미설치/네트워크 실패/ff 불가 시엔 경고만 띄우고 현재 코드로 계속). **로컬 `db.sqlite3` 는 스크립트가 절대 건드리지 않는다** — ff-only 라 백업/복원/checkout 없이 코드만 갱신한다 (`db.sqlite3` 는 Git 미추적이라 pull 로 바뀌지 않음, DB 갱신은 수동 명령어로만).
이어서 `run.sh` / `run.ps1` 는 python3.13 → 3.12 → 3.11 → 3.10 순으로 인터프리터를 자동 탐색한다.
기존 `.venv` 가 3.10 미만이면 재생성. 이후 `pip install` → `migrate` → `runserver 0.0.0.0:8601`.

### 수동 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # 최초 1회
python manage.py migrate
python manage.py runserver 0.0.0.0:8601
```

접속 주소 (`QGP_SCRIPT_NAME` 미설정 시)

- Portal: <http://localhost:8601>
- Admin: <http://localhost:8601/admin/>
- Suites: <http://localhost:8601/suites/>

### Nginx `/qg` 경로로 접속

기존 루트 배포는 그대로 사용하며, 하위 경로 배포에서만 `.env` 에 `QGP_SCRIPT_NAME=/qg` 를 설정한다. `DJANGO_ALLOWED_HOSTS` 의 기존 목록에는 접속 호스트 `172.20.0.221`, `qw.jkintra.net` 을 추가한다 (프로토콜·포트·경로 제외). 환경변수 변경 후 서비스는 `sudo systemctl restart qualitygate`, 수동 기동 환경은 `./run.sh` 로 재기동한다.

- 접속: <http://172.20.0.221/qg/> (로그인: `/qg/accounts/login/`, API 문서: `/qg/api/v2/docs/`).
- **최종 접속 주소는 게이트웨이 `https://qw.jkintra.net/qg/`** (게이트웨이가 `/qg/` 접두를 제거해 백엔드 `8601` 로 프록시). 옛 주소 `qualitygate.jkintra.net`(AWS 원본·ALB 직결)은 2026-10 서버 이관 완료로 **종료**됐다(502). 프로그램(Jenkins/CI) 호출은 `https://qw.jkintra.net/qg/ci-api/`. 게이트웨이 출발지(앱이 보는 `REMOTE_ADDR`)는 `172.20.0.222` 로 실측 확인(2026-10-09).
- Nginx: `/qg` → `/qg/` 리다이렉트, `/qg/` → `http://host.docker.internal:8601/` 로 프록시하며 **`/qg` 접두 경로를 제거**한다. 관리 콘솔의 저장과 실제 Nginx 적용은 별개이므로 적용 결과도 확인한다.
- 로그인 이동·정적 파일·Service Worker 범위·세션/CSRF 쿠키·화면 링크·API 응답 링크·OpenAPI 경로에 `/qg` 가 반영된다. 설정한 인스턴스의 브라우저 접속은 위 프록시 주소를 사용한다.
- `502` 는 Nginx에서 백엔드에 연결하지 못한 상태일 수 있다. 서비스 상태·로그, `0.0.0.0:8601` 리슨, 컨테이너에서 백엔드 접근 여부를 확인한다. 상태별 진단은 `maintenance.md` §11 참고.

---

## 7. 환경

| 항목 | 값 |
|---|---|
| Python | 3.10+ |
| Django | 4.2.x |
| DB | SQLite (`db.sqlite3`, Git 에서 제외 — `.gitignore`) |
| 캐시 | LocMem (`qgp:` prefix, Jenkins 파라미터 120초) |
| Celery | `TASK_ALWAYS_EAGER=True` (실질 미사용) |
| 서버 | `runserver 0.0.0.0:8601` (systemd `qualitygate.service`), 게이트웨이 `qw.jkintra.net/qg`(HTTPS) 뒤 — IDC `wx-qms221` |
| 시간대 / 언어 | Asia/Seoul / ko-kr |
| venv | `/home/taeshkim/qa_qualitygate_portal/.venv` |

`base.py` 에 PostgreSQL/Redis 기본값이 남아 있으나 실제 운영은 `dev.py` (SQLite + LocMem + eager celery) 기준으로 동작한다. 게이트웨이(HTTPS) → 앱(HTTP) 프록시 구조라 `CSRF_TRUSTED_ORIGINS`, `SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST` 가 세팅돼 있다.

---

## 8. 환경 변수

`.env.example` 을 복사해 `.env` 에 채운다. **현재 실제 사용되는 항목은 다음뿐이다:**

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- (선택) `QGP_SCRIPT_NAME` — Nginx 하위 경로 배포 시 `/qg`. 비우면 기존 `/` 배포. 프록시는 이 접두 경로를 제거해 백엔드로 전달해야 한다.
- `DJANGO_SETTINGS_MODULE=config.settings.dev`
- `DJANGO_CSRF_TRUSTED_ORIGINS` — 콤마 구분 오리진(스킴 포함). 코드 기본값은 이관 전 주소 `https://qualitygate.jkintra.net`(종료된 주소, 기본값이 그대로 남아 있을 뿐) — **게이트웨이로 접속하는 운영·이 호스트는 `.env` 에 `https://qualitygate.jkintra.net,https://qw.jkintra.net` 로 `https://qw.jkintra.net` 을 반드시 포함**해야 로그인/POST 의 CSRF 검증이 통과한다. `DJANGO_ALLOWED_HOSTS` 에도 `qw.jkintra.net` 포함
- (선택) **SSO 로그인 (Keycloak OIDC)** — 값이 없으면 SSO 는 꺼지고 자체 로그인이 유지된다. **`.env.example` 의 `OIDC_INTERNAL_*` 세 줄은 주석 처리돼 있다**(복사만으로 켜져 자체 로그인이 닫히는 락아웃 방지) — Keycloak client 준비 후 주석을 풀고 실제 값을 넣는다. 평문 시크릿은 `.env` 에만 두고 로그/화면/문서에 남기지 않는다.
  - `OIDC_INTERNAL_ISSUER` — 예 `https://sso.jkintra.net/realms/world`. `https://` 필수(localhost 제외)
  - `OIDC_INTERNAL_CLIENT_ID` / `OIDC_INTERNAL_CLIENT_SECRET` — Keycloak client 값. **셋 중 일부만 설정하면 기동 실패**(오타·누락 방지)
  - `OIDC_INTERNAL_POST_LOGOUT_REDIRECT` — `True` 면 로그아웃 시 Keycloak 세션까지 종료(end-session). 기본 `False`. **Keycloak client 에 Valid post logout redirect URI(`https://<호스트>[/qg]/accounts/login/`)가 등록된 뒤에만 켠다** — 미등록 상태로 켜면 Keycloak 이 오류 화면을 띄운다
  - `OIDC_INTERNAL_EMAIL_DOMAINS` — `email_verified` 없이 신뢰할 회사 이메일 도메인(콤마 구분, 기본 `worxphere.ai`)
  - `LOCAL_LOGIN_ENABLED` — 아이디/비밀번호 로그인·회원가입 병행 여부(기본 `False`). OIDC 값도 `GATEWAY_ENTRY_ENABLED` 도 없으면 무시하고 자체 로그인 유지(`LOCAL_LOGIN_ACTIVE = LOCAL_LOGIN_ENABLED or not (OIDC_PROVIDERS or GATEWAY_ENTRY_ENABLED)`). 진입 모드의 비상 복구 스위치
  - `GATEWAY_ENTRY_ENABLED` — 게이트웨이 진입 로그인(로그인 화면의 "Keycloak 로그인" 버튼 + 로그아웃 시 게이트웨이 세션 종료). 기본 `False`. **게이트웨이에 진입 경로(`/qg/accounts/gateway-login/`) 보호 라우팅이 적용·검증된 뒤에만** 켠다 — 켜면 자체 로그인이 꺼져 라우팅이 없으면 로그인 잠금. `.env.example` 에는 주석 처리
  - `TRUSTED_PROXY_CIDRS` — 게이트웨이(oauth2-proxy 앞단 Nginx)의 출발지 CIDR(콤마). **코드 기본값 `172.20.0.222/32`**(실측 확인 — 이 호스트에서도 명시 고정). **키를 빈 값으로(`TRUSTED_PROXY_CIDRS=`) 적으면 기본값이 가려져** `X-Auth-*` 헤더가 항상 제거된다(자동시작·헤더 로그인 없음) — 쓰지 않을 거면 키 자체를 지운다
  - `GATEWAY_LOGOUT_URL` — 게이트웨이 뒤 로그아웃 시 이동할 주소. **코드 기본값 `https://qw.jkintra.net/_qw/logout`**. 빈 값으로 적으면 기본값이 가려지고 앱 쪽 로그아웃 규칙(위 ②③)만 쓴다
- (선택) `JENKINS_DEFAULT_URL` / `JENKINS_DEFAULT_USER` / `JENKINS_DEFAULT_API_TOKEN` — Test Suite 가 외부 Jenkins 자격증명을 안 채웠을 때의 **2차 폴백**. **1차는 `/suites/credentials/` 에 저장된 내부 자격증명** 이고, 그게 비어있을 때 이 환경변수가 사용된다.
- (선택) `SLACK_BOT_TOKEN` — Slack 알림 사용 시 필수. `chat:write` scope. 봇이 알림 채널에 초대돼 있어야 함. **에러 노티의 담당자 자동 멘션을 사용하려면 `users:read` scope 도 함께 부여** (workspace 사용자 검색용).
- (선택) `SLACK_DEFAULT_CHANNEL` — 어드민 화면의 채널이 비어있을 때 폴백 (예: `#qg-alerts`)
- (선택) `QGP_BASE_URL` — Slack 메시지에 Run 상세 링크를 포함하려면 외부 접속 주소(게이트웨이 경로 포함)로 설정 (이 호스트 `.env` 값: `https://qw.jkintra.net/qg`). 비우면 Run 링크는 생략된다. 같은 값이 API Key 발급 화면·Swagger 의 호출 주소 `settings.CI_API_BASE`(= `QGP_BASE_URL` + `/ci-api`, 비어 있으면 기본 `https://qw.jkintra.net/qg/ci-api`)에도 쓰인다. 끝의 `/` 는 제거된다
- (선택) `TAPILOT_API_BASE` / `TAPILOT_API_KEY` — UI Test Automation(TAP) 탭 연동. TAP(tapilot, Playwright 기반) API 베이스 URL(기본 `https://tapilot.jkintra.net`)과 Bearer 키. 미설정 시 TAP 관련 싱크/실행/폴링은 조용히 스킵되고 UI 탭은 빈 목록으로 렌더(§4, `docs/prd_ui_test_automation.md`). **키 평문은 `.env` 에만 — 로그/화면/문서 어디에도 남기지 않는다.**
- (선택) `JENKINS_API_SYNC_ROOTS` — API 탭 Jenkins 잡 자동 싱크(`sync_api_suites`)가 추가로 스캔할 폴더 루트(콤마 구분). 비워두면 이미 등록된 내부 Suite 들의 상위 폴더(대부분 Jenkins 루트)만 스캔한다.
- **담당자 Google Sheets 동기화** (담당자 관리 화면 사용 시 필수). **IDC 이관(2026-10) 이후 이 호스트(`wx-qms221`)에서는 미사용** — AWS 전용 WIF(IMDS 필요)라 IDC 에서 인증이 불가해 `.env` 의 `GOOGLE_CREDENTIALS_FILE`·`GSHEET_OWNERS_SPREADSHEET_ID` 를 비우고 `GSHEET_OWNERS_WRITEBACK=0`, 자동 동기화 주기 0, `sync_api_owners_from_sheet` cron 미설치로 두며 담당자 마스터는 화면 편집·CSV 업로드로 자체 DB 에서 관리한다(`docs/prd_server_migration_sync.md`). 아래 항목은 시트 동기화를 다시 켤 때의 설정 설명이다:
  - `GOOGLE_CREDENTIALS_FILE` — 자격증명 파일 경로 (**현재 운영 방식**). 파일의 `type` 으로 종류를 자동 판별 — `service_account` / `authorized_user` / `external_account`(=Workload Identity Federation). 운영 서버는 AWS WIF(EC2 인스턴스 메타데이터 → STS → SA 임퍼소네이션)를 사용하며 키 파일이 아니라 페더레이션 설정만 둔다.
  - (대안) `GOOGLE_SA_JSON_B64` — 서비스 계정 JSON 키를 base64 인코딩한 값. 대안: `GOOGLE_SA_JSON` (JSON 원문 한 줄) 또는 `GOOGLE_SA_FILE` (SA 키 파일 경로). `GOOGLE_CREDENTIALS_FILE` 가 설정돼 있으면 그쪽이 우선.
  - `GSHEET_OWNERS_SPREADSHEET_ID` — 동기화 대상 스프레드시트 ID (URL 의 `/d/<ID>/`).
  - 사전 준비: **Drive API 활성화**, 대상 시트를 **호출 주체(principal)에 '뷰어'로 공유** — 서비스 계정이면 `...@....iam.gserviceaccount.com`, WIF 면 임퍼소네이트하는 서비스 계정 이메일(`service_account_impersonation_url` 에 박힌 주소, 담당자 관리 화면 상단에도 안내됨).

`.env.example` 에 보이는 PostgreSQL / Redis / TAS / Encryption / 그리고 레거시 Slack(`SLACK_SIGNING_SECRET`, `SLACK_QG_ALERTS_CHANNEL`, `SLACK_QPE_OPS_CHANNEL`) 변수들은 **레거시이며 동작에 영향 없다**.

Quality Gate 의 Jenkins/Argo CD 자격증명은 환경변수가 아니라 **인스턴스 등록 화면** 에서 입력한다.

---

## 9. URL 라우팅

아래는 앱 내부 경로다. `QGP_SCRIPT_NAME=/qg` 배포의 외부 주소에는 `/qg` 를 앞에 붙인다 (예: `/qg/suites/`, `/qg/api/v2/health/`).

| Prefix | 앱 | 비고 |
|---|---|---|
| `/` | dashboard | Suite 기준 14일 요약 카드 + Chart.js 차트 2행(1행: 일별 stacked bar·Suite Pass Rate hbar / 2행: 도넛 2개 서비스/QG 품질 지표) + 미 장애 판정 카드 + 최근 TestSuiteRun 15건 |
| `/visualization/` | visualization | "Quality Highway / Live Race Control" Canvas 페이지(`name=index`) + Test Suite 실행 피드 JSON(`/visualization/data/`, `name=data`, `?after=<epoch>` 6초 증분 폴링). 모델 없음(읽기 전용 집계) |
| `/accounts/` | accounts | **login (`/accounts/login/`, Keycloak 전용 로그인 페이지) / logout (`/accounts/logout/`, GET·POST — SSO 시 Keycloak 세션 종료, 게이트웨이 뒤에서는 게이트웨이 로그아웃 HTML 이동) / **게이트웨이 진입 (`/accounts/gateway-login/`, `GATEWAY_ENTRY_ENABLED` 일 때 로그인 화면 버튼의 목적지 — 게이트웨이가 이 경로만 보호)** / SSO 시작·콜백 (`/accounts/oidc/<provider>/login/`, `/accounts/oidc/<provider>/callback/` — 현재 provider=`internal`, Keycloak 에 등록할 redirect URI)** / signup (SSO 전용이면 404) / 사용자 관리 (`/accounts/users/`) + **사용자 직접 추가** (`/accounts/users/new/`, 승인 절차 없이 즉시 활성, admin 전용) / Slack 알림 설정 / **포털 설정 (`/accounts/portal-settings/`, 메뉴 표시 토글)** / **담당자 관리 (`/accounts/api-owners/`)** + **Google Sheets 동기화 (`/accounts/api-owners/sync/`, POST)** + **자동 동기화 주기 (`/accounts/api-owners/sync-settings/save/`, POST)** / **호스트 매핑 (`/accounts/api-hosts/`)** / **Slack 사용자 매칭 디버그 (`/accounts/slack-users/`)** / **수동 에러 노티 (`/accounts/api-owners/<pk>/notify-error/`)** / **대체 담당자 (`/accounts/api-owners/fallback/save/`, `/accounts/api-owners/fallback/notify/`)** / **UI 알림 담당자 (`/accounts/api-owners/?tab=ui` 화면, `/accounts/api-owners/ui/save/` POST)** |
| `/qualitygates/` | qualitygates | Folder/SubFolder/Gate/Run, `toggle-auto`, `toggle-parallel`, `rerun`, `rerun-test`, `argocd/<stage>/retry/`, `runs/<pk>/error-log/(image\|deploy\|prod)/` |
| `/suites/` | suites | Test Suite + Queue, `runs/`(Suite 최근 실행 목록, `name=run_list`, **`date` 필터**=일별 막대 드릴다운), **`runs/untriaged/`**(미 장애 판정 큐, `name=untriaged_list`), **`runs/untriaged/bulk-triage/`**(체크박스 선택 일괄 판정 POST, `name=untriaged_bulk_triage`), **`job-runs/`**(Job 결과 목록·도넛 드릴다운, `name=job_run_list`, `result`/`suite` 필터), **`job-runs/<uuid:pk>/triage/`**(판정 POST, `name=job_run_triage`), `job-runs/<pk>/error-log/`, **`<pk>/gwt-tokens/generate/`**(GWT 토큰 일괄 발급, `name=gwt_tokens_generate`), **`jobs/<pk>/gwt-token/regen/`**(워커별 GWT 토큰 재발급, `name=gwt_token_regen`), **`api-sync/`**(API 탭 Jenkins 자동 싱크 POST, admin 전용, `name=suites_api_sync`, 2026-07-28), **UI Test Automation(TAP, 2026-07-28)**: `tap/table/`(HTMX fragment, `name=tap_suite_table`) / `tap/<uuid:pk>/run/`(실행 POST, `can_write`, `name=tap_suite_run`) / `tap-runs/<uuid:pk>/cancel/`(취소 POST, `name=tap_run_cancel`) / `tap-runs/<uuid:pk>/`(Run 상세, `name=tap_run_detail`) |
| `/robustness/` | robustness | Reliability Test |
| `/logs/` | activitylog | 활동 로그 |
| `/admin/` | Django admin | |
| `/api/v1/` | DRF API | 레거시 (구 GateRun 모델) |
| `/api/v2/` | DRF API | **외부 시스템용 REST API** (API Key + Scope). 메타 조회 / Quality Gate Run 조회·트리거·제어 / Test Suite Run 조회·트리거·취소 / 콘솔 로그. Suite Run 트리거: `job_id` 생략 시 idle worker 1개 선택 실행, `run_all=true` 면 전체 순차 체인, 검증된 idle 없으면 409 `all_workers_busy`. 폴링 응답에 합/불 `result`(`passed`/`failed`/`null`) 노출. 내부 설계/정책 의도: `prd_api.md`, **외부 호출자용 단일 가이드(키 발급·시나리오·전체 엔드포인트 reference·에러 카탈로그·호출 예제): `docs/api_guide.md`** (구 `api_v2_quickstart.md` 는 이 문서로 통합·삭제, 2026-06-08) — **Swagger UI: `/api/v2/docs/`** (ReDoc: `/api/v2/redoc/`, raw schema: `/api/v2/schema/`, 세션 로그인 필요, 2026-07-27). API Key 관리는 `/accounts/api-keys/`. **서버 이관용 DB 스냅샷 내보내기**: `POST /api/v2/export/snapshots/`(202) · `GET …/<id>/`(상태·manifest·files) · `GET …/<id>/download/<name>/`(Range 206 지원, `X-Content-SHA256`) · `DELETE …/<id>/`(204) — 스코프 `export:read`(명시 부여 전용, `*` 미포함, admin 소유 키만), config 미포함, 24시간 만료·동시 1개, Swagger 에는 미노출 (`docs/api_guide.md` §4.5, 2026-10-09). **담당자 관리 API (2026-10-21)**: `GET·POST /api/v2/owners/` · `GET·PATCH·PUT·DELETE /api/v2/owners/<id>/` (`/ci-api/owners/` 동일) — 담당자 관리 화면의 API 엔드포인트 담당자(FE/BE) 매핑을 프로그램으로 조회·등록·수정·삭제, 스코프 `owners:manage`(명시 부여 전용, `*` 미포함, admin 소유 키만), `(method,endpoint)` 중복은 409 `already_exists`, Swagger 의 '담당자 관리' 그룹 (`docs/api_guide.md` §4.6). |
| `/ci-api/` | DRF API (키 전용) | **게이트웨이 로그인 예외 경로 (2026-10-12)** — 게이트웨이(nginx)가 SSO 쿠키 없는 요청을 401 HTML 로 막아 `Authorization: Bearer` 호출이 앱에 닿지 못하는 문제를 피하려고, nginx 가 `/qg/ci-api/` **만** 로그인 예외로 둔다(외부 주소 `https://qw.jkintra.net/qg/ci-api/`). `/api/v2/` 의 키 인증 엔드포인트(`health` · `me` · 메타 · Run 조회/트리거/제어 · Suite Run)와 같은 뷰·응답이며, **쿠키와 `X-Auth-*` 헤더는 모두 무시하고 API Key(Bearer 또는 Basic `curl -u x:$KEY` — 비밀번호(없으면 사용자명) 자리의 `qgp_` 키만 인정)로만 인증**한다. 응답의 `self_url`·`console_url` 도 `/ci-api/…` 로 나와 링크를 그대로 따라갈 수 있다. **문서(`schema`/`docs`/`redoc`)와 DB 내보내기(`export`)는 이 경로에 없다(404)** — 로그인 없이 열리는 경로에 둘 수 없어서이며, 문서·export 는 `/api/v2/` 로 접근(Swagger 문서에는 이 `/ci-api/` 경로가 표기됨). 게이트웨이 인증이 없으므로 API Key 인증·scope·Rate limit 이 유일한 방어선이다. |
| `/health/` | health check | `{"status":"ok"}` |
| `/sw.js` | Service Worker | 인라인 서빙, 포털 루트 스코프(`/` 또는 `/qg/`), no-cache |

`gates/` / `services/` 도 `urls.py` 에 등록돼 있으나 레거시.

---

## 10. 운영 메모

- **TAP 스케줄이 Running인데 실행수·트리거가 0인 경우**: TAP 시작 요청이 504/timeout으로 끝나면 TAP에서 실제 실행됐어도 포털이 세션 ID를 받지 못해 집계할 `TapRun`이 없을 수 있다. `maintenance.md`의 "TAP 스케줄 집계가 0" 항목대로 원본 세션·Suite·시각을 대조해 누락 이력을 복구한다. 2026-09-09 JK UI 169케이스 사례는 TAP의 직렬 준비 조회를 병렬화하고 누락 23회(3,887케이스)를 복구했다.
- **★ Reliability Test 는 cron 등록이 필수다.** 스케줄 트리거를 구동하는 별도 스케줄러가 없어, 아래 crontab 한 줄을 **한 번만** 등록해야 설정한 interval 대로 동작한다(미등록 시 화면을 열어둔 동안에만 돌아 간격이 들쭉날쭉해진다). 담당자 동기화 cron 과 같은 crontab 에 추가:

  ```cron
  * * * * * cd /home/taeshkim/qa_qualitygate_portal && flock -n /tmp/qg_reliability_sweep.lock .venv/bin/python manage.py sweep_reliability >> /tmp/qg_reliability_sweep.log 2>&1
  ```

  `sweep_reliability` 가 매분 `sweep_active_schedules(force=True)` 를 돌리고, 각 스케줄은 자기 interval 도래 시에만 1회 트리거된다. 동작 확인: `tail -f /tmp/qg_reliability_sweep.log`. **같은 명령이 단독(비-스케줄) RUNNING Suite Run 도 매분 폴링**한다 — UI/API 로 트리거한 뒤 화면·외부 폴러가 없어도 완료 감지와 PASS/FAIL Slack 알림이 1분 내에 이뤄지고, 완료 시각은 Jenkins 실제 빌드 종료 시각으로 기록돼 폴링이 늦어도 '소요 시간' 이 부풀지 않는다.
- **내부 Jenkins 인증 — GWT + 익명 read 전환(Test Suite / Reliability 한정)** — Keycloak SSO 뒤에 있는 내부 Jenkins 는 `user:api_token` Basic auth 가 SSO 세션 만료 시 거부돼 트리거가 'Jenkins 인증 실패'로 떨어지던 문제를 GWT + 익명 read 방식으로 근본 해결했다. **읽기는 익명**, **트리거는 워커별 GWT 토큰** (§4 "내부 Jenkins 익명 read + GWT 트리거" 참조). `jenkins_keepalive` cron(아래)은 Test Suite / Reliability 경로엔 사실상 불필요해졌다 — Quality Gate 파이프라인 트리거 전환 시 함께 검토 예정.
- **`jenkins_keepalive` cron (Quality Gate 파이프라인용, 선택)** — Quality Gate 의 deploy/image/prod 트리거는 아직 Basic auth 방식이라 SSO 세션 만료가 재현될 수 있다. 완화 목적으로 가벼운 `/api/json` 핑 cron 을 유지한다(등록 여부는 선택):

  ```cron
  * * * * * cd /home/taeshkim/qa_qualitygate_portal && flock -n /tmp/qg_jenkins_keepalive.lock .venv/bin/python manage.py jenkins_keepalive >> /tmp/qg_jenkins_keepalive.log 2>&1
  ```

  동작 확인: `tail -f /tmp/qg_jenkins_keepalive.log`.
- **`/ci-api/` 게이트웨이 로그인 예외 경로 운영 메모 (2026-10-12)** — nginx 예외는 `/qg/ci-api/` **하나만**(문서·UI·export 는 보호 유지). 프로그램(Jenkins/CI)은 `https://qw.jkintra.net/qg/ci-api/…` 로 호출하고 API Key 는 `Authorization: Bearer qgp_...` 또는 Basic(`curl -u x:$KEY`, Jenkins 자격증명) 로 보낸다. 장애 구분: 응답이 **nginx HTML 401** 이면 게이트웨이 예외가 적용되지 않은 것, **JSON `{"error":{"code":"not_authenticated",...}}` 401** 이면 앱까지 도달해 키가 없거나 잘못된 것. 배포 후 확인: `curl -i https://qw.jkintra.net/qg/ci-api/health/` → 200 JSON, `curl -i https://qw.jkintra.net/qg/ci-api/me/` (키 없이) → JSON 401. 아이디/비밀번호 Basic 은 이 경로에서 인증되지 않는다.
- **SSO 로그인 운영 메모** — ① 로그아웃 후에도 Keycloak 세션이 남으면 로그인 화면의 SSO 버튼이 비밀번호 없이 바로 재로그인된다. 공용 PC 에서도 안전하려면 Keycloak client 에 Post logout redirect URI 를 등록하고 `OIDC_INTERNAL_POST_LOGOUT_REDIRECT=True` 로 켠다(게이트웨이 경유는 `GATEWAY_LOGOUT_URL`, 진입 모드는 게이트웨이 로그아웃 호출로 세션 쿠키 제거). ② 기존 계정은 **이메일로만** SSO 계정에 자동 연결된다 — 이메일이 비었거나 회사 도메인이 아닌 계정(`admin@example.com` 류, 예시 도메인 테스트 계정 등)은 연결되지 않고 첫 SSO 로그인 때 **새 `viewer` 계정이 만들어지므로**, 배포 전에 이메일을 정리하거나 첫 로그인 후 역할을 다시 부여한다(절차: `maintenance.md` §7 "SSO 로그인"). ③ `/api/v2/` 는 API Key 가 기본이지만 DRF 설정에 Basic 인증이 남아 있어, 비밀번호가 있는 기존 계정은 SSO 전환 후에도 API 에서만 Basic 으로 인증될 수 있다(SSO 로 만든 계정은 비밀번호 없음). 상세·점검은 `maintenance.md` §7.
- **runserver 는 멀티스레드** 지만 SQLite 쓰기 락 + 외부 API 대기가 겹치면 다른 요청이 밀린다. 막힘이 길어지면 `./run.sh` 로 재기동.
- **외부 호출 타임아웃**: Jenkins 8s, Argo CD 6s. Argo CD 폴링은 4초 쿨다운.
- **파라미터 캐시**: `qgp:` prefix, 120초 LocMem. 모달 새로고침 = `gate_run_params?refresh=1`.
- **`db.sqlite3` 는 Git 에서 제외** (`.gitignore`, GitHub 100MB 제한 초과) — 커밋하지 않는다. 서버 간 DB 이관은 `apps/syncops` 스냅샷 명령(`export_snapshot`/`import_snapshot` 등) 사용.
- **Argo CD 재시도** 버튼은 `*_argocd_synced_at` 가 채워진 뒤에만 뜬다 — 한번이라도 Synced + Healthy 에 도달했어야 노출.
- **OS 알림이 안 뜨면** 우하단 **OS 알림 테스트** 버튼 → 진단 결과 확인.
  - macOS: 시스템 설정 → 알림 → Chrome → "배너" 또는 "경고", 집중 모드 OFF
  - Windows: 설정 → 시스템 → 알림 → Chrome ON, 알림 배너 표시 ON, 방해 금지 OFF
  - Chrome **탭을 완전히 닫으면 알림 안 옴** (워처 JS 가 죽음). 백그라운드/최소화는 OK.
- **`gate_type` 은 최초 생성 후 변경 불가** — 자격증명 매핑이 달라지므로 `gate_edit` 의 POST 값은 무시된다. 변경이 필요하면 새 게이트로 등록.
- **레거시 Slack / Webhooks / DRF API** (`apps/integrations/slack.py`, `webhooks.py`, `api/`) 는 옛 GateRun 모델 기반이라 현 파이프라인과 무관. 신규 파이프라인의 Slack 알림은 `apps/notifications/` 에서 처리.
