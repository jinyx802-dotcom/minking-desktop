from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import ClientDisconnect
from urllib.parse import urlsplit

from app import __version__
from app.admin_auth import admin_auth
from app.api.codex_gateway import openai_error
from app.api.codex_gateway import router as codex_router
from app.api.routes import router
from app.codex_gateway import GatewayError, codex_gateway, error_event_message
from app.config import settings
from app.downloads import EXE_NAME, SHA256_NAME, package_available, resolve_download, sha256_digest
from app.portal import router as portal_router, require_enabled
from app.console_api import router as console_router
from app.http_client import close_shared_client
from app.image_artifacts import image_artifacts
from app.mcp_server import mcp_http_app

WEB_DIR = Path(__file__).resolve().parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
error_logger = logging.getLogger("transfer_station.errors")
error_logger.setLevel(
    getattr(logging, settings.log_level.strip().upper(), logging.INFO)
)


async def _log_request_error(request: Request, exc: GatewayError) -> None:
    fields = (request.method, request.url.path, exc.status, exc.code)
    user_name = exc.user_name or getattr(request.state, "user_name", None)
    message = error_event_message(exc)
    if exc.status >= 500:
        error_logger.error(
            "request_failed method=%s path=%s status=%s code=%s user=%r detail=%r",
            *fields,
            user_name,
            message,
            exc_info=exc.__cause__ or exc,
        )
    else:
        error_logger.warning(
            "request_rejected method=%s path=%s status=%s code=%s user=%r detail=%r",
            *fields,
            user_name,
            message,
        )
    await codex_gateway.record_error(
        level="error" if exc.status >= 500 else "warning",
        category="request",
        code=exc.code,
        user_name=user_name,
        method=request.method,
        path=request.url.path,
        status=exc.status,
        message=message,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings.gateway_credential_dir()
    await codex_gateway.start()
    try:
        await image_artifacts.start(codex_gateway.secret)
        await admin_auth.start()
    except Exception:
        await image_artifacts.stop()
        await codex_gateway.stop()
        raise
    try:
        async with mcp_http_app.lifespan():
            yield
    finally:
        await close_shared_client()
        await image_artifacts.stop()
        await codex_gateway.stop()


app = FastAPI(
    title="MinKing AI",
    version=__version__,
    description="Multi-provider OpenAI-compatible forwarding gateway",
    lifespan=lifespan,
    root_path=settings.app_root_path,
)
app.include_router(router)
app.include_router(codex_router)
app.include_router(console_router)
app.include_router(portal_router)
app.include_router(portal_router, prefix="/admin")
app.include_router(portal_router, prefix="/v1")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.mount("/admin/static", StaticFiles(directory=str(WEB_DIR / "static")), name="admin_static")
app.mount("/v1/static", StaticFiles(directory=str(WEB_DIR / "static")), name="v1_static")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex
    started_monotonic = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    user_agent = request.headers.get("user-agent", "")
    if (
        settings.gateway_codebuddy_connection_close_enabled
        and request.url.path.startswith("/v1/")
        and "CodeBuddy/" in user_agent
    ):
        response.headers["Connection"] = "close"
    if settings.gateway_request_logging_enabled:
        client_host = request.client.host if request.client else None
        safe_user_agent = user_agent[:200].replace("\r", " ").replace("\n", " ")
        error_logger.info(
            "request_completed request_id=%s client=%s method=%s path=%s status=%s "
            "duration_ms=%s user_agent=%r content_type=%r content_length=%r "
            "transfer_encoding=%r content_encoding=%r",
            request.state.request_id,
            client_host,
            request.method,
            request.url.path,
            response.status_code,
            round((time.monotonic() - started_monotonic) * 1000),
            safe_user_agent,
            request.headers.get("content-type"),
            request.headers.get("content-length"),
            request.headers.get("transfer-encoding"),
            request.headers.get("content-encoding"),
        )
    return response


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError):
    await _log_request_error(request, exc)
    return openai_error(exc)


@app.exception_handler(ClientDisconnect)
async def client_disconnect_handler(request: Request, exc: ClientDisconnect):
    gateway_error = GatewayError(
        499,
        "Client disconnected before the request was complete",
        code="client_interrupted",
        error_type="request_error",
    )
    await _log_request_error(request, gateway_error)
    return openai_error(gateway_error)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    gateway_error = GatewayError(
        exc.status_code,
        str(exc.detail),
        code="invalid_request" if exc.status_code < 500 else "gateway_error",
        error_type="invalid_request_error" if exc.status_code < 500 else "gateway_error",
    )
    await _log_request_error(request, gateway_error)
    return openai_error(gateway_error)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    gateway_error = GatewayError(
        422,
        "Request validation failed",
        code="invalid_request",
        error_type="invalid_request_error",
    )
    error_logger.warning(
        "request_rejected method=%s path=%s status=422 code=invalid_request validation_errors=%s",
        request.method,
        request.url.path,
        len(exc.errors()),
    )
    await codex_gateway.record_error(
        level="warning",
        category="validation",
        code="invalid_request",
        user_name=getattr(request.state, "user_name", None),
        method=request.method,
        path=request.url.path,
        status=422,
        message="Request validation failed",
    )
    return openai_error(gateway_error)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    error_logger.error(
        "request_crashed method=%s path=%s status=500 code=internal_error",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    await codex_gateway.record_error(
        level="error",
        category="unhandled",
        code="internal_error",
        user_name=getattr(request.state, "user_name", None),
        method=request.method,
        path=request.url.path,
        status=500,
        message="Internal server error",
    )
    return openai_error(
        GatewayError(500, "Internal server error", code="internal_error")
    )


def _request_root_path(request: Request) -> str:
    scope_root = str(request.scope.get("root_path", "")).rstrip("/")
    if scope_root:
        return scope_root
    prefix_hdr = request.headers.get("x-forwarded-prefix", "").strip().rstrip("/")
    if prefix_hdr:
        return prefix_hdr
    if settings.gateway_public_base_url:
        parsed = urlsplit(settings.gateway_public_base_url)
        req_host = request.headers.get("host", "").split(":")[0].lower()
        if parsed.hostname and req_host == parsed.hostname.lower():
            base_path = parsed.path.rstrip("/")
            if base_path.endswith("/v1"):
                base_path = base_path[:-3].rstrip("/")
            if base_path:
                return base_path
    return ""


def _portal_root_path(request: Request) -> str:
    """Keep /v1 so public reverse proxies that only forward /maliang/v1 still reach the portal."""
    prefix = _request_root_path(request).rstrip("/")
    path = request.url.path
    under_v1 = path == "/v1" or path.startswith("/v1/")
    if not under_v1:
        return prefix
    if prefix.endswith("/v1"):
        return prefix
    return f"{prefix}/v1" if prefix else "/v1"


def _site_context(request: Request, *, focus: str = "") -> dict:
    return {
        "version": __version__,
        "root_path": _portal_root_path(request),
        "admin_path": f"{_request_root_path(request)}/admin",
        "package_available": package_available(),
        "sha256": sha256_digest(),
        "focus": focus,
    }


def _missing_package() -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": "安装包尚未发布",
                "type": "not_found",
                "param": None,
                "code": "not_found",
            }
        },
        status_code=404,
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/v1/site", response_class=HTMLResponse, include_in_schema=False)
async def marketing_site(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "site.html",
        _site_context(request),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/download", response_class=HTMLResponse, include_in_schema=False)
@app.get("/v1/download", response_class=HTMLResponse, include_in_schema=False)
async def marketing_download(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "site.html",
        _site_context(request, focus="download"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/download/{filename}", include_in_schema=False)
@app.get("/v1/download/{filename}", include_in_schema=False)
async def marketing_package(filename: str):
    path = resolve_download(filename)
    if path is None:
        return _missing_package()
    media = "text/plain; charset=utf-8" if filename == SHA256_NAME else "application/vnd.microsoft.portable-executable"
    return FileResponse(
        path,
        media_type=media,
        filename=EXE_NAME if filename == EXE_NAME else SHA256_NAME,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"version": __version__, "root_path": _request_root_path(request)},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/portal", response_class=HTMLResponse, include_in_schema=False)
@app.get("/admin/portal", response_class=HTMLResponse, include_in_schema=False)
@app.get("/v1/portal", response_class=HTMLResponse, include_in_schema=False)
async def portal_page(request: Request) -> HTMLResponse:
    require_enabled()
    return templates.TemplateResponse(
        request, "portal.html", {"version": __version__, "root_path": _portal_root_path(request)},
        headers={"Cache-Control": "no-store"},
    )


# Keep this catch-all mount last so the existing HTTP/admin routes win first.
app.mount("/", mcp_http_app, name="mcp")
