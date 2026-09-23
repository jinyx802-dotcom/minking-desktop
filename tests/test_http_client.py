from __future__ import annotations

import httpx

from app.config import settings
from app.http_client import (
    close_shared_client,
    rotate_shared_client,
    set_http_transport,
    shared_client,
)


async def test_pool_rotation_preserves_old_client_until_drain(monkeypatch):
    monkeypatch.setattr(settings, "gateway_pool_drain_seconds", 60.0)
    set_http_transport(httpx.MockTransport(lambda request: httpx.Response(200)))
    first = await shared_client()

    assert await rotate_shared_client(first, reason="test") is True
    second = await shared_client()

    assert second is not first
    assert first.is_closed is False
    assert await rotate_shared_client(first, reason="stale_test") is False

    await close_shared_client()
    assert first.is_closed is True
    assert second.is_closed is True
