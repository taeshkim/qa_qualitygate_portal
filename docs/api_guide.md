# QualityGate Portal `/api/v2/` API Guide

> 외부 시스템(Jenkins / GitHub Actions / 백오피스 / 사내 대시보드) 이 QualityGate
> 포털의 Run 을 트리거하고 진행을 추적할 수 있는 REST API.
>
> **외부 호출자용 단일 문서** — Quickstart(키 발급·시나리오) + 전체 엔드포인트
> reference + 응답 스키마 + 폴링 모델 + 상태 머신 + 에러 카탈로그 + 호출 예제를
> 모두 담는다. 내부 설계/정책 의도는 `prd_api.md` 참조.

운영 주소: 브라우저(포털/문서) `https://qw.jkintra.net/qg/` · **프로그램 호출(API Key) `https://qw.jkintra.net/qg/ci-api/`** (§2.1).
(구 주소 `https://qualitygate.jkintra.net`(AWS 원본)은 2026-10 서버 이관(IDC `wx-qms221`)으로 종료(502).)
**표기 규칙**: 호출 예제·Base URL 은 `/ci-api/`, §1 다이어그램과 §4 엔드포인트 reference 표의 `/api/v2/<path>` 는 **상대경로 표기**이며 프로그램은 `/ci-api/<path>` 로 호출한다(같은 엔드포인트, §2.1). Swagger/ReDoc(`/qg/api/v2/docs/`)도 모든 경로·예시를 `/ci-api/` 로 표시하고 Try it out 을 `/qg/ci-api/…` 로 호출한다.

---

## 1. 핵심 호출 패턴

```
        ┌──────────────────────────────────────────────────────────┐
        │ POST /api/v2/gates/<gate_id>/runs/                       │
        │   → 201 { "run_id": "<uuid>", "status": "...",           │
        │           "self_url": "/ci-api/runs/<uuid>/", ... }      │
        └──────────────────────────────────────────────────────────┘
                                  │
                                  ▼
        ┌──────────────────────────────────────────────────────────┐
        │ GET /api/v2/runs/<run_id>/status/   ← 폴링 (가벼움)       │
        │   → 200 { "status": "testing", "phase": "running",       │
        │           "is_terminal": false, ... }                    │
        │   Header: X-QGP-Poll-After: 5                            │
        └──────────────────────────────────────────────────────────┘
                                  │   매 5초마다 반복
                                  ▼
        ┌──────────────────────────────────────────────────────────┐
        │ GET /api/v2/runs/<run_id>/status/                        │
        │   → 200 { "status": "completed", "phase": "succeeded",   │
        │           "is_terminal": true, "result": "PASS" }        │
        │   Header: X-QGP-Poll-After: 0   ← stop                   │
        └──────────────────────────────────────────────────────────┘
```

폴링은 GET `/runs/<id>/status/` 또는 GET `/runs/<id>/` 둘 다 가능:

| 엔드포인트 | 응답 크기 | 용도 |
|---|---|---|
| `/runs/<id>/status/` | 작음 (status/phase/result 만) | **단순 폴링 체크**. CI/CD 시나리오 권장 |
| `/runs/<id>/` | 큼 (image/deploy/test_runs/prod/argocd badge 포함) | 대시보드 / 실패 진단 시 |

두 엔드포인트 모두 서버 측에서 `_poll_gate_run` 을 호출해 진행을 이어준다 — 즉 외부에서 GET 만 때려도 파이프라인이 멈추지 않는다.

---

## 2. 인증

`Authorization: Bearer qgp_<8자 prefix>_<32자 hex>` (Basic: 비밀번호 자리에 키 — §2.1)

### 2.1 호출 주소 `/ci-api/` — 프로그램은 이 주소로

운영 게이트웨이는 SSO 쿠키가 없는 요청을 **앱에 닿기 전에 401(nginx HTML)** 로 막는다. 그래서 `Authorization` 헤더만 보내는 CI/스크립트는 `/qg/api/v2/…` 로는 호출할 수 없다. 대신 게이트웨이가 로그인 검사를 건너뛰는 전용 경로 **`/qg/ci-api/`** 를 쓴다.

| 항목 | 내용 |
|---|---|
| Base URL | `https://qw.jkintra.net/qg/ci-api/` — `/api/v2/<path>` ↔ `/ci-api/<path>` (같은 엔드포인트·같은 응답) |
| 인증 | **API Key 로만**. 쿠키·SSO 헤더(`X-Auth-*`)는 전부 무시(세션으로 로그인된 브라우저에서 호출해도 키가 없으면 401) |
| Bearer | `Authorization: Bearer qgp_<prefix>_<secret>` |
| Basic | 비밀번호 자리에 키: `curl -u x:$QGP_KEY …` (Jenkins "Username with password" 자격증명 등). 사용자명은 무시. 아이디/비밀번호 Basic 은 거부(401) |
| 제공 범위 | health · me · meta · Quality Gate Run · Run 제어 · Test Suite Run (§4). **문서(schema/docs/redoc)·DB 내보내기(§4.5)는 없음**(404) — 문서는 브라우저 `/qg/api/v2/docs/` 사용 |
| Swagger 표기 | Swagger UI/ReDoc 에 보이는 **엔드포인트 경로·설명·예시가 모두 `/ci-api/` 기준**이고 **Try it out 도 `/qg/ci-api/…` 로 호출**된다(문서 페이지 자체만 브라우저용 `/qg/api/v2/docs/`). Authorize 에 키를 넣으면 우상단 배지가 `/ci-api/me/` 로 키를 검증해 ✅/❌ 로 알려준다 |
| 응답 링크 | `self_url`·`console_urls` 등도 `/qg/ci-api/…` 로 나온다(그대로 따라가도 게이트웨이 예외 경로 안) |

```bash
export QGP_KEY=qgp_...      # 발급 직후 1회 표시된 키
BASE=https://qw.jkintra.net/qg/ci-api

curl -s $BASE/health/                                   # 키 불필요 → {"status":"ok",...}
curl -s -H "Authorization: Bearer $QGP_KEY" $BASE/me/    # 키 유효성·scope 확인
curl -s -u x:$QGP_KEY $BASE/me/                          # Basic (비밀번호 자리에 키)
```

오류 구분: 응답이 **HTML 401**(nginx) 이면 게이트웨이 예외가 안 걸린 것(주소 오타 `/api/v2` 사용 포함), **JSON 401** `{"error":{"code":"not_authenticated"|"api_key_invalid"|…}}` 이면 앱까지 도달했고 키 문제다. 이 경로는 게이트웨이 인증이 없으므로 키 관리(최소 scope·만료·유출 시 즉시 회수)가 곧 보안이다.

### 2.0 API Key 발급

1. 포털 로그인 → 우측 사이드바 **API Keys** (`/accounts/api-keys/`) → **새 키 발급**.
2. 이름, 스코프(`meta:read` / `runs:read` / `runs:trigger` / `runs:control`, `*` 는
   admin 만), 만료(30/90/180/365/무제한, **디폴트 90일**) 선택.
3. 발급 직후 **평문**이 한 번만 노출된다(`qgp_<8자 prefix>_<32hex secret>`). 안전한
   곳에 즉시 복사 — 이후 어떤 화면에서도 재조회 불가(해시만 보관).
4. 잃어버린 키는 회수(revoke) 후 새로 발급.

키 발급/회수 UI: `/accounts/api-keys/` (본인) · `/accounts/api-keys/admin/` (admin).

```bash
# 인증/스코프 확인
curl -H "Authorization: Bearer $QGP_KEY" \
  https://qw.jkintra.net/qg/ci-api/me/
# → { "user": {...}, "api_key": { "scopes": [...], "expires_at": "..." },
#     "available_scopes": [...] }
```

### 스코프

| 스코프 | 허용 |
|---|---|
| `meta:read` | folder/subfolder/gate/suite/job 메타 + 파라미터 스키마 |
| `runs:read` | Run 목록/단건/콘솔 (QG + Suite 양쪽) |
| `runs:trigger` | 새 Run 생성 |
| `runs:control` | 취소 / 재실행 / Argo CD 재시도 |
| `*` | 전체 (admin 만 부여 가능). **`export:read`·`owners:manage` 는 포함하지 않는다** |
| `export:read` | DB 스냅샷 내보내기 (§4.5). **명시 부여 전용** — `*` 로 암묵 부여되지 않고, 키 소유자가 admin(`qpe_admin`/superuser) 이어야 동작. 서버 이관 용도 |
| `owners:manage` | 담당자 관리 API (§4.6). **명시 부여 전용** — `*` 로 암묵 부여되지 않고, 키 소유자가 admin 이어야 동작 |

### Rate limit

| 종류 | 한도 | 비고 |
|---|---|---|
| Read | 120 req/min | `meta:read` / `runs:read` |
| Write | 30 req/min | `runs:trigger` / `runs:control` (내보내기 API 의 POST/DELETE 도 Write) |

429 응답 시 `Retry-After` 초 단위 헤더 포함.

---

## 3. Status / Phase 매핑

DB 의 세분 status (14 개 / 3 개) 와 외부 친화적 phase (6 개) 가 모든 응답에 함께 노출된다. **CI/CD 자동화는 `phase` 만 보고 분기하면 충분.**

### Quality Gate Run

| `status` | `phase` | `is_terminal` | 의미 |
|---|---|---|---|
| `pending` | `queued` | false | Run 생성 직후, 즉시 다음 단계로 전이 |
| `building_image` | `running` | false | 이미지 빌드 진행 중 |
| `image_build_failed` | `failed` | true | terminal, 실패 |
| `waiting_deploy` | `awaiting_input` | false | 사용자 입력 대기 (수동 모드) |
| `deploying` | `running` | false | 검수대상 배포 + Argo CD sync |
| `deploy_failed` | `failed` | true | terminal, 실패 (재실행 가능) |
| `waiting_test` | `running` | false | 모든 Test Job 이 busy 라 대기 |
| `testing` | `running` | false | 테스트 진행 중 |
| `qg_passed` | `awaiting_input` | false | 검수 통과, 수동 prod 배포 대기 |
| `failed` | `failed` | true | terminal, 테스트 실패 (rerun-test 가능) |
| `prod_deploying` | `running` | false | 검수통과 배포 + Argo CD sync |
| `prod_deploy_failed` | `failed` | true | terminal, 실패 |
| `completed` | `succeeded` | true | terminal, 성공 |
| `cancelled` | `cancelled` | true | terminal, 사용자 취소 |

상태 머신 (간략):

```
queued → running ───────┬───→ succeeded (terminal)
                        ├───→ failed    (terminal)
                        ├───→ awaiting_input ──→ (사용자 수동 트리거) ──→ running
                        └───→ cancelled (terminal, 사용자 취소)
```

### Test Suite Run (단독)

| `status` | `phase` | `result` | `is_terminal` |
|---|---|---|---|
| `pending` | `queued` | `null` | false |
| `running` | `running` | `null` | false |
| `completed` (failed_jobs = 0) | `succeeded` | `"passed"` | true |
| `completed` (failed_jobs > 0) | `failed` | `"failed"` | true |

> **`result` (Pass/Fail)** — 외부 시스템이 폴링으로 합/불을 바로 읽도록 추가된 명시
> 필드. 종료 전(`is_terminal=false`)에는 `null`, 종료 후 `"passed"` / `"failed"`.
> `phase` 의 `succeeded`/`failed` 와 의미가 1:1 대응하지만, terminal 여부와 무관하게
> 항상 노출되는 `phase` 와 달리 `result` 는 **"결과가 확정됐는가"** 를 `null` 로 구분해
> 준다 — 폴링 루프에서 `result != null` 이면 종료로 보고 분기하면 된다.
>
> 개별 Job 결과도 `suite-runs/<id>/` 단건 응답의 `job_runs[].result`
> (`"passed"` / `"failed"` / `null`) 로 확인할 수 있다. (`aborted` 는 `"failed"` 로 집계.)
>
> **`job_runs[].status` 가능값** — `pending` / `waiting` / `queued` / `running` /
> `success` / `failure` / `aborted`. 이 중 `pending`·**`waiting`**·`queued`·`running`
> 은 **비종료(진행 중)** 이며 `result` 는 `null` 이다. `waiting` 은 "잡 실행 지연
> throttle(suite 단위 Jenkins 호출 분산)" 로 트리거가 잠시 보류된 상태로, 설정된 지연시간이
> 지나면 포털이 자동으로 트리거한다 — 소비자는 별도 처리 없이 `result != null`(또는
> suite_run 의 `is_terminal=true`)만으로 종료를 판정하면 된다.

---

## 4. 엔드포인트 Reference

### 4.1 메타 조회 (스코프: `meta:read`)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/v2/folders/` | 인스턴스 목록 |
| GET | `/api/v2/folders/<uuid>/` | 인스턴스 상세 |
| GET | `/api/v2/folders/<uuid>/subfolders/` | 하위폴더 목록 |
| GET | `/api/v2/subfolders/<uuid>/` | 하위폴더 상세 (Gate 목록 포함) |
| GET | `/api/v2/gates/` | 게이트 목록. 필터: `folder` / `subfolder` / `gate_type` (`dev_stg` 또는 `stg_prd`) / `q` / `is_active` |
| GET | `/api/v2/gates/<uuid>/` | 게이트 상세. `?include=latest_run,test_suites` |
| GET | `/api/v2/gates/<uuid>/params/?stage=image\|deploy\|prod&refresh=1` | Jenkins Job 파라미터 스키마 (Active Choices / Git Parameter 포함). 120s 캐시 |
| GET | `/api/v2/suites/` | Suite 목록. 필터: `service` / `q` / `is_active` |
| GET | `/api/v2/suites/<uuid>/` | Suite 상세 (jobs 포함) |

### 4.2 Quality Gate Run

| 메서드 | 경로 | 스코프 | 설명 |
|---|---|---|---|
| GET | `/api/v2/gates/<uuid>/runs/` | `runs:read` | Run 목록. 필터: `status` / `since` / `limit` |
| POST | `/api/v2/gates/<uuid>/runs/` | `runs:trigger` | 새 Run 트리거 — body: `{skip_image_build, parameters, auto_mode_override}` |
| GET | `/api/v2/runs/<uuid>/` | `runs:read` | Run 단건 상세 (전체 필드, 폴링 트리거) |
| GET | `/api/v2/runs/<uuid>/status/` | `runs:read` | **Run 가벼운 상태 (폴링용)** — status/phase/is_terminal/result |
| GET | `/api/v2/runs/<uuid>/console/<stage>/` | `runs:read` | stage ∈ `image\|deploy\|prod`. `?tail=N`. 1MB 초과시 자동 tail 50KB |
| POST | `/api/v2/runs/<uuid>/cancel/` | `runs:control` | Run 취소 |
| POST | `/api/v2/runs/<uuid>/rerun/` | `runs:control` | 처음부터 재실행 (새 Run 반환, 201) |
| POST | `/api/v2/runs/<uuid>/rerun-test/` | `runs:control` | 테스트만 재실행 |
| POST | `/api/v2/runs/<uuid>/rerun-deploy/` | `runs:control` | 검수대상 배포만 재실행 (`DEPLOY_FAILED` 전용) |
| POST | `/api/v2/runs/<uuid>/rerun-prod/` | `runs:control` | 검수통과 배포만 재실행 (`PROD_DEPLOY_FAILED` 전용) |
| POST | `/api/v2/runs/<uuid>/deploy/` | `runs:control` | `WAITING_DEPLOY` 에서 수동 deploy 트리거 |
| POST | `/api/v2/runs/<uuid>/prod-deploy/` | `runs:control` | `QG_PASSED` 에서 수동 prod 트리거 |
| POST | `/api/v2/runs/<uuid>/argocd/<stage>/retry/` | `runs:control` | stage ∈ `deploy\|prod`. Argo CD sync 리셋 + 재시도 (202) |

### 4.3 Test Suite Run (단독)

| 메서드 | 경로 | 스코프 | 설명 |
|---|---|---|---|
| GET | `/api/v2/suites/<uuid>/runs/` | `runs:read` | Suite Run 목록 |
| POST | `/api/v2/suites/<uuid>/runs/` | `runs:trigger` | Suite Run 트리거. 3가지 모드 (아래 참고) |
| GET | `/api/v2/suite-runs/<uuid>/` | `runs:read` | Suite Run 단건 |
| GET | `/api/v2/suite-runs/<uuid>/status/` | `runs:read` | **Suite Run 가벼운 상태 (폴링용)** |
| POST | `/api/v2/suite-runs/<uuid>/cancel/` | `runs:control` | 취소 |
| GET | `/api/v2/suite-job-runs/<uuid>/console/` | `runs:read` | Suite Job 콘솔 로그. `?tail=N` |

#### POST `/api/v2/suites/<uuid>/runs/` — 트리거 모드

| body | 동작 | 비고 |
|---|---|---|
| `{"job_id": "<uuid>"}` | 지정한 Job **1개만** 실행 (`total_jobs=1`) | 하위호환. 기존 호출 그대로 |
| `{}` / body 없음 / `job_id` 빈값 | 활성 대상 Job(`is_active=true & is_target=true`) 중 **idle worker 1개만** 선택해 실행. `order` 앞 순 우선 | 기본 동작 — 동시에 N개 돌지 않음 |
| `{"run_all": true}` | 활성 대상 Job **전체**를 `order` 순 **순차 체인**으로 실행 | 전체 실행이 필요할 때 명시적으로 지정 |

- 응답은 모든 모드 동일한 **201** (`suite_run_id` / `seq_no` / `status` / `self_url` / `detail_url` / `total_jobs`).
  호출자는 `suite_run_id` **1개만** 폴링하면 된다.
- **idle 판정**: Jenkins `lastBuild` 조회 성공 + `building=false` (또는 미빌드 404) 인 Job 만 idle 로 간주. 연결 오류 / 인증 실패 / 5xx 등 상태 확인이 불가능한 Job 은 건너뜀("검증되지 않은 worker 는 실행하지 않는다").
- **검증된 idle worker 가 없으면** (`all_workers_busy`) → **`409 all_workers_busy`** 반환, Run 을 생성하지 않는다. 잠시 후 재시도.
- **전체 체인 (`run_all=true`)**: 한 번에 Jenkins 빌드 1개만 트리거하고 앞 Job 이 끝나면 폴링이 다음 Job 을 트리거한다 (포털 UI 체인과 동일). Job 5개짜리 suite 를 호출해도 Jenkins 빌드가 동시에 5개 돌지 않는다.
- 활성 대상 Job 이 하나도 없으면 `409 no_active_jobs`.

### 4.4 기타

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/v2/health/` | 인증 불요. `{"status":"ok","version":"v2.0"}` |
| GET | `/api/v2/me/` | 현재 키 + 사용자 + 사용 가능 스코프 |

### 4.5 DB 스냅샷 내보내기 (스코프: `export:read`, 서버 이관용)

원본 서버의 DB 를 HTTPS 만으로 내려받기 위한 API (`ssh`/`rsync` 가 막힌 환경). 내부적으로 `apps/syncops` 의 `export_snapshot` 을 호출해 번들(`manifest.json` + `db.sqlite3.zst|.gz`)을 만든다. Swagger(`/api/v2/docs/`)에는 노출하지 않는다. **`/ci-api/` 에도 없고 브라우저용 `/api/v2/export/…` 에만 있는 엔드포인트**다(로그인 없이 열리는 경로에 둘 수 없음). 서버 이관은 2026-10 에 완료됐고 이 절은 이관 수행의 **이력/참고용**이다(현재 운영에서 상시 사용하지 않음).

- **인증/권한**: API Key 만(세션 불가) + `export:read` **명시 스코프**(`*` 로는 통과 안 됨) + **키 소유자가 admin**. 요청 시점에 소유자 역할을 다시 보므로 발급 후 강등된 키도 차단된다. 키 발급 폼(`/accounts/api-keys/new/`)에서는 admin 에게만 `export:read` 체크박스가 보인다.
- **config 는 절대 내보내지 않는다**: `config.tar`·`.env`·`secrets/` 는 이 API 로 오지 않는다(`include_config` 등 알 수 없는 필드는 400 `unsupported`). 필요하면 별도 경로(사람이 직접 전달)로 옮긴다.
- **수명/동시성**: 번들은 `<BASE_DIR>/snapshots/api-<id>/`(디렉터리 0700, 파일 0600)에 만들어지고 상태는 번들 안 `state.json` 에 둔다(DB 모델·마이그레이션 없음). **24시간 뒤 자동 만료**(파일 삭제 → `expired` 표식 → 다시 24시간 뒤 디렉터리 제거, 생성/조회 시 정리). **동시에 1개만** 실행.

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/v2/export/snapshots/` | 스냅샷 생성 시작. body `{"compress":"gzip"\|"zstd"\|"auto"}`(기본 `auto`, 본문 생략 가능). **202** `{"id","status":"running","created_at"}` |
| GET | `/api/v2/export/snapshots/<id>/` | **200** `{"id","status":"running\|ready\|failed\|expired","progress_pct","created_at","finished_at","error","manifest","files":[{"name","size","sha256"}]}`. `manifest`/`files` 는 `ready` 일 때만 채워짐(`files` 는 `manifest.json` + DB 파일만) |
| GET | `/api/v2/export/snapshots/<id>/download/<name>/` | 파일 스트리밍. `<name>` ∈ `manifest.json`, `db.sqlite3.zst`, `db.sqlite3.gz`, `db.sqlite3` (그 외 404). **Range**(단일 구간 `bytes=a-b`, `bytes=a-`, `bytes=-n`) → **206** + `Content-Range`. 응답 헤더 `Content-Length`, `ETag`, `Accept-Ranges: bytes`, `X-Content-SHA256`. 잘못된 Range 는 416 |
| DELETE | `/api/v2/export/snapshots/<id>/` | **204**, 번들 디렉터리 삭제 (실행 중이면 409 `export_in_progress`, 없으면 404) |

```bash
# QGP_KEY 는 export:read 스코프 키 (admin 소유). 평문을 명령행/로그에 남기지 말고 환경변수로만.
BASE=https://qw.jkintra.net/qg   # export 는 /ci-api/ 미제공 — 브라우저용 /api/v2/export/ 전용(서버 이관 완료, 이력용 예제)
H="Authorization: Bearer $QGP_KEY"

# 1) 생성 → id
SID=$(curl -fsS -X POST -H "$H" -H 'Content-Type: application/json' -d '{"compress":"auto"}' \
        "$BASE/api/v2/export/snapshots/" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# 2) ready 가 될 때까지 폴링 (progress_pct 참고)
curl -fsS -H "$H" "$BASE/api/v2/export/snapshots/$SID/"

# 3) 다운로드 — -C - 로 중단 후 Range 이어받기, X-Content-SHA256 / manifest 의 db.file_sha256 와 대조
curl -fsS -H "$H" -O "$BASE/api/v2/export/snapshots/$SID/download/manifest.json/"
curl -fsS -H "$H" -C - -o db.sqlite3.zst "$BASE/api/v2/export/snapshots/$SID/download/db.sqlite3.zst/"

# 4) 원격 번들 삭제
curl -fsS -X DELETE -H "$H" "$BASE/api/v2/export/snapshots/$SID/"
```

새 서버에서는 위 수동 호출 대신 `python manage.py fetch_snapshot_via_api --url https://… --key-env QGP_KEY` 가 폴링·이어받기·sha256 검증·번들 배치를 대신한다 (`docs/prd_server_migration_sync.md` §5.5·§6.3 경로 C).

---

### 4.6 담당자 관리 (스코프: `owners:manage`)

담당자 관리 화면(`/accounts/api-owners/` > API 탭)의 **API 엔드포인트별 담당자(FE/BE) 매핑**을 프로그램으로 관리한다. UI 의 추가/수정/삭제와 같은 데이터(`ApiEndpointOwner`)·같은 부수효과(수동 row 로 저장 → Sheet 동기화가 덮어쓰지 않음, URL→담당자 매칭 캐시 즉시 무효화, 활동 로그 기록)를 쓴다. `/ci-api/` 에서도 호출된다(Swagger 의 **담당자 관리** 그룹).

- **인증/권한**: API Key 만(세션 불가) + `owners:manage` **명시 스코프**(`*` 로는 통과 안 됨) + **키 소유자가 admin**. 요청 시점에 소유자 역할을 다시 본다. 키 발급 폼에서는 admin 에게만 이 스코프 체크박스가 보인다.
- **키 필드**: 필수 `repo`·`method`·`endpoint`, 선택 `category`·`area`·`mapped_app`·`fe_owner`·`be_owner`·`usage_count`·`step_ids`·`notes`. `method` 는 대소문자 무관(대문자 저장), `(method, endpoint)` 는 유일. `fe_owner`(4xx)/`be_owner`(5xx) 는 콤마로 여러 명(Slack 표시명 매칭). **담당자 미정이면 둘 다 생략·빈 문자열·`null` 로 등록 가능**(빈 값으로 저장 → 매칭 시 '담당자 매칭 없음' + 대체 담당자 경로), 나중에 `PATCH` 로 지정. UI 와 달리 `repo`/`category`/`area` 는 드롭다운 제한 없이 자유 입력.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/ci-api/owners/` | 목록. 필터 `q`·`repo`·`method`·`mapped_app`·`is_imported`, 페이지 `page`·`page_size`(기본 50, 최대 200). 페이지네이션 응답 `{count,next,previous,results}` |
| POST | `/ci-api/owners/` | 등록 → **201** 등록된 row. 같은 `(method,endpoint)` 가 있으면 **409** `already_exists` + `error.details.existing_id` |
| GET | `/ci-api/owners/<id>/` | 단건 |
| PATCH | `/ci-api/owners/<id>/` | 보낸 필드만 수정 (예: `{"fe_owner":"홍길동"}`) |
| PUT | `/ci-api/owners/<id>/` | 전체 수정(필수 필드 모두 필요) |
| DELETE | `/ci-api/owners/<id>/` | **204** |

```bash
# QGP_KEY 는 owners:manage 스코프 키 (admin 소유). 평문은 환경변수로만.
curl -s -X POST "https://qw.jkintra.net/qg/ci-api/owners/" \
  -H "Authorization: Bearer $QGP_KEY" -H "Content-Type: application/json" \
  -d '{"repo":"JK PC","method":"POST","endpoint":"/member/users/edit","fe_owner":"김프론트","be_owner":"김백엔드"}'
```

| HTTP | code | 의미 |
|---|---|---|
| 400 | `invalid_parameters` | 필수값 누락 / `method` 가 허용 목록 밖 / 빈 `endpoint`·`repo` |
| 403 | `scope_required` / `role_denied` | `owners:manage` 미명시(`*` 포함) / 소유자가 admin 아님 |
| 404 | `not_found` | 없는 id |
| 409 | `already_exists` | 같은 `(method, endpoint)` 가 이미 있음 |

## 5. 응답 스키마 (요약)

### 5.1 POST `/gates/<id>/runs/` — 201

```json
{
  "run_id": "uuid",
  "gate_id": "uuid",
  "seq_no": 87,
  "status": "building_image",
  "detail_url": "/qualitygates/runs/<uuid>/",
  "self_url": "/ci-api/runs/<uuid>/",
  "created_at": "2026-05-11T10:23:00+09:00"
}
```

### 5.2 GET `/runs/<id>/status/` — 200 + `X-QGP-Poll-After`

```json
{
  "run_id": "uuid",
  "seq_no": 87,
  "status": "testing",
  "phase": "running",
  "is_terminal": false,
  "result": null
}
```

terminal 도달 시:
```json
{
  "run_id": "uuid",
  "seq_no": 87,
  "status": "completed",
  "phase": "succeeded",
  "is_terminal": true,
  "result": "PASS"
}
```

`result` 값: `"PASS"` | `"FAIL"` | `"QG PASS"` | `"CANCELLED"` | `null`.

### 5.3 GET `/runs/<id>/` — 200 (단건 상세)

```json
{
  "run_id": "uuid",
  "gate": {"id": "...", "name": "auth-admin", "gate_type": "dev_stg",
           "folder": "...", "subfolder": "..."},
  "seq_no": 87,
  "status": "testing",
  "phase": "running",
  "result_label": null,
  "is_terminal": false,
  "auto_mode": true,
  "parallel_test": false,
  "image": {
    "present": true,
    "build_number": 542,
    "build_url": "https://jenkins/.../542",
    "started_at": "...", "completed_at": "...",
    "duration_ms": 123000,
    "git_branch": "release/release_0511",
    "git_commit": "a1b2c3d4...",
    "current_stage": "Build"
  },
  "deploy": {
    "build_number": 188,
    "build_url": "...",
    "started_at": "...", "completed_at": "...",
    "argocd": {
      "sync": "Synced", "health": "Healthy",
      "phase": "Succeeded", "synced_at": "...",
      "badge": {"kind": "synced", "level": "pass", "label": "Synced"}
    }
  },
  "test_runs": [
    {"order": 0, "suite_id": "...", "suite_name": "...",
     "status": "passed", "job_run_id": "...",
     "started_at": "...", "completed_at": "..."}
  ],
  "prod": null,
  "console_urls": {
    "image": "/ci-api/runs/<uuid>/console/image/",
    "deploy": "/ci-api/runs/<uuid>/console/deploy/",
    "prod": null
  },
  "triggered_by": {"id": "...", "username": "..."},
  "created_at": "..."
}
```

### 5.4 GET `/suite-runs/<id>/status/` — 200

진행 중:

```json
{
  "suite_run_id": "uuid",
  "seq_no": 12,
  "status": "running",
  "phase": "running",
  "result": null,
  "is_terminal": false,
  "passed_jobs": 0,
  "failed_jobs": 0,
  "total_jobs": 5
}
```

종료 (테스트 완료) — `result` 로 합/불 판정:

```json
{
  "suite_run_id": "uuid",
  "seq_no": 12,
  "status": "completed",
  "phase": "failed",
  "result": "failed",
  "is_terminal": true,
  "passed_jobs": 4,
  "failed_jobs": 1,
  "total_jobs": 5
}
```

> 폴링 측은 `is_terminal == true`(또는 `result != null`) 가 될 때까지 5초 간격으로
> 호출하고, 종료되면 `result` 가 `"passed"` 인지 `"failed"` 인지로 분기하면 된다.

### 5.5 콘솔 로그 — 200

```json
{
  "stage": "deploy",
  "build_number": 188,
  "build_url": "https://jenkins/.../188/console",
  "output": "...truncated string...",
  "length": 12345,
  "truncated": false
}
```

`?tail=N` 으로 마지막 N 글자만. tail 미지정 시 1MB 초과면 자동으로 마지막 50KB.

---

## 6. 폴링 모델

### 6.1 권장 cadence

```bash
KEY="qgp_..."
BASE=https://qw.jkintra.net/qg/ci-api
RUN_ID="..."

while true; do
  RESP=$(curl -s -H "Authorization: Bearer $KEY" \
              -D /tmp/headers \
              "$BASE/runs/$RUN_ID/status/")
  PHASE=$(echo "$RESP" | jq -r .phase)
  TERMINAL=$(echo "$RESP" | jq -r .is_terminal)
  WAIT=$(grep -i '^X-QGP-Poll-After:' /tmp/headers | awk '{print $2}' | tr -d '\r')

  echo "[$(date +%T)] phase=$PHASE terminal=$TERMINAL"
  [ "$TERMINAL" = "true" ] && break
  sleep "${WAIT:-5}"
done

# 종료 후 상세 결과
curl -s -H "Authorization: Bearer $KEY" "$BASE/runs/$RUN_ID/" | jq .
```

### 6.2 `X-QGP-Poll-After`

응답 헤더로 다음 폴링까지 대기할 초를 알려준다.

- `5` — 진행 중, 5초 후 재폴링
- `0` — terminal, 폴링 중단

5 미만 권장하지 않음 — 서버의 Argo CD 호출이 4초 쿨다운으로 막혀 실효 없음.

### 6.3 진행 동기화

`GET /runs/<id>/` 와 `GET /runs/<id>/status/` 모두 호출 시점에 서버가 내부 `_poll_gate_run` 을 실행한다. 즉 **외부에서 GET 만 때려도 파이프라인이 멈추지 않는다.** 별도 사용자가 포털 화면을 열어둘 필요 없음.

### 6.4 `awaiting_input` 단계 처리

`phase == "awaiting_input"` 이면 자동으로 다음 단계로 못 넘어가는 상태:
- `status=waiting_deploy` — `POST /runs/<id>/deploy/` 로 수동 deploy 트리거
- `status=qg_passed` — `POST /runs/<id>/prod-deploy/` 로 수동 prod 트리거

게이트의 `auto_mode=True` 이고 Dev→Stg 타입이면 이 단계가 자동 건너뜀.

### 6.5 `retry_count > 1` 인 Suite Run 폴링 주의사항

Suite 의 재시도 횟수(`retry_count`)가 2 이상이면 **첫 번째 Job 실패가 곧 Suite Run 종료가 아니다.** 포털이 자동으로 재트리거하며, 설정된 횟수 모두 FAILURE 가 나야 `is_terminal=true` 로 전환된다.

따라서 Suite Run 폴링 측은 반드시 `is_terminal=true`(또는 `result != null`) 를 기준으로 종료를 판정해야 한다. 중간 시도 실패 시에도 `status` 는 `running` 으로 유지되고 `result` 는 `null` 이다.

```text
# 재시도 중인 응답 예시 (retry_count=3, 첫 번째 실패 후)
{
  "status": "running",
  "phase": "running",
  "result": null,
  "is_terminal": false
}
```

> 응답 스키마 자체는 변경 없음 — `is_terminal` / `result` 를 그대로 사용하면 된다.
> 재시도 경과는 포털 Activity Log 의 `RETRY` 이벤트로 확인 가능.

---

## 7. 에러 응답

모든 4xx/5xx 응답은 동일한 구조:

```json
{
  "error": {
    "code": "run_already_active",
    "message": "이 게이트에는 이미 진행 중인 Run 이 있습니다.",
    "details": {"active_run_id": "uuid", "status": "testing"}
  }
}
```

판별은 `HTTP status + error.code` 조합.

### 7.1 인증 / 권한 / 공통 (DRF 레이어)

| HTTP | code | 의미 |
|---|---|---|
| 400 | `invalid_parameters` | body 파라미터 형식 오류 (예: `job_id` 가 해당 suite 의 Job 이 아님). `console`/`argocd` 의 잘못된 `stage` 도 포함 |
| 401 | `not_authenticated` | Authorization 헤더 없음/형식 오류 |
| 401 | `api_key_invalid` | 키 매칭 실패 |
| 401 | `api_key_revoked` | 회수된 키 |
| 401 | `api_key_expired` | 만료된 키 |
| 401 | `user_inactive` | 키는 유효하나 소유 사용자가 비활성 |
| 403 | `scope_required` | 키에 해당 스코프 없음 |
| 403 | `role_denied` | Viewer 가 write(`runs:control`) 시도 |
| 403 | `wildcard_admin_only` | `*` 스코프 키를 admin 아닌 사용자가 사용 |
| 404 | `not_found` | 리소스 없음 |
| 405 | `method_not_allowed` | 잘못된 HTTP 메서드 |
| 429 | `rate_limited` | Read 120/min, Write 30/min 초과. `Retry-After` 참고 |
| 500 | `internal_error` | 미분류 서버 예외 |

#### 7.1a 내보내기 API (`/api/v2/export/…`, §4.5) 전용 코드

(브라우저용 `/api/v2/` 전용 — `/ci-api/` 에 없음. 서버 이관 완료로 이력/참고용.)

| HTTP | code | 의미 |
|---|---|---|
| 400 | `unsupported` | 허용 필드(`compress`) 외 필드 — `include_config` 등 config/.env/secrets 요청 포함 |
| 400 | `invalid_parameters` | `compress` 값이 `gzip\|zstd\|auto` 가 아니거나 본문이 JSON 객체가 아님 |
| 403 | `scope_required` / `role_denied` | `export:read` 미명시 / 소유자가 admin 아님 |
| 404 | `not_found` | id 형식 오류(uuid 정규형이 아님)·없음·허용 목록에 없는 파일명·심볼릭 링크 |
| 409 | `export_in_progress` | 이미 실행 중인 스냅샷이 있음(생성) / 실행 중이라 삭제 불가(DELETE) |
| 409 | `export_not_ready` | `ready` 가 아닌 스냅샷(`running`/`failed`/`expired`)을 다운로드 시도 |
| 416 | `range_not_satisfiable` | 잘못된 Range (다중 구간·범위 초과·형식 오류). `Content-Range: bytes */<size>` 포함 |
| 503 | `export_unavailable` | 내보낼 DB 파일을 찾을 수 없음 |

### 7.2 Quality Gate Run 비즈니스 룰 (`GateRunError`)

| HTTP | code | 발생 시점 |
|---|---|---|
| 409 | `active_run_exists` | 게이트에 이미 ACTIVE 상태 Run 이 있어 새 Run 트리거 차단 (`POST /gates/<id>/runs/`) |
| 409 | `run_not_actionable` | 현재 상태에서 `deploy`/`prod-deploy`/`rerun-deploy`/`rerun-prod` 액션 불가 (`details.current_status` 포함) |
| 409 | `cannot_cancel` | 현재 상태에서 취소 불가 (`POST /runs/<id>/cancel/`) |
| 409 | `cannot_rerun` | 현재 상태에서 처음부터 재실행 불가 (`POST /runs/<id>/rerun/`) |
| 409 | `cannot_rerun_test` | 현재 상태에서 테스트만 재실행 불가 (`POST /runs/<id>/rerun-test/`) |
| 409 | `invalid_state` | deploy/prod 트리거를 허용되지 않는 상태에서 호출 (서비스 레이어 가드) |
| 400 | `invalid_app_image` | `APP_IMAGE` 가 비어있거나 태그 누락 — 모달에서 올바른 이미지 선택 필요 |
| 400 | `invalid_stage` | `console`/`params`/`argocd` 의 `stage` 가 허용 목록 밖(또는 이미지 빌드 Job 없는 게이트) |
| 502 | `jenkins_trigger_failed` | Jenkins 배포 Job 트리거 호출 실패 |
| 502 | `jenkins_error` | 파라미터 스키마 조회 등 Jenkins 통신 실패 (`details.stage`) |

### 7.3 Test Suite Run 비즈니스 룰 (`SuiteRunError`)

| HTTP | code | 발생 시점 |
|---|---|---|
| 409 | `all_workers_busy` | Suite **idle 트리거(기본)**에서 검증된 idle worker 가 없음 — Run 미생성. 잠시 후 재시도 (`details.candidate_jobs`) |
| 409 | `no_active_jobs` | Suite 트리거인데 실행할 활성 대상 Job 이 없음 |
| 409 | `run_already_active` | Suite **`run_all=true`(전체 체인)** 트리거인데 이미 진행 중인 Run 존재 (`details.suite_run_id`) |
| 409 | `not_actionable` | 이미 종료(completed)된 Suite Run 을 취소 시도 (`details.status`) |
| 400 | `invalid_parameters` | `job_id` 가 해당 suite 의 Job 이 아님 (`details.job_id`) |

> **주의** — Gate 트리거 충돌은 `active_run_exists`, Suite 전체 체인 충돌은
> `run_already_active` 로 **코드가 다르다**. 같은 "진행 중 Run" 이라도 분기해서 본다.

---

## 8. 외부 자동화 시나리오 예제

### 8.1 Jenkins Declarative Pipeline 에서 호출

```groovy
pipeline {
  agent any
  environment {
    QGP_KEY = credentials('qgp-api-key')   // Jenkins credentials store
    QGP_BASE = 'https://qw.jkintra.net/qg/ci-api'
  }
  stages {
    stage('Trigger Quality Gate') {
      steps {
        script {
          // 1) Run 트리거
          def resp = sh(returnStdout: true, script: """
            curl -sf -X POST -H "Authorization: Bearer \$QGP_KEY" \\
              -H 'Content-Type: application/json' \\
              -d '{"parameters":{"BRANCH_NAME":"${env.BRANCH_NAME}"}}' \\
              "${QGP_BASE}/gates/${GATE_ID}/runs/"
          """).trim()
          def trigger = readJSON(text: resp)
          env.QGP_RUN_ID = trigger.run_id
          echo "Run started: ${env.QGP_RUN_ID} (seq #${trigger.seq_no})"

          // 2) 폴링
          while (true) {
            sleep 5
            def status = sh(returnStdout: true, script: """
              curl -sf -H "Authorization: Bearer \$QGP_KEY" \\
                "${QGP_BASE}/runs/${env.QGP_RUN_ID}/status/"
            """).trim()
            def s = readJSON(text: status)
            echo "phase=${s.phase} status=${s.status}"
            if (s.is_terminal) {
              if (s.phase == 'succeeded') {
                echo "QG passed (${s.result})"
                break
              } else {
                error("Quality Gate failed: phase=${s.phase} result=${s.result}")
              }
            }
          }
        }
      }
    }
  }
}
```

### 8.2 GitHub Actions

```yaml
- name: Trigger QualityGate
  id: qg-trigger
  run: |
    RESP=$(curl -sf -X POST \
      -H "Authorization: Bearer ${{ secrets.QGP_KEY }}" \
      -H "Content-Type: application/json" \
      -d '{"parameters":{"BRANCH_NAME":"${{ github.ref_name }}"}}' \
      "https://qw.jkintra.net/qg/ci-api/gates/${{ vars.QGP_GATE_ID }}/runs/")
    echo "run_id=$(echo "$RESP" | jq -r .run_id)" >> "$GITHUB_OUTPUT"

- name: Wait for QualityGate
  run: |
    RUN_ID="${{ steps.qg-trigger.outputs.run_id }}"
    while true; do
      STATUS=$(curl -sf -H "Authorization: Bearer ${{ secrets.QGP_KEY }}" \
        "https://qw.jkintra.net/qg/ci-api/runs/$RUN_ID/status/")
      PHASE=$(echo "$STATUS" | jq -r .phase)
      TERMINAL=$(echo "$STATUS" | jq -r .is_terminal)
      echo "phase=$PHASE"
      if [ "$TERMINAL" = "true" ]; then
        [ "$PHASE" = "succeeded" ] && exit 0 || exit 1
      fi
      sleep 5
    done
```

### 8.3 Python

```python
import time, requests

BASE = "https://qw.jkintra.net/qg/ci-api"
KEY = "qgp_..."  # 환경변수에서
HEADERS = {"Authorization": f"Bearer {KEY}"}

def trigger_and_wait(gate_id, parameters):
    # 1) 트리거
    r = requests.post(
        f"{BASE}/gates/{gate_id}/runs/",
        json={"parameters": parameters},
        headers=HEADERS,
    )
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print(f"Run started: {run_id}")

    # 2) 폴링
    while True:
        r = requests.get(f"{BASE}/runs/{run_id}/status/", headers=HEADERS)
        r.raise_for_status()
        s = r.json()
        wait = int(r.headers.get("X-QGP-Poll-After", "5"))
        print(f"phase={s['phase']} status={s['status']}")
        if s["is_terminal"]:
            return s
        time.sleep(wait)


result = trigger_and_wait(
    gate_id="...",
    parameters={"BRANCH_NAME": "release/release_0511"},
)
if result["phase"] != "succeeded":
    raise SystemExit(f"QG failed: {result['result']}")
```

---

## 9. 보안

- 평문 키는 발급 직후 응답 본문에 1회만 노출. DB 에는 SHA-256 해시 + 앞 8자 prefix 만.
- `Authorization` 헤더는 절대 로그/Slack/Activity Log 에 남기지 마라 — 키 회수는 즉시 가능하지만 노출 자체가 자산.
- 사용 중지된 키는 즉시 회수 (`/accounts/api-keys/`). 회수 후 같은 키로 호출 → 401 `api_key_revoked`.
- 만료 디폴트 90일. 운영 환경 키는 90~180일 권장 + 만료 임박 시 회수 후 신규 발급으로 rotate.

---

## 10. 자주 묻는 것 (FAQ)

- **세션 인증과 병행 가능?** 가능. 같은 사용자가 포털 세션으로 로그인된 상태에서도
  `/api/v2/` 호출이 동작 — `request.user` 가 그대로 매핑된다(`/ci-api/` 는 쿠키를 무시하므로 해당 없음). 스코프 검사는 **API Key
  인증인 경우에만** 적용(세션 인증은 스코프 검사 없음).
- **Webhook outbound(이벤트 푸시)?** 현재 미지원 — Slack 알림만 발송. 외부 시스템은
  `GET .../status/` 폴링으로 진행을 추적한다. 차후 PRD 로 도입 검토.
- **OpenAPI / Swagger 스펙 파일?** drf-spectacular 로 제공된다 — 브라우저 `/qg/api/v2/docs/`
  (Swagger UI) · `/redoc/` · `/schema/`(로그인 필요). 문서에 표시되는 경로·예시는 프로그램 호출
  주소인 `/ci-api/` 기준이다(§2.1). 본 파일은 시나리오·에러 카탈로그·예제를 담은 보완 문서.
- **API Key per-IP allowlist?** 현재 미지원. 게이트웨이(nginx) 레벨에서 차단 권장.
- **Suite 한 번 호출에 worker 가 5개 다 도나?** 아니다. `job_id` 생략·`run_all` 미지정
  (기본)이면 **idle worker 1개만** 실행(`total_jobs=1`). 전체 5개를 순차로 돌리려면
  `run_all=true` 를 명시해야 한다 (§4.3).

---

## 11. 변경 이력

- **2026-10-21** — **담당자 관리 API** (`/api/v2/owners/` · `/ci-api/owners/`, §4.6) 추가: 담당자(FE/BE) 매핑 목록·등록·수정·삭제. 신규 스코프 `owners:manage`(명시 부여 전용 — `*` 미포함, admin 소유 키만).
- **2026-10-15** — Swagger UI/ReDoc 의 경로·설명·예시를 **`/ci-api/` 로 표기**(Try it out 도 `/qg/ci-api/…` 호출, 키 검증 배지도 `/ci-api/me/`). 실제 라우팅(`/api/v2/` 마운트)은 불변이고 문서 페이지·export 는 `/api/v2/` 에만 있다. 본 문서 §1 다이어그램·응답 예시의 링크를 `/ci-api/` 로 정정, export(§4.5·§7.1a)는 브라우저용·이관 완료(이력)임을 명시.
- **2026-10-12** — `/ci-api/`(게이트웨이 로그인 예외 경로, API Key Bearer/Basic 전용) 도입(§2.1). 서버 이관(2026-10)으로 구 주소 `qualitygate.jkintra.net` 종료.
- **2026-10-09** — 서버 이관용 **DB 스냅샷 내보내기 API**(`/api/v2/export/snapshots/`, §4.5) 추가: 신규 스코프 `export:read`(명시 부여 전용 — `*` 미포함, admin 소유 키만), Range 이어받기 다운로드, 24시간 자동 만료, 동시 1개, config 미포함. 내보내기 전용 에러 코드는 §7.1a.
- **2026-06-08** — Suite 트리거 기본을 **idle worker 1개**로 변경(`run_all=true` 만 전체
  순차 체인) + `all_workers_busy`(409) 추가. 외부 호출자 문서를 본 파일 1개로 통합
  (`api_v2_quickstart.md` 흡수·삭제) + 에러 카탈로그를 코드 기준(`active_run_exists` /
  `run_already_active` 분리, Gate/Suite 비즈니스 코드 전체)으로 현행화.
- **2026-05-11** — 1차 릴리즈 (Phase A~G + light status). 16개 엔드포인트 + phase 5단 매핑 + `X-QGP-Poll-After`.
