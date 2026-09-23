"""Run against a private socket-only MySQL instance, never the production server."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time
import pwd
import shutil
from decimal import Decimal

import pymysql
from pymysql.cursors import DictCursor


async def check(socket):
    from app.config import settings
    from app.store.gateway import gateway_store, iso_now
    from app.console_schema import migrate_console
    from app.wallet_engine import reserve,settle,wallet_summary
    from app.billing import update_settings
    def connect():
        return pymysql.connect(unix_socket=str(socket),user='root',database='console_check',cursorclass=DictCursor,autocommit=False)
    settings.mysql_host='isolated-socket'
    settings.mysql_database='console_check'
    settings.data_dir=socket.parent/'test-app-data'
    gateway_store._mysql_connection=connect
    before=connect()
    with before.cursor() as cur:
        cur.execute('SELECT id,usd_credit FROM portal_users'); balances={r['id']:Decimal(str(r['usd_credit'])) for r in cur.fetchall()}
        cur.execute('SELECT enforced FROM billing_settings WHERE id=1'); enforced=cur.fetchone()['enforced']
    before.close()
    await gateway_store.start()
    assert gateway_store.engine=='mysql'
    await migrate_console()
    await migrate_console()
    await gateway_store.execute("UPDATE billing_requests SET state='pending' WHERE state='reserved' AND request_id IN (SELECT request_id FROM call_records WHERE status<>'in_progress')")
    await gateway_store.all("SELECT v.video_id FROM video_jobs v JOIN billing_requests b ON b.request_id=v.billing_request_id LIMIT 1")
    after=await gateway_store.all('SELECT id,usd_credit FROM portal_users')
    assert balances=={r['id']:Decimal(str(r['usd_credit'])) for r in after}
    assert (await gateway_store.one('SELECT enforced FROM billing_settings WHERE id=1'))['enforced']==enforced
    await gateway_store.execute("INSERT INTO portal_users(id,email,name,usd_credit,created_at) VALUES('migration-test','migration@example.test','Migration','0.60',?)",(iso_now(),))
    await gateway_store.execute("INSERT INTO model_official_prices(model,provider,modality,official_input_usd_per_1m,official_output_usd_per_1m,status) VALUES('migration-test','codex','text','1','2','active')")
    await update_settings(enforced=True,price_multiplier='1')
    results=await asyncio.gather(*(reserve('migration-test',f'check-{i}','migration-test','/v1/responses') for i in range(5)),return_exceptions=True)
    assert sum(isinstance(r,Exception) for r in results)==3
    await settle('check-0',{'input_tokens':1,'output_tokens':1},'success')
    await settle('check-0',{'input_tokens':1,'output_tokens':1},'success')
    assert Decimal((await wallet_summary('migration-test'))['usd_credit'])==Decimal('0.599997')
    await gateway_store.stop()
    print('MYSQL_CHECK_OK: migration twice; original balances/settings unchanged; concurrent holds; microcharge; duplicate settlement',flush=True)


def main():
    root=Path(sys.argv[1]).resolve(); dump=Path(sys.argv[2]).resolve()
    assert root.name.startswith('transfer-station-staging-') and root.parent==Path('/home/jinyx01')
    private=Path('/var/lib/mysql')/('console-check-'+root.name)
    socketdir=private
    private.mkdir(mode=0o700,exist_ok=True);socketdir.mkdir(mode=0o700,exist_ok=True)
    mysql=pwd.getpwnam('mysql')
    os.chown(private,mysql.pw_uid,mysql.pw_gid);os.chown(socketdir,mysql.pw_uid,mysql.pw_gid)
    data=private/'data'; socket=socketdir/'mysql.sock'
    data.mkdir(mode=0o700,exist_ok=True)
    assert not any(data.iterdir()), 'Refusing to initialize a nonempty data directory'
    os.chown(data,mysql.pw_uid,mysql.pw_gid)
    log=(private/'server.log').open('wb')
    subprocess.run(['/usr/sbin/mysqld','--no-defaults','--user=mysql','--initialize-insecure',f'--datadir={data}'],stdout=log,stderr=log,check=True)
    server=subprocess.Popen(['/usr/sbin/mysqld','--no-defaults','--user=mysql',f'--datadir={data}',f'--socket={socket}',f'--pid-file={socketdir}/mysql.pid','--skip-networking','--mysqlx=0'],stdout=log,stderr=log)
    try:
        for _ in range(50):
            try:
                conn=pymysql.connect(unix_socket=str(socket),user='root')
                break
            except pymysql.Error:
                if server.poll() is not None: raise RuntimeError('Isolated MySQL exited; inspect protected server log')
                time.sleep(.5)
        else: raise RuntimeError('Isolated MySQL did not become ready')
        with conn.cursor() as cur: cur.execute('CREATE DATABASE console_check CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        conn.close()
        with dump.open('rb') as src:
            subprocess.run(['mysql','--no-defaults',f'--socket={socket}','-uroot','console_check'],stdin=src,stdout=log,stderr=log,check=True)
        asyncio.run(check(socket))
    finally:
        server.terminate();server.wait(timeout=30);log.close()
        shutil.copy2(private/'server.log',root/'mysql-check.log')
        assert private.parent==Path('/var/lib/mysql') and private.name.startswith('console-check-transfer-station-staging-')
        assert socketdir==private
        shutil.rmtree(private)


if __name__=='__main__': main()
