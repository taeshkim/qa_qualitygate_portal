# SKILLS.md — 빠른 작업 환경 & 스킬셋

이 프로젝트에서 빠르게 개발·검증하기 위한 **환경·명령어·스킬** 모음. 에이전트(병렬 작업) 구성은 [`AGENTS.md`](./AGENTS.md), 작업 절차는 [`CLAUDE.md`](./CLAUDE.md).

---

## 1. 실행 / 개발 환경

| 항목 | 값 |
|---|---|
| 가상환경 | `.venv` (python 3.13→3.12→3.11→3.10 자동 탐색) |
| 포트 | **8601** (`runserver 0.0.0.0:8601`) |
| DB | SQLite `db.sqlite3` (**git 에 포함**) |
| 설정 | `config.settings.dev` (SQLite + LocMem 캐시 + EAGER celery) — 운영도 이걸 사용 |
| 로컬 admin | `admin` / `admin123` |
| 캐시 | LocMem, prefix `qgp:`, Jenkins 파라미터 120초 |

### 서버 기동

`172.20.0.221` 은 `qualitygate.service` 로 부팅 자동 시작·종료 후 자동 복구한다. 설치 원본은 `deploy/qualitygate.service` (설치 절차 README §6). 서비스가 `.env` 를 읽는 앱을 `--noreload` 로 실행하므로 **코드·`.env` 변경 후 재시작**한다. Git/pip/migrate 는 별도 배포 작업이다.

```bash
systemctl status qualitygate
sudo systemctl restart qualitygate
sudo systemctl stop qualitygate
sudo journalctl -u qualitygate -f
```

아래는 수동 설치·개발용이다. 서비스 실행 중 동시에 띄우면 8601 포트가 충돌한다.

```bash
./run.sh            # macOS/Linux : origin/master ff 자동동기화 → venv → pip → migrate → runserver:8601
./run.ps1           # Windows
```
- `run.*` 는 기동 시 **`origin/master` 를 ff-only 로 당겨 코드만 최신화**한다. **`db.sqlite3` 는 절대 건드리지 않는다** (원격이 db 를 바꾼 커밋이면 ff 가 막혀 그 회차 동기화는 건너뜀 — DB 갱신은 수동 명령으로만).
- 서비스 실행 중(수동이면 실행 터미널 유지) `curl -s http://localhost:8601/health/` → `{"status":"ok"}` 로 백엔드 확인.
- **Nginx `/qg` 배포**: `.env` 에 `QGP_SCRIPT_NAME=/qg`, 기존 `DJANGO_ALLOWED_HOSTS` 목록에 `172.20.0.221`, `qw.jkintra.net` 추가 후 서비스 재시작 (수동 환경은 `./run.sh`). Nginx 는 `/qg/` → `http://host.docker.internal:8601/` 로 접두 경로를 제거해 전달하고 실제 적용까지 확인한다. 브라우저 주소는 `http://172.20.0.221/qg/`, 프록시 헬스체크는 `curl -s http://172.20.0.221/qg/health/`. 로컬 루트 브라우저 접속은 `QGP_SCRIPT_NAME` 미설정 시 사용한다.

### SSO(Keycloak) 로그인 환경 변수

`.env` 에서 설정(값 예시·의미는 `.env.example`, `README.md` §8, `maintenance.md` §7 "SSO 로그인"). **`OIDC_INTERNAL_ISSUER`/`CLIENT_ID`/`CLIENT_SECRET` 셋이 모두 있으면 SSO 가 켜지고 자체 로그인이 닫힌다** — 로컬 개발에서는 비워 두면 기존 `admin`/`admin123` 로그인이 유지된다. `.env.example` 의 해당 세 줄은 주석 처리돼 있다. 시크릿 평문은 `.env` 에만 두고 로그/출력에 남기지 않는다. `OIDC_INTERNAL_POST_LOGOUT_REDIRECT`(기본 False, Keycloak client 에 post logout redirect URI 등록 후 True)·`TRUSTED_PROXY_CIDRS`(코드 기본 `172.20.0.222/32`)·`GATEWAY_LOGOUT_URL`(코드 기본 `https://qw.jkintra.net/_qw/logout`)·`LOCAL_LOGIN_ENABLED`·**`GATEWAY_ENTRY_ENABLED`(게이트웨이 진입 로그인, 기본 False — 게이트웨이에 `/qg/accounts/gateway-login/` 보호 라우팅이 적용·검증된 뒤에만, `docs/handoff_sso_integration.md` §10)** 변경 후에도 `sudo systemctl restart qualitygate`. **`.env` 에 `TRUSTED_PROXY_CIDRS=`·`GATEWAY_LOGOUT_URL=` 처럼 빈 값 키를 두면 코드 기본값이 가려진다**(`os.getenv`) — 쓰지 않을 키는 지운다(키 존재 여부만 확인할 것: `grep -c '^KEY=' .env`, 값은 출력 금지). 운영 반영 순서·Keycloak client 신청 값은 `maintenance.md` §8 "SSO 운영 반영". `accounts 0003_externalidentity` 마이그레이션은 운영 DB 에 **사용자 승인 후 수동**으로만 적용한다.

---

## 2. 자주 쓰는 명령어 (`.venv/bin/python manage.py …`)

```bash
.venv/bin/python manage.py migrate                 # 마이그레이션 적용
.venv/bin/python manage.py makemigrations <app>    # 모델 변경 후
.venv/bin/python manage.py seed                    # 시드 데이터
.venv/bin/python manage.py test apps.integrations  # 앱 단위 테스트(빠름)
.venv/bin/python manage.py test                    # 전체 테스트
QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= GATEWAY_ENTRY_ENABLED= TRUSTED_PROXY_CIDRS= QGP_BASE_URL= .venv/bin/python manage.py test apps.accounts config.test_subpath   # SSO(Keycloak) 로그인·로그아웃 회귀 (배포값·외부 연동키를 비운 환경)
.venv/bin/python manage.py sync_api_owners_from_sheet           # 담당자 시트 수동 동기화
.venv/bin/python manage.py sync_api_owners_from_sheet --if-due  # 주기 도래 시에만 (매분 cron)
.venv/bin/python manage.py sweep_reliability                    # Reliability 스케줄 sweep + 단독 RUNNING Suite Run 폴링 (★매분 cron — 스케줄 트리거·완료 감지 구동)
.venv/bin/python manage.py jenkins_keepalive                    # Jenkins SSO 세션 유지 핑 (Quality Gate 파이프라인용, 선택 — Suite/Reliability 는 GWT 전환으로 불필요해짐)
.venv/bin/python manage.py slack_triage_listener                # Slack 실패알림 Failure/Skip 버튼 클릭 수신 (Socket Mode, ★상시 프로세스=systemd 상주. SLACK_APP_TOKEN=xapp- 필요)
.venv/bin/python manage.py export_snapshot --dry-run            # 서버 이관 스냅샷 계획(DB 크기·테이블별 행 수·config 항목·디스크). DB mode=ro, 아무것도 안 씀 → 운영에서 안전
.venv/bin/python manage.py export_snapshot [--out DIR] [--no-config | --passphrase-env VAR]   # ★ 실제 번들 생성(원본 서비스·cron 정지 후). snapshots/qg-<UTC>/
.venv/bin/python manage.py export_snapshot --api-bundle --compress gzip   # ★ 원본 셸(서비스·cron 정지 후): Export API 가 그대로 내려줄 snapshots/api-<uuid>/ 번들(state.json=ready, config 제외) 생성 → 서비스 기동 → 새 서버 `fetch_snapshot_via_api --existing-id <uuid>`. API 내부 백업이 쓰기 간섭으로 1800s 초과 실패할 때의 경로
.venv/bin/python manage.py import_snapshot snapshots/qg-<TS> --yes [--restore-config --passphrase-env VAR] [--force] [--allow-older-code] [--allow-same-host] [--target-db PATH]   # 새 서버 전용(같은 호스트·원본 번들은 --allow-same-host 없이 거부). --force=대상 덮어쓰기/읽기불가 DB 교체/config .bak 만, 코드가 오래된 경우는 --allow-older-code(비권장). 운영 DB 에 쓰지 말 것
.venv/bin/python manage.py verify_snapshot snapshots/qg-<TS> [--target-db PATH] [--allow-newer]   # 행 수·마이그레이션 비교(mode=ro). migrate 후엔 --allow-newer
.venv/bin/python manage.py sync_from_source --source USER@HOST --source-dir REMOTE_PATH [--include-config --passphrase-env VAR --restore-config] [--max-backup-seconds N --compress zstd|gzip|none --ssh-opts "..." --keep-remote --allow-same-host] --yes [--dry-run]   # 새 서버에서 pull(ssh→rsync→③검증 직후 원격 번들 삭제→import→verify; --source 는 USER@HOST 형식만). 패스프레이즈 단계는 사용자 터미널에서 read -rs 후 직접 실행. 긴 작업은 tmux/nohup. 절차: docs/prd_server_migration_sync.md
.venv/bin/python manage.py fetch_snapshot_via_api --url https://HOST[/prefix] --key-env QGP_KEY [--out-dir snapshots --compress auto|gzip|zstd --poll-interval 10 --timeout 14400 --existing-id UUID --keep-remote --dry-run]   # 새 서버에서 HTTPS Export API(/api/v2/export/snapshots/)로 DB 번들 수신(경로 C): 폴링→.part Range 이어받기→sha256 검증→snapshots/qg-<ts>/ 배치→원격 DELETE→import/verify 안내. 키는 `read -rs QGP_KEY; export QGP_KEY` 로 사용자 터미널에서만(명령행·로그·대화에 값 금지). config 는 안 옴. 원본에 API 코드 배포 필요. 실패 시 메시지의 --existing-id 명령으로 재개. 절차: docs/prd_server_migration_sync.md §5.5·§6.3
.venv/bin/python manage.py sync_slack_user_ids [--overwrite]    # 포털 User 이메일→Slack member ID 매핑(User.slack_user_id 백필). Triage 버튼 권한용. 담당자 시트 동기화 시 자동 호출됨
DJANGO_SUPERUSER_PASSWORD=admin123 .venv/bin/python manage.py createsuperuser --noinput --username admin --email admin@example.com
```

### 로컬 API 점검 (curl, admin:admin123)

아래는 `QGP_SCRIPT_NAME` 미설정 예시다. 하위 경로 배포에서는 주소를 `http://172.20.0.221/qg/...` 로 바꾼다.

```bash
curl -s http://localhost:8601/health/
curl -s http://localhost:8601/api/v1/services/ -u admin:admin123 | python3 -m json.tool
curl -s http://localhost:8601/api/v1/dashboard/overview/ -u admin:admin123
curl -s http://localhost:8601/api/v1/jenkins/test-connection/ -u admin:admin123
```
> 위 curl/한 줄 python 점검은 `.claude/settings.local.json` 에 허용돼 있어 권한 프롬프트 없이 실행된다. 새 점검 패턴이 반복되면 거기에 추가.

---

## 3. Claude Code 스킬 (언제 쓰나)

| 스킬 | 용도 | 이 프로젝트에서 |
|---|---|---|
| `/run` | 앱을 띄워 변경 동작 확인 | `./run.sh` 로 8601 띄우고 화면/엔드포인트 확인 |
| `/verify` | 변경이 실제로 동작하는지 앱 띄워 검증 | UI/플로우 변경 후 골든패스+엣지 |
| `/code-review` | 현재 diff 의 버그 리뷰 | 커밋 전. `code-reviewer` 에이전트와 병용 |
| `/security-review` | 브랜치 변경 보안 리뷰 | API Key 평문·권한·인증 변경 시 |
| `/review` | PR 리뷰 | PR 올린 뒤 |
| `/loop` | 일정 간격 반복 실행 | 폴링/배치 상태를 주기적으로 확인할 때 |
| `/schedule` | cron 기반 원격 에이전트 | 정기 점검 자동화 |

---

## 4. 검증 체크리스트 (커밋 전 필수 — CLAUDE.md 순서 엄수)

- **UI/화면 변경** → `./run.sh` + 브라우저로 골든패스 + 엣지. 외부 응답 필요해 자동검증 불가 시 **사용자 확인 요청**.
- **모델/마이그레이션** → `migrate` 적용 + 관련 뷰/admin 화면 정상 로드. (migrate 하면 `db.sqlite3` 가 수정 상태가 됨에 유의)
- **백엔드 로직** → 단위 테스트 또는 인터프리터 호출로 입출력 직접 검증.
- **하위 경로/Nginx 배포 변경** → `QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= GATEWAY_ENTRY_ENABLED= TRUSTED_PROXY_CIDRS= QGP_BASE_URL= .venv/bin/python manage.py test config.test_subpath apps.accounts apps.api_v2 apps.suites apps.visualization`. 로컬 배포값·외부 연동키를 비운 테스트 환경에서 루트/접두 경로 회귀를 확인한다. 실제 `./run.sh` + 프록시에서 로그인·회원가입·CSS/JS·`health/` HTTP 응답과 데스크톱/모바일 화면을 확인한다 (정적 URL 초기 캐시·권한·API 링크·스키마도 검증).
- **SSO(Keycloak)/로그인·로그아웃 변경** → `QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= GATEWAY_ENTRY_ENABLED= TRUSTED_PROXY_CIDRS= QGP_BASE_URL= .venv/bin/python manage.py test apps.accounts config.test_subpath` (로그아웃 (a')진입모드·(a)게이트웨이·(b)Keycloak end-session·(c)로그인 화면, id_token 비노출, 메타데이터 실패 폴백, 프록시 경계, 중복 연결 방지, 진입 모드 `GatewayEntryTests`/`GatewayEntrySettingsTests`·로그아웃 HTML `GatewayLogoutPageTests` 포함; 2026-10-09 기준 184개 OK). **검증 항목(진입 모드/로그아웃)**: ① 로그아웃 응답이 302 가 아니라 `Location` 헤더 없는 HTML(meta refresh+JS+링크, URL 이스케이프)인지 ② `GATEWAY_ENTRY_ENABLED` 에서 로그인 화면에 "Keycloak 로그인" 버튼이 있고 `LOCAL_LOGIN_ACTIVE` 가 꺼지는지(`LOCAL_LOGIN_ENABLED=True` 면 복구) ③ `/accounts/gateway-login/` 이 인증됨→`next` 리다이렉트, 헤더 없음→503 `sso_unavailable`(루프 없음), 거부→403 인지 ④ 플래그 꺼짐이면 기존 동작·버튼 없음. 실제 게이트웨이 왕복(버튼→Keycloak→대시보드, 로그아웃→로그인 화면, 로그아웃 후 재클릭 시 재인증 요구)은 게이트웨이 라우팅이 필요해 자동 검증이 안 되므로 **사용자 브라우저 확인을 요청**한다. 실제 Keycloak 왕복(로그인·로그아웃 복귀·재클릭 시 비밀번호 요구)은 운영 Keycloak client 가 필요해 자동 검증이 어려우므로 **사용자에게 시크릿 창 확인을 요청**한다. 로그인 화면 디자인은 아래 헤드리스 스냅샷(비로그인 화면이라 `shot.py` 의 로그인 세션 없이 `/accounts/login/`)으로 데스크톱·모바일(390px)·다크를 확인한다.
- **알림/담당자 매칭** → CLAUDE.md 의 **회귀 6 시나리오** + `.venv/bin/python manage.py test apps.integrations apps.notifications`.
- **상태 전이 로직** → `apps.qualitygates` 테스트 + `maintenance.md` §2 다이어그램 갱신 확인.
- **트리거 throttle / 지연 상태 전이** → `.venv/bin/python manage.py test apps.suites apps.robustness` (ClaimTriggerSlotTests / TriggerDelayGateTests / ParallelTriggerDelayTests 포함).
- **내부 Jenkins GWT 트리거 변경** (`trigger_webhook` / `trigger_job` / `gwt_token` 관련) → 익명 read 동작 확인(`/api/json` 응답 200), GWT 토큰 발급/재발급 뷰 접근 확인(`suites:gwt_tokens_generate`, `suites:gwt_token_regen`), `trigger_job` 에 `gwt_token=""` 넘겼을 때 `JenkinsError(status=400)` 발생 확인. 외부 suite 에서 `trigger_build` 경로 유지 확인.
  - **워커 잡 GWT 설정 정합 점검/수정** (anonymous Job/Configure 부여돼 있을 때): config.xml 읽기는 익명 `GET .../config.xml` 200. 쓰기는 `/crumbIssuer/api/json` 으로 crumb 받고(쿠키 동일 세션) `POST .../config.xml` 에 **`Content-Type: text/xml; charset=utf-8`** (charset 없으면 500). 각 워커 GenericTrigger `<token>` == 포털 DB `gwt_token` 이어야 하고 `<tokenCredentialId>` 는 없어야 함(있으면 그 크레덴셜 값이 평문 토큰을 덮어써 "Did not find any jobs"). invoke 성공 = `/queue/api/json` 에 해당 워커만 뜨거나 `lastBuild` 번호 증가. **점검 끝나면 anonymous Job/Configure 회수.** (상세: maintenance.md §9·§11)
- **서버 이관/스냅샷(`apps/syncops`) 변경** → `QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= GATEWAY_ENTRY_ENABLED= TRUSTED_PROXY_CIDRS= QGP_BASE_URL= .venv/bin/python manage.py test apps.syncops` (임시 가짜 DB 만 사용, ssh/rsync mock). 운영 DB 대상은 `export_snapshot --dry-run`(읽기 전용)만 — 실제 export/import 리허설·`migrate` 금지. 임시 디렉터리 가짜 DB 로 export→import→verify 왕복과 `sync_from_source --dry-run` 확인. 실제 ssh/rsync·IDC↔AWS 연결은 자동 검증 불가 → **사용자 확인 요청**.
- **Export API(`/api/v2/export/…`) / `fetch_snapshot_via_api` 변경** → `QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= GATEWAY_ENTRY_ENABLED= TRUSTED_PROXY_CIDRS= QGP_BASE_URL= .venv/bin/python manage.py test apps.api_v2.tests_export apps.syncops.tests_fetch_api apps.syncops.tests_api_roundtrip` (2026-10-09 기준 31+34+19=84개 OK, zstd 바이너리 없으면 1건 skip). 임시 DB·임시 `SYNCOPS_EXPORT_DIR`·LiveServer 가짜 서버만 사용 — **운영 `db.sqlite3` 를 읽거나 쓰지 않고, 원본 서버(`qualitygate.jkintra.net`)에 요청을 보내지 않는다**(실제 키도 없음). 확인 항목: ① `export:read` 없는 키·`*` 만 가진 키·비admin 소유 키 → 403 ② `include_config` 등 알 수 없는 필드 → 400 `unsupported`, 번들에 config 없음 ③ 동시 2번째 생성 → 409 ④ Range 206/416·`X-Content-SHA256`·허용 외 파일명/경로 조작 404 ⑤ 24시간 만료·stale `running` → `failed` ⑥ export→fetch→`import_snapshot`→`verify_snapshot` 왕복 ⑦ `.part` 이어받기·sha256 불일치 거부·교차 호스트 리다이렉트 거부 ⑧ 키 평문이 응답/로그/출력에 안 남음 ⑨ `QGP_SCRIPT_NAME= .venv/bin/python manage.py makemigrations --check --dry-run` 변경 없음(새 모델·마이그레이션 금지 — 단 이 명령은 기본 DB 를 열어 보므로 운영 DB 보호가 필요하면 `MigrationSafetyTests`(임시 DB)로 대체). 원본(ALB) 경유 실제 다운로드·배포 후 동작은 자동 검증 불가 → **사용자 확인 요청**.
- **`/ci-api/`(게이트웨이 로그인 예외 경로) / `ApiKeyAuthentication`(Bearer·Basic) / `CiApiBoundaryMiddleware` 변경** → `QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= GATEWAY_ENTRY_ENABLED= TRUSTED_PROXY_CIDRS= QGP_BASE_URL= .venv/bin/python manage.py test apps.api_v2.tests.test_ci_api` (13개 — 키 인증·Basic(`qgp_` 키만)·세션 쿠키 무시·`X-Auth-*` 무시·`reverse` 가 `/ci-api/` 접두·export/docs 404). 새 엔드포인트가 `/ci-api/` 에 노출되는지·export/docs 가 안 노출되는지 확인하고, `/api/v2/` 회귀로 `apps.api_v2 apps.accounts` 도 함께 돌린다. **배포 후 확인(게이트웨이 예외 적용 여부)**: `curl -i https://qw.jkintra.net/qg/ci-api/health/` → 200 JSON, `curl -i https://qw.jkintra.net/qg/ci-api/me/`(키 없이) → **JSON** 401(`error.code=not_authenticated`), `curl -i -u x:$KEY https://qw.jkintra.net/qg/ci-api/me/` → 200. nginx **HTML** 401 이면 게이트웨이 예외 미적용, `/qg/ci-api/docs/`·`/qg/ci-api/export/…` 는 404 가 정상. ci-api 는 게이트웨이 인증이 없으므로 API Key 인증·scope·Rate limit 이 유일한 방어선 — 약화하지 말 것.
- **API 문서(Swagger/ReDoc) 표기 / `apps/api_v2/schema.py`(`rewrite_paths_to_ci_api`) / `SPECTACULAR_SETTINGS` / `settings.CI_API_BASE` / `swagger_ui.html` 키 검증 배지 / API Key 발급·목록 화면 호출 예시 변경** → `QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= GATEWAY_ENTRY_ENABLED= TRUSTED_PROXY_CIDRS= QGP_BASE_URL= .venv/bin/python manage.py test apps.api_v2.tests.test_docs apps.api_v2.tests.test_ci_api` (`test_docs` 의 `SchemaCiApiNotationTests` 5개 — `paths` 에 `/api/v2/` 가 남지 않음·소개문이 `/ci-api/`/Bearer/Basic 설명·export/docs 스키마 미포함·`/api/v2/health/` 실제 라우팅 불변·Swagger 페이지가 키를 `/ci-api/` 로 검증). **배포 후 확인**: 로그인한 브라우저로 `https://qw.jkintra.net/qg/api/v2/docs/` 를 열어 엔드포인트 경로가 모두 **`/qg/ci-api/...`** 로 표시되는지, **Authorize** 에 유효 키(평문은 화면에만, 문서·로그에 남기지 말 것)를 넣으면 우상단 배지가 ✅(`/ci-api/me/` 검증), 무효 키면 ❌ 인지 확인한다. API Key 발급 완료/목록 화면의 curl 예시가 `QGP_BASE_URL` + `/ci-api/`(이 호스트 `https://qw.jkintra.net/qg/ci-api/`) 로 나오고 옛 도메인이 없는지도 본다.
- **API Key 관련** → 평문이 로그/응답/테스트/`print` 어디에도 안 남는지 확인.
- **정적 파일(`static/css/style.css` · `static/js/qg_notify.js`) 변경** → `templates/base.html` 의 `?v=YYYYMMDDx` **캐시버스트 토큰을 반드시 bump**. 안 올리면 사용자 브라우저가 옛 CSS/JS 를 캐시한 채라 변경(예: 버튼 비활성 스타일)이 화면에 반영 안 됨. (`grep -n "style.css\|qg_notify" templates/base.html` 로 위치 확인 — style.css 와 qg_notify.js 는 각각 별도 토큰). **`templates/accounts/login.html` 은 base.html 을 상속하지 않고 `style.css` 를 직접 로드하므로 그 `<link>` 의 `?v=` 토큰도 같은 값으로 함께 bump**. **`static/js/qg_highway.js` 는 별도 버스터가 `templates/visualization/index.html` 에 있음** — 이 파일 수정 시 그쪽 `?v=` 도 bump. (주의: `sed 's/qg_highway.js?v=.../'` 는 정규식 `?` 때문에 매칭 실패 — 정확한 문자열로 교체할 것.)
- **반응형/모바일 레이아웃 변경** → 헤드리스 chromium 으로 모바일 폭 스냅샷 검증. `/home/taeshkim/qg_shot/shot.py <name> <url-path> [--open] [--dark] [--w 390]` — Django 테스트 Client(세션=signed_cookies 라 `db.sqlite3` 미기록) 로 HTML 렌더 → `/static` 을 file:// 로 치환 → chromium(`--user-data-dir` 매 실행 새로) 캡처. `--open` 은 드로어 열린 상태. **주의**: `.page-header h1`(그라데이션) 긴 타이틀이 글자단위로 쪼개져 보이는 건 헤드리스 래스터화 버그이며 실제 Chrome 에선 정상(레이아웃 자체는 정상).
- **Canvas 애니메이션(Visualization/Quality Highway 등) 검증** → 정적 스냅샷은 fetch 불가 + `--virtual-time-budget` 으로는 rAF 가 몇 프레임만 돌아 부적합. **CDP 실시간 캡처** 사용: `chromium --headless=new --no-sandbox --remote-debugging-port=9222 --window-size=W,H <file-or-url>` 백그라운드 실행 후, Node `--experimental-websocket` 로 `http://127.0.0.1:9222/json` 의 page WS 에 붙어 수 초 대기 → `Page.captureScreenshot`. 참고 스크립트: `/home/taeshkim/qg_shot/cdp_shot.js`. 데이터 없이 차량을 보려면 페이지/하니스가 `QGHighway.init({seed:[...]})` 로 시드 주입(첫 fetch 생략).

---

## 5. 병렬 에이전트

독립 작업은 **기본적으로 병렬 서브에이전트**로 처리한다. 에이전트 목록·디스패치 패턴·예외(상태기계 등 단독 처리)는 [`AGENTS.md`](./AGENTS.md) §1~§2 참조.

---

## 6. 이 문서의 갱신 규칙

- 새 manage.py 명령·curl 점검 패턴·환경 변화(포트/파이썬/설정)·쓰게 된 스킬이 생기면 이 문서를 갱신한다.
- 매 작업 턴 마무리(문서 동기화) 단계에서 `docs-keeper` 가 점검.
