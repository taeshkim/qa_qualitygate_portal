"""`python manage.py import_snapshot <SNAPSHOT_DIR>` — 번들을 대상 DB/설정으로 복원.

안전 우선: 선행 검증 → .importing 으로 해제·검증 → 기존 DB 는 .pre-import-<ts> 로 (하드링크) 보관 후 원자적 교체.
실패해도 기존 DB 는 그대로 남고 종료코드 ≠ 0. OSError(ENOSPC 등)·sqlite3.Error 는 트레이스백 없이 CommandError 로 보고.
"""
from __future__ import annotations

import errno
import os
import sqlite3
import sys
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from apps.syncops import snapshot as sn


class Command(BaseCommand):
    help = "snapshot 번들(DB + 선택적 config)을 이 서버에 복원합니다."

    def add_arguments(self, parser):
        parser.add_argument("snapshot_dir", help="번들 디렉터리 (manifest.json 이 있는 곳)")
        parser.add_argument("--target-db", help="복원 대상 DB (기본 settings default)")
        parser.add_argument("--restore-config", action="store_true",
                            help="config 번들(.env/secrets/deploy/crontab/systemd)을 --config-dir 에 풂 (덮어쓰지 않음)")
        parser.add_argument("--config-dir", help="config 를 풀 디렉터리 (기본 BASE_DIR)")
        parser.add_argument("--passphrase-env", metavar="VAR",
                            help="암호화된 config 복호화용 패스프레이즈가 든 환경변수명")
        parser.add_argument("--force", action="store_true",
                            help="대상 DB 에 데이터가 있거나 읽을 수 없어도(손상 의심) .pre-import-<ts> 로 보관 후 덮어씀 / "
                                 "config 는 .bak-<ts> 보관 후 교체. (코드가 데이터보다 오래된 경우는 풀리지 않음 → --allow-older-code)")
        parser.add_argument("--allow-older-code", action="store_true",
                            help="번들 마이그레이션이 이 서버 코드에 없어도(코드가 데이터보다 오래됨) 진행 — 비권장. --force 로는 풀리지 않음")
        parser.add_argument("--allow-same-host", action="store_true",
                            help="번들이 이 서버(같은 호스트·저장소/DB)에서 만들어진 것이어도 import 허용 (리허설/복구용)")
        parser.add_argument("--ignore-running", action="store_true", help="서비스가 떠 있어도 진행 (비권장)")
        parser.add_argument("--service-port", type=int, default=sn.DEFAULT_SERVICE_PORT,
                            help="서비스 실행 여부를 확인할 127.0.0.1 포트 (기본 8601)")
        parser.add_argument("--skip-migrate", action="store_true", help="복원 후 migrate 생략")
        parser.add_argument("--yes", action="store_true", help="확인 프롬프트 생략")

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        self._db_swapped = False
        try:
            self._run(**opts)
        except OSError as e:
            if self._db_swapped:
                state = "DB 교체는 이미 끝났습니다 (이후 단계에서 실패)."
            else:
                state = "DB 교체 전이므로 기존 DB 는 변경되지 않았고 .importing 임시 파일은 정리했습니다."
            if e.errno == errno.ENOSPC:
                raise CommandError(f"디스크 공간 부족(ENOSPC) — {state} 여유 공간을 확보한 뒤 다시 실행하세요.")
            where = f" [{e.filename}]" if getattr(e, "filename", None) else ""
            raise CommandError(
                f"파일 시스템 오류: {e.strerror or e}{where} — {state} "
                "`ls <대상 DB>*` 로 .pre-import-*/.importing 상태를 확인하세요."
            )
        except sqlite3.Error as e:
            state = "DB 교체는 이미 끝났습니다." if self._db_swapped else "기존 DB 는 변경되지 않았습니다."
            raise CommandError(f"SQLite 오류: {e} — {state}")

    def _run(self, **opts):
        w = self.stdout.write
        base_dir = Path(settings.BASE_DIR)
        default_db = Path(settings.DATABASES["default"]["NAME"])
        target = Path(opts["target_db"] or default_db)
        bundle = Path(opts["snapshot_dir"])
        force = opts["force"]
        config_dir = Path(opts["config_dir"]) if opts["config_dir"] else base_dir

        try:
            manifest = sn.load_manifest(bundle)
            w(f"번들: {bundle}  (생성 {manifest.get('created_at')}, 원본 서버 {manifest.get('source', {}).get('hostname')})")
            conflict = sn.same_host_conflict(manifest, base_dir, target)
            if conflict:
                if not opts["allow_same_host"]:
                    raise sn.SnapshotError(conflict)
                w(self.style.WARNING("  경고: " + conflict + " (--allow-same-host 로 진행)"))
            w("[import 1/6] 번들 파일 sha256 검증 ...")
            sn.verify_bundle_files(bundle, manifest, log=w, check_config=opts["restore_config"])

            w("[import 2/6] 사전 점검 ...")
            self._preflight(bundle, manifest, target, opts, force)
            self._confirm(manifest, target, opts)

            w("[import 3/6] DB 복원 ...")
            report = sn.import_db(bundle, manifest, target, force=force, log=w)
            self._db_swapped = True
            w(self.style.SUCCESS(f"  교체 완료: {target}"))
            if report.pre_import:
                w(f"  기존 DB 보관: {report.pre_import}  (확인 후 직접 삭제)")
            if report.restored_sidecars:
                w(f"  잔재 제거: {', '.join(report.restored_sidecars)}")
        except sn.SnapshotError as e:
            raise CommandError(str(e))

        # migrate
        is_default = target.resolve() == default_db.resolve()
        if opts["skip_migrate"]:
            w("[import 4/6] migrate 생략 (--skip-migrate)")
        elif not is_default:
            w(f"[import 4/6] migrate 자동 생략 — --target-db({target}) 가 settings 기본 DB({default_db}) 가 아님.")
        else:
            w("[import 4/6] migrate 실행 ...")
            connections.close_all()
            try:
                call_command("migrate", interactive=False, verbosity=1, stdout=self.stdout)
            except Exception as e:  # noqa: BLE001
                raise CommandError(
                    f"DB 는 복원됐지만 migrate 실패: {e}\n"
                    "  -> 수동으로 `python manage.py migrate` 를 실행하세요 (복원 자체는 완료)."
                )

        # config
        if opts["restore_config"]:
            w("[import 5/6] config 복원 ...")
            try:
                res = sn.restore_config(bundle, manifest, config_dir, opts["passphrase_env"], force=force, log=w)
            except sn.SnapshotError as e:
                raise CommandError(f"DB 복원은 끝났지만 config 복원 실패: {e}")
            for r in res["written"]:
                w(f"  복원: {r}")
            for r in res["incoming"]:
                w(self.style.WARNING(f"  기존 파일 존재 -> {r} 로 저장 (직접 비교 후 교체)"))
            for r in res["backed_up"]:
                w(f"  기존 파일 보관: {r}")
            hints = sn.system_install_hints(config_dir, res)
            if hints:
                w("  시스템 설치는 자동으로 하지 않았습니다. 필요하면 직접 실행:")
                for h in hints:
                    w(f"    {h}")
                w(self.style.WARNING("  " + sn.SYSTEM_INSTALL_NOTE))
        else:
            w("[import 5/6] config 복원 생략 (--restore-config 없음)")

        w("[import 6/6] 요약")
        self._summary(target, manifest, bundle)

    # ------------------------------------------------------------------
    def _preflight(self, bundle: Path, manifest, target: Path, opts, force: bool):
        w = self.stdout.write
        port = opts["service_port"]
        if sn.service_running(port):
            if not opts["ignore_running"]:
                raise sn.SnapshotError(
                    f"127.0.0.1:{port} 에 서비스가 떠 있습니다. 서비스·cron 을 멈춘 뒤 다시 실행하세요 "
                    "(무시하려면 --ignore-running)."
                )
            w(self.style.WARNING(f"  경고: 127.0.0.1:{port} 서비스가 실행 중이지만 --ignore-running 으로 진행."))
        known = sn.disk_migration_keys()
        unknown = sn.unknown_migrations(manifest, known)
        if unknown:
            names = ", ".join(f"{a}.{n}" for a, n in unknown[:5])
            msg = (f"번들 DB 에 이 코드에 없는 마이그레이션 {len(unknown)}건 ({names} ...) — 이 서버의 코드가 데이터보다 "
                   "오래됐습니다. `git pull` 로 코드를 갱신하세요 "
                   "(--force 로는 풀리지 않음; 정말 무시하려면 --allow-older-code, 비권장).")
            if not opts["allow_older_code"]:
                raise sn.SnapshotError(msg)
            w(self.style.WARNING("  경고: " + msg))
        if target.exists() and target.stat().st_size > 0:
            data = None
            try:
                conn = sn.open_ro(target)
                try:
                    data = sn.user_data_tables(conn)
                finally:
                    conn.close()
            except sqlite3.Error as e:
                msg = (f"대상 DB 를 읽을 수 없음 — 손상 의심 ({e}). "
                       "--force 로 pre-import 보관 후 교체하세요.")
                if not force:
                    raise sn.SnapshotError(msg)
                w(self.style.WARNING("  경고: " + msg.replace("--force 로 pre-import 보관 후 교체하세요.",
                                                             "--force 이므로 .pre-import-<ts> 로 보관 후 교체합니다.")))
            if data:
                top = ", ".join(f"{t}({c})" for t, c in sorted(data.items(), key=lambda kv: -kv[1])[:5])
                msg = (f"대상 DB 에 이미 데이터가 있습니다: {top} ... 덮어쓰려면 --force "
                       "(기존 DB 는 .pre-import-<ts> 로 보관됨).")
                if not force:
                    raise sn.SnapshotError(msg)
                w(self.style.WARNING("  경고: " + msg))
        need = int(manifest["db"]["original_size"] * 1.05) + 64 * 1024 * 1024
        free = sn.free_bytes(target.parent)
        if free < need:
            raise sn.SnapshotError(
                f"디스크 여유 부족: {target.parent} 여유 {sn.human(free)} < 필요 {sn.human(need)} "
                "(기존 DB 는 보관되므로 압축 해제본 분량이 추가로 필요)."
            )
        if opts["restore_config"]:
            cfg = manifest.get("config")
            if not cfg:
                raise sn.SnapshotError("--restore-config 를 지정했지만 번들에 config 가 없습니다.")
            if cfg["encrypted"] and not (opts["passphrase_env"] and os.environ.get(opts["passphrase_env"])):
                raise sn.SnapshotError(
                    "config 가 암호화돼 있습니다 — --passphrase-env VAR 로 패스프레이즈가 든 환경변수를 지정하세요 "
                    "(예: read -rs QG_PASS; export QG_PASS)."
                )
            if cfg["encrypted"]:
                w("  config 패스프레이즈 검증 (첫 블록 복호화) ...")
                sn.verify_config_passphrase(bundle, manifest, opts["passphrase_env"])
        w(f"  OK (서비스 포트 {port} 비어 있음, 마이그레이션 호환, 디스크 여유 {sn.human(free)})")

    def _confirm(self, manifest, target: Path, opts):
        if opts["yes"]:
            return
        w = self.stdout.write
        w(f"\n  복원 대상 : {target}\n  원본 DB   : {sn.human(manifest['db']['original_size'])}, 테이블 {len(manifest['tables'])}개\n"
          f"  config    : {'복원함' if opts['restore_config'] else '복원 안 함'}")
        if not sys.stdin.isatty():
            raise sn.SnapshotError("대화형 터미널이 아닙니다 — 확인하려면 --yes 를 지정하세요.")
        ans = input("진행할까요? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            raise sn.SnapshotError("사용자 취소.")

    def _summary(self, target: Path, manifest, bundle: Path):
        w = self.stdout.write
        conn = sn.open_ro(target)
        try:
            counts = sn.table_counts(conn)
        finally:
            conn.close()
        rows = [(t, c) for t, c in sorted(counts.items(), key=lambda kv: -kv[1])]
        for line in sn.format_table(((t, c, manifest["tables"].get(t)) for t, c in rows), ("table", "rows", "manifest")):
            w(line)
        w(f"\n테이블 {len(counts)}개, 총 {sum(counts.values()):,}행. "
          f"검증: python manage.py verify_snapshot {bundle} --allow-newer")

