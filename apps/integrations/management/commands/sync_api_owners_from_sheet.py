"""`python manage.py sync_api_owners_from_sheet` — Google Sheets → 담당자 마스터 동기화.

권장 cron — **매분 `--if-due` 로 호출**하고, 실제 주기(분)는 담당자 관리 화면에서
설정한다 (`OwnerSyncSettings.interval_minutes`). crontab 은 한 번만 등록::

    * * * * * cd /home/taeshkim/qa_qualitygate_portal && \
        .venv/bin/python manage.py sync_api_owners_from_sheet --if-due >> /tmp/qg_owner_sync.log 2>&1

`--if-due` 는 설정 주기와 마지막 동기화 시각(ActivityLog API_OWNER_IMPORTED) 을 비교해
**주기 도래 시에만** 동기화한다 (주기=0 이면 항상 skip). 인자 없이 호출하면 즉시 1회 동기화.

CSV 업로드를 대체하는 동기화의 핵심 진입점. 항상 full mirror (시트에 없는 imported
row 삭제, 수동 row 보존). 자격증명/시트 ID 는 .env (GOOGLE_CREDENTIALS_FILE 등 +
GSHEET_OWNERS_SPREADSHEET_ID). 자세한 설정은 .env.example 참고.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.integrations.google_sheets import GoogleSheetSyncError
from apps.integrations.models import OwnerSyncSettings
from apps.integrations.services import sync_owners_from_google_sheet


class Command(BaseCommand):
    help = "Google Sheets 의 담당자 마스터를 ApiEndpointOwner 로 동기화 (full mirror)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="실제 저장하지 않고 결과만 확인",
        )
        parser.add_argument(
            "--spreadsheet-id",
            default=None,
            help="대상 스프레드시트 ID 오버라이드 (기본: .env 의 GSHEET_OWNERS_SPREADSHEET_ID)",
        )
        parser.add_argument(
            "--if-due",
            action="store_true",
            help="화면에서 설정한 주기(분)가 도래했을 때만 동기화. 매분 도는 cron 용.",
        )

    def handle(self, *args, **opts):
        dry_run: bool = opts["dry_run"]
        spreadsheet_id = opts.get("spreadsheet_id")
        if_due: bool = opts["if_due"]

        # --if-due: 설정 주기 vs 마지막 동기화 시각으로 도래 여부 판단 (주기=0 이면 skip).
        if if_due:
            should_run, reason = self._due_status()
            self.stdout.write(f"[--if-due] {reason}")
            if not should_run:
                return

        # ActivityLog 기록용 — superuser / 어드민 우선. 없으면 None 도 허용.
        User = get_user_model()
        user = User.objects.filter(is_superuser=True).first()
        if user is None:
            user = User.objects.filter(role="qpe_admin").first()

        try:
            result = sync_owners_from_google_sheet(
                user=user,
                dry_run=dry_run,
                spreadsheet_id=spreadsheet_id,
            )
        except GoogleSheetSyncError as e:
            # cron(--if-due) 에선 실패해도 비정상 종료로 매분 알림 폭주하지 않게 경고만.
            # (실패 시 ActivityLog 미기록 → 다음 분에 다시 '도래' 로 자동 재시도)
            if if_due:
                self.stdout.write(self.style.WARNING(f"동기화 실패 (다음 주기에 재시도): {e}"))
                return
            raise CommandError(f"동기화 실패: {e}")

        self.stdout.write("=== Google Sheets 담당자 동기화 결과 ===")
        self.stdout.write(f"  created       : {result.created}")
        self.stdout.write(f"  updated       : {result.updated}")
        self.stdout.write(f"  skipped_manual: {result.skipped_manual}")
        self.stdout.write(f"  deleted_stale : {result.deleted_stale}")
        self.stdout.write(f"  errors        : {len(result.errors)}")
        if dry_run:
            self.stdout.write(self.style.WARNING("  (dry-run — 실제 저장되지 않음)"))

        if result.errors:
            self.stdout.write("")
            self.stdout.write("--- 에러 (상위 10건) ---")
            for e in result.errors[:10]:
                self.stdout.write(f"  row {e.get('row')}: {e.get('error')}")
            if len(result.errors) > 10:
                self.stdout.write(f"  ... 외 {len(result.errors) - 10}건")
            self.stdout.write(self.style.ERROR("실패"))
        else:
            self.stdout.write(self.style.SUCCESS("성공"))

    def _due_status(self) -> tuple[bool, str]:
        """(동기화해야 하나?, 사유) — 설정 주기와 마지막 동기화 시각 비교.

        - 주기 <= 0 → 자동 동기화 꺼짐 (skip)
        - 마지막 동기화 없음 → 즉시 도래
        - now - 마지막 < 주기 → 미도래 (skip), 남은 분 표시
        """
        from apps.activitylog.models import ActivityLog

        interval = OwnerSyncSettings.get_solo().interval_minutes
        if interval <= 0:
            return False, "자동 동기화 꺼짐 (주기=0)"

        last = (
            ActivityLog.objects.filter(
                action=ActivityLog.Action.API_OWNER_IMPORTED
            )
            .order_by("-created_at")
            .first()
        )
        if last is None:
            return True, f"마지막 동기화 기록 없음 → 동기화 ({interval}분 주기)"

        elapsed_min = (timezone.now() - last.created_at).total_seconds() / 60.0
        if elapsed_min < interval:
            remain = interval - elapsed_min
            return False, (
                f"미도래 (마지막 {last.created_at:%Y-%m-%d %H:%M}, "
                f"{interval}분 주기, 약 {remain:.0f}분 남음)"
            )
        return True, (
            f"도래 (마지막 {last.created_at:%Y-%m-%d %H:%M} 이후 "
            f"{elapsed_min:.0f}분 ≥ {interval}분) → 동기화"
        )
