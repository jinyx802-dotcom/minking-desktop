from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SHAPE_PATH = Path(os.environ.get("TS_T0_SHAPE_JSONL", "output/t0/shape.jsonl"))


def test_t0_shape_file_records_responses_without_secrets():
    if os.getenv("TS_LIVE_CODEX") != "1":
        pytest.skip("set TS_LIVE_CODEX=1 and generate output/t0/shape.jsonl")
    if not SHAPE_PATH.is_file():
        pytest.skip(f"missing {SHAPE_PATH}")
    records = [
        json.loads(line)
        for line in SHAPE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records
    joined = json.dumps(records)
    assert "sk-" not in joined
    assert "Bearer " not in joined
    responses = [item for item in records if item.get("path") == "/v1/responses"]
    assert responses, "expected a captured POST /v1/responses"
    shape = responses[0]["shape"]
    assert "tool_types" in shape
    assert "has_image_generation" in shape
    assert isinstance(responses[0].get("user_agent"), str)
