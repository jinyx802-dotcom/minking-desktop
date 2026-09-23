from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class PreviewError(Exception):
    def __init__(self, message: str, *, code: str = "unsupported_capability") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def render_video_preview(data: bytes, variant: str) -> tuple[bytes, str]:
    chosen = (variant or "").strip().lower()
    if chosen == "thumbnail":
        frame = _extract_frame(data, offset=0.0)
        return _encode_image(frame, "WEBP"), "image/webp"
    if chosen == "spritesheet":
        duration = _duration_seconds(data)
        count = 8
        offsets = [0.0]
        if duration and duration > 0.2:
            span = max(0.0, duration - 0.05)
            offsets = [span * index / max(count - 1, 1) for index in range(count)]
        frames = []
        for offset in offsets:
            try:
                frames.append(_extract_frame(data, offset=offset))
            except PreviewError:
                continue
        if not frames:
            raise PreviewError("Cannot generate video preview", code="upstream_invalid_response")
        return _encode_image(_tile_frames(frames), "JPEG"), "image/jpeg"
    raise PreviewError("Unsupported video content variant", code="invalid_request")


def _ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise PreviewError("Video preview is not available", code="unsupported_capability") from exc
    if not path:
        raise PreviewError("Video preview is not available", code="unsupported_capability")
    return path


def _run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            timeout=30,
            startupinfo=startupinfo,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreviewError("Cannot generate video preview", code="upstream_invalid_response") from exc


def _duration_seconds(data: bytes) -> float | None:
    with tempfile.TemporaryDirectory(prefix="ts-video-") as folder:
        source = Path(folder) / "source.mp4"
        source.write_bytes(data)
        completed = _run_ffmpeg([_ffmpeg_bin(), "-i", str(source)])
    match = _DURATION_RE.search((completed.stderr or b"").decode("utf-8", "replace"))
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _extract_frame(data: bytes, *, offset: float) -> Image.Image:
    with tempfile.TemporaryDirectory(prefix="ts-video-") as folder:
        source = Path(folder) / "source.mp4"
        output = Path(folder) / "frame.jpg"
        source.write_bytes(data)
        completed = _run_ffmpeg(
            [
                _ffmpeg_bin(),
                "-ss",
                f"{max(0.0, offset):.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-q:v",
                "4",
                str(output),
            ]
        )
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size < 32:
            raise PreviewError("Cannot generate video preview", code="upstream_invalid_response")
        with Image.open(output) as image:
            return image.convert("RGB")


def _tile_frames(frames: list[Image.Image]) -> Image.Image:
    columns = min(4, len(frames))
    rows = (len(frames) + columns - 1) // columns
    cell_w = min(320, max(frame.width for frame in frames))
    resized: list[Image.Image] = []
    for frame in frames:
        height = max(1, int(frame.height * (cell_w / max(frame.width, 1))))
        resized.append(frame.resize((cell_w, height)))
    cell_h = max(frame.height for frame in resized)
    sheet = Image.new("RGB", (cell_w * columns, cell_h * rows), (0, 0, 0))
    for index, frame in enumerate(resized):
        col = index % columns
        row = index // columns
        sheet.paste(frame, (col * cell_w, row * cell_h))
    return sheet


def _encode_image(image: Image.Image, fmt: str) -> bytes:
    buffer = BytesIO()
    if fmt == "WEBP":
        image.save(buffer, format="WEBP", quality=80, method=4)
    else:
        image.save(buffer, format="JPEG", quality=80, optimize=True)
    return buffer.getvalue()
