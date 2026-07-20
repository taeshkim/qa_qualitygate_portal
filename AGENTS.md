# AGENTS.md — 서브에이전트 구성 & 병렬 작업 가이드

이 저장소는 **빠른 작업을 위해 역할별 서브에이전트를 두고 독립 작업을 기본적으로 병렬 디스패치**한다.
에이전트 정의는 `.claude/agents/*.md` 에 있고, 이 문서는 그 카탈로그 + 사용 패턴이다.

> 작업 절차(테스트→문서→커밋→push)·금지사항 등 상세 규칙은 [`CLAUDE.md`](./CLAUDE.md) 가 단일 진실 소스.

---

## 0. 작업 원칙 (요약)

- **코드 변경이 생길 때마다 `README.md` 를 업데이트**한다.
- **작업 한 턴이 끝날 때마다 코드 커밋 + push 를 생활화**한다.
- 한 턴의 마무리 = 테스트 → 문서 갱신(`README.md` / `maintenance.md` / `AGENTS.md` / `SKILLS.md`) → 단일 커밋 → push.
- **독립적으로 나눌 수 있는 작업은 기본적으로 병렬 서브에이전트로 동시 처리**한다 (아래 §2).

---

## 1. 에이전트 카탈로그

| 에이전트 | 담당 영역 | 주요 파일/앱 | 모델 |
|---|---|---|---|
| **pipeline-dev** | QG 파이프라인 상태기계·폴링·auto_mode·parallel_test·rerun·취소·Reliability sweep | `apps/qualitygates/`(특히 `views.py`), `apps/suites/`, `apps/robustness/` | sonnet |
| **integrations-dev** | 외부 연동 + Slack 알림 + 담당자 자동 매칭 | `apps/integrations/`(jenkins/argocd/google_sheets/slack_users/url_matcher/log_errors), `apps/notifications/` | sonnet |
| **frontend-dev** | 포털 UI, HTMX 5초 폴링, OS 알림/워처, 대시보드 화면 | `templates/`, `static/js/qg_notify.js` | sonnet |
| **backend-dev** | 모델·마이그레이션·`/api/v2`(API Key+Scope)·권한 미들웨어·DORA 집계 | `apps/api_v2/`, `apps/accounts/`, `apps/dashboard/`, `apps/*/models.py·migrations/` | sonnet |
| **qa-test** | 테스트 작성·실행·디버깅, 회귀 방지 | `apps/*/tests.py` | sonnet |
| **code-reviewer** | 변경 diff 의 버그·보안(API Key 평문)·성능·정합성 리뷰 (수정 X, 보고만) | 전체 diff | sonnet |
| **docs-keeper** | README/maintenance/AGENTS/SKILLS 를 코드와 동기화 | 4개 문서 | sonnet |

> 모델은 속도/비용 균형상 기본 `sonnet`. 더 깊은 추론이 필요하면 호출 시 `model: opus` 로 오버라이드 가능.

---

## 2. 병렬 디스패치 패턴 (기본값)

독립적인 작업 조각은 **한 번의 메시지에서 여러 에이전트를 동시에** 띄운다. 대표 패턴:

- **새 기능** → `backend-dev`(모델/마이그레이션/뷰) ∥ `frontend-dev`(템플릿/JS) ∥ `qa-test`(테스트) 를 동시 디스패치 → 모두 끝나면 `code-reviewer` + `docs-keeper`.
- **버그 여러 건** → 각 버그를 담당 영역 에이전트에 동시 배정.
- **외부 연동 변경** → `integrations-dev` 구현 ∥ `qa-test`(회귀 6 시나리오) ∥ (필요시) `frontend-dev`(에러로그 표/배지).
- **리뷰·문서는 항상 구현과 분리** → 구현 에이전트가 끝난 직후 `code-reviewer`(독립 시각)와 `docs-keeper`(문서)를 병렬로.

예시 (메인이 한 메시지에 동시 호출):
```
Agent(subagent_type="backend-dev",  prompt="...모델 X 필드 추가 + 마이그레이션...")
Agent(subagent_type="frontend-dev", prompt="...해당 화면에 입력 UI 추가...")
Agent(subagent_type="qa-test",      prompt="...X 필드 검증 테스트 추가...")
```

### 병렬을 쓰지 않는 경우 (예외)
- **상태기계 전이처럼 cross-cutting** 한 핵심 로직 — `pipeline-dev` 단독(또는 메인 단독)으로. 여러 에이전트가 같은 `views.py` 를 동시에 고치면 충돌.
- **같은 파일을 동시에 편집**해야 하는 작업 — 직렬화하거나 한 에이전트에 통합.
- **한두 줄짜리 사소한 수정** — 분할 이득이 없으면 메인이 직접.

### 메인이 직접 하는 일
- 작업 분해/디스패치, 에이전트 결과 종합·검증.
- **커밋과 push 는 항상 메인이 일괄** (에이전트는 커밋/push 하지 않는다).
- 에이전트가 "사용자 확인 필요" 로 보고한 항목을 사용자에게 전달.

---

## 3. 에이전트에 일을 맡길 때 (프롬프트 작성)
- 에이전트는 이 대화 맥락을 모른다 — **목표·관련 파일 경로·제약·검증 방법**을 자족적으로 적는다.
- "구현해줘" vs "조사만 해줘" 를 명시.
- 끝나면 무엇을 어떻게 검증했는지 + 문서 갱신 위치 + 사용자 확인 필요 여부를 보고하도록 요청.

---

## 4. 이 문서의 갱신 규칙
- `.claude/agents/` 에 에이전트를 **추가/삭제/역할 변경**하면 §1 표와 §2 패턴을 같은 작업에서 갱신한다.
- 갱신은 매 작업 턴 마무리(문서 동기화) 단계에서 `docs-keeper` 가 점검한다.
