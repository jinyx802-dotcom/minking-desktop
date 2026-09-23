from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from decimal import Decimal

from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.requests import ClientDisconnect

from app.admin_auth import SESSION_COOKIE, AdminSession, admin_auth
from app.bundle import codex_bundle
from app.client_skills_pack import (
    install_skill_zip,
    list_client_skills,
    restore_uploaded_skill,
    zip_client_skill,
)
from app.downloads import desktop_package_status, install_desktop_exe
from app.codex_gateway import (
    CallContext,
    GatewayError,
    TextDispatch,
    codex_gateway,
    error_event_message,
    models_mismatch,
)
from app.billing import (
    BillingError,
    create_card_batch,
    credit_from_hmac,
    disable_card,
    ensure_settings_row,
    grant_credit,
    grant_key_credit,
    list_admin_prices,
    list_cards,
    list_ledger,
    sync_official_prices,
    update_price_overrides,
    update_settings,
)
from app.config import settings
from app.image_artifacts import ArtifactError, image_artifacts
from app.providers.codex_catalog import wants_codex_image_urls
from app.providers.codex import client_version_from_user_agent
from app.providers.anthropic_protocol import (
    anthropic_error_body,
    anthropic_to_responses,
    convert_sse_to_anthropic,
    finalize_anthropic_message,
)
from app.providers.gemini_protocol import (
    convert_sse_to_gemini,
    finalize_gemini_response,
    gemini_to_responses,
)
from app.providers.codex_protocol import (
    codex_failed_code,
    convert_sse_to_chat_chunks,
    finalize_chat_completion,
    iter_sse_blocks,
    parse_sse_event,
    responses_failed_sse,
)

router = APIRouter()
error_logger = logging.getLogger("transfer_station.errors")


class KeyRouteSpec(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    preferred_account_id: str = Field(min_length=1, max_length=200)


class KeyCreate(BaseModel):
    name: str = Field(default="unnamed", max_length=100)
    preferred_account_id: str | None = Field(default=None, max_length=200)
    routes: list[KeyRouteSpec] = Field(default_factory=list)
    fast_enabled: bool = False


class KeyRouteUpdate(BaseModel):
    provider: str | None = Field(default=None, max_length=40)
    preferred_account_id: str | None = Field(default=None, max_length=200)


class EnabledUpdate(BaseModel):
    enabled: bool


class QuotaResetRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=200)
    credit_id: str | None = Field(default=None, max_length=500)
    confirmation_phrase: Literal["确定重置"]


class KeyUpdate(BaseModel):
    enabled: bool | None = None
    fast_enabled: bool | None = None


class LoginRequest(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=512)


class PasswordUpdate(BaseModel):
    current_password: str = Field(max_length=512)
    new_password: str = Field(max_length=512)


class BillingSettingsUpdate(BaseModel):
    price_multiplier: str | None = None
    enforced: bool | None = None
    new_user_usd: str | None = None
    request_budget_usd: Decimal | None = Field(default=None, gt=0, le=10000)


class PriceOverrideUpdate(BaseModel):
    provider: str | None = Field(default=None, max_length=32)
    modality: str | None = Field(default=None, max_length=16)
    official_input_usd_per_1m: str | None = None
    official_output_usd_per_1m: str | None = None
    official_cached_usd_per_1m: str | None = None
    official_usd_per_image: str | None = None
    official_usd_per_second: str | None = None
    multiplier_override: str | None = None
    sell_override_input: str | None = None
    sell_override_output: str | None = None
    sell_override_cached: str | None = None
    sell_override_image: str | None = None
    sell_override_second: str | None = None
    status: str | None = Field(default=None, max_length=32)


class KeyCreditRequest(BaseModel):
    amount: Decimal
    reason: str = Field(default="", max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=191)


class WalletCreditRequest(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    user_id: str | None = Field(default=None, max_length=64)
    amount: Decimal
    reason: str = Field(default="", max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=191)


class InternalWalletCreditRequest(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    user_id: str | None = Field(default=None, max_length=64)
    amount: Decimal
    reason: str = Field(default="", max_length=500)


class CardBatchRequest(BaseModel):
    amount_usd: Decimal
    count: int = Field(ge=1, le=100)
    expires_at: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=200)


class ImportLocalRequest(BaseModel):
    provider: str = Field(default="grok", max_length=40)


class OAuthStartRequest(BaseModel):
    provider: str = Field(default="codex", max_length=40)
    realm: Literal["cn", "global"] = "cn"


class OAuthCallbackRequest(BaseModel):
    provider: str | None = Field(default=None, max_length=40)
    state: str = Field(min_length=8, max_length=200)
    callback_url: str | None = Field(default=None, max_length=8000)
    code: str | None = Field(default=None, max_length=2048)


def openai_error(exc: GatewayError) -> JSONResponse:
    headers = None
    if exc.status == 429:
        headers = {"Retry-After": str(max(1, int(exc.retry_after or 2)))}
    return JSONResponse(
        status_code=exc.status,
        content={
            "error": {
                "message": exc.message,
                "type": exc.error_type,
                "param": None,
                "code": exc.code,
            }
        },
        headers=headers,
    )


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


async def _client_key(
    request: Request,
    authorization: str | None,
    x_api_key: str | None,
    extra: str | None = None,
) -> dict[str, Any]:
    candidates: list[str] = []
    query_key = ""
    raw_query = request.query_params.get("key")
    if isinstance(raw_query, str):
        query_key = raw_query.strip()
    for candidate in (_bearer(authorization), (x_api_key or "").strip(), (extra or "").strip(), query_key):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        return await codex_gateway.authenticate_key("")

    failures: list[GatewayError] = []
    for candidate in candidates:
        try:
            key = await codex_gateway.authenticate_key(candidate)
            request.state.user_name = str(key["name"])
            return key
        except GatewayError as exc:
            failures.append(exc)
    known_user_failure = next((exc for exc in failures if exc.user_name), None)
    raise known_user_failure or failures[0]


def _artifact_error(exc: ArtifactError) -> GatewayError:
    return GatewayError(
        exc.status,
        exc.message,
        code=exc.code,
        error_type="invalid_request_error" if exc.status < 500 else "upstream_error",
    )


def _public_base_url(request: Request) -> str:
    configured = settings.gateway_public_base_url.strip()
    return configured.rstrip("/") if configured else str(request.base_url).rstrip("/")


async def _compact_response_stream(
    source: AsyncIterator[bytes], *, owner_key_id: str, base_url: str
) -> AsyncIterator[bytes]:
    buffer = ""
    try:
        async for chunk in source:
            buffer += chunk.decode("utf-8", errors="replace")
            blocks, buffer = iter_sse_blocks(buffer)
            for block in blocks:
                event, data = parse_sse_event(block)
                if not data or data == "[DONE]":
                    yield f"{block}\n\n".encode()
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    yield f"{block}\n\n".encode()
                    continue
                if not isinstance(payload, dict):
                    yield f"{block}\n\n".encode()
                    continue
                event_type = str(payload.get("type") or event)
                if event_type in {
                    "response.image_generation_call.partial_image",
                    "image_generation.partial_image",
                    "image_edit.partial_image",
                }:
                    continue
                transformed = await image_artifacts.externalize_response_payload(
                    payload, owner_key_id=owner_key_id, base_url=base_url
                )
                prefix = "" if event == "message" else f"event: {event}\n"
                yield f"{prefix}data: {json.dumps(transformed, ensure_ascii=False)}\n\n".encode()
        if buffer.strip():
            yield f"{buffer}\n\n".encode()
    except ArtifactError as exc:
        error = {"error": {"message": exc.message, "type": "upstream_error", "code": exc.code}}
        yield f"data: {json.dumps(error, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()


async def _admin_read(request: Request) -> AdminSession:
    session = await admin_auth.require_session(request)
    request.state.user_name = session.username
    return session


async def _admin_write(request: Request) -> AdminSession:
    session = await admin_auth.require_write(request)
    request.state.user_name = session.username
    return session


AdminRead = Annotated[AdminSession, Depends(_admin_read)]
AdminWrite = Annotated[AdminSession, Depends(_admin_write)]


def _billing_error(exc: BillingError) -> GatewayError:
    return GatewayError(exc.status, exc.message, code=exc.code, error_type="billing_error")


def _admin_cookie_path(request: Request) -> str:
    root_path = str(request.scope.get("root_path", "")).rstrip("/")
    return f"{root_path}/admin"


def _set_session_cookie(
    request: Request, response: JSONResponse, session: AdminSession
) -> None:
    if not session.raw_token:
        return
    response.set_cookie(
        SESSION_COOKIE,
        session.raw_token,
        max_age=settings.admin_session_hours * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path=_admin_cookie_path(request),
    )


async def _json_object(request: Request) -> dict[str, Any]:
    raw_body = b""
    try:
        async with asyncio.timeout(max(1.0, settings.gateway_request_body_timeout_seconds)):
            raw_body = await request.body()
            body = json.loads(raw_body)
    except TimeoutError as exc:
        raise GatewayError(
            408,
            "Request body was not received before the timeout",
            code="request_body_timeout",
            error_type="request_error",
        ) from exc
    except ClientDisconnect as exc:
        raise GatewayError(
            499,
            "Client disconnected before the request body was complete",
            code="client_interrupted",
            error_type="request_error",
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            error_reason = exc.msg
            error_position = exc.pos
        else:
            error_reason = exc.reason
            error_position = exc.start
        client_host = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent", "")[:200].replace("\r", " ").replace("\n", " ")
        if settings.gateway_diagnostic_logging_enabled:
            error_logger.warning(
                "invalid_json_diagnostics request_id=%s path=%s client=%s "
                "user_agent=%r content_type=%r content_length=%r transfer_encoding=%r "
                "content_encoding=%r body_bytes=%s json_error=%r error_position=%s",
                getattr(request.state, "request_id", ""),
                request.url.path,
                client_host,
                user_agent,
                request.headers.get("content-type"),
                request.headers.get("content-length"),
                request.headers.get("transfer-encoding"),
                request.headers.get("content-encoding"),
                len(raw_body),
                error_reason,
                error_position,
            )
        raise GatewayError(
            400,
            "Request body must be valid JSON",
            code="invalid_json",
            error_type="invalid_request_error",
        ) from exc
    if not isinstance(body, dict):
        raise GatewayError(
            422,
            "Request body must be a JSON object",
            code="invalid_request",
            error_type="invalid_request_error",
        )
    return body


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


async def _record_call(
    request: Request,
    key: dict[str, Any],
    *,
    model: str,
    is_stream: bool = False,
    client_model: str | None = None,
) -> CallContext:
    context = await codex_gateway.begin_call(
        request_id=_request_id(request),
        key=key,
        endpoint=request.url.path,
        model=model,
        is_stream=is_stream,
    )
    raw_client = client_model if isinstance(client_model, str) else ""
    context.client_model = raw_client.strip()[:80]
    context.user_agent = request.headers.get("user-agent", "")[:200].replace("\r", " ").replace("\n", " ")
    context.client_version = client_version_from_user_agent(context.user_agent) or ""
    return context


def _model_headers(context: CallContext, *, streaming: bool = False) -> dict[str, str]:
    requested = (context.client_model or context.model or "").strip()[:80]
    actual = (context.response_model or "").strip()[:80]
    if streaming and not actual:
        actual = "pending"
    mismatch = models_mismatch(requested, actual) if actual and actual != "pending" else None
    headers = {"X-TS-Requested-Model": requested}
    if actual:
        headers["X-TS-Actual-Model"] = actual
    if mismatch is not None:
        headers["X-TS-Model-Mismatch"] = "true" if mismatch else "false"
    elif streaming:
        headers["X-TS-Model-Mismatch"] = "pending"
    return headers


async def _codex_failed_stream(exc: GatewayError) -> AsyncIterator[bytes]:
    sse = responses_failed_sse(
        response_id=f"resp_failed_{exc.code}",
        code=codex_failed_code(exc.status),
        message=error_event_message(exc),
    )
    yield f"{sse}\n\n".encode()


async def _finish_failure(
    context: CallContext,
    exc: BaseException,
    completed: dict[str, Any] | None = None,
) -> None:
    interrupted = (
        isinstance(exc, (asyncio.CancelledError, ClientDisconnect))
        or isinstance(exc, GatewayError) and exc.status == 499
    )
    if interrupted:
        await asyncio.shield(codex_gateway.finish_call(
            context,
            status="interrupted",
            http_status=499,
            error_code=getattr(exc, "code", "client_interrupted"),
            completed=completed,
        ))
        return
    if isinstance(exc, GatewayError):
        status, code = exc.status, exc.code
    elif isinstance(exc, HTTPException):
        status, code = exc.status_code, "invalid_request"
    else:
        status, code = 500, "internal_error"
    await asyncio.shield(codex_gateway.finish_call(
        context,
        status="failed",
        http_status=status,
        error_code=code,
        completed=completed,
    ))


@router.post("/admin/api/auth/login")
async def admin_login(request: Request, body: LoginRequest) -> JSONResponse:
    admin_auth.require_same_origin(request)
    session = await admin_auth.login(request, body.username, body.password)
    response = JSONResponse(
        {"username": session.username, "csrf_token": session.csrf_token, "expires_at": session.expires_at}
    )
    _set_session_cookie(request, response, session)
    return response


@router.post("/admin/api/auth/logout")
async def admin_logout(request: Request, _session: AdminWrite) -> JSONResponse:
    await admin_auth.logout(request)
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        SESSION_COOKIE, path=_admin_cookie_path(request), samesite="strict"
    )
    return response


@router.get("/admin/api/auth/session")
async def admin_session(session: AdminRead) -> dict[str, Any]:
    return {
        "username": session.username,
        "csrf_token": session.csrf_token,
        "expires_at": session.expires_at,
    }


@router.put("/admin/api/auth/password")
async def admin_password(
    request: Request, body: PasswordUpdate, _session: AdminWrite
) -> JSONResponse:
    try:
        session = await admin_auth.change_password(body.current_password, body.new_password)
    except ValueError as exc:
        raise GatewayError(422, str(exc), code="weak_admin_password") from exc
    response = JSONResponse(
        {
            "ok": True,
            "username": session.username,
            "csrf_token": session.csrf_token,
            "expires_at": session.expires_at,
        }
    )
    _set_session_cookie(request, response, session)
    return response


@router.post("/admin/api/accounts/import")
async def import_accounts(request: Request, _session: AdminWrite) -> dict[str, Any]:
    form = await request.form()
    files = [
        value
        for name, value in form.multi_items()
        if name in {"files", "files[]"} and isinstance(value, (UploadFile, StarletteUploadFile))
    ]
    provider = str(form.get("provider") or "codex").strip().lower() or "codex"
    return await codex_gateway.import_accounts(files, provider=provider)


@router.post("/admin/api/accounts/import-local")
async def import_local_accounts(body: ImportLocalRequest, _session: AdminWrite) -> dict[str, Any]:
    return await codex_gateway.import_local(body.provider)


@router.post("/admin/api/accounts/oauth/start")
async def start_account_oauth(
    request: Request, body: OAuthStartRequest, _session: AdminWrite
) -> dict[str, Any]:
    return await codex_gateway.start_oauth(
        body.provider, request_host=request.url.hostname, realm=body.realm
    )


@router.get("/admin/api/accounts/oauth/status")
async def account_oauth_status(
    _session: AdminRead,
    state: str = Query(min_length=8, max_length=200),
) -> dict[str, Any]:
    return await codex_gateway.oauth_status(state)


@router.post("/admin/api/accounts/oauth/callback")
async def complete_account_oauth(body: OAuthCallbackRequest, _session: AdminWrite) -> dict[str, Any]:
    return await codex_gateway.complete_oauth(
        state=body.state,
        code=body.code,
        callback_url=body.callback_url,
        provider=body.provider,
    )


@router.get("/admin/api/accounts")
async def accounts(_session: AdminRead) -> dict[str, Any]:
    return {"data": await codex_gateway.list_accounts()}


@router.get("/admin/api/accounts/quotas")
async def account_quotas(
    _session: AdminRead,
    response: Response,
    refresh: bool = False,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await codex_gateway.account_quotas(refresh=refresh)


@router.post("/admin/api/accounts/{account_id}/quota/reset")
async def reset_account_quota(
    account_id: str,
    body: QuotaResetRequest,
    _session: AdminWrite,
    response: Response,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await codex_gateway.reset_account_quota(
        account_id,
        idempotency_key=body.idempotency_key,
        credit_id=body.credit_id,
    )


@router.patch("/admin/api/accounts/{account_id}")
async def update_account(account_id: str, body: EnabledUpdate, _session: AdminWrite) -> dict[str, Any]:
    await codex_gateway.set_account_status(account_id, body.enabled)
    return {"ok": True}


@router.get("/admin/api/accounts/{account_id}/export")
async def export_account(account_id: str, _session: AdminRead) -> JSONResponse:
    payload = await codex_gateway.export_account(account_id)
    exported = JSONResponse(payload["data"])
    exported.headers["Cache-Control"] = "no-store"
    exported.headers["Content-Disposition"] = f'attachment; filename="{payload["filename"]}"'
    return exported


@router.delete("/admin/api/accounts/{account_id}")
async def remove_account(account_id: str, _session: AdminWrite) -> dict[str, Any]:
    await codex_gateway.delete_account(account_id)
    return {"deleted": True}


@router.post("/admin/api/keys/{key_id}/credit")
async def admin_key_credit(key_id: str, body: KeyCreditRequest, _session: AdminWrite) -> dict[str, Any]:
    try:
        return await grant_key_credit(
            key_id=key_id,
            amount=body.amount,
            reason=body.reason,
            actor=f"admin:{_session.username}",
            idempotency_key=body.idempotency_key,
        )
    except BillingError as exc:
        raise _billing_error(exc) from exc


@router.post("/admin/api/keys")
async def create_key(body: KeyCreate, _session: AdminWrite, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await codex_gateway.create_api_key(
        body.name,
        body.preferred_account_id,
        body.fast_enabled,
        routes=[item.model_dump() for item in body.routes],
    )


@router.get("/admin/api/keys")
async def keys(_session: AdminRead, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"data": await codex_gateway.list_api_keys()}


@router.get("/admin/api/providers")
async def admin_providers(_session: AdminRead) -> dict[str, Any]:
    return {"data": codex_gateway.list_providers(await codex_gateway.list_accounts())}


@router.post("/admin/api/keys/{key_id}/bundle")
async def admin_bundle(key_id: str, _session: AdminWrite) -> Response:
    payload = await codex_bundle(key_id)
    return Response(payload, media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="minking-api-codex.zip"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@router.post("/admin/api/keys/{key_id}/rotate")
async def rotate_key(key_id: str, _session: AdminWrite, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await codex_gateway.rotate_api_key(key_id)


@router.put("/admin/api/keys/{key_id}/route")
async def update_key_route(
    key_id: str, body: KeyRouteUpdate, _session: AdminWrite
) -> dict[str, Any]:
    if body.provider:
        await codex_gateway.update_provider_route(
            key_id, body.provider.strip().lower(), body.preferred_account_id or None
        )
    elif body.preferred_account_id:
        await codex_gateway.set_api_key_route(key_id, body.preferred_account_id)
    else:
        raise GatewayError(422, "Provider is required", code="invalid_request")
    return {"ok": True}


@router.patch("/admin/api/keys/{key_id}")
async def update_key(key_id: str, body: KeyUpdate, _session: AdminWrite) -> dict[str, Any]:
    submitted = body.model_fields_set
    if not submitted or any(getattr(body, field) is None for field in submitted):
        raise GatewayError(
            422, "At least one boolean API key field is required", code="invalid_request"
        )
    await codex_gateway.update_api_key(
        key_id,
        enabled=body.enabled if "enabled" in submitted else None,
        fast_enabled=body.fast_enabled if "fast_enabled" in submitted else None,
    )
    return {"ok": True}


@router.delete("/admin/api/keys/{key_id}")
async def remove_key(key_id: str, _session: AdminWrite) -> dict[str, Any]:
    await codex_gateway.delete_api_key(key_id)
    return {"deleted": True}


@router.get("/admin/api/usage")
async def usage(
    _session: AdminRead,
    usage_range: Literal["day", "week", "month", "all"] = Query(default="day", alias="range"),
    key_id: str | None = None,
    account_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    return await codex_gateway.usage(usage_range, key_id, account_id, model)


@router.get("/admin/api/usage/summary")
async def usage_summary(
    _session: AdminRead,
    usage_range: Literal["day", "week", "month", "all"] = Query(default="day", alias="range"),
) -> dict[str, Any]:
    return await codex_gateway.usage_summary(usage_range)


@router.get("/admin/api/dashboard")
async def dashboard(_session: AdminRead) -> dict[str, Any]:
    return await codex_gateway.dashboard()


@router.get("/admin/api/calls")
async def calls(
    _session: AdminRead,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    start: str | None = None,
    end: str | None = None,
    key_id: str | None = None,
    account_id: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return await codex_gateway.calls(
        page=page,
        page_size=page_size,
        start=start,
        end=end,
        key_id=key_id,
        account_id=account_id,
        model=model,
        endpoint=endpoint,
        status=status,
    )


@router.get("/admin/api/errors")
async def recent_errors(_session: AdminRead, limit: int = 50) -> dict[str, Any]:
    return {
        "data": await codex_gateway.recent_errors(limit),
        "capacity": settings.gateway_error_ring_capacity,
    }


class CatalogModelSpec(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model_id: str = Field(min_length=1, max_length=191)
    model_type: Literal["text", "image", "video"] = "text"


@router.get("/admin/api/models")
async def admin_models(_session: AdminRead) -> dict[str, Any]:
    return {"data": await codex_gateway.model_catalog()}


@router.post("/admin/api/models")
async def admin_add_model(body: CatalogModelSpec, _session: AdminWrite) -> dict[str, Any]:
    from app.providers.catalog_extra import PROVIDERS, normalize_model_id
    from app.scheduler import add_manual_model

    provider = body.provider.strip().lower()
    if provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail="Unknown provider")
    model_id = normalize_model_id(provider, body.model_id)
    if not model_id or any(char.isspace() for char in model_id):
        raise HTTPException(status_code=422, detail="Invalid model id")
    await add_manual_model(provider, model_id, body.model_type)
    return {"ok": True, "provider": provider, "model_id": model_id, "model_type": body.model_type}


@router.delete("/admin/api/models")
async def admin_remove_model(
    _session: AdminWrite,
    provider: str = Query(min_length=1, max_length=32),
    model_id: str = Query(min_length=1, max_length=191),
) -> dict[str, Any]:
    from app.scheduler import remove_extra_model

    removed = await remove_extra_model(provider.strip().lower(), model_id.strip())
    if not removed:
        raise HTTPException(status_code=404, detail="Only an added model can be removed")
    return {"ok": True}


class CatalogModelRestore(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model_id: str = Field(min_length=1, max_length=191)


@router.post("/admin/api/models/restore")
async def admin_restore_model(body: CatalogModelRestore, _session: AdminWrite) -> dict[str, Any]:
    return await codex_gateway.restore_model(body.provider, body.model_id)


@router.post("/admin/api/models/refresh")
async def admin_refresh_models(_session: AdminWrite) -> dict[str, Any]:
    from app.scheduler import refresh_provider_catalogs

    counts = await refresh_provider_catalogs(codex_gateway, force=True)
    return {"ok": True, "counts": counts, "codex_client_version": settings.codex_client_version}


@router.get("/admin/api/client-skills")
async def admin_client_skills(_session: AdminRead) -> JSONResponse:
    response = JSONResponse({"skills": list_client_skills()})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/admin/api/client-skills")
async def admin_upload_client_skill(request: Request, _session: AdminWrite) -> dict[str, Any]:
    form = await request.form()
    uploaded = None
    for key in ("file", "files", "files[]"):
        value = form.get(key)
        if isinstance(value, (UploadFile, StarletteUploadFile)):
            uploaded = value
            break
    if uploaded is None:
        raise GatewayError(400, "请选择技能 zip", code="invalid_skill_zip")
    blob = await uploaded.read()
    name = str(form.get("name") or "").strip()
    return {"ok": True, "skill": install_skill_zip(blob, name=name)}


@router.get("/admin/api/client-skills/{name}")
async def admin_download_client_skill(name: str, _session: AdminRead) -> Response:
    payload, digest = zip_client_skill(name)
    return Response(
        payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Skill-Sha256": digest,
        },
    )


@router.delete("/admin/api/client-skills/{name}")
async def admin_restore_client_skill(name: str, _session: AdminWrite) -> dict[str, Any]:
    return {"ok": True, "skill": restore_uploaded_skill(name)}


@router.get("/admin/api/desktop-package")
async def admin_desktop_package(_session: AdminRead) -> JSONResponse:
    response = JSONResponse(desktop_package_status())
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/admin/api/desktop-package")
async def admin_upload_desktop_package(request: Request, _session: AdminWrite) -> dict[str, Any]:
    form = await request.form()
    uploaded = None
    for key in ("file", "files", "files[]"):
        value = form.get(key)
        if isinstance(value, (UploadFile, StarletteUploadFile)):
            uploaded = value
            break
    if uploaded is None:
        raise GatewayError(400, "请选择安装包", code="invalid_desktop_exe")
    blob = await uploaded.read()
    return {"ok": True, "package": install_desktop_exe(blob)}


@router.get("/admin/api/billing/settings")
async def admin_billing_settings(_session: AdminRead) -> dict[str, Any]:
    return await ensure_settings_row()


@router.put("/admin/api/billing/settings")
async def admin_update_billing_settings(
    body: BillingSettingsUpdate, _session: AdminWrite
) -> dict[str, Any]:
    try:
        return await update_settings(
            price_multiplier=body.price_multiplier,
            enforced=body.enforced,
            new_user_usd=body.new_user_usd,
            request_budget_usd=body.request_budget_usd,
        )
    except BillingError as exc:
        raise _billing_error(exc) from exc


@router.get("/admin/api/billing/prices")
async def admin_billing_prices(_session: AdminRead) -> dict[str, Any]:
    return await list_admin_prices()


@router.put("/admin/api/billing/prices/{model}")
async def admin_update_billing_price(
    model: str, body: PriceOverrideUpdate, _session: AdminWrite
) -> dict[str, Any]:
    try:
        return await update_price_overrides(model, body.model_dump(exclude_unset=True))
    except BillingError as exc:
        raise _billing_error(exc) from exc


@router.post("/admin/api/billing/prices/sync")
async def admin_sync_billing_prices(_session: AdminWrite) -> dict[str, Any]:
    return await sync_official_prices()


@router.post("/admin/api/wallet/credit")
async def admin_wallet_credit(body: WalletCreditRequest, _session: AdminWrite) -> dict[str, Any]:
    try:
        return await grant_credit(
            user_id=body.user_id,
            email=body.email,
            amount=body.amount,
            reason=body.reason,
            actor=f"admin:{_session.username}",
            idempotency_key=body.idempotency_key,
        )
    except BillingError as exc:
        raise _billing_error(exc) from exc


@router.get("/admin/api/wallet/ledger")
async def admin_wallet_ledger(
    _session: AdminRead,
    user_id: str | None = None,
    email: str | None = None,
    kind: str | None = None,
    start: str | None = None,
    end: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    return await list_ledger(
        user_id=user_id,
        email=email,
        kind=kind,
        start=start,
        end=end,
        page=page,
        page_size=page_size,
    )


@router.post("/admin/api/cards/batches")
async def admin_create_card_batch(body: CardBatchRequest, _session: AdminWrite) -> dict[str, Any]:
    try:
        return await create_card_batch(
            amount_usd=body.amount_usd,
            count=body.count,
            expires_at=body.expires_at,
            created_by=_session.username,
            note=body.note,
        )
    except BillingError as exc:
        raise _billing_error(exc) from exc


@router.get("/admin/api/cards")
async def admin_list_cards(
    _session: AdminRead,
    status: str | None = None,
    batch_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    return await list_cards(status=status, batch_id=batch_id, page=page, page_size=page_size)


@router.post("/admin/api/cards/{card_id}/disable")
async def admin_disable_card(card_id: str, _session: AdminWrite) -> dict[str, Any]:
    try:
        return await disable_card(card_id)
    except BillingError as exc:
        raise _billing_error(exc) from exc


@router.post("/internal/wallet/credit")
async def internal_wallet_credit(
    body: InternalWalletCreditRequest,
    x_timestamp: str | None = Header(default=None),
    x_nonce: str | None = Header(default=None),
    x_idempotency_key: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        return await credit_from_hmac(
            timestamp=x_timestamp,
            nonce=x_nonce,
            idempotency_key=x_idempotency_key,
            signature=x_signature,
            user_id=body.user_id,
            email=body.email,
            amount=body.amount,
            reason=body.reason,
        )
    except BillingError as exc:
        raise _billing_error(exc) from exc


@router.get("/v1/models")
@router.get("/v1/codex/models", include_in_schema=False)
async def models(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    client_version: str | None = Query(default=None),
) -> dict[str, Any]:
    await _client_key(request, authorization, x_api_key)
    return await codex_gateway.models(
        client_version=client_version,
        user_agent=request.headers.get("user-agent"),
    )


@router.get("/v1/codex/status", include_in_schema=False)
async def codex_status(
    request: Request,
    authorization: str | None = Header(default=None), x_api_key: str | None = Header(default=None)
) -> dict[str, Any]:
    await _client_key(request, authorization, x_api_key)
    account_rows = await codex_gateway.list_accounts()
    return {
        "id": "codex",
        "configured": bool(account_rows),
        "accounts": len(account_rows),
        "healthy_accounts": sum(item["status"] == "active" for item in account_rows),
        "chat": True,
        "stream": True,
        "image_generation": True,
        "image_edit": True,
    }


@router.get("/v1/models/{model_id}")
async def model(
    request: Request,
    model_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    await _client_key(request, authorization, x_api_key)
    return await codex_gateway.model(model_id)


@router.post("/v1/files")
async def create_file(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    form = await request.form()
    uploaded = form.get("file")
    if not isinstance(uploaded, (UploadFile, StarletteUploadFile)):
        raise GatewayError(
            400, "A multipart file is required", code="file_required", error_type="invalid_request_error"
        )
    purpose = str(form.get("purpose") or "user_data")
    try:
        return await image_artifacts.create_upload(
            uploaded, owner_key_id=str(key["id"]), purpose=purpose
        )
    except ArtifactError as exc:
        raise _artifact_error(exc) from exc


@router.get("/v1/files")
async def list_files(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    return {"object": "list", "data": await image_artifacts.list(str(key["id"])), "has_more": False}


@router.get("/v1/files/{file_id}")
async def retrieve_file(
    request: Request,
    file_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    try:
        row = await image_artifacts.get(file_id, str(key["id"]))
        return image_artifacts.file_object(row)
    except ArtifactError as exc:
        raise _artifact_error(exc) from exc


@router.delete("/v1/files/{file_id}")
async def delete_file(
    request: Request,
    file_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    try:
        return await image_artifacts.delete(file_id, str(key["id"]))
    except ArtifactError as exc:
        raise _artifact_error(exc) from exc


@router.get("/v1/files/{file_id}/content", response_model=None)
async def file_content(
    request: Request,
    file_id: str,
    expires: int | None = None,
    sig: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> FileResponse:
    owner_key_id: str | None = None
    if not image_artifacts.verify_signature(file_id, expires, sig):
        key = await _client_key(request, authorization, x_api_key)
        owner_key_id = str(key["id"])
    try:
        path, mime_type = await image_artifacts.content(file_id, owner_key_id)
    except ArtifactError as exc:
        raise _artifact_error(exc) from exc
    return FileResponse(
        path,
        media_type=mime_type,
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/v1/responses", response_model=None)
async def responses(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None),
    x_ts_image_mode: str | None = Header(default=None),
) -> JSONResponse | StreamingResponse:
    key = await _client_key(request, authorization, x_api_key)
    context: CallContext | None = None
    completed: dict[str, Any] | None = None
    try:
        body = await _json_object(request)
        client_model = body.get("model") if isinstance(body.get("model"), str) else ""
        model = await codex_gateway.resolve_text_model(body, key)
        context = await _record_call(
            request,
            key,
            model=model,
            is_stream=body.get("stream") is True,
            client_model=client_model,
        )
        try:
            body = await image_artifacts.resolve_file_ids(body, str(key["id"]))
        except ArtifactError as exc:
            raise _artifact_error(exc) from exc
        lease, _payload, stream = await codex_gateway.response_request(
            body, key, x_session_id, context
        )
        buffered_model = codex_gateway.peek_response_model(lease)
        if buffered_model:
            context.response_model = buffered_model
        compact_images = wants_codex_image_urls(
            request.headers.get("user-agent"), x_ts_image_mode
        )
        if stream:
            source = codex_gateway.stream_response(lease, key, chat=False, context=context)
            if compact_images:
                source = _compact_response_stream(
                    source,
                    owner_key_id=str(key["id"]),
                    base_url=_public_base_url(request),
                )
            return StreamingResponse(
                source,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    **_model_headers(context, streaming=True),
                },
            )
        completed, _events = await codex_gateway.collect_response(lease, context)
        if compact_images:
            try:
                completed = await image_artifacts.externalize_response_payload(
                    completed,
                    owner_key_id=str(key["id"]),
                    base_url=_public_base_url(request),
                )
            except ArtifactError as exc:
                raise _artifact_error(exc) from exc
        await codex_gateway.finish_call(
            context, status="success", http_status=200, completed=completed
        )
        return JSONResponse(completed, headers=_model_headers(context))
    except BaseException as exc:
        if context is None:
            context = await _record_call(request, key, model="")
        await _finish_failure(context, exc, completed)
        if isinstance(exc, GatewayError) and context.is_stream:
            return StreamingResponse(
                _codex_failed_stream(exc),
                media_type="text/event-stream",
                status_code=200,
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    **_model_headers(context, streaming=True),
                },
            )
        raise


@router.post("/v1/chat/completions", response_model=None)
@router.post("/v1/codex/chat/completions", response_model=None, include_in_schema=False)
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None),
) -> JSONResponse | StreamingResponse:
    key = await _client_key(request, authorization, x_api_key)
    context: CallContext | None = None
    completed: dict[str, Any] | None = None
    try:
        body = await _json_object(request)
        client_model = body.get("model") if isinstance(body.get("model"), str) else ""
        model = await codex_gateway.resolve_text_model(body, key)
        context = await _record_call(
            request,
            key,
            model=model,
            is_stream=body.get("stream") is True,
            client_model=client_model,
        )
        try:
            body = await image_artifacts.resolve_file_ids(body, str(key["id"]))
        except ArtifactError as exc:
            raise _artifact_error(exc) from exc
        dispatch: TextDispatch = await codex_gateway.chat_request(body, key, x_session_id, context)
        if dispatch.lease is not None:
            buffered_model = codex_gateway.peek_response_model(dispatch.lease)
            if buffered_model:
                context.response_model = buffered_model
        if dispatch.mode == "json" and dispatch.json_body is not None:
            await codex_gateway.finish_call(
                context, status="success", http_status=200, completed=dispatch.json_body
            )
            return JSONResponse(dispatch.json_body, headers=_model_headers(context))
        assert dispatch.lease is not None
        if dispatch.stream:
            source = (
                codex_gateway.stream_passthrough(dispatch.lease, key, context)
                if dispatch.mode == "chat_sse"
                else codex_gateway.stream_response(dispatch.lease, key, chat=True, context=context)
            )
            return StreamingResponse(
                source,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    **_model_headers(context, streaming=True),
                },
            )
        completed, events = await codex_gateway.collect_response(dispatch.lease, context)
        state: dict[str, Any] = {}
        for event, data in events:
            convert_sse_to_chat_chunks(event, data, state)
        result = finalize_chat_completion(state)
        await codex_gateway.finish_call(
            context, status="success", http_status=200, completed=completed
        )
        return JSONResponse(result, headers=_model_headers(context))
    except BaseException as exc:
        if context is None:
            context = await _record_call(request, key, model="")
        await _finish_failure(context, exc, completed)
        raise


def _anthropic_error(exc: GatewayError) -> JSONResponse:
    headers = {"Retry-After": str(max(1, int(exc.retry_after or 2)))} if exc.status == 429 else None
    return JSONResponse(status_code=exc.status, content=anthropic_error_body(exc.status, exc.message), headers=headers)


async def _anthropic_sse_stream(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    buffer = ""
    state: dict[str, Any] = {}
    async for chunk in source:
        buffer += chunk.decode("utf-8", errors="replace")
        blocks, buffer = iter_sse_blocks(buffer)
        for block in blocks:
            event, data = parse_sse_event(block)
            for encoded in convert_sse_to_anthropic(event, data, state):
                yield encoded
    if buffer.strip():
        event, data = parse_sse_event(buffer)
        for encoded in convert_sse_to_anthropic(event, data, state):
            yield encoded


@router.post("/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None),
) -> JSONResponse | StreamingResponse:
    try:
        key = await _client_key(request, authorization, x_api_key)
    except GatewayError as exc:
        return _anthropic_error(exc)
    context: CallContext | None = None
    completed: dict[str, Any] | None = None
    try:
        body = await _json_object(request)
        client_stream = body.get("stream") is True
        payload = anthropic_to_responses(body, default_model=await codex_gateway.resolve_text_model(body, key))
        payload["stream"] = client_stream
        client_model = body.get("model") if isinstance(body.get("model"), str) else ""
        model = str(payload.get("model") or "")
        context = await _record_call(
            request,
            key,
            model=model,
            is_stream=client_stream,
            client_model=client_model,
        )
        try:
            payload = await image_artifacts.resolve_file_ids(payload, str(key["id"]))
        except ArtifactError as exc:
            raise _artifact_error(exc) from exc
        lease, _converted, stream = await codex_gateway.response_request(
            payload, key, x_session_id, context
        )
        buffered_model = codex_gateway.peek_response_model(lease)
        if buffered_model:
            context.response_model = buffered_model
        if stream:
            source = _anthropic_sse_stream(
                codex_gateway.stream_response(lease, key, chat=False, context=context)
            )
            return StreamingResponse(
                source,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    **_model_headers(context, streaming=True),
                },
            )
        completed, events = await codex_gateway.collect_response(lease, context)
        state: dict[str, Any] = {}
        for event, data in events:
            convert_sse_to_anthropic(event, data, state)
        result = finalize_anthropic_message(state)
        await codex_gateway.finish_call(
            context, status="success", http_status=200, completed=completed
        )
        return JSONResponse(result, headers=_model_headers(context))
    except GatewayError as exc:
        if context is None:
            context = await _record_call(request, key, model="")
        await _finish_failure(context, exc, completed)
        return _anthropic_error(exc)
    except BaseException as exc:
        if context is None:
            context = await _record_call(request, key, model="")
        await _finish_failure(context, exc, completed)
        raise


def _gemini_error(exc: GatewayError) -> JSONResponse:
    status = "UNAUTHENTICATED" if exc.status in {401, 403} else "INVALID_ARGUMENT"
    if exc.status == 429:
        status = "RESOURCE_EXHAUSTED"
    elif exc.status >= 500:
        status = "UNAVAILABLE"
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.status, "message": exc.message, "status": status}},
    )


async def _gemini_sse_stream(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    buffer = ""
    state: dict[str, Any] = {}
    async for chunk in source:
        buffer += chunk.decode("utf-8", errors="replace")
        blocks, buffer = iter_sse_blocks(buffer)
        for block in blocks:
            event, data = parse_sse_event(block)
            for encoded in convert_sse_to_gemini(event, data, state):
                yield encoded
    if buffer.strip():
        event, data = parse_sse_event(buffer)
        for encoded in convert_sse_to_gemini(event, data, state):
            yield encoded


@router.api_route("/v1beta/models/{model_name}:generateContent", methods=["POST"], response_model=None)
@router.api_route("/v1beta/models/{model_name}:streamGenerateContent", methods=["POST"], response_model=None)
@router.api_route("/v1/v1beta/models/{model_name}:generateContent", methods=["POST"], response_model=None)
@router.api_route("/v1/v1beta/models/{model_name}:streamGenerateContent", methods=["POST"], response_model=None)
async def gemini_generate_content(
    model_name: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_goog_api_key: str | None = Header(default=None, alias="x-goog-api-key"),
    x_session_id: str | None = Header(default=None),
) -> JSONResponse | StreamingResponse:
    try:
        key = await _client_key(request, authorization, x_api_key, extra=x_goog_api_key)
    except GatewayError as exc:
        return _gemini_error(exc)
    context: CallContext | None = None
    completed: dict[str, Any] | None = None
    streaming = request.url.path.endswith(":streamGenerateContent")
    try:
        body = await _json_object(request)
        if not isinstance(body.get("model"), str) or not body.get("model"):
            body["model"] = model_name
        payload = gemini_to_responses(body, default_model=await codex_gateway.resolve_text_model(body, key))
        payload["stream"] = streaming or body.get("stream") is True
        client_model = body.get("model") if isinstance(body.get("model"), str) else model_name
        model = str(payload.get("model") or model_name)
        context = await _record_call(
            request,
            key,
            model=model,
            is_stream=bool(payload.get("stream")),
            client_model=str(client_model or ""),
        )
        try:
            payload = await image_artifacts.resolve_file_ids(payload, str(key["id"]))
        except ArtifactError as exc:
            raise _artifact_error(exc) from exc
        lease, _converted, stream = await codex_gateway.response_request(
            payload, key, x_session_id, context
        )
        buffered_model = codex_gateway.peek_response_model(lease)
        if buffered_model:
            context.response_model = buffered_model
        if stream:
            source = _gemini_sse_stream(
                codex_gateway.stream_response(lease, key, chat=False, context=context)
            )
            return StreamingResponse(
                source,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    **_model_headers(context, streaming=True),
                },
            )
        completed, events = await codex_gateway.collect_response(lease, context)
        state: dict[str, Any] = {}
        for event, data in events:
            convert_sse_to_gemini(event, data, state)
        result = finalize_gemini_response(state)
        await codex_gateway.finish_call(
            context, status="success", http_status=200, completed=completed
        )
        return JSONResponse(result, headers=_model_headers(context))
    except GatewayError as exc:
        if context is None:
            context = await _record_call(request, key, model=model_name)
        await _finish_failure(context, exc, completed)
        return _gemini_error(exc)
    except BaseException as exc:
        if context is None:
            context = await _record_call(request, key, model=model_name)
        await _finish_failure(context, exc, completed)
        raise


@router.post("/v1/images/generations")
@router.post("/v1/codex/images/generations", include_in_schema=False)
async def images_generate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    body = await _json_object(request)
    raw_model = body.get("model") if isinstance(body.get("model"), str) else None
    context = await codex_gateway.begin_call(
        request_id=_request_id(request),
        key=key,
        endpoint=request.url.path,
        model=str(raw_model or ""),
        is_stream=False,
    )
    try:
        compact_url = str(body.get("response_format") or "").lower() == "url"
        upstream_body = dict(body)
        if compact_url:
            upstream_body.pop("response_format", None)
        result = await codex_gateway.image_json_tracked("generation", upstream_body, key, context)
        if compact_url:
            try:
                result = await image_artifacts.externalize_images_response(
                    result,
                    owner_key_id=str(key["id"]),
                    base_url=_public_base_url(request),
                )
            except ArtifactError as exc:
                raise _artifact_error(exc) from exc
        usage_data = result.get("usage")
        await codex_gateway.finish_call(
            context,
            status="success",
            http_status=200,
            usage=usage_data if isinstance(usage_data, dict) else None,
        )
        return result
    except BaseException as exc:
        await _finish_failure(context, exc)
        raise


@router.post("/v1/images/edits")
@router.post("/v1/codex/images/edits", include_in_schema=False)
async def images_edit(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    content_type = request.headers.get("content-type", "")
    response_format = ""
    raw_model: str | None = None
    form_fields: list[tuple[str, str]] | None = None
    form_files: list[tuple[str, UploadFile]] | None = None
    json_body: dict[str, Any] | None = None
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        form_fields = []
        form_files = []
        for name, value in form.multi_items():
            if isinstance(value, (UploadFile, StarletteUploadFile)):
                form_files.append((name, value))
            else:
                if name == "response_format":
                    response_format = str(value)
                elif name == "model":
                    raw_model = str(value)
                    form_fields.append((name, str(value)))
                else:
                    form_fields.append((name, str(value)))
    else:
        json_body = await _json_object(request)
        raw_model = json_body.get("model") if isinstance(json_body.get("model"), str) else None
        response_format = str(json_body.get("response_format") or "")
    context = await codex_gateway.begin_call(
        request_id=_request_id(request),
        key=key,
        endpoint=request.url.path,
        model=str(raw_model or ""),
        is_stream=False,
    )
    try:
        if form_fields is not None:
            files = form_files or []
            if not files:
                raise GatewayError(
                    422,
                    "At least one image file is required",
                    code="image_required",
                    error_type="invalid_request_error",
                )
            result = await codex_gateway.image_multipart(form_fields, files, key, context)
        else:
            assert json_body is not None
            upstream_body = dict(json_body)
            if response_format.lower() == "url":
                upstream_body.pop("response_format", None)
            result = await codex_gateway.image_json_tracked("edit", upstream_body, key, context)
        if response_format.lower() == "url":
            try:
                result = await image_artifacts.externalize_images_response(
                    result,
                    owner_key_id=str(key["id"]),
                    base_url=_public_base_url(request),
                )
            except ArtifactError as exc:
                raise _artifact_error(exc) from exc
        usage_data = result.get("usage")
        await codex_gateway.finish_call(
            context,
            status="success",
            http_status=200,
            usage=usage_data if isinstance(usage_data, dict) else None,
        )
        return result
    except BaseException as exc:
        await _finish_failure(context, exc)
        raise


async def _video_request_body(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return await _json_object(request)
    form = await request.form()
    body: dict[str, Any] = {}
    try:
        for name, value in form.multi_items():
            if isinstance(value, (UploadFile, StarletteUploadFile)):
                raw = await value.read()
                if len(raw) > settings.gateway_image_artifact_max_file_bytes:
                    raise GatewayError(
                        413,
                        "Video file is too large",
                        code="file_too_large",
                        error_type="invalid_request_error",
                    )
                body[f"_{name}_bytes"] = raw
                body[f"_{name}_mime"] = value.content_type or ""
                continue
            text = str(value)
            stripped = text.strip()
            if stripped[:1] in "{[":
                try:
                    body[name] = json.loads(stripped)
                    continue
                except json.JSONDecodeError:
                    pass
            body[name] = text
    finally:
        for _name, value in form.multi_items():
            if isinstance(value, (UploadFile, StarletteUploadFile)):
                await value.close()
    return body


async def _run_video_job(
    request: Request,
    key: dict[str, Any],
    handler,
) -> dict[str, Any]:
    context: CallContext | None = None
    try:
        body = await _video_request_body(request)
        raw_model = body.get("model") if isinstance(body.get("model"), str) else None
        context = await _record_call(
            request, key, model=str(raw_model or settings.grok_video_model)
        )
        result = await handler(body, key, context)
        await codex_gateway.finish_call(context, status="success", http_status=200)
        return result
    except BaseException as exc:
        if context is None:
            context = await _record_call(request, key, model=settings.grok_video_model)
        await _finish_failure(context, exc)
        raise


@router.post("/v1/videos/generations")
@router.post("/v1/videos")
async def videos_create(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    return await _run_video_job(request, key, codex_gateway.create_video)


@router.post("/v1/videos/edits")
async def videos_edit(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    return await _run_video_job(request, key, codex_gateway.edit_video)


@router.post("/v1/videos/extensions")
async def videos_extend(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    return await _run_video_job(request, key, codex_gateway.extend_video)


@router.get("/v1/videos")
async def videos_list(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    after: str | None = Query(default=None),
    limit: int = Query(default=20),
    order: str = Query(default="desc"),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    return await codex_gateway.list_videos(key, after=after, limit=limit, order=order)


@router.get("/v1/videos/{video_id}")
@router.get("/v1/videos/generations/{video_id}")
async def videos_get(
    request: Request,
    video_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    context = await codex_gateway.begin_call(
        request_id=_request_id(request),
        key=key,
        endpoint=request.url.path,
        model=settings.grok_video_model,
        is_stream=False,
    )
    try:
        result = await codex_gateway.get_video(video_id, key, context)
        await codex_gateway.finish_call(context, status="success", http_status=200)
        return result
    except BaseException as exc:
        await _finish_failure(context, exc)
        raise


@router.post("/v1/videos/{video_id}/remix")
async def videos_remix(
    request: Request,
    video_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)

    async def _remix(body: dict[str, Any], client_key: dict[str, Any], context: CallContext | None):
        return await codex_gateway.remix_video(video_id, body, client_key, context)

    return await _run_video_job(request, key, _remix)


@router.delete("/v1/videos/{video_id}")
@router.delete("/v1/videos/generations/{video_id}")
async def videos_delete(
    request: Request,
    video_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    key = await _client_key(request, authorization, x_api_key)
    return await codex_gateway.delete_video(video_id, key)


@router.get("/v1/videos/{video_id}/content")
@router.get("/v1/videos/generations/{video_id}/content")
async def videos_content(
    request: Request,
    video_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    variant: str | None = Query(default=None),
) -> Response:
    key = await _client_key(request, authorization, x_api_key)
    payload, content_type = await codex_gateway.video_content(
        video_id, key, variant=variant or "video"
    )
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )
