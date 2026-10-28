# PRD — Slack 실패 알림 인터랙티브 Triage 버튼 (Failure / Skip)

**Product:** Quality Gate Portal (QGP)
**Author:** QPE Team
**Date:** 2026-07-07
**Status:** 구현 완료 (2026-07-07) — Socket Mode PoC 실환경 검증 + 코드/마이그레이션/단위테스트(18) 완료. 활성화는 `SLACK_TRIAGE_BUTTONS=1` + 리스너 systemd 상주 필요.
**관련 조사:** 코드 조사(2026-07-07) — 발송=`apps/notifications/services.py`(attachments), 수신 인프라 전무, 인트라넷 도메인 → **Socket Mode 채택 확정**

---

## 1. 배경 & 문제 (Background)

Suite Run 이 실패하면 Slack 으로 FAIL 알림이 발송된다(`send_suite_run_event` → `_post_message`). 그런데 실패에는 **두 종류**가 섞여 있다:

- **실제 장애 (Failure)** — 대상 API/서비스가 진짜 깨진 것. F/U(추적) 필요.
- **자동화 코드 문제 (Skip)** — 테스트 스크립트/환경 이슈라 제품 장애가 아님. 넘겨야 함.

현재는 이 구분이 Slack 스레드/구두로만 이뤄져 **유실되고, 포털 결과는 무조건 "Fail"** 로만 남는다. 실패의 실제 성격(장애냐/자동화 문제냐)이 데이터로 안 남아 대시보드 지표가 왜곡된다.

## 2. 목표 (Goals)

- Slack **실패 알림 하단에 버튼 2개**(`Failure`, `Skip`)를 노출한다. (성공 알림엔 버튼 없음)
- 담당자가 버튼을 누르면 그 실패 Job 의 **결과가 `Fail` → `Failure(장애)` 또는 `Skip(자동화 문제)`** 로 확정된다.
- 확정 결과가 **포털(대시보드/Suite Runs 목록/상세)과 외부 API(api_v2)** 에 반영된다.
- 버튼을 누른 뒤 **Slack 원본 메시지가 갱신**되어 "누가/무엇으로 처리했는지" 남고 버튼은 비활성화된다.

### 2.1 Non-Goals (이번 범위 아님)

- 성공 알림에 버튼 추가 (불필요).
- Quality Gate 파이프라인(`apps/qualitygates`)의 실패 알림 — **1차는 Test Suite Run 만**. (QG 도 동일 구조로 확장 가능하나 별도.)
- 이미 발송된(과거) 메시지 소급 처리.
- Slack 에서 재실행/재시도 트리거 (조회·분류만).
- Skip 사유 텍스트 입력(모달) — 2차 확장 후보. 1차는 버튼 클릭만.

## 3. 사용자 & 권한 (Users & Permissions)

- 버튼 클릭 주체: **담당 개발자 / QPE**. Slack 유저 → 포털 `User.slack_user_id` 로 매핑해 `triaged_by` 기록. 매핑 실패 시 Slack username 만 문자열로 기록.
- 권한 정책(택1, §11 Open Question): (a) 누구나 클릭 가능(단순) / (b) `slack_user_id` 매핑되고 `can_write` 인 유저만 — **권장 (b)**. 매핑/권한 없으면 ephemeral 메시지로 "권한 없음" 안내.

## 4. 사용자 플로우 (User Flow)

```
Suite Run FAIL
      │
      ▼
Slack 알림 발송 (blocks + [Failure] [Skip] 버튼)   ← ts/channel 저장
      │  (담당자가 버튼 클릭)
      ▼
Slack → (Socket Mode WS) → 포털 리스너 프로세스
      │  block_actions payload {action_id, value=job_run_id, user}
      ▼
검증(권한) → TestSuiteJobRun.verdict = FAILURE|SKIP + triaged_by/at 저장
      │
      ├─→ 포털 결과 갱신 (집계/대시보드/목록/상세/api_v2)
      └─→ chat.update: "✅ 처리됨 — Failure by @홍길동" + 버튼 제거
```

## 5. 아키텍처 — 수신 = Socket Mode (확정)

인트라넷(`qualitygate.jkintra.net`, 사설 IP)이라 Slack(공개 인터넷)의 인바운드 HTTP 도달이 불가/불투명 → **Socket Mode**(앱이 Slack 으로 **아웃바운드 WebSocket** 연결, 공개 URL 불필요) 채택.

- **라이브러리**: `slack-bolt`(+`slack_sdk`, `websocket-client`). `requirements.txt` 추가.
- **App-level token**: `xapp-...` (Socket Mode 용). env `SLACK_APP_TOKEN`. 기존 `SLACK_BOT_TOKEN`(xoxb-, chat:write)은 그대로.
- **상시 리스너 프로세스**: `python manage.py slack_socket_listener` (신규 management command) 가 `SocketModeHandler` 로 Slack 이벤트 수신. 현재 배포는 runserver + cron 원샷 → **systemd(또는 supervisor) 유닛으로 상주화**(자동 재시작). run.sh 와 별개 프로세스.
- **핸들러**: bolt `@app.action("triage_failure")` / `@app.action("triage_skip")` 에서 payload 파싱 → 도메인 서비스 호출.
- **폴백/토글**: env `SLACK_SOCKET_MODE=1` 일 때만 리스너 동작. 꺼져 있으면 버튼은 그대로 보내되(선택) 클릭은 무처리 — 또는 버튼 자체를 flag off 시 미노출.

### 5.1 아웃바운드 발송(기존 재사용)
`_post_message` / 토큰·채널 해석 / async 데몬스레드 / `slack_users` 멘션 매핑은 그대로 활용. 발송 포맷만 attachments→blocks 로 확장.

## 6. 데이터 모델 변경 (Data Model)

`apps/suites/models.py`:

- **`TestSuiteJobRun.verdict`** (신규, `TextChoices`):
  - `UNTRIAGED = ""` (기본 — 실패했지만 미분류)
  - `FAILURE = "failure"` (실제 장애)
  - `SKIP = "skip"` (자동화 코드 문제)
- **`TestSuiteJobRun.triaged_by`** (FK User, null) / **`triaged_at`** (DateTime, null)
- **Slack 메시지 참조**(버튼 후 chat.update 용): `slack_message_ts` (Char) + `slack_channel_id` (Char) — 발송 시 저장. (또는 별도 `SlackMessage` 모델 — §11)
- 마이그레이션 신설. 기존 row 는 `verdict=""`(UNTRIAGED).

**Run-level 결과 파생** (verdict 반영):
- 단일 Job Suite(현 주 사용 패턴, T1): run 결과 = 그 Job 의 verdict.
- 다중 Job: 실패 Job 들의 verdict 집계 — 하나라도 `FAILURE` → **Failure**, 전부 `SKIP` → **Skip**, 하나라도 `UNTRIAGED` → **미분류(Fail)**.
- `failed_jobs` 카운트는 유지하되, **"실장애 수"**(verdict!=SKIP 인 실패)와 구분해 노출.

## 7. Slack 메시지 포맷 (Blocks)

기존 attachment(fields) → **blocks** 로 전환(실패 알림 한정). 마지막에 `actions` 블록 추가:

```json
{ "type": "actions", "block_id": "triage_<job_run_id>",
  "elements": [
    { "type": "button", "action_id": "triage_failure",
      "text": {"type":"plain_text","text":"🔴 Failure (실제 장애)"},
      "style":"danger", "value":"<job_run_id>",
      "confirm": { ... "이 실패를 실제 장애로 확정합니다" ... } },
    { "type": "button", "action_id": "triage_skip",
      "text": {"type":"plain_text","text":"⏭️ Skip (자동화 문제)"},
      "value":"<job_run_id>",
      "confirm": { ... "자동화 코드 문제로 넘깁니다" ... } }
  ] }
```

- 버튼 밑 짧은 설명(context block): "**Failure** = 실제 서비스 장애 · **Skip** = 자동화 코드/환경 문제로 제외".
- 기존 본문(담당자 멘션·실패 호출·에러 발췌)은 blocks(section)로 이식.
- `value` = `job_run_id`(UUID). 다중 실패 Job 이면 Job 별로 알림/버튼이 이미 분리돼 있는지 확인해 매핑(§11).

## 8. 인터랙션 처리 (Handler)

1. Socket Mode 로 `block_actions` 수신(bolt 가 서명/토큰 검증 처리 — 별도 signing secret 불필요).
2. `action_id`(`triage_failure|triage_skip`) + `value`(job_run_id) + `user.id`(Slack) 파싱.
3. 권한 체크(§3). Slack user → `User.slack_user_id` 매핑.
4. 서비스 `apply_triage(job_run_id, verdict, by_user)` (신규, `apps/suites/services` 또는 notifications):
   - 멱등(이미 분류돼 있으면 재확정/거부 — §11). `verdict` + `triaged_by/at` 저장.
   - run-level 결과 재계산(§6) + 캐시/집계 갱신.
5. `chat.update`(저장한 ts/channel) 로 원본 메시지 갱신:
   - actions 블록 제거 → context 블록 "✅ **Failure** 처리됨 — @홍길동, 07-07 18:20".
6. 실패/권한없음 → `respond`(ephemeral) 로 클릭한 사람에게만 안내.

## 9. 결과 반영 (Result Surfacing)

- **Suite Run 상세** (`run_detail`) — 실패 Job 에 verdict 배지(🔴 Failure / ⏭️ Skip / 미분류) + 처리자/시각. 포털에서도 수동 재분류 가능(선택).
- **Suite Runs 목록 / 대시보드** — result 컬럼/집계가 verdict 반영(Skip 은 "실장애"에서 제외).
- **api_v2 결과**(`phase.py` 등 외부 API) — verdict 필드 노출(소비자=강성진 클라우드인프라 영향 확인 필요, §11).
- **지표** — 대시보드 fail 카운트를 "실장애(Failure) / 자동화(Skip) / 미분류" 로 분리 노출(장애율 정확화).

## 10. 인프라 / 설정 요구사항 (Infra)

| 항목 | 필요 작업 | 담당 |
|---|---|---|
| Slack App | Socket Mode 활성화 + Interactivity 활성화 + app-level token(`xapp-`, `connections:write`) 발급 | Slack 워크스페이스 관리자 |
| Bot scope | `chat:write`(기존) 로 chat.update 가능 확인 | 〃 |
| env | `SLACK_APP_TOKEN=xapp-...`, `SLACK_SOCKET_MODE=1`, (선택)`SLACK_TRIAGE_BUTTONS=1` | 운영 |
| 방화벽 | EC2 → Slack `wss://` **아웃바운드 허용** | 인프라 |
| 프로세스 | `slack_socket_listener` 상주화(systemd 유닛 + 자동재시작) | 인프라/운영 |
| 의존성 | `slack-bolt` requirements 추가 + `.venv` 설치 | 개발 |

## 11. 미해결 / 결정 필요 (Open Questions)

1. **다중 실패 Job**: 한 Suite Run 에 실패 Job 이 여러 개면 알림/버튼을 Job 별로 낼지, Run 단위 1개로 낼지. (현 주 패턴은 T1 단일 Job → 단순.)
2. **권한**: 누구나 클릭 vs `can_write`+매핑 유저만. (권장 후자.)
3. **멱등/재분류**: 이미 처리된 걸 다시 누르면 — 거부/덮어쓰기/이력. (권장: 덮어쓰기 + 처리자·시각 갱신, 메시지에 최신 상태.)
4. **ts 저장 위치**: `TestSuiteJobRun` 필드 vs 별도 `SlackTriageMessage` 모델. (단순하면 필드.)
5. **api_v2 소비자 영향**: 외부 결과 스키마에 verdict 추가가 강성진 쪽 파싱을 깨지 않는지(추가 필드라 보통 안전) 확인.
6. **버튼만 먼저?**: Socket Mode/systemd 준비 전이라도 "버튼 노출 + 클릭 시 미처리"로 단계 배포할지.

## 12. 단계별 구현 (Phases)

- **P0 — 준비**: Slack 앱 Socket Mode/token/scope 설정(인프라·관리자), env, `slack-bolt` 도입, 아웃바운드 wss 방화벽·systemd 확인.
- **P1 — 데이터/발송**: `verdict`/`triaged_*`/`slack_ts` 모델+마이그레이션. `_post_message` 가 ts/channel 반환·저장. 실패 알림 blocks+버튼 발송(성공은 그대로).
- **P2 — 수신**: `slack_socket_listener` 커맨드 + bolt 핸들러 + `apply_triage` 서비스 + chat.update. (systemd 상주화)
- **P3 — 반영**: run-level 결과 파생 + 대시보드/목록/상세 배지 + api_v2 노출 + 지표 분리.
- **P4 — 검증/문서**: 단위테스트(핸들러 payload mock, 결과 파생, 멱등), README/maintenance/SKILLS 갱신, 회귀.

## 13. 테스트 계획 (Testing)

- 발송: 실패 시 blocks+2버튼 포함(성공은 미포함) 검증. ts/channel 저장 검증.
- 핸들러: `block_actions` payload(mock)로 `apply_triage` 호출 → verdict/triaged_by/at 저장 + chat.update 호출 인자 검증. 권한 없음·미매핑·멱등 케이스.
- 결과 파생: 단일/다중 Job 조합에서 run 결과(Failure/Skip/미분류) 정확성.
- Socket Mode 는 실제 WS 연결 대신 핸들러 함수 단위로 테스트(리스너 부팅은 smoke).

## 14. 리스크

- Socket Mode 리스너가 죽으면 버튼 클릭이 유실 → systemd 자동재시작 + 헬스 로깅 필수. (클릭 유실 시 포털에서 수동 재분류로 보완.)
- 아웃바운드 wss 방화벽 차단 시 전면 불가 → P0 에서 선검증.
- 메시지 blocks 전환으로 기존 알림 레이아웃 변동 → fallback 텍스트 유지 + QA.
