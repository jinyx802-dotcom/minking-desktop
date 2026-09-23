from __future__ import annotations

import json
from pathlib import Path

from app.providers.antigravity import (
    AntigravityAdapter,
    DEFAULT_HOST,
    DEFAULT_MODEL,
    IMAGE_MODEL,
    LOAD_CODE_ASSIST_METADATA,
    USER_AGENT,
    WIRE_MODELS,
    build_image_cloudcode_envelope,
    clean_json_schema,
    extract_image_b64_from_cloudcode,
    google_error_status,
    is_google_capacity_error,
    parse_oauth_payload,
    responses_to_cloudcode,
    wire_model,
)
from app.providers.registry import get_adapter

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity"
PUBLIC_SLUGS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.1-pro",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
)
ALIAS_SLUGS = (
    "gemini-3-flash",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "claude-opus-4-6",
    "claude-sonnet-4-6-thinking",
)
ALIAS_TARGETS = {
    "gemini-3-flash": DEFAULT_MODEL,
    "gemini-3.5-flash": DEFAULT_MODEL,
    "gemini-3.6-flash": DEFAULT_MODEL,
    "claude-opus-4-6": "claude-opus-4-6-thinking",
    "claude-sonnet-4-6-thinking": "claude-sonnet-4-6",
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_antigravity_adapter_is_ready_with_verified_catalog():
    adapter = get_adapter("antigravity")
    assert isinstance(adapter, AntigravityAdapter)
    assert adapter.ready is True
    catalog = {item.id: item for item in adapter.catalog()}
    for slug in PUBLIC_SLUGS:
        assert slug in catalog
        assert catalog[slug].alias_of is None
        assert catalog[slug].type == "text"
    assert catalog[DEFAULT_MODEL].default is True
    assert catalog["claude-sonnet-4-6"].owned_by == "anthropic"
    assert catalog["claude-opus-4-6-thinking"].owned_by == "anthropic"
    for slug in ALIAS_SLUGS:
        assert catalog[slug].alias_of == ALIAS_TARGETS[slug]
    assert catalog["gemini-3.1-flash-image"].type == "image"
    assert catalog["gemini-3.1-flash-image"].alias_of is None
    assert "gpt-oss-120b-medium" not in catalog
    assert "claude-sonnet-4-6-thinking" in catalog
    assert catalog["claude-sonnet-4-6-thinking"].alias_of == "claude-sonnet-4-6"
    assert adapter.default_host == DEFAULT_HOST
    assert DEFAULT_HOST.startswith("https://daily-cloudcode-pa.googleapis.com")
    assert "cloudcode-pa.googleapis.com" not in DEFAULT_HOST.replace("daily-cloudcode-pa.googleapis.com", "")
    assert "aidev_client" in USER_AGENT
    assert "auth_method=oauth" in USER_AGENT
    assert USER_AGENT.startswith("antigravity/ide/2.12.2")
    assert WIRE_MODELS["gemini-3.8-flash"] == "gemini-3.8-flash-tiered"
    assert WIRE_MODELS[IMAGE_MODEL] == IMAGE_MODEL
    assert wire_model("gemini-3.8-flash") == "gemini-3.8-flash-tiered"
    assert wire_model(IMAGE_MODEL) == IMAGE_MODEL
    assert wire_model("gemini-3-flash") == "gemini-3.8-flash-tiered"
    assert wire_model("gemini-3.8-flash-medium") == "gemini-3.8-flash-tiered"
    assert wire_model("gemini-3.1-pro", high_effort=True) == "gemini-pro-agent"
    assert LOAD_CODE_ASSIST_METADATA["platform"] == "PLATFORM_UNSPECIFIED"
    assert LOAD_CODE_ASSIST_METADATA["ideType"] == "ANTIGRAVITY"


def test_google_error_status_unwraps_cloudcode_array():
    body = [
        {
            "error": {
                "code": 400,
                "message": "User location is not supported for the API use.",
                "status": "FAILED_PRECONDITION",
            }
        }
    ]
    assert google_error_status(body) == "FAILED_PRECONDITION"
    from app.codex_gateway import CodexGateway

    denied = CodexGateway._antigravity_upstream_error(
        400,
        body,
        huge_system=False,
        account_id="antigravity:example",
        model="gemini-3.8-flash",
    )
    assert denied.status == 400
    assert denied.message == "User location is not supported for the API use."
    other = CodexGateway._antigravity_upstream_error(
        400,
        [{"error": {"code": 400, "message": "Request contains an invalid argument.", "status": "INVALID_ARGUMENT"}}],
        huge_system=False,
        account_id="antigravity:example",
        model="gemini-3.8-flash",
    )
    assert other.message == "Antigravity rejected the request"
    capacity = [
        {
            "error": {
                "code": 503,
                "message": "No capacity available for model claude-opus-4-6-thinking on the server",
            }
        }
    ]
    assert is_google_capacity_error(503, capacity) is True
    assert is_google_capacity_error(200, capacity) is True
    assert is_google_capacity_error(400, body) is False


def test_parse_oauth_example_keeps_camel_case_and_dummy_secrets():
    account = _load("oauth_account.example.json")
    imported = parse_oauth_payload(account, filename="oauth_account.example.json")
    assert len(imported) == 1
    assert imported[0].account_id == "antigravity:user@example.com"
    assert imported[0].label == "user@example.com"
    assert imported[0].payload["accessToken"].startswith("ya29.example")
    assert imported[0].payload["refreshToken"].startswith("1//0example")
    assert imported[0].payload["projectId"] == "example-cloud-project"
    assert "access_token" not in imported[0].payload
    assert imported[0].payload["expiresAt"] == 4102444800


def test_parse_oauth_accepts_windows_credman_nested_token():
    import base64

    def _segment(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    imported = parse_oauth_payload(
        {
            "auth_method": "consent",
            "id_token": f"{_segment({'alg': 'none'})}.{_segment({'email': 'user@example.com'})}.x",
            "token": {
                "access_token": "ya29.example-access-token",
                "refresh_token": "1//0example-refresh-token",
                "expiry": "2099-01-01T00:00:00Z",
                "token_type": "Bearer",
            },
        },
        filename="gemini:antigravity",
    )
    assert imported[0].account_id == "antigravity:user@example.com"
    assert imported[0].payload["accessToken"] == "ya29.example-access-token"
    assert imported[0].payload["refreshToken"] == "1//0example-refresh-token"
    assert "id_token" not in imported[0].payload


def test_parse_oauth_accepts_snake_case_and_omits_missing_project():
    imported = parse_oauth_payload(
        {
            "email": "user@example.com",
            "access_token": "ya29.example-access-token",
            "refresh_token": "1//0example-refresh-token",
            "expires_at": 4102444800,
            "client_id": "example-client-id.apps.googleusercontent.com",
        },
        filename="snake.json",
    )
    assert imported[0].payload["accessToken"] == "ya29.example-access-token"
    assert imported[0].payload["refreshToken"] == "1//0example-refresh-token"
    assert imported[0].payload["expiresAt"] == 4102444800
    assert "projectId" not in imported[0].payload
    assert "access_token" not in imported[0].payload


def test_oauth_example_has_import_fields_and_dummy_secrets():
    account = _load("oauth_account.example.json")
    for key in ("email", "accessToken", "refreshToken", "expiresAt", "projectId", "clientId", "scopes"):
        assert key in account
    assert account["accessToken"].startswith("ya29.example")
    assert account["refreshToken"].startswith("1//0example")
    assert "cloud-platform" in " ".join(account["scopes"])
    assert account["projectId"] == "example-cloud-project"


def test_text_envelope_puts_instructions_in_system_not_contents():
    responses = _load("responses_text_request.json")
    envelope = responses_to_cloudcode(responses, project="example-cloud-project")
    fixture = _load("cloudcode_text_envelope.json")
    request = envelope["request"]
    system_text = request["systemInstruction"]["parts"][0]["text"]
    assert responses["instructions"] in system_text
    roles = [item["role"] for item in request["contents"]]
    assert set(roles) <= {"user", "model"}
    assert "system" not in roles
    assert "safetySettings" not in request
    assert "safety_settings" not in request
    assert "safetySettings" not in envelope
    assert envelope["model"] == "gemini-3.8-flash-tiered"
    assert fixture["model"] == "gemini-3.8-flash-tiered"
    assert request["generationConfig"]["thinkingConfig"]["thinkingLevel"] in {"low", "medium", "high"}
    assert "thinkingBudget" not in json.dumps(request)


def test_function_envelope_keeps_apply_patch_and_drops_hosted_tools():
    responses = _load("responses_tools_request.json")
    expected = _load("cloudcode_function_envelope.json")
    ignored = _load("hosted_tools_ignored.json")
    envelope = responses_to_cloudcode(responses, project="example-cloud-project")
    inbound_types = {tool.get("type") for tool in responses["tools"]}
    assert {"image_generation", "web_search", "computer_use"} <= inbound_types
    declarations = envelope["request"]["tools"][0]["functionDeclarations"]
    names = {item["name"] for item in declarations}
    assert names == {"apply_patch"}
    for hosted in ignored["drop_from_tools"]:
        assert hosted not in names
        assert hosted not in json.dumps(envelope["request"].get("tools"))
    contents = envelope["request"]["contents"]
    call = contents[1]["parts"][0]["functionCall"]
    result = contents[2]["parts"][0]["functionResponse"]
    assert call["name"] == "apply_patch"
    assert call["args"] == expected["request"]["contents"][1]["parts"][0]["functionCall"]["args"]
    assert result["name"] == "apply_patch"
    assert result["name"] != "call_apply_1"
    assert isinstance(result["response"], dict)
    assert result["response"]
    assert result["response"] == expected["request"]["contents"][2]["parts"][0]["functionResponse"]["response"]
    params = declarations[0]["parameters"]
    assert params["type"] == "object"
    assert isinstance(params["type"], str)
    assert envelope["model"] == "gemini-3.8-flash-tiered"
    system_text = envelope["request"]["systemInstruction"]["parts"][0]["text"]
    for hosted in ignored["do_not_promise_in_system"]:
        assert hosted not in system_text


def test_schema_type_fixture_rejects_non_string_type():
    payload = _load("schema_type_must_be_string.json")
    assert not isinstance(payload["invalid_parameters"]["type"], str)
    cleaned = clean_json_schema(payload["invalid_parameters"])
    assert cleaned == payload["cleaned_parameters"]
    assert cleaned["type"] == "object"
    assert cleaned["properties"]["path"]["type"] == "string"
    assert isinstance(cleaned["type"], str)
    assert isinstance(cleaned["properties"]["path"]["type"], str)


def test_stream_chunk_unwraps_response_envelope():
    chunk = _load("cloudcode_stream_text_chunk.json")
    inner = chunk["response"]
    text = inner["candidates"][0]["content"]["parts"][0]["text"]
    assert text == "ok"
    assert inner["candidates"][0]["content"]["role"] == "model"


def test_image_cloudcode_envelope_and_extract():
    envelope = build_image_cloudcode_envelope(
        project="example-cloud-project",
        prompt="draw a cat",
        images=["data:image/png;base64,aaaa"],
        model=IMAGE_MODEL,
    )
    assert envelope["model"] == IMAGE_MODEL
    assert envelope["project"] == "example-cloud-project"
    parts = envelope["request"]["contents"][0]["parts"]
    assert parts[0] == {"text": "draw a cat"}
    assert parts[1]["inlineData"]["data"] == "aaaa"
    assert envelope["request"]["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    encoded = extract_image_b64_from_cloudcode(
        {
            "response": {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"inlineData": {"mimeType": "image/png", "data": "bbbb"}}]
                        }
                    }
                ]
            }
        }
    )
    assert encoded == "bbbb"
