"""apps/suites/throttle.py — 잡 트리거 지연 throttle (single source of truth).

동시에 같은 suite 의 여러 Jenkins 빌드를 한꺼번에 트리거하면 꼬이는 이슈가 있어,
모든 잡 트리거 경로(단독 Suite Run idle/지정/체인, Reliability Test 순차/병렬) 는
Jenkins 빌드를 트리거하기 **직전** 에 ``claim_trigger_slot(suite, delay_seconds)`` 를
호출한다.

- 그 suite 의 ``last_job_triggered_at`` 으로부터 ``delay_seconds`` 가 지났으면 →
  슬롯을 ``now`` 로 원자적 점유하고 **True** (호출자는 트리거 진행).
- 아직이면 → 갱신하지 않고 **False** (호출자는 잡을 WAITING 으로 보류, 다음
  폴링/sweep 에서 재시도).

**scope = suite 단위.** 서로 다른 suite/스케줄은 각자의 타임스탬프를 쓰므로 간섭하지
않는다. 한 suite 안에서만 트리거가 ``delay`` 간격으로 분산된다:

- **병렬 실행**(Reliability ``parallel_jobs`` ON): 한 sweep 에서 그 suite 의 슬롯을
  점유하는 잡은 1개뿐, 나머지는 WAITING. 이후 sweep 마다 ``delay`` 가 지난 잡이 1개씩
  추가로 트리거된다. 먼저 트리거된 잡은 계속 RUNNING 이므로 **잡들이 동시에 시작/완료
  되지 않고 ``delay`` 만큼 엇갈려 시작 → 병렬로 함께 돌다가 엇갈려 완료**된다.
  (순차 = '앞 잡 완료까지 대기' 가 아니다. 트리거 시점만 ``delay`` 간격으로 미룬다.)
- **동시 idle 호출**(같은 suite 를 여러 번): 각 호출이 만든 run 의 잡이 ``delay``
  간격으로 트리거되어 한꺼번에 Jenkins API 를 때리지 않는다.

조건부 UPDATE 한 번으로 경쟁 없이 정확히 1개 호출만 슬롯을 가져간다(robustness sweep
의 interval race-free 패턴과 동일).
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import TestSuite

# 잡에 trigger_delay_seconds 가 비정상값일 때의 폴백 (모델 default 와 동일).
DEFAULT_TRIGGER_DELAY_SECONDS = 30


def _normalize_delay(delay_seconds) -> int:
    try:
        return max(0, int(delay_seconds))
    except (TypeError, ValueError):
        return DEFAULT_TRIGGER_DELAY_SECONDS


def claim_trigger_slot(suite, delay_seconds) -> bool:
    """그 suite 의 마지막 트리거 후 ``delay_seconds`` 가 지났으면 슬롯을 점유하고 True.

    점유에 성공하면 ``suite.last_job_triggered_at`` 을 ``now`` 로 갱신한다(이후 잡은 이
    시각을 기준으로 다시 자기 딜레이만큼 기다린다). 아직이면 갱신 없이 False.

    ``delay <= 0`` 이면 throttle 을 끈 것 — 항상 통과하되 타임스탬프는 갱신한다(직전에
    API 를 쏜 사실은 남겨, 같은 suite 의 다음 잡이 자기 딜레이만큼 띄우도록).

    주의: 슬롯을 점유한 뒤 실제 ``trigger_build`` 가 실패해도 슬롯은 소비된 채로 둔다 —
    실패한 트리거를 곧바로 재시도하지 않고 딜레이만큼 backoff 하는 게 "동시 호출 꼬임"
    방지 의도에 부합(보수적).

    ``suite`` 는 ``TestSuite`` 인스턴스 또는 pk 둘 다 허용.
    """
    delay = _normalize_delay(delay_seconds)
    now = timezone.now()
    suite_pk = getattr(suite, "pk", suite)

    qs = TestSuite.objects.filter(pk=suite_pk)
    if delay > 0:
        cutoff = now - timedelta(seconds=delay)
        qs = qs.filter(
            Q(last_job_triggered_at__isnull=True)
            | Q(last_job_triggered_at__lte=cutoff)
        )
    # 조건부 UPDATE — 동시 호출 중 정확히 1개만 1 row affected.
    updated = qs.update(last_job_triggered_at=now)
    return updated == 1


def seconds_until_slot(suite, delay_seconds) -> int:
    """다음 트리거까지 남은 대기 시간(초, 0 이상). 화면 표시/디버그용 (게이트 X)."""
    delay = _normalize_delay(delay_seconds)
    if delay <= 0:
        return 0
    suite_pk = getattr(suite, "pk", suite)
    last = (
        TestSuite.objects.filter(pk=suite_pk)
        .values_list("last_job_triggered_at", flat=True)
        .first()
    )
    if last is None:
        return 0
    elapsed = (timezone.now() - last).total_seconds()
    return max(0, int(delay - elapsed))
