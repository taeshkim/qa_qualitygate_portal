# PRD — QualityGate Portal 외부 API (v2)

작성일: 2026-05-11
대상 릴리즈: 추후 결정 (단계별 머지 가능)

---

## 0. 한 줄 요약

기존 세션/htmx 기반 포털 UI 의 **Quality Gate Run · Test Suite Run** 흐름을 **외부 시스템에서도 호출/조회/제어할 수 있도록 `/api/v2/` 아래에 REST API 를 신설**한다. 인증은 다중 API Key (이름/만료/스코프 지정, 발급/회수 UI) 로 한다.

---

## 1. 배경

- 현재 모든 트리거/조회는 `@login_required` 세션 + `RolePermissionMiddleware` POST 분기를 거치므로 **외부 자동화에서 호출 불가**.
- `/api/v1/` 에 DRF 가 이미 마운트돼 있으나 **레거시 `apps.gates.GateRun` 모델 기반** — 현 파이프라인(`QualityGateRun` / `TestSuiteRun`) 과 무관.
- Slack 알림은 종료 통지에만 쓰이고, 외부에서 진행 상태를 조회할 표준 채널이 없다.
- 운영 시나리오상 다음이 필요:
  - **CI/CD (Jenkins / GitHub Actions / Argo CD Hook)** 가 게이트/Suite 를 트리거하고 결과를 받아감.
  - **사내 시스템 (백오피스, 통합 대시보드)** 가 서버-서버로 조회.
  - **포털 자체 SPA / 외부 UI** 가 세션 대신 API Key 로 호출.

---

## 2. 범위 / 비범위

### 2.1 In scope

- 다중 **API Key** 발급/회수/만료/스코프 지정 UI + 모델
- `/api/v2/` 라우팅 신설 (`/api/v1/` 는 레거시 보존)
- **메타 조회**: 폴더/하위폴더/게이트 목록, Suite/Job 목록, Job 파라미터 스키마
- **Quality Gate Run**: 트리거 (이미지/배포/검수통과 + 옵션), 단건 조회, 취소, 처음부터/테스트만/배포만/검수통과만 재실행, Argo CD 재시도
- **Test Suite Run**: 단독 Suite Run 트리거 (1 job), 단건 조회, 취소
- **콘솔 로그 조회** (image / deploy / prod / suite job)
- 페이지네이션 + 필터 + 정렬을 갖춘 **Run 목록 조회** (게이트별 / Suite 별)
- 표준 에러 포맷 + per-key rate limit + audit log

### 2.2 Out of scope (이번 PRD 범위 아님)

- Webhook outbound (Run 종료 시 외부 URL 콜백) — 차후 별도 PRD
- Server-Sent Events / WebSocket — polling 만 제공
- OpenAPI 자동 생성 (drf-spectacular) — 1차는 수기 문서, 후속 작업에서 도입 고려
- `/api/v1/` 레거시 제거 — 보존 (사용처 미확인이므로 건드리지 않음)
- 게이트/Suite CRUD (등록/수정/삭제) — 외부 자동화 use case 없음, 포털 UI 만 사용
- 사용자/권한 관리, Slack 설정 변경

---

## 3. 사용 시나리오 (실제 호출 흐름)

### 3.1 Jenkins/GitHub Actions 가 게이트 1회 실행

```
1) POST /api/v2/gates/{gate_id}/runs
   body: {"skip_image_build": false, "parameters": {"BRANCH_NAME": "release/release_0511", "APP_IMAGE": "..."}}
   → 201 {"run_id": "...", "seq_no": 87, "status": "building_image", "detail_url": "..."}

2) (선택) 폴링: GET /api/v2/runs/{run_id}
   → 200 {"status": "testing", "stage_progress": {...}, "test_runs": [...], "argocd": {...}}
   ...
   → 200 {"status": "completed", ...}

3) 실패 시 콘솔: GET /api/v2/runs/{run_id}/console/{stage}
   → 200 {"output": "...", "truncated": false}
```

### 3.2 외부 시스템이 정기 Suite 1개 실행

```
POST /api/v2/suites/{suite_id}/runs
  body: {"job_id": "..."}   # job_id 생략 시 default job 1개 선택 정책은 §6.4
→ 201 {"suite_run_id": "...", "status": "running"}
GET  /api/v2/suite-runs/{suite_run_id}
→ 200 {"status": "completed", "passed_jobs": 1, "failed_jobs": 0, ...}
```

### 3.3 사내 대시보드가 게이트 목록과 최근 Run 을 표시

```
GET  /api/v2/gates?folder=auth&include=latest_run
GET  /api/v2/gates/{gate_id}/runs?limit=20&status=failed
```

### 3.4 자동화 도중 막힌 Argo CD 강제 재시도

```
POST /api/v2/runs/{run_id}/argocd/{deploy|prod}/retry
→ 202 {"status": "deploying", "argocd": {"sync_requested_at": "..."}}
```

---

## 4. 인증 / 권한

### 4.1 다중 API Key 모델

`apps.api_v2.models.ApiKey` (신규):

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | UUID | PK |
| `name` | str | "Jenkins-CI", "Backoffice-Dashboard" 등 식별 라벨 (사용자별 unique) |
| `user` | FK(User) | 발급자 (= 권한 owner). 호출 시 `request.user` 로 매핑 |
| `key_prefix` | str(8) | 키 평문의 앞 8자리 (식별/검색용, 평문 노출 가능) |
| `key_hash` | str(64) | 키 평문의 SHA-256 hex digest. **평문은 발급 시점에만 1회 보여주고 DB 에 저장하지 않음** |
| `scopes` | JSONField(list[str]) | 허용 스코프 배열 (§4.3) |
| `expires_at` | datetime, nullable | NULL 이면 만료 없음 |
| `last_used_at` | datetime, nullable | 매 호출 시 갱신 (write 비용 줄이려 1분 throttle) |
| `revoked_at` | datetime, nullable | NULL 이 아니면 무효 |
| `created_at` / `updated_at` | datetime | |

**키 평문 형식**: `qgp_{prefix}_{32 hex}` 예: `qgp_a1b2c3d4_4f3e2d1c...` (총 45자).
- 발급 직후 응답 본문에만 평문 1회 노출 → 사용자가 안전한 곳에 저장.
- 이후 어떤 화면에서도 평문 재조회 불가 (해시만 보관).

### 4.2 헤더 / DRF Authentication

- 요청 헤더: `Authorization: Bearer qgp_{prefix}_{...}`
- 신규 `ApiKeyAuthentication(BaseAuthentication)` 클래스:
  - 헤더 파싱 → prefix 추출 → `ApiKey.objects.filter(key_prefix=..., revoked_at__isnull=True).first()`
  - 평문 SHA-256 해시 비교 (constant-time compare)
  - `expires_at < now()` 이면 401
  - 통과 시 `request.user = api_key.user`, `request.auth = api_key` 로 세팅
- `/api/v2/` 의 DRF 뷰는 `authentication_classes = [ApiKeyAuthentication, SessionAuthentication]` 로 통합 — 포털 내부에서 같은 사용자가 세션으로도 호출 가능.

### 4.3 스코프 정의

스코프는 **단순 문자열 리스트**. 와일드카드 `*` 는 admin 전용.

| 스코프 | 허용 작업 |
|---|---|
| `meta:read` | GET 메타 (folder/subfolder/gate/suite/job 목록, 파라미터 스키마) |
| `runs:read` | GET Run 목록/단건/콘솔 (Quality Gate + Test Suite 양쪽) |
| `runs:trigger` | POST 새 Run 생성 (게이트 / Suite) |
| `runs:control` | POST 취소 / 재실행 / Argo CD 재시도 |
| `*` | 위 전체 + 향후 추가 스코프. **포털 admin 만 부여 가능** |

조합 예:
- 정기 배포 Bot: `runs:trigger`, `runs:read`
- 대시보드: `meta:read`, `runs:read`
- 오퍼레이션 스크립트: `runs:read`, `runs:control`

### 4.4 권한 매트릭스 (스코프 + 역할)

API Key 사용자의 **포털 Role** 도 함께 검사한다 — Viewer 사용자가 발급한 키는 `runs:trigger` 를 갖고 있어도 거부. (Role 변경이 키 회수 없이 즉시 반영되도록.)

| 호출 | 필요 스코프 | 필요 Role |
|---|---|---|
| GET 메타/Run | `meta:read` 또는 `runs:read` | 모든 role (Viewer 포함) |
| POST trigger | `runs:trigger` | `can_write` (= role ≠ viewer) |
| POST cancel/rerun/retry | `runs:control` | `can_write` |
| API Key 발급/회수 | (UI only, 키 인증 아님) | 본인 또는 admin |

기존 `RolePermissionMiddleware` 는 `/api/v2/` 경로를 **bypass** 하도록 분기 추가 (DRF 가 자체적으로 권한 처리하므로 이중 적용 방지).

### 4.5 API Key 관리 UI

- 위치: `/accounts/api-keys/` (사용자 본인 메뉴) + `/accounts/api-keys/admin/` (admin 이 전체 보기)
- 기능: 발급 (이름, 스코프 체크박스, 만료일), 평문 1회 노출, 회수 (revoke), 마지막 사용 시각 표시
- ActivityLog 에 `API_KEY_CREATED` / `API_KEY_REVOKED` 기록 (action enum 추가)

### 4.6 Rate Limit

- DRF `UserRateThrottle` 커스텀: per-API-Key 기준
  - 메타/조회: 120 req / min
  - trigger/control: 30 req / min
- 429 응답 시 `Retry-After` 헤더 포함

---

## 5. URL 라우팅

`config/urls.py` 에 추가:

```python
path("api/v2/", include("apps.api_v2.urls")),
```

`apps.api_v2.urls`:

```
GET    /api/v2/health/                                   인증 불요. status:ok + version
GET    /api/v2/me/                                       현재 키의 user/scopes 확인용

GET    /api/v2/folders/                                  인스턴스 목록
GET    /api/v2/folders/{folder_id}/                      인스턴스 상세
GET    /api/v2/folders/{folder_id}/subfolders/           하위폴더 목록
GET    /api/v2/subfolders/{subfolder_id}/                하위폴더 상세

GET    /api/v2/gates/                                    게이트 목록 (filter: folder, subfolder, gate_type, q)
GET    /api/v2/gates/{gate_id}/                          게이트 상세 (+include=latest_run, test_suites)
GET    /api/v2/gates/{gate_id}/params/                   image/deploy Job 파라미터 스키마 (?stage=image|deploy|prod, ?refresh=1)

GET    /api/v2/gates/{gate_id}/runs/                     게이트의 Run 목록 (filter: status, since, limit)
POST   /api/v2/gates/{gate_id}/runs/                     새 Run 트리거

GET    /api/v2/runs/{run_id}/                            Run 단건 (전체 상태 + 단계별 + test_runs + argocd)
GET    /api/v2/runs/{run_id}/console/{stage}/            stage ∈ image|deploy|prod, 콘솔 로그
POST   /api/v2/runs/{run_id}/cancel/                     Run 취소
POST   /api/v2/runs/{run_id}/rerun/                      처음부터 재실행 (새 Run 생성)
POST   /api/v2/runs/{run_id}/rerun-test/                 테스트만 재실행
POST   /api/v2/runs/{run_id}/rerun-deploy/               검수대상 배포만 재실행
POST   /api/v2/runs/{run_id}/rerun-prod/                 검수통과 배포만 재실행
POST   /api/v2/runs/{run_id}/deploy/                     WAITING_DEPLOY 에서 수동 deploy 트리거
POST   /api/v2/runs/{run_id}/prod-deploy/                QG_PASSED 에서 수동 prod 트리거
POST   /api/v2/runs/{run_id}/argocd/{stage}/retry/       stage ∈ deploy|prod

GET    /api/v2/suites/                                   Suite 목록 (filter: q, service)
GET    /api/v2/suites/{suite_id}/                        Suite 상세 (jobs 포함)
GET    /api/v2/suites/{suite_id}/runs/                   Suite Run 목록
POST   /api/v2/suites/{suite_id}/runs/                   Suite Run 트리거 (1 job)
GET    /api/v2/suite-runs/{suite_run_id}/                Suite Run 단건
POST   /api/v2/suite-runs/{suite_run_id}/cancel/         Suite Run 취소
GET    /api/v2/suite-job-runs/{job_run_id}/console/      Suite Job 콘솔 로그
```

---

## 6. 엔드포인트 명세 (핵심)

### 6.1 `POST /api/v2/gates/{gate_id}/runs/`

**Request body** (JSON):

```json
{
  "skip_image_build": false,            // 게이트가 image_job 을 가지고 있어도 건너뛸지
  "parameters": {                       // Jenkins Job 파라미터 (이미지 단계가 있으면 이미지 Job, 없으면 deploy Job)
    "BRANCH_NAME": "release/release_0511",
    "APP_IMAGE": "registry/foo:123-abc"
  },
  "auto_mode_override": null            // null = 게이트 설정 따름. true/false 로 단발 오버라이드 (선택)
}
```

**검증**:
- `gate.runs.filter(status__in=ACTIVE_STATUSES).exists()` → 409 Conflict (`run_already_active`)
- 이전 WAITING_USER Run 은 기존 로직대로 자동 CANCELLED 처리
- 파라미터는 빈 값 dict 에서 제외하는 기존 규칙 (`_collect_default_params`) 적용

**Response 201**:

```json
{
  "run_id": "uuid",
  "gate_id": "uuid",
  "seq_no": 87,
  "status": "building_image",
  "detail_url": "/qualitygates/runs/{uuid}/",
  "self_url": "/api/v2/runs/{uuid}/",
  "created_at": "2026-05-11T10:23:00+09:00"
}
```

**에러**:
- 400 `invalid_parameters` (모름 키 / 타입 불일치)
- 403 `scope_required` (`runs:trigger` 없음)
- 409 `run_already_active`
- 502 `jenkins_error`

### 6.2 `GET /api/v2/runs/{run_id}/`

**Response 200** (요약 — 실제 필드는 직렬화기에서 확정):

```json
{
  "run_id": "uuid",
  "gate": {"id": "...", "name": "auth-admin", "gate_type": "dev_stg", "folder": "...", "subfolder": "..."},
  "seq_no": 87,
  "status": "testing",
  "result_label": null,                 // "PASS" | "FAIL" | "QG PASS" | "CANCELLED" | null
  "is_terminal": false,
  "auto_mode": true,
  "parallel_test": false,
  "image": {
    "present": true,
    "build_number": 542,
    "build_url": "https://jenkins/.../542",  // 원본 Jenkins URL (조회용)
    "started_at": "...", "completed_at": "...",
    "duration_ms": 123000,
    "git_branch": "release/release_0511",
    "git_commit": "a1b2c3d4...",
    "current_stage": "Build"
  },
  "deploy": {
    "build_number": 188, "build_url": "...", "started_at": "...", "completed_at": "...",
    "argocd": {"sync": "Synced", "health": "Healthy", "phase": "Succeeded", "synced_at": "...", "badge": {"kind": "synced", "level": "pass", "label": "Synced"}}
  },
  "test_runs": [
    {"order": 0, "suite_id": "...", "suite_name": "...", "status": "passed", "job_run_id": "...", "started_at": "...", "completed_at": "..."}
  ],
  "prod": null,                         // null 또는 deploy 와 동일 구조
  "console_urls": {
    "image": "/api/v2/runs/{uuid}/console/image/",
    "deploy": "/api/v2/runs/{uuid}/console/deploy/",
    "prod": null
  },
  "triggered_by": {"id": "...", "username": "..."},
  "created_at": "..."
}
```

폴링 호출이 잦으므로 select_related/prefetch 로 N+1 방지 (기존 `run_detail` 뷰의 prefetch 와 동일).

호출 시점에 `run.is_running` 이면 기존 `_poll_gate_run(run)` 을 **그대로 호출** — 이게 외부 진행을 끌어주는 폴링 트리거다. 즉 외부 자동화가 적당한 간격으로 GET 만 때려도 진행이 멈추지 않는다.

### 6.3 `POST /api/v2/runs/{run_id}/cancel/`, `.../rerun/`, `.../rerun-test/`, `.../rerun-deploy/`, `.../rerun-prod/`, `.../deploy/`, `.../prod-deploy/`, `.../argocd/{stage}/retry/`

기존 뷰 함수(`cancel_run`, `rerun_gate`, `rerun_test`, `deploy_trigger`, `prod_deploy`, `argocd_retry`) **를 그대로 재사용**하되 다음을 분리:
- 입력: HTML form POST 대신 JSON body 허용 (파라미터가 필요한 `rerun*`, `deploy*`, `prod-deploy` 만)
- 응답: redirect 대신 200 JSON (`{"run_id": ..., "status": ...}`)
- `messages.success(...)` 호출은 API 경로에서는 생략 (Django messages framework 가 세션 의존)

리팩토링 방향:
- 각 액션의 **핵심 로직을 service 함수로 분리** (예: `apps.qualitygates.services.cancel_run(run, user)`, `trigger_gate_run(gate, user, params, ...)`)
- 기존 view 는 service 호출 + redirect/messages 래핑만 담당
- API view 는 service 호출 + JSON 직렬화

이게 PRD 의 가장 큰 리팩토링 항목 — §10 의 작업 단위로 분리한다.

### 6.4 `POST /api/v2/suites/{suite_id}/runs/`

```json
{ "job_id": "uuid-or-null" }
```

`job_id` 가 null 이면 **거부 (400 `job_required`)** — 기존 `suite_run` 뷰가 1개 Job 만 지원하고 default 선택 정책이 없으므로, 외부 호출도 명시 강제. (향후 정책 추가 시 확장.)

**Response 201**:
```json
{
  "suite_run_id": "uuid",
  "suite_id": "uuid",
  "status": "running",
  "self_url": "/api/v2/suite-runs/{uuid}/",
  "detail_url": "/suites/runs/{uuid}/"
}
```

### 6.5 `GET /api/v2/gates/{gate_id}/params/?stage=image|deploy|prod&refresh=1`

기존 `gate_run_params` / `deploy_params` / `prod_deploy_params` 를 stage 파라미터로 통합. 120초 캐시 + `refresh=1` 동작 유지. APP_IMAGE 같은 prod 단계의 default override 도 동일하게 적용.

**Response 200**:
```json
{
  "stage": "deploy",
  "cached": true,
  "parameters": [
    {"name": "BRANCH_NAME", "type": "GitParameterDefinition", "default": "release/release_0511", "choices": [...], "description": "..."},
    ...
  ]
}
```

### 6.6 콘솔 로그

`GET /api/v2/runs/{run_id}/console/{stage}/`:
```json
{ "stage": "deploy", "output": "...truncated string...", "length": 12345, "truncated": false, "build_url": "https://jenkins/.../188/console" }
```
- DB 에 저장된 `*_console_output` 그대로 반환.
- `?tail=2000` 으로 마지막 N 줄만 (기본은 전체).

`GET /api/v2/suite-job-runs/{job_run_id}/console/` 도 동일 구조.

---

## 7. 응답 / 에러 규격

### 7.1 성공 응답

- 단건: 위 §6 의 dict 그대로 반환
- 목록: `{"count": 123, "next": "...", "previous": "...", "results": [...]}` (DRF PageNumberPagination)

### 7.2 에러 표준

```json
{
  "error": {
    "code": "run_already_active",
    "message": "이 게이트에는 이미 진행 중인 Run 이 있습니다.",
    "details": {"active_run_id": "uuid", "status": "testing"}
  }
}
```

- HTTP status + `error.code` (snake_case) 조합으로 판별. message 는 사람용.
- 코드 카탈로그: `invalid_parameters`, `scope_required`, `role_denied`, `key_expired`, `key_revoked`, `key_invalid`, `not_found`, `run_already_active`, `run_not_actionable`, `jenkins_error`, `argocd_error`, `rate_limited`, `internal_error`

### 7.3 시간 / UUID 포맷

- 시간: ISO 8601 KST (`+09:00` 표기 유지) — 기존 `USE_TZ=True, TIME_ZONE=Asia/Seoul` 그대로
- ID 는 모두 UUID 문자열 (기존 `UUIDField` 그대로)

---

## 8. 폴링 / 진행 동기화

- 외부에서 진행을 확인하는 표준은 **GET `/api/v2/runs/{run_id}/` 폴링**.
- 호출 시 내부적으로 `_poll_gate_run(run)` 실행 → Jenkins/Argo CD 한 번 더 진행시킴.
- 권장 폴링 간격: **5초 이상** (포털 htmx 와 동일). 더 짧으면 Argo CD 4초 쿨다운에 막혀 의미 없음.
- 응답 헤더에 `X-QGP-Poll-After: 5` (초 단위) 힌트 제공 — terminal 도달 시 `0` 으로 줄어들면 stop.

(SSE/Webhook 은 §2.2 대로 후속 PRD)

---

## 9. 보안 / 운영

- API Key 평문은 **DB 에 절대 저장 X**. 해시는 SHA-256. 평문 응답은 발급 직후 1회.
- `Authorization` 헤더는 log/Slack 에 노출 금지 (`SECRET_HEADER_NAMES` 마스킹 미들웨어 추가).
- ActivityLog: `API_KEY_CREATED`, `API_KEY_REVOKED`, `API_RUN_TRIGGERED` (target=Run, target_name=gate label)
- Slack 알림: 기존 이벤트 그대로 동작 — API 트리거 Run 도 자동으로 알림이 발송됨.
- `runserver` 단일 프로세스 / SQLite 운영이라 동시 호출이 많아지면 트랜잭션 락이 변수. trigger 류는 atomic + select_for_update **불가능** (SQLite) → 기존 ACTIVE 검사 race window 그대로 두되 같은 게이트 동시 호출은 어차피 DB 레벨에서 한쪽이 실패함을 문서에 명기.
- Rate limit (DRF Throttle) 외에 nginx/ALB 레벨에서도 abuse 제어 가능.

---

## 10. 데이터 모델 변경

### 10.1 신규

- `apps.api_v2` 앱 생성 (`urls.py`, `views/`, `serializers/`, `authentication.py`, `permissions.py`, `throttling.py`)
- `apps.api_v2.models.ApiKey` (위 §4.1)

### 10.2 기존 변경

- `apps.activitylog.models.ActivityLog.Action` 에 `API_KEY_CREATED`, `API_KEY_REVOKED`, `API_RUN_TRIGGERED` 추가 (migration 1개)
- `apps.accounts.middleware.RolePermissionMiddleware`: `/api/v2/` prefix 시 즉시 pass-through

### 10.3 마이그레이션 영향

- 신규 테이블 1개 (`api_v2_apikey`)
- ActivityLog choices 추가 — TextChoices 라 데이터 백필 불필요
- 기존 Run/Gate/Suite 모델은 무변경

---

## 11. 호환성

- `/api/v1/` 레거시 유지. 사용처가 발견되면 deprecation 안내 후 별도 PR 로 제거.
- 신규 `/api/v2/` 의 첫 릴리즈는 v2 로 고정. 이후 호환 깨는 변경이 필요하면 `/api/v3/` 신설 (v2 는 유지) — 외부 자동화 안전성 우선.
- 응답 필드 추가는 v2 안에서 허용. 필드 제거/이름 변경은 v3 로.

---

## 12. 단계별 구현 작업 (작업 단위 분할)

각 단계는 **독립 머지 가능**. 머지 후 README/maintenance.md 갱신 + commit + push.

### Phase A — 기반 (1~2 PR)

A-1. `apps/api_v2/` 앱 스캐폴딩 + URL 라우팅 (빈 health, me 엔드포인트만)
A-2. `ApiKey` 모델 + 마이그레이션 + `ApiKeyAuthentication` + 스코프/role permission classes + custom throttle
A-3. `/api/v2/health/`, `/api/v2/me/` 동작 + 단위 테스트 (key 발급/해시/만료/회수/스코프 거부)
A-4. `RolePermissionMiddleware` 의 `/api/v2/` bypass

### Phase B — API Key 관리 UI (1 PR)

B-1. `/accounts/api-keys/` 사용자 본인 발급/회수 화면
B-2. 발급 직후 평문 1회 노출 페이지 (복사 버튼)
B-3. admin 전체 키 조회/회수 화면
B-4. ActivityLog 이벤트 추가

### Phase C — 메타 조회 (1 PR)

C-1. Serializer: Folder, SubFolder, Gate, Suite, Job
C-2. ViewSet: `folders`, `subfolders`, `gates`, `suites` (GET only)
C-3. `GET /api/v2/gates/{id}/params/?stage=` — 기존 캐시 로직 재사용
C-4. 필터/검색/정렬/페이지네이션 (django-filter 도입 시 settings 추가)

### Phase D — Quality Gate Run 조회 / 트리거 (2 PR)

D-1. **(리팩토링)** `apps.qualitygates.services.py` 신설 — `trigger_gate_run`, `cancel_run`, `rerun_gate`, `rerun_test`, `rerun_deploy`, `rerun_prod`, `trigger_deploy`, `trigger_prod`, `argocd_retry` 를 view 에서 분리. 기존 view 는 service 호출 + messages/redirect 래핑.
D-2. Serializer: `QualityGateRunDetailSerializer` (단계별 + test_runs + argocd badge 포함)
D-3. `GET /api/v2/runs/{id}/` + 내부에서 `_poll_gate_run` 호출
D-4. `GET /api/v2/gates/{id}/runs/` 목록 (filter: status, since, limit)
D-5. `POST /api/v2/gates/{id}/runs/` 트리거
D-6. `GET /api/v2/runs/{id}/console/{stage}/`

### Phase E — Quality Gate Run 제어 (1 PR)

E-1. `POST cancel/`, `rerun/`, `rerun-test/`, `rerun-deploy/`, `rerun-prod/`
E-2. `POST deploy/`, `prod-deploy/` (파라미터 body 받음)
E-3. `POST argocd/{stage}/retry/`

### Phase F — Test Suite Run (1 PR)

F-1. `apps.suites.services.trigger_suite_run`, `cancel_suite_run` 분리
F-2. Serializer + ViewSet (Suite, SuiteRun, JobRun)
F-3. POST/GET/Cancel + 콘솔

### Phase G — 마무리 (1 PR)

G-1. Rate limit (DRF custom throttle, scope 별)
G-2. 에러 표준 응답 (`api_v2.exception_handler`)
G-3. 응답 헤더 `X-QGP-Poll-After`
G-4. README.md / maintenance.md / CLAUDE.md 갱신
G-5. 외부 호출자용 quickstart 문서 (`docs/api_v2_quickstart.md`)

---

## 13. 테스트 계획

각 Phase 머지 직전 (CLAUDE.md 절차):

- **단위 테스트** (`apps/api_v2/tests/`): authentication, scope/role permission, throttle, serializer, 트리거 race
- **시나리오 테스트**:
  - 키 발급 → 헬스체크 → 메타 조회 → Run 트리거 → 폴링 → 종료
  - 만료 키 / 회수 키 / 스코프 부족 / role viewer
  - ACTIVE Run 중 새 Run 트리거 거부
  - rerun-test 가 deploy_completed_at 없는 Run 에서 거부
- **수동 검증**: 실 Jenkins/Argo CD 연결된 게이트에 대해 사용자에게 확인 요청 (CLAUDE.md 의 "외부 자격증명 필요 흐름" 룰).

---

## 14. 문서 갱신 체크리스트

- `README.md` §9 URL 라우팅에 `/api/v2/` 추가, 기능 목록에 "외부 API" 항목
- `maintenance.md`:
  - §1 구조 다이어그램에 외부 API 경로
  - §4 외부 연동에 API Key 인증 흐름
  - §5 URL 라우팅 표
  - §7 권한 / 인증에 ApiKey + scope 매트릭스
  - §10 자주 하는 변경에 "스코프 / 엔드포인트 추가" 항목
- `CLAUDE.md`: 변동 없음 (작업 절차는 그대로). API key 평문 절대 로그 금지만 추가 고려.
- 신규 `docs/api_v2_quickstart.md` — 외부 호출자용 curl 예제 + 폴링 패턴 + 에러 코드 카탈로그

---

## 15. Open Questions (의사결정 필요)

1. **API Key 만료 일괄 정책**: 기본 만료를 자동으로 90일/180일/무제한 중 어느 걸로?
2. **scope `*` 부여 권한**: 발급자가 admin 이면 자동 허용? 아니면 추가 화면에서 명시?
3. **Webhook outbound** 차후 PRD 의 우선순위 (Slack 만으로 충분한지)?
4. **콘솔 로그 응답 크기 상한**: 현재 DB 에 통째로 들어 있음 — 1MB 넘어가면 ?tail= 강제할지?
5. **OpenAPI 자동 생성** (drf-spectacular) 도입 시점 — 이번 Phase G 에 포함할지 후속으로 미룰지?
6. **API Key per-IP allowlist** 가 필요한가? (사내 시스템이 IP 고정이면 추가 안전망)

이 6개는 본 PRD 머지 전 별도 확인이 필요. 답이 정해지지 않으면 보수적 디폴트 — 만료 90일, `*` 는 admin 전용 + 추가 체크박스, Webhook 후속, 콘솔 1MB tail 강제, OpenAPI 후속, IP allowlist 안 함 — 로 진행.
