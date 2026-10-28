# API 개발 요구사항 — 서버 이관용 데이터 내보내기 (원본 → 새 서버, HTTPS 만으로)

> **(2026-10 서버 이관 완료 — 이력 문서)** 이 문서의 옛 주소 `https://qualitygate.jkintra.net`(AWS 원본·ALB)은 이관 완료로 **종료**됐다(502). 현재 접속은 게이트웨이 `https://qw.jkintra.net/qg/`, 프로그램 호출(API Key)은 `https://qw.jkintra.net/qg/ci-api/` 이다. 아래 서술은 작성 당시(날짜 맥락) 기준이다.

작성: 2026-10-09 · 대상: 원본 Quality Gate (`https://qualitygate.jkintra.net`) 를 운영/배포하는 담당자 · 관련: `docs/prd_server_migration_sync.md`(스냅샷 번들 방식), `docs/api_guide.md`

> **상태 (2026-10-09): 구현됨 (P0, config 제외).** 서버 API `/api/v2/export/snapshots/`(스코프 `export:read`)와 새 서버 명령 `fetch_snapshot_via_api` 가 구현·테스트(84개 OK, 로컬 zstd 바이너리가 없어 zstd 왕복 1건 skip)됐다. **원본에 이 코드가 배포되기 전에는 쓸 수 없다**(§5-1). 사용법·오류 코드는 `docs/api_guide.md` §4.5·§7.1a, 이관 절차는 `docs/prd_server_migration_sync.md` §5.5·§6.3 경로 C. P1(테이블 스트리밍, R8~R11)은 구현하지 않았다. 아래 §2~§4 는 요구사항 원문이며, 구현 결과는 §6 에 정리한다.

## 1. 왜 필요한가

새 서버(IDC `wx-qms221`, `172.20.0.221`)에서 원본(AWS)으로 **SSH(22)·IP 직결(80/443/8601)은 막혀 있고**, `https://qualitygate.jkintra.net`(ALB) 의 **HTTPS 만 열려 있다**(2026-10-09 `curl` 확인: `/health/` 200). 원본 서버 셸 접근도 새 서버 쪽 작업자에게는 없다. 그래서 `apps/syncops` 의 `export_snapshot` → rsync 경로를 쓸 수 없고, **HTTPS API 로 데이터를 내려받는 수단**이 필요하다.

## 2. 현재 v2 API 로 가져올 수 있는 범위 (실측, 읽기 전용 GET)

키: `admin`(`qpe_admin`) 소유, 스코프 `meta:read runs:read runs:trigger runs:control *`.

| 엔드포인트 | 건수(원본) | 내용 |
|---|---:|---|
| `GET /api/v2/folders/` | 1 | id·name·description·gate_count·subfolder_count·타임스탬프 (요약) |
| `GET /api/v2/gates/` | 5 | id·name·gate_type·플래그·folder/subfolder 이름 (**Jenkins 설정·파라미터·FK 전체 아님**) |
| `GET /api/v2/suites/` | 18 | id·name·jenkins_folder_path/url·job_count (**Job 상세 없음**) |
| `GET /api/v2/gates/<id>/runs/` + `runs/<id>/` + `runs/<id>/console/<stage>/` | 게이트 Run 96 | Run 상태·단계별 콘솔 |

**못 가져오는 것** (원본 53테이블 중 대부분, 용량 기준 ≈ 100%):

| 영역 | 테이블 | 행 수(PRD §3.1 기준 시점) | 비고 |
|---|---|---:|---|
| Suite Job 콘솔 로그 | `suites_test_suite_job_run` | 53,214 | **DB 의 약 95% (≈4.9GB)**. API 없음 |
| Suite Run | `suites_test_suite_run` | 29,918 | API 없음 |
| TAP Run | `suites_tap_run` | 3,382 | 약 179MB, API 없음 |
| Reliability | `robustness_run` / `robustness_schedule` / `robustness_test` | 25,365 / … | API 없음 |
| 활동 로그 | `activity_log` | 39,029 | API 없음 |
| 사용자·세션·API Key | `accounts_user`, `django_session`, `api_v2_apikey` | 22 / 406 / … | 비밀번호 해시·키 해시 포함 — 일반 API 로 노출 금지 |
| 담당자·알림 설정 | `integrations_*`, `notifications_*` | 333 … | API 없음 |
| Gate 상세·Run 테스트 | `qualitygates_gate`, `_gate_suite`, `_run_test` … | 488 … | 요약만 있고 FK·설정 전체 없음 |
| 마이그레이션 상태 | `django_migrations` | 112 | 새 서버 코드와의 정합 검증에 필요 |

→ **현재 API 만으로는 "전체 데이터 싱크"가 불가능하다.** 요약 필드만 있는 게이트/스위트를 옮겨도 PK·FK·상세가 없어 정합한 복제가 안 된다. 추가 개발이 필요하다.

## 3. 요구사항

우선순위 **P0 = 권장안(가장 빠르고 안전)**, P1 = 대안. 둘 중 하나만 구현해도 된다.

### P0. 스냅샷 생성·다운로드 API (기존 `export_snapshot` 재사용)

`apps/syncops` 에 이미 검증된 export(sqlite3 backup API, 무결성 sha256, 압축, config 암호화)가 있다. 이것을 HTTPS 로 노출한다.

| ID | 요구 | 상세 |
|---|---|---|
| R1 | 전용 스코프 | 새 스코프 `export:read`(DB 내용) 와 `export:secrets`(config: `.env`·secrets·crontab) 를 분리. 일반 `*` 키로는 호출 불가. admin 이 발급·회수. 발급 시 만료 7일 이하 권장. |
| R2 | 스냅샷 생성 | `POST /api/v2/export/snapshots/` → 비동기 작업 시작, `202 {id, status:"running"}`. 내부는 `snapshot.export_snapshot()` 을 호출(원본 DB `mode=ro`, 운영에 부담이 없게 `--max-backup-seconds` 정책 유지). 동시에 하나만(`409`). |
| R3 | 상태 조회 | `GET /api/v2/export/snapshots/<id>/` → `{status, progress_pct, created_at, manifest}`. `manifest` 는 `manifest.json` 과 동일(테이블별 행 수, 마이그레이션 목록, git commit, db sha256). |
| R4 | 다운로드 | `GET /api/v2/export/snapshots/<id>/download/<file>/` — `db.sqlite3.gz`(또는 `.zst`), `manifest.json`, 선택적으로 `config.tar.enc`. **HTTP Range·`Content-Length`·`ETag` 지원(재개)**, 스트리밍, `X-Content-SHA256` 헤더. 5GB 급이므로 앱 프로세스 메모리에 올리지 말 것(`X-Accel-Redirect`/`FileResponse`). |
| R5 | 보안 | HTTPS 전용, 사내망 한정(ALB SG), 요청마다 감사 로그(`ActivityLog`: 누가·언제·어떤 파일·크기). 스냅샷은 **다운로드 완료 확인(`DELETE`) 또는 24h 후 자동 삭제**. 응답에 비밀(키 평문)을 절대 싣지 않는다 — config 는 `export:secrets` + 패스프레이즈 암호화(`--passphrase-env` 와 동일 방식)만. |
| R6 | 일관성 | 한 스냅샷 안의 모든 테이블은 같은 시점(sqlite backup 이 보장). `manifest.tables` 와 새 서버의 `verify_snapshot` 이 그대로 통과해야 한다. |
| R7 | 운영 영향 | 생성 중 원본 쓰기가 많으면 backup 이 재시작될 수 있다(`docs/prd_server_migration_sync.md` §9) → 실패 시 `status:"failed"` + 사유(`database is locked`/시간 초과)를 돌려주고 재시도 가능. 점검 창이 없으면 업무 외 시간 실행 권장. |

수락 기준: ① 새 서버에서 `curl` 만으로 번들 디렉터리(`manifest.json`, `db.sqlite3.*`)를 내려받는다. ② 그 디렉터리에 `import_snapshot --yes` + `verify_snapshot --allow-newer` 가 종료코드 0. ③ 다운로드 중단 후 Range 로 이어받기. ④ `export:read` 없는 키는 403.

### P1(대안). 테이블 단위 스트리밍 export

스냅샷 파일 생성이 부담이면 테이블별로 흘려보낸다.

| ID | 요구 | 상세 |
|---|---|---|
| R8 | 매니페스트 | `GET /api/v2/export/manifest/` → 테이블 목록·행 수·마이그레이션·git commit·허용 테이블 allowlist. |
| R9 | 테이블 스트림 | `GET /api/v2/export/tables/<table>/?after=<pk>&limit=5000` → **NDJSON**(gzip). 원본 컬럼 값 그대로(PK·FK·타임스탬프·NULL 보존, BLOB 은 base64), `pk` 오름차순 **keyset 페이지네이션**(offset 금지). `suites_test_suite_job_run` 은 행당 수십 KB 이므로 페이지 크기 조정·`Content-Encoding: gzip` 필수. |
| R10 | 정합 | 응답 헤더 `X-Table-Count`, 마지막 페이지에 테이블 sha256(행 정렬 기준) 제공. 새 서버가 행 수·해시를 검증. |
| R11 | 단점 | 테이블 간 시점이 다르다(서비스 가동 중이면 FK 불일치 가능) → 점검 창 필요, 또는 스냅샷 id 를 두고 한 읽기 트랜잭션(`BEGIN`) 안에서 서빙. P0 대비 구현·검증 비용이 크다. |

### 공통(P0·P1)

| ID | 요구 |
|---|---|
| R12 | 사용자 테이블의 `password` 해시·`django_session`·`api_v2_apikey.key_hash` 는 `export:secrets` 가 있을 때만. 평문 API Key 는 DB 에도 없다(해시만) — 내보내기에도 평문이 생기지 않음을 테스트로 보장. |
| R13 | 호출 제한: 키당 동시 1건, 분당 요청 제한(throttle) — 운영 폴링과 DB 락 경합 방지. |
| R14 | 문서: `docs/api_guide.md` 에 엔드포인트·스코프·오류 코드 추가, `/api/v2/docs/`(OpenAPI) 에 노출. |
| R15 | 테스트: 임시 DB 로 export→download→import→verify 왕복, 스코프 403, Range 재개, 평문 키 미노출. (`apps/syncops` 테스트 패턴 사용) |

## 4. 구현 위치 제안

- `apps/api_v2/urls_export.py`(신규) + `apps/api_v2/views_export.py`, 스코프는 `apps/api_v2/scopes.py` 에 `export:read`·`export:secrets` 추가.
- 생성 작업은 `apps.syncops.snapshot.export_snapshot`(이미 `interrupt_guard`·디스크 선점검 포함) 재사용. 비동기는 기존 방식대로(스레드/Celery EAGER 설정 확인 — `config.settings.dev` 는 EAGER).
- 새 서버 쪽은 추가 개발이 거의 없다: `curl` 로 번들 수신 → 기존 `import_snapshot`/`verify_snapshot`. (선택) 편의 명령 `fetch_snapshot_via_api --url --key-env VAR`(다운로드 + sha256 검증 + 재개).

## 5. 결정/확인이 필요한 것 (원문 — 구현 후 현황은 §7)

1. **원본에 이 코드를 배포할 수 있는 사람**: 원본 서버 셸/배포 권한자. API 개발이 끝나도 원본에 배포되지 않으면 쓸 수 없다. (이 저장소 변경은 원본이 `git pull` + 재시작해야 반영된다 — 원본 설치 유닛이 `run.sh`(자동 ff-merge)면 재시작만으로 반영.)
2. P0 vs P1 선택(권장 P0).
3. 점검 창: 원본 쓰기 중단 없이 스냅샷을 뜰 수 있는지(`max-backup-seconds` 안에 끝나는지)는 **미측정**. 안 되면 cron 정지 창이 필요하다.
4. `export:secrets`(config) 를 API 로 줄지, 별도 채널(사람이 직접 전달)로 둘지 — 비밀이 HTTPS 로 지나가는 것에 대한 보안 승인.
5. 임시 키 폐기: 조사에 쓴 `admin` 소유 `*` 스코프 키(prefix `qgp_4387a484`)는 **즉시 회수**할 것(대화 기록에 평문이 남아 있다).

## 6. 구현 결과 (2026-10-09, P0) — 요구사항 대비

| ID | 상태 | 구현 / 차이점 |
|---|---|---|
| R1 | 부분 | `export:read` 만 구현 — **명시 부여 전용**(`*` 미포함), 키 소유자 admin 이어야 동작(`ExportPermission`, 요청 시점 재확인). **`export:secrets` 는 만들지 않았다** — config(`.env`·secrets·crontab)는 API 로 내보내지 않는다(→ §7 결정 4) |
| R2 | 구현 | `POST /api/v2/export/snapshots/` → 202, 백그라운드 스레드에서 `export_snapshot(include_config=False)`. **동시에 1개**(409 `export_in_progress`). `include_config` 등 알 수 없는 필드는 400 `unsupported` |
| R3 | 구현 | `GET …/<id>/` → status(`running\|ready\|failed\|expired`)·`progress_pct`·`manifest`·`files`. 상태는 DB 모델이 아니라 번들 안 `state.json`(새 모델·마이그레이션 없음) |
| R4 | 구현 | `GET …/download/<name>/` — Range 206·`Content-Length`·`ETag`·`Accept-Ranges`·`X-Content-SHA256`, 1MiB 청크 스트리밍(`X-Accel-Redirect` 는 쓰지 않음). 허용 이름은 `manifest.json`·`db.sqlite3[.zst\|.gz]` 뿐 |
| R5 | 구현 | 요청마다 로거 + 기존 `ActivityLog`(새 enum 값 없이 재사용; Range 재개 요청은 키·스냅샷·파일당 1시간에 1회만 DB 기록). 24시간 뒤 자동 만료·삭제, `DELETE` 로 즉시 삭제. 응답/로그에 키 평문 없음(테스트로 보장) |
| R6 | 구현 | sqlite backup 일관 스냅샷. 새 서버 `import_snapshot --yes` + `verify_snapshot --allow-newer` 왕복 테스트 통과 |
| R7 | 구현(부분) | 실패 시 `status:"failed"` + `error` 사유, 프로세스가 죽어 남은 `running` 은 조회 시 `failed` 로 정리. 쓰기가 많을 때의 backup 지연은 **미측정**(§7 결정 3) |
| R8~R11 (P1) | 미구현 | 테이블 스트리밍은 만들지 않았다(P0 로 충분) |
| R12 | 해당 없음 | config·비밀을 내보내지 않으므로 `export:secrets` 불필요. DB 에는 사용자 비밀번호 해시·세션·`api_v2_apikey.key_hash` 가 **그대로 들어 있다**(번들 DB 는 암호화되지 않음 → admin·명시 스코프로만 접근). 평문 키는 DB 에 없고 내보내기에도 생기지 않음을 테스트로 보장 |
| R13 | 부분 | 동시 실행은 서버 전체 1개. 생성·삭제는 Write throttle(30/min), 조회는 Read throttle(120/min). **다운로드는 throttle 없음**(Range 재개가 한도에 걸리지 않도록; admin + 명시 스코프만 접근) |
| R14 | 부분 | `docs/api_guide.md` 에 엔드포인트·스코프·오류 코드·curl 예시 추가. **OpenAPI(`/api/v2/docs/`)에는 노출하지 않는다**(`extend_schema(exclude=True)`) |
| R15 | 구현 | 서버 `apps/api_v2/tests_export.py` 31개, 클라이언트 `apps/syncops/tests_fetch_api.py` 34개, 왕복(LiveServer) `apps/syncops/tests_api_roundtrip.py` 19개 — export→download→import→verify 왕복, 스코프 403, Range 재개, 평문 키 미노출, `makemigrations --check` 변경 없음 |
| 선택 | 구현 | 새 서버 편의 명령 `fetch_snapshot_via_api --url --key-env VAR`(폴링·`.part` 이어받기·sha256 검증·번들 배치·원격 DELETE) |

구현 위치: `apps/api_v2/{scopes,permissions,views_export,urls_export,urls}.py`, 키 발급 폼 `apps/accounts/views.py:_scope_options_for`(admin 에게만 `export:read` 노출), 설정 `config/settings/base.py` 의 `SYNCOPS_EXPORT_DB_PATH`/`SYNCOPS_EXPORT_DIR`(기본 None=운영 DB·`BASE_DIR/snapshots`, 테스트용 오버라이드), 클라이언트 `apps/syncops/management/commands/fetch_snapshot_via_api.py`.

## 7. 남은 결정 / 확인 사항

1. **원본에 배포**: 원본 서버 셸/배포 권한자가 이 코드를 반영(`git pull` + 서비스 재시작)해야 한다. 미배포 시 `/api/v2/export/snapshots/` 는 404. 이 저장소의 변경은 아직 커밋·push 전일 수 있으니 push 선행.
2. **P0 로 확정**(P1 미구현). 테이블 단위 스트리밍이 꼭 필요해지면 별도 작업.
3. **점검 창**: 원본 쓰기 중단 없이 스냅샷을 뜰 수 있는지(backup 이 끝까지 가는지)는 **미측정**. 안 되면 cron·웹 정지 창이 필요하다(PRD §9·§12 #2). 현재 API 에는 `--max-backup-seconds` 에 해당하는 옵션이 없다.
4. **config 전달**: 이번 구현은 config 를 API 로 주지 않기로 확정(`export:secrets` 미구현) — 새 서버의 `.env`/secrets 는 사람이 별도 채널로 전달·구성해야 한다(PRD §5.5·§12 #20). 비밀을 HTTPS 로 주는 안을 되살리려면 보안 승인이 먼저다.
5. **임시 키 폐기**: 조사에 쓴 `admin` 소유 `*` 스코프 키(prefix `qgp_4387a484`)는 **즉시 회수**(대화 기록에 평문이 남아 있다). 실제 이관에는 `export:read` 만 가진 **짧은 만료의 별도 키**를 새로 발급하고, 작업 후 회수한다.
6. **실제 원본 왕복은 자동 검증 불가**: 테스트는 로컬 가짜 서버/임시 DB 만 사용했다. 원본(ALB) 경유 실제 5GB 다운로드의 소요 시간·ALB 타임아웃/유휴 연결 제한 영향은 최초 리허설에서 확인해야 한다(끊겨도 `--existing-id` 로 Range 재개).
