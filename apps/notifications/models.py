"""Slack 알림 설정 모델 (singleton, PK=1)."""

from __future__ import annotations

from django.db import models


class SlackNotificationSettings(models.Model):
    """싱글톤 설정. PK=1 한 행만 사용한다.

    Slack 토큰은 .env (`SLACK_BOT_TOKEN`) 에 두고, 채널과 이벤트별 토글은 이 모델에서 관리.
    """

    enabled = models.BooleanField(default=False, verbose_name="Slack 알림 사용")
    default_channel = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="예: #qg-alerts (비워두면 .env 의 SLACK_DEFAULT_CHANNEL 사용)",
    )

    # Run 단위
    notify_run_started = models.BooleanField(default=True, verbose_name="Run 시작")
    notify_run_completed = models.BooleanField(default=True, verbose_name="Run 성공 종료")
    notify_run_failed = models.BooleanField(default=True, verbose_name="Run 실패")
    notify_run_cancelled = models.BooleanField(default=False, verbose_name="Run 취소")

    # Image 단계
    notify_image_started = models.BooleanField(default=False, verbose_name="이미지 빌드 시작")
    notify_image_completed = models.BooleanField(default=False, verbose_name="이미지 빌드 완료")
    notify_image_failed = models.BooleanField(default=True, verbose_name="이미지 빌드 실패")

    # Deploy 단계 (검수대상 배포)
    notify_deploy_started = models.BooleanField(default=False, verbose_name="검수대상 배포 시작")
    notify_deploy_completed = models.BooleanField(default=False, verbose_name="검수대상 배포 완료")
    notify_deploy_failed = models.BooleanField(default=True, verbose_name="검수대상 배포 실패")

    # Prod 단계 (검수통과 배포)
    notify_prod_started = models.BooleanField(default=False, verbose_name="검수통과 배포 시작")
    notify_prod_completed = models.BooleanField(default=True, verbose_name="검수통과 배포 완료")
    notify_prod_failed = models.BooleanField(default=True, verbose_name="검수통과 배포 실패")

    # Test Suite — Quality Gate 파이프라인 내부
    notify_test_suite_started = models.BooleanField(default=False, verbose_name="Test Suite 시작")
    notify_test_suite_passed = models.BooleanField(default=False, verbose_name="Test Suite PASS")
    notify_test_suite_failed = models.BooleanField(default=True, verbose_name="Test Suite FAIL")

    # Test Suite — 단독 실행
    notify_suite_run_started = models.BooleanField(default=False, verbose_name="Suite Run 시작 (단독)")
    notify_suite_run_passed = models.BooleanField(default=False, verbose_name="Suite Run PASS (단독)")
    notify_suite_run_failed = models.BooleanField(default=True, verbose_name="Suite Run FAIL (단독)")

    # 공통 에러
    # UI Test Automation (TAP) — 단독/스케줄 공통
    notify_tap_run_started = models.BooleanField(default=False, verbose_name="UI Suite 실행 시작")
    notify_tap_run_passed = models.BooleanField(default=False, verbose_name="UI Suite PASS")
    notify_tap_run_failed = models.BooleanField(default=True, verbose_name="UI Suite FAIL")

    notify_argocd_failed = models.BooleanField(default=True, verbose_name="Argo CD Sync 실패")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Slack 알림 설정"
        verbose_name_plural = "Slack 알림 설정"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls) -> "SlackNotificationSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
