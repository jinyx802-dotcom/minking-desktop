from __future__ import annotations

import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from minking_desktop.local_api import create_app
from minking_desktop.official import ag

KEY = "local-test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def response_events(text="官方回复", output=None):
    result = {"id": "resp_test", "object": "response", "model": "grok-official", "status": "completed",
              "output": output or [{"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                                      "content": [{"type": "output_text", "text": text, "annotations": []}]}]}
    items = [{"type": "response.created", "response": {**result, "status": "in_progress", "output": []}},
             {"type": "response.output_text.delta", "delta": text, "output_index": 0, "content_index": 0, "item_id": "msg_test"},
             {"type": "response.completed", "response": result}]
    return "".join(f"event: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n" for item in items).encode()


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(ag, "windows_credential_payloads", lambda: [])
    home, data, localdata = tmp_path / "home", tmp_path / "appdata/MinKing", tmp_path / "local"
    home.mkdir()
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "GET" and request.url.path.endswith("models"):
            return httpx.Response(200, json={"data": [{"id": "grok-official"}]})
        return httpx.Response(200, content=response_events(), headers={"content-type": "text/event-stream"})
    write(home / ".grok/auth.json", {"https://auth.x.ai::test": {"key": "official-secret", "user_id": "test"}})
    app = create_app(home=home, appdata=data, localappdata=localdata, api_key=KEY, transport=httpx.MockTransport(handler))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, app, home, data, calls


def test_scan_keeps_credentials_private_and_files_unchanged(local):
    client, _, home, data, calls = local
    original = (home / ".grok/auth.json").read_bytes()
    result = client.get("/api/bootstrap", headers=AUTH)
    assert result.status_code == 200
    assert "official-secret" not in result.text
    assert "access_token" not in result.text
    assert next(a for a in result.json()["accounts"] if a["id"] == "grok")["status"] == "detected"
    assert not calls
    client.post("/api/scan", headers=AUTH)
    assert (home / ".grok/auth.json").read_bytes() == original
    assert not data.exists()


def test_one_click_sync_includes_media_catalog_without_generation(local):
    client, _, _, _, calls = local
    result = client.post('/api/scan', headers=AUTH)
    account = next(a for a in result.json()['accounts'] if a['id'] == 'grok')
    assert account['status'] == 'verified'
    assert {'text', 'image', 'video'} <= {m['type'] for m in account['models']}
    assert all(m['availability'] == 'unverified' for m in account['models'] if m['type'] != 'text')
    assert all(call.method == 'GET' for call in calls)


def test_anthropic_caller_can_use_local_grok_model(local):
    client, _, _, _, calls = local
    result = client.post('/v1/messages', headers={'x-api-key': KEY}, json={
        'model': 'grok/grok-official', 'max_tokens': 128, 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert result.status_code == 200
    assert result.json()['type'] == 'message'
    assert result.json()['content'][0]['text'] == '官方回复'


def test_image_and_video_request_routes_use_official_media_protocol(local):
    client, app, _, _, calls = local
    # Replace only transport responses; preserve real routing, shaping and authorization.
    def handler(request):
        calls.append(request)
        assert request.headers['accept'] == 'application/json'
        if request.url.path.endswith('/images/generations'):
            assert json.loads(request.content)['model'] == 'grok-imagine-image-2.0'
            return httpx.Response(200, json={'data':[{'b64_json':'aW1hZ2U='}]})
        if request.url.path.endswith('/videos/generations'):
            return httpx.Response(200, json={'request_id':'video-test','status':'pending'})
        return httpx.Response(200, json={'status':'done','video':{'url':'https://example.com/video.mp4'}})
    gateway = app.state.gateway
    gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert client.post('/v1/images/generations', headers=AUTH, json={'model':'grok/grok-imagine-image-2.0','prompt':'square'}).json()['data']
    assert client.post('/v1/videos', headers=AUTH, json={'model':'grok/grok-imagine-video-1.5','prompt':'square'}).json()['request_id'] == 'video-test'
    assert client.get('/v1/videos/video-test',headers=AUTH).json()['status'] == 'done'


def test_auth_host_and_origin_boundaries(local):
    client, _, _, _, calls = local
    assert client.get("/v1/models").status_code == 401
    assert client.get("/api/bootstrap", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/bootstrap", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/bootstrap", headers={**AUTH, "Host": "evil.example"}).status_code == 400
    assert client.get("/api/bootstrap").status_code == 401
    assert client.get("/").status_code == 404
    assert client.get("/assets/app.js").status_code == 404
    assert client.post("/api/scan").status_code == 401
    assert not calls


def test_official_model_probe_and_grok_responses(local):
    client, _, _, _, calls = local
    result = client.post("/api/providers/grok/probe", headers=AUTH)
    assert result.status_code == 200
    assert result.json()["models"][0]["source"] == "official_api"
    assert result.json()["models"][0]["availability"] == "listed"
    result = client.post("/v1/responses", headers=AUTH, json={"model": "grok/grok-official", "input": "hello"})
    assert result.status_code == 200, result.text
    assert result.json()["output"][0]["content"][0]["text"] == "官方回复"
    assert result.headers["X-Local-Provider"] == "grok"
    assert calls[-1].url.host == "cli-chat-proxy.grok.com"
    assert calls[-1].headers["Authorization"] == "Bearer official-secret"
    sent = json.loads(calls[-1].content)
    assert sent["model"] == "grok-official"
    assert sent["stream"] is True
    assert client.get("/v1/models", headers=AUTH).json()["data"][0]["availability"] == "call_verified"


def test_streaming_unicode(local):
    client, _, _, _, _ = local
    result = client.post("/v1/responses", headers=AUTH, json={"model": "grok/grok-official", "input": "hello", "stream": True})
    assert result.status_code == 200
    assert "text/event-stream" in result.headers["content-type"]
    assert "response.completed" in result.text and "官方回复" in result.text


def test_config_disables_provider_and_survives_restart(local):
    client, _, _, data, calls = local
    result = client.put("/api/config", headers=AUTH, json={"default_model": "", "enabled_providers": ["codex"]})
    assert result.status_code == 200
    saved = json.loads((data / "local-gateway.json").read_text())
    assert saved == {"default_model": "", "enabled_providers": ["codex"]}
    result = client.post("/v1/responses", headers=AUTH, json={"model": "grok/grok-official", "input": "hello"})
    assert result.status_code == 403 and not calls
    assert "secret" not in (data / "local-gateway.json").read_text()


def test_codex_history_auth_is_used_when_live_key_is_local(local):
    client, _, home, data, _ = local
    write(home / ".codex/auth.json", {"OPENAI_API_KEY": "mk-local-replaced"})
    (home / ".codex/config.toml").write_text('model_provider = "minkingapi"\nmodel = "gpt-reserve"\n', encoding="utf-8")
    newer = data / "local-profiles/codex/backups/20260923-120000"
    older = data / "profiles/codex/backups/20260922-090000"
    write(newer / "auth.json", {"OPENAI_API_KEY": "sk-ts-replaced"})
    write(older / "auth.json", {"tokens": {"access_token": "codex-from-history", "account_id": "account"}})
    result = client.post("/api/scan", headers=AUTH)
    account = next(item for item in result.json()["accounts"] if item["id"] == "codex")
    assert account["status"] != "missing"
    assert account["credential_source"] == "official_history"


def test_codex_custom_provider_key_is_not_official(local):
    client, _, home, _, _ = local
    write(home / ".codex/auth.json", {"OPENAI_API_KEY": "custom-secret"})
    (home / ".codex/config.toml").write_text('model_provider = "minkingapi"\nmodel = "gpt-reserve"\n', encoding="utf-8")
    result = client.post("/api/scan", headers=AUTH)
    account = next(a for a in result.json()["accounts"] if a["id"] == "codex")
    assert account["status"] == "missing" and account["models"] == []
    assert account["configured_model"] is None


def test_codex_official_snapshot_and_chat_conversion(local):
    client, _, _, data, calls = local
    write(data / "profiles/codex/official/auth.json", {"tokens": {"access_token": "codex-official", "account_id": "account"}})
    client.post("/api/scan", headers=AUTH)
    result = client.post("/v1/chat/completions", headers=AUTH, json={"model": "codex/gpt-official", "messages": [{"role": "user", "content": "hello"}]})
    assert result.status_code == 200, result.text
    assert result.json()["choices"][0]["message"]["content"] == "官方回复"
    assert calls[-1].url.host == "chatgpt.com"
    assert json.loads(calls[-1].content)["model"] == "gpt-official"


def test_no_fallback_on_upstream_redirect_or_auth_error(local):
    client, app, _, _, _ = local
    seen = []
    def redirect(request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "https://third-party.example/steal"})
    app.state.gateway.client._transport = httpx.MockTransport(redirect)
    result = client.post("/v1/responses", headers=AUTH, json={"model": "grok/grok-official", "input": "hello"})
    assert result.status_code == 502
    assert len(seen) == 1 and seen[0].url.host == "cli-chat-proxy.grok.com"


def test_invalid_model_and_unsupported_video_do_not_call_upstream(local):
    client, _, home, _, calls = local
    for model in ["grok/../../evil", "minking/grok-official", "grok/minking-default", "grok-official"]:
        assert client.post("/v1/responses", headers=AUTH, json={"model": model, "input": "hi"}).status_code == 422
    write(home / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "claude-secret"}})
    client.post("/api/scan", headers=AUTH)
    calls.clear()  # Synchronization probes accounts; unsupported generation must not call upstream.
    assert client.post("/v1/videos", headers=AUTH, json={"model": "claude_code/claude-official", "prompt": "hi"}).status_code == 501
    assert not calls


def test_request_size_and_json_validation(local):
    client, _, _, _, calls = local
    assert client.post("/v1/responses", headers=AUTH, content='[]').status_code == 422
    assert client.post("/v1/responses", headers=AUTH, content='not-json').status_code == 422
    assert client.post("/v1/responses", headers=AUTH, content=b'x' * (4 * 1024 * 1024 + 1)).status_code == 413
    assert not calls


def test_codex_terminal_with_empty_output_collects_item_done(local):
    client, app, _, data, _ = local
    write(data / "profiles/codex/official/auth.json", {"tokens": {"access_token": "codex-test"}})
    client.post("/api/scan", headers=AUTH)
    item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}
    frames = [{"type": "response.output_item.done", "output_index": 0, "item": item},
              {"type": "response.completed", "response": {"id": "test", "status": "completed", "output": []}}]
    def handler(request):
        assert request.headers["x-openai-internal-codex-responses-lite"] == "true"
        return httpx.Response(200, content="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in frames))
    app.state.gateway.client._transport = httpx.MockTransport(handler)
    result = client.post("/v1/responses", headers=AUTH, json={"model": "codex/gpt-5.6-luna", "input": "test"})
    assert result.status_code == 200
    assert result.json()["output"][0]["content"][0]["text"] == "OK"
    result = client.post("/v1/responses", headers=AUTH, json={"model": "codex/gpt-5.6-luna", "input": "test", "stream": True})
    completed = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')][-1]
    assert completed["response"]["output"] == [item]


def test_codex_image_uses_shared_server_payload_and_stream_parser(local):
    from app.providers.image_protocol import _image_responses_payload
    client, app, _, data, _ = local
    write(data / 'profiles/codex/official/auth.json', {'tokens': {'access_token': 'codex-test'}})
    client.post('/api/scan', headers=AUTH)
    expected, _ = _image_responses_payload('generation', json_body={'model':'gpt-image-2.5-flare','prompt':'square'}, files=None)
    def handler(request):
        if request.url.path.endswith('/images/generations'):
            return httpx.Response(404)
        assert json.loads(request.content) == expected
        assert 'x-openai-internal-codex-responses-lite' not in request.headers
        frames = [{'type':'response.image_generation_call.completed','result':'aW1hZ2U='},
                  {'type':'response.completed','response':{'output':[],'status':'completed'}}]
        return httpx.Response(200, content=''.join('data: '+json.dumps(frame)+'\n\n' for frame in frames))
    app.state.gateway.client._transport = httpx.MockTransport(handler)
    result=client.post('/v1/images/generations',headers=AUTH,json={'model':'codex/gpt-image-2.5-flare','prompt':'square'})
    assert result.status_code == 200
    assert result.json()['data'][0]['b64_json'] == 'aW1hZ2U='


def test_grok_custom_tools_are_restored_and_version_header_present(local):
    client, app, _, _, _ = local
    def handler(request):
        assert request.headers.get("x-grok-client-version")
        body = json.loads(request.content)
        assert body["tools"][0]["type"] == "function"
        output = [{"type": "function_call", "name": "patch", "call_id": "c1", "arguments": json.dumps({"input": "test patch"})}]
        return httpx.Response(200, content=response_events(output=output))
    app.state.gateway.client._transport = httpx.MockTransport(handler)
    result = client.post("/v1/responses", headers=AUTH, json={"model":"grok/grok-official", "input":"test", "tools":[{"type":"custom","name":"patch","description":"edit"}]})
    assert result.status_code == 200, result.text
    assert result.json()["output"][0]["type"] == "custom_tool_call"
    assert result.json()["output"][0]["input"] == "test patch"


def test_claude_responses_text_and_tool_stream_conversion(local):
    client, app, home, _, _ = local
    write(home / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken":"claude-test"}})
    client.post("/api/scan", headers=AUTH)
    frames = [{"type":"message_start","message":{"usage":{"input_tokens":3}}},
              {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}},
              {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}},
              {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tool1","name":"lookup","input":{}}},
              {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":'{"q":"hi"}'}},
              {"type":"message_delta","usage":{"output_tokens":4}}, {"type":"message_stop"}]
    def handler(request):
        assert request.url.host == "api.anthropic.com"
        body = json.loads(request.content)
        assert body["model"] == "claude-official" and body["messages"][0]["content"][0]["text"] == "hello"
        return httpx.Response(200, content="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in frames))
    app.state.gateway.client._transport=httpx.MockTransport(handler)
    result=client.post('/v1/responses',headers=AUTH,json={"model":"claude_code/claude-official","input":"hello"})
    assert result.status_code==200,result.text
    assert result.json()["output"][0]["content"][0]["text"]=="你好"
    assert result.json()["output"][1]["name"]=="lookup"
    assert result.json()["usage"]["total_tokens"]==7


@pytest.mark.parametrize("content", [b'data: {"error":{"message":"secret upstream detail"}}\n\n', b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'])
def test_workbuddy_failed_or_truncated_stream_is_not_success(local, content):
    client, app, _, _, _ = local
    path = app.state.gateway.localappdata / 'CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info'
    write(path,{"auth":{"accessToken":"workbuddy-test"},"account":{"uid":"test"}})
    client.post('/api/scan',headers=AUTH)
    app.state.gateway.client._transport=httpx.MockTransport(lambda request:httpx.Response(200,content=content))
    result=client.post('/v1/responses',headers=AUTH,json={"model":"workbuddy/glm-5.2","input":"test"})
    assert result.status_code==502
    assert 'secret upstream detail' not in result.text


def test_local_service_lifecycle_and_native_bridge(tmp_path, monkeypatch):
    import socket
    from minking_desktop.local_desktop import LocalService, DesktopBridge
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    monkeypatch.setattr(ag,'windows_credential_payloads',lambda:[])
    service=LocalService(home=tmp_path/'home',appdata=tmp_path/'data',api_key=KEY)
    with socket.socket() as available:
        available.bind(('127.0.0.1',0));port=available.getsockname()[1]
    service.port=port
    bridge=DesktopBridge(service)
    try:
        service.start()
        state=bridge.state()
        assert state['service_running'] and state['base_url']==f'http://127.0.0.1:{port}/v1'
        assert bridge.request('https://evil.example',None,'GET')['ok'] is False
        with httpx.Client(trust_env=False) as client:
            assert client.get(f'http://127.0.0.1:{port}/').status_code==404
            assert client.get(f'http://127.0.0.1:{port}/api/bootstrap').status_code==401
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',0))
            result=bridge.control('port',occupied.getsockname()[1])
            assert not result['ok'] and service.port==port and service.running
        assert bridge.control('stop')['ok']
        assert not service.running
        assert not bridge.request('/v1/models',None,'GET')['ok']
        assert bridge.control('start')['ok']
    finally:
        service.stop()
