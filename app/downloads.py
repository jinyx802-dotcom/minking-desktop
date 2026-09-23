"""Official MinKing AI Windows package files under data/downloads/."""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.codex_gateway import GatewayError
from app.config import settings

EXE_NAME = "MinKingAI.exe"
SHA256_NAME = "MinKingAI.exe.sha256"
ALLOWED = frozenset({EXE_NAME, SHA256_NAME})
MAX_EXE_BYTES = 200 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")


def downloads_dir() -> Path:
    path = settings.data_dir / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_download(filename: str) -> Path | None:
    if filename not in ALLOWED:
        return None
    folder = downloads_dir().resolve()
    path = (folder / filename).resolve()
    if path.parent != folder or path.is_symlink() or not path.is_file():
        return None
    return path


def package_available() -> bool:
    return resolve_download(EXE_NAME) is not None


def desktop_package_status() -> dict[str, object]:
    path = resolve_download(EXE_NAME)
    if path is None:
        return {"available": False, "name": EXE_NAME, "bytes": 0, "sha256": ""}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "available": True,
        "name": EXE_NAME,
        "bytes": path.stat().st_size,
        "sha256": digest,
    }


def install_desktop_exe(blob: bytes) -> dict[str, object]:
    if not blob.startswith(b"MZ"):
        raise GatewayError(400, "请上传 Windows 安装包", code="invalid_desktop_exe")
    if len(blob) > MAX_EXE_BYTES:
        raise GatewayError(400, "安装包过大", code="invalid_desktop_exe")
    digest = hashlib.sha256(blob).hexdigest()
    folder = downloads_dir()
    temporary = folder / ".MinKingAI.exe.uploading"
    temporary.write_bytes(blob)
    temporary.replace(folder / EXE_NAME)
    (folder / SHA256_NAME).write_text(f"{digest}  {EXE_NAME}\n", encoding="utf-8")
    return desktop_package_status()


def sha256_digest() -> str:
    path = resolve_download(SHA256_NAME)
    if path is None:
        return ""
    try:
        token = path.read_text(encoding="utf-8", errors="replace")[:256].strip().split()[0]
    except OSError:
        return ""
    digest = token.lower()
    if len(digest) in {64, 128} and set(digest) <= _HEX:
        return digest
    return ""
