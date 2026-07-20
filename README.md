# QA Quality Gate Portal

Jenkins · Argo CD · Test Suite 를 하나의 검수 파이프라인으로 묶어 운영하는 Django 기반 품질 게이트 포털.

운영 도메인: <https://qualitygate.jkintra.net>

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
| 대시보드 | `/` | 7일 요약 카드 + **DORA 4지표 (DF/LT/CFR/MTTR, 30일)** + 최근 실행 (게이트별 누적 통계 컬럼) + 게이트별 현황 |
| Quality Gate 인스턴스 | `/qualitygates/` | 인스턴스(폴더) 목록 |
| 인스턴스 상세 | `/qualitygates/folders/<pk>/` | 하위폴더 카드 목록 + Jenkins/Argo CD 자격증명 관리 진입 |
| 하위폴더 상세 | `/qualitygates/subfolders/<pk>/` | 그 하위폴더에 속한 Quality Gate 목록 |
| 게이트 상세 | `/qualitygates/<pk>/` | Run 시작, auto_mode 토글, 최근 Run 15건 |
| Run 상세 | `/qualitygates/runs/<pk>/` | 5초 폴링되는 단계별 진행 + 콘솔 로그 + Argo CD 상태 |
| Test Suite | `/suites/` | 테스트 묶음 (Jenkins 폴더 단위) |
| Test Suite — Jenkins 자격증명 | `/suites/credentials/` | (admin) 내부 Jenkins · 외부 Jenkins default 자격증명 관리. API token 평문은 비노출 |
| Queue | `/suites/queue/` | 진행 중 빌드 모음 |
| Reliability Test | `/robustness/` | 스케줄 기반 반복 실행 |
| Activity Log | `/logs/` | 등록/실행/취소/재시도 로그 |
| Admin — 사용자 관리 | `/accounts/users/` | 가입 승인 + 권한 부여 |
| Admin — Slack 알림 | `/accounts/notifications/` | 이벤트별 토글 + 채널 설정 + 테스트 메시지 |
| API Keys (본인) | `/accounts/api-keys/` | 외부 호출용 API 키 발급/회수 — 평문은 발급 직후 1회만 노출. **사이드바 메뉴에서는 제거됨 (2026-05-27)** — URL 로만 접근, 메뉴상 API Keys 진입은 Admin 탭에만 노출 |
| API Keys (admin) | `/accounts/api-keys/admin/` | 전체 사용자 키 조회/회수 + **'새 키 발급' 버튼(본인=관리자 키 발급)**. **메뉴상 API Keys 의 유일한 진입점 (Admin → API Keys)** |
| Admin — 담당자 관리 | `/accounts/api-owners/` | API 엔드포인트별 담당자 마스터. **Google Sheets 동기화 (full mirror — 시트가 단일 진실 소스)** / 수동 추가/수정/삭제 / 필터 검색 / CSV 다운로드. 동기화는 **`(Method, Endpoint)` 키로 `update_or_create`** — 같은 endpoint 가 다른 repo 라벨로 들어와도 기존 row 를 덮어쓰기만 하고 새 row 는 만들지 않는다 (2026-05-20). 시트에 없는 imported row 는 삭제, 수동 row(`is_imported=False`) 는 보존. **자동 동기화 주기(분)는 화면 하단에서 설정** (`0`=끔) — 매분 도는 cron `sync_api_owners_from_sheet --if-due` 이 그 주기에 맞춰 실행하고, 화면의 **'Google Sheets 동기화'** 버튼은 수동 즉시 동기화 (CSV 업로드 대체, 2026-05-27). |
| Admin — 호스트 매핑 | `/accounts/api-hosts/` | `${BFF_URL}` 같은 환경변수를 실제 도메인(`bff-general-*.dev.albamon.com`) 과 연결. 매칭의 필수 조건이 아니라 같은 path 가 AM/JK 양쪽에 등록된 경우 disambiguation 용. |

---

## 3. 도메인 모델 요약

| 모델 | 설명 |
|---|---|
| `QualityGateFolder` | **인스턴스** 단위. Jenkins **Dev/Prod 자격증명** 2쌍 + Argo CD **Dev/Stg/Prd 토큰** 3개를 보관. 같은 인스턴스 안의 게이트가 자격증명을 공유. |
| `QualityGateSubFolder` | 인스턴스 안에서 게이트들을 묶는 **하위폴더**. `folder` (인스턴스) + `name` (인스턴스 안에서 unique). 기존 게이트는 마이그레이션 시 자동 생성된 `auth` 하위폴더에 배치. |
| `QualityGate` | 개별 게이트. `subfolder` (하위폴더) + `folder` (인스턴스), `gate_type`(`dev_stg`/`stg_prd`), `auto_mode`, image/deploy/prod 각 Job 경로, Argo CD App URL, `deploy_jenkins_base_url` (입력 우선) 등. |
| `QualityGateRun` | 단일 실행 인스턴스. 단계별 Jenkins build 정보 + Argo CD sync 정보(7개 필드 세트) 보유. `is_running`, `can_rerun_test_only` 등 다수 프로퍼티. |
| `QualityGateRunTest` | Run 안에서 Suite 별 진행 상태(PENDING/WAITING/TESTING/PASSED/FAILED). |
| `TestSuite` / `TestSuiteJob` | Jenkins 폴더 단위 테스트 묶음. `jenkins_base_url` 채우면 외부 Jenkins 도 지원. |
| `RobustnessTest` / `RobustnessSchedule` / `RobustnessRun` | 일정 간격으로 Suite 를 반복 실행하는 스케줄러 (Reliability Test). |
| `ActivityLog` | 주요 행위 추적 (생성/수정/삭제/실행/취소/재시도/로그인). |

---

## 4. 주요 기능

- **4단계 파이프라인** — 이미지 → 검수대상 배포 → 테스트 → 검수통과 배포. 각 단계 선택 가능.
- **`auto_mode` 자동 진행** — Dev→Stg 게이트에서 중간 대기 없이 기본 파라미터로 끝까지 진행.
- **`parallel_test` 테스트 병렬 실행** — 모든 Test Suite 를 동시에 트리거하고, 마지막 Suite 까지 끝나야 다음 단계로 진행. 기본 Off.
- **`rerun_test` / 검수대상 재실행 / 검수통과 재실행** — 실패 단계만 재시도. 이미지/검증배포/테스트 결과는 가능한 한 그대로 재사용.
- **Jenkins 파라미터 모달** — Active Choices / Git Parameter 까지 4가지 경로 (HTML 스크래프, scriptText Active Choices, Git Parameter HTTP, scriptText GitParameter) **병렬** 으로 시도. 결과는 120초 LocMem 캐싱, `?refresh=1` 로 강제 재조회. APP_IMAGE 같은 ImageTag 파라미터는 모달과 자동 트리거(`_auto_trigger_deploy` / `prod_deploy_params`) 양쪽에서 이 Run 의 image_build_number 로 default 오버라이드 — 자동 트리거에서 매칭이 불가능하면 `WAITING_DEPLOY` 로 떨어뜨려 사용자 확인을 요구.
- **Image Build git 정보 표시** — Image Build 단계에 사용된 source repository 의 **branch 명과 commit hash(앞 8자리)** 를 Run 상세 화면에 노출. release/feature/hotfix 어느 브랜치로 빌드했는지 한눈에 확인. Jenkins build_info 의 `actions[BuildData].lastBuiltRevision` 에서 추출하며, Pipeline-from-SCM 빌드처럼 BuildData 가 두 개(Jenkinsfile 저장소 + source 저장소) 들어오는 경우 `image_params_json` 의 값에서 자동 추출한 source repo URL/branch 를 hint 로 넘겨 source 측 BuildData 를 우선 선택. git plugin 정보가 없으면 `GIT_BRANCH`/`BRANCH` 등 트리거 파라미터를 폴백으로 사용. 표시 시 `origin/`, `refs/heads/`, `refs/remotes/origin/` prefix 는 자동 제거.
- **Argo CD 연동** — 배포 Job 성공 후 hard refresh + sync 트리거 → *Synced + Healthy* 도달까지 폴링. Dev/Stg/Prd 토큰 분리, 4초 쿨다운으로 외부 API 보호.
- **Argo CD 상태 배지** — Run 상세 화면의 Argo CD 표시는 sync/health/operation 조합으로 분기:
  *Synced & Healthy* (초록) / *동기화중* / *롤아웃중* / *OutOfSync — 재동기화 대기* (노랑) / *Sync 실패* / *Degraded* / *Missing* (빨강). `Sync 실패` 또는 health=`Degraded`/`Missing` 인 경우 Retry Sync 버튼이 즉시 노출되고, sync 작업이 `Failed`/`Error` 로 종료되면 15분 타임아웃을 기다리지 않고 곧바로 `(PROD_)DEPLOY_FAILED` 로 전환.
- **수동 Argo CD 재시도** — `argocd_retry(run, stage)` 엔드포인트로 sync 상태 리셋 + 즉시 재시도. `DEPLOY_FAILED` / `PROD_DEPLOY_FAILED` 모두 다시 `*_DEPLOYING` 으로 되돌려 폴링 재개.
- **htmx 5초 폴링** — Run 상세 화면이 부분 갱신. 외부 호출은 4초 쿨다운, Jenkins 8s / Argo CD 6s 타임아웃.
- **Error Log 버튼 (실패 단계만 노출)** — 이미지/검수대상 배포/검수통과 배포/Test Suite Job 어느 단계든 실패하면 `Console` 옆에 빨간 **Error Log** 버튼이 자동 노출. 클릭하면 Jenkins 콘솔 로그에서 자동화 스크립트가 마킹한 `@@@...@@@` 블록(또는 pytest `=== FAILURES ===` 섹션, `[FAIL]`/`[STOP]` 라인 주변) 만 추출해 보여준다. 전체 콘솔로 점프하는 *Full Console* 버튼도 같이 노출. 파싱 규칙은 `apps/integrations/log_errors.py`.
- **Error Log 상단의 "이슈 F/U 담당자" 표** — 추출된 에러 위에 콘솔에 등장한 URL / Method / 호출횟수 / Status / **FE 담당자** / **BE 담당자** 표가 자동 노출. URL→담당자 매핑은 `/accounts/api-owners/` 의 Google Sheets 동기화 마스터(`ApiEndpointOwner`) 와 **path-only 정규식 매칭** (호스트는 떼어내고 path 만 비교, `${PARAM}` 자리는 와일드카드). 행의 Status 가 4xx (400~499) 면 FE 담당자 칸이 노랑 음영, 5xx 면 BE 담당자 칸이 빨강 음영으로 강조. 매칭 실패한 URL 은 admin 에게 "+ 등록" 버튼이 노출되어 `?method=...&endpoint=path&host=netloc` 로 prefill 등록 가능. 호스트 매핑(`/accounts/api-hosts/`) 은 같은 path 가 AM/JK 양쪽에 등록된 경우의 disambiguation 용이라 비어있어도 매칭은 동작. **컬럼 변경 이력 (2026-05-20)**: `4xx 담당자` → `FE 담당자`, `5xx 담당자` → `BE 담당자`. `*담당팀` 컬럼은 삭제 (담당자만 관리). 동기화/CSV 다운로드 파서는 옛 헤더 (`400에러 담당자` 등) 도 alias 로 호환.
- **담당자 마스터 Google Sheets 동기화 (CSV 업로드 대체, 2026-05-27)** — `/accounts/api-owners/` 의 담당자 마스터를 Google Sheets 와 동기화한다. 시트 컬럼이 기존 CSV 헤더와 동일해 기존 임포트 파서(`(Method, Endpoint)` 키 `update_or_create`)를 그대로 재사용. **full mirror** — 시트가 단일 진실 소스이며 시트에 없는 imported row 는 삭제, 수동 row(`is_imported=False`)는 보존. 인증은 **서비스 계정 또는 Workload Identity Federation(WIF)** — `.env` 의 `GOOGLE_CREDENTIALS_FILE`(파일 type 자동 판별, 운영은 AWS WIF) 또는 `GOOGLE_SA_JSON_B64` 등에 자격증명 지정, 대상 시트를 호출 주체(SA / 임퍼소네이트 SA)에 '뷰어' 공유, 대상 시트 ID 는 `GSHEET_OWNERS_SPREADSHEET_ID`. 조회는 **Drive API `files.export(text/csv)`** (첫 탭 gid=0). 트리거 = ① 매분 도는 cron `python manage.py sync_api_owners_from_sheet --if-due` — 화면 하단에서 설정한 주기(분, `OwnerSyncSettings`, 0=끔)가 도래했을 때만 실행 ② 화면의 'Google Sheets 동기화' 버튼 (수동 즉시). 모듈: `apps/integrations/google_sheets.py`, 서비스: `services.sync_owners_from_google_sheet`, 주기 설정: `accounts:owner_sync_settings_save`.
- **"에러 노티" 버튼 (Error Log 페이지)** — 이슈 F/U 담당자 표의 각 행 우측에 빨간 종 모양의 **에러 노티** 버튼이 노출된다 (`can_write` 권한 필요). 클릭 → confirm → 해당 URL 의 4xx/5xx 담당자에게 Slack 으로 즉시 알림 발송. 입력값 없이 버튼 한 번이면 끝나고, 메시지에는 ① Slack `users.list` API 로 자동 조회한 `<@U0XXX>` 멘션 (실패 시 `@이름` plain text) ② Suite/Job 또는 Gate/Stage 컨텍스트 라벨 ③ URL/Method/Status ④ 콘솔에서 추출된 에러 발췌 (최대 1500자) ⑤ 포털 에러 로그 페이지 절대 URL 이 포함된다. `ActivityLog (owner_notified)` 에 기록.
- **Slack 자동 에러 알림 — 멘션 + 에러 발췌 + 로그 링크 (Suite/QG Run 실패 시)** — 단독 Test Suite Run 이 FAIL 하거나 파이프라인 내부 Test Suite 가 FAIL 하면 기존 Slack 메시지에 *이슈 F/U* 필드가 자동으로 추가된다. 콘솔에서 자동 추출한 URL 의 FE/BE 담당자 이름을 Slack workspace 의 `users.list` 와 자동 매칭 → `<@U0XXX>` 멘션. 매칭 실패한 이름은 `@이름` plain text 로 노출(알림은 안 가지만 메시지엔 등장). URL/Method/Status 표, 발췌된 에러 본문, 포털 에러 로그 페이지 링크를 한 번에 포함. 단독 Suite Run 의 FAIL 알림에서는 실패 job 중 첫 1건만 followup 으로 첨부 (메시지 길이 보호). 토큰/채널 미설정 등은 silent.
- **대체 담당자 (FallbackOwners)** — Error Log 의 URL 이 `ApiEndpointOwner` 마스터와 매칭되지 않은 경우 자동으로 노티를 받을 담당자. **담당자 관리 화면 (`/accounts/api-owners/`) 하단** 의 콤마 다중값 입력(예: `김태식, 김도연`) 으로 설정 (singleton, `FallbackOwners` 모델). Suite 자동 알림에서 매칭된 owner 가 0건이면 fallback 으로 자동 발송 + 메시지 본문에 "담당자 매칭 없음, 대체 담당자로 지정" 라벨. Error Log 페이지의 매칭 실패 행도 "에러 노티" 버튼이 노출되어 수동 발송 가능. 미설정 상태에서는 매칭 실패 URL 의 알림이 발송되지 않음.
- **Slack 사용자 자동 매칭** — `apps/integrations/slack_users.py:find_user_id_by_name(name)` 가 Slack `users.list` API 로 workspace 사용자 (id / name / real_name / display_name + normalized 변형) 를 LocMem 1시간 캐시에 인덱싱해서 owner 이름을 U_ID 로 변환. SLACK_BOT_TOKEN 에 **`users:read` scope 필수** (Slack 앱 설정 → OAuth & Permissions 에서 추가 후 워크스페이스 재설치 1회). scope 가 없으면 멘션이 평문(`@이름`) 으로만 나가고 알림은 안 간다. 별도 수동 매핑 테이블/어드민 메뉴는 없음 — workspace 멤버 정보가 단일 진실 소스.
- **Slack 매칭 디버그 페이지** — `/accounts/slack-users/` (admin only). 현재 인덱싱된 멤버 수, 마지막 `users.list` 호출 실패 사유 (missing_scope / 인증 오류 등), 이름 검색 (부분 일치 후보 노출), 캐시 강제 갱신 버튼. 자동 매칭이 안 될 때 1) scope/재설치 문제 vs 2) Slack 표시명/실명과 CSV 표기 불일치 를 즉시 구분 가능.
- **Run 일련번호(`seq_no`) + 최신 Run 배너** — 게이트별로 1부터 순증가하는 `#N` 번호를 모든 Run 에 부여. 상세 헤더, 탭 title (`auth-admin #42 - QG Portal`), 게이트 상세의 실행 이력 표, 대시보드 최근 실행에서 모두 `#N` 노출. 같은 application 의 Run 상세 탭을 여러 개 띄웠을 때 보고 있는 화면이 latest 가 아니면 상단에 노란 배너 + "최신 Run 으로 이동" 버튼이 자동 노출돼 휴먼 에러 방지.
- **OS 알림 + 글로벌 워처** — Service Worker 기반 데스크톱 알림. 실행 중 Run 은 `localStorage` 워처로 등록되어 **페이지 이동/새로고침해도** 5초 주기로 추적. 터미널 상태 도달 시 토스트 + 삐 + OS 알림. 사이드바 ON/OFF 토글, 우하단 진단 버튼/가이드 제공.
- **Slack 알림** — Quality Gate 파이프라인과 단독 Test Suite Run 양쪽 모두 상태 전이 시점에 Slack 채널로 알림 발송. Run 시작/종료, 각 단계(image/deploy/test/prod) 시작·종료, 파이프라인 내부 Test Suite, **단독 Suite Run (시작/PASS/FAIL)**, Argo CD Sync 실패 등 20개 이벤트를 어드민 화면에서 개별 토글. 파이프라인 내부 Suite (`TEST_SUITE_*`) 와 단독 Suite Run (`SUITE_RUN_*`) 토글은 분리돼 있어 각각 끄고 켤 수 있다. Bot 토큰은 `.env` 의 `SLACK_BOT_TOKEN`, 기본 채널은 어드민 화면(또는 `.env` 의 `SLACK_DEFAULT_CHANNEL`) 에서 관리. Run 상세 링크를 메시지에 포함하려면 `QGP_BASE_URL` 도 채울 것. 발송은 데몬 스레드로 비동기 처리되어 폴링 흐름을 막지 않으며, 토큰/채널 미설정 시 silent skip.
- **3단계 권한 (RolePermissionMiddleware)**
  - Admin (`qpe_admin` / superuser): 전부 허용
  - 일반 역할: 실행/시작/중단/재시도/상태 폴링 등 operational POST 만 허용
  - Viewer: 모든 POST 차단
- **회원가입 승인 대기** — `is_active=False` 로 가입 → 관리자가 승인 + 역할 부여.
- **Reliability Test (Robustness)** — Suite 별로 N분 간격 반복 트리거. HTMX 폴링 시점에 `sweep_active_schedules()` 가 돌아 트리거/상태 갱신. 테스트별 **잡 병렬 실행** 마스터 토글 (스케줄 목록의 *일괄 시작* 옆) — OFF (기본) 면 모든 스케줄이 Suite 의 잡을 한 잡씩 순차 실행, ON 이면 한 번 트리거 시 Suite 의 모든 잡을 동시에 트리거. 토글은 그 테스트에 속한 모든 스케줄에 일괄 적용. *일괄 시작/중지* 옆 **일괄 초기화** 버튼은 그 테스트의 모든 스케줄의 세션 통계(성공/실패/실행수/세션 시작/마지막 실행)를 빈 상태로 리셋한다 — `started_at` 까지 `None` 으로 비워 "세션 시작" 칼럼도 함께 비워지고, running 인 스케줄은 다음 트리거 시점에 `started_at` 이 자동으로 다시 채워져 카운트가 재개된다. `is_running` 은 손대지 않으므로 멈춰 있는 스케줄을 reset 한다고 자동 시작되지 않는다. interval 도달 시점에 이전 사이클이 끝나지 않았다면 **미완료 잡(PENDING/QUEUED/RUNNING)을 FAILURE 로 마감**하고 Jenkins 측 빌드는 `stop_build` 로 종료한 뒤 새 사이클로 진행한다 — 즉, 60분 interval 인데 이전 잡이 60분 안에 안 끝나면 그 잡은 실패로 처리되고 다음 사이클이 시작된다.
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
  dashboard/      # 7일 요약 + DORA 4지표 대시보드
  integrations/
    jenkins.py    # JenkinsClient (Active Choices/Git Parameter 4-path)
    argocd.py     # ArgoCDClient (appNamespace 규약)
    slack.py / webhooks.py / tasks.py  ← 레거시
  api/            # DRF API (구 모델 기반, 신 파이프라인과 무관)
  gates/, services/, approvals/, escalations/  ← 레거시 placeholder

config/
  settings/
    base.py        # 공통 (PostgreSQL/Redis 기본값은 사용 안 함)
    dev.py         # SQLite + LocMem + EAGER celery — 운영도 이걸 사용
    production.py  # 미사용
  urls.py          # /sw.js 인라인 서빙, /health/, 앱 include
  celery.py / wsgi.py

static/js/qg_notify.js   # 토스트 + OS 알림 + 글로벌 Run 워처
templates/               # Django 템플릿 (base.html 에 알림 인프라 포함)
```

파이프라인 진행 로직은 모두 `apps/qualitygates/views.py` 에 모여 있다 — `_poll_gate_run`, `_poll_image_build`, `_poll_deploy`, `_check_argocd_sync`, `_advance_test`, `_poll_current_test`, `_poll_prod_deploy`, `_finalize_run`, `_should_auto_advance`, `_auto_trigger_deploy`, `_auto_trigger_prod`, `argocd_retry`, `rerun_test`, `cancel_run`.

---

## 6. 실행 방법

### Linux / macOS

```bash
./run.sh
```

### Windows

```powershell
./run.ps1
```

`run.sh` / `run.ps1` / `run.bat` 는 실행 시마다 **먼저 `origin/master` 를 fast-forward(ff-only)로 당겨와 코드를 최신화**한다 (현재 브랜치가 `master` 가 아니거나 git 미설치/네트워크 실패/ff 불가 시엔 경고만 띄우고 현재 코드로 계속). **로컬 `db.sqlite3` 는 스크립트가 절대 건드리지 않는다** — ff-only 라 백업/복원/checkout 없이 코드만 갱신하며, 원격이 `db.sqlite3` 를 바꾼 커밋이면 로컬 변경과 충돌해 ff 가 막히고 그 회차 동기화를 건너뛴다. (DB 갱신은 수동 명령어로만)
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

접속 주소
- Portal: <http://localhost:8601>
- Admin: <http://localhost:8601/admin/>
- Suites: <http://localhost:8601/suites/>

---

## 7. 환경

| 항목 | 값 |
|---|---|
| Python | 3.10+ |
| Django | 4.2.x |
| DB | SQLite (`db.sqlite3`, Git 에 포함) |
| 캐시 | LocMem (`qgp:` prefix, Jenkins 파라미터 120초) |
| Celery | `TASK_ALWAYS_EAGER=True` (실질 미사용) |
| 서버 | `runserver 0.0.0.0:8601`, ALB(HTTPS) 뒤 |
| 시간대 / 언어 | Asia/Seoul / ko-kr |
| venv | `/home/taeshkim/qa_qualitygate_portal/.venv` |

`base.py` 에 PostgreSQL/Redis 기본값이 남아 있으나 실제 운영은 `dev.py` (SQLite + LocMem + eager celery) 기준으로 동작한다. ALB(HTTPS) → EC2(HTTP) 프록시 구조라 `CSRF_TRUSTED_ORIGINS`, `SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST` 가 세팅돼 있다.

---

## 8. 환경 변수

`.env.example` 을 복사해 `.env` 에 채운다. **현재 실제 사용되는 항목은 다음뿐이다:**

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_SETTINGS_MODULE=config.settings.dev`
- `DJANGO_CSRF_TRUSTED_ORIGINS` (기본 `https://qualitygate.jkintra.net`)
- (선택) `JENKINS_DEFAULT_URL` / `JENKINS_DEFAULT_USER` / `JENKINS_DEFAULT_API_TOKEN` — Test Suite 가 외부 Jenkins 자격증명을 안 채웠을 때의 **2차 폴백**. **1차는 `/suites/credentials/` 에 저장된 내부 자격증명** 이고, 그게 비어있을 때 이 환경변수가 사용된다.
- (선택) `SLACK_BOT_TOKEN` — Slack 알림 사용 시 필수. `chat:write` scope. 봇이 알림 채널에 초대돼 있어야 함. **에러 노티의 담당자 자동 멘션을 사용하려면 `users:read` scope 도 함께 부여** (workspace 사용자 검색용).
- (선택) `SLACK_DEFAULT_CHANNEL` — 어드민 화면의 채널이 비어있을 때 폴백 (예: `#qg-alerts`)
- (선택) `QGP_BASE_URL` — Slack 메시지에 Run 상세 링크를 포함하려면 운영 도메인으로 설정 (예: `https://qualitygate.jkintra.net`)
- **담당자 Google Sheets 동기화** (담당자 관리 화면 사용 시 필수):
  - `GOOGLE_CREDENTIALS_FILE` — 자격증명 파일 경로 (**현재 운영 방식**). 파일의 `type` 으로 종류를 자동 판별 — `service_account` / `authorized_user` / `external_account`(=Workload Identity Federation). 운영 서버는 AWS WIF(EC2 인스턴스 메타데이터 → STS → SA 임퍼소네이션)를 사용하며 키 파일이 아니라 페더레이션 설정만 둔다.
  - (대안) `GOOGLE_SA_JSON_B64` — 서비스 계정 JSON 키를 base64 인코딩한 값. 대안: `GOOGLE_SA_JSON` (JSON 원문 한 줄) 또는 `GOOGLE_SA_FILE` (SA 키 파일 경로). `GOOGLE_CREDENTIALS_FILE` 가 설정돼 있으면 그쪽이 우선.
  - `GSHEET_OWNERS_SPREADSHEET_ID` — 동기화 대상 스프레드시트 ID (URL 의 `/d/<ID>/`).
  - 사전 준비: **Drive API 활성화**, 대상 시트를 **호출 주체(principal)에 '뷰어'로 공유** — 서비스 계정이면 `...@....iam.gserviceaccount.com`, WIF 면 임퍼소네이트하는 서비스 계정 이메일(`service_account_impersonation_url` 에 박힌 주소, 담당자 관리 화면 상단에도 안내됨).

`.env.example` 에 보이는 PostgreSQL / Redis / TAS / Encryption / 그리고 레거시 Slack(`SLACK_SIGNING_SECRET`, `SLACK_QG_ALERTS_CHANNEL`, `SLACK_QPE_OPS_CHANNEL`) 변수들은 **레거시이며 동작에 영향 없다**.

Quality Gate 의 Jenkins/Argo CD 자격증명은 환경변수가 아니라 **인스턴스 등록 화면** 에서 입력한다.

---

## 9. URL 라우팅

| Prefix | 앱 | 비고 |
|---|---|---|
| `/` | dashboard | 7일 요약 + DORA 4지표(30일) + 최근 실행(게이트별 누적 통계) |
| `/accounts/` | accounts | login / signup / 사용자 관리 / Slack 알림 설정 / **담당자 관리 (`/accounts/api-owners/`)** + **Google Sheets 동기화 (`/accounts/api-owners/sync/`, POST)** + **자동 동기화 주기 (`/accounts/api-owners/sync-settings/save/`, POST)** / **호스트 매핑 (`/accounts/api-hosts/`)** / **Slack 사용자 매칭 디버그 (`/accounts/slack-users/`)** / **수동 에러 노티 (`/accounts/api-owners/<pk>/notify-error/`)** / **대체 담당자 (`/accounts/api-owners/fallback/save/`, `/notify/`)** |
| `/qualitygates/` | qualitygates | Folder/SubFolder/Gate/Run, `toggle-auto`, `toggle-parallel`, `rerun`, `rerun-test`, `argocd/<stage>/retry/`, `runs/<pk>/error-log/(image\|deploy\|prod)/` |
| `/suites/` | suites | Test Suite + Queue, `job-runs/<pk>/error-log/` |
| `/robustness/` | robustness | Reliability Test |
| `/logs/` | activitylog | 활동 로그 |
| `/admin/` | Django admin | |
| `/api/v1/` | DRF API | 레거시 (구 GateRun 모델) |
| `/api/v2/` | DRF API | **외부 시스템용 REST API** (API Key + Scope). 메타 조회 / Quality Gate Run 조회·트리거·제어 / Test Suite Run 조회·트리거·취소 / 콘솔 로그. 명세: `prd_api.md`, 외부 호출자용 가이드: `docs/api_v2_quickstart.md`. API Key 관리는 `/accounts/api-keys/`. |
| `/health/` | health check | `{"status":"ok"}` |
| `/sw.js` | Service Worker | 인라인 서빙, 루트 스코프, no-cache |

`gates/` / `services/` 도 `urls.py` 에 등록돼 있으나 레거시.

---

## 10. 운영 메모

- **runserver 는 멀티스레드** 지만 SQLite 쓰기 락 + 외부 API 대기가 겹치면 다른 요청이 밀린다. 막힘이 길어지면 `./run.sh` 로 재기동.
- **외부 호출 타임아웃**: Jenkins 8s, Argo CD 6s. Argo CD 폴링은 4초 쿨다운.
- **파라미터 캐시**: `qgp:` prefix, 120초 LocMem. 모달 새로고침 = `gate_run_params?refresh=1`.
- **`db.sqlite3` 는 Git 에 포함** — 충돌 시 항상 **로컬본을 최신으로** 유지. (운영 데이터 손실 방지)
- **Argo CD 재시도** 버튼은 `*_argocd_synced_at` 가 채워진 뒤에만 뜬다 — 한번이라도 Synced + Healthy 에 도달했어야 노출.
- **OS 알림이 안 뜨면** 우하단 **OS 알림 테스트** 버튼 → 진단 결과 확인.
  - macOS: 시스템 설정 → 알림 → Chrome → "배너" 또는 "경고", 집중 모드 OFF
  - Windows: 설정 → 시스템 → 알림 → Chrome ON, 알림 배너 표시 ON, 방해 금지 OFF
  - Chrome **탭을 완전히 닫으면 알림 안 옴** (워처 JS 가 죽음). 백그라운드/최소화는 OK.
- **`gate_type` 은 최초 생성 후 변경 불가** — 자격증명 매핑이 달라지므로 `gate_edit` 의 POST 값은 무시된다. 변경이 필요하면 새 게이트로 등록.
- **레거시 Slack / Webhooks / DRF API** (`apps/integrations/slack.py`, `webhooks.py`, `api/`) 는 옛 GateRun 모델 기반이라 현 파이프라인과 무관. 신규 파이프라인의 Slack 알림은 `apps/notifications/` 에서 처리.
