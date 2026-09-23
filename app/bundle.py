"""Build an isolated Codex configuration bundle for a single gateway key."""
from __future__ import annotations

import io
import json
import zipfile

from app.codex_gateway import GatewayError, codex_gateway
from app.config import settings
from app.providers.codex_catalog import WINDOWS_CODEX_CATALOG_PLACEHOLDER
from app.store.gateway import gateway_store


async def codex_bundle(key_id: str, *, owner_user_id: str | None = None) -> bytes:
    if not settings.gateway_public_base_url.startswith("https://"):
        raise GatewayError(503, "HTTPS public URL is not configured", code="bundle_https_required")
    row = await gateway_store.one(
        "SELECT id,key_ciphertext,status,owner_user_id FROM api_keys WHERE id=?", (key_id,)
    )
    if not row or row["status"] == "deleted" or (owner_user_id is not None and row["owner_user_id"] != owner_user_id):
        raise GatewayError(404, "API key not found", code="api_key_not_found")
    if row["status"] != "active":
        raise GatewayError(409, "API key is not active", code="api_key_inactive")
    raw = codex_gateway._decrypt_api_key(row["key_ciphertext"])
    if raw is None:
        raise GatewayError(409, "API key cannot be recovered; rotate it first", code="key_not_recoverable")
    try:
        catalog = (await codex_gateway.models(client_version="bundle"))["models"]
    except GatewayError as exc:
        if exc.code == "no_healthy_accounts":
            raise GatewayError(503, "No text model is currently available", code="no_text_models") from exc
        raise
    if not catalog:
        raise GatewayError(503, "No text model is currently available", code="no_text_models")
    model = catalog[0]["slug"]
    clean_base = settings.gateway_public_base_url.rstrip("/")
    endpoint_v1 = clean_base if clean_base.endswith("/v1") else f"{clean_base}/v1"
    quoted_model = json.dumps(model, ensure_ascii=False)
    quoted_base = json.dumps(endpoint_v1)
    quoted_catalog = json.dumps(WINDOWS_CODEX_CATALOG_PLACEHOLDER, ensure_ascii=False)
    config = (
        "# Codex CLI -> MinKing API Composite group\n"
        'model_provider = "minkingapi"\n'
        f"model = {quoted_model}\n"
        f"review_model = {quoted_model}\n"
        "disable_response_storage = true\n"
        "# Windows 必须写绝对路径。把「你的用户名」改成 C:/Users/ 后面那一段，例如 DELL。\n"
        f"model_catalog_json = {quoted_catalog}\n"
        "\n"
        "[model_providers.minkingapi]\n"
        'name = "MinKing API Composite"\n'
        f"base_url = {quoted_base}\n"
        'env_key = "OPENAI_API_KEY"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
        "supports_websockets = false\n"
        "\n"
        "[windows]\n"
        'sandbox = "unelevated"\n'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.toml", config)
        archive.writestr("codex-models.json", json.dumps({"models": catalog}, ensure_ascii=False, indent=2))
        archive.writestr(
            ".env",
            f"OPENAI_API_KEY={raw}\nOPENAI_BASE_URL={endpoint_v1}\n",
        )
        archive.writestr("auth.json", json.dumps({"OPENAI_API_KEY": raw}, ensure_ascii=False))
    return buffer.getvalue()
