from __future__ import annotations

import os
import posixpath
import stat
import sys
import time
from pathlib import Path

import paramiko

HOST = "10.1.102.36"
USER = "jinyx01"
PASSWORD = os.environ["TS_SSH_PASSWORD"]
LOCAL_ROOT = Path(__file__).resolve().parents[1]
REMOTE_APP = "/home/jinyx01/transfer-station"
REMOTE_RELEASES = "/home/jinyx01/transfer-station-releases"
REMOTE_BACKUPS = "/home/jinyx01/transfer-station-backups"
STAMP = time.strftime("%Y%m%d-%H%M%S")
RELEASE_NAME = f"{STAMP}-release-hold-0828"
STAGING = f"/home/jinyx01/transfer-station-staging-{STAMP}"
RELEASE = f"{REMOTE_RELEASES}/{RELEASE_NAME}"
BACKUP = f"{REMOTE_BACKUPS}/{STAMP}-before-release-hold-0828"
SKIP_STAGING_TESTS = os.environ.get("TS_SKIP_STAGING_TESTS", "").strip() in {"1", "true", "yes"}
MYSQL_ROOT_PASSWORD = os.environ.get("TS_MYSQL_ROOT_PASSWORD", "")
MYSQL_APP_USER = os.environ.get("TS_MYSQL_APP_USER", "lyxh")
MYSQL_APP_PASSWORD = os.environ.get("TS_MYSQL_APP_PASSWORD", MYSQL_ROOT_PASSWORD)
MYSQL_DATABASE = os.environ.get("TS_MYSQL_DATABASE", "longyuxuanhui")

SKIP_DIRS = {
    ".venv",
    "data",
    "__pycache__",
    ".pytest_cache",
    ".git",
    "_prod_live",
    "_prod_merge",
    "transfer_station.egg-info",
}
SKIP_FILES = {".env", ".env.bak.fix"}
UPLOAD_TOP = ("app", "tests", "scripts", "deploy", "doc", "pyproject.toml", ".env.example", "README.md")


def log(msg: str) -> None:
    print(msg, flush=True)


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("ssh transport is closed")
    chan = transport.open_session()
    chan.set_combine_stderr(True)
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    out = b""
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            chan.close()
            raise TimeoutError(f"cmd timed out ({timeout}s): {cmd[:180]}")
        if chan.recv_ready():
            chunk = chan.recv(32768)
            if not chunk:
                break
            out += chunk
            continue
        if chan.exit_status_ready():
            while chan.recv_ready():
                out += chan.recv(32768)
            break
        time.sleep(0.2)
    code = chan.recv_exit_status()
    text = out.decode("utf-8", "replace")
    return code, text, ""


def must(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    code, out, err = run(client, cmd, timeout=timeout)
    if code != 0:
        raise RuntimeError(f"cmd failed ({code}): {cmd}\n{out}\n{err}")
    return out


def sudo(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    full = f"sudo -S -p '' {cmd}"
    stdin, stdout, stderr = client.exec_command(full, timeout=timeout)
    stdin.write(PASSWORD + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    text = (out + err).replace(PASSWORD, "[redacted]")
    if code != 0:
        raise RuntimeError(f"sudo failed ({code}): {cmd}\n{text}")
    return text


def should_skip(path: Path) -> bool:
    if path.name in SKIP_FILES or path.name in SKIP_DIRS:
        return True
    if path.name.startswith("_remote_") or path.name.startswith("_fetch_") or path.name.startswith("deploy_remote"):
        return True
    if path.suffix in {".pyc", ".pyo"}:
        return True
    return False


def iter_upload_files() -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    for top in UPLOAD_TOP:
        src = LOCAL_ROOT / top
        if not src.exists():
            continue
        if src.is_file():
            items.append((src, top.replace("\\", "/")))
            continue
        for path in src.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(LOCAL_ROOT)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if should_skip(path):
                continue
            items.append((path, rel.as_posix()))
    return items


def sftp_mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = []
    current = ""
    for part in remote_dir.strip("/").split("/"):
        current += "/" + part
        parts.append(current)
    for path in parts:
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def upload_tree(sftp: paramiko.SFTPClient, remote_root: str) -> int:
    files = iter_upload_files()
    for local, rel in files:
        remote = posixpath.join(remote_root, rel)
        sftp_mkdirs(sftp, posixpath.dirname(remote))
        sftp.put(str(local), remote)
    return len(files)


def main() -> int:
    log(f"connecting {USER}@{HOST} stamp={STAMP}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        port=22,
        username=USER,
        password=PASSWORD,
        timeout=20,
        banner_timeout=30,
        auth_timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(15)
    sftp = client.open_sftp()

    log("== inspect ==")
    leftover = run(
        client,
        "find /home/jinyx01 -maxdepth 1 -type d -name 'transfer-station-staging-*' -print",
    )[1].strip()
    if leftover:
        log("removing leftover staging")
        must(client, "find /home/jinyx01 -maxdepth 1 -type d -name 'transfer-station-staging-*' -exec rm -rf {} +")
    health = must(client, "curl -fsS http://127.0.0.1:18787/health")
    log("health " + health.strip())
    active = must(client, "systemctl is-active transfer-station").strip()
    log("service " + active)

    log("== backup ==")
    must(client, f"mkdir -p {BACKUP}/credentials {BACKUP}/source")
    must(
        client,
        f"""/home/jinyx01/transfer-station/.venv/bin/python - <<'PY'
import os, pathlib, sqlite3, subprocess
backup = pathlib.Path("{BACKUP}")
src_path = pathlib.Path("/home/jinyx01/transfer-station/data/gateway.sqlite3")
if src_path.is_file():
    dst_path = backup / "gateway.sqlite3"
    src = sqlite3.connect(f"file:{{src_path}}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    src.backup(dst)
    dst.close()
    src.close()
    print("sqlite_backup_ok", dst_path.stat().st_size)
else:
    print("sqlite_backup_skipped")
env = {{}}
for line in pathlib.Path("/home/jinyx01/transfer-station/.env").read_text(encoding="utf-8").splitlines():
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    env[key.strip()] = value.strip().strip('"').strip("'")
if env.get("MYSQL_HOST") and env.get("MYSQL_DATABASE") and env.get("MYSQL_USER"):
    dump = backup / "mysql-longyuxuanhui.sql"
    command = [
        "mysqldump",
        "--protocol=TCP",
        "-h", env.get("MYSQL_HOST", "127.0.0.1"),
        "-P", env.get("MYSQL_PORT", "3306"),
        "-u", env["MYSQL_USER"],
        "--single-transaction",
        env["MYSQL_DATABASE"],
    ]
    copied = os.environ.copy()
    copied["MYSQL_PWD"] = env.get("MYSQL_PASSWORD", "")
    with dump.open("wb") as handle:
        subprocess.check_call(command, env=copied, stdout=handle)
    print("mysql_dump_ok", dump.stat().st_size)
else:
    print("mysql_dump_skipped")
PY""",
        timeout=180,
    )
    must(client, f"cp -a {REMOTE_APP}/.env {BACKUP}/.env")
    live_cwd = must(
        client,
        "systemctl show -p WorkingDirectory --value transfer-station",
    ).strip()
    live_tar = ""
    if live_cwd and live_cwd != REMOTE_APP:
        live_tar = (
            f"tar -C {live_cwd} --exclude=__pycache__ --exclude=.pytest_cache "
            f"-czf {BACKUP}/source/live-release.tgz . && "
        )
        log("live_cwd " + live_cwd)
    must(
        client,
        f"cp -a {REMOTE_APP}/data/gateway-credentials/. {BACKUP}/credentials/ && "
        f"tar -C {REMOTE_APP} --exclude=.venv --exclude=data --exclude=__pycache__ -czf {BACKUP}/source/transfer-station.tgz . && "
        f"{live_tar}"
        f"chmod -R go-rwx {BACKUP}",
        timeout=180,
    )
    log("backup " + BACKUP)

    log("== upload staging ==")
    must(client, f"mkdir -p {STAGING}")
    count = upload_tree(sftp, STAGING)
    log(f"uploaded {count} files to staging")

    log("== staging tests ==")
    if SKIP_STAGING_TESTS:
        log("staging tests skipped")
    else:
        test_out = must(
            client,
            f"""
set -e
STAGING={STAGING}
VENV={STAGING}/.venv
/home/jinyx01/.local/bin/uv venv --python /home/jinyx01/transfer-station/.venv/bin/python "$VENV"
/home/jinyx01/.local/bin/uv pip install --python "$VENV/bin/python" -e '{STAGING}[dev]'
cd "$STAGING"
PYTHONUNBUFFERED=1 DATA_DIR="$STAGING/tmp-data" "$VENV/bin/python" -m pytest -q --tb=line
""",
            timeout=900,
        )
        log("\n".join(test_out.strip().splitlines()[-8:]))
        log("staging tests passed")

    log("== promote release ==")
    must(client, f"mkdir -p {RELEASE}")
    must(
        client,
        f"""
set -e
cp -a {STAGING}/app {STAGING}/tests {STAGING}/pyproject.toml {STAGING}/README.md {RELEASE}/
[ -d {STAGING}/scripts ] && cp -a {STAGING}/scripts {RELEASE}/ || true
[ -d {STAGING}/deploy ] && cp -a {STAGING}/deploy {RELEASE}/ || true
ln -sfn {REMOTE_APP}/.env {RELEASE}/.env
ln -sfn {REMOTE_APP}/data {RELEASE}/data
# keep checkout in sync without touching env/data/venv
rm -rf {REMOTE_APP}/app {REMOTE_APP}/tests
cp -a {STAGING}/app {STAGING}/tests {STAGING}/pyproject.toml {STAGING}/README.md {REMOTE_APP}/
[ -f {STAGING}/.env.example ] && cp -a {STAGING}/.env.example {REMOTE_APP}/.env.example || true
""",
        timeout=120,
    )
    must(
        client,
        "/home/jinyx01/.local/bin/uv pip install "
        "--python /home/jinyx01/transfer-station/.venv/bin/python "
        "-e /home/jinyx01/transfer-station",
        timeout=180,
    )

    log("== mysql database ==")
    mysql_state = must(
        client,
        "grep -E '^MYSQL_HOST=' /home/jinyx01/transfer-station/.env >/dev/null "
        "&& echo mysql_configured || echo mysql_missing",
    ).strip()
    log(mysql_state)
    if mysql_state != "mysql_configured":
        if not MYSQL_ROOT_PASSWORD:
            raise RuntimeError("live .env has no MYSQL_HOST and TS_MYSQL_ROOT_PASSWORD is unset")
        must(
            client,
            f"""
set -e
export MYSQL_PWD='{MYSQL_ROOT_PASSWORD}'
mysql --protocol=TCP -h127.0.0.1 -P3306 -uroot -e "
CREATE DATABASE IF NOT EXISTS {MYSQL_DATABASE} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '{MYSQL_APP_USER}'@'localhost' IDENTIFIED BY '{MYSQL_APP_PASSWORD}';
ALTER USER '{MYSQL_APP_USER}'@'localhost' IDENTIFIED BY '{MYSQL_APP_PASSWORD}';
GRANT ALL PRIVILEGES ON {MYSQL_DATABASE}.* TO '{MYSQL_APP_USER}'@'localhost';
FLUSH PRIVILEGES;
"
unset MYSQL_PWD
""",
            timeout=60,
        )
        must(
            client,
            f"""
/home/jinyx01/transfer-station/.venv/bin/python - <<'PY'
from pathlib import Path
path = Path("/home/jinyx01/transfer-station/.env")
text = path.read_text(encoding="utf-8")
lines = [line for line in text.splitlines() if not line.startswith("MYSQL_")]
lines.extend([
    "MYSQL_HOST=127.0.0.1",
    "MYSQL_PORT=3306",
    "MYSQL_USER={MYSQL_APP_USER}",
    "MYSQL_PASSWORD={MYSQL_APP_PASSWORD}",
    "MYSQL_DATABASE={MYSQL_DATABASE}",
])
path.write_text("\\n".join(lines).rstrip() + "\\n", encoding="utf-8")
print("env_mysql_updated")
PY
""",
            timeout=30,
        )
        must(
            client,
            f"""
set -e
cd {RELEASE}
/home/jinyx01/transfer-station/.venv/bin/python - <<'PY'
import asyncio
from app.store.gateway import gateway_store
async def main():
    await gateway_store.start()
    await gateway_store.stop()
asyncio.run(main())
print("mysql_schema_ok")
PY
/home/jinyx01/transfer-station/.venv/bin/python {RELEASE}/scripts/migrate_sqlite_to_mysql.py \\
  --sqlite /home/jinyx01/transfer-station/data/gateway.sqlite3 \\
  --host 127.0.0.1 --port 3306 \\
  --user {MYSQL_APP_USER} --password '{MYSQL_APP_PASSWORD}' \\
  --database {MYSQL_DATABASE}
""",
            timeout=180,
        )
        log("mysql migrated")
    else:
        log("mysql already configured; skip rewrite and sqlite upsert")

    log("== upload desktop exe ==")
    exe_new = LOCAL_ROOT / "clients" / "minking-desktop" / "dist-new" / "MinKingAI.exe"
    exe_dist = LOCAL_ROOT / "clients" / "minking-desktop" / "dist" / "MinKingAI.exe"
    exe_candidates = [path for path in (exe_dist, exe_new) if path.is_file()]
    exe_local = max(exe_candidates, key=lambda path: path.stat().st_mtime) if exe_candidates else exe_dist
    sha_local = LOCAL_ROOT / "data" / "downloads" / "MinKingAI.exe.sha256"
    must(client, f"mkdir -p {REMOTE_APP}/data/downloads")
    if exe_local.is_file():
        sftp.put(str(exe_local), f"{REMOTE_APP}/data/downloads/MinKingAI.exe")
        if sha_local.is_file():
            sftp.put(str(sha_local), f"{REMOTE_APP}/data/downloads/MinKingAI.exe.sha256")
        log("desktop exe uploaded")
    else:
        log("desktop exe missing locally; skip")

    log("== switch systemd working directory ==")
    dropin = "/etc/systemd/system/transfer-station.service.d/99-working-directory.conf"
    sudo(
        client,
        f"bash -lc \"printf '%s\\n' '[Service]' 'WorkingDirectory={RELEASE}' > {dropin} && chmod 644 {dropin}\"",
    )
    sudo(client, "systemctl daemon-reload")
    sudo(client, "systemctl restart transfer-station")
    time.sleep(12)
    status = must(client, "systemctl is-active transfer-station").strip()
    if status != "active":
        journal = sudo(client, "journalctl -u transfer-station -n 80 --no-pager")
        raise RuntimeError("service not active after restart\n" + journal)
    log("service active")

    log("== verify ==")
    health2 = must(client, "curl -fsS -D - http://127.0.0.1:18787/health -o /tmp/ts-health.json")
    log(health2.strip())
    log(must(client, "cat /tmp/ts-health.json").strip())
    providers = must(client, "curl -fsS http://127.0.0.1:18787/v1/providers")
    log("providers " + providers.strip())
    admin = must(client, "curl -fsS -o /tmp/ts-admin.html -w '%{http_code}' http://127.0.0.1:18787/admin")
    log("admin_http " + admin.strip())
    page_bits = must(
        client,
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "t=Path('/tmp/ts-admin.html').read_text(encoding='utf-8',errors='replace')\n"
        "print('brand', int('MinKing AI' in t))\n"
        "print('accounts', int('上游账号' in t))\n"
        "print('bindings', int('会话绑定' in t))\n"
        "print('version', int('服务版本 v0.8.28' in t))\n"
        "print('cachebust', int('app.js?v=0.8.28' in t))\n"
        "print('skills_nav', int('data-page=\"skills\"' in t))\n"
        "print('dashboard', int('仪表盘' in t))\n"
        "print('calls', int('调用明细' in t))\n"
        "print('billing_nav', int('data-page=\"billing\"' in t))\n"
        "print('cards_nav', int('data-page=\"cards\"' in t))\n"
        "print('ledger_nav', int('data-page=\"ledger\"' in t))\n"
        "print('themes', int('data-theme' in t))\n"
        "print('switch_field', int('switch-field' in t))\n"
        "print('oauth_login', int('网页登录' in t and 'oauth-start-form' in t))\n"
        "print('file_import', int('文件导入/导出' in t))\n"
        "print('legacy_primary', int('key-primary-account' in t))\n"
        "PY",
    )
    log(page_bits.strip())
    page_map = dict(line.split(None, 1) for line in page_bits.splitlines() if line.strip())
    if (
        page_map.get("brand") != "1"
        or page_map.get("cachebust") != "1"
        or page_map.get("bindings") != "0"
        or page_map.get("billing_nav") != "1"
        or page_map.get("cards_nav") != "1"
        or page_map.get("ledger_nav") != "1"
        or page_map.get("skills_nav") != "1"
    ):
        raise RuntimeError("admin page checks failed\n" + page_bits)
    js_bits = must(
        client,
        "python3 - <<'PY'\n"
        f"from pathlib import Path\n"
        f"js=Path('{RELEASE}/app/web/static/app.js').read_text(encoding='utf-8',errors='replace')\n"
        "print('call_input_token', int('输入 Token' in js))\n"
        "print('call_total_token_col', int('总 Token' in js and 'callTokenCell' not in js))\n"
        "print('quota_more', int('data-quota-more' in js))\n"
        "print('export_account', int('data-export-account' in js))\n"
        "print('oauth_start', int('oauth-start-form' in js or '/admin/api/accounts/oauth/start' in js))\n"
        "print('gpt_oss_hidden', int(\"window_label === \\\"Claude\\\"\" in js))\n"
        "print('copy_fallback', int('execCommand' in js and 'clipboard unavailable' in js))\n"
        "print('key_email_col', int('人员名称' in js and '邮箱' in js and '✦ 管理路由' not in js))\n"
        "print('grok_user_code_link', int('带代码' in js))\n"
        "print('workbuddy_credits', int('window_label' in js and '积分' in js))\n"
        "print('route_button_only', int('providers.slice(0, 2)' not in js and 'data-route-key' in js))\n"
        "print('restore_valid', int('data-restore-account' in js and 'data-restore-model' in js and '/admin/api/models/restore' in js))\n"
        f"print('restore_deleted', int(\"_restore_deleted_oauth_accounts\" in Path('{RELEASE}/app/codex_gateway.py').read_text(encoding='utf-8') and \"status IN ('invalid','deleted')\" in Path('{RELEASE}/app/codex_gateway.py').read_text(encoding='utf-8')))\n"
        f"print('foreign_cipher', int('def _drop_foreign_encrypted_content' in Path('{RELEASE}/app/providers/codex_protocol.py').read_text(encoding='utf-8')))\n"
        f"print('openai_env_key', int('env_key = \\\"OPENAI_API_KEY\\\"' in Path('{RELEASE}/app/bundle.py').read_text(encoding='utf-8')))\n"
        f"print('grok_tool_choice', int('def _map_grok_tool_choice' in Path('{RELEASE}/app/providers/grok.py').read_text(encoding='utf-8') and 'return \\\"required\\\"' in Path('{RELEASE}/app/providers/grok.py').read_text(encoding='utf-8')))\n"
        f"print('catalog_placeholder', int('WINDOWS_CODEX_CATALOG_PLACEHOLDER' in Path('{RELEASE}/app/providers/codex_catalog.py').read_text(encoding='utf-8') and '%userprofile%' not in Path('{RELEASE}/app/bundle.py').read_text(encoding='utf-8').lower()))\n"
        f"print('created_at_stamp', int('def stamp_sse_created_at' in Path('{RELEASE}/app/providers/codex_protocol.py').read_text(encoding='utf-8') and 'stamp_sse_created_at(piece' in Path('{RELEASE}/app/codex_gateway.py').read_text(encoding='utf-8')))\n"
        f"print('anthropic_system_role', int('role == \\\"system\\\"' in Path('{RELEASE}/app/providers/anthropic_protocol.py').read_text(encoding='utf-8')))\n"
        f"print('billing_module', int(Path('{RELEASE}/app/billing.py').is_file() and Path('{RELEASE}/app/billing_prices.json').is_file()))\n"
        f"print('site_template', int(Path('{RELEASE}/app/web/templates/site.html').is_file()))\n"
        f"print('client_skills', int((Path('{RELEASE}/app/client_skills/minking-media') / 'SKILL.md').is_file()))\n"
        f"print('skills_no_base_url', int('OPENAI_BASE_URL' not in (Path('{RELEASE}/app/client_skills/minking-media') / 'SKILL.md').read_text(encoding='utf-8')))\n"
        f"print('skills_route', int('/desktop/skills' in Path('{RELEASE}/app/portal.py').read_text(encoding='utf-8')))\n"
        "print('skills_upload', int('/admin/api/client-skills' in js and 'refreshSkills' in js))\n"
        f"print('variant_fold', int('def public_slug_for_variant' in Path('{RELEASE}/app/providers/antigravity.py').read_text(encoding='utf-8')))\n"
        f"print('location_message', int('location is not supported' in Path('{RELEASE}/app/codex_gateway.py').read_text(encoding='utf-8')))\n"
        f"print('workbuddy_keep', int('foreign' in Path('{RELEASE}/app/harness_switch.py').read_text(encoding='utf-8')))\n"
        f"print('desktop_package', int('/admin/api/desktop-package' in Path('{RELEASE}/app/api/codex_gateway.py').read_text(encoding='utf-8') and 'desktop-package-form' in Path('{RELEASE}/app/web/templates/index.html').read_text(encoding='utf-8')))\n"
        f"print('lease_idle', int('gateway_stream_idle_timeout_seconds' in Path('{RELEASE}/app/config.py').read_text(encoding='utf-8')))\n"
        f"print('lease_reap', int('owner_task_done' in Path('{RELEASE}/app/scheduler.py').read_text(encoding='utf-8')))\n"
        "print('billing_js', int('data-page=\"billing\"' in js or '/admin/api/billing/settings' in js))\n"
        "PY",
    )
    log(js_bits.strip())
    js_map = dict(line.split(None, 1) for line in js_bits.splitlines() if line.strip())
    if (
        js_map.get("copy_fallback") != "1"
        or js_map.get("key_email_col") != "1"
        or js_map.get("route_button_only") != "1"
        or js_map.get("openai_env_key") != "1"
        or js_map.get("grok_tool_choice") != "1"
        or js_map.get("catalog_placeholder") != "1"
        or js_map.get("created_at_stamp") != "1"
        or js_map.get("anthropic_system_role") != "1"
        or js_map.get("billing_module") != "1"
        or js_map.get("site_template") != "1"
        or js_map.get("billing_js") != "1"
        or js_map.get("client_skills") != "1"
        or js_map.get("skills_no_base_url") != "1"
        or js_map.get("skills_route") != "1"
        or js_map.get("skills_upload") != "1"
        or js_map.get("variant_fold") != "1"
        or js_map.get("location_message") != "1"
        or js_map.get("workbuddy_keep") != "1"
        or js_map.get("desktop_package") != "1"
        or js_map.get("lease_idle") != "1"
        or js_map.get("lease_reap") != "1"
        or js_map.get("restore_valid") != "1"
        or js_map.get("restore_deleted") != "1"
        or js_map.get("foreign_cipher") != "1"
    ):
        raise RuntimeError("admin js checks failed\n" + js_bits)
    unauth = must(
        client,
        "curl -sS -o /tmp/ts-unauth.json -w '%{http_code}' http://127.0.0.1:18787/admin/api/accounts",
    )
    log("unauth_accounts_http " + unauth.strip() + " " + must(client, "cat /tmp/ts-unauth.json").strip())
    token = must(
        client,
        "curl -sS -o /tmp/ts-token.json -w '%{http_code}' "
        "-H 'X-Admin-Token: leftover' http://127.0.0.1:18787/admin/api/accounts",
    )
    log("legacy_token_http " + token.strip() + " " + must(client, "cat /tmp/ts-token.json").strip())
    reqid = must(
        client,
        "curl -fsS -D - -o /dev/null http://127.0.0.1:18787/health | grep -i x-request-id",
    )
    log(reqid.strip())
    version = must(
        client,
        "/home/jinyx01/transfer-station/.venv/bin/python -c \"import sys; sys.path.insert(0,'%s'); import app; print(app.__version__)\""
        % RELEASE,
    )
    log("running_version " + version.strip())
    if version.strip() != "0.8.28":
        raise RuntimeError(f"unexpected live version {version.strip()!r}")
    portal = must(client, "curl -fsS -o /tmp/ts-portal.html -w '%{http_code}' http://127.0.0.1:18787/portal")
    log("portal_http " + portal.strip())
    portal_bits = must(
        client,
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "t=Path('/tmp/ts-portal.html').read_text(encoding='utf-8',errors='replace')\n"
        "print('portal_brand', int('MinKing AI' in t))\n"
        "print('portal_css', int('portal.css?v=0.8.28' in t and '服务版本 v0.8.28' in t))\n"
        "print('portal_login_tab', int('id=\"tab-login\"' in t))\n"
        "print('portal_register_tab', int('id=\"tab-register\"' in t))\n"
        "print('portal_desktop_btn', int('open-desktop' in t))\n"
        "print('portal_calls', int('调用明细' in t))\n"
        "print('portal_ledger', int('资金流水' in t))\n"
        "print('portal_redeem', int('卡密兑换' in t))\n"
        "print('old_transfer', int('TRANSFER STATION' in t))\n"
        "PY",
    )
    log(portal_bits.strip())
    portal_map = dict(line.split(None, 1) for line in portal_bits.splitlines() if line.strip())
    if (
        portal_map.get("portal_brand") != "1"
        or portal_map.get("portal_css") != "1"
        or portal_map.get("portal_login_tab") != "1"
        or portal_map.get("portal_register_tab") != "1"
        or portal_map.get("portal_calls") != "1"
        or portal_map.get("portal_ledger") != "1"
        or portal_map.get("portal_redeem") != "1"
        or portal_map.get("old_transfer") != "0"
    ):
        raise RuntimeError("portal page checks failed\n" + portal_bits)
    captcha = must(client, "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18787/v1/portal/api/captcha")
    log("portal_captcha_http " + captcha.strip())
    if captcha.strip() != "200":
        raise RuntimeError("portal captcha failed: " + captcha)
    skills_http = must(
        client,
        "curl -sS -o /tmp/ts-skills.json -w '%{http_code}' http://127.0.0.1:18787/v1/portal/api/desktop/skills",
    )
    log("desktop_skills_http " + skills_http.strip() + " " + must(client, "cat /tmp/ts-skills.json").strip())
    if skills_http.strip() != "401":
        raise RuntimeError("desktop skills should require login: " + skills_http)
    desktop = must(
        client,
        "curl -sS -o /tmp/ts-desktop.json -w '%{http_code}' -X POST "
        "http://127.0.0.1:18787/v1/portal/api/desktop/auth/send-code "
        "-H 'content-type: application/json' -d '{}'",
    )
    log("desktop_send_code_http " + desktop.strip())
    if desktop.strip() == "404":
        raise RuntimeError("desktop send-code still 404")
    home = must(client, "curl -sS -o /tmp/ts-home.html -w '%{http_code}' http://127.0.0.1:18787/")
    log("home_http " + home.strip())
    if home.strip() != "200":
        raise RuntimeError("home page is not 200: " + home)
    site_bits = must(
        client,
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "t=Path('/tmp/ts-home.html').read_text(encoding='utf-8',errors='replace')\n"
        "print('site_brand', int('MinKing' in t))\n"
        "print('site_css', int('site.css?v=0.8.28' in t and '服务版本 v0.8.28' in t))\n"
        "print('site_exe', int('MinKingAI.exe' in t))\n"
        "print('site_no_install', int('无需安装' in t))\n"
        "print('site_admin_redirect', int('location' in t.lower() and '/admin' in t and 'MinKing' not in t))\n"
        "print('site_old_name', int('Transfer Station' in t))\n"
        "PY",
    )
    log(site_bits.strip())
    site_map = dict(line.split(None, 1) for line in site_bits.splitlines() if line.strip())
    if (
        site_map.get("site_brand") != "1"
        or site_map.get("site_css") != "1"
        or site_map.get("site_exe") != "1"
        or site_map.get("site_no_install") != "1"
        or site_map.get("site_old_name") != "0"
    ):
        raise RuntimeError("official site checks failed\n" + site_bits)
    exe_http = must(
        client,
        "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18787/download/MinKingAI.exe",
    )
    log("download_exe_http " + exe_http.strip())
    if exe_http.strip() != "200":
        raise RuntimeError("desktop exe download failed: " + exe_http)
    nginx = must(client, "curl -fsS http://127.0.0.1:8787/health")
    log("nginx_health " + nginx.strip())
    cwd = must(client, "systemctl show -p WorkingDirectory --value transfer-station")
    log("cwd " + cwd.strip())
    if cwd.strip() != RELEASE:
        raise RuntimeError(f"live cwd {cwd.strip()!r} is not {RELEASE}")

    log("== cleanup staging ==")
    must(client, f"rm -rf {STAGING}")
    sftp.close()
    client.close()
    log(f"DONE release={RELEASE} backup={BACKUP}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("DEPLOY_FAILED", type(exc).__name__, str(exc)[:2000], file=sys.stderr)
        raise
