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
│  Django (runserver 0.0.0.0:8601, 게이트웨이 → HTTPS)         │
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

- **DB**: SQLite (`db.sqlite3`, Git 에서 제외 — `.gitignore`)
- **캐시**: LocMemCache (Jenkins 파라미터 120초 캐싱용)
- **Celery**: `TASK_ALWAYS_EAGER=True` — 사실상 동기 실행. 폴링은 HTTP 요청 흐름에서 발생.
- **시간대**: Asia/Seoul, **언어**: ko-kr
- **인증**: Keycloak(OIDC) SSO — `/accounts/login/` 이 Keycloak 전용 로그인 페이지, 로그아웃은 앱·Keycloak 세션을 함께 종료 (§7 "SSO 로그인", 운영 반영 §8)

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
| `qg_passed` | 테스트 PASS, 검수통과 배포 **승인** 대기. `status_display` 가 "QG Passed (Awaiting {prod_env} **Approval**)" 로 표시됨 | `prod_deploy` (사용자 클릭 = 승인+실행) |
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
                                  │         (_poll_one_test_build)
                              ┌───┴────────────────────────────────────────────────────────────┐
                              │  Suite 빌드 실패 + attempt < test_run.retry_limit (스냅샷)    │
                              │  → test_run PENDING, 재트리거 (재시도)                       │
                              └────────────────────────────────────────────────────────────┘
                                  │ attempt 소진 or 취소/트리거 실패
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
  - **반대로 빈 값이라고 그냥 빼버리면** Jenkinsfile 이 `params.X` 가 아닌 `X` 형태로 변수를 직접 참조할 때 `groovy.lang.MissingPropertyException: No such property: APP_IMAGE for class: WorkflowScript` 로 즉시 실패한다 — Jenkins 의 `buildWithParameters` 는 빠진 파라미터를 plugin 런타임 default 로 채워주지 않고 그냥 언바인딩 상태로 둔다. 따라서 ImageTagParameter 처럼 필수인 파라미터는 호출 측에서 default 를 채워서 보내야 한다 — `_auto_trigger_deploy` 와 `_auto_trigger_prod` 가 모두 `resolve_and_apply_app_image(parameters, run)` 로 **create-image 콘솔에서 추출한 실제 푸시 태그**(폴백: `run.image_build_number-` prefix choice 매칭)를 default 로 채우는 이유. luxair ImageTagParameter 는 choices 가 비어 오므로 콘솔 추출이 사실상 유일한 공급원이다(모달 경로와 동일 로직). 그래도 채우지 못하면 (콘솔 추출·prefix 매칭 모두 실패) 자동 트리거는 보류하고 `_auto_trigger_deploy` 는 `WAITING_DEPLOY`, `_auto_trigger_prod` 는 `QG_PASSED` 로 떨어뜨려 사용자가 모달에서 직접 선택하게 한다. **2026-05-11 사고** — `_auto_trigger_prod` 에 이 안전망이 없어 자동 모드 게이트(`public-auth`)의 prod 단계가 APP_IMAGE 누락으로 `MissingPropertyException` 즉사한 케이스. deploy 측 안전망을 prod 에도 대칭으로 복제해 해소.

#### 수동 트리거 APP_IMAGE 가드 (`_validate_app_image_param`)

`apps/qualitygates/services.py` — `trigger_deploy` / `trigger_prod` 진입부.

수동 모달에서 사용자가 빈 값 또는 콜론으로 끝나는 깨진 태그를 전송할 경우 Jenkins 로 요청이 나가기 전에 차단한다.

```
_validate_app_image_param(parameters: dict)
  APP_IMAGE 키 없음 → 통과  (Jenkins 플러그인 default 사용 — d6652f1 의도)
  APP_IMAGE 값이 빈 문자열 또는 공백만 → GateRunError("invalid_app_image", http_status=400)
  APP_IMAGE 값이 "..." + ":" 로 끝남 → GateRunError("invalid_app_image", http_status=400)
  그 외 → 통과
```

- views.py `deploy_trigger` / `prod_deploy` 에서 `e.code == "invalid_app_image"` 를 명시 처리해 `messages.error()` + redirect — `invalid_state` 와 동일 스타일.
- API v2 경로 (`apps/api_v2/views/controls.py`) 도 `services.trigger_deploy` / `services.trigger_prod` 를 직접 호출하므로 자동 보호됨. 별도 분기 불필요.
- 자동 트리거 경로(`_auto_trigger_deploy` / `_auto_trigger_prod`) 는 이미 자기 사전 검증 단계에서 빈 dict 전송을 막으므로 이 가드를 통과해도 무해.
- **2026-06-02 사고** — Run `bca67034-...` (auth-worker #30) 에서 수동 모달이 APP_IMAGE 빈 태그(`registry/auth-worker:`)를 그대로 전송해 Argo CD `mapping values are not allowed in this context` 발생. 이 가드로 재발 방지.

#### 수동 모달 APP_IMAGE default 오버라이드 — luxair ImageTagParameter 대응

**문제 배경**: `deploy_job` / `prod_job` 의 `APP_IMAGE` 파라미터는 Jenkins 플러그인
`io.jenkins.plugins.luxair.ImageTagParameterDefinition` 으로 선언되어 있다. 이 플러그인은
빌드 폼 렌더 시 ECR 에서 태그 목록을 동적으로 조회하며, Jenkins `/api/json` 응답에는
`choices=[]` 로 비어 있다. 포털이 쓰는 3가지 fallback 경로(scriptText, GET build, luxair
fillValueItems) 는 운영 Jenkins(v2.502) 에서 모두 차단된다. 포털 호스트에는 AWS 자격증명도
없어 ECR 직접 조회도 불가. 결과적으로 `choices=[]` → 프론트엔드가 텍스트박스로 폴백 렌더.

**해결책**: `create-image` 빌드가 ECR 에 푸시한 정확한 태그는 그 빌드의 콘솔 로그(`run.image_console_output`, 이미 DB 저장)에 jib `--target=` 토큰으로 박혀 있다.
이 태그를 추출하여 `APP_IMAGE` 의 `default` + `choices` 에 주입한다.

```
추출 경로: jib '--target=<registry>/<repo>:<tag>' 토큰
  tag 형식: '{image_build_number}-{YYYYMMDD_HHMMSS}' (예: 210-20260602_153100)
  --target 없으면 일반 ECR 참조 패턴 폴백
  '{build_number}-' prefix 가드로 다른 빌드 태그 오채택 방지
```

**구현 구성요소**:

##### A. `extract_pushed_image_tag` (`apps/integrations/jenkins.py:866`)

모듈 레벨 순수함수.

```
extract_pushed_image_tag(console_output, build_number, repo_hint=None) → str | None

  console_output 없음 / build_number 없음 → None
  ★ 'Finished: SUCCESS' 가 콘솔에 없으면 → None  (중단/실패 빌드 방어)
  --target=<ref> 에서 tag 추출, 없으면 ECR 참조 패턴으로 폴백
  tag 가 '{build_number}-' 로 시작하는 후보만 채택
  중복 (ref,tag) 라인 dedup
  repo_hint 있으면 ref 가 해당 repo 로 끝나는 후보 우선 (0건이면 전체 폴백)
  후보들의 tag 가 모두 동일 → 그 tag 반환 (같은 태그 멀티-repo 포함)
  서로 다른 tag 가 섞임(진짜 모호) / 후보 0개 → None
```

★ **중단 빌드 방어 (필수)**: shell 은 jib 명령을 실행 *전에* echo (`+ jib ... --target=...:{bn}-...`) 하므로,
InterruptedException 등으로 중간에 죽은 빌드(`Finished: FAILURE`) 콘솔에도 `--target` 라인이 남는다.
이미지는 ECR 에 푸시되지 않았는데 태그를 반환하면 배포가 pull 단계에서 실패한다. jib 가 `--verbosity=lifecycle`
라 push-success 마커를 안 찍으므로, **빌드 최종 상태(`Finished: SUCCESS`)가 push 여부의 유일한 신뢰 판별자**다.
(실증: build 210=SUCCESS→추출 / 중단된 1070·702=FAILURE→None)

정규식:
- `_TARGET_RE = r'--target=["\']?([^\s"\']+)'` — 값이 따옴표로 감싸인 `--target="..."` 도 직접 캡처 (`apps/integrations/jenkins.py:857` 부근)
- `_ECR_REF_RE` — `<acct>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>` 패턴

##### B. `repo_hint` (멀티모듈 대비, 현재 미구현)

`extract_pushed_image_tag` 의 `repo_hint` 인자는 한 빌드가 여러 repo 에 서로 다른 태그를 푸시하는 멀티모듈 케이스에서 대상 repo 를 좁히기 위한 것. 실측상 모든 게이트의 create-image 는 단일 `--target`(repo 1개)이라 현재는 `repo_hint=None` 으로 충분하다. 멀티모듈 게이트가 생기면, deploy job `<job>/config.xml` 의 `io.jenkins.plugins.luxair.ImageTagParameterDefinition <image>` 를 읽어 `repo_hint` 로 넘기는 헬퍼를 추가하면 된다(YAGNI 로 현재 미구현).

##### C. `resolve_and_apply_app_image` (`apps/qualitygates/services.py:679`)

```
resolve_and_apply_app_image(parameters, run, *, repo_hint=None) → bool

  APP_IMAGE 파라미터 없음 → True (무관)
  tag = extract_pushed_image_tag(run.image_console_output, run.image_build_number, repo_hint)
  tag 있으면:
    param["default"] = tag
    tag 가 choices 에 없으면 choices 앞에 prepend ([tag] + existing)  → True
  tag 없으면(폴백):
    choices 중 '{build_number}-' prefix 항목 있으면 default 로 설정 → True
    없으면 → False
```

`get_job_param_schema` (`apps/qualitygates/services.py:581`) 에 `run=None` optional 추가:
- `run` 주어지고 `stage in ('deploy', 'prod')` → `resolve_and_apply_app_image`
- `run=None` (api_v2 meta 경로) → 기존 `_apply_app_image_default` (prefix 매칭) 유지 — api_v2 동작 완전 무파괴

캐시(`LocMemCache`) 관련: 캐시 히트 분기와 미스 분기 **모두** `resolve_and_apply_app_image` 를 적용한다. 캐시 히트 시 `copy.deepcopy(cached)` 로 깊은 복사 후 적용 — `resolve_and_apply_app_image` 가 내부 dict 를 in-place 수정하므로, LocMemCache 외 백엔드(Redis/Memcached) 로 교체해도 캐시 원본이 오염되지 않게 방어. choices prepend 시 `[tag] + existing` 으로 새 리스트를 생성해 in-place append 도 회피.

##### D. `QualityGateRun.image_app_tag` (`apps/qualitygates/models.py:470`)

```python
@property
def image_app_tag(self) -> str:
    from apps.integrations.jenkins import extract_pushed_image_tag
    return extract_pushed_image_tag(self.image_console_output, self.image_build_number) or ""
```

마이그레이션 없음 (DB 컬럼 추가 없이 `@property` 만). 템플릿에서 `{% if run.image_app_tag %}` 로 Image Build 스텝에 태그 표시 (`templates/qualitygates/_run_status.html:97`).

##### E. 호출 교체 (`apps/qualitygates/views.py`)

`deploy_params` (`views.py:847`) 와 `prod_deploy_params` (`views.py:892`) 에서:
```python
# 이전
matched = _override_app_image_default(parameters, run.image_build_number)
# 이후
matched = resolve_and_apply_app_image(parameters, run)
```
`_override_app_image_default` 함수 정의 자체(`views.py:46`)는 유지 — `tests.py:16` 이 직접 import 하므로 삭제 시 테스트 모듈 전체 collection 실패.

응답 JSON 에 `app_image_matched: bool` 키로 포함됨:

| 값 | 의미 |
|---|---|
| `true` | APP_IMAGE 없거나 콘솔 추출/prefix 매칭 성공 → 모달 select 정상, submit 허용 |
| `false` | 추출+매칭 모두 실패 → 프론트엔드가 submit 비활성화 + 경고 배너 |

- `choices=[tag]` 가 주입되면 `_renderParam` 이 `<select>` 로 정상 렌더 (기존 빈 choices 때의 텍스트박스 폴백 해소).
- `_override_app_image_default` 는 레거시 헬퍼로 남아 있음 (단위테스트 보호). 호출처만 교체.

#### 수동 모달 JS APP_IMAGE 차단 (`_renderParam` / `_isValidAppImage` / submit 가드)

`templates/qualitygates/run_detail.html` — 모달 파라미터 렌더링 + submit 이벤트 핸들러.

3번째 방어선 (브라우저 측):

- `_renderParam` (`line 81`): APP_IMAGE select 렌더 시 빈 문자열·null·undefined value 의 option 을 렌더링에서 제외. `required` 속성 + `data-app-image-select="1"` 마커 추가.
- `_isValidAppImage(val)` (`line 114`): 값이 비거나 콜론으로 끝나면 `false` 반환 — `_validate_app_image_param` 과 동일 규칙을 클라이언트에서 선검증.
- submit 가드 (`line 150`): form 에 `_submitGuard` 리스너 등록. APP_IMAGE select 값이 `_isValidAppImage` 실패면 `e.preventDefault()` + alert. 모달 재사용 시 중복 등록 방지 로직 포함.
- `app_image_matched === false` 분기 (`line 188`): 백엔드가 prefix 매칭 실패를 반환하면 submit 버튼 즉시 비활성화 + 경고 배너 표시. 사용자가 APP_IMAGE select 에서 유효한 값을 직접 고르면 `change` 이벤트에서 `_isValidAppImage` 로 재검증 후 submit 버튼 재활성화.

3중 방어선 요약:

| 순서 | 위치 | 시점 | 차단 조건 |
|---|---|---|---|
| 1 | `resolve_and_apply_app_image` (`services.py:679`) ← **교체됨** (구: `_override_app_image_default` `views.py:46`) | 모달 열기 (API 응답) | 콘솔 추출+prefix 매칭 모두 실패 → `app_image_matched=false` 반환, 프론트엔드가 submit 비활성화. 성공 시 choices=[tag] 주입 → select 정상 렌더 |
| 2 | 모달 JS submit 가드 (`run_detail.html:150`) | form submit | APP_IMAGE select 값이 빈 문자열 또는 `:` 로 끝남 |
| 3 | `_validate_app_image_param` (`services.py:346`) | 서버 수신 직후 | APP_IMAGE 값이 빈 문자열/공백/`:` 로 끝남 → HTTP 400 |

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

### `TestSuite` / `TestSuiteRun` / `TestSuiteJobRun` (`apps/suites/models.py`)

| 모델 | 클래스 라인 | 주요 필드 |
|---|---|---|
| `TestSuite` | `:47` | `retry_count` PositiveInt default 1 — 잡(worker)의 최대 실행 횟수. 편집 화면에서 설정. |
| `TestSuiteJob` | `:140` | `trigger_delay_seconds` PositiveInt default 30 — 잡별 실행 지연 시간(초). 0 이면 지연 없음. `/suites/<pk>/edit/` 화면 + Django admin 인라인에서 설정. |
| `TestSuiteRun` | `:153` | `retry_limit` PositiveInt default 1 — Run 시작 시점의 `suite.retry_count` 스냅샷. in-flight 중 설정 변경이 이 Run 에 영향 없도록 격리. `seq_no` PositiveInt default 0 — suite 별 1부터 순증가하는 표시용 일련번호. `@staticmethod next_seq_no(suite)` 헬퍼(`:221`)가 Max(seq_no)+1 패턴으로 반환. |
| `TestSuiteJobRun` | `:210` | `attempt` PositiveInt default 1 — 잡의 현재 시도 회차(1-based). FAILURE 발생 후 `attempt < suite_run.retry_limit` 이면 PENDING 으로 되돌려 `_try_trigger_next` 가 재트리거. `Status.WAITING = "waiting"` — 지연 throttle 로 보류된 상태(아직 트리거 안 됨). PENDING 과 구분해 화면에 "지연 대기" 배지로 표시. 전이: PENDING/WAITING → (delay 통과 시) QUEUED. |

#### `TestSuite.last_job_triggered_at` — suite 단위 트리거 throttle (`apps/suites/models.py:84`)

Jenkins API 동시 충돌 방지를 위한 **suite 단위** throttle 필드. 전역 singleton(`JobTriggerThrottle`) 을 대체(0011 마이그레이션에서 삭제).

| 필드 | 비고 |
|---|---|
| `TestSuite.last_job_triggered_at` | 그 suite 의 마지막 잡 트리거 UTC 시각. 조건부 UPDATE 로 원자적 갱신. null=True (한 번도 트리거 안 됐으면 null). |

throttle 핵심 로직의 single source of truth 는 `apps/suites/throttle.py`:
- `claim_trigger_slot(suite, delay_seconds) -> bool` — 그 suite 의 `last_job_triggered_at` 으로부터 `delay_seconds` 가 지났으면 해당 suite row 를 `now` 로 **원자적 조건부 UPDATE** 점유하고 `True`(호출자 트리거 진행), 아직이면 `False`(호출자가 WAITING 보류). 동시 호출 중 정확히 1개만 통과. `delay_seconds <= 0` 이면 항상 통과(타임스탬프는 갱신).
- `seconds_until_slot(suite, delay_seconds) -> int` — 다음 슬롯까지 대기 시간(초) 반환.
- `DEFAULT_TRIGGER_DELAY_SECONDS = 30`.

scope=suite 이므로 **서로 다른 suite/스케줄은 완전히 독립** — 한 suite 안에서만(병렬 run 의 잡들·동시 idle 호출) delay 간격으로 트리거 분산.

throttle 통합 지점:
- `apps/suites/views.py:_try_trigger_next` — PENDING+WAITING 후보를 순회하며 첫 번째 비-building 잡에 `claim_trigger_slot(suite_run.suite, job.trigger_delay_seconds)` 적용. 통과 시 트리거(QUEUED), 실패 시 WAITING 보류. `decided_next` 플래그로 한 번에 1개만 처리.
- `apps/suites/views.py:_abort_suite_run` — WAITING 상태 job_run 은 아직 트리거 안 됐으므로 Jenkins stop 없이 바로 ABORTED.
- `apps/suites/services.py:_trigger_single_idle_run` — idle worker busy 판정 필터에 WAITING 포함(지연 대기 중인 worker 도 busy).
- `apps/robustness/services.py:_trigger_all_pending` — 병렬 모드에서 각 잡 트리거 전 `claim_trigger_slot(suite_run.suite, job_run.job.trigger_delay_seconds)` 적용. 한 sweep 에 그 suite 의 슬롯 1개만 점유 → 나머지 WAITING → 이후 sweep 마다 delay 지난 잡 1개씩 추가 트리거. 먼저 트리거된 잡은 계속 RUNNING 이라 **잡들이 delay 간격으로 엇갈려 시작되어 병렬로 함께 실행**(앞 잡 완료를 기다리지 않음). 서로 다른 스케줄/suite 는 독립 슬롯이라 starvation 없음.
- `apps/robustness/services.py:_timeout_unfinished_runs` — WAITING 을 PENDING 과 동일 취급(interval 타임아웃 시 FAILURE 마감).

**QG 파이프라인 테스트 단계(`apps/qualitygates/views.py:_try_trigger_test`)는 이 throttle 이 적용되지 않는다** — Test Suites 와 Reliability Test 로만 범위 한정.

마이그레이션:
- `apps/suites/migrations/0008_*` — TestSuite.retry_count, TestSuiteRun.retry_limit, TestSuiteJobRun.attempt 3개 필드 동시 추가.
- `apps/suites/migrations/0009_add_seq_no_to_test_suite_run` — TestSuiteRun.seq_no AddField → RunPython 백필(suite별 started_at 오름차순, 동률이면 pk 오름차순으로 1..N) → UniqueConstraint(`suite_seq_no_uniq`, `seq_no > 0`) 추가.
- `apps/suites/migrations/0010_jobtriggerthrottle_and_more` — `JobTriggerThrottle` 테이블 신설(현재는 0011 에서 삭제됨) + `TestSuiteJob.trigger_delay_seconds` 필드 추가 + `TestSuiteJobRun.Status.WAITING` 값 추가.
- `apps/suites/migrations/0011_delete_jobtriggerthrottle_and_more` — `JobTriggerThrottle` 테이블 삭제 + `TestSuite.last_job_triggered_at` 필드 추가(suite 단위 throttle 로 전환).

`seq_no` 부여 위치 (5곳, 모두 `transaction.atomic` 안에서 `next_seq_no(suite)` 호출):
- `apps/suites/services.py:_trigger_any_idle` (`:130`) — idle worker 1개 경로
- `apps/suites/services.py:_trigger_single_job_run` (`:168`) — job_id 지정 경로
- `apps/suites/services.py:_trigger_chain_run` (`:233`) — run_all 체인 경로
- `apps/qualitygates/views.py:_try_trigger_test` (`:1770`) — QG 파이프라인 내 Suite 트리거
- `apps/robustness/services.py:_trigger_once` (`:214`) — Reliability Test 트리거

`seq_no` 표시:
- **최근 실행 목록** (`templates/suites/run_list.html`) — `#` 컬럼 첫 번째 열. 여러 suite 혼합이라 seq_no 가 중복돼 보여 **suite 구분자 prefix** (`run.suite.short_label`, 예 `am-dweb-prd`) + `· #N` 모노로 표시. seq_no=0 이면 `-`. (`short_label` = suite 이름에서 regression/api/test 토큰 제거, `apps/suites/models.py:TestSuite.short_label`)
- **Suite 상세 실행 이력** (`templates/suites/suite_detail.html`) — 동일 스타일 `#` 컬럼
- **Run 상세 헤더/탭 title** (`templates/suites/run_detail.html`) — `#{{ suite_run.seq_no }}` (seq_no >= 1 일 때만)
- **API v2** — `POST /api/v2/suites/<id>/runs/` 201 응답 + `GET /api/v2/suite-runs/<id>/status/` 200 응답에 `seq_no` 포함 (`apps/api_v2/views/suites_v2.py`, `apps/api_v2/serializers/suites.py`)

재시도 로직 위치:
- **단독/Reliability**: `apps/suites/views.py:_poll_run_status` FAILURE 분기 (`views.py:814`) — `attempt < suite_run.retry_limit` 이면 job_run 을 PENDING 으로 되돌리고 `_try_trigger_next` 재트리거(실패 집계 보류). 재시도 시 `job_run.jenkins_build_url = ""` 초기화(`views.py:828`). 소진 시 FAILURE 확정 후 finalize.
- **스냅샷 채우는 곳**: `apps/suites/services.py` 의 3개 trigger 헬퍼 + `apps/robustness/services.py:_trigger_once` → `TestSuiteRun.retry_limit`. QG 경로는 `apps/qualitygates/services.py:trigger_gate_run:125` → `QualityGateRunTest.retry_limit`.
- 재시도 대상: 순수 `result=FAILURE` 만. 사용자 취소(ABORTED)·트리거 실패·Reliability interval 타임아웃은 재시도 안 함.
- 중간 시도 실패는 Slack 미발송. 모든 시도 소진된 최종 실패에서만 기존 `SUITE_RUN_FAILED`/`TEST_SUITE_FAILED` 발송.

### `TapSuite` / `TapRun` — UI Test Automation(TAP) 연동 (`apps/suites/models.py:494`, `apps/suites/services_tap.py`, `apps/integrations/tapilot.py`, 2026-07-28)

TAP(tapilot, Playwright 기반 UI 자동화, 같은 호스트의 별도 FastAPI 앱)에 등록된 Suite/Run 을 미러링해 `/suites/?tab=ui` 에 노출. 설계 근거: `docs/prd_ui_test_automation.md`.

| 모델 | 클래스 라인 | 주요 필드 |
|---|---|---|
| `TapSuite` | `:494` | `tap_id`(unique, TAP 쪽 PK) / `name` / `project_name` / `case_count` / `owner_display` / `is_removed`(TAP 에서 사라진 suite — 삭제 대신 표시, 실행 이력 보존) / `synced_at`. `latest_run` 프로퍼티(`:523`) = `runs.order_by("-started_at").first()`. |
| `TapRun` | `:527` | `session_id`(unique, TAP 세션 ID) / `status`(`Status` TextChoices: `queued`/`pending`/`running`/`completed`/`stopped`/`error`) / `run_env` / `total_cases`·`completed_cases`·`passed`·`failed`·`others`(케이스 수 기준 집계) / `excluded_case_ids`(자동화 스크립트 없어 제외된 케이스) / `tap_url` / `testrail_run_id` / `raw_cases_json`(종결 시에만 채움, 케이스별 결과) / `triggered_by` / **`robustness_schedule`**(nullable FK → `robustness.RobustnessSchedule`, `related_name="tap_runs"`, `:564` — Reliability(UI Automation) 스케줄이 트리거한 run 이면 그 스케줄, 수동 실행은 `None`. `RobustnessSchedule._tap_aggregate()` 의 집계 기준, 2026-07-28). `ACTIVE_STATUSES = (queued, pending, running)`. 프로퍼티: `is_active`(`:579`), `progress_percent`(`:583`, `completed_cases/total_cases` — Jenkins stage 개수가 아니라 **케이스 수 기준**), `result_label`(`:589`, 대기/실행 중/Pass/Fail/Aborted). |

**상태 매핑** (`TapRun.result_label`, `apps/suites/models.py:589`):

| TAP `status` | 포털 표기 | 비고 |
|---|---|---|
| `queued` / `pending` | 대기 | |
| `running` | 실행 중 | |
| `stopped` | **Aborted** | 장애로 집계하지 않음 — Suite Run 의 **ABORTED=장애 아님 정책을 그대로 승계**(대시보드 결과 분포·미 장애 판정 큐와 동일 기준) |
| `error` | Fail | |
| `completed` | Pass / Fail | `failed > 0` 이면 Fail |

서비스 계층 (`apps/suites/services_tap.py`) — 아래 4개 모두 `TapError` 를 잡아 로그만 남기고 조용히 반환한다(트리거만 예외, 아래 참고):
- `sync_tap_suites(client=None) -> int` — TAP `GET /suites/` → `TapSuite.update_or_create`. TAP 에서 사라진 suite 는 삭제 대신 `is_removed=True`. 반환 `-1` = TAP 미설정/실패.
- `trigger_tap_suite(tap_suite, user=None) -> TapRun` — TAP `POST /suites/{id}/run/` → `TapRun` 생성. **유일하게 `TapError` 를 삼키지 않고 그대로 전파** — 호출부 `tap_suite_run` 뷰가 잡아 메시지 처리(사용자가 실행 버튼을 눌렀는데 실패를 조용히 무시하면 안 되므로).
- `poll_active_tap_runs() -> int` — `ACTIVE_STATUSES` 인 `TapRun` 전부 순회 폴링. 매분 cron(`sweep_reliability`)과 `tap_suite_table` fragment 뷰 양쪽에서 호출.
- `poll_tap_run(run, client=None)` — 1건 폴링, 종결(`terminal`) 시에만 `run_detail` 을 추가 호출해 `raw_cases_json` 저장(진행 중엔 매번 케이스 상세를 긁지 않아 TAP 부하 절감).
- `cancel_tap_run(run)` — TAP `POST /runs/{id}/cancel/` 후 즉시 1회 재폴링해 상태 반영.

클라이언트 (`apps/integrations/tapilot.py`, `TapClient`):
- `base_url`(기본 `https://tapilot.jkintra.net`, `.env` `TAPILOT_API_BASE`) + `api_key`(`.env` `TAPILOT_API_KEY`, `Authorization: Bearer` 헤더). `configured` 프로퍼티 = `bool(api_key)` — 키가 없으면 서비스 계층이 호출 자체를 스킵.
- 모든 HTTP 실패(연결 오류, 4xx/5xx)는 `TapError` 로 통일해 던짐. 타임아웃 `DEFAULT_TIMEOUT=10초`(TAP 이 동일 호스트라 왕복이 짧다는 전제 — 지연 시 빠르게 포기).
- 메서드: `list_suites` / `suite_detail` / `run_suite` / `run_status`(폴링, 응답 헤더 `X-TAP-Poll-After` 를 함께 반환) / `run_detail`(케이스별 결과) / `cancel_run`.

**TAP 장애 격리** (3중):
1. `TapClient.configured` 가 False(`TAPILOT_API_KEY` 미설정)면 `sync_tap_suites` / `poll_active_tap_runs` 가 즉시 반환(`-1` / `0`) — HTTP 호출 자체가 안 나감.
2. 개별 HTTP 호출 실패는 `TapError` 로 잡혀 `logger.warning` 만 남기고 그 사이클을 스킵 — 다음 사이클(매분 cron 또는 다음 fragment 요청)에 재시도.
3. `sweep_reliability` 커맨드는 TAP 동기화/폴링 블록 전체를 한 번 더 `try/except Exception`(`logger.exception`)으로 감싼다(`apps/robustness/management/commands/sweep_reliability.py:80-87`) — TAP 관련 예외가 뒤이은 Slack 알림 스레드 join 등 커맨드의 나머지 로직을 막지 못한다.
- 화면(`/suites/?tab=ui`, `tap_suite_table` fragment)은 항상 **DB 미러를 즉시 렌더** — TAP 이 죽어도 마지막으로 싱크된 목록이 그대로 보이고 `synced_at` 만 오래된 값으로 남는다.

**cron 통합** — 별도 crontab 신설 없이 **기존 Reliability sweep cron(매분, `sweep_reliability`)에 편승**: 그 커맨드의 끝부분(`sweep_reliability.py:80-87`)에서 `sync_tap_suites()` + `poll_active_tap_runs()` 를 호출. 신규 crontab 등록 불필요 — README §10 / SKILLS.md 의 기존 cron 한 줄을 그대로 재사용.

HTMX fragment 흐름(폴링 트리거/정지 조건, 케이스 배지 등)은 §6.4 참고.

**바꾸려면**:
- **싱크 주기를 바꾸고 싶다** → Suite 목록 미러(`sync_tap_suites`)는 매분 cron 에서만 갱신되고 화면 진입/fragment 요청 시에는 다시 부르지 않는다 — 갱신 주기를 cron 과 별개로 당기려면 `apps/suites/views.py:tap_suite_table` 에도 `sync_tap_suites()` 호출을 추가. 진행 중 Run 의 진행바 갱신은 이미 `tap_suite_table` fragment 요청마다 `poll_active_tap_runs()` 를 동기 호출하므로 화면을 열어두면 HTMX 10초 폴링 간격으로 사실상 라이브 갱신된다(신뢰성 하한은 매분 cron).
- **필드를 추가하고 싶다** → TAP 응답 원본이 `TapSuite.raw_json` / `TapRun.raw_cases_json` 에 이미 통째로 저장돼 있으니, 새 컬럼 없이 이 JSON 에서 꺼내 쓸 수 있는지 먼저 확인. DB 컬럼이 꼭 필요하면 `apps/suites/models.py` 에 필드 추가 → 마이그레이션 → `services_tap.py` 의 `TapSuite.objects.update_or_create`(싱크) 또는 `_apply_status`(폴링)에 매핑 추가.
- **상태 매핑을 바꾸고 싶다**(예: `stopped` 를 장애로 집계하고 싶다) → `TapRun.result_label` (`apps/suites/models.py:589`) 한 곳만 수정하면 목록/상세 템플릿에 모두 반영된다(둘 다 이 프로퍼티만 참조). **단, `stopped=Aborted=장애 아님` 은 Suite Run 전역 정책과 통일한 것**이므로 여기만 바꾸면 대시보드 결과 분포/미 장애 판정 큐의 ABORTED 처리와 표기가 어긋날 수 있다 — 정책 자체를 바꾸려면 그 전역 기준도 함께 검토. **Reliability(UI Automation) 스케줄이 트리거한 TapRun 도 이 매핑을 그대로 따른다** — `stopped`(스케줄 중지/취소)는 Aborted=장애 아님, interval 타임아웃 마감은 `error`(Fail) 로 별도 기록되어 실패 신호가 유지된다(아래 "Reliability Test 에 UI Automation(TAP) Suite 스케줄 연결" 항목 참고).

### API 탭 Jenkins 잡 자동 싱크 (`apps/suites/services_sync.py`, 2026-07-28)

내부 Jenkins 에서 이름에 `api` 가 포함된 폴더/잡을 탐색해 미등록 `TestSuite` 를 자동 등록.
설계 근거: `docs/prd_ui_test_automation.md` §3.4.

- `sync_api_suites(client=None, *, dry_run=False) -> dict` — `{"created", "synced", "candidates"}`
  반환. `client` 미지정 시 `JenkinsClient()`(내부, 익명 read). `dry_run=True` 면 아무것도
  쓰지 않고 `candidates` 만 채워 반환(사전 점검용).
  1. **탐색 루트** = 이미 등록된 내부(`jenkins_base_url=""`) `TestSuite` 들의
     `jenkins_folder_path` 부모 폴더 전부(`_parent_folder`, `/` 없으면 루트`""`) +
     env `JENKINS_API_SYNC_ROOTS`(콤마 구분, 선택) — `_scan_roots()`.
     실측 컨벤션(2026-07-28 내부 Jenkins)상 Suite 폴더가 전부 Jenkins **루트** 바로
     아래 있어 부모가 거의 항상 `""`(루트) 하나로 수렴한다.
  2. 각 루트에서 `JenkinsClient.list_children`(경량 자식 나열, `_class` 로 폴더/잡 구분)로
     깊이 제한(`_MAX_DEPTH=4`) 안까지 내려가며 이름에 `api`(대소문자 무관)가 포함된
     자식을 찾으면 `list_folder_jobs` 로 실제 워커 목록을 확인한다.
     - 워커가 **있으면** 그 경로를 Suite 로 확정 — 이미 등록(같은 base_url+folder_path)
       이면 스킵, 아니면 `TestSuite.objects.get_or_create` + `sync_suite_jobs`(아래)로
       워커 등록. 자식이 워커 Job 뿐이라는 전제로 그 아래는 더 내려가지 않는다.
     - 워커가 **없으면**(빈 폴더거나 leaf job 이라 `jobs` 키 자체가 없음) 그 아이템이
       폴더면 더 깊이 탐색(예: "api" 가 포함된 상위 그룹 폴더 아래 실제 Suite 폴더가
       한 단계 더 있는 경우 대비), 아니면(leaf job) 스킵.
     - `api` 가 없는 폴더도 계속 하위로 내려가며 탐색한다(깊이 제한까지).
  3. 폭주 방지 — 전체 스캔에 폴더/잡 조회 호출 예산(`_MAX_NODES=300`)을 둔다. 개별
     폴더 조회 실패는 `JenkinsError` 로 격리(그 가지만 스킵, `logger.warning`), 나머지
     탐색은 계속된다.
- `sync_suite_jobs(suite, jobs) -> int` — 워커 Job 목록으로 `TestSuiteJob` upsert. **기존
  `apps.suites.views.suite_sync`(수동 재싱크 버튼) 의 본문을 추출한 것** — 수동/자동 두
  경로가 이 함수 하나만 호출하므로 upsert 로직 변경은 한 곳만 고치면 된다.
- `JenkinsClient.list_children(folder_full_name="") -> list` (`apps/integrations/jenkins.py`)
  — `list_folder_jobs` 와 달리 build stage 등 무거운 조회 없이 `{"name","full_path","url",
  "is_folder"}` 만 가볍게 반환하는 **읽기 전용** 메서드. `is_folder` 판정은 Jenkins
  `_class` 문자열에 `"Folder"` 포함 여부(`com.cloudbees.hudson.plugins.folder.Folder` 등) —
  실측 내부 Jenkins 기준 `WorkflowJob`(일반 잡)에는 `Folder` 가 없어 구분된다. 폴더를
  비워 호출하면 Jenkins 루트를 나열.

트리거 2곳:
- **수동** — API 탭 상단 `[Jenkins 싱크]` 버튼(admin 전용) → `POST /suites/api-sync/`
  (`name=suites_api_sync`) → `views.suites_api_sync` → `sync_api_suites()` 호출 + 결과
  메시지 + `ActivityLog`(`Action.UPDATE`, `TargetType.SUITE`, target_name="API 탭 Jenkins
  자동 싱크"). 버튼 마크업은 `templates/suites/suite_list.html` API 탭 헤더,
  `{% if active_tab == "api" and user.is_admin %}` 안.
- **10분 주기 자동** — `apps/robustness/management/commands/sweep_reliability.py`
  (매분 cron) 끝부분에서 `cache.add("suites:api_jenkins_sync_cooldown", 1, 600)` 가
  10분 창 안에서 최초 1회만 `True` 를 반환할 때만 `sync_api_suites()` 호출
  (`apps/robustness/services.py:_SWEEP_COOLDOWN_KEY`/`_SWEEP_COOLDOWN_SECS` 와 동일한 `cache.add` 패턴). try/except
  로 격리해 Jenkins 장애가 나머지 sweep(스케줄/단독 run 폴링/TAP)을 막지 않는다.
  **crontab 무변경** — 기존 매분 cron 에 편승(§ TAP 싱크와 동일 패턴).

트리거/쓰기 Jenkins 호출 없음(익명 read 만), 삭제는 자동 반영 안 함(잡이 사라져도 Suite
유지 — 기존 수동 삭제 정책과 동일). 실측(2026-07-28, 내부 Jenkins): 등록 15개 대비 미등록
후보 2건(`gj-dweb-regression-api-dev`, `gj-dweb-regression-api-stg`) 발견·생성 확인, 재실행
시 0건(멱등).

**바꾸려면**:
- **스캔 루트를 넓히고 싶다** → `.env` `JENKINS_API_SYNC_ROOTS` 에 콤마 구분 폴더 경로
  추가(코드 변경 불필요).
- **매칭 키워드(`api`)를 바꾸고 싶다** → `services_sync.py` 의 `_MATCH_KEYWORD` 상수.
- **자동 주기(10분)를 바꾸고 싶다** → `sweep_reliability.py` 의 `cache.add(..., 600)` 세
  번째 인자(초).
- **스캔 깊이/예산을 조정하고 싶다** → `services_sync.py` 의 `_MAX_DEPTH` / `_MAX_NODES`.

### `QualityGateRunTest` — Suite 별 테스트 결과 (`apps/qualitygates/models.py:570`)

`gate_run` × `gate_suite` (n:m 중간 테이블에 상태 추가). `current_suite_index` 가 가리키는 항목이 현재 진행 중.

| 필드 | 비고 |
|---|---|
| `attempt` | PositiveInt default 1. Suite 의 현재 시도 회차(1-based). 빌드 실패 후 `attempt < retry_limit` 이면 PENDING 으로 되돌려 재트리거. 수동 `rerun_test` 리셋 시 1로 초기화. 마이그레이션 `apps/qualitygates/migrations/0023_qualitygateruntest_attempt.py`. |
| `retry_limit` | PositiveInt default 1. Run 시작 시점의 `suite.retry_count` 스냅샷. `trigger_gate_run` 에서 `max(1, gs.test_suite.retry_count)` 로 채워진다 (`apps/qualitygates/services.py:125`). 진행 중 suite 설정을 바꿔도 이 Run 의 재시도 한도는 고정. `_poll_one_test_build` 는 `test_run.retry_limit` 스냅샷만 읽는다 (`apps/qualitygates/views.py:1980`). 마이그레이션 `apps/qualitygates/migrations/0024_qualitygateruntest_retry_limit.py`. |

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

### `UiAutomationOwners` — UI Test Automation(TAP) 실패 알림 담당자 (`apps/integrations/models.py`, 2026-10-02)

TAP 케이스는 API URL 매칭 개념 밖이라 `ApiEndpointOwner` 매칭이 성립하지 않는다 — Suite/케이스 구분 없이 **UI 실패 알림 전체에 멘션할 담당자**를 통으로 두는 **singleton**. 마이그레이션 `0009_ui_automation_owners` (테이블 `integrations_ui_automation_owners`). 구조·`get_solo()`·`names()` 는 `FallbackOwners` 와 동일 (`owners` 콤마 다중값 max 500 + `updated_at`).

발송 흐름:
- `apps/notifications/services.py:_ui_owner_mentions()` → `owner_mentions.mentions_from_names(UiAutomationOwners.get_solo().names())` → `slack_users.find_user_id_by_name`. `send_tap_run_event` 가 **`TAP_RUN_FAILED` 일 때만** 메시지 top-level text(제목 아래 줄)에 `*담당자 F/U* <@U..> @이름` 을 붙인다 — attachment field 가 아니라 top-level text 라 푸시 알림 본문에도 노출된다. 시작/PASS 는 멘션 없음.
- **Reliability 스케줄 실행 FAIL 도 멘션** — API Suite 는 스케줄 run(`triggered_by` 없음) 의 담당자 F/U 를 빼고 Triage 버튼으로만 멘션하지만, TAP 은 Triage 버튼이 없고 운영 UI 실패 알림 대부분이 스케줄 실행(2026-10-02 기준 9/8 이후 수동 실행 FAIL 0건)이라 그 정책을 따르지 않는다. 자동 재시도 중간 시도는 원래 FAIL 미발송이라 최종 시도에만 멘션.
- 미설정(`owners=""`) 이거나 조회 예외(예: 배포 직후 migrate 전 테이블 없음) 면 멘션 없이 FAIL 알림은 그대로 발송 (`_ui_owner_mentions` 가 silent `""`).

Admin UI:
- 진입: `/accounts/api-owners/?tab=ui` — 담당자 관리 화면 **내부 탭 'UI 담당자'**. `api_owner_list` 가 `tab=ui` 면 API 표/시트 조회를 건너뛰고 `owner_tab="ui"`, `ui_owners_text` 만 넘겨 같은 템플릿의 UI 분기를 렌더.
- 폼: `UiAutomationOwnersForm` (`apps/integrations/forms.py`) — 단일 `owners`.
- 저장 endpoint: `accounts:ui_owners_save` (`/accounts/api-owners/ui/save/`, POST 만, admin) → `?tab=ui` 로 redirect. ActivityLog `UPDATE` + target_type `API_ENDPOINT_OWNER` + target_name="UI 알림 담당자".
- 테스트: `apps/integrations/tests.py:UiAutomationOwnersTests` (멘션·스케줄 run 멘션·미설정·시작/PASS 미멘션·조회 실패 silent·탭 렌더·저장/권한).


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

`base_url` 을 넘기지 않으면 `is_internal = True` (`jenkins.py:58`). 내부/외부 구분이 인증 방식과 트리거 방식을 결정한다.

테스트 Job 트리거는 `JenkinsClient()` (인자 없이) 호출 → `is_internal=True`, Quality Gate 의 deploy/image/prod 는 게이트 자격증명을 명시 → `is_internal=False`. Suite 별로 외부 Jenkins 를 쓰는 경우 `suite.get_jenkins_client()` 가 `TestSuite.jenkins_base_url/user/api_token` 을 명시적으로 넘긴다.

**내부 Jenkins 인증 방식 (Test Suite / Reliability — `is_internal=True`)**

내부 Jenkins 는 Keycloak SSO 뒤에 있어 `user:api_token` Basic auth 가 SSO 세션 만료 시 거부되는 근본 문제를 GWT + 익명 read 방식으로 해결했다(`jenkins.py:54-68`):
- **읽기(상태/스테이지/콘솔/idle 조회) = 익명** — `_auth_header` 가 `is_internal=True` 일 때 `""` 를 반환해 `Authorization` 헤더를 안 보냄(`jenkins.py:61-64`). Jenkins 에서 Role-Based Strategy 로 `anonymous` 에 Overall/Read + Job/Read(+Cancel) 부여돼 있어야 동작.
- **트리거 = Generic Webhook Trigger(GWT) 토큰** — `trigger_webhook(job_full_name, token)` (`jenkins.py:211`) 이 `POST /generic-webhook-trigger/invoke` 에 `token` 헤더로 워커별 고유 토큰을 보낸다. 상관키(build_number)는 트리거 직전 익명 `get_next_build_number` 예측값. `trigger_job(job_full_name, gwt_token=None)` (`jenkins.py:239`) 이 내부 = `trigger_webhook`, 외부 = `trigger_build`(Basic) 로 디스패치.
- **워커별 토큰 필드**: `TestSuiteJob.gwt_token` (`apps/suites/models.py:179`). `gen_gwt_token()` 로 `qgwt_` + hex(20) 생성. 발급 후 Jenkins 워커 → Configure → Build Triggers → Generic Webhook Trigger → Token 에 동일한 값을 넣어야 한다.
- **트리거 호출부**: `apps/suites/views.py:_try_trigger_next` (단독/QG 경로, `views.py:838`) 와 `apps/robustness/services.py:_trigger_all_pending` (Reliability, `services.py:213`) 모두 `client.trigger_job(..., gwt_token=job_run.job.gwt_token)` 로 교체됨.
- **GWT 토큰 관리 UI**: Suite 상세(`/suites/<pk>/`) 에 admin + 내부 suite 한정으로 "GWT 토큰" 카드 노출. "전체 발급" → `gwt_tokens_generate` 뷰(`views.py:103`), 워커별 "재발급" → `gwt_token_regen` 뷰(`views.py:124`).

**외부 Jenkins (`is_internal=False`) — 기존 Basic auth 유지**

외부 Jenkins(suite 가 `jenkins_base_url` 지정)는 SSO 가 없으므로 `_auth_header` 가 `Basic <credentials>` 를 반환하고, 트리거도 기존 `trigger_build` 경로를 그대로 쓴다.

**Quality Gate 파이프라인 — 아직 Basic auth**

QG 파이프라인의 deploy/image/prod 트리거는 아직 `trigger_build`(Basic auth) 방식 그대로다. 단 읽기(폴링)는 내부 클라이언트(`is_internal=True`)이므로 이미 익명으로 동작. 트리거 전환은 별도 작업.

핵심 메서드:
- `get_job_info(job)` — 파라미터 정의 + choices. **Active Choices / Git Parameter 대응을 위해 4가지 경로를 병렬 시도** (`ThreadPoolExecutor max_workers=4`):
  1. HTML 빌드 폼 스크래프 (`_scrape_build_form_choices`) — `<select name="value">` 파싱
  2. `/scriptText` Active Choices Groovy (`_get_active_choices_via_script`) — Overall/RunScripts 권한 필요
  3. Git Parameter descriptor HTTP (`_fetch_git_parameter_choices`) — JSON or HTML, 파라미터당 1회
  4. `/scriptText` GitParameter Groovy (`_get_git_parameter_choices_via_script`)

  병합 우선순위: HTML > Active Choices script > Git HTTP > Git script.
  Active Choices 의 `:selected` / `:disabled` 마커는 `_strip_active_choice_marker` 로 제거.
- `trigger_build(job, parameters)` — 파라미터 유무로 `/build` vs `/buildWithParameters` 자동 선택. 빌드 트리거 직전 `get_next_build_number(job)` 로 번호 미리 확보. **외부 Jenkins / Quality Gate 전용** — 내부 Suite/Reliability 는 `trigger_job` → `trigger_webhook` 경로.
- `trigger_webhook(job_full_name, token)` (`jenkins.py:211`) — GWT 전용. Authorization 헤더 없이 `token` 헤더만.
- `trigger_job(job_full_name, gwt_token=None)` (`jenkins.py:239`) — 내부/외부 트리거 통합 진입점.
- `get_build_info` / `get_build_stages` (wfapi) / `get_console_output` / `is_job_building` / `stop_build` / `get_last_build_info`.

**모듈 레벨 순수함수** (클래스 외부):
- `extract_pushed_image_tag(console_output, build_number, repo_hint=None)` (`jenkins.py:866`) — `create-image` 빌드 콘솔에서 jib `--target=<registry>/<repo>:<tag>` 패턴으로 ECR 이미지 태그를 추출. `build_number-` prefix 가드로 이 빌드 소속 보장. 실패 시 None. 자세한 동작은 §2 "수동 모달 APP_IMAGE default 오버라이드" 의 A 항목 참조.
- `_TARGET_RE`, `_ECR_REF_RE` — `extract_pushed_image_tag` 가 사용하는 컴파일된 정규식 상수 (`jenkins.py:857` 부근).

**SSO 가 깔린 Jenkins 의 인증 리다이렉트 감지** (외부/QG 경로 대상): `_request` 가 HTTPError 를 받을 때 `Location` 헤더가 `commenceLogin` / `securityRealm` 으로 향하거나 `reason` 에 "infinite loop" / "redirect error" 가 보이면 → basic auth 가 거부된 것. 한국어 메시지로 `JenkinsError(status=401)` raise(`jenkins.py:102`). 내부 Suite/Reliability 는 읽기가 익명이라 이 분기에 안 걸린다.

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
| Suite Run 트리거 (단독) | `apps/suites/services.py` 의 3개 trigger 헬퍼(`_trigger_single_idle_run`/`_trigger_single_job_run`/`_trigger_chain_run`) | `SUITE_RUN_STARTED` |
| Suite Run 트리거 (**Reliability Test**) | `apps/robustness/services.py:_trigger_once` (trigger 직후) | `SUITE_RUN_STARTED` |
| Suite Run 완료 (성공) | `_poll_run_status` (failed_jobs == 0) | `SUITE_RUN_PASSED` |
| Suite Run 완료 (실패/abort 포함) | `_poll_run_status` (failed_jobs > 0) | `SUITE_RUN_FAILED` |

**START 발송 위치 주의**: PASS/FAIL 은 단독·Reliability 가 공용 `_poll_run_status` 를 거쳐 자동 발송되지만, **START 는 각 트리거 경로가 직접 보낸다**. Reliability 경로는 `_trigger_once` 가 `suite_run` 을 직접 생성하므로 거기서 `SUITE_RUN_STARTED` 를 보낸다 — 이게 빠져 있어 Reliability Test 시작 노티만 안 가던 버그가 있었다(PASS/FAIL 은 정상). 새 트리거 경로를 추가하면 START 발송도 같이 넣어야 한다.

**의도적 예외**: 위 표의 PASS/FAIL 판정(`_poll_run_status`)은 `raw failed_jobs`(FAILURE+ABORTED 합산) `> 0` 그대로다 — 2026-07-14 "ABORTED(중단)는 장애가 아니다" 정책(§3 `annotate_triage`/대시보드 결과 분포/Reliability `_job_aggregate`)을 여기·`apps/notifications/services.py:_build_followup_for_job_run`(실패 job 발췌 대상 선정 — "Slack 자동 에러 알림" 절 참조)에는 적용하지 않았다 — Slack FAIL 알림의 발송 여부·발췌 로직은 기존 동작 유지가 요구사항이라 그대로 두었다(포털 화면 쪽 표시만 ABORTED 를 제외).

QG 파이프라인의 `TEST_SUITE_*` 와 토글이 별개이므로 어드민에서 따로 끄고 켤 수 있다. `_abort_suite_run` 은 별도 알림을 발송하지 않는다 (취소가 명시적 사용자 액션이라 추가 알림 가치 낮음).

#### 4.4.3b Suite Run 실패 알림 Triage 버튼 (Failure / Skip) — Socket Mode (2026-07-07)

`SUITE_RUN_FAILED` Slack 메시지에 실패 Job 별 **[🔴 Failure] [⏭️ Skip]** 버튼을 달아, 클릭으로 그 Job 의 실패 성격을 확정한다 — `Failure`=실제 장애 / `Skip`=자동화 코드·환경 문제. **인트라넷이라 인바운드 공개 URL 대신 Socket Mode(앱→Slack 아웃바운드 WS)로 클릭을 수신** (PoC 로 실환경 검증 완료).

- **데이터**(`apps/suites/models.py`): `TestSuiteJobRun.verdict`(`""`/`failure`/`skip`, `Verdict` TextChoices) + `triaged_by`/`triaged_at`. `TestSuiteRun.slack_message_ts`/`slack_channel_id`(chat.update 용). 마이그레이션 `0013`. `TestSuiteJobRun.result_label` 프로퍼티가 화면 결과(Pass/Failure/Skip/Fail(미분류))를 파생.
- **발송**: `services.send_suite_run_event` 의 FAILED 분기가 `triage.triage_buttons_enabled()`(env `SLACK_TRIAGE_BUTTONS=1`, 기본 off) 일 때 `build_suite_fail_payload(suite_run, ctx)`(blocks)로 발송 + 응답 ts/channel 을 run 에 저장. `_do_post`(신규, 응답 dict 반환)/`_post_message`(기존 bool 유지). 버튼 blocks 는 `triage.build_triage_blocks`(Job별 미분류=버튼/분류됨=처리됨 context, value=job_run_id). **flag off 면 기존 attachment 경로 그대로**(회귀 무영향).
- **수신**: 상시 프로세스 `manage.py slack_triage_listener`(`apps/notifications/management/commands/`) 가 slack_sdk `SocketModeClient` 로 block_actions 수신 → `dispatch_interaction` → `triage.apply_triage_action(action_id, job_run_id, slack_user_id)`. **권한 = `slack_user_id` 매핑 + `can_write`**(Viewer/미매핑 거부, response_url ephemeral 안내). **재분류(덮어쓰기) 허용**. 처리 후 `update_suite_fail_message`(`chat.update`, ✅ 대신 `➡️` 로 "분류됨" 표기)로 원본 메시지 갱신 + **분기별 쓰레드 멘션**(`post_triage_followup`, verdict 가 실제 바뀐 경우만) — **FAILURE → 실패 담당자 멘션 + "장애 F/U 진행이 필요합니다."** / **SKIP → 김태식·김도연 고정 멘션(`_SKIP_MENTION_NAMES`) + "자동화 테스트케이스 점검 요망"**. SKIP 정의 = "자동화 판정 오류 의심 및 자동화 코드 이슈 추정". followup 링크는 `에러 로그 상세 보기`(구 '포털 로그에서 보기'), blocks 하단 중복 'Run 상세' 링크 제거, 에러 발췌 2000자(section 2900자 안전 truncation).
- **스케줄 run 은 담당자 F/U 제외**: `_build_followup_for_job_run(..., owner_fu=False)` (`_suite_event_fields` 가 `suite_run.triggered_by is None` 이면 전달) — 상시 자동 실패가 담당자를 계속 핑하지 않도록 본문에서 **담당자 멘션·`→ 담당자` 배정을 뺀다**(실패 URL·에러 발췌·버튼은 유지). 담당자 알림은 필요 시 Triage `Failure` 버튼 → 쓰레드 멘션으로만. 수동(triggered_by 있음)은 기존대로 담당자 F/U 포함.
- **스케줄 중단/재활성화 버튼**(스케줄 run 한정): `build_schedule_action_blocks(suite_run)` — `_schedule_for_run`(=`suite_run.robustness_runs.first().schedule`, RobustnessSchedule)이 있을 때만 노출. 상태(`TestSuiteRun.slack_schedule_action` = ""/"stopped"/"reactivated") + 스케줄 `is_running` 로 분기: 실행중→`🛑 스케줄 중단`(action_id `schedule_stop`), 중단됨→`▶️ 스케줄 재활성화`(`schedule_reactivate`), 재활성화됨→context 만(버튼 없음). value=suite_run_pk. 핸들러 `apply_schedule_action`(리스너 `dispatch_interaction` 라우팅) — **권한=지정 담당자(김태식·김도연)만** (`_is_schedule_controller`: 포털 `User.first_name+last_name` 이 `_SCHEDULE_CONTROLLER_NAMES=["김태식","김도연"]` 에 포함될 때만; triage 버튼의 `can_write` 보다 강한 화이트리스트. 버튼 자체는 채널 모두에게 보이되 그 외 클릭 시 `schedule_forbidden` ephemeral 안내 "김태식·김도연 만 사용 가능"). 중단=`robustness.services.stop_schedule`(상위 스위트 스케줄 정지+진행 run abort), 재활성화=`start_schedule`+`sweep_schedule`(즉시 1회). 처리 후 `slack_schedule_action` 저장 + `update_suite_fail_message`(chat.update). 모델: 마이그 0014.
- **결과 반영 (verdict-aware, 2026-07-08)**: Skip 분류된 실패는 **대시보드·Suite Runs 목록에서 FAIL 로 안 센다**. `TestSuiteRun.triage_result`(`pass`/`fail`/`skip`/**`aborted`**, 아래) + `TestSuiteRun.annotate_triage(qs)`(`real_failed = failed_jobs − Skip 처리된 실패 Job 수 − 미분류(verdict="") ABORTED Job 수`, F-expression 주석 — job_run 레코드 유무 무관하게 일관, 레거시 job_run 없는 run 은 기존처럼 failed_jobs 그대로 실패). **사람이 verdict=failure 를 명시 확정한 ABORTED 는 빼지 않는다** — 실장애로 인정해 Run 상세 '🔴 FAILURE' 배지와 집계가 일치. 적용처: 대시보드 `_compute_metrics`/`_compute_daily`/`_compute_result_dist`/`_compute_suite_stats`/`recent_suite_runs`, `suite_run_list`(+`result=skip` 필터 옵션), `job_run_list`(도넛 드릴다운), 각 템플릿 결과 배지(PASS/FAIL/SKIP/**ABORTED**). Suite Run 상세(`_run_status.html`)는 실패 Job 에 `🔴 실장애`/`⏭️ Skip` 배지. **규칙(2026-07-14, "ABORTED=장애 아님" 반영): real_failed>0 → FAIL, real_failed==0 이고 Skip 분류 있음 → SKIP, real_failed==0 이고 Skip 도 없음(=실패 Job 전부 미분류 ABORTED) → ABORTED(비-실패). Failure 확정 ABORTED 는 real_failed 에 포함돼 FAIL.** `run_list.html`/`dashboard/index.html` 은 `triage_result=='aborted'` 에 별도 ABORTED 배지(`status-na`) 분기 — 없으면 PASS 로 새 보인다. Slack "분류됨" context 는 성공 ✅ 과 헷갈려 `:arrow_right:` 로 표기.
- **판정 공통 코어 (`apps/notifications/triage.py:apply_verdict`, 2026-07-08 / `apply_verdicts_bulk` 2026-07-20)**: `apply_verdict(job_run, verdict, user)` — verdict 저장(덮어쓰기 허용) + Slack 원본 메시지 갱신(`update_suite_fail_message`, ts 있을 때) + verdict 실제 변경 시 쓰레드 멘션(`post_triage_followup`). 권한 검사는 호출부 책임. **Slack 버튼**(`apply_triage_action`) 과 **포털 판정 화면**(`suites:job_run_triage`) 이 이 코어를 공유 — 포털에서 판정해도 Slack 원본 메시지 갱신 + 담당자 F/U 멘션이 발생한다. `apply_verdicts_bulk(job_runs, verdict, user)`(`triage.py:216`)는 다건 버전 — **DB 저장(verdict/triaged_by/triaged_at)은 동기**(요청 응답 시점에 큐에서 즉시 사라짐), **Slack chat.update/쓰레드 멘션은 데몬 스레드로 비동기**(웹 프로세스 상주라 join 불필요). `chat.update`는 **suite_run 별 1회로 dedupe**(같은 run 의 job 여러 개 선택해도 원본 메시지 갱신은 한 번), 쓰레드 멘션(`post_triage_followup`)은 **verdict 가 실제로 바뀐 job 만** 발송(재확정 시 중복 방지, 단건과 동일 정책). 권한 검사는 여기서도 호출부 책임. 반환 `{"count":, "changed":}`.
- **포털 판정 큐 (2026-07-08; ABORTED 제외 2026-07-14; 체크박스 일괄 판정 2026-07-20)**: `apps/suites/views.py:untriaged_list`(591) / `job_run_triage`(611) / **`untriaged_bulk_triage`(650)**. `TestSuiteJobRun.untriaged_pending()` classmethod — status=**FAILURE 만**(과거엔 `aborted` 도 포함했으나 "ABORTED=장애 아님" 정책으로 제외 — 스케줄 정지·수동 취소로 중단된 Job 이 판정 대기로 쌓이지 않게) & verdict="" & 발생시각(`Coalesce(completed_at, started_at, suite_run__started_at)`) ≥ `settings.UNTRIAGED_QUEUE_SINCE`(기본 `2026-07-08T05:58:00+00:00`, env `UNTRIAGED_QUEUE_SINCE` override). 최신순 최대 200건. 단건 판정 POST: `suites:job_run_triage`, redirect — `next=run_detail` 이면 Run 상세, 아니면 untriaged 큐. `ActivityLog(action="job_triaged")` 기록.
  - **일괄 판정** — `templates/suites/untriaged_list.html`: `can_write` 사용자에게 행별 체크박스(`name="job_run_ids"`, `form="ut-bulk-form"` 로 테이블 밖 `<form id="ut-bulk-form">` 에 연결) + 헤더 `#ut-check-all`(전체선택, 부분선택 시 `indeterminate`) + 상단 액션 바 [🔴 선택 일괄 장애][⏭️ 선택 일괄 Skip](선택 0건 disabled, `confirm()` 에 선택 수 표시). 빈 목록 `colspan` 은 `can_write` 여부로 7/6.
  - `POST /suites/runs/untriaged/bulk-triage/`(`name=untriaged_bulk_triage`) — `can_write` 필수(아니면 메시지+redirect). `verdict` 화이트리스트 위반은 `HttpResponseBadRequest`(400). `job_run_ids` 300건 초과도 400(큐가 최신 200건만 노출하므로 그 이상은 비정상 요청). 각 id 는 `uuid.UUID()` 파싱 실패 시 400(비정상 호출 차단 정책). 빈 선택은 메시지+redirect(에러 아님). 대상은 **`status=FAILURE` 로 필터링**(큐 표시 기준과 동일 — 요청에 ABORTED/SUCCESS id 가 섞여도 걸러짐). 처리 후 `apply_verdicts_bulk` 호출 → `ActivityLog(action="job_triaged", target_name=f"일괄 판정 {N}건")`(잡 이름 10개까지 나열 + 초과분은 "외 N건") 1건 기록 → untriaged 큐로 redirect. `verdict=""` 조건은 걸지 않는다 — 재분류(덮어쓰기) 허용 정책(단건/Slack 버튼과 동일), 같은 값 재확정은 followup 미발송이라 무해.
  - **Suite 필터 (복수 선택, 즉시 적용 — 2026-07-20)**: `untriaged_list` 가 `?suite=<uuid>`(반복 가능) GET 파라미터로 대기 목록을 필터. 칩 UI 는 대기 건이 있는 Suite 만 건수와 함께 노출(`suite_counts` = `untriaged_pending().values(suite id/name).annotate(Count).order_by(이름)` — **order_by 명시**로 기본 정렬 `_evt` 가 GROUP BY 에 끼는 것 방지, 건수는 필터와 무관하게 전체 대기 기준). 칩 체크박스 `onchange="this.form.submit()"` 으로 적용 버튼 없이 즉시 반영, "전체 보기" 링크로 해제. UUID 형식이 아닌 값은 조용히 버림(`_parse_suite_filter` — run_list suite 필터와 동일한 관용 정책). **판정 후 필터 유지**: 단건/일괄 판정 폼에 `filter_suite` hidden 으로 현재 필터를 실어 보내고 `_untriaged_redirect(request)` 가 검증 후 쿼리스트링으로 복원해 redirect(`job_run_triage` 는 untriaged 복귀 경로만 해당). 테스트: `apps/suites/tests.py:UntriagedSuiteFilterTests`(8종 — 단일/복수 필터, 잘못된 id 무시, 칩 건수 전체 기준, redirect 필터 보존/오염값 제거).
- **활성화 요건**: ① `.env` `SLACK_TRIAGE_BUTTONS=1` ② `SLACK_APP_TOKEN=xapp-`(Socket Mode) + `SLACK_BOT_TOKEN`(chat:write) ③ Slack 앱 Socket Mode·Interactivity ON ④ EC2 아웃바운드 wss 허용 ⑤ `slack_triage_listener` **systemd 상주화**(`deploy/qg-slack-triage.service`, 웹은 `qualitygate.service`) ⑥ 클릭할 사람의 `User.slack_user_id`. 리스너 미배포 상태면 `SLACK_TRIAGE_BUTTONS` off 로 죽은 버튼 방지.
- **`slack_user_id` 자동 매핑**(2026-07-08): `apps/integrations/slack_user_sync.py:backfill_user_slack_ids()` 가 `users.list` 의 profile.email(봇 `users:read.email` scope) 로 활성 User(이메일 일치)를 Slack ID 에 연결. 기본은 빈 값만 채움(수동값 보존, `--overwrite` 로 갱신). 수동 `manage.py sync_slack_user_ids`, 자동은 **`sync_owners_from_google_sheet` 끝에서 best-effort 호출** → 담당자 시트 동기화(매분 cron `--if-due`) 때마다 신규 유저 자동 반영. 이메일 없는/불일치 계정(admin·test·봇)은 미매칭으로 남고 그 사람이 클릭하면 "포털 계정 미연결" ephemeral 안내.
- 미적용: QG 파이프라인 `TEST_SUITE_FAILED`·편집/삭제 write-back·다중 Job 은 Job별 버튼(값=job_run_id)으로만. 테스트: `apps/notifications/tests.py`(18) — apply 전 케이스/블록/게이팅/dispatch/result_label/flag. PRD: `prd_slack_triage_buttons.md`.

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
| `TAP_RUN_FAILED` (`send_tap_run_event`) | Suite(프로젝트), 환경, 실행자/스케줄, PASS/FAIL/기타/총, 소요 시간, 실패 케이스(TestRail 링크, 최대 5), 포털/TAP 링크 + **top-level text 에 UI 알림 담당자 멘션** (`UiAutomationOwners`, 2026-10-02) |

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
| `/` | `dashboard` | Suite 기준 14일 요약 카드 + Chart.js 차트 2행(1행: 일별 stacked bar·Suite Pass Rate hbar / 2행: 도넛 2개 — 서비스 품질·QG 품질 지표) + 미 장애 판정 카드 + 최근 TestSuiteRun 15건 |
| `/accounts/` | `accounts` | login (`sso.login_view`, Keycloak 전용 로그인 페이지) / logout (`sso.logout_view`) / **SSO 시작·콜백 `oidc/<provider>/login/`·`oidc/<provider>/callback/`** (`name=sso_login`·`sso_callback`) / signup (SSO 전용이면 404) / 사용자 관리 / Slack 알림 설정 (`/accounts/notifications/`) |
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
- **Suite 상세 (Run 전체 이력)**: `/suites/runs/` (`name=run_list`, view: `suite_run_list`, 템플릿: `templates/suites/run_list.html`) — 모든 suite 의 단독 Run 을 **최근 활동 순**(`Coalesce(completed_at, started_at)` 내림차순, 2026-07-07) 최근 100건 — 뒤늦게 완료된 실패도 상단 노출. 필터: `suite`, `status`, `source`(manual=트리거 주체 있음, scheduled=스케줄/자동), **`result`(pass/fail/skip — completed 행만 적용, 뷰에서 `f_result` 쿼리 처리)**. `fail`=`real_failed>0`(Skip·**ABORTED** 제외한 실장애), `skip`=Skip 분류 있음 & `real_failed=0`. 행 배지는 PASS/FAIL/SKIP 외에 실패 Job 전부 ABORTED 인 run 은 **ABORTED 배지**(2026-07-14 — 장애로 안 셈). 사이드바 "Suite Runs"(fa-list-ul) 메뉴에서 진입(구 "Suite 상세"). 대시보드는 최근 15건 요약만 보여줌.
- **미 장애 판정 큐**: `/suites/runs/untriaged/` (`name=untriaged_list`, view: `untriaged_list`, 템플릿: `templates/suites/untriaged_list.html`) — 컷오프(`settings.UNTRIAGED_QUEUE_SINCE`) 이후 verdict="" 인 **실패(FAILURE) Job Run**(ABORTED 제외, 2026-07-14) 최신 200건. [🔴 장애][⏭️ Skip] 버튼(`can_write` 필요) + **체크박스 선택 후 일괄 판정**(2026-07-20, 상세 §4.4.3b).
- **판정 엔드포인트**: `/suites/job-runs/<uuid:pk>/triage/` (`name=job_run_triage`, POST, `can_write`) — `verdict=failure|skip`. 처리 후 redirect(`next=run_detail`이면 Run 상세, 아니면 untriaged 큐). `apply_verdict` 공통 코어 호출 → Slack 원본 갱신 + F/U 멘션. `ActivityLog(action="job_triaged")` 기록.
- **일괄 판정 엔드포인트**: `/suites/runs/untriaged/bulk-triage/` (`name=untriaged_bulk_triage`, POST, `can_write`) — `verdict=failure|skip` + `job_run_ids`(다건). `status=FAILURE` 만 대상 필터, 300건 초과·UUID 형식 오류는 400. `apply_verdicts_bulk` 호출(DB 저장 동기, Slack 은 비동기 dedupe). `ActivityLog(action="job_triaged", target_name="일괄 판정 N건")` 기록 후 untriaged 큐로 redirect. 상세 §4.4.3b.
- **Job 결과 목록 (도넛 드릴다운)**: `/suites/job-runs/` (`name=job_run_list`, view: `job_run_list`, 템플릿: `templates/suites/job_run_list.html`) — 최근 14일 완료 Suite Run 의 Job Run 최신 200건(읽기 전용). 필터 `result`(success/failure=**status=FAILURE**·Skip 아닌 실패·미분류 포함 **+ status=ABORTED&verdict=failure**(명시 확정)/skip=**status=FAILURE·ABORTED**&verdict=skip)·`suite` — **미분류 ABORTED(중단) Job 은 어느 필터에도 안 잡히고**(배지도 FAIL 아닌 ABORTED — `result_label` 이 미분류 ABORTED 에 'Aborted' 반환), verdict 확정된 ABORTED 는 그 판정을 따름. 카운트는 대시보드 결과 분포 도넛과 동일 산식. `run_list`(`suite_run_list`)에는 **`date` 필터**(일별 막대 드릴다운, `started_at` 로컬 날짜) 추가.
- Queue: `/suites/queue/`, `/suites/queue/stop-all/`
- 콘솔: `/suites/job-runs/<pk>/console/`
- 에러 로그: `/suites/job-runs/<pk>/error-log/` — 콘솔에서 실패 원인만 파싱 (`apps/integrations/log_errors.py`)
- **Jenkins 자격증명 관리**: `/suites/credentials/` (admin 전용 — 내부/외부 default 자격증명 저장)
- **GWT 토큰 관리**: `/suites/<pk>/gwt-tokens/generate/` (`name=gwt_tokens_generate`, 전체 발급), `/suites/jobs/<pk>/gwt-token/regen/` (`name=gwt_token_regen`, 워커별 재발급) — admin + 내부 suite 한정
- **API 탭 Jenkins 자동 싱크**: `/suites/api-sync/` (`name=suites_api_sync`, POST, admin 전용) — `apps.suites.services_sync.sync_api_suites()` 호출(§3 "API 탭 Jenkins 잡 자동 싱크" 참조). 10분 주기 자동 호출은 `sweep_reliability` cron 이 겸함(crontab 무변경).

---

## 6. 프론트엔드 — htmx + qg_notify.js

### 6.0 대시보드 차트 (`templates/dashboard/index.html`)

대시보드는 Suite 중심으로 개편(2026-06-10). 집계 기간 전면 14일 일원화 + 차트 2행 구조 + 미 장애 판정 카드 추가(2026-07-08). DORA 섹션은 제거됨.

- **Chart.js 4.4.1** — `extra_head` 에서 cdnjs 로 로드.
- **데이터 전달**: `{{ daily|json_script:"chart-daily" }}`, `{{ suite_stats|json_script:"chart-suite-stats" }}`, `{{ dist_quality|json_script:"chart-dist-quality" }}`, `{{ dist_skip|json_script:"chart-dist-skip" }}` — XSS 안전한 `json_script` 패턴. JS 에서 `JSON.parse(document.getElementById('...').textContent)` 로 읽음. **주의: json_script id 와 canvas id 가 같으면 `getElementById` 가 script 태그를 집어 Chart.js "can't acquire context" 오류 발생 — canvas id 에는 `-canvas` 접미사 필수** (예: json id `chart-dist-quality`, canvas id `chart-dist-quality-canvas`).
- **차트 2행 구조** (`extra_js` 블록 내 즉시 실행 함수):
  - 1행: `chart-daily-bar` — stacked bar (pass=#22c55e, fail=#ef4444), 14일 일별 추이. / `chart-suite-hbar` — horizontal bar, pass_rate>=90→초록, >=50→노랑, 미만→빨강, 상위 10 suite 14일.
  - 2행(신규): `chart-dist-quality-canvas` — doughnut (Pass/Failure), cutout 65%, 가운데 Pass Rate %. / `chart-dist-skip-canvas` — doughnut (Pass/Skip), 가운데 Pass Rate %.
- **컨텍스트 변수** (backend `apps/dashboard/views.py` 에서 주입):
  - `metrics` — `{total_runs, passed_count, failed_count, pass_rate, running_count, today_count}` (14일)
  - `daily` — `[{label, date, pass, fail}]` × 14일 (`date`=ISO, 막대 클릭 드릴다운용)
  - `suite_stats` — `[{suite_id, name, total, pass, fail, pass_rate, avg_duration_sec}]` 상위 10 (14일)
  - `dist_quality` — `{pass, fail}` Job 단위 (14일, 서비스 품질 지표 — 실장애·미분류 포함)
  - `dist_skip` — `{pass, skip}` Job 단위 (14일, 퀄리티게이트 품질 지표)
  - `untriaged_count` — 컷오프 이후 미분류 실패 Job 수 (기간 무관)
  - `recent_suite_runs` — `TestSuiteRun` 15건 (suite, seq_no, status, passed_jobs/failed_jobs/total_jobs, triggered_by, started_at, pk)
- **데이터 없을 때**: 각 차트 `canvas` 를 `<p>데이터가 없습니다.</p>` 로 교체.
- **클릭 드릴다운 (2026-07-08)**: 모든 차트에 `options.onClick`(+`onHover` 커서 pointer). 클릭 요소 → 필터된 목록으로 `window.location.href`.
  - 일별 막대: dataset 0(Pass)→`/suites/runs/?result=pass&date=<daily[i].date>`, 1(Fail)→`result=fail&date=…`.
  - Suite 막대: →`/suites/runs/?suite=<suite_stats[i].suite_id>`.
  - 도넛(공통 `buildDoughnut(...,sliceLinks)` 인자): quality slice 0/1→`/suites/job-runs/?result=success|failure`, skip slice 0/1→`?result=success|skip`.
  - 대상 뷰: `suite_run_list` 는 `date` 필터(started_at 로컬 날짜) 추가, `job_run_list`(신규, `apps/suites/views.py`)는 최근 14일 Job Run 을 `result`(success/failure/skip)·`suite` 로 필터해 `templates/suites/job_run_list.html` 렌더(읽기 전용, 판정 없음).

### 6.1 폴링 (htmx)

Run 상세 페이지는 5초마다 `runs/<pk>/status/` GET → `_run_status.html` 로 교체. 서버는 그 안에서 `_poll_gate_run(run)` 을 동기로 호출.

### 6.2 OS 알림 인프라

`templates/base.html` 에 인프라가 배치돼 있다:
- `#qg-toast-container` — 토스트 컨테이너 (우상단)
- `#notify-toggle-btn` (사이드바) — ON/OFF 토글
- `#qg-global-notify-test` — 우하단 고정 진단 버튼 + 가이드
- `#qg-notify-banner` — 상단 권한 요청 배너
  - 내부는 `<div style="display:flex;...flex-wrap:wrap">` 1개로 감싼 구조: 좌측 아이콘+`#qg-notify-banner-text`(핵심 문구, 항상 전체 노출) / 우측 `.qg-notify-hint`(보조 안내, `title` 속성에 전체 문구 — 자리 부족 시 ellipsis 로 잘리거나 다음 줄로 wrap). 바깥 `#qg-notify-banner` 자체의 `display: none|block` 토글은 `qg_notify.js updateBanner()` 가 인라인으로 계속 수행하므로 그 값은 건드리지 않는다.
  - `.visualization-page` 페이지는 `qg_highway.css` 의 `.visualization-page #qg-notify-banner .qg-notify-hint { flex-basis: 100% }` 로 보조 안내를 항상 다음 줄로 강제(cockpit UI 압축용) — 배너 내부 구조를 바꾸면 이 셀렉터도 같이 갱신.

`/sw.js` 는 `config/urls.py:17` 의 `service_worker()` 에서 인라인으로 서빙한다. `reverse("dashboard:index")` 를 기본 이동 경로와 `Service-Worker-Allowed` 로 사용한다 (`/qg` 배포면 외부 URL `/qg/sw.js`, scope `/qg/`). `templates/base.html` 의 `data-service-worker-url` / `data-service-worker-scope` 를 `static/js/qg_notify.js` 의 등록·진단 코드가 공통으로 사용한다.

### 6.3 글로벌 워처 (`static/js/qg_notify.js`)

페이지를 이동/새로고침해도 실행 중 Run 을 계속 추적하기 위한 **localStorage 기반 워처**.

- 키: `qg_watchers_v1`
- 등록: `QGNotify.watch(key, { kind, statusUrl, runUrl, label, initialStatus })`
  - `kind = 'qg_run' | 'suite_run'`
- 5초 주기로 `statusUrl` 을 GET → 응답 HTML 의 `data-run-status="..."` 추출 → 변경 감지
- 터미널 상태 도달 시: 토스트 + 삐 + OS 알림 발사 (SW 경로 우선, 실패 시 `new Notification()`)
- 다중 탭 dedupe: `notifiedKeys` 를 localStorage 에 기록 + OS 레벨에서 stable tag 사용
- 24시간 이상 묵은 항목 자동 정리
- **세션 만료(401) 자동 새로고침** — 게이트웨이는 세션이 끝나면 fetch/HTMX/POST 에 빈 401 을 돌려준다. `qg_notify.js` 의 `reloadOnSessionExpired()` 한 곳이 fetch 래퍼와 `htmx:responseError` 를 모두 받아, 같은 출처 요청이 401 이면 페이지를 새로고침해 게이트웨이 로그인을 다시 거치게 한다(무한 새로고침 방지: `sessionStorage` 의 `qg-401-reload-at` 로 10초 안 재시도 금지). 이 파일을 고치면 `base.html` 의 `?v=` 토큰도 bump (§10 정적 파일 규칙).

`_run_status.html` 와 Suite Run fragment 는 `data-run-status` 속성으로 현재 상태를 노출해야 한다.

OS 알림 메시지 매핑은 `QG_RUN_NOTIFY_MAP` (`qg_notify.js:494`) 에 있다 — 메시지 변경 시 여기를 수정.

### 6.4 UI Test Automation (TAP) 탭 (`templates/suites/suite_list.html` 외, 2026-07-28)

Test Suites 화면(`/suites/`)이 2탭으로 확장: **API Test Automation**(기본, 기존 Jenkins 목록 마크업 그대로) / **UI Test Automation**(`?tab=ui`). 탭은 `active_tab`(view `apps/suites/views.py:suite_list`) 로 서버가 렌더 시점에 분기하는 **일반 `<a>` 링크 + 쿼리스트링** 방식(JS 토글 아님) — 기존 `.admin-tabs` CSS 재사용.

- **`templates/suites/_tap_suite_table.html`** — UI 탭의 TAP Suite 표 fragment. `suite_list.html` 안에서 `<div id="tap-suite-table" hx-get="{% url 'suites:tap_suite_table' %}" hx-trigger="load, every 10s|load" hx-swap="innerHTML">`으로 감싸 `{% include %}` — `robustness/test_detail.html` 의 `_schedule_table.html` 폴링 패턴과 동일 구조(폴링 트리거 속성은 안 스왑되는 바깥 div 에 고정, 내부만 교체). `tap_any_active`(진행 중 TapRun 존재 여부) 가 False 면 최초 `hx-trigger="load"` 한 번만 실행되고, fragment 자체에도 `tap_any_active` 가 False 인 경우 `#tap-suite-table` 의 `hx-get`/`hx-trigger` 속성을 JS 로 제거하는 안전장치가 있다(`_run_status.html` 의 완료 시 폴링 중지 스크립트와 동일 패턴) — 진행 중 Run 이 있다가 이 fragment 요청 시점에 종결되면 그 다음부터 폴링이 멈춘다. `tap_suite_table` 뷰(`apps/suites/views.py`)가 fragment 요청마다 `poll_active_tap_runs()` 를 동기 호출해 진행바를 라이브 갱신(신뢰성은 매분 cron 이 보장 — Reliability 화면과 동일 원칙).
- **진행바** — `TapRun.progress_percent`(케이스 수 기준 `completed_cases`/`total_cases`) 를 `var(--gray-200)` 트랙 + 색상 채움 바로 표시. `_run_status.html` 의 Suite Run 진행바와 같은 시각 언어(트랙/채움 div, `min-width:2px`).
- **`templates/suites/tap_run_detail.html`** — TAP Run 1건 상세. **전용 fragment 엔드포인트가 없다**(`suites:tap_run_detail` 뷰가 항상 `base.html` 을 extends 하는 전체 페이지를 렌더) — 그래서 진행 중(`run.is_active`) 갱신은 같은 URL 을 GET 하되 `hx-select="#tap-run-content" hx-swap="outerHTML"` 로 htmx 가 응답 전체 HTML 중 그 id 요소만 뽑아 자기 자신과 교체하는 방식을 쓴다. 서버가 매 요청마다 `run.is_active` 를 재평가하므로, 교체된 새 `#tap-run-content` 에 더 이상 `hx-get`/`hx-trigger` 가 없으면(=종결) 폴링이 자연히 멈춘다. htmx 2.0.4 에서 `hx-select` 가 `<html>/<head>/<body>` 를 포함한 전체 문서 응답에도 정상 동작함을 격리 테스트로 확인(로컬 static 서버 + Playwright, 3회 폴링 후 서버가 폴링 속성을 응답에서 뺐더니 클라이언트도 자동 정지).
- **케이스 상태 배지** — TAP 케이스 `status` 값(`pending`/`running`/`passed`/`failed`/`skipped`/`error`, `pilot_ta_llm_agent` 의 `CaseStatus` enum 과 동일 문자열)을 기존 `status-badge` 클래스(`status-pass`/`status-fail`/`status-running`/`status-na`)로 매핑. 실패/에러 케이스의 `raw_output_tail` 은 `<details>` 접이식 + 기존 콘솔/에러 로그와 동일한 다크 터미널 `<pre>` 스타일(`#1e1e1e` 배경, `#f08080` 텍스트 — 라이트/다크 테마 무관 고정 스타일, `error_log.html`/`console_output.html` 과 동일).
- **Actions 컬럼 권한 가드** — 실행/취소 버튼은 `user.can_write` 로 감싸 Viewer 에게는 노출되지 않는다(뷰 쪽 `RolePermissionMiddleware` POST 차단과 프론트 가드 일치).

### 6.5 Reliability Test 상세 — TAP(UI Automation) 스케줄 (`templates/robustness/`, 2026-07-28)

Reliability Test 상세(`test_detail.html`)가 TAP Suite 도 스케줄 대상으로 받게 확장. PRD `docs/prd_ui_test_automation.md`. 백엔드 분기(`RobustnessSchedule.tap_suite`/`is_tap`/`target_name`/`_stats()`)는 이미 되어 있어 프론트는 노출·표기만 담당.

- **"Test Suite 연동 추가" 폼 2탭** — `test_detail.html`. `/suites/` 의 `.admin-tabs`(서버 링크 방식, §6.4)와 달리 이 폼은 **한 `<form>` 안에서 select 만 바꿔치기**해야 해서 `templates/accounts/notification_settings.html` 의 `.event-tabs`/`.event-tab-btn`/`.event-tab-panel` JS 토글 패턴을 재사용(페이지 리로드 없음, 시각 언어는 `.admin-tabs`와 동일한 하단 underline). API 탭 패널엔 기존 `test_suite_id` select, UI 탭 패널엔 `tap_suite_id` select(옵션 라벨에 `{{ suite.case_count }} 케이스` 병기) — `interval_minutes` 입력 1개를 두 패널이 공유. 탭 전환 시 JS 가 `test_suite_id.required`/`tap_suite_id.required` 를 서로 바꿔줘야 한다 — `display:none` 인 필드에 `required` 가 남아있으면 브라우저가 보이지 않는 필드의 제약을 검증하며 제출을 막을 수 있어 반드시 토글 필요(`test_detail.html` 하단 인라인 스크립트, `#schedule-add-card` 스코프). 일괄 추가(`schedule_add_all`)는 Jenkins 전용이라 API 탭 패널에만 노출.
- **`_schedule_table.html` 행 분기** — Suite 명 컬럼은 `schedule.target_name`(공용 프로퍼티) + TAP 이면 `status-running` 배지로 "UI" 표시(Jenkins 행은 기존처럼 `suites:suite_detail` 링크, TAP 행은 TAP 상세 화면이 없어 링크 없이 텍스트만). 실행 수 단위 라벨은 `schedule.is_tap` 로 "잡"/"케이스" 분기(수치 자체는 `_job_aggregate`/`_tap_aggregate` 가 이미 잡→케이스로 의미를 맞춰뒀으므로 라벨만 바꾸면 됨). **"마지막 실행" 링크** — 신규 모델 프로퍼티 `RobustnessSchedule.latest_run`(Jenkins: 최근 `RobustnessRun.suite_run`, TAP: 최근 `TapRun`, `models.py`)을 링크 대상으로 써서 Jenkins 는 `suites:run_detail`, TAP 은 `suites:tap_run_detail` 로 분기(과거엔 텍스트만 있고 링크가 없었음 — 이번에 Jenkins 쪽도 함께 링크가 생김, 회귀 아님). 시작/중지/수동실행/초기화/삭제 버튼은 기존 URL 그대로 공용(백엔드가 `schedule.is_tap` 으로 내부 분기).
- **`test_detail_fragment` (HTMX 10초 폴링) select_related 보정** — 기존엔 `select_related("test_suite")` 만 있어 TAP 행 렌더 시 `tap_suite` 참조가 매 폴링마다 추가 쿼리를 냈다. `select_related("test_suite", "tap_suite")` 로 `test_detail` 뷰와 동일하게 맞춤(`apps/robustness/views.py`).
- **`schedule_runs.html` + `apps/robustness/views.py:schedule_runs`** — TAP 스케줄이면 `schedule.tap_runs.order_by("-started_at")[:100]` 를 별도 컨텍스트(`tap_runs`)로 넘겨 세션(TapRun) 단위 이력 표(세션 ID/상태 배지 — `suites/_tap_suite_table.html` 과 동일한 배지 분기 로직 재사용/케이스 진행/Pass·Fail/시작·종료/`suites:tap_run_detail` 상세 링크)를 렌더. Jenkins 스케줄 경로(`runs` 컨텍스트, 기존 표)는 무변경. `sweep_schedule(schedule)` 호출은 그대로 두되(내부에서 이미 `schedule.tap_suite_id` 로 분기해 Jenkins/TAP 폴링을 나눔) view 레벨에서 컨텍스트만 분기.
- **검증**: 실 DB 에는 TAP 스케줄이 없어(TapSuite 1건, 스케줄 미연동) 격리 sqlite 복사본 + 임시 settings 모듈로 별도 포트에 서버를 띄우고 TAP 스케줄/TapRun(pass/fail/error/running 각 1건)을 시딩해 playwright 헤드리스로 렌더 확인 — 탭 JS 토글(required 속성 전환 포함)·폼 제출(API/UI 각각)·마지막 실행 링크 네비게이션·TAP 실행 이력 표·라이트/다크 대비, 콘솔 에러 0건. 실 서버(8601, 실 DB)에서도 기존 Jenkins-only 스케줄 화면이 그대로 렌더됨을 재확인(회귀 없음).

---

## 7. 권한 / 인증

### `User` 모델 (`apps/accounts/models.py`)

`Role`: `qpe_admin`, `qpe_ops`, `tribe_lead`, `tribe_dev`, `approver`, `viewer`

- `is_admin` = `qpe_admin` or superuser
- `is_qpe` = `qpe_admin` or `qpe_ops`
- `can_write` = role ≠ `viewer`
- `is_viewer` = role == `viewer` (사이드바 메뉴 제한 + GET 페이지 차단 분기에 사용)

### `RolePermissionMiddleware` (`apps/accounts/middleware.py`)

`__call__()` (`apps/accounts/middleware.py:190`) 은 `request.path_info` 로 권한 경로를 검사한다. 따라서 `/qg` 같은 배포 접두 경로를 제외한 기존 앱 경로에 GET/POST 허용 규칙을 적용하며, 차단 시 기본 복귀 주소는 `reverse("dashboard:index")` 로 생성한다.

**① Viewer GET 페이지 제한 (2026-06-24)**: role==`viewer` 의 GET/HEAD 는 허용 영역 외 portal 페이지를 차단하고 `dashboard:index` 로 리다이렉트. 허용 = `_viewer_page_allowed(path)`:
- exact: `/`(Dashboard), `/health/`, `/sw.js`, favicon
- prefix: `/suites/runs/`(Suite Run 목록·상세·상태폴링), `/suites/job-runs/`(콘솔·에러로그), `/static/`·`/media/`, `/api/`(자체 인증), `/ci-api/`(API Key 자체 인증), `/accounts/login`·`/accounts/oidc/`(SSO 시작·콜백)·`/accounts/logout`
- → 즉 **Dashboard / Suite Runs 영역만** 열람 (Visualization 은 2026-10-21 부터 QPE Admin 전용이라 허용 목록에서 제거). Test Suites 설정(`/suites/`, `/suites/<pk>/`)·Queue·Reliability·Quality Gate·Activity Log·Admin 은 막힘.
- XHR(`X-Requested-With`)면 message 없이 조용히 리다이렉트(전역 알림 폴링 메시지 누적 방지). 사이드바도 `base.html` 에서 `{% if not user.is_viewer %}` 로 해당 메뉴 숨김 — **새 메뉴/페이지 추가 시 viewer 가 봐야 하면 `_VIEWER_GET_ALLOW_*` 와 사이드바 분기 양쪽을 갱신**.

**② 쓰기(POST/PUT/PATCH/DELETE)**:
- **`/api/v2/` · `/ci-api/` 로 시작하면 즉시 통과** — DRF 가 자체 권한 처리. (PRD §4.4; `/ci-api/` 는 2026-10-12 추가 — `middleware.py:211`)
- `POST_ALWAYS_ALLOW`: `/accounts/logout/`, `/accounts/api-keys/` — 모두 허용
- Admin: 전부 허용
- Viewer: 전부 차단
- 그 외 (operational role): `REGULAR_POST_ALLOW` 패턴 (`/run`, `/start/`, `/stop/`, `/trigger/`, `/status/`, `/rerun/`, `/deploy/`, `/reset/`, `/triage/`) 만 허용

새 operational POST 엔드포인트 추가 시 이 패턴 매칭에 걸리는지 확인 — 안 걸리면 일반 사용자가 못 쓴다.

### SSO 로그인 (Keycloak OIDC — `apps/accounts/sso.py`)

> **세 가지 진입 경로가 있다.** ① 앱 직접 OIDC(옛 `qualitygate.jkintra.net`, ALB 직결 — **2026-10 서버 이관으로 이 경로의 주소는 종료**, 코드/설정 지원만 남아 있고 현재 운영 진입은 ②③) — Keycloak client 의 id/secret 이 `.env` 에 있어야 한다. ② 게이트웨이 헤더 로그인(`qw.jkintra.net/qg`) — 게이트웨이가 Keycloak 로그인을 대신하므로 앱에 client 정보가 필요 없다(`apps/accounts/sso.py:login_by_gateway_header`, `middleware.py:GatewaySessionMiddleware`). ③ **게이트웨이 진입 로그인**(`GATEWAY_ENTRY_ENABLED`, 기본 꺼짐) — 앱 자체 로그인 화면을 첫 화면으로 두고 "Keycloak 로그인" 버튼이 보호된 진입 경로로 보낸다(아래 "게이트웨이 진입 모드"). 헤더(`X-Auth-Request-Email`, `X-Auth-Realm=world`)는 `TRUSTED_PROXY_CIDRS`(기본 `172.20.0.222/32` = 게이트웨이 Nginx 출발지, **2026-10-09 실측 확인** — 헤더 로그인 성공·제거 경고 0건, 이 호스트 `.env` 에도 명시 고정) 출발지 것만 신뢰하며, 그 이메일의 `User.email` 이 정확히 1명이면 그 계정(역할 유지), 없으면 즉시 활성 `viewer` 로 생성해 바로 로그인시킨다(비활성·모호는 403). 게이트웨이 뒤 로그아웃 기본값은 `GATEWAY_LOGOUT_URL=https://qw.jkintra.net/_qw/logout`. 게이트웨이 외 다른 출발지(ALB 등)가 위조한 `X-Auth-*` 는 제거된다.

`/accounts/login/` 은 **Keycloak 전용 로그인 페이지**다. 처음 접속(미로그인)하면 이 화면이 열리고, Keycloak 로 로그인한 뒤 로그아웃하면 앱 세션과 Keycloak 세션을 함께 끝내고 이 화면으로 돌아온다. 사람은 `(issuer, sub)` 로만 식별하고(`ExternalIdentity`, `apps/accounts/models.py:81`) 이메일은 최초 연결에만 쓴다. 공급자는 `internal`(사내 Keycloak) 하나, Authlib client(PKCE S256), 마이그레이션 `0003_externalidentity`(additive).

**인증 흐름**

```
[미로그인] GET 아무 페이지 ─ login_required ─▶ /accounts/login/?next=…  (sso.login_view)
   │  · 게이트웨이 헤더(X-Auth-Realm=world, 신뢰 출발지) 있고 ?loggedout 없으면 → 화면 없이 바로 SSO 시작
   │  · 아니면 Keycloak 전용 로그인 페이지 렌더 (SSO 버튼 / 진입 모드면 "Keycloak 로그인" 버튼 / 자체 로그인이 열려 있으면 폼도)
   ▼ 버튼
/accounts/oidc/internal/login/  (sso_login: next 를 세션 sso_next 에 저장 → Keycloak authorize 302)
   ▼ Keycloak 로그인(Google Workspace)
/accounts/oidc/internal/callback/  (sso_callback)
   ├ 코드 교환 + ID 토큰 검증 실패 / iss 불일치 / sub 없음 ─▶ 로그인 화면 직접 렌더(400, sso_failed) — 리다이렉트 안 함(무한루프 방지)
   ├ _resolve_with_retry → _resolve_user: ① 기존 연결 ② 이메일 신뢰 검사 ③ 이메일 1명 일치 → 연결 ④ 없으면 viewer 자동 생성
   └ login() → 세션에 id_token(sso_id_token)·공급자(sso_provider) 보관 → next(같은 호스트만) 로 302

[로그아웃] /accounts/logout/ (GET·POST, 사이드바 링크)  (sso.logout_view)  — 우선순위 (a')→(a)→(b)→(c)
   (a') 진입 모드(GATEWAY_ENTRY_ENABLED) + GATEWAY_LOGOUT_URL ──▶ 앱 세션 종료 후 HTML 페이지(_gateway_logout_page then=로그인): fetch 로 게이트웨이 로그아웃 호출 → /accounts/login/?loggedout=1
        (앱 경로는 게이트웨이 비보호라 헤더가 없어도 동작해야 하므로 behind_gateway 를 보지 않는다)
   (a) 게이트웨이 뒤(헤더 있음) + GATEWAY_LOGOUT_URL ──▶ 앱 세션 종료 후 HTML 페이지로 게이트웨이 로그아웃 URL 이동 (302 아님 — 아래 "게이트웨이 진입 모드")
   (b) id_token 보관 + post_logout_redirect=True ──▶ 앱 세션 종료 후 Keycloak end_session_endpoint 302
         (id_token_hint + post_logout_redirect_uri=로그인 페이지 절대 URL, 쿼리 없음) ─▶ Keycloak 세션 종료 ─▶ /accounts/login/
   (c) 그 외(플래그 꺼짐·비밀번호 로그인·메타데이터 조회 실패) ──▶ 앱 세션만 종료, /accounts/login/?loggedout=1
```

- `_resolve_user` 거부 조건: 이메일 없음/신뢰 불가, **2명 이상과 일치(모호)** 시 연결 안 하고 신규 viewer 생성, **이메일 1명 일치지만 그 계정에 같은 issuer 의 다른 sub 연결이 이미 있음**(이메일 재사용 의심) → 중복 계정 없이 400 `sso_failed`(경고 로그에는 공급자만), 비활성 계정은 활성화하지 않고 403 `sso_inactive`.
- (b) 의 `post_logout_redirect_uri` 에 `?loggedout=1` 을 붙이지 않는 이유: Keycloak 은 client 에 등록된 URI 와 **정확히 일치**해야 하기 때문. 그래서 Keycloak 에서 돌아온 로그인 화면에는 "로그아웃되었습니다." 알림이 안 뜰 수 있다(정상). 게이트웨이 헤더가 없으면 자동시작이 없어 루프가 없고, 헤더가 있는 요청은 (a) 가 처리한다.
- `id_token` 은 세션에만 보관한다 — 로그아웃의 `id_token_hint` 로만 사용하고 로그/응답/템플릿에 노출하지 않는다. `logout()` 이 세션을 비우므로 **logout 호출 전에 읽는다**. 로그에는 공급자·예외 타입명만 남긴다(토큰/claims/secret 금지).

**게이트웨이 진입 모드 (`GATEWAY_ENTRY_ENABLED`, 기본 `False`)** — 요구: QG 자체 로그인 페이지가 첫 화면이고 거기 "Keycloak 로그인" 버튼으로 로그인, 로그아웃하면 다시 그 페이지. 게이트웨이가 `/qg/` 전체를 보호하면 비로그인 요청이 앱에 오지 못해 앱 로그인 화면을 보여 줄 수 없으므로, 게이트웨이 쪽에서 `/qg/` 는 비보호(none)·**진입 경로 `/qg/accounts/gateway-login/` 하나만 internal 로 보호**해야 한다(QA 인프라팀 요청 — 게이트웨이 설정은 우리가 바꾸지 않는다. 문안·켜는 순서·롤백은 `docs/handoff_sso_integration.md` §10).

```
GET /accounts/login/ (비보호) ─ 앱 로그인 화면, "Keycloak 로그인" 버튼 (gateway_entry_url, 플래그 켜졌을 때만)
   ▼ 버튼
GET /accounts/gateway-login/?next=…  (게이트웨이가 보호 → Keycloak 로그인 → X-Auth-* 헤더를 붙여 백엔드로)
   │ ProxyBoundaryMiddleware(신뢰 출발지만 헤더 유지) → GatewaySessionMiddleware 가 헤더 이메일로 앱 세션을 이미 생성
   ▼ sso.gateway_entry_view
   ├ 인증됨 ─▶ _safe_next 로 302 (대시보드)
   ├ request.gateway_denied(비활성·모호) ─▶ 로그인 화면 직접 렌더 403
   └ 세션 없음(헤더 없음·라우팅 미설정·신뢰 밖 출발지) ─▶ 로그인 화면 직접 렌더 503 sso_unavailable + 경고 로그 (리다이렉트 안 함 = 루프 방지)
```

- **노출 면 축소(코드 리뷰 반영)**: 진입 모드에서 `GatewaySessionMiddleware`(`middleware.py`) 는 진입 경로 외 모든 경로의 `HTTP_X_AUTH_*` 를 버린다(게이트웨이 설정 실수 방어). 진입 경로는 `_VIEWER_GET_ALLOW_PREFIX` 에 있어 viewer 첫 로그인도 `next` 로 간다. 로그아웃은 `id_token` 이 있으면(직접 OIDC) 진입 모드여도 end-session 분기를 유지한다.
- 켜면 `LOCAL_LOGIN_ACTIVE = LOCAL_LOGIN_ENABLED or not (OIDC_PROVIDERS or GATEWAY_ENTRY_ENABLED)` 로 자체 로그인(아이디/비밀번호)이 기본 꺼진다 — 비상 시 `LOCAL_LOGIN_ENABLED=True` + `sudo systemctl restart qualitygate`. **게이트웨이 라우팅이 적용·검증되기 전에는 켜지 않는다**(안 켜진 라우팅에서 켜면 로그인 잠금).
- **로그아웃 302 → HTML 변경 사유(404 사고, 2026-10-09)**: 게이트웨이가 `/qg/` 하위 응답의 `Location` 을 고쳐 쓰는 것으로 **추정**(미확인)된다 — `https://qw.jkintra.net/_qw/logout` 302 가 `/qg/_qw/logout` 으로 바뀌어 앱에 404 가 로그로 관측됐다. `_gateway_logout_page` 는 `Location` 헤더 없이 HTML(meta refresh + JS + 링크)로 이동시킨다. 진입 모드는 fetch(`credentials:'include'`, `redirect:'manual'`)로 게이트웨이 로그아웃을 호출해 **게이트웨이 세션 쿠키를 지우고** `then`(로그인 화면)으로 복귀한다(3초 타임아웃, no-JS 는 4초 meta refresh). Keycloak 세션 종료 여부는 게이트웨이의 `SSO_*_ISSUER` 설정에 달려 있어 **미확인** — 보장하지 않는다. 게이트웨이 `/_qw/logout` 은 익명으로 `/` 로 302(실측).
- 실측(2026-10-09): 게이트웨이는 `/qg/*` 전체를 보호(비로그인 `/qg/`·`/qg/accounts/login/`·`/qg/health/` 모두 401, 브라우저 화면 이동은 게이트웨이 `/_qw/login` 선택 화면) — 진입 모드가 동작하려면 위 라우팅 변경이 필요하다. 이 호스트(wx-qms221, `172.20.0.221`)에는 게이트웨이 nginx 가 없다(게이트웨이는 별도 2번 서버 wx-qms222).

**`apps/accounts/sso.py` 주요 위치**

| 위치 | 역할 |
|---|---|
| `enabled_providers` (`:75`) | 로그인 화면에 표시할 공급자 목록 `[{key,label,url}]` (`PROVIDER_LABELS` `:54`) |
| `gateway_realm` / `gateway_provider` / `behind_gateway` (`:88` / `:93` / `:99`) | 게이트웨이 헤더 해석. `GATEWAY_REALM_PROVIDERS`(`:58`)는 `world → internal` 만(ext 는 QG 대상 아님) |
| `_client` (`:104`) | Authlib client 프로세스 캐시, `code_challenge_method=S256`, `default_timeout=5` |
| `_safe_next` (`:128`) | open redirect 방지 — 같은 호스트의 URL 만 허용 |
| `_render_login` (`:143`) | 로그인 템플릿 컨텍스트 계약 `form/next/providers/local_login/error/logged_out/gateway_entry_url` (`logged_out = "loggedout" in request.GET`, `gateway_entry_url` 은 `GATEWAY_ENTRY_ENABLED` 일 때만 `accounts:gateway_login` 주소, 아니면 빈 문자열) |
| `gateway_entry_view` (`:167`) | 진입 모드 버튼 목적지(`accounts:gateway_login`, `urls.py`). 인증됨→`_safe_next`, 거부→403, 세션 없음→503 `sso_unavailable`(직접 렌더). `@never_cache @require_GET` |
| `login_view` (`:185`) | `@sensitive_post_parameters()` + `@never_cache`. POST 는 자체 로그인이 닫혀 있으면 403 `local_disabled`. 게이트웨이 자동시작 분기(`:210`) |
| `_gateway_logout_page` (`:218`) | 게이트웨이 로그아웃 이동을 **302 대신 HTML**(meta refresh + JS + 링크)로. `then` 이 있으면 fetch 로 호출 후 `then` 복귀. URL 은 `escape`/`json.dumps` 로 이스케이프 |
| `logout_view` (`:255`) | 위 (a')(a)(b)(c) 우선순위. `GET`/`POST` 모두 허용 |
| `_absolute_url` (`:300`) | Keycloak 등록 주소와 맞추기 위한 절대 URL — localhost 외에는 `https` 강제(ALB 뒤 Django 는 http 로 볼 수 있음). 콜백·로그아웃 복귀 URI 공통 |
| `sso_login` / `sso_callback` (`:314` / `:334`) | 시작·콜백. 콜백 성공 시 `login()` **이후** `sso_id_token` 보관(`:363`) — login() 이 세션 키를 순환하므로 반드시 뒤에 |
| `_resolve_with_retry` / `_resolve_user` (`:425` / `:438`) | 동시 첫 로그인 경합·DB 잠금은 1회 재시도. 연결·생성 규칙 본체 |
| `_email_trusted` (`:502`) | `OIDC_INTERNAL_EMAIL_DOMAINS` 이거나 `email_verified is True` |

**미들웨어 3종 (`apps/accounts/middleware.py`, 순서는 `config/settings/base.py:73`·`:75`·`:83`)**

| 미들웨어 | 위치 | 역할 |
|---|---|---|
| `ProxyBoundaryMiddleware` | `middleware.py:71`, **맨 앞** | `REMOTE_ADDR` 가 `TRUSTED_PROXY_CIDRS` 안일 때만 `X-Auth-*`(`HTTP_X_AUTH_*`) 헤더를 남기고 그 밖에서는 전부 삭제 → 게이트웨이를 거치지 않은 요청이 헤더로 다른 사람 행세·SSO 자동시작 유도를 못 한다. IPv4-mapped IPv6(`::ffff:a.b.c.d`)는 IPv4 로 정규화해 비교. CIDR 이 비었거나 출발지를 해석할 수 없으면 항상 삭제. 요청 시점에 settings 를 읽는다 |
| `CiApiBoundaryMiddleware` | `middleware.py:117`(`CI_API_PREFIX` `:114`), `ProxyBoundaryMiddleware` 바로 뒤·`SessionMiddleware` **앞** | (2026-10-12) `path_info` 가 `/ci-api/` 로 시작하면 `HTTP_COOKIE` 와 `HTTP_X_AUTH_*` 를 제거하고 `request.urlconf = "config.urls_ci"`, `request.is_ci_api = True` 지정. 이 경로는 게이트웨이(nginx)가 로그인 검사를 하지 않으므로 ① 세션 쿠키로 로그인한 사용자 행세(CSRF 우회 포함), ② 클라이언트가 보낸 `X-Auth-*` 로 `GatewaySessionMiddleware` 가 자동 로그인시키는 것을 막는다 — **반드시 `SessionMiddleware` 가 쿠키를 읽기 전에** 실행. `/api/v2/` 에는 영향 없음 |
| `GatewaySessionMiddleware` | `middleware.py:139`, `AuthenticationMiddleware` 뒤 | 게이트웨이가 확인한 이메일(`X-Auth-Request-Email`)과 앱 세션 사용자의 이메일(`User.email` 또는 `ExternalIdentity.email`)이 다르면 앱 세션 폐기(공용 PC 에서 A 의 앱 쿠키가 남은 채 B 가 게이트웨이 로그인한 경우). 헤더가 없으면(게이트웨이 밖) 아무것도 안 함. **제약: 자체 로그인(`LOCAL_LOGIN_ENABLED=True`)을 게이트웨이 경유로 함께 쓰면, 이메일이 게이트웨이 사용자와 다른 비밀번호 계정의 세션이 매 요청 폐기된다** — 자체 로그인은 게이트웨이를 거치지 않는 직접 접속(예: `qualitygate.jkintra.net`)에서만 쓴다 |

**설정 플래그 (`config/settings/base.py:270`~`:320`)**

| 설정 / env | 기본 | 의미 |
|---|---|---|
| `OIDC_PROVIDERS["internal"]` ← `OIDC_INTERNAL_ISSUER`/`_CLIENT_ID`/`_CLIENT_SECRET` | 없음(SSO 꺼짐) | 셋 모두 있을 때만 켠다. 일부만 있으면 `ImproperlyConfigured` 로 기동 실패, issuer 는 `https://`(localhost 제외) |
| `OIDC_PROVIDERS["internal"]["post_logout_redirect"]` ← `OIDC_INTERNAL_POST_LOGOUT_REDIRECT` | `False` | `True` 면 로그아웃 (b) 사용. Keycloak client 에 post logout redirect URI 등록 후에만 켠다(미등록이면 Keycloak 오류 화면) |
| `OIDC_INTERNAL_EMAIL_DOMAINS` | `worxphere.ai` | `email_verified` 없이 신뢰할 도메인(콤마). 그 외는 `email_verified=true` 일 때만 |
| `GATEWAY_ENTRY_ENABLED` | `False` | 게이트웨이 진입 모드(`base.py:312`). 로그인 화면에 "Keycloak 로그인" 버튼 + 로그아웃 (a'). 게이트웨이에 진입 경로 보호 라우팅이 적용·검증된 뒤에만 켠다 |
| `LOCAL_LOGIN_ENABLED` / `LOCAL_LOGIN_ACTIVE` | `False` / 파생 | `LOCAL_LOGIN_ACTIVE = LOCAL_LOGIN_ENABLED or not (OIDC_PROVIDERS or GATEWAY_ENTRY_ENABLED)`(`base.py:313`) 가 실제 적용값 — OIDC 값도 진입 플래그도 없으면 자체 로그인 유지(코드 먼저·OIDC 값은 나중 배포). 진입 모드의 비상 복구는 `LOCAL_LOGIN_ENABLED=True`. 닫히면 POST 403, `signup` 404, `/admin/login/`→앱 로그인(`config/urls.py:51`), admin 비밀번호 변경 404(`config/urls.py:63`) |
| `TRUSTED_PROXY_CIDRS` | `172.20.0.222/32` (`base.py:317`) | 게이트웨이 출발지 CIDR(콤마, 실측). 비어 있으면 `X-Auth-*` 항상 제거 — **`.env` 에 `TRUSTED_PROXY_CIDRS=` 처럼 빈 값 키가 있으면 `os.getenv` 가 빈 문자열을 돌려줘 기본값이 가려진다**(`.env.example` 도 이 키는 주석 처리) |
| `GATEWAY_LOGOUT_URL` | `https://qw.jkintra.net/_qw/logout` (`base.py:320`) | 게이트웨이 로그아웃 (a')/(a) 이동 주소. 빈 값 키가 있으면 기본값이 가려져 (b)(c) 로 폴백 |

로그인/로그아웃 이벤트는 `apps/accounts/signals.py` 의 `user_logged_in`/`user_logged_out` 가 ActivityLog 로 남긴다(SSO 연결·자동 생성은 `ActivityLog` CREATE 로 별도 기록). 템플릿은 `templates/accounts/login.html`(base.html 을 상속하지 않고 `style.css` 를 직접 로드 — **style.css 를 고치면 `base.html` 과 `login.html` 의 `?v=` 토큰을 함께 bump**). 회귀 테스트: `apps/accounts/test_sso.py`(로그인 화면·게이트웨이 자동시작·프록시 경계·세션 불일치·로그아웃 (a')(a)(b)(c)·콜백·사용자 결정·설정 파싱·`GatewayEntryTests`/`GatewayEntrySettingsTests`/`GatewayLogoutPageTests`), `config/test_subpath.py`.

**DRF Basic 인증 잔존 경로 (보안 메모)**: `REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]`(`base.py:154`)에 `SessionAuthentication` 외 `BasicAuthentication` 이 남아 있어, 자체 로그인을 닫아도 **비밀번호가 있는 기존 계정은 `/api/v1/`·`/api/v2/` 에서 Basic(아이디:비밀번호)으로 인증될 수 있다**(`test_api_key_and_basic_auth_unaffected_when_local_login_closed` 가 현 동작을 고정). SSO 로 만든 계정은 비밀번호가 없어 해당 없음. SSO 전환의 "비밀번호 로그인 차단" 목표와 어긋나므로, 후속으로 Basic 제거(외부 호출자가 API Key 로 이전했는지 확인 후)를 검토한다. **이것과 별개로** `ApiKeyAuthentication` 이 받는 Basic(`qgp_` 키를 비밀번호/사용자명 자리에 넣는 방식, 아래 "`/api/v2/` 외부 API 인증")은 API Key 인증이며 아이디/비밀번호 Basic 이 아니다 — `/ci-api/` 에서는 DRF 기본 `BasicAuthentication` 이 아니라 이 키 Basic 만 통한다.

**공용 PC 로그아웃 동작**: (c) 는 앱 세션만 끝내므로 Keycloak 세션이 남아 다음 사람이 SSO 버튼을 누르면 **비밀번호 없이 이전 사용자로 로그인**된다. 공용 PC 에서 안전하려면 (b)(`OIDC_INTERNAL_POST_LOGOUT_REDIRECT=True` + client 에 post logout redirect URI 등록) 또는 (a)/(a')(게이트웨이 로그아웃) 이 켜져 있어야 한다(게이트웨이가 Keycloak 세션까지 끊는지는 `SSO_*_ISSUER` 설정 의존 — 미확인). 비밀번호로 로그인했거나(`id_token` 없음) 메타데이터 조회가 실패한 세션은 (c) 로 폴백한다.

**확인 필요(사내 Keycloak realm)**: 첫 로그인 계정 자동 연결/생성은 Keycloak 의 이메일을 믿는다 — 사내 realm 에서 사용자가 자기 이메일을 편집할 수 없도록(Account console 의 이메일 편집 비활성) 설정돼 있는지 운영 전에 확인한다. 편집 가능하면 `OIDC_INTERNAL_EMAIL_DOMAINS` 신뢰 규칙만으로는 타인 이메일로의 계정 탈취를 막지 못한다.

### `/api/v2/` 외부 API 인증 (`apps/api_v2/`)

세션 의존 포털 UI 와 별개로 외부 시스템 (Jenkins / GitHub Actions / 백오피스 등) 이 호출할 수 있도록 `/api/v2/` 아래에 DRF 기반 REST API 를 둔다. PRD: `prd_api.md`.

| 파일 | 역할 |
|---|---|
| `apps/api_v2/models.py` | `ApiKey` (UUID PK, `key_prefix`, `key_hash` (SHA-256), `scopes`, `expires_at`, `revoked_at`, `last_used_at`). `ApiKey.issue(user, name, scopes, expires_in_days=90)` 로 발급 — 평문은 응답에 1회만 노출되고 DB 는 해시만 보관. `find_by_plaintext(...)` 가 `qgp_{prefix}_{secret}` 형식을 파싱해 constant-time 비교 후 매칭. |
| `apps/api_v2/scopes.py` | 스코프 상수 (`meta:read`, `runs:read`, `runs:trigger`, `runs:control`, `export:read`, `owners:manage`, `*`). `has_scope(scopes, required)` 헬퍼. `*` 는 admin 전용이며 **`EXPLICIT_ONLY_SCOPES`(= `export:read`, `owners:manage`) 는 포함하지 않는다** — 키에 명시돼 있어야 통과. `ALL_SCOPES` 에도 넣지 않아 `/me/` available_scopes 로 일반 키 사용자에게 노출되지 않는다. |
| `apps/api_v2/authentication.py` | `ApiKeyAuthentication` (`Authorization: Bearer qgp_...`, **2026-10-12 Basic 추가** — `_key_from_basic`(`authentication.py:50`) 가 base64 `user:password` 에서 비밀번호(없으면 사용자명) 자리의 `qgp_` 키만 꺼낸다(`curl -u x:$KEY`, Jenkins). `qgp_` 로 시작하지 않는 Basic(아이디/비밀번호)은 이 인증기가 인증하지 않고 `None` 을 돌려줘 DRF 다음 인증기로 넘어간다. `/api/v2/` 와 `/ci-api/` 에 동일 적용). 만료/회수/비활성 사용자 분기 모두 `AuthenticationFailed` 로 코드 (`api_key_expired`/`api_key_revoked`/`api_key_invalid`/`user_inactive`). |
| `apps/api_v2/permissions.py` | `HasScope` (view 의 `required_scope` 또는 `required_scopes` dict), `CanWriteForControl` (Viewer 거부), `AdminOnlyForWildcard` (안전망), `OwnersManagePermission` (2026-10-21 — 담당자 관리 API 전용, `ExportPermission` 과 같은 규칙: API Key + `owners:manage` 명시 스코프 + 요청 시점 소유자 `is_admin`), `ExportPermission` (`permissions.py` — 내보내기 API 전용: API Key 필수 + `export:read` 명시 스코프 + **요청 시점에** 소유자 `is_admin` 재확인, 아니면 `scope_required`/`role_denied` 403). 세션 인증으로 들어오면 일반 스코프 검사는 skip(내보내기 API 는 세션 불가). |
| `apps/api_v2/throttling.py` | `ReadThrottle` (120/min), `WriteThrottle` (30/min). 키 단위 식별 (`request.auth.pk`). 세션 인증은 throttle skip. |
| `apps/api_v2/exceptions.py` | DRF exception_handler 오버라이드. 모든 응답을 `{"error": {"code": "...", "message": "..."}}` 형태로 정규화 + 코드 카탈로그 매핑. `EXCEPTION_HANDLER` 로 `REST_FRAMEWORK` 에 등록. |
| `apps/api_v2/urls.py` | `/api/v2/health/` (인증 불요), `/api/v2/me/` (현재 키 + 사용자 정보 + 스코프 목록) — Phase A 의 최소 엔드포인트. 후속 Phase 에서 메타/Run/Suite 라우팅 추가. **2026-10-12**: health·me + `urls_meta/runs/controls/suites` include 를 공유 리스트 `ci_api_patterns`(`urls.py:22`)로 분리하고, `urlpatterns`(`:36`)는 문서(schema/docs/redoc) + `*ci_api_patterns` + `urls_export` 로 구성. |
| `apps/api_v2/urls_ci.py` · `config/urls_ci.py` | **`/ci-api/` 전용 라우팅 (2026-10-12)**. `apps/api_v2/urls_ci.py` 는 `ci_api_patterns` 만 노출(`app_name="api_v2"` 동일 → 뷰/`reverse` 이름 공유). `config/urls_ci.py` 는 기본 `config.urls` 패턴에서 `api/v2/` 마운트만 `ci-api/` 로 교체한 URLconf — `CiApiBoundaryMiddleware` 가 `request.urlconf` 로 지정하므로 `/ci-api/` 요청의 `reverse("api_v2:…")`(`self_url`·`console_url` 등)가 `/ci-api/...` 로 나온다. **문서(schema/docs/redoc)·export 는 `urls.py` 의 `urlpatterns` 에만 있고 ci-api 에는 없다(404)** — 로그인 없이 열리는 경로에 둘 수 없음. |
| `apps/api_v2/views/health.py` | health, me 핸들러. |
| `apps/api_v2/views_export.py` · `urls_export.py` | **서버 이관용 DB 스냅샷 내보내기 API** (`/api/v2/export/…`, 아래 "DB 스냅샷 내보내기 API" 및 §8 "서버 이관"). `urls.py` 가 `urls_export` 를 include. 뷰는 `@extend_schema(exclude=True)` 로 Swagger 에 노출하지 않는다. |
| `apps/api_v2/views/owners.py` · `urls_owners.py` · `serializers/owners.py` | **담당자 관리 API (2026-10-21)** — `ApiEndpointOwner` CRUD (`OwnerCollectionView` 목록·등록 / `OwnerDetailView` 단건·PATCH·PUT·DELETE). UI `integrations/views.py:api_owner_create/edit/delete` 와 같은 부수효과(`is_imported=False`, `invalidate_match_cache()`, `ActivityLog` `API_OWNER_*` — detail 에 `via=api key=<prefix>`, 등록 시 `writeback_owner_to_sheet_async`). `(method,endpoint)` 중복은 `DuplicateOwner`(code `already_exists`, 409, `details.existing_id`). `urls.py` 의 `ci_api_patterns` 에 포함돼 `/ci-api/owners/` 에서도 동작. 키 발급 폼 옵션은 `accounts/views.py:_scope_options_for`(admin 전용). 테스트 `apps/api_v2/tests/test_owners.py`. |
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
- `trigger_suite_run(*, suite, user, job_id="", run_all=False)` → `TestSuiteRun`. **3가지 분기**:
  - `job_id` 지정 → `_trigger_single_job_run` (Job 1개, `total_jobs=1`, 하위호환).
  - `job_id` 생략 + `run_all=False` (**기본**) → `_trigger_single_idle_run` — 활성 대상 Job(`is_active & is_target`, `order` 순) 중 **idle worker 1개만** 선택해 실행. idle 선택은 lazy import 로 `views.pick_idle_job(suite, jobs)` 를 호출. 검증된 idle worker 가 없으면 `SuiteRunError("all_workers_busy", 409)` — Run 을 생성하지 않는다.
  - `job_id` 생략 + `run_all=True` → `_trigger_chain_run` — 활성 대상 Job 전체에 PENDING job_run 을 `bulk_create` 하고 `_try_trigger_next` 1회 호출 → 한 번에 1개씩 순차 트리거(포털 UI 체인과 동일).
  - 공통: 진행 중 RUNNING Run 있으면 `SuiteRunError("run_already_active", 409, details=suite_run_id)`, 활성 대상 Job 0건이면 `SuiteRunError("no_active_jobs", 409)`.
  - (구) `job_id` 빈값 → `job_required` 400 은 **폐기**. (구) 생략/빈값 → 전체 순차 체인 동작도 **`run_all=True` 명시 플래그로 이전** — `job_id` 생략 기본은 이제 idle 1개 선택.
- `cancel_suite_run(*, suite_run, user)` → `TestSuiteRun` (이미 COMPLETED 면 `SuiteRunError("not_actionable")`)

`_try_trigger_next`, `_abort_suite_run`, `_poll_run_status` 는 views.py 에 그대로 — robustness 등이 import 하므로 호환 보존.

**idle 선택 헬퍼** `pick_idle_job(suite, jobs)` (`apps/suites/views.py:633`):
- 대상 jobs 를 `order` 순으로 순회하며 Jenkins `lastBuild` 를 조회.
- `building=False` (또는 미빌드 404) → idle, 즉시 반환.
- 연결 오류 / 인증 실패 / 5xx 등 상태 확인 불가 → 건너뜀 ("검증되지 않은 worker 는 실행하지 않는다").
- 전부 건너뛰어 후보 없음 → `None` 반환 → 호출측에서 `all_workers_busy` 409 처리.

**Pass/Fail 결과 노출** (`apps/api_v2/phase.py`): `result_for_suite_run(status, passed_jobs, failed_jobs)` → `"passed"`/`"failed"`/`None`(종료 전), `result_for_job_run(status)` → success=passed / failure·aborted=failed / 그 외 None. `suite-runs/<id>/status/` 응답에 `result`, 단건/목록 직렬화(`TestSuiteRunSummarySerializer`)에 `result`·`is_terminal`, `job_runs[].result`(`TestSuiteJobRunSerializer`) 로 포함. 외부 폴링이 `result != null` 로 종료·합불 판정. **의도적 예외**: 2026-07-14 "ABORTED(중단)는 장애가 아니다" 정책(§3 `annotate_triage`/`untriaged_pending`, 대시보드 결과 분포, Reliability `_job_aggregate`)은 이 파일에 적용하지 않는다 — `raw failed_jobs`/`ABORTED=failed` 그대로 유지가 외부 API 계약이라 손대지 않았다(변경 시 기존 외부 연동 깨짐).

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
| `/api/v2/export/snapshots/` | POST | `export:read` (명시·admin) | DB 스냅샷 생성 시작 → 202 `{id,status,created_at}`. body `{"compress":"gzip\|zstd\|auto"}` 외 필드는 400 `unsupported`. 실행 중이면 409 `export_in_progress` |
| `/api/v2/export/snapshots/<id>/` | GET | `export:read` | 상태/진행률/manifest/files (`ready` 일 때만 manifest·files). `expired` 면 파일 없음 |
| `/api/v2/export/snapshots/<id>/download/<name>/` | GET | `export:read` | 파일 스트리밍 (Range 206 / `X-Content-SHA256` / ETag). `ready` 아니면 409 `export_not_ready`, 허용 이름 외 404, 잘못된 Range 416 |
| `/api/v2/export/snapshots/<id>/` | DELETE | `export:read` | 번들 삭제 → 204 (실행 중이면 409) |

**라우팅 모듈 분리**: `apps/api_v2/urls.py` 가 `urls_meta.py` / `urls_runs.py` / `urls_controls.py` / `urls_suites.py` / `urls_export.py` 를 include — 각 Phase 가 독립 파일에서 자기 라우팅을 정의해 통합 충돌을 피한다. 라우팅 추가 시 적절한 모듈을 골라 수정. 위 표의 모든 `/api/v2/…` 키 인증 엔드포인트(export 제외)는 `/ci-api/…` 로도 동일하게 호출된다(같은 뷰).

**`/ci-api/` 게이트웨이 로그인 예외 경로 (2026-10-12)**: 게이트웨이(nginx)가 SSO 쿠키 없는 요청을 전부 401 HTML 로 막아 `Authorization: Bearer` 호출이 앱에 닿지 못해, 외부 주소 `https://qw.jkintra.net/qg/ci-api/`(앱 내부 `/ci-api/`)에 한해 nginx 가 로그인 예외로 둔다(인프라 완료). 앱 쪽은 ① `CiApiBoundaryMiddleware` 가 쿠키·`X-Auth-*` 제거 + `urlconf` 교체, ② `RolePermissionMiddleware` 가 `/ci-api/` 를 `/api/v2/` 와 같이 POST 우회·viewer GET 허용, ③ `ApiKeyAuthentication` 의 Bearer/Basic(키) 로만 인증. **게이트웨이 인증이 없으므로 앱의 API Key 인증·scope·Rate limit 이 유일한 방어선**이다. nginx 예외는 `/qg/ci-api/` 만(문서/UI/export 는 보호 유지). 장애 구분: **nginx HTML 401 = 게이트웨이 예외 미적용**, **JSON `{"error":{"code":"not_authenticated",...}}` 401 = 앱까지 도달**. 회귀 테스트 `apps/api_v2/tests/test_ci_api.py`(13개: 키 인증·Basic·세션 쿠키 무시·`X-Auth-*` 무시·reverse 접두·export/docs 404).

**제어 API 의 응답 형식**: 모든 control 류는 액션 후 Run 의 `QualityGateRunDetailSerializer` 본문을 200(또는 201/202)으로 돌려준다 — 외부 자동화가 동일 응답으로 후속 폴링을 바로 시작할 수 있다.

**`?include` 파싱** (메타 API): `latest_run` / `test_suites` 콤마 토큰. 미지정 시 해당 필드는 응답에서 빠짐.

**도메인 예외 → 에러 응답**: `apps.qualitygates.services.GateRunError` / `apps.suites.services.SuiteRunError` 의 `code` / `http_status` / `details` 가 `apps/api_v2/exceptions.py` 의 일반화된 핸들러에서 `{"error":{"code","message","details"}}` 로 변환됨.

**폴링 힌트 헤더 `X-QGP-Poll-After`**: `/api/v2/runs/<id>/` / `/api/v2/runs/<id>/status/` / `/api/v2/suite-runs/<id>/` / `/api/v2/suite-runs/<id>/status/` 응답에 포함. terminal 도달 시 `0` (stop), 그 외 `5` 초. 외부 자동화는 이 값을 보고 다음 폴링 간격 결정.

**phase 매핑** (`apps/api_v2/phase.py`): DB 의 세분 status (QG 14개 / Suite 3개) 를 외부 친화적 5~6단 phase (`queued` / `running` / `awaiting_input` / `succeeded` / `failed` / `cancelled`) 로 변환. 모든 Summary/Detail/Status 응답에 `phase` 필드로 함께 노출 — CI/CD 자동화는 `phase` 만 보면 충분.

**외부 호출자용 가이드** (단일 문서):
- `docs/api_guide.md` — 키 발급 + 시나리오 curl 예제 + 전체 엔드포인트 reference + 상태머신 + 응답 스키마 + 에러 카탈로그 + Jenkins/GitHub Actions/Python 호출 예제 + FAQ. (구 `api_v2_quickstart.md` 는 본 문서로 통합·삭제됨.)

| HTTP | code | 의미 |
|---|---|---|
| 400 | `invalid_parameters` | body 파라미터 형식 오류 (예: job_id 가 해당 suite Job 아님) |
| 401 | `not_authenticated` | 인증 필요 |
| 401 | `api_key_invalid`/`api_key_revoked`/`api_key_expired` | 키 자체 문제 |
| 403 | `scope_required` | 스코프 부족 |
| 403 | `role_denied` | Viewer 가 write 시도 (내보내기 API 는 키 소유자가 admin 이 아닐 때) |
| 404 | `not_found` | 리소스 없음 |
| 409 | `run_already_active` | 이미 진행 중인 Run 있음 (Suite 체인 트리거 시 details.suite_run_id 포함) |
| 409 | `all_workers_busy` | Suite idle 트리거(기본)에서 검증된 idle worker 가 없음 — Run 미생성, 잠시 후 재시도 |
| 409 | `no_active_jobs` | Suite 트리거인데 활성 대상 Job 없음 |
| 409 | `run_not_actionable` | 현재 상태에서 액션 불가 (details.current_status 포함) |
| 400 | `unsupported` | (내보내기) 허용 필드 `compress` 외 — config/.env/secrets 요청 포함 |
| 409 | `export_in_progress` / `export_not_ready` | (내보내기) 이미 실행 중(생성)·실행 중 삭제 불가 / `ready` 아닌 스냅샷 다운로드 |
| 416 | `range_not_satisfiable` | (내보내기) 잘못된 Range |
| 503 | `export_unavailable` | (내보내기) 내보낼 DB 파일 없음 |
| 429 | `rate_limited` | Retry-After 헤더 포함 |
| 500 | `internal_error` | 미분류 예외 |

### DB 스냅샷 내보내기 API (`apps/api_v2/views_export.py`, 2026-10-09)

ssh/rsync 가 막히고 HTTPS 만 열린 환경에서 원본 DB 를 새 서버로 옮기기 위한 API(PRD 경로 C — `docs/prd_server_migration_sync.md` §5.5·§6.3, 요구사항·결정 `docs/api_requirements_data_export.md`, 호출자 가이드 `docs/api_guide.md` §4.5·§7.1a).

- **권한**: `_ExportBase`(`views_export.py:496`) — `ApiKeyAuthentication` 만(세션 불가) + `ExportPermission`. `export:read` 는 `*` 가 포함하지 않는 명시 전용 스코프이고 키 소유자 admin 이어야 한다(요청마다 재확인 → 발급 후 강등도 차단). 발급 폼 `apps/accounts/views.py:_scope_options_for`(`:241`)에서 admin 에게만 체크박스 노출(`export:read` 는 `admin_only=True` — 비admin 이 POST 로 강제 전송해도 `allowed` 필터에서 걸러져 키가 발급되지 않는다). Throttle: 생성·삭제 Write(30/min), 상태 Read(120/min), **다운로드는 없음**(Range 재개가 한도에 걸리지 않게).
- **생성** (`SnapshotCreateView`, `:504`): 본문은 `compress` 만 허용(그 외 400 `unsupported` — config 는 구조적으로 내보낼 수 없다). `_start_export`(`:322`)가 `_sweep` 후 `_running_id` 로 동시 1개를 강제(409), `<BASE_DIR>/snapshots/api-<uuid4>/`(0700)에 `state.json`(0600) 기록 후 데몬 스레드 `_run_export`(`:259`) 시작 → 202. 작업은 `api-<id>/work/` 에서 `snapshot.export_snapshot(include_config=False)` 로 수행하고(비어 있지 않은 out_dir 거부 규칙 때문) 완료 시 DB 파일 → `manifest.json` 순으로 번들 루트로 `os.replace`(**manifest 존재 = 번들 완성**) 후 `state.status=ready`. 진행률은 `export_snapshot` 의 `log`/`progress` 콜백(`[1/4]`…, backup %)을 `_STAGE_PCT`/`_PCT_RE` 로 0~100 에 매핑해 `state.json` 에 기록.
- **상태 파일** `state.json`: `id,status(running|ready|failed|expired),progress_pct,created_at,finished_at,error,compress,pid,heartbeat`. **DB 모델·마이그레이션 없음**(`apps/api_v2/tests_export.py:MigrationSafetyTests` 가 `makemigrations --check` 변경 없음을 검증). 경로는 설정 `SYNCOPS_EXPORT_DIR`/`SYNCOPS_EXPORT_DB_PATH`(`config/settings/base.py:181`, 기본 None → `BASE_DIR/snapshots`·운영 DB; 테스트가 임시 경로로 오버라이드).
- **정리** (`_reconcile` `:183` / `_sweep` `:219`, 생성·조회·다운로드 때마다 호출): `running` 인데 작업 스레드가 없으면(`_is_stale_running` `:152` — 같은 pid 의 고아·죽은 pid·하트비트 `STALE_HEARTBEAT_SECONDS=300` 초과) `failed` 로 전환, `ready|failed` 가 `TTL_SECONDS`(24h, `:53`) 지나면 파일을 지우고 `expired` 표식만 남기며 다시 `TOMBSTONE_SECONDS`(24h) 뒤 디렉터리째 제거. `state.json` 없는 잔재 디렉터리는 제거. `api-<uuid>` 형식이 아닌 디렉터리는 건드리지 않는다.
- **다운로드** (`SnapshotDownloadView`, `:565`): `name` 은 고정 허용 목록 `ALLOWED_DOWNLOAD_NAMES`(`:61`: `manifest.json`·`db.sqlite3.zst|.gz`·`db.sqlite3`)와만 비교(경로 구분자/`..` 는 여기서 탈락), id 는 `_parse_id`(`:94`)가 uuid 정규형만 허용, 심볼릭 링크는 404. `ready` 아니면 409. `_parse_range`(`:450`)는 단일 구간(`a-b`, `a-`, `-n`)만 허용, 나머지는 416 + `Content-Range: bytes */size`. `_iter_file` 이 1MiB 청크로 스트리밍(메모리에 전체를 올리지 않음). 헤더: `Content-Length`/`Accept-Ranges`/`ETag`/`X-Content-SHA256`(DB 파일은 manifest `db.file_sha256`, manifest 는 파일 sha256)/`Cache-Control: private, no-store`. Accept 헤더와 무관하게 JSON 렌더러 선택(`_IgnoreClientContentNegotiation`, octet-stream 요청 시 406 방지).
- **감사** (`_audit` `:421`): logger 항상 + 기존 `ActivityLog` 재사용(새 enum 값 없음; 생성=`create`, 삭제=`delete`, 다운로드=`run`). `start==0` 다운로드는 매번, Range 재개 요청은 (key, 스냅샷, 파일)당 `DOWNLOAD_AUDIT_TTL`(3600s, `:418`) 안에 최초 1회만 DB 기록(캐시 키 `export-dl-audit:…`). 키 평문은 어디에도 남기지 않고 소유자 username·`key_prefix` 만.
- **새 서버 쪽**: `apps/syncops/management/commands/fetch_snapshot_via_api.py` — 아래 §8 "서버 이관".
- **원본 쓰기 간섭 회피**: 운영 DB 가 커서 API 내부 백업이 쓰기 때문에 재시작되면 `export_snapshot --api-bundle`(원본 셸, 서비스 정지 상태)로 같은 `snapshots/api-<uuid>/` 형식(`state.json` status=ready, config 제외)을 직접 만든다 — 이후 API 가 `--existing-id` 요청에 그대로 내려준다(`export_snapshot.py` `--api-bundle`, 상태 파일 쓰기는 `views_export._write_state` 재사용).
- **테스트**: 서버 `apps/api_v2/tests_export.py`(31개), 클라이언트 `apps/syncops/tests_fetch_api.py`(34개), LiveServer 왕복 `apps/syncops/tests_api_roundtrip.py`(19개) — 합계 84개 OK(zstd 바이너리가 없는 환경에서는 zstd 왕복 1건 skip).

### `/api/v2/` OpenAPI 문서 (Swagger/ReDoc — drf-spectacular, 2026-07-27)

`/api/v2/` 의 기존 뷰/인증/응답은 **한 비트도 바꾸지 않고** 문서만 얹은 것 — `@extend_schema` 데코레이터는 `drf_spectacular.openapi.AutoSchema` 가 스키마 생성 시에만 읽고, 런타임 요청 처리에는 관여하지 않는다.

| 구성 요소 | 위치 | 역할 |
|---|---|---|
| `SPECTACULAR_SETTINGS` | `config/settings/base.py` | `TITLE`/`DESCRIPTION`/`VERSION`, `SERVE_PERMISSIONS=["...IsAuthenticated"]` + `SERVE_AUTHENTICATION=["...SessionAuthentication"]` (포털 세션 로그인 사용자만 스키마/문서 열람), `PREPROCESSING_HOOKS=["apps.api_v2.schema.preprocess_only_api_v2"]` + `SCHEMA_PATH_PREFIX=r"/api/v2"` (legacy `apps.api` 등 제외), **`POSTPROCESSING_HOOKS=["apps.api_v2.schema.rewrite_paths_to_ci_api"]`**(`base.py:264`, 2026-10-15 — 문서에 보이는 경로를 `/ci-api/` 로 표기), `DESCRIPTION` 의 Base URL·예시·401 안내는 `settings.CI_API_BASE` 를 f-string 으로 끼워 넣음(`base.py:193` 부터), `SWAGGER_UI_DIST`/`SWAGGER_UI_FAVICON_HREF`/`REDOC_DIST` 모두 `"SIDECAR"` (외부 CDN 미의존, 인트라넷 대응) |
| `apps/api_v2/schema.py` | 신규 모듈 | `ApiKeyAuthScheme` (`OpenApiAuthenticationExtension`, `target_class=apps.api_v2.authentication.ApiKeyAuthentication`, `name="ApiKeyAuth"`) — 커스텀 API Key 인증을 OpenAPI security scheme(`Bearer qgp_{prefix}_{secret}`)으로 노출해 Swagger UI 의 **Authorize** 버튼에서 Try-it-out 가능하게 함. `preprocess_only_api_v2(endpoints)`(`schema.py:42`) — path 가 `/api/v2/` 로 시작하는 엔드포인트만 스키마에 남김. **`rewrite_paths_to_ci_api(result, generator, request, public)`**(`schema.py:63`, POSTPROCESSING_HOOK) + 재귀 헬퍼 `_to_ci_api`(`:52`) — 생성된 스키마 `paths` 의 키(맨 앞 1회)와 하위 설명·예시 문자열의 `/api/v2/` 를 `/ci-api/` 로 치환한다. **Swagger/ReDoc 에 표시되는 엔드포인트 경로·설명·예시가 모두 `/ci-api/` 로 보이고 Try it out 도 `/qg/ci-api/…`(`SCHEMA_PATH_PREFIX_INSERT` 의 `/qg` + `/ci-api/`)로 호출**된다 — 게이트웨이가 쿠키 없는 `/qg/api/v2/` 요청을 HTML 401 로 막으므로 키만으로 통과하는 `/ci-api/` 를 문서의 호출 주소로 삼은 것. 뷰 코드·실제 라우팅(`/api/v2/` 마운트)은 불변. 상단 소개문 `info.description` 은 두 주소를 비교 설명하므로 치환 제외. 문서 페이지(`/api/v2/docs/`·`redoc/`·`schema/`)와 DB export 는 여전히 `/api/v2/` 에만 있다. |
| `apps/api_v2/apps.py` `ready()` | `ApiV2Config.ready()` | `from . import schema` — 모듈 import 시 `ApiKeyAuthScheme` 의 `OpenApiAuthenticationExtension.__init_subclass__` 등록이 트리거되도록 앱 로딩 시점에 강제 import (import 부수효과만, 런타임 로직 없음) |
| `apps/api_v2/urls.py` (3종 추가) | `schema/` → `SpectacularAPIView` (raw OpenAPI JSON/YAML) / `docs/` → `login_required(SpectacularSwaggerView)` (Swagger UI) / `redoc/` → `login_required(SpectacularRedocView)` (ReDoc). `docs/`·`redoc/` 는 `login_required` 로 한 번 더 감싸 미로그인 브라우저를 포털 로그인 화면으로 유도(스키마 JSON 자체는 `SERVE_PERMISSIONS` 가 이미 `IsAuthenticated` 로 막음). |
| 문서 UI(Swagger) 키 검증 배지 | `templates/api_v2/swagger_ui.html`(`:125` 부근) | Authorize 로 키를 넣으면 `{% url 'api_v2:me' %}` 의 `/api/v2/` 를 **`/ci-api/` 로 치환한 `/ci-api/me/`** 를 쿠키 없이 키만으로 호출해 ✅/❌ 배지를 띄운다(`/api/v2/me/` 는 게이트웨이가 HTML 401 로 막아 검증 불가하기 때문). |
| `settings.CI_API_BASE` · context processor `ci_api_base` | `config/settings/base.py:188` · `apps/accounts/context_processors.py:30`(`portal_settings` 가 함께 주입, `TEMPLATES` 의 `context_processors` 등록) | `CI_API_BASE` = (`QGP_BASE_URL` 끝 `/` 제거, 비면 기본 `https://qw.jkintra.net/qg`) + `/ci-api`. Swagger 소개문과 API Key 발급 완료(`api_key_created.html`)·목록(`api_key_list.html`) 화면의 호출 예시·안내가 이 값(템플릿 변수 `ci_api_base`)을 쓴다 — 발급 화면 curl 예시에서 옛 도메인 하드코딩 제거. |
| `requirements.txt` | 신규 의존성 | `drf-spectacular>=0.27`, `drf-spectacular-sidecar` |

뷰 함수별 `@extend_schema`(summary/description/parameters/responses, 한국어 summary)는 `docs/api_guide.md` 의 기존 요청/응답 예제를 근거로 각 `apps/api_v2/views/*.py` 뷰 **바로 위**에 추가되어 있다 — 문서 텍스트만 담당하며 뷰 본문·인증·권한·throttle·URL 은 무변경. 검증: `.venv/bin/python manage.py spectacular --file <output.yml>` 로 "Error [...]" 경고가 없어야 하고, `.venv/bin/python manage.py test apps.api_v2` 로 동작 불변을 확인한다. 테스트 `apps/api_v2/tests/test_docs.py` — 기존 스키마/Swagger/ReDoc/생성 테스트는 `/ci-api/` 표기로 단언하고, **`SchemaCiApiNotationTests`**(5개, `:149`)가 ① `paths` 키·하위 JSON 어디에도 `/api/v2/` 가 남지 않음 ② 소개문이 `/ci-api/`·`Bearer`·`Basic` 을 설명 ③ export/docs/schema 가 스키마에 없음 ④ `/api/v2/health/` 실제 라우팅 불변 ⑤ Swagger 페이지가 키를 `/ci-api/` 로 검증하는 스크립트를 포함함을 확인한다. 실행: `QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= GATEWAY_ENTRY_ENABLED= TRUSTED_PROXY_CIDRS= QGP_BASE_URL= .venv/bin/python manage.py test apps.api_v2.tests.test_docs apps.api_v2.tests.test_ci_api`.

### 회원가입 플로우

`apps/accounts/views.py:signup`:
1. `is_active=False` 로 사용자 생성 → "승인 대기" 화면
2. 관리자가 `accounts:user_list` 에서 `is_active=True` + role 부여
3. 로그인 가능

**SSO 전용(`LOCAL_LOGIN_ACTIVE=False`)이면 `signup` 이 404** (`views.py:29`) — 계정은 SSO 첫 로그인으로 생성된다(위 "SSO 로그인").

세션은 24시간 만료 (`SESSION_COOKIE_AGE`, `base.py:131`), 매 요청 갱신 안 함.

### 사용자 직접 추가 (`/accounts/users/new/`) — 승인 플로우와의 차이

관리자가 사용자 관리 화면(`/accounts/users/`)에서 승인 대기 절차를 거치지 않고 계정을 바로 만드는 경로. 위 회원가입 플로우와 **검증 로직은 동일**하되 활성화 시점이 다르다.

| 구분 | 회원가입(`signup`) | 사용자 직접 추가(`user_create`) |
|---|---|---|
| 폼 | `apps/accounts/forms.py:SignupForm` (L11) | `apps/accounts/forms.py:AdminUserCreateForm` (L79, `SignupForm` 상속) |
| 검증 | 중복 ID/이메일, 비밀번호 8자+특수문자, 확인 일치 | 동일 (부모 그대로 상속, 추가 검증 없음) |
| 역할(role) | 미지정(가입 시점엔 없음) — 승인 시 관리자가 부여 | 폼에 `role` `ChoiceField`(기본 `viewer`) 추가 — 생성 시점에 관리자가 바로 지정 |
| `is_active` | `False` (승인 대기) | `True` (즉시 활성, `save()` 에서 강제 세팅) |
| 진입 | `/accounts/signup/`, 인증 불요 | `/accounts/users/new/` (`name=accounts:user_create`), `apps/accounts/views.py:user_create` (L62) — `login_required` + `_require_admin(request.user)` (L43) 가드로 admin 전용 |
| 이력 | 없음 | `ActivityLog.log(..., Action.CREATE, TargetType.USER, target=user, detail="관리자 직접 추가 / role=... / 즉시 활성")` — **비밀번호는 어디에도 기록하지 않음** |
| UI 진입점 | — | `templates/accounts/user_list.html` 헤더의 "사용자 추가" 버튼 → `templates/accounts/user_form.html` (신규 템플릿) |

라우팅: `apps/accounts/urls.py` 에 `users/new/` 를 `users/`(목록) 와 `users/<uuid:pk>/update/` 사이에 등록 (`uuid` path converter 라 `new` 문자열과는 애초에 충돌하지 않음).

---

## 8. 실행 / 배포

### 운영 환경

- 접속 주소: `https://qw.jkintra.net/qg/` (게이트웨이 nginx → IDC `wx-qms221` 의 앱 HTTP `8601`, `SECURE_PROXY_SSL_HEADER`), 프로그램 호출은 `https://qw.jkintra.net/qg/ci-api/`. 이전 AWS 운영 도메인 `qualitygate.jkintra.net`(ALB → EC2)은 2026-10 서버 이관으로 **종료**(502)
- 실행: `python manage.py runserver 0.0.0.0:8601` (단일 프로세스, 멀티스레드)
- venv: `/home/taeshkim/qa_qualitygate_portal/.venv` (Python 3.10+)
- 설정: `DJANGO_SETTINGS_MODULE=config.settings.dev` (production.py 도 있으나 운영은 dev.py 사용)
- DB: `db.sqlite3` (**Git 에서 제외**, `.gitignore` — GitHub 100MB 제한 초과로 2026-10-28 추적 해제). 서버 간 이관은 `apps/syncops` 스냅샷 명령으로.

하위 경로 배포(`http://172.20.0.221/qg/`)는 `.env` 의 `QGP_SCRIPT_NAME=/qg` 로 선택한다. 미설정이면 기존 루트 배포다. `DJANGO_ALLOWED_HOSTS` 의 기존 목록에 `172.20.0.221`, `qw.jkintra.net` 을 추가하고 서버를 재기동한다. Nginx 는 `/qg` 를 `/qg/` 로 리다이렉트하고, `/qg/` 를 `http://host.docker.internal:8601/` 로 전달하면서 접두 경로를 제거해야 한다. 관리 콘솔에 저장한 라우트가 실제 실행 중인 Nginx 설정에 적용됐는지도 확인한다.

- `config/settings/base.py:22` — `QGP_SCRIPT_NAME` 정규화·검증 후 `FORCE_SCRIPT_NAME` 지정. 로그인 설정은 URL 이름을 사용하고, 세션/CSRF 쿠키 경로도 포털 루트에 맞춘다.
- `config/settings/base.py:145` — `STATIC_URL` 을 접두 경로가 포함된 절대 경로로 명시한다. 상대 `static/` 에 의존하면 runserver 초기 검사 중 script prefix 설정 전에 `/static/` 으로 캐시될 수 있다. `config/urls.py:92` 의 **DEBUG 전용** staticfiles 라우트가 프록시에서 접두 경로를 제거한 `/static/...` 요청을 처리한다. `DEBUG=False` 배포는 Nginx 등 별도 정적 파일 서빙이 필요하다.
- 화면 이동·TAP UI 탭 복귀·API v2의 `self_url`/`detail_url`/콘솔 링크는 URL 역참조를 사용한다. `config/settings/base.py:240` 의 `SCHEMA_PATH_PREFIX_INSERT` 로 OpenAPI 호출 주소도 접두 경로를 포함한다 (`config/settings/base.py:269`. `schema.py` 전처리는 앱 내부 `/api/v2/` 경로 기준이고, 후처리 `rewrite_paths_to_ci_api` 가 접두 삽입 뒤의 `/qg/api/v2/…` 를 `/qg/ci-api/…` 로 바꿔 표기한다).
- 회귀 검증: `config/test_subpath.py` 는 루트/하위 경로, 초기 정적 URL 캐시, 로그인·권한·Service Worker 를 검사한다. API 링크·스키마는 `apps/api_v2/tests/` 와 함께 검증하며, 실행 명령은 `SKILLS.md` §4 참고.

### 시작 스크립트

`172.20.0.221` 의 상시 기동은 **`qualitygate.service`** 가 담당한다. 원본 `deploy/qualitygate.service` 를 `/etc/systemd/system/qualitygate.service` 로 설치하고 `daemon-reload` → `enable --now qualitygate` 로 등록한다 (명령은 README §6). 사용자/그룹 `taeshkim`, 작업 디렉터리 `/home/taeshkim/qa-qg`, `.venv` Python으로 `runserver 0.0.0.0:8601 --noreload` 실행. `multi-user.target` 부팅 자동 시작, `Restart=always`·5초 대기·60초 안에 시작 5회 제한, stdout/stderr 는 journal 로 수집한다.

- `EnvironmentFile` 은 사용하지 않는다. 앱의 python-dotenv 가 프로젝트 `.env` 를 읽으며, 코드·환경변수 변경 후 `sudo systemctl restart qualitygate` 가 필요하다.
- 서비스는 Git/pip/migrate 를 실행하지 않는다. 코드·의존성·스키마 배포는 별도 수행하며, unit 변경 시 원본 재설치·`daemon-reload`·재시작한다.
- 운영 확인: `systemctl status qualitygate`, `sudo journalctl -u qualitygate -f`. 의도적 종료는 `sudo systemctl stop qualitygate` (자동 재시작하지 않음). 서비스가 8601을 사용 중일 때 아래 수동 스크립트를 동시에 실행하지 않는다.

`run.sh` / `run.ps1` / `run.bat` 가 동일 동작:
0. `origin/master` fast-forward(ff-only) pull 로 코드 최신화. git 미설치 / 브랜치가 `master` 아님 / `git fetch` 실패 / ff 불가 → 경고만 출력하고 현재 코드로 계속. **`db.sqlite3` 는 스크립트가 일절 write/checkout/reset 하지 않음** — ff-only 라 코드만 갱신 (`db.sqlite3` 는 git 미추적, DB 는 수동 명령어로만 변경). → 각 스크립트의 `[1/6] 최신 master 동기화` 블록.
1. python 3.13 → 3.12 → 3.11 → 3.10 → python3 순으로 인터프리터 탐색
2. `.venv` 가 3.10 미만이면 재생성
3. `pip install -q -r requirements.txt`
4. `.env` 가 없으면 `.env.example` 복사
5. `migrate --run-syncdb`
6. `runserver 0.0.0.0:8601`

### 서버 이관 / 전체 데이터 싱크 (`apps/syncops`, 2026-10-09)

모델 없는 관리 명령 앱. 라이브러리 `apps/syncops/snapshot.py`, 명령 `apps/syncops/management/commands/{export_snapshot,import_snapshot,verify_snapshot,sync_from_source,fetch_snapshot_via_api}.py`. 사용자 시점 절차·AI 런북은 `docs/prd_server_migration_sync.md`(단일 진실 소스), SSO 이어하기는 `docs/handoff_sso_integration.md`.

- **번들**: `snapshots/qg-<UTC>/`(0700, 파일 0600, `.gitignore`) = `manifest.json`(`schema_version=1`, `db{sha256,file_sha256,compression,…}`, `tables{이름: 행 수}` — `sqlite_sequence` 포함 54개, `migrations`, `config{encrypted,entries(파일명만)}`) + `db.sqlite3.zst|.gz|무압축` + `config.tar[.enc]`.
- **export** (`snapshot.py:export_snapshot`, `backup_db`): 원본은 `mode=ro` 로만 연다. sqlite3 backup API(`BACKUP_PAGES=1024`)로 임시본 → `quick_check` → sha256 → 압축. 다른 프로세스가 쓰면 backup 이 재시작되므로 `--max-backup-seconds`(기본 1800) 초과 시 "서비스·cron 정지 후 재실행" 안내로 실패 — **운영 컷오버는 원본 cron·웹·리스너를 멈춘 뒤 export**. `--dry-run`(`plan_export`)은 아무것도 쓰지 않는다. config 암호화는 `openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:VAR`(패스프레이즈는 환경변수로만). **번들 DB 자체는 암호화되지 않는다**(세션·해시 포함 — 종료 메시지·`--dry-run` 경고, 외부 매체는 별도 암호화). manifest `db.source_path`(원본 DB 절대경로, schema_version 유지) 기록. SIGHUP/SIGTERM 수신 시 `interrupt_guard` 가 `SnapshotInterrupted` 를 던져 `.partial`·만든 출력 디렉터리를 정리(종료 시 핸들러 원복).
- **import** (`snapshot.py:import_db`, `_preserve_and_swap`, `restore_config`): 선행검증(schema_version · **같은 호스트 가드 `same_host_conflict`**(manifest.source.hostname == 이 호스트 이고 base_dir 같음 또는 대상 DB realpath == `manifest.db.source_path` 이면 거부, `--allow-same-host` 로만 허용) · 번들 sha256 · `service_running` · `unknown_migrations`(코드가 데이터보다 오래됨 — **`--force` 로는 안 풀리고 `--allow-older-code` 로만**) · `user_data_tables`(대상에 데이터 → `--force`; 대상 DB 를 읽다 `sqlite3.Error` 면 손상 의심으로 `--force` 없이는 거부) · 디스크 · config 암호화 시 `verify_config_passphrase`(첫 512B 복호화→tar 헤더 검증, DB 교체 전)) → `<target>.importing` 스트리밍 해제(`decompress_to`) → sha256/`quick_check`/행 수 일치 → 기존 DB(+`-journal/-wal/-shm`)를 **`os.link` 하드링크로 `.pre-import-<ts>` 보관한 뒤 `os.replace` 만 수행**(대상 경로가 비는 구간 없음; 하드링크 불가 FS 면 rename 폴백 + "대상 없음+pre-import 존재 → mv 복구" 안내, 실패 시 롤백). `handle()` 은 `OSError`(ENOSPC 별도 메시지)·`sqlite3.Error` 를 트레이스백 없이 `CommandError` 로 변환(기존 DB 불변·`.importing` 정리). 기본 DB 가 대상이면 이어서 `migrate`, 아니면 자동 생략. `--restore-config` 는 기존 파일을 덮어쓰지 않고(`.incoming`, `--force` 면 `.bak-<ts>`), crontab·systemd 는 `restored-system/` 에 풀기만 하고 설치 명령 + 주의문(`SYSTEM_INSTALL_NOTE`: `#QGMIGRATE#` 접두·원본 경로 치환, run.sh 가 아닌 새 서버 방식 — PRD §6.3)만 출력, 경로 이탈·심볼릭 링크 tar 항목 거부.
- **verify** (`snapshot.py:compare_counts`): 행 수 `ok`/`newer(+N)`(`--allow-newer`)/`MISMATCH`/`MISSING`, manifest 에 없는 대상 테이블은 `extra` 로 보고만. manifest 의 마이그레이션이 대상에 미적용이면 `--allow-newer` 여도 실패. migrate 가 `django_migrations`·권한 행을 늘리므로 import 직후 검증은 `--allow-newer`. 출력 말미에 "행 수·마이그레이션만 비교, 내용 무결성은 import sha256 이 보증" 안내(내용 비교 `--deep`·HMAC 서명·`--encrypt-db` 는 미구현 한계).
- **sync_from_source**: `--source` 는 `USER@HOST` 형식만(`-` 시작·공백·메타문자 거부, ssh/rsync 에 `--` 구분자). ssh(BatchMode + 사용자 `--ssh-opts` + 기본 `ServerAliveInterval=30`/`ServerAliveCountMax=20`) 원격 export(`--max-backup-seconds`·`--compress` 지정 시에만 전달) → rsync(`--partial`) → ③ manifest·sha256 검증 **직후 원격 번들 삭제**(`--keep-remote` 면 보존, 경로가 `.../snapshots/qg-*` 일 때만) → ④ import(`--force`/`--allow-older-code`/`--allow-same-host` 통과) → ⑤ verify(`--allow-newer`). 패스프레이즈는 ssh stdin(원격 `read` → `QG_SNAPSHOT_PASS`)으로만. 모든 실패 메시지에 원격 번들 경로·`rm -rf --` 정리 명령(이미 삭제됐으면 "이미 삭제됨")과, 사용자가 준 옵션에서 재구성한 재개 명령(④ 이후 실패는 `import_snapshot <로컬번들>`; 패스프레이즈는 변수명만 + 재설정 안내)을 출력. ssh 불가 시 번들을 수동 이동 후 `import_snapshot`.
- **fetch_snapshot_via_api** (`fetch_snapshot_via_api.py`, 경로 C — HTTPS 만 열린 경우): 원본의 Export API(위 "DB 스냅샷 내보내기 API")로 번들을 받는다. `Command`(`:118`) → `_run`(`:161`): ① `_create`(`:261`, POST — **재시도 안 함**, 중복 생성 방지) 또는 `--existing-id` 재개 → ② `_wait_ready`(`:302`) 폴링(`--poll-interval`/`--timeout`) → ③ `_download`(`:337`)가 `<out-dir>/.fetch-<id>/<name>.part` 로 받으며 `Range: bytes=<have>-` 로 이어받기(`_consume` `:403` 이 206 `Content-Range` 시작 일치·200(Range 무시→처음부터)·416(이미 완전한 .part 수용) 처리, `MAX_RETRIES=5`·5xx/네트워크 오류 재시도), 응답 `X-Content-SHA256`·상태의 `files[].sha256`·manifest `db.file_sha256` **모두 일치해야** 최종 이름으로 `os.replace`(서버가 sha256 을 안 주면 거부) → ④ `_place_bundle`(`:457`)이 `<out-dir>/qg-<manifest.created_at>/` 에 DB 파일 → `manifest.json` 순으로 배치(같은 디렉터리가 이미 있으면 덮어쓰지 않고 실패) → ⑤ `_delete_remote`(`:321`, `--keep-remote` 면 생략; 실패해도 경고만 — 24h 뒤 자동 만료) → `import_snapshot`/`verify_snapshot --allow-newer` 안내 출력. **보안**: 키는 `--key-env` 환경변수에서만 읽고 `Client.scrub` 로 메시지에서 `***` 치환, `validate_url`(`:51`)이 `https://` 만(예외 `http://127.0.0.1|localhost`)·userinfo/query 금지, 리다이렉트는 같은 origin 만 수동 추적(Authorization 유출 방지), 파일명은 `ALLOWED_NAME_RE` 허용 목록, manifest 의 `config` 가 비어 있지 않으면 거부, 인증서 검증을 끄는 옵션 없음. 실패 메시지에는 항상 원격 snapshot id 와 `--existing-id` 재개 명령 포함(`_fail_message` `:149`). **config 는 이 경로로 오지 않는다** — `.env`/secrets 는 별도 전달.
- **주의(운영 호스트)**: 운영 `db.sqlite3` 에 `import_snapshot`/`migrate` 금지, 실제 export 는 점검 창에서만(`--dry-run` 만 상시 안전). `run.sh` 는 기동 시 `git merge`·`pip`·`migrate` 를 자동 수행하므로 이관 import 전에 새 서버에서 실행하지 않는다. `db.sqlite3` 는 git 미추적이라 새 서버 clone 에는 DB 가 없으므로 `--force` 없이 import 하는 것이 기본 절차 (예전 clone 에 옛 DB 가 남아 있으면 `mv db.sqlite3 db.sqlite3.git-clone` 으로 치운 뒤 import)(`--force` 는 의도적 덮어쓰기에만, `--allow-older-code` 는 쓰지 않음). `*.incoming`·`*.bak-*`·`db.sqlite3.pre-import-*`·`restored-system/` 등은 `.gitignore` 대상이며 `git add -A` 금지.
- **테스트**: `QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= .venv/bin/python manage.py test apps.syncops` (임시 가짜 DB·ssh/rsync mock 만 사용, 운영 DB 불접촉; Export API 클라이언트/왕복 `tests_fetch_api`·`tests_api_roundtrip` 은 로컬 가짜 서버·LiveServer·임시 DB 만 쓰고 원본 서버로 요청을 보내지 않는다. 서버 쪽은 `apps.api_v2.tests_export`). 실제 ssh/rsync·5GB 급 export 는 자동 검증 불가 → 이관 리허설에서 수동 확인.

### SSO 운영 반영 (Keycloak client 신청 · 배포 순서 · 되돌리기)

**Keycloak client 신청 값** (신청처: **worxhub**)

| 항목 | 값 |
|---|---|
| 유형 | Confidential, Standard flow 만, PKCE S256 |
| Valid redirect URIs (2종) | `https://qualitygate.jkintra.net/accounts/oidc/internal/callback/` · 게이트웨이 경유 `https://qw.jkintra.net/qg/accounts/oidc/internal/callback/` |
| Valid post logout redirect URIs (2종) | `https://qualitygate.jkintra.net/accounts/login/` · `https://qw.jkintra.net/qg/accounts/login/` |
| Web origins | 비움 |

Post logout redirect URI 는 로그아웃 (b) 의 `post_logout_redirect_uri`(쿼리 없는 `/accounts/login/` 절대 URL)와 **정확히 일치**해야 한다. 발급받은 client id/secret 은 `.env` 에만 넣는다.

**배포 순서** (서비스는 Git/pip/migrate 를 실행하지 않는다 — 위 §8 "시작 스크립트")
1. `git pull`
2. `pip install -r requirements.txt` (`authlib` 추가)
3. 운영 DB 수동 `migrate` — `accounts 0003_externalidentity`(additive, 신규 테이블만). **사용자 승인 후에만 실행** (`db.sqlite3` 는 스크립트가 건드리지 않는다)
4. `.env` 에 `OIDC_INTERNAL_ISSUER=https://sso.jkintra.net/realms/world`, `OIDC_INTERNAL_CLIENT_ID`, `OIDC_INTERNAL_CLIENT_SECRET`, (게이트웨이 경유면) `TRUSTED_PROXY_CIDRS`(게이트웨이 Nginx 출발지 `/32`), `GATEWAY_LOGOUT_URL=https://qw.jkintra.net/_qw/logout` 추가. Keycloak 에 post logout redirect URI 등록이 끝났으면 `OIDC_INTERNAL_POST_LOGOUT_REDIRECT=True` 도 추가
5. `sudo systemctl restart qualitygate`
6. 확인 — 시크릿 창으로 첫 접속하면 Keycloak 전용 로그인 페이지 → SSO 로그인 성공 → 로그아웃하면 전용 로그인 페이지로 복귀 → 버튼을 다시 누르면 Keycloak 이 비밀번호를 요구(`POST_LOGOUT_REDIRECT=True` 또는 게이트웨이 로그아웃일 때)

**사전 점검 — 기존 계정 22개**: 이메일 자동 연결은 이메일이 정확히 1명과 일치할 때만 된다. **이메일이 없는 계정(`ls-b2f538`)과 도메인이 `worxphere.ai` 가 아닌 계정(`admin@example.com`, `test`(`jobkorea.co.kr`), `tester`·`test01`~`test04` 등 example 류)은 자동 연결되지 않는다** — 첫 SSO 로그인 시 새 `viewer` 계정이 만들어지므로, 배포 전에 Django admin 에서 이메일을 `@worxphere.ai` 실제 주소로 정리하거나, SSO 첫 로그인 후 새 계정에 역할을 재부여하고 옛 계정은 비활성화한다.

**되돌리기**: `.env` 에서 `OIDC_INTERNAL_ISSUER`/`CLIENT_ID`/`CLIENT_SECRET` 를 지우거나 주석 처리 → `sudo systemctl restart qualitygate`. OIDC 값이 없으면 `LOCAL_LOGIN_ACTIVE=True` 라 자체 로그인·회원가입이 즉시 복구된다(`ExternalIdentity` 테이블과 SSO 로 만든 계정은 그대로 남고 무해). 로그아웃만 되돌리려면 `OIDC_INTERNAL_POST_LOGOUT_REDIRECT` 를 `False`/삭제 후 재시작. 코드 자체를 되돌려도 0003 테이블은 남는다(additive).

**이 호스트(wx-qms221, `172.20.0.221`) 반영 이력 (2026-10-09)**: `accounts.0003_externalidentity`·`integrations.0009_ui_automation_owners` migrate 적용(사전 DB 백업 `snapshots/db-pre-sso-<ts>.sqlite3`), `.env` 의 `DJANGO_CSRF_TRUSTED_ORIGINS` 에 `https://qw.jkintra.net` 추가, `TRUSTED_PROXY_CIDRS=172.20.0.222/32` 명시 고정(기본값과 동일), `sudo systemctl restart qualitygate`(systemd `Restart=always`, `runserver --noreload`). 이 호스트에는 게이트웨이 nginx 가 없다(게이트웨이는 별도 2번 서버 wx-qms222). 게이트웨이 진입 모드(`GATEWAY_ENTRY_ENABLED`)는 **`.env` 에 키가 없어 꺼져 있다**(2026-10-09 키 존재 여부만 확인) — 게이트웨이 라우팅 변경(QA 인프라팀) 후 `docs/handoff_sso_integration.md` §10 순서로 켠다.

### 환경 변수 (`.env`)

핵심:
- `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_SETTINGS_MODULE`
- `DJANGO_CSRF_TRUSTED_ORIGINS` (콤마 구분. 코드 기본값은 이관 전 주소 `https://qualitygate.jkintra.net` 그대로 — 종료된 주소지만 기본값이 남아 있음. **이 호스트 `.env` 는 `https://qualitygate.jkintra.net,https://qw.jkintra.net`** — 게이트웨이 접속에는 `https://qw.jkintra.net` 이 필수)
- `QGP_BASE_URL` (외부 접속 주소, 이 호스트 `https://qw.jkintra.net/qg`) — Slack Run 링크(`apps/notifications/services.py:79`)와 `settings.CI_API_BASE`(= 이 값 + `/ci-api`, 비면 기본 `https://qw.jkintra.net/qg/ci-api` — Swagger 소개문·API Key 화면 호출 예시)에 쓰인다
- `QGP_SCRIPT_NAME` (선택, 예: `/qg`; 비우면 루트 배포)
- SSO: `OIDC_INTERNAL_ISSUER`/`_CLIENT_ID`/`_CLIENT_SECRET`/`_POST_LOGOUT_REDIRECT`/`_EMAIL_DOMAINS`, `LOCAL_LOGIN_ENABLED`, `GATEWAY_ENTRY_ENABLED`, `TRUSTED_PROXY_CIDRS`, `GATEWAY_LOGOUT_URL` — 의미·기본값은 §7 "SSO 로그인" 설정 플래그 표, 값 예시는 `.env.example`(`OIDC_INTERNAL_*`·`GATEWAY_ENTRY_ENABLED`·`TRUSTED_PROXY_CIDRS`·`GATEWAY_LOGOUT_URL` 은 락아웃/기본값 가림 방지를 위해 주석 처리)

PostgreSQL/Redis/TAS 및 레거시 Slack 변수들은 현재 미사용이다. 현재 알림·TAP·Google Sheets 연동 환경변수는 `README.md` §8 참고.

### 라이브러리 (`requirements.txt`)

Django 4.2 / DRF / django-filter / python-dotenv / django-cors-headers / celery / redis / django-celery-beat. **`drf-spectacular` + `drf-spectacular-sidecar`** (`/api/v2/` Swagger/ReDoc 문서 — §7 참고) 추가(2026-07-27). **`authlib>=1.8,<2`** (Keycloak OIDC 로그인, `apps/accounts/sso.py`). 의존성은 최소.

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
- `deploy_params` (`views.py:831` 부근) — WAITING_DEPLOY 모달용. `resolve_and_apply_app_image(parameters, run)` 호출 후 `app_image_matched` 반환. 콘솔 추출 성공 시 `choices=[tag]` 주입 → select 정상 렌더.
- `prod_deploy_params` (`views.py:876` 부근) — QG_PASSED 모달용. 동일하게 `resolve_and_apply_app_image` 호출. prod 가 dev 와 동일한 ECR repo 를 쓰므로 run.image_console_output 에서 추출한 태그를 그대로 적용.
- APP_IMAGE 주입 핵심 로직: `resolve_and_apply_app_image` (`services.py:679`) / `extract_pushed_image_tag` (`jenkins.py:866`) — §2 "수동 모달 APP_IMAGE default 오버라이드" 참조.
- 트리거 측: `gate_run` (`views.py:539`) / `deploy_trigger` (`views.py:853`) / `prod_deploy` (`views.py:898`)
- 클라이언트는 `_collect_params(post)` 가 `param_*` prefix 로 파라미터를 수집

### "APP_IMAGE 빈값 차단 / default 오버라이드 규칙 변경"
- **빈값 차단 조건 수정**: `apps/qualitygates/services.py:346` — `_validate_app_image_param` 의 조건식. 현재 `빈 문자열/공백/콜론 종료` 를 400으로 막음. 더 넓게/좁게 막으려면 이 함수 한 곳만 수정하면 `trigger_deploy`/`trigger_prod` 양쪽 모두 반영됨.
- **클라이언트 측 검증 규칙 수정**: `templates/qualitygates/run_detail.html:114` — `_isValidAppImage` 함수. 서버 조건과 일치하게 유지할 것.
- **콘솔 추출 태그 주입 규칙 수정**: `apps/qualitygates/services.py:679` — `resolve_and_apply_app_image`. 콘솔 추출 → choices prepend → prefix 매칭 폴백 순서. `extract_pushed_image_tag` 의 정규식/prefix 가드를 조정하려면 `apps/integrations/jenkins.py:866`.
- **레거시 prefix 매칭 헬퍼(api_v2/auto_mode 폴백용)**: `apps/qualitygates/services.py:666` — `_apply_app_image_default`. run 없이 `image_build_number` 만 있을 때의 폴백 (api_v2 meta 경로). 삭제 금지.
- **구 헬퍼 정의 보존**: `apps/qualitygates/views.py:46` — `_override_app_image_default`. 함수 자체는 유지 (tests.py:16 import), 호출처만 `resolve_and_apply_app_image` 로 교체된 상태.
- **APP_IMAGE select 빈 옵션 필터 수정**: `templates/qualitygates/run_detail.html:81` — `_renderParam` 의 APP_IMAGE 분기. choice 필터 조건을 여기서 조정.

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
| `QG_PASSED` | QG Passed (Awaiting Stg **Approval**) | QG Passed (Awaiting Prd **Approval**) |
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

### "테스트 재시도 횟수(`retry_count`) 동작 변경"

- **Suite 편집**: `apps/suites/views.py:suite_edit` POST 가 `retry_count` 검증(1 이상) + 저장.
- **스냅샷 출처 (단독/Reliability)**: Run 트리거 시 `apps/suites/services.py` 의 3개 trigger 헬퍼(`trigger_suite_run` 관련 분기)와 `apps/robustness/services.py:_trigger_once` 가 `suite_run.retry_limit = suite.retry_count` 로 복사 → `TestSuiteRun.retry_limit`.
- **스냅샷 출처 (QG 경로)**: `apps/qualitygates/services.py:trigger_gate_run` 이 `QualityGateRunTest.objects.create(..., retry_limit=max(1, gs.test_suite.retry_count))` 로 복사 (`services.py:125`) → `QualityGateRunTest.retry_limit`. 이후 `_try_trigger_test` 가 생성하는 per-attempt `TestSuiteRun` 도 `retry_limit=max(1, test_run.retry_limit)` 으로 통일 (`views.py:1766`).
- **단독/Reliability 재시도 분기**: `apps/suites/views.py:_poll_run_status` (`views.py:814`) — FAILURE 판정 시 `job_run.attempt < suite_run.retry_limit` 이면 `job_run.attempt += 1` + PENDING 으로 되돌려 `_try_trigger_next` 재트리거. **재시도 시 `job_run.jenkins_build_url = ""`** 로 초기화해 직전 실패 빌드 URL 이 한 사이클 노출되는 버그 해소 (`views.py:828`). 소진 시 FAILURE 확정 후 `_finalize_run`.
- **QG 재시도 분기**: `apps/qualitygates/views.py:_poll_one_test_build` (`views.py:1980`) — 비-SUCCESS 시 해당 job_run/suite_run 을 COMPLETED(실패)로 마감(이력 보존), `test_run.attempt < test_run.retry_limit` 스냅샷이면 test_run 을 PENDING 으로 되돌리고 return False (구: `suite.retry_count` 라이브 값 참조 → 진행 중 변경 시 모순 표시/조기·지연 소진 버그 있었음). 순차 재트리거는 `_poll_test_sequential`(PENDING → WAITING_TEST + `_advance_test`), 병렬은 `_poll_tests_parallel` 가 자동 재트리거.
- **병렬 Robustness 재시도 강등 수정**: `apps/robustness/services.py:_update_run_status` (`services.py:240`) 가 `_poll_run_status` 직후 `suite_run.refresh_from_db()` 하고, 스케줄이 `parallel_jobs=True` 이고 suite_run 이 RUNNING 이면 `_trigger_all_pending(suite_run)` 을 호출 (`services.py:254~256`). 구현 전에는 `_poll_run_status` 내부의 `_try_trigger_next` 가 PENDING 1개만 재트리거하므로, 동시 실패한 잡들이 사이클마다 1개씩 순차 재시도로 강등되던 문제가 있었음.
- **재시도 제외 조건**: ABORTED(사용자 취소)·트리거 실패·Reliability interval 타임아웃은 재시도 안 함 — 즉시 FAILURE 확정.
- **Slack 알림 제어**: 중간 시도 실패는 `ActivityLog RETRY` 기록만. 모든 시도 소진된 최종 실패에서만 `SUITE_RUN_FAILED`/`TEST_SUITE_FAILED` 발송.
- **재시도 한도 단일 출처 요약**:

  | 경로 | 스냅샷 필드 | 채우는 곳 |
  |---|---|---|
  | 단독 Suite Run / Reliability | `TestSuiteRun.retry_limit` | `suites/services.py` trigger 헬퍼, `robustness/services.py:_trigger_once` |
  | QG 파이프라인 | `QualityGateRunTest.retry_limit` | `qualitygates/services.py:trigger_gate_run:125` |

- **seq_no 단일 출처 요약** (suite 별 1부터 순증가, `TestSuiteRun.next_seq_no(suite)` 한 곳):

  | 경로 | 부여 위치 |
  |---|---|
  | idle worker 1개 | `suites/services.py:_trigger_any_idle` |
  | job_id 지정 | `suites/services.py:_trigger_single_job_run` |
  | run_all 체인 | `suites/services.py:_trigger_chain_run` |
  | QG 파이프라인 Suite | `qualitygates/views.py:_try_trigger_test` |
  | Reliability Test | `robustness/services.py:_trigger_once` |

### "다크/라이트 테마 — 색 추가/수정, 새 컴포넌트가 다크에서 깨질 때"

테마는 **CSS 변수 + `[data-theme="dark"]`** 로 동작 (`static/css/style.css` 상단).
- **토글**: `templates/base.html` 의 `<head>` 인라인 `QGTheme`(localStorage `qg-theme`, 첫 페인트 전 `<html data-theme>` set → 무깜빡임) + 사이드바 '알림' 버튼 아래 `#theme-toggle-btn`(`onclick="QGTheme.toggle()"`). 기본 light(=속성 없음).
- **semantic 변수**(반드시 이걸 써라, 하드코딩 금지): `--bg`(페이지) / `--surface`(카드·모달·패널) / `--surface-2`(은은한 표면) / `--input-bg` / `--text` / `--text-muted` / `--border` / `--border-strong` / `--shadow-color` / `--glass-bg`·`--glass-border`(대시보드 글래스) / `--warn-bg`·`--warn-text` / `--info-bg`·`--info-text` / `--sidebar-*`(사이드바 전용, 두 테마 공통 다크).
- 다크 블록은 semantic 변수 + **gray scale(`--gray-50..900`) 를 뒤집어** override → `var(--gray-XX)`/semantic 을 쓰는 컴포넌트는 자동 전환. accent(`--primary` 등)도 다크에서 밝게 조정.
- **외부 CSS 로 override 안 되는 인라인 `style="background:#fff"` 류는 금지** — 인라인은 specificity 가 높아 테마가 안 먹는다. 템플릿 인라인 색은 반드시 `var(--surface)` 등으로. (과거 base/dashboard/qualitygates 인라인 하드코딩을 전부 var 로 치환함)
- 스타일시트 내 리터럴 light hex tint(qg-step 섹션/상태 tint, alert, status-badge)는 파일 끝 `[data-theme="dark"]` override 블록에서 다크 값으로.
- **style.css 수정 시 `templates/base.html` 의 `?v=` 캐시버스트 토큰 bump 필수**(SKILLS §4).
- 차트(Chart.js) 축/그리드 색은 두 테마 공용 mid-slate(`#64748b`)로 둬서 토글 시 re-render 없이 양쪽에서 읽힘. 데이터 색(녹/노/빨)은 semantic.
- **전 페이지 톤 통일**: 오로라 그라데이션 배경 `.app-bg`(fixed, 콘텐츠 영역) 와 타이틀 그라데이션 `.page-header h1` 은 **전역**(`static/css/style.css`) 이고 `templates/base.html` 의 `<div class="app-bg">` 1개로 모든 페이지에 적용된다(라이트/다크 각각 override). 대시보드는 이 전역 배경 위에 글래스 카드만 추가로 얹는다 — 대시보드 전용 `.dash-bg`/`.dash .page-header h1` 은 제거됨.

### "반응형 — 모바일에서 깨짐 / 사이드바 드로어 / 새 페이지가 가로로 넘침"

브레이크포인트는 `static/css/style.css` 파일 끝 `Responsive` 섹션의 `@media` 블록에 집중돼 있다.
- **>1024px**: 기본(사이드바 240px 고정, `.layout` flex).
- **≤1024px (태블릿)**: `--sidebar-width: 200px`, 콘텐츠 패딩 완화.
- **≤768px (모바일)**: 핵심 전환.
  - `.layout { display:block }` — 사이드바가 fixed 드로어가 되므로 flex 불필요(이걸 빼면 flex intrinsic-size 때문에 긴 타이틀이 좁게 줄바꿈됨).
  - 사이드바: `transform:translateX(-100%)` 숨김 → `.sidebar.open` 으로 슬라이드 인. `.mobile-topbar`(햄버거+brand, fixed top) + `.sidebar-overlay`(딤) 노출. 토글은 `templates/base.html` 의 `<head>` 인라인 **`QGNav`**(open/close/toggle, body.nav-open 스크롤락) — 햄버거 `onclick`, 오버레이 `onclick`, 메뉴 링크 클릭/ESC/리사이즈(>768) 시 자동 close.
  - `.main-content { margin-left:0; padding-top:68px }`(상단 바 높이), `.app-bg { left:0; top:52px }`.
  - `.page-header { flex-direction:column }`(타이틀 한 줄 + 액션 버튼 아래로), `.detail-grid` 1열, `.form-row`/`.filter-form` 줄바꿈, `.admin-tabs`/`.event-tabs` 가로 스크롤(`flex-wrap:nowrap; overflow-x:auto`), 토스트/우하단 OS알림 클러스터 재배치, `.qg-pipeline { flex-direction:column }`(3섹션 세로 적층).
- **≤480px (초소형)**: 폰트/패딩 한 단계 더 축소, 테이블 셀 패딩 완화.
- **전역 안전망**: `.main-content { min-width:0 }`(flex item 이 내부 wide 콘텐츠로 뷰포트를 밀지 않게) + `.table-wrapper { overflow-x:auto }`(테이블은 내부 스크롤).
- **새 페이지/템플릿이 모바일에서 가로로 넘칠 때**: ① 버튼 여러 개를 담은 인라인 `display:flex` 컨테이너에 `flex-wrap:wrap`, ② `<table class="table">` 를 `<div class="table-wrapper">` 로 감싸기, ③ 인라인 고정폭(≥300px) 블록에 `max-width:100%`. (전역 클래스 동작 변경은 style.css 의 미디어쿼리에서.)
- 검증: 헤드리스 chromium 으로 모바일 폭 스냅샷 가능(`/home/taeshkim/qg_shot/shot.py`, SKILLS 참조). **주의** — 헤드리스 래스터화 버그로 `.page-header h1`(그라데이션 `background-clip:text`) 긴 타이틀이 글자단위로 쪼개져 *보이지만* computed layout(블록·전체폭·한 줄)은 정상이라 실제 Chrome 에선 정상 렌더된다.
- **style.css 수정 시 base.html `?v=` 캐시버스트 bump 필수**(현재 `20260618a`).

### "Visualization (Quality Highway / Live Race Control) — 화면·상태 매핑·폴링 수정"

`/visualization/`은 최근 Test Suite Run을 레이싱 머신으로 표현하는 Canvas와 상태 관제용 Race Control HUD를 합친 읽기 전용 화면이다. Quality Gate Run은 의도적으로 제외한다.

- **파일 구성**
  - 백엔드: `apps/visualization/views.py`의 `index`(페이지)·`data`(JSON), `apps/visualization/urls.py`. 별도 모델은 없고 두 endpoint 모두 `@login_required` + **`@_admin_only`(2026-10-21 — `request.user.is_admin` 만 통과; 페이지는 비-admin 을 대시보드로 리다이렉트(+메시지), `/data/` 는 403 `role_denied` JSON)**. 사이드바 메뉴(`templates/base.html`)도 `{% if user.is_admin %}`.
  - 마크업: `templates/visualization/index.html`. Canvas, 연결/동기화 상태, 일시정지, 상태별 통계, 최근 전송 피드, 범례, 전체 Suite Run 링크를 배치하고 `QGHighway.init(...)`에 data/detail/background URL과 6초 간격을 넘긴다.
  - 스타일: **전용** `static/css/qg_highway.css`. Race Control 패널, Canvas chrome, 툴팁, 상태 카드, 라이트/다크 변수와 1280/1024/768/520px 반응형 배치를 여기서 관리하며, 전역 `style.css`에 남은 구 `.hw-*` 기본 셀렉터보다 후순위로 로드해 덮어쓴다. CSS/JS 캐시버스트도 `templates/visualization/index.html`에서 함께 올린다.
  - 런타임: `static/js/qg_highway.js`의 `window.QGHighway.init`. Canvas 렌더·차량 물리·HUD 동기화·상세 이동·접근성·6초 폴링을 담당한다.
  - 환경 plate: imagegen으로 만든 메가시티 `static/images/visualization/highway-command-bg.webp`(1823×863). `init`이 비동기로 로드하고 `drawScene`이 cover 방식으로 합성한다. 로드 실패/자산 누락 시 `drawScene`의 절차적 도시·피트월·서킷 조명이 즉시 폴백하므로 Canvas 기능은 유지된다.

- **JSON 피드와 상태의 단일 출처** (`views.data`, `_suite_run_to_dict`)
  - 응답 스키마는 `id/kind("suite")/label/status("pass"|"running"|"fail"|"skip")/ts/detail` + 최상위 query-start `server_ts`와 `reset` boolean. `PENDING`은 제외한다.
  - RUNNING → `running`; 완료+실패 Job 없음 → `pass`; 완료+Skip이 아닌 실패/미분류가 하나라도 있음(`real_failed>0`) → `fail`; 완료+`real_failed==0`이고 실패 Job 있음 → `skip` — **실패 Job 이 모두 verdict=skip 인 경우와 모두 미분류 ABORTED(중단)인 경우 둘 다** 여기 해당한다(`triage_result`가 `'aborted'`를 반환하면 `_suite_run_to_dict`가 JS 가 아는 `'skip'`으로 다시 매핑 — 별도 aborted 시각화는 없음, 둘 다 "사고 아닌 피트 이탈"; Failure 확정 ABORTED 는 real_failed 에 포함돼 `fail`). 완료 상태는 `TestSuiteRun.annotate_triage()`의 `real_failed`를 제한된 후보에 계산하고 `triage_result`를 재사용한다.
  - 화면 의미는 PASS=정상 완주, RUNNING=점멸 라이브 주행, FAIL=스핀·연기·스키드 후 렉카 리커버리, SKIP=사고가 아닌 방향지시등·감속 피트 이탈이다. PASS/RUNNING/SKIP을 FAIL 사고로 만들지 않는다.
  - 활동 시각은 DB와 응답 모두 `Coalesce(completed_at, started_at)` 기준이다. 초기(seed) 요청은 이 시각의 최신 **40건**(`_SEED_LIMIT`)을 먼저 제한한 뒤 triage 집계하며 newest-first로 응답한다. 오래전에 시작했지만 방금 완료된 Run도 seed 선두에 잡힌다.
  - 유효한 `?after=<epoch>` 증분 요청은 ① 활동 시각이 after 이후인 Run, ② 시작 시각이 오래됐어도 현재 `RUNNING`인 Run(진행률 재전송), ③ `TestSuiteJobRun.triaged_at > after`인 완료 Run(FAIL→SKIP 같은 사후 판정 재전송)을 합쳐 **건수를 자르지 않고** `(ts,id)` 오름차순으로 보낸다. 따라서 한 poll 사이에 60건을 넘는 burst도 누락하지 않는다. triage 대상은 `Subquery`로 찾아 JOIN 중복과 Run별 N+1을 피한다.
  - `after`가 0 이하·15분보다 오래됨·비정상 문자열·Python datetime 범위 밖이면 전체 backlog를 읽거나 500을 내지 않고 최신 40건 seed와 `reset:true`를 보낸다. 브라우저는 이전 pool/주행 차량을 이 snapshot으로 교체한다. 한 Run이 여러 증분 조건을 동시에 만족해도 후보 id 기반이라 중복 응답하지 않는다.

- **브라우저 데이터/HUD**
  - `prime`이 seed를 적재하고, `poll`은 Run의 표시용 `ts`와 분리된 `pollCursor`에 마지막 성공 응답의 query-start `server_ts`만 저장한다. 6초마다 `dataUrl?after=max(1,pollCursor-10초)`로 짧게 replay하며 id 기반 갱신으로 중복을 흡수한다. 이 조합은 query snapshot 경계에서 commit된 실행을 다음 poll이 다시 포착한다. `fetchJSON`은 HTTP 성공·예상 redirect·JSON content type을 검증한다.
  - prime/poll은 공통 in-flight guard로 응답 역전을 막는다. 숨은 탭에서는 폴링과 단일 예약 rAF를 멈추고 복귀 즉시 하나의 rAF와 poll만 재개한다.
  - `addToPool`은 `seenIds`에 **Run id별 레코드 참조**를 보관한다. 같은 RUNNING Run의 진행률이나 terminal/triage 상태가 재전송되면 새 차량을 중복 추가하지 않고 pool·현재 주행 차량의 status/detail/label/ts/kind/href를 갱신한다. 상시 관제 탭은 주행 중 레코드를 먼저 보존하면서 비활성 오래된 항목부터 최신 80개로 정리하고, 화면 통계도 복제 차량 수가 아니라 이 고유 Run pool 기준이다.
  - `updateCommandHUD`가 PASS/RUNNING/SKIP/FAIL·전체 고유 Run 수와 최근 4건(`LATEST TRANSMISSIONS`)을 갱신한다. 최근 피드 행과 Canvas 차량은 모두 `runUrlTemplate`으로 Suite Run 상세 링크를 만든다.
  - `setNetworkState`는 CONNECTING/LIVE FEED/LINK LOST/STALE LINK를 구분한다. 마지막 성공 동기화 후 `pollMs × 2.5`(현재 15초)가 지나면 stale, fetch 실패면 offline이다. `setEmptyState`는 최초 loading, 정상 empty, 데이터 없는 offline error를 별도 문구로 표시하며 기존 pool이 있으면 연결 실패 중에도 마지막 관제 화면을 유지한다. interval이 다음 요청을 자동 재시도한다.

- **상호작용·접근성·성능**
  - 포인터 hover는 상태/Job 진행/상대 시각/상세 이동 툴팁을 띄우고 클릭·탭은 Run 상세를 연다. Canvas에 포커스하면 방향키로 차량을 순환 선택하고 Enter/Space로 열며 Escape로 해제한다(`pickCar`, `openCar`, `selectKeyboardCar`). 최근 피드는 일반 링크라 키보드/터치에서도 동일하게 이동한다.
  - 일시정지는 **애니메이션 시뮬레이션만** 멈추고 폴링/HUD는 계속 갱신한다. `prefers-reduced-motion`의 초기값과 런타임 변경을 추적해 프레임·속도·스폰 빈도를 낮추고 DOM/CSS 애니메이션도 축소한다. 빈 grid/reduced-motion/paused 상태는 `frame`의 렌더 간격을 낮춰 CPU를 절약한다.
  - `resize`는 `devicePixelRatio`를 1~2.5로 제한해 고해상도 Canvas를 만들고 차량 진행률·사고/피트 위치를 새 viewport에 맞춰 보존한다. window resize와 `ResizeObserver`를 `scheduleResize`로 합치며, 폭 560px 미만에서는 4차선→3차선으로 전환한다. 테마 변경은 `MutationObserver`로 pause 중에도 즉시 다시 그린다.

- **주요 튜닝 위치** (`static/js/qg_highway.js`)
  - 서킷 비율/차선: `roadTop`, `roadH`, `LANES`, `laneY`; 색/명암: `palette`, `drawScene`, `drawOverlay`.
  - 차량 밀도/속도: `spawnCar`의 `speed`·`fast`·`crashAt`·`pitAt`, `frame`의 `nextSpawn`; client 이력 상한: `addToPool`의 80.
  - 상태 연출: `updateCar`의 drive/pit/crash/wreck/tow 상태와 `drawCar`·`drawTow`; 안전거리/차선 변경: `planTraffic`, `laneClear`, `tryLaneChange`; 갓길 중첩 방지: `offRoadPhysics`.
  - rAF `dt`는 rAF timestamp만으로 계산하고 0.08초로 clamp한다. `performance.now()`를 섞거나 resize 때 차량 좌표 재보정을 제거하면 탭 복귀/반응형 전환에서 점프한다.

- **검증 방법**
  - 백엔드/계약: `.venv/bin/python manage.py test apps.visualization`; 전체 회귀: `.venv/bin/python manage.py test`(이번 개편 기준 458 tests). JS 구문은 `node --check static/js/qg_highway.js`로 먼저 확인한다.
  - 실제 동작은 `./run.sh` 후 로그인한 Chromium에서 `/visualization/`을 열어 데스크톱·390px 모바일, 라이트·다크, PASS/RUNNING/FAIL/SKIP, loading/empty/offline/stale, 통계·최근 피드, 차량 클릭/탭, 방향키+Enter, pause를 확인한다.
  - 애니메이션 캡처는 정적 `shot.py`/`--virtual-time-budget` 대신 SKILLS.md의 **CDP 실시간 캡처**를 쓴다. 데이터 없는 하니스는 `QGHighway.init({seed:[...]})`로 네 상태를 주입하고 수 초 뒤 캡처한다.

### "Test Suite stage 동그라미(dot) 갯수가 카운트(X/Y)와 안 맞음 / 빈 회색 dot"

stage dot 은 **반드시 `jr.display_stages_json`**(NOT_EXECUTED 제외) 을 순회해야 카운트
`stages_total`(= `len(display_stages_json)`) 과 일치한다. raw `jr.stages_json` 을 순회하면
`when{}` 조건으로 skip 된 NOT_EXECUTED stage 가 회색 dot(`--gray-300`) 으로 끼어들어
dot 갯수 > Y 가 된다(과거 버그). 적용 위치:
- `templates/suites/_run_status.html`, `templates/suites/queue.html` — dot 루프 + `{% if %}` 가드 모두 `display_stages_json`.
- `templates/qualitygates/_run_status.html` 는 원래부터 `display_stages_json` 사용(정답 패턴).
- 모델: `apps/suites/models.py:TestSuiteJobRun.display_stages_json`(NOT_EXECUTED 필터), `stages_total/done/running/current_number` 모두 이 필터된 집합 기준.

> **참고(사전 분석 불가)**: Jenkinsfile 을 미리 파싱해 'declared stage 수' 를 아는 건 비신뢰 —
> `when{}` 조건부 stage, scripted pipeline, parallel/matrix, Pipeline-from-SCM 때문에
> declared ≠ executed. wfapi 는 '실행된' stage 만 주므로 화면은 실행 기준(NOT_EXECUTED 제외)
> 으로 표시하는 게 정답. `TestSuiteJob.total_stages` DB 필드는 dot/카운트에 안 쓰임(메타만).

### "잡 실행 지연 시간(trigger_delay_seconds) / throttle 동작 변경"

- **지연 시간 편집**: `/suites/<pk>/edit/` POST — `trigger_delay_<job_id>` 숫자 입력. `apps/suites/views.py:suite_edit` 가 잡별로 저장 (빈값=기존 유지, 0 이상 클램프). Django admin 인라인(`TestSuiteJobInline`) 에도 노출.
- **throttle 단일 출처**: `apps/suites/throttle.py:claim_trigger_slot(suite, delay_seconds)`. suite 단위 조건부 UPDATE 로 슬롯 원자 점유 — 같은 suite 의 여러 폴링 스레드가 동시에 호출해도 정확히 1개만 통과. 서로 다른 suite 는 독립 슬롯.
  - 지연 판정 기준 변경 → `claim_trigger_slot` 의 UPDATE 조건식.
  - 기본 지연값 변경 → `DEFAULT_TRIGGER_DELAY_SECONDS` 상수.
  - 슬롯 실패 시 보류 상태 변경 → `_try_trigger_next` / `_trigger_all_pending` 의 WAITING 분기.
- **WAITING 상태 처리**:
  - 취소: `_abort_suite_run` 에서 WAITING job_run 은 Jenkins stop 없이 ABORTED.
  - idle 판정: `_trigger_single_idle_run` 에서 WAITING 도 busy 취급.
  - interval 타임아웃: `_timeout_unfinished_runs` 에서 WAITING 을 PENDING 과 동일하게 FAILURE 마감.
- **QG 파이프라인 제외**: `_try_trigger_test` 는 이 throttle 을 호출하지 않는다. 추후 적용하려면 `claim_trigger_slot` 을 import 해 `_try_trigger_test` 의 트리거 직전에 삽입.
- **테스트 위치**: `apps/suites/tests.py` `ClaimTriggerSlotTests` / `TriggerDelayGateTests` / `SuiteEditTriggerDelayTests`, `apps/robustness/tests.py` `ParallelTriggerDelayTests`.

### "Reliability Test 통계 초기화 동작" / "중지→재개해도 통계가 안 비워짐"

- 헬퍼: `apps/robustness/services.py:reset_schedule_stats(schedule)` — `started_at`/`last_triggered_at`/`stopped_at` 을 모두 **None** 으로 비운다. `is_running` 은 보존.
- `session_runs = runs.filter(triggered_at__gte=started_at)` 이고 `started_at=None` 이면 빈 쿼리셋이라 성공/실패/실행수/성공률/마지막 실행/세션 시작 표시가 깨끗하게 비어 보인다.
- running 인 스케줄을 reset 한 경우 다음 sweep 트리거에서 `_trigger_once` 가 `started_at` 을 자동으로 now 로 채워 그 이후 run 들이 정상 카운트된다. `_trigger_once`(`apps/robustness/services.py:242-244`) 시작에 `if schedule.started_at is None: schedule.started_at = now()` 1줄. 멈춰 있는 스케줄은 sweep 자체가 안 도니까 사용자가 다시 *시작* 을 누를 때 `start_schedule` 이 채운다.
- Row 별 reset: `robustness:schedule_reset` POST (`schedule_reset_stats` view).
- 일괄 reset: `robustness:schedule_reset_all` POST (`schedule_reset_all_stats` view). UI 는 *일괄 시작/중지* 옆의 *일괄 초기화* 버튼.
- **`start_schedule` (`apps/robustness/services.py`) 은 더 이상 매번 `started_at` 을 리셋하지 않는다** — 과거엔 시작(재개 포함)할 때마다 무조건 `started_at = now()` 로 덮어써서, `session_runs` 가 `triggered_at__gte=started_at` 필터로 이전 run 을 전부 걸러내 **중지 → 재시작만 해도 성공률/실행수가 0 으로 초기화**됐다. 현재는 conditional UPDATE 성공 후 `started_at` 이 NULL 일 때만 지금으로 채움 — 기존 세션이 있으면(즉 reset 되지 않은 채 중지만 한 경우) 그대로 유지해 재개 후에도 이어서 집계된다. `last_triggered_at` 은 이전과 동일하게 `None` 으로 비워 재개 직후 sweep 에서 즉시 1회 트리거되는 UX 는 그대로다. **통계를 실제로 비우는 것은 `reset_schedule_stats` 뿐**이고, `start_schedule`/`stop_schedule` 은 통계에 관여하지 않는다(단, `stop_schedule` 이 만드는 ABORTED 잡은 아래 `_job_aggregate` 에서 집계 제외됨).
- **`start_schedule` 은 race-free 멱등 (2026-07-16)** — `filter(is_running=False).update(...)` conditional UPDATE 로 **이미 실행 중이면 no-op + False 반환**. 과거엔 실행 중 스케줄에 재호출되면 `last_triggered_at=None` 리셋 → 직후 `sweep_schedule` 이 interval 도래로 오인 → **방금 트리거된 run 을 `_timeout_unfinished_runs` 가 '이전 사이클 미완료' 로 전 잡 FAILURE 마감**하는 사고가 났다(2026-07-16 `am-mapp-regression-api-stg` seq 643 — 개별 '시작' 3초 뒤 '일괄 시작' 이 같은 스케줄을 중복 시작). 호출부(`schedule_start`/`schedule_start_all`)는 False 면 sweep 을 건너뛰고 "이미 실행 중" 안내/카운트 제외. Slack `schedule_reactivate`(triage.py)도 같은 함수를 타므로 자동 보호.
- 회귀 테스트: `apps/robustness/tests.py:SchedulePauseResumeTests` — 재개 시 `started_at` 유지 + 통계 이어짐(`test_resume_keeps_started_at_and_continues_stats`), reset 은 여전히 비움(`test_reset_stats_clears_session`), 중지→reset→시작은 새로 채움(`test_start_after_reset_fills_started_at_again`), 중복 시작 no-op + lta 보존(`test_duplicate_start_is_noop_and_keeps_last_triggered_at`).

### "Reliability Test interval 도달 시 이전 사이클 마감 정책"

`apps/robustness/services.py:sweep_schedule` 이 interval 도달을 판정한 직후 `_trigger_once` 를 호출하기 직전에 `_timeout_unfinished_runs(schedule)` 를 호출. 동작:

- 해당 스케줄의 PENDING/RUNNING `RobustnessRun` 중 **트리거된 지 한 interval 이상 지난 것만** 수집(**나이 가드**, 2026-07-16 — `triggered_at <= now - interval`). 중복 시작/reset 직후 sweep 등으로 `last_triggered_at` 만 초기화돼도 방금 트리거된 멀쩡한 run 을 '이전 사이클' 로 오인해 죽이지 않는 2차 방어선(1차는 `start_schedule` 멱등화).
- SuiteRun 의 잡 중 `PENDING/QUEUED/RUNNING` 은 모두 `FAILURE` 로 마킹 (ABORTED 아님 — 사용자 의도가 "실패로 처리").
- `QUEUED/RUNNING` 잡은 `jenkins_build_number` 가 있으면 `client.stop_build(...)` 로 Jenkins 빌드 종료까지 시도. 실패해도 상태 마킹은 그대로 진행 (logger.exception).
- **증거 보존 (2026-07-16)**: 마감된 모든 잡의 `console_output` 에 `[interval timeout]` 사유(스케줄명/interval/마감 시각/마감 전 상태/빌드번호)를 남기고, 빌드가 있는 잡은 중단 시점까지의 **Jenkins 콘솔을 best-effort 수집**해 함께 저장 — Error Log 파싱/Run 상세에서 원인 확인 가능. 스케줄 단위로 `ActivityLog(action=CANCEL, user=None)` 1건 기록. "왜 실패인지 아무 로그도 없던" 무증거 실패 방지.
- 집계 갱신 (passed=SUCCESS 수, failed=FAILURE+ABORTED 수) 후 SuiteRun=`COMPLETED`, RobustnessRun=`FAILURE` 로 마감.

수동 트리거 (`schedule_trigger_now` 뷰) 는 이 로직을 거치지 않는다 — 사용자가 의도해서 또 돌리는 경우라 이전 사이클을 그대로 둔다. interval 기반 sweep 만 영향. 나이 가드 덕에 interval 중간의 수동 실행 run 도 다음 boundary 에서 한 interval 을 채우기 전엔 죽지 않는다.

테스트: `apps/robustness/tests.py:TimeoutUnfinishedRunsTests` — 갓 트리거된 run 생존(`test_young_run_survives`), interval 초과 run 마감 + 콘솔 사유/로그 + ActivityLog(`test_old_run_killed_with_evidence`).

`_abort_suite_run`(`apps/suites/views.py`, 중지/취소 경로)도 동일하게 **`[aborted]` 사유 + Jenkins 콘솔 best-effort 수집**을 `console_output` 에 남긴다 (2026-07-16).

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

### "Reliability Test 에 UI Automation(TAP) Suite 스케줄을 연결하고 싶다 / TAP 스케줄이 Jenkins 와 다르게 동작한다" (2026-07-28)

Reliability 스케줄이 Jenkins Suite 뿐 아니라 TAP(UI Automation) Suite 도 동일한 N분 간격 반복 트리거 대상으로 받도록 확장. 설계 근거: `docs/prd_ui_test_automation.md` §3.5(1차로 앞당김).

- **모델** (`apps/robustness/models.py:44`, 마이그레이션 `0007_robustnessschedule_tap_suite_and_more.py`) — `test_suite` 가 `null=True, blank=True` 로 바뀌고(과거엔 필수), `tap_suite`(`suites.TapSuite` FK, nullable) 가 추가됐다. `Meta.constraints` 에 `CheckConstraint(name="robustness_schedule_exactly_one_target")` — `test_suite`/`tap_suite` 중 **정확히 하나만** 채워져야 한다(DB 레벨 가드, `models.py:75-81`). TAP 쪽 유니크는 `UniqueConstraint(fields=["robustness_test", "tap_suite"], name="uniq_robustness_test_tap_suite")`(`:82-85`) — 기존 `unique_together=[("robustness_test", "test_suite")]`(Jenkins 쪽) 은 그대로 유지.
- **공용 표시 헬퍼** — `is_tap`(`:93`, `tap_suite_id is not None`) / `target_name`(`:97`, Jenkins 는 `test_suite.name` TAP 은 `tap_suite.name` — `__str__`/모든 뷰의 `ActivityLog` target_name/메시지가 `schedule.test_suite.name` 대신 이걸 참조하도록 전부 교체) / `latest_run`(`:103`, Jenkins: 최근 `RobustnessRun.suite_run`, TAP: 최근 `TapRun` — `_schedule_table.html` "마지막 실행" 링크가 대상별로 `suites:run_detail`/`suites:tap_run_detail` 로 분기하는 데 사용, §6.5).
- **세션 통계 분기** — `_stats()`(`:214`) 가 `is_tap` 이면 `_tap_aggregate()`(`:129`), 아니면 기존 `_job_aggregate()`(`:162`) 를 호출. `total_executions`/`success_count`/`failure_count`/`in_flight_count`/`trigger_count`/`success_rate` 프로퍼티(`:222-`)는 전부 `_job_aggregate()` 직접 호출에서 `_stats()` 경유로 바뀌었을 뿐 반환 딕셔너리 키(`total`/`passed`/`failed`/`in_flight`/`triggers`)는 Jenkins/TAP 양쪽이 동일해 화면 컬럼이 값 하나로 함께 보인다.
  - `_tap_aggregate()`(`:129`) — **케이스 수 기준**: `passed`/`failed` 는 세션의 `TapRun.passed`/`failed` 합계, `in_flight` 는 진행 중(`is_active`) run 의 `total_cases - completed_cases` 합(종결 run 은 0). **`stopped`(Aborted, 사용자 중지) run 은 그 시점까지의 pass/fail 만 집계하고 중단 자체는 실패로 안 침**(Jenkins 의 "ABORTED=장애 아님" 전역 정책 승계). **`error`(interval 타임아웃 마감) run 은 판정 못 받은 잔여 케이스를 `failed` 로 편입**(`:149-151`) — Jenkins 경로의 interval 타임아웃이 미완료 잡을 FAILURE 로 마킹해 실패 신호를 유지하는 것과 동일한 의도.
  - `session_tap_runs`(`:123`, `TapRun.robustness_schedule` 역참조 — `apps/suites/models.py:564` 신규 FK) — Jenkins `session_runs`(`:116`) 의 TAP 대응.
- **sweep 분기** — `apps/robustness/services.py:sweep_schedule`(`:530`) 진입 시 `schedule.tap_suite_id` 가 있으면 `_sweep_tap_schedule(schedule)`(`:506`) 로 위임하고 즉시 return(Jenkins 폴링/트리거 로직은 전혀 안 탐). `_sweep_tap_schedule` 골격은 Jenkins 경로와 동일한 4단계: ① 진행 중 `TapRun` 폴링(`services_tap.poll_tap_run`) ② race-free interval 판정 ③ 타임아웃 마감 ④ 새 트리거.
  - `_claim_trigger_turn(schedule)`(`:428`) — 기존 `sweep_schedule` 안에 인라인이었던 conditional UPDATE(위 "sweep race-free 트리거 결정" 항목) 를 **공용 함수로 추출**한 것뿐, 판정 로직 자체는 무변경 — Jenkins `sweep_schedule`(`:557`)과 TAP `_sweep_tap_schedule`(`:524`) 양쪽이 호출해 두 경로가 같은 스케줄을 동시에 트리거하는 일이 없다.
  - `_timeout_unfinished_tap_runs(schedule)`(`:450`) — Jenkins `_timeout_unfinished_runs`(`:120`) 대응. 트리거된 지 한 interval 이상 지난 `ACTIVE_STATUSES` TapRun 을 `cancel_tap_run` 으로 TAP 에 취소 요청 후, 로컬 상태를 `TapRun.Status.ERROR` 로 마감(**Jenkins 의 FAILURE 마킹과 동일한 의미** — `stopped`(Aborted) 가 아니라 `error` 로 남겨 실패 신호 유지) + `finished_at` 채움 + `ActivityLog(CANCEL)`. 나이 가드(`started_at`)는 Jenkins 경로와 동일.
  - `_trigger_tap_once(schedule)`(`:491`) — Jenkins `_trigger_once`(`:310`) 대응. `started_at` 없으면 채우고 `services_tap.trigger_tap_suite` 호출, `TapError` 는 `logger.warning` 만 남기고 스케줄 자체는 죽지 않음(다음 사이클에 재시도).
- **중지** — `stop_schedule`(`:82`) 이 `schedule.tap_suite_id` 이면 진행 중 TapRun 을 `cancel_tap_run` 요청하고 `is_running=False`+`stopped_at` 만 갱신한 뒤 조기 return(Jenkins 잡 abort 로직은 건너뜀). 시작(`start_schedule`)/reset(`reset_schedule_stats`)/멱등 가드는 대상 종류와 무관하게 완전히 공유(모델 필드만 보고 동작하므로 분기 불필요).
- **수동 실행** — `robustness/views.py:schedule_trigger_now`(`:356`) 가 `schedule.tap_suite_id` 로 `_trigger_tap_once` / 기존 `_trigger_once` 를 분기 호출.
- **화면 연동 지점** — `views.py:test_detail` 이 `available_tap_suites`(미연동 `TapSuite`) 를 계산해 "Test Suite 연동 추가" 폼에 넘기고, `schedule_add` 가 `tap_suite_id` POST(택1, `test_suite_id` 와 상호배타) 를 받아 `RobustnessSchedule.objects.get_or_create(tap_suite=...)` 로 생성. 프론트(2탭 폼/스케줄 표/실행 이력) 상세는 §6.5.
- **테스트**: `apps/robustness/tests.py` (TAP 스케줄 CheckConstraint, `_tap_aggregate` 집계, `_sweep_tap_schedule` 트리거/타임아웃/중지 분기 — 실 TAP 호출 없이 `services_tap` 함수 모킹).
- **바꾸려면**: 세션 통계 산식 → `_tap_aggregate`. sweep 골격(폴링/트리거 순서) → `_sweep_tap_schedule`. 타임아웃 마감 시 상태(`error` 대신 다른 값) → `_timeout_unfinished_tap_runs`. race-free 판정 자체(Jenkins·TAP 공용) → `_claim_trigger_turn`.

### "TAP 스케줄 집계가 0 — Running인데 케이스/트리거가 모두 0" (2026-09-09)

- **확인 순서**: `RobustnessSchedule.tap_runs` 전체/현재 세션 수 → `/tmp/qg_reliability_sweep.log`의 해당 스케줄 `트리거 실패` → TAP History의 Suite·시작 시각·세션 ID. `_claim_trigger_turn`은 HTTP 호출 전에 `last_triggered_at`을 갱신하지만 `services_tap.trigger_tap_suite`는 응답의 `session_id`를 받은 뒤에만 `TapRun`을 생성한다. 따라서 504/timeout은 Running과 이동하는 다음 실행 시각, 집계 0이 함께 나타날 수 있다. 원격 실행 여부가 미확정이므로 실패 응답만 보고 바로 재실행하지 않는다.
- **실사례**: 테스트 `2e7e070f-030e-493c-b475-29f3843db809`, 스케줄 `b89f27a3-0f5c-49f7-9a49-fabafb2e5e98`, `jk-dweb-regression-ui-dev`(TAP Suite 9). 169건 원문을 TAP의 실행 시작 API가 직렬 조회해 504가 발생했지만 원격 실행은 계속됐다. 포털 미러는 0건, TAP에는 2026-09-08 17:52 KST 이후 시간 간격과 일치하는 세션 2481~2525(홀수) 23건이 있었다.
- **재발 방지 수정 위치**: TAP 저장소 `apps/dashboard/views.py:_fetch_run_cases/_create_run_session`, `api_v2/routers/runs.py:create_run`. 선택 원문 최대 8개 병렬 조회 → 완전한 세션/결과 행 atomic 저장 → dispatch. 같은 169건의 원문 조회가 10.65초에 끝남을 확인했다. 포털 집계 산식·스케줄 주기·재시도 설정은 변경하지 않았다.
- **복구 범위/검증**: 원본 DB를 읽기 전용으로 열어 Suite 9·`via_api`·현재 세션 시작 이후인지 확인하고, TAP 상세 API 결과와 대조한 23건만 `TapRun.session_id` 기준 멱등 삽입했다. 원본 생성/종료 시각·상태·케이스 합불·결과 상세·링크를 보존하고 해당 스케줄 FK를 연결했다. 과거 실패 재시도·Slack 재알림은 실행하지 않았다. 재검사 누락 0건, 기존 화면 렌더링에서 **3,887케이스 / 트리거 23회**, 마지막 실행 상세 링크를 확인했다. 그중 마지막 세션은 진행 중이므로 합불·진행중 수는 정상 폴링으로 계속 갱신된다. 운영 DB는 기존 로컬 변경을 보존하며 Git 커밋에서 제외한다.

### "Reliability Test 화면이 느림 — sweep 위치 / 쿨다운 / 통계 쿼리"

증상: `/robustness/` 목록·상세 진입이 느림. 원인은 **동기 sweep(Jenkins 직렬 폴링, 호출당 최대 8초)이 페이지 로드 경로에 있었던 것**. 현재 구조:

- **`test_list` (`apps/robustness/views.py`)**: sweep **안 함**. 목록은 `schedule_count`/`running_count`(DB count)만 보여주므로 Jenkins 폴링 불필요. (불필요하던 `prefetch_related` 도 제거)
- **`test_detail` GET**: sweep **안 함**. DB 상태로 즉시 렌더. 과거의 `sweep_active_schedules()` + `refresh_from_db()` 제거.
- **라이브 갱신**: `templates/robustness/test_detail.html` 의 fragment `hx-trigger="load, every 10s"` → `test_detail_fragment` 가 비동기로 `sweep_active_schedules()` 호출 후 `_schedule_table.html` 만 swap. 첫 페인트는 즉시, 최신값은 첫 라운드트립 직후.
- **sweep 쿨다운**: `apps/robustness/services.py:sweep_active_schedules(*, force=False)` 가 `cache.add(_SWEEP_COOLDOWN_KEY, 1, _SWEEP_COOLDOWN_SECS=5)` 로 5초 창 안의 중복 호출을 1회로 합친다(다중 탭/유저 폴링이 SQLite 쓰기 락을 물고 늘어져 포털 전체가 느려지는 것 방지 — README §10). 명시적 사용자 액션(start/stop/trigger)은 `sweep_schedule`/`_trigger_once` 를 직접 호출하므로 쿨다운 무관. `force=True` 로 쿨다운 우회 가능.
- **통계 쿼리**: `RobustnessSchedule._job_aggregate()`(`apps/robustness/models.py:77-125`) 를 인스턴스 단위 memoize + `Count('id')`(=`trigger_count`) 합산 → 화면 한 행이 쓰던 `total_executions`/`success_count`/`failure_count`/`in_flight_count`/`success_rate`/`trigger_count` 6쿼리를 **2쿼리**로 축소(과거 1쿼리였으나 아래 ABORTED 제외를 위해 분리).
  - **ABORTED 잡은 total/failed 에서 제외**된다 — `TestSuiteJobRun.Status.ABORTED` 인 job_run 은 그 경로(중지로 인한 강제 종료든, 스케줄이 도는 중 Jenkins 쪽에서 빌드가 취소된 것이든)를 가리지 않고 대상. 단 **사람이 verdict=failure 를 명시 확정한 ABORTED 는 장애로 인정해 빼지 않는다**(필터에 `~Q(verdict="failure")`). `suite_run.failed_jobs` 에는 FAILURE+ABORTED 가 합산돼 있는데, 중단은 제품 실패가 아니므로(전역 **"ABORTED=장애 아님"** 정책 — §3 `untriaged_pending`/`annotate_triage`, 대시보드 결과 분포와 동일 기준) 그대로 두면 **중지/취소가 성공률을 깎는** 문제가 있었다. 1차 aggregate(`Sum(suite_run__total_jobs/passed_jobs/failed_jobs)`, `Count('id')`)와 별개로 2차 aggregate(`session_runs` 전체에 `Count('suite_run__job_runs', filter=job_run status=ABORTED & ~verdict=failure)`)를 돌려 그 수를 total/failed 양쪽에서 뺀다.
  - **제외 범위는 (과거엔) `RobustnessRun.status=ABORTED`(= `stop_schedule`/queue 일괄중지가 세팅) 로 한정돼 있었으나 현재는 전역 정책 적용으로 그 한정을 없앴다** — 스케줄이 도는 중 Jenkins 쪽에서 빌드가 취소돼(폴링 결과 ABORTED) run 이 FAILURE 로 끝난 경우도 이제 동일하게 제외한다("ABORTED(중단)로 끝난 것은 장애로 보지 않는다" 결정에 따라 Reliability 통계도 다른 화면과 기준을 맞춤). interval 타임아웃 마감처럼 **FAILURE 로 마킹된 잡**은 여전히 실패로 집계되므로 Reliability 가 감지해야 할 이상 신호(잡이 안 끝남)는 계속 잡힌다.
  - **왜 쿼리를 2개로 나눴나**: `Sum(suite_run 필드)`와 `job_runs` 조인 `Count`를 한 aggregate 에 섞으면 조인 fan-out(잡이 여러 개면 SuiteRun 행이 잡 수만큼 복제)으로 `Sum` 값이 부풀기 때문. memoize(`self._job_agg_cache`)는 그대로 유지되므로 행당 총 쿼리는 6→2(과거는 6→1). 두 쿼리의 정합은 RobustnessRun↔TestSuiteRun 1:1(`_trigger_once` 에서만 생성) 불변조건에 의존한다.
  - interval 타임아웃 마감(`_timeout_unfinished_runs`)은 미완료 잡을 FAILURE 로 마킹하므로(ABORTED 아님) 이 제외 대상이 아니고 여전히 실패로 집계된다.
  - **실행 이력 표와의 차이**: `schedule_runs.html` 의 run 별 Pass/Fail 칸은 `suite_run.passed_jobs/failed_jobs` 원시값(ABORTED 포함)을 그대로 보여주므로, abort 된 run 이 있으면 상단 세션 통계 카드(제외 반영)와 합이 다를 수 있다 — run 행의 Aborted 배지로 구분.
  - 테스트: `apps/robustness/tests.py:JobAggregateMemoizeTests.test_stats_use_shared_two_queries`(쿼리 수 2 확인), `SchedulePauseResumeTests.test_stop_aborts_inflight_without_hurting_success_rate`(중지 ABORTED 제외 확인), `SchedulePauseResumeTests.test_jenkins_side_abort_also_excluded_from_stats`(Jenkins 측 취소도 제외되는지 확인 — 과거 이름 `test_jenkins_side_abort_still_counts_as_failure` 에서 정책 flip 과 함께 개명), `SchedulePauseResumeTests.test_aborted_with_explicit_failure_verdict_counts_as_failure`(Failure 확정 ABORTED 는 실패로 집계).

바꾸려면: sweep 빈도/쿨다운 → `_SWEEP_COOLDOWN_SECS`. 어디서 sweep 할지 → 위 3개 뷰 + fragment 의 호출 위치. 통계 합계 식·ABORTED 제외 로직 → `_job_aggregate`.

### "Reliability Test 가 설정 interval 대로 안 돈다 / 간격이 들쭉날쭉" ★

증상: 60분 interval 인데 실제 트리거 간격이 61분/120분/957분/1189분처럼 제각각. **원인**: Reliability Test 는 Celery beat 같은 별도 스케줄러가 없고, 트리거는 `sweep_active_schedules()` 가 **호출되는 시점에만** 진행된다. 과거엔 그 호출이 화면 폴링(HTMX fragment)에서만 일어나, **누군가 Reliability Test 상세 화면을 열어둔 동안에만** 스케줄이 돌았다 → 아무도 안 보면 interval 이 지나도 트리거가 안 되고, 다음에 누가 화면을 열 때 한꺼번에 돈다(그래서 간격이 들쭉날쭉). 위 "화면 느림" 최적화로 sweep 위치를 fragment 로 좁히면서 이 의존이 더 두드러졌다.

**해결(현재 구조)**: `apps/robustness/management/commands/sweep_reliability.py` 를 **cron 으로 매분 호출**해 브라우저와 무관하게 sweep 한다. 각 스케줄 interval 도래 여부는 `sweep_schedule` 내부 conditional UPDATE(`last_triggered_at` vs `interval_minutes`)가 race-free 하게 판정하므로, 매분 호출해도 각 스케줄은 자기 interval 에만 1회 트리거. 명령은 `sweep_active_schedules(force=True)`(쿨다운 우회) 호출 + 끝에 데몬 스레드(Slack 비동기 발송) join(짧은 cron 프로세스가 즉시 종료돼 알림이 잘리지 않게).

- crontab(한 번만 등록, 담당자 동기화 cron 과 같은 위치): README §10 참고. `flock -n` 으로 중첩 방지.
- 화면 fragment 의 sweep 은 그대로 둔다 — 보고 있을 때의 **라이브 갱신용**(스케줄링 신뢰성은 cron 이 보장). 둘 다 같은 `sweep_active_schedules` 를 호출하지만 per-schedule race-free 판정이라 중복 트리거 없음.
- **TAP Suite 재시도 (2026-07-30)**: `TapSuite.retry_count`(기본 1, 포털 로컬 설정 — TAP 싱크가 안 덮음, 마이그 0017) + `TapRun.attempt`/`retry_origin`(self FK). `poll_tap_run` 의 terminal 전이에서 completed+failed>0 이고 attempt<retry_count 면 `_retry_tap_run` 이 전체 재실행 트리거(시작 알림 미발송, ActivityLog RETRY) — 중간 실패는 Slack FAIL 미발송, 최종 시도만 발송. error/stopped 는 재시도 안 함('순수 FAILURE 만 재시도' 정책 승계). 세션 통계(`_tap_aggregate`)는 재시도된 run(뒤 시도가 있는 run)을 제외해 **최종 시도만 집계**. Edit 화면 `suites:tap_suite_edit`(admin, retry_count 만). 테스트: `apps/suites/tests.py:TapRetryTests`(5)/`TapSuiteEditViewTests`(5).
- **TAP Run Slack 알림 (2026-07-29)**: 이벤트 3종 `TAP_RUN_STARTED/PASSED/FAILED`(`apps/notifications/events.py`, 토글 필드 `notify_tap_run_*` — 마이그 0003, 기본 FAIL 만 ON). 발송 `send_tap_run_event(_async)`(`apps/notifications/services.py`) — 케이스 수 기준 본문, FAIL 은 실패 케이스 최대 5건(TestRail 링크)+`외 n건` + **UI 알림 담당자(`UiAutomationOwners`, 담당자 관리 > UI 탭) top-level 멘션**(2026-10-02, 스케줄 실행 포함 — TAP 케이스는 URL 매칭 개념 밖이라 통 설정, §3 `UiAutomationOwners`). 훅: `trigger_tap_suite`→STARTED, `poll_tap_run` 의 **active→terminal 전이 1회**에 completed→PASS/FAIL·error→FAIL·stopped→silent(취소=명시적 액션, Jenkins abort 와 동일 정책), `_timeout_unfinished_tap_runs`→FAIL(타임아웃=실패 신호). 테스트: `apps/suites/tests.py:TapRunSlackNotifyTests`(6종).
- **단독 Suite Run 폴링도 이 명령이 겸한다 (2026-07-21)**: UI/API 로 트리거된(robustness 스케줄에 안 묶인) RUNNING `TestSuiteRun` 을 매분 `_poll_run_status` 로 폴링. 과거엔 화면(HTMX)·외부 폴러(api_v2 status)가 유일한 폴링 주체라 **외부 폴러가 죽으면 완료 감지·Slack 알림이 다음 접속 때까지 지연**됐다(2026-07-21 am-mapp-regression-api-dev seq 1284 — 39초 빌드가 24시간 43분 뒤 FAIL 알림). robustness 소속 run 은 스케줄 sweep 이 폴링하므로 제외(이중 폴링 방지). 테스트: `apps/robustness/tests.py:SweepReliabilityCommandTests.test_command_polls_standalone_running_runs_only`.
- **완료 시각은 Jenkins 실제 종료 시각으로 backdate (2026-07-21)**: `_poll_run_status` 가 잡 완료(SUCCESS/FAILURE 확정/ABORTED) 시 `completed_at` 을 폴링 시각(now)이 아니라 `build_info.timestamp+duration`(`_jenkins_build_end`, `apps/suites/views.py`) 으로 기록하고, SuiteRun 마감도 마지막 잡의 `completed_at`(Max) 으로 찍는다 — 폴링이 늦어도 Slack '소요 시간' 이 부풀지 않음. timestamp 미제공 시 now() 폴백. 테스트: `apps/suites/tests.py:PollBackdatesCompletionTests`(3종).
- cron 미등록이 의심되면: `tail -f /tmp/qg_reliability_sweep.log` / `crontab -l` 확인.

### "Reliability Test 실행 모드 — 순차 / 병렬(`parallel_jobs`) / 단일(`single_job_mode`)"

**테스트별** (RobustnessTest 단위) 3가지 모드. `_trigger_once` 분기:
- **순차**(둘 다 False, 기본): Suite 의 활성 잡 전체를 한 잡씩 — `_try_trigger_next`.
- **병렬**(`parallel_jobs`): 전체 잡 — `_trigger_all_pending`(throttle 로 delay 간격 엇갈림).
- **단일**(`single_job_mode`): 전체가 아니라 **idle 잡 1개만** — 단독 idle 경로와 동일하게 `pick_idle_job`(busy 제외 + Jenkins building 체크)로 1개 선택해 그 잡만 suite_run 에 담아 `_try_trigger_next`. idle 이 없으면 그 사이클 skip(`return None`). **단일·병렬은 상호배타** — 단일 ON 시 병렬 강제 OFF, UI 병렬 버튼 disabled + 서버 `test_toggle_parallel` 도 단일 ON 이면 no-op.
- 토글: `robustness:test_toggle_single`(`test_toggle_single`, 단일 ON 시 parallel_jobs=False 동시 저장) / `test_toggle_parallel`. 버튼은 `_schedule_table.html` 의 "잡 병렬 실행" 옆 "단일 테스트". 모델 `RobustnessTest.single_job_mode`(마이그레이션 0006).

(이하 parallel 관련 상세)

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

#### 역방향 기록 (write-back, 2026-06-24) — 수동 등록분을 시트에 append
- **목적**: 동기화는 시트→DB **단방향**이라 Error Log 모달 "+ 등록"(수동 row, `is_imported=False`)은 시트에 안 남았다. 이걸 팀이 보는 시트에도 남기기 위해 등록 직후 **시트에 1행 append** 한다. 다음 동기화 때 같은 `(method, endpoint)` 가 시트에서 다시 읽혀 그 row 가 `is_imported=True` 로 흡수되므로 중복 안 생김.
- **흐름**: `api_owner_create` 저장 직후 `services.writeback_owner_to_sheet_async(obj)` → daemon thread → `google_sheets.append_owner_row(values)`. **best-effort** — 실패해도 등록 자체엔 영향 없음(로그 warning). 등록 응답은 append 를 기다리지 않음(비동기).
- **양식 보존**: `append_owner_row` 가 시트의 **실제 헤더 행을 읽어 그 순서대로** 값을 배치(`HEADER_MAP`/`_canonical_header` 로 헤더↔필드 매핑). 시트에만 있는 컬럼(예: 비고)은 빈칸, 헤더에 없는 모델 필드(notes)는 생략. `valueInputOption=USER_ENTERED` 라 `${BFF_URL}` 등은 원형 보존(앞글자가 수식 트리거 아님), 사용 횟수는 숫자로.
- **읽기와 다른 점**: 읽기는 Drive API(`drive.readonly`), **쓰기는 Sheets API(`spreadsheets` 스코프)** + `_build_session(scopes=_SHEETS_RW_SCOPES)`. 첫 탭(gid=0) 제목 → 헤더행(`values.get`) → `values.append`.
- **활성화 3요건** (셋 다 충족해야 동작):
  1. **`.env` 에 `GSHEET_OWNERS_WRITEBACK=1`** (기본 off=opt-in; `is_owner_writeback_enabled()`). + 서버 재기동.
  2. SA(`get_service_account_email()`, 현재 `qg-gdocs-reader@team-qpe.iam.gserviceaccount.com`)가 시트에 **'편집자'**.
  3. **GCP 프로젝트에 Google Sheets API 활성화** (`sheets.googleapis.com`). ← 읽기(Drive API)만 켜져 있으면 append 가 HTTP 403 "Sheets API ... disabled" 로 실패. `gcloud services enable sheets.googleapis.com` 또는 콘솔에서 enable.
- **안전 가드**: `_is_test_run()`(`'test' in sys.argv` / `PYTEST_CURRENT_TEST`)이면 env 가 켜져 있어도 **append 안 함** — 테스트가 운영 시트를 건드리지 않게. 단위 테스트(`OwnerSheetWritebackTests`)는 세션을 mock 해 네트워크 없이 헤더순서/`${}`보존/옵트인/가드 검증.
- **미적용**: 편집(`api_owner_edit`)·삭제는 write-back 안 함(append 만). 시트 행 갱신/삭제는 행 탐색이 필요해 보류.
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
- view: `apps/integrations/views.py` — `api_owner_list/create/sync/edit/delete/export`, `api_host_list/create/edit/delete`, `owner_sync_settings_save`, `ui_owners_save`. `api_owner_list` 는 **내부 탭 2개** — `?tab=` 없음=API Endpoint 담당자(기존 화면 전체), `?tab=ui`=UI 알림 담당자(§3 `UiAutomationOwners`). `api_owner_sync` / `owner_sync_settings_save` 는 **POST only** (`@require_POST`) — 처리 후 list 로 redirect. (구 `api_owner_import` 폼 view 는 2026-05-27 제거.)
- 라우팅: `apps/accounts/urls.py` 가 `from apps.integrations import views as int_views` 로 위임 등록. URL name 은 `accounts` namespace 안에 (`accounts:api_owner_list`, `accounts:api_owner_sync` 등).
- 권한: 모든 view 가 `@login_required` + `_require_admin(request.user)` 가드.
- 폼: `apps/integrations/forms.py` 의 2개 — `ApiEndpointOwnerForm` / `ApiHostMappingForm` (ModelForm). (`ApiOwnerImportForm` 은 CSV 업로드 제거와 함께 삭제.)
  - `ApiEndpointOwnerForm` **레포/카테고리/영역은 드롭다운**(`__init__` 에서 DB distinct 값으로 `ChoiceField` 동적 구성 — 시트 통제 어휘에 맞춰 일관 등록). `repo` 만 required, category/area 는 빈 옵션 허용. 편집/prefill 중인 현재 값은 distinct 목록에 없어도 choices 에 포함돼 보존. **`usage_count` 는 필수 아님**(`required=False`, `clean_usage_count` 가 빈값→0). 드롭다운 값을 늘리려면 시트에 row 가 생겨 동기화되면 자동 반영(통제 어휘에 없는 값은 폼이 거부 — 빈값으로 두면 됨).
- 탬플릿: `templates/accounts/api_owner_list.html` / `api_owner_form.html` / `_api_owner_modal.html`(Error Log 모달) / `api_host_list.html` / `api_host_form.html`. `api_owner_list.html` 헤더의 'Google Sheets 동기화' POST 버튼 + 시트 링크/자동 동기화 주기/마지막 동기화/공유 주체 이메일 안내, 하단에 **'자동 동기화 주기' 카드** (분 입력 폼 → `owner_sync_settings_save`) + '대체 담당자' 카드. 이 전부가 API 탭 분기이고, `_admin_tabs.html` 아래 **링크형 내부 탭**(`nav.event-tabs` + `a.event-tab-btn`, `?tab=ui`) 으로 UI 탭 분기(UI 알림 담당자 카드 → `ui_owners_save`)와 전환한다 (`a.event-tab-btn` 스타일은 `static/css/style.css`, 2026-10-02 캐시버스트 `?v=20261002a`). `_admin_tabs.html` 에 2개 탭 추가됨. `base.html:50-56` sidebar active 조건에도 `api_owner_*` / `api_host_*` url_name 추가. (구 `api_owner_import.html` 삭제.)
- list view context: `last_sync` (최근 `API_OWNER_IMPORTED` ActivityLog), `sheet_url`, `sa_email`, `sync_interval_minutes` — 화면 상단 안내 + 주기 폼 prefill. (`last_sync.detail` 은 `생성 N · 수정 N · 수동 보존 N · 삭제 N · 오류 N` 한국어 포맷 — `services.py` 의 ActivityLog 기록부에서 생성.)
- 페이지네이션 — `Paginator(queryset, 50)`. GET `page`. ActivityLog `views.py:23-24` 패턴 그대로.
- 필터 (GET 파라미터): `q` (전체 LIKE), `repo`, `mapped_app`, `team`, `is_imported`.
- export.csv 는 UTF-8 BOM (`b"\xef\xbb\xbf"`) 한 번만 prepend. content_type=text/csv. 헤더는 한국어 12개 그대로.
- **Error Log "+ 등록" = 모달** (2026-06-24): Error Log 페이지(`qualitygates/error_log.html`·`suites/error_log.html`)의 매칭 실패 행 "+ 등록"(admin only)이 페이지 이동 대신 **그 자리 모달**(`accounts/_api_owner_modal.html`)을 `openOwnerModal({method,endpoint,host})` 로 띄움 — 뒤의 에러 로그를 보면서 등록. 모달 폼은 `owner_form`(=`ApiEndpointOwnerForm()`, admin 일 때만 context 주입; `_render_error_log`·`job_run_error_log`)을 렌더하고, fetch 로 `accounts:api_owner_create` 에 `X-Requested-With: XMLHttpRequest` POST → `api_owner_create` 가 **JSON**(`{ok:true}` / 400 `{ok:false, errors}`)으로 응답, 성공 시 `location.reload()` 로 표 갱신. 모달 UI 는 섹션 그룹(대상 API / 분류 / 담당자) + 그리드 + 통일 인풋 스타일(`.om-*` 클래스, self-contained `<style>`, 라이트/다크·모바일 반응형) + 부가필드(`매핑 앱/사용 횟수/Step IDs/비고`)는 `<details>` "추가 정보 (선택)" 로 접음. JS 검증 에러는 escape 후 표시(XSS 방지), 401/403 분기, 재오픈 시 errBox 초기화 + 레포 select 자동 포커스.
- standalone create 페이지(`/accounts/api-owners/new/?method=...&endpoint=...&host=...`)도 유지 — 비AJAX POST 는 기존대로 list 로 redirect. host 는 모델 필드가 없어 모달 상단 안내로만 노출(필요 시 endpoint 에 `${BFF_URL}` prefix 수동 추가).
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
- `extract_urls()`: **① `ERROR_URL:` 마커 우선** (2026-06-24~) — 사내 자동화 스크립트가 실패 step 의 URL 을 단일·유니크하게 표기한 라인(`_ERROR_URL_RE`, 단어경계 보장). 로그에 하나라도 있으면 *그것만* 권위 소스로 쓰고, method/status 는 같은 로그 상세 블록(`URL:`+`Method:`+`Status:`)에서 같은 URL 을 찾아 보강(없으면 GET/None), count = 마커 출현 횟수(=실패 횟수). → 변수 host-only(`BFF_X_URL=https://host`)·성공 step·노이즈 URL 자동 배제. **② 마커가 전혀 없으면(구 포맷 fallback)** `URL:`/`URL :`/단독 `https?://\S+` 를 블록 단위로 인식 + 같은 (method,url) dedupe+count (기존 동작 그대로). 블록 구분은 `===` 5+ / `@@@` 5+ / 빈 줄 3+, 결과는 count 내림차순 → url 사전순.
- Method 매칭 — **정확한 메서드 우선, 없으면 메서드 무관 fallback** (`find_endpoint_owner`, 2026-06-24): 먼저 호출 메서드의 풀에서 path 매칭, 그게 **0건일 때만** 같은 path 를 *다른* 메서드로 등록한 owner 로 fallback 한다. 시트가 endpoint 를 한 메서드(예: GET)로만 등록했는데 테스트가 다른 메서드(예: POST)로 호출해 매칭 실패하던 케이스 방어 (실제 사례: `${BFF_PERSONAL_URL}/member/users/edit-by-pop-up` 가 시트엔 GET, 호출은 POST). **정확한 메서드 매칭이 있으면 fallback 하지 않으므로** 같은 path 의 GET/POST 별도 owner 구분은 그대로 유지. ⚠️ path 는 같은데 메서드별 담당팀이 진짜 다른 드문 경우엔 등록된 쪽으로 귀속되니, 정확히 하려면 시트에 메서드별 row 를 추가.

#### 단위 테스트 (`apps/integrations/tests.py`)
- 상세는 §"Error Log 화면 통합" 뒤의 "단위 테스트" 항목 참조 (총 25 개).

#### Error Log 화면 통합 (`_render_error_log`, `job_run_error_log`)
- `apps/qualitygates/views.py:_render_error_log` 와 `apps/suites/views.py:job_run_error_log` 가 **`extract_urls(error_log or "")`** 를 호출해 `matched_urls` 컨텍스트를 채움. console 원본 전체가 아니라 `extract_error_log(...)` 의 추출본(추출된 에러 카드와 같은 텍스트) 안의 URL 만 매칭 대상으로 좁힌다 — 정상 step 의 URL 까지 표에 나오면 누가 F/U 해야 할지 한눈에 안 들어와 사용자가 명시적으로 좁혀달라 요청 (2026-05-20).
- 템플릿 `templates/qualitygates/error_log.html`, `templates/suites/error_log.html` 의 "추출된 에러" 카드 위에 카드 제목 **"이슈 F/U 담당자"** 로 표 렌더. 표 컬럼: URL / Method / 호출 횟수 / Status / **카테고리 / 영역** / FE 담당자 / BE 담당자 / 액션(에러 노티 + admin 은 모달 "+등록"). 카테고리·영역은 매칭된 `m.owner.category`/`m.owner.area`, 미매칭 행은 `-` (colspan 으로 9컬럼 정렬 유지). 컬럼명은 `4xx/5xx 담당자` 로 통일 (팀 정보는 한 셀 안에 `이름 (팀)` 형태).
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
- `UrlMatcherTests` 10 — AM env-var / JK 절대경로 / `${RECRUIT_NO}` 와일드카드 / Method strict / **cross-method fallback** / **정확한 메서드 우선(fallback 양보)** / Longest specificity / 매칭 없음 / 호스트 매핑 없이도 동작 / host_var tie-break.
- `ExtractUrlsTests` 14 — **`ERROR_URL:` 마커: 실제 Jenkins 샘플(host-only 변수 배제+method/status 보강+owner 매칭) / 노이즈보다 우선 / 실패횟수 count** / `URL:` 라벨 + 단독 형태(구 포맷) / dedupe + count / count 정렬 / 긴 separator backtracking 회귀 방지 / Slack 자동 매칭 + plain fallback (`build_mentions`) / `_split_names` 콤마 처리 / `_names_for_status` 4xx vs 5xx / `excerpt_error` 길이 + 백틱 escape / `api_owner_notify_error` POST → 302 + ActivityLog / `slack_users._build_name_index` deleted/bot 제외 + 5개 이름 키 / `_name_head` 팀 접미사 제거 + `이름(팀)` → `이름` 키 자동 등록.

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

### "단독 Suite Run 트리거 모드(idle 1개 / 전체 체인 / job_id 지정)를 바꾸고 싶다"

- **핵심 분기**: `apps/suites/services.py:trigger_suite_run(*, suite, user, job_id="", run_all=False)`.
  - idle 선택 로직 변경 → `apps/suites/views.py:pick_idle_job(suite, jobs)` 수정. Jenkins `lastBuild` 조회 결과로 idle 판정하며, 조회 실패(연결/인증 오류 등)는 건너뜀. 판정 기준을 바꾸려면 이 함수만 수정.
  - idle 없을 때의 에러 코드/메시지 변경 → `_trigger_single_idle_run` 안의 `SuiteRunError("all_workers_busy", 409)` 부분.
  - 전체 체인 트리거 로직 변경 → `_trigger_chain_run`.
  - 단일 job_id 트리거 → `_trigger_single_job_run`.
- **API 경로**: `apps/api_v2/views/suites_v2.py` 가 `request.data` 에서 `job_id`/`run_all` 을 꺼내 `trigger_suite_run` 으로 넘긴다. body 파라미터 추가 시 이 뷰에서 추출 + `trigger_suite_run` 시그니처 확장.
- **201 응답 `total_jobs`**: `apps/api_v2/views/suites_v2.py` 가 `suite_run.total_jobs` 를 직접 직렬화. 응답 필드 추가 시 같은 파일 수정.
- **에러 코드 `all_workers_busy`**: `apps/api_v2/exceptions.py` 의 코드 카탈로그에 추가돼 있어야 프론트엔드/외부 자동화가 에러 구조를 정규화해서 받을 수 있다.

### "테스트 Suite 가 외부 Jenkins 라 다른 자격증명을 써야 한다"
- `TestSuite.jenkins_base_url/jenkins_user/jenkins_api_token` 채우기 → `suite.get_jenkins_client()` 가 `is_internal=False` 인 외부 클라이언트를 반환 → Basic auth + `trigger_build` 경로 사용 (`apps/suites/models.py`).
- 내부 default Jenkins(SSO 뒤)를 쓰려면 비워두면 됨 → `is_internal=True` → 익명 read + GWT 트리거.

### "내부 Jenkins 워커에 GWT 토큰을 등록/재발급하고 싶다"
- Suite 상세(`/suites/<pk>/`) "GWT 토큰" 카드 (admin + `is_internal=True` 인 suite 한정).
  - "전체 발급" → `gwt_tokens_generate` 뷰(`apps/suites/views.py:103`) — `gwt_token=""` 인 활성 워커에 일괄 발급.
  - 워커별 "재발급" → `gwt_token_regen` 뷰(`apps/suites/views.py:124`) — 해당 워커만 재발급(재발급 후 Jenkins 워커 GWT Token 도 갱신 필요).
- 토큰 값을 바꾸는 로직: `TestSuiteJob.gen_gwt_token()` (`apps/suites/models.py:186`) — `qgwt_` + `secrets.token_hex(20)`.
- Jenkins 워커 설정: Configure → Build Triggers → Generic Webhook Trigger → Token 에 포털 발급 토큰과 동일한 값을 넣어야 트리거가 연결된다. **불변식: Jenkins 워커의 GWT `<token>` == 포털 DB `TestSuiteJob.gwt_token`** (불일치하면 그 워커만 트리거 실패). 단일 진실원천은 포털 DB.
  - **주의 — `Token Credential` 칸은 비워둔다**. 거기에 크레덴셜(예: `SLACK_BOT_TOKEN_API_TEST`)을 지정하면 GWT 가 평문 `<token>` 을 무시하고 그 *크레덴셜 값*을 토큰으로 써서 `Did not find any jobs with GenericTrigger configured` 가 난다 (2026-06-24 5개 워커 전부 이 상태였음). config.xml 상으로는 `<tokenCredentialId>` 가 없거나(`MISSING`) 빈 값이어야 정상.
  - **익명 config.xml 로 일괄 점검/수정** (taeshkim 이 anonymous 에 Job/Configure 부여한 동안만 가능): 읽기 `GET .../job/<folder>/job/<worker>/config.xml` 는 익명 200. 쓰기는 `POST` 에 **CSRF crumb(`/crumbIssuer/api/json`, 쿠키 동일 세션) + `Content-Type: text/xml; charset=utf-8`** 필수 — charset 빠지면 HTTP 500("Oops!"). 작업 끝나면 **anonymous Job/Configure 권한은 즉시 회수**(보안).
  - **전 내부 Suite 일괄 셋업 완료 (2026-06-24)**: 18개 내부 Suite·활성 워커 60개 전부 GWT 토큰 발급 + Jenkins GenericTrigger 등록 완료. 잡 구조가 3가지여서 케이스별 변환을 적용했다 — **(A)** GenericTrigger 이미 있음(완전형) → 블록 교체로 `<token>`=포털값·`<tokenCredentialId/>` 비움. **(B1)** `PipelineTriggersJobProperty` 는 있고 `<triggers/>` 빔 → 그 안에 GenericTrigger 삽입(= UI "Generic Webhook Trigger" 체크 ON). **(B2)** `PipelineTriggersJobProperty` 자체 없음 → `<properties>` 에 PipelineTriggersJobProperty+GenericTrigger 통째 삽입. 표준 GenericTrigger 블록은 `silentResponse=true`(invoke 응답 빈 바디가 정상)·`tokenCredentialId` 빈 element. 비활성 워커(`*-prd` 의 `_2`~`_5`)는 Jenkins 잡 자체가 없어(404) 제외. 검증: 60/60 재조회로 토큰·cred·트리거 위치 확인 + 케이스별 dev 워커 invoke 시 자기 워커만 큐 적재 확인.

### "Test Suite Jenkins 자격증명을 중앙에서 관리/수정하고 싶다"
- `/suites/credentials/` (admin 전용) — 내부/외부 자격증명을 DB 에 저장.
- 모델: `apps.suites.models.SuiteJenkinsCredential` (`kind` 가 unique, `internal` / `external` 2개 row 만 존재).
- 내부 row 는 `JenkinsClient()` 의 `base_url` 폴백으로 사용 (env 변수보다 우선). **단, 내부 Jenkins 는 이제 읽기가 익명 + 트리거가 GWT 라 user/api_token 이 실질적으로 안 쓰인다** — QG 파이프라인 전환 전까지는 기존 row 그대로 유지.
- 외부 row 는 `/suites/register/` 폼이 자동으로 채우는 default 값.
- **API token 평문은 화면에 절대 노출하지 않는다** — `SuiteJenkinsCredential.token_masked` 로 prefix 4자 + `*` 만 표시. 입력칸 비운 채 저장하면 기존 값 유지, 값 입력 시에만 갱신.
- 진입: `/suites/` 상단 `Jenkins 자격증명` 버튼 (admin 만 노출).

### "대시보드 차트 데이터 산식/범위를 바꾸고 싶다"
- 위치: `apps/dashboard/views.py:index` (컨텍스트 변수 구성 전부 이 함수). 집계 기간 상수 `WINDOW_DAYS = 14` (모듈 최상단) 을 바꾸면 카드·차트·분포 모두 반영.
- **일별 추이 (14일)**: `_compute_daily(now)` — `started_at` 기준으로 최근 14일을 날짜별 group. `real_failed=0` → pass, `real_failed>0` → fail. Skip 분류된 실패와 **ABORTED(중단)뿐인 실패**(2026-07-14, `real_failed` 산식에 반영) 는 둘 다 real_failed 계산에서 빠지므로 pass 쪽으로 집계된다(장애 아님).
- **Suite 별 Pass Rate (14일)**: `_compute_suite_stats(now)` — `TestSuiteRun` 을 suite 별 group, 실행 수 내림차순 상위 10. `avg_duration_sec` 는 completed 행의 `completed_at - started_at` 평균. (구 30일 → 2026-07-08 14일로 일원화)
- **결과 분포 (14일, Job 단위)**: `_compute_result_dist(now)` → 튜플 `(dist_quality, dist_skip)`. **Suite Run 단위가 아니라 Job(worker) 단위 집계**. `dist_quality={pass, fail}` (pass=success Job, fail=**status=FAILURE** & verdict!=skip(미분류 포함) **+ status=ABORTED & verdict=failure**(명시 확정)). `dist_skip={pass, skip}` (skip=**status=FAILURE·ABORTED** & verdict=skip). **미분류 ABORTED(중단) Job 은 어느 도넛에도 세지 않는다**(2026-07-14 "ABORTED=장애 아님" 전역 정책 — 단 사람이 verdict 를 명시 확정한 ABORTED 는 그 판정을 따라 집계). 두 도넛의 pass 는 동일. (구 `_compute_dist` → `_compute_result_dist` 로 함수명 변경·시그니처 변경)
- **미 장애 판정 대기 수**: `TestSuiteJobRun.untriaged_pending().count()` — 기간 무관, 컷오프 이후분만.
- **최근 15건**: `recent_suite_runs` — **최근 활동 순**(`Coalesce(completed_at, started_at)` 내림차순) 상위 15건, `select_related('suite', 'triggered_by')`. ⚠️ **started_at 이 아니라 완료 시각 기준** (2026-07-07): long-running run 이 뒤늦게(예: read-timeout 으로 1~2h 후) 완료돼 실패하면, `started_at` 정렬에선 시작 시각 자리에 묻혀(그 사이 다른 run 이 많이 시작되면 최근 15 에서 밀려남) 방금 실패한 run 이 대시보드/목록에 안 보이던 문제. Slack FAIL 알림은 완료 시점에 오는데 목록은 시작 순이라 불일치했음. **Suite Runs 목록(`suite_run_list`)·Suite 상세 이력도 동일 정렬**.
- 차트 색상/임계 변경: `templates/dashboard/index.html` `extra_js` 블록 내 각 `buildXxx()` 함수.

### "대시보드 최근 실행 표에 게이트별 누적 성공률 컬럼이 어떻게 채워지나" (레거시 — 현재 대시보드는 Suite Run 표로 교체됨)
- 2026-06-10 이후 대시보드는 `TestSuiteRun` 기반 표를 사용. 이전 `_compute_gate_lifetime_stats` 함수와 `recent_runs` 컨텍스트 변수는 views.py 에서 제거됨 (backend 작업과 동시 진행).

### "`/ci-api/`(게이트웨이 로그인 예외 경로) 동작을 바꾸거나 엔드포인트를 노출/제외하고 싶다" (2026-10-12)
- 새 엔드포인트가 ci-api 에도 노출되는 규칙: `urls_meta.py`/`urls_runs.py`/`urls_controls.py`/`urls_suites.py` 에 추가하면 `ci_api_patterns` 를 통해 `/api/v2/` 와 `/ci-api/` 양쪽에 자동 노출된다. **문서·export·세션 로그인이 필요한 것은 `urls.py` 의 `urlpatterns` 에만** 둔다(ci-api 는 로그인 없이 열리는 경로 — 그곳에 두면 보호 해제와 같다).
- 경로 접두 변경: `CI_API_PREFIX`(`apps/accounts/middleware.py:114`) + `config/urls_ci.py` 의 마운트 + nginx 예외 경로를 **함께** 수정. 뷰 쪽 `reverse("api_v2:…")` 접두는 `request.urlconf` 가 결정한다.
- 인증 강화/변경: 키 파싱(Bearer/Basic) `apps/api_v2/authentication.py`, scope `apps/api_v2/scopes.py`·`permissions.py`, Rate limit `throttling.py`. 쿠키·`X-Auth-*` 제거 위치는 `CiApiBoundaryMiddleware`(`SessionMiddleware` 앞 순서 유지 — `config/settings/base.py:75`).
- 테스트: `QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= GATEWAY_ENTRY_ENABLED= TRUSTED_PROXY_CIDRS= QGP_BASE_URL= .venv/bin/python manage.py test apps.api_v2.tests.test_ci_api`.

### "Swagger/ReDoc 에 보이는 주소·경로 표기(`/ci-api/`)를 바꾸고 싶다" (2026-10-15)
- 경로 표기 치환 규칙: `apps/api_v2/schema.py:rewrite_paths_to_ci_api`(POSTPROCESSING_HOOK, `settings.SPECTACULAR_SETTINGS["POSTPROCESSING_HOOKS"]`) — `paths` 하위만 `/api/v2/` → `/ci-api/` 재귀 치환. 표기를 원래대로(`/api/v2/`) 돌리려면 settings 에서 이 훅을 빼면 된다(뷰·라우팅은 건드린 적 없음). 접두(`/ci-api/`)를 바꾸면 `CI_API_PREFIX`·`config/urls_ci.py`·nginx 예외(위 `/ci-api/` 항목)와 이 훅·`swagger_ui.html` 배지의 치환 문자열을 **함께** 수정.
- 소개문(상단 Base URL 표·예시·401 안내): `config/settings/base.py` 의 `SPECTACULAR_SETTINGS["DESCRIPTION"]`. 표시 호출 주소는 `settings.CI_API_BASE`(`QGP_BASE_URL` + `/ci-api`) — 값은 `.env` `QGP_BASE_URL` 로 바꾼다.
- 키 검증 배지가 호출하는 경로: `templates/api_v2/swagger_ui.html`(`/api/v2/` → `/ci-api/` 치환 후 `me`). API Key 발급 완료/목록 화면의 호출 예시: `templates/accounts/api_key_created.html`·`api_key_list.html` + context processor `ci_api_base`(`apps/accounts/context_processors.py`).
- 테스트: `apps.api_v2.tests.test_docs`(`SchemaCiApiNotationTests`).

### "서버 이관 Export API(`/api/v2/export/…`) / `fetch_snapshot_via_api` 동작을 바꾸고 싶다" (2026-10-09)
- 권한·스코프 규칙(누가 호출 가능한가): `apps/api_v2/scopes.py`(`EXPORT_READ`·`EXPLICIT_ONLY_SCOPES`·`has_scope`), `apps/api_v2/permissions.py:ExportPermission`, 발급 폼 노출 `apps/accounts/views.py:_scope_options_for`. `*` 가 export 를 포함하도록 **바꾸지 말 것**(명시 전용이 계약).
- 엔드포인트·응답·상태머신(`running→ready|failed→expired`)·동시 1개·24h 만료·Range/헤더: `apps/api_v2/views_export.py`(위 "DB 스냅샷 내보내기 API" 의 함수별 라인), 라우팅 `urls_export.py`. 허용 필드를 늘릴 때는 `SnapshotCreateView.post` 의 `extra` 검사를 고치되 **config 계열은 계속 거부**(`include_config=False` 는 `_run_export` 에 고정).
- 다운로드 허용 파일 추가: `ALLOWED_DOWNLOAD_NAMES`/`DB_FILE_NAMES`(서버)와 `ALLOWED_NAME_RE`(클라이언트 `fetch_snapshot_via_api.py:34`)를 **함께** 수정.
- 만료 시간/stale 판정: `TTL_SECONDS`·`TOMBSTONE_SECONDS`·`STALE_HEARTBEAT_SECONDS`. 번들 위치: `SYNCOPS_EXPORT_DIR`(`base.py:181`).
- 새 서버 쪽 재시도·이어받기·검증 로직: `fetch_snapshot_via_api.py` 의 `_download`/`_consume`/`_wait_ready`/`MAX_RETRIES`/`RETRY_BACKOFF`. 서버·클라이언트 계약(헤더·상태 값·오류 코드)을 바꾸면 `docs/api_guide.md` §4.5·§7.1a, `docs/prd_server_migration_sync.md` §5.5·§6.3 경로 C 와 두 테스트 파일을 같이 고친다.
- **새 DB 모델·마이그레이션을 만들지 않는다**(원본 DB 스키마 불변이 설계 전제; 상태는 번들 안 `state.json`).

### "로그인/로그아웃 동작(Keycloak 전용 로그인 페이지)을 바꾸고 싶다"
- 로그인 화면 문구·버튼·알림: `templates/accounts/login.html` + `.login-*` 스타일(`static/css/style.css`). 컨텍스트 계약은 `sso._render_login`(`form/next/providers/local_login/error/logged_out/gateway_entry_url`). 버튼 문구는 `PROVIDER_LABELS`(`sso.py:54`), 진입 모드의 "Keycloak 로그인" 버튼은 `login.html` 의 `gateway_entry_url` 블록. CSS 를 고치면 `base.html` · `login.html` 의 `?v=` 토큰을 함께 bump.
- 로그아웃 이동 규칙 (a')(a)(b)(c): `sso.logout_view`(`sso.py:255`), 게이트웨이 이동 HTML 은 `sso._gateway_logout_page`(`:218`) — 302 로 되돌리지 않는다(Location 재작성 404). `id_token` 은 `logout()` 전에 읽는다. Keycloak 세션 종료 on/off 는 코드가 아니라 `OIDC_INTERNAL_POST_LOGOUT_REDIRECT`.
- 계정 연결/자동 생성 규칙(역할·이메일 신뢰): `sso._resolve_user`(`sso.py:438`), 신뢰 도메인은 `OIDC_INTERNAL_EMAIL_DOMAINS`.
- 게이트웨이 헤더 신뢰·세션 정합: `middleware.ProxyBoundaryMiddleware`/`GatewaySessionMiddleware`, `TRUSTED_PROXY_CIDRS`.
- 게이트웨이 진입 모드(버튼·진입 경로·로그아웃 복귀): `sso.gateway_entry_view`, `accounts/urls.py` 의 `gateway-login/`, `GATEWAY_ENTRY_ENABLED`(`base.py:312`). 게이트웨이 라우팅 요청 문안·켜는 순서는 `docs/handoff_sso_integration.md` §10.
- 새 공급자 추가: `settings.OIDC_PROVIDERS`·`OIDC_PROVIDER_KEYS`, `PROVIDER_LABELS`, 필요 시 `GATEWAY_REALM_PROVIDERS`. URL 은 `oidc/<provider>/…` 가 이미 일반화돼 있다.

---

## 11. 트러블슈팅

| 증상 | 확인 | 조치 |
|---|---|---|
| `/qg` 에서 Nginx `404` | 실제 적용 설정에 `/qg` 라우트가 있는지, 관리 콘솔 저장 후 적용됐는지 | `/qg/` 라우트·백엔드·접두 경로 제거 설정을 확인하고 대상 Nginx 에 적용. Django 404 면 백엔드로 전달된 경로도 확인. |
| SSO 로그인 후 `400` 로그인 오류 화면("로그인을 완료하지 못했습니다", `sso_failed`) | `journalctl -u qualitygate` 의 `SSO 콜백 실패`/`SSO 토큰 검증 실패`/`SSO 이메일을 신뢰할 수 없어…`/`여러 계정과 일치`/`기존 계정에 다른 SSO 연결이 있어…`/`SSO 사용자 처리 실패` 경고(공급자·예외 타입만 남는다) | 상태값 불일치(세션 만료·다른 브라우저로 콜백)·사용자 취소 → 다시 시도. issuer/redirect URI/시크릿 불일치면 `.env` 와 Keycloak client(§8 신청 값) 대조. 이메일 미제공·신뢰 불가 도메인이면 `OIDC_INTERNAL_EMAIL_DOMAINS` 또는 `email_verified` 확인. `SSO 시작 실패`(`sso_unavailable`)는 Keycloak 메타데이터 조회 실패 — issuer 주소·네트워크 확인. |
| SSO 로그인 후 `403`(`sso_inactive`, "비활성화되었거나 승인 대기 중인 계정") | 연결 대상 계정 `is_active` | 비활성 계정은 SSO 가 활성화하지 않는다 — 관리자가 사용자 관리에서 활성화. |
| 기존 계정으로 SSO 로그인했는데 역할이 viewer 인 새 계정이 생김 / **이메일 불일치** | 기존 계정 `email` 이 비었거나 Keycloak 이메일과 다른지(대소문자는 무시, 철자·도메인은 정확히), 같은 이메일 계정이 2명 이상인지 | 이메일이 정확히 1명과 일치해야 연결된다. Django admin 에서 기존 계정 이메일을 Keycloak 이메일로 고친 뒤, **새로 만들어진 계정을 삭제/비활성화**하고 다시 로그인(새 계정에 이미 SSO 연결이 있으면 먼저 Django admin `External identities` 에서 그 연결 삭제 — superuser 만 삭제 가능). 연결 후엔 이메일이 바뀌어도 `(issuer, sub)` 로 로그인된다. |
| **기존 SSO 연결이 있는 계정에 다시 연결 불가**(`기존 계정에 다른 SSO 연결이 있어 연결하지 않음` 경고, 400) / 중복 계정 | 해당 계정의 `ExternalIdentity`(같은 issuer, 다른 sub) — 이메일 재사용·Keycloak 계정 재생성 의심 | 의도된 안전장치(중복 viewer 계정을 만들지 않는다). **복구: 운영자가 Django admin → `External identities` 에서 그 계정의 기존 연결을 삭제**하면 다음 SSO 로그인 때 이메일로 다시 연결된다. 새 Keycloak 계정이 정말 같은 사람인지 확인한 뒤 삭제. |
| SSO 로그아웃 후 비밀번호 없이 바로 재로그인됨 / 로그아웃 때 Keycloak 오류 화면 | `OIDC_INTERNAL_POST_LOGOUT_REDIRECT` 값, Keycloak client 의 Valid post logout redirect URI, 세션에 id_token 이 있는지(비밀번호로 로그인했으면 없음) | 재로그인됨 = 플래그가 꺼져 (c) 로 폴백한 것 → client 에 `https://<호스트>[/qg]/accounts/login/` 등록 후 `True`. Keycloak 오류 화면 = 등록 URI 와 불일치(쿼리·경로·호스트 정확히 일치해야 함) 또는 미등록 → 등록 확인 또는 플래그를 `False` 로. `SSO 로그아웃 주소 생성 실패` 경고는 메타데이터 조회 실패로 (c) 폴백한 것. |
| 게이트웨이(qw) 경유인데 로그인 화면이 반복되거나 세션이 매번 풀림 | `TRUSTED_PROXY_CIDRS` 에 게이트웨이 Nginx 출발지가 들어 있는지(없으면 `X-Auth-*` 가 제거돼 자동시작·세션 정합이 동작 안 함), `LOCAL_LOGIN_ENABLED=True` 로 비밀번호 로그인한 계정의 이메일이 게이트웨이 사용자와 다른지 | CIDR 정정 후 재시작. 자체 로그인+게이트웨이 헤더 동시 사용 시 `GatewaySessionMiddleware` 가 이메일 불일치 세션을 폐기하는 것은 설계된 제약(§7) — 자체 로그인은 직접 접속에서만 사용. |
| 로그아웃 후 `404`(경로가 `/qg/_qw/logout` 으로 보임) | 앱 로그의 `/_qw/logout` 404 요청 경로, 로그아웃 응답이 302 인지 HTML 인지 | 게이트웨이가 `/qg/` 응답의 `Location` 을 고쳐 쓴 것(추정). 현재 코드는 `_gateway_logout_page`(HTML)로 이동하므로 재현되면 구버전 코드가 떠 있는 것 → `sudo systemctl restart qualitygate`. HTML 이동도 404 면 게이트웨이의 `/qg/_qw/...` 라우팅·재작성 규칙을 QA 인프라팀에 확인. |
| 진입 모드(`GATEWAY_ENTRY_ENABLED`)에서 "Keycloak 로그인" 후 503 `sso_unavailable` 화면 | 앱 로그의 `게이트웨이 진입 경로에 인증 헤더가 없음` 경고, 게이트웨이에서 `/qg/accounts/gateway-login/` 가 internal 로 보호돼 있는지, `TRUSTED_PROXY_CIDRS`(빈 값 키 여부 포함) | 라우팅 미설정이면 인프라팀 요청(handoff §10). 출발지가 다르면 §5 절차로 `/32` 재확정. 급하면 `GATEWAY_ENTRY_ENABLED` 제거(또는 `LOCAL_LOGIN_ENABLED=True`) 후 재시작. |
| `/qg/` 에서 `502 Bad Gateway` | Nginx error log 의 upstream 연결 오류, `systemctl status qualitygate`, `ss -ltn` 의 8601 리슨, 컨테이너에서 백엔드 접근 | 서비스 로그 확인 후 `sudo systemctl restart qualitygate` (수동 환경은 `./run.sh`). 계속 실패하면 Docker 호스트 매핑·네트워크·백엔드 포트를 점검. |
| `/qg/ci-api/…` 호출이 HTML 401 / JSON 401 | 응답 본문이 nginx HTML 인지 JSON(`error.code=not_authenticated`)인지 | HTML 401 = 게이트웨이(nginx)에 `/qg/ci-api/` 로그인 예외가 적용되지 않음(인프라 확인). JSON 401 = 앱 도달 — 키 헤더(`Bearer qgp_...` 또는 Basic 비밀번호 자리의 `qgp_` 키)·만료/회수·scope 확인. 아이디/비밀번호 Basic 은 인증되지 않는다. `/ci-api/docs/`·`/ci-api/export/…` 404 는 의도(문서·export 는 `/api/v2/` 에서만). |
| 프록시 접속에서 Django `400` / `DisallowedHost` | 전달된 Host 와 `DJANGO_ALLOWED_HOSTS` | 기존 허용 목록에 접속 호스트(예: `172.20.0.221`, `qw.jkintra.net`)를 추가하고 재기동. 프로토콜·경로·포트는 넣지 않는다. |
| `/qg/` 화면 CSS/JS `404` 또는 로그인 후 루트로 이동 | `QGP_SCRIPT_NAME`, HTML의 정적 파일 URL, Nginx 접두 경로 제거 | `/qg` 설정 후 재기동. HTML은 `/qg/static/...`, 백엔드는 `/static/...` 이어야 한다. DEBUG 정적 라우트 또는 별도 정적 서빙 확인. |
| 페이지가 멈춤 | runserver 가 한 요청에 막힌 상태 | `sudo systemctl restart qualitygate` (수동 환경은 `./run.sh`). 외부 호출(Jenkins 8s, Argo CD 6s)이 줄줄이 실패하면 한동안 밀린다. |
| Jenkins 파라미터 모달이 비어 있음 | Job 이 Active Choices / Git Parameter 사용 + 토큰 권한 부족 | `JenkinsClient` 토큰에 Overall/RunScripts 권한 부여하거나 HTML 스크래프 경로가 동작하는지 빌드 폼 직접 확인 |
| deploy/prod 모달에서 APP_IMAGE 가 빈 텍스트박스로 뜸 | luxair `ImageTagParameterDefinition` 으로 choices=[] → 콘솔 추출도 실패한 경우 | `run.image_console_output` 에 jib `--target=` 라인이 있는지 확인. 없으면 create-image 빌드가 실패했거나 콘솔이 저장 안 된 상태. create-image 빌드 재실행 후 콘솔 저장 여부 재확인. |
| deploy/prod 모달 APP_IMAGE 가 select 가 아닌 텍스트박스로 뜨고 submit 비활성화 | `resolve_and_apply_app_image` 가 False 반환 (콘솔 추출 + prefix 폴백 모두 실패) | `QualityGateRun.image_console_output` 와 `image_build_number` 를 DB 에서 확인. 콘솔에 `--target=` 라인이 있으면 `extract_pushed_image_tag` 시뮬레이션 (`jenkins.py:866`) 으로 태그 추출 시도. |
| Argo CD 버튼이 안 보임 | `*_argocd_synced_at` 이 None | 정상 — Synced + Healthy 도달 시점에만 노출. Sync 실패/Degraded 시에도 Retry 버튼은 자동 노출 |
| Argo CD 가 계속 "Syncing..." 만 나옴 | `*_argocd_operation_phase`/`sync_status`/`health_status` 값 | 배지가 sync/health/operation 조합으로 분기되도록 변경됨 — `Operation: Succeeded` 인데 `health=Progressing` 이면 *롤아웃중*, `sync=OutOfSync` 이면 *OutOfSync — 재동기화 대기*. 그래도 진행이 없으면 15분 후 timeout. |
| 검수대상 배포가 완료되기도 전에 테스트가 시작됨 (직전 revision 으로 테스트) | Run 상세의 deploy 단계 Argo CD 가 "Done" 인데 실제 ArgoCD 화면은 sync 진행 중 | `_check_argocd_sync` 가 첫 트리거(refresh+sync) 직후 같은 호출에서 polling 해 직전 Run 의 `Synced+Healthy` 가 그대로 잡혔던 게 원인. 첫 호출은 polling skip + 이후 폴링에서도 `operationState.finishedAt` 이 `sync_requested_at` 이전이면 stale 로 거름. 새 Run 부터 정상 동작 — 보고된 케이스에서 다음 sync 트리거가 시작되기 전까지 deploy 단계는 *동기화중* 으로 유지된다. |
| ArgoCD 가 Synced+Healthy 라는데 새 ReplicaSet pod 가 ContainerCreating | 사용자 ArgoCD UI 에서 Pod 가 sync 중인데 Portal 은 완료 표기 | ArgoCD 의 application-level health 는 Deployment 단위라 새 Pod 가 readiness probe 통과 전에도 Healthy 로 보고될 수 있다. `_check_argocd_sync` 가 (1) `resource-tree` 호출해 자식 Pod 모두 Healthy 검증 + (2) 30초 grace period 후에만 진짜 완료 판정. 그 사이 health 가 다시 떨어지면 healthy_first_at reset. |
| Argo CD sync 가 즉시 `Sync 실패` 로 떨어짐 | `*_argocd_sync_error` 의 phase + Argo 메시지 | `_check_argocd_sync` 가 `operation_phase ∈ Failed/Error` 면 곧바로 `(PROD_)DEPLOY_FAILED` 전환. 매니페스트/이미지/리소스 누락이 흔한 원인. UI Retry Sync 로 즉시 재시도 가능. |
| Run 상세에 `sync 실패: HTTP 400 ...` 빨간 글씨가 떴다 사라진다 | 최초 sync 트리거가 `409`/`400` (이미 동기화 중/auto-sync) 으로 떨어졌을 때 채워지는 임시 메시지 | 정상. UI 는 `show_deploy_argocd_error` / `show_prod_argocd_error` 프로퍼티로 **최종 실패 상태**(`(PROD_)DEPLOY_FAILED`) 에서만 sync_error 를 노출하도록 좁혀져 있음. 폴링 중 임시 메시지는 logger.warning 으로만 남음. |
| OS 알림이 안 옴 | base.html 우하단 **OS 알림 테스트** → 진단 결과 | macOS: 시스템 설정 → 알림 → Chrome → 배너/경고 + 집중모드 OFF. Windows: 알림 + 알림 배너 표시 ON. Chrome 탭이 완전히 닫히면 워처가 죽음 — 백그라운드/최소화는 OK. |
| `gate_type` 변경 안 됨 | 의도된 제약 | 새 게이트로 등록할 것. 자격증명/Argo CD 환경 매핑이 달라지므로 변경 시 silent breakage 위험. |
| 검수통과 배포 모달 default 가 잘못됨 | APP_IMAGE 가 이 Run 의 이미지 태그로 설정됐는지 | `prod_deploy_params` 가 `resolve_and_apply_app_image(parameters, run)` 으로 콘솔 추출 태그를 default+choices 에 주입 (`views.py:892`). 이미지 빌드 단계가 없는 게이트면 그대로 첫 choice. luxair choices=[] 문제도 콘솔 추출로 해결됨. |
| Argo CD 가 `yaml: line N: mapping values are not allowed in this context` 로 sync 실패 (자동 트리거) | GitOps 저장소의 `deployment.yaml` 에 `image: registry/foo:` 처럼 빈 태그가 푸시되었는지 | 자동 deploy/prod 트리거 시 ImageTagParameter / Active Choice 의 default 가 비어 있어 Portal 이 빈 태그를 명시 전송하던 게 원인. `_collect_default_params` 가 빈 문자열을 dict 에서 제외하도록 수정됨 (`views.py`). 이미 푸시된 깨진 매니페스트는 데브옵스가 GitOps 저장소에서 직접 정정하거나 다음 정상 Run 으로 덮어써야 함. |
| Argo CD 가 `yaml: line N: mapping values are not allowed in this context` 로 sync 실패 (수동 모달) | 수동 배포 모달에서 APP_IMAGE 에 빈 태그(`registry/foo:`)가 선택된 채 submit 됐는지 | **2026-06-02 사고** (Run `bca67034-...`, auth-worker #30). 3중 방어로 재발 차단: ① 모달 열기 시 `resolve_and_apply_app_image` (구: `_override_app_image_default`) 가 이 Run 의 콘솔 추출 태그(폴백: 이미지 prefix)로 default 오버라이드 + 매칭/추출 실패 시 submit 버튼 비활성화. ② 모달 JS submit 가드(`_isValidAppImage`)가 브라우저에서 차단. ③ 서버 `_validate_app_image_param` (`services.py:346`)이 HTTP 400 반환. 이미 푸시된 깨진 매니페스트는 GitOps 저장소에서 직접 정정. |
| Auto deploy 가 `MissingPropertyException: No such property: APP_IMAGE for class: WorkflowScript` 로 실패 | Jenkins 빌드의 ParametersAction 에 APP_IMAGE 가 빠져 있는지 | ImageTagParameter 의 choices 가 트리거 시점에 비어 들어왔거나 (`_scrape_build_form_choices` 가 일시적으로 빈 결과 반환 등) HTML scrape 가 실패해 `_collect_default_params` 가 빈 값으로 판단해 dict 에서 제외했고, Jenkins 는 빠진 파라미터를 plugin default 로 채우지 않아 Jenkinsfile 의 `APP_IMAGE` 참조가 미바인딩이 됐던 케이스. `_auto_trigger_deploy` 가 `run.image_build_number-` 로 시작하는 choice 를 사전에 default 로 오버라이드해서 명시 전송하도록 수정됨 (`views.py`). choices 자체가 비어 매칭이 불가능한 경우엔 자동 진행을 보류하고 `WAITING_DEPLOY` 로 전환해 사용자에게 모달을 띄움. |
| db.sqlite3 머지 충돌 | 2026-10-28 이전 커밋(DB 가 Git 에 포함되던 시절) 기준으로 pull 할 때 | 현재는 `.gitignore` 대상 — 로컬본을 유지하고 `git rm --cached db.sqlite3` 로 추적만 해제. |
| `fetch_snapshot_via_api` 가 404 `not_found` (HTML 404 포함) | 원본에 Export API 코드가 배포·재시작됐는지(`/api/v2/export/snapshots/<uuid>/` 가 JSON `not_found` 인지 HTML 404 인지), `--url` 의 접두 경로(`/qg` 등) | 원본에 이 저장소 변경이 반영돼야 한다(`git pull` + 서비스 재시작). 접두 경로가 있으면 `--url https://host/qg` 처럼 포함. |
| Export API 호출이 401/403 (`scope_required`/`role_denied`) | 키에 `export:read` 가 **명시**됐는지(`*` 만으로는 안 됨), 키 소유자가 admin(`qpe_admin`/superuser) 인지, 만료·회수 여부 | admin 이 `/accounts/api-keys/new/` 에서 `export:read` 키를 새로 발급(소유자 강등 시 기존 키도 차단됨). 키 평문은 발급 직후 1회만 보인다. |
| Export API 409 `export_in_progress` / `export_not_ready` | 다른 스냅샷이 실행 중인지(`GET …/<id>/` 의 `status`), 다운로드 대상이 `ready` 인지 | 하나만 동시에 실행. 끝나길 기다리거나 `--existing-id` 로 기존 id 재개. 서버가 죽어 `running` 으로 남았으면 조회 시 5분(하트비트)·pid 판정으로 `failed` 가 되어 새로 만들 수 있다. |
| 다운로드가 중간에 끊김 / 재실행하면 처음부터 받음 | `<out-dir>/.fetch-<id>/*.part` 가 남아 있는지, 서버가 Range 를 무시(200)하는지(프록시/ALB) | 같은 명령(또는 실패 메시지의 `--existing-id` 명령)으로 재실행하면 `.part` 를 Range 로 이어받는다. 원격 스냅샷은 24시간 뒤 만료(`expired`)되므로 그 전에 끝낸다. sha256 불일치 시 `.part` 를 지우고 처음부터 받는다. |
| 파라미터 캐시가 오래됨 | 120초 이상 안 지났는데 Job 변경 사항이 반영 안 됨 | 모달의 새로고침 버튼 = `?refresh=1` 호출 |
| 내부 Suite 트리거가 `JenkinsError ... Did not find any jobs with GenericTrigger configured` | 워커 잡 config.xml 의 GenericTrigger 에 `<tokenCredentialId>` 가 박혀 있는지 / `<token>` 이 포털 DB `gwt_token` 과 일치하는지 | ① `<tokenCredentialId>` 제거(비우면 저장 시 element 자체가 사라짐) → 평문 `<token>` 사용. ② Jenkins `<token>` 을 포털 DB 값으로 맞춤. 둘 다 §9 "GWT 토큰 등록/재발급" howto 참조. invoke 성공 검증 = `/queue/api/json` 또는 워커 `lastBuild` 빌드번호 증가. |

---

## 12. 안 쓰는 / 레거시 코드

리팩토링 시 참고 (지금은 그대로 두지만, 신 파이프라인과 무관):

- `drawBillboards` (`static/js/qg_highway.js`) — 구 Tron/네온 빌보드용 Canvas 헬퍼. Live Race Control의 `drawScene`은 메가시티 plate + 절차적 서킷을 사용하며 이 함수를 호출하지 않는다.
- `_override_app_image_default` (`apps/qualitygates/views.py:46`) — 구 prefix-only 매칭 헬퍼. `deploy_params` / `prod_deploy_params` 의 호출처는 `resolve_and_apply_app_image` 로 교체됨. 함수 정의는 `tests.py:16` 의 import 보호로 유지 (삭제 시 테스트 모듈 전체 collection 실패).
- `_compute_dora`, `_dora_grade_df`, `_dora_grade_lt`, `_dora_grade_cfr`, `_dora_grade_mttr` (`apps/dashboard/views.py`) — **b71c09f 이후 커밋(2026-06-10)에서 삭제됨**. 대시보드 개편으로 DORA 4지표 산식 전체 제거. 복원은 `git log --all apps/dashboard/views.py` 로 커밋 찾아 `git show <hash>:apps/dashboard/views.py` 로 확인.
- `_compute_dist` (`apps/dashboard/views.py`) — **2026-07-08 삭제**. Suite Run 단위 30일 pass/fail/running 3버킷 집계 함수. `_compute_result_dist` (Job 단위, 14일, 2종 도넛) 로 교체. 컨텍스트 키도 `dist` → `dist_quality`/`dist_skip` 로 변경.
- `_compute_gate_lifetime_stats` (`apps/dashboard/views.py`) — **동상(2026-06-10) 삭제**. 게이트별 누적 성공률 집계 함수. `recent_runs` 컨텍스트 변수 및 QG/QualityGateRun import 도 함께 제거됨. 대시보드 표는 `recent_suite_runs` (TestSuiteRun 기반)로 교체.
- `apps/gates/` — 구 GateRun 모델 (DRF API 가 이걸 쓴다)
- `apps/services/`, `apps/approvals/`, `apps/escalations/` — PRD 단계의 placeholder
- `apps/integrations/slack.py`, `apps/integrations/webhooks.py`, `apps/integrations/tasks.py` — 구 GateRun 기준. **신규 Slack 알림은 `apps/notifications/` 사용** (§4.4)
- `apps/api/` — 구 모델 기반 DRF API (`/api/v1/...`)
- `config/celery.py`, `CELERY_*` 환경변수 — `TASK_ALWAYS_EAGER` 라 실질 미사용
- `config/settings/production.py` — 운영도 `dev.py` 로 동작
- `base.py` 의 PostgreSQL/Redis 기본값 — `dev.py` 가 SQLite/LocMem 으로 덮음
- `.env.example` 의 PostgreSQL/Redis/Slack/TAS/Encryption 섹션
- `LOGOUT_REDIRECT_URL` (`config/settings/base.py:266`) — **SSO 도입(커스텀 `sso.logout_view`)으로 더 이상 쓰이지 않는다.** 구 Django `LogoutView` 가 읽던 값이며 지금 로그아웃 이동은 `logout_view` 가 직접 결정한다((a')(a)(b)(c)). `LOGIN_URL`/`LOGIN_REDIRECT_URL` 은 계속 사용. `urls.py` 의 `auth_views.LoginView`/`LogoutView` 도 `sso.login_view`/`sso.logout_view` 로 교체됨.
