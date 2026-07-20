# Quality Gate Portal — PRD (Product Requirements Document)

**Product Name:** Quality Gate Portal (QGP)
**Version:** v1.0
**Author:** QPE Team (김태식)
**Date:** 2026-04-03
**Tech Stack:** Python Django + PostgreSQL + Celery + Redis
**Status:** Draft

---

## 1. 개요 (Overview)

### 1.1 배경

현재 배포 Quality Gate는 Jenkins 파이프라인, Slack 알림, Notion 대시보드 등 **다수의 시스템에 분산**되어 운영되고 있다. 
QG 결과 확인, 이력 조회, 대시보드 열람, 예외 승인 등이 각각 다른 도구에서 이루어지고 있어 **운영 효율성과 가시성에 한계**가 있다.

Quality Gate Portal은 이러한 분산된 QG 운영 정보를 **하나의 웹 포털로 통합**하여, QPE팀과 트라이브 개발팀 모두가 QG 상태를 실시간으로 파악하고, 
데이터 기반 의사결정을 할 수 있도록 지원하는 내부 플랫폼이다.

### 1.2 Product Vision

> "코드가 머지되면 프로덕션까지, 품질이 시스템으로 통제되는 상태를 **하나의 포털에서 확인하고 관리**할 수 있다."

### 1.3 핵심 변경 사항

| 구분 | AS-IS | TO-BE (QGP 도입 후) |
|------|-------|---------------------|
| QG 결과 확인 | Slack 알림 + Jenkins UI 분산 확인 | 포털 대시보드에서 통합 확인 |
| QG 이력 조회 | Jenkins 빌드 히스토리 수동 탐색 | 포털에서 서비스/환경/기간별 검색 |
| QG 대시보드 | Notion 수동 업데이트 (주간) | 포털에서 실시간 자동 집계 |
| 예외 승인 관리 | Slack DM/쓰레드 기반 | 포털에서 요청-승인 워크플로우 |
| 품질 리포트 | QPE가 수동 작성 | 포털에서 자동 생성 (주간/월간) |
| 에스컬레이션 추적 | Slack 쓰레드 기반 (유실 가능) | 포털에서 체계적 이력 관리 |

---

## 2. 사용자 및 역할 (Users & Roles)

### 2.1 사용자 역할 정의

| 역할 | 설명 | 주요 액션 |
|------|------|----------|
| **QPE Admin** | QPE 팀 리드. 전체 QG 설정 및 정책 관리 | QG 기준 설정, 예외 승인, 리포트 발행, 시스템 관리 |
| **QPE Ops** | QPE Ops 엔지니어. QG 운영 및 테스트 관리 | 테스트 스위트 관리, 환경이슈 분류, 플레이크 관리, Re-run 실행 |
| **Tribe Lead** | 트라이브 개발 리드 | QG 결과 확인, 긴급배포 예외 요청, 에스컬레이션 |
| **Tribe Developer** | 트라이브 개발자 | QG 결과 확인, 실패 원인 파악, Re-run 요청 |
| **Approver** | Prd 배포 승인자 (지정) | Prd 배포 승인/거부 |
| **Viewer** | 경영진/Chapter Lead | 대시보드 열람, 월간 리포트 조회 |

### 2.2 인증/인가

- SSO 연동 (사내 OAuth/LDAP 기반)
- Django 기본 인증 + 역할 기반 접근제어 (RBAC)
- Slack OAuth 연동 (Slack 사용자 매핑)

---

## 3. 핵심 기능 (Core Features)

### 3.1 대시보드 (Dashboard)

**목적:** QG 상태를 한눈에 파악할 수 있는 메인 화면

#### 3.1.1 실시간 QG 현황 보드

- 서비스별 (잡코리아, 알바몬, 게임잡, 하이어링센터) × 환경별 (Dev, Stg, Prd) 매트릭스
- 각 셀에 최근 QG 결과 표시 (✅ PASS / ❌ FAIL / ⏳ Running / ⚪ N/A)
- 클릭 시 해당 QG 실행 상세 페이지로 이동

#### 3.1.2 핵심 메트릭 카드

| 메트릭 | 설명 | 갱신 주기 |
|--------|------|----------|
| QG 통과율 (Pass Rate) | 서비스별 최근 7일 QG 통과율 | 실시간 |
| 플레이크율 (Flake Rate) | 환경 이슈/플레이크로 인한 실패 비율 | 일간 |
| 평균 QG 소요시간 | 테스트 실행~판정 완료까지 평균 시간 | 실시간 |
| 배포 빈도 vs 차단율 | 전체 배포 대비 QG 차단 비율 | 주간 |
| Prod Escape Rate | 프로덕션 결함 유출율 | 월간 |

#### 3.1.3 트렌드 차트

- 서비스별 QG 통과율 추이 (7일/30일/90일)
- 환경별 실패 원인 분류 (버그 vs 환경이슈 vs 플레이크) 파이차트
- 배포 빈도 vs QG 차단율 시계열 차트

### 3.2 QG 실행 결과 관리 (Gate Results)

**목적:** 모든 QG 실행 결과를 체계적으로 기록·조회

#### 3.2.1 QG 실행 목록 (Gate Run List)

- 필터: 서비스, 환경, 결과(PASS/FAIL), 기간, 트리거 유형
- 정렬: 최신순, 소요시간순
- 검색: 빌드번호, 커밋 SHA, 버전

#### 3.2.2 QG 실행 상세 (Gate Run Detail)

- **기본 정보:** 서비스명, 환경, 버전, 커밋 SHA, 브랜치, 트리거 시각, 소요시간
- **판정 결과:** PASS / FAIL + 판정 근거
- **테스트 결과 요약:**
  - API Test (Blocking): PASS/FAIL + 항목별 결과
  - E2E UI Test (Reference): PASS Rate % + 실패 시나리오
- **실패 Top 3:** 자동 추출된 주요 실패 원인
- **링크:** Jenkins 빌드, 테스트 리포트 (HTML), 변경 이력 (Changelog)
- **액션 버튼:** Re-run Gate, 다음 단계 진행 (권한별)

#### 3.2.3 실패 분류 (Failure Triage)

- QPE Ops가 실패 원인을 분류할 수 있는 UI
- 분류 카테고리: `Bug` / `Environment Issue` / `Flaky Test` / `Configuration`
- 분류 결과가 메트릭 집계에 반영

### 3.3 서비스/환경 관리 (Service & Environment)

#### 3.3.1 서비스 관리

- 서비스 등록/수정/비활성화 (잡코리아, 알바몬, 게임잡, 하이어링센터)
- 서비스별 QG 적용 환경 설정
- 서비스별 트라이브/담당자 매핑
- 서비스별 Slack 알림 채널 설정

#### 3.3.2 환경 관리

- 환경 정의: Dev / Stg / Prd
- 환경별 QG 정책:
  - Dev: Jenkins Trigger → Blocking Gate → Stg 버튼 제어
  - Stg: Slack Only → Blocking Gate → Prd 버튼 제어
  - Prd: Slack Only + 승인자 지정 → 모니터링 (Gate 미적용)

### 3.4 QG 정책 설정 (Gate Policy Configuration)

**목적:** QPE Admin이 QG Pass/Fail 기준을 시스템에서 직접 관리

#### 3.4.1 차단 정책 (Blocking Policy)

| 테스트 유형 | 차단 여부 | 설정 항목 |
|------------|----------|----------|
| API Test | Blocking | 응답코드 정합성, 필수 필드 체크, Latency 임계치, 5xx 에러율 |
| E2E UI Test | Reference Only | PASS Rate 임계치 (경고 기준) |

#### 3.4.2 임계치 설정

- 서비스별 / 환경별 커스터마이징 가능
- API Test: 응답시간 임계치 (ms), 5xx 허용률 (%)
- E2E Test: 최소 PASS Rate (%), 경고 PASS Rate (%)
- 변경 이력 Audit Trail 자동 기록

### 3.5 Slack 연동 (Slack Integration)

**목적:** 기존 Slack 기반 워크플로우를 유지하면서 포털과 양방향 연동

#### 3.5.1 알림 발송

- QG 결과 자동 Slack 알림 발송 (표준 템플릿 준수)
- 필수 필드: Environment, Service, Version, Gate Result, Links, Actions
- FAIL 시: Fail Summary Top 3, Triage Owner 멘션
- 알림 내 포털 링크 포함 (상세 결과 페이지 딥링크)

#### 3.5.2 알림 수신 채널 정책

| 알림 유형 | 수신 채널 |
|----------|----------|
| QG PASS/FAIL | 해당 트라이브 채널 + #qpe-gate-alerts |
| Prd 배포 요청 | Prd 배포 승인 채널 |
| Prd 배포 완료 | 트라이브 채널 + QPE 채널 + Ops 채널 |

#### 3.5.3 Slack Interactive Actions

- Slack 메시지 내 버튼 클릭 → 포털 API 호출
- "Proceed to STG Deploy" / "Re-run Gate" / "Open Report" 등
- Slash command: `/qg status [service]` → 현재 QG 상태 조회

### 3.6 Jenkins 연동 (Jenkins Integration)

**목적:** Jenkins 파이프라인과 양방향 통신 — 테스트 자동 실행 트리거 및 결과 수신

#### 3.6.1 Inbound (Jenkins → Portal)

- Jenkins Webhook 수신: 빌드 완료, 테스트 실행 완료, 결과 전송
- TAS (Test Automation Solution) 테스트 결과 수신 API
- Webhook 인증: HMAC-SHA256 서명 검증 (Webhook Secret 기반)
- Webhook Payload 스키마 버저닝 (`X-QGP-Webhook-Version` 헤더)

#### 3.6.2 Outbound (Portal → Jenkins)

- **Re-run Gate 트리거:** Jenkins Remote Build API (`/job/{job_name}/build`)
- **다음 환경 배포 트리거:** 파라미터 빌드 호출 (`/job/{job_name}/buildWithParameters`)
- **빌드 상태 조회:** Jenkins JSON API (`/job/{job_name}/{build_number}/api/json`)
- **빌드 콘솔 로그 조회:** 실패 시 로그 요약 추출 (`/consoleText`)
- 인증 방식: Jenkins API Token + User ID (Basic Auth)

#### 3.6.3 Jenkins 연결 관리 (Connection Management)

포털 Admin UI에서 Jenkins 인스턴스별 연결 정보를 등록/관리한다.

| 설정 항목 | 설명 | 예시 |
|----------|------|------|
| Jenkins Base URL | Jenkins 서버 주소 | `https://jenkins.workspear.io` |
| API User ID | API 호출용 서비스 계정 | `qgp-service` |
| API Token | Jenkins API Token (암호화 저장) | `11xxxxxxxxxxxxx` |
| Webhook Secret | Webhook HMAC 검증용 시크릿 | 자동 생성 (UUID) |
| Connection Name | 관리용 이름 | `QA Jenkins`, `Prod Jenkins` |
| Health Check | 연결 상태 확인 (주기적 ping) | 5분 간격 자동 체크 |

#### 3.6.4 Jenkins Job 매핑

서비스/환경별로 어떤 Jenkins Job이 QG 테스트를 실행하는지 매핑한다.

| 설정 항목 | 설명 |
|----------|------|
| Service | 대상 서비스 (잡코리아, 알바몬 등) |
| Environment | 대상 환경 (Dev, Stg, Prd) |
| Test Type | api_test / e2e_ui_test |
| Jenkins Connection | 사용할 Jenkins 인스턴스 |
| Job Path | Jenkins Job 경로 (`/job/QG/job/jobkorea-dev-api-test`) |
| Build Parameters | 기본 파라미터 (key-value) |
| Timeout (sec) | 빌드 타임아웃 (기본 600초) |

#### 3.6.5 TAS (Test Automation Solution) 연동

TAS와 포털 간 양방향 통신도 동일한 패턴으로 관리한다.

| 설정 항목 | 설명 |
|----------|------|
| TAS Base URL | TAS 서버 주소 |
| API Key | TAS API 인증 키 (암호화 저장) |
| Result Callback URL | Portal의 Webhook 수신 URL (TAS → Portal) |
| Webhook Secret | TAS Webhook HMAC 검증용 시크릿 |

### 3.10 외부 시스템 연동 설정 관리 (Integration Settings)

**목적:** 포털이 연동하는 모든 외부 시스템(Jenkins, Slack, TAS 등)의 크레덴셜과 연결 정보를 체계적으로 관리

#### 3.10.1 설정 관리 원칙

- **민감 정보(토큰, 시크릿)는 `.env` 파일 또는 시크릿 매니저**에서 관리 (코드/DB에 평문 저장 금지)
- DB에 저장이 필요한 경우 **AES-256 대칭 암호화** 적용 (암호화 키는 .env에서 로드)
- 포털 Admin 설정 UI에서 연결 테스트(Connection Test) 기능 제공
- 모든 크레덴셜 변경은 **Audit Trail** 자동 기록

#### 3.10.2 시크릿 관리 전략

| 등급 | 저장 위치 | 대상 항목 |
|------|----------|----------|
| **Level 1 (인프라)** | `.env` / AWS Secrets Manager | DB 접속 정보, Django SECRET_KEY, 암호화 마스터 키 |
| **Level 2 (외부 API)** | DB (AES-256 암호화) + `.env` 초기 시드 | Jenkins API Token, Slack Bot Token, TAS API Key |
| **Level 3 (Webhook)** | DB (AES-256 암호화) | Webhook Secret (자동 생성/로테이션) |

#### 3.10.3 `.env` 환경변수 전체 목록

```bash
# ============================================================
# Quality Gate Portal — Environment Variables (.env)
# ============================================================

# ----- Django Core -----
DJANGO_SECRET_KEY=your-secret-key-here
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=qgp.workspear.io,localhost
DJANGO_SETTINGS_MODULE=config.settings.production

# ----- Database (PostgreSQL) -----
DATABASE_URL=postgres://qgp_user:password@db-host:5432/quality_gate_portal
# 또는 개별 설정:
# DB_HOST=db-host
# DB_PORT=5432
# DB_NAME=quality_gate_portal
# DB_USER=qgp_user
# DB_PASSWORD=your-db-password

# ----- Redis (Cache + Celery Broker) -----
REDIS_URL=redis://redis-host:6379/0
CELERY_BROKER_URL=redis://redis-host:6379/1
CELERY_RESULT_BACKEND=redis://redis-host:6379/2

# ----- 암호화 (DB 내 시크릿 암호화용) -----
ENCRYPTION_MASTER_KEY=base64-encoded-aes-256-key

# ----- Jenkins -----
# 기본 Jenkins 인스턴스 (추가 인스턴스는 Admin UI에서 등록)
JENKINS_DEFAULT_URL=https://jenkins.workspear.io
JENKINS_DEFAULT_USER=qgp-service
JENKINS_DEFAULT_API_TOKEN=your-jenkins-api-token
JENKINS_WEBHOOK_SECRET=your-webhook-hmac-secret

# ----- Slack -----
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_SIGNING_SECRET=your-slack-signing-secret
SLACK_APP_TOKEN=xapp-your-slack-app-token  # Socket Mode 사용 시
SLACK_QG_ALERTS_CHANNEL=C09XXXXXX         # #qpe-gate-alerts 채널 ID
SLACK_QPE_OPS_CHANNEL=C09XXXXXX           # #qpe-ops 채널 ID

# ----- TAS (Test Automation Solution) -----
TAS_BASE_URL=https://tas.workspear.io
TAS_API_KEY=your-tas-api-key
TAS_WEBHOOK_SECRET=your-tas-webhook-secret

# ----- SSO / OAuth -----
SSO_CLIENT_ID=your-sso-client-id
SSO_CLIENT_SECRET=your-sso-client-secret
SSO_AUTHORITY_URL=https://sso.workspear.io
SSO_REDIRECT_URI=https://qgp.workspear.io/auth/callback/

# ----- Sentry (에러 모니터링) -----
SENTRY_DSN=https://xxxxx@sentry.io/xxxxx

# ----- Portal -----
QGP_BASE_URL=https://qgp.workspear.io     # Slack 딥링크 등에 사용
QGP_ADMIN_EMAIL=taesik.kim@workspear.io

# ----- 선택: Grafana 연동 -----
# GRAFANA_URL=https://grafana.workspear.io
# GRAFANA_API_KEY=your-grafana-api-key
# GRAFANA_DASHBOARD_UID=quality-gate-overview
```

#### 3.10.4 환경별 `.env` 파일 관리

| 환경 | 파일명 | 관리 방식 |
|------|--------|----------|
| 로컬 개발 | `.env.local` | 개발자 로컬 (Git 제외, `.gitignore` 등록) |
| 개발 서버 | `.env.dev` | Docker Compose `env_file` 또는 서버 환경변수 |
| 스테이징 | `.env.staging` | AWS Secrets Manager → 컨테이너 주입 |
| 프로덕션 | `.env.production` | AWS Secrets Manager → 컨테이너 주입 (직접 파일 X) |

#### 3.10.5 Admin UI — 연동 설정 화면

포털의 `/settings/integrations/` 에서 관리하는 항목:

- **Jenkins Connections:** 인스턴스 등록/수정/삭제, Connection Test, Job 매핑
- **Slack Configuration:** Bot Token 갱신, 채널 매핑, 알림 정책
- **TAS Configuration:** TAS 서버 등록, API Key 갱신, Callback URL 확인
- **SSO Configuration:** OAuth 설정 확인 (변경은 .env에서만)
- **Webhook 관리:** Webhook URL 발급, Secret 로테이션, 수신 로그 조회

각 연동 항목마다 **Connection Test** 버튼으로 실시간 연결 확인 가능.

### 3.7 예외 승인 관리 (Exception Approval)

**목적:** 긴급 배포 시 QG Skip 승인을 체계적으로 관리

#### 3.7.1 예외 승인 워크플로우

```
[트라이브 리드] 포털에서 예외 요청 생성
    ├── 사유, 영향 범위, 리스크 레벨 입력
    ↓
[QPE 리드] 리스크 판단
    ├── 리스크 낮음 → QPE 리드 단독 승인
    └── 리스크 높음 → Chapter Lead 검토 요청 → 필요시 CTO 승인
    ↓
[승인 완료] 배포 진행 + 사후 QG 실행 예약
    ↓
[사후 처리] 사후 QG 결과 기록 (Audit)
```

#### 3.7.2 예외 승인 이력

- 모든 예외 승인 요청/결과 영구 보관
- 요청자, 승인자, 사유, 리스크 레벨, 사후 QG 결과 기록
- 월간 리포트에 예외 승인 건수/사유 분석 포함

### 3.8 에스컬레이션 관리 (Escalation Tracking)

**목적:** 에스컬레이션 이력을 체계적으로 추적

#### 3.8.1 에스컬레이션 레벨

| Level | 상황 | 대응 주체 | 에스컬레이션 경로 |
|-------|------|----------|----------------|
| L1 | QG 1회 실패 | 트라이브 개발팀 | 자체 해결 |
| L2 | 동일 항목 3회 연속 실패 | 트라이브 + QPE 합동 | QPE 리드 → 트라이브 리드 |
| L3 | 환경 장애로 QG 전체 불가 | QPE Ops | QPE 리드 → Chapter Lead |
| L4 | 긴급 배포 QG Skip 요청 | 트라이브 리드 + QPE 리드 | QPE 리드 → Chapter Lead → CTO |

#### 3.8.2 자동 에스컬레이션

- 동일 테스트 항목 3회 연속 실패 시 자동 L2 에스컬레이션 생성
- 에스컬레이션 생성 시 Slack 알림 자동 발송

### 3.9 리포트 (Reporting)

**목적:** 정기 품질 리포트를 자동 생성하여 의사결정 근거 제공

#### 3.9.1 주간 QG 리포트

- 서비스별 QG 통과율, 플레이크율, 평균 소요시간
- 주요 실패 원인 Top 5
- 에스컬레이션 발생 건수
- 자동 생성 → QPE + 트라이브 리드 발송

#### 3.9.2 월간 품질 리포트

- 배포 빈도 vs QG 차단율
- Prod Escape Rate (도입 전후 비교)
- 예외 승인 건수/사유 분석
- 서비스별 품질 성숙도 평가
- 자동 생성 → CTO / 경영진 발송

#### 3.9.3 Ad-hoc 리포트

- 기간/서비스/환경 필터로 커스텀 리포트 생성
- CSV/PDF 다운로드 지원

---

## 4. 시스템 아키텍처 (System Architecture)

### 4.1 기술 스택

| 레이어 | 기술 | 비고 |
|--------|------|------|
| **Backend** | Python 3.12 + Django 5.x | Django REST Framework (DRF) |
| **Frontend** | Django Templates + HTMX + Alpine.js | SPA 불필요, 서버 렌더링 기반 |
| **Database** | PostgreSQL 16 | 메인 데이터 저장소 |
| **Cache** | Redis | 세션, 캐시, Celery 브로커 |
| **Task Queue** | Celery + Redis | 비동기 작업 (리포트 생성, 알림 발송) |
| **Scheduler** | Celery Beat | 주기적 리포트, 메트릭 집계 |
| **Deployment** | Docker + Docker Compose | 개발/스테이징/프로덕션 |
| **CI/CD** | Jenkins | 기존 인프라 활용 |

### 4.2 아키텍처 다이어그램

```
                          ┌─────────────────┐
                          │   Quality Gate   │
                          │     Portal       │
                          │  (Django + DRF)  │
                          └──────┬──────────┘
                                 │
           ┌─────────────┬───────┼───────┬──────────────┐
           │             │       │       │              │
    ┌──────▼──────┐ ┌────▼────┐ │  ┌────▼────┐  ┌─────▼─────┐
    │  PostgreSQL  │ │  Redis  │ │  │  Celery  │  │  Celery   │
    │  (Main DB)   │ │ (Cache) │ │  │ (Worker) │  │  (Beat)   │
    └─────────────┘ └─────────┘ │  └──────────┘  └───────────┘
                                │
           ┌────────────────────┼─────────────────────┐
           │                    │                     │
    ┌──────▼──────┐     ┌──────▼──────┐      ┌──────▼──────┐
    │   Jenkins    │     │   Slack     │      │    TAS      │
    │  (Webhook)   │     │  (Bot API)  │      │  (Result)   │
    └─────────────┘     └─────────────┘      └─────────────┘
```

### 4.3 Django 앱 구조

```
quality_gate_portal/
├── config/                   # Django 프로젝트 설정
│   ├── settings/
│   │   ├── base.py
│   │   ├── dev.py
│   │   ├── staging.py
│   │   └── production.py
│   ├── urls.py
│   ├── celery.py
│   └── wsgi.py
├── apps/
│   ├── accounts/             # 사용자 인증/인가, RBAC
│   ├── dashboard/            # 대시보드, 메트릭 카드, 트렌드 차트
│   ├── gates/                # QG 실행 결과, 실패 분류, 정책 설정
│   ├── services/             # 서비스/환경 관리
│   ├── approvals/            # 예외 승인 워크플로우
│   ├── escalations/          # 에스컬레이션 추적
│   ├── reports/              # 리포트 생성/조회
│   ├── integrations/         # Jenkins/Slack/TAS 연동
│   │   ├── jenkins/
│   │   ├── slack/
│   │   └── tas/
│   └── api/                  # DRF API (v1)
├── templates/                # Django 템플릿 (HTMX)
├── static/                   # CSS, JS (Alpine.js, Chart.js)
└── manage.py
```

---

## 5. 데이터 모델 (Data Model)

### 5.1 핵심 엔티티

#### Service (서비스)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| name | VARCHAR(100) | 서비스명 (잡코리아, 알바몬 등) |
| code | VARCHAR(20) | 서비스 코드 (JK, AM, GJ, HC) |
| tribe_name | VARCHAR(100) | 소속 트라이브명 |
| slack_channel_id | VARCHAR(50) | 알림 수신 Slack 채널 ID |
| is_active | BOOLEAN | 활성/비활성 |
| created_at | TIMESTAMP | 생성일시 |

#### Environment (환경)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| name | VARCHAR(20) | 환경명 (Dev, Stg, Prd) |
| trigger_type | VARCHAR(30) | 트리거 방식 (jenkins_trigger, slack_only) |
| gate_mode | VARCHAR(20) | blocking / reference / monitoring |
| next_env_control | VARCHAR(50) | 다음 단계 제어 방식 |

#### GatePolicy (QG 정책)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| service_id | FK → Service | 대상 서비스 |
| environment_id | FK → Environment | 대상 환경 |
| test_type | VARCHAR(30) | api_test / e2e_ui_test |
| blocking_mode | VARCHAR(20) | blocking / reference |
| pass_threshold | DECIMAL | 통과 임계치 (%) |
| latency_threshold_ms | INTEGER | 응답시간 임계치 (ms) |
| error_rate_threshold | DECIMAL | 에러율 허용 임계치 (%) |
| updated_by | FK → User | 최종 수정자 |
| updated_at | TIMESTAMP | 최종 수정일시 |

#### GateRun (QG 실행)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| service_id | FK → Service | 대상 서비스 |
| environment_id | FK → Environment | 대상 환경 |
| version | VARCHAR(50) | 배포 버전 (Semantic Versioning) |
| commit_sha | VARCHAR(40) | 커밋 SHA |
| branch | VARCHAR(100) | 브랜치명 |
| result | VARCHAR(10) | PASS / FAIL |
| api_test_result | VARCHAR(10) | PASS / FAIL |
| e2e_test_result | DECIMAL | PASS Rate (%) |
| duration_seconds | INTEGER | 소요시간 (초) |
| jenkins_build_url | TEXT | Jenkins 빌드 URL |
| report_url | TEXT | 테스트 리포트 URL |
| triggered_by | VARCHAR(50) | 트리거 유형 (webhook, manual, rerun) |
| triggered_at | TIMESTAMP | 실행 시작 시각 |
| completed_at | TIMESTAMP | 실행 완료 시각 |
| created_at | TIMESTAMP | 레코드 생성일시 |

#### GateFailure (실패 상세)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| gate_run_id | FK → GateRun | 소속 GateRun |
| test_name | VARCHAR(200) | 실패 테스트명 |
| failure_type | VARCHAR(30) | assertion / timeout / error / schema |
| error_message | TEXT | 에러 메시지 |
| category | VARCHAR(30) | bug / environment / flaky / configuration (QPE 분류) |
| categorized_by | FK → User | 분류자 |
| categorized_at | TIMESTAMP | 분류 시각 |

#### ExceptionApproval (예외 승인)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| service_id | FK → Service | 대상 서비스 |
| environment_id | FK → Environment | 대상 환경 |
| requested_by | FK → User | 요청자 (트라이브 리드) |
| reason | TEXT | 예외 사유 |
| impact_scope | TEXT | 영향 범위 |
| risk_level | VARCHAR(10) | low / medium / high / critical |
| status | VARCHAR(20) | pending / approved / rejected |
| approved_by | FK → User | 승인자 |
| approved_at | TIMESTAMP | 승인 시각 |
| post_gate_run_id | FK → GateRun | 사후 QG 실행 결과 |
| created_at | TIMESTAMP | 요청 시각 |

#### Escalation (에스컬레이션)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| gate_run_id | FK → GateRun | 관련 GateRun |
| level | VARCHAR(5) | L1 / L2 / L3 / L4 |
| trigger_reason | TEXT | 에스컬레이션 사유 |
| status | VARCHAR(20) | open / in_progress / resolved |
| assigned_to | FK → User | 담당자 |
| resolved_at | TIMESTAMP | 해결 시각 |
| resolution_note | TEXT | 해결 내용 |
| is_auto_generated | BOOLEAN | 자동 생성 여부 |
| created_at | TIMESTAMP | 생성 시각 |

### 5.2 연동 설정 엔티티

#### JenkinsConnection (Jenkins 연결)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| name | VARCHAR(100) | 연결 이름 (`QA Jenkins`, `Prod Jenkins`) |
| base_url | VARCHAR(500) | Jenkins 서버 URL |
| api_user | VARCHAR(100) | API 호출용 User ID |
| api_token_encrypted | TEXT | API Token (AES-256 암호화 저장) |
| webhook_secret_encrypted | TEXT | Webhook HMAC Secret (암호화 저장) |
| is_healthy | BOOLEAN | 마지막 Health Check 결과 |
| last_health_check_at | TIMESTAMP | 마지막 Health Check 시각 |
| is_active | BOOLEAN | 활성 여부 |
| created_by | FK → User | 등록자 |
| created_at | TIMESTAMP | 생성일시 |
| updated_at | TIMESTAMP | 수정일시 |

#### JenkinsJobMapping (Jenkins Job 매핑)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| service_id | FK → Service | 대상 서비스 |
| environment_id | FK → Environment | 대상 환경 |
| test_type | VARCHAR(30) | api_test / e2e_ui_test |
| jenkins_connection_id | FK → JenkinsConnection | 사용할 Jenkins 인스턴스 |
| job_path | VARCHAR(500) | Jenkins Job 경로 |
| build_params | JSONB | 기본 빌드 파라미터 (key-value) |
| timeout_seconds | INTEGER | 빌드 타임아웃 (기본 600) |
| is_active | BOOLEAN | 활성 여부 |

#### SlackChannelMapping (Slack 채널 매핑)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| service_id | FK → Service | 대상 서비스 |
| channel_type | VARCHAR(30) | tribe_alert / qpe_alert / prd_approval / ops |
| channel_id | VARCHAR(50) | Slack 채널 ID |
| channel_name | VARCHAR(100) | Slack 채널명 (표시용) |
| is_active | BOOLEAN | 활성 여부 |

#### IntegrationAuditLog (연동 설정 변경 이력)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| integration_type | VARCHAR(30) | jenkins / slack / tas / sso |
| action | VARCHAR(30) | create / update / delete / rotate_secret |
| target_id | UUID | 변경 대상 레코드 ID |
| changed_fields | JSONB | 변경된 필드 목록 (이전값은 마스킹) |
| changed_by | FK → User | 변경자 |
| created_at | TIMESTAMP | 변경 시각 |

#### WebhookLog (Webhook 수신 로그)

| 필드 | 타입 | 설명 |
|------|------|------|
| id | UUID | PK |
| source | VARCHAR(30) | jenkins / tas |
| endpoint | VARCHAR(200) | 수신 엔드포인트 |
| payload_hash | VARCHAR(64) | Payload SHA-256 해시 |
| signature_valid | BOOLEAN | HMAC 서명 검증 결과 |
| http_status | INTEGER | 응답 HTTP 상태 코드 |
| processing_ms | INTEGER | 처리 소요시간 (ms) |
| error_message | TEXT | 에러 메시지 (실패 시) |
| created_at | TIMESTAMP | 수신 시각 |

---

## 6. API 설계 (API Design)

### 6.1 REST API 엔드포인트

Base URL: `/api/v1/`

#### 인증

| Method | Endpoint | 설명 |
|--------|----------|------|
| POST | `/auth/login/` | 로그인 (JWT 발급) |
| POST | `/auth/refresh/` | 토큰 갱신 |
| GET | `/auth/me/` | 현재 사용자 정보 |

#### 대시보드

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/dashboard/overview/` | 전체 현황 매트릭스 |
| GET | `/dashboard/metrics/` | 핵심 메트릭 카드 데이터 |
| GET | `/dashboard/trends/` | 트렌드 차트 데이터 |

#### Gate 실행 결과

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/gates/runs/` | QG 실행 목록 (필터/검색/페이징) |
| GET | `/gates/runs/{id}/` | QG 실행 상세 |
| POST | `/gates/runs/{id}/rerun/` | Re-run 요청 |
| PATCH | `/gates/runs/{id}/failures/{fid}/categorize/` | 실패 분류 |

#### 서비스/환경

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/services/` | 서비스 목록 |
| POST | `/services/` | 서비스 등록 |
| GET | `/services/{id}/` | 서비스 상세 |
| PUT | `/services/{id}/` | 서비스 수정 |
| GET | `/environments/` | 환경 목록 |

#### 정책 설정

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/policies/` | 정책 목록 |
| PUT | `/policies/{id}/` | 정책 수정 |
| GET | `/policies/{id}/history/` | 정책 변경 이력 |

#### 예외 승인

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/approvals/` | 예외 승인 목록 |
| POST | `/approvals/` | 예외 승인 요청 생성 |
| PATCH | `/approvals/{id}/approve/` | 승인 |
| PATCH | `/approvals/{id}/reject/` | 거부 |

#### 에스컬레이션

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/escalations/` | 에스컬레이션 목록 |
| PATCH | `/escalations/{id}/resolve/` | 해결 처리 |

#### 리포트

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/reports/weekly/` | 주간 리포트 목록 |
| GET | `/reports/monthly/` | 월간 리포트 목록 |
| POST | `/reports/custom/` | 커스텀 리포트 생성 |
| GET | `/reports/{id}/download/` | 리포트 다운로드 (CSV/PDF) |

#### Webhook (외부 시스템 → Portal)

| Method | Endpoint | 설명 |
|--------|----------|------|
| POST | `/webhooks/jenkins/` | Jenkins 빌드 완료 Webhook |
| POST | `/webhooks/tas/` | TAS 테스트 결과 수신 |

#### Slack Interactive

| Method | Endpoint | 설명 |
|--------|----------|------|
| POST | `/integrations/slack/actions/` | Slack 버튼 액션 수신 |
| POST | `/integrations/slack/commands/` | Slack Slash command 수신 |

#### 연동 설정 관리 (Admin Only)

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | `/settings/jenkins/connections/` | Jenkins 연결 목록 |
| POST | `/settings/jenkins/connections/` | Jenkins 연결 등록 |
| PUT | `/settings/jenkins/connections/{id}/` | Jenkins 연결 수정 |
| DELETE | `/settings/jenkins/connections/{id}/` | Jenkins 연결 삭제 |
| POST | `/settings/jenkins/connections/{id}/test/` | Jenkins 연결 테스트 |
| POST | `/settings/jenkins/connections/{id}/rotate-secret/` | Webhook Secret 로테이션 |
| GET | `/settings/jenkins/job-mappings/` | Job 매핑 목록 |
| POST | `/settings/jenkins/job-mappings/` | Job 매핑 등록 |
| PUT | `/settings/jenkins/job-mappings/{id}/` | Job 매핑 수정 |
| GET | `/settings/slack/channels/` | Slack 채널 매핑 목록 |
| POST | `/settings/slack/channels/` | Slack 채널 매핑 등록 |
| POST | `/settings/slack/test-notification/` | 테스트 알림 발송 |
| GET | `/settings/webhooks/logs/` | Webhook 수신 로그 조회 |
| GET | `/settings/audit-logs/` | 연동 설정 변경 이력 조회 |

화면 목록에도 추가:

| # | 화면명 | 경로 | 설명 |
|---|--------|------|------|
| 10 | Jenkins 연동 관리 | `/settings/integrations/jenkins/` | 연결 등록, Job 매핑, 연결 테스트 |
| 11 | Slack 연동 관리 | `/settings/integrations/slack/` | 채널 매핑, 테스트 알림 |
| 12 | Webhook 로그 | `/settings/integrations/webhooks/` | 수신 로그, 에러 모니터링 |
| 13 | 감사 로그 | `/settings/audit/` | 설정 변경 이력 조회 |

---

## 7. 화면 설계 (Screen Design)

### 7.1 주요 화면 목록

| # | 화면명 | 경로 | 설명 |
|---|--------|------|------|
| 1 | 메인 대시보드 | `/` | QG 현황 매트릭스 + 메트릭 카드 + 트렌드 |
| 2 | Gate 실행 목록 | `/gates/` | QG 실행 결과 목록 (필터/검색) |
| 3 | Gate 실행 상세 | `/gates/{id}/` | 실행 상세 + 실패 Top 3 + 액션 |
| 4 | 서비스 관리 | `/services/` | 서비스 목록/등록/수정 |
| 5 | 정책 설정 | `/policies/` | QG Pass/Fail 기준 설정 |
| 6 | 예외 승인 | `/approvals/` | 예외 요청/승인 워크플로우 |
| 7 | 에스컬레이션 | `/escalations/` | 에스컬레이션 목록/상세 |
| 8 | 리포트 | `/reports/` | 주간/월간/커스텀 리포트 |
| 9 | 시스템 설정 | `/settings/` | 사용자/역할/연동 관리 (Admin) |

### 7.2 UI/UX 원칙

- **Server-Side Rendering** (Django Templates) + HTMX로 부분 업데이트
- **Alpine.js**로 경량 인터랙션 처리 (모달, 토글, 드롭다운)
- **Chart.js**로 트렌드 차트, 파이차트 렌더링
- 색상 체계: Dev(🔵 Blue), Stg(🟢 Green), Prd(🟠 Orange)
- 모바일 반응형 (Slack에서 링크 클릭 시 모바일 뷰 대응)

---

## 8. 비기능 요구사항 (Non-Functional Requirements)

### 8.1 성능

| 항목 | 기준 |
|------|------|
| 대시보드 로딩 | < 2초 |
| API 응답시간 | p95 < 500ms |
| 동시 사용자 | 50명 이상 |
| QG 결과 데이터 보관 | 최소 2년 |

### 8.2 보안

#### 8.2.1 인증/인가

- HTTPS 필수 (TLS 1.2+)
- SSO 인증 + RBAC 기반 인가
- API 인증: JWT (Access Token 15분 + Refresh Token 7일)
- Webhook 인증: HMAC-SHA256 서명 검증 (시스템별 개별 Secret)
- CSRF 보호 (Django 기본)

#### 8.2.2 시크릿 관리

| 관리 항목 | 저장 방식 | 로테이션 주기 |
|----------|----------|-------------|
| Django SECRET_KEY | `.env` / AWS Secrets Manager | 배포 시 |
| DB Password | `.env` / AWS Secrets Manager | 90일 |
| Jenkins API Token | DB (AES-256 암호화) | 180일 또는 수동 |
| Slack Bot Token | `.env` / AWS Secrets Manager | Slack 앱 재생성 시 |
| Slack Signing Secret | `.env` / AWS Secrets Manager | Slack 앱 재생성 시 |
| TAS API Key | DB (AES-256 암호화) | 180일 또는 수동 |
| Webhook Secret | DB (AES-256 암호화) | Admin UI에서 수동 로테이션 |
| 암호화 마스터 키 | `.env` / AWS Secrets Manager | 연 1회 (키 마이그레이션 필요) |

#### 8.2.3 암호화 구현

- DB 내 토큰/시크릿: `django-encrypted-model-fields` 또는 커스텀 AES-256-GCM
- 암호화 마스터 키: `.env`의 `ENCRYPTION_MASTER_KEY` (Base64 인코딩)
- Admin UI에서 토큰 조회 시 마스킹 표시 (`xoxb-****-****-XXXX`)
- 복호화는 실제 API 호출 시점에만 수행 (메모리에서 즉시 해제)

#### 8.2.4 `.env` 보안 가이드라인

- `.env` 파일은 **절대 Git에 커밋하지 않음** (`.gitignore` 필수 등록)
- `.env.example` 파일에 키 목록만 기재 (값은 `your-xxx-here` 플레이스홀더)
- 프로덕션은 `.env` 파일 직접 사용 금지 → AWS Secrets Manager에서 런타임 주입
- Docker Compose: `env_file` 지시자 사용, `docker-compose.yml`에 값 직접 기재 금지

### 8.3 가용성/운영

- Docker 기반 배포 (무중단 배포 가능)
- Healthcheck 엔드포인트 (`/health/`)
- 에러 로깅 (Sentry 연동)
- 메트릭 모니터링 (Prometheus + Grafana 연동 가능)

---

## 9. 로드맵 연계 (Roadmap Alignment)

Quality Gate Portal의 기능 개발은 QG 운영 로드맵의 Phase에 맞춰 단계적으로 진행한다.

### Phase 1 (2026 Q2) — MVP

> QG 결과 수신 및 조회 + 대시보드 기본 기능

- Webhook 수신 (Jenkins, TAS) → GateRun 저장
- Gate 실행 목록/상세 화면
- 메인 대시보드 (현황 매트릭스 + 기본 메트릭)
- 실패 분류 (Failure Triage) UI
- Slack 알림 연동 (기본)
- 서비스/환경 관리 CRUD
- 기본 인증 (Django Auth)

### Phase 2 (2026 Q3~Q4) — 확장

> 정책 관리 + 예외 승인 + 리포트 자동화

- QG 정책 설정 UI (임계치, 차단 모드)
- 예외 승인 워크플로우
- 에스컬레이션 자동 생성 + 추적
- 주간/월간 리포트 자동 생성 (Celery Beat)
- Slack Interactive Actions (버튼 → Portal API)
- SSO 연동
- 트렌드 차트 고도화

### Phase 3 (2027 Q1~Q2) — 고도화

> 데이터 기반 의사결정 + 자율 운영 지원

- Prod Escape Rate 측정/추적
- 월간 경영진 리포트 자동화
- Prd 배포 승인 프로세스 포털 통합
- 실패 패턴 분석 (반복 실패 자동 감지)
- Grafana 연동 (모니터링 대시보드 통합)
- 권한 세분화 (Deployer vs Approver)

---

## 10. 성공 기준 (Success Criteria)

| # | 기준 | 측정 방법 |
|---|------|----------|
| 1 | QPE/트라이브가 포털에서 QG 결과를 확인하는 비율 80% 이상 | 포털 접속 로그 vs Slack 링크 클릭률 |
| 2 | Notion 수동 대시보드 업데이트 제거 | QPE Ops 업무 시간 측정 |
| 3 | 예외 승인 이력 100% 추적 가능 | 포털 Audit Trail 데이터 |
| 4 | 주간/월간 리포트 자동 생성률 100% | Celery Beat 실행 이력 |
| 5 | QG 결과 데이터 기반 의사결정 루틴 정착 | 월간 경영진 보고 실행 여부 |

---

## 11. 리스크 및 의존성 (Risks & Dependencies)

### 11.1 리스크

| 리스크 | 영향도 | 대응 방안 |
|--------|-------|----------|
| Jenkins/TAS Webhook 스펙 변경 | 높음 | Webhook 스키마 버저닝, 하위 호환성 유지 |
| Slack API 제한 (Rate Limit) | 중간 | 메시지 큐잉 (Celery), 배치 발송 |
| 초기 데이터 마이그레이션 | 중간 | 기존 Jenkins 이력 import 스크립트 별도 개발 |
| 트라이브 온보딩 저조 | 중간 | Slack 알림에 포털 딥링크 기본 포함, UX 최적화 |

### 11.2 의존성

| 의존성 | 담당 | 상태 |
|--------|------|------|
| Jenkins Webhook 설정 | IDP Tribe | 협의 필요 |
| TAS 결과 API 스펙 | QPE Ops | 정의 중 |
| Slack Bot 생성/권한 | QPE Admin | 진행 가능 |
| SSO (OAuth) 연동 | 인프라팀 | 협의 필요 |
| 서버 인프라 (AWS) | 클라우드인프라팀 | 협의 필요 |

---

## 12. 용어 정의 (Glossary)

| 용어 | 설명 |
|------|------|
| **Quality Gate (QG)** | 배포 파이프라인의 환경 전환 시점에서 자동화 테스트 결과를 기준으로 Go/No-Go를 판정하는 품질 체크포인트 |
| **Blocking Gate** | FAIL 시 다음 환경 배포를 차단하는 게이트 (API Test) |
| **Reference Gate** | FAIL 시 경고만 표시하고 배포는 허용하는 게이트 (E2E UI Test) |
| **TAS** | Test Automation Solution. 자동화 테스트 실행 엔진 |
| **Flake / Flaky Test** | 코드 변경 없이 결과가 불안정한(PASS↔FAIL) 테스트 |
| **Prod Escape Rate** | QG를 통과했으나 프로덕션에서 발견된 결함 비율 |
| **MTTR** | Mean Time To Recovery. 장애 발생~복구까지 평균 시간 |
| **Triage** | 실패 원인을 분류하는 행위 (Bug / Environment / Flaky) |

---

*End of Document*