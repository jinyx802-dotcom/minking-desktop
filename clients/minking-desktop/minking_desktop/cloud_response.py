"""Collect a cloud Responses stream for the desktop's synchronous call form."""
from __future__ import annotations

import json


def collect_response(value):
    if isinstance(value, dict):
        return (502 if value.get("error") else 200), value
    if not isinstance(value, str):
        return 502, {"error": {"message": "云端响应格式无效"}}
    items, terminal = {}, None
    data, event = [], ""
    for line in [*value.splitlines(), ""]:
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line:
            raw = "\n".join(data)
            data = []
            if not raw or raw == "[DONE]":
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                return 502, {"error": {"message": "云端返回了无效的流式事件"}}
            if not isinstance(item, dict):
                return 502, {"error": {"message": "云端流式事件格式无效"}}
            kind = item.get("type", event)
            event = ""
            if kind in {"error", "response.failed"}:
                response = item.get("response") or {}
                error = item.get("error") or response.get("error") or {"message": item.get("message", "云端生成失败")}
                return 502, {"error": error}
            if kind == "response.output_item.done" and isinstance(item.get("item"), dict):
                index = item.get("output_index", len(items))
                items[index] = item["item"]
            if kind in {"response.completed", "response.incomplete"}:
                terminal = item.get("response")
    if not isinstance(terminal, dict):
        return 502, {"error": {"message": "云端响应中断，未收到完成事件"}}
    if not terminal.get("output") and items:
        terminal = {**terminal, "output": [items[index] for index in sorted(items)]}
    if terminal.get("error"):
        return 502, terminal
    return 200, terminal
