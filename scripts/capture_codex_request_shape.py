"""Capture Codex /v1 request shape without logging secrets or prompt text.

Usage:
  python scripts/capture_codex_request_shape.py --listen 127.0.0.1:8788 \\
      --upstream https://ceshi.007ka.cn/maliang --out output/t0/shape.jsonl
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def request_shape(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"kind": type(payload).__name__}
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    tool_types: list[str] = []
    tool_names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            tool_types.append("other")
            continue
        tool_type = str(tool.get("type") or "unknown")
        tool_types.append(tool_type)
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = tool.get("name") or function.get("name")
        if isinstance(name, str) and name:
            tool_names.append(name)
    item_types: list[str] = []
    message_roles: list[str] = []
    incoming = payload.get("input")
    if isinstance(incoming, list):
        for item in incoming:
            if isinstance(item, dict):
                item_types.append(str(item.get("type") or item.get("role") or "other"))
                role = item.get("role")
                if isinstance(role, str) and role:
                    message_roles.append(role)
            else:
                item_types.append("other")
    elif isinstance(incoming, str):
        item_types.append("string")
    instructions = payload.get("instructions")
    return {
        "model": payload.get("model") if isinstance(payload.get("model"), str) else None,
        "stream": payload.get("stream") if isinstance(payload.get("stream"), bool) else None,
        "instruction_chars": len(instructions) if isinstance(instructions, str) else 0,
        "has_instructions_field": isinstance(instructions, str),
        "input_item_types": item_types,
        "message_roles": message_roles,
        "tool_count": len(tools),
        "tool_types": tool_types,
        "tool_names": tool_names,
        "has_image_generation": "image_generation" in tool_types,
        "has_generate_image": "generate_image" in tool_names,
        "has_apply_patch": "apply_patch" in tool_names,
        "top_keys": sorted(str(key) for key in payload.keys()),
    }


def _handler_class(upstream: str, out_path: Path, ready: threading.Event) -> type[BaseHTTPRequestHandler]:
    client = httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0), trust_env=True, follow_redirects=False)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _record(self, method: str, path: str, user_agent: str, payload: Any | None) -> None:
            record = {
                "method": method,
                "path": path.split("?", 1)[0],
                "query": path.split("?", 1)[1] if "?" in path else "",
                "user_agent": user_agent[:200],
                "content_type": self.headers.get("content-type", ""),
                "shape": request_shape(payload) if payload is not None else None,
            }
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        def _forward(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            payload: Any | None = None
            if raw:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    payload = parsed if isinstance(parsed, dict) else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
            user_agent = self.headers.get("user-agent", "")
            self._record(self.command, self.path, user_agent, payload)
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in HOP_BY_HOP
            }
            url = upstream.rstrip("/") + self.path
            try:
                response = client.request(
                    self.command,
                    url,
                    content=raw or None,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                body = json.dumps({"error": {"message": type(exc).__name__, "code": "capture_upstream_error"}}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() in HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.content)

        def do_GET(self) -> None:  # noqa: N802
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._forward()

    ready.set()
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture Codex request shape and forward to the gateway")
    parser.add_argument("--listen", default="127.0.0.1:8788")
    parser.add_argument("--upstream", default="https://ceshi.007ka.cn/maliang")
    parser.add_argument("--out", default="output/t0/shape.jsonl")
    args = parser.parse_args()
    host, port_text = args.listen.rsplit(":", 1)
    parsed = urlparse(args.upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("upstream must be an absolute http(s) URL")
    out_path = Path(args.out)
    ready = threading.Event()
    handler = _handler_class(args.upstream.rstrip("/"), out_path, ready)
    server = ThreadingHTTPServer((host, int(port_text)), handler)
    print(f"capture_listening {host}:{port_text} -> {args.upstream} out={out_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
