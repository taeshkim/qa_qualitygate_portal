"""`python manage.py verify_snapshot <manifest|dir>` — 대상 DB 행 수를 manifest 와 비교."""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.syncops import snapshot as sn


NOTE = "참고: 행 수·마이그레이션만 비교합니다. 내용 무결성은 import 단계의 sha256 이 보증합니다."


class Command(BaseCommand):
    help = "대상 DB 의 테이블별 행 수를 snapshot manifest 와 비교합니다 (DB 는 mode=ro). 불일치 시 종료코드 1."

    def add_arguments(self, parser):
        parser.add_argument("manifest", help="manifest.json 경로 또는 번들 디렉터리")
        parser.add_argument("--target-db", help="대상 DB (기본 settings default)")
        parser.add_argument("--allow-newer", action="store_true",
                            help="대상이 더 많은 행(이관 후 신규 데이터)은 허용")

    def handle(self, *args, **opts):
        try:
            manifest = sn.load_manifest(Path(opts["manifest"]))
            target = Path(opts["target_db"] or settings.DATABASES["default"]["NAME"])
            res = sn.compare_counts(manifest, target, allow_newer=opts["allow_newer"])
        except sn.SnapshotError as e:
            raise CommandError(str(e))

        w = self.stdout.write
        w(f"manifest: {manifest.get('created_at')}  source={manifest.get('source', {}).get('hostname')}")
        w(f"target  : {target}")
        for line in sn.format_table(res["rows"], ("table", "manifest", "target", "status")):
            w(line)
        w(f"\n마이그레이션: manifest 에만 있음(대상 미적용) {len(res['mig_missing'])}건, "
          f"대상에만 있음(이관 후 적용) {len(res['mig_extra'])}건")
        for a, n in res["mig_missing"][:20]:
            w(self.style.ERROR(f"  - 미적용: {a}.{n}"))
        for a, n in res["mig_extra"][:20]:
            w(f"  + 추가: {a}.{n}")
        if not res["ok"]:
            w(NOTE)
            raise CommandError("검증 실패: 행 수/마이그레이션 불일치 (위 표 참고).")
        w(self.style.SUCCESS("검증 OK"))
        w(NOTE)
