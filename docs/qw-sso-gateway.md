qw.jkintra.net 게이트웨이 SSO 운영
작성일 2026-10-07. 설계 배경은 Keycloak SSO 중앙화 분석에 있습니다. 이 문서는 구현된 구조와 운영 절차입니다.

구조
브라우저 ─HTTPS→ 2번 Nginx ──auth_request──→ qw-sso-world (oauth2-proxy, world realm)
                     │                    └→ qw-sso-ext   (oauth2-proxy, ext realm)
                     │ 통과: X-Auth-Request-Email, X-Auth-Realm … 헤더를 붙여
                     └──────────────────────→ 각 서비스 백엔드 (기존 위치 그대로)
구성 요소	위치	역할
edge/compose.qw-sso.yml	Compose override	oauth2-proxy 컨테이너 두 개와 Nginx include 마운트. 기존 Nginx compose 조합 맨 뒤에 추가
edge/qw-sso.conf	Nginx server 수준 include	/oauth2/, /oauth2-ext/ 프록시, /_qw/auth/* 내부 검사 location, 401·403 처리, /_qw/ 포털 페이지
edge/qw-auth/internal.conf, partner.conf	Nginx location 수준 include	서비스 location에 렌더링되는 한 줄 include의 대상. auth_request와 식별 헤더 전달
edge/qw-sso-portal.conf	runtime qw-sso.d/portal.conf로 복사하는 스위치	포털 /과 /admin/을 SSO 뒤에 둠. 파일 삭제 + reload로 해제
shared/routing.py, shared/gateway.py	렌더러 (qw-v3)	경로별 auth(none/internal/partner)와 auth_exempt 렌더링, 식별 헤더 덮어쓰기
dashboard/	admin·포털	경로 설정의 로그인 방식 선택, /_qw/login 선택 화면, /_qw/forbidden, /_qw/logout, 협력사 realm 포털 필터
$EDGE_RUNTIME_DIR/sso/world.env, ext.env	운영 비밀 (0600)	issuer, client id·secret, cookie secret. 예시: servers/wx-qms222/sso.*.env.example
동작 규칙:

서비스마다 admin에서 로그인 방식을 고릅니다. 기본은 서비스 자체 로그인만 사용(none)이라 SSO를 켜도 기존 서비스 동작은 바뀌지 않습니다. 라우팅 설정이므로 초안 저장 후 서버에 적용해야 반영됩니다.
internal: world realm(사내 직원) 세션만 통과. 협력사 세션은 403(화면 이동 요청은 /_qw/forbidden 페이지).
partner: world·ext 세션 모두 통과. X-Auth-Realm에 검증된 realm(world/ext)을 넣어 줍니다.
세션이 없으면 화면 이동 요청만 /_qw/login(사내/협력사 선택)으로 보내고, API·POST·WebSocket 요청은 401을 그대로 돌려줍니다. 앱이 401을 받으면 페이지를 새로고침하도록 처리하면 됩니다.
auth_exempt(예: /api/)는 SSO 없이 통과하는 하위 경로입니다. CI·스크립트·외부 삽입 위젯용이며, 앱의 기존 API 인증으로 보호해야 합니다.
식별 헤더 X-Auth-Request-Email, X-Auth-Request-User, X-Auth-Request-Preferred-Username, X-Auth-Request-Groups, X-Auth-Realm은 모든 서비스 location에서 게이트웨이가 덮어씁니다. SSO를 안 쓰는 서비스에는 빈 값(헤더 제거)으로 갑니다. 클라이언트가 보낸 값은 절대 백엔드에 닿지 않습니다.
컨테이너 안 loopback(127.0.0.1) 요청은 SSO를 건너뜁니다. 헬스체크가 여기에 해당하며 HTTPS 리다이렉트와 같은 전제입니다.
서비스가 internal/partner인데 SSO override가 마운트되지 않았으면 nginx -t가 실패해 적용이 거부됩니다. 반쯤 켜진 상태로 배포되지 않습니다.
Keycloak 호출은 oauth2-proxy의 로그인·토큰 갱신(15분)·로그아웃 때만 일어납니다. 서비스는 요청마다 Keycloak에 묻지 않습니다.

서비스 쪽에서 할 일
1단계(게이트웨이 인증만): 서비스 변경 없음. 401 응답 처리와 WebSocket 재연결만 확인합니다.

2단계(자체 로그인 제거): 요청 헤더로 사용자를 식별합니다. 서비스 담당자용 가이드는 service-sso-migration.md입니다. 전제는 게이트웨이를 거치지 않는 직접 접근이 막혀 있는 것입니다. 직접 접근이 가능한 장비(맥 미니 등)는 평문 헤더를 믿지 말고 Keycloak 서명 토큰을 검증해야 합니다. 협력사 realm 사용자가 첫 로그인으로 자동 생성될 때는 권한 없음으로 만드세요.

Keycloak client 신청
world (사내)	ext (협력사)
신청처	worxhub 포털	클라우드 인프라팀 (Slack)
Client ID	qw-gateway	qw-gateway-ext
유형	Confidential, Standard flow만, PKCE S256	같음
Valid redirect URIs	https://qw.jkintra.net/oauth2/callback	https://qw.jkintra.net/oauth2-ext/callback
Valid post logout redirect URIs	https://qw.jkintra.net/	https://qw.jkintra.net/
Web origins	비움	비움
Notion Mirror의 기존 client(qw-notion-mirror, qw-notion-mirror-partner)는 redirect URI가 /nm/oidc/<provider>/callback/ 하나뿐이라 그대로는 쓸 수 없습니다. 시험용으로 쓰려면 그 client에 위 콜백 주소를 추가해 달라고 요청해야 하며, 이후 게이트웨이 전용 client로 바꿀 때는 env 파일의 OAUTH2_PROXY_CLIENT_ID·OAUTH2_PROXY_CLIENT_SECRET만 바꾸고 oauth2-proxy 컨테이너를 재시작하면 됩니다.

운영 반영 기록
2026-10-07: enable-qw-sso.sh로 2번 서버에 적용(qw-v3 재적용 완료). /nm을 partner로 전환하고 사내 계정 로그인 → 게이트웨이 통과 → Notion Mirror SSO 비밀번호 재입력 없이 진입을 확인했습니다. 협력사 계정 로그인은 ext 테스트 계정 확보 후 확인 예정입니다. 포털·admin 스위치(qw-sso.d/portal.conf)는 같은 날 안내했습니다.

2026-10-08: 포털·admin 스위치(qw-sso.d/portal.conf)를 켰습니다. /는 로그인 후 목록(협력사는 허용 서비스만), /admin/은 사내 세션 + 기존 비밀번호입니다. 사용자가 브라우저에서 정상 동작을 확인했습니다.

운영 주의점
2026-10-07 후속: 세션 저장소를 Redis로 바꿨습니다. 아래 쿠키 분할·갱신 문제의 근본 해결입니다. edge/compose.qw-sso.yml에 qw-sso-redis(세션 전용, 디스크 저장 없음)가 추가되고 두 oauth2-proxy가 OAUTH2_PROXY_SESSION_STORE_TYPE=redis로 동작합니다. 브라우저 쿠키는 작은 세션 ID만 들고, 15분 토큰 갱신은 Redis 안에서 끝나므로 auth_request 응답에 Set-Cookie가 실리지 않습니다. Redis가 재시작되면 사용자는 다시 로그인하면 됩니다. 아래 기록은 Redis 전환 전의 관측입니다.

trusted proxy IP: 같은 변경에서 OAUTH2_PROXY_TRUSTED_PROXY_IPS를 edge 네트워크 대역으로 제한했습니다. enable-qw-sso.sh가 servers/wx-qms222/.env에 QW_SSO_TRUSTED_PROXY_IPS=<jkintra_edge 서브넷>을 한 번 기록합니다. 수동으로 compose를 실행할 때도 이 env 파일을 쓰므로 값이 유지됩니다.

2026-10-07 적용 후 서버 쪽 점검(설정 검사, 컨테이너 상태, oauth2-proxy·Nginx 로그, 익명·위조 쿠키·위조 헤더 요청)에서 확인한 사항입니다. 동작은 설계대로였고, 아래 두 가지만 지켜보면 됩니다.

auth 서브리퀘스트 버퍼(2026-10-07 10:14 UTC 장애). 로그인 15분 뒤 토큰 갱신이 시작되자 /oauth2/auth 응답에 분할 Set-Cookie가 실려 Nginx 기본 4KB 버퍼를 넘었고, upstream sent too big header → auth 502 → /nm/ 전체가 500이 됐습니다. edge/qw-auth/auth-subrequest.conf에 proxy_buffer_size 16k; proxy_buffers 4 32k;를 넣고 reload해 해결했습니다. 같은 증상이 다시 보이면 이 파일의 버퍼 값과 아래 쿠키 크기 항목을 먼저 보세요.
갱신된 세션이 브라우저에 저장되지 않습니다. auth_request 응답의 Set-Cookie는 클라이언트로 전달되지 않습니다(Nginx는 첫 Set-Cookie만 변수로 꺼낼 수 있고, 분할 쿠키는 여러 개라 전달이 불가능). 그래서 로그인 15분 뒤부터는 요청마다 oauth2-proxy가 Keycloak에 토큰 갱신을 요청합니다(로그의 Refreshing session 반복). 동작은 하지만 Keycloak 부하가 늘고, Keycloak이 refresh token 재사용을 막도록 바뀌면 15분마다 재로그인하게 됩니다. 근본 해결은 Redis 세션 저장소(쿠키가 작아지고 갱신이 서버 쪽에서 끝남)이며, 그 전까지는 아래 그룹 매퍼 축소로 쿠키를 4KB 아래로 만들면 첫 Set-Cookie 전달로 땜질할 수 있습니다.
세션 쿠키가 4KB를 넘어 분할됩니다. 사내 계정은 Keycloak 그룹·role 목록이 길어 oauth2-proxy가 Multiple cookies are required for this session 경고를 남깁니다. Nginx는 large_client_header_buffers 4 16k로 받아 주지만, 이 쿠키는 qw.jkintra.net의 모든 서비스 요청에 실려 갑니다. gunicorn처럼 헤더 한 줄을 8KB로 제한하는 백엔드는 SSO를 쓰지 않는 서비스라도 400을 낼 수 있습니다. 어떤 서비스가 로그인 뒤에만 400을 내면 이것을 먼저 의심하세요. 근본 해결은 Keycloak client scope에서 groups 매퍼를 게이트웨이에 필요한 그룹만 내보내도록 줄이거나, oauth2-proxy 세션 저장소를 Redis로 바꾸는 것입니다.
oauth2-proxy의 trusted proxy IP가 비어 있습니다. --reverse-proxy가 켜져 있는데 신뢰할 프록시 대역이 없어 모든 발신지의 X-Forwarded-* 헤더를 믿는다는 경고가 시작 로그에 남습니다. 두 컨테이너는 호스트에 포트를 공개하지 않고 edge 네트워크의 Nginx만 접근할 수 있어 실질 위험은 낮습니다. 여유가 있을 때 Nginx 컨테이너 대역으로 제한하면 경고가 사라집니다.
그 밖에 Nginx 로그의 dashboard 이름 해석 실패는 enable-qw-sso.sh 5단계의 dashboard 재시작 순간에만 생기는 일시적 오류이고, an upstream response is buffered to a temporary file 경고는 큰 응답의 디스크 버퍼링이라 SSO와 무관합니다.

운영 반영 (2번 서버)
전제: 2번 → sso.jkintra.net:443 허용(2026-10 완료 기록), client 두 개 발급.

git pull --ff-only origin develop
비밀 파일 작성(SSH 없이 서버 터미널에서 직접). 키 값은 채팅·Git에 남기지 않습니다.
sudo install -d -m 0700 /var/lib/qe-edge/wx-qms222/runtime/sso
sudo sh -c 'umask 077; cat > /var/lib/qe-edge/wx-qms222/runtime/sso/world.env'   # 붙여넣고 Ctrl-D
sudo sh -c 'umask 077; cat > /var/lib/qe-edge/wx-qms222/runtime/sso/ext.env'
python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'   # cookie secret, realm마다 다르게
dashboard/.env에 로그아웃 링크용 공개값 추가(선택): SSO_WORLD_ISSUER, SSO_WORLD_CLIENT_ID, SSO_EXT_ISSUER, SSO_EXT_CLIENT_ID. 없으면 로그아웃은 게이트웨이 세션만 끝냅니다.
oauth2-proxy 이미지 반입 — 2번 서버는 quay.io에 나가지 못하므로(2026-10-07 확인) 이미지를 파일로 올립니다. 외부 PC에서:
docker pull --platform linux/amd64 quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine
docker save --platform linux/amd64 quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine | gzip -6 > oauth2-proxy-v7.15.5-alpine-amd64.tar.gz
Docker Hub는 2번 서버에서 직접 받아지므로(2026-10-07 확인) redis:7-alpine은 docker pull로 충분합니다. quay.io 이미지만 admin 파일 업로드로 올린 뒤 서버에서 sudo docker load -i /home/david639/uploads/*oauth2-proxy-v7.15.5-alpine-amd64.tar.gz(Redis는 *redis-7-alpine-amd64.tar.gz). docker image inspect ... --format '{{.Architecture}}'가 amd64여야 합니다(맥 기본 pull은 arm64). compose는 같은 태그의 로컬 이미지를 그대로 씁니다. 버전을 올릴 때는 edge/compose.qw-sso.yml의 태그와 함께 같은 방식으로 반입합니다.
sudo scripts/enable-qw-sso.sh — Keycloak 연결 확인 → nginx -t → oauth2-proxy 시작과 Nginx 재생성(짧은 끊김) → /opt/qe-edge-control 렌더러 갱신 → dashboard 재빌드 → --reapply(qw-v3, 경로 변경 없음) → /_qw/login 응답 확인. 실패한 단계에서 멈춥니다.
admin에서 시험용 경로 하나에 사내 직원 전용을 지정해 적용하고 로그인·API 401·로그아웃을 확인합니다.
서비스별로 순서대로 켭니다. /nm은 사내 직원 + 협력사 허용으로 두고, 앱의 자체 OIDC는 유지합니다.
포털·admin까지 SSO 뒤로 두려면 sudo scripts/enable-qw-sso.sh --portal(재실행 안전) 또는 edge/qw-sso-portal.conf를 runtime/qw-sso.d/portal.conf로 복사하고 nginx -t && nginx -s reload.
이후 모든 Nginx 배포 명령에 -f edge/compose.qw-sso.yml을 포함해야 합니다. 빠뜨리면 보호된 서비스가 있을 때 nginx -t가 실패합니다.

되돌리기
서비스 하나: admin에서 로그인 방식을 서비스 자체 로그인만 사용으로 바꿔 적용.
포털·admin만: sudo rm runtime/qw-sso.d/portal.conf && sudo docker exec qe-edge-nginx nginx -s reload.
Redis만 재시작: docker compose ... restart qw-sso-redis. 세션이 비워져 모두 다시 로그인합니다.
전체: 먼저 모든 서비스를 자체 로그인으로 적용한 뒤 compose.qw-sso.yml 없이 Nginx를 재생성합니다. 보호된 서비스가 남아 있으면 include가 없어 nginx -t가 실패하므로 순서를 지켜야 합니다. oauth2-proxy 컨테이너는 docker compose ... rm -sf qw-sso-world qw-sso-ext.
Keycloak 장애 시: oauth2-proxy의 기존 세션은 토큰 갱신 주기(15분)까지 유지되고, 이후 로그인이 막힙니다. 급하면 위 “서비스 하나” 절차로 해당 서비스를 풉니다.
로컬 검증
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/integration_sso.py
통합 테스트는 운영 compose 파일 그대로 로컬 Keycloak(두 realm, 시험 계정 포함)·oauth2-proxy 두 개·Nginx·에코 백엔드를 띄워, SSO 마운트 없는 nginx -t 거부, 익명 처리(선택 화면·API 401·WebSocket 401·예외 경로·헤더 제거), 사내·협력사 로그인 전 과정, 포털·admin 스위치, 로그아웃, 허용되지 않은 이메일 도메인 거부, loopback 헬스체크 우회, edge_control 재적용·실패 복구를 확인합니다. 운영 Keycloak의 realm 설정·계정·방화벽은 검증하지 않습니다.
