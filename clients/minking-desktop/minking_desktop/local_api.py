"""HTTP API owned by the desktop process. No browser pages are served."""
from __future__ import annotations

import argparse
import codecs
import contextlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from minking_desktop.official import OfficialGateway, NAMES, cp, read_object
from minking_desktop.paths import appdata_root
from minking_desktop.secrets import SecretError, default_protect, default_unprotect, forget_protected

def local_key(root: Path) -> str:
    """Separate from the portal token; stable across restarts when OS protection is available."""
    path = root / "local-api-key.dpapi"
    if path.exists():
        try:
            return default_unprotect(path.read_bytes()).decode("utf-8")
        except (SecretError, OSError, UnicodeDecodeError):
            pass
    key = "mk-local-" + secrets.token_urlsafe(32)
    try:
        blob = default_protect(key.encode("utf-8"))
    except SecretError:
        return key
    root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            forget_protected(path.read_bytes())
        except OSError:
            pass
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(blob)
    temporary.replace(path)
    return key


async def events(chunks):
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    try:
        async for chunk in chunks:
            buffer += decoder.decode(chunk)
            blocks, buffer = cp.iter_sse_blocks(buffer)
            for block in blocks:
                yield cp.parse_sse_event(block)
        buffer += decoder.decode(b"", final=True)
        if buffer.strip():
            yield cp.parse_sse_event(buffer)
    finally:
        await chunks.aclose()


def create_app(*, home: Path | None = None, appdata: Path | None = None, api_key: str | None = None,
               transport=None, localappdata: Path | None = None):
    root = appdata or appdata_root()
    gateway = OfficialGateway(home or Path.home(), root, transport=transport, localappdata=localappdata)
    token = api_key or local_key(root)
    settings_path = root / "local-gateway.json"
    saved = read_object(settings_path)
    config = {"default_model": saved.get("default_model", ""),
              "enabled_providers": [p for p in saved.get("enabled_providers", list(NAMES)) if p in NAMES]}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await gateway.scan()
        yield
        await gateway.close()

    app = FastAPI(title="MinKing Local", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gateway = gateway
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        if (origin and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": {"message": "仅允许本机同源访问"}}, 403)
        if request.url.path.startswith(("/v1/", "/api/")):
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ") or request.headers.get("x-api-key", "")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"error": {"message": "本地 API Key 无效", "type": "authentication_error"}}, 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(HTTPException)
    async def failure(request, exc):
        return JSONResponse({"error": {"message": str(exc.detail), "type": "local_gateway_error"}}, exc.status_code)

    @app.exception_handler(Exception)
    async def internal_failure(request, exc):
        # Never include a credential-bearing exception repr in an HTTP response.
        return JSONResponse({"error": {"message": "本地处理失败，请重新扫描后重试", "type": "local_gateway_error"}}, 500)

    async def body(request: Request) -> dict:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4 * 1024 * 1024:
                raise HTTPException(413, "请求超过 4 MB")
        try:
            value = json.loads(raw)
        except ValueError:
            raise HTTPException(422, "请求必须是 JSON 对象") from None
        if not isinstance(value, dict):
            raise HTTPException(422, "请求必须是 JSON 对象")
        return value

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "local_official"}

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request):
        return {"api_key": token, "base_url": str(request.base_url).rstrip("/") + "/v1",
                "mode": "official_only", "network": "system_proxy" if gateway.proxy else "direct",
                "config": config, "accounts": gateway.public()}

    @app.post("/api/scan")
    async def scan():
        return {"accounts": await gateway.sync()}

    @app.post("/api/providers/{provider}/probe")
    async def probe(provider: str):
        return await gateway.probe(provider)

    @app.put("/api/config")
    async def save_config(request: Request):
        value = await body(request)
        enabled = value.get("enabled_providers", list(NAMES))
        if not isinstance(enabled, list) or any(not isinstance(p, str) or p not in NAMES for p in enabled):
            raise HTTPException(422, "平台列表无效")
        default = value.get("default_model", "")
        if default:
            account, _ = gateway.resolve(default)
            if account.provider not in enabled:
                raise HTTPException(422, "默认模型的平台必须启用")
        elif not isinstance(default, str):
            raise HTTPException(422, "默认模型格式无效")
        new = {"default_model": default, "enabled_providers": list(dict.fromkeys(enabled))}
        root.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(settings_path)
        config.update(new)
        return config

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{**m, "object": "model", "owned_by": a.provider}
                for a in gateway.accounts.values() if a.provider in config["enabled_providers"]
                for m in a.models]}

    async def dispatch(request: Request, endpoint: str):
        from app.providers import anthropic_protocol as ap
        payload = await body(request)
        payload.setdefault("model", config["default_model"])
        account, model = gateway.resolve(payload.get("model"))
        if account.provider not in config["enabled_providers"]:
            raise HTTPException(403, "该平台已在本地配置中停用")
        if "stream" in payload and not isinstance(payload["stream"], bool):
            raise HTTPException(422, "stream 必须是布尔值")
        chunks, account, mode = await gateway.generate(payload, endpoint)
        headers = {"X-Local-Provider": account.provider, "X-Local-Official-Model": model}
        stream = payload.get("stream", False) and endpoint not in {"videos", "images/generations"}

        def note_success():
            for m in account.models:
                if m["official_id"] == model:
                    m["availability"] = "call_verified"

        async def translated():
            state = {"model": model}
            items, terminal = {}, False
            try:
                if mode == "native":
                    async for chunk in chunks:
                        yield chunk
                else:
                    async for name, data in events(chunks):
                        if data and data != "[DONE]":
                            item = json.loads(data)
                            kind = item.get("type", name)
                            if kind == "response.output_item.done" and isinstance(item.get("item"), dict):
                                items[item.get("output_index", len(items))] = item["item"]
                            if kind in {"response.completed", "response.incomplete"}:
                                response = item.get("response", {})
                                if not response.get("output") and items:
                                    response["output"] = [items[i] for i in sorted(items)]
                                data = json.dumps(item, ensure_ascii=False)
                                terminal = True
                                if kind == "response.completed":
                                    note_success()
                            if kind in {"response.failed", "error"}:
                                terminal = True
                        if endpoint == "responses":
                            yield f"event: {name}\ndata: {data}\n\n".encode("utf-8")
                        elif endpoint == "messages":
                            for chunk in ap.convert_sse_to_anthropic(name, data, state):
                                yield chunk
                        else:
                            for chunk in cp.convert_sse_to_chat_chunks(name, data, state):
                                yield chunk
                    if not terminal:
                        raise HTTPException(502, "官方流未正常结束")
            except (httpx.HTTPError, HTTPException, ValueError):
                yield b'event: error\ndata: {"error":{"message":"Official stream interrupted"}}\n\n'
            finally:
                await chunks.aclose()

        if stream:
            return StreamingResponse(translated(), media_type="text/event-stream", headers=headers)
        try:
            if mode == "native":
                result = bytearray()
                async for chunk in chunks:
                    result.extend(chunk)
                    if len(result) > 64 * 1024 * 1024:
                        raise HTTPException(502, "官方响应超过本地大小限制")
                parsed = json.loads(result)
            else:
                state = {"model": model}
                completed = None
                items = {}
                async for name, data in events(chunks):
                    if name in {"error", "response.failed"}:
                        raise HTTPException(502, "官方模型调用失败")
                    if endpoint == "chat/completions":
                        cp.convert_sse_to_chat_chunks(name, data, state)
                    elif endpoint == "messages":
                        ap.convert_sse_to_anthropic(name, data, state)
                    if data and data != "[DONE]":
                        item = json.loads(data)
                        event_type = item.get("type", name)
                        if event_type == "response.output_item.done" and isinstance(item.get("item"), dict):
                            items[item.get("output_index", len(items))] = item["item"]
                        if event_type in {"error", "response.failed"}:
                            raise HTTPException(502, "官方模型调用失败")
                        if event_type in {"response.completed", "response.incomplete"}:
                            completed = item.get("response")
                if not isinstance(completed, dict):
                    raise HTTPException(502, "官方响应未正常完成")
                if not completed.get("output") and items:
                    completed["output"] = [items[i] for i in sorted(items)]
                parsed = cp.finalize_chat_completion(state) if endpoint == "chat/completions" else completed
                if endpoint == "messages":
                    parsed = ap.finalize_anthropic_message(state)
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "官方响应中断或格式无法识别") from None
        finally:
            await chunks.aclose()
        if isinstance(parsed, dict) and not parsed.get("error") and parsed.get("status") not in {"failed", "incomplete"}:
            note_success()
        return JSONResponse(parsed, headers=headers)

    for endpoint in ("responses", "chat/completions", "messages", "images/generations", "videos"):
        def route(path):
            async def invoke(request: Request):
                return await dispatch(request, path)
            return invoke
        app.add_api_route("/v1/" + endpoint, route(endpoint), methods=["POST"])

    @app.get("/v1/videos/{job_id}")
    async def video_status(job_id: str):
        from minking_desktop.official import MODEL_ID
        if not MODEL_ID.fullmatch(job_id):
            raise HTTPException(422, "视频任务 ID 无效")
        if "grok" not in config["enabled_providers"]:
            raise HTTPException(403, "Grok 已停用")
        response = await gateway.request(gateway.require("grok"), "GET", "/videos/" + job_id)
        return JSONResponse(response.json())

    return app


def run(argv=None):
    import uvicorn
    parser = argparse.ArgumentParser(description="MinKing 官方模型本地网关")
    parser.add_argument("--port", type=int, default=18787)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须介于 1024–65535")
    # Upstream debug output can include headers: keep protocol helpers quiet.
    for name in ("httpx", "httpcore", "transfer_station.errors"):
        logging.getLogger(name).disabled = True
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="critical")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
