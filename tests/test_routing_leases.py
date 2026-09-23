from __future__ import annotations

import asyncio
import time

import pytest

from app.codex_gateway import CallContext, GatewayError, UpstreamLease, codex_gateway
from app.config import settings
from app.scheduler import expiry, maintain
from app.store.gateway import gateway_store


class _DoneTask:
    def done(self) -> bool:
        return True


class _LiveTask:
    def done(self) -> bool:
        return False


@pytest.fixture
async def routing_app():
    await codex_gateway.start()
    try:
        yield
    finally:
        await codex_gateway.stop()


def _context(request_id: str) -> CallContext:
    return CallContext(
        request_id=request_id,
        key_id="key-routing-lease",
        key_name="lease-test",
        endpoint="/v1/responses",
        model="gpt-6-astra",
        is_stream=True,
        started_monotonic=time.monotonic(),
        account_id="acct-routing-lease",
    )


async def _insert_lease(lease_id: str) -> None:
    await gateway_store.execute(
        "INSERT INTO routing_leases(lease_id,account_id,provider,model,work_units,expires_at) VALUES(?,?,?,?,?,?)",
        (lease_id, "acct-routing-lease", "codex", "gpt-6-astra", 1, expiry()),
    )


async def test_finish_call_releases_lease_when_finalize_fails(routing_app, monkeypatch):
    lease_id = "lease-finish-fail"
    await _insert_lease(lease_id)
    context = _context(lease_id)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("finalize exploded")

    monkeypatch.setattr(gateway_store, "finalize_call", boom)
    with pytest.raises(RuntimeError, match="finalize exploded"):
        await codex_gateway.finish_call(
            context, status="failed", http_status=502, error_code="upstream_error"
        )
    leftover = await gateway_store.one(
        "SELECT lease_id FROM routing_leases WHERE lease_id=?", (lease_id,)
    )
    assert leftover is None
    assert context.finalized is True


async def test_maintain_reaps_lease_when_owner_task_is_done(routing_app, monkeypatch):
    async def _no_catalog(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("app.scheduler.refresh_provider_catalogs", _no_catalog)
    lease_id = "lease-owner-done"
    await _insert_lease(lease_id)
    codex_gateway._active_routing[lease_id] = _DoneTask()
    await maintain(codex_gateway)
    leftover = await gateway_store.one(
        "SELECT lease_id FROM routing_leases WHERE lease_id=?", (lease_id,)
    )
    assert leftover is None
    assert lease_id not in codex_gateway._active_routing


async def test_maintain_keeps_lease_while_owner_task_runs(routing_app, monkeypatch):
    async def _no_catalog(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("app.scheduler.refresh_provider_catalogs", _no_catalog)
    lease_id = "lease-owner-live"
    await _insert_lease(lease_id)
    codex_gateway._active_routing[lease_id] = _LiveTask()
    await maintain(codex_gateway)
    leftover = await gateway_store.one(
        "SELECT lease_id FROM routing_leases WHERE lease_id=?", (lease_id,)
    )
    assert leftover is not None
    codex_gateway._active_routing.pop(lease_id, None)
    await gateway_store.execute("DELETE FROM routing_leases WHERE lease_id=?", (lease_id,))


async def test_upstream_lease_idle_timeout_raises(monkeypatch):
    monkeypatch.setattr(settings, "gateway_stream_idle_timeout_seconds", 0.05)

    async def hang():
        await asyncio.sleep(2)
        yield b"late"

    class _Response:
        async def aiter_bytes(self):
            async for chunk in hang():
                yield chunk

        async def aclose(self) -> None:
            return None

    lease = UpstreamLease(
        _Response(),
        {"account_id": "acct-idle"},
        asyncio.Semaphore(1),
        iterator=hang(),
    )
    with pytest.raises(GatewayError) as caught:
        async for _chunk in lease.aiter_bytes():
            pass
    assert caught.value.code == "upstream_stream_timeout"
    assert caught.value.status == 502
