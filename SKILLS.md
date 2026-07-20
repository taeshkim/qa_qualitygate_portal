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
```bash
./run.sh            # macOS/Linux : origin/master ff 자동동기화 → venv → pip → migrate → runserver:8601
./run.ps1           # Windows
```
- `run.*` 는 기동 시 **`origin/master` 를 ff-only 로 당겨 코드만 최신화**한다. **`db.sqlite3` 는 절대 건드리지 않는다** (원격이 db 를 바꾼 커밋이면 ff 가 막혀 그 회차 동기화는 건너뜀 — DB 갱신은 수동 명령으로만).
- 빠른 헬스체크: `timeout 15 ./run.sh` 후 `curl -s http://localhost:8601/health/` → `{"status":"ok"}`.

---

## 2. 자주 쓰는 명령어 (`.venv/bin/python manage.py …`)

```bash
.venv/bin/python manage.py migrate                 # 마이그레이션 적용
.venv/bin/python manage.py makemigrations <app>    # 모델 변경 후
.venv/bin/python manage.py seed                    # 시드 데이터
.venv/bin/python manage.py test apps.integrations  # 앱 단위 테스트(빠름)
.venv/bin/python manage.py test                    # 전체 테스트
.venv/bin/python manage.py sync_api_owners_from_sheet           # 담당자 시트 수동 동기화
.venv/bin/python manage.py sync_api_owners_from_sheet --if-due  # 주기 도래 시에만 (매분 cron)
DJANGO_SUPERUSER_PASSWORD=admin123 .venv/bin/python manage.py createsuperuser --noinput --username admin --email admin@example.com
```

### 로컬 API 점검 (curl, admin:admin123)
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
- **알림/담당자 매칭** → CLAUDE.md 의 **회귀 6 시나리오** + `.venv/bin/python manage.py test apps.integrations apps.notifications`.
- **상태 전이 로직** → `apps.qualitygates` 테스트 + `maintenance.md` §2 다이어그램 갱신 확인.
- **API Key 관련** → 평문이 로그/응답/테스트/`print` 어디에도 안 남는지 확인.

---

## 5. 병렬 에이전트

독립 작업은 **기본적으로 병렬 서브에이전트**로 처리한다. 에이전트 목록·디스패치 패턴·예외(상태기계 등 단독 처리)는 [`AGENTS.md`](./AGENTS.md) §1~§2 참조.

---

## 6. 이 문서의 갱신 규칙

- 새 manage.py 명령·curl 점검 패턴·환경 변화(포트/파이썬/설정)·쓰게 된 스킬이 생기면 이 문서를 갱신한다.
- 매 작업 턴 마무리(문서 동기화) 단계에서 `docs-keeper` 가 점검.
