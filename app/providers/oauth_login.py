from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlsplit

OAUTH_PROVIDERS = ("codex", "grok", "antigravity", "workbuddy")
LOGIN_NAMES = {
    "codex": "Codex",
    "grok": "Grok",
    "antigravity": "Google",
    "workbuddy": "WorkBuddy",
}


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def jwt_claims(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    padding = "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def parse_oauth_callback(value: str) -> dict[str, str]:
    text = (value or "").strip()
    if not text:
        return {}
    if "=" not in text and "?" not in text:
        return {"code": text}
    if "://" not in text:
        if text.startswith("?"):
            text = "http://127.0.0.1/" + text
        elif text.startswith("/"):
            text = "http://127.0.0.1" + text
        elif text.lower().startswith(("localhost", "127.0.0.1")):
            text = "http://" + text
        else:
            text = "http://127.0.0.1/callback?" + text
    split = urlsplit(text)
    query = split.query or split.fragment
    params = dict(parse_qsl(query, keep_blank_values=False))
    parsed: dict[str, str] = {}
    for key in ("code", "state", "error"):
        item = params.get(key)
        if isinstance(item, str) and item.strip():
            parsed[key] = item.strip()
    return parsed
