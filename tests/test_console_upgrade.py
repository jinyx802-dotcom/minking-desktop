"""Regression coverage for money conservation, dynamic routing and tenant isolation."""
import asyncio
import datetime as dt
import json
import time
from decimal import Decimal

import pytest
from conftest import ADMIN_HEADERS, auth_payload, import_pool
from app.billing import BillingError, quote_usage, update_settings, grant_credit
from app.codex_gateway import codex_gateway, GatewayError
from app.console_schema import migrate_console
from app.scheduler import quota_state, binding
from app.store.gateway import gateway_store, iso_now
from app.wallet_engine import reserve, settle, wallet_summary, reverse, expire_reservations, constrain_payload


def wallet(client, credit='1.00', user='payer'):
    client.portal.call(gateway_store.execute,
        "INSERT INTO portal_users(id,email,name,usd_credit,created_at) VALUES(?,?,?,?,?)",
        (user,user+'@example.test',user,credit,iso_now()))
    client.portal.call(gateway_store.execute,
        "INSERT INTO api_keys(id,name,key_prefix,fingerprint,owner_user_id,status,created_at,usd_credit) VALUES(?,?,?,?,?,'active',?,?)",
        (user,user,'sk-'+user,'fp-'+user,user,iso_now(),credit))
    client.portal.call(gateway_store.execute,
        "INSERT INTO model_official_prices(model,provider,modality,official_input_usd_per_1m,official_output_usd_per_1m,status) VALUES('metered','codex','text','1','2','active')")
    async def enable():
        await update_settings(enforced=True,price_multiplier='1')
    client.portal.call(enable)
    return user


def hold(client, user, request_id):
    client.portal.call(reserve,user,request_id,'metered','/v1/responses')


def test_microcharges_snapshot_idempotence_and_credit_preserve_fraction(client):
    user=wallet(client)
    hold(client,user,'micro')
    client.portal.call(gateway_store.execute,"UPDATE model_official_prices SET official_input_usd_per_1m='100' WHERE model='metered'")
    client.portal.call(settle,'micro',{'input_tokens':1,'output_tokens':1},'success')
    client.portal.call(settle,'micro',{'input_tokens':1,'output_tokens':1},'success')
    result=client.portal.call(wallet_summary,user)
    assert Decimal(result['usd_credit'])==Decimal('0.999997')
    async def credit():
        return await grant_credit(user_id=user,amount='1',actor='test')
    client.portal.call(credit)
    assert Decimal(client.portal.call(wallet_summary,user)['usd_credit'])==Decimal('1.999997')
    assert client.portal.call(gateway_store.one,"SELECT COUNT(*) AS n FROM wallet_ledger WHERE request_id='micro'")['n']==1


def test_parallel_reservations_never_overspend(client):
    user=wallet(client,'0.60')
    async def many():
        return await asyncio.gather(*(reserve(user,str(i),'metered','/v1/responses') for i in range(5)),return_exceptions=True)
    results=client.portal.call(many)
    assert sum(isinstance(r,BillingError) for r in results)==3
    summary=client.portal.call(wallet_summary,user)
    assert Decimal(summary['reserved_usd'])==Decimal('.60')
    assert Decimal(summary['available_usd'])==0


def test_budget_cap_platform_difference_and_reversal(client):
    user=wallet(client)
    hold(client,user,'expensive')
    client.portal.call(settle,'expensive',{'output_tokens':1_000_000},'interrupted')
    row=client.portal.call(gateway_store.one,"SELECT * FROM billing_requests WHERE request_id='expensive'")
    assert Decimal(row['charged_usd'])==Decimal('.5')
    assert Decimal(row['absorbed_usd'])==Decimal('1.5')
    client.portal.call(reverse,'expensive','Verified refund','admin')
    client.portal.call(reverse,'expensive','Verified refund','admin')
    assert Decimal(client.portal.call(wallet_summary,user)['usd_credit'])==1


def test_unknown_usage_timeout_never_retroactively_charges(client):
    user=wallet(client)
    hold(client,user,'unknown')
    client.portal.call(settle,'unknown',None,'interrupted')
    row=client.portal.call(gateway_store.one,"SELECT state,charged_usd FROM billing_requests WHERE request_id='unknown'")
    assert row['state']=='released'
    assert Decimal(row['charged_usd'])==0
    assert Decimal(client.portal.call(wallet_summary,user)['reserved_usd'])==0
    client.portal.call(settle,'unknown',{'output_tokens':500},'success')
    assert Decimal(client.portal.call(wallet_summary,user)['usd_credit'])==1
    assert Decimal(client.portal.call(wallet_summary,user)['reserved_usd'])==0


def test_failed_dispatch_without_usage_does_not_stack_holds(client):
    user=wallet(client)
    hold(client,user,'fail-a')
    client.portal.call(settle,'fail-a',None,'failed')
    hold(client,user,'fail-b')
    client.portal.call(settle,'fail-b',None,'failed')
    summary=client.portal.call(wallet_summary,user)
    assert Decimal(summary['usd_credit'])==1
    assert Decimal(summary['reserved_usd'])==0
    assert Decimal(summary['available_usd'])==1


def test_output_delta_bytes_count_streamed_text_once():
    from app.codex_gateway import _output_delta_bytes

    assert _output_delta_bytes(
        "response.output_text.delta",
        '{"type":"response.output_text.delta","delta":"hi"}',
    ) == 2
    assert _output_delta_bytes(
        "response.output_text.done",
        '{"type":"response.output_text.done","text":"hi"}',
    ) == 0
    assert _output_delta_bytes("", '{"choices":[{"delta":{"content":"ab"}}]}') == 2


def test_failed_dispatch_charges_forwarded_output_then_releases(client):
    user=wallet(client)
    hold(client,user,'partial')
    client.portal.call(settle,'partial',{'output_tokens':1000},'failed')
    row=client.portal.call(gateway_store.one,"SELECT state,charged_usd FROM billing_requests WHERE request_id='partial'")
    assert row['state']=='settled'
    assert Decimal(row['charged_usd'])==Decimal('0.002')
    summary=client.portal.call(wallet_summary,user)
    assert Decimal(summary['reserved_usd'])==0
    assert Decimal(summary['usd_credit'])==Decimal('0.998')


def test_pre_dispatch_failure_releases_hold_and_unpriced_is_rejected(client):
    user=wallet(client)
    hold(client,user,'rejected')
    async def reject():
        await settle('rejected',None,'failed',dispatched=False)
    client.portal.call(reject)
    assert Decimal(client.portal.call(wallet_summary,user)['reserved_usd'])==0
    with pytest.raises(BillingError):
        client.portal.call(reserve,user,'missing','no-price','/v1/responses')


def test_payload_limit_and_budget_preflight(client):
    user=wallet(client)
    hold(client,user,'limited')
    payload={'messages':[{'role':'user','content':'hello'}],'max_tokens':1_000_000}
    client.portal.call(constrain_payload,'limited',payload)
    assert 0<payload['max_tokens']<250_000


def test_console_api_auth_and_atomic_reversal(client):
    user=wallet(client)
    hold(client,user,'console')
    assert client.get('/admin/api/console/summary').status_code==200
    assert client.get('/admin/api/console/summary?start=2026-09-22&end=2026-09-21').status_code==422
    assert client.post('/admin/api/billing/requests/console/reverse',json={'reason':'test'}).status_code==403
    response=client.post('/admin/api/billing/requests/console/reverse',json={'reason':'Test release'},headers=ADMIN_HEADERS)
    assert response.status_code==200
    assert client.get('/admin/api/billing/requests').json()['data'][0]['state']=='reversed'


def test_schema_upgrade_idempotent_keeps_old_money(client):
    user=wallet(client,'42.17')
    client.portal.call(migrate_console)
    client.portal.call(migrate_console)
    assert Decimal(client.portal.call(wallet_summary,user)['usd_credit'])==Decimal('42.17')


def test_dynamic_load_quota_hysteresis_and_unknown_fallback(client):
    imported=import_pool(client,auth_payload('pool-a'),auth_payload('pool-b'))
    key=client.portal.call(codex_gateway.authenticate_key,imported['generated_api_key']['key'])
    async def choose():
        return await codex_gateway.select_account(key,kind='chat',provider='codex',model='metered')
    selected=[client.portal.call(choose)['account_id'] for _ in range(20)]
    assert selected.count('pool-a')==selected.count('pool-b')==10
    def cache(percent):
        codex_gateway._quota_cache['pool-a']=(time.monotonic(),{'limits':[{'primary':{'remaining_percent':percent}}]})
    cache(9)
    assert client.portal.call(choose)['account_id']=='pool-b'
    cache(12)
    assert quota_state(codex_gateway,'pool-a','metered')[1]
    cache(15)
    assert not quota_state(codex_gateway,'pool-a','metered')[1]
    codex_gateway._quota_cache.clear()
    assert quota_state(codex_gateway,'pool-a','metered')[0] is None


def test_manual_preference_uses_another_account_when_preferred_cannot(client):
    imported = import_pool(client, auth_payload("pref-down"), auth_payload("pref-up"))
    key = client.portal.call(codex_gateway.authenticate_key, imported["generated_api_key"]["key"])
    assert client.put(
        f"/admin/api/keys/{imported['generated_api_key']['id']}/route",
        json={"provider": "codex", "preferred_account_id": "pref-down"},
        headers=ADMIN_HEADERS,
    ).status_code == 200

    async def choose():
        return await codex_gateway.select_account(key, kind="chat", provider="codex", model="metered")

    assert client.portal.call(choose)["account_id"] == "pref-down"
    client.portal.call(gateway_store.execute, "UPDATE accounts SET status='disabled' WHERE account_id=?", ("pref-down",))
    assert client.portal.call(choose)["account_id"] == "pref-up"
    event = client.portal.call(
        gateway_store.one, "SELECT reason FROM routing_events ORDER BY created_at DESC LIMIT 1"
    )
    assert event["reason"] == "failover"
    client.portal.call(gateway_store.execute, "UPDATE accounts SET status='active' WHERE account_id=?", ("pref-down",))
    codex_gateway._quota_cache["pref-down"] = (
        time.monotonic(), {"limits": [{"primary": {"remaining_percent": 0}}]}
    )
    assert client.portal.call(choose)["account_id"] == "pref-up"
    codex_gateway._quota_cache.clear()


def test_continuation_isolated_hashed_and_does_not_switch(client):
    imported=import_pool(client,auth_payload('bound-a'),auth_payload('bound-b'))
    key=client.portal.call(codex_gateway.authenticate_key,imported['generated_api_key']['key'])
    async def choose():
        return await codex_gateway.select_account(key,kind='responses',provider='codex',model='metered',conversation='private-session-value')
    first=client.portal.call(choose)
    assert client.portal.call(choose)['account_id']==first['account_id']
    rows=client.portal.call(gateway_store.all,'SELECT * FROM routing_bindings')
    assert 'private-session-value' not in json.dumps(rows)
    client.portal.call(gateway_store.execute,"UPDATE accounts SET status='disabled' WHERE account_id=?",(first['account_id'],))
    with pytest.raises(GatewayError,match='Pinned account unavailable'):
        client.portal.call(choose)


def test_separate_reasoning_is_not_counted_twice():
    quote=quote_usage('x',output_tokens=100,reasoning_tokens=40,settings_row={'price_multiplier':'1'},
        price_row={'model':'x','official_output_usd_per_1m':'1','official_reasoning_usd_per_1m':'2'})
    assert quote['sell_usd']==Decimal('0.00014')


def test_finalized_identifier_cannot_authorize_another_generation(client):
    user=wallet(client)
    hold(client,user,'once')
    client.portal.call(settle,'once',{'output_tokens':1},'success')
    with pytest.raises(BillingError,match='already finalized'):
        hold(client,user,'once')


def test_expiry_checked_even_without_maintenance(client):
    user=wallet(client)
    hold(client,user,'late')
    client.portal.call(gateway_store.execute,"UPDATE billing_requests SET expires_at='2000-01-01T00:00:00Z'")
    client.portal.call(settle,'late',{'output_tokens':1000},'success')
    assert Decimal(client.portal.call(wallet_summary,user)['usd_credit'])==1


def test_missing_required_price_dimension_rejected(client):
    user=wallet(client)
    client.portal.call(gateway_store.execute,"UPDATE model_official_prices SET official_output_usd_per_1m=NULL WHERE model='metered'")
    with pytest.raises(BillingError,match='dimension'):
        hold(client,user,'incomplete')


def test_video_async_completion_polls_settle_original_request_once(client,monkeypatch):
    user=wallet(client)
    client.portal.call(gateway_store.execute,"UPDATE model_official_prices SET modality='video',official_usd_per_second='0.02' WHERE model='metered'")
    client.portal.call(reserve,user,'video-original','metered','/v1/videos')
    client.portal.call(settle,'video-original',None,'success')
    async def fetch(*args):
        return {'status':'done','video':{'duration':6}}
    async def touch(*args):
        pass
    monkeypatch.setattr(codex_gateway,'_fetch_grok_video_result',fetch)
    monkeypatch.setattr(codex_gateway,'_touch_video_job',touch)
    job={'billing_request_id':'video-original','model':'metered','created_at':iso_now()}
    for _ in range(2):
        result=client.portal.call(codex_gateway._get_grok_video,'video-id',job,{},None)
        assert result['status']=='completed'
    assert Decimal(client.portal.call(wallet_summary,user)['usd_credit'])==Decimal('.88')
    assert client.portal.call(gateway_store.one,"SELECT COUNT(*) AS n FROM wallet_ledger WHERE request_id='video-original'")['n']==1


def test_portal_bill_scope_budget_and_admin_date_filter(client):
    from app import portal
    from app.main import app
    user=wallet(client)
    hold(client,user,'own-bill')
    client.portal.call(gateway_store.execute,"INSERT INTO portal_users(id,email,name,usd_credit,created_at) VALUES('other','other@example.test','Other','5',?)",(iso_now(),))
    client.portal.call(gateway_store.execute,"INSERT INTO api_keys(id,name,key_prefix,fingerprint,owner_user_id,status,created_at,usd_credit) VALUES('other','Other','sk-other','fp-other','other','active',?,'5')",(iso_now(),))
    hold(client,'other','other-bill')
    identity=portal.PortalSession(user_id=user,csrf_token='test',name='Payer',email='payer@example.test')
    app.dependency_overrides[portal.session]=lambda: identity
    app.dependency_overrides[portal.write_session]=lambda: identity
    try:
        rows=client.get('/portal/api/wallet/requests').json()['data']
        assert [r['request_id'] for r in rows]==['own-bill']
        assert client.put('/portal/api/wallet/budget',json={'request_budget_usd':'.6'}).status_code==422
        assert client.put('/portal/api/wallet/budget',json={'request_budget_usd':'.1'}).status_code==200
        hold(client,user,'lower-budget')
        row=client.portal.call(gateway_store.one,"SELECT reserved_usd FROM billing_requests WHERE request_id='lower-budget'")
        assert Decimal(row['reserved_usd'])==Decimal('.1')
        assert client.get('/admin/api/billing/requests?state=open').json()['total']==3
        assert client.get('/admin/api/billing/requests?start=2000-01-01&end=2000-01-01').json()['total']==0
    finally:
        app.dependency_overrides.pop(portal.session,None)
        app.dependency_overrides.pop(portal.write_session,None)
