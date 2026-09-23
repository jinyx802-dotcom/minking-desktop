"""Transactional prepaid reservations; stores quantities, never request content."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

from app.billing import (BillingError, money8, usd_text, ensure_settings_row,
                         get_price_row, quote_usage, is_generation_endpoint, _usage_quantities,
                         price_is_complete, _credit_key_balance)
from app.store.gateway import gateway_store, iso_now, utc_now


def _lock() -> str:
    return " FOR UPDATE" if gateway_store.engine == "mysql" else ""


def validate_price(price: dict | None, *, model: str = "") -> None:
    label = model or str((price or {}).get("model") or "")
    detail = f" ({label})" if label else ""
    if not price or str(price.get("status") or "active") != "active":
        raise BillingError(422, f"This model has no active price{detail}", code="unpriced_model")
    if not price_is_complete(price):
        raise BillingError(422, f"A required billing dimension is not priced{detail}", code="unpriced_model")


def _held(db, key_id: str) -> Decimal:
    return sum((money8(r["reserved_usd"]) for r in db.execute(
        "SELECT reserved_usd FROM billing_requests WHERE key_id=? AND state IN ('reserved','pending')",
        (key_id,),
    ).fetchall()), Decimal(0))


async def expire_reservations() -> None:
    await gateway_store.execute(
        "UPDATE billing_requests SET state='released',outcome='unverified_platform_cost',updated_at=? "
        "WHERE state IN ('reserved','pending') AND expires_at<=?", (iso_now(), iso_now()))


async def reserve(key_id: str, request_id: str, model: str, endpoint: str) -> None:
    if not model or not is_generation_endpoint(endpoint):
        return
    config = await ensure_settings_row()
    price = await get_price_row(model)
    validate_price(price, model=model)
    snapshot = json.dumps(price, sort_keys=True, default=str)
    config_json = json.dumps(config, sort_keys=True, default=str)
    version = hashlib.sha256((snapshot + config_json).encode()).hexdigest()[:24]
    await expire_reservations()

    def operation(db):
        key = db.execute("SELECT * FROM api_keys WHERE id=?" + _lock(), (key_id,)).fetchone()
        if not key or key["status"] == "deleted":
            raise BillingError(403, "Wallet owner not found", code="wallet_not_found")
        owner = str(key.get("owner_user_id") or "")
        user = db.execute("SELECT * FROM portal_users WHERE id=?", (owner,)).fetchone() if owner else None
        existing = db.execute("SELECT user_id,key_id,state FROM billing_requests WHERE request_id=?", (request_id,)).fetchone()
        if existing:
            if (existing.get("key_id") or existing["user_id"]) not in {key_id, owner}:
                raise BillingError(409, "Request identifier conflict", code="request_conflict")
            if existing["state"] != "reserved":
                raise BillingError(409, "Request is already finalized or awaiting reconciliation", code="request_finalized")
            return
        available = money8(key["usd_credit"]) - _held(db, key_id)
        budget = min(available, money8(config["request_budget_usd"]))
        if user and user.get("request_budget_usd") is not None:
            budget = min(budget, money8(user["request_budget_usd"]))
        if budget <= 0:
            raise BillingError(402, "Insufficient available credit", code="insufficient_usd_credit")
        minimum = quote_usage(model, settings_row=config, price_row=price,
                              images=1 if price.get("modality") == "image" else 0,
                              seconds=1 if price.get("modality") == "video" else 0)["sell_usd"]
        if minimum > budget:
            raise BillingError(402, "Request cost exceeds budget", code="request_budget_exceeded")
        now = iso_now()
        expires = (utc_now() + dt.timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        db.execute("""INSERT INTO billing_requests(request_id,user_id,key_id,model,endpoint,state,reserved_usd,
            price_version,price_snapshot,settings_snapshot,created_at,expires_at,updated_at)
            VALUES(?,?,?,?,?,'reserved',?,?,?,?,?,?,?)""",
            (request_id, owner or key_id, key_id, model, endpoint, usd_text(budget), version, snapshot, config_json, now, expires, now))
    await gateway_store.call(operation)


async def constrain_payload(request_id: str, payload: dict, *, supports_limit: bool = True) -> None:
    row = await gateway_store.one("SELECT * FROM billing_requests WHERE request_id=?", (request_id,))
    if not row or row["state"] != "reserved":
        return
    price, config = json.loads(row["price_snapshot"]), json.loads(row["settings_snapshot"])
    budget = money8(row["reserved_usd"])
    modality = price.get("modality", "text")
    # Character/byte count is deliberately conservative; it is not used for settlement.
    if modality == "text":
        inputs = payload.get("input", payload.get("messages", []))
        estimated_input = len(json.dumps(inputs, ensure_ascii=False).encode()) + len(str(payload.get("instructions", "")).encode())
        cost = quote_usage(row["model"], input_tokens=estimated_input, price_row=price, settings_row=config)["sell_usd"]
        if cost >= budget:
            raise BillingError(402, "Input exceeds request budget; increase budget or shorten input", code="request_budget_exceeded")
        rate = quote_usage(row["model"], output_tokens=1_000_000, price_row=price, settings_row=config)["sell_usd"]
        if supports_limit and rate > 0:
            limit = max(1, int((budget-cost) * Decimal(1_000_000) / rate))
            field = "max_tokens" if "messages" in payload else "max_output_tokens"
            existing = payload.get("max_completion_tokens", payload.get(field))
            field = "max_completion_tokens" if "max_completion_tokens" in payload else field
            payload[field] = min(int(existing), limit) if existing else limit
    else:
        cost = quote_usage(row["model"], images=int(payload.get("n", 1)) if modality == "image" else 0,
                           seconds=float(payload.get("seconds", payload.get("duration", 1))) if modality == "video" else 0,
                           price_row=price, settings_row=config)["sell_usd"]
        if cost > budget:
            raise BillingError(402, "Generation exceeds request budget", code="request_budget_exceeded")


async def prepare_image_price(request_id: str, model: str, count: int = 1) -> None:
    row = await gateway_store.one("SELECT * FROM billing_requests WHERE request_id=?", (request_id,))
    if not row:
        return
    snapshot = json.loads(row["price_snapshot"])
    price = snapshot.get("hosted_image_price") or await get_price_row(model)
    validate_price(price, model=model)
    cost = quote_usage(model,images=count,price_row=price,settings_row=json.loads(row["settings_snapshot"]))["sell_usd"]
    if cost > money8(row["reserved_usd"]):
        raise BillingError(402,"Hosted image cost exceeds budget",code="request_budget_exceeded")
    if "hosted_image_price" not in snapshot:
        snapshot["hosted_image_price"] = price
        text = json.dumps(snapshot,sort_keys=True,default=str)
        version = hashlib.sha256((text+row["settings_snapshot"]).encode()).hexdigest()[:24]
        await gateway_store.execute("UPDATE billing_requests SET price_snapshot=?,price_version=? WHERE request_id=? AND state='reserved'", (text,version,request_id))


async def settle(request_id: str, usage: dict | None, outcome: str, *, dispatched: bool = True) -> bool:
    row = await gateway_store.one("SELECT * FROM billing_requests WHERE request_id=?", (request_id,))
    if not row:
        return False
    # Only provider usage fields are allowed to persist.
    price, config = json.loads(row["price_snapshot"]), json.loads(row["settings_snapshot"])
    trusted = isinstance(usage, dict) and any(k in usage for k in (
        "input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "images", "seconds", "hosted_images"))
    quantities = _usage_quantities(usage, row["endpoint"], price) if trusted else None
    quote = quote_usage(row["model"], price_row=price, settings_row=config, **quantities) if quantities else None
    if quote and usage.get("hosted_images") and price.get("hosted_image_price"):
        image_price = price["hosted_image_price"]
        image_quote = quote_usage(image_price["model"],images=int(usage["hosted_images"]),price_row=image_price,settings_row=config)
        quote["sell_usd"] += image_quote["sell_usd"]
        quote["official_usd"] += image_quote["official_usd"]
        quantities["hosted_images"] = int(usage["hosted_images"])
    def operation(db):
        current = db.execute("SELECT * FROM billing_requests WHERE request_id=?" + _lock(), (request_id,)).fetchone()
        if current["state"] not in {"reserved", "pending"}:
            return
        wallet_id = current.get("key_id") or current["user_id"]
        key = db.execute("SELECT * FROM api_keys WHERE id=?" + _lock(), (wallet_id,)).fetchone()
        now = iso_now()
        if current["expires_at"] <= now:
            db.execute("UPDATE billing_requests SET state='released',outcome='unverified_platform_cost',updated_at=? WHERE request_id=?", (now,request_id))
            return
        if not quote:
            # Success can still receive usage later (an async video poll).
            # A failure has no later usage report. Leaving the full reservation
            # pending freezes the wallet until expiry, and expiry charges nothing.
            state = "pending" if dispatched and outcome == "success" else "released"
            db.execute("UPDATE billing_requests SET state=?,outcome=?,updated_at=? WHERE request_id=?",
                       (state,outcome,now,request_id))
            return
        if not key:
            db.execute("UPDATE billing_requests SET state='pending',outcome=?,updated_at=? WHERE request_id=?", (outcome,now,request_id))
            return
        actual = money8(quote["sell_usd"])
        charged = min(actual, money8(current["reserved_usd"]), money8(key["usd_credit"]))
        _credit_key_balance(
            db, key, -charged, kind="usage", actor="system", reason=outcome or "usage",
            request_id=request_id, card_id=None, official=usd_text(quote["official_usd"]),
            multiplier=str(quote["multiplier"]), now=now,
        )
        db.execute("""UPDATE billing_requests SET state='settled',actual_usd=?,charged_usd=?,absorbed_usd=?,
            usage_snapshot=?,outcome=?,updated_at=? WHERE request_id=?""",
            (usd_text(actual),usd_text(charged),usd_text(actual-charged),json.dumps(quantities),outcome,now,request_id))
        db.execute("UPDATE call_records SET usd_charged=? WHERE request_id=?", (usd_text(charged),request_id))
    await gateway_store.call(operation)
    return True


async def wallet_summary(user_id: str) -> dict:
    await expire_reservations()
    config = await ensure_settings_row()
    def operation(db):
        key = db.execute(
            "SELECT * FROM api_keys WHERE owner_user_id=? AND status!='deleted' ORDER BY created_at LIMIT 1",
            (user_id,),
        ).fetchone()
        if not key:
            key = db.execute("SELECT * FROM api_keys WHERE id=? AND status!='deleted'", (user_id,)).fetchone()
        owner = (key or {}).get("owner_user_id") or user_id
        row = db.execute("SELECT * FROM portal_users WHERE id=?", (owner,)).fetchone() or {}
        balance = money8((key or {}).get("usd_credit", 0))
        held = _held(db, str((key or {}).get("id") or user_id))
        cap = money8(config["request_budget_usd"])
        personal = row.get("request_budget_usd")
        budget = cap if personal is None else min(cap, money8(personal))
        return {"usd_credit":usd_text(balance),"available_usd":usd_text(balance-held),
                "reserved_usd":usd_text(held),"currency":"USD","enforced":True,
                "request_budget_usd":usd_text(budget)}
    return await gateway_store.call(operation)


async def bills(*, user_id: str | None = None, state: str | None = None, page: int = 1, start: str | None = None, end: str | None = None) -> dict:
    clauses, args = ["1=1"], []
    if user_id:
        clauses.append("user_id=?"); args.append(user_id)
    if state == 'open':
        clauses.append("state IN ('reserved','pending')")
    elif state:
        clauses.append("state=?"); args.append(state)
    if start:
        clauses.append("created_at>=?"); args.append(start)
    if end:
        clauses.append("created_at<?"); args.append(end)
    where = " AND ".join(clauses)
    total = await gateway_store.one(f"SELECT COUNT(*) AS n FROM billing_requests WHERE {where}", tuple(args))
    rows = await gateway_store.all(f"SELECT * FROM billing_requests WHERE {where} ORDER BY created_at DESC LIMIT 50 OFFSET ?", (*args,(max(1,page)-1)*50))
    for row in rows:
        row.pop("settings_snapshot", None)
        row["price_snapshot"] = json.loads(row["price_snapshot"])
        row["usage_snapshot"] = json.loads(row["usage_snapshot"]) if row["usage_snapshot"] else None
        for key in ("reserved_usd","actual_usd","charged_usd","absorbed_usd"):
            row[key] = usd_text(row[key]) if row[key] is not None else None
    return {"data":rows,"page":max(1,page),"total":total["n"]}


async def reverse(request_id: str, reason: str, actor: str) -> dict:
    if not reason.strip():
        raise BillingError(422,"A reason is required",code="reason_required")
    def operation(db):
        row = db.execute("SELECT * FROM billing_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise BillingError(404,"Bill not found",code="bill_not_found")
        wallet_id = row.get("key_id") or row["user_id"]
        key = db.execute("SELECT * FROM api_keys WHERE id=?" + _lock(), (wallet_id,)).fetchone()
        row = db.execute("SELECT * FROM billing_requests WHERE request_id=?" + _lock(), (request_id,)).fetchone()
        if row["state"] in {"reversed","released"}:
            return {"ok":True,"idempotent":True}
        if not key:
            raise BillingError(404,"Wallet owner not found",code="wallet_not_found")
        amount = money8(row["charged_usd"])
        _credit_key_balance(
            db, key, amount, kind="reversal", actor=actor, reason=reason[:500],
            request_id="reversal:"+request_id, card_id=None,
        )
        db.execute("UPDATE billing_requests SET state='reversed',updated_at=? WHERE request_id=?", (iso_now(),request_id))
        return {"ok":True,"idempotent":False}
    return await gateway_store.call(operation)
