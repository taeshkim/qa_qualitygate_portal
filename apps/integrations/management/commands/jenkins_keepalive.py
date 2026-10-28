"""`python manage.py jenkins_keepalive` — 내부 Jenkins SSO 세션 유지용 가벼운 핑 (cron).

**왜 필요한가**: 내부 Jenkins 가 Keycloak SSO 뒤에 있으면, 포털이 저장된 API 토큰으로 하는
Basic auth 는 그 사용자의 SSO 세션이 살아있을 때만 통과한다. 세션이 idle 타임아웃되면
Jenkins 가 Basic auth 를 SSO 로그인으로 302 리다이렉트해서 Suite/QG/Reliability 트리거가
'Jenkins 인증 실패'로 떨어진다(브라우저로 Keycloak 로그인 한 번 하면 잠시 복구되는 증상).

이 명령을 cron 으로 자주 호출해 가벼운 ``/api/json?tree=nodeName`` GET 으로 idle 타이머를
계속 리셋, 세션을 warm 하게 유지한다.

**한계**:
  1. 세션이 이미 끊긴 뒤에는 이 핑도 실패해 스스로 복구하지 못한다 — 최초 1회는 브라우저
     Keycloak 로그인으로 세션을 살려야 한다(그 뒤로는 이 핑이 유지).
  2. 'API 토큰 호출이 SSO idle 타이머를 리셋하는' 구성일 때만 효과가 있다. 구성에 따라
     무효일 수 있으니 적용 후 실제로 트리거가 안 끊기는지 확인할 것(근본 해결은 인프라 측).

crontab — **한 번만 등록** (기존 owner sync / reliability sweep 과 같은 crontab)::

    * * * * * cd /home/taeshkim/qa_qualitygate_portal && \
        flock -n /tmp/qg_jenkins_keepalive.lock \
        .venv/bin/python manage.py jenkins_keepalive >> /tmp/qg_jenkins_keepalive.log 2>&1
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "내부 Jenkins SSO 세션 유지용 keep-alive 핑 (cron)"

    def handle(self, *args, **options):
        from apps.suites.models import SuiteJenkinsCredential
        from apps.integrations.jenkins import JenkinsClient, JenkinsError

        cred = SuiteJenkinsCredential.objects.filter(
            kind=SuiteJenkinsCredential.Kind.INTERNAL,
        ).first()
        if not cred or not (cred.base_url and cred.user and cred.api_token):
            self.stdout.write("internal Jenkins 자격증명 미설정 — keepalive skip")
            return

        ts = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
        try:
            # internal 자격증명을 쓰는 가장 가벼운 인증 요청(nodeName 만).
            JenkinsClient()._request("/api/json?tree=nodeName")
            self.stdout.write(f"[{ts}] jenkins keepalive ok")
        except JenkinsError as e:
            # 세션이 이미 끊겼거나 토큰 문제 — 핑으로는 복구 불가. 조용히 로깅만(트리거에 영향 없음).
            self.stdout.write(f"[{ts}] jenkins keepalive failed: {e}")
            logger.info(f"jenkins keepalive failed: {e}")
        except Exception as e:  # 네트워크 등 예외도 cron 을 죽이지 않게 흡수
            self.stdout.write(f"[{ts}] jenkins keepalive error: {e}")
            logger.info(f"jenkins keepalive error: {e}")
