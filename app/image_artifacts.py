from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import datetime as dt
import hashlib
import hmac
import io
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import UploadFile

from app.config import settings
from app.store.gateway import gateway_store, iso_now, utc_now


class ArtifactError(Exception):
    def __init__(self, status: int, message: str, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def _detected_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _timestamp(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value)
    return int(parsed.timestamp())


class ImageArtifactStore:
    def __init__(self) -> None:
        self._secret = b""
        self._cleanup_task: asyncio.Task[None] | None = None

    async def start(self, secret: bytes) -> None:
        self._secret = secret
        settings.gateway_image_artifact_dir()
        await self.prune()
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="image-artifact-cleanup"
        )

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(max(60, settings.gateway_image_cleanup_seconds))
            await self.prune()

    def _path(self, artifact_id: str, mime_type: str) -> Path:
        extension = _MIME_EXTENSIONS.get(mime_type, ".bin")
        return settings.gateway_image_artifact_dir() / f"{artifact_id}{extension}"

    @staticmethod
    def file_object(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "object": "file",
            "bytes": int(row["byte_size"]),
            "created_at": _timestamp(str(row["created_at"])),
            "filename": str(row["filename"]),
            "purpose": str(row["purpose"]),
            "status": "processed",
            "expires_at": _timestamp(str(row["expires_at"])),
        }

    async def create_upload(
        self, uploaded: UploadFile, *, owner_key_id: str, purpose: str
    ) -> dict[str, Any]:
        if purpose not in {"user_data", "vision"}:
            raise ArtifactError(400, "purpose must be 'user_data' or 'vision'", "invalid_purpose")
        chunks: list[bytes] = []
        size = 0
        try:
            while True:
                chunk = await uploaded.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.gateway_image_artifact_max_file_bytes:
                    raise ArtifactError(413, "Image file is too large", "file_too_large")
                chunks.append(chunk)
        finally:
            await uploaded.close()
        return await self.create_bytes(
            b"".join(chunks),
            owner_key_id=owner_key_id,
            purpose=purpose,
            filename=uploaded.filename or "image",
            ttl=dt.timedelta(hours=max(1, settings.gateway_file_input_ttl_hours)),
        )

    async def create_bytes(
        self,
        raw: bytes,
        *,
        owner_key_id: str,
        purpose: str,
        filename: str,
        ttl: dt.timedelta,
        mime_hint: str | None = None,
    ) -> dict[str, Any]:
        if not raw:
            raise ArtifactError(400, "Image file is empty", "invalid_image")
        if len(raw) > settings.gateway_image_artifact_max_file_bytes:
            raise ArtifactError(413, "Image file is too large", "file_too_large")
        mime_type = _detected_mime(raw)
        if mime_type is None:
            raise ArtifactError(400, "Only PNG, JPEG, and WEBP images are supported", "invalid_image")
        if mime_hint and mime_hint.startswith("image/") and mime_hint not in _MIME_EXTENSIONS:
            raise ArtifactError(400, "Unsupported image content type", "invalid_image")

        artifact_id = f"file_{secrets.token_urlsafe(24)}"
        destination = self._path(artifact_id, mime_type)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(raw)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, destination)

        now = utc_now()
        expires = now + ttl
        safe_name = Path(filename).name[:255] or f"image{_MIME_EXTENSIONS[mime_type]}"
        row = {
            "id": artifact_id,
            "owner_key_id": owner_key_id,
            "purpose": purpose,
            "filename": safe_name,
            "mime_type": mime_type,
            "byte_size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
        }
        try:
            await gateway_store.execute(
                """
                INSERT INTO file_artifacts(
                    id,owner_key_id,purpose,filename,mime_type,byte_size,sha256,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                tuple(row[field] for field in (
                    "id", "owner_key_id", "purpose", "filename", "mime_type",
                    "byte_size", "sha256", "created_at", "expires_at"
                )),
            )
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return self.file_object(row)

    async def list(self, owner_key_id: str) -> list[dict[str, Any]]:
        rows = await gateway_store.all(
            "SELECT * FROM file_artifacts WHERE owner_key_id=? AND expires_at>? ORDER BY created_at DESC",
            (owner_key_id, iso_now()),
        )
        return [self.file_object(row) for row in rows]

    async def get(self, artifact_id: str, owner_key_id: str | None = None) -> dict[str, Any]:
        params: tuple[Any, ...]
        sql = "SELECT * FROM file_artifacts WHERE id=? AND expires_at>?"
        params = (artifact_id, iso_now())
        if owner_key_id is not None:
            sql += " AND owner_key_id=?"
            params += (owner_key_id,)
        row = await gateway_store.one(sql, params)
        if row is None:
            raise ArtifactError(404, "File not found", "file_not_found")
        return row

    async def delete(self, artifact_id: str, owner_key_id: str) -> dict[str, Any]:
        row = await self.get(artifact_id, owner_key_id)
        await gateway_store.execute(
            "DELETE FROM file_artifacts WHERE id=? AND owner_key_id=?",
            (artifact_id, owner_key_id),
        )
        self._path(artifact_id, str(row["mime_type"])).unlink(missing_ok=True)
        return {"id": artifact_id, "object": "file", "deleted": True}

    async def content(self, artifact_id: str, owner_key_id: str | None = None) -> tuple[Path, str]:
        row = await self.get(artifact_id, owner_key_id)
        path = self._path(artifact_id, str(row["mime_type"]))
        if not path.is_file():
            await gateway_store.execute("DELETE FROM file_artifacts WHERE id=?", (artifact_id,))
            raise ArtifactError(404, "File not found", "file_not_found")
        return path, str(row["mime_type"])

    def signed_url(self, artifact_id: str, base_url: str) -> str:
        expires = int(
            (utc_now() + dt.timedelta(minutes=max(1, settings.gateway_image_output_ttl_minutes))).timestamp()
        )
        signature = hmac.new(
            self._secret, f"{artifact_id}:{expires}".encode(), hashlib.sha256
        ).hexdigest()
        query = urlencode({"expires": expires, "sig": signature})
        return f"{base_url.rstrip('/')}/v1/files/{artifact_id}/content?{query}"

    def verify_signature(self, artifact_id: str, expires: int | None, signature: str | None) -> bool:
        if not expires or not signature or expires < int(utc_now().timestamp()):
            return False
        expected = hmac.new(
            self._secret, f"{artifact_id}:{expires}".encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def resolve_file_ids(self, payload: dict[str, Any], owner_key_id: str) -> dict[str, Any]:
        resolved = copy.deepcopy(payload)

        async def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    await visit(item)
                return
            if not isinstance(value, dict):
                return
            block_type = value.get("type")
            if (
                isinstance(block_type, str)
                and block_type in {"input_image", "image_url"}
                and isinstance(value.get("file_id"), str)
            ):
                row = await self.get(str(value["file_id"]), owner_key_id)
                path = self._path(str(row["id"]), str(row["mime_type"]))
                raw = path.read_bytes()
                encoded = base64.b64encode(raw).decode("ascii")
                data_url = f"data:{row['mime_type']};base64,{encoded}"
                value.pop("file_id", None)
                if value.get("type") == "image_url":
                    current = value.get("image_url")
                    nested = dict(current) if isinstance(current, dict) else {}
                    nested["url"] = data_url
                    value["image_url"] = nested
                else:
                    value["image_url"] = data_url
            for child in list(value.values()):
                await visit(child)

        await visit(resolved)
        return resolved

    async def externalize_images_response(
        self, payload: dict[str, Any], *, owner_key_id: str, base_url: str
    ) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        output_format = str(result.get("output_format") or "png")
        data = result.get("data")
        if not isinstance(data, list):
            return result
        for index, item in enumerate(data):
            if not isinstance(item, dict) or not isinstance(item.get("b64_json"), str):
                continue
            raw = self._decode_base64(item["b64_json"])
            file_object = await self.create_bytes(
                raw,
                owner_key_id=owner_key_id,
                purpose="generated",
                filename=f"generated-{index}.{output_format}",
                ttl=dt.timedelta(minutes=max(1, settings.gateway_image_output_ttl_minutes)),
            )
            item.pop("b64_json", None)
            item["url"] = self.signed_url(str(file_object["id"]), base_url)
        return result

    async def externalize_response_payload(
        self, payload: dict[str, Any], *, owner_key_id: str, base_url: str
    ) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        cached: dict[str, str] = {}

        async def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    await visit(item)
                return
            if not isinstance(value, dict):
                return
            if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
                encoded = str(value["result"])
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                url = cached.get(digest)
                if url is None:
                    raw = self._decode_base64(encoded)
                    file_object = await self.create_bytes(
                        raw,
                        owner_key_id=owner_key_id,
                        purpose="generated",
                        filename="generated.png",
                        ttl=dt.timedelta(minutes=max(1, settings.gateway_image_output_ttl_minutes)),
                    )
                    url = self.signed_url(str(file_object["id"]), base_url)
                    cached[digest] = url
                value["result"] = url
                value["result_format"] = "url"
            for child in list(value.values()):
                await visit(child)

        await visit(result)
        return result

    @staticmethod
    def _decode_base64(value: str) -> bytes:
        encoded = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ArtifactError(502, "Upstream returned invalid image data", "upstream_invalid_image") from exc

    async def prune(self) -> None:
        rows = await gateway_store.all(
            "SELECT id,mime_type FROM file_artifacts WHERE expires_at<=?", (iso_now(),)
        )
        for row in rows:
            self._path(str(row["id"]), str(row["mime_type"])).unlink(missing_ok=True)
        await gateway_store.execute("DELETE FROM file_artifacts WHERE expires_at<=?", (iso_now(),))
        known = {
            self._path(str(row["id"]), str(row["mime_type"])).resolve()
            for row in await gateway_store.all("SELECT id,mime_type FROM file_artifacts")
        }
        for path in settings.gateway_image_artifact_dir().iterdir():
            if path.is_file() and path.resolve() not in known:
                path.unlink(missing_ok=True)


def _compact_data_url(url: str) -> str:
    if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
        return url
    encoded = url.split(";base64,", 1)[1]
    if len(encoded) <= settings.gateway_input_image_max_bytes:
        return url
    try:
        raw = base64.b64decode(encoded, validate=False)
    except (ValueError, binascii.Error):
        return url
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            edge = max(1, settings.gateway_input_image_max_edge)
            image.thumbnail((edge, edge))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=80, optimize=True)
            compact = buffer.getvalue()
    except Exception:
        return url
    if not compact:
        return url
    out = "data:image/jpeg;base64," + base64.b64encode(compact).decode("ascii")
    return out if len(out) < len(url) else url


def compact_payload_images(payload: dict[str, Any]) -> dict[str, Any]:
    """Downscale inbound data-URL images so history cannot grow without bound."""

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        if isinstance(value, str) and value.startswith("data:image/"):
            return _compact_data_url(value)
        return value

    compacted = visit(payload)
    return compacted if isinstance(compacted, dict) else payload


image_artifacts = ImageArtifactStore()
