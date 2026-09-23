from __future__ import annotations

import asyncio
import logging
import sqlite3

import httpx
import pytest
from conftest import ADMIN_HEADERS, ADMIN_PASSWORD, auth_payload, import_pool
from starlette.requests import ClientDisconnect, Request
from test_codex_gateway import key_headers, sse

from app.__main__ import _configure_application_logging
from app.api.codex_gateway import _finish_failure, _json_object
from app.codex_gateway import GatewayError, codex_gateway
from app.config import settings
from app.http_client import set_http_transport
from app.store.gateway import GatewayStore, gateway_store, iso_now

ORIGIN = {"Origin": "http://testserver"}


async def test_candidate_start_can_skip_in_progress_call_recovery(tmp_path, monkeypatch):
    from app.codex_gateway import CodexGateway

    monkeypatch.setattr(settings, "data_dir", tmp_path / "candidate-data")
    monkeypatch.setattr(settings, "gateway_skip_call_recovery_on_startup", True)
    candidate = CodexGateway()
    await candidate.start()
    try:
        assert gateway_store.engine == "sqlite"
    finally:
        await candidate.stop()


def test_application_logging_configuration_enables_debug_handler(monkeypatch):
    logger = logging.getLogger("transfer_station.errors")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    try:
        logger.handlers = []
        monkeypatch.setattr(settings, "log_level", "debug")
        _configure_application_logging()
        assert logger.level == logging.DEBUG
        assert logger.propagate is False
        assert len(logger.handlers) == 1
        assert logger.handlers[0].level == logging.DEBUG
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate


async def test_disconnected_request_body_is_classified_as_interrupted(monkeypatch):
    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    request = Request(
        {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []},
        receive,
    )
    with pytest.raises(GatewayError) as raised:
        await _json_object(request)
    assert raised.value.status == 499
    assert raised.value.code == "client_interrupted"

    captured = {}

    async def capture_finish(_context, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(codex_gateway, "finish_call", capture_finish)
    await _finish_failure(object(), ClientDisconnect())
    assert captured["status"] == "interrupted"
    assert captured["http_status"] == 499
    assert captured["error_code"] == "client_interrupted"


async def test_incomplete_request_body_times_out(monkeypatch):
    async def receive() -> dict[str, str]:
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr(settings, "gateway_request_body_timeout_seconds", 0.01)
    request = Request(
        {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []},
        receive,
    )
    with pytest.raises(GatewayError) as raised:
        await _json_object(request)
    assert raised.value.status == 408
    assert raised.value.code == "request_body_timeout"


def test_admin_cookie_is_hashed_and_has_required_attributes(client):
    login = client.post(
        "/admin/api/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
        headers=ORIGIN,
    )
    cookie = login.headers["set-cookie"]
    raw_session = client.cookies.get("ts_admin_session")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/admin" in cookie
    assert "Secure" not in cookie
    database = (settings.data_dir / "gateway.sqlite3").read_bytes()
    assert raw_session.encode() not in database
    assert ADMIN_PASSWORD.encode() not in database
    credential = client.portal.call(
        gateway_store.one,
        "SELECT password_hash FROM admin_credentials WHERE username='admin'",
    )
    assert credential["password_hash"].startswith("$argon2id$")


def test_login_failure_rate_limit_session_expiry_logout_and_csrf(client):
    for _ in range(settings.admin_login_max_failures):
        denied = client.post(
            "/admin/api/auth/login",
            json={"username": "admin", "password": "definitely-wrong"},
            headers=ORIGIN,
        )
        assert denied.status_code == 401
    limited = client.post(
        "/admin/api/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
        headers=ORIGIN,
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "admin_login_rate_limited"

    missing_origin = client.post(
        "/admin/api/keys",
        json={"name": "blocked"},
        headers={"X-CSRF-Token": ADMIN_HEADERS["X-CSRF-Token"]},
    )
    assert missing_origin.status_code == 403
    assert missing_origin.json()["error"]["code"] == "admin_origin_required"
    missing_csrf = client.post("/admin/api/keys", json={"name": "blocked"}, headers=ORIGIN)
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "invalid_csrf_token"

    client.portal.call(gateway_store.execute, "UPDATE admin_sessions SET expires_at='2000-01-01T00:00:00Z'")
    expired = client.get("/admin/api/auth/session")
    assert expired.status_code == 401


def test_password_change_rotates_session_and_logout(client):
    old_cookie = client.cookies.get("ts_admin_session")
    replacement = "replacement-admin-password-2026"
    changed = client.put(
        "/admin/api/auth/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": replacement},
        headers=ADMIN_HEADERS,
    )
    assert changed.status_code == 200
    assert changed.json()["csrf_token"] != ADMIN_HEADERS["X-CSRF-Token"]
    assert client.cookies.get("ts_admin_session") != old_cookie
    ADMIN_HEADERS["X-CSRF-Token"] = changed.json()["csrf_token"]

    old_login = client.post(
        "/admin/api/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
        headers=ORIGIN,
    )
    assert old_login.status_code == 401
    new_login = client.post(
        "/admin/api/auth/login",
        json={"username": "admin", "password": replacement},
        headers=ORIGIN,
    )
    assert new_login.status_code == 200
    ADMIN_HEADERS["X-CSRF-Token"] = new_login.json()["csrf_token"]
    logout = client.post("/admin/api/auth/logout", headers=ADMIN_HEADERS)
    assert logout.status_code == 200
    assert client.get("/admin/api/auth/session").status_code == 401


def test_call_records_request_id_usage_filters_dashboard_and_invalid_key_exclusion(client):
    imported = import_pool(client, auth_payload("acct-calls"))
    key = imported["generated_api_key"]["key"]
    usage = {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "input_tokens_details": {"cached_tokens": 4},
        "output_tokens_details": {"reasoning_tokens": 3},
    }
    set_http_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("response-must-not-be-stored", usage=usage),
            )
        )
    )
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "secret prompt"}]},
        headers=key_headers(key),
    )
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    invalid = client.post(
        "/v1/responses",
        json={"input": "not recorded"},
        headers=key_headers("sk-ts-invalid"),
    )
    assert invalid.status_code == 401 and "x-request-id" in invalid.headers
    assert client.get("/admin/api/calls", headers=ADMIN_HEADERS).json()["total"] == 1
    database = (settings.data_dir / "gateway.sqlite3").read_bytes()
    for secret in ("secret prompt", "response-must-not-be-stored", key):
        assert secret.encode() not in database


def test_failures_unknown_usage_and_image_retry_are_recorded(client):
    imported = import_pool(client, auth_payload("acct-one"), auth_payload("acct-two"))
    key = imported["generated_api_key"]["key"]
    seen = 0

    def image_handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        if seen == 1:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(image_handler))
    image = client.post(
        "/v1/images/generations", json={"prompt": "cat"}, headers=key_headers(key)
    )
    assert image.status_code == 200

    malformed = client.post(
        "/v1/responses",
        content="not-json",
        headers={**key_headers(key), "Content-Type": "application/json"},
    )
    assert malformed.status_code == 400
    assert seen == 2


def test_invalid_utf8_json_is_a_400_not_an_internal_error(client):
    imported = import_pool(client, auth_payload("acct-invalid-json"))
    key = imported["generated_api_key"]["key"]
    response = client.post(
        "/v1/responses",
        content=b"\xff\xfe\xfa",
        headers={**key_headers(key), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_invalid_json_logs_transport_metadata_without_body(client, caplog, monkeypatch):
    imported = import_pool(client, auth_payload("acct-invalid-json-log"))
    key = imported["generated_api_key"]["key"]
    body = "diagnostic-secret-not-json"
    monkeypatch.setattr(settings, "gateway_request_logging_enabled", True)
    monkeypatch.setattr(settings, "gateway_diagnostic_logging_enabled", True)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="transfer_station.errors"):
        response = client.post(
            "/v1/chat/completions",
            content=body,
            headers={**key_headers(key), "Content-Type": "application/json"},
        )

    assert response.status_code == 400
    records = [
        record.message
        for record in caplog.records
        if record.message.startswith("invalid_json_diagnostics ")
    ]
    assert len(records) == 1
    diagnostic = records[0]
    assert f"body_bytes={len(body.encode())}" in diagnostic
    assert "json_error='Expecting value'" in diagnostic
    assert "error_position=0" in diagnostic
    assert "content_type='application/json'" in diagnostic
    assert body not in diagnostic
    assert key not in diagnostic
    request_records = [
        record.message
        for record in caplog.records
        if record.message.startswith("request_completed ")
    ]
    assert len(request_records) == 1
    assert "path=/v1/chat/completions status=400" in request_records[0]
    assert body not in request_records[0]
    assert key not in request_records[0]

    caplog.clear()
    monkeypatch.setattr(settings, "gateway_diagnostic_logging_enabled", False)
    response = client.post(
        "/v1/chat/completions",
        content=body,
        headers={**key_headers(key), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert not any(
        record.message.startswith("invalid_json_diagnostics ")
        for record in caplog.records
    )


def test_codebuddy_v1_responses_force_connection_close(client, monkeypatch):
    monkeypatch.setattr(settings, "gateway_codebuddy_connection_close_enabled", True)
    codebuddy_headers = {
        "User-Agent": "CLI/2.138.0 CodeBuddy/2.138.0",
        "Content-Type": "application/json",
    }

    codebuddy = client.post(
        "/v1/chat/completions",
        content="not-json",
        headers=codebuddy_headers,
    )
    assert codebuddy.status_code == 401
    assert codebuddy.headers["connection"] == "close"

    ordinary = client.post(
        "/v1/chat/completions",
        content="not-json",
        headers={"User-Agent": "ordinary-client", "Content-Type": "application/json"},
    )
    assert ordinary.status_code == 401
    assert "connection" not in ordinary.headers

    non_api = client.get(
        "/health",
        headers={"User-Agent": "CLI/2.138.0 CodeBuddy/2.138.0"},
    )
    assert non_api.status_code == 200
    assert "connection" not in non_api.headers

    monkeypatch.setattr(settings, "gateway_codebuddy_connection_close_enabled", False)
    disabled = client.post(
        "/v1/chat/completions",
        content="not-json",
        headers=codebuddy_headers,
    )
    assert disabled.status_code == 401
    assert "connection" not in disabled.headers


def test_recovery_and_retention_do_not_reduce_permanent_usage(client):
    imported = import_pool(client, auth_payload("acct-retention"))
    key_info = imported["generated_api_key"]
    set_http_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("retention-response"),
            )
        )
    )
    assert client.post(
        "/v1/responses", json={"input": "hello"}, headers=key_headers(key_info["key"])
    ).status_code == 200
    assert client.delete(
        f"/admin/api/keys/{key_info['id']}", headers=ADMIN_HEADERS
    ).status_code == 200
    assert client.delete("/admin/api/accounts/acct-retention", headers=ADMIN_HEADERS).status_code == 200


def test_admin_session_can_be_forcibly_expired(client):
    assert client.get("/admin/api/auth/session").status_code == 200
    client.portal.call(
        gateway_store.execute,
        "UPDATE admin_sessions SET expires_at=?",
        ("2000-01-01T00:00:00Z",),
    )
    assert client.get("/admin/api/auth/session").status_code == 401
    assert iso_now().endswith("Z")


async def test_legacy_usage_foreign_keys_are_migrated_without_data_loss(tmp_path):
    store = GatewayStore()
    await store.start(tmp_path / "fresh.sqlite3")
    try:
        tables = await store.all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {row["name"] for row in tables}
        assert "accounts" in names and "api_keys" in names
        assert "call_records" in names
        assert "usage_daily" in names
        assert "session_bindings" not in names
    finally:
        await store.stop()


async def test_legacy_error_ring_adds_user_and_message_columns_without_data_loss(tmp_path):
    store = GatewayStore()
    await store.start(tmp_path / "fresh-errors.sqlite3")
    try:
        tables = await store.all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {row["name"] for row in tables}
        assert "error_ring" in names
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_cancelled_database_waiter_does_not_kill_worker(tmp_path):
    store = GatewayStore()
    await store.start(tmp_path / "cancelled-waiter.sqlite3")
    try:
        assert store._queue is not None
        assert store._worker is not None
        cancelled = asyncio.get_running_loop().create_future()
        cancelled.cancel()
        await store._queue.put(
            (
                lambda db: db.execute(
                    "INSERT INTO api_keys(id,name,key_prefix,fingerprint,created_at) "
                    "VALUES('cancelled-key','Cancelled','sk-ts-can','cancelled-fp','2020-01-01')"
                ),
                cancelled,
            )
        )
        await store._queue.join()

        assert not store._worker.done()
        assert await store.one(
            "SELECT name FROM api_keys WHERE id='cancelled-key'"
        ) == {"name": "Cancelled"}
    finally:
        await store.stop()
