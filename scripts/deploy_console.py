"""Controlled console release; SSH password supplied in the process environment."""
import json
import os
import sys
from pathlib import Path
import deploy_remote as d


def connect():
    client = d.paramiko.SSHClient()
    known = Path.home() / '.ssh' / 'known_hosts'
    if known.exists():
        for line in known.read_text(errors='replace').splitlines():
            try:
                entry = d.paramiko.hostkeys.HostKeyEntry.from_line(line)
                if entry:
                    for hostname in entry.hostnames:
                        client.get_host_keys().add(hostname, entry.key.get_name(), entry.key)
            except (ValueError, d.paramiko.hostkeys.InvalidHostKey):
                continue
    client.set_missing_host_key_policy(d.paramiko.AutoAddPolicy())
    client.connect(d.HOST, username=d.USER, password=d.PASSWORD, timeout=20, allow_agent=False, look_for_keys=False)
    client.get_transport().set_keepalive(15)
    return client


def prepare(client):
    stamp=d.STAMP
    staging=f'/home/jinyx01/transfer-station-staging-{stamp}-console'
    release=f'{d.REMOTE_RELEASES}/{stamp}-console-0814'
    backup=f'{d.REMOTE_BACKUPS}/{stamp}-before-console-0814'
    live=d.must(client,'readlink -f /proc/$(systemctl show -p MainPID --value transfer-station)/cwd').strip()
    state=dict(staging=staging,release=release,backup=backup,live=live)
    resume='--resume' in sys.argv
    if resume:
        state=json.loads(Path('output/console-deploy-state.json').read_text())
        staging,release,backup,live=(state[k] for k in ('staging','release','backup','live'))
    Path('output/console-deploy-state.json').write_text(json.dumps(state),encoding='utf8')
    d.log('Creating protected database/source/config backup')
    code=f'''import os, pathlib, sqlite3, subprocess, shutil
from dotenv import dotenv_values
base=pathlib.Path({d.REMOTE_APP!r}); dest=pathlib.Path({backup!r})
dest.mkdir(parents=True,mode=0o700)
env=dotenv_values(base/'.env')
shutil.copy2(base/'.env',dest/'.env')
shutil.copytree(base/'data/gateway-credentials',dest/'credentials')
for dbfile in (base/'data').glob('*.sqlite3'):
    src=sqlite3.connect(f'file:{{dbfile}}?mode=ro',uri=True); dst=sqlite3.connect(dest/dbfile.name)
    src.backup(dst);dst.close();src.close()
command=['mysqldump','--protocol=TCP','-h',env.get('MYSQL_HOST','127.0.0.1'),'-P',env.get('MYSQL_PORT','3306'),'-u',env['MYSQL_USER'],'--single-transaction','--no-tablespaces',env['MYSQL_DATABASE']]
copy=os.environ.copy();copy['MYSQL_PWD']=env['MYSQL_PASSWORD']
with (dest/'mysql.sql').open('wb') as output:
    subprocess.run(command,env=copy,stdout=output,check=True)
subprocess.run(['tar','-C',{live!r},'--exclude=.env','--exclude=data','--exclude=.venv','--exclude=__pycache__','-czf',str(dest/'live-source.tgz'),'.'],check=True)
subprocess.run(['tar','-C',str(base),'--exclude=.env','--exclude=data','--exclude=.venv','--exclude=__pycache__','-czf',str(dest/'checkout-source.tgz'),'.'],check=True)
subprocess.run(['chmod','-R','go-rwx',str(dest)],check=True)
print('backup_ok', (dest/'mysql.sql').stat().st_size)
'''
    if not resume:
        print(d.must(client,f"{d.REMOTE_APP}/.venv/bin/python - <<'PY'\n{code}\nPY",timeout=240))
    d.must(client,f"mkdir -p {staging}")
    d.sudo(client,f"cp /etc/systemd/system/transfer-station.service.d/99-working-directory.conf {backup}/working-directory.conf")
    d.UPLOAD_TOP=(*d.UPLOAD_TOP,'docs')
    count=d.upload_tree(client.open_sftp(),staging)
    d.log(f'Uploaded {count} source/test files')
    command=f'''set -e
/home/jinyx01/.local/bin/uv venv --python {d.REMOTE_APP}/.venv/bin/python {staging}/.venv
/home/jinyx01/.local/bin/uv pip install --python {staging}/.venv/bin/python -e '{staging}[dev]'
cd {staging}
MYSQL_HOST='' DATA_DIR={staging}/tmp-data {staging}/.venv/bin/python -m pytest -q --show-capture=no --tb=line > {staging}/test-results.log 2>&1
tail -n 3 {staging}/test-results.log
'''
    d.log('Running full staging test suite')
    print(d.must(client,command,timeout=900))
    d.log('Staging tests passed; production remains running')


def promote(client):
    state=json.loads(Path('output/console-deploy-state.json').read_text())
    staging,release,backup,live=(state[k] for k in ('staging','release','backup','live'))
    result=d.must(client,f'tail -n 3 {staging}/test-results.log')
    if '331 passed' not in result: raise RuntimeError('Staging suite is not complete')
    d.log(result.strip())
    d.must(client,f'mkdir -p {release}; cp -a {staging}/app {staging}/tests {staging}/pyproject.toml {staging}/README.md {release}/; ln -sfn {d.REMOTE_APP}/.env {release}/.env; ln -sfn {d.REMOTE_APP}/data {release}/data')
    d.log('Stopping service for final consistent backup and migration')
    d.sudo(client,'systemctl stop transfer-station')
    code=f'''import pathlib, subprocess, os, json
from dotenv import dotenv_values
import pymysql
from pymysql.cursors import DictCursor
base=pathlib.Path({d.REMOTE_APP!r}); dest=pathlib.Path({backup!r}); env=dotenv_values(base/'.env')
copy=os.environ.copy();copy['MYSQL_PWD']=env['MYSQL_PASSWORD']
with (dest/'mysql-at-switch.sql').open('wb') as output:
 subprocess.run(['mysqldump','--protocol=TCP','-h',env['MYSQL_HOST'],'-P',env.get('MYSQL_PORT','3306'),'-u',env['MYSQL_USER'],'--single-transaction','--no-tablespaces',env['MYSQL_DATABASE']],env=copy,stdout=output,check=True)
db=pymysql.connect(host=env['MYSQL_HOST'],port=int(env.get('MYSQL_PORT','3306')),user=env['MYSQL_USER'],password=env['MYSQL_PASSWORD'],database=env['MYSQL_DATABASE'],cursorclass=DictCursor)
with db.cursor() as cur:
 cur.execute('SELECT enforced FROM billing_settings WHERE id=1'); enforced=cur.fetchone()['enforced']
 cur.execute('SELECT id,usd_credit FROM portal_users'); balances={{r['id']:str(r['usd_credit']) for r in cur.fetchall()}}
(dest/'money-before.json').write_text(json.dumps(dict(enforced=enforced,balances=balances)))
os.chmod(dest/'mysql-at-switch.sql',0o600);os.chmod(dest/'money-before.json',0o600)
print('Final consistent MySQL backup ready; billing_enforced=',enforced)
'''
    try:
        print(d.must(client,f"{d.REMOTE_APP}/.venv/bin/python - <<'PY'\n{code}\nPY",timeout=180))
        d.must(client,f'cp -a {staging}/app {staging}/tests {staging}/pyproject.toml {staging}/README.md {d.REMOTE_APP}/')
        d.must(client,f'/home/jinyx01/.local/bin/uv pip install --python {d.REMOTE_APP}/.venv/bin/python -e {d.REMOTE_APP}',timeout=180)
        sftp=client.open_sftp()
        with sftp.file(f'{staging}/working-directory.conf','w') as out:
            out.write(f'[Service]\nWorkingDirectory={release}\n')
        d.sudo(client,f'cp {staging}/working-directory.conf /etc/systemd/system/transfer-station.service.d/99-working-directory.conf')
        d.sudo(client,'systemctl daemon-reload')
        d.sudo(client,'systemctl start transfer-station')
        for _ in range(60):
            status,out,_=d.run(client,'curl -fsS http://127.0.0.1:18787/health')
            if status==0:
                d.log('HEALTH_OK '+out.strip());break
            d.time.sleep(1)
        else: raise RuntimeError('New service did not become healthy')
    except Exception:
        d.log('Release failed; restoring previous service working directory')
        d.sudo(client,'systemctl stop transfer-station')
        d.sudo(client,f'cp {backup}/working-directory.conf /etc/systemd/system/transfer-station.service.d/99-working-directory.conf')
        d.sudo(client,'systemctl daemon-reload')
        d.sudo(client,'systemctl start transfer-station')
        raise
    print(d.must(client,'systemctl is-active transfer-station; systemctl show transfer-station -p WorkingDirectory'))
    d.log('Release activated; backup '+backup)


if __name__ == '__main__':
    client = connect()
    if len(sys.argv)>1 and sys.argv[1]=='prepare':
        prepare(client)
        client.close()
        sys.exit(0)
    if len(sys.argv)>1 and sys.argv[1]=='promote':
        promote(client)
        client.close()
        sys.exit(0)
    print(d.must(client, "systemctl is-active transfer-station; systemctl show transfer-station -p WorkingDirectory -p MainPID; readlink -f /proc/$(systemctl show -p MainPID --value transfer-station)/cwd; ls -ld /home/jinyx01/transfer-station /home/jinyx01/transfer-station/data; curl -fsS http://127.0.0.1:8787/health; curl -fsS http://127.0.0.1:18787/health"))
    print(d.must(client, "cd /home/jinyx01/transfer-station && .venv/bin/python - <<'PY'\nfrom app.config import settings\nprint('database', 'mysql' if settings.uses_mysql else 'sqlite')\nprint('port',settings.port)\nPY"))
    client.close()
