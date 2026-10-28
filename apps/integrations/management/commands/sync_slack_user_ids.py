"""포털 User 이메일 → Slack member ID 자동 매핑 (User.slack_user_id 백필).

Slack Triage 버튼(Failure/Skip) 권한 검사에 필요. `users.list` 의 email 로 매칭.
수동: `manage.py sync_slack_user_ids [--overwrite]`.
자동: `sync_owners_from_google_sheet`(담당자 시트 동기화) 끝에서 best-effort 호출.
"""
from django.core.management.base import BaseCommand

from apps.integrations.slack_user_sync import backfill_user_slack_ids


class Command(BaseCommand):
    help = "포털 User 이메일로 Slack member ID 를 조회해 User.slack_user_id 를 채운다."

    def add_arguments(self, parser):
        parser.add_argument("--overwrite", action="store_true",
                            help="이미 값이 있는 유저도 최신 Slack ID 로 갱신")

    def handle(self, *args, **options):
        res = backfill_user_slack_ids(overwrite=options["overwrite"])
        self.stdout.write(self.style.SUCCESS(
            f"매핑 {res['mapped']} · 기존유지 {res['already']} · "
            f"미매칭 {len(res['unmatched'])} (Slack index {res['index_size']})"))
        if res.get("unmatched"):
            self.stdout.write("  미매칭(이메일 불일치): " + ", ".join(res["unmatched"]))
