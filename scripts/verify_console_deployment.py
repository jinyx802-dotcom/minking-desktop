"""Post-release smoke checks. Temporary verification session is revoked on exit."""
import asyncio
import json
import sys
from pathlib import Path
from decimal import Decimal
import httpx
from app.admin_auth import admin_auth, SESSION_COOKIE
from app.store.gateway import gateway_store


async def main():
    base='http://127.0.0.1:18787'
    before=json.loads((Path(sys.argv[1])/'money-before.json').read_text())
    await gateway_store.start()
    session=None
    try:
        actual=await gateway_store.all('SELECT id,usd_credit FROM portal_users')
        assert {r['id']:Decimal(str(r['usd_credit'])) for r in actual}=={k:Decimal(v) for k,v in before['balances'].items()}
        config=await gateway_store.one('SELECT enforced FROM billing_settings WHERE id=1')
        assert config['enforced']==before['enforced']
        for table,column in [('portal_users','usd_credit'),('wallet_ledger','amount_usd'),('call_records','usd_charged')]:
            info=await gateway_store.one(f"SHOW COLUMNS FROM {table} LIKE '{column}'")
            assert info['Type'].lower()=='decimal(24,8)'
        print('MONEY_AND_BILLING_SETTINGS_PRESERVED',flush=True)
        async with httpx.AsyncClient(base_url=base,timeout=30) as http:
            for path in ['/health','/admin','/portal','/v1/portal','/static/console-admin.js','/v1/static/console-portal.js']:
                response=await http.get(path)
                assert response.status_code==200,(path,response.status_code)
                assert response.headers.get('X-Request-ID')
            assert 'console-admin.js' in (await http.get('/admin')).text
            assert (await http.get('/admin/api/dashboard',headers={'X-Admin-Token':'legacy-rejected'})).status_code==401
            assert (await http.get('/admin/api/auth/session')).status_code==401
            assert (await http.get('/portal/api/wallet/requests')).status_code==401
            print('PUBLIC_PAGES_ASSETS_PREFIX_REQUEST_ID_AUTH_BOUNDARIES_OK',flush=True)
            # Server-issued, short-lived deployment check; no password or token is printed.
            session=await admin_auth.create_session()
            http.cookies.set(SESSION_COOKIE,session.raw_token)
            for path in ['/admin/api/auth/session','/admin/api/dashboard','/admin/api/console/summary','/admin/api/console/routing','/admin/api/billing/requests','/admin/api/billing/settings']:
                response=await http.get(path)
                assert response.status_code==200,(path,response.status_code)
                response.json()
            response=await http.post('/admin/api/billing/requests/verification-nonexistent/reverse',json={'reason':'verification'})
            assert response.status_code==403
            print('AUTHENTICATED_SESSION_DASHBOARD_ROUTING_BILLING_CSRF_OK',flush=True)
    finally:
        if session:
            await gateway_store.execute('DELETE FROM admin_sessions WHERE session_hash=?',(admin_auth._digest(session.raw_token),))
        await gateway_store.stop()


if __name__=='__main__': asyncio.run(main())
