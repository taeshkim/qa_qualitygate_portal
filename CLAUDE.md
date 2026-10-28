# 작업 지침

이 저장소에서 작업할 때 항상 다음 절차를 따른다.

## 작업 시작 전

- **`README.md` · `maintenance.md` · `AGENTS.md` · `SKILLS.md` 를 먼저 읽어 현재 구현 상태와 작업 환경을 파악**한 뒤 작업을 진행한다.
  - `README.md` — 운영/사용자 시점의 기능 요약, URL 라우팅, 환경 정보.
  - `maintenance.md` — 파이프라인 상태 기계, 도메인 모델 매핑, "X 를 바꾸려면 어디를 고치나" 룩업, 트러블슈팅.
  - `AGENTS.md` — 서브에이전트 카탈로그 + 병렬 디스패치 패턴 (작업을 나눠 맡길 때 참조).
  - `SKILLS.md` — 실행 환경, 자주 쓰는 명령어, 스킬, 검증 체크리스트.
- 문서가 코드와 어긋난 부분이 보이면 그 작업 안에서 함께 바로잡는다.
- **작업을 독립 조각으로 나눌 수 있으면 기본적으로 병렬 서브에이전트로 분배**한다 (아래 "병렬 에이전트 (기본 지침)" 참조).

## 작업 종료 후 (커밋 직전 필수)

> **순서 엄수**: 1번(테스트) 이 통과해야 2번(문서) → 3번(커밋) → 4번(push) 으로 진행한다. 테스트를 건너뛴 채 문서/커밋/push 로 가지 않는다.

1. **사용자 의도대로 구현되었는지 반드시 테스트**한다. 통과하지 못한 채로 다음 단계로 넘어가지 않는다.
   - UI / 화면 변경 → 로컬 dev server (`./run.sh`) 를 띄우고 실제 브라우저에서 동작 확인. 외부 자격증명/실데이터가 필요해 자동 검증이 어려운 경우 (예: 운영 Jenkins/Argo CD 응답이 필요한 흐름) 는 **사용자에게 동작 확인을 요청** 하고 OK 응답을 받은 뒤 진행.
   - 모델 / 마이그레이션 변경 → `python manage.py migrate` 적용 + 관련 뷰/관리자 화면이 정상 로드되는지 확인.
   - 백엔드 로직 → 가능하면 단위 테스트나 인터프리터 호출로 입력/출력을 직접 검증. 검증이 어려우면 사용자에게 시나리오 확인을 요청.
   - 테스트 실패 시: 원인 분석 → 수정 → **재테스트**. 통과 전에는 README/maintenance.md 갱신과 커밋을 모두 보류.

2. **변경 내용을 문서에 반영**한다. 매 작업 턴 마무리마다 아래 **4개 문서를 점검·갱신**한다 (변경 성격에 맞는 것만 정확히):
   - `README.md` — 새 기능/엔드포인트/모델 필드 → 기능 목록 + URL 라우팅. (사용자 화면/운영에 무영향인 순수 내부 리팩토링이면 건너뜀)
   - `maintenance.md` — 동작이 바뀐 함수의 파일:라인 참조·설명 최신화. 상태 전이는 §2 다이어그램까지. 더 이상 안 쓰는 코드는 "안 쓰는 / 레거시 코드" 섹션에 추가.
   - `AGENTS.md` — `.claude/agents/` 에 에이전트를 추가/삭제/역할 변경했으면 카탈로그·패턴을 갱신.
   - `SKILLS.md` — 실행 명령·환경·검증 절차·새로 쓰게 된 스킬이 바뀌었으면 갱신.
   - 이 문서 동기화는 `docs-keeper` 에이전트에 맡겨 구현과 병렬로 처리해도 된다.

3. 변경된 파일들과 함께 문서 갱신을 **하나의 커밋**으로 묶는다. 커밋 메시지는 기존 스타일 (`feat:` / `fix:` / `docs:` / `chore:` 등) 을 따른다.

4. **`git push` 까지 수행**한다. push 가 실패하면 원인을 사용자에게 보고.

## CLAUDE.md 자체의 갱신

- 사용자가 "앞으로는 ~ 해줘", "~ 하지 마", "매번 ~ 해" 같이 **앞으로의 작업 방식**에 대한 지침을 주면, 그 자리에서 **이 `CLAUDE.md` 에 반영**한다.
- `CLAUDE.md` 가 변경된 작업 역시 위와 동일하게 한 커밋으로 묶어 **`git push` 까지** 수행한다.

## 알림 발송 / 담당자 자동 매칭 수정 시

Slack 자동 멘션이나 담당자(`ApiEndpointOwner` / `FallbackOwners`) 관련 로직을 손댈 때는 항상 다음 항목을 모두 확인한다.

### 매칭 로직 single source of truth
모든 발송 경로는 `apps/integrations/slack_users.py:find_user_id_by_name(name)` 를 통한다. 인덱싱 로직 (`_build_name_index`) 한 곳을 수정하면 다음 경로 모두에 자동 반영된다:
- 자동 발송: `apps/notifications/services.py:_build_followup_for_job_run` (Test Suite FAIL Slack 메시지)
- 수동 발송 (담당자 매칭됨): `apps/integrations/views.py:api_owner_notify_error` (Error Log 페이지 "에러 노티" 버튼)
- 수동 발송 (대체 담당자): `apps/integrations/views.py:notify_fallback_owners` (Error Log 페이지 매칭 실패 행)
- 자동 발송 (UI Test Automation FAIL): `apps/notifications/services.py:send_tap_run_event` → `_ui_owner_mentions` (`UiAutomationOwners` — 담당자 관리 > UI 탭, URL 매칭 없이 통 멘션)
- Error Log 페이지 담당자 표 노출: `apps/integrations/url_matcher.py:extract_urls` + 그것을 호출하는 view 들

→ 매칭 룰 변경은 `slack_users.py` 한 곳에 집중. 알림 형식 / fallback 분기 같은 발송 로직은 자동/수동 양쪽에 동일하게 반영해야 한다.

### Slack 표시명 정규화 변경 위치
Slack 워크스페이스의 `display_name` / `real_name` 에 회사 컨벤션으로 붙는 표기를 무시하려면 `apps/integrations/slack_users.py` 안의 다음을 수정한다:
- **접미사 `(팀명)`, `[부서]` 등**: `_SUFFIX_TRIM_RE` 정규식
- **상태 prefix (한글 단어 + separator + 한글 이름)**: `_STATUS_PREFIX_RE` 가 generic 패턴(`^[가-힣]+[\s_·\-:|]+([가-힣].+)$`) 으로 처리하므로 `휴가중_`, `재택 `, `행사_`, `교육_` 등 **어떤 한글 prefix 든 자동 인식** — 키워드 추가 작업 불필요. 중첩 prefix (`재택_미팅중_홍길동`) 도 반복 제거됨.
- separator 추가 (`_`, 공백, `·`, `-`, `:`, `|` 외) 가 필요하면 `_STATUS_PREFIX_RE` 의 character class 수정
- **prefix 패턴이 한글이 아닌 경우** (예: 영문 상태 `WFH_`, `OOO_`) 는 별도 대응이 필요 — 현재는 의도적으로 한글만 처리

### 알림 발송 변경 시 회귀 테스트 항목
다음 6 가지 시나리오를 항상 같이 점검 (테스트 또는 수동 검증):
1. **자동 발송 + 정상 매칭** — `ApiEndpointOwner` row 있고 `fe_owner`/`be_owner` 채워짐 → 멘션 발송
2. **자동 발송 + owner row 비어있음** — `fe_owner=""`/`be_owner=""` → `FallbackOwners` 사용 + "담당자 매칭 없음" 라벨
3. **자동 발송 + owner 매칭도 없고 fallback 도 없음** → followup 생략 (silent)
4. **수동 발송 + 정상**
5. **수동 발송 + owner row 비어있음** → fallback + 라벨
6. **Slack 표시명에 상태 prefix / 팀 접미사** → 자동 매칭되어 `<@U..>` 멘션 발생

각 시나리오는 `apps/integrations/tests.py` 에 대응 테스트가 있어야 한다 (회귀 방지).

## 병렬 에이전트 (기본 지침)

- **빠른 작업을 위해, 독립적으로 나눌 수 있는 일은 기본적으로 병렬 서브에이전트로 동시에 처리**한다. 직렬로 하나씩 처리하지 않는 것을 기본으로 삼는다.
- 에이전트는 `.claude/agents/` 에 정의돼 있다 — `pipeline-dev` / `integrations-dev` / `frontend-dev` / `backend-dev` / `qa-test` / `code-reviewer` / `docs-keeper`. 담당 영역·디스패치 패턴의 단일 진실 소스는 `AGENTS.md`.
- **한 번의 메시지에서 여러 에이전트를 동시에** 띄운다. 예: 새 기능 = `backend-dev` ∥ `frontend-dev` ∥ `qa-test` → 직후 `code-reviewer` ∥ `docs-keeper`.
- **예외 (직렬/단독으로 처리)**: ① 상태기계 전이 등 cross-cutting 핵심 로직(`pipeline-dev` 또는 메인 단독) ② 같은 파일을 동시에 편집해야 하는 작업 ③ 분할 이득이 없는 한두 줄짜리 수정(메인이 직접).
- **커밋·push 는 항상 메인이 일괄** 처리한다. 에이전트는 구현/조사/리뷰만 하고 커밋/push 하지 않는다.
- 에이전트가 "사용자 확인 필요" 로 보고한 항목(외부 자격증명·실데이터 의존 등)은 메인이 사용자에게 전달한다.

## 일반 원칙

- 응답은 한국어로, 짧은 원인 → 조치 형태로.
- **독립 작업은 병렬 서브에이전트로 분배하는 것을 기본**으로 한다 (상세: 위 "병렬 에이전트 (기본 지침)" / `AGENTS.md`).
- `db.sqlite3` 는 Git 에서 제외(`.gitignore`) 한다 — 커밋/`git add -f` 하지 않는다 (GitHub 100MB 제한 초과). DB 이관·동기화는 `apps/syncops` 스냅샷 명령으로만.
- 파이프라인 상태 전이 로직을 건드릴 때는 maintenance.md §2 의 상태 기계 다이어그램을 함께 갱신.
- **API Key (`apps/api_v2/models.ApiKey`) 의 평문**은 발급 직후 1회 외 어디에도 남기지 않는다. 로그/Slack/Activity Log/응답 본문/테스트 fixture 어디에도 평문을 저장하지 않는다 — DB 에는 SHA-256 해시(`key_hash`) 와 prefix 만. 평문을 디버깅용으로 print 한 코드/콘솔 출력이 보이면 즉시 제거.
