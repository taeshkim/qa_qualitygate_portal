# `/api/v2/` 외부 호출자용 Quickstart

> 정식 명세는 `prd_api.md`. 이 문서는 외부 자동화(Jenkins / GitHub Actions /
> 백오피스 / 사내 대시보드 등) 가 빠르게 시작할 수 있는 가이드.

운영 도메인: `https://qualitygate.jkintra.net`

---

## 1. API Key 발급

1. 포털에 로그인 → 우측 사이드바 **API Keys** → **새 키 발급**.
2. 이름, 스코프 (`meta:read` / `runs:read` / `runs:trigger` / `runs:control`,
   `*` 는 admin 만), 만료 (30/90/180/365/무제한, **디폴트 90일**) 선택.
3. 발급 직후 **평문**이 한 번만 노출된다 (`qgp_<8자 prefix>_<32hex secret>`).
   안전한 곳에 즉시 복사. 이후 어떤 화면에서도 재조회 불가 (해시만 보관).
4. 잃어버린 키는 회수(revoke) 후 새로 발급.

```
Authorization: Bearer qgp_a1b2c3d4_4f3e2d1c8c1b2a394e9d7c5b3a2f1e0d
```

---

## 2. 인증 확인

```bash
curl -H "Authorization: Bearer $QGP_KEY" \
  https://qualitygate.jkintra.net/api/v2/me/
```

응답에 `user`, `api_key.scopes`, `available_scopes` 가 포함되면 정상.

`health` 는 인증 없이 호출 가능 — 운영 모니터링용.

```bash
curl https://qualitygate.jkintra.net/api/v2/health/
# {"status":"ok","version":"v2.0"}
```

---

## 3. 시나리오 — Jenkins 가 게이트를 1회 실행

### 3.1 게이트 찾기

```bash
curl -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/gates/?q=auth-admin"
```

응답:
```json
{
  "count": 1,
  "results": [{"id": "uuid", "name": "auth-admin", "gate_type": "dev_stg", ...}]
}
```

### 3.2 새 Run 트리거

스코프 `runs:trigger` 필요.

```bash
curl -X POST -H "Authorization: Bearer $QGP_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "skip_image_build": false,
        "parameters": {
          "BRANCH_NAME": "release/release_0511",
          "APP_IMAGE": "registry/foo:123-abc"
        }
      }' \
  "https://qualitygate.jkintra.net/api/v2/gates/<gate_id>/runs/"
```

응답 (201):
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

### 3.3 폴링 — 진행 동기화는 GET 으로

```bash
curl -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/runs/<run_id>/"
```

매 호출 시 서버가 내부적으로 `_poll_gate_run` 을 실행 — 외부에서 GET 만
때려도 진행이 멈추지 않는다. 권장 폴링 간격은 응답 헤더 **`X-QGP-Poll-After`**
(초 단위) 를 따른다:

- `5` — 다음 폴링까지 대기 (진행 중).
- `0` — terminal 도달, 폴링 중단.

응답의 `status`, `is_terminal`, `result_label`, 단계별 `image/deploy/test_runs/prod`
를 보고 후속 동작 결정.

### 3.4 콘솔 로그 조회 (실패 디버깅)

```bash
# 마지막 2000 글자만
curl -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/runs/<run_id>/console/deploy/?tail=2000"
```

`stage` ∈ `image` / `deploy` / `prod`. tail 미지정 시 전체 응답, 다만 **1 MB 초과 시 자동으로 마지막 50 KB 만 반환** (`truncated: true`).

---

## 4. 시나리오 — 단독 Suite Run

```bash
# Suite 찾기
curl -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/suites/?q=integration"

# 트리거 (job_id 필수 — 디폴트 정책 없음)
curl -X POST -H "Authorization: Bearer $QGP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"job_id": "<job_uuid>"}' \
  "https://qualitygate.jkintra.net/api/v2/suites/<suite_id>/runs/"

# 폴링
curl -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/suite-runs/<suite_run_id>/"

# 콘솔 (각 job_run 별로)
curl -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/suite-job-runs/<job_run_id>/console/"
```

---

## 5. 시나리오 — 제어 (취소 / 재실행 / Argo CD 재시도)

스코프 `runs:control` 필요. 모든 응답은 액션 후 Run 의 detail 본문 (200 OK,
`rerun` 만 새 Run 으로 201, `argocd retry` 는 202).

```bash
# 취소
curl -X POST -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/runs/<run_id>/cancel/"

# 처음부터 재실행 (새 Run id 반환)
curl -X POST -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/runs/<run_id>/rerun/"

# 테스트만 재실행
curl -X POST -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/runs/<run_id>/rerun-test/"

# 검수대상/검수통과 배포만 재실행 — DEPLOY_FAILED / PROD_DEPLOY_FAILED 만 허용
curl -X POST -H "Authorization: Bearer $QGP_KEY" -H "Content-Type: application/json" \
  -d '{"parameters": {"BRANCH_NAME": "release/release_0511"}}' \
  "https://qualitygate.jkintra.net/api/v2/runs/<run_id>/rerun-deploy/"

# WAITING_DEPLOY / QG_PASSED 에서 수동 다음 단계 트리거
curl -X POST -H "Authorization: Bearer $QGP_KEY" -H "Content-Type: application/json" \
  -d '{"parameters": {"BRANCH_NAME": "release/release_0511"}}' \
  "https://qualitygate.jkintra.net/api/v2/runs/<run_id>/deploy/"

# Argo CD 강제 재시도 (stage = deploy | prod)
curl -X POST -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/runs/<run_id>/argocd/deploy/retry/"
```

---

## 6. 메타 조회 (대시보드 용)

```bash
# 게이트 목록 + 최근 Run 1건 + suite 목록 함께
curl -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/gates/<gate_id>/?include=latest_run,test_suites"

# 게이트 Run 목록 (필터)
curl -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/gates/<gate_id>/runs/?status=failed&limit=20"

# Jenkins Job 파라미터 스키마 (Active Choices / Git Parameter 포함)
curl -H "Authorization: Bearer $QGP_KEY" \
  "https://qualitygate.jkintra.net/api/v2/gates/<gate_id>/params/?stage=deploy"
# 120초 캐시. refresh=1 로 강제 재조회.
```

---

## 7. 에러 응답 표준

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

판별은 `HTTP status + error.code` 조합. `message` 는 사람용.

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
| 429 | `rate_limited` | Read 120/min, Write 30/min 초과. `Retry-After` 헤더 참고 |
| 500 | `internal_error` | 미분류 서버 예외 — 운영 팀 알림 |

---

## 8. Rate Limit / 폴링 권장

- 키 단위 read: **120 req/min**, write: **30 req/min**. 429 시 `Retry-After` 초 단위.
- 폴링은 응답 헤더 `X-QGP-Poll-After` (초) 를 따른다 — terminal 도달 시 `0` 으로 줄어들면 stop. 5초 미만은 권장하지 않음 (Argo CD 4초 쿨다운에 의미 없음).

---

## 9. 자주 묻는 것

- **세션 인증과 병행 가능?** 가능. 같은 사용자가 포털 세션으로 로그인된 상태에서도 `/api/v2/` 호출이 동작 — `request.user` 가 그대로 매핑. 스코프 검사는 키 인증인 경우에만 적용.
- **Webhook outbound (이벤트 푸시)?** 현재 미지원 — Slack 알림만 발송. 차후 PRD 로 도입 검토.
- **OpenAPI / Swagger 스펙 파일?** 1차 릴리즈에서는 수기 문서만. drf-spectacular 도입은 후속.
- **API Key per-IP allowlist?** 현재 미지원. nginx/ALB 레벨에서 차단 권장.

---

## 10. 변경 이력

- 2026-05-11 — 1차 릴리즈 (Phase A~G). 전체 엔드포인트 + 표준 에러 + 폴링 헤더.
