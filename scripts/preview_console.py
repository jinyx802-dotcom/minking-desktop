"""Isolated loopback-only UI sandbox. No production configuration or upstream traffic.

Run: python scripts/preview_console.py
"""
from __future__ import annotations
import base64
import datetime as dt
import io
import json
import secrets
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
import uvicorn
from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from app import config

preview_dir = Path(tempfile.mkdtemp(prefix="minking-console-preview-"))
config.settings = config.Settings(_env_file=None,data_dir=preview_dir,mysql_host="",
    host="127.0.0.1",port=8791,admin_initial_password=secrets.token_urlsafe(30),
    routing_secret=secrets.token_urlsafe(32),portal_enabled=True,
    gateway_public_base_url="https://preview.invalid",portal_smtp_host="preview.invalid",
    portal_smtp_from="demo@example.test",gateway_import_local_enabled=False,
    gateway_request_logging_enabled=False,gateway_diagnostic_logging_enabled=False,access_log_enabled=False)
from app.main import app
from app.admin_auth import AdminSession
from app.api.codex_gateway import _admin_read, _admin_write
from app.portal import PortalSession, session, write_session
from app.codex_gateway import codex_gateway
from app.http_client import set_http_transport
from app.store.gateway import gateway_store, iso_now, utc_now
from app.billing import sync_official_prices, update_settings
from app.wallet_engine import reserve, settle

csrf = secrets.token_urlsafe(24)
async def preview_admin(request:Request):
    if request.client.host not in {"127.0.0.1","::1"}:
        raise HTTPException(403)
    if request.method not in {"GET","HEAD"} and request.headers.get('origin') != 'http://127.0.0.1:8791':
        raise HTTPException(403)
    return AdminSession('本地演示',csrf,'2099-01-01T00:00:00Z')
async def preview_user(request:Request):
    await preview_admin(request)
    return PortalSession('preview-user',csrf,'小明','demo@example.test')
app.dependency_overrides.update({_admin_read:preview_admin,_admin_write:preview_admin,session:preview_user,write_session:preview_user})


def upstream(request):
    if request.url.path.endswith('/responses'):
        body={'id':'resp_'+uuid.uuid4().hex,'object':'response','status':'completed','model':'gpt-6-astra',
              'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'本地模拟调用成功。'}]}],
              'usage':{'input_tokens':1200,'output_tokens':420,'total_tokens':1620}}
        event={'type':'response.completed','response':body}
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content='event: response.completed\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n')
    return httpx.Response(503,json={'error':{'message':'Preview blocks external requests'}})
set_http_transport(httpx.MockTransport(upstream))


async def demo_quotas(*,refresh=False):
    result=[]
    for i,remaining in enumerate((78,46,8)):
        aid=f'demo-codex-{i+1}'
        quota={'account_id':aid,'provider':'codex','label':f'创作节点 0{i+1}',
               'limits':[{'primary':{'label':'5 小时窗口','remaining_percent':remaining,'used_percent':100-remaining,
                                     'reset_at':int(time.time())+3600}}],'cached':True,'stale':False}
        codex_gateway._quota_cache[aid]=(time.monotonic(),quota)
        result.append(quota)
    return {'data':result}
codex_gateway.account_quotas=demo_quotas
original_lifespan=app.router.lifespan_context
demo_key=None

@asynccontextmanager
async def preview_lifespan(application):
    global demo_key
    async with original_lifespan(application):
        for i in range(3):
            aid=f'demo-codex-{i+1}'
            claims={'https://api.openai.com/auth':{'chatgpt_account_id':aid},'exp':4900000000}
            encoded=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
            auth={'auth_mode':'chatgpt','tokens':{'access_token':f'eyJhbGciOiJub25lIn0.{encoded}.','refresh_token':'preview-only','account_id':aid}}
            await codex_gateway.import_accounts([UploadFile(file=io.BytesIO(json.dumps(auth).encode()),filename='preview.json')])
            await gateway_store.execute('UPDATE accounts SET label=? WHERE account_id=?',(f'创作节点 0{i+1}',aid))
        created=await codex_gateway.create_api_key('小明 · 创作工作台')
        await gateway_store.execute("INSERT INTO portal_users(id,email,name,usd_credit,created_at) VALUES('preview-user','demo@example.test','小明','25.00',?)",(iso_now(),))
        await gateway_store.execute("UPDATE api_keys SET owner_user_id='preview-user' WHERE id=?",(created['id'],))
        demo_key=await codex_gateway.authenticate_key(created['key'])
        await sync_official_prices()
        await update_settings(enforced=True)
        for i in range(180):
            started=(utc_now()-dt.timedelta(minutes=i*48)).isoformat().replace('+00:00','Z')
            state='failed' if i%19==0 else 'success'
            await gateway_store.execute("""INSERT INTO call_records(request_id,started_at,ended_at,key_id,key_name,account_id,
                endpoint,model,status,http_status,duration_ms,total_tokens,input_tokens,output_tokens,usage_unknown,usd_charged)
                VALUES(?,?,?,?,?,?,'/v1/responses','gpt-6-astra',?,?,?,?,?,?,0,?)""",
                (f'demo-history-{i}',started,started,created['id'],'小明 · 创作工作台',f'demo-codex-{i%2+1}',state,
                 502 if state=='failed' else 200,840+i%13*290,1620,1200,420,'0.00126' if state=='success' else '0'))
        for i in range(6):
            rid=f'demo-bill-{i}'
            await reserve('preview-user',rid,'gpt-6-astra','/v1/responses')
            await settle(rid,{'input_tokens':1200,'output_tokens':420} if i<4 else None,'success' if i<4 else 'interrupted')
        await demo_quotas()
        yield
app.router.lifespan_context=preview_lifespan


@app.post('/preview/call')
async def demo_call(request:Request):
    await preview_admin(request)
    context=await codex_gateway.begin_call(request_id=uuid.uuid4().hex,key=demo_key,
        endpoint='/v1/responses',model='gpt-6-astra',is_stream=False)
    try:
        lease,_,_=await codex_gateway.response_request({'model':'gpt-6-astra','input':'Local demonstration'},demo_key,None,context)
        completed,_=await codex_gateway.collect_response(lease,context)
        await codex_gateway.finish_call(context,status='success',http_status=200,completed=completed)
        return {'ok':True,'account':context.account_id,'request_id':context.request_id}
    except Exception:
        await codex_gateway.finish_call(context,status='failed',http_status=502)
        raise


# The production app ends with an MCP catch-all; put this demo-only route before it.
app.router.routes.insert(0, app.router.routes.pop())

@app.middleware('http')
async def preview_banner(request:Request,call_next):
    response=await call_next(request)
    if response.headers.get('content-type','').startswith('text/html'):
        from starlette.responses import HTMLResponse
        body=b''.join([chunk async for chunk in response.body_iterator]).decode()
        banner='''<div style="position:fixed;bottom:10px;left:50%;transform:translateX(-50%);z-index:90;display:flex;gap:14px;align-items:center;padding:9px 18px;border:1px solid #cbc9ee;background:#ffffffed;border-radius:12px;color:#514b79;font-size:12px;box-shadow:0 4px 25px #25234d18">本地模拟数据 <a href="/admin">后台</a><a href="/portal">用户门户</a><button id="preview-call" style="font-size:12px;padding:5px 10px;min-height:28px">模拟调用</button><span id="preview-result" role="status"></span></div><script>document.getElementById('preview-call').onclick=async function(){this.disabled=true;try{const r=await fetch('/preview/call',{method:'POST'});const b=await r.json();document.getElementById('preview-result').textContent=r.ok?'成功 · '+b.account:'调用未成功';if(typeof refreshPage==='function')refreshPage();else if(typeof refreshDashboard==='function')refreshDashboard();}finally{this.disabled=false;}};</script>'''
        banner='<style>body>div:has(>#preview-call){white-space:nowrap;width:max-content;max-width:calc(100vw - 16px);flex-wrap:wrap;justify-content:center;gap:8px!important}</style>'+banner
        return HTMLResponse(body.replace('</body>',banner+'</body>'),status_code=response.status_code)
    return response

if __name__=='__main__':
    uvicorn.run(app,host='127.0.0.1',port=8791,access_log=False,log_level='warning')
