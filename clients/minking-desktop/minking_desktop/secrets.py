"""Protected desktop token. Never write the raw token or API key as plaintext."""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from minking_desktop.paths import appdata_root, settings_path, token_path

ProtectFn = Callable[[bytes], bytes]
UnprotectFn = Callable[[bytes], bytes]


class SecretError(RuntimeError):
    pass


def _windows_protect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "MinKing",
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise SecretError("DPAPI protect failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _windows_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise SecretError("DPAPI unprotect failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


_KEYCHAIN_PREFIX = b"keychain:"
_KEYCHAIN_SERVICE = "cn.minking.desktop"


def _security(*args: str) -> bytes:
    completed = subprocess.run(
        ["security", *args],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SecretError("Keychain operation failed")
    return completed.stdout


def _keychain_set(account: str, data: bytes) -> None:
    payload = base64.b64encode(data).decode("ascii")
    _security(
        "add-generic-password",
        "-s",
        _KEYCHAIN_SERVICE,
        "-a",
        account,
        "-w",
        payload,
        "-U",
    )


def _keychain_get(account: str) -> bytes:
    raw = _security(
        "find-generic-password",
        "-s",
        _KEYCHAIN_SERVICE,
        "-a",
        account,
        "-w",
    ).strip()
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise SecretError("Keychain payload is unreadable") from exc


def _keychain_delete(account: str) -> None:
    try:
        _security(
            "delete-generic-password",
            "-s",
            _KEYCHAIN_SERVICE,
            "-a",
            account,
        )
    except SecretError:
        return


def _macos_protect(data: bytes) -> bytes:
    account = secrets.token_hex(16)
    _keychain_set(account, data)
    return _KEYCHAIN_PREFIX + account.encode("ascii")


def _macos_unprotect(data: bytes) -> bytes:
    if not data.startswith(_KEYCHAIN_PREFIX):
        raise SecretError("Keychain payload is unreadable")
    account = data[len(_KEYCHAIN_PREFIX) :].decode("ascii")
    return _keychain_get(account)


def _is_windows() -> bool:
    return os.name == "nt"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def forget_protected(data: bytes) -> None:
    if not _is_macos() or not data.startswith(_KEYCHAIN_PREFIX):
        return
    try:
        account = data[len(_KEYCHAIN_PREFIX) :].decode("ascii")
    except UnicodeDecodeError:
        return
    if account:
        _keychain_delete(account)


def default_protect(data: bytes) -> bytes:
    if _is_windows():
        return _windows_protect(data)
    if _is_macos():
        return _macos_protect(data)
    raise SecretError("secret protection is unavailable on this platform")


def default_unprotect(data: bytes) -> bytes:
    if _is_windows():
        return _windows_unprotect(data)
    if _is_macos():
        return _macos_unprotect(data)
    raise SecretError("secret protection is unavailable on this platform")


class SecretStore:
    def __init__(
        self,
        *,
        appdata: Path | None = None,
        protect: ProtectFn | None = None,
        unprotect: UnprotectFn | None = None,
    ) -> None:
        self.appdata = appdata_root(appdata=appdata)
        self.protect = protect or default_protect
        self.unprotect = unprotect or default_unprotect

    def load_settings(self) -> dict[str, Any]:
        path = settings_path(appdata=self.appdata)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def save_settings(self, payload: dict[str, Any]) -> None:
        path = settings_path(appdata=self.appdata)
        path.parent.mkdir(parents=True, exist_ok=True)
        clean = {key: value for key, value in payload.items() if key not in {"token", "key", "api_key"}}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def save_token(self, token: str) -> None:
        if not token or token.lower().startswith("sk-"):
            raise SecretError("refusing to store an API key as a desktop token")
        path = token_path(appdata=self.appdata)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            try:
                forget_protected(path.read_bytes())
            except OSError:
                pass
        blob = self.protect(token.encode("utf-8"))
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(blob)
        tmp.replace(path)

    def load_token(self) -> str | None:
        path = token_path(appdata=self.appdata)
        if not path.is_file():
            return None
        try:
            token = self.unprotect(path.read_bytes()).decode("utf-8")
        except (SecretError, OSError, UnicodeDecodeError):
            return None
        return token or None

    def clear_token(self) -> None:
        path = token_path(appdata=self.appdata)
        if path.exists():
            try:
                forget_protected(path.read_bytes())
            except OSError:
                pass
            path.unlink()
