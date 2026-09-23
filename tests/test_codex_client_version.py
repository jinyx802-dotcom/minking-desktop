from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from app.config import settings
from app.http_client import set_http_transport
from app.providers.codex_version import parse_codex_cli_version
from app.scheduler import refresh_provider_catalogs
from app.store.gateway import gateway_store
from conftest import ADMIN_HEADERS, auth_payload


def test_codex_client_version_seed_is_the_current_stable_cli():
    from app.config import Settings

    assert Settings.model_fields["codex_client_version"].default == "0.156.0"


def test_stable_cli_versions_are_accepted_and_prereleases_are_not():
    assert parse_codex_cli_version({"version": "0.156.0"}) == "0.156.0"
    assert parse_codex_cli_version({"version": "v0.156.0"}) == "0.156.0"
    assert parse_codex_cli_version({"version": "0.157.0-alpha.9"}) is None
    assert parse_codex_cli_version({"version": "latest"}) is None
    assert parse_codex_cli_version(["0.156.0"]) is None


async def test_catalog_refresh_loads_latest_cli_version_before_codex_models(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "codex_client_version", "0.155.1")
    credential = tmp_path / "codex.json"
    credential.write_text(json.dumps(auth_payload("acct-cli-version")), encoding="utf-8")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "registry.npmjs.org" in str(request.url):
            return httpx.Response(200, json={"version": "0.156.0"})
        assert request.url.params["client_version"] == "0.156.0"
        assert request.headers["version"] == "0.156.0"
        return httpx.Response(
            200,
            json={"models": [{"slug": "gpt-6-sol"}, {"slug": "gpt-6-luna"}]},
        )

    set_http_transport(httpx.MockTransport(handler))

    async def healthy(provider: str | None = None):
        if provider == "codex":
            return [{"account_id": "acct-cli-version", "credential_path": str(credential)}]
        return []

    saved: list[tuple[str, list[tuple[str, str]]]] = []

    async def persist(provider: str, rows: list[tuple[str, str]]) -> None:
        saved.append((provider, rows))

    monkeypatch.setattr(gateway_store, "healthy_accounts", healthy)
    monkeypatch.setattr("app.scheduler.persist_upstream", persist)
    gateway = SimpleNamespace()
    counts = await refresh_provider_catalogs(gateway, force=True)
    assert settings.codex_client_version == "0.156.0"
    assert counts["codex"] == 2
    assert saved == [("codex", [("gpt-6-sol", "text"), ("gpt-6-luna", "text")])]
    assert any("registry.npmjs.org" in url for url in seen)
    assert any("/models" in url and "client_version=0.156.0" in url for url in seen)

    seen.clear()
    await refresh_provider_catalogs(gateway, force=False)
    assert seen == []


async def test_catalog_refresh_keeps_the_seed_version_when_npm_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "codex_client_version", "0.155.1")
    credential = tmp_path / "codex.json"
    credential.write_text(json.dumps(auth_payload("acct-cli-keep")), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if "registry.npmjs.org" in str(request.url):
            return httpx.Response(503, json={"error": "unavailable"})
        assert request.url.params["client_version"] == "0.155.1"
        return httpx.Response(200, json={"models": [{"slug": "gpt-6-luna"}]})

    set_http_transport(httpx.MockTransport(handler))

    async def healthy(provider: str | None = None):
        if provider == "codex":
            return [{"account_id": "acct-cli-keep", "credential_path": str(credential)}]
        return []

    async def persist(provider: str, rows: list[tuple[str, str]]) -> None:
        return None

    monkeypatch.setattr(gateway_store, "healthy_accounts", healthy)
    monkeypatch.setattr("app.scheduler.persist_upstream", persist)
    counts = await refresh_provider_catalogs(SimpleNamespace(), force=True)
    assert settings.codex_client_version == "0.155.1"
    assert counts["codex"] == 1


def test_admin_refresh_reports_the_updated_cli_version(client, monkeypatch):
    monkeypatch.setattr(settings, "codex_client_version", "0.155.1")
    set_http_transport(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"version": "0.156.0"}))
    )
    response = client.post("/admin/api/models/refresh", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["codex_client_version"] == "0.156.0"
    assert body["counts"]["codex"] == 0
