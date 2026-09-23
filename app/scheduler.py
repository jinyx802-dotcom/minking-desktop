"""Quota-aware dynamic routing with opaque continuation bindings."""
from __future__ import annotations
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import settings
from app.store.gateway import gateway_store, iso_now, utc_now

logger = logging.getLogger(__name__)


def expiry(minutes=5):
    return (utc_now()+dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def binding(gateway, key_id, provider, value):
    return hmac.new(gateway.secret, f"{key_id}:{provider}:{value}".encode(), hashlib.sha256).hexdigest()


def quota_state(gateway, account_id, model=""):
    cache = gateway._quota_cache.get(account_id)
    stopped = getattr(gateway, "_quota_stopped", set())
    gateway._quota_stopped = stopped
    if not cache or time.monotonic()-cache[0] > settings.gateway_quota_cache_seconds:
        return None, False, "额度未知或过期 · 按负载调度"
    windows = []
    for limit in cache[1].get("limits", []):
        if model and str(limit.get("limit_id", "")).startswith("antigravity-"):
            from app.providers.antigravity import _quota_family
            family = _quota_family(model)
            if family and limit.get("limit_name") != family:
                continue
        limit_model = limit.get("model")
        if limit_model and model and limit_model != model:
            continue
        windows.extend(w for w in (limit.get("primary"),limit.get("secondary")) if w and w.get("remaining_percent") is not None)
    if not windows:
        return None, False, "无可用额度窗口 · 按负载调度"
    remaining = min(float(w["remaining_percent"]) for w in windows)
    marker = (account_id,model)
    if remaining <= 10:
        stopped.add(marker)
    elif remaining >= 15:
        stopped.discard(marker)
    paused = marker in stopped
    return remaining, paused, "低额度 · 停止新会话" if paused else "动态均衡"


async def select(gateway, key, *, provider, model, context=None, previous=None, conversation=None, excluded=None, kind="chat"):
    from app.codex_gateway import GatewayError
    excluded = excluded or set()
    accounts = await gateway_store.healthy_accounts(provider)
    now = iso_now()
    cooled = {r["account_id"] for r in await gateway_store.all(
        "SELECT account_id FROM account_model_health WHERE model=? AND cooldown_until>?", (model,now))}
    candidates = [a for a in accounts if a["account_id"] not in excluded|cooled]
    from app.providers.base import parse_capabilities
    required = {"image","image_edit"} if kind.startswith("image") else {"video"} if kind == "video" else {"chat","responses"}
    candidates = [a for a in candidates if not parse_capabilities(a.get("capabilities")) or required.intersection(parse_capabilities(a.get("capabilities")))]
    if not candidates:
        raise GatewayError(503,"No healthy account is available",code="no_healthy_accounts")
    quotas = {a["account_id"]:quota_state(gateway,a["account_id"],model) for a in candidates}
    lease_id = context.request_id if context else uuid.uuid4().hex
    def operation(db):
        # Serialize pool selection across worker processes before reading occupancy.
        if gateway_store.engine == "mysql":
            db.execute("SELECT account_id FROM accounts WHERE provider=? ORDER BY account_id FOR UPDATE", (provider,)).fetchall()
        db.execute("DELETE FROM routing_leases WHERE expires_at<=?", (now,))
        route = db.execute("SELECT * FROM api_key_routes WHERE key_id=? AND provider=?", (key["id"],provider)).fetchone() or {}
        pinned = None
        reason = "dynamic"
        token = previous or conversation
        digest = binding(gateway,key["id"],provider,token) if token else None
        if digest:
            record = db.execute("SELECT account_id FROM routing_bindings WHERE binding_hash=? AND expires_at>?", (digest,now)).fetchone()
            if record:
                pinned = record["account_id"]
                reason = "continuation"
            elif previous:
                legacy = db.execute("SELECT account_id FROM routing_legacy WHERE key_id=? AND provider=? AND expires_at>?", (key["id"],provider,now)).fetchone()
                pinned = (legacy or {}).get("account_id")
                reason = "legacy_continuation"
                if not pinned:
                    raise GatewayError(409,"Continuation unavailable; send complete history",code="continuation_unavailable")
        if not pinned and route.get("preferred_account_id"):
            pinned = route["preferred_account_id"]
            reason = "manual"
        continuation = reason in {"continuation", "legacy_continuation"}
        cutoff = (utc_now()-dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        loads = {r["account_id"]:float(r["work"] or 0) for r in db.execute(
            "SELECT account_id,SUM(CASE WHEN total_tokens>0 THEN total_tokens ELSE 1000 END) AS work "
            "FROM call_records WHERE started_at>=? AND status<>'in_progress' GROUP BY account_id", (cutoff,)).fetchall()}
        live = {r["account_id"]:r for r in db.execute(
            "SELECT account_id,SUM(work_units) AS work,COUNT(*) AS n FROM routing_leases GROUP BY account_id").fetchall()}
        last = {r["account_id"]:r["last_used"] for r in db.execute(
            "SELECT account_id,MAX(created_at) AS last_used FROM routing_events WHERE provider=? GROUP BY account_id", (provider,)).fetchall()}
        fresh = {r['account_id'] for r in db.execute("SELECT account_id FROM accounts WHERE status='active' AND (cooldown_until IS NULL OR cooldown_until<=?)", (now,)).fetchall()}

        def open_seats(rows):
            return [a for a in rows if a["account_id"] in fresh and int(live.get(a["account_id"], {}).get("n", 0)) < settings.gateway_account_max_concurrency]

        def dynamic_pool():
            rows = [a for a in candidates if not quotas[a["account_id"]][1]]
            next_reason = reason
            if not rows:
                if len(candidates) == 1 and quotas[candidates[0]["account_id"]][0] != 0:
                    rows = candidates
                    if next_reason not in {"failover", "manual"}:
                        next_reason = "only_account_low_quota"
                else:
                    raise GatewayError(503,"Accounts are conserving quota; retry after reset",code="quota_pool_paused")
            rows = open_seats(rows)
            if not rows:
                raise GatewayError(503,"Account pool is busy or unavailable",code="account_pool_busy")
            return rows, next_reason

        if continuation:
            pool = [a for a in candidates if a["account_id"] == pinned and quotas[a["account_id"]][0] != 0]
            if not pool:
                raise GatewayError(409 if token else 503,"Pinned account unavailable",code="continuation_unavailable" if token else "no_healthy_accounts")
            pool = open_seats(pool)
            if not pool:
                raise GatewayError(503,"Account pool is busy or unavailable",code="account_pool_busy")
        elif reason == "manual":
            preferred = open_seats([
                a for a in candidates if a["account_id"] == pinned and quotas[a["account_id"]][0] != 0
            ])
            if preferred:
                pool = preferred
            else:
                reason = "failover"
                pool, reason = dynamic_pool()
                if reason != "only_account_low_quota":
                    reason = "failover"
        else:
            pool, reason = dynamic_pool()
        known_weights = [q[0]/100 for q in quotas.values() if q[0] is not None and q[0]>0]
        neutral_weight = sum(known_weights)/len(known_weights) if known_weights else 1
        def score(a):
            aid = a["account_id"]
            remaining = quotas[aid][0]
            weight = max(.01,remaining/100) if remaining is not None else neutral_weight
            return ((loads.get(aid,0)+float(live.get(aid,{}).get("work",0)))/weight,
                    max(int(live.get(aid,{}).get("n",0)),gateway.current_concurrency(aid)), last.get(aid,""), aid)
        chosen = min(pool,key=score)
        aid = chosen["account_id"]
        expected = db.execute("SELECT AVG(total_tokens) AS units FROM call_records WHERE model=? AND started_at>=? AND total_tokens>0", (model,cutoff)).fetchone()
        units = max(1,float(expected['units'] or 1000))
        db.execute("DELETE FROM routing_leases WHERE lease_id=?", (lease_id,))
        db.execute("INSERT INTO routing_leases(lease_id,account_id,provider,model,work_units,expires_at) VALUES(?,?,?,?,?,?)",
                   (lease_id,aid,provider,model,units,expiry()))
        db.execute("INSERT INTO routing_events(id,key_id,account_id,provider,reason,created_at) VALUES(?,?,?,?,?,?)",
                   (uuid.uuid4().hex,key["id"],aid,provider,reason,now))
        db.execute("""INSERT INTO api_key_routes(key_id,provider,active_account_id,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(key_id,provider) DO UPDATE SET active_account_id=excluded.active_account_id,updated_at=excluded.updated_at""",
            (key["id"],provider,aid,now))
        if conversation:
            db.execute("""INSERT INTO routing_bindings(binding_hash,account_id,expires_at) VALUES(?,?,?)
                ON CONFLICT(binding_hash) DO UPDATE SET expires_at=excluded.expires_at""",
                (binding(gateway,key["id"],provider,conversation),aid,expiry(7*24*60)))
        return chosen, bool(token)
    selected, bound = await gateway_store.call(operation)
    if context:
        context.route_bound = bound
        context.account_id = selected["account_id"]
        track_route(gateway, lease_id)
    return selected


def track_route(gateway, lease_id: str) -> None:
    gateway._active_routing[lease_id] = asyncio.current_task()


def untrack_route(gateway, lease_id: str) -> None:
    gateway._active_routing.pop(lease_id, None)


async def complete_route(gateway, context, completed):
    await gateway_store.execute("DELETE FROM routing_leases WHERE lease_id=?", (context.request_id,))
    untrack_route(gateway, context.request_id)
    response_id = (completed or {}).get("id")
    if response_id and context.account_id:
        await gateway_store.execute("""INSERT INTO routing_bindings(binding_hash,account_id,expires_at) VALUES(?,?,?)
            ON CONFLICT(binding_hash) DO UPDATE SET account_id=excluded.account_id,expires_at=excluded.expires_at""",
            (binding(gateway,context.key_id,context.provider,response_id),context.account_id,expiry(7*24*60)))


async def occupy_attempt(gateway, context, account):
    """Move the same reservation to a retry account before dispatch, never double occupy."""
    from app.codex_gateway import GatewayError
    aid = account['account_id']
    def operation(db):
        if gateway_store.engine == 'mysql':
            db.execute("SELECT account_id FROM accounts WHERE provider=? ORDER BY account_id FOR UPDATE", (context.provider,)).fetchall()
        current = db.execute("SELECT * FROM routing_leases WHERE lease_id=?", (context.request_id,)).fetchone()
        live = db.execute("SELECT COUNT(*) AS n FROM routing_leases WHERE account_id=? AND expires_at>? AND lease_id<>?", (aid,iso_now(),context.request_id)).fetchone()
        if live['n'] >= settings.gateway_account_max_concurrency:
            raise GatewayError(503,'Retry account is busy',code='account_pool_busy')
        db.execute("DELETE FROM routing_leases WHERE lease_id=?", (context.request_id,))
        db.execute("INSERT INTO routing_leases(lease_id,account_id,provider,model,work_units,expires_at) VALUES(?,?,?,?,?,?)",
                   (context.request_id,aid,context.provider,context.model,(current or {}).get('work_units',1000),expiry()))
        if current and current['account_id'] != aid:
            db.execute("INSERT INTO routing_events(id,key_id,account_id,provider,reason,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex,context.key_id,aid,context.provider,'retry',iso_now()))
    await gateway_store.call(operation)
    track_route(gateway, context.request_id)


def _task_finished(task: Any) -> bool:
    done = getattr(task, "done", None)
    return bool(callable(done) and done())


async def maintain(gateway):
    now = iso_now()
    for lease_id, task in tuple(gateway._active_routing.items()):
        if _task_finished(task):
            await gateway_store.execute("DELETE FROM routing_leases WHERE lease_id=?", (lease_id,))
            untrack_route(gateway, lease_id)
            logger.warning("routing_lease_reaped lease_id=%s reason=owner_task_done", lease_id[:16])
            continue
        await gateway_store.execute("UPDATE routing_leases SET expires_at=? WHERE lease_id=?", (expiry(), lease_id))
    await gateway_store.execute("DELETE FROM routing_leases WHERE expires_at<=?", (now,))
    await gateway_store.execute("DELETE FROM routing_bindings WHERE expires_at<=?", (now,))
    await gateway_store.execute("DELETE FROM routing_events WHERE created_at<?", ((utc_now()-dt.timedelta(days=7)).isoformat().replace("+00:00","Z"),))
    await refresh_provider_catalogs(gateway)


async def load_catalog_models() -> None:
    from app.providers.catalog_extra import ExtraModel, replace_all

    rows = await gateway_store.all(
        "SELECT provider, model_id, model_type, source FROM catalog_models"
    )
    replace_all(
        [
            ExtraModel(
                str(row["provider"]),
                str(row["model_id"]),
                str(row["model_type"] or "text"),
                str(row["source"] or "manual"),
            )
            for row in rows
        ]
    )


async def persist_upstream(provider: str, rows: list[tuple[str, str]]) -> None:
    from app.providers.catalog_extra import set_upstream

    if not rows:
        return
    marks = ",".join("?" * len(rows))
    await gateway_store.execute(
        f"DELETE FROM catalog_models WHERE provider=? AND source='upstream' AND model_id NOT IN ({marks})",
        (provider, *[model_id for model_id, _model_type in rows]),
    )
    created = iso_now()
    for model_id, model_type in rows:
        await gateway_store.execute(
            """INSERT INTO catalog_models(provider, model_id, model_type, source, created_at)
               VALUES(?,?,?,'upstream',?)
               ON CONFLICT(provider, model_id) DO UPDATE SET
                 model_type=CASE WHEN catalog_models.source='manual' THEN catalog_models.model_type ELSE excluded.model_type END,
                 source=CASE WHEN catalog_models.source='manual' THEN catalog_models.source ELSE 'upstream' END""",
            (provider, model_id, model_type, created),
        )
    set_upstream(provider, rows)


async def add_manual_model(provider: str, model_id: str, model_type: str) -> None:
    from app.providers.catalog_extra import upsert_manual

    await gateway_store.execute(
        """INSERT INTO catalog_models(provider, model_id, model_type, source, created_at)
           VALUES(?,?,?,'manual',?)
           ON CONFLICT(provider, model_id) DO UPDATE SET
             model_type=excluded.model_type, source='manual', created_at=excluded.created_at""",
        (provider, model_id, model_type, iso_now()),
    )
    upsert_manual(provider, model_id, model_type)


async def remove_extra_model(provider: str, model_id: str) -> bool:
    from app.providers.catalog_extra import remove, source_for

    if source_for(provider, model_id) is None:
        return False
    await gateway_store.execute(
        "DELETE FROM catalog_models WHERE provider=? AND model_id=?",
        (provider, model_id),
    )
    remove(provider, model_id)
    return True


def _pairs(ids: list[str], *, prefix: str = "") -> list[tuple[str, str]]:
    from app.providers.catalog_extra import infer_model_type, normalize_model_id

    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for model_id in ids:
        text = model_id.strip()
        if prefix and not text.startswith(prefix):
            text = prefix + text
        if prefix == "workbuddy/":
            text = normalize_model_id("workbuddy", text)
        if not text or text in seen:
            continue
        seen.add(text)
        rows.append((text, infer_model_type(text)))
    return rows


async def _get_json(
    client: Any,
    url: str,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
) -> Any | None:
    try:
        response = await client.get(url, headers=headers, params=params, timeout=10)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None


async def _post_json(client: Any, url: str, headers: dict[str, str], body: dict[str, Any]) -> Any | None:
    try:
        response = await client.post(url, headers=headers, json=body, timeout=10)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None


async def _refresh_grok() -> list[tuple[str, str]]:
    from app.http_client import shared_client
    from app.providers.catalog_extra import parse_model_ids
    from app.providers.grok import build_headers, load_credentials, should_refresh, upstream_base_url

    accounts = await gateway_store.healthy_accounts("grok")
    if not accounts:
        return []
    path = Path(str(accounts[0]["credential_path"]))
    parsed = load_credentials(json.loads(path.read_text(encoding="utf-8-sig")), path=path)
    if should_refresh(parsed):
        return []
    payload = await _get_json(
        await shared_client(),
        f"{upstream_base_url(parsed.auth_mode)}/models",
        build_headers(parsed),
    )
    return _pairs([item for item in parse_model_ids(payload) if item.startswith("grok-")])


async def _refresh_codex() -> list[tuple[str, str]]:
    from app.http_client import shared_client
    from app.providers.catalog_extra import parse_model_ids
    from app.providers.codex import (
        build_headers,
        parse_auth_payload,
        should_refresh,
        upstream_base_url,
        upstream_client_params,
    )

    accounts = await gateway_store.healthy_accounts("codex")
    if not accounts:
        return []
    path = Path(str(accounts[0]["credential_path"]))
    parsed = parse_auth_payload(json.loads(path.read_text(encoding="utf-8-sig")), source="pool", path=path)
    if should_refresh(parsed):
        return []
    payload = await _get_json(
        await shared_client(),
        f"{upstream_base_url(parsed.auth_mode).rstrip('/')}/models",
        build_headers(parsed, accept="application/json"),
        params=upstream_client_params(),
    )
    return _pairs(parse_model_ids(payload))


async def _refresh_antigravity() -> list[tuple[str, str]]:
    from app.http_client import shared_client
    from app.providers.antigravity import (
        build_headers,
        fetch_available_models_url,
        load_credentials,
        should_refresh,
    )
    from app.providers.catalog_extra import parse_antigravity_model_ids

    accounts = await gateway_store.healthy_accounts("antigravity")
    if not accounts:
        return []
    path = Path(str(accounts[0]["credential_path"]))
    parsed = load_credentials(json.loads(path.read_text(encoding="utf-8-sig")), path=path)
    if should_refresh(parsed):
        return []
    payload = await _post_json(
        await shared_client(),
        fetch_available_models_url(parsed.host),
        build_headers(parsed),
        {},
    )
    return _pairs(parse_antigravity_model_ids(payload))


async def _refresh_workbuddy() -> list[tuple[str, str]]:
    from app.http_client import shared_client
    from app.providers.catalog_extra import parse_model_ids
    from app.providers.workbuddy import base_url, chat_headers, load_credentials

    accounts = await gateway_store.healthy_accounts("workbuddy")
    if not accounts:
        return []
    path = Path(str(accounts[0]["credential_path"]))
    parsed = load_credentials(json.loads(path.read_text(encoding="utf-8-sig")), path=path)
    payload = await _get_json(
        await shared_client(),
        base_url(parsed.realm) + "/v2/models",
        chat_headers(parsed),
    )
    return _pairs(parse_model_ids(payload), prefix="workbuddy/")


_CATALOG_FETCHERS = (
    ("grok", _refresh_grok),
    ("codex", _refresh_codex),
    ("antigravity", _refresh_antigravity),
    ("workbuddy", _refresh_workbuddy),
)


async def refresh_provider_catalogs(gateway, *, force: bool = False) -> dict[str, int]:
    """Pull model ids from each upstream. Empty or failed pulls keep the last list."""
    now = time.monotonic()
    stamps = getattr(gateway, "_catalog_refresh_at", {})
    counts: dict[str, int] = {}
    for provider, fetcher in _CATALOG_FETCHERS:
        if not force and now - float(stamps.get(provider, 0.0)) < 6 * 3600:
            continue
        if provider == "codex":
            from app.providers.codex_version import refresh_codex_client_version

            await refresh_codex_client_version()
        stamps[provider] = now
        try:
            rows = await fetcher()
        except Exception:
            rows = []
        if rows:
            try:
                await persist_upstream(provider, rows)
            except Exception:
                rows = []
        counts[provider] = len(rows)
    gateway._catalog_refresh_at = stamps
    return counts


async def status(gateway):
    rows = await gateway_store.all("SELECT account_id,label,provider,status FROM accounts WHERE status<>'deleted'")
    for row in rows:
        remaining,paused,reason = quota_state(gateway,row["account_id"])
        leases = await gateway_store.one("SELECT COUNT(*) AS n FROM routing_leases WHERE account_id=? AND expires_at>?", (row['account_id'],iso_now()))
        cache = gateway._quota_cache.get(row['account_id'])
        age = max(0,time.monotonic()-cache[0]) if cache else None
        row.update(remaining_percent=remaining,paused=paused,reason=reason,
                   current_concurrency=max(leases['n'],gateway.current_concurrency(row["account_id"])),
                   quota_age_seconds=round(age) if age is not None else None,
                   quota_windows=cache[1].get('limits',[]) if cache else [])
        row["events"] = await gateway_store.all("SELECT reason,created_at FROM routing_events WHERE account_id=? ORDER BY created_at DESC LIMIT 8", (row["account_id"],))
        recent = await gateway_store.one("SELECT SUM(total_tokens) AS tokens,COUNT(*) AS calls FROM call_records WHERE account_id=? AND started_at>=?",
            (row["account_id"],(utc_now()-dt.timedelta(minutes=5)).isoformat().replace("+00:00","Z")))
        row["recent_tokens"] = int(recent["tokens"] or 0)
        row["recent_calls"] = int(recent["calls"] or 0)
    return {"data":rows,"updated_at":iso_now()}
