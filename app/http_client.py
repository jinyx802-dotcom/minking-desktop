from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

_http_transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None
_shared_client: httpx.AsyncClient | None = None
_shared_transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None
_client_lock = asyncio.Lock()
_retired_clients: set[httpx.AsyncClient] = set()
_retire_tasks: set[asyncio.Task[None]] = set()
logger = logging.getLogger(__name__)


def reset_http_transport() -> None:
    global _http_transport
    _http_transport = None


def set_http_transport(
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None,
) -> None:
    global _http_transport
    _http_transport = transport


def new_client(**kwargs: Any) -> httpx.AsyncClient:
    if _http_transport is not None:
        kwargs.setdefault("transport", _http_transport)
    return httpx.AsyncClient(**kwargs)


async def shared_client() -> httpx.AsyncClient:
    """Return the process-wide upstream pool, rebuilding it when tests swap transports."""
    global _shared_client, _shared_transport
    stale_client: httpx.AsyncClient | None = None
    async with _client_lock:
        if _shared_client is None or _shared_transport is not _http_transport:
            stale_client = _shared_client
            _shared_client = _build_shared_client()
            _shared_transport = _http_transport
        client = _shared_client
    if stale_client is not None:
        await stale_client.aclose()
    return client


def _build_shared_client() -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {
        "timeout": None,
        "limits": httpx.Limits(
            max_connections=settings.gateway_max_connections,
            max_keepalive_connections=settings.gateway_max_keepalive_connections,
        ),
    }
    if _http_transport is not None:
        kwargs["transport"] = _http_transport
    return httpx.AsyncClient(**kwargs)


async def _close_retired_client(client: httpx.AsyncClient) -> None:
    try:
        await asyncio.sleep(max(0.0, settings.gateway_pool_drain_seconds))
    finally:
        try:
            await asyncio.shield(client.aclose())
        finally:
            _retired_clients.discard(client)


async def rotate_shared_client(
    expected_client: httpx.AsyncClient | None = None,
    *,
    reason: str = "pool_timeout",
) -> bool:
    """Move new work to a fresh pool while allowing existing streams to drain."""
    global _shared_client, _shared_transport
    async with _client_lock:
        current = _shared_client
        if current is None:
            _shared_client = _build_shared_client()
            _shared_transport = _http_transport
            logger.warning("upstream_pool_initialized reason=%s", reason)
            return True
        if expected_client is not None and current is not expected_client:
            return False
        _shared_client = _build_shared_client()
        _shared_transport = _http_transport
        _retired_clients.add(current)
        task = asyncio.create_task(_close_retired_client(current))
        _retire_tasks.add(task)
        task.add_done_callback(_retire_tasks.discard)
    logger.warning(
        "upstream_pool_rotated reason=%s drain_seconds=%s",
        reason,
        round(max(0.0, settings.gateway_pool_drain_seconds), 3),
    )
    return True


async def close_shared_client() -> None:
    global _shared_client, _shared_transport
    async with _client_lock:
        clients = list(_retired_clients)
        if _shared_client is not None:
            clients.append(_shared_client)
        tasks = list(_retire_tasks)
        _shared_client = None
        _shared_transport = None
        _retired_clients.clear()
        _retire_tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for client in clients:
        if not client.is_closed:
            await client.aclose()
