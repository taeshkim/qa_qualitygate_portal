# SSO 연동 인수인계 (새 서버에서 이어서 작업하기)

> **(2026-10 서버 이관 완료 — 이력 문서)** 이 문서의 옛 주소 `https://qualitygate.jkintra.net`(AWS 원본·ALB)은 이관 완료로 **종료**됐다(502). 현재 접속은 게이트웨이 `https://qw.jkintra.net/qg/`, 프로그램 호출(API Key)은 `https://qw.jkintra.net/qg/ci-api/` 이다. 아래 서술은 작성 당시(날짜 맥락) 기준이다.

> **AI 에게 이렇게 말하세요**
>
> ```
> docs/handoff_sso_integration.md 보고 작업 이어서 해줘
> ```
>
> 먼저 `docs/prd_server_migration_sync.md` 로 데이터 싱크가 끝난 새 서버(IDC 게이트웨이 서버)여야 한다. 싱크가 안 됐으면 "docs/prd_server_migration_sync.md 보고 싱크해줘" 를 먼저 말한다. (싱크의 패스프레이즈 단계·원본 서버 정지/보관은 사용자가 자기 터미널에서 직접 수행한다 — PRD §7.5, §6.3.)

| 항목 | 값 |
|---|---|
| 작성 시점 | 2026-10-09 |
| 작성 근거 커밋 | `c381940` (feat(accounts): 게이트웨이 헤더로 바로 로그인) 위 — 직전 `6f6fe37` (Keycloak OIDC SSO). 2026-10-09 후속 변경(`GATEWAY_ENTRY_ENABLED` 진입 모드, 로그아웃 HTML 이동)은 `apps/accounts/sso.py`·`urls.py`·`config/settings/base.py`·`templates/accounts/login.html` — 커밋은 메인이 처리 |
| 현재 상태 한 줄 | (2026-10-09 갱신) 코드·테스트 완료(184개 OK). 이 호스트(wx-qms221)에서 **#3 완료**(게이트웨이 출발지 `172.20.0.222` 실측·`.env` 고정), **#4 헤더 로그인으로 대시보드 진입을 사용자 브라우저에서 확인**. **#6 로그아웃은 302→HTML 로 고친 뒤 재확인 대기**(404 사고). 새로 **§10 게이트웨이 진입 로그인**(앱 로그인 화면 + Keycloak 버튼) 구현 — 켜려면 게이트웨이 라우팅 변경(QA 인프라팀 요청)이 선행돼야 한다 |
| 사용자 선호 | 한국어, 짧은 "원인 → 조치" 포맷. 답답해하는 상태이므로 **확인 안 된 것을 된 것처럼 말하지 않는다** |

비밀값(.env·토큰·client secret)은 이 문서에도, 로그에도, 응답에도 쓰지 않는다. 파일명·키 이름만 다룬다.

---

## 1. 목표 (사용자 요구 원문 취지)

Quality Gate 에 Keycloak 을 연동한다.

- 로그인 페이지는 **Keycloak 전용**으로 새로 만들고, 그 계정으로 로그인한다. 로그아웃하면 **새로 만든 로그인 페이지로 돌아온다**.
- 다른 서비스들처럼 **`.env` 에 Keycloak client secret 을 넣지 않는다.** 게이트웨이(`qw.jkintra.net`, oauth2-proxy)가 Keycloak 로그인을 대신하고, 앱은 게이트웨이가 붙여 주는 `X-Auth-*` 헤더를 신뢰한다 (`docs/service-sso-migration.md` §4 의 헤더 방식).
- 사용자 요구 6항 (원문은 Notion Mirror 용이었으나 이 앱은 Quality Gate 에 적용):
  1. 자체 로그인 제거 — `LOCAL_LOGIN_ENABLED` 비상 플래그만 남기고 **기본 꺼짐**
  2. `X-Auth-Realm` 이 있으면 로그인 화면 없이 자동 로그인
  3. 세션 사용자 ≠ 헤더 이메일이면 세션 폐기 후 재로그인
  4. 로그아웃은 `https://qw.jkintra.net/_qw/logout`
  5. `TRUSTED_PROXY_CIDRS` 밖의 `X-Auth-*` 제거 + 운영에서 Nginx 피어 `/32` 로 좁히는 절차를 문서화
  6. fetch/API 401 이면 새로고침
- **게이트웨이 설정은 바꾸지 않는다** (QA 인프라팀 소유).
- `qualitygate.jkintra.net`(ALB 직결)은 **이관 전 임시 접속 주소**로만 남기고, 최종 접속 주소는 `https://qw.jkintra.net/qg/` 로 일원화한다 — 전환 검증(§4 단계 4~8) 후 `qualitygate.jkintra.net` 을 **폐기**한다 (`docs/prd_server_migration_sync.md` §6.3(7)·§11-2 와 같은 순서). 게이트웨이가 `/qg/` 접두를 제거해 백엔드 `8601` 로 프록시하고, 로그인 방식은 `internal`(사내 world realm)이다.

---

## 2. 구현 상태 (코드는 완료, 검증은 미완)

### 2.1 커밋별 내용

| 커밋 | 내용 |
|---|---|
| `6f6fe37` | 앱 직접 OIDC (Authlib) — `apps/accounts/sso.py`, `ExternalIdentity` 모델 + 마이그레이션 `0003_externalidentity`, SSO 전용 로그인 페이지(`templates/accounts/login.html`), 로그아웃 시 Keycloak end-session 옵션(`OIDC_INTERNAL_POST_LOGOUT_REDIRECT`), `ProxyBoundaryMiddleware`, 프런트 401 새로고침(`static/js/qg_notify.js`), 테스트 `apps/accounts/test_sso.py` (116 케이스). **`OIDC_INTERNAL_*` 가 `.env` 에 없으면 이 경로는 비활성** |
| `c381940` | **게이트웨이 헤더 로그인** — `login_by_gateway_header` + `GatewaySessionMiddleware`. 헤더 이메일로 `User` 조회, 없으면 viewer 로 즉시 활성 생성, 비활성·모호(여러 명 일치)는 403. 기본값 `TRUSTED_PROXY_CIDRS=172.20.0.222/32`, `GATEWAY_LOGOUT_URL=https://qw.jkintra.net/_qw/logout` |
| (미커밋) | `ProxyBoundaryMiddleware` 가 신뢰 밖 출발지의 `X-Auth-*` 를 제거할 때 **peer IP 를 WARNING 로그**로 남기는 진단 (§3.3). 이관 작업 커밋에 포함될 예정 — 새 서버에서 `git log` 로 반영 여부 확인 |

> 같은 이관 작업 커밋에서 `docs/qw-sso-gateway.md`, `docs/service-sso-migration.md`(현재 git 미추적)도 함께 `git add` 될 예정이다. 새 서버에서 두 파일이 없으면 이관 커밋이 아직 push 되지 않은 것이다.

### 2.2 요구 6항 대응표

| 요구 | 구현 위치 | 상태 |
|---|---|---|
| 1 자체 로그인 기본 꺼짐 | `config/settings/base.py:307` `LOCAL_LOGIN_ENABLED`(기본 False), `:312` `GATEWAY_ENTRY_ENABLED`(기본 False), `:313` `LOCAL_LOGIN_ACTIVE = LOCAL_LOGIN_ENABLED or not (OIDC_PROVIDERS or GATEWAY_ENTRY_ENABLED)` | **플래그로 구현, 아직 미적용** — `GATEWAY_ENTRY_ENABLED` 를 켜야 꺼진다(§4 단계 9, §10). 게이트웨이 라우팅 검증 전에는 켜지 않는다(락아웃) |
| 2 헤더면 자동 로그인 | `apps/accounts/middleware.py:111` `GatewaySessionMiddleware` → `apps/accounts/sso.py:373` `login_by_gateway_header`. 헤더가 world realm 이어야 함 | 코드·테스트 완료, **이 호스트에서 헤더 로그인 성공 + 대시보드 진입 확인**(사용자 브라우저, 2026-10-09) |
| 3 세션≠헤더 → 폐기 | `GatewaySessionMiddleware.__call__`/`_matches` (`User.email` 또는 `ExternalIdentity.email` 과 비교) | 코드·테스트 완료, 실검증 못 함 |
| 4 로그아웃 | `apps/accounts/sso.py:255` `logout_view` — 게이트웨이 뒤 + `GATEWAY_LOGOUT_URL` 이면 앱 세션 종료 후 `_gateway_logout_page`(`:218`, **302 가 아니라 HTML**)로 이동. 진입 모드면 헤더 없이도 게이트웨이 로그아웃을 fetch 로 호출한 뒤 로그인 화면으로 복귀 | 코드·테스트 완료. 302 는 게이트웨이의 `Location` 재작성(추정)으로 `/qg/_qw/logout` 404 가 났다 → **HTML 로 수정, 재확인 대기** |
| 5 신뢰 경계 | `apps/accounts/middleware.py:69` `ProxyBoundaryMiddleware` (`base.py:73`, 맨 앞) — `REMOTE_ADDR` 가 `TRUSTED_PROXY_CIDRS` 밖이면 `HTTP_X_AUTH_*` 전부 삭제 | 코드 완료. **출발지 실측 확정**: `172.20.0.222`(헤더 로그인 성공·제거 경고 0건), `.env` 에 `/32` 로 명시 고정 — §5 |
| 6 401 새로고침 | `static/js/qg_notify.js:27-58` — 같은 출처 fetch / htmx `responseError` 가 401 이면 새로고침, 10초 내 재시도 방지(sessionStorage `qg-401-reload-at`) | 코드·테스트 완료, 실검증 못 함 |

흐름 다이어그램과 설정 플래그 표는 `maintenance.md` §7 "SSO 로그인" 절에 있다 (이 문서에 복사하지 않음).

### 2.3 알려진 문서 불일치 (이어서 작업하며 바로잡을 것)

- ~~`maintenance.md` §7 설정 표와 `README.md` 환경변수 절의 `TRUSTED_PROXY_CIDRS`·`GATEWAY_LOGOUT_URL` 기본값 "빈 값"~~ → **해결(2026-10-09)**: 코드 기본값 `172.20.0.222/32`·`https://qw.jkintra.net/_qw/logout`(`base.py:317`, `:320`)으로 두 문서를 정정했다.
- ~~`maintenance.md` §7 의 라인 참조가 오래됨~~ → **해결(2026-10-09)**: `sso.py`·`middleware.py`·`base.py` 참조를 현재 코드에 맞춰 갱신(`ProxyBoundaryMiddleware` `:69`, `GatewaySessionMiddleware` `:111`, `RolePermissionMiddleware` `:144`). 코드가 바뀌면 `grep -n` 으로 다시 확인한다.
- ~~**`.env.example` 의 `TRUSTED_PROXY_CIDRS=`·`GATEWAY_LOGOUT_URL=` 빈 값**~~ → **해결(2026-10-09)**: 두 줄을 주석 처리하고 코드 기본값·경고 주석을 달았다. 원인(남겨 둠): `os.getenv` 는 키가 존재하면 빈 문자열을 그대로 돌려주므로(코드 기본값이 적용되지 않음) `.env.example` 을 그대로 복사한 `.env` 에서는 신뢰 프록시가 없어 게이트웨이 헤더가 항상 제거된다. 이관 시 복원되는 운영 `.env` 에는 이 두 키가 없어(키 존재 여부만 확인, 2026-10-09) 코드 기본값이 쓰인다 — 새 서버에서 `.env` 를 새로 만들었다면 두 키를 지우거나 값을 채운다.
- (2026-10-15 현행화로 `README.md`/`maintenance.md`/`intro.md` 는 ALB 직결 종료로 정정됨 — 아래는 당시 기록) `maintenance.md`/`README.md` 의 "진입 경로 공존 / `qualitygate.jkintra.net` ALB 직결" 서술은 이관 후 폐기 방침과 맞지 않는다(이관 전에는 임시 접속 주소로 유효). 앱 직접 OIDC 코드는 지우지 않았다(`OIDC_INTERNAL_*` 가 없으면 휴면). 이 코드를 "게이트웨이 헤더 실패 시 대비"로 남길지 "안 쓰는/레거시 코드"로 옮길지는 사용자 결정 사항 — 임의로 지우지 말고 물어본다.

---

## 3. 새 서버에서 이어갈 때의 주의와 교훈

### 3.1 환경 / 재시작

1. **프로세스 환경변수가 `.env` 를 이긴다.** `runserver` 부모가 시작 때 `load_dotenv` 로 `.env` 를 `os.environ` 에 올리고 autoreload 자식이 그걸 상속한다. `.env` 를 고쳐도 **전체 재시작 전에는 반영되지 않는다** (`DJANGO_ALLOWED_HOSTS` 를 고쳤는데 400 `DisallowedHost` 가 계속 나서 겪음).
2. **`run.sh` 가 종료코드 0 으로 끝나면 systemd(`Restart=on-failure`)가 재시작하지 않는다.** 프로세스를 `kill` 하면 서비스가 `inactive` 로 남는다 (504/중단 사고의 원인이었음). 재시작은 항상 `sudo systemctl restart qualitygate`. 유닛은 `ExecStart=/bin/bash <repo>/run.sh`, 포트 `8601`. (작성 시점에 이 호스트의 `qualitygate` 서비스는 `inactive` 였다.) `deploy/qualitygate.service` 는 설치본(`/etc/systemd/system/qualitygate.service`)과 내용이 다른 별개 버전이므로 새 서버에 어느 쪽을 설치했는지 `systemctl cat qualitygate` 로 확인한다 (PRD §6.3(5) 기준 새 서버는 `run.sh` 가 아닌 `deploy/` 템플릿 방식이 기본 — 이 경우 위 `run.sh` 종료코드 문제 대신 유닛의 `Restart=` 설정을 `systemctl cat qualitygate` 로 확인한다).
3. **하위 경로 서비스 설정** — 앱이 `/qg` 아래에서 서비스되려면 `.env` 에 `QGP_SCRIPT_NAME=/qg`, `DJANGO_ALLOWED_HOSTS` 에 `qw.jkintra.net`, `DJANGO_CSRF_TRUSTED_ORIGINS` 에 `https://qw.jkintra.net` 이 있어야 한다. 이 호스트의 `.env` 는 호스트·CSRF 값은 반영돼 있었으나 **작성 시점에 `QGP_SCRIPT_NAME` 키가 `.env` 에 없었다** (키 존재 여부만 확인; 2026-10-09 재확인 시 값이 채워져 있다). 이 호스트는 2026-10-09 `DJANGO_CSRF_TRUSTED_ORIGINS` 에 `https://qw.jkintra.net` 을 추가했다. 새 서버에서 `grep -c QGP_SCRIPT_NAME .env` 로 확인하고, 없으면 `/qg` 로 추가 후 **전체 재시작**. `.env` 는 config 번들로 이관되지만 호스트 목록의 `10.44.35.151`(옛 AWS 주소)은 정리하고, `qualitygate.jkintra.net`(임시 접속 주소)은 전환 검증 동안 유지했다가 폐기 시 함께 제거한다.
4. **게이트웨이가 같은 서버라면** 업스트림은 `host.docker.internal:8601` 그대로 두고, `TRUSTED_PROXY_CIDRS` 는 **앱이 실제로 보는 Nginx 컨테이너 출발지 `/32`** 로 한다 (§3.3 로그로 확인). 게이트웨이가 2번 호스트의 앱이면 Docker 네트워크 주소다. 기본값 `172.20.0.222/32` 는 이 호스트(게이트웨이는 별도 2번 서버 wx-qms222)에서 **2026-10-09 실측으로 확정**됐다 — 다른 새 서버로 옮기면 다시 §3.3 로그로 실측한다.
5. **상태코드 해석**: `400` = `DJANGO_ALLOWED_HOSTS` 에 호스트 없음 / `504` = 게이트웨이 → 백엔드 네트워크 문제(앱 로그에 요청이 안 찍힘) / **로그인 폼이 보이면** 요청이 이 앱의 예전 코드이거나 다른 서버로 간 것(게이트웨이 경로면 헤더 로그인이 되어 폼이 안 보여야 정상) / `403` 로그인 화면(`sso_inactive`·`sso_failed`) = 게이트웨이 헤더는 왔지만 계정이 비활성이거나 이메일이 여러 계정과 일치.
6. 서버가 `DJANGO_DEBUG=True` 로 운영 중이다 (그래서 `LOCAL_LOGIN_ACTIVE` 를 DEBUG 로 판단하지 않는다 — `base.py:304`). 이 호스트 서비스는 systemd `Restart=always`, `runserver --noreload`(2026-10-09 확인).
7. (이 호스트는 2026-10-09 `accounts.0003_externalidentity`·`integrations.0009_ui_automation_owners` 적용 완료, 사전 백업 `snapshots/db-pre-sso-<ts>.sqlite3`. 다른 새 서버에는 아래 그대로 적용.) DB 가 바뀌었으므로 **accounts 마이그레이션 `0003_externalidentity` 가 운영 DB 에 아직 적용되지 않았을 수 있다**(작성 시점 `django_migrations` 의 accounts 는 `0001`, `0002` 뿐). 싱크 직후 `manage.py migrate` 로 적용한다. `import_snapshot` 이 기본으로 migrate 를 이어서 실행하므로 보통 자동이지만 `--skip-migrate` 를 썼다면 수동으로 한다. 적용 후 `showmigrations accounts` 로 `[X] 0003` 확인.

### 3.2 실제 연동을 끝내 검증 못 한 이유

AWS(10.44.35.151) 서버는 `qw` 게이트웨이가 접근하지 못해 **504** 였고, 앱 로그에 **게이트웨이 요청이 한 건도 없었다**(보안그룹/라우팅). 그래서 다음은 모두 미확인이었다(**2026-10-09 이 호스트에서 (a)는 확정, (b)는 헤더 로그인만 확인 — 로그아웃 왕복은 재확인 대기, (c)는 §10 플래그로 구현했으나 켜기 전, (d)는 적용 완료**).

- (a) 게이트웨이의 실제 출발지 IP (→ `TRUSTED_PROXY_CIDRS` /32 미확정)
- (b) 헤더 로그인·로그아웃 왕복
- (c) 요구 1 "자체 로그인 기본 꺼짐" 은 **락아웃 위험 때문에 검증 전에는 보류**했다 (OIDC 값이 없으면 자체 로그인이 유지되는 현재 로직이 안전망)
- (d) 운영 DB 에 accounts `0003` 미적용 (§3.1-7)
- (e) 기존 계정 이메일 정리 (§6)

### 3.3 출발지 진단 로그 (미커밋 변경)

신뢰 밖 출발지의 `X-Auth-*` 헤더를 제거할 때 아래 WARNING 이 남는다 (값은 남기지 않고 헤더 **이름**과 peer IP 만).

```
신뢰 밖 출발지의 X-Auth-* 헤더 제거 peer=<IP> headers=<이름,…> — 게이트웨이 출발지면 TRUSTED_PROXY_CIDRS 에 추가
```

확인: `journalctl -u qualitygate | grep "X-Auth-\* 헤더 제거"` (또는 `server.log`). **게이트웨이를 통해 접속했는데 이 로그가 나오면 그 `peer=` 가 게이트웨이 출발지**이므로 `TRUSTED_PROXY_CIDRS=<peer>/32` 로 설정한다. 이 로그가 새 서버 코드에 없으면(커밋 미반영) `apps/accounts/middleware.py` 의 `ProxyBoundaryMiddleware.__call__` 에 같은 진단을 넣는다.

---

## 4. 이어서 할 일 체크리스트 (순서대로, 각 항목에 완료 기준)

각 단계는 **직전 단계 완료 기준을 충족한 뒤에** 진행한다. 확인 못 한 단계는 "미확인" 으로 보고하고 넘어가지 않는다.

| # | 할 일 | 완료 기준 | 상태 |
|---|---|---|---|
| 1 | 새 서버 기동 + 데이터 싱크 (`docs/prd_server_migration_sync.md`) | `verify_snapshot <번들> --allow-newer` 종료코드 0, `sudo systemctl status qualitygate` 가 active, `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8601/health/` 가 200 | 진행 (이 호스트 반영 이력은 §3.1) |
| 2 | `manage.py migrate` | `showmigrations accounts` 에 `[X] 0003_externalidentity`. `.env` 에 `QGP_SCRIPT_NAME=/qg`·호스트·CSRF 값 확인(§3.1-3), 정리 후 `sudo systemctl restart qualitygate` | 진행 (migrate·CSRF 추가·재시작 완료) |
| 3 | `TRUSTED_PROXY_CIDRS` /32 확정 | 게이트웨이로 한 번 접속해 §3.3 로그의 `peer=` 확인 → `.env` 에 `TRUSTED_PROXY_CIDRS=<peer>/32` → 전체 재시작 → 같은 접속에서 제거 로그가 더 이상 안 남 (§5 절차) | **완료** (2026-10-09 `172.20.0.222/32`, `.env` 고정) |
| 4 | `https://qw.jkintra.net/qg/` 첫 접속 | 게이트웨이 Keycloak 로그인 → **앱 로그인 폼을 거치지 않고** 바로 앱 진입. 앱 로그에 요청이 찍힘 (504 면 네트워크: 게이트웨이→8601 도달 여부부터) | **확인됨** (헤더 로그인으로 대시보드 진입 — 사용자 브라우저) |
| 5 | 대시보드 | 로그인 직후 `/qg/` 대시보드가 렌더되고 사이드바 사용자명이 본인 계정, CSS/JS 가 `/qg/static/...` 로 정상 로드 | #4 와 함께 확인됨(사용자 브라우저) |
| 6 | 로그아웃 | 사이드바 로그아웃 → 앱 세션 종료 → `https://qw.jkintra.net/_qw/logout` → 게이트웨이/Keycloak 세션 종료 → 로그인 화면으로 복귀. 이어서 `/qg/` 재접속 시 다시 Keycloak 로그인을 요구하고, 같은 PC 에서 다른 계정으로 들어가면 그 계정으로 로그인됨 | **재확인 대기** — 302→HTML 수정 후 |
| 7 | 위조 헤더 차단 테스트 (§7-A) | 게이트웨이를 거치지 않고(백엔드 직접) 보낸 `X-Auth-Request-Email`/`X-Auth-Realm` 헤더가 무시되고 로그인 안 됨 + §3.3 경고 로그 확인 | 대기 |
| 8 | 세션 불일치 + 401 새로고침 테스트 (§7-B, §7-C) | 세션≠헤더 → 세션 폐기 후 헤더 사용자로 재로그인. 401 응답 시 페이지가 1회 새로고침되고 10초 내 반복 안 함 | 대기 |
| 9 | **자체 로그인 기본 꺼짐 전환** | `GATEWAY_ENTRY_ENABLED=True` 로 구현됨(코드 기본값 변경 아님 — `LOCAL_LOGIN_ACTIVE = LOCAL_LOGIN_ENABLED or not (OIDC_PROVIDERS or GATEWAY_ENTRY_ENABLED)`, `base.py:313`). **게이트웨이 진입 경로 라우팅이 적용되고 단계 4~8 이 실서버에서 통과한 뒤에만 켠다**(§10 순서). 켠 뒤 `/accounts/login/` POST 는 403 `local_disabled`, `signup` 404, `/admin/login/` 은 앱 로그인으로 리다이렉트. **비상 플래그** `LOCAL_LOGIN_ENABLED=True` + 재시작으로 다시 열리는지 확인(복구 절차는 §10 "롤백" 과 `maintenance.md` §7). 플래그 없을 때 기존 동작(OIDC 없으면 자체 로그인 유지)은 그대로라 `LocalLoginClosedTests`·`OidcSettingsTests` 기대값은 바꾸지 않았다 | 대기 (§10 선행) |
| 10 | 계정 이메일 정리 (§6) | 이메일 없음/비-worxphere.ai 계정이 정리되었거나, 첫 SSO 로그인 때 viewer 가 새로 생겨도 된다는 사용자 결정을 받음 | 대기 |
| 11 | 문서 갱신 | `README.md`(SSO 항목·환경변수·접속 주소), `maintenance.md` §7·§8(§2.3 불일치 포함), `SKILLS.md`(검증 명령) 갱신 | 대기 |
| 12 | 커밋 · push | CLAUDE.md 절차: **테스트 → 문서 → 커밋 → push** (커밋 메시지 `feat:`/`fix:`/`docs:` 스타일, 커밋·push 는 메인이) | 대기 |

완료 기준 중 외부 시스템(게이트웨이/Keycloak)이 필요한 단계 4~8 은 자동 검증이 안 되므로 **사용자에게 브라우저 확인을 요청**하고 OK 응답을 받은 뒤 진행한다 (CLAUDE.md "작업 종료 후" 규칙).

---

## 5. TRUSTED_PROXY_CIDRS 를 Nginx 피어 /32 로 좁히는 절차 (요구 5)

목적: 게이트웨이(Nginx)가 아닌 곳에서 온 요청이 `X-Auth-*` 로 다른 사람 행세를 하거나 자동 로그인을 유도하지 못하게 한다. **넓은 대역(`/16` 등)으로 두지 말고 Nginx 한 대의 `/32`** 로 좁힌다.

1. 게이트웨이를 통해 `https://qw.jkintra.net/qg/` 에 한 번 접속한다 (이때 `TRUSTED_PROXY_CIDRS` 가 틀려 있으면 자동 로그인이 안 되고 로그인 화면이 보인다 — 정상).
2. 앱 로그에서 `peer=` 를 읽는다: `journalctl -u qualitygate --since "5 min ago" | grep "X-Auth-\* 헤더 제거"`. 여러 IP 가 보이면 게이트웨이가 아닌 요청(사내 스캐너 등)이 섞인 것이므로, **게이트웨이 접속 시각과 일치하는 한 건**만 고른다.
3. `.env` 의 `TRUSTED_PROXY_CIDRS=<peer>/32` 로 설정한다 (게이트웨이 컨테이너가 재시작으로 IP 가 바뀔 수 있는 구성이면 Docker 네트워크 고정 IP 여부를 인프라팀에 확인 — 바뀌면 이 값도 바뀌어야 한다).
4. **전체 재시작**(`sudo systemctl restart qualitygate`, §3.1-1) 후 같은 접속을 반복해 제거 로그가 없고 자동 로그인이 되는지 확인한다.
5. 헤더 위조 테스트(§7-A)로 신뢰 밖 출발지에서는 로그인이 안 되는지 확인한다.

`172.20.0.222/32` 는 `docs/service-sso-migration.md` 예시의 "2번 Nginx" 출발지로 코드 기본값에 들어 있고, **2026-10-09 이 호스트(wx-qms221)에서 실측으로 확정**됐다(헤더 로그인 성공, 제거 경고 로그 0건, `.env` 에 `TRUSTED_PROXY_CIDRS=172.20.0.222/32` 명시 고정 — 기본값과 동일). 게이트웨이와 같은 서버로 옮기는 등 구성이 바뀌면 값이 달라지므로 그때마다 §3.3 로그로 다시 실측한다.

---

## 6. 계정 이메일 정리 (이관 전후)

게이트웨이 헤더 로그인은 `User.email` 이 헤더 이메일과 **정확히 1명** 일치할 때 그 계정(역할 유지)으로 들어간다. 일치하는 계정이 없으면 **viewer 계정이 새로 생긴다**(기존 권한 계정과 별개). 그래서 기존 계정 이메일이 실제 SSO 이메일과 다르면 그 사람은 첫 로그인에 권한 없는 viewer 로 들어간다.

작성 시점(2026-10-09) 운영 DB 읽기 전용 조회 결과 — 전체 22 계정(이메일 전문은 기재하지 않음):

| 조건 | 계정 아이디 | 영향 |
|---|---|---|
| 이메일 비어 있음 | `ls-b2f538` (역할 `qpe_ops`) | 이메일 자동 연결 불가 → 첫 SSO 로그인 때 viewer 신규 생성 |
| `worxphere.ai` 도메인이 아닌 이메일 | `admin`, `test` (둘 다 `qpe_admin`) | 실제 사내 SSO 이메일과 다를 가능성 — 사용자에게 어떤 계정인지 확인 |
| `worxphere.ai` 도메인이지만 사람 계정이 아닐 수 있음 | `tester`, `test01`~`test04`, `qatest01` | 테스트용이면 비활성/삭제 여부를 사용자에게 확인 |
| 같은 이메일을 쓰는 계정 | 없음(중복 0) | 모호(403) 가능성 없음 |

> 위 표는 시점 스냅샷이다. 새 서버에서 다시 확인한다 (읽기 전용 · 이메일 값은 출력하지 않음):
>
> ```bash
> .venv/bin/python - <<'E'
> import sqlite3
> c = sqlite3.connect("file:db.sqlite3?mode=ro", uri=True)
> for u, r, a, e in c.execute("select username, role, is_active, email from accounts_user order by username"):
>     e = (e or "").strip().lower()
>     print(u, r, a, "no-email" if not e else ("worxphere.ai" if e.endswith("@worxphere.ai") else "other-domain"))
> E
> ```

정리 방법 (사용자 승인 후, 운영 DB 쓰기이므로 항상 사용자 확인): Admin 화면 사용자 관리(`/accounts/users/`)에서 이메일을 실제 사내 이메일로 수정. 이미 viewer 가 자동 생성된 사람은 기존 권한 계정의 이메일을 맞춘 뒤 자동 생성된 viewer 를 비활성화/삭제한다 (둘이 같은 이메일이면 모호 → 403 이 되므로 반드시 하나만 남긴다).

---

## 7. 검증 시나리오 상세

기본 준비: `.env` 값은 건드리지 않고 `curl` 에 헤더를 직접 준다. **로컬 포트 8601 로 직접 보내는 요청의 출발지는 `127.0.0.1` 이므로 `TRUSTED_PROXY_CIDRS` 밖** — 이를 이용해 신뢰 밖 출발지를 흉내 낸다. 신뢰 안 출발지 흉내는 임시로 `TRUSTED_PROXY_CIDRS` 에 `127.0.0.1/32` 를 넣어 재시작해 확인하되 **확인 후 반드시 원복**한다 (또는 Django 테스트 클라이언트의 `REMOTE_ADDR` 사용).

- **A. 위조 헤더 차단**: `curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -H 'X-Auth-Request-Email: <기존 사용자 이메일>' -H 'X-Auth-Realm: world' http://127.0.0.1:8601/` (게이트웨이가 접두를 제거해 백엔드로 보내는 경로와 같은 `/` 로 호출한다 — `/qg/` 를 붙이지 않는다) → 로그인 화면으로 리다이렉트(로그인 안 됨)여야 한다. 앱 로그에 제거 경고(§3.3)가 남아야 한다.
- **B. 세션 불일치**: 게이트웨이로 계정 A 로 로그인한 상태의 앱 쿠키를 유지한 채, 게이트웨이를 B 로 로그인(또는 테스트 클라이언트로 A 세션 + B 헤더 요청) → A 세션 폐기 후 B 로 로그인되어야 한다. 단위 테스트 `GatewaySessionMismatchTests` 가 있다.
- **C. 401 새로고침**: 브라우저 개발자도구에서 게이트웨이 세션 쿠키를 지운 뒤 화면의 fetch/HTMX 동작(예: Suite Run 상태 폴링)을 유발 → 페이지가 새로고침되며 Keycloak 로그인으로 이동. 무한 새로고침이 없는지(10초 가드) 확인. 단위 테스트 `SessionExpiredReloadScriptTests`.
- **D. 비활성/모호 계정**: 계정을 비활성으로 두고 헤더 로그인 → 403 `sso_inactive` 화면(자동시작 루프 없음).

회귀 테스트 (외부 연동 값이 테스트에 섞이지 않도록 환경변수를 비워서 실행):

```bash
QGP_SCRIPT_NAME= SLACK_BOT_TOKEN= TAPILOT_API_KEY= .venv/bin/python manage.py test apps.accounts config.test_subpath
```

`apps/accounts/test_sso.py` 주요 클래스: `GatewayHeaderLoginTests`(헤더 로그인), `GatewaySessionMismatchTests`, `ProxyBoundaryTests`, `ForgedHeaderTests`, `GatewayAutoStartTests`, `LogoutTests`/`SSOLogoutTests`, `GatewayLogoutPageTests`(로그아웃 HTML 이동), `GatewayEntryTests`/`GatewayEntrySettingsTests`(진입 모드), `LocalLoginClosedTests`(자체 로그인 닫힘 — 단계 9 에서 기대값 점검 필요), `SessionExpiredReloadScriptTests`, `ExistingBehaviorRegressionTests`.

---

## 8. 참고 문서·코드 위치

| 무엇 | 어디 |
|---|---|
| 게이트웨이 구조·운영 절차 | `docs/qw-sso-gateway.md` (git 미추적 → 이관 커밋에서 add 예정) |
| 서비스 SSO 전환 가이드(헤더 방식 §3.1·§4, 401 §3.3, 로그아웃 §3.4) | `docs/service-sso-migration.md` (동일, 미추적 → add 예정) |
| SSO 흐름·미들웨어·설정 플래그 | `maintenance.md` §7 "SSO 로그인", §8 |
| 사용자 시점 SSO 항목·환경변수 | `README.md` SSO 항목, `.env.example` 의 `OIDC_INTERNAL_*`(주석 처리됨)·`TRUSTED_PROXY_CIDRS`·`GATEWAY_LOGOUT_URL` |
| 앱 직접 OIDC · 헤더 로그인 본체 | `apps/accounts/sso.py` (`gateway_entry_view` `:167`, `login_view` `:185`, `_gateway_logout_page` `:218`, `logout_view` `:255`, `login_by_gateway_header` `:373`, `_gateway_user` `:398`) |
| 미들웨어 | `apps/accounts/middleware.py` (`ProxyBoundaryMiddleware` `:69`, `GatewaySessionMiddleware` `:111`), 등록 순서 `config/settings/base.py:71-83` |
| 설정 | `config/settings/base.py:270-320` (OIDC·LOCAL_LOGIN·`GATEWAY_ENTRY_ENABLED` `:312`·TRUSTED_PROXY_CIDRS `:317`·GATEWAY_LOGOUT_URL `:320`) |
| admin 로그인 닫힘 | `config/urls.py` `admin_login`/`admin_password_change` |
| 프런트 401 | `static/js/qg_notify.js:27-58` |
| 게이트웨이 진입 로그인(앱 로그인 화면 + Keycloak 버튼) | 이 문서 §10, `maintenance.md` §7 "게이트웨이 진입 모드" |
| 서비스/운영 | `deploy/qualitygate.service`, `sudo systemctl restart qualitygate`, `run.sh`(포트 8601) |

(라인 번호는 2026-10-09 기준이다. 수정 전에 `grep -n` 으로 재확인한다.)

## 9. 새 AI 세션이 지킬 것

- 시작 시 `README.md`·`maintenance.md`·`AGENTS.md`·`SKILLS.md` 를 읽는다 (CLAUDE.md 지침). 독립 작업은 병렬 서브에이전트로 나눈다. 커밋·push 는 메인이.
- 운영 DB(`db.sqlite3`)는 사용자 확인 없이 쓰지 않는다. 계정 수정은 §6 처럼 승인 후.
- 게이트웨이/Keycloak 설정은 바꾸지 않는다. 필요한 변경은 사용자에게 요청 사항으로 전달한다.
- 비밀값(`.env`, client secret, 쿠키, 토큰)을 출력·로그·문서에 남기지 않는다.
- 검증 못 한 것은 "미확인" 이라고 말한다.

---

## 10. 게이트웨이 진입 로그인 — 앱 자체 로그인 화면 + Keycloak 버튼

> 상태(2026-10-09): **코드·테스트 완료(`GatewayEntryTests` 등), `GATEWAY_ENTRY_ENABLED` 는 꺼짐(`.env` 에 키 없음).** 게이트웨이 라우팅 변경이 적용·검증되기 전에는 켜지 않는다. 게이트웨이 설정은 우리가 바꾸지 않는다 — 아래 요청 문안을 QA 인프라팀에 전달한다.

### 10.1 요구

QG 자체 로그인 페이지(`/accounts/login/`)가 **첫 화면**이고, 거기 **"Keycloak 로그인" 버튼**으로 로그인한다. 로그아웃하면 다시 그 페이지로 돌아온다. 현재 게이트웨이는 `/qg/*` 전체를 보호(비로그인 `/qg/`·`/qg/accounts/login/`·`/qg/health/` 모두 401, 브라우저 이동은 게이트웨이 `/_qw/login` 선택 화면)하므로 비로그인 요청이 앱에 닿지 못해 앱 로그인 화면을 보여 줄 수 없다 → **진입 경로 하나만 보호하는 라우팅**이 필요하다.

### 10.2 동작 흐름

```
[비로그인] https://qw.jkintra.net/qg/  (게이트웨이: 비보호 → 앱)
   ▼ 앱: login_required → /qg/accounts/login/  (앱 로그인 화면 + "Keycloak 로그인" 버튼)
   ▼ 버튼 = /qg/accounts/gateway-login/?next=…  (게이트웨이: internal 보호 → Keycloak 로그인)
   ▼ 게이트웨이가 X-Auth-* 헤더를 붙여 백엔드(8601)로
앱: ProxyBoundaryMiddleware(신뢰 출발지 헤더만 유지) → GatewaySessionMiddleware 가 헤더 이메일로 앱 세션 생성
   ▼ sso.gateway_entry_view → next(같은 호스트만) 로 302 → 대시보드
   · 세션이 안 생겼으면(헤더 없음·라우팅 미설정·신뢰 밖 출발지) 503 sso_unavailable 로그인 화면을 직접 렌더(루프 없음)
   · 계정 비활성·모호면 403

[로그아웃] /qg/accounts/logout/ → 앱 세션 종료 → HTML 페이지(fetch 로 게이트웨이 /_qw/logout 호출 → 세션 쿠키 제거)
   ▼ /qg/accounts/login/?loggedout=1  (3초 타임아웃, no-JS 는 4초 meta refresh)
```

구현 위치: `apps/accounts/sso.py` `gateway_entry_view`(`:167`)·`_gateway_logout_page`(`:218`)·`logout_view`(`:255`), `apps/accounts/urls.py`(`accounts:gateway_login`), `config/settings/base.py:312`(플래그)·`:313`(`LOCAL_LOGIN_ACTIVE`), `templates/accounts/login.html`. 상세·표는 `maintenance.md` §7 "게이트웨이 진입 모드".

### 10.3 QA 인프라팀 요청 문안

> **QG(Quality Gate) 게이트웨이 라우팅 변경 요청**
>
> 대상 서비스: Quality Gate (`https://qw.jkintra.net/qg/`), 백엔드 `wx-qms221:8601` (게이트웨이가 `/qg/` 접두를 제거해 전달 — 현행 동일).
>
> 요청: 현재 `/qg/` 전체가 보호돼 있는데, 서비스 자체 로그인 화면을 첫 화면으로 쓰기 위해 아래 두 라우트로 나눠 주세요.
>
> | 경로 | 로그인 방식 | 비고 |
> |---|---|---|
> | `/qg/` (하위 전체) | **none** (서비스 자체 로그인 사용) | `/qg/` 접두 제거 후 백엔드 `8601` |
> | `/qg/accounts/gateway-login/` | **internal** (사내 직원 전용, world realm) | `/qg/` 접두 제거 후 백엔드 `8601` — 일반 경로보다 구체적인(longest-prefix) 라우트로 적용 |
>
> 보호 라우트가 통과한 요청에는 기존과 같이 `X-Auth-Request-Email`·`X-Auth-Realm` 등 `X-Auth-*` 헤더를 붙여 주세요(백엔드는 게이트웨이 Nginx 출발지 `172.20.0.222` 만 신뢰). 비보호 라우트(`/qg/`)로 들어온 요청에는 `X-Auth-*` 헤더가 붙지 않거나, 붙더라도 클라이언트가 보낸 값은 제거해 주세요.
>
> 확인 부탁드립니다.
> 1. `/qg/_qw/...` 형태의 경로 라우팅이나 응답 `Location` 헤더 재작성 규칙이 있는지 — 앱이 보낸 `https://qw.jkintra.net/_qw/logout` 302 가 `/qg/_qw/logout` 으로 바뀌어 404 가 난 적이 있습니다(2026-10-09). 지금은 앱이 302 대신 HTML 로 이동시키지만 규칙의 유무를 알고 싶습니다.
> 2. 게이트웨이 `/_qw/logout` 호출이 Keycloak 세션(SSO)까지 종료하는지(`SSO_*_ISSUER` 설정 의존으로 알고 있음) — 미확인입니다.
> 3. 비보호 `/qg/` 라우트가 외부(인터넷)에서도 접근 가능한 주소인지 — 접근 범위를 사내망으로 제한할 수 있는지.
> 4. 적용 시각과, 문제가 있을 때 이전 설정(`/qg/` 전체 internal 보호)으로 되돌리는 방법.

### 10.4 켜는 순서

1. QA 인프라팀이 위 라우팅을 적용(앱은 아직 건드리지 않음 — 플래그 꺼진 상태에서는 `/qg/` 가 비보호가 되어 **앱 자체 로그인 폼이 그대로 노출**되는 구간이므로 아래 2~4 를 바로 이어서 한다).
2. `.env` 에 `GATEWAY_ENTRY_ENABLED=True` 추가(값만 쓰고 다른 키는 건드리지 않는다).
3. `sudo systemctl restart qualitygate` (`.env` 는 전체 재시작 전에는 반영되지 않는다 — §3.1-1).
4. **브라우저 검증**(사용자): 시크릿 창에서 `https://qw.jkintra.net/qg/` → 앱 로그인 화면 + "Keycloak 로그인" 버튼 → 클릭 → 대시보드(앱 비밀번호 폼 없음). 사이드바 로그아웃 → 로그인 화면으로 복귀(`?loggedout=1`) → 버튼을 다시 눌렀을 때 재인증을 요구하는지(게이트웨이/Keycloak 세션 종료 여부 — 미확인 항목). `/accounts/login/` 에 아이디/비밀번호 폼이 보이지 않아야 한다.
5. 문제가 있으면 아래 롤백. 이전 라우팅으로 되돌릴 때는 **먼저 앱 플래그를 끄고(롤백 1)** 그다음 인프라팀에 라우팅 원복을 요청한다.

### 10.5 롤백

- 앱: `.env` 에서 `GATEWAY_ENTRY_ENABLED` 제거(또는 `False`) + `sudo systemctl restart qualitygate` → 버튼 없는 기존 동작(OIDC 값이 없으므로 자체 로그인 폼이 다시 열림). 로그인이 잠겼으면(라우팅 미적용 상태에서 켠 경우) 대신/함께 `LOCAL_LOGIN_ENABLED=True` + 재시작.
- 게이트웨이: 인프라팀에 `/qg/` 전체를 internal 로 보호하던 이전 라우팅으로 원복 요청. 이전 상태에서는 헤더 로그인(§2)으로 대시보드에 바로 들어간다.

### 10.6 보안 메모

- `X-Auth-*` 헤더는 `TRUSTED_PROXY_CIDRS` 출발지(게이트웨이 `172.20.0.222/32`) 것만 유지된다 — 비보호 `/qg/` 로 들어온 요청에는 게이트웨이가 헤더를 붙이지 않으므로 인증은 **앱 세션 쿠키로만** 이뤄진다. 헤더는 진입 경로(`/qg/accounts/gateway-login/`)를 통과한 요청에서만 생기고, 그때 `GatewaySessionMiddleware` 가 세션을 만든다.
- **앱 쪽 방어(코드 리뷰 반영, 2026-10-09)**: 진입 모드에서는 `GatewaySessionMiddleware` 가 **진입 경로(`/accounts/gateway-login`) 외 모든 경로의 `X-Auth-*` 를 버린다** — 게이트웨이가 비보호 경로에서 헤더를 덮어쓰지 않는 설정 실수가 있어도 클라이언트가 보낸 이메일 헤더로 로그인되지 않는다(신뢰 출발지를 거쳤어도). viewer 역할의 첫 로그인도 `next` 로 가도록 `/accounts/gateway-login` 은 viewer 허용 prefix 에 있다. id_token 이 있는 직접 OIDC 로그인 사용자의 로그아웃은 진입 모드에서도 기존 Keycloak end-session 분기를 유지한다.
- 세션이 만료되면 게이트웨이 401(이전 방식) 이 아니라 **앱 로그인 화면**으로 간다(`login_required`). `static/js/qg_notify.js` 의 401 새로고침(요구 6)은 게이트웨이가 401 을 줄 때만 동작하며, 진입 모드에서는 앱 `302 → 로그인 화면` 이라 영향이 없다(확인 필요: fetch/HTMX 가 로그인 화면 HTML 을 받는 경우의 UX — 미확인).
- **노출 범위 증가(확인 필요)**: `/qg/` 가 비보호가 되면 게이트웨이 앞단 인증 없이 앱의 모든 경로(`/api/v1`·`/api/v2`(API Key)·`/accounts/login/` 등)에 도달할 수 있다. 각 경로는 앱 자체 인증(세션/API Key/DRF Basic)으로 보호되지만, 이전에는 게이트웨이가 한 겹 더 막고 있었다. 외부 호출자(Jenkins/GitHub Actions 등)가 `/qg/api/v2/` 를 쓰는지, 비보호 라우트의 접근 범위(사내망 한정 여부)를 인프라팀과 확인한다(§10.3 질문 3). DRF Basic 잔존 경로 메모는 `maintenance.md` §7.
- 평문 비밀번호 로그인 폼은 진입 모드를 켠 뒤 기본으로 닫힌다(`LOCAL_LOGIN_ACTIVE`). 켜기 전 구간(§10.4-1~3)에는 열려 있으므로 그 구간을 짧게 둔다.

### 10.7 운영 반영 기록 (이 호스트 wx-qms221)

- 2026-10-09 19:2x: `.env` 에 `GATEWAY_ENTRY_ENABLED=True` 추가 후 `sudo systemctl restart qualitygate`. 게이트웨이가 아직 `/qg/` 전체를 보호하는 구성에서도 동작한다 — 게이트웨이 로그인을 통과한 사용자가 앱 로그인 화면(Keycloak 버튼)을 거쳐 진입 경로로 들어오고, 로그아웃은 게이트웨이 세션을 끊은 뒤 로그인 화면으로 간다(이때는 게이트웨이 로그인 화면이 먼저 보일 수 있음 — `/qg/accounts/login/` 이 `auth_exempt` 가 되면 앱 로그인 화면이 첫 화면). 로컬 검증: 로그인 화면에 버튼·비밀번호 폼 없음, 위조 헤더(신뢰 밖) 로그인 안 됨. **브라우저 왕복(로그인·로그아웃·재로그인 시 Keycloak 비밀번호 요구 여부)은 미확인.**
- 롤백: `.env` 의 `GATEWAY_ENTRY_ENABLED` 줄 삭제(또는 `LOCAL_LOGIN_ENABLED=True`) + `sudo systemctl restart qualitygate`.
- 데이터 이관(HTTPS 만 열림): 현재 v2 API 로는 불가 → `docs/api_requirements_data_export.md`.
