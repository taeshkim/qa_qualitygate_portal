# PRD / AI 런북 — 게이트웨이 서버(IDC) 이관 + 전체 데이터 싱크

> **(2026-10 서버 이관 완료 — 이력 문서)** 이 문서의 옛 주소 `https://qualitygate.jkintra.net`(AWS 원본·ALB)은 이관 완료로 **종료**됐다(502). 현재 접속은 게이트웨이 `https://qw.jkintra.net/qg/`, 프로그램 호출(API Key)은 `https://qw.jkintra.net/qg/ci-api/` 이다. 아래 서술은 작성 당시(날짜 맥락) 기준이다.

작성: 2026-10-09 · 상태: **구현·검증 완료 (apps/syncops), 3관점 리뷰 지적 반영 중(C1~C10)** · 관련: `docs/api_requirements_data_export.md`·`docs/api_guide.md` §4.5(HTTPS Export API, 경로 C), `docs/handoff_sso_integration.md`(SSO 이어하기), `docs/qw-sso-gateway.md`, `docs/service-sso-migration.md`

> 이 문서는 사람이 읽는 PRD 이면서 **AI 에이전트가 그대로 따라 실행하는 런북**이다. 명령의 `<대문자>` 는 사용자에게 받은 값(§7.1)으로 치환한다.

## 1. 한 줄 요약

저장소를 새 서버에 `git clone` 한 뒤, `apps/syncops` 의 **스냅샷 번들(export → 전송 → import → verify)** 로 현재 서버의 **전체 데이터(DB 53테이블(+`sqlite_sequence`) 5.1GB + 비밀/설정 + cron/systemd + 마이그레이션 상태)** 를 옮긴다.

### AI 에게 이렇게 말하세요

새 서버에서 저장소를 clone 한 디렉터리에서 AI(Claude Code 등)를 열고:

```text
docs/prd_server_migration_sync.md 보고 싱크해줘
```

- AI 는 §7.1 의 변수를 먼저 물어보고, §6 절차를 순서대로 실행한다. 리허설만 원하면 "리허설만 해줘", 최종 이관이면 "최종 컷오버 싱크로 해줘" 라고 덧붙인다.
- **패스프레이즈가 필요한 단계(config 암호화 전달·복원)와 원본 서버 정지·보관은 AI 가 하지 않는다.** 사용자가 자기 터미널에서 직접 실행하고 AI 에게는 결과(종료코드·요약)만 알려 준다(§7.5, §6.3).
- SSO 연동 작업은 이관 후 별도로: `docs/handoff_sso_integration.md 보고 작업 이어서 해줘`.

## 2. 배경 · 목표 · 비목표

**배경**
- 이 포털(QA Quality Gate Portal, Django 4.2 + SQLite)은 현재 한 호스트에서 **실 DB·실 cron 으로 운영 중**이다. 게이트웨이 서버(IDC)로 통째로 이관해야 한다.
- 데이터의 대부분은 Job 콘솔 로그(`suites_test_suite_job_run`)라 DB 가 5.1GB 다. 파일 하나 `scp` 로는 무결성/재개/롤백 보장이 약해서 검증 가능한 번들 형식과 명령이 필요하다.
- 코드는 git(`origin/master`)으로, **데이터·비밀·운영 설정은 스냅샷 번들**로 옮긴다.

**목표**
1. 새 서버에서 한 번의 명령(`sync_from_source`) 또는 3단계 오프라인 경로(`export_snapshot` → 전송 → `import_snapshot`)로 전체 데이터를 복제.
2. 모든 단계에 검증(sha256, `PRAGMA quick_check`, 테이블별 행 수, 마이그레이션 목록)이 들어 있고 실패 시 **기존 DB 는 그대로** 남는다.
3. 사용자가 "문서 보고 싱크해줘" 한마디만 해도 AI 가 이 문서대로 완료한다.
4. 컷오버 중 원본은 삭제하지 않고 읽기 전용으로 보관 → 언제든 롤백 가능.

**비목표**
- 무중단 이관, 지속적 양방향 복제, 증분 싱크. (컷오버는 짧은 점검 창을 둔 1회성 전체 복제)
- SQLite → PostgreSQL 전환. 이관 후에도 `config.settings.dev` 의 SQLite 를 그대로 쓴다.
- 이 서버의 다른 서비스(TAPilot `:8602`, partnership `:8604`)의 이관.
- 게이트웨이(nginx/oauth2-proxy) 구성 자체. (`docs/qw-sso-gateway.md` 소관 — §11 에서 연결만)

## 3. 상태 인벤토리 (2026-10-09, 이 호스트에서 읽기 전용 확인)

### 3.1 데이터베이스

| 항목 | 값 |
|---|---|
| 파일 | `<BASE_DIR>/db.sqlite3` (`DJANGO_SETTINGS_MODULE=config.settings.dev` → `BASE_DIR / "db.sqlite3"`) |
| 크기 | 5,143,064,576 바이트(약 4.8GiB) · page_size 4096 · 약 1,255,631 페이지 |
| journal_mode | `delete` (WAL 아님 → `-wal`/`-shm` 없음, 쓰기 중에만 `-journal` 존재) |
| 테이블 | 53개 + 내부 `sqlite_sequence` = `sqlite_master` 54개 (`export_snapshot --dry-run`·manifest `tables` 는 54개로 센다) · `django_migrations` 112행 |
| 미적용 마이그레이션 | `accounts.0003_externalidentity` (DB 에는 accounts 0001·0002 까지만 있음 → 이관 후 migrate 로 적용) |

상위 크기 테이블 (dbstat 기준, 행 수는 조회 시점 값이며 cron 이 계속 늘린다):

| 테이블 | 행 수 | 크기 |
|---|---:|---:|
| `suites_test_suite_job_run` (Job 콘솔 로그) | 53,214 | 약 4,910MB (전체의 약 95%) |
| `suites_tap_run` | 3,382 | 약 179MB |
| `qualitygates_run` | 147 | 약 10MB |
| `activity_log` | 39,029 | 약 7.5MB |
| `suites_test_suite_run` | 29,918 | 약 5.1MB |
| `robustness_run` | 25,365 | 약 4.5MB |
| `django_session` | 406 | 0.2MB |
| `integrations_api_endpoint_owner` | 333 | 0.1MB |
| `qualitygates_run_test` | 488 | 0.1MB |
| `accounts_user` | 22 | 0.0MB |
| `suites_test_suite_job` / `suites_test_suite` | 74 / 18 | 0.0MB |

### 3.2 설정 / 비밀 파일 (파일명·키 이름만. 값은 이 문서에 없다)

| 파일 | 설명 |
|---|---|
| `<BASE_DIR>/.env` | 환경변수. 현재 설정된 키: `DJANGO_SECRET_KEY` `DJANGO_DEBUG` `DJANGO_ALLOWED_HOSTS` `DJANGO_SETTINGS_MODULE` `DJANGO_CSRF_TRUSTED_ORIGINS` `DB_*` `REDIS_URL` `CELERY_*` `JENKINS_DEFAULT_URL/USER/API_TOKEN` `JENKINS_WEBHOOK_SECRET` `SLACK_BOT_TOKEN` `SLACK_SIGNING_SECRET` `SLACK_APP_TOKEN` `SLACK_QG_ALERTS_CHANNEL` `SLACK_QPE_OPS_CHANNEL` `SLACK_TRIAGE_BUTTONS` `QGP_BASE_URL` `GOOGLE_CREDENTIALS_FILE` `GOOGLE_SA_JSON_B64` `GSHEET_OWNERS_SPREADSHEET_ID` `GSHEET_OWNERS_WRITEBACK` `TAPILOT_API_BASE` `TAPILOT_API_KEY` |
| `<BASE_DIR>/secrets/qg-owners-wif.json` | Google 담당자 시트용 자격증명 (`GOOGLE_CREDENTIALS_FILE` 이 **절대경로**로 가리킴 → 새 경로면 수정 필요) |
| `<BASE_DIR>/secrets/README.txt` | 표시용 (git 비추적 `secrets/` 안) |
| `<BASE_DIR>/deploy/qualitygate.service`, `deploy/qg-slack-triage.service` | systemd 유닛 템플릿 (git 추적) |
| `/etc/systemd/system/qualitygate.service` | **실제 설치본**. `deploy/` 템플릿과 내용이 다르다(설치본은 `ExecStart=/bin/bash <BASE_DIR>/run.sh`, `deploy/` 는 `runserver --noreload` 직접 실행이며 경로가 `/home/taeshkim/qa-qg`). 현재 상태: `inactive`(서버는 별도 수동 기동 프로세스가 `:8601` 점유) |
| `/etc/systemd/system/qg-slack-triage.service` | 설치본, 현재 `active` (Socket Mode 리스너) |

### 3.3 cron (사용자 crontab 3줄, 모두 매분)

```cron
* * * * * cd <BASE_DIR> && <BASE_DIR>/.venv/bin/python manage.py sync_api_owners_from_sheet --if-due >> /tmp/qg_owner_sync.log 2>&1
* * * * * cd <BASE_DIR> && flock -n /tmp/qg_reliability_sweep.lock <BASE_DIR>/.venv/bin/python manage.py sweep_reliability >> /tmp/qg_reliability_sweep.log 2>&1
* * * * * cd <BASE_DIR> && flock -n /tmp/qg_jenkins_keepalive.lock <BASE_DIR>/.venv/bin/python manage.py jenkins_keepalive >> /tmp/qg_jenkins_keepalive.log 2>&1
```

(`<BASE_DIR>` = 현재 `/home/taeshkim/qa_qualitygate_portal`. 앞에 설명 주석 2줄이 있다.)

### 3.4 systemd 유닛 2종

| 유닛 | 역할 | 비고 |
|---|---|---|
| `qualitygate.service` | 웹(`runserver 0.0.0.0:8601`) | `User=taeshkim`, `UMask=0027` (`deploy/` 템플릿 기준) |
| `qg-slack-triage.service` | `manage.py slack_triage_listener` (Slack Socket Mode, 실패 알림 Triage 버튼) | 인바운드 URL 불필요(아웃바운드 WS). **동시에 두 곳에서 켜면 이벤트가 중복/분산 처리되므로 한 곳만** |

### 3.5 기타 확인 사항

- Python 3.12.3 / Django 4.2.29 / `.venv` 사용. 이 호스트는 `nproc`=4 vCPU·RAM 약 7GB(다른 서비스와 공유)·디스크 여유 약 49GB (`/dev/root` 96GB 중).
- 원격: `origin = https://git.jobkorea.co.kr/qa-management/qa_qualitygate_portal.git`, 브랜치 `master`.
- **`db.sqlite3` 가 git 에 추적되고 있다** (HEAD 의 blob 약 514MB, 과거 시점 스냅샷). 운영 DB 는 이보다 훨씬 최신이다. → §6.1·§12 참조.
- `apps/syncops/`, `docs/*.md`(신규), `apps/accounts/middleware.py`·`config/settings/base.py` 변경분이 **아직 커밋 전**일 수 있다. 새 서버가 clone 하려면 먼저 커밋·push 되어 있어야 한다(커밋은 메인 작업자 몫). 커밋 전에 `git status` 로 `*.incoming`·`*.bak-*`·`snapshots/`·`restored-system/` 같은 이관 산출물이 섞이지 않았는지 확인하고, `git add -A`/`git commit -a` 는 쓰지 않는다(`db.sqlite3` 가 추적 파일이라 5GB 가 커밋될 수 있음 — §12).

## 4. 아키텍처 · 번들 형식

```text
[원본 서버]                                      [새 서버]
 export_snapshot ──(번들 디렉터리)──→ 전송 ──→ import_snapshot ──→ verify_snapshot
   sqlite3 backup API                rsync/scp/    sha256·quick_check·행 수
   + zstd, config.tar(.enc)          S3/USB        하드링크 보관 + 원자적 교체(os.replace)

 sync_from_source (새 서버에서 실행) = ssh로 원격 export → rsync 수신 → manifest·sha256 검증 → 원격 번들 삭제 → import → verify
 fetch_snapshot_via_api (새 서버에서 실행, 경로 C) = HTTPS Export API 로 원격 export 요청 → 폴링 → Range 이어받기·sha256 검증 → 번들 배치 → 원격 DELETE (config 는 제외 — 별도 전달)
```

번들 = 디렉터리 `snapshots/qg-<UTC yyyymmddTHHMMSSZ>/` (디렉터리 0700, 파일 0600):

| 파일 | 내용 |
|---|---|
| `manifest.json` | `schema_version=1`, `created_at`(UTC), `source{hostname, base_dir, git{commit, branch, dirty_files}, python, django}`, `db{engine, original_size, source_path(원본 DB 절대경로 — 같은 호스트 가드용, 없으면 hostname+base_dir 만 비교), sha256(압축 전 백업본), file, file_sha256, compression, page_size, journal_mode}`, `tables{테이블명: 행 수}`, `migrations[[app,name],...]`, `config{file, encrypted, sha256, entries[파일명만]}` 또는 `null`, `notes` |
| `db.sqlite3.zst` (또는 `.gz` / 무압축 `db.sqlite3`) | sqlite3 backup API 로 뜬 일관된 스냅샷을 압축한 것 |
| `config.tar` 또는 `config.tar.enc` | `.env`, `secrets/*`, `deploy/*`, `crontab.txt`(= `crontab -l` 출력), `/etc/systemd/system/qualitygate*.service`·`qg-*.service`(읽을 수 있을 때) |

설계 원칙: 운영 DB 는 읽기만 한다 / 번들은 sha256 으로 검증 / import 는 임시 파일(`<target>.importing`)에서 모든 검증을 마친 뒤 기존 DB 를 `os.link` 하드링크로 `<target>.pre-import-<ts>` 에 보존하고 `os.replace` 로 원자적 교체(대상 경로가 비는 구간 없음) / 비밀은 로그·manifest 에 값 없이 파일명만. **한계(§8·§12)**: DB 는 암호화·서명되지 않는다 — 번들 무결성은 sha256, 출처 인증은 없다.

## 5. 명령 레퍼런스

모든 명령은 저장소 루트에서 `.venv/bin/python manage.py <명령>`. (이하 `PY` = `.venv/bin/python`)

### 5.1 `export_snapshot` (원본 서버)

```text
export_snapshot [--out DIR] [--db-path PATH] [--no-config] [--passphrase-env VAR]
                [--compress auto|zstd|gzip|none] [--max-backup-seconds N] [--dry-run]
```

| 옵션 | 설명 |
|---|---|
| `--out DIR` | 출력 번들 디렉터리. 기본 `<BASE_DIR>/snapshots/qg-<UTC ts>/` |
| `--db-path PATH` | 원본 DB. 기본은 settings `DATABASES["default"]["NAME"]` |
| `--no-config` | `.env`/secrets/deploy/crontab/systemd 제외. (`--passphrase-env` 와 함께 쓸 수 없음) |
| `--passphrase-env VAR` | 환경변수 `VAR` 값을 패스프레이즈로 `openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:VAR` 암호화(`config.tar.enc`). 값은 명령행 인자로 넘기지 않음 |
| `--compress` | 기본 `auto`: zstd CLI 있으면 `zstd -T0 -3`, 없으면 pigz/gzip |
| `--max-backup-seconds N` | 기본 1800. 쓰기가 끼어들어 백업이 재시작돼 이 시간 안에 못 끝나면 "서비스·cron 을 멈추고 다시 실행" 안내와 함께 실패 |
| `--dry-run` | 아무것도 쓰지 않음. DB 를 `mode=ro` 로 열어 DB 크기·테이블별 행 수 상위·config 항목명·예상 번들 크기·디스크 여유를 출력. **운영에서 안전** |

동작: 디스크 여유 선점검(원본 크기 + 압축본) → backup API 청크 복사(`pages=1024`, 짧은 sleep, 진행률 stderr) → 임시본 `PRAGMA quick_check` → sha256 → 압축 → 임시본 삭제. 암호화 없이 config 를 포함하면 0600 + 경고. 끝에 번들 경로와 "다음 단계" 안내.
- 종료 메시지와 `--dry-run` 출력에 **"DB 는 암호화되지 않습니다(세션·해시 포함) — 외부 매체로 옮길 때 별도 암호화"** 경고가 나온다(`--passphrase-env` 는 config 만 암호화). HMAC 서명·`--deep`·`--encrypt-db` 는 구현하지 않았다(§8·§12).
- export 가 SIGHUP/SIGTERM 을 받으면(ssh 단절·`kill`) 임시 파일(`.partial` 등)·출력 디렉터리를 정리하고 종료한다(기존 실패 정리 경로 재사용, 핸들러는 명령 종료 시 원복). 원본 DB 는 읽기만 하므로 변하지 않는다.
- `--compress auto` 의 `zstd -T0` 는 코어 수만큼 병렬이다(이 호스트 `nproc`=4). 실제 소요 시간은 측정 필요(§9).

### 5.2 `import_snapshot` (새 서버)

```text
import_snapshot SNAPSHOT_DIR [--target-db PATH] [--restore-config] [--config-dir PATH]
                [--passphrase-env VAR] [--force] [--allow-older-code] [--allow-same-host]
                [--ignore-running] [--service-port 8601] [--skip-migrate] [--yes]
```

| 옵션 | 설명 |
|---|---|
| `SNAPSHOT_DIR` | `manifest.json` 이 있는 번들 디렉터리 |
| `--target-db PATH` | 복원 대상 DB. 기본 settings 기본 DB. 기본 DB 가 아니면 migrate 자동 생략(이유 출력) — 리허설용 |
| `--restore-config` | config 번들을 `--config-dir`(기본 `BASE_DIR`)에 푼다. 기존 파일은 덮어쓰지 않고 `<name>.incoming` 으로 저장. `--force` 면 `.bak-<ts>` 보관 후 교체. `.env` 0600, `secrets/` 0700·파일 0600. `crontab.txt`·systemd 유닛은 `<config-dir>/restored-system/` 에 **풀기만 하고 설치하지 않으며** 설치 명령을 출력. `../`·절대경로·심볼릭 링크 tar 항목 거부 |
| `--config-dir PATH` | config 풀 위치 |
| `--passphrase-env VAR` | 암호화된 config 복호화용 환경변수 이름 |
| `--force` | **의도적 덮어쓰기 전용**: ① 대상 DB 에 이미 데이터가 있어도 진행(기존 DB 는 `pre-import-<ts>` 로 보존) ② `--restore-config` 시 기존 config 를 `.bak-<ts>` 로 보관 후 교체 ③ 대상 DB 를 읽을 수 없을 때(손상 의심) 경고 후 pre-import 보관·교체. **"코드가 데이터보다 오래됨" 거부는 `--force` 로 풀리지 않는다** |
| `--allow-older-code` | 번들 마이그레이션 중 현재 코드에 없는 것이 있어도(코드가 데이터보다 오래됨) 진행. **쓰지 말 것** — 새 서버 코드를 `git pull` 로 맞추는 것이 정답(§6.1, §7.3) |
| `--allow-same-host` | "원본 서버에 원본 번들을 import 하려 합니다" 가드를 해제. 같은 호스트 왕복 테스트 전용이며 **운영에서 쓰지 말 것**(원본 DB 덮어쓰기 위험) |
| `--ignore-running` | `127.0.0.1:<service-port>` 에 서비스가 떠 있어도 진행(비권장) |
| `--service-port` | 기본 8601. 이 포트에 연결되면 서비스 가동 중으로 간주해 거부 |
| `--skip-migrate` | 복원 후 migrate 생략 |
| `--yes` | 확인 프롬프트 생략 |

선행 검증(하나라도 실패하면 아무것도 바꾸지 않고 종료코드 ≠ 0): manifest `schema_version` 지원 · 번들 파일 sha256 일치 · 서비스 미가동 · **같은 호스트 가드**(`manifest.source.hostname == socket.gethostname()` 이고 `source.base_dir == settings.BASE_DIR` 이거나 대상 DB realpath 가 `manifest.db.source_path` 와 같으면 `--allow-same-host` 없이는 거부) · 번들의 migrations 중 **현재 코드에 없는 (app,name)** 없음(있으면 코드가 데이터보다 오래됨 → `git pull` 먼저, `--force` 로 안 풀림, `--allow-older-code` 는 쓰지 않는다) · 대상 DB 에 사용자 테이블 데이터 없음(단 `migrate` 가 채우는 `django_migrations`·`django_content_type`·`auth_permission`·`sqlite_sequence`·`django_site` 는 데이터로 치지 않으므로, migrate 만 끝난 새 서버는 `--force` 없이 import 가능) · **대상 DB 를 읽다 sqlite3 오류**가 나면 `--force` 없이는 "대상 DB 를 읽을 수 없음 — 손상 의심, --force 로 pre-import 보관 후 교체" 로 거부(`--force` 면 경고 후 진행) · `config.tar.enc` 가 있으면 **첫 블록만 복호화해 tar 헤더 유효성으로 패스프레이즈를 DB 교체 전에 검증**(틀리면 DB 는 건드리지 않고 거부) · 디스크 여유. 실행 중 `OSError`(디스크 부족 ENOSPC 포함)·`sqlite3.Error` 는 트레이스백 없이 `CommandError` 로 보고되고 기존 DB 는 불변, `.importing` 은 정리된다.
복원: `<target>.importing` 으로 스트리밍 압축 해제 → sha256 == `manifest.db.sha256` → `PRAGMA quick_check` → 모든 테이블 행 수 == `manifest.tables` → 기존 대상 DB 를 `os.link(target, <target>.pre-import-<ts>)` **하드링크로 보존**(삭제·개명 안 함) → `os.replace(<target>.importing, target)` 만 수행(대상 경로가 비는 구간 없음). 하드링크가 안 되는 파일시스템이면 `rename` 으로 폴백하고, "대상 없음 + `pre-import-<ts>` 존재 = `pre-import-<ts>` 를 원래 이름으로 `mv`" 복구 안내를 출력한다. 대상의 `-journal/-wal/-shm` 는 보존본 옆으로 보관 후 대상에서는 제거한다. 실패 시 `.importing` 정리, 기존 DB 유지. 이어서 기본적으로 `migrate` 실행 후 요약표 출력.
`--restore-config` 의 시스템 설치 안내에는 "crontab.txt 는 `#QGMIGRATE#` 접두(주석)·원본 경로가 들어 있을 수 있어 경로 치환 후 설치, 유닛은 `run.sh` 가 아닌 새 서버 방식 — §6.3 참조" 한 줄이 붙는다.

### 5.3 `verify_snapshot`

```text
verify_snapshot MANIFEST_OR_DIR [--target-db PATH] [--allow-newer]
```

대상 DB(`mode=ro`)의 테이블별 행 수를 manifest 와 비교해 표로 출력, 불일치 시 종료코드 1. `--allow-newer` 는 대상이 더 많은 행(이관 후 신규 데이터, migrate 로 늘어난 `django_migrations`·`django_content_type`·`auth_permission` 등)을 허용. 대상이 **더 적은** 행·대상에 **없는 테이블**(`MISSING`)·manifest 의 마이그레이션이 대상에 **미적용**이면 `--allow-newer` 여도 실패(종료코드 1). manifest 에 없는 대상 테이블(`extra(매니페스트에 없음)`)과 "대상에만 있는 마이그레이션"(이관 후 migrate 로 적용된 것, 예 `accounts.0003`)은 보고만 하고 실패시키지 않는다.
출력 말미에 **"행 수·마이그레이션만 비교합니다. 내용 무결성은 import 단계의 sha256 이 보증"** 안내가 붙는다 — verify 는 내용 비교(`--deep`)를 하지 않는다(§12).

### 5.4 `sync_from_source` (새 서버에서 실행하는 pull 오케스트레이터)

```text
sync_from_source --source USER@HOST --source-dir REMOTE_PATH
                 [--source-python REMOTE_PATH/.venv/bin/python] [--ssh-opts "..."]
                 [--local-dir DIR] [--include-config] [--passphrase-env VAR]
                 [--max-backup-seconds N] [--compress auto|zstd|gzip|none]
                 [--keep-remote] [--remote-bundle REMOTE_BUNDLE] [--dry-run]
                 [--target-db PATH] [--restore-config] [--force] [--yes]
                 [--allow-older-code] [--allow-same-host] [--ignore-running] [--skip-migrate] [--service-port 8601]
```

`--force`·`--allow-older-code`·`--allow-same-host` 는 ④ `import_snapshot` 에 그대로 통과된다(의미는 §5.2; `--allow-older-code` 는 쓰지 않는다). `--source` 는 `USER@HOST` 형식만 허용하고 `-` 로 시작하거나 형식이 다르면 `CommandError`, ssh/rsync 호출에는 `--` 구분자를 쓴다.

흐름: ① ssh(`BatchMode=yes`, 비밀번호 입력 없음)로 원격 `cd REMOTE_PATH && PYTHON manage.py export_snapshot --out <원격 임시 디렉터리> [--no-config | 패스프레이즈 전달] [--max-backup-seconds N] [--compress …]` ② `rsync -a --partial --info=progress2` 로 `--local-dir`(기본 `snapshots/`)에 수신(재개 가능) ③ manifest·sha256 검증 **직후 원격 임시 번들 삭제**(`--keep-remote` 면 유지) ④ `import_snapshot` ⑤ `verify_snapshot`. ③ 이후 단계가 실패하면 원격 번들은 이미 없으므로 **로컬 번들로 재개**한다(`import_snapshot <로컬번들> …`, 안내문에 명령이 출력됨).
- `--include-config` 를 줘야 config 가 번들에 포함된다(없으면 `--no-config`). 패스프레이즈 값은 명령행·로그에 절대 노출하지 않고 ssh stdin 으로만 전달(원격이 `read` 로 환경변수 구성). **패스프레이즈 환경변수는 사용자가 자기 터미널에서 `read -rs` 로 만들어 직접 실행한다(§7.5).**
- `--dry-run`: 실행할 명령만 출력(값은 마스킹).
- **실패 메시지(모든 단계)에는 원격 번들 경로(`remote_out`)와 "원격에 번들이 남아 있음: ssh … rm -rf -- <경로>" 가 항상 포함**되고, 재개 안내 명령은 사용자가 준 옵션 전부(`--ssh-opts`·`--include-config`·`--passphrase-env`·`--restore-config`·`--force`·`--yes`·`--target-db`·`--service-port`·`--allow-same-host` 등. 값이 비밀인 옵션 없음 — 패스프레이즈는 이름만)에서 재구성된다. `--remote-bundle REMOTE_BUNDLE`(이미 만들어 둔 원격 번들 절대경로를 재사용 — ① 생략)은 ③ 이전 실패의 재개용이다.
- `--max-backup-seconds`·`--compress` 는 원격 `export_snapshot` 으로 그대로 전달된다. 기본 ssh 옵션에 `-o ServerAliveInterval=30 -o ServerAliveCountMax=20` 이 포함되며, `--ssh-opts` 를 주면 그 뒤에 **덧붙는다**(긴 export 중 ssh 단절 완화).
- `--source` 는 `-` 로 시작하거나 `USER@HOST`(`([A-Za-z0-9._-]+@)?[A-Za-z0-9._:\[\]-]+`) 형식이 아니면 `CommandError`, ssh/rsync 호출에는 `--` 구분자를 쓴다(옵션 주입 방어).
- 제약: `--restore-config`·`--passphrase-env` 는 `--include-config` 없이 쓰면 거부된다. ⑤ verify 는 항상 `--allow-newer`(+ `--target-db` 지정 시 통과)로 호출한다. ③ 의 원격 삭제는 원격 경로가 `.../snapshots/qg-*` 형태일 때만 수행한다.

### 5.5 `fetch_snapshot_via_api` (새 서버에서 실행 — HTTPS 만 열린 경우, 경로 C)

원본 서버의 Export API(`POST/GET/DELETE /api/v2/export/snapshots/…`, 스코프 `export:read` — 엔드포인트·오류 코드는 `docs/api_guide.md` §4.5·§7.1a, 요구사항·결정은 `docs/api_requirements_data_export.md`)로 DB 번들을 내려받아 `export_snapshot` 과 **같은 번들 형식**(`manifest.json` + `db.sqlite3.zst|gz`)으로 `snapshots/qg-<manifest 의 UTC>/` 에 배치한다. ssh/rsync 가 막히고 HTTPS 만 열린 환경(IDC → ALB)용이다.

```text
fetch_snapshot_via_api --url https://HOST[/prefix] --key-env VAR
                       [--out-dir snapshots] [--compress auto|gzip|zstd]
                       [--poll-interval 10] [--timeout 14400]
                       [--existing-id UUID] [--keep-remote] [--dry-run]
```

- **키는 환경변수로만**: `--key-env VAR` 가 가리키는 변수에서 읽는다. 명령행 인자·로그·예외 메시지·출력에 값을 남기지 않는다(응답 오류문에서도 `***` 로 치환). 사용자가 자기 터미널에서 `read -rs QGP_KEY; export QGP_KEY` 로 만든다(§7.5 와 같은 원칙 — AI 는 키 값을 받지 않는다).
- **흐름**: ① POST 로 생성(또는 `--existing-id` 재개) → ② `ready` 까지 폴링(`--poll-interval`, `--timeout`) → ③ `manifest.json`, DB 파일을 `<out-dir>/.fetch-<id>/` 에 `.part` 로 받으며 **Range 이어받기**, 다운로드 후 **sha256 검증**(응답 `X-Content-SHA256`·상태 응답 `files[].sha256`·manifest `db.file_sha256` 모두 일치해야 함) → ④ `<out-dir>/qg-<ts>/` 로 배치(`manifest.json` 마지막에 이동) → ⑤ 원격 `DELETE`(`--keep-remote` 면 보존) → 다음 단계 안내(`import_snapshot` / `verify_snapshot --allow-newer`).
- **안전장치**: `https://` 만 허용(`http://` 는 127.0.0.1/localhost 테스트용만, 인증서 검증 끄는 옵션 없음) · URL 에 사용자/비밀번호·query 금지 · 다른 호스트/스킴으로의 리다이렉트는 거부(Authorization 유출 방지) · 서버가 알려준 파일명은 허용 목록(`manifest.json`, `db.sqlite3[.zst|.gz]`)만 · manifest 에 `config` 가 있으면 거부 · sha256 을 서버가 주지 않으면 거부 · 디스크 여유 공간 선점검 · 5xx/네트워크 오류는 최대 5회 재시도(POST 는 중복 생성 방지로 재시도 안 함) · 이미 같은 `qg-<ts>/` 가 있으면 덮어쓰지 않음.
- **실패 시**: 메시지에 원격 snapshot id 와 재개 명령(`--existing-id`)이 항상 포함된다(원격은 24시간 뒤 자동 만료). 부분 다운로드는 `.fetch-<id>/*.part` 로 남아 재개 시 Range 로 이어받는다. 원격에서 다른 export 가 실행 중이면 409 `export_in_progress`.
- **config 는 오지 않는다**: Export API 는 `.env`·`secrets/`·crontab·systemd 유닛을 내보내지 않는다(`--no-config` 번들과 동일). 새 서버의 `.env`/secrets 는 **별도 경로**(원본 관리자가 `export_snapshot --passphrase-env` 로 만든 `config.tar.enc` 를 사람이 직접 전달해 `import_snapshot --restore-config`, 또는 새 서버에서 값을 직접 구성)로 옮긴다 — §6.3 (4) 와 §12 #20.

## 6. 절차

공통 전제: 모든 `<…>` 는 §7.1 변수. 명령은 별도 표기가 없으면 **새 서버**에서 저장소 루트 기준.

### 6.1 사전 준비 (새 서버, 서비스·cron 아직 켜지 않음)

요구사항: Linux, Python 3.10+(원본은 3.12), git, `zstd`, `rsync`, `openssl`, `flock`(util-linux), `cron`, `tmux`(긴 단계용, §7.4), sudo 권한, 디스크 여유 **약 25GB 이상**(복원본 5.1GB + 압축 번들 + 임시 `.importing` 5.1GB + `pre-import` 보존본 + 여유. 정확한 압축률은 §9 에서 측정), 새 서버→git 서버(`git.jobkorea.co.kr`) 접근, (pull 방식이면) 새 서버→원본 ssh 접근.

```bash
# 1) 코드
git clone https://git.jobkorea.co.kr/qa-management/qa_qualitygate_portal.git <NEW_DIR>
cd <NEW_DIR>
git log -1 --format=%H          # 하드 게이트(§6.3 (0)): 원본의 manifest.source.git.commit 과 같아야 함 (다르면 원본에서 push 했는지 확인)

# 2) git 에 추적된 옛 db.sqlite3(약 514MB): git 동작이 건드리지 않게 고정하고, import 전에 치워 둔다
git update-index --skip-worktree db.sqlite3
mv db.sqlite3 db.sqlite3.git-clone       # 치워 두면 import 에 --force 가 필요 없다 (.gitignore 에 있어 커밋되지 않음)

# 3) venv + 의존성 (run.sh 를 실행하지 말 것 — 아래 주의)
python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt

# 4) .env 최소 설정 — import 단계 전에는 DJANGO_SETTINGS_MODULE 만 있으면 된다
printf 'DJANGO_SETTINGS_MODULE=config.settings.dev\n' > .env && chmod 600 .env
#   (원본 .env 는 §6.3 에서 config 번들로 복원한다. 직접 값을 채워 넣지 말 것)

# 5) 도구 확인
for c in zstd rsync openssl flock crontab; do command -v $c >/dev/null && echo "ok $c" || echo "MISSING $c"; done
.venv/bin/python manage.py check
```

주의
- **`run.sh` / `qualitygate.service`(설치본)를 import 전에 실행하지 말 것.** `run.sh` 는 기동 시 `git merge --ff-only`, `pip install`, `migrate --run-syncdb` 를 자동 수행한다. 데이터 없는 상태에서 migrate 되거나 git 이 db.sqlite3 를 갱신하면 import 의 "대상에 이미 데이터" 검증과 충돌한다.
- clone 된 `db.sqlite3`(옛 스냅샷, 데이터 있음)는 위 2) 처럼 `db.sqlite3.git-clone` 으로 치워 둔다 → 대상 DB 가 비어 있으므로 import 에 `--force` 가 필요 없다. `--force` 는 의도적 덮어쓰기에만 쓴다. 치우지 않았다면 import 는 "대상에 이미 데이터" 로 거부된다(그때 `--force` 를 쓰기 전에 사용자에게 보여주고 동의를 받는다, §7.3).
- 원본 쪽 준비: 원본에서도 `git pull` 이 아니라 **`apps/syncops` 가 포함된 같은 커밋**이어야 `export_snapshot` 이 존재한다. 원본에서 `git log -1` 이 새 서버와 같은지 비교. 원본에 미커밋 변경이 있으면(`manifest.source.git.dirty_files`) 새 서버에는 그 코드가 없으므로 먼저 커밋·push 되어야 한다(§12 #3, #14).

### 6.2 리허설 (원본 서비스 가동 중, 읽기 전용만)

목적: 도구·네트워크·디스크·소요시간을 최종 컷오버 전에 확인. **평시 리허설은 아래 (A)(B) 와 §9 의 압축률 샘플 측정만** 한다. 새 서버의 실제 DB 는 건드리지 않는다.

```bash
# (A) 원본에서 읽기 전용 점검 — 안전 (DB mode=ro, 아무것도 쓰지 않음)
cd <SOURCE_DIR> && .venv/bin/python manage.py export_snapshot --dry-run

# (B) 새 서버에서 명령 계획만 출력 (실행 안 함, 값 마스킹)
.venv/bin/python manage.py sync_from_source \
  --source <SOURCE_USER>@<SOURCE_HOST> --source-dir <SOURCE_DIR> \
  --ssh-opts "-i <SSH_KEY_PATH>" --dry-run

# (압축률 샘플) §9 의 절차로 DB 앞 1GiB 압축 크기·시간 측정 (원본 읽기만)
```

리허설 통과 기준: ① (A) 종료코드 0 ② (B) 종료코드 0 이고 출력된 ssh/rsync/import 명령이 의도와 같음 ③ 압축률 샘플·전송 속도 측정값을 기록 → 컷오버 점검 창 길이 결정.

**(C) 원본 가동 중 실제 5GB export 는 기본 절차에서 제외한다.** 원본이 매분 cron·폴링으로 DB 에 쓰므로 backup API 가 재시작될 수 있고, 5GB 읽기 IO 가 운영에 부담이다. 실제 export 리허설은 **원본 cron·웹·리스너를 멈춘 점검 창**이거나 **사용자의 명시 동의**가 있을 때만, `--max-backup-seconds` 를 짧게(예 `300`) 주어 실행한다. 임시 대상 DB 는 `mktemp -d` 아래에 둔다:

```bash
rh=$(mktemp -d)                      # 0700 임시 디렉터리 — 리허설 DB·번들 전용
.venv/bin/python manage.py sync_from_source \
  --source <SOURCE_USER>@<SOURCE_HOST> --source-dir <SOURCE_DIR> \
  --ssh-opts "-i <SSH_KEY_PATH>" --max-backup-seconds 300 \
  --local-dir "$rh" --target-db "$rh/rehearsal.sqlite3" --yes      # migrate 자동 생략, config 미포함
.venv/bin/python manage.py verify_snapshot "$rh"/qg-* --target-db "$rh/rehearsal.sqlite3"
rm -rf -- "$rh"                      # 리허설 산출물 정리
```

리허설에서 `config` 는 `--include-config` 없이(= `--no-config`) 돌려 비밀이 새 서버에 불필요하게 복제되지 않게 한다. 같은 호스트(원본)에서 `import_snapshot` 을 돌리는 왕복 테스트는 `--allow-same-host` 가 필요하며 운영 DB 경로를 `--target-db` 로 지정하면 안 된다(§5.2).

### 6.3 최종 컷오버 싱크

사전 합의: 점검 창 공지(원본 정지 ~ 게이트웨이 전환 사이 포털 사용 불가), Slack 알림·Jenkins 콜백이 이 사이 유실될 수 있음을 인지.

**(0) 하드 게이트 — import 전 필수 (하나라도 틀리면 중단하고 사용자에게 보고)**

```bash
git log -1 --format=%H                                     # 새 서버 HEAD
ssh -o BatchMode=yes -i <SSH_KEY_PATH> -- <SOURCE_USER>@<SOURCE_HOST> "git -C <SOURCE_DIR> log -1 --format=%H; git -C <SOURCE_DIR> status --short | grep -v '^??' | head"
# 번들을 받은 뒤(경로 B 는 import 전)에는 manifest.source.git.commit 과 직접 대조:
.venv/bin/python -c "import json,sys; m=json.load(open(sys.argv[1])); print(m['source']['git']['commit'], m['source']['git']['dirty_files'])" snapshots/qg-<TS>/manifest.json
```

통과 기준: 새 서버 `git log -1 --format=%H` == `manifest.source.git.commit`, 또는 새 서버 코드가 더 새롭고 번들의 모든 마이그레이션이 현재 코드에 있다(import 선행 검증이 확인). `dirty_files` 가 비어 있지 않으면 그 변경이 새 서버 코드에 들어 있는지 확인(push 선행). 코드가 데이터보다 오래된 경우 `--allow-older-code` 로 우회하지 말고 `git pull` 로 코드를 맞춘다. 또 새 서버의 clone 된 옛 `db.sqlite3` 는 §6.1 2) 로 `db.sqlite3.git-clone` 으로 치워져 있어야 한다.

**(1) 원본 정지 — 사용자가 원본 서버에서 직접 수행하고 완료를 보고한다.** (원격 `sudo` 는 비밀번호가 필요하고 AI 는 입력하지 않는다. AI 는 원본 정지 명령을 대신 실행하지 않는다.) 이 호스트의 서비스는 systemd 가 아니라 수동 기동 프로세스일 수 있으므로 어떻게 기동했는지도 사용자가 판단한다. 사용자가 원본에서:

```bash
# cron 3줄 보관 후 비활성화 (export 가 crontab 을 번들에 담으므로 원본은 반드시 pristine 사본을 따로 보관)
crontab -l > ~/qg-crontab.pristine
crontab -l | sed -E 's/^(\* \* \* \* \* .*manage\.py (sync_api_owners_from_sheet|sweep_reliability|jenkins_keepalive).*)$/#QGMIGRATE#\1/' | crontab -
crontab -l | grep -c '^#QGMIGRATE#'       # 3 이어야 함
pgrep -af 'manage.py (sync_api_owners_from_sheet|sweep_reliability|jenkins_keepalive)' || echo "진행 중 cron 작업 없음"

# 웹/리스너 정지 (systemd 면 아래, 수동 프로세스면 PID 종료)
sudo systemctl stop qg-slack-triage
sudo systemctl stop qualitygate 2>/dev/null || true
ss -ltnp | grep ':8601' || echo "8601 비어 있음"   # 비어야 함. 남아 있으면 해당 PID 종료
```

사용자가 "원본 정지 완료(cron 3줄 비활성·8601 비어 있음)" 라고 보고하면 AI 는 (2) 로 진행한다(§7.1 확인 질문). `crontab.txt` 는 `#QGMIGRATE#` 접두(주석 상태)와 원본 경로를 담은 채 번들에 들어가므로 새 서버에서 설치할 때 접두 제거·경로 치환이 필요하다(아래 (5)).

**(2) export + 전송 + import + verify**

먼저 사용자가 config 전달 방식 `<CONFIG_MODE>` 를 결정한다(§7.1).
- **암호화(권장)**: 패스프레이즈가 필요하므로 **사용자가 자기 터미널에서 직접 실행**한다. AI 의 Bash 도구는 호출 간 셸 환경변수를 유지하지 않고, 값이 대화·도구 로그에 남으면 안 된다. AI 는 사용자가 알려 주는 종료코드·요약표(와 아래 로그 파일의 비밀 아닌 줄)만 받는다.
- **평문(사내 신뢰구간에서만)**: config 를 평문 tar(0600)로 전달하고 **번들을 import 직후 즉시 삭제**한다. 패스프레이즈가 없으므로 AI 가 tmux/nohup 으로 직접 실행할 수 있다. 사용자의 명시 동의 필요(비밀이 평문으로 네트워크·디스크에 잠시 존재).

경로 A — 새 서버가 원본에 ssh 가능(권장, 한 줄). 긴 단계이므로 tmux 안에서 실행하고 로그를 남긴다(§7.4).

```bash
# 암호화 모드 — 사용자 본인 터미널에서. 값은 화면·히스토리·로그에 남기지 않는다
tmux new -s qgsync
read -rs <PASSPHRASE_ENV>; export <PASSPHRASE_ENV>        # 프롬프트에 패스프레이즈 입력 (에코 없음)
cd <NEW_DIR> && mkdir -p snapshots
.venv/bin/python manage.py sync_from_source \
  --source <SOURCE_USER>@<SOURCE_HOST> --source-dir <SOURCE_DIR> \
  --ssh-opts "-i <SSH_KEY_PATH>" \
  --include-config --passphrase-env <PASSPHRASE_ENV> \
  --restore-config --force --yes 2>&1 | tee -a snapshots/sync.log
echo "exit=${PIPESTATUS[0]}" | tee -a snapshots/sync.log
unset <PASSPHRASE_ENV>
```

```bash
# 평문 모드 — AI 가 실행 가능 (사용자 동의 후). 번들은 끝나면 즉시 삭제
cd <NEW_DIR> && mkdir -p snapshots
nohup .venv/bin/python manage.py sync_from_source \
  --source <SOURCE_USER>@<SOURCE_HOST> --source-dir <SOURCE_DIR> \
  --ssh-opts "-i <SSH_KEY_PATH>" \
  --include-config --restore-config --force --yes > snapshots/sync.log 2>&1 &
# 끝난 뒤(§7.4 폴링으로 exit 확인): rm -rf -- snapshots/qg-<TS>
```

`--force` 는 이 명령에서 **신규 `.env`(§6.1 의 최소본) 를 `.env.bak-<ts>` 로 보관하고 원본 `.env` 로 교체**하기 위해서만 쓴다(config 덮어쓰기 의도). 대상 DB 는 §6.1 2) 로 비워 두었으므로 DB 덮어쓰기 목적이 아니다. 원격 번들은 ③ 검증 직후 자동 삭제된다(`--keep-remote` 로 보존 가능).

경로 B — 오프라인(ssh/네트워크 불가, §10): 원본에서 export, 아무 수단으로 번들 디렉터리를 이동, 새 서버에서 import. **원본 쪽 export 는 사용자가 원본에서 직접 실행**(암호화 모드에서는 패스프레이즈가 필요하므로 §7.5 동일).

```bash
# 원본 (사용자 터미널, tmux 권장)
read -rs <PASSPHRASE_ENV>; export <PASSPHRASE_ENV>
cd <SOURCE_DIR> && .venv/bin/python manage.py export_snapshot --passphrase-env <PASSPHRASE_ENV>
unset <PASSPHRASE_ENV>
#   출력된 번들 경로: <SOURCE_DIR>/snapshots/qg-<TS>/

# 전송 (예시 — 하나만 사용)
rsync -a --partial --info=progress2 -- <SOURCE_DIR>/snapshots/qg-<TS>/ <NEW_USER>@<NEW_HOST>:<NEW_DIR>/snapshots/qg-<TS>/   # 원본→새 서버 push (원본이 새 서버로 ssh 가능할 때)
# aws s3 cp --recursive <SOURCE_DIR>/snapshots/qg-<TS>/ s3://<BUCKET>/qg-<TS>/   # S3 경유 (DB 는 암호화되지 않음 — SSE/별도 암호화 필수, 전송 후 삭제)
# USB/외장 디스크: 디렉터리째 복사 (매체 암호화 필수)

# 새 서버 — §6.3 (0) 하드 게이트(manifest 커밋 대조)를 통과한 뒤. 암호화 모드는 사용자 터미널에서
read -rs <PASSPHRASE_ENV>; export <PASSPHRASE_ENV>
.venv/bin/python manage.py import_snapshot snapshots/qg-<TS> --restore-config --passphrase-env <PASSPHRASE_ENV> --force --yes
unset <PASSPHRASE_ENV>
.venv/bin/python manage.py verify_snapshot snapshots/qg-<TS> --allow-newer      # 패스프레이즈 불필요 — AI 가 실행 가능
```

오프라인 번들은 sha256 무결성만 있고 **출처 인증(서명)은 없다** — 번들 전달 경로가 신뢰되는지(누가 만들어 어떻게 옮겼는지)를 사람이 확인한다(§8).

경로 C — HTTPS 만 열려 있음(ssh/rsync/직결 불가, 예: IDC `wx-qms221` → `https://qualitygate.jkintra.net`): 원본의 Export API 를 `fetch_snapshot_via_api`(§5.5)로 내려받는다. 경로 A/B 와 달리 **원본 쪽 셸 작업이 필요 없다**. 단, 다음 전제가 있다.

- **원본에 이 코드(Export API)가 배포돼 있어야 한다.** 이 저장소 변경이 원본에 반영돼야(`git pull` + 서비스 재시작, 원본 설치 유닛이 `run.sh` 면 재시작만으로 반영) `/api/v2/export/snapshots/` 가 생긴다. 배포 전에는 404 라서 쓸 수 없다 — 원본 배포 권한자에게 요청한다.
- admin 이 원본 포털(`/accounts/api-keys/new/`)에서 **`export:read` 스코프를 명시한 키**를 발급한다(`*` 는 포함하지 않는다, 만료는 짧게 — 7일 이하 권장). 평문은 발급 직후 1회만 보이므로 사용자가 새 서버 환경변수로 직접 옮기고, **작업이 끝나면 즉시 회수**한다. 대화/로그/문서에 평문을 남기지 않는다.
- **config(`.env`·`secrets/`·crontab·systemd)는 API 로 오지 않는다.** 별도로 전달·구성해야 한다(§5.5).
- 컷오버 일관성 규칙은 같다: 최종 컷오버는 원본 cron·웹·리스너 정지 후(§6.3 (1)) 스냅샷을 뜬다. 리허설용은 가동 중에도 가능하나 `--max-backup-seconds` 정책(§9)과 같은 이유로 backup 이 늦어질 수 있다.

```bash
# 새 서버 (사용자 터미널, tmux 권장). 키는 화면·히스토리·로그에 남기지 않는다
tmux new -s qgfetch
read -rs QGP_KEY; export QGP_KEY                             # 프롬프트에 키 입력 (에코 없음)
cd <NEW_DIR> && mkdir -p snapshots
.venv/bin/python manage.py fetch_snapshot_via_api --url https://qualitygate.jkintra.net --key-env QGP_KEY \
  --out-dir snapshots 2>&1 | tee -a snapshots/fetch.log      # 중단돼도 같은 명령(또는 출력된 --existing-id 명령)으로 이어받기
unset QGP_KEY

# §6.3 (0) 하드 게이트(manifest 커밋 대조) 통과 후
.venv/bin/python manage.py import_snapshot snapshots/qg-<TS> --yes                 # config 가 번들에 없으므로 --restore-config 없음
.venv/bin/python manage.py verify_snapshot snapshots/qg-<TS> --allow-newer
# 이어서 §6.3 (3) 검증, (4) 에서 .env/secrets 를 별도 경로로 구성한 뒤 (5) 이후 진행
```

- 사전 점검(원본에 코드가 배포됐는지): `curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $QGP_KEY" https://qualitygate.jkintra.net/api/v2/export/snapshots/<임의 uuid>/` → 404(`not_found` JSON)면 라우트 존재, 라우트가 없으면 HTML 404, 403 이면 스코프/admin 부족. `--dry-run` 은 요청을 보내지 않고 계획만 출력한다.
- 번들 DB 는 암호화되지 않는다 — HTTPS 구간은 TLS 가 보호하지만 `.fetch-<id>/`·`qg-<TS>/`·원격 `api-<id>/` 에 평문 DB 가 잠시 존재한다. 끝나면 `rm -rf -- snapshots/qg-<TS> snapshots/.fetch-*`(import 후 번들 보관 불필요 시), 원격은 자동 DELETE(또는 24시간 만료).

**(3) 검증 (이 단계 통과 전에는 (4) 이후로 넘어가지 않는다)**

```bash
.venv/bin/python manage.py verify_snapshot snapshots/qg-<TS> --allow-newer ; echo "exit=$?"   # 0
.venv/bin/python manage.py showmigrations accounts | tail -3                                     # 0003 [X]
.venv/bin/python manage.py migrate --plan | head                                                  # "No planned migration operations"
.venv/bin/python manage.py check
.venv/bin/python - <<'EOF'
import sqlite3; c = sqlite3.connect("file:db.sqlite3?mode=ro", uri=True)
print(c.execute("pragma quick_check").fetchone(), c.execute("select count(*) from suites_test_suite_job_run").fetchone())
EOF
```

`--allow-newer` 가 필요한 이유: migrate 가 `django_migrations` 에 `accounts.0003_externalidentity` 행을 추가하고 새 테이블·권한 행이 생기므로 대상이 manifest 보다 많아진다(§12 참조). `import_snapshot` 이 migrate 까지 한 뒤 `--allow-newer` 없이 `verify_snapshot` 을 돌리면 `django_migrations` 등이 MISMATCH 로 나올 수 있다 — 정상이므로 `--allow-newer` 로 재확인. verify 는 **행 수·마이그레이션만 비교**한다 — 내용 무결성은 import 단계의 sha256 이 보증한다.

**(4) 설정 정리** — `restore-config` 가 푼 `.env` 의 경로·호스트 값을 새 환경에 맞게 수정 (값은 출력하지 말고 키만 확인):
`GOOGLE_CREDENTIALS_FILE`(절대경로 → `<NEW_DIR>/secrets/qg-owners-wif.json`), `DJANGO_ALLOWED_HOSTS`(새 서버 IP/도메인 추가), `QGP_BASE_URL`·`DJANGO_CSRF_TRUSTED_ORIGINS`(도메인이 그대로면 유지). `DJANGO_SECRET_KEY` 와 암호화 관련 키는 **원본과 동일하게 유지**(세션·서명값 유효성). 비밀을 `.incoming` 으로 받았다면 비교 후 반영(`diff <(cut -d= -f1 .env | sort) <(cut -d= -f1 .env.incoming | sort)` — 키 이름만 비교).

**(5) systemd · cron 설치**

`restored-system/crontab.txt` 는 ① 각 줄이 `#QGMIGRATE#` 접두(원본 정지 때 붙인 주석)이고 ② **원본 경로 `<SOURCE_DIR>`** 가 들어 있으므로, 접두 제거와 경로 치환 후 설치한다. `restored-system/` 의 `qualitygate.service` 는 원본 설치본(`run.sh` 호출)이므로 **설치하지 않는다** — 새 서버는 `deploy/` 템플릿(`runserver --noreload` 직접 실행, run.sh 의 자동 `git merge`·`migrate` 없음)을 치환해 설치한다(§12 #5).

```bash
cd <NEW_DIR>/restored-system 2>/dev/null || cd <NEW_DIR>
# cron: 이미 있는 crontab 이 있으면 덮어쓰지 않도록 먼저 확인
crontab -l 2>/dev/null | cat   # 비어 있어야 함
tmp=$(mktemp)                  # 0600 임시 파일 (/tmp 고정 이름 금지)
sed -E 's/^#QGMIGRATE#//; s#<SOURCE_DIR>#<NEW_DIR>#g' crontab.txt > "$tmp"
grep -vE '^(#|$)' "$tmp"       # 3줄이고 경로가 새 경로인지 육안 확인 (경로에 <SOURCE_DIR> 가 남아 있으면 안 됨)
crontab "$tmp"; rm -f "$tmp"

# systemd: deploy/ 템플릿 기준(경로·User 를 새 서버에 맞게 치환)
sed -e 's#/home/taeshkim/qa-qg#<NEW_DIR>#g' -e 's#/home/taeshkim/qa_qualitygate_portal#<NEW_DIR>#g' -e 's#taeshkim#<NEW_USER>#g' \
  <NEW_DIR>/deploy/qualitygate.service | sudo tee /etc/systemd/system/qualitygate.service >/dev/null
sed -e 's#/home/taeshkim/qa_qualitygate_portal#<NEW_DIR>#g' -e 's#taeshkim#<NEW_USER>#g' \
  <NEW_DIR>/deploy/qg-slack-triage.service | sudo tee /etc/systemd/system/qg-slack-triage.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now qualitygate qg-slack-triage
systemctl is-active qualitygate qg-slack-triage     # active active
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8601/   # 200 또는 로그인 리다이렉트(302)
journalctl -u qualitygate -n 30 --no-pager
```

(`sudo` 가 비밀번호를 요구하면 사용자가 직접 실행한다.) 주의: `deploy/qualitygate.service` 는 `Environment=DJANGO_SETTINGS_MODULE=config.settings.dev` + `runserver --noreload` 이고, 원본의 설치본은 `run.sh` 를 호출한다. 새 서버는 `run.sh`(자동 git 동기화·migrate) 대신 `deploy/` 템플릿을 쓰는 것을 기본으로 한다(결정 사항은 §12).

**(6) 서비스 가동 후 검증**: cron 1분 후 `/tmp/qg_owner_sync.log`·`/tmp/qg_reliability_sweep.log`·`/tmp/qg_jenkins_keepalive.log` 에 에러가 없는지, Slack 알림 채널·`qg-slack-triage` 로그에 연결 성공이 보이는지, 포털 로그인과 `/suites/`·Run 목록이 보이는지 확인. 최종 `verify_snapshot ... --allow-newer` 재실행.

**(7) 게이트웨이 업스트림 전환**: `qw.jkintra.net`(`/qg/` 접두 제거 → 새 서버 `:8601`) 의 nginx 업스트림을 새 서버로 변경 → reload → `https://qw.jkintra.net/qg/` 로 접속 확인. `qualitygate.jkintra.net` 은 **이관 전 임시 접속 주소**(ALB 직결)로 남아 있다가, 최종 접속 주소는 `qw.jkintra.net/qg` 로 일원화되고 **이관(전환 검증) 후 `qualitygate.jkintra.net` 을 폐기**한다(`docs/handoff_sso_integration.md` §1). (게이트웨이 설정은 이 저장소 밖; `docs/qw-sso-gateway.md`)

**(8) 원본 읽기 전용 보관 — 사용자가 원본에서 직접 수행하고 완료를 보고한다.** 원본의 cron·`qg-slack-triage`·웹은 **정지 상태 유지**(재기동 금지), `db.sqlite3` 와 `snapshots/` 는 삭제하지 않는다. 사용자가 `chmod a-w <SOURCE_DIR>/db.sqlite3` 로 실수 쓰기를 막는다. 롤백 대비 최소 `<RETENTION_DAYS>`(예: 2주) 보관. (새 서버 쪽 정리: 컷오버 안정화 후 `db.sqlite3.git-clone`·번들·`.incoming`·`.bak-*` 삭제, §11.)

### 6.4 롤백

**원본이 살아 있는 한 롤백은 "원본 재가동 + 게이트웨이 원복"이다.** 새 서버에서 생긴 데이터는 원본에 자동 반영되지 않는다.

| 시점 | 조치 |
|---|---|
| import 실패(종료코드 ≠ 0) | 새 서버 기존 DB 는 그대로, `.importing` 은 정리됨. 에러 원인 해결 후 같은 명령 재실행. 원본은 (1) 정지 상태이므로 급하면 아래 복구 |
| import 성공했지만 verify/기동 검증 실패 | 새 서버: `mv db.sqlite3 db.sqlite3.failed-<ts> && mv db.sqlite3.pre-import-<ts> db.sqlite3`(옛 DB 복원, `pre-import-<ts>` 가 있을 때만 — §6.1 2) 로 clone DB 를 치워 뒀다면 `pre-import` 가 없으므로 `db.sqlite3.failed-<ts>` 개명 후 `db.sqlite3.git-clone` 을 원래 이름으로) 후 원인 분석. 업스트림은 아직 원본이므로 원본 재가동 |
| 교체 구간에서 중단돼 `db.sqlite3` 가 없음(하드링크 불가 FS 의 rename 폴백) | "대상 없음 + `pre-import-<ts>` 존재" → `mv db.sqlite3.pre-import-<ts> db.sqlite3` (import 가 같은 안내를 출력) |
| 게이트웨이 전환 후 문제 | 업스트림을 원본으로 원복 → 새 서버 `qualitygate`·`qg-slack-triage`·cron 정지(**동시 가동 금지**) → 원본 복구 |

원본 복구 명령 (사용자가 원본에서 직접 수행하고 AI 에게 완료를 보고한다 — `sudo` 비밀번호):

```bash
chmod u+w <SOURCE_DIR>/db.sqlite3
crontab ~/qg-crontab.pristine                    # (1) 에서 보관한 pristine 사본으로 복원
sudo systemctl start qg-slack-triage             # 웹은 원래 기동하던 방식으로 시작
```

새 서버에서 이미 쌓인 데이터를 원본에 되돌려야 하면 이 문서 범위 밖(역방향 싱크는 같은 도구로 방향만 바꿔 수행 가능하나 별도 계획 필요).

## 7. AI 실행 규칙

### 7.1 먼저 사용자에게 물어볼 변수

| 변수 | 질문 | 비고 |
|---|---|---|
| `<MODE>` | 리허설만 / 최종 컷오버 | 기본 리허설부터 |
| `<SOURCE_HOST>`, `<SOURCE_USER>` | 현재(원본) 서버 호스트·ssh 계정 | |
| `<SOURCE_DIR>` | 원본의 저장소 절대경로 (예: `/home/taeshkim/qa_qualitygate_portal`) | |
| `<SSH_KEY_PATH>` | 새 서버에서 원본에 접속할 ssh 키 경로 | 비밀번호 로그인 불가(BatchMode). 키 경로만 받고 내용은 읽지 않는다 |
| `<NEW_DIR>`, `<NEW_USER>` | 새 서버의 저장소 경로·실행 계정 | |
| `<CONFIG_MODE>` | config 전달 방식: **암호화(권장)** / **평문(사내 신뢰구간, 번들 즉시 삭제)** — 사용자 결정 | §6.3(2), §7.5. 평문은 비밀이 평문으로 전송·보관되는 것에 대한 명시 동의 필요 |
| `<PASSPHRASE_ENV>` | (암호화 모드) 패스프레이즈를 담을 **환경변수 이름**(예 `QG_SNAP_PASS`) | **이름만** 받는다. 값은 사용자가 자기 터미널에서 `read -rs` 로 입력(§7.5). AI 는 값을 받거나 읽거나 출력하지 않는다 |
| `<ROUTE>` | ssh pull 가능(경로 A) / 오프라인(경로 B) | §10 |
| 원본 기동 방식 | systemd/수동 프로세스? 웹 정지 방법 | §6.3(1) — 정지는 사용자가 수행 |
| **원본 정지 완료?** | "원본에서 cron 3줄 비활성(`#QGMIGRATE#` 3), 웹·리스너 정지, `:8601` 비어 있음을 직접 확인하셨나요?" | **예** 라는 답을 받기 전에는 §6.3(2) 를 시작하지 않는다. AI 가 대신 정지하지 않는다 |
| **원본 보관 완료?** | "원본 `db.sqlite3` 읽기 전용(`chmod a-w`) 처리·cron·서비스 비활성 유지를 직접 확인하셨나요?" | §6.3(8) — 사용자가 수행 후 보고 |
| 실행 방식 | 긴 단계를 tmux 에서 돌릴 수 있는지(`tmux` 설치·세션 유지) | §7.4 |
| `<ssh 최초 접속 여부>` | 새 서버→원본 호스트키가 `known_hosts` 에 있는지 | §10 — 없으면 먼저 등록 |
| 점검 창 | 컷오버 일시, 공지 여부 | |
| 게이트웨이 담당 | 업스트림 전환 수행 주체 | |
| `<BUCKET>` / 전송 수단 | 경로 B 의 전송 수단 | |

### 7.2 단계별 검증 기준 (통과 못 하면 중단하고 사용자에게 보고)

| 단계 | 통과 기준 |
|---|---|
| 사전 준비 | `manage.py check` 종료코드 0, 도구 5종 ok, 옛 `db.sqlite3` 가 `db.sqlite3.git-clone` 으로 치워짐 |
| 하드 게이트(§6.3(0)) | 새 서버 `git log -1 --format=%H` == `manifest.source.git.commit` (또는 새 서버가 더 새롭고 번들 마이그레이션이 현재 코드에 있음), `dirty_files` 확인. 불일치 시 import 금지 |
| 원본 정지 | 사용자가 "정지 완료" 를 보고함(AI 는 원본을 정지하지 않음) |
| export | 종료코드 0, 번들에 `manifest.json`·`db.sqlite3.*`(·`config.tar[.enc]`) 존재, 디렉터리 0700/파일 0600 |
| 전송 | rsync 종료코드 0(또는 `sha256sum` 이 manifest `db.file_sha256` 과 일치) |
| import | 종료코드 0, **import 자체 검증(`.importing` 단계: sha256·quick_check·테이블 행 수)이 통과**했고 요약표 차이는 **migrate 로 인한 `django_migrations`/`django_content_type`/`auth_permission`/`sqlite_sequence` 증가분뿐**, `.importing` 잔재 없음. `pre-import-<ts>` 는 대상에 기존 DB 가 있었을 때만 존재(§6.1 2) 로 치웠다면 없음) |
| migrate | `accounts.0003_externalidentity` `[X]`, `migrate --plan` 비어 있음 |
| verify | 종료코드 0, 불일치 0건(`--allow-newer` 사용 시 "대상이 더 많음"만 허용), 마이그레이션 차이는 0003 하나만 |
| 기동 | `systemctl is-active` 2종 `active`, `curl 127.0.0.1:8601` 200/302, cron 로그 무에러 |

### 7.3 절대 하지 말 것

- **운영 DB(원본 `db.sqlite3`) 에 쓰기**: 원본에서 `migrate`·`import_snapshot`·임의 SQL 금지. 원본에서 허용되는 것은 `export_snapshot`(DB 읽기)·`--dry-run`·`mode=ro` 조회뿐.
- **새 서버의 기존 DB 를 사용자 확인 없이 덮어쓰기**: `--force` 는 의도적 덮어쓰기(대상에 이미 데이터가 있음, config `.bak` 교체, 손상 의심 DB 교체)에만, 그 사실을 사용자에게 보여주고 명시적 동의를 받은 뒤에. 항상 `pre-import-<ts>` 보존 확인. 옛 clone DB 는 `--force` 가 아니라 `mv db.sqlite3 db.sqlite3.git-clone` 으로 치운다.
- **`--allow-older-code`·`--allow-same-host` 사용**: 코드가 데이터보다 오래되면 `git pull`(또는 원본에서 push) 로 코드를 맞춘다. `--allow-same-host` 는 원본 서버에서의 왕복 테스트에서만이며 운영 DB 경로를 대상으로 쓰지 않는다.
- **AI 가 패스프레이즈를 받거나 인라인으로 쓰기**: `export VAR='<값>'` 형태를 포함해 어떤 값도 도구 호출·대화·로그에 남기지 않는다(§7.5).
- **AI 가 원본 서버를 정지·보관 처리**(cron 비활성, `systemctl stop`, `chmod`): 사용자가 직접 수행하고 보고한다(§6.3 (1)(8)).
- **Export API 키(`export:read`) 평문을 명령행 인자·로그·대화·문서에 남기기**: `fetch_snapshot_via_api` 에는 `--key-env` 로 변수명만 준다. 키는 사용자가 자기 터미널에서 `read -rs` 로 만들고, 작업이 끝나면 원본에서 회수한다(§6.3 경로 C). 원본(`qualitygate.jkintra.net`) 서버로의 요청은 사용자 동의 후에만.
- **패스프레이즈·`.env`·secrets·토큰 값 출력/기록**: 명령행 인자, 로그, 채팅 응답, 문서, 커밋 어디에도 쓰지 않는다. 키 이름과 파일명만 말한다. `cat .env`·`env` 전체 출력 금지.
- **원본 삭제/덮어쓰기**: 원본의 `db.sqlite3`·`snapshots/`·`.env` 삭제, `crontab -r`, 원본 `git reset/clean` 금지. 컷오버 후에도 원본은 보관.
- **`git add -A` / `git commit -a` 로 `db.sqlite3` 커밋**(5GB). 커밋·push 는 사용자(메인) 가 한다.
- **새 서버와 원본의 cron/Slack 리스너/웹 동시 가동**(중복 알림·이중 폴링·DB 이원화).
- `import_snapshot --ignore-running` 로 가동 중인 서비스 위에 복원.
- 검증 단계 실패 후 다음 단계 진행, 또는 실패를 숨기고 "완료" 보고.
- 원본 가동 중(운영 시간 중)에 원본에서 5GB 실제 export(§6.2 (C))를 사용자 동의·점검 창 없이 실행. 동의가 있어도 `--max-backup-seconds` 를 짧게(예 300) 준다.

### 7.4 긴 단계 실행 (tmux/nohup + 로그 폴링)

원격 export + rsync + import 는 **수십 분**이 걸릴 수 있다(소요 시간은 측정 필요, §9). AI 도구의 포그라운드 호출은 타임아웃이 있고, ssh 단절 시 export 가 SIGHUP 으로 중단될 수 있으므로 다음을 따른다.

- 포그라운드로 직접 돌리지 말고 `tmux new -s qgsync`(권장) 또는 `nohup … > snapshots/sync.log 2>&1 &` 로 실행한다. 사용자 터미널 단계(암호화 모드)는 사용자가 tmux 에서 실행하고 `tee -a snapshots/sync.log` 로 로그를 남긴다.
- AI 는 로그를 **폴링**한다: `tail -n 20 snapshots/sync.log`, 종료 확인은 로그 말미의 `exit=<코드>` 줄 또는 `pgrep -af sync_from_source`. 로그에는 패스프레이즈·`.env` 값이 없어야 하며(도구가 출력하지 않음), 의심되면 중단하고 사용자에게 알린다.
- ssh 옵션: `ServerAliveInterval=30`·`ServerAliveCountMax=20` 은 이제 **기본 포함**이다. `--ssh-opts "-i <SSH_KEY_PATH>"` 만 주면 되고, 필요하면 `--ssh-opts "-i <SSH_KEY_PATH> -o ConnectTimeout=15"` 처럼 덧붙인다(기본값 뒤에 붙음).
- 재개: 실패 메시지가 원격 번들 경로와 재개 명령을 출력한다. **③(manifest·sha256 검증) 이전 실패** → 같은 명령 재실행(rsync `--partial` 이어받기) 또는 `--remote-bundle <원격 번들 경로>`. **③ 이후 실패**(원격은 이미 삭제됨) → 로컬 번들로 `import_snapshot snapshots/qg-<TS> …` 만 다시 실행. export 중 SIGHUP/SIGTERM 으로 끊기면 원격 임시 파일은 정리되므로 처음부터 다시 실행. 원격에 번들이 남았으면 `ssh … rm -rf -- <경로>` 로 정리(사용자 확인 후).

### 7.5 패스프레이즈 취급

- AI 의 Bash 도구는 호출 간 셸 환경변수를 유지하지 않는다. 한 호출에서 `export` 한 값이 다음 호출에 없고, 값을 인자로 쓰면 대화·도구 로그에 남는다. 따라서 **패스프레이즈가 필요한 단계는 사용자가 자기 터미널에서 직접 실행**한다: `read -rs <PASSPHRASE_ENV>; export <PASSPHRASE_ENV>` 로 입력(에코 없음) → 명령 실행 → `unset <PASSPHRASE_ENV>`.
- 해당 단계: `sync_from_source --include-config --passphrase-env …`(경로 A), 오프라인 경로의 원본 `export_snapshot --passphrase-env …` 와 새 서버 `import_snapshot --restore-config --passphrase-env …`(경로 B). 이 외 단계(verify, migrate 확인, cron/systemd 설치, 서비스 확인)는 패스프레이즈가 필요 없어 AI 가 실행한다.
- AI 는 사용자가 알려 주는 **종료코드·요약표**만 받는다. 값·`.env` 내용은 요청하지도 받지도 않는다.
- 대안(사용자 결정 `<CONFIG_MODE>` = 평문): 사내 신뢰구간이면 config 를 평문(0600)으로 전달하고 import 직후 번들을 삭제한다. 이 경우 패스프레이즈가 없어 AI 가 실행할 수 있다. 단 비밀이 평문으로 네트워크·디스크를 지나므로 사용자 명시 동의가 필요하다.

## 8. 보안

- 번들은 **민감정보 덩어리**다: DB(사용자·세션·Jenkins/Slack 관련 데이터) + `.env`(토큰·`DJANGO_SECRET_KEY`) + `secrets/*`(Google 자격증명). 디렉터리 0700·파일 0600 으로 만들어지지만 전송 경로(S3·USB·scp)에서는 별도로 보호해야 한다.
- **config 는 암호화 권장**: `--passphrase-env` 사용(AES-256-CBC + PBKDF2). 패스프레이즈는 환경변수·ssh stdin 으로만 전달하며, **사용자가 자기 터미널에서 `read -rs` 로 입력**한다(§7.5, AI 는 값을 받지 않음). import 는 DB 교체 전에 `config.tar.enc` 첫 블록을 복호화해 패스프레이즈를 먼저 검증한다. 암호화 없이 `--include-config` 를 쓰면 경고가 나오며 사내 신뢰 구간에서만(번들 즉시 삭제).
- **한계 — DB 는 암호화되지 않는다**: 번들 DB 에는 세션·비밀번호 해시·Jenkins/Slack 관련 데이터가 들어 있다(`--passphrase-env` 는 config 만 암호화). export 종료 메시지·`--dry-run` 이 이를 경고한다. 외부 매체(S3/USB)로 옮길 때는 매체 측 암호화(SSE-KMS, LUKS 등) 또는 별도 암호화 필수. `--encrypt-db` 는 구현하지 않았다.
- **한계 — 출처 인증 없음**: 오프라인 번들은 sha256 **무결성**만 있고 HMAC 서명 같은 **출처 인증은 없다**(번들과 manifest 를 함께 바꾸면 탐지 못 함). 신뢰되는 경로로 옮긴 번들만 import 한다.
- **한계 — verify 는 얕다**: `verify_snapshot` 은 행 수·마이그레이션만 비교한다(`--deep` 내용 비교 미구현). 내용 무결성은 import 단계의 sha256 이 보증한다.
- **같은 호스트 가드**: manifest 의 hostname/base_dir(및 DB 경로)가 현재 호스트와 같으면 import 가 거부된다(`--allow-same-host` 로만 해제) — 원본에 원본 번들을 되돌려 덮어쓰는 사고 방지.
- **전송 후 삭제**: 새 서버 import·검증 완료 후 원격 임시 번들(`sync_from_source` 가 기본 삭제)과 중간 매체의 번들을 삭제. 새 서버의 `snapshots/qg-*` 도 컷오버 안정화(예: 1~2주) 후 삭제. `snapshots/` 와 이관 부산물(`.env.incoming`·`.env.bak-*`·`*.incoming`·`*.bak-*`·`restored-system/`·`db.sqlite3.pre-import-*`·`db.sqlite3.importing`·`db.sqlite3.git-clone`)은 `.gitignore` 에 있어 git 에 올라가지 않는다. 단 `db.sqlite3` 는 **git 추적 파일이라 `.gitignore` 로 막히지 않는다** — 새 서버로 코드를 옮기려면 push 가 먼저 필요하고(원본에서 커밋·push), 커밋 전 `git status` 로 `*.incoming`·`*.bak-*` 와 `db.sqlite3` 변경이 섞이지 않았는지 확인하며 **`git add -A`·`git commit -a` 는 금지**한다(§12 #1, #14).
- `.env`/secrets 는 복원 시 기존 파일을 덮어쓰지 않고(`.incoming`) 0600/0700 으로 둔다. `.incoming`·`.bak-<ts>` 파일은 정리 후 삭제.
- tar 복원은 `../`·절대경로·심볼릭 링크 항목을 거부한다. 출처 불명 번들은 import 하지 않는다.
- 새 서버의 ssh 키는 이 용도 전용·읽기 가능 범위 최소화, 컷오버 후 원본의 `authorized_keys` 에서 제거.

## 9. SQLite 락 / WAL · 용량

- journal_mode 가 `delete` 이므로 WAL 파일은 없다. `export_snapshot` 은 `sqlite3.Connection.backup`(청크 `pages=1024`)을 쓴다. **다른 프로세스가 원본에 쓰면 backup 이 처음부터 재시작될 수 있다** (원본은 cron 3종·웹·Slack 리스너·폴링 스레드가 계속 쓴다). 그래서 컷오버에서는 원본 쓰기 주체를 모두 멈추고 export 한다. 못 끝나면 `--max-backup-seconds` 초과로 실패하며 안내문이 나온다(원본 DB 는 변하지 않음).
- 읽는 쪽이 길게 잡고 있는 동안 쓰기 쪽이 `database is locked` 를 만날 수 있다 → 리허설을 운영 시간 중에 돌릴 때 주의(리허설 중 cron 로그에 `locked` 가 늘면 중단).
- import 는 `<target>.importing` 에 풀어 검증하고 기존 DB 를 하드링크 보관한 뒤 `os.replace` 로 교체하므로 **같은 파일시스템**에 충분한 여유가 필요하다. 대상 DB 옆에 임시 5.1GB + 기존 DB 보존본이 동시에 존재한다.
- 필요 디스크(보수적 추정): 원본 = 5.1GB(임시 백업본) + 압축본, 새 서버 = 압축본 + `.importing` 5.1GB + 복원된 DB 5.1GB(교체 전 기존 DB 보존분 포함). 정확히는 각 명령의 디스크 선점검이 판정한다.
- **소요 시간은 단정하지 않는다. 측정 방법**:

```bash
cd <SOURCE_DIR>
# 1) 원본 읽기 전용 점검 + 예상 번들 크기
.venv/bin/python manage.py export_snapshot --dry-run
# 2) 압축률/속도 샘플 (DB 앞 1GiB 로 측정, 원본 읽기만 함)
time (head -c 1073741824 db.sqlite3 | zstd -T0 -3 -c | wc -c)     # 압축 후 바이트 → 약 5배 하면 전체 예상
nproc                                                              # 이 호스트는 4 (zstd -T0 은 코어 수만큼 병렬 — 실제 이득은 측정으로 확인)
# 3) 전송 속도: 임의의 큰 파일로 두 서버 사이 처리량 측정 (§10 연결이 되는 경우)
# 4) 실제 시간: 컷오버 시 export 로그의 단계별 경과(stderr 진행률)와 import 요약표의 시간 기록
```

  측정한 압축 시간·번들 크기·전송 시간·import 시간을 합산해 점검 창을 결정한다. 소요 시간은 **측정 필요**(미측정). 이 호스트는 `nproc`=4·RAM 약 7GB 이고 다른 서비스(TAPilot 등)와 공유하므로 측정값이 시간대에 따라 흔들릴 수 있다. 원본 가동 중 실제 5GB export 로 측정하지 말고(§6.2 (C)) 위 샘플 측정과 점검 창 리허설로 대신한다.

## 10. 네트워크

- **IDC ↔ AWS 연결은 미확인**이다. 이 호스트에서 `172.20.x` 로 타임아웃이 관찰된 바 있다. 새 서버(IDC)와 원본(AWS) 사이에 어느 방향이 열려 있는지 사전에 확인한다.

```bash
# 새 서버에서 원본으로 (경로 A 의 전제)
ssh -o BatchMode=yes -o ConnectTimeout=10 -i <SSH_KEY_PATH> <SOURCE_USER>@<SOURCE_HOST> 'echo ok'
# 원본에서 새 서버로 (경로 B 의 push 전송 전제)
ssh -o BatchMode=yes -o ConnectTimeout=10 <NEW_USER>@<NEW_HOST> 'echo ok'
# 새 서버에서 git 서버
git ls-remote https://git.jobkorea.co.kr/qa-management/qa_qualitygate_portal.git HEAD
```

- **최초 호스트키 등록(경로 A 의 `BatchMode=yes` 는 미등록 호스트를 거부한다)**: 새 서버에서 한 번 아래 중 하나를 수행하고, 지문을 사용자가 확인한다.

```bash
ssh-keyscan -H <SOURCE_HOST> >> ~/.ssh/known_hosts          # 지문은 원본 관리자에게 확인 (MITM 방지)
# 또는 첫 접속 1회만: ssh -o StrictHostKeyChecking=accept-new -i <SSH_KEY_PATH> <SOURCE_USER>@<SOURCE_HOST> 'echo ok'
# sync_from_source 에서 쓰려면: --ssh-opts "-i <SSH_KEY_PATH> -o StrictHostKeyChecking=accept-new"
```

- **원본 쪽 도구 점검**(`sync_from_source` 는 원격에서 export 를 돌리므로 원본에 필요): 사용자가 원본에서(또는 ssh 로) 확인한다.

```bash
ssh -o BatchMode=yes -i <SSH_KEY_PATH> -- <SOURCE_USER>@<SOURCE_HOST> 'for c in rsync zstd openssl; do command -v $c >/dev/null && echo "ok $c" || echo "MISSING $c"; done; test -x <SOURCE_DIR>/.venv/bin/python && echo "ok venv"; cd <SOURCE_DIR> && .venv/bin/python manage.py help export_snapshot >/dev/null && echo "ok export_snapshot"'
```

- 결과별 선택: 새→원본 ssh 가능 = 경로 A(`sync_from_source`). 원본→새 방향만 가능 = 원본에서 `export_snapshot` 후 `rsync` push(§6.3 경로 B). 둘 다 불가 = S3/사내 파일 서버/USB 로 **번들 디렉터리** 이동 후 `import_snapshot`. **HTTPS(ALB) 만 열려 있음 = 경로 C**(`fetch_snapshot_via_api`, §5.5 — 원본에 Export API 배포 필요, config 는 별도 전달).
- **오프라인 경로는 항상 가능해야 한다**: `import_snapshot` 은 로컬 번들 디렉터리만 필요로 한다(네트워크·원본 불필요). 번들이 완전히 복사됐는지는 `manifest.json` 의 `db.file_sha256`(import 가 자동 검증)이 보증한다.
- 전송 중단 시: `rsync --partial` 이 이어받기를 지원하므로 같은 명령 재실행. `sync_from_source` 는 실패 단계·원격 번들 경로·재개 명령을 출력한다(③ 이전 실패는 `--remote-bundle` 로 원격 번들 재사용, ③ 이후는 로컬 번들로 `import_snapshot`). 긴 전송은 tmux 로(§7.4).
- 새 서버에서 Jenkins(`jenkins-qa.dev.jkintra.net`), Slack(아웃바운드 WS/HTTPS), Google API, TAPilot(`tapilot.jkintra.net`), Argo CD 로의 **아웃바운드 연결**도 사전 확인 대상(기동 후 기능이 망 문제로 죽지 않게).

## 11. 이관 후 해야 할 일

1. **SSO 연동 이어가기**: 새 서버에서 `docs/handoff_sso_integration.md 보고 작업 이어서 해줘`. 진행 중 작업: Keycloak OIDC SSO 로그인 + 게이트웨이 헤더 로그인(`apps/accounts/middleware.py`, `config/settings/base.py`, `accounts.0003_externalidentity`), `docs/qw-sso-gateway.md`·`docs/service-sso-migration.md` 참조. 관련 `.env` 키(`QGP_ADMIN_EMAIL`, `OIDC_INTERNAL_EMAIL_DOMAINS`, `LOCAL_LOGIN_ENABLED`, `TRUSTED_PROXY_CIDRS`, `GATEWAY_LOGOUT_URL`)는 `.env.example` 에 있다 — 새 서버의 프록시 CIDR 에 맞게 확인.
2. **도메인·게이트웨이**: `qw.jkintra.net/qg`(최종 접속 주소)의 업스트림을 새 서버로 전환(§6.3(7)). `qualitygate.jkintra.net` 은 이관 전 임시 접속 주소였으므로 전환 검증 후 폐기하고 `DJANGO_ALLOWED_HOSTS`·`DJANGO_CSRF_TRUSTED_ORIGINS` 에서도 정리(`docs/handoff_sso_integration.md` §1·§3.1-3). TLS·`X-Forwarded-*` 헤더·`TRUSTED_PROXY_CIDRS` 확인. 외부 시스템이 **IP 로** 포털을 호출하는 곳(예: Jenkins 웹훅·Argo 콜백, `DJANGO_ALLOWED_HOSTS` 의 `10.44.35.151`)이 있으면 새 주소로 갱신.
3. 원본의 TAPilot/partnership 서비스가 포털을 로컬호스트로 참조하는지 확인(포털을 옮기면 끊길 수 있음).
4. 문서 갱신: `maintenance.md` §8(실행/배포)·`README.md` §7(환경)·`SKILLS.md`(싱크 명령)에 새 서버·`apps/syncops` 반영(docs-keeper).
5. 안정화 후(1~2주): 원본 정리 여부 결정, 번들·`.incoming`·`.bak-*`·`pre-import-*`·`db.sqlite3.git-clone` 삭제.

## 12. 미결정 / 위험

| # | 항목 | 내용 / 대응 |
|---|---|---|
| 1 | **`db.sqlite3` 가 git 에 추적됨** | HEAD 에 약 514MB 옛 DB 가 들어 있어 clone 이 느리고, 로컬 수정(5GB)이 `git status` 에 `M` 로 보인다. 실수로 커밋하면 5GB. 새 서버는 `git update-index --skip-worktree db.sqlite3` 로 고정하고 `mv db.sqlite3 db.sqlite3.git-clone` 으로 치운다(§6.1). 근본 해결(`git rm --cached db.sqlite3` + `.gitignore` 에 `db.sqlite3` 추가, 히스토리 정리 여부)은 **별도 결정** |
| 2 | 원본 쓰기 중 backup 재시작 | 컷오버는 반드시 원본 cron·웹·리스너 정지 후. 정지 없이는 `--max-backup-seconds` 초과 가능 |
| 3 | 코드·데이터 버전 불일치 | 새 서버 코드가 원본보다 오래되면 import 가 거부된다. **`--force` 로는 풀리지 않으며**(`--force` 는 덮어쓰기 전용) `--allow-older-code` 도 쓰지 않는다 — `git pull`/원본 push 로 코드를 맞춘다. import 전 §6.3(0) 하드 게이트(HEAD == `manifest.source.git.commit`). 원본의 미커밋 변경(`manifest.source.git.dirty_files`)이 있으면 새 서버에 그 코드가 없다 → push 선행 |
| 4 | `verify_snapshot` 와 migrate 후 행 수 | migrate 가 `django_migrations`(+1)·새 테이블을 만들어 엄격 비교는 불일치로 나옴 → 컷오버 검증은 `--allow-newer`. manifest 에 없는 새 테이블은 `extra(매니페스트에 없음)` 로 표시만 하고 실패시키지 않음(구현 확인 완료) |
| 5 | systemd 설치본 vs `deploy/` 템플릿 불일치 | 원본 설치본은 `run.sh` 호출, 템플릿은 `runserver --noreload` 이며 템플릿 경로(`/home/taeshkim/qa-qg`)가 현재 저장소 경로와 다르다. 새 서버 설치 시 치환 필수. `run.sh` 의 자동 `git merge`·`migrate` 를 운영 기동에 쓸지 결정 |
| 6 | `GOOGLE_CREDENTIALS_FILE` 절대경로 | 새 경로면 `.env` 수정. 담당자 시트 동기화(cron 1번)가 조용히 실패할 수 있음 |
| 7 | 이중 가동 | 컷오버 중 원본·새 서버의 Slack 리스너/cron 동시 가동 금지(중복 알림, Jenkins 이중 폴링) |
| 8 | 컷오버 중 유실 | 점검 창 동안 Jenkins 콜백·Slack 버튼 클릭·Argo 상태 변화는 처리되지 않는다. Run 폴링은 재개 시 `sweep_reliability` 등이 따라잡는지 컷오버 후 확인 |
| 9 | IDC↔AWS 연결 | 미확인(§10). 불가하면 오프라인 번들 경로 |
| 10 | 새 서버 사양 | 이 호스트는 `nproc`=4(RAM 약 7GB). 새 서버 사양·디스크·Python 버전 미확인 |
| 11 | 압축/전송/복원 시간 | **측정 필요**(미측정). §9 방법(샘플 측정)으로 리허설 전에 측정. 원본 가동 중 실제 export 는 하지 않음(§6.2) |
| 12 | 원본 crontab 은 사용자 단위 | root/다른 사용자 crontab·다른 서비스의 포털 DB 직접 접근은 인벤토리에 없다. 컷오버 전 `ls /etc/cron.d`, `sudo crontab -l`, 포털 DB 를 여는 외부 스크립트 유무 확인 |
| 13 | 암호화된 DB 필드의 키 | `.env` 의 `DJANGO_SECRET_KEY`(및 `.env.example` 의 `ENCRYPTION_MASTER_KEY` 가 설정된 경우)를 원본과 다르게 만들면 세션·암호화 값이 무효가 될 수 있다 — `.env` 는 반드시 번들에서 복원 |
| 14 | **새 서버에 push 가 먼저 필요 / `db.sqlite3` 추적** | `apps/syncops` 와 SSO 변경이 커밋·push 되어야 새 서버가 clone 으로 받는다. `db.sqlite3` 는 git 추적 파일이라 `git add -A`·`git commit -a` 금지, 커밋 전 `git status` 로 `*.incoming`·`*.bak-*`·`snapshots/`·`db.sqlite3` 확인(`.gitignore` 는 부산물만 막음). 커밋·push 는 메인/사용자가 한다 |
| 15 | DB 는 암호화되지 않음 | 번들 DB 에 세션·해시 포함. 외부 매체(S3/USB) 전송 시 별도 암호화. `--encrypt-db` 미구현(§8) |
| 16 | 오프라인 번들 출처 인증 없음 | sha256 무결성만. HMAC 서명 미구현 → 신뢰 경로로만 전달·수신(§8) |
| 17 | verify 는 행 수·마이그레이션만 비교 | 내용 비교(`--deep`) 미구현. 내용 무결성은 import 단계 sha256 이 보증 |
| 18 | AI 가 패스프레이즈를 다룰 수 없음 | 호출 간 환경변수 비유지·값 노출 금지 → 패스프레이즈 단계는 사용자 터미널(§7.5). 평문 config 는 사용자 결정 사항 |
| 19 | 같은 호스트 가드 | 원본에 원본 번들 import 거부(`--allow-same-host`). `manifest.db.source_path` 는 신규 필드(없는 옛 번들은 hostname+base_dir 만 비교) |
| 20 | 경로 C(Export API) 한계 | ① 원본에 Export API 코드가 배포돼야 한다(미배포 시 404). ② **config(`.env`·secrets·crontab·systemd)는 API 로 오지 않는다** — 별도 채널 필요(§5.5). ③ 원본 쓰기 중 backup 이 느려지거나 **재시작이 반복돼 `--max-backup-seconds`(기본 1800s) 초과로 실패**할 수 있다(실제 발생: 2026-10-09, 원본 ≈수 GB·journal_mode=delete, 58회 재시작). 대책 = **원본 서비스·cron 을 멈춘 채 원본 셸에서 `manage.py export_snapshot --api-bundle --compress gzip`** 으로 `snapshots/api-<uuid>/`(state.json=ready, config 제외)를 만든 뒤 서비스를 올리고 새 서버에서 `fetch_snapshot_via_api --existing-id <uuid>`(API 가 그대로 내려줌)로 받는다(§9, 컷오버는 원본 정지 후). ④ `export:read` 키는 admin 소유·명시 스코프 — 사용 후 즉시 회수. ⑤ 번들 DB 는 암호화되지 않는다(TLS 로 전송 구간만 보호) |

## 13. 실제 이관 기록 (2026-10-09 ~ 10-12, AWS `i-02e4ef36a0fa8f4d0` → IDC `wx-qms221`)

경로 C(HTTPS Export API)로 수행. 결과·교훈:

- **번들**: `qg-20261009T135844Z` (원본 DB 5.1GB → gzip 867.5MB, 55개 테이블·153,055행, 마이그레이션 113건). 리허설(임시 DB 복원 25초, `verify_snapshot` OK) 후 `import_snapshot --force --yes` 로 실서비스 교체, 기존 DB 는 `db.sqlite3.pre-import-<ts>` 로 보존, `migrate` 적용 대상 없음.
- **API 내부 백업이 실패한 원인**: 원본 운영 DB(`journal_mode=delete`)에 **웹 서비스 + cron 3개(`* * * * *`)가 계속 쓰기** → sqlite backup 이 58회 재시작되며 1800s 초과(§12 #20 ③). 해결 = 서비스·cron 을 멈춘 채 원본 셸에서 `export_snapshot --api-bundle --compress gzip` 으로 `snapshots/api-<uuid>/` 를 만들고, 서비스만 올려 `fetch_snapshot_via_api --existing-id <uuid>` 로 받음. (cron 은 `crontab -l > 백업; crontab -r` 로 비우고 사후 복원 가능. **서비스를 멈추면 API 도 멈추므로 번들 생성 후 서비스는 올려야 한다.**)
- **받는 쪽 주의**: 긴 명령은 웹 터미널에서 줄바꿈되어 인자가 잘린다 → `~/qg-fetch.sh <id>`(키는 `read -rs` 로 스크립트가 직접 받음) 같은 짧은 래퍼 사용. 키를 프롬프트가 아닌 셸에 붙여넣으면 bash 기록에 남는다.
- **새 서버 구성**: 서비스 `qualitygate`(8601, `runserver --noreload`) + `qg-slack-triage`(deploy/ 유닛의 경로만 치환) + `/etc/cron.d/qualitygate`(`sweep_reliability`·`jenkins_keepalive` 매분). 사용자 crontab 은 `cron.allow` 때문에 쓸 수 없어 `/etc/cron.d` 사용. `.env`/secrets 는 API 로 오지 않아 이 서버의 기존 `.env` 유지.
- **외부 접속**: IDC 아웃바운드는 인프라 요청 후 Slack(`slack.com`·`wss-primary/backup.slack.com`)·Google 허용. 요청 전에는 Slack 전송 실패·triage 크래시 루프(859회 재시작) 발생.
- **Google Sheets 담당자 동기화는 폐기**: 인증이 AWS 전용 WIF(`credential_source.environment_id=aws1`, IMDS 필요)라 IDC 에서 불가했고, 조직 정책(`iam.managed.disableServiceAccountKeyCreation`)이 서비스 계정 키 발급도 막았다. 담당자 마스터는 **자체 DB 에서 관리(화면 편집·CSV 업로드)** — `.env` 의 `GOOGLE_CREDENTIALS_FILE`·`GSHEET_OWNERS_SPREADSHEET_ID` 비움, `GSHEET_OWNERS_WRITEBACK=0`, 자동 동기화 주기 0, `sync_api_owners_from_sheet` cron 미설치. (담당자 화면의 "Google Sheets 동기화" 버튼·"시트가 원본" 안내 문구는 남아 있음 — 후속 정리 대상.)
- **정리**: 이관용 API 키 3개(`qg_sync`·`qa_sync_2`·`sync_api`) 회수, `QGP_BASE_URL=https://qw.jkintra.net/qg`, 원본 서비스·cron 정지(인스턴스는 안정화 전까지 삭제 금지). 새 서버 `db.sqlite3.pre-import-*`·`snapshots/qg-*`·`.env.bak-*` 는 안정화 후 삭제.
- **미검증**: 브라우저 로그인·과거 Run 표시, Slack 실제 FAIL 알림·triage 버튼, 원본에서 번들 시점 이후 쌓인 쓰기(있다면 유실).
