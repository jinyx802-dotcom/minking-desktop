from __future__ import annotations

import base64
import binascii
import datetime as dt
import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from app.codex_gateway import CallContext, GatewayError, codex_gateway
from app.config import settings
from app.image_artifacts import ArtifactError, image_artifacts

logger = logging.getLogger("transfer_station.errors")


@dataclass(frozen=True)
class MCPRequestIdentity:
    key: dict[str, Any]
    request_id: str
    base_url: str


_request_identity: ContextVar[MCPRequestIdentity | None] = ContextVar(
    "transfer_station_mcp_identity", default=None
)


class MCPBearerAuthMiddleware:
    """Authenticate Streamable HTTP requests with existing gateway API keys."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") not in {"/mcp", "/mcp/"}:
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        raw_key = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        try:
            key = await codex_gateway.authenticate_key(raw_key)
        except GatewayError:
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        configured = settings.gateway_public_base_url.strip().rstrip("/")
        host = headers.get("host", "127.0.0.1")
        base_url = configured or f"{scope.get('scheme', 'http')}://{host}"
        identity = MCPRequestIdentity(
            key=key,
            request_id=uuid.uuid4().hex,
            base_url=base_url,
        )
        token: Token[MCPRequestIdentity | None] = _request_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_identity.reset(token)


mcp = FastMCP(
    "MinKing AI Image Generation",
    instructions=(
        "Generate or edit images through the authenticated MinKing AI account pool. "
        "Image calls consume upstream quota. Never echo authentication data. Generated image "
        "URLs expire after one hour."
    ),
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    log_level="WARNING",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "10.1.102.36:8787",
            "10.1.102.36:*",
            "127.0.0.1:*",
            "localhost:*",
            "testserver",
        ],
        allowed_origins=[
            "http://10.1.102.36:8787",
            "http://10.1.102.36:*",
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://testserver",
        ],
    ),
)


def _identity() -> MCPRequestIdentity:
    identity = _request_identity.get()
    if identity is None:
        raise RuntimeError("Authenticated MCP request context is unavailable")
    return identity


def _image_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    raise ArtifactError(502, "Upstream returned an unsupported image", "upstream_invalid_image")


def _decode_image(value: str) -> bytes:
    encoded = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ArtifactError(502, "Upstream returned invalid image data", "upstream_invalid_image") from exc


async def _file_data_url(file_id: str, owner_key_id: str) -> str:
    path, mime_type = await image_artifacts.content(file_id, owner_key_id)
    raw = path.read_bytes()
    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"


async def _finish_failure(context: CallContext, exc: BaseException) -> None:
    status = exc.status if isinstance(exc, (GatewayError, ArtifactError)) else 500
    code = exc.code if isinstance(exc, (GatewayError, ArtifactError)) else "internal_error"
    await codex_gateway.finish_call(
        context, status="failed", http_status=status, error_code=code
    )


async def _tool_result(
    payload: dict[str, Any], *, identity: MCPRequestIdentity, output_format: str
) -> CallToolResult:
    blocks: list[TextContent | ImageContent] = []
    metadata: list[dict[str, Any]] = []
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise GatewayError(
            502,
            "Image generation returned no image",
            code="upstream_invalid_response",
            error_type="upstream_error",
        )
    for index, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("b64_json"), str):
            continue
        raw = _decode_image(str(item["b64_json"]))
        mime_type = _image_mime(raw)
        artifact = await image_artifacts.create_bytes(
            raw,
            owner_key_id=str(identity.key["id"]),
            purpose="generated",
            filename=f"mcp-generated-{index}.{output_format}",
            ttl=dt.timedelta(minutes=max(1, settings.gateway_image_output_ttl_minutes)),
            mime_hint=mime_type,
        )
        url = image_artifacts.signed_url(str(artifact["id"]), identity.base_url)
        blocks.append(ImageContent(type="image", data=base64.b64encode(raw).decode("ascii"), mimeType=mime_type))
        metadata.append({
            "url": url,
            "mime_type": mime_type,
            "byte_size": int(artifact["bytes"]),
            "expires_at": int(artifact["expires_at"]),
        })
    if not metadata:
        raise GatewayError(
            502,
            "Image generation returned no usable image",
            code="upstream_invalid_response",
            error_type="upstream_error",
        )
    blocks.insert(0, TextContent(
        type="text",
        text="Generated image URLs (valid for one hour):\n" + "\n".join(
            str(item["url"]) for item in metadata
        ),
    ))
    structured = {
        "created": payload.get("created"),
        "model": payload.get("model") or settings.codex_image_model,
        "images": metadata,
    }
    return CallToolResult(content=blocks, structuredContent=structured, isError=False)


async def _execute_image(
    *, kind: Literal["generation", "edit"], body: dict[str, Any], endpoint: str, count: int = 1
) -> CallToolResult:
    identity = _identity()
    context = await codex_gateway.begin_call(
        request_id=identity.request_id,
        key=identity.key,
        endpoint=endpoint,
        model=str(body.get("model") or settings.codex_image_model),
        is_stream=False,
    )
    try:
        combined: dict[str, Any] = {"data": []}
        usage_total: dict[str, int] = {}
        for _ in range(count):
            request_body = dict(body)
            request_body.pop("n", None)
            result = await codex_gateway.image_json_tracked(
                kind, request_body, identity.key, context
            )
            if combined.get("created") is None:
                combined.update({key: value for key, value in result.items() if key != "data"})
            if isinstance(result.get("data"), list):
                combined["data"].extend(result["data"])
            if isinstance(result.get("usage"), dict):
                for key, value in result["usage"].items():
                    if isinstance(value, int):
                        usage_total[key] = usage_total.get(key, 0) + value
        if usage_total:
            combined["usage"] = usage_total
        tool_result = await _tool_result(
            combined,
            identity=identity,
            output_format=str(body.get("output_format") or "png"),
        )
        await codex_gateway.finish_call(
            context,
            status="success",
            http_status=200,
            usage=usage_total or None,
        )
        return tool_result
    except (GatewayError, ArtifactError) as exc:
        await _finish_failure(context, exc)
        return CallToolResult(
            content=[TextContent(type="text", text=exc.message)],
            isError=True,
        )
    except Exception as exc:
        await _finish_failure(context, exc)
        logger.exception("mcp_image_tool_failed endpoint=%s", endpoint)
        return CallToolResult(
            content=[TextContent(type="text", text="Image tool failed")],
            isError=True,
        )


_IMAGE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
_IMAGE_QUALITY_VALUES = {"auto", "low", "medium", "high"}


def _quality_error(quality: str | None) -> CallToolResult | None:
    if quality is None or quality in _IMAGE_QUALITY_VALUES:
        return None
    return CallToolResult(
        content=[TextContent(
            type="text",
            text="quality must be one of auto, low, medium, high",
        )],
        isError=True,
    )


@mcp.tool(
    name="generate_image",
    description="Generate one or more images from a text prompt with GPT Image.",
    annotations=_IMAGE_ANNOTATIONS,
    structured_output=False,
)
async def generate_image(
    prompt: str,
    model: str = "gpt-image-2.5-flare",
    n: int = 1,
    size: str | None = None,
    quality: str | None = None,
    background: str | None = None,
    output_format: str = "png",
    output_compression: int | None = None,
    moderation: str | None = None,
) -> CallToolResult:
    if not prompt.strip():
        return CallToolResult(
            content=[TextContent(type="text", text="prompt must not be empty")], isError=True
        )
    if n < 1 or n > 4:
        return CallToolResult(
            content=[TextContent(type="text", text="n must be between 1 and 4")], isError=True
        )
    if quality_error := _quality_error(quality):
        return quality_error
    body: dict[str, Any] = {"prompt": prompt, "model": model, "output_format": output_format}
    for key, value in {
        "size": size,
        "quality": quality,
        "background": background,
        "output_compression": output_compression,
        "moderation": moderation,
    }.items():
        if value is not None:
            body[key] = value
    return await _execute_image(
        kind="generation", body=body, endpoint="/mcp/tools/generate_image", count=n
    )


@mcp.tool(
    name="edit_image",
    description="Edit one or more images, optionally with a mask, using GPT Image.",
    annotations=_IMAGE_ANNOTATIONS,
    structured_output=False,
)
async def edit_image(
    prompt: str,
    image_urls: list[str] | None = None,
    file_ids: list[str] | None = None,
    mask_url: str | None = None,
    mask_file_id: str | None = None,
    model: str = "gpt-image-2.5-flare",
    size: str | None = None,
    quality: str | None = None,
    background: str | None = None,
    output_format: str = "png",
    output_compression: int | None = None,
    input_fidelity: str | None = None,
    moderation: str | None = None,
) -> CallToolResult:
    identity = _identity()
    if not prompt.strip():
        return CallToolResult(
            content=[TextContent(type="text", text="prompt must not be empty")], isError=True
        )
    if mask_url and mask_file_id:
        return CallToolResult(
            content=[TextContent(type="text", text="mask_url and mask_file_id are mutually exclusive")],
            isError=True,
        )
    if quality_error := _quality_error(quality):
        return quality_error
    images = list(image_urls or [])
    try:
        images.extend([
            await _file_data_url(file_id, str(identity.key["id"]))
            for file_id in (file_ids or [])
        ])
        if not images:
            raise ArtifactError(400, "At least one input image is required", "image_required")
        mask = mask_url
        if mask_file_id:
            mask = await _file_data_url(mask_file_id, str(identity.key["id"]))
    except ArtifactError as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=exc.message)], isError=True
        )
    body: dict[str, Any] = {
        "prompt": prompt,
        "model": model,
        "image": images,
        "output_format": output_format,
    }
    if mask:
        body["mask"] = mask
    for key, value in {
        "size": size,
        "quality": quality,
        "background": background,
        "output_compression": output_compression,
        "input_fidelity": input_fidelity,
        "moderation": moderation,
    }.items():
        if value is not None:
            body[key] = value
    return await _execute_image(kind="edit", body=body, endpoint="/mcp/tools/edit_image")


class RestartableMCPHTTPApp:
    """Recreate the SDK transport manager for each application lifespan."""

    def __init__(self) -> None:
        self._app: Any | None = None

    @asynccontextmanager
    async def lifespan(self):
        # FastMCP session managers are intentionally single-use. Uvicorn has one
        # lifespan, while the test suite starts the same ASGI app repeatedly.
        mcp._session_manager = None
        starlette_app = mcp.streamable_http_app()
        self._app = MCPBearerAuthMiddleware(starlette_app)
        try:
            async with starlette_app.router.lifespan_context(starlette_app):
                yield
        finally:
            self._app = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self._app is None:
            body = b'{"error":"mcp_unavailable"}'
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


mcp_http_app = RestartableMCPHTTPApp()
