"""Wallet, official catalog, and usage quoting. Never log secrets or card plaintext."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.config import settings
from app.store.gateway import SHANGHAI_TIMEZONE, gateway_store, iso_now, utc_now

logger = logging.getLogger("transfer_station.errors")

TWOPLACES = Decimal("0.01")
FOURPLACES = Decimal("0.0001")
EIGHTPLACES = Decimal("0.00000001")
MILLION = Decimal("1000000")
DEFAULT_MULTIPLIER = Decimal("0.12")
MAX_CREDIT_USD = Decimal("10000.00")
HMAC_MAX_SKEW_SECONDS = 300
CARD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CARD_CODE_RE = re.compile(r"^MK-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")
OFFICIAL_COLUMNS = (
    "provider",
    "modality",
    "official_input_usd_per_1m",
    "official_output_usd_per_1m",
    "official_cached_usd_per_1m",
    "official_reasoning_usd_per_1m",
    "official_usd_per_image",
    "official_usd_per_second",
)
OVERRIDE_COLUMNS = (
    "multiplier_override",
    "sell_override_input",
    "sell_override_output",
    "sell_override_cached",
    "sell_override_image",
    "sell_override_second",
    "status",
)
CATALOG_PATH = Path(__file__).with_name("billing_prices.json")


class BillingError(Exception):
    def __init__(self, status: int, message: str, *, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def money(value: Any, places: Decimal = FOURPLACES) -> Decimal:
    if value is None or value == "":
        return Decimal("0").quantize(places, rounding=ROUND_HALF_UP)
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except InvalidOperation as exc:
            raise BillingError(422, "Invalid monetary value", code="invalid_amount") from exc
    if not parsed.is_finite():
        raise BillingError(422, "Invalid monetary value", code="invalid_amount")
    return parsed.quantize(places, rounding=ROUND_HALF_UP)


def money2(value: Any) -> Decimal:
    return money(value, TWOPLACES)


def money4(value: Any) -> Decimal:
    return money(value, FOURPLACES)


def money8(value: Any) -> Decimal:
    return money(value, EIGHTPLACES)


def usd_text(value: Any, places: Decimal = EIGHTPLACES) -> str:
    quantized = money(value, places)
    if places == TWOPLACES:
        return f"{quantized:.2f}"
    if places == EIGHTPLACES:
        whole, fraction = format(quantized, "f").split(".")
        return whole + "." + fraction.rstrip("0").ljust(2, "0")
    return format(quantized, "f")


def dec_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def load_catalog() -> dict[str, Any]:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"source": "official", "as_of": "2026-09-20", "models": []}
    models = payload.get("models")
    if not isinstance(models, list):
        payload["models"] = []
    return payload


def is_generation_endpoint(endpoint: str) -> bool:
    path = (endpoint or "").split("?")[0].rstrip("/").lower()
    if path.endswith("/v1/videos") or path.endswith("/videos/generations"):
        return True
    markers = (
        "/v1/responses",
        "/v1/chat/completions",
        "/v1/messages",
        ":generatecontent",
        ":streamgeneratecontent",
        "/images/generations",
        "/images/edits",
        "/videos/edits",
        "/videos/extensions",
        "/remix",
        "generate_image",
        "edit_image",
    )
    return any(token in path for token in markers)


def is_billable_text_endpoint(endpoint: str) -> bool:
    return is_generation_endpoint(endpoint) and not any(
        token in (endpoint or "").lower()
        for token in ("/images", "/videos", "generate_image", "edit_image", "/remix")
    )


def quote_usage(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    images: int | float = 0,
    seconds: int | float = 0,
    settings_row: dict[str, Any] | None = None,
    price_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    global_multiplier = money4(
        (settings_row or {}).get("price_multiplier") or DEFAULT_MULTIPLIER
    )
    if not price_row or str(price_row.get("status") or "active") != "active":
        if model and not price_row:
            logger.info("unpriced_model model=%s", str(model)[:80])
        return {
            "model": model,
            "official_usd": Decimal("0.0000"),
            "multiplier": global_multiplier,
            "sell_usd": Decimal("0.0000"),
            "unpriced": True,
        }
    override_multiplier = dec_or_none(price_row.get("multiplier_override"))
    multiplier = money4(override_multiplier if override_multiplier is not None else global_multiplier)
    cached = max(0, int(cached_tokens or 0))
    billed_input = max(0, int(input_tokens or 0) - cached)
    # A separately priced reasoning dimension is a subset of output, not extra output.
    separate_reasoning = price_row.get("official_reasoning_usd_per_1m") is not None
    quantities = {
        "input": Decimal(billed_input) / MILLION,
        "output": Decimal(max(0, int(output_tokens or 0) - (int(reasoning_tokens or 0) if separate_reasoning else 0))) / MILLION,
        "cached": Decimal(cached) / MILLION,
        "reasoning": Decimal(max(0, int(reasoning_tokens or 0))) / MILLION if separate_reasoning else Decimal(0),
        "image": Decimal(str(max(0, images or 0))),
        "second": Decimal(str(max(0, seconds or 0))),
    }
    official_keys = {
        "input": "official_input_usd_per_1m",
        "output": "official_output_usd_per_1m",
        "cached": "official_cached_usd_per_1m",
        "reasoning": "official_reasoning_usd_per_1m",
        "image": "official_usd_per_image",
        "second": "official_usd_per_second",
    }
    official_usd = Decimal("0")
    sell_usd = Decimal("0")
    for dimension, qty in quantities.items():
        if qty <= 0:
            continue
        official_rate = dec_or_none(price_row.get(official_keys[dimension])) or Decimal("0")
        official_part = official_rate * qty
        official_usd += official_part
        sell_usd += official_part * multiplier
    return {
        "model": model,
        "official_usd": money8(official_usd),
        "multiplier": multiplier,
        "sell_usd": money8(sell_usd),
        "unpriced": False,
    }


def price_is_complete(price_row: dict[str, Any] | None) -> bool:
    if not price_row or str(price_row.get("status") or "active") != "active":
        return False
    modality = str(price_row.get("modality") or "text")
    if modality == "image":
        return dec_or_none(price_row.get("official_usd_per_image")) is not None
    if modality == "video":
        return dec_or_none(price_row.get("official_usd_per_second")) is not None
    return (
        dec_or_none(price_row.get("official_input_usd_per_1m")) is not None
        and dec_or_none(price_row.get("official_output_usd_per_1m")) is not None
    )


def _settings_from_row(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    return {
        "price_multiplier": usd_text(row.get("price_multiplier") or DEFAULT_MULTIPLIER, FOURPLACES),
        "enforced": bool(int(row.get("enforced") or 0)),
        "request_budget_usd": usd_text(row.get("request_budget_usd") or "0.50"),
        "new_user_usd": usd_text(row.get("new_user_usd") or "0.00"),
        "updated_at": row.get("updated_at"),
    }


async def ensure_settings_row() -> dict[str, Any]:
    def operation(db: Any) -> dict[str, Any]:
        row = db.execute("SELECT * FROM billing_settings WHERE id=1").fetchone()
        if row is None:
            now = iso_now()
            db.execute(
                """
                INSERT INTO billing_settings(id,price_multiplier,enforced,new_user_usd,updated_at)
                VALUES(1,?,?,?,?)
                """,
                ("0.12", 0, "0.00", now),
            )
            row = db.execute("SELECT * FROM billing_settings WHERE id=1").fetchone()
        return dict(row or {})

    return _settings_from_row(await gateway_store.call(operation))


async def update_settings(
    *,
    price_multiplier: Any | None = None,
    enforced: bool | None = None,
    new_user_usd: Any | None = None,
    request_budget_usd: Any | None = None,
) -> dict[str, Any]:
    current = await ensure_settings_row()
    budget = money8(current["request_budget_usd"] if request_budget_usd is None else request_budget_usd)
    if not budget.is_finite() or budget <= 0 or budget > MAX_CREDIT_USD:
        raise BillingError(422, "Invalid request budget", code="invalid_budget")
    multiplier = current["price_multiplier"]
    if price_multiplier is not None:
        parsed = money4(price_multiplier)
        if parsed <= 0 or parsed > Decimal("10"):
            raise BillingError(422, "price_multiplier must be between 0 and 10", code="invalid_price_multiplier")
        multiplier = usd_text(parsed, FOURPLACES)
    credit = current["new_user_usd"]
    if new_user_usd is not None:
        parsed_credit = money2(new_user_usd)
        if parsed_credit < 0 or parsed_credit > MAX_CREDIT_USD:
            raise BillingError(422, "new_user_usd is out of range", code="invalid_new_user_usd")
        credit = usd_text(parsed_credit)
    flag = current["enforced"] if enforced is None else bool(enforced)
    now = iso_now()

    def operation(db: Any) -> dict[str, Any]:
        db.execute(
            """
            UPDATE billing_settings
            SET price_multiplier=?, enforced=?, new_user_usd=?, updated_at=?, request_budget_usd=?
            WHERE id=1
            """,
            (multiplier, int(flag), credit, now, usd_text(budget)),
        )
        return dict(db.execute("SELECT * FROM billing_settings WHERE id=1").fetchone() or {})

    return _settings_from_row(await gateway_store.call(operation))


def _catalog_entry_values(item: dict[str, Any], synced_at: str) -> tuple[Any, ...]:
    return (
        str(item["model"]),
        str(item.get("provider") or ""),
        str(item.get("modality") or "text"),
        item.get("official_input_usd_per_1m"),
        item.get("official_output_usd_per_1m"),
        item.get("official_cached_usd_per_1m"),
        item.get("official_reasoning_usd_per_1m"),
        item.get("official_usd_per_image"),
        item.get("official_usd_per_second"),
        synced_at,
    )


async def sync_official_prices() -> dict[str, Any]:
    catalog = load_catalog()
    synced_at = iso_now()
    models = [item for item in catalog.get("models") or [] if isinstance(item, dict) and item.get("model")]

    def operation(db: Any) -> dict[str, int]:
        inserted = 0
        updated = 0
        skipped = 0
        for item in models:
            existing = db.execute(
                "SELECT model, price_source FROM model_official_prices WHERE model=?",
                (str(item["model"]),),
            ).fetchone()
            values = _catalog_entry_values(item, synced_at)
            if existing is None:
                db.execute(
                    """
                    INSERT INTO model_official_prices(
                        model,provider,modality,official_input_usd_per_1m,official_output_usd_per_1m,
                        official_cached_usd_per_1m,official_reasoning_usd_per_1m,official_usd_per_image,
                        official_usd_per_second,official_synced_at,price_source
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,'catalog')
                    """,
                    values,
                )
                inserted += 1
            elif str(existing["price_source"] or "catalog") == "manual":
                skipped += 1
            else:
                db.execute(
                    """
                    UPDATE model_official_prices SET
                        provider=?, modality=?,
                        official_input_usd_per_1m=?, official_output_usd_per_1m=?,
                        official_cached_usd_per_1m=?, official_reasoning_usd_per_1m=?,
                        official_usd_per_image=?, official_usd_per_second=?,
                        official_synced_at=?, price_source='catalog'
                    WHERE model=?
                    """,
                    (*values[1:], values[0]),
                )
                updated += 1
        return {"inserted": inserted, "updated": updated, "skipped_manual": skipped, "total": inserted + updated}

    result = await gateway_store.call(operation)
    result["as_of"] = catalog.get("as_of")
    result["source"] = catalog.get("source")
    return result


async def _prices_ready() -> None:
    await ensure_settings_row()
    row = await gateway_store.one("SELECT COUNT(*) AS n FROM model_official_prices")
    if int((row or {}).get("n") or 0) == 0:
        await sync_official_prices()


def _price_names(model: str) -> list[str]:
    raw = (model or "").strip()
    names: list[str] = []

    def add(value: str) -> None:
        if value and value not in names:
            names.append(value)

    add(raw)
    if raw.endswith("-fast"):
        add(raw[: -len("-fast")])
    if "/" in raw:
        add(raw.split("/", 1)[1])
    else:
        for provider in ("workbuddy", "codex", "grok", "antigravity"):
            add(f"{provider}/{raw}")
    try:
        from app.providers.registry import provider_catalog

        by_id = {item.id: item for item in provider_catalog()}
        for name in list(names):
            item = by_id.get(name)
            if item is not None and item.alias_of:
                add(str(item.alias_of))
    except Exception:
        return names
    return names


async def get_price_row(model: str) -> dict[str, Any] | None:
    await _prices_ready()
    names = _price_names(model)
    found: dict[str, Any] | None = None
    for name in names:
        row = await gateway_store.one("SELECT * FROM model_official_prices WHERE model=?", (name,))
        if not row:
            continue
        item = dict(row)
        if price_is_complete(item):
            return item
        found = found or item
    return found


def _quoted_sell(price_row: dict[str, Any], settings_row: dict[str, Any], **qty: Any) -> Decimal:
    return quote_usage(
        str(price_row.get("model") or ""),
        settings_row=settings_row,
        price_row=price_row,
        **qty,
    )["sell_usd"]


def _effective_multiplier(price_row: dict[str, Any], settings_row: dict[str, Any]) -> Decimal:
    override = dec_or_none(price_row.get("multiplier_override"))
    if override is not None:
        return money4(override)
    return money4(settings_row.get("price_multiplier") or DEFAULT_MULTIPLIER)


def _official_public(price_row: dict[str, Any]) -> dict[str, str]:
    modality = str(price_row.get("modality") or "text")
    official: dict[str, str] = {}
    if modality == "image":
        value = dec_or_none(price_row.get("official_usd_per_image"))
        if value is not None:
            official["usd_per_image"] = usd_text(value, FOURPLACES)
    elif modality == "video":
        value = dec_or_none(price_row.get("official_usd_per_second"))
        if value is not None:
            official["usd_per_second"] = usd_text(value, FOURPLACES)
    else:
        for source, target in (
            ("official_input_usd_per_1m", "input_usd_per_1m"),
            ("official_output_usd_per_1m", "output_usd_per_1m"),
            ("official_cached_usd_per_1m", "cached_usd_per_1m"),
            ("official_reasoning_usd_per_1m", "reasoning_usd_per_1m"),
        ):
            value = dec_or_none(price_row.get(source))
            if value is not None:
                official[target] = usd_text(value, FOURPLACES)
    return official


def _sell_fields(price_row: dict[str, Any], settings_row: dict[str, Any]) -> dict[str, Any]:
    modality = str(price_row.get("modality") or "text")
    sell: dict[str, str] = {}
    if modality == "image":
        sell["usd_per_image"] = usd_text(_quoted_sell(price_row, settings_row, images=1), FOURPLACES)
    elif modality == "video":
        sell["usd_per_second"] = usd_text(_quoted_sell(price_row, settings_row, seconds=1), FOURPLACES)
    else:
        sell["input_usd_per_1m"] = usd_text(
            _quoted_sell(price_row, settings_row, input_tokens=1_000_000), FOURPLACES
        )
        sell["output_usd_per_1m"] = usd_text(
            _quoted_sell(price_row, settings_row, output_tokens=1_000_000), FOURPLACES
        )
        sell["cached_usd_per_1m"] = usd_text(
            _quoted_sell(price_row, settings_row, cached_tokens=1_000_000), FOURPLACES
        )
        if dec_or_none(price_row.get("official_reasoning_usd_per_1m")) is not None:
            sell["reasoning_usd_per_1m"] = usd_text(
                _quoted_sell(price_row, settings_row, reasoning_tokens=1_000_000), FOURPLACES
            )
    payload: dict[str, Any] = {
        "model": str(price_row.get("model") or ""),
        "provider": str(price_row.get("provider") or ""),
        "modality": modality,
        "multiplier": usd_text(_effective_multiplier(price_row, settings_row), FOURPLACES),
        "official": _official_public(price_row),
        "sell": sell,
    }
    payload.update(sell)
    return payload


async def list_public_pricing() -> dict[str, Any]:
    await _prices_ready()
    settings_row = await ensure_settings_row()
    catalog = load_catalog()
    rows = await gateway_store.all(
        "SELECT * FROM model_official_prices WHERE status='active' ORDER BY provider, model"
    )
    return {
        "source": catalog.get("source") or "official",
        "as_of": catalog.get("as_of"),
        "currency": "USD",
        "models": [
            _sell_fields(dict(row), settings_row)
            for row in rows
            if price_is_complete(dict(row))
        ],
    }


def _admin_price_row(row: dict[str, Any], settings_row: dict[str, Any]) -> dict[str, Any]:
    payload = {key: row.get(key) for key in (
        "model", "provider", "modality", *OFFICIAL_COLUMNS[2:], *OVERRIDE_COLUMNS,
        "official_synced_at", "price_source",
    )}
    for key, value in list(payload.items()):
        if hasattr(value, "as_tuple") and hasattr(value, "quantize") and key != "model":
            payload[key] = format(value, "f")
        elif value is not None and key not in {"model", "provider", "modality", "status", "official_synced_at"}:
            payload[key] = str(value)
    sell = _sell_fields(row, settings_row)
    payload["multiplier"] = sell["multiplier"]
    payload["official"] = sell["official"]
    payload["sell"] = sell["sell"]
    payload["priced"] = price_is_complete(row)
    return payload


async def priced_models() -> dict[str, dict[str, Any]]:
    await _prices_ready()
    settings_row = await ensure_settings_row()
    rows = await gateway_store.all(
        "SELECT * FROM model_official_prices WHERE status='active'"
    )
    priced: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        if price_is_complete(item):
            priced[str(item["model"])] = _sell_fields(item, settings_row)
    return priced


async def list_admin_prices() -> dict[str, Any]:
    await _prices_ready()
    settings_row = await ensure_settings_row()
    rows = await gateway_store.all("SELECT * FROM model_official_prices ORDER BY provider, model")
    return {"data": [_admin_price_row(dict(row), settings_row) for row in rows]}


_OFFICIAL_WRITABLE = (
    "official_input_usd_per_1m",
    "official_output_usd_per_1m",
    "official_cached_usd_per_1m",
    "official_usd_per_image",
    "official_usd_per_second",
)


def _money_assignment(key: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    parsed = money4(value)
    if key == "multiplier_override" and (parsed <= 0 or parsed > Decimal("10")):
        raise BillingError(422, "multiplier_override is out of range", code="invalid_price_override")
    if parsed < 0:
        raise BillingError(422, "Price cannot be negative", code="invalid_price_override")
    return usd_text(parsed, FOURPLACES)


async def update_price_overrides(model: str, fields: dict[str, Any]) -> dict[str, Any]:
    await _prices_ready()
    model_id = (model or "").strip()
    if not model_id or len(model_id) > 191:
        raise BillingError(422, "Invalid model", code="invalid_price_override")
    official_in = {key: fields.get(key) for key in _OFFICIAL_WRITABLE if key in fields}
    overrides = {key: fields.get(key) for key in OVERRIDE_COLUMNS if key in fields}
    provider = str(fields.get("provider") or "").strip().lower()
    modality = str(fields.get("modality") or "").strip().lower()
    if not official_in and not overrides and not provider and not modality:
        raise BillingError(422, "No price overrides supplied", code="invalid_price_override")
    if modality and modality not in {"text", "image", "video"}:
        raise BillingError(422, "Invalid modality", code="invalid_price_override")
    if provider and provider not in {"codex", "grok", "antigravity", "workbuddy"}:
        raise BillingError(422, "Unknown provider", code="invalid_price_override")
    if "status" in overrides and overrides["status"] not in {None, "active", "disabled"}:
        raise BillingError(422, "Invalid price status", code="invalid_price_status")

    def operation(db: Any) -> dict[str, Any] | None:
        current = db.execute("SELECT * FROM model_official_prices WHERE model=?", (model_id,)).fetchone()
        merged = dict(current) if current else {"model": model_id, "status": "active", "price_source": "catalog"}
        if provider:
            merged["provider"] = provider
        if modality:
            merged["modality"] = modality
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in {**official_in, **overrides}.items():
            if key == "status":
                merged["status"] = value or "active"
                assignments.append("status=?")
                values.append(merged["status"])
                continue
            stored = _money_assignment(key, value)
            merged[key] = stored
            if stored is None:
                assignments.append(f"{key}=NULL")
            else:
                assignments.append(f"{key}=?")
                values.append(stored)
        if provider:
            assignments.append("provider=?")
            values.append(provider)
        if modality:
            assignments.append("modality=?")
            values.append(modality)
        if official_in:
            if not price_is_complete(merged):
                raise BillingError(422, "A required billing dimension is not priced", code="unpriced_model")
            merged["price_source"] = "manual"
            assignments.append("price_source=?")
            values.append("manual")
        if current is None:
            if not official_in:
                return None
            if not merged.get("provider") or not merged.get("modality"):
                raise BillingError(422, "provider and modality are required", code="invalid_price_override")
            columns = ["model", "provider", "modality", "status", "price_source", "official_synced_at"]
            insert_values: list[Any] = [
                model_id, merged["provider"], merged["modality"], merged.get("status") or "active",
                "manual", iso_now(),
            ]
            for key in (*_OFFICIAL_WRITABLE, *OVERRIDE_COLUMNS):
                if key == "status" or merged.get(key) in (None, ""):
                    continue
                columns.append(key)
                insert_values.append(merged[key])
            placeholders = ",".join("?" for _ in columns)
            db.execute(
                f"INSERT INTO model_official_prices({','.join(columns)}) VALUES({placeholders})",
                tuple(insert_values),
            )
        else:
            if not assignments:
                return dict(current)
            values.append(model_id)
            db.execute(
                f"UPDATE model_official_prices SET {', '.join(assignments)} WHERE model=?",
                tuple(values),
            )
        return dict(db.execute("SELECT * FROM model_official_prices WHERE model=?", (model_id,)).fetchone() or {})

    row = await gateway_store.call(operation)
    if row is None:
        raise BillingError(404, "Unknown model", code="unknown_model")
    return _admin_price_row(row, await ensure_settings_row())


def _row_lock() -> str:
    return " FOR UPDATE" if gateway_store.engine == "mysql" else ""


def _find_key_for_user(db: Any, user_id: str) -> dict[str, Any] | None:
    return db.execute(
        "SELECT * FROM api_keys WHERE owner_user_id=? AND status!='deleted' ORDER BY created_at LIMIT 1"
        + _row_lock(),
        (user_id,),
    ).fetchone()


def _find_key(db: Any, key_id: str) -> dict[str, Any] | None:
    return db.execute(
        "SELECT * FROM api_keys WHERE id=? AND status!='deleted'" + _row_lock(),
        (key_id,),
    ).fetchone()


def _credit_key_balance(
    db: Any,
    key: dict[str, Any],
    amount: Decimal,
    *,
    kind: str,
    actor: str,
    reason: str,
    request_id: str | None,
    card_id: str | None,
    official: str | None = None,
    multiplier: str | None = None,
    now: str | None = None,
) -> Decimal:
    balance = money8(money8(key["usd_credit"]) + amount)
    db.execute("UPDATE api_keys SET usd_credit=? WHERE id=?", (usd_text(balance), key["id"]))
    owner = key.get("owner_user_id") or None
    db.execute(
        """
        INSERT INTO wallet_ledger(
            id,user_id,key_id,amount_usd,balance_after,kind,request_id,card_id,actor,reason,
            official_usd,multiplier,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            uuid.uuid4().hex,
            owner,
            key["id"],
            usd_text(amount),
            usd_text(balance),
            kind,
            request_id,
            card_id,
            actor[:191],
            (reason or "")[:500],
            official,
            multiplier,
            now or iso_now(),
        ),
    )
    return balance


def _find_user(db: Any, *, user_id: str | None, email: str | None) -> dict[str, Any] | None:
    lock = " FOR UPDATE" if gateway_store.engine == "mysql" else ""
    if user_id:
        return db.execute("SELECT id,email,name,usd_credit FROM portal_users WHERE id=?" + lock, (user_id,)).fetchone()
    if email:
        return db.execute(
            "SELECT id,email,name,usd_credit FROM portal_users WHERE email=?" + lock,
            (email.strip().lower(),),
        ).fetchone()
    return None


async def grant_credit(
    *,
    user_id: str | None = None,
    email: str | None = None,
    amount: Any,
    reason: str = "",
    actor: str = "admin",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    credit = money2(amount)
    if credit <= 0 or credit > MAX_CREDIT_USD:
        raise BillingError(422, "金额必须大于 0，且不超过 10000 美元", code="invalid_credit_amount")
    reason_text = (reason or "").strip()[:500]
    actor_text = (actor or "admin")[:191]
    key = (idempotency_key or "").strip()[:191] or None

    def operation(db: Any) -> dict[str, Any]:
        if key:
            existing = db.execute(
                "SELECT * FROM wallet_ledger WHERE request_id=? AND kind='grant'",
                (key,),
            ).fetchone()
            if existing:
                user = db.execute(
                    "SELECT id,email,usd_credit FROM portal_users WHERE id=?",
                    (existing["user_id"],),
                ).fetchone()
                return {
                    "ok": True,
                    "idempotent": True,
                    "user_id": existing["user_id"],
                    "email": (user or {}).get("email"),
                    "amount_usd": usd_text(existing["amount_usd"]),
                    "balance_after": usd_text(existing["balance_after"]),
                }
        user = _find_user(db, user_id=user_id, email=email)
        if user is None:
            raise BillingError(404, "User not found", code="user_not_found")
        owned = _find_key_for_user(db, user["id"])
        if owned is None:
            raise BillingError(404, "API key not found", code="key_not_found")
        balance = _credit_key_balance(
            db, owned, credit, kind="grant", actor=actor_text, reason=reason_text,
            request_id=key, card_id=None,
        )
        return {
            "ok": True,
            "idempotent": False,
            "user_id": user["id"],
            "email": user["email"],
            "key_id": owned["id"],
            "amount_usd": usd_text(credit),
            "balance_after": usd_text(balance),
        }

    return await gateway_store.call(operation)


async def grant_key_credit(
    *,
    key_id: str,
    amount: Any,
    reason: str = "",
    actor: str = "admin",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    credit = money2(amount)
    if credit <= 0 or credit > MAX_CREDIT_USD:
        raise BillingError(422, "金额必须大于 0，且不超过 10000 美元", code="invalid_credit_amount")
    reason_text = (reason or "").strip()[:500]
    actor_text = (actor or "admin")[:191]
    marker = (idempotency_key or "").strip()[:191] or None

    def operation(db: Any) -> dict[str, Any]:
        if marker:
            existing = db.execute(
                "SELECT * FROM wallet_ledger WHERE request_id=? AND kind='grant'",
                (marker,),
            ).fetchone()
            if existing:
                return {
                    "ok": True,
                    "idempotent": True,
                    "key_id": existing.get("key_id") or key_id,
                    "user_id": existing.get("user_id"),
                    "amount_usd": usd_text(existing["amount_usd"]),
                    "balance_after": usd_text(existing["balance_after"]),
                }
        owned = _find_key(db, key_id)
        if owned is None:
            raise BillingError(404, "API key not found", code="api_key_not_found")
        balance = _credit_key_balance(
            db, owned, credit, kind="grant", actor=actor_text, reason=reason_text,
            request_id=marker, card_id=None,
        )
        return {
            "ok": True,
            "idempotent": False,
            "key_id": owned["id"],
            "user_id": owned.get("owner_user_id"),
            "amount_usd": usd_text(credit),
            "balance_after": usd_text(balance),
        }

    return await gateway_store.call(operation)


async def charge_usage(
    user_id: str,
    request_id: str,
    quote: dict[str, Any],
    *,
    actor: str = "system",
    reason: str = "usage",
) -> dict[str, Any]:
    cost = money8(quote.get("sell_usd") or 0)
    official = money4(quote.get("official_usd") or 0)
    multiplier = money4(quote.get("multiplier") or DEFAULT_MULTIPLIER)
    if cost <= 0:
        return {"ok": True, "charged": False, "reason": "zero_cost", "balance_after": None}

    def operation(db: Any) -> dict[str, Any]:
        if request_id:
            existing = db.execute(
                "SELECT * FROM wallet_ledger WHERE kind='usage' AND request_id=?",
                (request_id,),
            ).fetchone()
            if existing:
                return {
                    "ok": True,
                    "charged": True,
                    "idempotent": True,
                    "balance_after": usd_text(existing["balance_after"]),
                    "amount_usd": usd_text(existing["amount_usd"]),
                }
        owned = _find_key_for_user(db, user_id) or _find_key(db, user_id)
        if owned is None:
            return {"ok": False, "charged": False, "reason": "user_not_found"}
        current = money8(owned["usd_credit"])
        from app.wallet_engine import _held
        if current - _held(db, str(owned["id"])) < cost:
            return {
                "ok": False,
                "charged": False,
                "reason": "insufficient_usd_credit",
                "balance_after": usd_text(current),
            }
        balance = _credit_key_balance(
            db, owned, -cost, kind="usage", actor=actor, reason=reason or "usage",
            request_id=request_id, card_id=None,
            official=usd_text(official, FOURPLACES), multiplier=usd_text(multiplier, FOURPLACES),
        )
        db.execute(
            "UPDATE call_records SET usd_charged=? WHERE request_id=?",
            (usd_text(cost), request_id),
        )
        return {
            "ok": True,
            "charged": True,
            "idempotent": False,
            "balance_after": usd_text(balance),
            "amount_usd": usd_text(cost),
        }

    return await gateway_store.call(operation)


def _usage_quantities(usage: dict[str, Any] | None, endpoint: str, price_row: dict[str, Any] | None) -> dict[str, Any]:
    blob = usage if isinstance(usage, dict) else {}
    input_tokens = int(blob.get("input_tokens") or blob.get("prompt_tokens") or 0)
    output_tokens = int(blob.get("output_tokens") or blob.get("completion_tokens") or 0)
    input_details = blob.get("input_tokens_details") or blob.get("prompt_tokens_details") or {}
    output_details = blob.get("output_tokens_details") or blob.get("completion_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0) if isinstance(input_details, dict) else 0
    cached_tokens = min(max(0, cached_tokens), max(0, input_tokens))
    reasoning_tokens = (
        int(output_details.get("reasoning_tokens") or 0) if isinstance(output_details, dict) else 0
    )
    images = int(blob.get("images") or blob.get("n") or 0)
    try:
        seconds = float(blob.get("seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    modality = str((price_row or {}).get("modality") or "")
    lowered = (endpoint or "").lower()
    if not modality:
        if "/images" in lowered or "generate_image" in lowered or "edit_image" in lowered:
            modality = "image"
        elif "/videos" in lowered:
            modality = "video"
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "images": images,
        "seconds": seconds,
    }


async def charge_finished_call(
    key_id: str,
    request_id: str,
    model: str,
    endpoint: str,
    usage: dict[str, Any] | None,
) -> dict[str, Any] | None:
    key = await gateway_store.one("SELECT owner_user_id FROM api_keys WHERE id=?", (key_id,))
    owner_id = str((key or {}).get("owner_user_id") or "")
    if not owner_id:
        return None
    settings_row = await ensure_settings_row()
    price_row = await get_price_row(model)
    quantities = _usage_quantities(usage, endpoint, price_row)
    quote = quote_usage(model, settings_row=settings_row, price_row=price_row, **quantities)
    if quote["unpriced"] or money8(quote["sell_usd"]) <= 0:
        return {"ok": True, "charged": False, "reason": "unpriced" if quote["unpriced"] else "zero_cost"}
    result = await charge_usage(owner_id, request_id, quote)
    if not result.get("ok"):
        logger.warning(
            "billing_charge_failed request_id=%s code=%s",
            request_id,
            result.get("reason") or "charge_failed",
        )
    return result


async def reject_if_enforced_empty(
    owner_user_id: str, *, endpoint: str, model: str = ""
) -> None:
    if not owner_user_id or not is_generation_endpoint(endpoint):
        return
    settings_row = await ensure_settings_row()
    if not settings_row["enforced"]:
        return
    row = await gateway_store.one("SELECT usd_credit FROM portal_users WHERE id=?", (owner_user_id,))
    if row is None:
        return
    balance = money2(row.get("usd_credit"))
    if balance <= 0:
        raise BillingError(402, "Insufficient USD credit", code="insufficient_usd_credit")
    price_row = await get_price_row(model) if model else None
    lowered = (endpoint or "").lower()
    images = 0
    seconds = 0.0
    if "/images" in lowered or "generate_image" in lowered or "edit_image" in lowered:
        images = 1
    elif "/videos" in lowered or "/remix" in lowered:
        seconds = 1
    if images or seconds:
        quote = quote_usage(
            model or "",
            images=images,
            seconds=seconds,
            settings_row=settings_row,
            price_row=price_row,
        )
        if money2(quote["sell_usd"]) > balance:
            raise BillingError(402, "Insufficient USD credit", code="insufficient_usd_credit")


def hash_card_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_card_code() -> str:
    parts = ["".join(secrets.choice(CARD_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "MK-" + "-".join(parts)


def normalize_card_code(code: str) -> str:
    return re.sub(r"\s+", "", (code or "")).upper()


def card_prefix(code: str) -> str:
    pieces = normalize_card_code(code).split("-")
    if len(pieces) >= 2:
        return f"{pieces[0]}-{pieces[1]}"
    return pieces[0] if pieces else "MK"


async def create_card_batch(
    *,
    amount_usd: Any,
    count: int,
    expires_at: str | None = None,
    created_by: str = "admin",
    note: str | None = None,
) -> dict[str, Any]:
    amount = money2(amount_usd)
    if amount <= 0 or amount > MAX_CREDIT_USD:
        raise BillingError(422, "amount_usd must be greater than 0 and at most 10000", code="invalid_card_amount")
    safe_count = int(count)
    if safe_count < 1 or safe_count > 100:
        raise BillingError(422, "count must be between 1 and 100", code="invalid_card_count")
    expiry = (expires_at or "").strip() or None
    note_text = (note or "").strip()[:200] or None
    batch_id = uuid.uuid4().hex
    now = iso_now()
    created = []

    def operation(db: Any) -> list[dict[str, str]]:
        rows = []
        for _ in range(safe_count):
            for _attempt in range(8):
                code = generate_card_code()
                digest = hash_card_code(code)
                exists = db.execute("SELECT id FROM card_keys WHERE code_hash=?", (digest,)).fetchone()
                if exists:
                    continue
                card_id = uuid.uuid4().hex
                db.execute(
                    """
                    INSERT INTO card_keys(
                        id,code_hash,code_prefix,amount_usd,status,expires_at,created_by,created_at,batch_id,note
                    ) VALUES(?,?,?,?,'unused',?,?,?,?,?)
                    """,
                    (card_id, digest, card_prefix(code), usd_text(amount), expiry, created_by[:64], now, batch_id, note_text),
                )
                rows.append({"id": card_id, "code": code, "prefix": card_prefix(code)})
                break
            else:
                raise BillingError(500, "Could not allocate a unique card code", code="card_generate_failed")
        return rows

    created = await gateway_store.call(operation)
    return {
        "batch_id": batch_id,
        "amount_usd": usd_text(amount),
        "count": len(created),
        "expires_at": expiry,
        "note": note_text,
        "codes": [item["code"] for item in created],
        "data": [{"id": item["id"], "prefix": item["prefix"]} for item in created],
    }


def _card_public(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "unused")
    expires_at = row.get("expires_at")
    if status == "unused" and expires_at and str(expires_at) <= iso_now():
        status = "expired"
    return {
        "id": row["id"],
        "prefix": row["code_prefix"],
        "amount_usd": usd_text(row["amount_usd"]),
        "status": status,
        "expires_at": expires_at,
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "redeemed_by": row.get("redeemed_by"),
        "redeemed_at": row.get("redeemed_at"),
        "batch_id": row.get("batch_id"),
        "note": row.get("note") or "",
    }


async def list_cards(
    *,
    status: str | None = None,
    batch_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    safe_page = max(1, int(page))
    safe_size = max(1, min(int(page_size), 100))
    clauses = ["1=1"]
    values: list[Any] = []
    if status:
        clauses.append("status=?")
        values.append(status)
    if batch_id:
        clauses.append("batch_id=?")
        values.append(batch_id)
    where = " AND ".join(clauses)
    total_row = await gateway_store.one(f"SELECT COUNT(*) AS n FROM card_keys WHERE {where}", tuple(values))
    rows = await gateway_store.all(
        f"""
        SELECT id,code_prefix,amount_usd,status,expires_at,created_by,created_at,
               redeemed_by,redeemed_at,batch_id,note
        FROM card_keys WHERE {where}
        ORDER BY created_at DESC LIMIT ? OFFSET ?
        """,
        (*values, safe_size, (safe_page - 1) * safe_size),
    )
    return {
        "data": [_card_public(dict(row)) for row in rows],
        "page": safe_page,
        "page_size": safe_size,
        "total": int((total_row or {}).get("n") or 0),
    }


async def disable_card(card_id: str) -> dict[str, Any]:
    def operation(db: Any) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM card_keys WHERE id=?", (card_id,)).fetchone()
        if row is None:
            return None
        status = str(row["status"] or "")
        if status == "redeemed":
            raise BillingError(409, "Redeemed cards cannot be disabled", code="card_already_redeemed")
        if status != "disabled":
            db.execute("UPDATE card_keys SET status='disabled' WHERE id=? AND status!='redeemed'", (card_id,))
            row = db.execute("SELECT * FROM card_keys WHERE id=?", (card_id,)).fetchone()
        return dict(row or {})

    row = await gateway_store.call(operation)
    if row is None:
        raise BillingError(404, "Card not found", code="card_not_found")
    return _card_public(row)


REDEEM_DAILY_LIMIT = 10
REDEEM_IP_DAILY_LIMIT = 40


def _shanghai_day_start_key() -> str:
    """Lexicographic lower bound for timestamps on the current Asia/Shanghai day."""
    start = dt.datetime.now(SHANGHAI_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")


async def redeem_card(user_id: str, code: str, *, ip_hash: str) -> dict[str, Any]:
    normalized = normalize_card_code(code)
    now = iso_now()
    day_start = _shanghai_day_start_key()

    def reserve_attempt(db: Any) -> None:
        user_attempts = db.execute(
            "SELECT COUNT(*) AS n FROM card_redeem_attempts WHERE user_id=? AND created_at>=?",
            (user_id, day_start),
        ).fetchone()["n"]
        ip_attempts = db.execute(
            "SELECT COUNT(*) AS n FROM card_redeem_attempts WHERE ip_hash=? AND created_at>=?",
            (ip_hash, day_start),
        ).fetchone()["n"]
        if int(user_attempts) >= REDEEM_DAILY_LIMIT:
            raise BillingError(
                429,
                "今天的兑换次数已用完，每个账号每天最多 10 次，成功和失败都计算在内",
                code="redeem_rate_limited",
            )
        if int(ip_attempts) >= REDEEM_IP_DAILY_LIMIT:
            raise BillingError(429, "今天这个网络的兑换次数过多，请明天再试", code="redeem_rate_limited")
        db.execute(
            "INSERT INTO card_redeem_attempts(id,user_id,ip_hash,created_at) VALUES(?,?,?,?)",
            (uuid.uuid4().hex, user_id, ip_hash, now),
        )

    await gateway_store.call(reserve_attempt)

    def operation(db: Any) -> dict[str, Any]:
        if not CARD_CODE_RE.match(normalized):
            raise BillingError(404, "Invalid card", code="invalid_card")
        digest = hash_card_code(normalized)
        card = db.execute("SELECT * FROM card_keys WHERE code_hash=?" + (" FOR UPDATE" if gateway_store.engine == "mysql" else ""), (digest,)).fetchone()
        if card is None:
            raise BillingError(404, "Invalid card", code="invalid_card")
        status = str(card["status"] or "")
        if status == "redeemed":
            raise BillingError(409, "Card already redeemed", code="card_already_redeemed")
        if status == "disabled":
            raise BillingError(409, "Card is disabled", code="card_disabled")
        expires_at = card["expires_at"]
        if expires_at and str(expires_at) <= now:
            db.execute("UPDATE card_keys SET status='expired' WHERE id=? AND status='unused'", (card["id"],))
            raise BillingError(409, "Card has expired", code="card_expired")
        if status != "unused":
            raise BillingError(409, "Card is not redeemable", code="card_not_redeemable")
        amount = money2(card["amount_usd"])
        user = db.execute("SELECT id FROM portal_users WHERE id=?" + _row_lock(), (user_id,)).fetchone()
        if user is None:
            raise BillingError(404, "User not found", code="user_not_found")
        owned = _find_key_for_user(db, user_id)
        if owned is None:
            raise BillingError(404, "API key not found", code="key_not_found")
        updated = db.execute(
            "UPDATE card_keys SET status='redeemed', redeemed_by=?, redeemed_at=? WHERE id=? AND status='unused'",
            (user_id, now, card["id"]),
        )
        if int(getattr(updated, "rowcount", 1) or 0) == 0:
            raise BillingError(409, "Card already redeemed", code="card_already_redeemed")
        balance = _credit_key_balance(
            db, owned, amount, kind="redeem", actor=f"user:{user_id}", reason="card_redeem",
            request_id=None, card_id=str(card["id"]), now=now,
        )
        db.execute(
            "INSERT INTO card_redemptions(id,card_id,user_id,amount_usd,created_at) VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, card["id"], user_id, usd_text(amount), now),
        )
        return {
            "ok": True,
            "amount_usd": usd_text(amount),
            "balance_after": usd_text(balance),
            "prefix": card["code_prefix"],
        }

    return await gateway_store.call(operation)


def canonical_credit_string(
    timestamp: str,
    nonce: str,
    user: str,
    amount: Any,
    idempotency: str,
) -> str:
    return f"{timestamp}.{nonce}.{user}.{usd_text(amount)}.{idempotency}"


def sign_credit_request(
    *,
    timestamp: str,
    nonce: str,
    user: str,
    amount: Any,
    idempotency: str,
    secret: str,
) -> str:
    payload = canonical_credit_string(timestamp, nonce, user, amount, idempotency)
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _parse_timestamp(value: str | None) -> int:
    if not value or not re.fullmatch(r"-?\d+", value.strip()):
        raise BillingError(401, "Invalid signature", code="invalid_wallet_signature")
    return int(value.strip())


async def credit_from_hmac(
    *,
    timestamp: str | None,
    nonce: str | None,
    idempotency_key: str | None,
    signature: str | None,
    user_id: str | None,
    email: str | None,
    amount: Any,
    reason: str,
) -> dict[str, Any]:
    secret = (settings.wallet_credit_secret or "").strip()
    if not secret:
        raise BillingError(401, "Invalid signature", code="invalid_wallet_signature")
    if not timestamp or not nonce or not idempotency_key or not signature:
        raise BillingError(401, "Invalid signature", code="invalid_wallet_signature")
    user_key = (user_id or "").strip() or (email or "").strip().lower()
    if not user_key:
        raise BillingError(422, "email or user_id is required", code="invalid_credit_target")
    parsed_ts = _parse_timestamp(timestamp)
    if abs(int(time.time()) - parsed_ts) > HMAC_MAX_SKEW_SECONDS:
        raise BillingError(401, "Invalid signature", code="invalid_wallet_signature")
    expected = sign_credit_request(
        timestamp=str(parsed_ts),
        nonce=nonce,
        user=user_key,
        amount=amount,
        idempotency=idempotency_key,
        secret=secret,
    )
    supplied = signature.strip().lower()
    if len(supplied) != len(expected) or not hmac.compare_digest(expected, supplied):
        raise BillingError(401, "Invalid signature", code="invalid_wallet_signature")
    credit = money2(amount)
    if credit <= 0 or credit > MAX_CREDIT_USD:
        raise BillingError(422, "amount must be greater than 0 and at most 10000", code="invalid_credit_amount")

    def operation(db: Any) -> dict[str, Any]:
        existing_nonce = db.execute("SELECT nonce FROM billing_nonces WHERE nonce=?", (nonce,)).fetchone()
        if existing_nonce:
            raise BillingError(401, "Invalid signature", code="invalid_wallet_signature")
        db.execute(
            "INSERT INTO billing_nonces(nonce,created_at) VALUES(?,?)",
            (nonce, iso_now()),
        )
        prune_before = (utc_now() - dt.timedelta(seconds=HMAC_MAX_SKEW_SECONDS * 2)).isoformat().replace("+00:00", "Z")
        db.execute("DELETE FROM billing_nonces WHERE created_at<? AND nonce<>?", (prune_before, nonce))
        if idempotency_key:
            existing = db.execute(
                "SELECT * FROM wallet_ledger WHERE request_id=? AND kind='grant'",
                (idempotency_key,),
            ).fetchone()
            if existing:
                user = db.execute(
                    "SELECT id,email,usd_credit FROM portal_users WHERE id=?",
                    (existing["user_id"],),
                ).fetchone()
                return {
                    "ok": True,
                    "idempotent": True,
                    "user_id": existing["user_id"],
                    "email": (user or {}).get("email"),
                    "amount_usd": usd_text(existing["amount_usd"]),
                    "balance_after": usd_text(existing["balance_after"]),
                }
        user = _find_user(db, user_id=user_id or None, email=None if user_id else email)
        if user is None:
            raise BillingError(404, "User not found", code="user_not_found")
        owned = _find_key_for_user(db, user["id"])
        if owned is None:
            raise BillingError(404, "API key not found", code="key_not_found")
        balance = _credit_key_balance(
            db, owned, credit, kind="grant", actor="internal", reason=reason or "",
            request_id=idempotency_key, card_id=None,
        )
        return {
            "ok": True,
            "idempotent": False,
            "user_id": user["id"],
            "email": user["email"],
            "amount_usd": usd_text(credit),
            "balance_after": usd_text(balance),
        }

    return await gateway_store.call(operation)


async def list_ledger(
    *,
    user_id: str | None = None,
    email: str | None = None,
    kind: str | None = None,
    start: str | None = None,
    end: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    safe_page = max(1, int(page))
    safe_size = max(1, min(int(page_size), 100))
    clauses = ["1=1"]
    values: list[Any] = []
    if user_id:
        clauses.append("l.user_id=?")
        values.append(user_id)
    elif email:
        clauses.append("u.email=?")
        values.append(email.strip().lower())
    if kind:
        clauses.append("l.kind=?")
        values.append(kind)
    if start:
        clauses.append("l.created_at>=?")
        values.append(start)
    if end:
        clauses.append("l.created_at<?")
        values.append(end)
    where = " AND ".join(clauses)
    total_row = await gateway_store.one(
        f"""
        SELECT COUNT(*) AS n FROM wallet_ledger l
        LEFT JOIN portal_users u ON u.id=l.user_id
        LEFT JOIN api_keys k ON k.id=l.key_id
        WHERE {where}
        """,
        tuple(values),
    )
    rows = await gateway_store.all(
        f"""
        SELECT l.id,l.user_id,l.key_id,u.email,k.name AS key_name,l.amount_usd,l.balance_after,l.kind,l.request_id,
               l.card_id,l.actor,l.reason,l.official_usd,l.multiplier,l.created_at
        FROM wallet_ledger l
        LEFT JOIN portal_users u ON u.id=l.user_id
        LEFT JOIN api_keys k ON k.id=l.key_id
        WHERE {where}
        ORDER BY l.created_at DESC LIMIT ? OFFSET ?
        """,
        (*values, safe_size, (safe_page - 1) * safe_size),
    )
    data = []
    for row in rows:
        item = dict(row)
        item["amount_usd"] = usd_text(item.get("amount_usd"))
        item["balance_after"] = usd_text(item.get("balance_after"))
        item["amount"] = item["amount_usd"]
        item["balance"] = item["balance_after"]
        item["type"] = item.get("kind")
        item["note"] = item.get("reason") or ""
        if item.get("official_usd") is not None:
            item["official_usd"] = usd_text(item.get("official_usd"), FOURPLACES)
        if item.get("multiplier") is not None:
            item["multiplier"] = usd_text(item.get("multiplier"), FOURPLACES)
        data.append(item)
    return {
        "data": data,
        "page": safe_page,
        "page_size": safe_size,
        "total": int((total_row or {}).get("n") or 0),
    }


async def user_wallet(user_id: str) -> dict[str, Any]:
    from app.wallet_engine import wallet_summary
    return await wallet_summary(user_id)
