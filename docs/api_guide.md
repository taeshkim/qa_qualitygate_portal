# QualityGate Portal `/api/v2/` API Guide

> 외부 시스템(Jenkins / GitHub Actions / 백오피스 / 사내 대시보드) 이 QualityGate
> 포털의 Run 을 트리거하고 진행을 추적할 수 있는 REST API.
>
> - PRD: `prd_api.md`
> - 시나리오 중심 빠른 시작: `docs/api_v2_quickstart.md`
> - 본 문서: 엔드포인트 reference + 응답 스키마 + 폴링 모델 + 상태 머신

운영 도메인: `https://qualitygate.jkintra.net`

---

## 1. 핵심 호출 패턴

```
        ┌──────────────────────────────────────────────────────────┐
        │ POST /api/v2/gates/<gate_id>/runs/                       │
        │   → 201 { "run_id": "<uuid>", "status": "...",           │
        │           "self_url": "/api/v2/runs/<uuid>/", ... }      │
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

`Authorization: Bearer qgp_<8자 prefix>_<32자 hex>`

키 발급/회수 UI: `/accounts/api-keys/` (본인) · `/accounts/api-keys/admin/` (admin).

```bash
# 인증/스코프 확인
curl -H "Authorization: Bearer $QGP_KEY" \
  https://qualitygate.jkintra.net/api/v2/me/
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
| `*` | 전체 (admin 만 부여 가능) |

### Rate limit

| 종류 | 한도 | 비고 |
|---|---|---|
| Read | 120 req/min | `meta:read` / `runs:read` |
| Write | 30 req/min | `runs:trigger` / `runs:control` |

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

| `status` | `phase` | `is_terminal` |
|---|---|---|
| `pending` | `queued` | false |
| `running` | `running` | false |
| `completed` (passed_jobs > 0, failed_jobs = 0) | `succeeded` | true |
| `completed` (failed_jobs > 0) | `failed` | true |

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
| POST | `/api/v2/suites/<uuid>/runs/` | `runs:trigger` | Suite Run 트리거 — body: `{"job_id": "..."}` (필수) |
| GET | `/api/v2/suite-runs/<uuid>/` | `runs:read` | Suite Run 단건 |
| GET | `/api/v2/suite-runs/<uuid>/status/` | `runs:read` | **Suite Run 가벼운 상태 (폴링용)** |
| POST | `/api/v2/suite-runs/<uuid>/cancel/` | `runs:control` | 취소 |
| GET | `/api/v2/suite-job-runs/<uuid>/console/` | `runs:read` | Suite Job 콘솔 로그. `?tail=N` |

### 4.4 기타

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/v2/health/` | 인증 불요. `{"status":"ok","version":"v2.0"}` |
| GET | `/api/v2/me/` | 현재 키 + 사용자 + 사용 가능 스코프 |

---

## 5. 응답 스키마 (요약)

### 5.1 POST `/gates/<id>/runs/` — 201

```json
{
  "run_id": "uuid",
  "gate_id": "uuid",
  "seq_no": 87,
  "status": "building_image",
  "detail_url": "/qualitygates/runs/<uuid>/",
  "self_url": "/api/v2/runs/<uuid>/",
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
    "image": "/api/v2/runs/<uuid>/console/image/",
    "deploy": "/api/v2/runs/<uuid>/console/deploy/",
    "prod": null
  },
  "triggered_by": {"id": "...", "username": "..."},
  "created_at": "..."
}
```

### 5.4 GET `/suite-runs/<id>/status/` — 200

```json
{
  "suite_run_id": "uuid",
  "status": "running",
  "phase": "running",
  "is_terminal": false,
  "passed_jobs": 0,
  "failed_jobs": 0,
  "total_jobs": 1
}
```

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
BASE=https://qualitygate.jkintra.net
RUN_ID="..."

while true; do
  RESP=$(curl -s -H "Authorization: Bearer $KEY" \
              -D /tmp/headers \
              "$BASE/api/v2/runs/$RUN_ID/status/")
  PHASE=$(echo "$RESP" | jq -r .phase)
  TERMINAL=$(echo "$RESP" | jq -r .is_terminal)
  WAIT=$(grep -i '^X-QGP-Poll-After:' /tmp/headers | awk '{print $2}' | tr -d '\r')

  echo "[$(date +%T)] phase=$PHASE terminal=$TERMINAL"
  [ "$TERMINAL" = "true" ] && break
  sleep "${WAIT:-5}"
done

# 종료 후 상세 결과
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/v2/runs/$RUN_ID/" | jq .
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

### 코드 카탈로그

| HTTP | code | 의미 |
|---|---|---|
| 400 | `invalid_parameters` | body 파라미터 형식 오류 |
| 400 | `job_required` | Suite Run 트리거에 `job_id` 없음 |
| 401 | `not_authenticated` | Authorization 헤더 없음/형식 오류 |
| 401 | `api_key_invalid` | 키 매칭 실패 |
| 401 | `api_key_revoked` | 회수된 키 |
| 401 | `api_key_expired` | 만료된 키 |
| 403 | `scope_required` | 키에 해당 스코프 없음 |
| 403 | `role_denied` | Viewer 가 write 시도 |
| 403 | `wildcard_admin_only` | `*` 스코프 키를 admin 아닌 사용자가 사용 |
| 404 | `not_found` | 리소스 없음 |
| 405 | `method_not_allowed` | 잘못된 HTTP 메서드 |
| 409 | `run_already_active` | 이미 ACTIVE 상태 Run 존재 — 새 Run 차단 |
| 409 | `run_not_actionable` | 현재 상태에서 이 액션 불가 (`details.current_status` 포함) |
| 429 | `rate_limited` | Read 120/min, Write 30/min 초과. `Retry-After` 참고 |
| 500 | `internal_error` | 미분류 서버 예외 |

---

## 8. 외부 자동화 시나리오 예제

### 8.1 Jenkins Declarative Pipeline 에서 호출

```groovy
pipeline {
  agent any
  environment {
    QGP_KEY = credentials('qgp-api-key')   // Jenkins credentials store
    QGP_BASE = 'https://qualitygate.jkintra.net'
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
              "${QGP_BASE}/api/v2/gates/${GATE_ID}/runs/"
          """).trim()
          def trigger = readJSON(text: resp)
          env.QGP_RUN_ID = trigger.run_id
          echo "Run started: ${env.QGP_RUN_ID} (seq #${trigger.seq_no})"

          // 2) 폴링
          while (true) {
            sleep 5
            def status = sh(returnStdout: true, script: """
              curl -sf -H "Authorization: Bearer \$QGP_KEY" \\
                "${QGP_BASE}/api/v2/runs/${env.QGP_RUN_ID}/status/"
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
      "https://qualitygate.jkintra.net/api/v2/gates/${{ vars.QGP_GATE_ID }}/runs/")
    echo "run_id=$(echo "$RESP" | jq -r .run_id)" >> "$GITHUB_OUTPUT"

- name: Wait for QualityGate
  run: |
    RUN_ID="${{ steps.qg-trigger.outputs.run_id }}"
    while true; do
      STATUS=$(curl -sf -H "Authorization: Bearer ${{ secrets.QGP_KEY }}" \
        "https://qualitygate.jkintra.net/api/v2/runs/$RUN_ID/status/")
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

BASE = "https://qualitygate.jkintra.net"
KEY = "qgp_..."  # 환경변수에서
HEADERS = {"Authorization": f"Bearer {KEY}"}

def trigger_and_wait(gate_id, parameters):
    # 1) 트리거
    r = requests.post(
        f"{BASE}/api/v2/gates/{gate_id}/runs/",
        json={"parameters": parameters},
        headers=HEADERS,
    )
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print(f"Run started: {run_id}")

    # 2) 폴링
    while True:
        r = requests.get(f"{BASE}/api/v2/runs/{run_id}/status/", headers=HEADERS)
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

## 10. 변경 이력

- **2026-05-11** — 1차 릴리즈 (Phase A~G + light status). 16개 엔드포인트 + phase 5단 매핑 + `X-QGP-Poll-After`.
