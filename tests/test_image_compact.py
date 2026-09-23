from __future__ import annotations

import base64
import io
import os

from PIL import Image

from app.image_artifacts import compact_payload_images


def _png_data_url(width: int, height: int, *, noisy: bool = False) -> str:
    if noisy:
        image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    else:
        image = Image.new("RGB", (width, height), (12, 80, 160))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def test_compact_payload_images_downscales_large_png():
    original = _png_data_url(1600, 1600, noisy=True)
    assert len(original) > 250 * 1024
    payload = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "look"},
                    {"type": "input_image", "image_url": original},
                ],
            }
        ]
    }
    compacted = compact_payload_images(payload)
    url = compacted["input"][0]["content"][1]["image_url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert len(url) < len(original)
    raw = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert max(image.size) <= 1280


def test_compact_payload_images_keeps_small_png():
    original = _png_data_url(32, 32)
    payload = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": original}}]}]}
    compacted = compact_payload_images(payload)
    assert compacted["messages"][0]["content"][0]["image_url"]["url"] == original
