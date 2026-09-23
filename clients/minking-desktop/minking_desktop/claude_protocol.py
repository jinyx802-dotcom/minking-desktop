"""Responses/Chat to official Anthropic Messages, without executing client tools."""
from __future__ import annotations

import codecs
import json

from fastapi import HTTPException
from app.providers import codex_protocol as cp, workbuddy as wb


def messages_payload(envelope: dict, model: str) -> dict:
    for tool in envelope.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise HTTPException(422, "Claude 兼容接口目前仅支持 function 工具，请使用 /v1/messages 传入原生工具")
    try:
        chat = wb.responses_to_chat(envelope, model=model)
    except ValueError:
        raise HTTPException(422, "Claude 请求内容无法转换") from None
    messages, system = [], []
    for message in chat["messages"]:
        role, content = message["role"], message.get("content")
        if role in {"system", "developer"}:
            if isinstance(content, str):
                system.append(content)
            continue
        blocks = []
        if role == "tool":
            role = "user"
            blocks.append({"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": content or ""})
        elif isinstance(content, str) and content:
            blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    blocks.append(part)
                elif part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    if url.startswith("data:") and ";base64," in url:
                        head, data = url.split(";base64,", 1)
                        source = {"type": "base64", "media_type": head[5:], "data": data}
                    else:
                        source = {"type": "url", "url": url}
                    blocks.append({"type": "image", "source": source})
        for tool in message.get("tool_calls", []):
            try:
                arguments = json.loads(tool["function"]["arguments"])
            except ValueError:
                raise HTTPException(422, "工具参数必须是 JSON") from None
            blocks.append({"type": "tool_use", "id": tool["id"], "name": tool["function"]["name"], "input": arguments})
        if blocks:
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
    result = {"model": model, "messages": messages, "stream": True,
              "max_tokens": envelope.get("max_output_tokens") or envelope.get("max_tokens") or 4096}
    if system:
        result["system"] = "\n\n".join(system)
    tools = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
              "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
             for t in chat.get("tools", [])]
    if tools:
        result["tools"] = tools
        choice = chat.get("tool_choice")
        if isinstance(choice, str):
            result["tool_choice"] = {"type": {"required": "any"}.get(choice, choice)}
        elif isinstance(choice, dict) and choice.get("function", {}).get("name"):
            result["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    return result


async def as_chat_chunks(chunks):
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    completed = False
    usage = {}

    def convert(block):
        nonlocal completed
        name, data = cp.parse_sse_event(block)
        if not data or data == "[DONE]":
            return []
        event = json.loads(data)
        kind = event.get("type", name)
        if kind == "error":
            raise HTTPException(502, "Claude 官方流返回错误")
        if kind == "message_start":
            usage["prompt_tokens"] = event.get("message", {}).get("usage", {}).get("input_tokens", 0)
        if kind == "message_delta":
            usage["completion_tokens"] = event.get("usage", {}).get("output_tokens", 0)
        delta = None
        if kind == "content_block_start":
            content = event.get("content_block", {})
            if content.get("type") == "tool_use":
                delta = {"tool_calls": [{"index": event["index"], "id": content["id"], "type": "function",
                                         "function": {"name": content["name"], "arguments": ""}}]}
            elif content.get("type") == "text" and content.get("text"):
                delta = {"content": content["text"]}
        elif kind == "content_block_delta":
            content = event.get("delta", {})
            if content.get("type") == "text_delta":
                delta = {"content": content.get("text", "")}
            elif content.get("type") == "input_json_delta":
                delta = {"tool_calls": [{"index": event["index"], "function": {"arguments": content.get("partial_json", "")}}]}
        if kind == "message_stop":
            completed = True
            return [cp.encode_sse_json({"choices": [], "usage": usage}), b"data: [DONE]\n\n"]
        return [cp.encode_sse_json({"choices": [{"index": 0, "delta": delta}]})] if delta else []

    try:
        async for chunk in chunks:
            buffer += decoder.decode(chunk)
            blocks, buffer = cp.iter_sse_blocks(buffer)
            for block in blocks:
                for output in convert(block):
                    yield output
        buffer += decoder.decode(b"", final=True)
        if buffer.strip():
            for output in convert(buffer):
                yield output
        if not completed:
            raise HTTPException(502, "Claude 官方响应未正常结束")
    finally:
        await chunks.aclose()
