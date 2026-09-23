from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote, unquote

from app.config import settings
from app.desktop_recipes import desktop_import_url, harness_catalog, public_v1_url
from app.harness_switch import (
    MANAGED_SKILL_NAME,
    apply_claude_code,
    apply_codex,
    apply_grok,
    apply_workbuddy,
    apply_zcode,
    detect_harnesses,
    managed_skill_source,
    restore_harness,
)


def test_managed_skill_source_ships_with_gateway():
    source = managed_skill_source()
    assert (source / "SKILL.md").is_file()
    assert (source / "scripts" / "minking-media.ps1").is_file()
    assert (source / "scripts" / "minking-media.sh").is_file()
    assert (source / "references" / "errors.md").is_file()
    fallback = Path(__file__).resolve().parents[1] / "app" / "client_skills" / MANAGED_SKILL_NAME
    assert (fallback / "SKILL.md").is_file()
    assert (fallback / "references" / "errors.md").is_file()
    shipped = (fallback / "SKILL.md").read_text(encoding="utf-8")
    assert "minking-media.ps1" in shipped
    assert "OPENAI_BASE_URL" not in shipped
    assert "New-SafeJpeg" in (fallback / "scripts" / "minking-media.ps1").read_text(encoding="utf-8")


def test_codex_and_workbuddy_snapshot_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example/v1")
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")
    (codex / "auth.json").write_text('{"tokens":{"access_token":"chatgpt-official"}}\n', encoding="utf-8")
    models_seed = json.dumps(
        {
            "models": [{"id": "local-ollama", "name": "Local"}],
            "availableModels": ["local-ollama", "deepseek-v4-pro"],
        }
    )
    workbuddy = home / ".workbuddy"
    codebuddy = home / ".codebuddy"
    for folder in (workbuddy, codebuddy):
        folder.mkdir()
        (folder / "models.json").write_text(models_seed, encoding="utf-8")
    claude = home / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "official-claude"}}), encoding="utf-8")
    grok = home / ".grok"
    grok.mkdir()
    (grok / "config.toml").write_text('[models]\ndefault = "grok-4.6"\n', encoding="utf-8")
    keep = workbuddy / "skills" / "workbuddy-checkin"
    keep.mkdir(parents=True)
    (keep / "SKILL.md").write_text("# keep-me\n", encoding="utf-8")
    profile = tmp_path / "profiles"

    detected = detect_harnesses(home=home)
    names = {item["id"]: item["installed"] for item in detected}
    assert names["codex"] is True
    assert names["workbuddy"] is True
    assert names["claude_code"] is True
    assert names["grok"] is True

    apply_codex(
        home=home,
        profile_root=profile,
        api_key="sk-ts-test",
        model="gpt-6-astra",
        catalog={"models": [{"slug": "gpt-6-astra", "display_name": "GPT-6 Astra", "context_window": 1000000}]},
    )
    apply_workbuddy(home=home, profile_root=profile, api_key="sk-ts-test", models=["gpt-5.6-luna", "gpt-6-astra"])
    apply_claude_code(home=home, profile_root=profile, api_key="sk-ts-test")
    apply_grok(home=home, profile_root=profile, api_key="sk-ts-test", models=["grok-4.6", "gpt-5.6-luna"])

    config = (codex / "config.toml").read_text(encoding="utf-8")
    assert "%userprofile%" not in config.lower()
    assert f'model_catalog_json = "{(home / ".codex" / "codex-models.json").as_posix()}"' in config
    written = json.loads((codex / "codex-models.json").read_text(encoding="utf-8"))
    assert written["models"][0]["display_name"] == "GPT-6 Astra"
    auth = json.loads((codex / "auth.json").read_text(encoding="utf-8"))
    assert auth["OPENAI_API_KEY"] == "sk-ts-test"
    for folder in (workbuddy, codebuddy):
        models = json.loads((folder / "models.json").read_text(encoding="utf-8"))
        assert "minking-default" not in {item["id"] for item in models["models"]}
        assert "local-ollama" in models["availableModels"]
        assert "deepseek-v4-pro" in models["availableModels"]
        luna = next(item for item in models["models"] if item.get("id") == "gpt-5.6-luna")
        assert luna["url"] == "https://portal.example/v1"
        assert luna["vendor"] == "user"
        assert luna["supportsReasoning"] is True
        assert "/chat/completions" not in luna["url"]
    claude_settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert claude_settings["env"]["ANTHROPIC_API_KEY"] == "sk-ts-test"
    assert claude_settings["env"]["ANTHROPIC_BASE_URL"] == "https://portal.example"
    assert claude_settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "gpt-5.6-sol"
    assert claude_settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "grok-4.6"
    assert claude_settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "gemini-3.8-flash"
    assert claude_settings["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "gpt-6-astra"
    assert claude_settings["model"] == "sonnet"
    grok_cfg = (grok / "config.toml").read_text(encoding="utf-8")
    assert "[model_providers.minkingapi]" in grok_cfg
    assert 'default = "minking-grok-4.6"' in grok_cfg
    assert 'api_key = "sk-ts-test"' in grok_cfg
    for folder in (codex, grok, claude, workbuddy, codebuddy):
        skill = folder / "skills" / MANAGED_SKILL_NAME
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        assert "minking-media.ps1" in text
        assert "OPENAI_BASE_URL" not in text
        assert "English" in text
        assert "JPEG" in text
        assert "cooldown" in text
        assert "Transfer Station" not in text
        assert (skill / "references" / "errors.md").is_file()
        assert "input_reference" in (skill / "scripts" / "minking-media.ps1").read_text(encoding="utf-8")
        assert "New-SafeJpeg" in (skill / "scripts" / "minking-media.ps1").read_text(encoding="utf-8")
        assert "--reference" in (skill / "scripts" / "minking-media.sh").read_text(encoding="utf-8")
    agents = (codex / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.count("# >>> minking media block") == 1
    assert "primary image/video path" in agents
    assert "scripts/image_gen.py" in agents

    recipes = {item["id"]: item for item in harness_catalog(messages_ready=True)}
    restore_harness(recipes["codex"], home=home, profile_root=profile)
    restore_harness(recipes["workbuddy"], home=home, profile_root=profile)
    restore_harness(recipes["claude_code"], home=home, profile_root=profile)
    restore_harness(recipes["grok"], home=home, profile_root=profile)

    backups = sorted((profile / "codex" / "backups").iterdir())
    assert backups
    assert (backups[0] / "auth.json").is_file()
    restored_auth = json.loads((codex / "auth.json").read_text(encoding="utf-8"))
    assert restored_auth == {"tokens": {"access_token": "chatgpt-official"}}
    for folder in (workbuddy, codebuddy):
        restored_wb = json.loads((folder / "models.json").read_text(encoding="utf-8"))
        assert [item["id"] for item in restored_wb["models"]] == ["local-ollama"]
    restored_claude = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert restored_claude["env"]["ANTHROPIC_API_KEY"] == "official-claude"
    assert "ANTHROPIC_BASE_URL" not in restored_claude["env"]
    assert "sk-ts-test" not in (codex / "config.toml").read_text(encoding="utf-8")
    restored_grok = (grok / "config.toml").read_text(encoding="utf-8")
    assert 'default = "grok-4.6"' in restored_grok
    assert "minking managed block" not in restored_grok
    assert not (codex / "skills" / MANAGED_SKILL_NAME).exists()
    assert not (workbuddy / "skills" / MANAGED_SKILL_NAME).exists()
    assert (workbuddy / "skills" / "workbuddy-checkin" / "SKILL.md").read_text(encoding="utf-8") == "# keep-me\n"


def test_codex_connect_keeps_windows_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example/v1")
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text(
        'model = "gpt-5.4"\n\n[windows]\nsandbox = "elevated"\n',
        encoding="utf-8",
    )
    apply_codex(
        home=home,
        profile_root=profile,
        api_key="sk-ts-test",
        model="gpt-6-astra",
        catalog={"models": [{"slug": "gpt-6-astra", "display_name": "GPT-6 Astra"}]},
    )
    config = (codex / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "minkingapi"' in config
    assert 'sandbox = "elevated"' in config
    assert config.count("[windows]") == 1


def _mark_installed(home, harness_id: str) -> None:
    if harness_id == "zcode":
        (home / ".zcode" / "v2").mkdir(parents=True)


def test_zcode_detect_and_copy_recipes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example/v1")
    catalog = {item["id"]: item for item in harness_catalog(messages_ready=True)}
    assert catalog["codex"]["one_click"] is True
    assert catalog["workbuddy"]["one_click"] is True
    assert catalog["claude_code"]["one_click"] is True
    assert catalog["grok"]["one_click"] is True
    assert catalog["zcode"]["one_click"] is True
    assert "trae" not in catalog
    assert "qcode" not in catalog
    assert "antigravity" not in catalog
    v1 = public_v1_url()
    assert v1.endswith("/v1")
    recipe = catalog["zcode"]
    assert recipe["copy"]["base_url"] == v1
    assert recipe["copy"]["full_url"] is False
    assert "API Key" in recipe["copy"]["fields"]
    assert "sk-" not in json.dumps(recipe)
    assert any("设置" in step or "模型" in step for step in recipe["copy"]["navigation"])
    home = tmp_path / "home"
    detected = {item["id"]: item for item in detect_harnesses(home=home)}
    assert detected["zcode"]["installed"] is False
    _mark_installed(home, "zcode")
    found = {item["id"]: item for item in detect_harnesses(home=home)}
    assert found["zcode"]["installed"] is True


def test_zcode_merge_snapshot_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example/v1")
    home = tmp_path / "home"
    live = home / ".zcode" / "v2"
    live.mkdir(parents=True)
    original = {
        "schemaVersion": 1,
        "config": {
            "providerConfigRules": {
                "providerRules": [
                    {
                        "providerId": "official-zai",
                        "providerName": "Z.AI",
                        "config": {"access": {"type": "api-key", "apiKey": "official-token"}},
                    }
                ]
            },
            "modelConfigRules": {"providerModelRules": [{"modelId": "glm-5"}], "manualProviderModelRules": []},
            "defaultModelSelection": {"providerId": "official-zai", "modelId": "glm-5"},
        },
    }
    (live / "provider_config.json").write_text(json.dumps(original), encoding="utf-8")
    profile = tmp_path / "profiles"
    apply_zcode(home=home, profile_root=profile, api_key="sk-ts-test", model="gpt-6-astra")
    merged = json.loads((live / "provider_config.json").read_text(encoding="utf-8"))
    rules = merged["config"]["providerConfigRules"]["providerRules"]
    ids = [item["providerId"] for item in rules]
    assert ids == ["official-zai", "gpt-6-astra"]
    minking = next(item for item in rules if item["providerId"] == "gpt-6-astra")
    assert minking["providerName"] == "gpt-6-astra"
    assert minking["config"]["api"]["baseUrl"] == "https://portal.example/v1"
    assert minking["config"]["api"]["baseUrl"].endswith("/v1")
    assert minking["config"]["personalModelIds"] == ["gpt-6-astra"]
    assert minking["config"]["modelOrder"] == ["gpt-6-astra"]
    assert minking["config"]["access"]["apiKey"] == "sk-ts-test"
    assert merged["config"]["defaultModelSelection"] == {"providerId": "official-zai", "modelId": "glm-5"}
    assert merged["config"]["modelConfigRules"]["providerModelRules"] == [
        {"modelId": "glm-5"},
        {"modelId": "gpt-6-astra", "providerId": "gpt-6-astra", "config": {"enabled": True}},
    ]
    recipes = {item["id"]: item for item in harness_catalog(messages_ready=True)}
    restore_harness(recipes["zcode"], home=home, profile_root=profile)
    restored = json.loads((live / "provider_config.json").read_text(encoding="utf-8"))
    assert restored == original


def test_zcode_apply_creates_file_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example")
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    apply_zcode(home=home, profile_root=profile, api_key="sk-ts-test", models=["gpt-6-astra", "grok-4.6"])
    payload = json.loads((home / ".zcode" / "v2" / "provider_config.json").read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 1
    ids = [item["providerId"] for item in payload["config"]["providerConfigRules"]["providerRules"]]
    assert ids == ["gpt-6-astra", "grok-4.6"]
    assert payload["config"]["defaultModelSelection"] == {"providerId": "gpt-6-astra", "modelId": "gpt-6-astra"}
    assert payload["config"]["providerOrder"] == ["gpt-6-astra", "grok-4.6"]
    assert payload["config"]["providerConfigRules"]["providerRules"][0]["config"]["api"]["baseUrl"] == "https://portal.example/v1"
    apply_zcode(home=home, profile_root=profile, api_key="sk-ts-test", model="gpt-6-astra")
    replaced = json.loads((home / ".zcode" / "v2" / "provider_config.json").read_text(encoding="utf-8"))
    assert [item["providerId"] for item in replaced["config"]["providerConfigRules"]["providerRules"]] == ["gpt-6-astra"]
    recipes = {item["id"]: item for item in harness_catalog(messages_ready=True)}
    restore_harness(recipes["zcode"], home=home, profile_root=profile)
    assert not (home / ".zcode" / "v2" / "provider_config.json").exists()


def test_desktop_import_url_omits_api_key(monkeypatch):
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example/v1")
    url = desktop_import_url(email="ada@example.com")
    assert url.startswith("minking://import?")
    assert "base=" in url
    assert "email=" in url
    assert "sk-" not in url
    assert "key=" not in url.lower()
    assert unquote(url.split("base=", 1)[1].split("&", 1)[0]) == "https://portal.example/v1"
    assert quote("ada@example.com", safe="") in url
