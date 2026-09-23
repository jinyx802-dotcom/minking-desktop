from __future__ import annotations

import asyncio
import base64
import codecs
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, UploadFile

from app.config import settings
from app.providers.image_protocol import (
    _image_request_assets, _image_responses_payload, _image_results_from_sse, _image_data_url,
)
from app.http_client import new_client, rotate_shared_client, shared_client
from app.image_artifacts import ArtifactError, compact_payload_images, image_artifacts
from app.providers.base import (
    ImportedAccount,
    account_provider,
    canonical_image_model,
    default_model_for,
    parse_capabilities,
    resolve_provider_model,
)
from app.providers.codex import (
    CodexCredentials,
    OAUTH_BIND_HOSTS as CODEX_OAUTH_BIND_HOSTS,
    OAUTH_CALLBACK_PORT as CODEX_OAUTH_CALLBACK_PORT,
    _upstream_message,
    build_headers,
    build_oauth_auth_url as build_codex_oauth_auth_url,
    exchange_authorization_code as exchange_codex_authorization_code,
    map_upstream_status,
    oauth_redirect_uri as codex_oauth_redirect_uri,
    parse_auth_payload,
    refresh_credentials,
    should_refresh,
    upstream_base_url,
    upstream_client_params,
    upstream_error_metadata,
)
from app.providers.codex_protocol import (
    apply_responses_lite_contract,
    completed_response_from_sse,
    convert_sse_to_chat_chunks,
    ensure_response_created_at,
    iter_sse_blocks,
    parse_sse_event,
    responses_lite_enabled,
    stamp_sse_created_at,
    strip_bare_reasoning,
    to_responses_payload,
)
from app.providers.grok import (
    GrokCredentials,
    GrokRewriteSpec,
    TOOL_SEARCH_WIRE_NAME,
    _SHELL_TOOL_NAMES,
    build_grok_compact_payload,
    build_headers as grok_build_headers,
    compact_history_usable,
    extract_grok_message_text,
    grok_compact_encrypted_content,
    grok_compact_json_response,
    grok_compact_v2_sse,
    grok_reject_hint,
    load_credentials as load_grok_credentials,
    OAUTH_BIND_HOSTS as GROK_OAUTH_BIND_HOSTS,
    OAUTH_CALLBACK_PORT as GROK_OAUTH_CALLBACK_PORT,
    build_oauth_auth_url as build_grok_oauth_auth_url,
    exchange_authorization_code as exchange_grok_authorization_code,
    exchange_device_authorization as exchange_grok_device_authorization,
    start_device_authorization as start_grok_device_authorization,
    local_auth_path,
    map_upstream_status as grok_map_upstream_status,
    oauth_redirect_uri as grok_oauth_redirect_uri,
    parse_quota_snapshot as parse_grok_quota_snapshot,
    refresh_credentials as refresh_grok_credentials,
    rewrite_grok_codex_sse_block,
    rewrite_grok_completed_response,
    sanitize_grok_chat_payload,
    sanitize_grok_responses,
    shape_grok_image_body,
    should_refresh as grok_should_refresh,
    upstream_base_url as grok_upstream_base_url,
)
from app.providers.antigravity import (
    DEFAULT_MODEL as ANTIGRAVITY_DEFAULT_MODEL,
    LOAD_CODE_ASSIST_METADATA,
    AntigravityCredentials,
    build_headers as antigravity_build_headers,
    build_image_cloudcode_envelope,
    OAUTH_BIND_HOSTS as ANTIGRAVITY_OAUTH_BIND_HOSTS,
    OAUTH_CALLBACK_PORT as ANTIGRAVITY_OAUTH_CALLBACK_PORT,
    build_oauth_auth_url as build_antigravity_oauth_auth_url,
    cloudcode_bytes_to_codex_sse,
    exchange_authorization_code as exchange_antigravity_authorization_code,
    extract_image_b64_from_cloudcode,
    extract_project_id as extract_antigravity_project_id,
    fetch_available_models_url as antigravity_fetch_models_url,
    generate_url as antigravity_generate_url,
    google_error_message,
    google_error_status,
    is_claude_model as antigravity_is_claude_model,
    is_google_capacity_error,
    is_huge_system_instruction,
    load_code_assist_url as antigravity_load_code_assist_url,
    load_credentials as load_antigravity_credentials,
    oauth_redirect_uri as antigravity_oauth_redirect_uri,
    parse_oauth_payload,
    parse_quota_snapshot as parse_antigravity_quota_snapshot,
    persist_credential_fields as persist_antigravity_fields,
    refresh_credentials as refresh_antigravity_credentials,
    responses_to_cloudcode,
    should_refresh as antigravity_should_refresh,
)
from app.providers.oauth_login import (
    LOGIN_NAMES,
    OAUTH_PROVIDERS,
    generate_pkce,
    parse_oauth_callback,
)
from app.providers.workbuddy import (
    WorkBuddyCredentials,
    base_url as workbuddy_base_url,
    chat_to_response as workbuddy_chat_to_response,
    chat_headers as workbuddy_chat_headers,
    common_headers as workbuddy_common_headers,
    load_credentials as load_workbuddy_credentials,
    parse_quota_snapshot as parse_workbuddy_quota_snapshot,
    poll_authorization as poll_workbuddy_authorization,
    prepare_chat_payload as prepare_workbuddy_chat_payload,
    refresh_credentials as refresh_workbuddy_credentials,
    response_sse as workbuddy_response_sse,
    responses_to_chat as workbuddy_responses_to_chat,
    should_refresh as workbuddy_should_refresh,
    start_authorization as start_workbuddy_authorization,
    stream_chat_as_responses as stream_workbuddy_chat_as_responses,
)
from app.providers.codex_catalog import picker_entry, wants_codex_catalog
from app.providers.registry import (
    all_adapters,
    get_adapter,
    is_image_model,
    is_video_model,
    provider_catalog,
    provider_supports_video,
    resolve_image_request_model,
    resolve_video_request_model,
)
from app.providers.video_protocol import (
    OPENAI_CONTENT_VARIANTS,
    grok_content_url,
    grok_edit_video_model,
    grok_result_seconds,
    grok_result_size,
    grok_video_error,
    grok_video_id,
    grok_video_progress,
    map_provider_video_status,
    openai_video_deleted,
    openai_video_list,
    openai_video_object,
    requested_video_fields,
    shape_grok_video_body,
    shape_grok_video_edit_body,
    shape_grok_video_extend_body,
    source_video_id,
    unix_seconds,
    video_download_headers,
)
from app.video_preview import PreviewError, render_video_preview
from app.store.gateway import gateway_store, iso_now, usage_date_today, utc_now

logger = logging.getLogger("transfer_station.errors")

TEXT_RETRY_DELAYS_SECONDS = (5.0, 10.0, 20.0)
GROK_TOOL_CACHE_TTL_SECONDS = 6 * 60 * 60
GROK_TOOL_CACHE_MAX_KEYS = 256
GROK_TOOL_CACHE_MAX_TOOLS = 128
GROK_FAST_AUTH_MODES = frozenset({"oauth"})
USAGE_RANGES = {"day", "week", "month", "all"}
_SENSITIVE_TOKEN_PATTERN = re.compile(r"\b(?:sk|sess)-[A-Za-z0-9_-]{8,}\b")
_MODEL_FAMILY_RE = re.compile(r"^([a-z][a-z0-9]*-\d+(?:\.\d+)?)")
MODEL_FAILURE_WINDOW = dt.timedelta(minutes=30)
MODEL_COOLDOWN = dt.timedelta(minutes=10)
OAUTH_LOGIN_TTL_SECONDS = 600
OAUTH_LOGIN_MAX_PENDING = 8


@dataclass(slots=True)
class _OAuthLoginSession:
    provider: str
    state: str
    redirect_uri: str
    created_at: float
    callback_port: int
    bind_hosts: tuple[str, ...]
    code_verifier: str | None = None
    nonce: str | None = None
    login_mode: str = "loopback"
    device_code: str | None = None
    user_code: str | None = None
    realm: str = "cn"
    workbuddy_client: httpx.AsyncClient | None = None
    status: str = "pending"
    error: str | None = None
    result: dict[str, Any] | None = None


def model_family(model_id: str | None) -> str:
    text = str(model_id or "").strip().lower()
    if not text:
        return ""
    match = _MODEL_FAMILY_RE.match(text)
    return match.group(1) if match else text


def _safe_log_text(value: Any, limit: int = 200) -> str:
    return str(value or "")[:limit].replace("\r", " ").replace("\n", " ")


def _model_from_sse_payload(payload: dict[str, Any]) -> str:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    if isinstance(response, dict):
        model = response.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()[:80]
    return ""


def models_mismatch(request_model: str | None, response_model: str | None) -> bool:
    requested = str(request_model or "").strip()
    returned = str(response_model or "").strip()
    if not requested or not returned:
        return False
    left = model_family(requested)
    right = model_family(returned)
    return bool(left and right and left != right)


async def _retry_pause(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)


def _output_delta_bytes(event: str, data: str) -> int:
    """Bytes of output text actually forwarded to the client.

    Count deltas only. Done events repeat the same text and would double-charge.
    """
    if not data or data == "[DONE]":
        return 0
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    event_type = str(payload.get("type") or event or "")
    if event_type == "response.output_text.delta" and isinstance(payload.get("delta"), str):
        return len(payload["delta"].encode())
    total = 0
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                total += len(delta["content"].encode())
    return total


def _usage_has_billable_fields(usage: dict[str, Any] | None) -> bool:
    return isinstance(usage, dict) and any(
        key in usage
        for key in (
            "input_tokens",
            "prompt_tokens",
            "output_tokens",
            "completion_tokens",
            "images",
            "seconds",
            "hosted_images",
        )
    )


def _usage_from_sse_data(data: str) -> dict[str, Any] | None:
    if not data or data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return usage if isinstance(usage, dict) else None


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str,
        error_type: str = "gateway_error",
        retry_after: float | None = None,
        user_name: str | None = None,
        log_message: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.error_type = error_type
        self.retry_after = retry_after
        self.user_name = user_name
        self.log_message = log_message


def error_event_message(exc: GatewayError) -> str:
    """Return a bounded diagnostic message that cannot retain request or credential data."""
    if exc.log_message is not None:
        message = exc.log_message
    elif exc.code == "upstream_error":
        message = f"Codex upstream request failed (gateway HTTP {exc.status})"
    elif exc.code == "model_not_found":
        message = "Requested model was not found"
    else:
        message = exc.message
    message = _SENSITIVE_TOKEN_PATTERN.sub("[REDACTED]", message)
    return " ".join(message.replace("\r", " ").replace("\n", " ").split())[:300]


@dataclass(slots=True)
class UpstreamLease:
    response: httpx.Response
    account: dict[str, Any]
    semaphore: asyncio.Semaphore
    iterator: AsyncIterator[bytes] | None = None
    buffered_chunks: list[bytes] | None = None
    pending_chunk: asyncio.Task[bytes] | None = None
    closed: bool = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self.buffered_chunks:
            for chunk in self.buffered_chunks:
                yield chunk
            self.buffered_chunks.clear()
        if self.pending_chunk is not None:
            task = self.pending_chunk
            self.pending_chunk = None
            try:
                yield await task
            except StopAsyncIteration:
                return
        iterator = self.iterator or self.response.aiter_bytes()
        self.iterator = iterator
        idle = max(0.0, settings.gateway_stream_idle_timeout_seconds)
        while True:
            try:
                if idle > 0:
                    chunk = await asyncio.wait_for(anext(iterator), timeout=idle)
                else:
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise GatewayError(
                    502,
                    "Upstream stream went idle",
                    code="upstream_stream_timeout",
                    error_type="upstream_error",
                ) from exc
            yield chunk

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.pending_chunk is not None and not self.pending_chunk.done():
            self.pending_chunk.cancel()
        await self.response.aclose()
        self.semaphore.release()


@dataclass(slots=True)
class SyntheticLease:
    chunks: list[bytes]
    account: dict[str, Any] | None = None
    closed: bool = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class WorkBuddyResponsesLease:
    upstream: UpstreamLease
    model: str
    chat_payload: dict[str, Any] | None = None
    run_hosted: Any = None

    @property
    def account(self) -> dict[str, Any]:
        return self.upstream.account

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        hosted_calls: list[dict[str, Any]] = []
        async for chunk in stream_workbuddy_chat_as_responses(
            self.upstream.aiter_bytes(), model=self.model, hosted_calls=hosted_calls
        ):
            yield chunk
        if not hosted_calls or self.run_hosted is None:
            if hosted_calls:
                yield _workbuddy_completed_event(self.model, hosted_calls)
            return
        extra, follow_lease = await self.run_hosted(hosted_calls)
        for event in extra:
            yield event
        if follow_lease is not None:
            try:
                async for chunk in stream_workbuddy_chat_as_responses(
                    follow_lease.aiter_bytes(), model=self.model
                ):
                    yield chunk
            finally:
                await follow_lease.close()
            return
        yield _workbuddy_completed_event(self.model, hosted_calls)

    async def close(self) -> None:
        await self.upstream.close()


@dataclass(slots=True)
class CallContext:
    request_id: str
    key_id: str
    key_name: str
    endpoint: str
    model: str
    is_stream: bool
    started_monotonic: float
    account_id: str | None = None
    attempts: int = 0
    finalized: bool = False
    route_bound: bool = False
    billed_images: int = 0
    delivered_output_bytes: int = 0
    provider: str = "codex"
    text_mode: str = "responses_sse"
    grok_freeform_tools: frozenset[str] = frozenset()
    grok_rewrite: GrokRewriteSpec = field(default_factory=GrokRewriteSpec)
    grok_compact_v2: bool = False
    grok_compact_direct: bool = False
    response_model: str | None = None
    client_model: str = ""
    user_agent: str = ""
    client_version: str = ""
    logged_upstream_events: set[str] = field(default_factory=set)


@dataclass(slots=True)
class TextDispatch:
    lease: UpstreamLease | None
    payload: dict[str, Any]
    stream: bool
    mode: str
    json_body: dict[str, Any] | None = None


class CodexGateway:
    def __init__(self) -> None:
        self.secret = b""
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._route_locks: dict[str, asyncio.Lock] = {}
        self._active_routing: dict[str, asyncio.Task[Any] | None] = {}
        self._quota_locks: dict[str, asyncio.Lock] = {}
        self._quota_reset_locks: dict[str, asyncio.Lock] = {}
        self._quota_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._grok_tool_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._key_touches: dict[str, float] = {}
        self._maintenance_task: asyncio.Task[None] | None = None
        self._routing_heartbeat_task: asyncio.Task[None] | None = None
        self._oauth_lock = asyncio.Lock()
        self._oauth_pending: dict[str, _OAuthLoginSession] = {}
        self._oauth_servers: dict[int, list[asyncio.AbstractServer]] = {}

    async def start(self) -> None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        await gateway_store.start(settings.gateway_db_path())
        from app.console_schema import migrate_console
        from app.wallet_engine import expire_reservations
        await migrate_console()
        await expire_reservations()
        from app.scheduler import load_catalog_models
        await load_catalog_models()
        self.secret = self._load_secret()
        await self._ensure_api_key_routes()
        if not settings.gateway_skip_call_recovery_on_startup:
            await gateway_store.recover_and_prune_calls(settings.gateway_call_retention_days)
        else:
            await gateway_store.prune_admin_sessions()
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="gateway-daily-maintenance"
        )
        self._routing_heartbeat_task = asyncio.create_task(self._routing_heartbeat(), name="routing-lease-heartbeat")
        recovered_at = iso_now()
        await gateway_store.execute(
            "UPDATE billing_requests SET state='released',outcome='process_recovered',updated_at=? "
            "WHERE state='reserved' AND request_id IN ("
            "SELECT request_id FROM call_records WHERE status IN ('failed','interrupted'))",
            (recovered_at,),
        )
        await gateway_store.execute(
            "UPDATE billing_requests SET state='pending',outcome='process_recovered',updated_at=? "
            "WHERE state='reserved' AND request_id IN ("
            "SELECT request_id FROM call_records WHERE status<>'in_progress')",
            (recovered_at,),
        )

    async def stop(self) -> None:
        if self._routing_heartbeat_task is not None:
            self._routing_heartbeat_task.cancel()
            try:
                await self._routing_heartbeat_task
            except asyncio.CancelledError:
                pass
            self._routing_heartbeat_task = None
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        await self._stop_oauth_loopback()
        for session in self._oauth_pending.values():
            if session.workbuddy_client is not None:
                await session.workbuddy_client.aclose()
        self._oauth_pending.clear()
        await gateway_store.stop()
        self._semaphores.clear()
        self._refresh_locks.clear()
        self._route_locks.clear()
        self._active_routing.clear()
        self._quota_locks.clear()
        self._quota_reset_locks.clear()
        self._quota_cache.clear()
        self._grok_tool_cache.clear()
        self._key_touches.clear()

    async def _routing_heartbeat(self) -> None:
        from app.scheduler import maintain
        while True:
            await asyncio.sleep(30)
            try:
                await maintain(self)
            except Exception:
                logger.warning("routing_heartbeat_failed code=storage")

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            from app.wallet_engine import expire_reservations
            await expire_reservations()
            try:
                await self.account_quotas()
            except Exception:
                logger.warning("quota_refresh_failed code=maintenance")
            await gateway_store.prune_calls(settings.gateway_call_retention_days)
            jobs = await gateway_store.all("SELECT v.* FROM video_jobs v JOIN billing_requests b ON b.request_id=v.billing_request_id WHERE b.state='pending' AND v.status NOT IN ('completed','failed') ORDER BY v.created_at LIMIT 10")
            for job in jobs:
                account = await gateway_store.one("SELECT * FROM accounts WHERE account_id=? AND status='active'", (job['account_id'],))
                if account:
                    try:
                        await self._get_grok_video(job['video_id'],job,account,None)
                    except Exception:
                        logger.warning("video_settlement_poll_failed code=upstream")

    async def resolve_text_model(self, body: dict[str, Any], key: dict[str, Any]) -> str:
        hint = await self._preferred_provider(key)
        _provider, model = resolve_provider_model(
            body.get("model") if isinstance(body.get("model"), str) else None,
            default_provider=hint,
            default_model=default_model_for(hint, "text"),
        )
        return model

    async def begin_call(
        self,
        *,
        request_id: str,
        key: dict[str, Any],
        endpoint: str,
        model: str,
        is_stream: bool,
        account_id: str | None = None,
    ) -> CallContext:
        if "/images/" in endpoint:
            hint = await self._preferred_provider(key)
            _provider, model = resolve_image_request_model(model or None, default_provider=hint)
        from app.billing import BillingError
        from app.wallet_engine import reserve

        try:
            await reserve(str(key["id"]), request_id, model, endpoint)
        except BillingError as exc:
            raise GatewayError(
                exc.status,
                exc.message,
                code=exc.code,
                error_type="billing_error",
            ) from exc
        context = CallContext(
            request_id=request_id,
            key_id=str(key["id"]),
            key_name=str(key["name"]),
            endpoint=endpoint,
            model=model,
            is_stream=is_stream,
            started_monotonic=time.monotonic(),
            account_id=account_id,
        )
        await gateway_store.begin_call(
            request_id=request_id,
            key_id=context.key_id,
            key_name=context.key_name,
            endpoint=endpoint,
            model=model,
            is_stream=is_stream,
            account_id=account_id,
        )
        return context

    async def note_attempt(self, context: CallContext | None, account: dict[str, Any]) -> None:
        if context is None:
            return
        await self._prepare_budget(context)
        from app.scheduler import occupy_attempt
        await occupy_attempt(self, context, account)
        context.account_id = str(account["account_id"])
        context.attempts += 1
        await gateway_store.update_call_progress(
            context.request_id,
            account_id=context.account_id,
            model=context.model,
            is_stream=context.is_stream,
            attempts=context.attempts,
        )

    async def _prepare_budget(self, context: CallContext | None, payload: dict | None = None, *, supports_limit: bool = True) -> None:
        if context is None:
            return
        from app.wallet_engine import reserve, constrain_payload
        from app.billing import BillingError
        key = await gateway_store.one("SELECT id FROM api_keys WHERE id=?", (context.key_id,))
        if not key:
            return
        try:
            await reserve(str(key["id"]), context.request_id, context.model, context.endpoint)
            if payload is not None:
                await constrain_payload(context.request_id, payload, supports_limit=supports_limit)
                if any(t.get("type") == "image_generation" for t in payload.get("tools", []) if isinstance(t, dict)):
                    from app.wallet_engine import prepare_image_price
                    await prepare_image_price(context.request_id, canonical_image_model(context.provider))
        except BillingError as exc:
            raise GatewayError(exc.status, exc.message, code=exc.code, error_type="billing_error") from exc

    async def finish_call(
        self,
        context: CallContext,
        *,
        status: str,
        http_status: int,
        error_code: str | None = None,
        completed: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        if context.finalized:
            return
        try:
            if completed:
                returned = completed.get("model")
                if isinstance(returned, str) and returned.strip():
                    context.response_model = returned.strip()
                completed_usage = completed.get("usage")
                if usage is None and isinstance(completed_usage, dict):
                    usage = completed_usage
            if not context.response_model and context.model:
                context.response_model = context.model
            if context.billed_images:
                usage = {**(usage or {}), "hosted_images": context.billed_images}
            if (
                status != "success"
                and not _usage_has_billable_fields(usage)
                and context.delivered_output_bytes > 0
            ):
                # The usage frame never arrived, but the client already received text.
                # Bill that text as one token per byte, capped later by the reservation.
                usage = {"output_tokens": context.delivered_output_bytes}
            await gateway_store.finalize_call(
                request_id=context.request_id,
                ended_at=iso_now(),
                duration_ms=round((time.monotonic() - context.started_monotonic) * 1000),
                account_id=context.account_id,
                model=context.model,
                is_stream=context.is_stream,
                attempts=context.attempts,
                http_status=http_status,
                status=status,
                error_code=error_code,
                usage=usage,
                response_model=context.response_model,
            )
            await gateway_store.note_route_outcome(
                context.account_id, success=status == "success"
            )
            if status == "success" and context.account_id:
                await self._route_success(context)
            elif (
                status == "failed" and context.account_id
                and http_status in {401, 429, 502, 503}
                and error_code in {"account_invalid", "rate_limit_exceeded", "upstream_error",
                                   "upstream_connection_error", "upstream_invalid_response"}
            ):
                await self._route_failure(context.account_id, context)
            from app.wallet_engine import settle
            await settle(context.request_id, usage, status, dispatched=context.attempts > 0)
        finally:
            from app.scheduler import complete_route
            try:
                await complete_route(self, context, completed)
            except Exception:
                logger.warning("routing_lease_release_failed request_id=%s", context.request_id)
            context.finalized = True

    async def _finish_stream(
        self,
        context: CallContext,
        *,
        status: str,
        http_status: int,
        error_code: str | None = None,
        completed: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.shield(
            self.finish_call(
                context,
                status=status,
                http_status=http_status,
                error_code=error_code,
                completed=completed,
                usage=usage,
            )
        )

    def _log_codex_upstream_returned(
        self,
        context: CallContext | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if context is None or context.provider != "codex":
            return
        if event_type not in {"response.created", "response.completed"}:
            return
        if event_type in context.logged_upstream_events:
            return
        context.logged_upstream_events.add(event_type)
        returned_model = _model_from_sse_payload(payload)
        requested_model = context.client_model or context.model
        logger.info(
            "codex_upstream_returned request_id=%s event=%s requested_model=%s "
            "sent_model=%s returned_model=%s model_mismatch=%s",
            context.request_id,
            event_type,
            _safe_log_text(requested_model, 80),
            _safe_log_text(context.model, 80),
            _safe_log_text(returned_model, 80),
            models_mismatch(requested_model, returned_model),
        )

    async def record_error(
        self,
        *,
        level: str,
        category: str,
        code: str,
        user_name: str | None = None,
        method: str | None = None,
        path: str | None = None,
        status: int | None = None,
        message: str = "",
    ) -> None:
        logger.log(
            logging.ERROR if level == "error" else logging.WARNING,
            "gateway_event category=%s code=%s status=%s user=%r method=%s path=%s message=%r",
            category,
            code,
            status,
            user_name,
            method,
            path,
            message[:300],
        )
        try:
            await gateway_store.record_error_event(
                capacity=settings.gateway_error_ring_capacity,
                level=level,
                category=category,
                code=code,
                user_name=user_name[:100] if user_name else None,
                method=method,
                path=path,
                status=status,
                message=message[:300],
            )
        except Exception:
            logger.exception("error_ring_write_failed category=%s code=%s", category, code)

    async def recent_errors(self, limit: int = 50) -> list[dict[str, Any]]:
        return await gateway_store.recent_error_events(limit)

    def _load_secret(self) -> bytes:
        configured = settings.routing_secret.strip()
        if configured:
            return configured.encode()
        path = settings.data_dir / "routing.secret"
        if path.is_file():
            return path.read_bytes().strip()
        value = secrets.token_urlsafe(48).encode()
        path.write_bytes(value)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return value

    def digest(self, value: str) -> str:
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def _api_key_encryption_key(self) -> bytes:
        return hmac.new(
            self.secret, b"transfer-station/api-key-encryption/v1", hashlib.sha256
        ).digest()

    def _encrypt_api_key(self, raw: str) -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._api_key_encryption_key()).encrypt(
            nonce, raw.encode(), b"transfer-station-api-key"
        )
        return "v1." + base64.urlsafe_b64encode(nonce + ciphertext).decode().rstrip("=")

    def _decrypt_api_key(self, envelope: Any) -> str | None:
        if not isinstance(envelope, str) or not envelope.startswith("v1."):
            return None
        try:
            encoded = envelope[3:]
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            return AESGCM(self._api_key_encryption_key()).decrypt(
                payload[:12], payload[12:], b"transfer-station-api-key"
            ).decode()
        except (InvalidTag, ValueError, UnicodeDecodeError):
            return None

    async def _ensure_api_key_routes(self) -> None:
        """Legacy hook: keys no longer pin a preferred upstream account."""
        return

    async def import_accounts(
        self, files: list[UploadFile], *, provider: str = "codex"
    ) -> dict[str, Any]:
        if not files:
            raise GatewayError(422, "At least one auth.json file is required", code="files_required")
        if len(files) > settings.gateway_max_import_files:
            raise GatewayError(413, "Too many files in one batch", code="too_many_files")
        provider_id = (provider or "codex").strip().lower() or "codex"
        try:
            adapter = get_adapter(provider_id)
        except KeyError as exc:
            raise GatewayError(422, f"Unknown provider '{provider_id}'", code="unknown_provider") from exc
        if not adapter.ready and provider_id != "antigravity":
            raise GatewayError(501, f"{adapter.display_name} is not ready", code="provider_not_ready")
        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for uploaded in files:
            filename = uploaded.filename or "auth.json"
            try:
                raw = await uploaded.read(settings.gateway_max_auth_file_bytes + 1)
                if len(raw) > settings.gateway_max_auth_file_bytes:
                    raise ValueError("file exceeds 200KB")
                payload = json.loads(raw.decode("utf-8-sig"))
                imported = adapter.parse_import(payload, filename)
                for item in imported:
                    stored = dict(item.payload)
                    if item.source_path:
                        stored["source_path"] = item.source_path
                    path = settings.gateway_credential_dir() / f"{self.digest(item.account_id)[:32]}.json"
                    self._atomic_credential_write(path, stored)
                    is_created = await gateway_store.upsert_account(
                        item.account_id,
                        path,
                        item.expires_at,
                        provider=provider_id,
                        label=item.label,
                        auth_mode=item.auth_mode,
                        capabilities=json.dumps(list(item.capabilities)),
                    )
                    public = self._public_account(
                        {
                            "account_id": item.account_id,
                            "status": "active",
                            "expires_at": item.expires_at,
                            "provider": provider_id,
                            "label": item.label,
                            "auth_mode": item.auth_mode,
                            "capabilities": json.dumps(list(item.capabilities)),
                        }
                    )
                    (created if is_created else updated).append(public)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, HTTPException) as exc:
                message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                failed.append({"filename": filename, "error": message})
                logger.warning(
                    "account_import_failed filename=%s provider=%s error_type=%s",
                    filename,
                    provider_id,
                    type(exc).__name__,
                )
                await self.record_error(
                    level="warning",
                    category="account_import",
                    code="account_import_failed",
                    user_name="admin",
                    method="POST",
                    path="/admin/api/accounts/import",
                    status=422,
                    message=f"{adapter.display_name} account import failed",
                )
            finally:
                await uploaded.close()
        return {"created": created, "updated": updated, "failed": failed, "generated_api_key": None}

    async def import_local(self, provider: str = "grok") -> dict[str, Any]:
        if not settings.gateway_import_local_enabled:
            raise GatewayError(403, "Local credential import is disabled", code="import_local_disabled")
        provider_id = (provider or "grok").strip().lower() or "grok"
        if provider_id == "antigravity":
            return await self._import_local_antigravity()
        if provider_id != "grok":
            raise GatewayError(
                422,
                "Local import currently supports only Grok and Antigravity",
                code="unsupported_local_provider",
            )
        path = local_auth_path()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise GatewayError(
                404,
                "Local Grok auth.json was not found",
                code="local_auth_missing",
            ) from exc
        if len(raw) > settings.gateway_max_auth_file_bytes:
            raise GatewayError(413, "Local auth.json exceeds 200KB", code="file_too_large")
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
            adapter = get_adapter("grok")
            imported = adapter.parse_import(payload, str(path))
        except (UnicodeDecodeError, json.JSONDecodeError, HTTPException) as exc:
            message = str(exc.detail) if isinstance(exc, HTTPException) else "Local auth.json is invalid"
            raise GatewayError(422, message, code="invalid_local_auth") from exc
        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        for item in imported:
            stored = dict(item.payload)
            stored["source_path"] = str(path)
            credential_path = settings.gateway_credential_dir() / f"{self.digest(item.account_id)[:32]}.json"
            self._atomic_credential_write(credential_path, stored)
            is_created = await gateway_store.upsert_account(
                item.account_id,
                credential_path,
                item.expires_at,
                provider="grok",
                label=item.label,
                auth_mode=item.auth_mode,
                capabilities=json.dumps(list(item.capabilities)),
            )
            public = self._public_account(
                {
                    "account_id": item.account_id,
                    "status": "active",
                    "expires_at": item.expires_at,
                    "provider": "grok",
                    "label": item.label,
                    "auth_mode": item.auth_mode,
                    "capabilities": json.dumps(list(item.capabilities)),
                }
            )
            (created if is_created else updated).append(public)
        return {"created": created, "updated": updated, "failed": [], "generated_api_key": None}

    async def _import_local_antigravity(self) -> dict[str, Any]:
        from app.providers.antigravity import discover_local_accounts

        imported = discover_local_accounts()
        if not imported:
            raise GatewayError(
                404,
                "No Antigravity oauth JSON was found under ~/.gemini/antigravity, Antigravity AppData, or Windows credential gemini:antigravity. Import a JSON file instead.",
                code="local_auth_missing",
            )
        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        for item in imported:
            stored = dict(item.payload)
            if item.source_path:
                stored["source_path"] = item.source_path
            credential_path = settings.gateway_credential_dir() / f"{self.digest(item.account_id)[:32]}.json"
            self._atomic_credential_write(credential_path, stored)
            is_created = await gateway_store.upsert_account(
                item.account_id,
                credential_path,
                item.expires_at,
                provider="antigravity",
                label=item.label,
                auth_mode=item.auth_mode,
                capabilities=json.dumps(list(item.capabilities)),
            )
            public = self._public_account(
                {
                    "account_id": item.account_id,
                    "status": "active",
                    "expires_at": item.expires_at,
                    "provider": "antigravity",
                    "label": item.label,
                    "auth_mode": item.auth_mode,
                    "capabilities": json.dumps(list(item.capabilities)),
                }
            )
            (created if is_created else updated).append(public)
        return {"created": created, "updated": updated, "failed": [], "generated_api_key": None}

    @staticmethod
    def _admin_is_local(request_host: str | None) -> bool:
        host = (request_host or "").split("%")[0].strip().strip("[]").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    @staticmethod
    def _oauth_label(provider: str) -> str:
        return LOGIN_NAMES.get(provider, provider)

    def _oauth_bind_spec(self, provider: str) -> tuple[int, tuple[str, ...], str]:
        if provider == "codex":
            return CODEX_OAUTH_CALLBACK_PORT, CODEX_OAUTH_BIND_HOSTS, codex_oauth_redirect_uri()
        if provider == "grok":
            return GROK_OAUTH_CALLBACK_PORT, GROK_OAUTH_BIND_HOSTS, grok_oauth_redirect_uri()
        if provider == "workbuddy":
            return 0, (), ""
        return (
            ANTIGRAVITY_OAUTH_CALLBACK_PORT,
            ANTIGRAVITY_OAUTH_BIND_HOSTS,
            antigravity_oauth_redirect_uri(),
        )

    async def start_oauth(
        self, provider: str, *, request_host: str | None = None, realm: str = "cn"
    ) -> dict[str, Any]:
        provider_id = (provider or "").strip().lower()
        if provider_id not in OAUTH_PROVIDERS:
            raise GatewayError(422, "Unknown login provider", code="unsupported_oauth_provider")
        get_adapter(provider_id)
        label = self._oauth_label(provider_id)
        now = time.time()
        port, hosts, redirect_uri = self._oauth_bind_spec(provider_id)
        state = secrets.token_urlsafe(32)
        code_verifier = None
        nonce = None
        login_mode = "loopback"
        user_code = None
        device_code = None
        workbuddy_client = None
        expires_in = OAUTH_LOGIN_TTL_SECONDS
        if provider_id == "codex":
            code_verifier, challenge = generate_pkce()
            auth_url = build_codex_oauth_auth_url(
                state, code_challenge=challenge, redirect_uri=redirect_uri
            )
        elif provider_id == "grok":
            device = await start_grok_device_authorization()
            login_mode = "device"
            auth_url = str(device["verification_url"])
            user_code = str(device["user_code"])
            device_code = str(device["device_code"])
            expires_in = min(OAUTH_LOGIN_TTL_SECONDS, int(device["expires_in"]))
        elif provider_id == "antigravity":
            auth_url = build_antigravity_oauth_auth_url(state, redirect_uri=redirect_uri)
        else:
            try:
                state, auth_url, workbuddy_client = await start_workbuddy_authorization(realm=realm)
            except (HTTPException, httpx.HTTPError) as exc:
                raise GatewayError(502, "WorkBuddy login could not start", code="oauth_failed") from exc
            login_mode = "poll"
            port, hosts, redirect_uri = 0, (), ""
        async with self._oauth_lock:
            self._prune_oauth_sessions_locked(now)
            if len(self._oauth_pending) >= OAUTH_LOGIN_MAX_PENDING:
                if workbuddy_client is not None:
                    await workbuddy_client.aclose()
                raise GatewayError(
                    429,
                    f"Too many {label} login attempts are waiting. Finish or cancel one first.",
                    code="oauth_busy",
                )
            self._oauth_pending[state] = _OAuthLoginSession(
                provider=provider_id,
                state=state,
                redirect_uri=redirect_uri,
                created_at=now,
                callback_port=port,
                bind_hosts=hosts,
                code_verifier=code_verifier,
                nonce=nonce,
                login_mode=login_mode,
                device_code=device_code,
                user_code=user_code,
                realm=realm,
                workbuddy_client=workbuddy_client,
            )
        loopback_bound = False
        if login_mode == "loopback":
            loopback_bound = await self._ensure_oauth_loopback(port, hosts)
        return {
            "provider": provider_id,
            "auth_url": auth_url,
            "state": state,
            "redirect_uri": redirect_uri if login_mode == "loopback" else None,
            "listen_bound": bool(loopback_bound and self._admin_is_local(request_host)),
            "login_mode": login_mode,
            "user_code": user_code,
            "expires_in": expires_in,
        }

    async def start_antigravity_oauth(self, *, request_host: str | None = None) -> dict[str, Any]:
        return await self.start_oauth("antigravity", request_host=request_host)

    async def oauth_status(self, state: str) -> dict[str, Any]:
        token = (state or "").strip()
        if not token:
            raise GatewayError(422, "Missing login state", code="oauth_state_required")
        now = time.time()
        async with self._oauth_lock:
            self._prune_oauth_sessions_locked(now)
            session = self._oauth_pending.get(token)
            if session is None:
                raise GatewayError(410, "Login expired. Start again.", code="oauth_expired")
            if (
                session.provider == "grok"
                and session.login_mode == "device"
                and session.status == "pending"
                and session.device_code
            ):
                session.status = "running"
                device_code = session.device_code
                workbuddy_client = None
                workbuddy_realm = "cn"
            elif (
                session.provider == "workbuddy"
                and session.login_mode == "poll"
                and session.status == "pending"
                and session.workbuddy_client is not None
            ):
                session.status = "running"
                device_code = None
                workbuddy_client = session.workbuddy_client
                workbuddy_realm = session.realm
            else:
                return self._public_oauth_session(session)
        if workbuddy_client is not None:
            try:
                payload = await poll_workbuddy_authorization(
                    token, workbuddy_client, realm=workbuddy_realm
                )
            except HTTPException as exc:
                message = str(exc.detail) if exc.detail else "WorkBuddy login failed"
                await self._fail_oauth_session(token, message)
                raise GatewayError(exc.status_code, message, code="oauth_failed") from exc
            except httpx.HTTPError as exc:
                await self._fail_oauth_session(token, "WorkBuddy login status is unavailable")
                raise GatewayError(502, "WorkBuddy login status is unavailable", code="oauth_failed") from exc
            if payload is None:
                async with self._oauth_lock:
                    session = self._oauth_pending.get(token)
                    if session is None:
                        raise GatewayError(410, "Login expired. Start again.", code="oauth_expired")
                    session.status = "pending"
                    return self._public_oauth_session(session)
            try:
                try:
                    imported = get_adapter("workbuddy").parse_import(payload, "oauth")
                except ValueError as exc:
                    await self._fail_oauth_session(token, "WorkBuddy login returned no account")
                    raise GatewayError(
                        502, "WorkBuddy login returned no account", code="oauth_failed"
                    ) from exc
                return await self._finish_oauth_import(token, "workbuddy", imported)
            finally:
                await workbuddy_client.aclose()
        try:
            payload = await exchange_grok_device_authorization(device_code)
        except HTTPException as exc:
            message = str(exc.detail) if exc.detail else "Grok login failed"
            await self._fail_oauth_session(token, message)
            if exc.status_code == 410:
                raise GatewayError(410, message, code="oauth_expired") from exc
            raise GatewayError(exc.status_code or 422, message, code="oauth_failed") from exc
        except Exception as exc:
            await self._fail_oauth_session(token, "Grok login failed")
            raise GatewayError(422, "Grok login failed", code="oauth_failed") from exc
        if payload is None:
            async with self._oauth_lock:
                session = self._oauth_pending.get(token)
                if session is not None and session.status == "running":
                    session.status = "pending"
                if session is None:
                    raise GatewayError(410, "Login expired. Start again.", code="oauth_expired")
                return self._public_oauth_session(session)
        imported = get_adapter("grok").parse_import(payload, "oauth")
        return await self._finish_oauth_import(token, "grok", imported)

    async def antigravity_oauth_status(self, state: str) -> dict[str, Any]:
        return await self.oauth_status(state)

    async def complete_oauth(
        self,
        *,
        state: str,
        code: str | None = None,
        callback_url: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        parsed = parse_oauth_callback(callback_url or "")
        token_state = (parsed.get("state") or state or "").strip()
        token_code = (parsed.get("code") or code or "").strip()
        oauth_error = parsed.get("error")
        if not token_state:
            raise GatewayError(422, "Missing login state", code="oauth_state_required")
        now = time.time()
        completed: dict[str, Any] | None = None
        redirect_uri = ""
        code_verifier: str | None = None
        async with self._oauth_lock:
            self._prune_oauth_sessions_locked(now)
            session = self._oauth_pending.get(token_state)
            if session is None:
                raise GatewayError(410, "Login expired. Start again.", code="oauth_expired")
            provider_id = session.provider
            if provider and provider.strip().lower() not in {"", provider_id}:
                raise GatewayError(422, "Login provider does not match this attempt", code="oauth_provider_mismatch")
            label = self._oauth_label(provider_id)
            if session.status == "completed" and session.result is not None:
                completed = session.result
            elif session.status == "failed":
                raise GatewayError(422, session.error or f"{label} login failed", code="oauth_failed")
            elif oauth_error:
                session.status = "failed"
                session.error = f"{label} login was cancelled or not authorized"
                raise GatewayError(422, session.error, code="oauth_denied")
            elif not token_code:
                raise GatewayError(
                    422,
                    "Paste the full localhost address from the browser after login",
                    code="oauth_code_required",
                )
            elif session.status == "running":
                raise GatewayError(409, f"{label} login is already completing", code="oauth_busy")
            else:
                session.status = "running"
                redirect_uri = session.redirect_uri
                code_verifier = session.code_verifier
        if completed is not None:
            restored = await self._restore_deleted_oauth_accounts(completed)
            if restored:
                logger.info(
                    "oauth_login_restored provider=%s accounts=%s",
                    provider_id,
                    restored,
                )
            return self._public_oauth_session(session)
        try:
            imported = await self._oauth_imported_accounts(
                provider_id,
                code=token_code,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
            )
        except HTTPException as exc:
            message = str(exc.detail) if exc.detail else f"{self._oauth_label(provider_id)} login failed"
            await self._fail_oauth_session(token_state, message)
            raise GatewayError(exc.status_code or 422, message, code="oauth_failed") from exc
        except GatewayError:
            raise
        except Exception as exc:
            await self._fail_oauth_session(token_state, f"{self._oauth_label(provider_id)} login failed")
            raise GatewayError(
                422, f"{self._oauth_label(provider_id)} login failed", code="oauth_failed"
            ) from exc
        return await self._finish_oauth_import(token_state, provider_id, imported)

    async def _restore_deleted_oauth_accounts(self, result: dict[str, Any]) -> int:
        """Put a deleted account back on the list when the same login is submitted again."""
        rows = list(result.get("created") or []) + list(result.get("updated") or [])
        account_ids = [
            str(item["account_id"])
            for item in rows
            if isinstance(item, dict) and item.get("account_id")
        ]
        restored = 0
        now = iso_now()
        for account_id in account_ids:
            path = settings.gateway_credential_dir() / f"{self.digest(account_id)[:32]}.json"
            if not path.is_file():
                continue
            restored += await gateway_store.execute(
                """UPDATE accounts
                   SET status='active', cooldown_until=NULL, network_failures=0, updated_at=?
                   WHERE account_id=? AND status='deleted'""",
                (now, account_id),
            )
        return restored

    async def _finish_oauth_import(
        self,
        token_state: str,
        provider_id: str,
        imported: list[ImportedAccount],
    ) -> dict[str, Any]:
        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        try:
            for item in imported:
                public, is_created = await self._store_imported_account(item, provider_id=provider_id)
                (created if is_created else updated).append(public)
        except Exception as exc:
            await self._fail_oauth_session(token_state, f"{self._oauth_label(provider_id)} login failed")
            raise GatewayError(
                422, f"{self._oauth_label(provider_id)} login failed", code="oauth_failed"
            ) from exc
        result = {
            "created": created,
            "updated": updated,
            "failed": [],
            "generated_api_key": None,
        }
        login_mode = "loopback"
        async with self._oauth_lock:
            session = self._oauth_pending.get(token_state)
            if session is not None:
                session.status = "completed"
                session.error = None
                session.result = result
                session.device_code = None
                login_mode = session.login_mode
        logger.info(
            "oauth_login_completed provider=%s created=%s updated=%s",
            provider_id,
            len(created),
            len(updated),
        )
        return {
            **result,
            "provider": provider_id,
            "status": "completed",
            "login_mode": login_mode,
            "listen_bound": bool(self._oauth_servers.get(self._oauth_bind_spec(provider_id)[0])),
        }

    async def complete_antigravity_oauth(
        self,
        *,
        state: str,
        code: str | None = None,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        return await self.complete_oauth(state=state, code=code, callback_url=callback_url, provider="antigravity")

    async def _oauth_imported_accounts(
        self,
        provider_id: str,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
    ) -> list[ImportedAccount]:
        if provider_id == "codex":
            payload = await exchange_codex_authorization_code(
                code, redirect_uri=redirect_uri, code_verifier=code_verifier or ""
            )
            return get_adapter("codex").parse_import(payload, "oauth")
        if provider_id == "grok":
            payload = await exchange_grok_authorization_code(
                code, redirect_uri=redirect_uri, code_verifier=code_verifier or ""
            )
            return get_adapter("grok").parse_import(payload, "oauth")
        payload = await exchange_antigravity_authorization_code(code, redirect_uri=redirect_uri)
        return parse_oauth_payload(payload, filename="google-oauth")

    async def _store_imported_account(
        self, item: ImportedAccount, *, provider_id: str
    ) -> tuple[dict[str, Any], bool]:
        stored = dict(item.payload)
        if item.source_path:
            stored["source_path"] = item.source_path
        path = settings.gateway_credential_dir() / f"{self.digest(item.account_id)[:32]}.json"
        self._atomic_credential_write(path, stored)
        is_created = await gateway_store.upsert_account(
            item.account_id,
            path,
            item.expires_at,
            provider=provider_id,
            label=item.label,
            auth_mode=item.auth_mode,
            capabilities=json.dumps(list(item.capabilities)),
        )
        public = self._public_account(
            {
                "account_id": item.account_id,
                "status": "active",
                "expires_at": item.expires_at,
                "provider": provider_id,
                "label": item.label,
                "auth_mode": item.auth_mode,
                "capabilities": json.dumps(list(item.capabilities)),
            }
        )
        return public, is_created

    async def export_account(self, account_id: str) -> dict[str, Any]:
        row = await gateway_store.one(
            "SELECT account_id, provider, label, credential_path, status FROM accounts WHERE account_id=?",
            (account_id,),
        )
        if row is None or row["status"] == "deleted":
            raise GatewayError(404, "Account not found", code="account_not_found")
        path = Path(str(row["credential_path"]))
        try:
            raw = path.read_text(encoding="utf-8-sig")
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError(404, "Account credential file was not found", code="credential_missing") from exc
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.pop("source_path", None)
        provider = account_provider(row)
        label = row.get("label") if isinstance(row.get("label"), str) else account_id
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(label or provider)).strip("-")[:40] or provider
        return {
            "filename": f"{provider}-{safe}.json",
            "provider": provider,
            "account_id": account_id,
            "data": payload,
        }

    def _prune_oauth_sessions_locked(self, now: float) -> None:
        expired = [
            key
            for key, session in self._oauth_pending.items()
            if now - session.created_at > OAUTH_LOGIN_TTL_SECONDS
        ]
        for key in expired:
            session = self._oauth_pending.pop(key, None)
            if session is not None and session.workbuddy_client is not None:
                asyncio.create_task(session.workbuddy_client.aclose())

    def _public_oauth_session(self, session: _OAuthLoginSession) -> dict[str, Any]:
        result = session.result or {}
        return {
            "provider": session.provider,
            "status": session.status,
            "created": list(result.get("created") or []),
            "updated": list(result.get("updated") or []),
            "failed": list(result.get("failed") or []),
            "error": session.error,
            "listen_bound": bool(self._oauth_servers.get(session.callback_port)),
            "login_mode": session.login_mode,
            "user_code": session.user_code,
            "expires_in": max(
                0, int(OAUTH_LOGIN_TTL_SECONDS - (time.time() - session.created_at))
            ),
        }

    async def _fail_oauth_session(self, state: str, message: str) -> None:
        async with self._oauth_lock:
            session = self._oauth_pending.get(state)
            if session is None or session.status == "completed":
                return
            session.status = "failed"
            session.error = message

    async def _ensure_oauth_loopback(self, port: int, hosts: tuple[str, ...]) -> bool:
        async with self._oauth_lock:
            existing = self._oauth_servers.get(int(port))
            if existing:
                return True
            bound: list[asyncio.AbstractServer] = []
            for host in hosts:
                try:
                    server = await asyncio.start_server(
                        self._oauth_loopback_client,
                        host=host,
                        port=int(port),
                    )
                except OSError:
                    continue
                bound.append(server)
            if bound:
                self._oauth_servers[int(port)] = bound
                return True
            return False

    async def _stop_oauth_loopback(self) -> None:
        groups = self._oauth_servers
        self._oauth_servers = {}
        for servers in groups.values():
            for server in servers:
                server.close()
                try:
                    await server.wait_closed()
                except OSError:
                    pass

    async def _oauth_loopback_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status = 200
        page = "登录已完成，可以关闭此窗口，回到管理台。"
        try:
            raw = await asyncio.wait_for(reader.read(8192), timeout=5)
            head = raw.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            parts = head.split(" ")
            target = parts[1] if len(parts) >= 2 else "/"
            parsed = parse_oauth_callback("http://127.0.0.1" + target)
            if parsed.get("error"):
                status = 400
                page = "登录未完成，请回到管理台重试。"
                await self._fail_oauth_session(
                    parsed.get("state") or "",
                    "Login was cancelled or not authorized",
                )
            elif parsed.get("code") and parsed.get("state"):
                try:
                    await self.complete_oauth(state=parsed["state"], code=parsed["code"])
                except GatewayError:
                    status = 400
                    page = "登录结果未能写入账号，请回到管理台把地址栏网址贴回去。"
            else:
                status = 404
                page = "未收到登录回调。"
        except Exception:
            status = 400
            page = "登录回调处理失败，请回到管理台把地址栏网址贴回去。"
        body = (
            "<!doctype html><meta charset=utf-8><title>上游账号登录</title>"
            f"<p>{page}</p>"
        ).encode("utf-8")
        try:
            writer.write(
                (
                    f"HTTP/1.1 {status} {'OK' if status == 200 else 'ERROR'}\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    "Connection: close\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode("ascii")
                + body
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _atomic_credential_write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)

    async def create_api_key(
        self,
        name: str,
        preferred_account_id: str | None = None,
        fast_enabled: bool = False,
        routes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del preferred_account_id, routes
        raw = f"sk-ts-{secrets.token_urlsafe(32)}"
        key_id = uuid.uuid4().hex
        prefix = raw[:12]
        now = iso_now()

        await gateway_store.execute(
            """INSERT INTO api_keys(
                   id,name,key_prefix,fingerprint,status,created_at,key_ciphertext,fast_enabled,usd_credit
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                key_id, name.strip() or "unnamed", prefix, self.digest(raw),
                "active", now, self._encrypt_api_key(raw), int(fast_enabled), "0.00",
            ),
        )
        return {
            "id": key_id, "name": name.strip() or "unnamed", "prefix": prefix, "key": raw,
            "preferred_account_id": None,
            "active_account_id": None,
            "fast_enabled": bool(fast_enabled),
            "routes": [],
        }

    async def authenticate_key(self, raw: str) -> dict[str, Any]:
        if not raw:
            raise GatewayError(
                401,
                "Missing API key",
                code="invalid_api_key",
                error_type="authentication_error",
                log_message="No API key was supplied in Authorization Bearer or X-API-Key",
            )
        row = await gateway_store.one(
            "SELECT * FROM api_keys WHERE fingerprint=?", (self.digest(raw),)
        )
        if row is None:
            raise GatewayError(
                401,
                "Invalid API key",
                code="invalid_api_key",
                error_type="authentication_error",
                log_message="No active API key matched the supplied authentication headers",
            )
        if row["status"] != "active":
            status_message = (
                "API key is disabled" if row["status"] == "disabled" else "API key has been removed"
            )
            raise GatewayError(
                401,
                "Invalid API key",
                code="invalid_api_key",
                error_type="authentication_error",
                user_name=str(row["name"]),
                log_message=status_message,
            )
        key_id = str(row["id"])
        if self._touch_due(self._key_touches, key_id):
            await gateway_store.execute(
                "UPDATE api_keys SET last_used_at=? WHERE id=?", (iso_now(), key_id)
            )
        return row

    async def list_accounts(self) -> list[dict[str, Any]]:
        rows = await gateway_store.all(
            """
            SELECT a.*,
              COALESCE((SELECT SUM(total_tokens) FROM usage_daily u WHERE u.account_id=a.account_id),0)
                AS total_tokens,
              COALESCE((SELECT COUNT(*) FROM api_key_routes r JOIN api_keys k ON k.id=r.key_id
                        WHERE r.preferred_account_id=a.account_id AND k.status='active'),0)
                AS preferred_keys,
              COALESCE((SELECT COUNT(*) FROM api_key_routes r JOIN api_keys k ON k.id=r.key_id
                        WHERE r.active_account_id=a.account_id AND k.status='active'),0)
                AS routed_keys
            FROM accounts a
            WHERE a.status!='deleted'
            ORDER BY created_at
            """
        )
        return [self._public_account(row) for row in rows]

    @staticmethod
    def _quota_window(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        used = value.get("used_percent")
        seconds = value.get("limit_window_seconds")
        resets_at = value.get("reset_at")
        try:
            used_percent = max(0.0, min(100.0, float(used)))
        except (TypeError, ValueError):
            used_percent = 0.0
        try:
            window_minutes = max(0, round(float(seconds) / 60))
        except (TypeError, ValueError):
            window_minutes = None
        try:
            reset_timestamp = int(resets_at)
        except (TypeError, ValueError):
            reset_timestamp = None
        return {
            "used_percent": used_percent,
            "remaining_percent": max(0.0, 100.0 - used_percent),
            "window_minutes": window_minutes,
            "resets_at": reset_timestamp,
        }

    @classmethod
    def _quota_limit(
        cls,
        *,
        limit_id: str,
        limit_name: str | None,
        rate_limit: Any,
        credits: Any = None,
        spend_control: Any = None,
    ) -> dict[str, Any]:
        details = rate_limit if isinstance(rate_limit, dict) else {}
        credit_details = credits if isinstance(credits, dict) else {}
        spend_details = spend_control if isinstance(spend_control, dict) else {}
        individual = spend_details.get("individual_limit")
        if not isinstance(individual, dict):
            individual = None
        return {
            "limit_id": limit_id,
            "limit_name": limit_name,
            "primary": cls._quota_window(details.get("primary_window")),
            "secondary": cls._quota_window(details.get("secondary_window")),
            "credits": {
                "has_credits": bool(credit_details.get("has_credits")),
                "unlimited": bool(credit_details.get("unlimited")),
                "balance": credit_details.get("balance"),
            } if credit_details else None,
            "spend_control_reached": spend_details.get("reached")
            if isinstance(spend_details.get("reached"), bool) else None,
            "individual_limit": {
                "limit": individual.get("limit"),
                "used": individual.get("used"),
                "remaining_percent": individual.get("remaining_percent"),
                "resets_at": individual.get("reset_at"),
            } if individual else None,
        }

    @staticmethod
    def _quota_reset_credits(summary: Any, details: Any) -> dict[str, Any]:
        summary_data = summary if isinstance(summary, dict) else {}
        detail_data = details if isinstance(details, dict) else {}
        credits: list[dict[str, Any]] = []
        for item in detail_data.get("credits") or []:
            if not isinstance(item, dict):
                continue
            credits.append({
                "id": str(item.get("id") or ""),
                "reset_type": str(item.get("reset_type") or "unknown"),
                "status": str(item.get("status") or "unknown"),
                "granted_at": item.get("granted_at"),
                "expires_at": item.get("expires_at"),
                "title": item.get("title"),
                "description": item.get("description"),
            })
        count = detail_data.get("available_count", summary_data.get("available_count", 0))
        try:
            available_count = max(0, int(count))
        except (TypeError, ValueError):
            available_count = 0
        return {"available_count": available_count, "credits": credits}

    @staticmethod
    def _quota_next_reset(limits: list[dict[str, Any]]) -> int | None:
        candidates: list[int] = []
        for item in limits:
            for name in ("primary", "secondary"):
                window = item.get(name)
                if isinstance(window, dict) and isinstance(window.get("resets_at"), int):
                    candidates.append(window["resets_at"])
            individual = item.get("individual_limit")
            if isinstance(individual, dict):
                try:
                    candidates.append(int(individual["resets_at"]))
                except (KeyError, TypeError, ValueError):
                    pass
        if not candidates:
            return None
        now = int(dt.datetime.now(dt.UTC).timestamp())
        future = [value for value in candidates if value >= now]
        return min(future or candidates)

    async def _quota_json(
        self,
        client: httpx.AsyncClient,
        credentials: CodexCredentials,
        path: str,
        *,
        optional: bool = False,
    ) -> dict[str, Any] | None:
        url = f"{settings.codex_chatgpt_backend_url.rstrip('/')}{path}"
        try:
            response = await client.get(
                url,
                headers=build_headers(credentials, accept="application/json"),
                timeout=httpx.Timeout(
                    settings.gateway_quota_timeout_seconds,
                    connect=min(10.0, settings.gateway_quota_timeout_seconds),
                    pool=settings.gateway_pool_timeout_seconds,
                ),
            )
        except httpx.TimeoutException as exc:
            raise GatewayError(504, "Codex quota query timed out", code="quota_timeout") from exc
        except httpx.HTTPError as exc:
            raise GatewayError(502, "Cannot reach Codex quota service", code="quota_unavailable") from exc
        if optional and response.status_code == 404:
            return None
        if response.status_code >= 400:
            status = 401 if response.status_code == 401 else 502
            code = "quota_unauthorized" if response.status_code == 401 else "quota_upstream_error"
            raise GatewayError(status, "Codex quota service rejected the request", code=code)
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise GatewayError(502, "Codex quota service returned invalid JSON", code="quota_invalid") from exc
        if not isinstance(body, dict):
            raise GatewayError(502, "Codex quota service returned invalid data", code="quota_invalid")
        return body

    async def _fetch_account_quota(
        self, account: dict[str, Any], client: httpx.AsyncClient
    ) -> dict[str, Any]:
        credentials = await self.credentials(account, client=client)
        usage = await self._quota_json(client, credentials, "/wham/usage")
        assert usage is not None
        reset_details: dict[str, Any] | None = None
        reset_details_error: str | None = None
        try:
            reset_details = await self._quota_json(
                client, credentials, "/wham/rate-limit-reset-credits", optional=True
            )
        except GatewayError as exc:
            reset_details_error = exc.code

        limits = [self._quota_limit(
            limit_id="codex",
            limit_name=None,
            rate_limit=usage.get("rate_limit"),
            credits=usage.get("credits"),
            spend_control=usage.get("spend_control"),
        )]
        reset_credits = self._quota_reset_credits(
            usage.get("rate_limit_reset_credits"), reset_details
        )
        if reset_details_error:
            reset_credits["error"] = reset_details_error
        return {
            "plan_type": str(usage.get("plan_type") or "unknown"),
            "limits": limits,
            "next_reset_at": self._quota_next_reset(limits),
            "reset_credits": reset_credits,
            "fetched_at": iso_now(),
            "stale": False,
            "error": None,
        }

    async def _account_quota(
        self,
        account: dict[str, Any],
        client: httpx.AsyncClient,
        *,
        refresh: bool,
    ) -> dict[str, Any]:
        account_id = str(account["account_id"])
        base = {
            "account_id": account_id,
            "label": self._public_account(account)["label"],
            "provider": account_provider(account),
            "status": account.get("status"),
            "local_total_tokens": int(account.get("total_tokens") or 0),
        }
        cached = self._quota_cache.get(account_id)
        max_age = max(1, settings.gateway_quota_cache_seconds)
        if not refresh and cached and time.monotonic() - cached[0] < max_age:
            return {**base, **cached[1], "cached": True}
        lock = self._quota_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            cached = self._quota_cache.get(account_id)
            if not refresh and cached and time.monotonic() - cached[0] < max_age:
                return {**base, **cached[1], "cached": True}
            provider = account_provider(account)
            if provider == "grok":
                try:
                    quota = await self._fetch_grok_account_quota(account, client)
                except GatewayError as exc:
                    return {
                        **base,
                        **self._quota_failure(cached, exc, message="额度获取失败"),
                        "cached": bool(cached),
                        "provider": provider,
                    }
            elif provider == "antigravity":
                try:
                    quota = await self._fetch_antigravity_account_quota(account, client)
                except GatewayError as exc:
                    return {
                        **base,
                        **self._quota_failure(cached, exc),
                        "cached": bool(cached),
                        "provider": provider,
                    }
            elif provider == "workbuddy":
                try:
                    quota = await self._fetch_workbuddy_account_quota(account, client)
                except GatewayError as exc:
                    return {
                        **base,
                        **self._quota_failure(cached, exc, message="积分获取失败"),
                        "cached": bool(cached),
                        "provider": provider,
                    }
            elif provider != "codex":
                quota = self._placeholder_quota(
                    plan_type="subscription",
                    quota_kind="subscription",
                    message="订阅额度（不可重置）",
                    provider=provider,
                )
            else:
                try:
                    quota = await self._fetch_account_quota(account, client)
                except GatewayError as exc:
                    return {**base, **self._quota_failure(cached, exc), "cached": bool(cached)}
            self._quota_cache[account_id] = (time.monotonic(), quota)
            return {**base, **quota, "cached": False, "provider": provider}

    @staticmethod
    def _placeholder_quota(
        *,
        plan_type: str,
        quota_kind: str,
        message: str,
        provider: str,
    ) -> dict[str, Any]:
        return {
            "plan_type": plan_type,
            "limits": [],
            "next_reset_at": None,
            "reset_credits": {"available_count": 0, "credits": []},
            "fetched_at": iso_now(),
            "stale": False,
            "error": None,
            "quota_kind": quota_kind,
            "message": message,
            "provider": provider,
        }

    def _quota_failure(
        self,
        cached: tuple[float, dict[str, Any]] | None,
        exc: GatewayError,
        *,
        message: str | None = None,
    ) -> dict[str, Any]:
        error = {"code": exc.code, "message": exc.message}
        if cached:
            return {**cached[1], "stale": True, "error": error}
        quota = {
            "plan_type": "unknown",
            "limits": [],
            "next_reset_at": None,
            "reset_credits": {"available_count": 0, "credits": []},
            "fetched_at": None,
            "stale": True,
            "error": error,
        }
        if message:
            quota["quota_kind"] = "subscription"
            quota["message"] = message
        return quota

    async def _fetch_grok_account_quota(
        self, account: dict[str, Any], client: httpx.AsyncClient
    ) -> dict[str, Any]:
        if str(account.get("auth_mode") or "") == "api_key":
            return self._placeholder_quota(
                plan_type="metered",
                quota_kind="metered",
                message="API Key 按量计费",
                provider="grok",
            )
        credentials = await self.grok_credentials(account, client=client)
        timeout = httpx.Timeout(
            settings.grok_quota_timeout_seconds,
            connect=min(5.0, settings.grok_quota_timeout_seconds),
            pool=settings.gateway_pool_timeout_seconds,
        )
        headers = grok_build_headers(credentials, accept="application/json")
        base = grok_upstream_base_url(credentials.auth_mode)
        try:
            response = await client.get(
                f"{base}/billing?format=credits", headers=headers, timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise GatewayError(504, "Grok quota query timed out", code="quota_timeout") from exc
        except httpx.HTTPError as exc:
            raise GatewayError(502, "Cannot reach Grok quota service", code="quota_unavailable") from exc
        if response.status_code >= 400:
            status = 401 if response.status_code == 401 else 502
            code = "quota_unauthorized" if response.status_code == 401 else "quota_upstream_error"
            raise GatewayError(status, "Grok quota service rejected the request", code=code)
        try:
            billing = response.json()
        except json.JSONDecodeError as exc:
            raise GatewayError(502, "Grok quota service returned invalid JSON", code="quota_invalid") from exc
        snapshot = parse_grok_quota_snapshot(billing)
        if snapshot is None:
            raise GatewayError(502, "Grok quota service returned invalid data", code="quota_invalid")
        snapshot["fetched_at"] = iso_now()
        snapshot["stale"] = False
        snapshot["error"] = None
        snapshot["provider"] = "grok"
        return snapshot

    async def _fetch_antigravity_account_quota(
        self, account: dict[str, Any], client: httpx.AsyncClient
    ) -> dict[str, Any]:
        credentials = await self.antigravity_credentials(account, client=client)
        timeout = httpx.Timeout(
            settings.gateway_quota_timeout_seconds,
            connect=min(10.0, settings.gateway_quota_timeout_seconds),
            pool=settings.gateway_pool_timeout_seconds,
        )
        url = antigravity_fetch_models_url(credentials.host)
        headers = antigravity_build_headers(credentials, accept="application/json")
        try:
            response = await self._antigravity_post(client, url, headers=headers, json={}, timeout=timeout)
            if response.status_code == 401:
                credentials = await refresh_antigravity_credentials(credentials, client)
                headers = antigravity_build_headers(credentials, accept="application/json")
                response = await self._antigravity_post(client, url, headers=headers, json={}, timeout=timeout)
        except HTTPException as exc:
            raise GatewayError(401, "Antigravity authentication expired", code="quota_unauthorized") from exc
        except httpx.TimeoutException as exc:
            raise GatewayError(504, "Antigravity quota query timed out", code="quota_timeout") from exc
        except httpx.HTTPError as exc:
            raise GatewayError(502, "Cannot reach Antigravity quota service", code="quota_unavailable") from exc
        if response.status_code >= 400:
            status = 401 if response.status_code == 401 else 502
            code = "quota_unauthorized" if response.status_code == 401 else "quota_upstream_error"
            raise GatewayError(status, "Antigravity quota service rejected the request", code=code)
        try:
            catalog = response.json()
        except json.JSONDecodeError as exc:
            raise GatewayError(502, "Antigravity quota service returned invalid JSON", code="quota_invalid") from exc
        snapshot = parse_antigravity_quota_snapshot(catalog)
        if snapshot is None:
            raise GatewayError(502, "Antigravity quota service returned invalid data", code="quota_invalid")
        snapshot["fetched_at"] = iso_now()
        snapshot["stale"] = False
        snapshot["error"] = None
        snapshot["provider"] = "antigravity"
        return snapshot

    async def _fetch_workbuddy_account_quota(
        self, account: dict[str, Any], client: httpx.AsyncClient
    ) -> dict[str, Any]:
        credentials = await self.workbuddy_credentials(account, client=client)
        timeout = httpx.Timeout(
            settings.gateway_quota_timeout_seconds,
            connect=min(10.0, settings.gateway_quota_timeout_seconds),
            pool=settings.gateway_pool_timeout_seconds,
        )
        headers = workbuddy_common_headers(credentials.realm)
        headers["Authorization"] = "Bearer " + credentials.access_token
        headers["X-User-Id"] = credentials.uid
        if credentials.enterprise_id:
            headers["X-Enterprise-Id"] = credentials.enterprise_id
        url = workbuddy_base_url(credentials.realm) + "/v2/billing/meter/get-user-resource"
        try:
            response = await client.post(
                url,
                headers=headers,
                json={
                    "PageNumber": 1,
                    "PageSize": 100,
                    "ProductCode": "p_tcaca",
                    "Status": [0, 3],
                    "OnlyValidPeriod": True,
                },
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise GatewayError(504, "WorkBuddy quota query timed out", code="quota_timeout") from exc
        except httpx.HTTPError as exc:
            raise GatewayError(502, "Cannot reach WorkBuddy quota service", code="quota_unavailable") from exc
        if response.status_code >= 400:
            status = 401 if response.status_code == 401 else 502
            code = "quota_unauthorized" if response.status_code == 401 else "quota_upstream_error"
            raise GatewayError(status, "WorkBuddy quota service rejected the request", code=code)
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise GatewayError(502, "WorkBuddy quota service returned invalid JSON", code="quota_invalid") from exc
        snapshot = parse_workbuddy_quota_snapshot(body)
        if snapshot is None:
            raise GatewayError(502, "WorkBuddy quota service returned invalid data", code="quota_invalid")
        snapshot["fetched_at"] = iso_now()
        snapshot["stale"] = False
        snapshot["error"] = None
        snapshot["provider"] = "workbuddy"
        return snapshot

    async def account_quotas(self, *, refresh: bool = False) -> dict[str, Any]:
        accounts = await gateway_store.all(
            """
            SELECT a.* FROM accounts a
            WHERE a.status!='deleted'
            ORDER BY created_at
            """
        )
        codex_limit = max(1, settings.gateway_quota_max_concurrency)
        other_limit = max(1, settings.gateway_quota_max_concurrency)
        codex_slots = asyncio.Semaphore(codex_limit)
        other_slots = asyncio.Semaphore(other_limit)
        limits = httpx.Limits(
            max_connections=codex_limit + other_limit,
            max_keepalive_connections=codex_limit + other_limit,
        )
        async with new_client(timeout=None, limits=limits) as client:
            async def load(account: dict[str, Any]) -> dict[str, Any]:
                slots = codex_slots if account_provider(account) == "codex" else other_slots
                async with slots:
                    return await self._account_quota(account, client, refresh=refresh)

            data = await asyncio.gather(*(load(account) for account in accounts))
        return {"data": data, "refresh_interval_seconds": settings.gateway_quota_cache_seconds}

    async def reset_account_quota(
        self,
        account_id: str,
        *,
        idempotency_key: str,
        credit_id: str | None,
    ) -> dict[str, Any]:
        account = await gateway_store.one("SELECT * FROM accounts WHERE account_id=?", (account_id,))
        if account is None or account["status"] == "deleted":
            raise GatewayError(404, "Codex account not found", code="account_not_found")
        if account_provider(account) != "codex":
            raise GatewayError(
                400,
                "Quota reset is only available for Codex accounts",
                code="unsupported_capability",
                error_type="invalid_request_error",
            )
        lock = self._quota_reset_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            async with new_client(timeout=None, limits=httpx.Limits(max_connections=2)) as client:
                credentials = await self.credentials(account, client=client)
                url = (
                    f"{settings.codex_chatgpt_backend_url.rstrip('/')}"
                    "/wham/rate-limit-reset-credits/consume"
                )
                payload: dict[str, Any] = {"redeem_request_id": idempotency_key}
                if credit_id:
                    payload["credit_id"] = credit_id
                try:
                    response = await client.post(
                        url,
                        headers=build_headers(credentials, accept="application/json"),
                        json=payload,
                        timeout=httpx.Timeout(
                            settings.gateway_quota_timeout_seconds,
                            connect=min(10.0, settings.gateway_quota_timeout_seconds),
                            pool=settings.gateway_pool_timeout_seconds,
                        ),
                    )
                except httpx.TimeoutException as exc:
                    raise GatewayError(
                        504,
                        "Quota reset result is unknown; retry with the same idempotency key",
                        code="quota_reset_timeout",
                    ) from exc
                except httpx.HTTPError as exc:
                    raise GatewayError(502, "Cannot reach Codex quota service", code="quota_unavailable") from exc
                if response.status_code >= 400:
                    raise GatewayError(502, "Codex quota reset was rejected", code="quota_reset_rejected")
                try:
                    body = response.json()
                except json.JSONDecodeError as exc:
                    raise GatewayError(502, "Codex quota reset returned invalid JSON", code="quota_invalid") from exc
                outcome = body.get("code") if isinstance(body, dict) else None
                if outcome not in {"reset", "nothing_to_reset", "no_credit", "already_redeemed"}:
                    raise GatewayError(502, "Codex quota reset returned an unknown result", code="quota_invalid")
                self._quota_cache.pop(account_id, None)
                refreshed = await self._account_quota(account, client, refresh=True)
                return {
                    "outcome": outcome,
                    "windows_reset": int(body.get("windows_reset") or 0),
                    "quota": refreshed,
                }

    def _public_account(self, row: dict[str, Any]) -> dict[str, Any]:
        account_id = str(row["account_id"])
        provider = account_provider(row)
        stored_label = row.get("label")
        if isinstance(stored_label, str) and stored_label.strip():
            label = stored_label.strip()
        else:
            label = account_id if len(account_id) <= 8 else f"{account_id[:4]}…{account_id[-4:]}"
        capabilities = parse_capabilities(row.get("capabilities"))
        return {
            "account_id": account_id,
            "label": label,
            "provider": provider,
            "auth_mode": row.get("auth_mode") or ("oauth" if provider != "codex" else "chatgpt"),
            "capabilities": capabilities,
            "status": row.get("status"),
            "expires_at": row.get("expires_at"),
            "cooldown_until": row.get("cooldown_until"),
            "current_concurrency": self.current_concurrency(account_id),
            "total_tokens": int(row.get("total_tokens") or 0),
            "preferred_keys": int(row.get("preferred_keys") or 0),
            "routed_keys": int(row.get("routed_keys") or 0),
        }

    def current_concurrency(self, account_id: str) -> int:
        semaphore = self._semaphores.get(account_id)
        if semaphore is None:
            return 0
        return settings.gateway_account_max_concurrency - semaphore._value

    async def set_account_status(self, account_id: str, enabled: bool) -> None:
        row = await gateway_store.one("SELECT status FROM accounts WHERE account_id=?", (account_id,))
        if row is None or row["status"] == "deleted":
            raise GatewayError(404, "Account not found", code="account_not_found")
        current = str(row["status"] or "")
        if enabled:
            if current == "active":
                return
            if current not in {"disabled", "invalid"}:
                raise GatewayError(
                    409,
                    "A removed account cannot be re-enabled; import it again",
                    code="account_unavailable",
                )

            def revive(db: Any) -> int:
                changed = db.execute(
                    """UPDATE accounts
                       SET status='active', cooldown_until=NULL, network_failures=0, updated_at=?
                       WHERE account_id=? AND status IN ('disabled','invalid')""",
                    (iso_now(), account_id),
                ).rowcount
                db.execute(
                    """UPDATE account_model_health
                       SET failures=0, cooldown_until=NULL
                       WHERE account_id=?""",
                    (account_id,),
                )
                return int(changed or 0)

            if not await gateway_store.call(revive):
                raise GatewayError(404, "Account not found", code="account_not_found")
            return
        if current == "disabled":
            return
        if current != "active":
            raise GatewayError(
                409,
                "Only an active account can be disabled",
                code="account_unavailable",
            )
        changed = await gateway_store.execute(
            "UPDATE accounts SET status=?, updated_at=? WHERE account_id=?",
            ("disabled", iso_now(), account_id),
        )
        if not changed:
            raise GatewayError(404, "Account not found", code="account_not_found")

    async def delete_account(self, account_id: str) -> None:
        row = await gateway_store.one(
            "SELECT account_id,status FROM accounts WHERE account_id=?", (account_id,)
        )
        if row is None or row["status"] == "deleted":
            raise GatewayError(404, "Account not found", code="account_not_found")

        def operation(db: Any) -> None:
            db.execute(
                "UPDATE accounts SET status='deleted',cooldown_until=NULL,updated_at=? WHERE account_id=?",
                (iso_now(), account_id),
            )
            db.execute(
                """UPDATE api_key_routes SET preferred_account_id=NULL,active_account_id=NULL,
                       failover_until=NULL,last_failure_code=NULL,updated_at=?
                   WHERE preferred_account_id=? OR active_account_id=?""",
                (iso_now(), account_id, account_id),
            )

        await gateway_store.call(operation)

    async def list_api_keys(self) -> list[dict[str, Any]]:
        rows = await gateway_store.all(
            """
            SELECT k.id,k.name,k.key_prefix AS prefix,k.fingerprint,k.key_ciphertext,
              k.owner_user_id,k.usd_credit,k.fast_enabled,k.status,k.last_used_at,k.created_at,u.email AS email
            FROM api_keys k
            LEFT JOIN portal_users u ON u.id=k.owner_user_id
            WHERE k.status!='deleted' ORDER BY k.created_at
            """
        )
        route_rows = await gateway_store.all(
            """
            SELECT r.key_id,r.provider,r.preferred_account_id,r.active_account_id,
                   r.failover_until,r.last_failure_code,r.updated_at,
                   COALESCE(s.failures,0) AS consecutive_failures
             FROM api_key_routes r JOIN api_keys k ON k.id=r.key_id
             LEFT JOIN route_streaks s ON s.key_id=r.key_id AND s.provider=r.provider
                                      AND s.account_id=r.active_account_id
             WHERE k.status!='deleted'
            """
        )
        cooling_rows = await gateway_store.all(
            """SELECT account_id,model,cooldown_until FROM account_model_health
               WHERE cooldown_until>? ORDER BY cooldown_until""", (iso_now(),)
        )
        cooling_by_account: dict[str, list[dict[str, str]]] = {}
        for cooling in cooling_rows:
            cooling_by_account.setdefault(str(cooling["account_id"]), []).append({
                "model": str(cooling["model"]),
                "until": str(cooling["cooldown_until"]),
            })
        routes_by_key: dict[str, list[dict[str, Any]]] = {}
        for route in route_rows:
            routes_by_key.setdefault(str(route["key_id"]), []).append(route)
        for row in rows:
            key_routes = sorted(
                routes_by_key.get(str(row["id"]), []),
                key=lambda item: (
                    0 if str(item.get("provider") or "") == "codex" else 1,
                    str(item.get("provider") or ""),
                ),
            )
            public_routes = []
            for route in key_routes:
                public_routes.append({
                    "provider": route.get("provider") or "codex",
                    "preferred_account_id": route.get("preferred_account_id"),
                    "active_account_id": route.get("active_account_id"),
                    "route_status": (
                        "unconfigured" if not route.get("active_account_id")
                        else "failover" if route.get("preferred_account_id")
                            and route.get("active_account_id") != route.get("preferred_account_id")
                        else "active"
                    ),
                    "failover_until": route.get("failover_until"),
                    "last_failure_code": route.get("last_failure_code"),
                    "consecutive_failures": int(route.get("consecutive_failures") or 0),
                    "mode": "manual" if route.get("preferred_account_id") else "auto",
                    "model_cooldowns": cooling_by_account.get(
                        str(route.get("active_account_id") or ""), []
                    ),
                })
            convenience = public_routes[0] if public_routes else {}
            row["routes"] = public_routes
            row["preferred_account_id"] = convenience.get("preferred_account_id")
            row["active_account_id"] = convenience.get("active_account_id")
            row["current_account_id"] = convenience.get("active_account_id")
            row["failover_until"] = convenience.get("failover_until")
            row["last_failure_code"] = convenience.get("last_failure_code")
            row["route_status"] = convenience.get("route_status") or "unconfigured"
            row["key"] = self._decrypt_api_key(row.pop("key_ciphertext", None))
            row["recoverable"] = row["key"] is not None
            row["fast_enabled"] = bool(row.get("fast_enabled"))
            row["email"] = str(row["email"]).strip() if row.get("email") else None
            from app.billing import usd_text
            row["usd_credit"] = usd_text(row.get("usd_credit") or "0")
            row.pop("fingerprint", None)
        return rows

    async def set_api_key_route(self, key_id: str, preferred_account_id: str) -> None:
        key = await gateway_store.one(
            "SELECT status FROM api_keys WHERE id=?", (key_id,)
        )
        if key is None or key["status"] == "deleted":
            raise GatewayError(404, "API key not found", code="api_key_not_found")
        if preferred_account_id:
            account = await gateway_store.one(
                "SELECT account_id,provider,status,cooldown_until FROM accounts WHERE account_id=?",
                (preferred_account_id,),
            )
            if not account or account["status"] != "active" or (
                account.get("cooldown_until") and account["cooldown_until"] > iso_now()
            ):
                raise GatewayError(422, "Select an active account", code="invalid_account")
            provider = account_provider(account)
        else:
            raise GatewayError(422, "Provider is required to restore automatic routing", code="invalid_request")
        await self.update_provider_route(key_id, provider, preferred_account_id)

    async def update_provider_route(
        self, key_id: str, provider: str, account_id: str | None
    ) -> None:
        try:
            get_adapter(provider)
        except KeyError as exc:
            raise GatewayError(422, "Unknown provider", code="unknown_provider") from exc
        key = await gateway_store.one("SELECT status FROM api_keys WHERE id=?", (key_id,))
        if not key or key["status"] == "deleted":
            raise GatewayError(404, "API key not found", code="api_key_not_found")
        if account_id:
            account = await gateway_store.one(
                "SELECT * FROM accounts WHERE account_id=?", (account_id,)
            )
            if not account or account_provider(account) != provider or account["status"] != "active" or (
                account.get("cooldown_until") and account["cooldown_until"] > iso_now()
            ):
                raise GatewayError(422, "Select a healthy account from this provider", code="invalid_account")
        await gateway_store.execute(
            """INSERT INTO api_key_routes(key_id,provider,preferred_account_id,active_account_id,
                        failover_until,last_failure_code,updated_at)
               VALUES(?,?,?,?,NULL,NULL,?)
               ON CONFLICT(key_id,provider) DO UPDATE SET
                 preferred_account_id=excluded.preferred_account_id,
                 active_account_id=excluded.active_account_id,
                 failover_until=NULL,last_failure_code=NULL,updated_at=excluded.updated_at""",
            (key_id, provider, account_id, account_id, iso_now()),
        )
        await gateway_store.execute(
            "DELETE FROM route_streaks WHERE key_id=? AND provider=?", (key_id, provider)
        )

    async def rotate_api_key(self, key_id: str) -> dict[str, Any]:
        row = await gateway_store.one("SELECT name,status FROM api_keys WHERE id=?", (key_id,))
        if row is None or row["status"] == "deleted":
            raise GatewayError(404, "API key not found", code="api_key_not_found")
        raw = f"sk-ts-{secrets.token_urlsafe(32)}"
        prefix = raw[:12]

        def operation(db: Any) -> None:
            db.execute(
                """UPDATE api_keys SET key_prefix=?,fingerprint=?,key_ciphertext=?,status='active'
                   WHERE id=?""",
                (prefix, self.digest(raw), self._encrypt_api_key(raw), key_id),
            )

        await gateway_store.call(operation)
        return {"id": key_id, "name": str(row["name"]), "prefix": prefix, "key": raw}

    async def set_api_key_status(self, key_id: str, enabled: bool) -> None:
        row = await gateway_store.one("SELECT status FROM api_keys WHERE id=?", (key_id,))
        if row is None:
            raise GatewayError(404, "API key not found", code="api_key_not_found")
        if row["status"] in {"revoked", "deleted"}:
            raise GatewayError(409, "A removed API key cannot be re-enabled", code="api_key_removed")
        changed = await gateway_store.execute(
            "UPDATE api_keys SET status=? WHERE id=?", ("active" if enabled else "disabled", key_id)
        )
        if not changed:
            raise GatewayError(404, "API key not found", code="api_key_not_found")

    async def update_api_key(
        self,
        key_id: str,
        *,
        enabled: bool | None = None,
        fast_enabled: bool | None = None,
    ) -> None:
        row = await gateway_store.one("SELECT status FROM api_keys WHERE id=?", (key_id,))
        if row is None or row["status"] == "deleted":
            raise GatewayError(404, "API key not found", code="api_key_not_found")
        if enabled is not None and row["status"] == "revoked":
            raise GatewayError(409, "A removed API key cannot be re-enabled", code="api_key_removed")
        assignments: list[str] = []
        values: list[Any] = []
        if enabled is not None:
            assignments.append("status=?")
            values.append("active" if enabled else "disabled")
        if fast_enabled is not None:
            assignments.append("fast_enabled=?")
            values.append(int(fast_enabled))
        if not assignments:
            raise GatewayError(422, "At least one API key field is required", code="invalid_request")
        values.append(key_id)
        await gateway_store.execute(
            f"UPDATE api_keys SET {', '.join(assignments)} WHERE id=?", tuple(values)
        )

    async def delete_api_key(self, key_id: str) -> None:
        def operation(db: Any) -> int:
            changed = db.execute(
                """UPDATE api_keys SET status='deleted',key_ciphertext=NULL
                   WHERE id=? AND status!='deleted'""",
                (key_id,),
            ).rowcount
            if changed:
                db.execute("DELETE FROM api_key_routes WHERE key_id=?", (key_id,))
            return changed

        if not await gateway_store.call(operation):
            raise GatewayError(404, "API key not found", code="api_key_not_found")

    async def _usage_range_bounds(
        self, usage_range: str
    ) -> tuple[str | None, str | None]:
        if usage_range not in USAGE_RANGES:
            raise GatewayError(422, "Invalid usage range", code="invalid_usage_range")
        if usage_range == "all":
            bounds = await gateway_store.one(
                "SELECT MIN(usage_date) AS start_date,MAX(usage_date) AS end_date FROM usage_daily"
            )
            return (bounds or {}).get("start_date"), (bounds or {}).get("end_date")
        today = dt.date.fromisoformat(usage_date_today())
        if usage_range == "day":
            start = today
        elif usage_range == "week":
            start = today - dt.timedelta(days=today.weekday())
        else:
            start = today.replace(day=1)
        return start.isoformat(), today.isoformat()

    async def usage(
        self,
        usage_range: str,
        key_id: str | None,
        account_id: str | None,
        model: str | None,
    ) -> dict[str, Any]:
        start, end = await self._usage_range_bounds(usage_range)
        clauses = ["1=1"]
        values: list[Any] = []
        if usage_range != "all":
            clauses.extend(("usage_date>=?", "usage_date<=?"))
            values.extend((start, end))
        for column, value in (("key_id", key_id), ("account_id", account_id), ("model", model)):
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        rows = await gateway_store.all(
            f"SELECT * FROM usage_daily WHERE {' AND '.join(clauses)} "
            "ORDER BY usage_date DESC,key_id,account_id,model",
            tuple(values),
        )
        return {"data": rows, "range": usage_range, "start_date": start, "end_date": end}

    async def usage_summary(self, usage_range: str) -> dict[str, Any]:
        start, end = await self._usage_range_bounds(usage_range)
        join = "u.key_id=k.id"
        values: tuple[Any, ...] = ()
        if usage_range != "all":
            join += " AND u.usage_date>=? AND u.usage_date<=?"
            values = (start, end)
        rows = await gateway_store.all(
            f"""
            SELECT
              k.id AS key_id,
              k.name,
              k.key_prefix AS prefix,
              k.status,
              COALESCE(SUM(u.input_tokens),0) AS input_tokens,
              COALESCE(SUM(u.output_tokens),0) AS output_tokens,
              COALESCE(SUM(u.cached_tokens),0) AS cached_tokens,
              COALESCE(SUM(u.reasoning_tokens),0) AS reasoning_tokens,
              COALESCE(SUM(u.total_tokens),0) AS total_tokens
            FROM api_keys k
            LEFT JOIN usage_daily u ON {join}
            WHERE k.status!='deleted'
            GROUP BY k.id,k.name,k.key_prefix,k.status
            ORDER BY total_tokens DESC,k.name,k.id
            """,
            values,
        )
        return {"data": rows, "range": usage_range, "start_date": start, "end_date": end}

    async def dashboard(self) -> dict[str, Any]:
        start, end = _local_day_bounds_utc()
        metrics = await gateway_store.one(
            """
            SELECT
              COUNT(*) AS calls,
              SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes,
              SUM(CASE WHEN status IN ('failed','interrupted') THEN 1 ELSE 0 END) AS failures,
              COALESCE(SUM(input_tokens),0) AS input_tokens,
              COALESCE(SUM(output_tokens),0) AS output_tokens,
              COALESCE(SUM(cached_tokens),0) AS cached_tokens,
              COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
              COALESCE(SUM(total_tokens),0) AS total_tokens,
              COALESCE(ROUND(AVG(duration_ms)),0) AS average_duration_ms
            FROM call_records WHERE started_at>=? AND started_at<?
            """,
            (start, end),
        )
        healthy = await gateway_store.one(
            """
            SELECT COUNT(*) AS count FROM accounts
            WHERE status='active' AND (cooldown_until IS NULL OR cooldown_until<=?)
            """,
            (iso_now(),),
        )
        result = dict(metrics or {})
        for name in (
            "calls",
            "successes",
            "failures",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "total_tokens",
            "average_duration_ms",
        ):
            result[name] = int(result.get(name) or 0)
        result["healthy_accounts"] = int((healthy or {}).get("count") or 0)
        result["recent_errors"] = await self.recent_errors(5)
        return result

    async def calls(
        self,
        *,
        page: int,
        page_size: int,
        start: str | None,
        end: str | None,
        key_id: str | None,
        account_id: str | None,
        model: str | None,
        endpoint: str | None,
        status: str | None,
    ) -> dict[str, Any]:
        safe_page = max(1, int(page))
        safe_size = max(1, min(int(page_size), 100))
        clauses = ["1=1"]
        values: list[Any] = []
        if start:
            clauses.append("c.started_at>=?")
            values.append(_date_filter_value(start, end_of_day=False))
        if end:
            clauses.append("c.started_at<?")
            values.append(_date_filter_value(end, end_of_day=True))
        for column, value in (
            ("c.key_id", key_id),
            ("c.account_id", account_id),
            ("c.endpoint", endpoint),
            ("c.status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        if model:
            clauses.append("(c.model=? OR c.request_model=? OR c.response_model=?)")
            values.extend([model, model, model])
        where = " AND ".join(clauses)
        total_row = await gateway_store.one(
            f"SELECT COUNT(*) AS count FROM call_records c WHERE {where}", tuple(values)
        )
        rows = await gateway_store.all(
            f"""
            SELECT c.request_id,c.started_at,c.ended_at,c.duration_ms,c.key_id,
                   COALESCE(NULLIF(k.name,''),c.key_name) AS key_name,c.account_id,
                   c.endpoint,c.model,c.request_model,c.response_model,c.is_stream,c.attempts,
                   c.http_status,c.status,c.error_code,c.input_tokens,c.output_tokens,
                   c.cached_tokens,c.reasoning_tokens,c.total_tokens,c.usage_unknown
            FROM call_records c
            LEFT JOIN api_keys k ON k.id=c.key_id AND k.status!='deleted'
            WHERE {where}
            ORDER BY c.started_at DESC LIMIT ? OFFSET ?
            """,
            (*values, safe_size, (safe_page - 1) * safe_size),
        )
        for row in rows:
            row["is_stream"] = bool(row["is_stream"])
            row["usage_unknown"] = bool(row["usage_unknown"])
            request_model = str(row.get("request_model") or row.get("model") or "")
            response_model = str(row.get("response_model") or "")
            row["request_model"] = request_model or None
            row["response_model"] = response_model or None
            row["model_mismatch"] = models_mismatch(request_model, response_model)
        current_keys = await gateway_store.all(
            "SELECT id AS key_id,name,status FROM api_keys WHERE status!='deleted' ORDER BY created_at"
        )
        historical_keys = await gateway_store.all(
            """SELECT key_id,MAX(key_name) AS name,'deleted' AS status
               FROM call_records GROUP BY key_id ORDER BY name,key_id"""
        )
        key_options = {str(row["key_id"]): row for row in historical_keys}
        key_options.update({str(row["key_id"]): row for row in current_keys})
        current_accounts = await gateway_store.all(
            """SELECT account_id,status,provider,label FROM accounts
               WHERE status!='deleted' ORDER BY created_at"""
        )
        historical_accounts = await gateway_store.all(
            """SELECT DISTINCT account_id,'deleted' AS status FROM call_records
               WHERE account_id IS NOT NULL ORDER BY account_id"""
        )
        account_options = {str(row["account_id"]): row for row in historical_accounts}
        account_options.update({str(row["account_id"]): row for row in current_accounts})
        return {
            "data": rows,
            "page": safe_page,
            "page_size": safe_size,
            "total": int((total_row or {}).get("count") or 0),
            "key_options": sorted(
                key_options.values(), key=lambda row: (str(row["name"]).casefold(), str(row["key_id"]))
            ),
            "account_options": sorted(
                account_options.values(), key=lambda row: str(row["account_id"])
            ),
        }

    def _session_binding_hash(self, key_id: str, binding_value: str, provider: str) -> str:
        if provider == "codex":
            return self.digest(f"{key_id}:{binding_value}")
        return self.digest(f"{key_id}:{provider}:{binding_value}")

    def _grok_tool_cache_get(self, cache_key: str) -> dict[str, Any] | None:
        cached = self._grok_tool_cache.get(cache_key)
        if cached is None:
            return None
        expires_at, entry = cached
        if expires_at <= time.monotonic() or not isinstance(entry, dict):
            self._grok_tool_cache.pop(cache_key, None)
            return None
        return entry

    def _grok_tool_cache_put(self, cache_key: str, entry: dict[str, Any]) -> None:
        now = time.monotonic()
        stale = [key for key, (expires_at, _entry) in self._grok_tool_cache.items() if expires_at <= now]
        for key in stale:
            self._grok_tool_cache.pop(key, None)
        if cache_key not in self._grok_tool_cache and len(self._grok_tool_cache) >= GROK_TOOL_CACHE_MAX_KEYS:
            oldest = min(self._grok_tool_cache, key=lambda key: self._grok_tool_cache[key][0])
            self._grok_tool_cache.pop(oldest, None)
        self._grok_tool_cache[cache_key] = (now + GROK_TOOL_CACHE_TTL_SECONDS, entry)

    @staticmethod
    def _tool_names_used_in_payload(payload: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        items = payload.get("input")
        if not isinstance(items, list):
            return names
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"function_call", "custom_tool_call", "mcp_call"}:
                name = item.get("name")
                if isinstance(name, str) and name:
                    names.add(name)
        return names

    @staticmethod
    def _apply_grok_fast_tier(
        payload: dict[str, Any], key: dict[str, Any], account: dict[str, Any]
    ) -> None:
        payload.pop("service_tier", None)
        auth_mode = str(account.get("auth_mode") or "")
        if bool(key.get("fast_enabled")) and auth_mode in GROK_FAST_AUTH_MODES:
            payload["service_tier"] = "priority"

    def _apply_grok_tool_cache(
        self,
        payload: dict[str, Any],
        rewrite: GrokRewriteSpec,
        *,
        cache_key: str | None,
        remember: bool,
    ) -> GrokRewriteSpec:
        cached = self._grok_tool_cache_get(cache_key) if cache_key else None
        tools = payload.get("tools")
        advertised: list[dict[str, Any]] = (
            [item for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []
        )
        advertised_seen: set[tuple[str, str]] = {
            (str(item.get("type") or ""), str(item.get("name") or "")) for item in advertised
        }
        used_names = self._tool_names_used_in_payload(payload)
        cached_tools = (
            [item for item in cached.get("tools") or [] if isinstance(item, dict)] if cached else []
        )
        namespaces = dict(cached.get("namespaces") or {}) if cached else {}
        namespaces.update(rewrite.namespace_map)
        freeform = set(rewrite.freeform_tool_names)
        if cached:
            for name in cached.get("freeform") or []:
                if isinstance(name, str) and name:
                    freeform.add(name)

        sent = list(advertised)
        sent_seen = set(advertised_seen)
        if advertised:
            for item in cached_tools:
                key = (str(item.get("type") or ""), str(item.get("name") or ""))
                name = str(item.get("name") or "")
                if key in sent_seen or not name or name not in used_names:
                    continue
                sent.append(item)
                sent_seen.add(key)
        else:
            for item in cached_tools:
                key = (str(item.get("type") or ""), str(item.get("name") or ""))
                if key in sent_seen:
                    continue
                sent.append(item)
                sent_seen.add(key)
        if sent:
            payload["tools"] = sent[:GROK_TOOL_CACHE_MAX_TOOLS]
            if payload.get("tool_choice") is None:
                payload.pop("tool_choice", None)

        remembered: list[dict[str, Any]] = []
        remembered_seen: set[tuple[str, str]] = set()
        for item in [*cached_tools, *advertised]:
            key = (str(item.get("type") or ""), str(item.get("name") or ""))
            if key in remembered_seen:
                continue
            remembered_seen.add(key)
            remembered.append(dict(item))
            if len(remembered) >= GROK_TOOL_CACHE_MAX_TOOLS:
                break

        sent_names = {str(item.get("name") or "") for item in sent if isinstance(item.get("name"), str)}
        tool_search = rewrite.tool_search_wire_name
        if not tool_search and TOOL_SEARCH_WIRE_NAME in sent_names:
            tool_search = TOOL_SEARCH_WIRE_NAME
        pruned_ns = {
            name: namespace
            for name, namespace in namespaces.items()
            if name in sent_names or name in used_names
        }
        pruned_freeform = frozenset(
            name for name in freeform if name in sent_names or name in used_names
        )

        if remember and cache_key and remembered:
            self._grok_tool_cache_put(
                cache_key,
                {
                    "tools": remembered,
                    "namespaces": dict(namespaces),
                    "tool_search": bool(tool_search) or bool(cached and cached.get("tool_search")),
                    "freeform": sorted(freeform),
                },
            )
        if (
            pruned_ns == rewrite.namespace_map
            and tool_search == rewrite.tool_search_wire_name
            and pruned_freeform == rewrite.freeform_tool_names
        ):
            return rewrite
        return GrokRewriteSpec(
            freeform_tool_names=pruned_freeform,
            tool_search_wire_name=tool_search,
            namespace_map=pruned_ns,
        )

    async def select_account(
        self,
        key: dict[str, Any],
        *,
        kind: str,
        session_id: str | None = None,
        previous_response_id: str | None = None,
        conversation: str | None = None,
        provider: str = "codex",
        model: str = "",
        context: CallContext | None = None,
    ) -> dict[str, Any]:
        from app.scheduler import select
        if kind in {"retry", "image-retry"}:
            return await self._select_configured_account(key, provider=provider, model=model)
        return await select(self, key, provider=provider, model=model, context=context,
                            previous=previous_response_id, conversation=conversation, kind=kind)

    def _route_lock(self, key_id: str, provider: str = "codex") -> asyncio.Lock:
        return self._route_locks.setdefault(f"{key_id}:{provider}", asyncio.Lock())

    async def _balanced_account(
        self, key: dict[str, Any], *, excluded: set[str], provider: str = "codex",
        model: str = "",
    ) -> dict[str, Any]:
        now = iso_now()
        cooled = {
            str(row["account_id"])
            for row in await gateway_store.all(
                "SELECT account_id FROM account_model_health WHERE model=? AND cooldown_until>?",
                (model, now),
            )
        } if model else set()
        accounts = [
            item
            for item in await gateway_store.healthy_accounts(provider)
            if str(item["account_id"]) not in excluded
            and str(item["account_id"]) not in cooled
        ]
        if not accounts:
            label = get_adapter(provider).display_name if provider in {"codex", "grok", "antigravity", "workbuddy"} else provider
            raise GatewayError(
                503,
                f"No alternate healthy {label} account is available",
                code="no_healthy_accounts",
            )
        assignments = await gateway_store.all(
            """SELECT r.active_account_id,COUNT(*) AS total
               FROM api_key_routes r JOIN api_keys k ON k.id=r.key_id
               WHERE r.provider=? AND r.active_account_id IS NOT NULL AND k.status='active'
               GROUP BY r.active_account_id""", (provider,)
        )
        loads = {str(row["active_account_id"]): int(row["total"]) for row in assignments}
        sticky = str(key["fingerprint"])
        return min(
            accounts,
            key=lambda item: (
                loads.get(str(item["account_id"]), 0),
                self.current_concurrency(str(item["account_id"])),
                int(item.get("network_failures") or 0),
                -self._route_score(sticky, str(item["account_id"])),
            ),
        )

    async def _select_configured_account(
        self, key: dict[str, Any], *, provider: str = "codex", model: str = ""
    ) -> dict[str, Any]:
        async with self._route_lock(str(key["id"]), provider):
            route = await gateway_store.one(
                "SELECT active_account_id FROM api_key_routes WHERE key_id=? AND provider=?",
                (str(key["id"]), provider),
            )
            active = str((route or {}).get("active_account_id") or "")
            if active:
                eligible = [
                    row for row in await gateway_store.healthy_accounts(provider)
                    if str(row["account_id"]) == active
                ]
                cooled = await gateway_store.one(
                    """SELECT cooldown_until FROM account_model_health
                       WHERE account_id=? AND model=? AND cooldown_until>?""",
                    (active, model, iso_now()),
                )
                if eligible and not cooled:
                    return eligible[0]
            try:
                selected = await self._balanced_account(
                    key, excluded={active} if active else set(), provider=provider, model=model
                )
            except GatewayError as exc:
                raise GatewayError(
                    503,
                    f"No healthy {provider} account is available",
                    code="no_healthy_accounts",
                ) from exc
            await self._save_active_route(key, provider, str(selected["account_id"]))
            return selected

    async def _save_active_route(
        self, key: dict[str, Any], provider: str, account_id: str,
        *, failure_code: str | None = None,
    ) -> None:
        await gateway_store.execute(
            """INSERT INTO api_key_routes(key_id,provider,preferred_account_id,active_account_id,
                        failover_until,last_failure_code,updated_at)
               VALUES(?,?,NULL,?,NULL,?,?)
               ON CONFLICT(key_id,provider) DO UPDATE SET
                 active_account_id=excluded.active_account_id,
                 last_failure_code=excluded.last_failure_code,updated_at=excluded.updated_at""",
            (str(key["id"]), provider, account_id, failure_code, iso_now()),
        )
        await gateway_store.execute(
            "DELETE FROM route_streaks WHERE key_id=? AND provider=?",
            (str(key["id"]), provider),
        )

    async def _route_success(self, context: CallContext) -> None:
        await gateway_store.execute(
            "DELETE FROM route_streaks WHERE key_id=? AND provider=? AND account_id=?",
            (context.key_id, context.provider, context.account_id),
        )

    async def _route_failure(self, account_id: str, context: CallContext | None) -> None:
        if context is None or not context.model or context.provider not in {"codex", "grok", "antigravity", "workbuddy"}:
            return
        now = iso_now()
        cutoff = (utc_now() - MODEL_FAILURE_WINDOW).isoformat().replace("+00:00", "Z")
        cooldown = (utc_now() + MODEL_COOLDOWN).isoformat().replace("+00:00", "Z")
        def update(db: Any) -> int:
            health = db.execute(
                """SELECT failures,window_started_at,cooldown_until FROM account_model_health
                   WHERE account_id=? AND model=?""",
                (account_id, context.model),
            ).fetchone()
            reuse_window = bool(
                health and health["window_started_at"] >= cutoff
                and not (health["cooldown_until"] and health["cooldown_until"] <= now)
            )
            count = (int(health["failures"]) if reuse_window else 0) + 1
            started = health["window_started_at"] if reuse_window else now
            db.execute(
                """INSERT INTO account_model_health(account_id,model,failures,window_started_at,cooldown_until)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(account_id,model) DO UPDATE SET failures=excluded.failures,
                   window_started_at=excluded.window_started_at,cooldown_until=excluded.cooldown_until""",
                (account_id, context.model, count, started, cooldown if count >= 10 else None),
            )
            route = db.execute(
                "SELECT active_account_id FROM api_key_routes WHERE key_id=? AND provider=?",
                (context.key_id, context.provider),
            ).fetchone()
            if not route or route["active_account_id"] != account_id:
                return 0
            streak = db.execute(
                "SELECT failures FROM route_streaks WHERE key_id=? AND provider=? AND account_id=?",
                (context.key_id, context.provider, account_id),
            ).fetchone()
            failures = int(streak["failures"]) + 1 if streak else 1
            db.execute(
                """INSERT INTO route_streaks(key_id,provider,account_id,failures) VALUES(?,?,?,?)
                   ON CONFLICT(key_id,provider) DO UPDATE SET account_id=excluded.account_id,
                   failures=excluded.failures""",
                (context.key_id, context.provider, account_id, failures),
            )
            return failures
        failures = await gateway_store.call(update)
        if failures >= 2:
            route = await gateway_store.one("SELECT preferred_account_id FROM api_key_routes WHERE key_id=? AND provider=?", (context.key_id,context.provider))
            if route and route.get("preferred_account_id"):
                return
            key = await gateway_store.one(
                "SELECT id,fingerprint FROM api_keys WHERE id=?", (context.key_id,)
            )
            if key:
                try:
                    await self._fallback_account(
                        key, account_id, provider=context.provider,
                        model=context.model, failure_code="consecutive_failures",
                    )
                except GatewayError:
                    pass

    def _route_score(self, sticky: str, account_id: str) -> int:
        digest = hmac.new(self.secret, f"{sticky}:{account_id}".encode(), hashlib.sha256).digest()
        return int.from_bytes(digest, "big")

    @staticmethod
    def _touch_due(cache: dict[str, float], key: str) -> bool:
        now = time.monotonic()
        if now - cache.get(key, 0.0) < 60.0:
            return False
        cache[key] = now
        return True

    def _semaphore(self, account_id: str) -> asyncio.Semaphore:
        return self._semaphores.setdefault(
            account_id, asyncio.Semaphore(settings.gateway_account_max_concurrency)
        )

    async def credentials(
        self,
        account: dict[str, Any],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> CodexCredentials:
        provider = account_provider(account)
        if provider == "grok":
            raise GatewayError(500, "Grok credentials must be loaded via grok_credentials", code="internal_error")
        if provider == "antigravity":
            raise GatewayError(
                500,
                "Antigravity credentials must be loaded via antigravity_credentials",
                code="internal_error",
            )
        if provider == "workbuddy":
            raise GatewayError(
                500, "WorkBuddy credentials need workbuddy_credentials", code="internal_error"
            )
        path = Path(str(account["credential_path"]))
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            parsed = parse_auth_payload(payload, source="pool", path=path)
        except (OSError, json.JSONDecodeError, HTTPException) as exc:
            await self._mark_invalid(str(account["account_id"]))
            raise GatewayError(503, "Codex account credentials are unavailable", code="account_invalid") from exc
        if should_refresh(parsed):
            lock = self._refresh_locks.setdefault(str(account["account_id"]), asyncio.Lock())
            async with lock:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                parsed = parse_auth_payload(payload, source="pool", path=path)
                if should_refresh(parsed):
                    try:
                        parsed = await refresh_credentials(
                            parsed, client if client is not None else await shared_client()
                        )
                    except HTTPException as exc:
                        await self._mark_invalid(str(account["account_id"]))
                        raise GatewayError(503, "Codex account refresh failed", code="account_invalid") from exc
                    expires = parsed.expires_at.isoformat().replace("+00:00", "Z") if parsed.expires_at else None
                    await gateway_store.execute(
                        "UPDATE accounts SET expires_at=?,last_refresh_at=?,updated_at=? WHERE account_id=?",
                        (expires, iso_now(), iso_now(), account["account_id"]),
                    )
        return parsed

    async def grok_credentials(
        self,
        account: dict[str, Any],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> GrokCredentials:
        path = Path(str(account["credential_path"]))
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            parsed = load_grok_credentials(payload, path=path)
        except (OSError, json.JSONDecodeError, HTTPException) as exc:
            await self._mark_invalid(str(account["account_id"]))
            raise GatewayError(503, "Grok account credentials are unavailable", code="account_invalid") from exc
        if grok_should_refresh(parsed):
            lock = self._refresh_locks.setdefault(str(account["account_id"]), asyncio.Lock())
            async with lock:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                parsed = load_grok_credentials(payload, path=path)
                if grok_should_refresh(parsed):
                    try:
                        parsed = await refresh_grok_credentials(
                            parsed, client if client is not None else await shared_client()
                        )
                    except HTTPException as exc:
                        await self._mark_invalid(str(account["account_id"]))
                        raise GatewayError(503, "Grok account refresh failed", code="account_invalid") from exc
                    expires = (
                        parsed.expires_at.isoformat().replace("+00:00", "Z") if parsed.expires_at else None
                    )
                    await gateway_store.execute(
                        "UPDATE accounts SET expires_at=?,last_refresh_at=?,updated_at=? WHERE account_id=?",
                        (expires, iso_now(), iso_now(), account["account_id"]),
                    )
        return parsed

    async def antigravity_credentials(
        self,
        account: dict[str, Any],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> AntigravityCredentials:
        path = Path(str(account["credential_path"]))
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            parsed = load_antigravity_credentials(payload, path=path)
        except (OSError, json.JSONDecodeError, HTTPException) as exc:
            await self._mark_invalid(str(account["account_id"]))
            raise GatewayError(
                503,
                "Antigravity account credentials are unavailable",
                code="account_invalid",
            ) from exc
        if antigravity_should_refresh(parsed):
            lock = self._refresh_locks.setdefault(str(account["account_id"]), asyncio.Lock())
            async with lock:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                parsed = load_antigravity_credentials(payload, path=path)
                if antigravity_should_refresh(parsed):
                    try:
                        parsed = await refresh_antigravity_credentials(
                            parsed, client if client is not None else await shared_client()
                        )
                    except HTTPException as exc:
                        if exc.status_code == 401:
                            await self._mark_invalid(str(account["account_id"]))
                            raise GatewayError(
                                401,
                                "Antigravity authentication expired",
                                code="account_invalid",
                            ) from exc
                        parsed = load_antigravity_credentials(payload, path=path)
                        if not parsed.access_token:
                            raise GatewayError(
                                503,
                                "Antigravity account refresh failed",
                                code="account_invalid",
                            ) from exc
                    expires = (
                        parsed.expires_at.isoformat().replace("+00:00", "Z") if parsed.expires_at else None
                    )
                    await gateway_store.execute(
                        "UPDATE accounts SET expires_at=?,last_refresh_at=?,updated_at=? WHERE account_id=?",
                        (expires, iso_now(), iso_now(), account["account_id"]),
                    )
        return parsed

    async def workbuddy_credentials(
        self, account: dict[str, Any], *, client: httpx.AsyncClient | None = None
    ) -> WorkBuddyCredentials:
        path = Path(str(account["credential_path"]))
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            parsed = load_workbuddy_credentials(payload, path=path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            await self._mark_invalid(str(account["account_id"]))
            raise GatewayError(503, "WorkBuddy credentials are unavailable", code="account_invalid") from exc
        if workbuddy_should_refresh(parsed):
            lock = self._refresh_locks.setdefault(str(account["account_id"]), asyncio.Lock())
            async with lock:
                try:
                    parsed = load_workbuddy_credentials(
                        json.loads(path.read_text(encoding="utf-8-sig")), path=path
                    )
                    if workbuddy_should_refresh(parsed):
                        parsed = await refresh_workbuddy_credentials(
                            parsed, client if client is not None else await shared_client()
                        )
                        self._atomic_credential_write(path, parsed.raw)
                        expires = (
                            parsed.expires_at.isoformat().replace("+00:00", "Z")
                            if parsed.expires_at else None
                        )
                        await gateway_store.execute(
                            "UPDATE accounts SET expires_at=?,last_refresh_at=?,updated_at=? WHERE account_id=?",
                            (expires, iso_now(), iso_now(), account["account_id"]),
                        )
                except (OSError, ValueError, json.JSONDecodeError, HTTPException, httpx.HTTPError) as exc:
                    if isinstance(exc, HTTPException) and exc.status_code == 401:
                        await self._mark_invalid(str(account["account_id"]))
                    raise GatewayError(503, "WorkBuddy account refresh failed", code="account_invalid") from exc
        return parsed

    async def open_workbuddy_chat(
        self, account: dict[str, Any], payload: dict[str, Any], context: CallContext | None
    ) -> UpstreamLease:
        account_id = str(account["account_id"])
        await self.note_attempt(context, account)
        semaphore = self._semaphore(account_id)
        try:
            await asyncio.wait_for(semaphore.acquire(), settings.gateway_queue_timeout_seconds)
        except TimeoutError as exc:
            raise GatewayError(
                429, "WorkBuddy account concurrency limit reached", code="rate_limit_exceeded"
            ) from exc
        response: httpx.Response | None = None
        try:
            client = await shared_client()
            credentials = await self.workbuddy_credentials(account, client=client)
            request = client.build_request(
                "POST",
                workbuddy_base_url(credentials.realm) + "/v2/chat/completions",
                headers=workbuddy_chat_headers(credentials),
                json=prepare_workbuddy_chat_payload(payload, realm=credentials.realm),
                timeout=httpx.Timeout(180.0, connect=20.0, pool=settings.gateway_pool_timeout_seconds),
            )
            response = await client.send(request, stream=True)
            if response.status_code >= 400:
                retry_after = _retry_after_seconds(response)
                logger.warning("workbuddy_upstream_rejected status=%s", response.status_code)
                await self._mark_failure(account_id, response.status_code, retry_after, context=context)
                status = 429 if response.status_code == 429 else 401 if response.status_code == 401 else 502
                raise GatewayError(
                    status,
                    "WorkBuddy upstream rejected the request",
                    code="rate_limit_exceeded" if status == 429 else "upstream_error",
                    error_type="upstream_error",
                    retry_after=retry_after,
                )
            lease = UpstreamLease(
                response, account, semaphore, iterator=response.aiter_bytes(), buffered_chunks=[]
            )
            await self._bootstrap_any_stream(lease)
            return lease
        except BaseException as exc:
            if response is not None:
                await response.aclose()
            semaphore.release()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, GatewayError):
                raise
            if isinstance(exc, httpx.TimeoutException):
                raise GatewayError(
                    504, "WorkBuddy upstream timed out", code="upstream_timeout",
                    error_type="upstream_error",
                ) from exc
            if isinstance(exc, httpx.ConnectError):
                raise GatewayError(
                    502, "Cannot connect to WorkBuddy upstream", code="upstream_connection_error",
                    error_type="upstream_error",
                ) from exc
            if isinstance(exc, httpx.HTTPError):
                raise GatewayError(
                    502, "WorkBuddy upstream closed the connection", code="upstream_connection_error",
                    error_type="upstream_error",
                ) from exc
            raise GatewayError(
                502, "Cannot reach WorkBuddy upstream", code="upstream_connection_error",
                error_type="upstream_error",
            ) from exc

    async def _discover_antigravity_project(
        self,
        account: dict[str, Any],
        credentials: AntigravityCredentials,
        client: httpx.AsyncClient,
    ) -> AntigravityCredentials:
        if credentials.project_id:
            return credentials
        url = antigravity_load_code_assist_url(credentials.host)
        timeout = httpx.Timeout(30.0, connect=10.0, pool=settings.gateway_pool_timeout_seconds)
        body = {"metadata": dict(LOAD_CODE_ASSIST_METADATA)}
        headers = antigravity_build_headers(credentials, accept="application/json")
        try:
            response = await self._antigravity_post(client, url, headers=headers, json=body, timeout=timeout)
            if response.status_code == 401:
                credentials = await refresh_antigravity_credentials(credentials, client)
                headers = antigravity_build_headers(credentials, accept="application/json")
                response = await self._antigravity_post(client, url, headers=headers, json=body, timeout=timeout)
        except HTTPException as exc:
            await self._mark_invalid(str(account["account_id"]))
            raise GatewayError(401, "Antigravity authentication expired", code="account_invalid") from exc
        except httpx.HTTPError as exc:
            raise GatewayError(
                502,
                "Cannot reach Antigravity upstream",
                code="upstream_connection_error",
                error_type="upstream_error",
            ) from exc
        if response.status_code >= 400:
            raise GatewayError(
                422,
                "Antigravity credential is missing a Cloud Code project id",
                code="invalid_request",
                error_type="invalid_request_error",
            )
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = None
        project_id = extract_antigravity_project_id(payload)
        if not project_id:
            raise GatewayError(
                422,
                "Antigravity credential is missing a Cloud Code project id",
                code="invalid_request",
                error_type="invalid_request_error",
            )
        return persist_antigravity_fields(credentials, {"projectId": project_id})

    @staticmethod
    def _antigravity_upstream_error(
        status: int,
        body: Any,
        *,
        huge_system: bool,
        account_id: str,
        model: str,
    ) -> GatewayError:
        google_status = google_error_status(body)
        google_message = google_error_message(body).replace("\n", " ")[:160]
        logger.warning(
            "antigravity_upstream_rejected status=%s google_status=%s model=%s account=%s detail=%s",
            status,
            google_status or "unknown",
            model[:80],
            account_id[:8],
            google_message,
        )
        if google_status == "RESOURCE_EXHAUSTED" or status == 429:
            if huge_system:
                return GatewayError(
                    400,
                    "Antigravity request is too large",
                    code="invalid_request",
                    error_type="invalid_request_error",
                )
            return GatewayError(
                429,
                "Antigravity rate limit reached; retry later",
                code="rate_limit_exceeded",
                error_type="upstream_error",
                retry_after=2.0,
            )
        if is_google_capacity_error(status, body):
            return GatewayError(
                429,
                "Antigravity model is at capacity; retry later",
                code="rate_limit_exceeded",
                error_type="upstream_error",
                retry_after=2.0,
            )
        if status == 401 or google_status == "UNAUTHENTICATED":
            return GatewayError(401, "Antigravity authentication expired", code="account_invalid")
        if status == 403 or google_status == "PERMISSION_DENIED":
            return GatewayError(
                403,
                "Antigravity account is not allowed to use this model",
                code="upstream_error",
                error_type="upstream_error",
            )
        if status in {400, 422} or google_status in {"INVALID_ARGUMENT", "FAILED_PRECONDITION"}:
            if "location is not supported" in google_message.lower():
                detail = "User location is not supported for the API use."
            else:
                detail = "Antigravity rejected the request"
            return GatewayError(
                400,
                detail,
                code="invalid_request",
                error_type="invalid_request_error",
            )
        if status >= 500:
            return GatewayError(
                502,
                f"Antigravity upstream is temporarily unavailable (HTTP {status})",
                code="upstream_error",
                error_type="upstream_error",
            )
        return GatewayError(
            status if status >= 400 else 502,
            "Antigravity upstream request failed",
            code="upstream_error",
            error_type="upstream_error",
        )

    async def _antigravity_post(
        self,
        client: httpx.AsyncClient,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        last: httpx.HTTPError | None = None
        for attempt in range(3):
            try:
                return await client.post(url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
                await asyncio.sleep(0.4 * (attempt + 1))
        assert last is not None
        raise last

    async def _open_antigravity_image(
        self,
        account: dict[str, Any],
        *,
        json_body: dict[str, Any] | None,
        files: list[Any] | None,
        context: CallContext | None,
    ) -> dict[str, Any]:
        account_id = str(account["account_id"])
        await self.note_attempt(context, account)
        semaphore = self._semaphore(account_id)
        try:
            await asyncio.wait_for(semaphore.acquire(), settings.gateway_queue_timeout_seconds)
        except TimeoutError as exc:
            raise GatewayError(
                429,
                "Antigravity account concurrency limit reached",
                code="rate_limit_exceeded",
            ) from exc
        try:
            client = await shared_client()
            credentials = await self.antigravity_credentials(account, client=client)
            credentials = await self._discover_antigravity_project(account, credentials, client)
            if not credentials.project_id:
                raise GatewayError(
                    422,
                    "Antigravity credential is missing a Cloud Code project id",
                    code="invalid_request",
                    error_type="invalid_request_error",
                )
            values, images, mask = _image_request_assets(json_body=json_body, files=files)
            prompt = str(values.get("prompt") or "")
            model = str(values.get("model") or settings.antigravity_image_model)
            try:
                envelope = build_image_cloudcode_envelope(
                    project=credentials.project_id,
                    prompt=prompt,
                    images=images,
                    mask=mask,
                    model=model,
                )
            except HTTPException as exc:
                raise GatewayError(
                    400 if exc.status_code not in {400, 422} else exc.status_code,
                    str(exc.detail),
                    code="invalid_request",
                    error_type="invalid_request_error",
                ) from exc
            url = antigravity_generate_url(credentials.host)
            headers = antigravity_build_headers(credentials, accept="application/json")
            timeout = httpx.Timeout(
                settings.antigravity_image_timeout_seconds,
                connect=20.0,
                pool=settings.gateway_pool_timeout_seconds,
            )
            response: httpx.Response | None = None
            body: Any = None
            for attempt in range(3):
                try:
                    response = await self._antigravity_post(
                        client, url, headers=headers, json=envelope, timeout=timeout
                    )
                except httpx.HTTPError as exc:
                    await self._mark_network_failure(account_id, context=context)
                    raise GatewayError(
                        502,
                        "Cannot reach Antigravity upstream",
                        code="upstream_connection_error",
                        error_type="upstream_error",
                    ) from exc
                if response.status_code == 401:
                    try:
                        credentials = await refresh_antigravity_credentials(credentials, client)
                    except HTTPException as exc:
                        await self._mark_invalid(account_id)
                        raise GatewayError(401, "Antigravity authentication expired", code="account_invalid") from exc
                    headers = antigravity_build_headers(credentials, accept="application/json")
                    try:
                        response = await self._antigravity_post(
                            client, url, headers=headers, json=envelope, timeout=timeout
                        )
                    except httpx.HTTPError as exc:
                        await self._mark_network_failure(account_id, context=context)
                        raise GatewayError(
                            502,
                            "Cannot reach Antigravity upstream",
                            code="upstream_connection_error",
                            error_type="upstream_error",
                        ) from exc
                if response.status_code < 400:
                    body = None
                    break
                try:
                    body = response.json()
                except json.JSONDecodeError:
                    body = None
                if not is_google_capacity_error(response.status_code, body) or attempt >= 2:
                    break
                await asyncio.sleep(0.7 * (attempt + 1))
            if response is None:
                raise GatewayError(
                    502,
                    "Cannot reach Antigravity upstream",
                    code="upstream_connection_error",
                    error_type="upstream_error",
                )
            if response.status_code >= 400:
                retry_after = _retry_after_seconds(response)
                capacity = is_google_capacity_error(response.status_code, body)
                if response.status_code not in {400, 422} and not capacity:
                    await self._mark_failure(account_id, response.status_code, retry_after, context=context)
                error = self._antigravity_upstream_error(
                    response.status_code,
                    body,
                    huge_system=False,
                    account_id=account_id,
                    model=model,
                )
                if retry_after and error.status == 429:
                    error.retry_after = retry_after
                raise error
            try:
                encoded = extract_image_b64_from_cloudcode(response.content)
            except HTTPException as exc:
                raise GatewayError(
                    502,
                    str(exc.detail),
                    code="upstream_invalid_response",
                    error_type="upstream_error",
                ) from exc
            await gateway_store.execute(
                "UPDATE accounts SET network_failures=0,cooldown_until=NULL,updated_at=? WHERE account_id=?",
                (iso_now(), account_id),
            )
            return {"created": int(time.time()), "data": [{"b64_json": encoded}]}
        finally:
            semaphore.release()

    async def _open_antigravity_text(
        self,
        account: dict[str, Any],
        payload: dict[str, Any],
        context: CallContext | None = None,
    ) -> SyntheticLease:
        account_id = str(account["account_id"])
        await self.note_attempt(context, account)
        semaphore = self._semaphore(account_id)
        try:
            await asyncio.wait_for(semaphore.acquire(), settings.gateway_queue_timeout_seconds)
        except TimeoutError as exc:
            raise GatewayError(
                429,
                "Antigravity account concurrency limit reached",
                code="rate_limit_exceeded",
            ) from exc
        try:
            client = await shared_client()
            credentials = await self.antigravity_credentials(account, client=client)
            credentials = await self._discover_antigravity_project(account, credentials, client)
            if not credentials.project_id:
                raise GatewayError(
                    422,
                    "Antigravity credential is missing a Cloud Code project id",
                    code="invalid_request",
                    error_type="invalid_request_error",
                )
            try:
                envelope = responses_to_cloudcode(payload, project=credentials.project_id)
            except HTTPException as exc:
                raise GatewayError(
                    400 if exc.status_code not in {400, 422} else exc.status_code,
                    str(exc.detail),
                    code="invalid_request",
                    error_type="invalid_request_error",
                ) from exc
            claude_names = envelope.pop("_ts_claude_names", None)
            huge_system = is_huge_system_instruction(envelope)
            request_body = envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
            fc_n = 0
            fc_signed = 0
            image_n = 0
            last_role = ""
            contents = request_body.get("contents") or []
            for content in contents:
                if not isinstance(content, dict):
                    continue
                last_role = str(content.get("role") or "")
                for part in content.get("parts") or []:
                    if not isinstance(part, dict):
                        continue
                    if "functionCall" in part:
                        fc_n += 1
                        if part.get("thoughtSignature"):
                            fc_signed += 1
                    if "inlineData" in part or "fileData" in part:
                        image_n += 1
            logger.info(
                "antigravity_dispatch model=%s fc_n=%s fc_signed=%s image_n=%s last_role=%s",
                str(payload.get("model") or "")[:80],
                fc_n,
                fc_signed,
                image_n,
                last_role or "none",
            )
            url = antigravity_generate_url(credentials.host)
            headers = antigravity_build_headers(credentials, accept="application/json")
            timeout = httpx.Timeout(
                settings.codex_chat_timeout_seconds,
                connect=20.0,
                pool=settings.gateway_pool_timeout_seconds,
            )
            response: httpx.Response | None = None
            body: Any = None
            for attempt in range(3):
                try:
                    response = await self._antigravity_post(
                        client, url, headers=headers, json=envelope, timeout=timeout
                    )
                except httpx.HTTPError as exc:
                    await self._mark_network_failure(account_id, context=context)
                    raise GatewayError(
                        502,
                        "Cannot reach Antigravity upstream",
                        code="upstream_connection_error",
                        error_type="upstream_error",
                    ) from exc
                if response.status_code == 401:
                    try:
                        credentials = await refresh_antigravity_credentials(credentials, client)
                    except HTTPException as exc:
                        await self._mark_invalid(account_id)
                        raise GatewayError(401, "Antigravity authentication expired", code="account_invalid") from exc
                    headers = antigravity_build_headers(credentials, accept="application/json")
                    try:
                        response = await self._antigravity_post(
                            client, url, headers=headers, json=envelope, timeout=timeout
                        )
                    except httpx.HTTPError as exc:
                        await self._mark_network_failure(account_id, context=context)
                        raise GatewayError(
                            502,
                            "Cannot reach Antigravity upstream",
                            code="upstream_connection_error",
                            error_type="upstream_error",
                        ) from exc
                if response.status_code < 400:
                    body = None
                    break
                try:
                    body = response.json()
                except json.JSONDecodeError:
                    body = None
                if not is_google_capacity_error(response.status_code, body) or attempt >= 2:
                    break
                await asyncio.sleep(0.7 * (attempt + 1))
            if response is None:
                raise GatewayError(
                    502,
                    "Cannot reach Antigravity upstream",
                    code="upstream_connection_error",
                    error_type="upstream_error",
                )
            if response.status_code >= 400:
                retry_after = _retry_after_seconds(response)
                capacity = is_google_capacity_error(response.status_code, body)
                if response.status_code not in {400, 422} and not capacity:
                    await self._mark_failure(account_id, response.status_code, retry_after, context=context)
                error = self._antigravity_upstream_error(
                    response.status_code,
                    body,
                    huge_system=huge_system,
                    account_id=account_id,
                    model=str(payload.get("model") or ""),
                )
                if retry_after and error.status == 429:
                    error.retry_after = retry_after
                raise error
            await gateway_store.execute(
                "UPDATE accounts SET network_failures=0,cooldown_until=NULL,updated_at=? WHERE account_id=?",
                (iso_now(), account_id),
            )
            return SyntheticLease(
                chunks=[
                    cloudcode_bytes_to_codex_sse(
                        response.content,
                        model=str(payload.get("model") or ANTIGRAVITY_DEFAULT_MODEL),
                        name_map=claude_names if isinstance(claude_names, dict) else None,
                    )
                ],
                account=account,
            )
        finally:
            semaphore.release()

    async def open_responses(
        self,
        account: dict[str, Any],
        payload: dict[str, Any],
        context: CallContext | None = None,
    ) -> UpstreamLease:
        account_id = str(account["account_id"])
        await self.note_attempt(context, account)
        semaphore = self._semaphore(account_id)
        try:
            await asyncio.wait_for(semaphore.acquire(), settings.gateway_queue_timeout_seconds)
        except TimeoutError as exc:
            raise GatewayError(429, "Codex account concurrency limit reached", code="rate_limit_exceeded") from exc
        try:
            credentials = await self.credentials(account)
            client = await shared_client()
            sent_model = str(payload.get("model") or "")[:80]
            lite = responses_lite_enabled(sent_model)
            url = f"{upstream_base_url(credentials.auth_mode)}/responses"
            client_params = upstream_client_params()
            logger.info(
                "codex_upstream_dispatch request_id=%s client_model=%s sent_model=%s lite=%s "
                "originator=%s version=%s url=%s user_agent=%r",
                context.request_id if context is not None else "",
                _safe_log_text(context.client_model if context is not None else "", 80),
                sent_model,
                lite,
                settings.codex_originator,
                client_params["client_version"],
                url,
                _safe_log_text(context.user_agent if context is not None else ""),
            )
            request = client.build_request(
                "POST",
                url,
                params=client_params,
                headers=build_headers(credentials, lite=lite),
                json=payload,
                timeout=httpx.Timeout(
                    settings.codex_chat_timeout_seconds,
                    connect=20.0,
                    pool=settings.gateway_pool_timeout_seconds,
                ),
            )
            first_token_timeout = max(0.0, settings.gateway_first_token_timeout_seconds)
            first_token_deadline = (
                asyncio.get_running_loop().time() + first_token_timeout
                if first_token_timeout > 0
                else None
            )
            try:
                if first_token_deadline is None:
                    response = await client.send(request, stream=True)
                else:
                    response = await asyncio.wait_for(
                        client.send(request, stream=True),
                        timeout=first_token_timeout,
                    )
            except TimeoutError as exc:
                raise self._first_token_timeout(first_token_timeout) from exc
            if response.status_code >= 400:
                await response.aread()
                metadata = upstream_error_metadata(response)
                logger.warning(
                    "upstream_rejected status=%s model=%s error_type=%s error_code=%s "
                    "error_param=%s payload_shape=%s",
                    response.status_code,
                    str(payload.get("model") or "")[:80],
                    metadata.get("type", "unknown"),
                    metadata.get("code", "unknown"),
                    metadata.get("param", "unknown"),
                    _payload_shape(payload),
                )
                retry_after = _retry_after_seconds(response)
                await self._mark_failure(account_id, response.status_code, retry_after, context=context)
                message = _upstream_message(response)
                status, detail = map_upstream_status(response.status_code, message)
                await response.aclose()
                raise GatewayError(
                    status,
                    detail,
                    code="rate_limit_exceeded" if status == 429 else "upstream_error",
                    error_type="upstream_error",
                    retry_after=retry_after,
                )
            await gateway_store.execute(
                "UPDATE accounts SET network_failures=0,cooldown_until=NULL,updated_at=? WHERE account_id=?",
                (iso_now(), account_id),
            )
            lease = UpstreamLease(
                response,
                account,
                semaphore,
                iterator=response.aiter_bytes(),
                buffered_chunks=[],
            )
            await self._bootstrap_stream(lease, deadline=first_token_deadline)
            return lease
        except GatewayError:
            if "response" in locals():
                await response.aclose()
            semaphore.release()
            raise
        except httpx.PoolTimeout as exc:
            rotated = await rotate_shared_client(
                client if "client" in locals() else None,
                reason="pool_timeout",
            )
            logger.error("upstream_pool_timeout rotated=%s", rotated)
            await self._mark_network_failure(account_id, context=context)
            semaphore.release()
            raise GatewayError(
                502,
                "Cannot reach Codex upstream",
                code="upstream_connection_error",
                error_type="upstream_error",
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            await self._mark_network_failure(account_id, context=context)
            semaphore.release()
            raise GatewayError(502, "Cannot reach Codex upstream", code="upstream_connection_error", error_type="upstream_error") from exc

    async def _bootstrap_stream(
        self,
        lease: UpstreamLease,
        *,
        deadline: float | None = None,
    ) -> None:
        """Buffer through the first meaningful SSE token or fail the request."""
        iterator = lease.iterator
        assert iterator is not None
        timeout_seconds = max(0.0, settings.gateway_first_token_timeout_seconds)
        if timeout_seconds == 0:
            return
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + timeout_seconds
        parsed_buffer = ""
        byte_count = 0
        while byte_count < max(1024, settings.gateway_stream_bootstrap_max_bytes):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise self._first_token_timeout(timeout_seconds)
            try:
                chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
            except TimeoutError as exc:
                raise self._first_token_timeout(timeout_seconds) from exc
            except StopAsyncIteration:
                return
            assert lease.buffered_chunks is not None
            lease.buffered_chunks.append(chunk)
            byte_count += len(chunk)
            parsed_buffer += chunk.decode("utf-8", errors="replace")
            blocks, parsed_buffer = iter_sse_blocks(parsed_buffer)
            for block in blocks:
                event, data = parse_sse_event(block)
                event_type, payload = _sse_event_payload(event, data)
                if event_type in {"response.failed", "error"}:
                    message = _sse_error_message(payload)
                    raise GatewayError(
                        502,
                        message,
                        code="upstream_error",
                        error_type="upstream_error",
                    )
                if event_type == "response.incomplete":
                    raise GatewayError(
                        502,
                        "Codex upstream returned an incomplete response before output",
                        code="upstream_error",
                        error_type="upstream_error",
                    )
                if (
                    event_type == "response.completed"
                    or event_type.endswith(".delta")
                    or event_type == "response.image_generation_call.partial_image"
                ):
                    return
        raise GatewayError(
            502,
            "Codex upstream sent too much metadata before its first token",
            code="upstream_invalid_response",
            error_type="upstream_error",
        )

    @staticmethod
    def _first_token_timeout(timeout_seconds: float) -> GatewayError:
        logger.warning(
            "upstream_first_token_timeout timeout_seconds=%s",
            round(timeout_seconds, 3),
        )
        return GatewayError(
            504,
            "Codex upstream did not produce a first token in time",
            code="upstream_first_token_timeout",
            error_type="timeout_error",
        )

    async def _preferred_provider(self, key: dict[str, Any]) -> str:
        routes = await gateway_store.all(
            "SELECT provider, preferred_account_id FROM api_key_routes WHERE key_id=?",
            (str(key["id"]),),
        )
        if not routes:
            return "codex"
        ordered = sorted(
            routes,
            key=lambda row: (
                0 if str(row.get("provider") or "") == "codex" else 1,
                str(row.get("provider") or ""),
            ),
        )
        for row in ordered:
            if not row.get("preferred_account_id"):
                continue
            provider = str(row.get("provider") or "").strip().lower()
            return provider if provider in {"codex", "grok", "antigravity", "workbuddy"} else "codex"
        return "codex"

    def _require_ready_provider(self, provider: str) -> None:
        try:
            adapter = get_adapter(provider)
        except KeyError as exc:
            raise GatewayError(404, f"Unknown provider '{provider}'", code="unknown_provider") from exc
        if not adapter.ready:
            raise GatewayError(
                501,
                f"{adapter.display_name} is not ready",
                code="provider_not_ready",
                error_type="invalid_request_error",
            )

    async def _dispatch_antigravity_text(
        self,
        payload: dict[str, Any],
        account: dict[str, Any],
        context: CallContext | None,
    ) -> SyntheticLease:
        model = str(payload.get("model") or "")
        if not antigravity_is_claude_model(model) and _should_synthetic_image_generation(payload):
            return await self._synthetic_image_generation_lease(
                payload, account, context, provider="antigravity"
            )
        _strip_hosted_image_generation(payload)
        return await self._open_antigravity_text(account, payload, context)

    async def response_request(
        self,
        body: dict[str, Any],
        key: dict[str, Any],
        session_id: str | None,
        context: CallContext | None = None,
    ) -> tuple[UpstreamLease | SyntheticLease | WorkBuddyResponsesLease, dict[str, Any], bool]:
        body = compact_payload_images(dict(body))
        hint = await self._preferred_provider(key)
        provider, model = resolve_provider_model(
            body.get("model") if isinstance(body.get("model"), str) else None,
            default_provider=hint,
            default_model=default_model_for(hint, "text"),
        )
        self._require_ready_provider(provider)
        if context is not None:
            context.model = model
            context.provider = provider
        if is_image_model(model):
            raise GatewayError(
                400,
                "Image models cannot be used as /v1/responses model. "
                "Generate with /v1/images/generations or edit with /v1/images/edits.",
                code="invalid_request",
                error_type="invalid_request_error",
            )
        previous_response_id = _string(body.get("previous_response_id"))
        conversation = _conversation_id(body.get("conversation"))
        account = await self.select_account(
            key,
            kind="responses",
            session_id=session_id,
            previous_response_id=previous_response_id,
            conversation=conversation,
            provider=provider,
            model=model,
            context=context,
        )
        payload = compact_payload_images(dict(body))
        payload["model"] = model
        client_stream = payload.get("stream") is True
        await self._prepare_budget(context, payload, supports_limit=not (provider == "codex" and responses_lite_enabled(model)))
        if provider == "codex" and responses_lite_enabled(model):
            # Codex Responses Lite rejects this otherwise-standard Responses
            # field. Accept it from callers for API compatibility, but do not
            # send it to the subscription-backed upstream.
            payload.pop("max_output_tokens", None)
        if context is not None:
            context.model = model
            context.is_stream = client_stream
            context.provider = provider
        payload["stream"] = True
        payload["store"] = False
        if provider == "workbuddy":
            if _should_synthetic_image_generation(payload):
                lease = await self._synthetic_image_generation_lease(
                    payload, account, context, provider="workbuddy"
                )
                return lease, payload, client_stream
            try:
                chat_payload = workbuddy_responses_to_chat(payload, model=model)
            except ValueError as exc:
                raise GatewayError(
                    400, str(exc), code="invalid_request", error_type="invalid_request_error"
                ) from exc
            chat_lease = await self.open_workbuddy_chat(account, chat_payload, context)
            if client_stream:
                lease = WorkBuddyResponsesLease(
                    upstream=chat_lease,
                    model=model,
                    chat_payload=chat_payload,
                    run_hosted=lambda calls: self._workbuddy_hosted_followup(
                        account, model, chat_payload, calls, context
                    ),
                )
            else:
                completion = await self._collect_openai_chat(chat_lease, model=model)
                completed = workbuddy_chat_to_response(completion, model=model)
                lease = SyntheticLease(chunks=workbuddy_response_sse(completed), account=account)
            if context is not None:
                context.text_mode = "responses_sse"
        elif provider == "antigravity":
            lease = await self._dispatch_antigravity_text(payload, account, context)
        elif provider == "grok":
            if _has_grok_coding_tools(_iter_client_tools(payload)) and not (
                _tool_choice_forces_image_generation(payload.get("tool_choice"))
            ):
                _strip_hosted_image_generation(payload)
            lease = await self._open_grok_text_with_retries(
                account,
                payload,
                key,
                context,
                path="/responses",
                allow_account_switch=True,
                cache_value=conversation or session_id,
            )
        else:
            if _should_synthetic_image_generation(payload):
                lease = await self._synthetic_image_generation_lease(
                    payload, account, context, provider="codex"
                )
                return lease, payload, client_stream
            _strip_hosted_image_generation(payload)
            lease = await self._open_text_with_retries(
                account,
                payload,
                key,
                context,
                allow_account_switch=True,
            )
        return lease, payload, client_stream

    async def _synthetic_image_generation_lease(
        self,
        payload: dict[str, Any],
        account: dict[str, Any],
        context: CallContext | None,
        *,
        provider: str,
    ) -> SyntheticLease:
        prompt, images, mask = _extract_responses_image_assets(payload)
        kind = _responses_image_kind(images=images, mask=mask, payload=payload)
        image_model = canonical_image_model(provider)
        json_body: dict[str, Any] = {"model": image_model, "prompt": prompt}
        if kind == "edit":
            if len(images) == 1:
                json_body["image"] = images[0]
            elif images:
                json_body["image"] = images
            if mask:
                json_body["mask"] = mask
        result = await self._post_image(
            account,
            "generations" if kind == "generation" else "edits",
            json_body=json_body,
            context=context,
            provider=provider,
        )
        encoded = await _b64_from_images_json(result)
        if context:
            context.billed_images += len(result.get("data") or [1])
        response_id = f"resp_img_{uuid.uuid4().hex[:12]}"
        model = str(payload.get("model") or default_model_for(provider, "text"))
        sse = _synthetic_image_generation_sse(
            encoded=encoded, response_id=response_id, model=model
        )
        return SyntheticLease(chunks=[sse], account=account)

    async def chat_request(
        self,
        body: dict[str, Any],
        key: dict[str, Any],
        session_id: str | None,
        context: CallContext | None = None,
    ) -> TextDispatch:
        body = compact_payload_images(dict(body))
        hint = await self._preferred_provider(key)
        provider, model = resolve_provider_model(
            body.get("model") if isinstance(body.get("model"), str) else None,
            default_provider=hint,
            default_model=default_model_for(hint, "text"),
        )
        self._require_ready_provider(provider)
        client_stream = body.get("stream") is True
        if context is not None:
            context.model = model
            context.is_stream = client_stream
            context.provider = provider
        await self._prepare_budget(context, body, supports_limit=not (provider == "codex" and responses_lite_enabled(model)))
        account = await self.select_account(
            key, kind="chat", session_id=session_id, provider=provider, model=model, context=context
        )
        if provider == "grok":
            return await self._grok_chat_request(
                body, key, account, session_id, context, model=model, stream=client_stream
            )
        if provider == "workbuddy":
            native = compact_payload_images(dict(body))
            native["model"] = model
            native["stream"] = True
            lease = await self.open_workbuddy_chat(account, native, context)
            if context is not None:
                context.text_mode = "chat_sse"
            if client_stream:
                return TextDispatch(lease=lease, payload=native, stream=True, mode="chat_sse")
            completed = await self._collect_openai_chat(lease, model=model)
            return TextDispatch(lease=None, payload=native, stream=False, mode="json", json_body=completed)
        if provider == "antigravity":
            payload, _lite = to_responses_payload(
                {**body, "model": model}, default_model=ANTIGRAVITY_DEFAULT_MODEL
            )
            if context is not None:
                context.model = str(payload.get("model") or model)
                context.text_mode = "responses_sse"
            lease = await self._dispatch_antigravity_text(payload, account, context)
            return TextDispatch(lease=lease, payload=payload, stream=client_stream, mode="responses_sse")
        payload, _lite = to_responses_payload({**body, "model": model}, default_model=settings.codex_default_model)
        if context is not None:
            context.model = str(payload.get("model") or model)
            context.text_mode = "responses_sse"
        lease = await self._open_text_with_retries(
            account,
            payload,
            key,
            context,
            allow_account_switch=True,
        )
        return TextDispatch(lease=lease, payload=payload, stream=client_stream, mode="responses_sse")

    async def _open_text_with_retries(
        self,
        account: dict[str, Any],
        payload: dict[str, Any],
        key: dict[str, Any],
        context: CallContext | None,
        *,
        allow_account_switch: bool,
    ) -> UpstreamLease:
        apply_responses_lite_contract(payload)
        strip_bare_reasoning(payload)
        if bool(key.get("fast_enabled")):
            payload["service_tier"] = "priority"
        if context and context.route_bound:
            allow_account_switch = False
        provider = account_provider(account)
        current = account
        for retry_index in range(len(TEXT_RETRY_DELAYS_SECONDS) + 1):
            try:
                return await self.open_responses(current, payload, context)
            except GatewayError as exc:
                if retry_index >= len(TEXT_RETRY_DELAYS_SECONDS) or not self._retryable_text_error(exc):
                    raise

                next_account = current
                switched = False
                if allow_account_switch:
                    try:
                        candidate = await self.select_account(
                            key, kind="retry", provider=provider,
                            model=str(payload.get("model") or ""),
                        )
                        if account_provider(candidate) == provider:
                            next_account = candidate
                            switched = (
                                str(candidate.get("account_id"))
                                != str(current.get("account_id"))
                            )
                        else:
                            logger.error(
                                "provider_failover_blocked requested=%s actual=%s account=%s",
                                provider,
                                account_provider(candidate),
                                str(candidate.get("account_id") or "")[:8],
                            )
                    except GatewayError as fallback_exc:
                        logger.warning(
                            "provider_failover_unavailable provider=%s account=%s "
                            "failure_code=%s fallback_code=%s retrying_same_account=true",
                            provider,
                            str(current.get("account_id") or "")[:8],
                            exc.code,
                            fallback_exc.code,
                        )
                        if exc.status == 401:
                            raise exc

                delay = TEXT_RETRY_DELAYS_SECONDS[retry_index]
                logger.warning(
                    "upstream_retry_scheduled provider=%s retry=%s delay_seconds=%s status=%s "
                    "code=%s account=%s switched=%s",
                    provider,
                    retry_index + 1,
                    int(delay),
                    exc.status,
                    exc.code,
                    str(current["account_id"])[:8],
                    switched,
                )
                await _retry_pause(delay)
                current = next_account

        raise RuntimeError("unreachable")

    @staticmethod
    def _retryable_text_error(exc: GatewayError) -> bool:
        return exc.status in {401, 429, 502, 503} and exc.code in {
            "account_invalid",
            "rate_limit_exceeded",
            "upstream_error",
            "upstream_connection_error",
        }

    async def _grok_chat_request(
        self,
        body: dict[str, Any],
        key: dict[str, Any],
        account: dict[str, Any],
        session_id: str | None,
        context: CallContext | None,
        *,
        model: str,
        stream: bool,
    ) -> TextDispatch:
        native = dict(body)
        native["model"] = model
        native["stream"] = True
        stream_options = native.get("stream_options")
        merged_options = dict(stream_options) if isinstance(stream_options, dict) else {}
        merged_options["include_usage"] = True
        native["stream_options"] = merged_options
        try:
            lease = await self._open_grok_text_with_retries(
                account,
                native,
                key,
                context,
                path="/chat/completions",
                allow_account_switch=True,
                cache_value=session_id,
            )
            if context is not None:
                context.text_mode = "chat_sse"
            if stream:
                return TextDispatch(lease=lease, payload=native, stream=True, mode="chat_sse")
            completed = await self._collect_openai_chat(lease, model=model)
            return TextDispatch(
                lease=None, payload=native, stream=False, mode="json", json_body=completed
            )
        except GatewayError as exc:
            if exc.status != 404:
                raise
        payload, _lite = to_responses_payload(
            {**body, "model": model}, default_model=settings.grok_default_model
        )
        if context is not None:
            context.text_mode = "responses_sse"
        lease = await self._open_grok_text_with_retries(
            account,
            payload,
            key,
            context,
            path="/responses",
            allow_account_switch=True,
            cache_value=session_id,
        )
        return TextDispatch(lease=lease, payload=payload, stream=stream, mode="responses_sse")

    async def _open_grok_text_with_retries(
        self,
        account: dict[str, Any],
        payload: dict[str, Any],
        key: dict[str, Any],
        context: CallContext | None,
        *,
        path: str,
        allow_account_switch: bool,
        cache_value: str | None = None,
    ) -> UpstreamLease | SyntheticLease:
        if path.rstrip("/").endswith("chat/completions") and isinstance(payload, dict):
            payload = sanitize_grok_chat_payload(payload)
        if path.rstrip("/").endswith("responses") and isinstance(payload, dict):
            sanitized = sanitize_grok_responses(payload)
            payload = sanitized.payload
            rewrite = sanitized.rewrite
            if not sanitized.compact_v2:
                cache_key = (
                    self._session_binding_hash(str(key["id"]), cache_value, "grok")
                    if cache_value
                    else None
                )
                rewrite = self._apply_grok_tool_cache(
                    payload, rewrite, cache_key=cache_key, remember=True
                )
            if context is not None:
                context.grok_freeform_tools = rewrite.freeform_tool_names
                context.grok_rewrite = rewrite
                context.grok_compact_v2 = sanitized.compact_v2
            if sanitized.compact_v2:
                if not compact_history_usable(payload):
                    sse = grok_compact_v2_sse(
                        response_id=f"resp_compact_{uuid.uuid4().hex[:12]}",
                        failed_code="invalid_prompt",
                        failed_message="Grok compact requires inline history; previous_response_id is not stored",
                    )
                    if context is not None:
                        context.grok_compact_direct = True
                    return SyntheticLease(chunks=[f"{sse}\n\n".encode()], account=account)
                payload = build_grok_compact_payload(payload)
        if isinstance(payload, dict):
            self._apply_grok_fast_tier(payload, key, account)
            if path.rstrip("/").endswith("responses"):
                tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
                logger.info(
                    "grok_dispatch fast=%s auth=%s shell_mapped=%s tools_n=%s",
                    "priority" if payload.get("service_tier") == "priority" else "omit",
                    str(account.get("auth_mode") or "unknown"),
                    sum(
                        1
                        for item in tools
                        if isinstance(item, dict)
                        and str(item.get("name") or "") in _SHELL_TOOL_NAMES
                    ),
                    len(tools),
                )
        provider = account_provider(account)
        if provider != "grok":
            raise GatewayError(
                500,
                "Grok retry received an account from another provider",
                code="provider_mismatch",
                error_type="gateway_error",
            )
        current = account
        if context and context.route_bound:
            allow_account_switch = False
        for retry_index in range(len(TEXT_RETRY_DELAYS_SECONDS) + 1):
            try:
                return await self.open_grok_request(current, "POST", path, json_body=payload, context=context)
            except GatewayError as exc:
                if retry_index >= len(TEXT_RETRY_DELAYS_SECONDS) or not self._retryable_text_error(exc):
                    raise
                next_account = current
                switched = False
                if allow_account_switch:
                    try:
                        candidate = await self.select_account(
                            key, kind="retry", provider=provider,
                            model=str(payload.get("model") or ""),
                        )
                        if account_provider(candidate) == provider:
                            next_account = candidate
                            switched = (
                                str(candidate.get("account_id"))
                                != str(current.get("account_id"))
                            )
                        else:
                            logger.error(
                                "provider_failover_blocked requested=%s actual=%s account=%s",
                                provider,
                                account_provider(candidate),
                                str(candidate.get("account_id") or "")[:8],
                            )
                    except GatewayError as fallback_exc:
                        logger.warning(
                            "provider_failover_unavailable provider=%s account=%s "
                            "failure_code=%s fallback_code=%s retrying_same_account=true",
                            provider,
                            str(current.get("account_id") or "")[:8],
                            exc.code,
                            fallback_exc.code,
                        )
                        if exc.status == 401:
                            raise exc
                delay = TEXT_RETRY_DELAYS_SECONDS[retry_index]
                logger.warning(
                    "upstream_retry_scheduled provider=%s retry=%s delay_seconds=%s status=%s "
                    "code=%s account=%s switched=%s",
                    provider,
                    retry_index + 1,
                    int(delay),
                    exc.status,
                    exc.code,
                    str(current["account_id"])[:8],
                    switched,
                )
                await _retry_pause(delay)
                current = next_account
        raise RuntimeError("unreachable")

    async def open_grok_request(
        self,
        account: dict[str, Any],
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        files: list[Any] | None = None,
        stream: bool = True,
        context: CallContext | None = None,
        timeout_seconds: float | None = None,
        accept: str = "text/event-stream",
    ) -> UpstreamLease | dict[str, Any] | httpx.Response:
        account_id = str(account["account_id"])
        await self.note_attempt(context, account)
        semaphore = self._semaphore(account_id)
        try:
            await asyncio.wait_for(semaphore.acquire(), settings.gateway_queue_timeout_seconds)
        except TimeoutError as exc:
            raise GatewayError(429, "Grok account concurrency limit reached", code="rate_limit_exceeded") from exc
        response: httpx.Response | None = None
        try:
            credentials = await self.grok_credentials(account)
            headers = grok_build_headers(
                credentials, accept=accept if stream else "application/json"
            )
            if files is not None:
                headers.pop("Content-Type", None)
            client = await shared_client()
            url = f"{grok_upstream_base_url(credentials.auth_mode)}{path}"
            timeout = httpx.Timeout(
                timeout_seconds or settings.grok_chat_timeout_seconds,
                connect=20.0,
                pool=settings.gateway_pool_timeout_seconds,
            )
            request = client.build_request(
                method,
                url,
                headers=headers,
                json=json_body if files is None else None,
                files=files,
                timeout=timeout,
            )
            first_token_timeout = max(0.0, settings.gateway_first_token_timeout_seconds) if stream else 0.0
            try:
                if stream and first_token_timeout > 0:
                    response = await asyncio.wait_for(
                        client.send(request, stream=True),
                        timeout=first_token_timeout,
                    )
                else:
                    response = await client.send(request, stream=stream)
            except TimeoutError as exc:
                raise self._first_token_timeout(first_token_timeout) from exc
            if response.status_code >= 400:
                await response.aread()
                metadata = upstream_error_metadata(response)
                message = _upstream_message(response)
                logger.warning(
                    "grok_upstream_rejected status=%s path=%s model=%s account=%s "
                    "error_type=%s error_code=%s error_param=%s reject_hint=%s payload_shape=%s",
                    response.status_code,
                    path,
                    str((json_body or {}).get("model") or "")[:80],
                    account_id[:8],
                    metadata.get("type", "unknown"),
                    metadata.get("code", "unknown"),
                    metadata.get("param", "unknown"),
                    grok_reject_hint(message),
                    _payload_shape(json_body) if isinstance(json_body, dict) else "none",
                )
                retry_after = _retry_after_seconds(response)
                if response.status_code != 404:
                    await self._mark_failure(account_id, response.status_code, retry_after, context=context)
                status, detail = grok_map_upstream_status(response.status_code, message)
                await response.aclose()
                raise GatewayError(
                    status,
                    detail,
                    code="rate_limit_exceeded" if status == 429 else "upstream_error",
                    error_type="upstream_error",
                    retry_after=retry_after,
                )
            await gateway_store.execute(
                "UPDATE accounts SET network_failures=0,cooldown_until=NULL,updated_at=? WHERE account_id=?",
                (iso_now(), account_id),
            )
            if not stream:
                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise GatewayError(
                        502,
                        "Grok upstream returned invalid JSON",
                        code="upstream_invalid_response",
                        error_type="upstream_error",
                    ) from exc
                await response.aclose()
                semaphore.release()
                if not isinstance(payload, dict):
                    raise GatewayError(
                        502,
                        "Grok upstream returned invalid JSON",
                        code="upstream_invalid_response",
                        error_type="upstream_error",
                    )
                return payload
            lease = UpstreamLease(
                response,
                account,
                semaphore,
                iterator=response.aiter_bytes(),
                buffered_chunks=[],
            )
            await self._bootstrap_any_stream(lease)
            return lease
        except GatewayError:
            if response is not None:
                await response.aclose()
            semaphore.release()
            raise
        except httpx.PoolTimeout as exc:
            rotated = await rotate_shared_client(
                client if "client" in locals() else None,
                reason="pool_timeout",
            )
            logger.error("upstream_pool_timeout rotated=%s", rotated)
            await self._mark_network_failure(account_id, context=context)
            semaphore.release()
            raise GatewayError(
                502,
                "Cannot reach Grok upstream",
                code="upstream_connection_error",
                error_type="upstream_error",
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            await self._mark_network_failure(account_id, context=context)
            semaphore.release()
            raise GatewayError(
                502,
                "Cannot reach Grok upstream",
                code="upstream_connection_error",
                error_type="upstream_error",
            ) from exc

    async def _bootstrap_any_stream(self, lease: UpstreamLease) -> None:
        iterator = lease.iterator
        assert iterator is not None
        timeout_seconds = max(0.0, settings.gateway_first_token_timeout_seconds)
        if timeout_seconds == 0:
            return
        try:
            chunk = await asyncio.wait_for(anext(iterator), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise self._first_token_timeout(timeout_seconds) from exc
        except StopAsyncIteration:
            return
        assert lease.buffered_chunks is not None
        lease.buffered_chunks.append(chunk)

    async def _collect_openai_chat(self, lease: UpstreamLease, *, model: str) -> dict[str, Any]:
        decoder = codecs.getincrementaldecoder("utf-8")()
        buffer = ""
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, Any] | None = None
        response_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        try:
            async for chunk in lease.aiter_bytes():
                buffer += decoder.decode(chunk)
                blocks, buffer = iter_sse_blocks(buffer)
                for block in blocks:
                    _event, data = parse_sse_event(block)
                    if not data or data == "[DONE]":
                        continue
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if isinstance(payload.get("id"), str) and payload["id"]:
                        response_id = payload["id"]
                    if isinstance(payload.get("usage"), dict):
                        usage = payload["usage"]
                    choices = payload.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    if isinstance(choice.get("finish_reason"), str) and choice["finish_reason"]:
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                    piece = delta.get("content") if isinstance(delta.get("content"), str) else None
                    if piece is None and isinstance(message.get("content"), str):
                        piece = message["content"]
                    if piece:
                        content_parts.append(piece)
                    calls = delta.get("tool_calls") or message.get("tool_calls")
                    if isinstance(calls, list):
                        for position, call in enumerate(calls):
                            if not isinstance(call, dict):
                                continue
                            index = call.get("index") if isinstance(call.get("index"), int) else position
                            entry = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if isinstance(call.get("id"), str):
                                entry["id"] = call["id"]
                            function = call.get("function") if isinstance(call.get("function"), dict) else {}
                            if isinstance(function.get("name"), str):
                                entry["function"]["name"] += function["name"]
                            if isinstance(function.get("arguments"), str):
                                entry["function"]["arguments"] += function["arguments"]
            assistant_message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
            if tool_calls:
                assistant_message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
            result = {
                "id": response_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": assistant_message,
                        "finish_reason": "tool_calls" if tool_calls else finish_reason,
                    }
                ],
            }
            if isinstance(usage, dict):
                prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                total = int(usage.get("total_tokens") or prompt + completion)
                if prompt or completion or total:
                    result["usage"] = usage
            return result
        finally:
            await lease.close()

    async def stream_passthrough(
        self,
        lease: UpstreamLease,
        key: dict[str, Any],
        context: CallContext,
    ) -> AsyncIterator[bytes]:
        completed = False
        usage: dict[str, Any] | None = None
        buffer = ""
        try:
            async for chunk in lease.aiter_bytes():
                if b"[DONE]" in chunk or b"finish_reason" in chunk:
                    completed = True
                buffer += chunk.decode("utf-8", errors="replace")
                blocks, buffer = iter_sse_blocks(buffer)
                for block in blocks:
                    event, data = parse_sse_event(block)
                    extracted = _usage_from_sse_data(data)
                    if extracted is not None:
                        usage = extracted
                    context.delivered_output_bytes += _output_delta_bytes(event, data)
                yield chunk
            if buffer.strip():
                extracted = _usage_from_sse_data(parse_sse_event(buffer)[1])
                if extracted is not None:
                    usage = extracted
        except asyncio.CancelledError:
            await self._finish_stream(
                context,
                status="interrupted",
                http_status=499,
                error_code="client_interrupted",
                usage=usage,
            )
            raise
        except GatewayError as exc:
            await self._finish_stream(
                context,
                status="failed",
                http_status=exc.status,
                error_code=exc.code,
                usage=usage,
            )
            yield (
                b'data: {"error":{"message":"Grok upstream stream failed","type":"upstream_error","code":"'
                + exc.code.encode()
                + b'"}}\n\ndata: [DONE]\n\n'
            )
            return
        except Exception:
            logger.exception(
                "upstream_stream_failed account=%s key_id=%s chat=%s",
                str(lease.account["account_id"])[:8],
                str(key["id"])[:8],
                True,
            )
            await self._finish_stream(
                context,
                status="failed",
                http_status=502,
                error_code="upstream_stream_failed",
                usage=usage,
            )
            yield b'data: {"error":{"message":"Grok upstream stream failed","type":"upstream_error","code":"upstream_stream_failed"}}\n\ndata: [DONE]\n\n'
            return
        finally:
            try:
                await asyncio.shield(lease.close())
            except Exception:
                logger.warning("upstream_lease_close_failed")
            if not context.finalized:
                await self._finish_stream(
                    context,
                    status="success" if completed else "failed",
                    http_status=200 if completed else 502,
                    error_code=None if completed else "upstream_incomplete_stream",
                    usage=usage,
                )

    async def _fallback_account(
        self,
        key: dict[str, Any],
        excluded: str,
        *,
        failure_code: str | None = None,
        retry_after: float | None = None,
        provider: str = "codex",
        model: str = "",
    ) -> dict[str, Any]:
        del retry_after
        async with self._route_lock(str(key["id"]), provider):
            alternate = await self._balanced_account(
                key, excluded={excluded}, provider=provider, model=model
            )
            if account_provider(alternate) != provider:
                logger.error(
                    "provider_failover_blocked requested=%s actual=%s account=%s",
                    provider,
                    account_provider(alternate),
                    str(alternate.get("account_id") or "")[:8],
                )
                raise GatewayError(
                    503,
                    f"No healthy {provider} account is available",
                    code="no_healthy_accounts",
                )
            await self._save_active_route(
                key, provider, str(alternate["account_id"]), failure_code=failure_code
            )
            return alternate

    def _grok_rewrite(self, context: CallContext | None, *, chat: bool = False) -> GrokRewriteSpec:
        if chat or context is None:
            return GrokRewriteSpec()
        return context.grok_rewrite

    def _wrap_grok_compact(
        self,
        completed: dict[str, Any] | None,
        context: CallContext | None,
    ) -> dict[str, Any]:
        if context is None or not context.grok_compact_v2 or context.grok_compact_direct:
            return completed or {}
        summary = extract_grok_message_text(completed or {})
        if len(summary) < 20:
            raise GatewayError(
                400,
                "Grok compact did not produce a usable summary",
                code="invalid_prompt",
                error_type="invalid_request_error",
            )
        body = grok_compact_json_response(encrypted_content=grok_compact_encrypted_content(summary))
        if isinstance(completed, dict) and isinstance(completed.get("usage"), dict):
            body["usage"] = completed["usage"]
        if isinstance(completed, dict) and isinstance(completed.get("id"), str):
            body["id"] = completed["id"]
        else:
            body["id"] = f"resp_compact_{uuid.uuid4().hex[:12]}"
        body["object"] = "response"
        body["status"] = "completed"
        ensure_response_created_at(body, created_at=int(time.time()))
        return body

    async def collect_response(
        self,
        lease: UpstreamLease | SyntheticLease,
        context: CallContext | None = None,
    ) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        events: list[tuple[str, str]] = []
        buffer = ""
        rewrite_state: dict[str, Any] = {}
        rewrite = self._grok_rewrite(context)
        try:
            async for chunk in lease.aiter_bytes():
                buffer += chunk.decode("utf-8", errors="replace")
                if len(buffer) > settings.gateway_sse_max_event_bytes:
                    raise GatewayError(502, "Codex SSE event is too large", code="upstream_invalid_response")
                blocks, buffer = iter_sse_blocks(buffer)
                for block in blocks:
                    if rewrite.active():
                        for rewritten in rewrite_grok_codex_sse_block(block, rewrite, rewrite_state):
                            events.append(parse_sse_event(rewritten))
                            event, data = events[-1]
                            event_type, parsed = _sse_event_payload(event, data)
                            self._log_codex_upstream_returned(context, event_type, parsed)
                    else:
                        events.append(parse_sse_event(block))
                        event, data = events[-1]
                        event_type, parsed = _sse_event_payload(event, data)
                        self._log_codex_upstream_returned(context, event_type, parsed)
            if buffer.strip():
                if rewrite.active():
                    for rewritten in rewrite_grok_codex_sse_block(buffer, rewrite, rewrite_state):
                        events.append(parse_sse_event(rewritten))
                        event, data = events[-1]
                        event_type, parsed = _sse_event_payload(event, data)
                        self._log_codex_upstream_returned(context, event_type, parsed)
                else:
                    events.append(parse_sse_event(buffer))
                    event, data = events[-1]
                    event_type, parsed = _sse_event_payload(event, data)
                    self._log_codex_upstream_returned(context, event_type, parsed)
            try:
                completed = completed_response_from_sse(events)
            except HTTPException as exc:
                raise GatewayError(
                    exc.status_code,
                    str(exc.detail),
                    code="upstream_error",
                    error_type="upstream_error",
                ) from exc
            if rewrite.active():
                completed = rewrite_grok_completed_response(completed, rewrite)
            if context is not None and context.grok_compact_v2 and not context.grok_compact_direct:
                completed = self._wrap_grok_compact(completed, context)
            if isinstance(completed, dict):
                ensure_response_created_at(completed, created_at=int(time.time()))
            return completed, events
        finally:
            await lease.close()

    async def stream_response(
        self,
        lease: UpstreamLease | SyntheticLease,
        key: dict[str, Any],
        *,
        chat: bool,
        context: CallContext,
    ) -> AsyncIterator[bytes]:
        buffer = ""
        state: dict[str, Any] = {}
        rewrite_state: dict[str, Any] = {}
        completed: dict[str, Any] | None = None
        terminal_failure = False
        rewrite = self._grok_rewrite(context, chat=chat)
        compact_pending = context.grok_compact_v2 and not context.grok_compact_direct and not chat
        created_at = int(time.time())
        try:
            async for chunk in lease.aiter_bytes():
                buffer += chunk.decode("utf-8", errors="replace")
                if len(buffer) > settings.gateway_sse_max_event_bytes:
                    raise GatewayError(502, "Codex SSE event is too large", code="upstream_invalid_response")
                blocks, buffer = iter_sse_blocks(buffer)
                for block in blocks:
                    outgoing = (
                        rewrite_grok_codex_sse_block(block, rewrite, rewrite_state)
                        if rewrite.active()
                        else [block]
                    )
                    for piece in outgoing:
                        event, data = parse_sse_event(piece)
                        event_type, _payload = _sse_event_payload(event, data)
                        self._log_codex_upstream_returned(context, event_type, _payload)
                        terminal_failure = terminal_failure or event_type in {"response.failed", "error"}
                        completed = _completed_from_event(event, data) or completed
                        forwarded = False
                        if chat:
                            for encoded in convert_sse_to_chat_chunks(event, data, state):
                                yield encoded
                                forwarded = True
                        elif not compact_pending:
                            yield f"{stamp_sse_created_at(piece, created_at=created_at)}\n\n".encode()
                            forwarded = True
                        if forwarded:
                            context.delivered_output_bytes += _output_delta_bytes(event, data)
            if buffer.strip():
                outgoing = (
                    rewrite_grok_codex_sse_block(buffer, rewrite, rewrite_state)
                    if rewrite.active()
                    else [buffer]
                )
                for piece in outgoing:
                    event, data = parse_sse_event(piece)
                    event_type, _payload = _sse_event_payload(event, data)
                    self._log_codex_upstream_returned(context, event_type, _payload)
                    terminal_failure = terminal_failure or event_type in {"response.failed", "error"}
                    completed = _completed_from_event(event, data) or completed
                    forwarded = False
                    if chat:
                        for encoded in convert_sse_to_chat_chunks(event, data, state):
                            yield encoded
                            forwarded = True
                    elif not compact_pending:
                        yield f"{stamp_sse_created_at(piece, created_at=created_at)}\n\n".encode()
                        forwarded = True
                    if forwarded:
                        context.delivered_output_bytes += _output_delta_bytes(event, data)
            if compact_pending:
                try:
                    wrapped = self._wrap_grok_compact(completed, context)
                except GatewayError as exc:
                    sse = grok_compact_v2_sse(
                        response_id=f"resp_compact_{uuid.uuid4().hex[:12]}",
                        failed_code=exc.code or "invalid_prompt",
                        failed_message=str(exc),
                    )
                    for frame in sse.split("\n\n"):
                        if frame.strip():
                            yield f"{stamp_sse_created_at(frame, created_at=created_at)}\n\n".encode()
                    completed = None
                else:
                    item = wrapped["output"][0]
                    sse = grok_compact_v2_sse(
                        response_id=str(wrapped.get("id") or f"resp_compact_{uuid.uuid4().hex[:12]}"),
                        encrypted_content=str(item.get("encrypted_content") or ""),
                        usage=wrapped.get("usage") if isinstance(wrapped.get("usage"), dict) else None,
                    )
                    for frame in sse.split("\n\n"):
                        if frame.strip():
                            yield f"{stamp_sse_created_at(frame, created_at=created_at)}\n\n".encode()
                    completed = wrapped
            elif completed is None and not terminal_failure:
                if chat and not state.get("done"):
                    yield b'data: {"error":{"message":"Codex upstream returned an incomplete stream","type":"upstream_error","code":"upstream_incomplete_stream"}}\n\ndata: [DONE]\n\n'
                elif not chat:
                    yield b'event: error\ndata: {"type":"error","message":"Codex upstream returned an incomplete stream","code":"upstream_incomplete_stream"}\n\n'
        except asyncio.CancelledError:
            logger.info(
                "upstream_stream_cancelled account=%s key_id=%s",
                str((lease.account or {}).get("account_id") or "")[:8],
                str(key["id"])[:8],
            )
            await self._finish_stream(
                context,
                status="interrupted",
                http_status=499,
                error_code="client_interrupted",
                completed=completed,
            )
            raise
        except GatewayError as exc:
            await self._finish_stream(
                context,
                status="failed",
                http_status=exc.status,
                error_code=exc.code,
                completed=completed,
            )
            code = exc.code.encode()
            if chat:
                yield (
                    b'data: {"error":{"message":"Codex upstream stream failed","type":"upstream_error","code":"'
                    + code
                    + b'"}}\n\ndata: [DONE]\n\n'
                )
            else:
                yield (
                    b'event: error\ndata: {"type":"error","message":"Codex upstream stream failed","code":"'
                    + code
                    + b'"}\n\n'
                )
            return
        except Exception:
            logger.exception(
                "upstream_stream_failed account=%s key_id=%s chat=%s",
                str((lease.account or {}).get("account_id") or "")[:8],
                str(key["id"])[:8],
                chat,
            )
            await self.record_error(
                level="error",
                category="upstream_stream",
                code="upstream_stream_failed",
                user_name=context.key_name,
                method="POST",
                path="/v1/chat/completions" if chat else "/v1/responses",
                status=502,
                message="Codex upstream stream failed",
            )
            await self._finish_stream(
                context,
                status="failed",
                http_status=502,
                error_code="upstream_stream_failed",
                completed=completed,
            )
            if chat:
                yield b'data: {"error":{"message":"Codex upstream stream failed","type":"upstream_error","code":"upstream_stream_failed"}}\n\ndata: [DONE]\n\n'
            else:
                yield b'event: error\ndata: {"type":"error","message":"Codex upstream stream failed","code":"upstream_stream_failed"}\n\n'
            return
        finally:
            try:
                await asyncio.shield(lease.close())
            except Exception:
                logger.warning("upstream_lease_close_failed")
            if not context.finalized:
                await self._finish_stream(
                    context,
                    status="success" if completed is not None else "failed",
                    http_status=200 if completed is not None else 502,
                    error_code=(
                        None if completed is not None else
                        "upstream_error" if terminal_failure else "upstream_incomplete_stream"
                    ),
                    completed=completed,
                )

    @staticmethod
    def peek_response_model(
        lease: UpstreamLease | SyntheticLease | WorkBuddyResponsesLease,
    ) -> str | None:
        """Read a model from already-buffered SSE bytes without consuming the lease."""
        if isinstance(lease, WorkBuddyResponsesLease):
            return lease.model
        chunks = lease.buffered_chunks if isinstance(lease, UpstreamLease) else lease.chunks
        if not chunks:
            return None
        buffer = b"".join(chunks).decode("utf-8", errors="replace")
        blocks, _remainder = iter_sse_blocks(buffer)
        for block in blocks:
            event, data = parse_sse_event(block)
            _event_type, payload = _sse_event_payload(event, data)
            model = _model_from_sse_payload(payload)
            if model:
                return model
        return None

    def _capability_labels(self, model: Any) -> list[str]:
        mapping = {
            "chat": "Chat Completions",
            "stream": "Stream",
            "responses": "Responses",
            "image": "图片生成",
            "image_edit": "图片编辑",
            "video": "视频生成",
        }
        labels = [mapping[item] for item in model.capabilities if item in mapping]
        if model.type == "image" and not labels:
            return ["图片生成", "图片编辑"]
        if model.type == "video" and not labels:
            return ["视频生成"]
        if model.type == "text" and not labels:
            return ["Responses", "Chat Completions"]
        return labels

    async def models(
        self,
        *,
        client_version: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        healthy = await gateway_store.healthy_accounts()
        if not healthy:
            raise GatewayError(503, "No healthy account is available", code="no_healthy_accounts")
        ready_providers = {account_provider(row) for row in healthy}
        catalog = [
            item for item in provider_catalog() if item.provider in ready_providers and not item.alias_of
        ]
        codex_picker = wants_codex_catalog(client_version, user_agent)
        from app.billing import priced_models
        priced = await priced_models()
        seen: set[str] = set()
        data: list[dict[str, Any]] = []
        priority = 1
        for item in catalog:
            if item.id in seen or item.id not in priced:
                continue
            seen.add(item.id)
            if codex_picker:
                if item.type != "text":
                    continue
                entry = picker_entry(item, priority=priority)
                entry["pricing"] = priced[item.id]
                data.append(entry)
                priority += 1
            else:
                data.append(
                    {
                        "id": item.id,
                        "object": "model",
                        "owned_by": item.owned_by,
                        "provider": item.provider,
                        "pricing": priced[item.id],
                    }
                )
        if codex_picker:
            return {"models": data}
        return {"object": "list", "data": data}

    async def model_catalog(self) -> list[dict[str, Any]]:
        healthy = await gateway_store.healthy_accounts()
        ready_providers = {account_provider(row) for row in healthy}
        cooled = {
            str(row["model"])
            for row in await gateway_store.all(
                "SELECT model FROM account_model_health WHERE cooldown_until>?",
                (iso_now(),),
            )
        }
        catalog = []
        seen: set[str] = set()
        for item in provider_catalog():
            if item.id in seen:
                continue
            seen.add(item.id)
            from app.providers.catalog_extra import source_for

            catalog.append(
                {
                    "id": item.id,
                    "provider": item.provider,
                    "type": item.type,
                    "capabilities": self._capability_labels(item),
                    "default": item.default,
                    "available": item.provider in ready_providers,
                    "cooled": item.id in cooled or bool(item.alias_of and item.alias_of in cooled),
                    "alias_of": item.alias_of,
                    "source": source_for(item.provider, item.id) or "builtin",
                }
            )
        from app.billing import priced_models
        priced_ids = set((await priced_models()).keys())
        for row in catalog:
            row["priced"] = row["id"] in priced_ids
        return catalog

    async def restore_model(self, provider: str, model_id: str) -> dict[str, Any]:
        """Clear one model's cooldown. Revive invalid or deleted accounts only when none are healthy."""
        provider = provider.strip().lower()
        model_id = model_id.strip()
        if provider not in {"codex", "grok", "antigravity", "workbuddy"}:
            raise GatewayError(422, "Unknown provider", code="invalid_request")
        item = next(
            (row for row in provider_catalog() if row.provider == provider and row.id == model_id),
            None,
        )
        if item is None:
            raise GatewayError(404, "Model not found", code="model_not_found")
        model_ids = (model_id, item.alias_of) if item.alias_of and item.alias_of != model_id else (model_id,)

        def operation(db: Any) -> int:
            now = iso_now()
            marks = ",".join("?" * len(model_ids))
            db.execute(
                f"UPDATE account_model_health SET failures=0, cooldown_until=NULL WHERE model IN ({marks})",
                model_ids,
            )
            healthy = db.execute(
                """SELECT account_id FROM accounts
                   WHERE status='active' AND (cooldown_until IS NULL OR cooldown_until<=?)
                     AND COALESCE(provider, 'codex')=?""",
                (now, provider),
            ).fetchall()
            if healthy:
                return 0
            rows = db.execute(
                """SELECT account_id, status, credential_path FROM accounts
                   WHERE status IN ('invalid','deleted') AND COALESCE(provider, 'codex')=?""",
                (provider,),
            ).fetchall()
            revived = 0
            for row in rows:
                account_id = str(row["account_id"])
                if str(row["status"]) == "deleted":
                    credential = Path(str(row.get("credential_path") or ""))
                    if not credential.is_file():
                        continue
                changed = db.execute(
                    """UPDATE accounts
                       SET status='active', cooldown_until=NULL, network_failures=0, updated_at=?
                       WHERE account_id=? AND status IN ('invalid','deleted')""",
                    (now, account_id),
                ).rowcount
                if not changed:
                    continue
                db.execute(
                    """UPDATE account_model_health
                       SET failures=0, cooldown_until=NULL
                       WHERE account_id=?""",
                    (account_id,),
                )
                revived += 1
            return revived

        revived = await gateway_store.call(operation)
        available = bool(await gateway_store.healthy_accounts(provider))
        return {"ok": True, "available": available, "revived_accounts": revived}

    async def model(self, model_id: str) -> dict[str, Any]:
        provider, canonical = resolve_provider_model(model_id)
        models = await self.models()
        for item in models["data"]:
            if item["id"] in {model_id, canonical}:
                return item
        raise GatewayError(404, f"Model '{model_id}' not found", code="model_not_found", error_type="invalid_request_error")

    def list_providers(self, accounts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        rows = accounts or []
        counts: dict[str, int] = {}
        for row in rows:
            if row.get("status") == "active":
                counts[account_provider(row)] = counts.get(account_provider(row), 0) + 1
        payload = []
        for adapter in all_adapters():
            caps = adapter.capabilities
            payload.append(
                {
                    "id": adapter.id,
                    "display_name": adapter.display_name,
                    "configured": bool(counts.get(adapter.id)),
                    "ready": adapter.ready,
                    "chat": "chat" in caps,
                    "stream": "stream" in caps,
                    "image_generation": "image" in caps,
                    "image_edit": "image_edit" in caps,
                    "video": "video" in caps,
                }
            )
        return payload

    async def create_video(
        self,
        body: dict[str, Any],
        key: dict[str, Any],
        context: CallContext | None = None,
    ) -> dict[str, Any]:
        hint = await self._preferred_provider(key)
        provider, model = resolve_video_request_model(
            body.get("model") if isinstance(body.get("model"), str) else None,
            default_provider=hint,
        )
        if not provider_supports_video(provider) or not is_video_model(model):
            raise GatewayError(
                404,
                "Video generation is not available for this model",
                code="unsupported_capability",
                error_type="invalid_request_error",
            )
        self._require_ready_provider(provider)
        if context is not None:
            context.model = model
            context.provider = provider
        if provider == "grok":
            await self._inline_video_uploads(body, key)
            return await self._create_grok_video(body, key, model, context)
        raise GatewayError(
            404,
            "Video generation is not available for this model",
            code="unsupported_capability",
            error_type="invalid_request_error",
        )

    async def _create_grok_video(
        self,
        body: dict[str, Any],
        key: dict[str, Any],
        model: str,
        context: CallContext | None,
    ) -> dict[str, Any]:
        payload = shape_grok_video_body(body)
        payload["model"] = model
        seconds, size = requested_video_fields(body)
        return await self._submit_grok_video(
            "/videos/generations",
            payload,
            key,
            model,
            context,
            seconds=seconds,
            size=size,
        )

    async def _submit_grok_video(
        self,
        path: str,
        payload: dict[str, Any],
        key: dict[str, Any],
        model: str,
        context: CallContext | None,
        *,
        seconds: str | None = None,
        size: str | None = None,
        remixed_from_video_id: str | None = None,
    ) -> dict[str, Any]:
        if not payload.get("prompt"):
            raise GatewayError(
                400,
                "A prompt is required",
                code="invalid_request",
                error_type="invalid_request_error",
            )
        await self._prepare_budget(context, payload)
        account = await self.select_account(key, kind="video", provider="grok", model=model, context=context)
        result = await self.open_grok_request(
            account,
            "POST",
            path,
            json_body=payload,
            stream=False,
            context=context,
            timeout_seconds=settings.grok_video_timeout_seconds,
            accept="application/json",
        )
        if not isinstance(result, dict):
            raise GatewayError(
                502,
                "Grok video endpoint returned invalid JSON",
                code="upstream_invalid_response",
                error_type="upstream_error",
            )
        video_id = grok_video_id(result)
        if not video_id:
            raise GatewayError(
                502,
                "Grok video endpoint returned no request id",
                code="upstream_invalid_response",
                error_type="upstream_error",
            )
        status = map_provider_video_status("grok", result.get("status") or "queued")
        created_at = iso_now()
        await gateway_store.execute(
            """
            INSERT INTO video_jobs(
                video_id, account_id, key_id, provider, created_at, upstream_id, model,
                seconds, size, status, remixed_from_video_id
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(video_id) DO UPDATE SET
                account_id=excluded.account_id,
                key_id=excluded.key_id,
                provider=excluded.provider,
                upstream_id=excluded.upstream_id,
                model=excluded.model,
                seconds=excluded.seconds,
                size=excluded.size,
                status=excluded.status,
                remixed_from_video_id=excluded.remixed_from_video_id
            """,
            (
                video_id,
                str(account["account_id"]),
                str(key["id"]),
                "grok",
                created_at,
                video_id,
                model,
                seconds,
                size,
                status,
                remixed_from_video_id,
            ),
        )
        if context:
            await gateway_store.execute("UPDATE video_jobs SET billing_request_id=? WHERE video_id=?", (context.request_id, video_id))
        return openai_video_object(
            video_id=video_id,
            model=model,
            status=status,
            created_at=unix_seconds(created_at) or int(time.time()),
            progress=grok_video_progress(result),
            seconds=seconds,
            size=size,
            remixed_from_video_id=remixed_from_video_id,
        )

    def _video_object_from_job(self, job: dict[str, Any]) -> dict[str, Any]:
        status = str(job.get("status") or "queued")
        remixed = job.get("remixed_from_video_id")
        return openai_video_object(
            video_id=str(job["video_id"]),
            model=self._video_job_model(job),
            status=status,
            created_at=unix_seconds(job.get("created_at")) or int(time.time()),
            completed_at=unix_seconds(job.get("created_at")) if status == "completed" else None,
            progress=100 if status == "completed" else 0,
            seconds=job.get("seconds") if isinstance(job.get("seconds"), str) else None,
            size=job.get("size") if isinstance(job.get("size"), str) else None,
            remixed_from_video_id=str(remixed) if isinstance(remixed, str) and remixed else None,
        )

    async def _touch_video_job(self, video_id: str, payload: dict[str, Any]) -> None:
        await gateway_store.execute(
            "UPDATE video_jobs SET status=? WHERE video_id=?",
            (str(payload.get("status") or "queued"), video_id),
        )

    async def list_videos(
        self,
        key: dict[str, Any],
        *,
        after: str | None = None,
        limit: int = 20,
        order: str = "desc",
    ) -> dict[str, Any]:
        page = max(1, min(100, int(limit or 20)))
        descending = str(order or "desc").lower() != "asc"
        direction = "DESC" if descending else "ASC"
        params: list[Any] = [str(key["id"])]
        where = "key_id=?"
        if after:
            cursor = await gateway_store.one(
                "SELECT video_id, created_at FROM video_jobs WHERE video_id=? AND key_id=?",
                (after, str(key["id"])),
            )
            if cursor is not None:
                if descending:
                    where += " AND (created_at < ? OR (created_at = ? AND video_id < ?))"
                else:
                    where += " AND (created_at > ? OR (created_at = ? AND video_id > ?))"
                params.extend([cursor["created_at"], cursor["created_at"], cursor["video_id"]])
        rows = await gateway_store.all(
            f"SELECT * FROM video_jobs WHERE {where} ORDER BY created_at {direction}, video_id {direction} LIMIT ?",
            (*params, page + 1),
        )
        has_more = len(rows) > page
        items = [self._video_object_from_job(row) for row in rows[:page]]
        return openai_video_list(items, has_more=has_more)

    async def delete_video(self, video_id: str, key: dict[str, Any]) -> dict[str, Any]:
        await self._bound_video_job(video_id, key)
        await gateway_store.execute(
            "DELETE FROM video_jobs WHERE video_id=? AND key_id=?",
            (video_id, str(key["id"])),
        )
        return openai_video_deleted(video_id)

    def _resolve_video_model(
        self, body: dict[str, Any], *, hint: str, kind: str
    ) -> tuple[str, str]:
        provider, model = resolve_video_request_model(
            body.get("model") if isinstance(body.get("model"), str) else None,
            default_provider=hint,
        )
        if kind in {"edit", "extend", "remix"} and provider == "grok":
            model = grok_edit_video_model(model)
        if not provider_supports_video(provider) or not is_video_model(model):
            raise GatewayError(
                404,
                "Video generation is not available for this model",
                code="unsupported_capability",
                error_type="invalid_request_error",
            )
        self._require_ready_provider(provider)
        return provider, model

    async def _inline_file_id(self, file_id: str, key: dict[str, Any]) -> dict[str, str]:
        try:
            row = await image_artifacts.get(file_id, str(key["id"]))
            path, mime = await image_artifacts.content(file_id, str(key["id"]))
        except ArtifactError as exc:
            raise GatewayError(
                exc.status,
                exc.message,
                code=exc.code,
                error_type="invalid_request_error",
            ) from exc
        raw = path.read_bytes()
        if len(raw) > settings.gateway_image_artifact_max_file_bytes:
            raise GatewayError(
                413,
                "Video file is too large",
                code="file_too_large",
                error_type="invalid_request_error",
            )
        encoded = base64.b64encode(raw).decode("ascii")
        mime_type = mime or str(row.get("mime_type") or "application/octet-stream")
        return {"url": f"data:{mime_type};base64,{encoded}"}

    async def _inline_video_uploads(self, body: dict[str, Any], key: dict[str, Any]) -> None:
        mapping = (
            ("input_reference", "image"),
            ("image", "image"),
            ("video", "video"),
        )
        for field, target in mapping:
            raw = body.get(f"_{field}_bytes")
            if isinstance(raw, (bytes, bytearray)) and raw:
                if len(raw) > settings.gateway_image_artifact_max_file_bytes:
                    raise GatewayError(
                        413,
                        "Video file is too large",
                        code="file_too_large",
                        error_type="invalid_request_error",
                    )
                mime = str(body.get(f"_{field}_mime") or "")
                if target == "image" and not mime.startswith("image/"):
                    mime = "image/png"
                if target == "video" and not mime.startswith("video/"):
                    mime = "video/mp4"
                body[target] = {
                    "url": f"data:{mime};base64,{base64.b64encode(bytes(raw)).decode('ascii')}"
                }
        for field in ("input_reference", "image", "video"):
            value = body.get(field)
            if isinstance(value, dict) and isinstance(value.get("file_id"), str):
                body[field] = await self._inline_file_id(str(value["file_id"]), key)

    async def _grok_job_content_url(self, video_id: str, key: dict[str, Any]) -> str:
        job, account = await self._bound_video_job(video_id, key)
        result = await self._fetch_grok_video_result(video_id, job, account, None)
        status = map_provider_video_status("grok", result.get("status"))
        if status != "completed":
            raise GatewayError(
                409,
                "Video is not ready",
                code="video_not_ready",
                error_type="invalid_request_error",
            )
        url = grok_content_url(result)
        if not url:
            raise GatewayError(
                502,
                "Grok video endpoint returned no video data",
                code="upstream_invalid_response",
                error_type="upstream_error",
            )
        return url

    async def _resolve_grok_video_source(self, body: dict[str, Any], key: dict[str, Any]) -> None:
        await self._inline_video_uploads(body, key)
        video_id = source_video_id(body)
        if video_id:
            body["video"] = {"url": await self._grok_job_content_url(video_id, key)}

    async def edit_video(
        self, body: dict[str, Any], key: dict[str, Any], context: CallContext | None = None
    ) -> dict[str, Any]:
        hint = await self._preferred_provider(key)
        provider, model = self._resolve_video_model(body, hint=hint, kind="edit")
        if context is not None:
            context.model = model
            context.provider = provider
        if provider != "grok":
            raise GatewayError(
                404,
                "Video generation is not available for this model",
                code="unsupported_capability",
                error_type="invalid_request_error",
            )
        remixed_from = body.get("_remix_of") if isinstance(body.get("_remix_of"), str) else None
        await self._resolve_grok_video_source(body, key)
        payload = shape_grok_video_edit_body(body)
        payload["model"] = model
        if "video" not in payload:
            raise GatewayError(
                400,
                "A source video is required",
                code="invalid_request",
                error_type="invalid_request_error",
            )
        return await self._submit_grok_video(
            "/videos/edits",
            payload,
            key,
            model,
            context,
            remixed_from_video_id=remixed_from,
        )

    async def extend_video(
        self, body: dict[str, Any], key: dict[str, Any], context: CallContext | None = None
    ) -> dict[str, Any]:
        hint = await self._preferred_provider(key)
        provider, model = self._resolve_video_model(body, hint=hint, kind="extend")
        if context is not None:
            context.model = model
            context.provider = provider
        if provider != "grok":
            raise GatewayError(
                404,
                "Video generation is not available for this model",
                code="unsupported_capability",
                error_type="invalid_request_error",
            )
        await self._resolve_grok_video_source(body, key)
        payload = shape_grok_video_extend_body(body)
        payload["model"] = model
        if "video" not in payload:
            raise GatewayError(
                400,
                "A source video is required",
                code="invalid_request",
                error_type="invalid_request_error",
            )
        seconds, _size = requested_video_fields(body)
        return await self._submit_grok_video(
            "/videos/extensions",
            payload,
            key,
            model,
            context,
            seconds=seconds,
        )

    async def remix_video(
        self,
        video_id: str,
        body: dict[str, Any],
        key: dict[str, Any],
        context: CallContext | None = None,
    ) -> dict[str, Any]:
        payload = dict(body)
        payload["video"] = {"id": video_id}
        payload["_remix_of"] = video_id
        return await self.edit_video(payload, key, context)

    async def _bound_video_job(
        self, video_id: str, key: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        job = await gateway_store.one(
            "SELECT * FROM video_jobs WHERE video_id=? AND key_id=?",
            (video_id, str(key["id"])),
        )
        if job is None:
            raise GatewayError(
                404,
                "Video job not found",
                code="video_not_found",
                error_type="invalid_request_error",
            )
        account = await gateway_store.one(
            "SELECT * FROM accounts WHERE account_id=?", (str(job["account_id"]),)
        )
        if account is None or account.get("status") != "active":
            raise GatewayError(
                503,
                "The account bound to this video job is unavailable",
                code="session_account_unavailable",
            )
        return job, account

    def _video_job_model(self, job: dict[str, Any]) -> str:
        model = job.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        return settings.grok_video_model

    async def get_video(
        self, video_id: str, key: dict[str, Any], context: CallContext | None = None
    ) -> dict[str, Any]:
        job, account = await self._bound_video_job(video_id, key)
        provider = str(job.get("provider") or "grok")
        model = self._video_job_model(job)
        if context is not None:
            context.account_id = str(account["account_id"])
            context.provider = provider
            context.model = model
        if provider != "grok":
            raise GatewayError(
                404,
                "Video generation is not available for this model",
                code="unsupported_capability",
                error_type="invalid_request_error",
            )
        return await self._get_grok_video(video_id, job, account, context)

    async def video_content(
        self, video_id: str, key: dict[str, Any], *, variant: str = "video"
    ) -> tuple[bytes, str]:
        chosen = (variant or "video").strip().lower()
        if chosen not in OPENAI_CONTENT_VARIANTS:
            raise GatewayError(
                400,
                "Unsupported video content variant",
                code="invalid_request",
                error_type="invalid_request_error",
            )
        job, account = await self._bound_video_job(video_id, key)
        provider = str(job.get("provider") or "grok")
        if provider != "grok":
            raise GatewayError(
                404,
                "Video generation is not available for this model",
                code="unsupported_capability",
                error_type="invalid_request_error",
            )
        data, content_type = await self._grok_video_content(video_id, job, account)
        if chosen == "video":
            return data, content_type
        try:
            return render_video_preview(data, chosen)
        except PreviewError as exc:
            raise GatewayError(
                400 if exc.code in {"unsupported_capability", "invalid_request"} else 502,
                exc.message,
                code=exc.code,
                error_type="invalid_request_error"
                if exc.code in {"unsupported_capability", "invalid_request"}
                else "upstream_error",
            ) from exc

    async def _fetch_grok_video_result(
        self,
        video_id: str,
        job: dict[str, Any],
        account: dict[str, Any],
        context: CallContext | None,
    ) -> dict[str, Any]:
        upstream_id = str(job.get("upstream_id") or video_id)
        result = await self.open_grok_request(
            account,
            "GET",
            f"/videos/{upstream_id}",
            stream=False,
            context=context,
            timeout_seconds=settings.grok_video_timeout_seconds,
            accept="application/json",
        )
        if not isinstance(result, dict):
            raise GatewayError(
                502,
                "Grok video endpoint returned invalid JSON",
                code="upstream_invalid_response",
                error_type="upstream_error",
            )
        return result

    async def _get_grok_video(
        self,
        video_id: str,
        job: dict[str, Any],
        account: dict[str, Any],
        context: CallContext | None,
    ) -> dict[str, Any]:
        result = await self._fetch_grok_video_result(video_id, job, account, context)
        status = map_provider_video_status("grok", result.get("status"))
        if job.get("billing_request_id"):
            from app.wallet_engine import settle
            actual_seconds = grok_result_seconds(result, None)
            if status == "completed" and actual_seconds is not None:
                await settle(job["billing_request_id"], {"seconds": float(actual_seconds)}, "success")
            elif status == "failed":
                await settle(job["billing_request_id"], result.get("usage"), "failed")
        created_at = unix_seconds(job.get("created_at")) or int(time.time())
        completed_at = int(time.time()) if status == "completed" else None
        seconds = grok_result_seconds(result, job.get("seconds") if isinstance(job.get("seconds"), str) else None)
        size = grok_result_size(result, job.get("size") if isinstance(job.get("size"), str) else None)
        model = str(result.get("model") or self._video_job_model(job))
        remixed = job.get("remixed_from_video_id")
        payload = openai_video_object(
            video_id=video_id,
            model=model,
            status=status,
            created_at=created_at,
            completed_at=completed_at,
            progress=grok_video_progress(result),
            seconds=seconds,
            size=size,
            error=grok_video_error(result) if status == "failed" else None,
            remixed_from_video_id=str(remixed) if isinstance(remixed, str) and remixed else None,
        )
        await self._touch_video_job(video_id, payload)
        return payload

    async def _grok_video_content(
        self,
        video_id: str,
        job: dict[str, Any],
        account: dict[str, Any],
    ) -> tuple[bytes, str]:
        result = await self._fetch_grok_video_result(video_id, job, account, None)
        status = map_provider_video_status("grok", result.get("status"))
        if status == "failed":
            error = grok_video_error(result) or {}
            raise GatewayError(
                400,
                str(error.get("message") or "Video generation failed"),
                code=str(error.get("code") or "video_generation_failed"),
                error_type="invalid_request_error",
            )
        if status != "completed":
            raise GatewayError(
                409,
                "Video is not ready",
                code="video_not_ready",
                error_type="invalid_request_error",
            )
        url = grok_content_url(result)
        if not url:
            raise GatewayError(
                502,
                "Grok video endpoint returned no video data",
                code="upstream_invalid_response",
                error_type="upstream_error",
            )
        credentials = await self.grok_credentials(account)
        headers = video_download_headers(
            url, grok_build_headers(credentials, accept="*/*")
        )
        client = await shared_client()
        try:
            response = await client.get(
                url,
                headers=headers,
                timeout=httpx.Timeout(
                    settings.grok_video_timeout_seconds,
                    connect=20.0,
                    pool=settings.gateway_pool_timeout_seconds,
                ),
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise GatewayError(
                502,
                "Cannot download generated video",
                code="upstream_connection_error",
                error_type="upstream_error",
            ) from exc
        if response.status_code >= 400:
            status_code, detail = grok_map_upstream_status(
                response.status_code, _upstream_message(response)
            )
            raise GatewayError(
                status_code, detail, code="upstream_error", error_type="upstream_error"
            )
        content_type = (response.headers.get("content-type") or "video/mp4").split(";")[0]
        data = response.content
        if len(data) < 32:
            raise GatewayError(
                502,
                "Grok video endpoint returned no video data",
                code="upstream_invalid_response",
                error_type="upstream_error",
            )
        if len(data) > 80 * 1024 * 1024:
            raise GatewayError(
                502,
                "Generated video is too large",
                code="upstream_invalid_response",
                error_type="upstream_error",
            )
        return data, content_type or "video/mp4"

    async def image_json(self, kind: str, body: dict[str, Any], key: dict[str, Any]) -> dict[str, Any]:
        return await self.image_json_tracked(kind, body, key, None)

    async def image_json_tracked(
        self,
        kind: str,
        body: dict[str, Any],
        key: dict[str, Any],
        context: CallContext | None,
    ) -> dict[str, Any]:
        hint = await self._preferred_provider(key)
        provider, model = resolve_image_request_model(
            body.get("model") if isinstance(body.get("model"), str) else None,
            default_provider=hint,
        )
        self._require_ready_provider(provider)
        account = await self.select_account(key, kind="image", provider=provider, model=model, context=context)
        if context is not None:
            context.model = model
            context.provider = provider
        payload = dict(body)
        payload["model"] = model
        await self._prepare_budget(context, payload)
        path = "generations" if kind == "generation" else "edits"
        try:
            result = await self._post_image(
                account, path, json_body=payload, context=context, provider=provider
            )
        except GatewayError as exc:
            if exc.status not in {401, 429, 502, 503}:
                raise
            try:
                account = await self.select_account(
                    key, kind="image-retry", provider=provider, model=model
                )
            except GatewayError:
                raise exc
            result = await self._post_image(
                account, path, json_body=payload, context=context, provider=provider
            )
        if isinstance(result.get("data"), list):
            result["usage"] = {**(result.get("usage") or {}), "images": len(result["data"])}
        return result

    async def image_multipart(
        self,
        fields: list[tuple[str, str]],
        files: list[tuple[str, UploadFile]],
        key: dict[str, Any],
        context: CallContext | None = None,
    ) -> dict[str, Any]:
        hint = await self._preferred_provider(key)
        raw_model = next((value for name, value in fields if name == "model" and value), None)
        provider, model = resolve_image_request_model(raw_model, default_provider=hint)
        self._require_ready_provider(provider)
        if context is not None:
            context.model = model
            context.provider = provider
        await self._prepare_budget(context, dict(fields))
        account = await self.select_account(key, kind="image", provider=provider, model=model, context=context)
        parts: list[tuple[str, tuple[str | None, bytes | str, str | None]]] = []
        replaced_model = False
        for name, value in fields:
            if name == "model":
                parts.append((name, (None, model, None)))
                replaced_model = True
            else:
                parts.append((name, (None, value, None)))
        if not replaced_model:
            parts.append(("model", (None, model, None)))
        if context is not None:
            context.model = model
            context.provider = provider
        total_bytes = 0
        try:
            for field_name, uploaded in files:
                content_type = uploaded.content_type or "application/octet-stream"
                if content_type not in {"image/png", "image/jpeg", "image/webp", "application/octet-stream"}:
                    raise GatewayError(
                        422,
                        "Only PNG, JPEG, and WEBP image files are supported",
                        code="invalid_image",
                        error_type="invalid_request_error",
                    )

                chunks: list[bytes] = []
                file_bytes = 0
                while True:
                    chunk = await uploaded.read(1024 * 1024)
                    if not chunk:
                        break
                    file_bytes += len(chunk)
                    total_bytes += len(chunk)
                    if file_bytes > settings.gateway_image_artifact_max_file_bytes:
                        raise GatewayError(
                            413, "Image file is too large", code="file_too_large", error_type="invalid_request_error"
                        )
                    if total_bytes > settings.gateway_image_artifact_max_request_bytes:
                        raise GatewayError(
                            413, "Image request is too large", code="request_too_large", error_type="invalid_request_error"
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
                parts.append((
                    field_name,
                    (uploaded.filename or f"{field_name}.png", raw, content_type),
                ))
        finally:
            for _field_name, uploaded in files:
                await uploaded.close()
        try:
            result = await self._post_image(
                account, "edits", files=parts, context=context, provider=provider
            )
        except GatewayError as exc:
            if exc.status not in {401, 429, 502, 503}:
                raise
            try:
                account = await self.select_account(
                    key, kind="image-retry", provider=provider, model=model
                )
            except GatewayError:
                raise exc
            result = await self._post_image(
                account, "edits", files=parts, context=context, provider=provider
            )
        if isinstance(result.get("data"), list):
            result["usage"] = {**(result.get("usage") or {}), "images": len(result["data"])}
        return result

    async def _post_image(
        self, account: dict[str, Any], path: str, *, json_body: dict[str, Any] | None = None,
        files: list[Any] | None = None, context: CallContext | None = None,
        provider: str = "codex",
    ) -> dict[str, Any]:
        if provider == "antigravity":
            return await self._open_antigravity_image(
                account, json_body=json_body, files=files, context=context
            )
        if provider == "workbuddy":
            return await self._open_workbuddy_image(
                account, path, json_body=json_body, files=files, context=context
            )
        if provider == "grok":
            kind = "generation" if path == "generations" else "edit"
            if files is not None:
                values, images, mask = _image_request_assets(json_body=json_body, files=files)
                body = {
                    key: value
                    for key, value in values.items()
                    if key not in {"image", "images", "mask"}
                    and not isinstance(value, (bytes, bytearray))
                }
                if images:
                    body["image"] = images[0] if len(images) == 1 else list(images)
                if mask:
                    body["mask"] = mask
            else:
                body = dict(json_body or {})
            grok_body = shape_grok_image_body(kind, body)
            result = await self.open_grok_request(
                account,
                "POST",
                f"/images/{path}",
                json_body=grok_body,
                files=None,
                stream=False,
                context=context,
                timeout_seconds=settings.grok_image_timeout_seconds,
                accept="application/json",
            )
            if not isinstance(result, dict):
                raise GatewayError(
                    502,
                    "Grok image endpoint returned invalid JSON",
                    code="upstream_invalid_response",
                    error_type="upstream_error",
                )
            await _ensure_images_json_b64(result)
            return result
        account_id = str(account["account_id"])
        await self.note_attempt(context, account)
        semaphore = self._semaphore(account_id)
        try:
            await asyncio.wait_for(semaphore.acquire(), settings.gateway_queue_timeout_seconds)
        except TimeoutError as exc:
            raise GatewayError(429, "Codex account concurrency limit reached", code="rate_limit_exceeded") from exc
        try:
            credentials = await self.credentials(account)
            headers = build_headers(credentials, accept="application/json")
            if files is not None:
                headers.pop("Content-Type", None)
            client = await shared_client()
            response = await client.post(
                f"{upstream_base_url(credentials.auth_mode)}/images/{path}",
                params=upstream_client_params(),
                headers=headers,
                json=json_body, files=files,
                timeout=httpx.Timeout(
                    settings.codex_image_timeout_seconds,
                    connect=20.0,
                    pool=settings.gateway_pool_timeout_seconds,
                ),
            )
            if (
                credentials.auth_mode == "chatgpt"
                and response.status_code in {400, 404, 405}
            ):
                return await self._post_image_via_responses(
                    credentials,
                    "generation" if path == "generations" else "edit",
                    json_body=json_body,
                    files=files,
                )
            if response.status_code >= 400:
                retry_after = _retry_after_seconds(response)
                await self._mark_failure(account_id, response.status_code, retry_after, context=context)
                status, detail = map_upstream_status(response.status_code, _upstream_message(response))
                raise GatewayError(
                    status,
                    detail,
                    code="rate_limit_exceeded" if status == 429 else "upstream_error",
                    error_type="upstream_error",
                    retry_after=retry_after,
                )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise GatewayError(
                    502,
                    "Codex image endpoint returned invalid JSON",
                    code="upstream_invalid_response",
                    error_type="upstream_error",
                ) from exc
            if not isinstance(payload, dict):
                raise GatewayError(502, "Codex image endpoint returned invalid JSON", code="upstream_invalid_response", error_type="upstream_error")
            return payload
        except httpx.PoolTimeout as exc:
            rotated = await rotate_shared_client(
                client if "client" in locals() else None,
                reason="pool_timeout",
            )
            logger.error("upstream_pool_timeout rotated=%s", rotated)
            await self._mark_network_failure(account_id, context=context)
            raise GatewayError(502, "Cannot reach Codex upstream", code="upstream_connection_error", error_type="upstream_error") from exc
        except httpx.HTTPError as exc:
            await self._mark_network_failure(account_id, context=context)
            raise GatewayError(502, "Cannot reach Codex upstream", code="upstream_connection_error", error_type="upstream_error") from exc
        finally:
            semaphore.release()

    async def _open_workbuddy_image(
        self,
        account: dict[str, Any],
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        files: list[Any] | None = None,
        context: CallContext | None = None,
    ) -> dict[str, Any]:
        account_id = str(account["account_id"])
        await self.note_attempt(context, account)
        semaphore = self._semaphore(account_id)
        try:
            await asyncio.wait_for(semaphore.acquire(), settings.gateway_queue_timeout_seconds)
        except TimeoutError as exc:
            raise GatewayError(
                429, "WorkBuddy account concurrency limit reached", code="rate_limit_exceeded"
            ) from exc
        try:
            client = await shared_client()
            credentials = await self.workbuddy_credentials(account, client=client)
            body = dict(json_body or {})
            body.setdefault("model", canonical_image_model("workbuddy"))
            body.setdefault("n", 1)
            body.setdefault("response_format", "b64_json")
            if files:
                values, images, mask = _image_request_assets(json_body=json_body, files=files)
                body.update({key: value for key, value in values.items() if key not in {"image", "images", "mask"}})
                if images:
                    body["image"] = images[0] if len(images) == 1 else images
                if mask:
                    body["mask"] = mask
            headers = workbuddy_chat_headers(credentials)
            headers["Accept"] = "application/json"
            suffix = "/v1/images/generations" if path == "generations" else "/v1/images/edits"
            try:
                response = await client.post(
                    workbuddy_base_url(credentials.realm) + suffix,
                    headers=headers,
                    json=body,
                    timeout=httpx.Timeout(120.0, connect=20.0, pool=settings.gateway_pool_timeout_seconds),
                )
            except httpx.TimeoutException as exc:
                raise GatewayError(
                    504, "WorkBuddy image generation timed out", code="upstream_timeout",
                    error_type="upstream_error",
                ) from exc
            except httpx.HTTPError as exc:
                raise GatewayError(
                    502, "Cannot reach WorkBuddy image endpoint", code="upstream_connection_error",
                    error_type="upstream_error",
                ) from exc
            if response.status_code >= 400:
                retry_after = _retry_after_seconds(response)
                await self._mark_failure(account_id, response.status_code, retry_after, context=context)
                raise GatewayError(
                    429 if response.status_code == 429 else 502,
                    "WorkBuddy image generation was rejected",
                    code="rate_limit_exceeded" if response.status_code == 429 else "upstream_error",
                    error_type="upstream_error",
                    retry_after=retry_after,
                )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise GatewayError(
                    502, "WorkBuddy image endpoint returned invalid JSON",
                    code="upstream_invalid_response", error_type="upstream_error",
                ) from exc
            shaped = _workbuddy_images_json(payload)
            await _ensure_images_json_b64(shaped)
            return shaped
        finally:
            semaphore.release()

    async def _workbuddy_hosted_followup(
        self,
        account: dict[str, Any],
        model: str,
        chat_payload: dict[str, Any],
        calls: list[dict[str, Any]],
        context: CallContext | None,
    ) -> tuple[list[bytes], UpstreamLease | None]:
        extra: list[bytes] = []
        assistant_calls: list[dict[str, Any]] = []
        tool_messages: list[dict[str, Any]] = []
        follow = False
        for item in calls:
            arguments = item.get("arguments") if isinstance(item.get("arguments"), str) else ""
            call_id = str(item.get("call_id") or item.get("id") or "call_wb")
            parsed: dict[str, Any] = {}
            if arguments:
                try:
                    loaded = json.loads(arguments)
                    if isinstance(loaded, dict):
                        parsed = loaded
                except json.JSONDecodeError:
                    parsed = {}
            if item.get("type") == "image_generation_call":
                if context:
                    from app.wallet_engine import prepare_image_price
                    from app.billing import BillingError
                    try:
                        await prepare_image_price(context.request_id, canonical_image_model("workbuddy"), context.billed_images + 1)
                    except BillingError as exc:
                        raise GatewayError(exc.status, exc.message, code=exc.code) from exc
                prompt = str(parsed.get("prompt") or "").strip() or "image"
                result = await self._open_workbuddy_image(
                    account,
                    "generations",
                    json_body={"prompt": prompt, "model": canonical_image_model("workbuddy")},
                    context=context,
                )
                encoded = await _b64_from_images_json(result)
                if context:
                    context.billed_images += len(result.get("data") or [1])
                item["result"] = encoded
                item["status"] = "completed"
                extra.append(
                    f"event: response.output_item.done\ndata: {json.dumps({'type': 'response.output_item.done', 'item': item}, ensure_ascii=False)}\n\n".encode()
                )
            elif item.get("type") == "web_search_call":
                action = item.get("action") if isinstance(item.get("action"), dict) else {}
                query = str(action.get("query") or parsed.get("query") or "").strip()
                snippet = await self._workbuddy_web_search(query)
                follow = True
                assistant_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "WebSearch", "arguments": arguments or json.dumps({"query": query})},
                })
                tool_messages.append({"role": "tool", "tool_call_id": call_id, "content": snippet})
        follow_lease = None
        if follow:
            payload = dict(chat_payload)
            messages = list(payload.get("messages") or [])
            messages.append({"role": "assistant", "content": None, "tool_calls": assistant_calls})
            messages.extend(tool_messages)
            payload["messages"] = messages
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            follow_lease = await self.open_workbuddy_chat(account, payload, context)
        return extra, follow_lease

    async def _workbuddy_web_search(self, query: str) -> str:
        client = await shared_client()
        try:
            response = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                timeout=httpx.Timeout(20.0, connect=10.0, pool=settings.gateway_pool_timeout_seconds),
            )
        except httpx.HTTPError:
            return "Search is temporarily unavailable."
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return "Search returned no usable results."
        if not isinstance(payload, dict):
            return "Search returned no usable results."
        parts: list[str] = []
        abstract = payload.get("AbstractText")
        if isinstance(abstract, str) and abstract.strip():
            parts.append(abstract.strip())
        related = payload.get("RelatedTopics")
        if isinstance(related, list):
            for item in related[:5]:
                if isinstance(item, dict) and isinstance(item.get("Text"), str) and item["Text"].strip():
                    parts.append(item["Text"].strip())
        return "\n".join(parts) if parts else "No web results."

    async def _post_image_via_responses(
        self,
        credentials: CodexCredentials,
        kind: str,
        *,
        json_body: dict[str, Any] | None,
        files: list[Any] | None,
    ) -> dict[str, Any]:
        request_body, image_options = _image_responses_payload(
            kind, json_body=json_body, files=files
        )
        client = await shared_client()
        response = await client.post(
            f"{upstream_base_url(credentials.auth_mode)}/responses",
            params=upstream_client_params(),
            headers=build_headers(credentials),
            json=request_body,
            timeout=httpx.Timeout(
                settings.codex_image_timeout_seconds,
                connect=20.0,
                pool=settings.gateway_pool_timeout_seconds,
            ),
        )
        if response.status_code >= 400:
            status, detail = map_upstream_status(
                response.status_code, _upstream_message(response)
            )
            raise GatewayError(
                status,
                detail,
                code="rate_limit_exceeded" if status == 429 else "upstream_error",
                error_type="upstream_error",
                retry_after=_retry_after_seconds(response),
            )
        buffer = response.text
        blocks, remainder = iter_sse_blocks(buffer)
        if remainder.strip():
            blocks.append(remainder)
        events = [parse_sse_event(block) for block in blocks]
        try:
            completed = completed_response_from_sse(events)
        except HTTPException as exc:
            raise GatewayError(
                502,
                str(exc.detail),
                code="upstream_error",
                error_type="upstream_error",
            ) from exc
        images = _image_results_from_sse(events, completed)
        if not images:
            event_types = sorted({
                event_type
                for event_type in (_sse_event_type(event, data) for event, data in events)
                if event_type
            })
            output = completed.get("output")
            output_types = sorted({
                str(item.get("type") or "unknown")
                for item in output if isinstance(item, dict)
            }) if isinstance(output, list) else []
            logger.warning(
                "upstream_image_result_missing event_types=%s output_types=%s",
                event_types,
                output_types,
            )
            raise GatewayError(
                502,
                "Codex image tool returned no image",
                code="upstream_invalid_response",
                error_type="upstream_error",
            )
        result: dict[str, Any] = {
            "created": int(time.time()),
            "data": images,
        }
        for field in ("background", "output_format", "quality", "size"):
            if field in image_options:
                result[field] = image_options[field]
        if isinstance(completed.get("usage"), dict):
            result["usage"] = completed["usage"]
        return result

    async def _mark_invalid(self, account_id: str) -> None:
        await gateway_store.execute(
            "UPDATE accounts SET status='invalid',updated_at=? WHERE account_id=?", (iso_now(), account_id)
        )

    async def _mark_failure(
        self, account_id: str, status: int, retry_after: float | None = None,
        *, context: CallContext | None = None,
    ) -> None:
        if status == 401:
            await self._mark_invalid(account_id)
        elif status == 429:
            cooldown = retry_after or settings.gateway_cooldown_seconds
            until = utc_now() + dt.timedelta(seconds=max(1, cooldown))
            await gateway_store.execute(
                "UPDATE accounts SET cooldown_until=?,updated_at=? WHERE account_id=?",
                (until.isoformat().replace("+00:00", "Z"), iso_now(), account_id),
            )
        elif status >= 500:
            await self._mark_network_failure(
                account_id, context=context, model_scoped=True
            )
            return

    async def _mark_network_failure(
        self, account_id: str, *, context: CallContext | None = None,
        model_scoped: bool = False,
    ) -> None:
        row = await gateway_store.one("SELECT network_failures FROM accounts WHERE account_id=?", (account_id,))
        failures = int((row or {}).get("network_failures") or 0) + 1
        cooldown = None
        if failures >= settings.gateway_network_error_threshold:
            if not model_scoped or context is None or not context.model:
                cooldown = (utc_now() + dt.timedelta(seconds=settings.gateway_circuit_breaker_seconds)).isoformat().replace("+00:00", "Z")
            failures = 0
        await gateway_store.execute(
            "UPDATE accounts SET network_failures=?,cooldown_until=?,updated_at=? WHERE account_id=?",
            (failures, cooldown, iso_now(), account_id),
        )


_GROK_CODING_TOOL_TYPES = frozenset(
    {
        "function",
        "custom",
        "custom_tool",
        "apply_patch",
        "shell",
        "local_shell",
        "shell_command",
        "unified_exec",
        "exec",
        "computer",
        "computer_use_preview",
    }
)
_GROK_CODING_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "shell",
        "local_shell",
        "shell_command",
        "unified_exec",
        "exec",
    }
)


def _tool_type_and_name(tool: Any) -> tuple[str, str]:
    if not isinstance(tool, dict):
        return "", ""
    tool_type = str(tool.get("type") or "")
    name = str(tool.get("name") or "")
    function = tool.get("function")
    if not name and isinstance(function, dict):
        nested = function.get("name")
        if isinstance(nested, str):
            name = nested
    return tool_type, name


def _is_hosted_image_generation_tool(tool: Any) -> bool:
    tool_type, _name = _tool_type_and_name(tool)
    return tool_type == "image_generation"


def _iter_client_tools(payload: dict[str, Any]) -> list[Any]:
    tools: list[Any] = []
    raw = payload.get("tools")
    if isinstance(raw, list):
        tools.extend(raw)
    items = payload.get("input")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "additional_tools":
                continue
            extra = item.get("tools")
            if isinstance(extra, list):
                tools.extend(extra)
    return tools


def _tool_choice_forces_image_generation(choice: Any) -> bool:
    if choice == "image_generation":
        return True
    if isinstance(choice, dict):
        return str(choice.get("type") or "") == "image_generation"
    return False


def _has_grok_coding_tools(tools: list[Any]) -> bool:
    for tool in tools:
        tool_type, name = _tool_type_and_name(tool)
        if tool_type in _GROK_CODING_TOOL_TYPES or name in _GROK_CODING_TOOL_NAMES:
            return True
    return False


def _should_synthetic_image_generation(payload: dict[str, Any]) -> bool:
    tools = _iter_client_tools(payload)
    if _tool_choice_forces_image_generation(payload.get("tool_choice")):
        return True
    if not any(_is_hosted_image_generation_tool(tool) for tool in tools):
        return False
    return not _has_grok_coding_tools(tools)


def _lock_hosted_image_generation_model(payload: dict[str, Any], image_model: str) -> None:
    # /v1/responses intercepts or strips hosted image_generation instead of forwarding it.
    # Do not call this from Images HTTP fallback; that path must keep the client image slug.
    def rewrite(tools: list[Any]) -> list[Any]:
        out: list[Any] = []
        for tool in tools:
            if _is_hosted_image_generation_tool(tool) and isinstance(tool, dict):
                copied = dict(tool)
                copied["model"] = image_model
                out.append(copied)
            else:
                out.append(tool)
        return out

    tools = payload.get("tools")
    if isinstance(tools, list):
        payload["tools"] = rewrite(tools)
    items = payload.get("input")
    if not isinstance(items, list):
        return
    rewritten: list[Any] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "additional_tools":
            rewritten.append(item)
            continue
        extra = item.get("tools")
        if not isinstance(extra, list):
            rewritten.append(item)
            continue
        copied = dict(item)
        copied["tools"] = rewrite(extra)
        rewritten.append(copied)
    payload["input"] = rewritten


def _strip_hosted_image_generation(payload: dict[str, Any]) -> None:
    tools = payload.get("tools")
    if isinstance(tools, list):
        kept = [tool for tool in tools if not _is_hosted_image_generation_tool(tool)]
        if kept:
            payload["tools"] = kept
        else:
            payload.pop("tools", None)
            payload.pop("parallel_tool_calls", None)
    if _tool_choice_forces_image_generation(payload.get("tool_choice")):
        payload["tool_choice"] = "auto"
    items = payload.get("input")
    if not isinstance(items, list):
        return
    rewritten: list[Any] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "additional_tools":
            rewritten.append(item)
            continue
        extra = item.get("tools")
        if not isinstance(extra, list):
            rewritten.append(item)
            continue
        kept = [tool for tool in extra if not _is_hosted_image_generation_tool(tool)]
        if not kept:
            continue
        copied = dict(item)
        copied["tools"] = kept
        rewritten.append(copied)
    payload["input"] = rewritten


def _image_url_from_block(block: dict[str, Any]) -> str | None:
    image_url = block.get("image_url")
    if isinstance(image_url, str) and image_url.strip():
        return image_url.strip()
    if isinstance(image_url, dict):
        nested = image_url.get("url")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    url = block.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _extract_responses_image_assets(payload: dict[str, Any]) -> tuple[str, list[str], str | None]:
    prompt = _extract_responses_image_prompt(payload)
    images: list[str] = []
    mask: str | None = None
    raw = payload.get("input")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, dict):
                blocks = [content]
            elif isinstance(content, list):
                blocks = content
            else:
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")
                if block_type not in {"input_image", "image_url", "output_image"} and "image_url" not in block:
                    continue
                url = _image_url_from_block(block)
                if url:
                    images.append(url)
    for tool in _iter_client_tools(payload):
        if not isinstance(tool, dict) or not _is_hosted_image_generation_tool(tool):
            continue
        mask_spec = tool.get("input_image_mask")
        if isinstance(mask_spec, str) and mask_spec.strip():
            mask = mask_spec.strip()
        elif isinstance(mask_spec, dict):
            url = _image_url_from_block(mask_spec)
            nested = mask_spec.get("image_url")
            if url:
                mask = url
            elif isinstance(nested, str) and nested.strip():
                mask = nested.strip()
    return prompt, images, mask


def _responses_image_kind(
    *,
    images: list[str],
    mask: str | None,
    payload: dict[str, Any],
) -> str:
    if images or mask:
        return "edit"
    for tool in _iter_client_tools(payload):
        if isinstance(tool, dict) and _is_hosted_image_generation_tool(tool):
            if str(tool.get("action") or "").lower() == "edit":
                return "edit"
    return "generation"


def _extract_responses_image_prompt(payload: dict[str, Any]) -> str:
    raw = payload.get("input")
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                parts.append(text)
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "message")
        role = item.get("role")
        if item_type != "message" or role not in (None, "user"):
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, str) and block.strip():
                parts.append(block.strip())
                continue
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "input_text")
            text = block.get("text")
            if (
                block_type in {"input_text", "text", "output_text"}
                and isinstance(text, str)
                and text.strip()
            ):
                parts.append(text.strip())
    return "\n".join(parts)


def _b64_from_data_url(url: str) -> str | None:
    marker = ";base64,"
    if not url.startswith("data:") or marker not in url:
        return None
    encoded = url.split(marker, 1)[1].strip()
    return encoded or None


def _image_url_host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host[:80]


async def _ensure_images_json_b64(result: dict[str, Any]) -> None:
    data = result.get("data")
    if not isinstance(data, list):
        return
    client: httpx.AsyncClient | None = None
    for item in data:
        if not isinstance(item, dict):
            continue
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        from_data = _b64_from_data_url(url)
        if from_data:
            item["b64_json"] = from_data
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        host = _image_url_host(url)
        if client is None:
            client = await shared_client()
        try:
            response = await client.get(
                url,
                timeout=httpx.Timeout(30.0, connect=20.0, pool=settings.gateway_pool_timeout_seconds),
                follow_redirects=True,
            )
        except httpx.HTTPError:
            logger.warning("image_url_hydrate_failed reason=http_error host=%s", host)
            continue
        if response.status_code >= 400:
            logger.warning(
                "image_url_hydrate_failed reason=status status=%s host=%s",
                response.status_code,
                host,
            )
            continue
        raw = response.content
        if not raw or len(raw) > settings.gateway_image_artifact_max_file_bytes:
            logger.warning(
                "image_url_hydrate_failed reason=empty_or_too_large host=%s bytes=%s",
                host,
                0 if not raw else len(raw),
            )
            continue
        item["b64_json"] = base64.b64encode(raw).decode("ascii")


def _workbuddy_images_json(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GatewayError(
            502, "WorkBuddy image endpoint returned invalid JSON",
            code="upstream_invalid_response", error_type="upstream_error",
        )
    data = payload.get("data")
    if isinstance(data, list):
        return payload
    if isinstance(payload.get("code"), int) and payload.get("code") != 0:
        raise GatewayError(
            502, "WorkBuddy image generation was rejected",
            code="upstream_error", error_type="upstream_error",
        )
    nested = data if isinstance(data, dict) else payload
    item: dict[str, Any] = {}
    if isinstance(nested.get("b64_json"), str):
        item["b64_json"] = nested["b64_json"]
    elif isinstance(nested.get("image"), str):
        image = nested["image"]
        item["b64_json"] = image.split(",", 1)[1] if image.startswith("data:") and "," in image else image
    elif isinstance(nested.get("url"), str):
        item["url"] = nested["url"]
    if not item:
        raise GatewayError(
            502, "WorkBuddy image endpoint returned no image data",
            code="upstream_invalid_response", error_type="upstream_error",
        )
    return {"data": [item]}


def _workbuddy_completed_event(model: str, output: list[dict[str, Any]]) -> bytes:
    body = {
        "type": "response.completed",
        "response": {
            "id": f"resp_wb_{uuid.uuid4().hex[:12]}",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output": output,
        },
    }
    return f"event: response.completed\ndata: {json.dumps(body, ensure_ascii=False)}\n\n".encode()


async def _b64_from_images_json(result: dict[str, Any]) -> str:
    await _ensure_images_json_b64(result)
    data = result.get("data")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            encoded = item.get("b64_json")
            if isinstance(encoded, str) and encoded:
                return encoded
    raise GatewayError(
        502,
        "Image endpoint returned no image data",
        code="upstream_invalid_response",
        error_type="upstream_error",
    )


def _synthetic_image_generation_sse(
    *, encoded: str, response_id: str, model: str
) -> bytes:
    item = {
        "id": f"img_{uuid.uuid4().hex[:12]}",
        "type": "image_generation_call",
        "status": "completed",
        "result": encoded,
    }
    created = {
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "in_progress",
            "model": model,
            "output": [],
        },
    }
    done = {"type": "response.output_item.done", "item": item}
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output": [item],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        },
    }
    return "".join(
        f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for event, data in (
            ("response.created", created),
            ("response.output_item.done", done),
            ("response.completed", completed),
        )
    ).encode()








def _sse_event_type(event_name: str, data: str) -> str:
    if not data or data == "[DONE]":
        return ""
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return event_name
    return str(payload.get("type") or event_name) if isinstance(payload, dict) else event_name




def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _conversation_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        return _string(value.get("id"))
    return None


def _completed_from_event(event: str, data: str) -> dict[str, Any] | None:
    if not data:
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or str(payload.get("type") or event) not in {
        "response.completed", "response.incomplete"
    }:
        return None
    response = payload.get("response")
    return response if isinstance(response, dict) else None


def _sse_event_payload(event: str, data: str) -> tuple[str, dict[str, Any]]:
    if not data or data == "[DONE]":
        return event, {}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return event, {}
    if not isinstance(payload, dict):
        return event, {}
    return str(payload.get("type") or event), payload


def _sse_error_message(payload: dict[str, Any]) -> str:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    error = response.get("error") if isinstance(response, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:500]
    if isinstance(payload.get("message"), str):
        return str(payload["message"])[:500]
    return "Codex upstream request failed"


def _payload_shape(payload: dict[str, Any]) -> str:
    """Summarize request structure for diagnostics without text, names, URLs, or sizes."""
    known_fields = {
        "model", "store", "stream", "instructions", "input", "tools", "tool_choice",
        "parallel_tool_calls", "include", "reasoning", "max_output_tokens", "text",
        "service_tier", "previous_response_id", "conversation",
    }
    known_item_types = {
        "message", "function_call", "function_call_output", "additional_tools",
        "reasoning", "computer_call", "computer_call_output", "item_reference",
    }
    known_content_types = {
        "input_text", "output_text", "input_image", "input_file", "refusal",
    }
    known_roles = {"system", "developer", "user", "assistant", "tool"}
    known_tool_types = {
        "function", "custom", "computer", "computer_use_preview", "web_search",
        "web_search_preview", "file_search", "image_generation", "local_shell",
    }

    def bucket(value: Any, allowed: set[str]) -> str:
        return value if isinstance(value, str) and value in allowed else "other"

    item_types: dict[str, int] = {}
    roles: dict[str, int] = {}
    content_types: dict[str, int] = {}
    tool_types: dict[str, int] = {}

    def increment(target: dict[str, int], name: str) -> None:
        target[name] = target.get(name, 0) + 1

    items = payload.get("input")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                increment(item_types, "other")
                continue
            increment(item_types, bucket(item.get("type"), known_item_types))
            if "role" in item:
                increment(roles, bucket(item.get("role"), known_roles))
            content = item.get("content")
            if isinstance(content, list):
                for block in content:
                    increment(
                        content_types,
                        bucket(block.get("type") if isinstance(block, dict) else None, known_content_types),
                    )
            embedded_tools = item.get("tools") if item.get("type") == "additional_tools" else None
            if isinstance(embedded_tools, list):
                for tool in embedded_tools:
                    increment(
                        tool_types,
                        bucket(tool.get("type") if isinstance(tool, dict) else None, known_tool_types),
                    )
    top_tools = payload.get("tools")
    if isinstance(top_tools, list):
        for tool in top_tools:
            increment(
                tool_types,
                bucket(tool.get("type") if isinstance(tool, dict) else None, known_tool_types),
            )
    tool_choice = payload.get("tool_choice")
    tool_choice_type = (
        bucket(tool_choice.get("type"), known_tool_types | {"auto", "none", "required"})
        if isinstance(tool_choice, dict)
        else bucket(tool_choice, {"auto", "none", "required"})
    )
    reasoning = payload.get("reasoning")
    reasoning_fields = sorted(
        field for field in ("effort", "summary", "context")
        if isinstance(reasoning, dict) and field in reasoning
    )
    return json.dumps(
        {
            "fields": sorted(field for field in payload if field in known_fields),
            "unknown_fields": sum(1 for field in payload if field not in known_fields),
            "items": len(items) if isinstance(items, list) else 0,
            "item_types": item_types,
            "roles": roles,
            "content_types": content_types,
            "tool_types": tool_types,
            "tool_choice": tool_choice_type,
            "reasoning_fields": reasoning_fields,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value:
        try:
            return max(1.0, float(value))
        except ValueError:
            pass
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend((payload.get("resets_in_seconds"), payload.get("retry_after")))
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend((error.get("resets_in_seconds"), error.get("retry_after")))
        reset_at = payload.get("resets_at")
        if reset_at is None and isinstance(error, dict):
            reset_at = error.get("resets_at")
        if isinstance(reset_at, (int, float)):
            candidates.insert(0, float(reset_at) - utc_now().timestamp())
    for candidate in candidates:
        if isinstance(candidate, (int, float)) and candidate > 0:
            return float(candidate)
    return None


def _local_day_bounds_utc() -> tuple[str, str]:
    local_now = dt.datetime.now().astimezone()
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + dt.timedelta(days=1)
    return (
        start_local.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        end_local.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
    )


def _date_filter_value(value: str, *, end_of_day: bool) -> str:
    if len(value) == 10:
        try:
            parsed_date = dt.date.fromisoformat(value)
        except ValueError as exc:
            raise GatewayError(422, "Invalid date filter", code="invalid_date") from exc
        local_tz = dt.datetime.now().astimezone().tzinfo
        moment = dt.datetime.combine(parsed_date, dt.time.min, tzinfo=local_tz)
        if end_of_day:
            moment += dt.timedelta(days=1)
        return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise GatewayError(422, "Invalid date filter", code="invalid_date") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


codex_gateway = CodexGateway()
