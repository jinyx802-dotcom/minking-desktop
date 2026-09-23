from __future__ import annotations

import json

from minking_desktop.secrets import SecretStore


def test_token_not_written_plaintext(tmp_path):
    store = SecretStore(
        appdata=tmp_path / "MinKing",
        protect=lambda data: b"ENC" + data[::-1],
        unprotect=lambda data: data[3:][::-1],
    )
    store.save_token("desktop-token-value")
    store.save_settings({"public_base_url": "https://portal.example/v1", "email": "ada@example.com", "token": "nope", "key": "sk-ts-no"})
    settings = json.loads((tmp_path / "MinKing" / "settings.json").read_text(encoding="utf-8"))
    assert "token" not in settings
    assert "key" not in settings
    blob = (tmp_path / "MinKing" / "token.dpapi").read_bytes()
    assert b"desktop-token-value" not in blob
    assert store.load_token() == "desktop-token-value"
    store.clear_token()
    assert store.load_token() is None


def test_dpapi_roundtrip_on_windows(tmp_path):
    import os

    if os.name != "nt":
        return
    store = SecretStore(appdata=tmp_path / "MinKing")
    store.save_token("desktop-live-token")
    raw = (tmp_path / "MinKing" / "token.dpapi").read_bytes()
    assert b"desktop-live-token" not in raw
    assert store.load_token() == "desktop-live-token"


def test_macos_keychain_roundtrip(monkeypatch, tmp_path):
    vault: dict[str, bytes] = {}
    monkeypatch.setattr("minking_desktop.secrets._is_windows", lambda: False)
    monkeypatch.setattr("minking_desktop.secrets._is_macos", lambda: True)
    monkeypatch.setattr("minking_desktop.secrets._keychain_set", vault.__setitem__)
    monkeypatch.setattr("minking_desktop.secrets._keychain_get", vault.__getitem__)
    monkeypatch.setattr("minking_desktop.secrets._keychain_delete", lambda account: vault.pop(account, None))

    store = SecretStore(appdata=tmp_path / "MinKing")
    store.save_token("desktop-mac-token")
    blob = (tmp_path / "MinKing" / "token.dpapi").read_bytes()
    assert b"desktop-mac-token" not in blob
    assert blob.startswith(b"keychain:")
    assert store.load_token() == "desktop-mac-token"
    store.clear_token()
    assert store.load_token() is None
    assert vault == {}


def test_local_key_persists_on_macos(monkeypatch, tmp_path):
    vault: dict[str, bytes] = {}
    monkeypatch.setattr("minking_desktop.secrets._is_windows", lambda: False)
    monkeypatch.setattr("minking_desktop.secrets._is_macos", lambda: True)
    monkeypatch.setattr("minking_desktop.secrets._keychain_set", vault.__setitem__)
    monkeypatch.setattr("minking_desktop.secrets._keychain_get", vault.__getitem__)
    monkeypatch.setattr("minking_desktop.secrets._keychain_delete", lambda account: vault.pop(account, None))
    from minking_desktop.local_api import local_key

    root = tmp_path / "MinKing"
    first = local_key(root)
    second = local_key(root)
    assert first == second
    assert first.startswith("mk-local-")
    assert b"mk-local-" not in (root / "local-api-key.dpapi").read_bytes()


def test_refuses_api_key_as_token(tmp_path):
    store = SecretStore(
        appdata=tmp_path / "MinKing",
        protect=lambda data: data,
        unprotect=lambda data: data,
    )
    try:
        store.save_token("sk-ts-this-is-a-key")
    except Exception as exc:
        assert "refusing" in str(exc)
    else:
        raise AssertionError("should refuse API keys")
