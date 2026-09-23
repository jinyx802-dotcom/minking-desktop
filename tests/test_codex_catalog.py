from __future__ import annotations

from app.providers.base import ModelInfo
from app.providers.codex_catalog import (
    WINDOWS_CODEX_CATALOG_PLACEHOLDER,
    codex_catalog_toml_path,
    is_codex_client_ua,
    picker_entry,
    wants_codex_catalog,
    wants_codex_image_urls,
)


def test_codex_catalog_path_is_posix_absolute_under_home(tmp_path):
    home = tmp_path / "Users" / "DELL"
    path = codex_catalog_toml_path(home)
    assert path == (home / ".codex" / "codex-models.json").as_posix()
    assert "\\" not in path
    assert "%userprofile%" not in WINDOWS_CODEX_CATALOG_PLACEHOLDER.lower()
    assert WINDOWS_CODEX_CATALOG_PLACEHOLDER.startswith("C:/Users/")
    assert WINDOWS_CODEX_CATALOG_PLACEHOLDER.endswith("/.codex/codex-models.json")


def test_codex_client_ua_matches_desktop_cli_and_exec():
    assert is_codex_client_ua("codex_cli_rs/0.154.0")
    assert is_codex_client_ua("codex_exec/0.155.0-alpha.2.6 (Windows; x86_64) dumb (codex_exec; 0.155.0-alpha.2.6)")
    assert is_codex_client_ua("codex/0.155.0")
    assert is_codex_client_ua("Codex Desktop/26.911.61220")
    assert not is_codex_client_ua("curl/8.5.0")
    assert not is_codex_client_ua("openai-python/1.0")
    assert not is_codex_client_ua("")
    assert not is_codex_client_ua(None)


def test_codex_exec_ua_selects_picker_without_client_version():
    assert wants_codex_catalog(None, "codex_exec/0.155.0-alpha.2.6")
    assert wants_codex_catalog("0.155.0", "curl/8.5.0")
    assert not wants_codex_catalog(None, "curl/8.5.0")


def test_codex_ua_defaults_to_image_urls_and_header_overrides():
    exec_ua = "codex_exec/0.155.0-alpha.2.6"
    assert wants_codex_image_urls(exec_ua, None)
    assert wants_codex_image_urls(exec_ua, "")
    assert wants_codex_image_urls("codex_cli_rs/0.154.0", "url")
    assert wants_codex_image_urls("curl/8.5.0", "url")
    assert not wants_codex_image_urls(exec_ua, "b64")
    assert not wants_codex_image_urls(exec_ua, "b64_json")
    assert not wants_codex_image_urls("curl/8.5.0", None)
    assert not wants_codex_image_urls(None, None)


def test_picker_advertises_image_generation_for_text_models():
    grok = picker_entry(
        ModelInfo(id="grok-4.6", provider="grok", type="text", capabilities=()),
        priority=1,
    )
    gpt = picker_entry(
        ModelInfo(id="gpt-6-astra", provider="codex", type="text", capabilities=()),
        priority=2,
    )
    gemini = picker_entry(
        ModelInfo(id="gemini-3.8-flash", provider="antigravity", type="text", capabilities=()),
        priority=3,
    )
    sonnet = picker_entry(
        ModelInfo(id="claude-sonnet-4-6", provider="antigravity", type="text", capabilities=()),
        priority=4,
    )
    assert grok["experimental_supported_tools"] == ["image_generation"]
    assert gpt["experimental_supported_tools"] == ["image_generation"]
    assert gemini["experimental_supported_tools"] == ["image_generation"]
    assert sonnet["experimental_supported_tools"] == []
    assert sonnet["display_name"] == "Claude Sonnet 4.6"
    assert grok["additional_speed_tiers"] == ["fast"]
    assert grok["service_tiers"][0]["id"] == "priority"
    assert gpt["additional_speed_tiers"] == []
    assert gpt["service_tiers"] == []
    assert gemini["additional_speed_tiers"] == []
    image = picker_entry(
        ModelInfo(id="grok-imagine-image-2.0", provider="grok", type="image", capabilities=()),
        priority=5,
    )
    assert image["additional_speed_tiers"] == []
    assert image["service_tiers"] == []
