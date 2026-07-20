# 유지보수 가이드 (maintenance.md)

이 문서는 현재 운영 중인 QA Quality Gate Portal 의 **현 시점 구현**을 기준으로
"어디를 고쳐야 어떤 동작이 바뀌는지" 를 빠르게 찾아볼 수 있도록 정리한 것이다.
이 코드 외부에 있는 정책/설계 의도는 README.md 와 PRD.md 를 참고할 것.

Jenkins 안에서 실행되는 LLM 브라우저 자동화 러너의 프롬프트/액션 코드는 이 저장소에
포함되어 있지 않다. 다만 콘솔 로그 분석 보강은 `apps/suites/diagnostics.py` 에 있고,
러너 system/developer context 에 포함할 정책 문서는 `docs/llm_browser_automation_policy.md`
에 둔다. C17508 같은 chrome-devtools-mcp 실패는 이 문서를 프롬프트 컨텍스트에 포함해
UID 재사용, styled checkbox, 팝업 선택, 완료 페이지 검증 규칙을 강제한다.

---

## 1. 한눈에 보는 구조

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (htmx 5s 폴링 + Service Worker OS 알림)             │
└─────────────────────────────────────────────────────────────┘
            │ /qualitygates/runs/<pk>/status/  (5초)
            ▼
┌─────────────────────────────────────────────────────────────┐
│  Django (runserver 0.0.0.0:8601, ALB → HTTPS)               │
│  apps/qualitygates/views.py                                 │
│   └─ _poll_gate_run() ─┬─ _poll_image_build  ─┐             │
│                        ├─ _poll_deploy        │  외부 호출    │
│                        ├─ _check_argocd_sync  │  (4s 쿨다운) │
│                        ├─ _advance_test       │             │
│                        ├─ _poll_current_test  │             │
│                        └─ _poll_prod_deploy  ─┘             │
└────────────┬───────────────────────────────────┬────────────┘
             ▼                                   ▼
       Jenkins API                          Argo CD API
   (apps/integrations/                  (apps/integrations/
       jenkins.py)                          argocd.py)
```

- **DB**: SQLite (`db.sqlite3`, Git 에 포함)
- **캐시**: LocMemCache (Jenkins 파라미터 120초 캐싱용)
- **Celery**: `TASK_ALWAYS_EAGER=True` — 사실상 동기 실행. 폴링은 HTTP 요청 흐름에서 발생.
- **시간대**: Asia/Seoul, **언어**: ko-kr

---

## 2. 파이프라인 상태 기계

### 2.1 상태 정의

`apps/qualitygates/models.py:235` `QualityGateRun.Status`

| Status | 의미 | 다음 단계 트리거 |
|---|---|---|
| `pending` | Run 생성 직후 (즉시 다음으로 전이) | `gate_run` 뷰가 동기로 트리거 |
| `building_image` | 이미지 Job 빌드 중 | `_poll_image_build` |
| `image_build_failed` | terminal | — |
| `waiting_deploy` | 이미지 성공, 사용자 입력 대기 | `deploy_trigger` (사용자 클릭) |
| `deploying` | 배포 Job 빌드 중 + Argo CD sync 대기 | `_poll_deploy` |
| `deploy_failed` | terminal | — |
| `waiting_test` | 모든 테스트 Job 이 busy 라 대기 | `_advance_test` (다음 폴링) |
| `testing` | 테스트 Job 진행 중 | `_poll_current_test` |
| `qg_passed` | 테스트 PASS, 검수통과 배포 대기 | `prod_deploy` (사용자 클릭) |
| `failed` | 테스트 단계 실패 (rerun-test 가능) | — |
| `prod_deploying` | 검수통과 배포 + Argo CD | `_poll_prod_deploy` |
| `prod_deploy_failed` | terminal (rerun-prod-only 가능) | — |
| `completed` | terminal (성공) | — |
| `cancelled` | terminal (사용자 취소) | — |

상태 그룹 상수도 같은 파일에 있다 (`apps/qualitygates/models.py:252-258`):
- `WAITING_USER_STATUSES = ("waiting_deploy", "qg_passed")` — 새 Run 시작 시 자동 CANCELLED 처리
- `ACTIVE_STATUSES = (pending, building_image, deploying, waiting_test, testing, prod_deploying)` — 동시 Run 차단

### 2.2 상태 전이 흐름

#### 일반 모드 (`auto_mode=off` 또는 Stg→Prd)

```
[gate_run]
   │
   ▼
[pending] ──(image Job 있음)──▶ [building_image]
   │                                  │
   │                                  ▼ _poll_image_build
   │                          [waiting_deploy]  ◀─ 사용자 입력 대기
   │                                  │
   │                                  ▼ deploy_trigger
   └──(image Job 없음)──▶ [deploying]
                              │
                              ▼ _poll_deploy → _check_argocd_sync(deploy)
                              [waiting_test] ──▶ _advance_test
                                  │
                                  ▼
                              [testing] ──▶ _poll_current_test
                                  │
                              ┌───┴────┐
                              ▼        ▼
                         [completed]  [failed]    (prod 없음 / 테스트 실패)
                                  ▼
                              [qg_passed] ◀─ 사용자 입력 대기
                                  │
                                  ▼ prod_deploy
                              [prod_deploying]
                                  │
                                  ▼ _poll_prod_deploy → _check_argocd_sync(prod)
                              [completed]
```

#### 자동 모드 (`auto_mode=on`, **Dev→Stg 한정**)

`waiting_deploy` 와 `qg_passed` 단계를 건너뛰고 **기본 파라미터** 로 다음 단계를 자동 트리거.
- 진입 함수: `_should_auto_advance(gate)` (`apps/qualitygates/views.py:1017`)
- 트리거: `_auto_trigger_deploy` / `_auto_trigger_prod`
- 기본값 수집: `_collect_default_params(client, job_path, *, prefer_release_branch=False)` (`apps/qualitygates/views.py:1022`)
  - boolean → `"true"` / `"false"`
  - choices → default 가 choices 안에 있으면 default, 아니면 첫 choice
  - 그 외 → default 문자열
  - **이 규칙을 어기면 Git Parameter 같이 default 가 실제 브랜치 목록에 없을 때 모달이 보내던 값과 달라져 체크아웃이 실패한다.**
  - **`prefer_release_branch=True`** (auto_mode 진입 — `gate_run` / `_auto_trigger_deploy` / `_auto_trigger_prod`) 일 때, Jenkins type 에 `git` 이 포함된 파라미터의 choices 에서 `release/release_*` 패턴 (`_pick_release_branch`, `_RELEASE_BRANCH_RE`) 으로 매칭되는 항목을 default 대신 사용. `origin/`, `refs/remotes/origin/` prefix 도 인식하며, 여러 매칭이면 사전순 가장 큰 것을 선택. 매칭 없으면 일반 규칙으로 fallback. 운영의 정기 배포가 매번 `release/release_MMDD` 단일 브랜치를 새로 만드는 컨벤션에 맞춘 자동 선택.
  - **String 타입 git ref 파라미터(예: `DEPLOYER_REFS=refs/heads/master`)는 의도적으로 손대지 않는다.** 보통 Pipeline-from-SCM 의 Jenkinsfile 저장소 ref 를 가리키는데, Jenkinsfile 저장소엔 release branch 가 없는 경우가 많아 자동 치환 시 `fatal: couldn't find remote ref refs/heads/release/release_MMDD` 로 fetch 실패 (Jenkins 콘솔의 `CpsScmFlowDefinition.create` 단계). Jenkinsfile 자체는 base branch(보통 master) 에서 가져오게 두고, 그 안의 source 체크아웃은 BRANCH_NAME 파라미터(release 로 자동 선택됨) 를 받도록 둔다. — 한 번 String 도 자동 치환했다가 사고남(2026-05-04 Run #186), 롤백 이력.
  - **빈 문자열은 dict 에 담지 않는다.** ImageTagParameter / Active Choice 처럼 정적 default 가 비어 있고 choices 도 못 긁은 경우 빈 값을 명시 전송하면 deploy 스크립트가 `image: registry/foo:` 같은 깨진 매니페스트를 푸시한다 (예: `internal-auth` STG, Argo CD `yaml: line N: mapping values are not allowed in this context` 에러).
  - **반대로 빈 값이라고 그냥 빼버리면** Jenkinsfile 이 `params.X` 가 아닌 `X` 형태로 변수를 직접 참조할 때 `groovy.lang.MissingPropertyException: No such property: APP_IMAGE for class: WorkflowScript` 로 즉시 실패한다 — Jenkins 의 `buildWithParameters` 는 빠진 파라미터를 plugin 런타임 default 로 채워주지 않고 그냥 언바인딩 상태로 둔다. 따라서 ImageTagParameter 처럼 필수인 파라미터는 호출 측에서 default 를 채워서 보내야 한다 — `_auto_trigger_deploy` 와 `_auto_trigger_prod` 가 모두 `run.image_build_number-` prefix 로 매칭된 choice 를 강제로 default 로 오버라이드하는 이유. 그래도 매칭이 안 되면 (choices 가 비어 있는 경우) 자동 트리거는 보류하고 `_auto_trigger_deploy` 는 `WAITING_DEPLOY`, `_auto_trigger_prod` 는 `QG_PASSED` 로 떨어뜨려 사용자가 모달에서 직접 선택하게 한다. **2026-05-11 사고** — `_auto_trigger_prod` 에 이 안전망이 없어 자동 모드 게이트(`public-auth`)의 prod 단계가 APP_IMAGE 누락으로 `MissingPropertyException` 즉사한 케이스. deploy 측 안전망을 prod 에도 대칭으로 복제해 해소.

### 2.3 폴링 구조

`run_detail` / `run_status_fragment` 가 `_poll_gate_run(run)` 을 호출.
htmx 가 **5초 주기로 status fragment 를 GET** 하기 때문에 폴링은 자연히 5초마다 발생.

서버 부하 보호:
- **Argo CD 호출 쿨다운 4초**: `_ARGOCD_POLL_COOLDOWN_SECS` (`apps/qualitygates/views.py:1171`)
- **Jenkins 타임아웃 8초** (요청별, `apps/integrations/jenkins.py:55`)
- **Argo CD 타임아웃 6초** (`apps/integrations/argocd.py:63`)

Argo CD sync 데드라인:
- **`_ARGOCD_SYNC_TIMEOUT_SECS = 15분`** (`apps/qualitygates/views.py`).
  첫 sync 요청 후 15분 안에 Synced+Healthy 도달 못 하면 `_check_argocd_sync` 가 Run 을
  `(PROD_)DEPLOY_FAILED` 로 전환하고 `*_argocd_sync_error` 에 마지막 상태/Argo 메시지를
  남긴다. 매니페스트 깨짐 등으로 Argo 가 영원히 sync 못 가는 Run 이 "Prod Deploying"
  으로 무한히 남는 사고 방지.

페이지를 닫아도 추적이 유지되도록 브라우저 측에 **글로벌 워처** 가 있다 — 자세한 건 §6.

---

## 3. 도메인 모델

### `QualityGateFolder` — 인스턴스 (`apps/qualitygates/models.py:6`)

자격증명을 묶어 게이트가 공유하는 단위.

| 필드 | 용도 |
|---|---|
| `dev_jenkins_*`, `prod_jenkins_*` | Jenkins URL/User/Token 2쌍 |
| `dev_argocd_token` / `stg_argocd_token` / `prd_argocd_token` | Argo CD 환경별 토큰 (3개 분리) |
| `argocd_token_for(env)` | env=`dev`/`stg`/`prd` 에 맞는 토큰 반환 |

related_name:
- `folder.subfolders` → 하위폴더 목록 (UI 진입 경로)
- `folder.gates` → 인스턴스 직속 Gate 전체 (subfolder 경로와 별개로 평탄화 조회용)

### `QualityGateSubFolder` — 하위폴더 (`apps/qualitygates/models.py` 의 SubFolder 블록)

인스턴스 안에서 게이트를 분류하는 폴더. 화면 흐름: `folder_detail` (하위폴더 카드) → `subfolder_detail` (Gate 목록) → `gate_detail`.

| 필드 | 용도 |
|---|---|
| `folder` | 소속 인스턴스 (FK, related_name `subfolders`) |
| `name` | 하위폴더명. (folder, name) `unique_together` |
| `description` | 설명 (선택) |
| `gate_count` | `self.gates.filter(is_active=True).count()` |

마이그레이션 `0017_qualitygatesubfolder` 가 모든 인스턴스에 `auth` 하위폴더를 자동 생성하고 기존 Gate 들을 그 안으로 이동시킨다.

### `QualityGate` — 게이트 (`apps/qualitygates/models.py:50`)

| 필드 | 비고 |
|---|---|
| `subfolder` | 소속 하위폴더 (FK, related_name `gates`). 신규 등록은 `subfolder_pk` 경로로만 가능. `_gate_log_label(gate)` 가 `인스턴스/하위폴더/게이트명` 라벨을 만든다. |
| `folder` | 인스턴스 (FK, related_name `gates`). `subfolder.folder` 와 같은 값. 자격증명 접근(`gate.folder.dev_jenkins_url` 등)이 이 필드를 통하므로 그대로 유지. |
| `gate_type` | `dev_stg` 또는 `stg_prd`. **최초 생성 후 변경 불가** — 자격증명 매핑이 달라지므로. `gate_edit` 의 POST 값은 무시 (`views.py:382`) |
| `auto_mode` | Dev→Stg 에서만 의미 있음. 토글은 Dev→Stg 게이트에만 노출 |
| `parallel_test` | True 면 검증 배포 완료 후 모든 Test Suite 를 동시 트리거. 마지막 Suite 까지 모두 끝나야 다음 단계로 진행 (실패 한 개라도 있으면 모두 끝난 뒤 `FAILED`). 게이트 타입 무관. 기본 False. |
| `image_job_path` | (선택) 이미지 빌드 Job — 없으면 1단계 건너뜀 |
| `deploy_job_path` | 검수대상 배포 Job (필수) |
| `deploy_jenkins_base_url` | 입력값 우선, 없으면 folder 측 URL 폴백 |
| `deploy_credential` | `dev` / `prod` (게이트 타입에 따라 자동 결정 — `_credentials_for_gate_type`) |
| `deploy_argocd_app_url` | 비어있으면 Argo CD 체크 건너뜀 |
| `deploy_argocd_env` | `dev`/`stg`/`prd` (게이트 타입에 따라 자동 결정 — `_argocd_envs_for_gate_type`) |
| `prod_*` | 검수통과 배포 (선택). 비어있으면 테스트 PASS 시 바로 `completed` |

게이트 타입별 자격증명/Argo CD 환경 매핑 (`apps/qualitygates/views.py:25-36`):

| `gate_type` | `deploy_credential` | `prod_credential` | `deploy_argocd_env` | `prod_argocd_env` |
|---|---|---|---|---|
| `dev_stg` | `dev` | `dev` | `dev` | `stg` |
| `stg_prd` | `dev` | `prod` | `stg` | `prd` |

### Run 일련번호 (`seq_no`)

게이트별 1부터 순증가하는 `PositiveIntegerField`. `unique_together(gate, seq_no)` (단 `seq_no > 0` 조건부 — 마이그레이션 도중 기본값 0 인 임시 상태 허용).

- 부여: `apps/qualitygates/views.py:gate_run` 에서 `transaction.atomic` 안에 `aggregate(Max('seq_no')) + 1` 로 채움.
- 표시: 상세 헤더(`<h1>{{ gate.name }} <span>#{{ run.seq_no }}</span></h1>`), 브라우저 탭 title, 게이트 상세 실행 이력 표(`#` 컬럼), 대시보드 `최근 Quality Gate 실행 내역`.
- 더 최신 Run 이 있으면 `templates/qualitygates/run_detail.html` 의 `{% with newer=run.newer_run_for_gate %}` 분기에서 노란 `alert-warning` 배너 + 최신 Run 으로 이동 링크 노출. 모델 프로퍼티는 `apps/qualitygates/models.py:QualityGateRun.newer_run_for_gate` / `is_latest_for_gate`.
- 마이그레이션: `0020_qualitygaterun_seq_no_and_more` — 컬럼 추가 → RunPython 으로 게이트별 created_at 순 1..N 백필 → constraint 추가 순.

### `QualityGateRun` — 실행 인스턴스 (`apps/qualitygates/models.py:234`)

각 단계마다 **빌드 정보 5개 필드 세트** + **Argo CD sync 정보 7개 필드 세트** 를 가진다:

```
{stage}_build_number / _build_url / _duration_ms / _stages_json / _current_stage
{stage}_console_output / _started_at / _completed_at
{stage}_argocd_sync_status / _health_status / _synced_at / _checked_at
{stage}_argocd_sync_requested_at / _operation_phase / _operation_message / _sync_error
```

`stage ∈ {image, deploy, prod}`. 이미지 단계에는 Argo CD 필드가 없다.

추가로 image 단계만 git 정보를 보관:

```
image_git_branch (200자) / image_git_commit (40자, full SHA1)
```

`_poll_image_build` 가 Jenkins build_info 의 `actions[BuildData].lastBuiltRevision` 에서 채워 넣고, 없으면 `actions[ParametersAction]` 의 `GIT_BRANCH`/`BRANCH`/`BRANCH_NAME`/`GIT_TAG`/`TAG`/`REVISION` 파라미터로 폴백. `origin/` 접두어는 자동 제거. 화면에서는 commit 의 앞 8자리만 모노스페이스로 노출 (`templates/qualitygates/_run_status.html`). 추출 헬퍼는 `apps/integrations/jenkins.py:_extract_git_info`.

주요 프로퍼티 (`apps/qualitygates/models.py:327-399`):
- `is_terminal` / `is_running` / `is_success` / `is_failed`
- `can_deploy_now` (= `WAITING_DEPLOY`)
- `can_prod_deploy` (= `QG_PASSED` and prod 설정됨)
- `can_rerun` (= `FAILED`/`PROD_DEPLOY_FAILED`/`IMAGE_BUILD_FAILED`/`DEPLOY_FAILED`) ← "처음부터 재실행" 카드 노출 조건
- `can_rerun_test_only` (= `FAILED` and `deploy_completed_at` 있음) ← rerun-test 버튼 노출 조건
- `can_rerun_deploy_only` (= `DEPLOY_FAILED`) ← 검수대상 배포만 재시도
- `can_rerun_prod_only` (= `PROD_DEPLOY_FAILED` and prod 설정됨) ← prod 단독 재시도

### `QualityGateRunTest` — Suite 별 테스트 결과 (`apps/qualitygates/models.py:402`)

`gate_run` × `gate_suite` (n:m 중간 테이블에 상태 추가). `current_suite_index` 가 가리키는 항목이 현재 진행 중.

### `ApiEndpointOwner` — API 엔드포인트별 담당자 (`apps/integrations/models.py`)

**컬럼 변경 (2026-05-20)**: `error_4xx_owner` → `fe_owner`, `error_5xx_owner` → `be_owner`. `error_4xx_team` / `error_5xx_team` 컬럼은 **삭제** (담당자만 관리). 비즈니스 룰: Status 가 4 로 시작 → FE 담당자, 5 로 시작 → BE 담당자. 마이그레이션 `0006_rename_owner_fields` — RenameField 로 owner 값 보존 + team RemoveField. CSV 임포트는 옛 헤더 (`400에러 담당자` / `500에러 담당자` / `*담당팀`) 도 alias 로 받음 — `*담당팀` 은 들어와도 무시되고 본문은 새 헤더로 normalize.

### `FallbackOwners` — API Endpoint 매칭 실패 시 대체 담당자 (`apps/integrations/models.py`)

`ApiEndpointOwner` 와 매칭되지 않는 URL 의 에러 노티 발송 시점에 사용되는 **singleton** 모델. 마이그레이션 `0007_fallback_owners`.

| 필드 | 비고 |
|---|---|
| `owners` | 콤마 다중값 (예: `김태식, 김도연`). max_length=500. |
| `updated_at` | 자동 |

조회: `FallbackOwners.get_solo()` 가 단일 row 보장 (없으면 자동 생성). `instance.names()` 가 콤마 split + trim.

발송 흐름:
- **자동** — `apps/notifications/services.py:_build_followup_for_job_run` 이 owner=None 만 매칭된 경우 (또는 매칭된 owner 가 0건) `FallbackOwners` 사용. 본문 라벨에 "담당자 매칭 없음, 대체 담당자로 지정" 추가.
- **수동** — Error Log 페이지의 매칭 실패 행에 노출되는 "에러 노티" 버튼이 `accounts:notify_fallback_owners` POST 호출. view: `apps/integrations/views.py:notify_fallback_owners`.

Admin UI:
- 진입: `/accounts/api-owners/` 페이지 하단 별도 카드.
- 폼: `FallbackOwnersForm` (`apps/integrations/forms.py`) — 단일 `owners` CharField.
- 저장 endpoint: `accounts:fallback_owners_save` (POST 만). ActivityLog `UPDATE` + target_type `API_ENDPOINT_OWNER` + target_name="대체 담당자".

운영 메모:
- 미설정 상태 (`owners=""`)에서는 자동/수동 모두 발송 없음 — 매칭 실패 URL 의 알림이 누락되니 도입 직후 한 번 등록 필요.
- 이름은 Slack 표시명과 정확 일치 (또는 `(팀명)` 접미사 제거 후 일치) — `slack_users.find_user_id_by_name` 로 자동 매칭.


Error Log 화면에 등장한 URL 을 담당자와 자동으로 매칭해 보여주기 위한 마스터 데이터. 한국어 컬럼(레포/카테고리/영역/Method/Endpoint/FE 담당자/BE 담당자/매핑 앱/사용 횟수/step_ids)을 그대로 평면화한 형태. 2026-05-27 부터 **Google Sheets 동기화**가 채운다 (`sync_owners_from_google_sheet`, full mirror). CSV 다운로드는 동일 헤더로 export.

| 필드 | 비고 |
|---|---|
| `repo` | 'JK PC' / 'AM Mobile' 등 — 유니크 키 일부 |
| `method` | 'GET' / 'POST' / 'PATCH' / 'DELETE' / 'PUT' — 유니크 키 일부 |
| `endpoint` | `${BFF_URL}/apply/recruits/${RECRUIT_NO}/online-apply` 같은 패턴 그대로. 최대 500자. 유니크 키 일부. `${HOST_VAR}` prefix 와 `${PATH_PARAM}` 가 혼재. |
| `category` / `area` / `mapped_app` | 분류 컬럼 (한국어) |
| `fe_owner` / `be_owner` | FE/BE 담당자 (2026-05-20 개명, team 컬럼 제거). `"이동훈, 윤복영"` 같은 콤마 다중값을 그대로 보존 — 정규화 X. |
| `usage_count` | step 사용 횟수 |
| `step_ids` | 콤마 분리된 step ID 모음 (TextField) |
| `is_imported` | **True 면 동기화/CSV 임포트가 덮어쓸 수 있음**, False (수동 등록 row) 는 절대 건드리지 않음. Google Sheets 정기 동기화와 운영자 직접 입력을 안전하게 공존시키는 분리 플래그. |
| `notes` | 수동 메모 (선택) |

UniqueConstraint: **`(method, endpoint)`** (2026-05-20 변경). 같은 endpoint 가 다른 repo 라벨로 들어와도 기존 row 를 덮어쓰기만 하고 새 row 는 만들지 않는다. 마이그레이션 `0005_alter_apiendpointowner_options_and_more` 가 (a) Meta options 변경 (b) 기존 중복 정리 RunPython (수동 row > usage_count > created_at 우선) (c) 새 unique constraint 추가 의 3 step. 운영 로컬 DB 에서 531 → 331 (200건 중복 정리됨). Index: `mapped_app` (team 컬럼 제거로 team 인덱스도 함께 삭제).

### `ApiHostMapping` — 환경변수 → 실제 호스트 매핑 (`apps/integrations/models.py`)

`${BFF_URL}` 같은 CSV 의 호스트 변수를 실제 도메인(`bff-general-*.dev.albamon.com`) 과 연결. 매칭의 **필수 조건이 아니라 tie-breaker 용** — `ApiEndpointOwner.endpoint` 의 host 변수 prefix 는 컴파일 시 떼어내고 path 만 비교하므로 매핑이 비어있어도 path 매칭은 동작. 같은 path 가 AM/JK 양쪽에 등록된 경우에만 disambiguation 으로 활용.

| 필드 | 비고 |
|---|---|
| `variable_name` | `BFF_URL` 처럼 `${}` 떼고 저장 |
| `env` | `dev` / `stg` / `prd` |
| `host_pattern` | `bff-general-*.dev.albamon.com` 같은 와일드카드 호스트 |

UniqueConstraint: `(variable_name, env, host_pattern)`.

### Slack 사용자 자동 매칭 (`apps/integrations/slack_users.py`)

`ApiEndpointOwner.error_4xx_owner` / `error_5xx_owner` 의 콤마 다중값(`"이동훈, 윤복영"`) 을 발송 시점에 split → Slack `users.list` API 결과(LocMem 1시간 캐시) 에서 정확 매칭 → `<@U0XXX>` 멘션 문자열로 변환한다. 매칭 실패 시 `@이름` plain text 로 fallback (알림은 안 가지만 메시지엔 등장). 별도 수동 매핑 테이블이나 어드민 메뉴는 없음.

| 함수 | 비고 |
|---|---|
| `find_user_id_by_name(name)` | trim+lower 한 키로 매칭. 매칭 없으면 None. |
| `get_user_index(force_refresh=False)` | 전체 인덱스(`name_key → U_ID`) 반환. 캐시 hit 시 즉시 반환. |
| `invalidate_user_cache()` | 강제 갱신 (디버그 페이지의 "Slack 에서 다시 조회" 버튼이 호출). |
| `get_last_error()` | 마지막 `users.list` 호출 실패 사유 (디버그 페이지에 surfaced). |

매칭 키 (한 멤버당 5개 + 각각의 접미사 제거 head 까지 등록):
1. `name` (legacy `@username` 의 username)
2. `profile.real_name` (예: "장호철")
3. `profile.real_name_normalized`
4. `profile.display_name`
5. `profile.display_name_normalized`

**접미사 자동 제거 (`_name_head`)** — Slack 워크스페이스 관습으로 `장호철(talentclient eng팀)`, `이동훈 (Backend)`, `이동훈[엔지니어]` 같이 표시명 끝에 팀명/부서를 괄호/대괄호로 붙인 케이스가 흔하다. CSV 의 owner 컬럼은 보통 이름만 가지므로, 첫 번째 `(`/`[`/`{`/`<` 직전까지를 head 로 잘라 별도 키로도 등록한다. 정규식: `_SUFFIX_TRIM_RE = r"\s*[\(\[\{<].*$"`. 회귀 방지 테스트: `test_slack_users_index_strips_team_suffix`.

**상태 prefix 자동 제거 (`_strip_status_prefix`)** — 출퇴근/근태/이벤트 상태를 이름 앞에 표기하는 컨벤션 (`휴가중_박세건`, `재택 이동훈`, `퇴근중-홍길동`, `행사_윤복영`, `교육중_홍길동`). **화이트리스트 없는 generic 패턴** — `_STATUS_PREFIX_RE = r"^[가-힣]+[\s_·\-:|]+([가-힣].+)$"` 로 어떤 한글 단어든 prefix 로 인식 + separator (`_`, 공백, `·`, `-`, `:`, `|`) 로 분리되어 그 뒤에 한글로 시작하는 본문이 오면 prefix 제거. 중첩 prefix (`재택_미팅중_홍길동`) 는 while 루프로 끝까지 떼어낸다. 새 상태 키워드 추가 코드 작업 불필요. 영문 prefix (`WFH_`, `OOO_`) 는 의도적으로 미처리. 회귀 방지 테스트: `test_slack_users_strip_status_prefix`, `test_slack_users_index_handles_status_prefix` (`행사_윤복영(discovery eng팀)` 케이스 포함).

페이지네이션 (`response_metadata.next_cursor`) 자동 처리, 최대 `MAX_PAGES=20` (≈ 4000명 workspace) 까지 안전.

**SLACK_BOT_TOKEN scope 요구사항**:
- `chat:write` — 메시지 발송 (기본).
- `users:read` — workspace 사용자 검색 (자동 멘션을 위해 **추가 필요**). Slack 앱 설정 → OAuth & Permissions → Bot Token Scopes 에 추가하고 워크스페이스 **재설치 1회** (`Reinstall to Workspace`). 재설치 후 토큰 값은 그대로지만 권한이 확장된다.
- scope 가 없으면 `users.list` 응답이 `error: missing_scope` → 로그 경고 + 빈 매핑 → 모든 이름이 `@이름` plain text 로 fallback.

---

## 4. 외부 연동

### 4.1 Jenkins (`apps/integrations/jenkins.py`)

`JenkinsClient(base_url, user, api_token)` — 인자가 비어있으면 **DB → 환경변수** 순으로 폴백.
1. `apps.suites.models.SuiteJenkinsCredential` 의 `kind='internal'` row (`/suites/credentials/` 에서 관리)
2. 그래도 비어있으면 환경변수 (`JENKINS_DEFAULT_URL/USER/API_TOKEN`)

테스트 Job 트리거는 `JenkinsClient()` (인자 없이) 호출, Quality Gate 의 deploy/image/prod 는 게이트 자격증명을 명시. Suite 별로 외부 Jenkins 를 쓰는 경우 `suite.get_jenkins_client()` 가 `TestSuite.jenkins_base_url/user/api_token` 을 명시적으로 넘긴다.

내부 자격증명을 DB 보다 우선해 강제하려면 `JenkinsClient(base_url, user, api_token)` 으로 명시 인자를 넘긴다.

핵심 메서드:
- `get_job_info(job)` — 파라미터 정의 + choices. **Active Choices / Git Parameter 대응을 위해 4가지 경로를 병렬 시도** (`ThreadPoolExecutor max_workers=4`):
  1. HTML 빌드 폼 스크래프 (`_scrape_build_form_choices`) — `<select name="value">` 파싱
  2. `/scriptText` Active Choices Groovy (`_get_active_choices_via_script`) — Overall/RunScripts 권한 필요
  3. Git Parameter descriptor HTTP (`_fetch_git_parameter_choices`) — JSON or HTML, 파라미터당 1회
  4. `/scriptText` GitParameter Groovy (`_get_git_parameter_choices_via_script`)

  병합 우선순위: HTML > Active Choices script > Git HTTP > Git script.
  Active Choices 의 `:selected` / `:disabled` 마커는 `_strip_active_choice_marker` 로 제거.
- `trigger_build(job, parameters)` — 파라미터 유무로 `/build` vs `/buildWithParameters` 자동 선택. 빌드 트리거 직전 `get_next_build_number(job)` 로 번호 미리 확보.
- `get_build_info` / `get_build_stages` (wfapi) / `get_console_output` / `is_job_building` / `stop_build` / `get_last_build_info`.

**SSO 가 깔린 Jenkins 의 토큰 무효/만료 감지**: `_request` 가 HTTPError 를 받을 때 `Location` 헤더가 `commenceLogin` / `securityRealm` 으로 향하거나 `reason` 에 "infinite loop" / "redirect error" 가 보이면 → basic auth 가 거부되어 익명으로 강등됐고 user-context endpoint 라 SSO 로 리다이렉트된 상황. 이 경우 모호한 "HTTP 302: redirect loop" 대신 **"Jenkins 인증 실패 — 토큰 만료/취소 가능성, 새 API 토큰 발급 후 Suite 자격증명 갱신"** 한국어 메시지로 `JenkinsError(status=401)` 를 raise. 더불어 `_try_trigger_next` (`apps/suites/views.py`) 와 `_trigger_all_pending` (`apps/robustness/services.py`) 의 trigger 실패 처리부가 이 메시지를 `TestSuiteJobRun.console_output` 에 `[trigger failed] ...` 형태로 저장해 화면에서 즉시 원인을 보게 한다.

같은 Jenkins 서버라도 user 식별자는 SSO 가 매핑한 ID 와 정확히 일치해야 한다 — 화면 표시명만 보고 `taeshkim` 식의 short ID 로 저장했는데 실제 Jenkins 가 인식하는 ID 는 이메일 형식인 경우 401 (잘못된 user) 또는 302 (그 user 자체 없음) 가 떨어진다. 토큰 발급 화면 (Jenkins 우상단 사용자명 → Configure → API Tokens) 에서 실제 어떤 user 로 보이는지 확인해서 저장한다.

### 4.2 Argo CD (`apps/integrations/argocd.py`)

`parse_app_url("https://argocd/.../applications/<ns>/<app>")` → `(base, "ns/app")`. `<ns>` 는 **namespace** 이지 project 가 아니다 — API 호출 시 `appNamespace` 쿼리/바디로 전달해야 정상 응답.

`ArgoCDClient(base_url, token)`:
- `get_application(app_id, refresh="hard"|"normal"|"")` — refresh 지정 시 sync 트리거 발생
- `sync_application(app_id, prune=False)`
- `extract_status(app)` → `{sync, health, operation_phase, operation_message}`
- `is_synced_and_healthy(app)` → operation_phase 이 `Running`/`Terminating` 이면 False (아직 진행 중)

`classify_argocd_status(sync, health, op_phase, op_message)` (모듈 함수) — UI 배지 분류기. 반환 dict 의 `kind` / `level` / `label`:

| 우선순위 | 조건 | kind | level | label |
|---|---|---|---|---|
| 1 | `health` ∈ Degraded/Missing | `failed` | `fail` | `Degraded` 또는 `Missing` |
| 2 | `op_phase` ∈ Failed/Error | `failed` | `fail` | `Sync 실패` |
| 3 | `op_phase` ∈ Running/Terminating | `syncing` | `running` | `동기화중` |
| 4 | sync=Synced + health=Healthy | `synced` | `pass` | `Synced` |
| 5 | sync=Synced (그 외 health) | `rollout` | `running` | `롤아웃중` |
| 6 | sync=OutOfSync 등 | `outofsync_pending` | `running` | `OutOfSync — 재동기화 대기` |
| 7 | 그 외 (정보 미수신) | `pending` | `running` | `Syncing...` |

`QualityGateRun.deploy_argocd_badge` / `prod_argocd_badge` 프로퍼티가 이 함수를 호출. 템플릿 (`_run_status.html`) 은 `<span class="status-badge status-{{ badge.level }}">{{ badge.label }}</span>` 로 렌더링하고, `badge.kind == "failed"` 면 Retry Sync 버튼을 즉시 노출한다.

### 4.3 `_check_argocd_sync(run, gate, stage)` (`apps/qualitygates/views.py:1174`)

- `app_url` 또는 `token` 비면 즉시 `True` 반환 (Argo CD 미사용 게이트는 패스)
- 4초 쿨다운 — `{stage}_argocd_checked_at` 기준
- **최초 진입 시** (`{stage}_argocd_sync_requested_at is None`):
  1. `get_application(refresh="hard")`
  2. `sync_application()`
  3. `sync_requested_at`/`checked_at` 만 set 하고 **즉시 False 반환** — 같은 호출에서 polling 안 함.
  - 사고 방지(2026-05-04 보고): refresh+sync 직후 곧바로 polling 하면 Argo CD 가 우리 sync 요청을 받아 새 operation 을 만들기 전이라 직전 Run 의 `Synced + Healthy` 가 그대로 잡힌다 → 이전 revision 으로 테스트가 시작되는 사고. 첫 호출은 무조건 다음 폴링(4초 쿨다운 뒤)부터 검사하도록.
  - refresh / sync 가 실패해도 `{stage}_argocd_sync_error` 에 메시지 누적 후 진행.
- 이후 매 호출에서 `get_application()` 으로 상태 폴링.
- **stale 결과 거름** (같은 사고의 보강): 폴링 결과의 `operationState.finishedAt` 이 우리 `sync_requested_at` 이전이고 phase 가 Running/Terminating/Failed/Error 가 아니면 직전 sync 의 잔재로 보고 False 반환. Argo CD 가 우리 sync 를 받아 새 operation 의 `finishedAt` 을 갱신할 때까지 진행 보류. `extract_status` 의 `operation_finished_at` 키.
- 매 폴링마다 `operation_phase` ∈ Failed/Error 면 즉시 `(PROD_)DEPLOY_FAILED` 로 전환 + `*_argocd_sync_error` 에 phase/message 기록 → 15분 타임아웃을 기다리지 않고 끊는다.
- Synced + Healthy 도달 시 두 단계 추가 검증 후 `True` 반환:
  1. **Pod-level 검증** (`_all_argocd_pods_healthy`) — `client.get_resource_tree(app_id)` 호출해 자식 Pod 노드의 `health.status` 가 모두 `Healthy` 인지 확인. ArgoCD 의 application-level `health.status` 는 Deployment 레벨이라 maxUnavailable 등 rolling-update 설정으로 새 ReplicaSet 의 Pod 가 readiness probe 통과 *전*에도 Healthy 로 보고될 수 있다 — 자식 Pod 까지 봐야 진짜 traffic 받을 수 있는 상태. Pod 노드가 하나도 없으면 (Service-only 매니페스트) skip 통과. resource-tree API 가 실패하면 보수적으로 False.
  2. **Grace period** (`_ARGOCD_HEALTHY_GRACE_SECS = 30`) — 첫 Healthy 도달 시각을 `{stage}_argocd_healthy_first_at` 에 기록하고, 그 시각으로부터 30초가 지난 뒤 한 번 더 Healthy 가 유지되어야 진짜 sync 완료로 판정. 그 사이 health 가 다시 떨어지면 `healthy_first_at = None` 으로 reset 하고 처음부터 다시 카운트.
  검증 통과 시 `{stage}_argocd_synced_at` 기록 + sync_error 클리어. `argocd_retry` / deploy-only / prod-only 재시도 분기에서도 `healthy_first_at` 를 함께 reset.

`argocd_retry(run_pk, stage)` — Argo CD 가 멈춰 있을 때 사용자가 수동 재시도. `*_sync_requested_at` 등 Argo 관련 필드를 모두 비우고, `run.status` 가 이미 다음 단계 또는 `(PROD_)DEPLOY_FAILED` 로 떨어졌으면 `*_DEPLOYING` 으로 되돌린 뒤 즉시 1회 폴링. UI 에서는 `deploy_argocd_synced_at` 가 채워졌거나 `deploy_argocd_badge.kind == "failed"` 일 때 Retry Sync 버튼이 노출된다 (prod 도 동일).

### 4.4 Slack 알림 (`apps/notifications/`)

Quality Gate 파이프라인 + 단독 Test Suite Run 양쪽을 커버하는 Slack 알림 모듈. 어드민 화면에서 20개 이벤트를 개별 토글 가능 (QG 파이프라인 14개 + Suite Run 단독 3개 + Argo CD 1개 + Run 단위 4개 중복없이).

#### 4.4.1 구성요소

| 파일 | 역할 |
|---|---|
| `apps/notifications/models.py` | `SlackNotificationSettings` (싱글톤, PK=1). `enabled`, `default_channel`, `notify_*` 토글 20개. `get_solo()` 헬퍼. |
| `apps/notifications/events.py` | 이벤트 상수 (`RUN_STARTED`, `IMAGE_FAILED`, `TEST_SUITE_PASSED`, `SUITE_RUN_PASSED` 등) + `EVENT_TABS` (관리 화면 탭→그룹→이벤트 2단 구조 — Quality Gate / Test Suite 분리). 호환을 위해 `EVENT_GROUPS` (flat) 도 같이 export. |
| `apps/notifications/services.py` | `send_event(event_type, run, **ctx)` (QualityGateRun) 동기 발송 + `_qg_event_fields` 으로 이벤트별 메시지 빌드. `send_event_async(...)` 데몬 스레드 발송. `send_suite_run_event(event_type, suite_run, **ctx)` (TestSuiteRun) 동기 발송 + `_suite_event_fields`. `send_suite_run_event_async(...)` 데몬 스레드 발송. `send_test_message(channel)` 어드민 테스트 버튼용. `_stage_console_field(run, stage, base)` 이미지/배포/검수통과 콘솔 로그 링크 헬퍼. |
| `apps/notifications/admin.py` | Django admin 등록 (실제 운영 UI 는 `/accounts/notifications/` 에 있음). |
| `apps/accounts/views.py:notification_settings` / `notification_settings_test` | 어드민 화면 GET/POST + 테스트 메시지 발송. |
| `templates/accounts/notification_settings.html` | 설정 화면. |
| `templates/accounts/_admin_tabs.html` | 사용자 관리 / Slack 알림 공통 탭 헤더 partial. `admin_active_tab` context 변수로 활성 탭 결정. |

#### 4.4.2 토큰 / 채널 우선순위

- 토큰: `.env` 의 `SLACK_BOT_TOKEN` (DB 에는 저장 안 함 — 보안상 .env 만)
- 채널: `SlackNotificationSettings.default_channel` (어드민 입력) → 비어있으면 `.env` 의 `SLACK_DEFAULT_CHANNEL` 폴백
- Run 상세 링크: `.env` 의 `QGP_BASE_URL` 이 채워져 있을 때만 attachment 의 `title_link` 로 추가

#### 4.4.3 트리거 통합 지점 (`apps/qualitygates/views.py`)

폴링 흐름을 막지 않도록 모두 `send_event_async` 사용. 데몬 스레드가 fresh `QualityGateRun` 을 select_related 로 다시 fetch 한 뒤 발송한다.

| 발송 시점 | 호출 위치 | 이벤트 |
|---|---|---|
| Run 생성 직후 | `gate_run` | `RUN_STARTED` |
| 이미지 빌드 트리거 | `gate_run` (use_image_build True) | `IMAGE_STARTED` / 실패 시 `IMAGE_FAILED` + `RUN_FAILED` |
| Image 단계 없이 deploy 첫 트리거 | `gate_run` (use_image_build False) | `DEPLOY_STARTED` / 실패 시 `DEPLOY_FAILED` + `RUN_FAILED` |
| 이미지 빌드 결과 폴링 | `_poll_image_build` | SUCCESS → `IMAGE_COMPLETED` / 그 외 → `IMAGE_FAILED` + `RUN_FAILED` |
| Auto / 수동 deploy 트리거 | `_auto_trigger_deploy` / `deploy_trigger` | `DEPLOY_STARTED` / 실패 시 `DEPLOY_FAILED` + `RUN_FAILED` |
| Deploy 빌드 + Argo CD 완료 | `_poll_deploy` (양쪽 분기) | `DEPLOY_COMPLETED` / 실패 시 `DEPLOY_FAILED` + `RUN_FAILED` |
| Argo CD sync 실패 / 타임아웃 | `_check_argocd_sync` | `ARGOCD_FAILED` + `(DEPLOY|PROD)_FAILED` + `RUN_FAILED` |
| Test Suite 트리거 | `_try_trigger_test` | `TEST_SUITE_STARTED` (suite_name ctx 포함) |
| Test Suite 종료 | `_poll_one_test_build` | PASSED → `TEST_SUITE_PASSED` / FAILED → `TEST_SUITE_FAILED` |
| 순차 테스트 즉시 FAIL | `_poll_test_sequential` | `RUN_FAILED` |
| 모든 Test 종료 판정 | `_finalize_run` | has_failed → `RUN_FAILED` / prod 없이 PASS → `RUN_COMPLETED` |
| Auto / 수동 prod 트리거 | `_auto_trigger_prod` / `prod_deploy` | `PROD_STARTED` / 실패 시 `PROD_FAILED` + `RUN_FAILED` |
| Prod 빌드 + Argo CD 완료 | `_poll_prod_deploy` (양쪽 분기) | `PROD_COMPLETED` + `RUN_COMPLETED` / 실패 시 `PROD_FAILED` + `RUN_FAILED` |
| 사용자 취소 | `cancel_run` | `RUN_CANCELLED` |

전이가 발생하지 않는 폴링 결과(`building=True`, 동일 상태 유지) 에서는 알림이 발생하지 않도록 모든 호출이 상태 전이 분기 안에만 들어 있다.

> **주의**: `TEST_SUITE_*` 이벤트는 **Quality Gate 내부에서 실행되는 Suite 만** 대상이다. 단독 Suite Run (apps/suites) 은 §4.4.3a 의 별도 이벤트(`SUITE_RUN_*`) 를 사용한다.

#### 4.4.3a 단독 Test Suite Run 트리거 통합 지점 (`apps/suites/views.py`)

QG 파이프라인을 거치지 않고 사용자가 Suite 화면에서 직접 Run 을 돌릴 때 발송되는 알림. `send_suite_run_event_async` 가 데몬 스레드에서 fresh `TestSuiteRun` 을 select_related 로 다시 fetch 한 뒤 발송한다.

| 발송 시점 | 호출 위치 | 이벤트 |
|---|---|---|
| Suite Run 트리거 | `suite_run` (POST 직후) | `SUITE_RUN_STARTED` |
| Suite Run 완료 (성공) | `_poll_run_status` (failed_jobs == 0) | `SUITE_RUN_PASSED` |
| Suite Run 완료 (실패/abort 포함) | `_poll_run_status` (failed_jobs > 0) | `SUITE_RUN_FAILED` |

QG 파이프라인의 `TEST_SUITE_*` 와 토글이 별개이므로 어드민에서 따로 끄고 켤 수 있다. `_abort_suite_run` 은 별도 알림을 발송하지 않는다 (취소가 명시적 사용자 액션이라 추가 알림 가치 낮음).

#### 4.4.4 이벤트별 메시지 본문

`_qg_event_fields(event_type, run, ctx)` 와 `_suite_event_fields(...)` 가 이벤트마다 다른 정보를 메시지에 넣는다 (services.py).

| 이벤트 | 포함되는 정보 |
|---|---|
| `RUN_STARTED` | 실행자, 옵션 (이미지 빌드 / 병렬 테스트 / 검수통과 배포 여부) |
| `RUN_COMPLETED` | 총 소요 시간, Prod 배포 여부 |
| `RUN_FAILED` | 실패 단계 한국어 라벨, 실패 Suite 수, 경과 시간 |
| `RUN_CANCELLED` | 실행자, 취소 시점 상태 |
| `IMAGE_STARTED/COMPLETED` | Branch, Commit (8자), 소요 시간 (COMPLETED) |
| `IMAGE_COMPLETED` | + **포털 image 콘솔 링크** |
| `IMAGE_FAILED` | Branch, Commit, **포털 image 콘솔 링크** |
| `DEPLOY_STARTED` | Branch, Commit |
| `DEPLOY_COMPLETED` | 소요 시간, Argo CD health |
| `DEPLOY_FAILED` | **포털 deploy 콘솔 링크**, Argo CD sync 오류 |
| `PROD_STARTED` | Branch, Commit |
| `PROD_COMPLETED` | 소요 시간, Argo CD health |
| `PROD_FAILED` | **포털 prod 콘솔 링크**, Argo CD sync 오류 |
| `TEST_SUITE_STARTED/PASSED` | Test Suite 이름 |
| `TEST_SUITE_FAILED` | Test Suite 이름, **포털 job-runs 콘솔 링크** (호출 측이 `job_run_id=str(test_run.job_run.pk)` 전달) |
| `ARGOCD_FAILED` | 단계 (검수대상/검수통과) |
| `SUITE_RUN_STARTED` | Suite, 실행자, Job 수 |
| `SUITE_RUN_PASSED` | Suite, 실행자, 결과 (PASS/총), 소요 시간 |
| `SUITE_RUN_FAILED` | Suite, 실행자, PASS/FAIL/총, 소요 시간, **실패 Job 별 포털 콘솔 링크 (최대 5개)** |

**링크 정책**: 모든 콘솔 로그 링크는 **포털의 콘솔 뷰** (`/qualitygates/runs/.../console/{image,deploy,prod}/` 또는 `/suites/job-runs/.../console/`) 만 사용하고 **원본 Jenkins URL 은 노출하지 않는다**. 사용자가 QG 포털 안에서 로그를 보도록 UX 통일하기 위함. 링크는 `<URL\|텍스트>` 마크다운 형식, attachment 의 `mrkdwn_in: ["fields"]` 로 렌더. **링크가 동작하려면 `.env` 의 `QGP_BASE_URL` 이 운영 도메인** (`https://qualitygate.jkintra.net` 등) 으로 설정돼야 한다.

#### 4.4.5 발송 실패 처리

- `send_event` 는 어떤 실패도 호출자에게 throw 하지 않는다 (`logger.warning` 만).
- `send_event_async` 는 데몬 스레드 + 자체 try/except 으로 감싸 폴링 흐름을 절대 차단하지 않는다.
- 토큰 미설정 / 채널 미지정 / Slack API 오류 / 네트워크 타임아웃(6초) 모두 silent skip.

#### 4.4.6 레거시 (`apps/integrations/{slack.py, webhooks.py, tasks.py}`)

옛 GateRun (`apps.gates`) 모델을 기반으로 한 코드라 신 파이프라인과 **무관**. 새 알림은 위 `apps/notifications/` 모듈만 사용한다. 신규 코드 추가 시 `apps/integrations/slack.py` 가 아니라 `apps/notifications/` 에 작성할 것.

---

## 5. URL 라우팅

`config/urls.py`:

| Prefix | 앱 | 비고 |
|---|---|---|
| `/` | `dashboard` | 7일 요약 + DORA 4지표(30일) + 최근 실행 + 게이트별 현황 |
| `/accounts/` | `accounts` | login / signup / 사용자 관리 / Slack 알림 설정 (`/accounts/notifications/`) |
| `/qualitygates/` | `qualitygates` | Folder/Gate/Run (핵심) |
| `/suites/` | `suites` | Test Suite + Queue |
| `/robustness/` | `robustness` | Reliability Test (구 Robustness) |
| `/logs/` | `activitylog` | Activity Log |
| `/admin/` | Django admin | |
| `/api/v1/` | `api` | DRF (구 모델 기반 — 신 파이프라인과 무관) |
| `/health/` | health check | `{"status":"ok"}` |
| `/sw.js` | Service Worker | 인라인 서빙, 루트 스코프, no-cache |

`gates/`, `services/` 도 등록돼 있으나 레거시.

### `qualitygates` 의 핵심 URL (`apps/qualitygates/urls.py`)

- Folder CRUD: `folders/`, `folders/register/`, `folders/<pk>/(edit|delete)/`
- SubFolder CRUD: `folders/<folder_pk>/subfolders/register/`, `subfolders/<pk>/(edit|delete)/`, `subfolders/<pk>/` (Gate 목록)
- Gate CRUD: `subfolders/<subfolder_pk>/gates/register/`, `<pk>/(edit|delete)/`
- Gate 토글: `<pk>/toggle-auto/` (POST, Dev→Stg 만), `<pk>/toggle-parallel/` (POST, 게이트 타입 무관 — 테스트 병렬 실행)
- Run 시작: `<pk>/run/`, `<pk>/run/params/` (Job 파라미터 JSON, `?refresh=1` 로 캐시 무시)
- Run 상세: `runs/<pk>/`, `runs/<pk>/status/` (htmx fragment)
- Run 액션: `deploy/`, `deploy/params/`, `prod-deploy/`, `prod-deploy/params/`, `rerun/`, `rerun-test/`, `cancel/`
- 콘솔: `console/(image|deploy|prod)/`
- 에러 로그: `error-log/(image|deploy|prod)/` — 콘솔 로그에서 실패 원인(`@@@...@@@` 블록 / pytest FAILURES / [FAIL] 키워드)만 파싱 (`apps/integrations/log_errors.py`)
- Argo CD 재시도: `argocd/<stage>/retry/` (stage = `deploy` or `prod`)

### `suites` 의 핵심 URL (`apps/suites/urls.py`)

- Suite CRUD: `/suites/`, `/suites/register/`, `/suites/<pk>/(edit|delete|sync)/`
- Suite Run: `/suites/<pk>/run/`, `/suites/runs/<pk>/(status|delete)/`, `/suites/<pk>/runs/clear/`
- Queue: `/suites/queue/`, `/suites/queue/stop-all/`
- 콘솔: `/suites/job-runs/<pk>/console/`
- 에러 로그: `/suites/job-runs/<pk>/error-log/` — 콘솔에서 실패 원인만 파싱 (`apps/integrations/log_errors.py`)
- **Jenkins 자격증명 관리**: `/suites/credentials/` (admin 전용 — 내부/외부 default 자격증명 저장)

---

## 6. 프론트엔드 — htmx + qg_notify.js

### 6.1 폴링 (htmx)

Run 상세 페이지는 5초마다 `runs/<pk>/status/` GET → `_run_status.html` 로 교체. 서버는 그 안에서 `_poll_gate_run(run)` 을 동기로 호출.

### 6.2 OS 알림 인프라

`templates/base.html` 에 인프라가 배치돼 있다:
- `#qg-toast-container` — 토스트 컨테이너 (우상단)
- `#notify-toggle-btn` (사이드바) — ON/OFF 토글
- `#qg-global-notify-test` — 우하단 고정 진단 버튼 + 가이드
- `#qg-notify-banner` — 상단 권한 요청 배너

`/sw.js` 는 `config/urls.py` 에서 인라인으로 서빙 (루트 스코프 + `Service-Worker-Allowed: /` 헤더).

### 6.3 글로벌 워처 (`static/js/qg_notify.js`)

페이지를 이동/새로고침해도 실행 중 Run 을 계속 추적하기 위한 **localStorage 기반 워처**.

- 키: `qg_watchers_v1`
- 등록: `QGNotify.watch(key, { kind, statusUrl, runUrl, label, initialStatus })`
  - `kind = 'qg_run' | 'suite_run'`
- 5초 주기로 `statusUrl` 을 GET → 응답 HTML 의 `data-run-status="..."` 추출 → 변경 감지
- 터미널 상태 도달 시: 토스트 + 삐 + OS 알림 발사 (SW 경로 우선, 실패 시 `new Notification()`)
- 다중 탭 dedupe: `notifiedKeys` 를 localStorage 에 기록 + OS 레벨에서 stable tag 사용
- 24시간 이상 묵은 항목 자동 정리

`_run_status.html` 와 Suite Run fragment 는 `data-run-status` 속성으로 현재 상태를 노출해야 한다.

OS 알림 메시지 매핑은 `QG_RUN_NOTIFY_MAP` (`qg_notify.js:494`) 에 있다 — 메시지 변경 시 여기를 수정.

---

## 7. 권한 / 인증

### `User` 모델 (`apps/accounts/models.py`)

`Role`: `qpe_admin`, `qpe_ops`, `tribe_lead`, `tribe_dev`, `approver`, `viewer`

- `is_admin` = `qpe_admin` or superuser
- `is_qpe` = `qpe_admin` or `qpe_ops`
- `can_write` = role ≠ `viewer`

### `RolePermissionMiddleware` (`apps/accounts/middleware.py`)

POST/PUT/PATCH/DELETE 에 대해:
- **`/api/v2/` 로 시작하면 즉시 통과** — DRF 가 자체 권한 처리. (PRD §4.4)
- `POST_ALWAYS_ALLOW`: `/accounts/logout/` — 모두 허용
- Admin: 전부 허용
- Viewer: 전부 차단
- 그 외 (operational role): `REGULAR_POST_ALLOW` 패턴 (`/run`, `/start/`, `/stop/`, `/trigger/`, `/status/`, `/rerun/`, `/deploy/`, `/reset/`, `/triage/`) 만 허용

새 operational POST 엔드포인트 추가 시 이 패턴 매칭에 걸리는지 확인 — 안 걸리면 일반 사용자가 못 쓴다.

### `/api/v2/` 외부 API 인증 (`apps/api_v2/`)

세션 의존 포털 UI 와 별개로 외부 시스템 (Jenkins / GitHub Actions / 백오피스 등) 이 호출할 수 있도록 `/api/v2/` 아래에 DRF 기반 REST API 를 둔다. PRD: `prd_api.md`.

| 파일 | 역할 |
|---|---|
| `apps/api_v2/models.py` | `ApiKey` (UUID PK, `key_prefix`, `key_hash` (SHA-256), `scopes`, `expires_at`, `revoked_at`, `last_used_at`). `ApiKey.issue(user, name, scopes, expires_in_days=90)` 로 발급 — 평문은 응답에 1회만 노출되고 DB 는 해시만 보관. `find_by_plaintext(...)` 가 `qgp_{prefix}_{secret}` 형식을 파싱해 constant-time 비교 후 매칭. |
| `apps/api_v2/scopes.py` | 스코프 상수 (`meta:read`, `runs:read`, `runs:trigger`, `runs:control`, `*`). `has_scope(scopes, required)` 헬퍼. `*` 는 admin 전용. |
| `apps/api_v2/authentication.py` | `ApiKeyAuthentication` (`Authorization: Bearer qgp_...`). 만료/회수/비활성 사용자 분기 모두 `AuthenticationFailed` 로 코드 (`api_key_expired`/`api_key_revoked`/`api_key_invalid`/`user_inactive`). |
| `apps/api_v2/permissions.py` | `HasScope` (view 의 `required_scope` 또는 `required_scopes` dict), `CanWriteForControl` (Viewer 거부), `AdminOnlyForWildcard` (안전망). 세션 인증으로 들어오면 스코프 검사는 skip. |
| `apps/api_v2/throttling.py` | `ReadThrottle` (120/min), `WriteThrottle` (30/min). 키 단위 식별 (`request.auth.pk`). 세션 인증은 throttle skip. |
| `apps/api_v2/exceptions.py` | DRF exception_handler 오버라이드. 모든 응답을 `{"error": {"code": "...", "message": "..."}}` 형태로 정규화 + 코드 카탈로그 매핑. `EXCEPTION_HANDLER` 로 `REST_FRAMEWORK` 에 등록. |
| `apps/api_v2/urls.py` | `/api/v2/health/` (인증 불요), `/api/v2/me/` (현재 키 + 사용자 정보 + 스코프 목록) — Phase A 의 최소 엔드포인트. 후속 Phase 에서 메타/Run/Suite 라우팅 추가. |
| `apps/api_v2/views/health.py` | health, me 핸들러. |
| `apps/api_v2/admin.py` | Django admin 등록 (운영자가 키 직접 회수 가능). `key_hash` 는 readonly. |

발급 디폴트: 만료 90일, scope `*` 는 admin 전용 (PRD §15 의 보수적 디폴트).

### API Key 관리 UI (`/accounts/api-keys/`)

| 경로 | 뷰 | 역할 |
|---|---|---|
| `/accounts/api-keys/` | `apps/accounts/views.py:api_key_list` | 본인 키 목록 (이름/prefix/scopes/만료/마지막 사용/회수 버튼) |
| `/accounts/api-keys/new/` | `api_key_create` | 발급 폼 (스코프 체크박스, 만료 30/90/180/365/무제한). POST 시 `ApiKey.issue(...)` 호출 → 결과 페이지로 redirect 하여 평문 1회 노출 |
| `/accounts/api-keys/<uuid>/revoke/` | `api_key_revoke` | POST 만. 본인 키 회수. 멱등 처리 (이미 회수된 키 재호출 시 안내만) |
| `/accounts/api-keys/admin/` | `api_key_admin_list` | admin 전용 (`_require_admin`). 전체 키 표 + 사용자명 + 회수 버튼 |
| `/accounts/api-keys/admin/<uuid>/revoke/` | `api_key_admin_revoke` | admin 전용. 어떤 사용자의 키든 회수 |

- 미들웨어: `POST_ALWAYS_ALLOW` 에 `/accounts/api-keys/` 추가 — Viewer 포함 모든 인증 사용자가 자기 키 발급/회수 가능. admin 전용 페이지는 `_require_admin` 으로 별도 차단.
- **사이드바 메뉴 노출 (2026-05-27 변경)**: 본인 API Keys 링크는 사이드바(`templates/base.html`)에서 **제거**됨. 메뉴상 API Keys 진입점은 **Admin → API Keys 탭(`api_key_admin_list`, `templates/accounts/_admin_tabs.html`)** 하나.
  - 발급도 메뉴 안에서 가능하도록 `api_key_admin_list.html` 카드 헤더에 **'새 키 발급' 버튼**을 둠 (→ `api_key_create`, 발급 대상은 **로그인한 관리자 본인** 키). 이 페이지의 표 자체는 여전히 전체 사용자 키 조회/회수용.
  - 발급 폼(`api_key_form.html`)의 '취소' 와 발급 완료(`api_key_created.html`)의 '목록으로' 는 `{% if user.is_admin %}` 분기 — admin 이면 `api_key_admin_list`, 그 외엔 `api_key_list` 로 복귀. 덕분에 관리자는 Admin 영역 안에서 발급→완료→복귀 루프를 돈다.
  - 독립 개인 목록 페이지(`api_key_list`)와 권한은 그대로(URL 접근 가능)지만 사이드바 링크만 없다 (비admin 사용자는 메뉴상 API Keys 접근 불가). 사이드바에 다시 노출하려면 `base.html` 의 `nav-menu` 에 `api_key_list` 링크를 복원.
- 와일드카드 `*` 스코프: 발급 폼의 체크박스가 일반 사용자에게는 `disabled`. POST 본문으로 강제 전송 시 view 가 `PermissionDenied` (403).
- ActivityLog: 발급/회수 시 `API_KEY_CREATED` / `API_KEY_REVOKED` 액션. (target_type 은 `USER`, target_name 에 `username/keyname` 기록 — 전용 target_type 은 향후 추가 가능.)

### Service layer 분리 (`apps/qualitygates/services.py`, `apps/suites/services.py`)

`/api/v2/` 외부 API 가 같은 비즈니스 로직을 호출할 수 있도록 view 의 핵심을 service 함수로 추출. view 는 request 파싱 + `messages` + `redirect` 만 담당하는 얇은 래퍼로 축소.

#### `apps/qualitygates/services.py`

- `GateRunError(code, message, *, http_status, details)` — 비즈니스 룰 위반. `code` 가 API 에러 코드와 1:1 매핑.
- `trigger_gate_run(*, gate, user, parameters, skip_image_build, auto_mode_override)` → `QualityGateRun`
- `cancel_run(*, run, user)` → `QualityGateRun`
- `rerun_gate(*, run, user)` → 새 Run (`trigger_gate_run` 호출)
- `rerun_test(*, run, user)` → `QualityGateRun`
- `trigger_deploy(*, run, user, parameters)` → `QualityGateRun` (`WAITING_DEPLOY` + `DEPLOY_FAILED` 재시도 겸용)
- `trigger_prod(*, run, user, parameters)` → `QualityGateRun` (`QG_PASSED` + `PROD_DEPLOY_FAILED` 재시도 겸용)
- `argocd_retry(*, run, stage, user)` → `QualityGateRun` (stage = `deploy` | `prod`)

view 측 함수 (`gate_run`, `cancel_run`, `rerun_gate`, `rerun_test`, `deploy_trigger`, `prod_deploy`, `argocd_retry`) 는 service 호출 + 예외 → messages 변환 + redirect 만. ActivityLog 기록, Slack 알림 (`send_event_async`), 폴링 호출 (`_poll_gate_run` 등) 은 모두 service 안에서 수행. **폴링 함수군은 그대로 view 모듈에 남아 있다** — service 가 lazy import 로 호출 (순환 import 회피).

#### `apps/suites/services.py`

- `SuiteRunError(code, message, *, http_status, details)`
- `trigger_suite_run(*, suite, user, job_id)` → `TestSuiteRun` (job_id 빈/None 이면 `SuiteRunError("job_required")`)
- `cancel_suite_run(*, suite_run, user)` → `TestSuiteRun` (이미 COMPLETED 면 `SuiteRunError("not_actionable")`)

`_try_trigger_next`, `_abort_suite_run`, `_poll_run_status` 는 views.py 에 그대로 — robustness 등이 import 하므로 호환 보존.

### `/api/v2/` 엔드포인트 (라우팅 — `apps/api_v2/urls.py`)

| 경로 | 메서드 | 스코프 | 비고 |
|---|---|---|---|
| `/api/v2/health/` | GET | 없음 | `{"status":"ok","version":...}` |
| `/api/v2/me/` | GET | (인증만) | 현재 키 + 사용자 + 사용 가능 스코프 |
| `/api/v2/folders/` | GET | `meta:read` | 인스턴스 목록 |
| `/api/v2/folders/<id>/` | GET | `meta:read` | 인스턴스 상세 |
| `/api/v2/folders/<id>/subfolders/` | GET | `meta:read` | 하위폴더 목록 |
| `/api/v2/subfolders/<id>/` | GET | `meta:read` | 하위폴더 상세 |
| `/api/v2/gates/` | GET | `meta:read` | 게이트 목록. 필터: `folder`, `subfolder`, `gate_type`, `q`, `is_active` |
| `/api/v2/gates/<id>/` | GET | `meta:read` | 게이트 상세. `?include=latest_run,test_suites` |
| `/api/v2/gates/<id>/params/?stage=image\|deploy\|prod&refresh=1` | GET | `meta:read` | Jenkins Job 파라미터 스키마 (120s 캐시) |
| `/api/v2/gates/<id>/runs/` | GET | `runs:read` | 게이트의 Run 목록. 필터: `status`, `since`, `limit` |
| `/api/v2/gates/<id>/runs/` | POST | `runs:trigger` | 새 Run 트리거. body: `{skip_image_build, parameters, auto_mode_override}` |
| `/api/v2/runs/<id>/` | GET | `runs:read` | Run 단건 상세 (호출 시 `_poll_gate_run` 트리거 — 폴링 모드) |
| `/api/v2/runs/<id>/status/` | GET | `runs:read` | Run 가벼운 status (폴링용). `{run_id, status, phase, is_terminal, result}` 만. `X-QGP-Poll-After` 동일. |
| `/api/v2/runs/<id>/console/<stage>/` | GET | `runs:read` | stage ∈ image\|deploy\|prod. `?tail=N`, 1MB 초과 시 자동 tail (50KB) |
| `/api/v2/runs/<id>/cancel/` | POST | `runs:control` | Run 취소 |
| `/api/v2/runs/<id>/rerun/` | POST | `runs:control` | 처음부터 재실행 (새 Run 반환, 201) |
| `/api/v2/runs/<id>/rerun-test/` | POST | `runs:control` | 테스트만 재실행 |
| `/api/v2/runs/<id>/rerun-deploy/` | POST | `runs:control` | 검수대상 배포만 재실행 (`DEPLOY_FAILED` 전용) |
| `/api/v2/runs/<id>/rerun-prod/` | POST | `runs:control` | 검수통과 배포만 재실행 (`PROD_DEPLOY_FAILED` 전용) |
| `/api/v2/runs/<id>/deploy/` | POST | `runs:control` | `WAITING_DEPLOY` 에서 수동 deploy 트리거 |
| `/api/v2/runs/<id>/prod-deploy/` | POST | `runs:control` | `QG_PASSED` 에서 수동 prod 트리거 |
| `/api/v2/runs/<id>/argocd/<stage>/retry/` | POST | `runs:control` | stage ∈ deploy\|prod. Argo CD sync 리셋 + 재시도 (202) |
| `/api/v2/suites/` | GET | `meta:read` | Suite 목록. 필터: `service`, `q`, `is_active` |
| `/api/v2/suites/<id>/` | GET | `meta:read` | Suite 상세 (jobs 포함) |
| `/api/v2/suites/<id>/runs/` | GET | `runs:read` | Suite Run 목록 |
| `/api/v2/suites/<id>/runs/` | POST | `runs:trigger` | Suite Run 트리거. body: `{"job_id": "..."}` (필수) |
| `/api/v2/suite-runs/<id>/` | GET | `runs:read` | Suite Run 단건 (호출 시 `_poll_run_status` 트리거) |
| `/api/v2/suite-runs/<id>/status/` | GET | `runs:read` | Suite Run 가벼운 status (폴링용). `{suite_run_id, status, phase, is_terminal, passed/failed/total_jobs}`. |
| `/api/v2/suite-runs/<id>/cancel/` | POST | `runs:control` | Suite Run 취소 (이미 COMPLETED 면 409 `not_actionable`) |
| `/api/v2/suite-job-runs/<id>/console/` | GET | `runs:read` | Suite Job 콘솔 로그. `?tail=N`, 1MB 초과시 자동 tail |

**라우팅 모듈 분리**: `apps/api_v2/urls.py` 가 `urls_meta.py` / `urls_runs.py` / `urls_controls.py` / `urls_suites.py` 를 include — 각 Phase 가 독립 파일에서 자기 라우팅을 정의해 통합 충돌을 피한다. 라우팅 추가 시 적절한 모듈을 골라 수정.

**제어 API 의 응답 형식**: 모든 control 류는 액션 후 Run 의 `QualityGateRunDetailSerializer` 본문을 200(또는 201/202)으로 돌려준다 — 외부 자동화가 동일 응답으로 후속 폴링을 바로 시작할 수 있다.

**`?include` 파싱** (메타 API): `latest_run` / `test_suites` 콤마 토큰. 미지정 시 해당 필드는 응답에서 빠짐.

**도메인 예외 → 에러 응답**: `apps.qualitygates.services.GateRunError` / `apps.suites.services.SuiteRunError` 의 `code` / `http_status` / `details` 가 `apps/api_v2/exceptions.py` 의 일반화된 핸들러에서 `{"error":{"code","message","details"}}` 로 변환됨.

**폴링 힌트 헤더 `X-QGP-Poll-After`**: `/api/v2/runs/<id>/` / `/api/v2/runs/<id>/status/` / `/api/v2/suite-runs/<id>/` / `/api/v2/suite-runs/<id>/status/` 응답에 포함. terminal 도달 시 `0` (stop), 그 외 `5` 초. 외부 자동화는 이 값을 보고 다음 폴링 간격 결정.

**phase 매핑** (`apps/api_v2/phase.py`): DB 의 세분 status (QG 14개 / Suite 3개) 를 외부 친화적 5~6단 phase (`queued` / `running` / `awaiting_input` / `succeeded` / `failed` / `cancelled`) 로 변환. 모든 Summary/Detail/Status 응답에 `phase` 필드로 함께 노출 — CI/CD 자동화는 `phase` 만 보면 충분.

**외부 호출자용 가이드**:
- `docs/api_v2_quickstart.md` — 키 발급부터 트리거/폴링/제어/콘솔 조회까지 시나리오 중심 curl 예제.
- `docs/api_guide.md` — 전체 엔드포인트 reference + 상태머신 + 응답 스키마 + Jenkins/GitHub Actions/Python 호출 예제.

| HTTP | code | 의미 |
|---|---|---|
| 400 | `invalid_parameters` | body 파라미터 형식 오류 |
| 400 | `job_required` | Suite Run 트리거에 `job_id` 없음 |
| 401 | `not_authenticated` | 인증 필요 |
| 401 | `api_key_invalid`/`api_key_revoked`/`api_key_expired` | 키 자체 문제 |
| 403 | `scope_required` | 스코프 부족 |
| 403 | `role_denied` | Viewer 가 write 시도 |
| 404 | `not_found` | 리소스 없음 |
| 409 | `run_already_active` | 이미 진행 중인 Run 있음 |
| 409 | `run_not_actionable` | 현재 상태에서 액션 불가 (details.current_status 포함) |
| 429 | `rate_limited` | Retry-After 헤더 포함 |
| 500 | `internal_error` | 미분류 예외 |

### 회원가입 플로우

`apps/accounts/views.py:signup`:
1. `is_active=False` 로 사용자 생성 → "승인 대기" 화면
2. 관리자가 `accounts:user_list` 에서 `is_active=True` + role 부여
3. 로그인 가능

세션은 24시간 만료 (`SESSION_COOKIE_AGE`, `base.py:112`), 매 요청 갱신 안 함.

---

## 8. 실행 / 배포

### 운영 환경

- 도메인: `https://qualitygate.jkintra.net` (ALB → EC2 HTTP, `SECURE_PROXY_SSL_HEADER`)
- 실행: `python manage.py runserver 0.0.0.0:8601` (단일 프로세스, 멀티스레드)
- venv: `/home/taeshkim/qa_qualitygate_portal/.venv` (Python 3.10+)
- 설정: `DJANGO_SETTINGS_MODULE=config.settings.dev` (production.py 도 있으나 운영은 dev.py 사용)
- DB: `db.sqlite3` (Git 에 포함). **충돌 시 로컬본을 최신으로** 유지.

### 시작 스크립트

`run.sh` / `run.ps1` / `run.bat` 가 동일 동작:
0. `origin/master` fast-forward(ff-only) pull 로 코드 최신화. git 미설치 / 브랜치가 `master` 아님 / `git fetch` 실패 / ff 불가 → 경고만 출력하고 현재 코드로 계속. **`db.sqlite3` 는 스크립트가 일절 write/checkout/reset 하지 않음** — ff-only 라 코드만 갱신하고, 원격이 `db.sqlite3` 를 바꾼 커밋이면 로컬 변경과 충돌해 ff 가 막혀 그 회차만 건너뜀 (DB 는 수동 명령어로만 변경). → 각 스크립트의 `[1/6] 최신 master 동기화` 블록.
1. python 3.13 → 3.12 → 3.11 → 3.10 → python3 순으로 인터프리터 탐색
2. `.venv` 가 3.10 미만이면 재생성
3. `pip install -q -r requirements.txt`
4. `.env` 가 없으면 `.env.example` 복사
5. `migrate --run-syncdb`
6. `runserver 0.0.0.0:8601`

### 환경 변수 (`.env`)

핵심:
- `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_SETTINGS_MODULE`
- `DJANGO_CSRF_TRUSTED_ORIGINS` (기본값 `https://qualitygate.jkintra.net`)

`.env.example` 에 보이는 PostgreSQL/Redis/Slack/TAS 변수들은 **현재 사용되지 않는다** (레거시).

### 라이브러리 (`requirements.txt`)

Django 4.2 / DRF / django-filter / python-dotenv / django-cors-headers / celery / redis / django-celery-beat. 의존성은 최소.

---

## 9. 캐싱

`apps/qualitygates/views.py:gate_run_params` 만 캐시 사용:
- 키: `qgp:` + sha1(`qg_params:{gate.pk}:{target_job}`)
- TTL: 120초
- `?refresh=1` 로 강제 무효화

Active Choices / Git Parameter 가 있는 Job 은 첫 호출이 5~15초 걸리므로 캐싱이 필수.

---

## 10. 자주 하는 변경

### "이미지/배포/검수통과 단계의 Jenkins 파라미터 입력 방식을 바꾸고 싶다"
- `gate_run_params` (`views.py:507`) — Run 시작 시 모달에 띄울 파라미터 JSON
- `deploy_params` (`views.py:657`) — WAITING_DEPLOY 모달용
- `prod_deploy_params` (`views.py:700`) — QG_PASSED 모달용. **APP_IMAGE 디폴트를 이 Run 의 image_build_number 로 오버라이드** 하는 로직이 들어 있음.
- 트리거 측: `gate_run` (`views.py:539`) / `deploy_trigger` / `prod_deploy`
- 클라이언트는 `_collect_params(post)` 가 `param_*` prefix 로 파라미터를 수집

### "Argo CD 가 Synced + Healthy 인데 다음 단계로 안 넘어간다"
- `_check_argocd_sync` 가 `is_synced_and_healthy` 를 호출 — operation_phase 가 `Running`/`Terminating` 이면 False 반환
- 4초 쿨다운 안에 들어왔다면 `{stage}_argocd_synced_at` 가 비어 있어도 False
- 수동 재시작: `argocd_retry(run, stage)` 또는 UI 의 **Argo CD 재시도** 버튼

### "테스트만 다시 돌리고 싶다"
- `rerun_test` 뷰 (`views.py`). 조건: `status=FAILED` and `deploy_completed_at` 있음.
- 이미 PASS 한 Suite 는 유지하고, **PASSED 가 아닌 모든 Suite (FAILED/PENDING/WAITING)** 를 PENDING 으로 리셋 → `WAITING_TEST` → `_advance_test`.
- 순차 모드와 병렬 모드 모두 같은 규칙으로 동작 — 순차에서는 결국 `current_suite_index` 이후 (= 실패한 Suite 부터) 리셋과 같은 집합이 된다. 병렬 모드에서는 추가로 `current_suite_index` 가 PASSED 카운트로 재설정된다 (UI 표시용).
- 병렬 모드에서는 `_advance_test` 가 PENDING 인 모든 Suite 를 다시 동시에 트리거한다.

### "검수대상 배포만 다시 돌리고 싶다"
- `deploy_trigger` 뷰가 두 가지 진입을 통합 처리 — `WAITING_DEPLOY` (정상 첫 트리거) + `DEPLOY_FAILED` (재시도). UI 버튼은 `_run_status.html` 의 `can_rerun_deploy_only` 분기. `prod_deploy` 와 동일한 패턴.
- 재시도 분기에서는 `deploy_*` 빌드 정보 + `deploy_argocd_*` 7개 필드를 비워 폴링 (`_poll_deploy` / `_check_argocd_sync`) 이 새 빌드 결과로 채우게 한다. 이미지 빌드 결과(`image_*`, `image_git_*`) 는 건드리지 않음.
- ActivityLog 에는 `rerun deploy only` 로 남는다.

### "하위폴더(SubFolder) 동작 변경"
- 모델: `apps/qualitygates/models.py` 의 `QualityGateSubFolder`. 인스턴스에 종속 (FK + `unique_together(folder, name)`).
- 뷰: `apps/qualitygates/views.py` `subfolder_register/detail/edit/delete` (`views.py:236` 부근). `gate_register` 는 `subfolder_pk` 인자를 받아 `subfolder.folder` 로 인스턴스 자격증명에 접근.
- 템플릿: `templates/qualitygates/subfolder_{detail,form,delete_confirm}.html`. `folder_detail.html` 은 하위폴더 카드 그리드만 보여주고, Gate 목록은 `subfolder_detail.html` 에서.
- `subfolder_detail.html` 의 "Last Result" 컬럼은 `run.status` 를 직접 분기 — `qg_passed` 는 별도 라벨로 노출하고 (status-na), `completed` 만 PASS 로 본다. `run.is_success` 는 두 상태 모두 True 라 PASS 만 보여주면 검수통과 환경 배포 대기 중인 Run 이 완전히 끝난 것처럼 보여 오해를 일으켰음.
- `QualityGateRun.status_display` (`models.py:473`) 가 **gate_type 에 따라 deploy/prod 단계 라벨을 환경명으로 동적 변환**. enum 의 `get_status_display()` 는 "Prod Deploying" / "Deploy Failed" 같은 generic 라벨이라 DEV_STG 게이트 (deploy=Dev, prod=Stg) 에서는 실제 환경과 어긋남. property 매핑:

| 상태 enum | DEV_STG 표시 | STG_PRD 표시 |
|---|---|---|
| `WAITING_DEPLOY` | Awaiting Dev Deploy | Awaiting Stg Deploy |
| `DEPLOYING` | Dev Deploying | Stg Deploying |
| `DEPLOY_FAILED` | Dev Deploy Failed | Stg Deploy Failed |
| `QG_PASSED` | QG Passed (Awaiting Stg Deploy) | QG Passed (Awaiting Prd Deploy) |
| `PROD_DEPLOYING` | **Stg Deploying** | Prd Deploying |
| `PROD_DEPLOY_FAILED` | **Stg Deploy Failed** | Prd Deploy Failed |

다른 상태 (`PENDING`, `BUILDING_IMAGE`, `TESTING`, `COMPLETED` 등) 는 환경 무관이라 `get_status_display()` 그대로 fall-through. `get_status_display` 를 노출하던 모든 템플릿(`_run_status`, `gate_detail`, `run_detail`, `subfolder_detail`, `dashboard/index`) 은 `status_display` 로 통일. 회귀 방지 테스트: `apps/qualitygates/tests.py:StatusDisplayTests` (DEV_STG / STG_PRD / non-env 3개).
- ActivityLog 라벨: `_gate_log_label(gate)` 가 `인스턴스/하위폴더/게이트명` 형태로 만들어 모든 Gate 관련 로그에 사용.

### "Image Build 단계의 git branch/commit 표시 변경"

- 추출 로직: `apps/integrations/jenkins.py:_extract_git_info(actions, *, prefer_remote_urls, prefer_branches)` — actions 안의 모든 BuildData 를 수집한 뒤, hint(URL/branch 정규화 비교) 로 매칭되는 BuildData 를 우선 선택. 매칭 없으면 첫 BuildData. 그래도 비면 ParametersAction 의 BRANCH/GIT_BRANCH/TAG 등 fallback. branch 정규화는 `_normalize_branch_name` (`origin/`, `refs/heads/`, `refs/remotes/origin/` prefix 제거), URL 정규화는 `_normalize_repo_url` (scheme/credential/`.git`/`/` 정리).
- Hint 자동 분리: `apps/qualitygates/views.py:_hint_from_image_params(image_params_json)` 가 값에서 `http(s)://`/`ssh://`/`git@`/`.git` 으로 끝나는 항목을 repo URL 후보, 그 외 string 을 branch 후보로 분리. 게이트마다 SCM_URL/BRANCH_NAME 같은 정확한 키 이름을 알 필요 없이 dict 의 모든 string 값에서 자동 매칭.
- 저장: `_poll_image_build` 가 폴링마다 `client.get_build_info(...)` 에 hint 를 넘겨 source 측 git branch/commit 을 받아 `run.image_git_branch` / `run.image_git_commit` 에 갱신. 빈 문자열은 덮어쓰지 않아 폴링 중간에 actions 가 비어 와도 지워지지 않는다.
- 표시: `templates/qualitygates/_run_status.html` 의 Image Build step body. commit 은 `slice:":8"` 로 8자리만 노출, 전체 SHA 는 `title` 속성에 들어있어 hover 로 확인.
- 과거 Run 백필: 새 hint 로직이 적용된 직후 한 번만 — `_hint_from_image_params` + `client.get_build_info(...)` 로 `image_git_branch/commit` 을 다시 채우는 일회성 스크립트로 처리(2026-05-04 운영). Jenkins 가 폐기한 오래된 빌드(404) 는 갱신 불가.
- deploy/prod 단계에 동일 정보를 추가하려면 모델에 `deploy_git_branch/commit`, `prod_git_branch/commit` 을 만들고 `_poll_deploy` / `_poll_prod_deploy` 에 같은 로직을 복사. 현재는 image 단계만.

### "auto_mode 토글 위치/동작 변경"
- POST 엔드포인트: `qualitygates:gate_toggle_auto` (`views.py`)
- 조건: `gate_type == DEV_STG` 이고 `request.user.can_write`
- 진입 함수: `_should_auto_advance(gate)`
- 적용 위치: `gate_run`, `_poll_image_build` (성공 분기), `_finalize_run` (PASS 분기)
- **자동 모드의 Git branch 자동 선택 규칙 변경**: `_pick_release_branch(choices)` (`views.py`) 의 정규식 `_RELEASE_BRANCH_RE = r"(?:^|/)(release/release_[^/\s]+)$"` 를 수정. 다른 컨벤션(예: hotfix branch 우선 등) 으로 바꾸려면 이 정규식 + 정렬 키만 손보면 된다. 일반 모달 흐름은 영향 없음 — `_collect_default_params_from_info(parameters)` 는 인자 없이도 호출되므로 기본 동작은 그대로.

### "테스트 병렬 실행 (`parallel_test`) 동작 변경"
- POST 엔드포인트: `qualitygates:gate_toggle_parallel` (`views.py` `gate_toggle_parallel`). 게이트 타입 무관.
- 핵심 분기는 `_advance_test` / `_poll_current_test` 가 `gate.parallel_test` 값으로 분기:
  - 순차: `_advance_test_sequential` / `_poll_test_sequential` — 기존 1-by-1 로직.
  - 병렬: `_advance_test_parallel` / `_poll_tests_parallel` — 모든 PENDING/WAITING Suite 를 한 번에 트리거 시도, TESTING 인 모든 Suite 를 매 폴링마다 폴링, 모두 terminal 상태(`PASSED`/`FAILED`) 에 도달해야 `_finalize_run` 호출.
- 공통 헬퍼:
  - `_try_trigger_test(test_run, run)` — Suite 의 활성 Job 중 idle 한 것 하나 찾아 트리거. 못 찾으면 WAITING 으로 두고 False.
  - `_poll_one_test_build(test_run)` — 단일 test_run 의 Jenkins build 상태 폴링/저장. terminal 도달 시 True.
  - `_resolve_parallel_run_status(run, test_runs)` — 병렬에서 run.status 와 `current_suite_index` (= 완료 카운트, UI 표시용) 갱신.
- **병렬 모드에서는 한 Suite 가 FAIL 해도 다른 Suite 가 끝날 때까지 폴링을 계속한다.** 즉시 `FAILED` 로 떨어뜨리지 않는다 (사용자 요청 사양 — "마지막까지 다 끝나야 다음 단계로").
- `current_suite_index` 의미가 모드별로 다름:
  - 순차: 현재 진행 중인 Suite 의 인덱스 (0-based).
  - 병렬: 지금까지 PASSED+FAILED 된 Suite 의 카운트 (UI 의 "X/N 완료" 표시용).
- `cancel_run`: 병렬 모드에서는 TESTING 상태 test_run 이 여러 개일 수 있어 모두 abort 한다.
- `rerun_test`: 모드 무관, "PASSED 아닌 모든 것" 을 PENDING 으로 리셋 후 `_advance_test`.

### "Reliability Test 통계 초기화 동작"

- 헬퍼: `apps/robustness/services.py:reset_schedule_stats(schedule)` — `started_at`/`last_triggered_at`/`stopped_at` 을 모두 **None** 으로 비운다. `is_running` 은 보존.
- `session_runs = runs.filter(triggered_at__gte=started_at)` 이고 `started_at=None` 이면 빈 쿼리셋이라 성공/실패/실행수/성공률/마지막 실행/세션 시작 표시가 깨끗하게 비어 보인다.
- running 인 스케줄을 reset 한 경우 다음 sweep 트리거에서 `_trigger_once` 가 `started_at` 을 자동으로 now 로 채워 그 이후 run 들이 정상 카운트된다. `_trigger_once` 시작에 `if schedule.started_at is None: schedule.started_at = now()` 1줄. 멈춰 있는 스케줄은 sweep 자체가 안 도니까 사용자가 다시 *시작* 을 누를 때 `start_schedule` 이 채운다.
- Row 별 reset: `robustness:schedule_reset` POST (`schedule_reset_stats` view).
- 일괄 reset: `robustness:schedule_reset_all` POST (`schedule_reset_all_stats` view). UI 는 *일괄 시작/중지* 옆의 *일괄 초기화* 버튼.

### "Reliability Test interval 도달 시 이전 사이클 마감 정책"

`apps/robustness/services.py:sweep_schedule` 이 interval 도달을 판정한 직후 `_trigger_once` 를 호출하기 직전에 `_timeout_unfinished_runs(schedule)` 를 호출. 동작:

- 해당 스케줄의 PENDING/RUNNING `RobustnessRun` 을 모두 수집.
- SuiteRun 의 잡 중 `PENDING/QUEUED/RUNNING` 은 모두 `FAILURE` 로 마킹 (ABORTED 아님 — 사용자 의도가 "실패로 처리").
- `QUEUED/RUNNING` 잡은 `jenkins_build_number` 가 있으면 `client.stop_build(...)` 로 Jenkins 빌드 종료까지 시도. 실패해도 상태 마킹은 그대로 진행 (logger.exception).
- 집계 갱신 (passed=SUCCESS 수, failed=FAILURE+ABORTED 수) 후 SuiteRun=`COMPLETED`, RobustnessRun=`FAILURE` 로 마감.

수동 트리거 (`schedule_trigger_now` 뷰) 는 이 로직을 거치지 않는다 — 사용자가 의도해서 또 돌리는 경우라 이전 사이클을 그대로 둔다. interval 기반 sweep 만 영향.

ABORTED 가 아니라 FAILURE 로 마킹하는 이유는 `stop_schedule` (사용자가 명시적으로 멈춤) 과 의미를 분리하기 위함 — timeout 은 "스케줄은 계속 도는데 그 잡이 시간 안에 못 끝남" 이라 통계상 실패로 잡혀야 한다.

### "Reliability Test sweep race-free 트리거 결정"

`sweep_schedule` 는 5초 HTMX 폴링/여러 워커에 의해 동시 호출될 수 있다. 과거 구현은 `last_triggered_at` 을 비교해서 `should_trigger=True` 면 `_trigger_once` 까지 직진했는데, 동시 호출 시 모두 같은 옛 값을 보고 `True` 로 평가되어 **같은 사이클에 RobustnessRun 이 여러 개 생성**되는 race 가 있었다 (예: `_timeout_unfinished_runs` 가 직전 sweep 의 RUNNING 잡을 즉시 FAILURE 로 마킹하고, 같은 빌드번호로 다음 Run 이 추적을 이어가는 식의 잘못된 집계).

`apps/robustness/services.py:sweep_schedule` 는 트리거 결정을 **conditional UPDATE** 로 한다 — 본 `last_triggered_at` 값과 같을 때만 `now` 로 set. 동시 호출 중 정확히 1개 워커만 1 row affected 를 받고 통과, 나머지는 0 row 라 즉시 return. 이후 `_timeout_unfinished_runs` → `_trigger_once` 는 단독으로 진행.

```python
if prev_lta is None:
    qs = qs.filter(last_triggered_at__isnull=True)
else:
    if prev_lta > now - timedelta(minutes=schedule.interval_minutes):
        return  # interval 아직 안 지남
    qs = qs.filter(last_triggered_at=prev_lta)
updated = qs.update(last_triggered_at=now)
if not updated:
    return
```

`_trigger_once` 끝에서 `last_triggered_at` 을 또 한 번 set 하지만, conditional UPDATE 가 이미 같은 now 값으로 잡았기 때문에 redundant overwrite 이고 race 에는 영향 없다 (수동 트리거 `schedule_trigger_now` 경로에서는 이 끝줄 갱신이 의미 있음).

### "Reliability Test 잡 병렬 실행 (`parallel_jobs`) 동작 변경"

**테스트별** (RobustnessTest 단위) 마스터 토글. 기본 Off (순차). 자동 트리거(interval) / 수동 트리거(`schedule_trigger_now`) 모두 동일하게 모드를 따른다. 한 테스트 안의 모든 스케줄이 동일 모드를 공유 — row 별로 다른 모드는 불가.

- 모델 필드: `apps/robustness/models.py:RobustnessTest.parallel_jobs` (BooleanField, default False). 과거에 `RobustnessSchedule` 에 동일 필드가 잠시 있었으나 한 토글로 통일하면서 test 레벨로 이전 (마이그레이션 `0005_remove_robustnessschedule_parallel_jobs_and_more.py` 의 RunPython 으로 값 이전).
- POST 토글: `robustness:test_toggle_parallel` (`apps/robustness/views.py:test_toggle_parallel`). HX-Request 면 204, 아니면 detail 로 redirect. `ActivityLog` 에 `parallel_jobs=on|off` 로 기록.
- 트리거 분기는 `apps/robustness/services.py:_trigger_once` 가 `schedule.robustness_test.parallel_jobs` 로:
  - 순차 (`False`): 기존대로 `_try_trigger_next(suite_run)` — Suite 의 PENDING 잡 중 1개만 트리거. 이후 `_poll_run_status` 가 잡 완료마다 다음 PENDING 잡을 트리거 (suites/views.py).
  - 병렬 (`True`): `_trigger_all_pending(suite_run)` — 모든 PENDING 잡에 대해 한 번씩 `client.trigger_build`. 이미 Jenkins 에서 돌고 있는 잡은 그 빌드를 추적(RUNNING), 트리거 실패는 즉시 FAILURE 처리. 폴링은 동일한 `_poll_run_status` 가 처리 — 모든 잡이 SUCCESS/FAILURE/ABORTED 로 끝나야 SuiteRun 이 COMPLETED.
- UI: `templates/robustness/_schedule_table.html` 의 *일괄 시작* 옆에 토글 버튼 (스케줄이 1개 이상이고 `can_write` 일 때 노출).
- 일괄 시작 (`schedule_start_all`) 은 모드를 건드리지 않는다 — 현재 토글 상태로 모든 스케줄을 시작.

### "취소(abort) 동작 변경"
- `cancel_run` 뷰 (`views.py:848`). 현재 단계의 Jenkins 빌드를 stop 시도하고 `status=CANCELLED`.
- TESTING 단계는 현재 진행 중인 `QualityGateRunTest` 의 `job_run` 을 통해 Jenkins build 를 abort 한 뒤 SuiteRun/JobRun 상태 정리.

### "새 Run 을 시작했더니 이전 Run 이 사라졌다"
- 정상 동작. `gate_run` 진입 시 `WAITING_USER_STATUSES` (waiting_deploy, qg_passed) 의 이전 Run 을 자동 CANCELLED 처리 (`views.py:548`). `ACTIVE_STATUSES` 의 Run 이 있으면 새 Run 자체가 차단됨.

### "테스트 영역 카운트(N/M) 가 stale"

- 표시 값은 `_compute_section_counts(run, test_runs)` 의 `sec2_total` (= `len(test_runs)`) 가 단일 출처. `run.total_suites` 모델 필드는 Run 생성 시 fix 된 값이라 게이트에서 suite 가 빠진 후엔 stale — `_run_status.html` 의 진행 텍스트도 `sec2_total` 로 통일.
- `_compute_section_counts` 가 호출될 때마다 `run.total_suites != sec2_total` 이면 자동 동기화 — admin 표시도 일치.
- 게이트에서 `QualityGateTestSuite` 가 삭제되면 `QualityGateRunTest.gate_suite` FK 의 `on_delete=CASCADE` 로 진행 중/끝난 Run 의 test_runs 도 함께 삭제 → `len(test_runs)` 자동 감소.

### "Run 상세 / Suite Run 페이지 브라우저 탭 title 변경"
- `templates/qualitygates/run_detail.html` `{% block title %}` — `{{ run.gate.name }} - Quality Gate Portal`. 게이트 이름이 탭에 노출돼 여러 application pipeline 을 동시에 띄울 때 구분 용이.
- `templates/suites/run_detail.html` 도 동일하게 `{{ suite_run.suite.name }}` 노출.
- 다른 정보(빌드 번호/상태 등) 를 더 추가하려면 같은 자리에 보탠다. htmx 폴링은 본문만 교체하므로 상태 변화에 따른 동적 갱신이 필요하면 별도 JS (예: `document.title = ...`) 를 status fragment 응답에서 처리해야 한다.

### "OS 알림 메시지 변경"
- `static/js/qg_notify.js:494` `QG_RUN_NOTIFY_MAP`. 캐시 버스터로 base.html 의 `?v=...` 쿼리스트링도 함께 올릴 것.

### "Slack 알림 메시지 포맷 / 색상 / 이모지 변경"
- `apps/notifications/services.py:_EVENT_META` — event_type 별 (toggle 필드, 색상, 이모지, 라벨) 튜플. 색상 hex 와 이모지 코드만 바꾸면 즉시 반영.
- `_post_message` 의 attachment 구조 (`title`, `title_link`, `fields`) 를 손보면 메시지 레이아웃 자체를 변경 가능.
- 메시지에 추가 필드를 노출하고 싶으면 호출 측에서 `extra={"키": "값"}` dict 로 넘기면 자동으로 attachment fields 에 추가된다.

### "Slack 알림 이벤트 추가"
- `apps/notifications/events.py` 에 새 상수 추가 + `EVENT_TABS` 의 적절한 탭/그룹에 등록 (자동으로 `EVENT_GROUPS` flat 에도 반영됨).
- `apps/notifications/models.py:SlackNotificationSettings` 에 `notify_<code>` BooleanField 추가 → makemigrations.
- `services.py:_EVENT_META` 에 새 항목 등록.
- `qualitygates/views.py` 의 해당 전이 지점에 `send_event_async(nev.<NEW>, run, ...)` 호출 삽입.
- `templates/accounts/notification_settings.html` 의 체크박스 분기에 `{% if code == '<new>' and settings_obj.notify_<new> %}checked{% endif %}` 한 줄 추가.

### "API 엔드포인트별 담당자 매핑 — Google Sheets 동기화 / 수동 row / URL 매칭"

- **모델**: `apps/integrations/models.py` 의 `ApiEndpointOwner`, `ApiHostMapping` (§3 참조).

#### Google Sheets 동기화 (CSV 업로드 대체, 2026-05-27)
- **단일 진실 소스 = Google Sheets**. 시트 컬럼이 기존 CSV 헤더(레포/카테고리/영역/Method/Endpoint/FE 담당자/BE 담당자/매핑 앱/사용 횟수/step_ids)와 동일해 **임포트 파서를 그대로 재사용** — 시트를 CSV 로 받아 `import_api_endpoint_owners` 로 흘려보낸다.
- **외부 연동 모듈**: `apps/integrations/google_sheets.py`.
  - `export_sheet_csv(spreadsheet_id=None) -> bytes` — 서비스 계정으로 **Drive API `files.export(mimeType=text/csv)`** 호출 (첫 탭 gid=0 만). 실패 시 `GoogleSheetSyncError`.
  - 인증: `_load_credentials()` 가 자격증명을 만든다. 우선순위 ① `GOOGLE_CREDENTIALS_FILE`(파일 경로) → `google.auth.load_credentials_from_file` 가 `type` 자동 판별 (`service_account` / `authorized_user` / `external_account`=WIF). ② 인라인 서비스 계정 `load_service_account_info()` (`GOOGLE_SA_JSON_B64` → `GOOGLE_SA_JSON` → `GOOGLE_SA_FILE`, type=service_account 강제). `get_spreadsheet_id()` 는 `GSHEET_OWNERS_SPREADSHEET_ID`. (slack_users.py 가 토큰을 os.getenv 로 직접 읽는 컨벤션과 동일 — settings 에 안 둠.)
  - **운영 환경 = AWS Workload Identity Federation (external_account)**. `secrets/qg-owners-wif.json` 의 `credential_source.environment_id=aws1` → EC2 인스턴스 메타데이터(IMDSv2, `169.254.169.254`)에서 AWS 자격을 읽어 STS(`sts.googleapis.com`)로 토큰 교환 → `qg-gdocs-reader@team-qpe.iam.gserviceaccount.com` 임퍼소네이션. 키 파일(장기 비밀) 없음. 시트는 이 임퍼소네이트 SA 에 '뷰어' 공유돼 있어야 함.
  - `get_service_account_email()` — 시트 공유 안내용 주체 이메일. SA 면 `client_email`, external_account 면 `service_account_impersonation_url` 에서 SA 이메일 추출(`_impersonation_email`), authorized_user 면 None. 담당자 관리 화면 상단에 노출.
  - scope: `https://www.googleapis.com/auth/drive.readonly`. `_build_session()` 이 `google.auth.transport.requests.AuthorizedSession` 으로 토큰 자동 갱신. google-auth/requests 는 함수 내부 import (미설치 시 모듈 import 자체는 안 깨짐).
- **동기화 서비스**: `services.sync_owners_from_google_sheet(*, user=None, dry_run=False, spreadsheet_id=None)`.
  - `export_sheet_csv` → `io.BytesIO` → `import_api_endpoint_owners(remove_stale=True, source_label="Google Sheet")`. **항상 full mirror** (시트에 없는 imported row 삭제, 수동 row 보존). 성공 시 `invalidate_match_cache()`.
- **트리거 2경로**:
  1. **cron (주기 자동)** — **매분 `--if-due` 로 호출**하고 실제 주기는 화면에서 설정한다 (crontab 한 줄 고정):
     `* * * * * cd <repo> && .venv/bin/python manage.py sync_api_owners_from_sheet --if-due >> /tmp/qg_owner_sync.log 2>&1`.
     `--if-due` 는 `OwnerSyncSettings.interval_minutes`(분, 0=끔) 와 마지막 동기화 시각(ActivityLog `API_OWNER_IMPORTED` 의 `created_at`) 을 비교해 **주기 도래 시에만** 동기화 — `Command._due_status()` 가 (실행여부, 사유) 반환. cron 에선 `GoogleSheetSyncError` 를 경고만 하고 0 종료(다음 분 자동 재시도). 인자 없이 호출하면 즉시 1회 동기화.
  2. **수동 버튼** — 담당자 관리 화면의 'Google Sheets 동기화' → `accounts:api_owner_sync` (POST only) → 같은 서비스 호출.
- **자동 동기화 주기 설정** = `OwnerSyncSettings` (singleton, `get_solo()`, `db_table=integrations_owner_sync_settings`). 화면 하단 '자동 동기화 주기' 카드 → `accounts:owner_sync_settings_save` (POST only) 가 `interval_minutes`(>=0, 음수는 0 보정, 비정수는 거부) 저장 + ActivityLog `UPDATE`. **0 이면 자동 동기화 끔** (cron 이 매분 skip).
- `GoogleSheetSyncError` (설정 누락/인증 실패/미공유/시트 조회 실패) 는 view 에서 `messages.error`, 관리 명령(인자 없음)에서 `CommandError`, `--if-due` 에선 경고 로그로 변환.

#### 임포트 파서 + 레거시 CSV CLI
- `apps/integrations/services.py:import_api_endpoint_owners(csv_file, *, user, dry_run=False, remove_stale=True, source_label="CSV")`.
  `ImportResult` dataclass 가 created/updated/skipped_manual/deleted_stale/errors 를 반환. `source_label` 은 ActivityLog 표기용 ("CSV" / "Google Sheet").
  `transaction.atomic()` 안에서 `update_or_create((method, endpoint))` 루프 — repo 는 defaults 로 넘겨 update 대상. `KEY_FIELDS = ("method", "endpoint")` 상수.
  헤더 누락 / 비정수 사용횟수 / 빈 행 모두 안전. errors 5개 이상이면 즉시 rollback. `_open_csv` 는 path / text / **binary(BytesIO)** 모두 허용 (utf-8-sig).
- **레거시 CLI** (파일 경로 직접 임포트, 디버그용 보존): `.venv/bin/python manage.py import_api_owners <csv_path> [--dry-run] [--remove-stale]`. 운영 동기화는 위 `sync_api_owners_from_sheet` 를 쓴다.
- **수동 row 보호**: `is_imported=False` 인 row 는 import/동기화가 절대 덮어쓰지 않는다. `skipped_manual` 카운트로 확인.
- **시트/CSV 내부·DB 간 중복**: 같은 `(method, endpoint)` 가 등장하면 마지막 행이 update (repo 도 덮어씀). 다른 repo 라벨로 같은 endpoint 가 들어와도 새 row 가 생기지 않음 — 2026-05-20 변경.
- **`remove_stale`** 도 키 기준으로 동작 — `(method, endpoint)` 가 입력에 없으면 imported row 삭제 후보. (동기화는 항상 True.)

#### Admin UI (`/accounts/api-owners/`, `/accounts/api-hosts/`)
- view: `apps/integrations/views.py` — `api_owner_list/create/sync/edit/delete/export`, `api_host_list/create/edit/delete`, `owner_sync_settings_save`. `api_owner_sync` / `owner_sync_settings_save` 는 **POST only** (`@require_POST`) — 처리 후 list 로 redirect. (구 `api_owner_import` 폼 view 는 2026-05-27 제거.)
- 라우팅: `apps/accounts/urls.py` 가 `from apps.integrations import views as int_views` 로 위임 등록. URL name 은 `accounts` namespace 안에 (`accounts:api_owner_list`, `accounts:api_owner_sync` 등).
- 권한: 모든 view 가 `@login_required` + `_require_admin(request.user)` 가드.
- 폼: `apps/integrations/forms.py` 의 2개 — `ApiEndpointOwnerForm` / `ApiHostMappingForm` (ModelForm). (`ApiOwnerImportForm` 은 CSV 업로드 제거와 함께 삭제.)
- 탬플릿: `templates/accounts/api_owner_list.html` / `api_owner_form.html` / `api_host_list.html` / `api_host_form.html`. `api_owner_list.html` 헤더의 'Google Sheets 동기화' POST 버튼 + 시트 링크/자동 동기화 주기/마지막 동기화/공유 주체 이메일 안내, 하단에 **'자동 동기화 주기' 카드** (분 입력 폼 → `owner_sync_settings_save`) + '대체 담당자' 카드. `_admin_tabs.html` 에 2개 탭 추가됨. `base.html:50-56` sidebar active 조건에도 `api_owner_*` / `api_host_*` url_name 추가. (구 `api_owner_import.html` 삭제.)
- list view context: `last_sync` (최근 `API_OWNER_IMPORTED` ActivityLog), `sheet_url`, `sa_email`, `sync_interval_minutes` — 화면 상단 안내 + 주기 폼 prefill. (`last_sync.detail` 은 `생성 N · 수정 N · 수동 보존 N · 삭제 N · 오류 N` 한국어 포맷 — `services.py` 의 ActivityLog 기록부에서 생성.)
- 페이지네이션 — `Paginator(queryset, 50)`. GET `page`. ActivityLog `views.py:23-24` 패턴 그대로.
- 필터 (GET 파라미터): `q` (전체 LIKE), `repo`, `mapped_app`, `team`, `is_imported`.
- export.csv 는 UTF-8 BOM (`b"\xef\xbb\xbf"`) 한 번만 prepend. content_type=text/csv. 헤더는 한국어 12개 그대로.
- prefill 동선: `?method=...&endpoint=...&host=...` 쿼리로 create 폼이 초기값을 채움 — Phase 4 의 Error Log "+ 등록" 버튼이 이 경로를 사용.
- ActivityLog: 수동 CRUD 는 `API_OWNER_CREATED/UPDATED/DELETED`, 임포트는 `API_OWNER_IMPORTED`, 호스트 매핑은 `API_HOST_*`. target_type = `API_ENDPOINT_OWNER` / `API_HOST_MAPPING`.

#### URL 매칭 엔진 (`apps/integrations/url_matcher.py`)
- 공개 API: `find_endpoint_owner(method, url) -> ApiEndpointOwner | None`, `extract_urls(text) -> list[MatchedUrl]`, `invalidate_match_cache()`.
- **캐시 무효화 3중 안전망 (2026-05-20)**:
  1. `post_save`/`post_delete` signal — `ApiEndpointOwner` / `ApiHostMapping` 변경 시 자동.
  2. Admin CRUD view (`api_owner_create/edit/delete`) + 동기화 (`sync_owners_from_google_sheet`) 직후 명시적 `invalidate_match_cache()` 호출 — multi-worker 환경에서 signal 이 한 worker 에만 도달하는 케이스 방어.
  3. Error Log 페이지 view (`_render_error_log`, `job_run_error_log`) 진입 시 무조건 `invalidate_match_cache()` 호출 — 사용자가 본 페이지가 항상 fresh 한 담당자 정보를 보장. 비용: LocMem `cache.delete` + index rebuild (~ 수십 ms / 300 row). 동일 페이지 안의 여러 매칭 호출은 캐시 hit.
- 회귀 방지 테스트: `test_owner_update_reflected_in_matcher_after_save` — owner.save() 후 find_endpoint_owner 가 새 값 반환하는지.
- `MatchedUrl(url, method, count, owner, status_code)` dataclass.
- **path-only 매칭** — CSV endpoint 의 `${HOST_VAR}/` prefix 는 컴파일 시 떼어내고 path 부분만 정규식화. `${PARAM}` (path param) → `[^/]+`. 리터럴은 `re.escape`. query string `?...` 제거.
- 입력 URL 은 `urlsplit` 으로 path 만 사용 (netloc 매칭 안 함).
- 컴파일 결과 인덱스: `{method: [{pk, pattern, host_var, raw_path, specificity}, ...]}`. LocMem 캐시 키 `qgp:api_owner_patterns:v1`, `qgp:api_host_mappings:v1`. `post_save`/`post_delete` 시그널이 자동 무효화 (`apps/integrations/url_matcher.py` 안에 receiver, `apps/integrations/apps.py:ready()` 가 모듈 import 로 등록).
- 동률 처리 (improvement.md §4.2 단계 4):
  1. specificity (리터럴 segment 수) 최대.
  2. `ApiHostMapping` 의 host_pattern 으로 netloc → variable_name 추론된 row 우선 (`fnmatch.fnmatchcase`, `*` 적은 패턴 우선).
  3. host_var 채워진 row > host_var None 인 row.
  4. repo↔netloc 키워드 가점 (`AM`↔`albamon`, `JK`↔`jobkorea`).
  5. usage_count 큰 row.
  6. pk 안정 정렬.
- `extract_urls()`: `URL:`, `URL :`, 단독 `https?://\S+` 모두 인식. 같은 step 블록 안의 `Method:` / `Status:` 를 url 에 묶음. 블록 구분은 `===` 5+ / `@@@` 5+ / 빈 줄 3+. dedupe + count, 결과는 count 내림차순 → url 사전순.
- Method 는 항상 strict 매칭 (같은 path 가 GET/POST 다른 owner 일 수 있어서).

#### 단위 테스트 (`apps/integrations/tests.py`)
- 상세는 §"Error Log 화면 통합" 뒤의 "단위 테스트" 항목 참조 (총 25 개).

#### Error Log 화면 통합 (`_render_error_log`, `job_run_error_log`)
- `apps/qualitygates/views.py:_render_error_log` 와 `apps/suites/views.py:job_run_error_log` 가 **`extract_urls(error_log or "")`** 를 호출해 `matched_urls` 컨텍스트를 채움. console 원본 전체가 아니라 `extract_error_log(...)` 의 추출본(추출된 에러 카드와 같은 텍스트) 안의 URL 만 매칭 대상으로 좁힌다 — 정상 step 의 URL 까지 표에 나오면 누가 F/U 해야 할지 한눈에 안 들어와 사용자가 명시적으로 좁혀달라 요청 (2026-05-20).
- 템플릿 `templates/qualitygates/error_log.html`, `templates/suites/error_log.html` 의 "추출된 에러" 카드 위에 카드 제목 **"이슈 F/U 담당자"** 로 표 렌더. 표 컬럼: URL / Method / 호출 횟수 / Status / 4xx 담당자 / 5xx 담당자 / 액션(편집 또는 +등록). 컬럼명은 `4xx/5xx 담당자` 로 통일 (팀 정보는 한 셀 안에 `이름 (팀)` 형태).
- Status 컬럼은 `status_code >= 500` → status-fail, `>= 400` → status-na, 그 외 → status-pass 로 색 분기.
- **F/U 책임 음영** — 행의 status_code 가 4xx (400~499) 면 4xx 담당자 td 가 옅은 노랑(`rgba(255,193,7,0.18)`) + bold + tooltip. 5xx (>=500) 면 5xx 담당자 td 가 옅은 빨강(`rgba(220,53,69,0.18)`). 2xx/3xx/None 이면 음영 X. 어느 담당자가 F/U 해야 할지 한눈에 보이기 위함.
- 매칭 실패 URL 은 admin 한정 "+ 등록" 버튼이 `{% url 'accounts:api_owner_create' %}?method=...&endpoint={{ url|url_path|urlencode }}&host={{ url|url_host|urlencode }}` 로 이동. endpoint 는 URL 의 path 부분, host 는 netloc 만 분리해 prefill — Phase 2 의 `api_owner_create` GET 분기가 `request.GET` 에서 받아 form initial 로 채움. host 는 모델 필드가 없어 `notes` 에 `host=...` 형태로 prefill + 폼 상단에 `prefill_host` 안내 박스.
- 커스텀 템플릿태그 `{% load url_filters %}` (정의: `apps/integrations/templatetags/url_filters.py` 의 `url_path`, `url_host`).

#### 회귀 fix — `_BLOCK_SEP_RE` catastrophic backtracking (2026-05-20)
- `apps/integrations/url_matcher.py:_BLOCK_SEP_RE` 의 초기 패턴 `(?:^|\n)(?:=+|@+){5,}\s*\n|...` 은 그룹 안에 또 `+` 가 들어있어 `===` 70자 라인이 들어오면 지수 시간 백트래킹에 빠진다 (실측: 48KB pytest 콘솔에서 무한 대기).
- 평탄한 형태 `(?:^|\n)(?:={5,}|@{5,})\s*\n|...` 로 치환 → 동일 콘솔이 0.1초에 처리.
- 회귀 방지 단위 테스트 `test_extract_urls_handles_long_separator_lines_fast` — 60자 `===` 라인 100개 + `@@@` 라인 50개 텍스트를 1초 안에 처리하는지 검증. 향후 정규식 손댈 때 이 테스트가 깨지면 backtracking 회귀.

#### 단위 테스트 (`apps/integrations/tests.py`, 총 27 개)
- `ImportApiOwnersTests` 8 — 헤더 누락 / created / quoted-comma escape / updated / skip manual / blank rows / dry-run / remove-stale.
- `UrlMatcherTests` 8 — AM env-var / JK 절대경로 / `${RECRUIT_NO}` 와일드카드 / Method strict / Longest specificity / 매칭 없음 / 호스트 매핑 없이도 동작 / host_var tie-break.
- `ExtractUrlsTests` 11 — `URL:` 라벨 + 단독 형태 / dedupe + count / count 정렬 / 긴 separator backtracking 회귀 방지 / Slack 자동 매칭 + plain fallback (`build_mentions`) / `_split_names` 콤마 처리 / `_names_for_status` 4xx vs 5xx / `excerpt_error` 길이 + 백틱 escape / `api_owner_notify_error` POST → 302 + ActivityLog / `slack_users._build_name_index` deleted/bot 제외 + 5개 이름 키 / `_name_head` 팀 접미사 제거 + `이름(팀)` → `이름` 키 자동 등록.

#### 운영 메모
- 본 import/CRUD 는 admin 전용. 일반/Viewer 는 Error Log 화면에서 결과만 본다 (담당자/팀 정보는 평문 공개 정보로 분류).
- ApiHostMapping 0건 상태에서도 path-only 매칭이 동작하므로 첫 도입 시 호스트 매핑은 비워둬도 OK. 같은 path 가 AM/JK 양쪽에 등록된 케이스에서만 추가 등록 필요.
- LocMem 캐시는 단일 프로세스. runserver 멀티스레드 OK. multi-worker 전환 시 signal 무효화는 worker 1개에만 닿으므로 재검토 필요 (현재 환경엔 영향 없음).

### "Slack 에러 노티에 담당자 멘션을 걸고 에러 발췌/포털 링크를 함께 보내고 싶다"

#### 핵심 파일
- 사용자 매칭: `apps/integrations/slack_users.py:find_user_id_by_name(name) -> U_ID | None` — Slack `users.list` 결과(1시간 LocMem 캐시) 에서 이름 정확 매칭. SLACK_BOT_TOKEN 의 `users:read` scope 필요. §3 참조.
- 메시지 빌더: `apps/notifications/owner_mentions.py`
  - `build_mentions(owners_with_status) -> (mention_text, unresolved_names)` — `(owner, status_code)` 쌍 리스트를 받아 status 별로 적절한 owner 이름들을 union → `find_user_id_by_name` 으로 자동 조회 → `<@U0XXX> <@U0YYY> @매칭실패이름` 형태로 합친다. unresolved 는 워크스페이스 매칭 실패한 이름 (로깅용).
  - `excerpt_error(text, max_chars=1500)` — 긴 에러 본문 자르고 마지막에 `... (이하 N자 생략)`. 본문에 백틱 3개(```` ``` ````) 가 있으면 zero-width-space 로 escape 해 code block 깨짐 방지.
  - `build_error_followup_text(matches, error_excerpt, portal_log_url, context_label, mention_text)` — Slack mrkdwn 본문(멘션 + 컨텍스트 + URL 표 최대 5개 + 에러 발췌 code block + 포털 로그 링크). 4000자 안전 절단.
  - 상수 `SLACK_TEXT_LIMIT = 4000`.
- 자동 발송 (Suite/QG 내부 Test Suite 가 실패할 때):
  - `apps/notifications/services.py:_build_followup_for_job_run(job_run_id, context_label_extra)` — `TestSuiteJobRun` 1건을 받아 console_output → `extract_error_log` → `extract_urls` → `build_mentions` + `build_error_followup_text` 로 한 문자열(`이슈 F/U` 필드값)을 만든다. 실패 시 silent.
  - `_qg_event_fields(TEST_SUITE_FAILED, ...)` 와 `_suite_event_fields(SUITE_RUN_FAILED, ...)` 에서 호출. SUITE_RUN_FAILED 는 메시지 길이 보호를 위해 **실패 job 중 첫 1건만** followup 첨부.
- 수동 발송 (Error Log 페이지 버튼):
  - View: `apps/integrations/views.py:api_owner_notify_error(request, pk)` — POST 전용, `can_write` 권한. POST body: `url`, `method`, `status_code`, `source_type` (`suite_job`|`qg_run`), `source_id`, `stage` (qg_run 일 때 `image`|`deploy`|`prod`).
  - 흐름: source 로부터 console_output / context_label / portal_log_url 추출 → `extract_error_log` → `excerpt_error` → `build_mentions` + `build_error_followup_text` → `nsvc._post_message(channel, text)` → `ActivityLog.OWNER_NOTIFIED` 1건. 토큰/채널 부재 시 사용자에게 명시적 messages.error.
  - URL: `accounts:api_owner_notify_error` (`/accounts/api-owners/<pk>/notify-error/`).
- 템플릿 통합 — `templates/qualitygates/error_log.html` / `templates/suites/error_log.html` 의 이슈 F/U 담당자 표 우측 셀에 `{% if user.can_write %}` 분기로 `<form action="{% url 'accounts:api_owner_notify_error' m.owner.pk %}" method="post" ...>` + hidden inputs (`url`, `method`, `status_code`, `source_type`, `source_id`, `stage`) + `<button class="btn btn-sm btn-warning">`. confirm 다이얼로그 1번 띄움.
- 컨텍스트 source 식별자 — view 가 `notify_source_type` (`suite_job`|`qg_run`), `notify_source_id`, `notify_stage` 를 템플릿에 전달:
  - QG: `apps/qualitygates/views.py:_render_error_log` — `console_url_name` 끝 segment 로 stage 추론 (`image_console` → `image`).
  - Suite: `apps/suites/views.py:job_run_error_log` — source_type=`suite_job`, stage="".

#### 운영 메모
- 자동 발송은 토글 OFF 또는 토큰/채널 없으면 silent. 수동 발송은 `_post_message` 가 실패 사유를 `messages.error` 로 사용자에게 노출.
- 매칭 실패한 이름은 `@이름` plain text 로 노출 (실제 Slack 알림은 가지 않지만 메시지엔 등장). 수동 발송 화면의 success message 에도 매칭 실패 이름 목록 표시.
- 메시지 본문 길이는 `SLACK_TEXT_LIMIT=4000` 이내로 안전 절단 — code block 안 자르고 종료 처리.
- CSV 의 owner 컬럼 값과 Slack workspace 의 `name` / `real_name` / `display_name` (+ normalized 변형) 중 어느 하나라도 trim+lower 비교로 일치하면 매칭. **콤마 다중값** (`이동훈, 윤복영`) 은 split 후 각 토큰을 단독 조회.
- **자동 매칭이 안 되는 경우** — (a) `users:read` scope 가 SLACK_BOT_TOKEN 에 없음 (Slack 앱 설정에서 추가 + 워크스페이스 재설치 1회), (b) Slack 프로필의 표시명/실명과 CSV 표기가 다름 (예: "이동훈 (Backend)" vs "이동훈" — 정확 일치 필요), (c) workspace 가 5000명 초과 (`MAX_PAGES=20` × `limit=200` = 4000 한도, 필요 시 상수 조정), (d) Slack API 일시 장애. 매칭 실패는 `@이름` plain text 로 노출되니 사고로 이어지진 않지만 알림이 안 가서 인지 지연이 생길 수 있음. 사용자 측의 워크스페이스 표시명 정리가 운영 책임.
- 과거 `OwnerSlackHandle` 모델/Admin 메뉴가 있었으나 자동 매칭 전환과 함께 제거됨 (migration 0004_delete_ownerslackhandle). ActivityLog 의 `slack_handle_*` action enum / `owner_slack_handle` target_type 도 제거됨 (migration `activitylog/0006_alter_activitylog_action_and_more`). 과거 row 의 raw 값은 보존되며 display label 만 raw 그대로 표시.

#### 디버그 페이지 (`/accounts/slack-users/`)
- view: `apps/integrations/views.py:slack_users_debug` (admin only).
- 노출 정보: 현재 인덱싱된 멤버 수, 마지막 `users.list` 호출 실패 사유 (`get_last_error()`), 이름 검색 결과 (정확 매칭 U_ID + 부분 일치 후보 최대 15개), 인덱스 전체 키 샘플 (200개까지).
- 액션: "Slack 에서 다시 조회" (POST, `invalidate_user_cache` + `get_user_index(force_refresh=True)`), 이름 검색 (GET `?q=...`).
- 사용 시나리오:
  - 자동 매칭 0건 + last_error 표시 → scope 문제 또는 인증 오류. 메시지가 안내하는 조치 (Reinstall 등) 적용 후 캐시 갱신.
  - 매칭 N건 있지만 특정 이름이 안 됨 → 검색해서 부분 일치 후보 노출. Slack 표시명 vs CSV 표기 차이 확인.
  - 캐시가 stale (사람 입퇴사) → 캐시 갱신 버튼 클릭.

### "Error Log 버튼이 보여주는 내용 / 파싱 규칙 변경"

- 파서: `apps/integrations/log_errors.py:extract_error_log(console_output)` — 콘솔 로그에서 실패 원인 부분만 잘라 문자열로 반환. 우선순위:
  1. `@@@...@@@` (10자 이상 연속된 `@`) 라인 사이 블록 — 사내 자동화 스크립트(`am-mapp-regression-api-*` 등)가 최종 실패 step 을 의도적으로 감싼 영역. 사용자 보고 기준 정답 케이스.
  2. pytest `=== FAILURES ===` 블록 + `=== short test summary info ===` (다음 `=== ... ===` 헤더 직전까지).
  3. `❌ [Step]` / `[FAIL]` / `[STOP]` / `FAILED ` / `AssertionError` 키워드 라인 ±2 줄 컨텍스트 (인접 hit 는 자동 병합).
  4. 셋 다 못 찾으면 빈 문자열 → 템플릿이 "추출 가능한 에러 마커를 찾지 못했습니다" 안내.
- 마커 추가/변경: `_AT_BLOCK_LINE` 정규식 (현재 `^\s*@{10,}\s*$`), `_FAIL_KEYWORDS` 튜플, `_PYTEST_FAILURES_HDR`/`_PYTEST_END_HDR` 정규식만 수정.
- 호출 측 (view 4개): `apps/qualitygates/views.py:image_error_log` / `deploy_error_log` / `prod_error_log` (`_render_error_log` 공용 헬퍼) + `apps/suites/views.py:job_run_error_log`. 각 view 가 콘솔 원본을 읽어 파서 → 템플릿에 `error_log` 컨텍스트로 전달.
- 템플릿: `templates/qualitygates/error_log.html` / `templates/suites/error_log.html`. *Full Console* 버튼(원본 콘솔 페이지로 이동) + Jenkins 직링크가 함께 노출.
- UI 노출 조건: 실패 상태에서만 노출. QG 단계는 `run.status == 'image_build_failed'/'deploy_failed'/'prod_deploy_failed'`, 테스트 칸은 `tr.status == 'failed' or tr.job_run.status == 'failure'`, 단독 Suite 의 job 표는 `jr.status == 'failure'`. 콘솔 로그가 비어있으면 그 자체로 *Console* 버튼도 안 나오므로 별도 가드 불필요.
- 새 단계 (예: `staging_deploy`) 에 같은 기능을 붙이려면 (a) 해당 단계의 `console_output` 필드를 받는 view 함수, (b) URL 라우팅, (c) `_run_status.html` 의 버튼 한 줄을 추가. 템플릿은 재사용 가능.

### "테스트 Suite 가 외부 Jenkins 라 다른 자격증명을 써야 한다"
- `TestSuite.jenkins_base_url/jenkins_user/jenkins_api_token` 채우기 → `suite.get_jenkins_client()` 가 자동으로 외부 클라이언트 반환 (`apps/suites/models.py`).
- 내부 default Jenkins 를 쓰려면 비워두면 됨.

### "Test Suite Jenkins 자격증명을 중앙에서 관리/수정하고 싶다"
- `/suites/credentials/` (admin 전용) — 내부/외부 자격증명을 DB 에 저장.
- 모델: `apps.suites.models.SuiteJenkinsCredential` (`kind` 가 unique, `internal` / `external` 2개 row 만 존재).
- 내부 row 는 `JenkinsClient()` 의 default 로 사용 (env 변수보다 우선). 외부 row 는 `/suites/register/` 폼이 자동으로 채우는 default 값.
- **API token 평문은 화면에 절대 노출하지 않는다** — `SuiteJenkinsCredential.token_masked` 로 prefix 4자 + `*` 만 표시. 입력칸 비운 채 저장하면 기존 값 유지, 값 입력 시에만 갱신.
- 진입: `/suites/` 상단 `Jenkins 자격증명` 버튼 (admin 만 노출).

### "대시보드 DORA 지표 산식을 바꾸고 싶다"
- 위치: `apps/dashboard/views.py:_compute_dora` (단일 함수, 모두 LocMem-free).
- 윈도우: 30일 고정 (Accelerate 보고서 기준). 변경하려면 `index()` 의 `thirty_days_ago` + 라벨 텍스트("최근 30일") 동시 수정.
- 4 지표 산식:
  - **DF (Deployment Frequency)** — `SUCCESS_STATUSES` (`COMPLETED`/`QG_PASSED`) 의 30일 카운트 / 30. 표시: `>=1/day` 면 `2.6/day`, 그 외 `12/30d`.
  - **LT (Lead Time)** — 성공 Run 마다 `prod_completed_at or deploy_completed_at - created_at` 의 중앙값(`statistics.median`). cancelled 제외.
  - **CFR (Change Failure Rate)** — `FAILURE_STATUSES` / (`SUCCESS_STATUSES` + `FAILURE_STATUSES`) × 100. cancelled 는 분모/분자 양쪽에서 제외.
  - **MTTR (Mean Time to Recovery)** — 게이트별 시간순 정렬 → `fail_at` 마킹 → 다음 성공까지의 시간 수집 → 중앙값.
- 등급 (Elite/High/Medium/Low) 임계: `_dora_grade_df/lt/cfr/mttr` 4 함수에 분리. Accelerate 2023 기준. 사내 기준 변경 시 그 함수의 if 조건만 수정.
- 색상 CSS: `templates/dashboard/index.html` 의 `.dora-grade.{elite,high,medium,low,na}` 클래스 — green/blue/yellow/red/grey 톤.

### "대시보드 최근 실행 표에 게이트별 누적 성공률 컬럼이 어떻게 채워지나"
- 산정: `apps/dashboard/views.py:_compute_gate_lifetime_stats` — 모든 `TERMINAL_STATUSES` Run 을 gate 별로 group + `(SUCCESS / TOTAL) × 100`.
- 데이터 흐름: view 가 `recent_runs` 각각에 `gate_lifetime_stats` 어트리뷰트 부착 → 템플릿이 `{{ run.gate_lifetime_stats.success }}/{{ ...total }} ({{ ...rate }}%)` 출력.
- 게이트가 한 번도 실행 안 된 경우 dict 에 없어 컬럼은 `-`.
- Cancelled 는 total 카운트에 포함되지만 success 분자에는 포함 X → 취소가 잦은 게이트는 자연스럽게 % 가 낮아짐 (사용자 의도).

---

## 11. 트러블슈팅

| 증상 | 확인 | 조치 |
|---|---|---|
| 페이지가 멈춤 | runserver 가 한 요청에 막힌 상태 | `./run.sh` 재기동. 외부 호출(Jenkins 8s, Argo CD 6s)이 줄줄이 실패하면 한동안 밀린다. |
| Jenkins 파라미터 모달이 비어 있음 | Job 이 Active Choices / Git Parameter 사용 + 토큰 권한 부족 | `JenkinsClient` 토큰에 Overall/RunScripts 권한 부여하거나 HTML 스크래프 경로가 동작하는지 빌드 폼 직접 확인 |
| Argo CD 버튼이 안 보임 | `*_argocd_synced_at` 이 None | 정상 — Synced + Healthy 도달 시점에만 노출. Sync 실패/Degraded 시에도 Retry 버튼은 자동 노출 |
| Argo CD 가 계속 "Syncing..." 만 나옴 | `*_argocd_operation_phase`/`sync_status`/`health_status` 값 | 배지가 sync/health/operation 조합으로 분기되도록 변경됨 — `Operation: Succeeded` 인데 `health=Progressing` 이면 *롤아웃중*, `sync=OutOfSync` 이면 *OutOfSync — 재동기화 대기*. 그래도 진행이 없으면 15분 후 timeout. |
| 검수대상 배포가 완료되기도 전에 테스트가 시작됨 (직전 revision 으로 테스트) | Run 상세의 deploy 단계 Argo CD 가 "Done" 인데 실제 ArgoCD 화면은 sync 진행 중 | `_check_argocd_sync` 가 첫 트리거(refresh+sync) 직후 같은 호출에서 polling 해 직전 Run 의 `Synced+Healthy` 가 그대로 잡혔던 게 원인. 첫 호출은 polling skip + 이후 폴링에서도 `operationState.finishedAt` 이 `sync_requested_at` 이전이면 stale 로 거름. 새 Run 부터 정상 동작 — 보고된 케이스에서 다음 sync 트리거가 시작되기 전까지 deploy 단계는 *동기화중* 으로 유지된다. |
| ArgoCD 가 Synced+Healthy 라는데 새 ReplicaSet pod 가 ContainerCreating | 사용자 ArgoCD UI 에서 Pod 가 sync 중인데 Portal 은 완료 표기 | ArgoCD 의 application-level health 는 Deployment 단위라 새 Pod 가 readiness probe 통과 전에도 Healthy 로 보고될 수 있다. `_check_argocd_sync` 가 (1) `resource-tree` 호출해 자식 Pod 모두 Healthy 검증 + (2) 30초 grace period 후에만 진짜 완료 판정. 그 사이 health 가 다시 떨어지면 healthy_first_at reset. |
| Argo CD sync 가 즉시 `Sync 실패` 로 떨어짐 | `*_argocd_sync_error` 의 phase + Argo 메시지 | `_check_argocd_sync` 가 `operation_phase ∈ Failed/Error` 면 곧바로 `(PROD_)DEPLOY_FAILED` 전환. 매니페스트/이미지/리소스 누락이 흔한 원인. UI Retry Sync 로 즉시 재시도 가능. |
| Run 상세에 `sync 실패: HTTP 400 ...` 빨간 글씨가 떴다 사라진다 | 최초 sync 트리거가 `409`/`400` (이미 동기화 중/auto-sync) 으로 떨어졌을 때 채워지는 임시 메시지 | 정상. UI 는 `show_deploy_argocd_error` / `show_prod_argocd_error` 프로퍼티로 **최종 실패 상태**(`(PROD_)DEPLOY_FAILED`) 에서만 sync_error 를 노출하도록 좁혀져 있음. 폴링 중 임시 메시지는 logger.warning 으로만 남음. |
| OS 알림이 안 옴 | base.html 우하단 **OS 알림 테스트** → 진단 결과 | macOS: 시스템 설정 → 알림 → Chrome → 배너/경고 + 집중모드 OFF. Windows: 알림 + 알림 배너 표시 ON. Chrome 탭이 완전히 닫히면 워처가 죽음 — 백그라운드/최소화는 OK. |
| `gate_type` 변경 안 됨 | 의도된 제약 | 새 게이트로 등록할 것. 자격증명/Argo CD 환경 매핑이 달라지므로 변경 시 silent breakage 위험. |
| 검수통과 배포 모달 default 가 잘못됨 | APP_IMAGE 가 첫 옵션(가장 최신 태그) 으로 설정됐는지 | `prod_deploy_params` 가 `run.image_build_number` 로 시작하는 choice 를 default 로 오버라이드 (`views.py:719-728`). 이미지 빌드 단계가 없는 게이트면 그대로 첫 choice. |
| Argo CD 가 `yaml: line N: mapping values are not allowed in this context` 로 sync 실패 | GitOps 저장소의 `deployment.yaml` 에 `image: registry/foo:` 처럼 빈 태그가 푸시되었는지 | 자동 deploy/prod 트리거 시 ImageTagParameter / Active Choice 의 default 가 비어 있어 Portal 이 빈 태그를 명시 전송하던 게 원인. `_collect_default_params` 가 빈 문자열을 dict 에서 제외하도록 수정됨 (`views.py`). 이미 푸시된 깨진 매니페스트는 데브옵스가 GitOps 저장소에서 직접 정정하거나 다음 정상 Run 으로 덮어써야 함. |
| Auto deploy 가 `MissingPropertyException: No such property: APP_IMAGE for class: WorkflowScript` 로 실패 | Jenkins 빌드의 ParametersAction 에 APP_IMAGE 가 빠져 있는지 | ImageTagParameter 의 choices 가 트리거 시점에 비어 들어왔거나 (`_scrape_build_form_choices` 가 일시적으로 빈 결과 반환 등) HTML scrape 가 실패해 `_collect_default_params` 가 빈 값으로 판단해 dict 에서 제외했고, Jenkins 는 빠진 파라미터를 plugin default 로 채우지 않아 Jenkinsfile 의 `APP_IMAGE` 참조가 미바인딩이 됐던 케이스. `_auto_trigger_deploy` 가 `run.image_build_number-` 로 시작하는 choice 를 사전에 default 로 오버라이드해서 명시 전송하도록 수정됨 (`views.py`). choices 자체가 비어 매칭이 불가능한 경우엔 자동 진행을 보류하고 `WAITING_DEPLOY` 로 전환해 사용자에게 모달을 띄움. |
| db.sqlite3 머지 충돌 | Git pull 중 발생 | 로컬본을 항상 최신으로 유지 — 운영 데이터 손실 방지. |
| 파라미터 캐시가 오래됨 | 120초 이상 안 지났는데 Job 변경 사항이 반영 안 됨 | 모달의 새로고침 버튼 = `?refresh=1` 호출 |

---

## 12. 안 쓰는 / 레거시 코드

리팩토링 시 참고 (지금은 그대로 두지만, 신 파이프라인과 무관):

- `apps/gates/` — 구 GateRun 모델 (DRF API 가 이걸 쓴다)
- `apps/services/`, `apps/approvals/`, `apps/escalations/` — PRD 단계의 placeholder
- `apps/integrations/slack.py`, `apps/integrations/webhooks.py`, `apps/integrations/tasks.py` — 구 GateRun 기준. **신규 Slack 알림은 `apps/notifications/` 사용** (§4.4)
- `apps/api/` — 구 모델 기반 DRF API (`/api/v1/...`)
- `config/celery.py`, `CELERY_*` 환경변수 — `TASK_ALWAYS_EAGER` 라 실질 미사용
- `config/settings/production.py` — 운영도 `dev.py` 로 동작
- `base.py` 의 PostgreSQL/Redis 기본값 — `dev.py` 가 SQLite/LocMem 으로 덮음
- `.env.example` 의 PostgreSQL/Redis/Slack/TAS/Encryption 섹션
