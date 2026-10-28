"""`python manage.py sweep_reliability` — Reliability Test 스케줄 sweep (cron 용).

**왜 필요한가**: Reliability Test 는 Celery beat 같은 별도 스케줄러가 없고, 트리거는
``sweep_active_schedules()`` 가 호출되는 시점에만 진행된다. 과거에는 그 호출이 화면
폴링(HTMX)에서만 일어나서 **누군가 Reliability Test 화면을 열어둬야만** 스케줄이 돌고,
아무도 안 보고 있으면 interval 이 한참 지나도 트리거가 안 되는(=설정 간격대로 안 도는)
문제가 있었다. 이 명령을 **cron 으로 매분 호출**하면 브라우저와 무관하게 스케줄이
자기 interval 에 맞춰 돈다.

각 스케줄의 interval(분) 도래 여부는 ``sweep_schedule`` 내부에서 race-free conditional
UPDATE 로 판정하므로, 매분 호출해도 각 스케줄은 자기 interval 에만 1회 트리거된다
(60분 interval 이면 마지막 트리거 후 60분이 지난 첫 호출에서만 트리거).

권장 crontab — **한 번만 등록**한다 (서버의 담당자 동기화 cron 과 같은 crontab)::

    * * * * * cd /home/taeshkim/qa_qualitygate_portal && \
        flock -n /tmp/qg_reliability_sweep.lock \
        .venv/bin/python manage.py sweep_reliability >> /tmp/qg_reliability_sweep.log 2>&1

``flock -n`` 으로 이전 sweep 이 아직 돌고 있으면 그 분은 건너뛴다(외부 Jenkins 폴링이
느릴 때 중첩 방지). 화면 폴링(HTMX fragment)도 계속 sweep 하지만 그건 라이브 갱신용일
뿐, 스케줄링의 신뢰성은 이 cron 이 보장한다.

**단독 Suite Run 폴링도 겸한다 (2026-07-21)**: 스케줄과 무관하게 UI/API 로 트리거된
RUNNING TestSuiteRun 은 원래 화면(HTMX)이나 외부 폴러(api_v2 status 호출)가 폴링해야만
완료가 감지됐다 — 외부 폴러가 죽으면 39초짜리 빌드가 **하루 뒤에야 FAIL 알림**되는
문제(am-mapp-regression-api-dev seq 1284, 24시간 43분 소요로 표기). 이 명령이 매분
robustness 스케줄에 안 묶인 RUNNING run 도 `_poll_run_status` 로 돌아 완료 감지/알림이
브라우저·외부 폴러와 무관하게 1분 내에 이뤄진다.
"""

from __future__ import annotations

import logging
import threading

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.robustness.services import sweep_active_schedules
from apps.suites.models import TestSuiteRun
from apps.suites.views import _poll_run_status

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Reliability Test 활성 스케줄 sweep + 단독 RUNNING Suite Run 폴링 "
        "(cron 매분 호출용)."
    )

    def handle(self, *args, **opts):
        started = timezone.now()
        # force=True: 화면 폴링용 5초 쿨다운을 무시하고 항상 sweep (cron 은 의도적 호출).
        # 내부에서 각 스케줄을 try/except 로 감싸므로 한 스케줄 오류가 전체를 막지 않는다.
        sweep_active_schedules(force=True)

        # 단독(비-robustness) RUNNING Suite Run 폴링 — 외부 폴러/화면이 없어도
        # 완료 감지·PASS/FAIL 알림이 1분 내에 이뤄지게. robustness 에 묶인 run 은
        # 위 sweep 이 이미 폴링하므로 제외(이중 폴링 방지).
        standalone = (
            TestSuiteRun.objects
            .filter(status=TestSuiteRun.Status.RUNNING, robustness_runs__isnull=True)
            .select_related("suite")
        )
        polled = 0
        for run in standalone:
            try:
                _poll_run_status(run)
                polled += 1
            except Exception:
                logger.exception("standalone suite_run poll failed: %s", run.pk)
        if polled:
            self.stdout.write(f"standalone suite runs polled: {polled}")

        # UI Test Automation(TAP) — 매분 Suite 목록 싱크 + 진행 중 TapRun 폴링
        # (PRD docs/prd_ui_test_automation.md §3.2/§3.3 — crontab 무변경 재사용).
        # TAP 미설정/장애는 내부에서 격리되어 스케줄 sweep 에 영향 없다.
        try:
            from apps.suites.services_tap import poll_active_tap_runs, sync_tap_suites
            synced = sync_tap_suites()
            tap_polled = poll_active_tap_runs()
            if synced >= 0 or tap_polled:
                self.stdout.write(f"tap: suites synced={synced}, runs polled={tap_polled}")
        except Exception:
            logger.exception("TAP sync/poll failed")

        # API 탭 — 내부 Jenkins 'api' 폴더/잡 자동 싱크. 이 커맨드는 매분 돌지만 전체 폴더
        # 트리 스캔은 1분 주기로는 부하 과다라, cache.add 로 10분 쿨다운을 건다(그 창 안에서
        # 최초 1회만 True — robustness/services.py:_SWEEP_COOLDOWN 과 동일 패턴).
        # PRD docs/prd_ui_test_automation.md §3.4.
        try:
            from django.core.cache import cache
            from apps.suites.services_sync import sync_api_suites
            if cache.add("suites:api_jenkins_sync_cooldown", 1, 600):
                result = sync_api_suites()
                self.stdout.write(
                    f"api suite sync: created={result['created']}, synced={result['synced']}"
                )
        except Exception:
            logger.exception("API suite Jenkins sync failed")

        # Slack 알림은 데몬 스레드로 비동기 발송된다. 짧게 끝나는 cron 프로세스가 즉시
        # 종료되면 발송 중이던 알림(예: SUITE_RUN_STARTED/PASS/FAIL)이 잘릴 수 있어,
        # 메인 외 스레드가 끝나도록 잠깐 join 한다.
        main = threading.main_thread()
        for t in threading.enumerate():
            if t is not main and t.is_alive():
                t.join(timeout=15)

        elapsed = (timezone.now() - started).total_seconds()
        self.stdout.write(
            f"[{started:%Y-%m-%d %H:%M:%S}] reliability sweep done ({elapsed:.1f}s)"
        )
