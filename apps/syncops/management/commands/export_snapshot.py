"""`python manage.py export_snapshot` — 전체 데이터 스냅샷 번들 생성 (서버 이관용).

DB(sqlite3 backup API) + 비밀/설정(.env, secrets/, deploy/, crontab, systemd 유닛) + manifest.
운영 DB 는 mode=ro 로만 읽는다. --dry-run 은 아무것도 쓰지 않는다.
SIGHUP/SIGTERM(ssh 단절·kill)을 받으면 임시 파일(.partial)과 만든 출력 디렉터리를 정리하고 종료한다.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.syncops import snapshot as sn

DB_PLAINTEXT_WARNING = (
    "DB 는 암호화되지 않습니다(세션·해시 포함) — 외부 매체로 옮길 때 별도 암호화"
)


class Command(BaseCommand):
    help = "DB + 비밀/설정 전체를 snapshots/qg-<UTC>/ 번들로 내보냅니다 (서버 이관용)."

    def add_arguments(self, parser):
        parser.add_argument("--out", help="출력 디렉터리 (기본 <BASE_DIR>/snapshots/qg-<UTC ts>/)")
        parser.add_argument("--db-path", help="원본 DB (기본 settings DATABASES default NAME)")
        parser.add_argument("--no-config", action="store_true", help="config(.env/secrets/...) 제외")
        parser.add_argument(
            "--passphrase-env", metavar="VAR",
            help="config 를 암호화할 패스프레이즈가 들어있는 환경변수명 (값은 명령행으로 넘기지 않음)",
        )
        parser.add_argument("--compress", choices=["auto", "zstd", "gzip", "none"], default="auto")
        parser.add_argument(
            "--max-backup-seconds", type=int, default=sn.DEFAULT_MAX_BACKUP_SECONDS,
            help="DB 백업 제한 시간(초). 쓰기 간섭으로 재시작이 반복돼 넘으면 실패 (기본 1800)",
        )
        parser.add_argument(
            "--api-bundle", action="store_true",
            help="HTTPS export API(/api/v2/export/snapshots/<id>/)가 그대로 내려줄 수 있는 번들로 만든다 "
                 "(snapshots/api-<uuid>/ + state.json=ready, config 제외). 서비스를 멈춘 채 만들어 "
                 "쓰기 간섭(백업 재시작)을 피한 뒤 서비스를 올리고 fetch_snapshot_via_api --existing-id 로 받는다.",
        )
        parser.add_argument("--dry-run", action="store_true", help="아무것도 쓰지 않고 계획만 출력")

    def handle(self, *args, **opts):
        base_dir = Path(settings.BASE_DIR)
        db_path = Path(opts["db_path"] or settings.DATABASES["default"]["NAME"])
        out_dir = Path(opts["out"]) if opts["out"] else sn.default_out_dir(base_dir)
        include_config = not opts["no_config"]
        api_id = None
        if opts["api_bundle"]:
            if opts["out"]:
                raise CommandError("--api-bundle 은 출력 위치를 자동으로 정합니다 (--out 과 함께 쓸 수 없음).")
            if opts["passphrase_env"]:
                raise CommandError("--api-bundle 은 config 를 포함하지 않습니다 (--passphrase-env 불필요).")
            if opts["compress"] == "none":
                raise CommandError("--api-bundle 은 압축이 필요합니다 (--compress gzip|zstd|auto).")
            from apps.api_v2 import views_export as ve  # 상태 파일 형식의 단일 진실 소스
            import uuid
            api_id = str(uuid.uuid4())
            out_dir = ve._bundle_dir(api_id)
            include_config = False
        pass_env = opts["passphrase_env"]
        if pass_env and not include_config:
            raise CommandError("--passphrase-env 는 config 를 포함할 때만 의미가 있습니다 (--no-config 와 함께 쓸 수 없음).")

        try:
            if opts["dry_run"]:
                self._dry_run(db_path, base_dir, out_dir, opts, include_config, pass_env)
                return
            with sn.interrupt_guard():  # SIGHUP/SIGTERM → 기존 실패 정리 경로(.partial·출력 디렉터리 삭제)
                manifest = sn.export_snapshot(
                    db_path=db_path, base_dir=base_dir, out_dir=out_dir,
                    compress=opts["compress"], include_config=include_config,
                    passphrase_env=pass_env, max_backup_seconds=opts["max_backup_seconds"],
                    log=self.stdout.write,
                )
        except sn.SnapshotError as e:
            raise CommandError(str(e))

        if api_id:
            now = ve._now()
            ve._write_state(out_dir, {
                "id": api_id, "status": "ready", "progress_pct": 100,
                "created_at": ve._iso(now), "finished_at": ve._iso(now), "error": None,
                "compress": opts["compress"], "pid": None, "heartbeat": None,
            })
            self.stdout.write(self.style.SUCCESS(f"API 번들 id: {api_id}"))
            self.stdout.write("  서비스를 올린 뒤 새 서버에서: bash ~/qg-fetch.sh " + api_id)
        total = sum(f.stat().st_size for f in out_dir.iterdir() if f.is_file())
        self.stdout.write(self.style.SUCCESS(f"\n번들 생성 완료: {out_dir}  ({sn.human(total)})"))
        self.stdout.write(f"  테이블 {len(manifest['tables'])}개, 마이그레이션 {len(manifest['migrations'])}건, "
                          f"DB sha256={manifest['db']['sha256'][:16]}...")
        if manifest["config"] and not manifest["config"]["encrypted"]:
            self.stdout.write(self.style.WARNING("  [경고] config.tar 가 평문입니다 (0600). 전송/보관에 주의."))
        self.stdout.write(self.style.WARNING("  [경고] " + DB_PLAINTEXT_WARNING + "."))
        self.stdout.write(
            "다음 단계: 새 서버에서 `python manage.py sync_from_source --source USER@HOST --source-dir <경로>` "
            f"(pull) 또는 번들 디렉터리를 옮겨 `python manage.py import_snapshot {out_dir} --yes`."
        )

    def _dry_run(self, db_path, base_dir, out_dir, opts, include_config, pass_env):
        plan = sn.plan_export(
            db_path, base_dir, out_dir, compress=opts["compress"],
            include_config=include_config, passphrase_env=pass_env,
        )
        w = self.stdout.write
        w(self.style.WARNING("[dry-run] 아무것도 쓰지 않습니다 (DB 는 mode=ro)."))
        w(f"DB            : {plan['db_path']}")
        w(f"  크기        : {sn.human(plan['db_size'])}  (page_size={plan['page_size']}, journal_mode={plan['journal_mode']})")
        w(f"  테이블      : {len(plan['tables'])}개, 총 {sum(plan['tables'].values()):,}행")
        top = sorted(plan["tables"].items(), key=lambda kv: -kv[1])[:10]
        for t, c in top:
            w(f"    {t:<48} {c:>12,}")
        w(f"압축          : {plan['compression']}")
        if include_config:
            w(f"config 항목   : {', '.join(plan['config_entries']) or '(없음)'}")
            w(f"  암호화      : {'예 (' + pass_env + ')' if plan['config_encrypted'] else '아니오 — 평문 0600 (경고)'}")
        else:
            w("config        : 제외 (--no-config)")
        w(f"예상 번들 크기: 약 {sn.human(plan['est_bundle_size'])} (압축률 추정치)")
        w(self.style.WARNING("[경고] " + DB_PLAINTEXT_WARNING + "."))
        w(f"출력 위치     : {plan['out_dir']}")
        w(f"디스크 여유   : {sn.human(plan['free'])} / 필요 약 {sn.human(plan['need'])} "
          f"-> {'OK' if plan['space_ok'] else self.style.ERROR('부족')}")
        if not plan["space_ok"]:
            raise CommandError("디스크 여유 공간 부족 — --out 을 다른 디스크로 지정하세요.")
