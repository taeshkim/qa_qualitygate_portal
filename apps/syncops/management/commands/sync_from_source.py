"""`python manage.py sync_from_source` — 새 서버에서 실행하는 풀(pull) 동기화 오케스트레이터.

① ssh 로 원격 export_snapshot  ② rsync 수신(재개 가능)  ③ manifest/sha256 검증 + 원격 임시 번들 삭제
④ import_snapshot  ⑤ verify_snapshot  ⑥ 마무리.
③ 이후 단계가 실패해도 재개는 로컬 번들(import_snapshot <로컬번들>)로 하므로 원격 번들은 ③ 직후 지운다(--keep-remote 면 보존).
모든 실패 메시지는 원격 번들 경로와 정리 명령, 사용자가 준 옵션을 그대로 재구성한 재개 명령을 포함한다.
ssh 불가(IDC↔AWS 차단 등)면 번들 디렉터리를 아무 수단(scp·S3·USB)으로 옮겨 `import_snapshot` 만 쓰면 된다.
패스프레이즈 값은 명령행/로그에 절대 노출하지 않고 ssh stdin 으로만 전달한다.
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.syncops import snapshot as sn

REMOTE_PASS_ENV = sn.CONFIG_PASS_ENV_FIXED

# --source 는 USER@HOST 형식만 허용 ('-' 로 시작하면 ssh/rsync 옵션 주입 위험)
SOURCE_RE = re.compile(r"([A-Za-z0-9._-]+@)?[A-Za-z0-9._:\[\]-]+")
# 긴 단계(원격 export·rsync) 동안 유휴 연결이 끊기지 않게 하는 기본 keepalive. 사용자 --ssh-opts 가 먼저 오므로 덮어쓸 수 있다.
SSH_KEEPALIVE_OPTS = ["-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=20"]


def validate_source(source: str) -> str:
    if not source or source.startswith("-") or not SOURCE_RE.fullmatch(source):
        raise CommandError(f"--source 는 USER@HOST 형식이어야 합니다 ('-' 로 시작 불가): {source!r}")
    return source


def run_cmd(cmd: list[str], input_text: str | None = None) -> int:
    """서브프로세스 실행 (stdout/stderr 는 그대로 흘려 진행률을 보여줌). 테스트에서 대체 가능."""
    return subprocess.run(
        cmd,
        input=input_text.encode() if input_text is not None else None,
        stdin=None if input_text is not None else subprocess.DEVNULL,
    ).returncode


def build_remote_export_command(
    source_dir: str, python: str, remote_out: str, with_config: bool, with_pass: bool,
    max_backup_seconds: int | None = None, compress: str | None = None,
) -> str:
    """원격 셸에서 실행할 한 줄 명령. 패스프레이즈는 stdin 첫 줄로 받아 환경변수로만 구성."""
    parts = [f"cd {shlex.quote(source_dir)}"]
    args = f"{shlex.quote(python)} manage.py export_snapshot --out {shlex.quote(remote_out)}"
    if max_backup_seconds is not None:
        args += f" --max-backup-seconds {int(max_backup_seconds)}"
    if compress:
        args += f" --compress {shlex.quote(compress)}"
    if not with_config:
        args += " --no-config"
    elif with_pass:
        parts.append(f"IFS= read -r {REMOTE_PASS_ENV}")
        parts.append(f"export {REMOTE_PASS_ENV}")
        args += f" --passphrase-env {REMOTE_PASS_ENV}"
    parts.append(args)
    return " && ".join(parts)


class Command(BaseCommand):
    help = "원본(운영) 서버에서 전체 데이터를 당겨와(pull) 이 서버에 복원·검증합니다."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="USER@HOST (ssh 키 인증, 비밀번호 입력 없음; '-' 로 시작 불가)")
        parser.add_argument("--source-dir", required=True, help="원본 서버의 저장소 절대경로")
        parser.add_argument("--source-python", help="원본 python (기본 <source-dir>/.venv/bin/python)")
        parser.add_argument("--ssh-opts", default="",
                            help='추가 ssh 옵션 문자열 (예: "-p 2222 -i ~/.ssh/k"). 기본으로 BatchMode=yes + '
                                 'ServerAliveInterval=30/ServerAliveCountMax=20 이 포함되며 이 옵션이 먼저 적용된다')
        parser.add_argument("--local-dir", help="번들 수신 디렉터리 (기본 <BASE_DIR>/snapshots)")
        parser.add_argument("--include-config", action="store_true", help=".env/secrets/crontab 등 config 도 가져옴")
        parser.add_argument("--passphrase-env", metavar="VAR",
                            help="config 암호화/복호화 패스프레이즈가 든 로컬 환경변수명 (ssh stdin 으로만 전달)")
        parser.add_argument("--keep-remote", action="store_true",
                            help="원격 임시 번들을 삭제하지 않음 (기본: ③ 검증 직후 삭제)")
        parser.add_argument("--remote-bundle", help="이미 만들어 둔 원격 번들 경로 (① 생략, 재개용)")
        parser.add_argument("--max-backup-seconds", type=int, default=None,
                            help="원격 export 의 DB 백업 제한 시간(초) — 원격 export_snapshot 에 전달 (기본: 원격 기본값 1800)")
        parser.add_argument("--compress", choices=["auto", "zstd", "gzip", "none"], default=None,
                            help="원격 export 의 압축 방식 — 원격 export_snapshot 에 전달 (기본: 원격 기본값 auto)")
        parser.add_argument("--dry-run", action="store_true", help="실행할 명령만 출력 (값은 마스킹)")
        # import 통과 옵션
        parser.add_argument("--target-db")
        parser.add_argument("--restore-config", action="store_true")
        parser.add_argument("--force", action="store_true",
                            help="import 통과: 대상 DB 데이터 덮어쓰기/읽기 불가 DB 교체, config .bak 교체 "
                                 "(코드가 오래된 경우는 --allow-older-code)")
        parser.add_argument("--allow-older-code", action="store_true",
                            help="import 통과: 번들 마이그레이션이 이 코드에 없어도 진행 (비권장)")
        parser.add_argument("--allow-same-host", action="store_true",
                            help="import 통과: 같은 호스트(원본 서버)에서 import 허용 (리허설/복구용)")
        parser.add_argument("--yes", action="store_true")
        parser.add_argument("--ignore-running", action="store_true")
        parser.add_argument("--skip-migrate", action="store_true")
        parser.add_argument("--service-port", type=int, default=sn.DEFAULT_SERVICE_PORT)

    # ------------------------------------------------------------------
    @staticmethod
    def _import_opt_args(opts, pass_env, with_pass: bool = True) -> list[str]:
        """import_snapshot 에 줄 옵션을 사용자가 준 그대로 재구성 (비밀 값 없음 — 패스프레이즈는 변수명만)."""
        a: list[str] = []
        if opts["target_db"]:
            a += ["--target-db", opts["target_db"]]
        for flag, key in (("--restore-config", "restore_config"), ("--force", "force"),
                          ("--yes", "yes"), ("--ignore-running", "ignore_running"),
                          ("--skip-migrate", "skip_migrate"), ("--allow-same-host", "allow_same_host"),
                          ("--allow-older-code", "allow_older_code")):
            if opts[key]:
                a.append(flag)
        if with_pass and pass_env and opts["restore_config"]:
            a += ["--passphrase-env", pass_env]
        if opts["service_port"] != sn.DEFAULT_SERVICE_PORT:
            a += ["--service-port", str(opts["service_port"])]
        return a

    def _sync_rerun_cmd(self, opts, source, source_dir, pass_env, remote_bundle: str | None) -> str:
        """sync_from_source 재실행 명령 — 사용자가 준 모든 옵션을 재구성."""
        a = ["python", "manage.py", "sync_from_source", "--source", source, "--source-dir", source_dir]
        if opts["source_python"]:
            a += ["--source-python", opts["source_python"]]
        if opts["ssh_opts"]:
            a += ["--ssh-opts", opts["ssh_opts"]]
        if opts["local_dir"]:
            a += ["--local-dir", opts["local_dir"]]
        if opts["include_config"]:
            a.append("--include-config")
        if pass_env:
            a += ["--passphrase-env", pass_env]
        if opts["keep_remote"]:
            a.append("--keep-remote")
        if remote_bundle:
            a += ["--remote-bundle", remote_bundle]
        else:
            if opts["max_backup_seconds"] is not None:
                a += ["--max-backup-seconds", str(opts["max_backup_seconds"])]
            if opts["compress"]:
                a += ["--compress", opts["compress"]]
        a += self._import_opt_args(opts, pass_env, with_pass=False)
        cmd = " ".join(shlex.quote(x) for x in a)
        if pass_env:
            cmd += f"   # 먼저 `read -rs {pass_env}; export {pass_env}` 로 패스프레이즈 환경변수를 다시 설정"
        return cmd

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        w = self.stdout.write
        source = validate_source(opts["source"])
        source_dir = opts["source_dir"].rstrip("/")
        if not source_dir.startswith("/"):
            raise CommandError("--source-dir 는 원격의 절대경로여야 합니다.")
        python = opts["source_python"] or f"{source_dir}/.venv/bin/python"
        include_config = opts["include_config"]
        pass_env = opts["passphrase_env"]
        if opts["restore_config"] and not include_config:
            raise CommandError("--restore-config 에는 --include-config 가 필요합니다.")
        if pass_env and not include_config:
            raise CommandError("--passphrase-env 는 --include-config 와 함께 써야 합니다.")
        if pass_env and not opts["dry_run"] and not os.environ.get(pass_env):
            raise CommandError(f"환경변수 {pass_env} 가 비어 있습니다 (패스프레이즈).")
        if opts["max_backup_seconds"] is not None and opts["max_backup_seconds"] <= 0:
            raise CommandError("--max-backup-seconds 는 양수여야 합니다.")

        remote_out = opts["remote_bundle"]
        if remote_out:
            remote_out = remote_out.rstrip("/")
            if not remote_out.startswith("/"):
                raise CommandError("--remote-bundle 은 원격 절대경로여야 합니다.")
        else:
            remote_out = f"{source_dir}/snapshots/qg-{sn.utc_ts()}"
        bundle_name = posixpath.basename(remote_out)
        local_root = Path(opts["local_dir"]) if opts["local_dir"] else Path(settings.BASE_DIR) / "snapshots"
        local_bundle = local_root / bundle_name

        try:
            user_ssh = shlex.split(opts["ssh_opts"])
        except ValueError as e:
            raise CommandError(f"--ssh-opts 를 해석할 수 없습니다: {e}")
        # BatchMode 먼저(비밀번호 프롬프트 금지), 사용자 옵션, 마지막에 기본 keepalive (ssh 는 먼저 나온 -o 값이 우선)
        ssh_base = ["ssh", "-o", "BatchMode=yes", *user_ssh, *SSH_KEEPALIVE_OPTS]
        ssh_cmd_str = " ".join(shlex.quote(x) for x in ssh_base)
        remote_cmd = build_remote_export_command(
            source_dir, python, remote_out, include_config, bool(pass_env),
            max_backup_seconds=opts["max_backup_seconds"], compress=opts["compress"],
        )
        export_cmd = [*ssh_base, "--", source, remote_cmd]
        rsync_cmd = ["rsync", "-a", "--partial", "--info=progress2", "-e", ssh_cmd_str,
                     "--", f"{source}:{remote_out}/", f"{local_bundle}/"]
        rm_cmd = [*ssh_base, "--", source, f"rm -rf -- {shlex.quote(remote_out)}"]
        rm_hint = " ".join(shlex.quote(x) for x in rm_cmd)

        import_args = self._import_opt_args(opts, pass_env)
        import_hint = " ".join(shlex.quote(x) for x in ["python", "manage.py", "import_snapshot", str(local_bundle), *import_args])
        verify_hint = " ".join(shlex.quote(x) for x in (
            ["python", "manage.py", "verify_snapshot", str(local_bundle), "--allow-newer"]
            + (["--target-db", opts["target_db"]] if opts["target_db"] else [])))
        rerun_fresh = self._sync_rerun_cmd(opts, source, source_dir, pass_env, None)
        rerun_resume = self._sync_rerun_cmd(opts, source, source_dir, pass_env, remote_out)

        if opts["dry_run"]:
            self._dry_run(opts, export_cmd, rsync_cmd, rm_cmd, local_bundle, import_hint, verify_hint, remote_out, pass_env)
            return

        done: list[str] = []
        state = {"remote_deleted": False}

        def fail(stage: str, msg: str, resume: str):
            w("")
            w(self.style.ERROR(f"실패: {stage} — {msg}"))
            w(f"  완료된 단계: {', '.join(done) or '(없음)'}")
            if state["remote_deleted"]:
                w(f"  원격 번들  : {remote_out} — 이미 삭제됨 (재개는 로컬 번들 {local_bundle} 로)")
            else:
                w(f"  원격 번들  : {remote_out}")
                w(f"  원격에 번들이 남아 있음: {rm_hint}")
            w(f"  재개 방법  : {resume}")
            raise CommandError(f"sync 중단 ({stage})")

        # ① 원격 export
        if opts["remote_bundle"]:
            w(f"[1/6] 원격 export 생략 (--remote-bundle {remote_out})")
            done.append("① (생략)")
        else:
            w(f"[1/6] 원격 export_snapshot 실행: {source}:{remote_out}")
            if include_config and not pass_env:
                w(self.style.WARNING("  경고: config 를 암호화 없이 가져옵니다 (원격 번들 0600 + ssh 구간 암호화). "
                                     "--passphrase-env 사용을 권장."))
            stdin = (os.environ[pass_env] + "\n") if (include_config and pass_env) else None
            rc = run_cmd(export_cmd, stdin)
            if rc != 0:
                fail("① 원격 export", f"ssh/export 종료코드 {rc} (ssh 접속·원격 경로·python 경로 확인; "
                     "IDC↔AWS 가 막혀 있으면 오프라인 경로: 번들을 직접 옮겨 import_snapshot)",
                     f"{rerun_fresh}  (같은 옵션으로 다시 실행 — 원격에서 중단된 export 는 스스로 정리됨)")
            done.append("① 원격 export")

        # ② rsync
        w(f"[2/6] rsync 수신 -> {local_bundle}")
        try:
            local_root.mkdir(parents=True, exist_ok=True)
            local_bundle.mkdir(parents=True, exist_ok=True)
            os.chmod(local_bundle, 0o700)
        except OSError as e:
            fail("② 로컬 수신 디렉터리 준비", f"{e.strerror or e}: {e.filename or local_bundle}", rerun_resume)
        rc = run_cmd(rsync_cmd)
        if rc != 0:
            fail("② rsync", f"종료코드 {rc}", rerun_resume + "  (rsync 가 이어받기 합니다)")
        done.append("② rsync")

        # ③ 검증 + 원격 삭제
        w("[3/6] manifest / sha256 검증")
        try:
            manifest = sn.load_manifest(local_bundle)
            sn.verify_bundle_files(local_bundle, manifest, log=w)
        except sn.SnapshotError as e:
            fail("③ 번들 검증", str(e), rerun_resume + "  (손상됐으면 로컬 번들 디렉터리를 지우고 재실행)")
        except OSError as e:
            fail("③ 번들 검증", f"{e.strerror or e}", rerun_resume)
        done.append("③ 검증")
        # 이후 단계 실패 시 재개는 로컬 번들로 하므로 원격 번들은 검증 직후 지운다.
        if opts["keep_remote"]:
            w(f"      원격 번들 유지 (--keep-remote): {source}:{remote_out}")
        elif not self._safe_remote_path(remote_out):
            w(self.style.WARNING(f"      원격 경로가 snapshots/qg-* 형태가 아니어서 삭제하지 않음: {remote_out}"))
        else:
            w("      검증 완료 — 원격 임시 번들 삭제 (이후 재개는 로컬 번들 사용)")
            rc = run_cmd(rm_cmd)
            if rc == 0:
                state["remote_deleted"] = True
            else:
                w(self.style.WARNING(f"      원격 삭제 실패(rc={rc}) — 수동 삭제: {rm_hint}"))

        # ④ import
        w("[4/6] import_snapshot")
        try:
            call_command("import_snapshot", str(local_bundle), stdout=self.stdout, stderr=self.stderr,
                         target_db=opts["target_db"], restore_config=opts["restore_config"],
                         passphrase_env=pass_env, force=opts["force"], yes=opts["yes"],
                         ignore_running=opts["ignore_running"], skip_migrate=opts["skip_migrate"],
                         allow_same_host=opts["allow_same_host"], allow_older_code=opts["allow_older_code"],
                         service_port=opts["service_port"])
        except CommandError as e:
            fail("④ import", str(e), f"원인 해결 후 `{import_hint}` (번들은 이미 로컬에 있음)"
                 + (f"  # 패스프레이즈: `read -rs {pass_env}; export {pass_env}`" if pass_env and opts["restore_config"] else ""))
        done.append("④ import")

        # ⑤ verify (migrate 로 마이그레이션 행이 늘 수 있으므로 --allow-newer)
        w("[5/6] verify_snapshot")
        try:
            vk = {"allow_newer": True}
            if opts["target_db"]:
                vk["target_db"] = opts["target_db"]
            call_command("verify_snapshot", str(local_bundle), stdout=self.stdout, stderr=self.stderr, **vk)
        except CommandError as e:
            fail("⑤ verify", str(e), f"`{verify_hint}`")
        done.append("⑤ verify")

        # ⑥ 마무리
        if opts["keep_remote"] or not state["remote_deleted"]:
            w(f"[6/6] 원격 번들 남아 있음: {source}:{remote_out}  (정리: {rm_hint})")
        else:
            w("[6/6] 원격 번들은 ③ 에서 삭제됨")
        w(self.style.SUCCESS("\n동기화 완료. 서비스 기동 전 .env / crontab / systemd 유닛 설치 여부를 점검하세요."))

    # ------------------------------------------------------------------
    @staticmethod
    def _safe_remote_path(p: str) -> bool:
        parent = posixpath.basename(posixpath.dirname(p))
        return parent == "snapshots" and posixpath.basename(p).startswith("qg-")

    def _dry_run(self, opts, export_cmd, rsync_cmd, rm_cmd, local_bundle, import_hint, verify_hint, remote_out, pass_env):
        w = self.stdout.write
        w(self.style.WARNING("[dry-run] 실행하지 않고 명령만 출력합니다 (값은 마스킹)."))
        if opts["remote_bundle"]:
            w("① (생략: --remote-bundle)")
        else:
            w("① 원격 export (ssh):")
            w("   " + " ".join(shlex.quote(x) for x in export_cmd))
            if opts["include_config"] and pass_env:
                w(f"   (stdin 으로 로컬 ${pass_env} 값을 한 줄 전달 — 값은 출력/로그 안 됨)")
        w("② rsync:")
        w("   " + " ".join(shlex.quote(x) for x in rsync_cmd))
        w(f"③ 검증: {local_bundle}/manifest.json + sha256")
        if opts["keep_remote"] or not self._safe_remote_path(remote_out):
            w("   원격 번들 유지")
        else:
            w("   ③ 직후 원격 번들 삭제: " + " ".join(shlex.quote(x) for x in rm_cmd))
        w(f"④ {import_hint}")
        w(f"⑤ {verify_hint}")
