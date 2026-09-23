"""Filesystem locations for the desktop client. Snapshots stay on this machine."""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_PUBLIC_V1 = "https://ceshi.007ka.cn/maliang/v1"
APP_FOLDER = "MinKing"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def user_home(home: Path | None = None) -> Path:
    return Path(home) if home is not None else Path.home()


def appdata_root(*, appdata: Path | None = None) -> Path:
    if appdata is not None:
        return Path(appdata)
    raw = os.environ.get("APPDATA")
    if raw:
        return Path(raw) / APP_FOLDER
    if _is_macos():
        return user_home() / "Library" / "Application Support" / APP_FOLDER
    return user_home() / "AppData" / "Roaming" / APP_FOLDER


def localappdata_root(*, home: Path | None = None, localappdata: Path | None = None) -> Path:
    if localappdata is not None:
        return Path(localappdata)
    raw = os.environ.get("LOCALAPPDATA")
    if raw:
        return Path(raw)
    base = user_home(home)
    if _is_macos():
        return base / "Library" / "Application Support"
    return base / "AppData" / "Local"


def workbuddy_credential_files(*, home: Path, localappdata: Path) -> list[Path]:
    relative = Path("CodeBuddyExtension") / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
    files = [Path(localappdata) / relative]
    mac = Path(home) / "Library" / "Application Support" / relative
    extra = Path(home) / ".workbuddy" / "workbuddy-desktop.info"
    for path in (mac, extra):
        if path not in files:
            files.append(path)
    return files


def profile_root(*, appdata: Path | None = None) -> Path:
    return appdata_root(appdata=appdata) / "profiles"


def settings_path(*, appdata: Path | None = None) -> Path:
    return appdata_root(appdata=appdata) / "settings.json"


def token_path(*, appdata: Path | None = None) -> Path:
    return appdata_root(appdata=appdata) / "token.dpapi"


def instance_path(*, appdata: Path | None = None) -> Path:
    return appdata_root(appdata=appdata) / "instance.json"


def expand_user_path(value: str, *, home: Path) -> Path:
    replaced = value.replace("%USERPROFILE%", str(home))
    replaced = replaced.replace("%LOCALAPPDATA%", str(home / "AppData" / "Local"))
    replaced = replaced.replace("%APPDATA%", str(home / "AppData" / "Roaming"))
    replaced = replaced.replace("$HOME", str(home))
    return Path(os.path.expandvars(replaced))


def public_v1_url(base: str | None) -> str:
    clean = (base or "").strip().rstrip("/")
    if not clean:
        return DEFAULT_PUBLIC_V1
    return clean if clean.endswith("/v1") else f"{clean}/v1"


def public_root_url(base: str | None) -> str:
    endpoint = public_v1_url(base)
    return endpoint[:-3] if endpoint.endswith("/v1") else endpoint
