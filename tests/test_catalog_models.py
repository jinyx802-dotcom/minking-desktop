from __future__ import annotations

from conftest import ADMIN_HEADERS

from app.providers.antigravity import AntigravityAdapter
from app.providers.catalog_extra import (
    parse_antigravity_model_ids,
    parse_model_ids,
    replace_all,
    set_upstream,
    upsert_manual,
)
from app.providers.codex import CodexAdapter
from app.providers.workbuddy import WorkBuddyAdapter


def setup_function() -> None:
    replace_all([])


def teardown_function() -> None:
    replace_all([])


def test_upstream_lists_cover_codex_antigravity_and_workbuddy():
    assert parse_model_ids({"models": [{"slug": "gpt-6-terra"}, {"id": "gpt-6-terra"}]}) == ["gpt-6-terra"]
    assert parse_antigravity_model_ids(
        {"models": {"gemini-3.9-flash": {}, "tab_jump": {}, "claude-new": {}}}
    ) == ["gemini-3.9-flash", "claude-new"]
    set_upstream("codex", [("gpt-6-terra", "text")])
    set_upstream("antigravity", [("gemini-3.9-flash", "text"), ("claude-new", "text")])
    upsert_manual("workbuddy", "workbuddy/new-model", "text")
    set_upstream("workbuddy", [("workbuddy/new-model", "image"), ("workbuddy/from-upstream", "text")])
    assert "gpt-6-terra" in {item.id for item in CodexAdapter().catalog()}
    antigravity = {item.id for item in AntigravityAdapter().catalog()}
    assert {"gemini-3.9-flash", "claude-new"} <= antigravity
    workbuddy = {item.id: item for item in WorkBuddyAdapter().catalog()}
    assert workbuddy["workbuddy/new-model"].type == "text"
    assert "workbuddy/from-upstream" in workbuddy


def test_effort_variants_are_hidden_and_fold_to_the_public_slug():
    from app.providers.base import resolve_provider_model

    found = set(
        parse_antigravity_model_ids(
            {
                "models": {
                    "gemini-3.8-flash-medium": {},
                    "gemini-3.8-flash-tiered": {},
                    "gemini-3.7-flash-low": {},
                    "gemini-3.6-flash-high": {},
                    "gemini-2.5-flash": {},
                    "gemini-3.1-flash-image": {},
                    "tab_jump": {},
                }
            }
        )
    )
    assert "gemini-3.8-flash-medium" not in found
    assert {"gemini-2.5-flash", "gemini-3.1-flash-image"} <= found
    set_upstream(
        "antigravity",
        [
            ("gemini-3.8-flash-medium", "text"),
            ("gemini-2.5-flash", "text"),
            ("gemini-3.1-flash-lite", "text"),
        ],
    )
    ids = {item.id for item in AntigravityAdapter().catalog()}
    assert "gemini-3.8-flash" in ids
    assert "gemini-3.8-flash-medium" not in ids
    assert "gemini-2.5-flash" in ids
    assert "gemini-3.1-flash-lite" in ids
    assert resolve_provider_model("gemini-3.8-flash-medium") == ("antigravity", "gemini-3.8-flash")


def test_admin_can_add_and_remove_a_model(client):
    created = client.post(
        "/admin/api/models",
        json={"provider": "antigravity", "model_id": "gemini-3.9-flash", "model_type": "text"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    listed = client.get("/admin/api/models", headers=ADMIN_HEADERS).json()["data"]
    row = next(item for item in listed if item["id"] == "gemini-3.9-flash")
    assert row["provider"] == "antigravity"
    assert row["source"] == "manual"
    removed = client.delete(
        "/admin/api/models",
        params={"provider": "antigravity", "model_id": "gemini-3.9-flash"},
        headers=ADMIN_HEADERS,
    )
    assert removed.status_code == 200, removed.text
    listed = client.get("/admin/api/models", headers=ADMIN_HEADERS).json()["data"]
    assert all(item["id"] != "gemini-3.9-flash" for item in listed)
    missing = client.delete(
        "/admin/api/models",
        params={"provider": "codex", "model_id": "gpt-6-astra"},
        headers=ADMIN_HEADERS,
    )
    assert missing.status_code == 404
