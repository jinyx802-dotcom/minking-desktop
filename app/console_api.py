"""Read models for the console and authenticated wallet operations."""
import datetime as dt
import math
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.codex_gateway import AdminRead, AdminWrite
from app.codex_gateway import codex_gateway
from app.billing import BillingError, quote_usage, get_price_row, ensure_settings_row, usd_text, money8
from app.portal import PortalSession, session, write_session, router as portal_router
from app.store.gateway import gateway_store, iso_now
from app import wallet_engine

router = APIRouter()


def bounds(start, end):
    zone = dt.timezone(dt.timedelta(hours=8))
    today = dt.datetime.now(zone).date()
    a, b = start or today, end or today
    if b < a or (b-a).days > 366:
        raise HTTPException(422,"日期范围必须在 367 天以内")
    def utc(day):
        return dt.datetime.combine(day,dt.time(),zone).astimezone(dt.timezone.utc).isoformat().replace("+00:00","Z")
    return utc(a),utc(b+dt.timedelta(days=1))


async def summary(start=None,end=None,user_id=None):
    a,b = bounds(start,end)
    args = [a,b]
    condition = "c.started_at>=? AND c.started_at<?"
    if user_id:
        condition += " AND k.owner_user_id=?"; args.append(user_id)
    rows = await gateway_store.all(f"SELECT c.started_at,c.status,c.duration_ms,c.total_tokens,c.usd_charged FROM call_records c LEFT JOIN api_keys k ON k.id=c.key_id WHERE {condition}",tuple(args))
    durations = sorted(int(r["duration_ms"]) for r in rows if r["duration_ms"] is not None)
    daily = {}
    for row in rows:
        day = (dt.datetime.fromisoformat(row["started_at"].replace("Z","+00:00"))+dt.timedelta(hours=8)).date().isoformat()
        daily[day] = daily.get(day,0)+1
    first = dt.datetime.fromisoformat(a.replace('Z','+00:00')).astimezone(dt.timezone(dt.timedelta(hours=8))).date()
    last = dt.datetime.fromisoformat(b.replace('Z','+00:00')).astimezone(dt.timezone(dt.timedelta(hours=8))).date()
    while first < last:
        daily.setdefault(first.isoformat(),0)
        first += dt.timedelta(days=1)
    bill_where = "created_at>=? AND created_at<?" + (" AND user_id=?" if user_id else "")
    bills = await gateway_store.all(f"SELECT state,reserved_usd,charged_usd,absorbed_usd FROM billing_requests WHERE {bill_where}",tuple(args))
    charge = sum((money8(r["usd_charged"] or 0) for r in rows), Decimal(0))
    pending = sum((money8(r["reserved_usd"]) for r in bills if r["state"] in {"reserved","pending"}),Decimal(0))
    return {"calls":len(rows),"success_rate":round(100*sum(r["status"]=="success" for r in rows)/len(rows),1) if rows else None,
            "p95_ms":durations[max(0,math.ceil(len(durations)*.95)-1)] if durations else None,
            "charged_usd":usd_text(charge),"pending_usd":usd_text(pending),
            "absorbed_usd":usd_text(sum((money8(r["absorbed_usd"]) for r in bills),Decimal(0))),
            "total_tokens":sum(int(r["total_tokens"] or 0) for r in rows),
            "trend":[{"date":d,"calls":n} for d,n in sorted(daily.items())],"start":a,"end":b,"updated_at":iso_now()}


@router.get("/admin/api/console/summary")
async def admin_summary(_session: AdminRead,start:dt.date|None=None,end:dt.date|None=None):
    result = await summary(start,end)
    healthy = await gateway_store.one("SELECT COUNT(*) AS n FROM accounts WHERE status='active' AND (cooldown_until IS NULL OR cooldown_until<=?)", (iso_now(),))
    result["healthy_accounts"] = healthy["n"]
    return result


@router.get("/admin/api/console/routing")
async def routing_status(_session: AdminRead):
    from app.scheduler import status
    return await status(codex_gateway)


@router.get("/admin/api/billing/requests")
async def admin_bills(_session: AdminRead,state:str|None=None,page:int=Query(1,ge=1),start:dt.date|None=None,end:dt.date|None=None):
    a,b = bounds(start,end) if start or end else (None,None)
    return await wallet_engine.bills(state=state,page=page,start=a,end=b)


class Reversal(BaseModel):
    reason: str = Field(min_length=1,max_length=500)


@router.post("/admin/api/billing/requests/{request_id}/reverse")
async def reverse_bill(request_id:str,body:Reversal,admin:AdminWrite):
    try:
        return await wallet_engine.reverse(request_id,body.reason,admin.username)
    except BillingError as exc:
        raise HTTPException(exc.status,exc.message) from exc


class Quote(BaseModel):
    model:str
    input_tokens:int=Field(0,ge=0)
    output_tokens:int=Field(0,ge=0)
    cached_tokens:int=Field(0,ge=0)
    images:int=Field(0,ge=0)
    seconds:float=Field(0,ge=0,allow_inf_nan=False)


@router.post("/admin/api/billing/quote")
async def preview_quote(body:Quote,_session:AdminRead):
    quote = quote_usage(**body.model_dump(),price_row=await get_price_row(body.model),settings_row=await ensure_settings_row())
    return {k:str(v) if isinstance(v,Decimal) else v for k,v in quote.items()}


@portal_router.get("/summary")
async def user_summary(user:PortalSession=Depends(session),start:dt.date|None=None,end:dt.date|None=None):
    return await summary(start,end,user.user_id)


@portal_router.get("/wallet/requests")
async def user_bills(user:PortalSession=Depends(session),state:str|None=None,page:int=Query(1,ge=1)):
    return await wallet_engine.bills(user_id=user.user_id,state=state,page=page)


class Budget(BaseModel):
    request_budget_usd:Decimal=Field(gt=0,le=10000,allow_inf_nan=False)


@portal_router.put("/wallet/budget")
async def user_budget(body:Budget,user:PortalSession=Depends(write_session)):
    config = await ensure_settings_row()
    if body.request_budget_usd > money8(config["request_budget_usd"]):
        raise HTTPException(422,"用户预算不能超过平台单次预算")
    await gateway_store.execute("UPDATE portal_users SET request_budget_usd=? WHERE id=?",(usd_text(body.request_budget_usd),user.user_id))
    return await wallet_engine.wallet_summary(user.user_id)
