from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import __version__
from app.codex_gateway import codex_gateway
from app.store.gateway import gateway_store

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, Any]:
    rows = await gateway_store.all("SELECT provider, COUNT(*) AS count FROM accounts WHERE status='active' GROUP BY provider")
    by_provider = {str(row.get("provider") or "codex"): int(row.get("count") or 0) for row in rows}
    total = sum(by_provider.values())
    return {
        "status": "ok" if total else "degraded",
        "codex_accounts": by_provider.get("codex", 0),
        "accounts": total,
        "providers": by_provider,
        "version": __version__,
    }


@router.get("/v1/providers")
async def providers() -> dict[str, Any]:
    accounts = await gateway_store.all("SELECT provider, status FROM accounts")
    return {"data": codex_gateway.list_providers(accounts)}
