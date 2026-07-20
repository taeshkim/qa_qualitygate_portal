"""`python manage.py import_api_owners <csv_path>` — CSV 일괄 임포트 CLI.

Phase 1 검증용. Admin UI 는 Phase 2 에서 추가.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.integrations.services import import_api_endpoint_owners


class Command(BaseCommand):
    help = "API 엔드포인트 담당자 CSV 를 ApiEndpointOwner 테이블로 임포트합니다."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="CSV 파일 경로")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="실제 저장하지 않고 결과만 확인",
        )
        parser.add_argument(
            "--remove-stale",
            action="store_true",
            default=False,
            help="CSV 에 없는 imported row 삭제",
        )

    def handle(self, *args, **opts):
        csv_path: str = opts["csv_path"]
        dry_run: bool = opts["dry_run"]
        remove_stale: bool = opts["remove_stale"]

        User = get_user_model()
        user = User.objects.filter(is_superuser=True).first()
        if user is None:
            user = User.objects.filter(role="qpe_admin").first()
        # user 가 여전히 None 이어도 services 는 user=None 을 허용함

        try:
            result = import_api_endpoint_owners(
                csv_path,
                user=user,
                dry_run=dry_run,
                remove_stale=remove_stale,
            )
        except FileNotFoundError as e:
            raise CommandError(f"CSV 파일을 찾을 수 없습니다: {e}")

        self.stdout.write("=== API 엔드포인트 담당자 CSV 임포트 결과 ===")
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

        if result.errors:
            self.stdout.write(self.style.ERROR("실패"))
        else:
            self.stdout.write(self.style.SUCCESS("성공"))
