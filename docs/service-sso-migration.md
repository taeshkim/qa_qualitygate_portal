서비스 자체 로그인 걷어내기 — 게이트웨이 SSO 연동 가이드
대상: qw.jkintra.net 뒤 서비스 담당자(Test Hub, LLM Agent Chrome, Quality Gate, TestDataset, Notion Mirror 등). 2026-10-07 작성.

게이트웨이(2번 공용 Nginx)가 Keycloak 로그인을 대신 처리하고 요청마다 사용자 정보를 헤더로 전달. 서비스는 로그인 화면·비밀번호·세션 쿠키·토큰 갱신·로그아웃이 불필요. 바꿀 곳은 "이 요청의 사용자가 누구인가"를 읽는 한 지점. 권한(누가 무엇을 보는가)은 지금처럼 서비스가 관리.

1. 게이트웨이가 넘기는 헤더
헤더	값	비고
X-Auth-Request-Email	hong@worxphere.ai	사용자 식별 키
X-Auth-Realm	world / ext	world=사내 직원, ext=협력사(@ext.worxphere.ai)
X-Auth-Request-Preferred-Username	hong	표시용. 식별 키로 사용 금지
X-Auth-Request-User	이메일과 동일	oauth2-proxy 호환용
X-Auth-Request-Groups	비어 있음	그룹 클레임 미사용
보장 사항:

게이트웨이를 거친 요청의 위 헤더는 항상 게이트웨이가 덮어씀. 클라이언트가 보낸 같은 이름의 헤더는 백엔드에 도달하지 않음.
로그인 방식이 "자체 로그인만"인 서비스에는 헤더 없음. 헤더가 있으면 게이트웨이가 인증한 것.
세션 없는 요청은 서비스에 도달하지 않음. 화면 이동은 로그인 선택 화면, API·POST·WebSocket은 401. 서비스는 "헤더 없음 = 비로그인"으로 처리하면 충분.
사내 전용(internal) 서비스에 협력사 세션은 들어오지 않음(게이트웨이가 403). X-Auth-Realm: ext는 partner 서비스에만 도달.
Keycloak 호출은 게이트웨이(oauth2-proxy)가 로그인·15분 갱신·로그아웃 때만 수행. 서비스가 Keycloak에 확인할 필요 없음.

2. 전제: 게이트웨이 우회 접근 차단 (가장 중요)
평문 헤더를 믿어도 되는 조건은 하나. 서비스 포트에 게이트웨이(2번 서버) 외 접속 불가. 앱 포트로 직접 붙어 X-Auth-Request-Email: 아무개를 보내면 그 사람이 되기 때문.

서비스 위치	조치
2번 서버 호스트 앱(6001, 7000 등)	Docker 브리지 주소(172.17.0.1/172.18.0.1)에만 바인딩하거나 방화벽으로 2번 Nginx 컨테이너 출발지만 허용. ss -ltnp로 0.0.0.0 바인딩 여부 확인
1번 서버 앱(8601, 8602, 8605)	방화벽에서 172.20.0.222만 허용. 사내 PC에서 http://172.20.0.221:8605가 열리면 안 됨
맥 미니 등 사내 장비(tap2)	사내 PC에서 직접 접속되는 장비는 헤더 신뢰 불가. 자체 로그인 유지 또는 4절 토큰 검증 방식 필요
앱에서도 출발지 IP 검사를 한 겹 추가. 요청의 REMOTE_ADDR(앱이 실제로 본 TCP 상대)가 게이트웨이 주소가 아니면 헤더 무시. X-Forwarded-For가 아니라 TCP 상대 주소. Notion Mirror의 TRUSTED_PROXY_CIDRS·ProxyBoundaryMiddleware가 이 방식.

2번 Nginx의 출발지: 2번 호스트 앱에서는 Docker 네트워크 주소(예: 172.18.0.2), 1번 서버 앱에서는 172.20.0.222.

3. 서비스 변경 사항
3.1 사용자 식별 (필수)
X-Auth-Request-Email로 사용자 테이블 조회. 없으면 첫 로그인으로 생성.
협력사(X-Auth-Realm: ext) 자동 생성 시 권한 없음 상태로. 기본 권한 부여 시 게이트웨이를 통과한 모든 협력사가 즉시 데이터 열람 가능. 담당자가 권한을 부여하는 흐름 필요.
협력사 판별은 이메일 도메인 추측이 아닌 X-Auth-Realm 기준.
기존 계정 연결: 사용자명이 이메일과 다르면 1회 매핑(이메일 컬럼 채우기) 필요. 운영 DB 작업이므로 전환 전 완료.
세션을 유지하는 프레임워크(Django 등)는 세션 사용자 ≠ 헤더 사용자이면 세션 폐기. 공용 PC에서 A 로그아웃 후 B 로그인 시 A 세션 잔존 방지.
3.2 제거 대상
로그인·회원가입·비밀번호 변경 화면과 API(TestDataset /signup 포함)
로그인 리다이렉트(LOGIN_URL, ?next=). 게이트웨이가 처리.
프런트의 localStorage 토큰과 로그인 API 호출. 쿠키는 브라우저가 자동 전송하므로 프런트는 아무것도 붙일 필요 없음.
앱 자체 OIDC(Notion Mirror): 유지 가능(같은 Keycloak 세션이라 비밀번호 재입력 없음). 제거 시 ExternalIdentity의 (issuer, sub) 대신 이메일로 조회하도록 변경. 급하지 않음.
3.3 401 처리 (프런트)
세션 만료 시 API 호출은 게이트웨이에서 401(로그인 HTML 아님, 빈 401)로 종료. 프런트는 401 수신 시 window.location.reload() 또는 현재 주소로 이동. 화면 이동 요청이므로 게이트웨이가 로그인으로 보내고 로그인 후 원래 주소로 복귀.

WebSocket은 연결 시점에만 인증. 재연결이 401로 실패하면 동일하게 새로고침.

3.4 로그아웃
로그아웃 버튼은 https://qw.jkintra.net/_qw/logout으로. 게이트웨이 세션 종료 후, 설정돼 있으면 Keycloak 세션도 종료. 서비스 자체 세션이 남아 있으면 그 전에 비울 것.

3.5 CI·스크립트·외부 삽입 위젯
브라우저가 아닌 클라이언트는 SSO 쿠키가 없어 차단됨. 예: CI가 Quality Gate API 호출, TestDataset widget.js의 외부 사이트 삽입. 해당 하위 경로(/api/, /webhook/ 등)를 admin의 로그인 예외 하위 경로로 등록하도록 QA팀에 전달. 예외 경로에는 식별 헤더가 오지 않으므로 기존 API 토큰 인증 유지 필수. 비브라우저 호출 존재 여부는 access log의 User-Agent로 사전 확인.

3.6 리버스 프록시 헤더 (기존 완료 사항)
X-Forwarded-Proto(https), X-Forwarded-Host, X-Forwarded-Prefix(예: /th) 신뢰 설정이 있어야 절대 URL·쿠키 Secure·CSRF origin이 정상. HTTPS 전환 시 완료한 항목이나, 신뢰 대상 IP를 2번 게이트웨이로 한정했는지 재확인.

4. 프레임워크별 예시
Django (tap, qg, Notion Mirror)
Django 기본 기능으로 충분. 기존 @login_required, request.user, 권한 검사는 그대로 동작.

# settings.py
MIDDLEWARE = [
    ...,
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.GatewayUserMiddleware",   # AuthenticationMiddleware 바로 뒤
    ...,
]
AUTHENTICATION_BACKENDS = ["accounts.backends.GatewayBackend"]
GATEWAY_PEERS = ["172.20.0.222"]   # 앱이 실제로 보는 2번 Nginx 출발지. 2번 호스트 앱이면 Docker 네트워크 대역
# accounts/middleware.py
import ipaddress
from django.conf import settings
from django.contrib.auth.middleware import RemoteUserMiddleware


class GatewayUserMiddleware(RemoteUserMiddleware):
    header = "HTTP_X_AUTH_REQUEST_EMAIL"
    force_logout_if_no_header = True   # 헤더가 없거나 바뀌면 기존 세션 폐기

    def process_request(self, request):
        peer = ipaddress.ip_address(request.META["REMOTE_ADDR"])
        trusted = any(peer in ipaddress.ip_network(net) for net in settings.GATEWAY_PEERS)
        if not trusted:
            # 게이트웨이를 거치지 않은 요청: 식별 헤더 무시
            for key in list(request.META):
                if key.startswith("HTTP_X_AUTH_"):
                    del request.META[key]
        super().process_request(request)
# accounts/backends.py
from django.contrib.auth.backends import RemoteUserBackend


class GatewayBackend(RemoteUserBackend):
    create_unknown_user = True

    def configure_user(self, request, user, created=True):
        # 첫 로그인: 협력사는 권한 없이 생성
        if created:
            user.email = user.username
            user.is_active = request.META.get("HTTP_X_AUTH_REALM") == "world"
            user.save()
        return user
username에 이메일이 저장됨. 기존 사용자명이 다르면 clean_username 재정의로 이메일 → 기존 사용자명 변환, 또는 전환 전 사용자명을 이메일로 통일. 위 예시는 협력사를 is_active=False로 생성해 관리자가 활성화하는 방식. 서비스 권한 모델에 맞게 조정.

API 백엔드 + SPA (th: Vite 프런트)
백엔드의 “현재 사용자” 의존성 한 곳만 변경.

# FastAPI 예시. Flask·Express도 동일 구조
from fastapi import Depends, HTTPException, Request

GATEWAY_PEERS = {"172.20.0.222"}

def current_user(request: Request):
    if request.client.host not in GATEWAY_PEERS:
        raise HTTPException(401)
    email = request.headers.get("x-auth-request-email")
    if not email:
        raise HTTPException(401)
    realm = request.headers.get("x-auth-realm")
    return users.get_or_create(email=email, partner=(realm == "ext"))
프런트: 로그인 화면·토큰 저장 제거, /api/v1/auth/me 등의 401에서 새로고침. Authorization 헤더 부착 코드 삭제.

직접 접근 차단 불가 장비 (tap2 맥 미니)
헤더 대신 서명 토큰 검증 필요. 게이트웨이가 Keycloak access token을 함께 전달하는 추가 설정이 있어야 하므로 필요 시 QA팀에 요청. 그 전까지 자체 로그인 유지.

5. 전환 순서 (서비스 단위)
2절 직접 접근 차단과 출발지 IP 검사. 미완이면 3절 진행 금지.
3.5절 비브라우저 호출 확인 → 예외 경로를 QA팀에 전달.
계정 매핑(이메일 채우기), 협력사 자동 생성 정책 결정.
헤더 없으면 기존 로그인으로 폴백하는 형태로 코드 배포. 게이트웨이에서 로그인 방식을 켜면 SSO, 끄면 원복. 전환 리스크 최소.
QA팀이 admin에서 로그인 방식을 internal(사내 전용) 또는 partner(협력사 허용)로 적용.
확인: 로그인 → 진입, 정적 파일·API·WebSocket, 권한 없는 사용자, 로그아웃 → 재로그인 요구, 협력사 계정(partner인 경우) 권한 없음 상태로 생성.
문제 시 QA팀이 로그인 방식 원복 → 즉시 복구(4번 폴백 덕분에 재배포 불필요).
6. 서비스별 메모
서비스	로그인 방식(예정)	특이 사항
/th Test Hub	internal	SPA + API. /api/v1/auth/me가 401 처리 지점. WebSocket 재연결 처리. 운영에서 Vite 개발 서버 서빙 여부 확인
/tap LLM Agent Chrome	internal	Django. ALLOWED_HOSTS·CSRF origin은 HTTPS 때 완료
/qg Quality Gate	internal	Django. CI의 API 호출 여부 확인 후 /api/ 예외 결정
/qd TestDataset	internal	자체 회원가입 비활성화. /api/assistant/widget.js 외부 삽입이면 예외
/nm Notion Mirror	partner (적용됨)	앱 OIDC 유지 가능. TRUSTED_PROXY_CIDRS /32 축소 작업 유효
/tap2 맥 미니	당분간 none	직접 접근 가능 → 헤더 신뢰 불가
문의: QA팀(게이트웨이 설정·예외 경로·로그인 방식 전환). 게이트웨이 구조는 qw-sso-gateway.md 참고.
