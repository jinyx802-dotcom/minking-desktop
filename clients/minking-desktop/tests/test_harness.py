from __future__ import annotations

import json
import sys
from pathlib import Path

from minking_desktop.harness import (
    MANAGED_SKILL_NAME,
    apply_claude_code,
    apply_codex,
    apply_grok,
    apply_harness,
    apply_workbuddy,
    apply_zcode,
    codex_catalog_toml_path,
    detect_harnesses,
    managed_skill_source,
    packed_codex_catalog,
    planned_sync_files,
    remove_managed_skill,
    list_restore_versions,
    restore_harness,
    snapshot_harness,
    sync_codex_sessions,
    sync_managed_skill,
)
from minking_desktop.recipes import harness_catalog

PUBLIC = "https://portal.example/v1"


def _seed(home: Path) -> None:
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")
    (codex / "auth.json").write_text('{"tokens":{"access_token":"chatgpt-official"}}\n', encoding="utf-8")
    models_seed = json.dumps(
        {"models": [{"id": "local-ollama", "name": "Local"}], "availableModels": ["local-ollama"]}
    )
    for name in (".workbuddy", ".codebuddy"):
        folder = home / name
        folder.mkdir()
        (folder / "models.json").write_text(models_seed, encoding="utf-8")
    claude = home / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "official-claude"}}), encoding="utf-8")
    zcode = home / ".zcode" / "v2"
    zcode.mkdir(parents=True)
    (zcode / "provider_config.json").write_text(
        json.dumps({"schemaVersion": 1, "config": {"providerConfigRules": {"providerRules": [{"providerId": "zhipu"}]}}}),
        encoding="utf-8",
    )
    grok = home / ".grok"
    grok.mkdir()
    (grok / "config.toml").write_text(
        '[models]\ndefault = "grok-4.6"\n\n[ui]\nyolo = false\n',
        encoding="utf-8",
    )
    keep = home / ".workbuddy" / "skills" / "workbuddy-checkin"
    keep.mkdir(parents=True)
    (keep / "SKILL.md").write_text("# keep-me\n", encoding="utf-8")


def _assert_media_skill(root: Path) -> None:
    skill = root / "skills" / MANAGED_SKILL_NAME
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert "minking-media.ps1" in text
    assert "input_reference" in text
    assert "OPENAI_BASE_URL" not in text
    assert "English" in text
    assert "ImageGen" in text
    assert "image_generation" in text
    assert "JPEG" in text
    assert "cooldown" in text
    assert "Transfer Station" not in text
    assert (skill / "references" / "errors.md").is_file()
    ps1 = (skill / "scripts" / "minking-media.ps1").read_text(encoding="utf-8")
    assert "-Reference" in ps1
    assert "input_reference" in ps1
    assert "New-SafeJpeg" in ps1
    assert "--form-string" in ps1
    sh = (skill / "scripts" / "minking-media.sh").read_text(encoding="utf-8")
    assert "--reference" in sh
    assert "input_reference" in sh
    assert "prepare_jpeg" in sh
    assert (skill / ".minking-managed").is_file()


def test_apply_workbuddy_keeps_official_names_and_drops_injection_whitelist(tmp_path):
    home = tmp_path / "home"
    folder = home / ".workbuddy"
    folder.mkdir(parents=True)
    (folder / "models.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "gpt-6-astra",
                        "name": "gpt-6-astra",
                        "vendor": "user",
                        "url": "https://portal.example/v1",
                        "apiKey": "sk-old",
                    },
                    {"id": "local-ollama", "name": "Local"},
                ],
                "availableModels": ["gpt-6-astra", "deepseek-v4-pro", "glm-5.2"],
            }
        ),
        encoding="utf-8",
    )
    apply_workbuddy(
        home=home,
        profile_root=tmp_path / "profiles",
        api_key="sk-new",
        public_base=PUBLIC,
        models=["grok-4.6"],
    )
    payload = json.loads((folder / "models.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in payload["models"]] == ["local-ollama", "grok-4.6"]
    assert payload["availableModels"] == ["deepseek-v4-pro", "glm-5.2", "grok-4.6"]

    (folder / "models.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "gpt-6-astra",
                        "name": "gpt-6-astra",
                        "vendor": "user",
                        "url": "https://portal.example/v1",
                        "apiKey": "sk-old",
                    },
                    {
                        "id": "gpt-5.6-sol",
                        "name": "gpt-5.6-sol",
                        "vendor": "user",
                        "url": "https://portal.example/v1",
                        "apiKey": "sk-old",
                    },
                ],
                "availableModels": ["gpt-6-astra", "gpt-5.6-sol"],
            }
        ),
        encoding="utf-8",
    )
    apply_workbuddy(
        home=home,
        profile_root=tmp_path / "profiles",
        api_key="sk-new",
        public_base=PUBLIC,
        models=["grok-4.6"],
    )
    wiped = json.loads((folder / "models.json").read_text(encoding="utf-8"))
    assert "availableModels" not in wiped
    assert any(item["id"] == "grok-4.6" for item in wiped["models"])


def test_sync_installs_every_pack_and_restore_keeps_user_skills(tmp_path):
    home = tmp_path / "home"
    own = home / ".codex" / "skills" / "my-own"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    root = tmp_path / "skills"
    for name, text in (("minking-media", "# media\n"), ("extra-skill", "# extra\n")):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
    sync_managed_skill(home=home, extra=("codex",), source=root)
    assert (home / ".codex" / "skills" / "minking-media" / "SKILL.md").read_text(encoding="utf-8") == "# media\n"
    assert (home / ".codex" / "skills" / "extra-skill" / "SKILL.md").read_text(encoding="utf-8") == "# extra\n"
    assert (home / ".codex" / "skills" / "extra-skill" / ".minking-managed").is_file()
    assert (own / "SKILL.md").read_text(encoding="utf-8") == "# mine\n"
    remove_managed_skill(home=home, harness_id="codex")
    assert not (home / ".codex" / "skills" / "minking-media").exists()
    assert not (home / ".codex" / "skills" / "extra-skill").exists()
    assert (own / "SKILL.md").read_text(encoding="utf-8") == "# mine\n"


def test_sync_managed_skill_uses_override_source(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    source = tmp_path / "remote-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("# remote\n", encoding="utf-8")
    sync_managed_skill(home=home, extra=("codex",), source=source)
    installed = home / ".codex" / "skills" / MANAGED_SKILL_NAME / "SKILL.md"
    assert installed.read_text(encoding="utf-8") == "# remote\n"


def test_planned_sync_files_lists_live_paths(tmp_path):
    home = tmp_path / "home"
    _seed(home)
    recipe = next(item for item in harness_catalog(public_base=PUBLIC) if item["id"] == "codex")
    files = planned_sync_files(recipe, home=home)
    names = {item["name"] for item in files}
    assert {"auth.json", "config.toml", ".env", "codex-models.json"} <= names
    assert any(item["exists"] and item["name"] == "auth.json" for item in files)


def test_detect_and_modes(tmp_path):
    home = tmp_path / "home"
    _seed(home)
    detected = {item["id"]: item for item in detect_harnesses(home=home, public_base=PUBLIC)}
    assert detected["codex"]["installed"] is True
    assert detected["workbuddy"]["installed"] is True
    assert detected["claude_code"]["installed"] is True
    assert detected["grok"]["installed"] is True
    assert detected["codex"]["mode"] == "official"
    assert detected["grok"]["mode"] == "official"
    assert "trae" not in detected
    assert "qcode" not in detected
    assert "antigravity" not in detected
    assert detected["zcode"]["one_click"] is True
    assert detected["zcode"]["installed"] is True


def test_codex_workbuddy_claude_snapshot_restore(tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    _seed(home)
    apply_codex(
        home=home,
        profile_root=profile,
        api_key="sk-ts-test",
        model="gpt-6-astra",
        catalog={"models": [{"slug": "gpt-6-astra"}]},
        public_base=PUBLIC,
    )
    apply_workbuddy(
        home=home,
        profile_root=profile,
        api_key="sk-ts-test",
        public_base=PUBLIC,
        models=["gpt-5.6-luna", "grok-4.6"],
    )
    apply_claude_code(home=home, profile_root=profile, api_key="sk-ts-test", public_base=PUBLIC)
    apply_zcode(home=home, profile_root=profile, api_key="sk-ts-test", public_base=PUBLIC, model="gpt-6-astra")
    apply_grok(
        home=home,
        profile_root=profile,
        api_key="sk-ts-test",
        public_base=PUBLIC,
        models=["gpt-5.6-luna", "grok-4.6", "grok-imagine-image-2.0"],
        catalog={"models": [{"slug": "grok-4.6", "display_name": "Grok 4.6"}]},
    )

    config = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "minkingapi" in config
    assert "https://portal.example/v1" in config
    assert "ANTHROPIC" not in config
    assert "%userprofile%" not in config.lower()
    assert f'model_catalog_json = "{codex_catalog_toml_path(home)}"' in config
    auth = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert auth["OPENAI_API_KEY"] == "sk-ts-test"
    env = (home / ".codex" / ".env").read_text(encoding="utf-8")
    assert "OPENAI_BASE_URL=https://portal.example/v1" in env

    for name in (".workbuddy", ".codebuddy"):
        models = json.loads((home / name / "models.json").read_text(encoding="utf-8"))
        assert "minking-default" not in {item["id"] for item in models["models"]}
        assert "local-ollama" in models["availableModels"]
        assert models["availableModels"][-2:] == ["gpt-5.6-luna", "grok-4.6"]
        luna = next(item for item in models["models"] if item.get("id") == "gpt-5.6-luna")
        assert luna["name"] == "gpt-5.6-luna"
        assert luna["vendor"] == "user"
        assert luna["url"] == "https://portal.example/v1"
        assert luna["supportsToolCall"] is True
        assert luna["supportsImages"] is True
        assert luna["supportsReasoning"] is True
        assert "maxInputTokens" not in luna
        assert "/chat/completions" not in luna["url"]

    claude_settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert claude_settings["env"]["ANTHROPIC_API_KEY"] == "sk-ts-test"
    assert claude_settings["env"]["ANTHROPIC_BASE_URL"] == "https://portal.example"
    assert claude_settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "gpt-5.6-sol"
    assert claude_settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "grok-4.6"
    assert claude_settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "gemini-3.8-flash"
    assert claude_settings["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "gpt-6-astra"
    assert claude_settings["env"]["ANTHROPIC_MODEL"] == "grok-4.6"
    assert claude_settings["model"] == "sonnet"
    zcode_cfg = json.loads((home / ".zcode" / "v2" / "provider_config.json").read_text(encoding="utf-8"))
    rules = zcode_cfg["config"]["providerConfigRules"]["providerRules"]
    assert any(item.get("providerId") == "zhipu" for item in rules)
    minking = next(item for item in rules if item.get("providerId") == "gpt-6-astra")
    assert minking["providerName"] == "gpt-6-astra"
    assert minking["config"]["personalModelIds"] == ["gpt-6-astra"]
    assert minking["config"]["api"]["baseUrl"] == "https://portal.example/v1"
    assert "minkingapi" not in {item.get("providerId") for item in rules}
    grok_cfg = (home / ".grok" / "config.toml").read_text(encoding="utf-8")
    assert 'default = "minking-grok-4.6"' in grok_cfg
    assert "[model_providers.minkingapi]" in grok_cfg
    assert 'base_url = "https://portal.example/v1"' in grok_cfg
    assert 'api_key = "sk-ts-test"' in grok_cfg
    assert '[model."minking-grok-4.6"]' in grok_cfg
    assert 'model = "grok-4.6"' in grok_cfg
    assert "grok-imagine-image-2.0" not in grok_cfg
    assert "[ui]" in grok_cfg
    assert "yolo = false" in grok_cfg

    for name in (".codex", ".grok", ".claude", ".workbuddy", ".codebuddy"):
        _assert_media_skill(home / name)
    agents = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.count("minking media block") == 2
    assert "primary image/video path" in agents
    assert "scripts/image_gen.py" in agents
    assert "image_generation" in agents
    (home / ".codex" / "skills" / MANAGED_SKILL_NAME / "SKILL.md").write_text("stale\n", encoding="utf-8")
    apply_codex(
        home=home,
        profile_root=profile,
        api_key="sk-ts-test",
        model="gpt-6-astra",
        catalog={"models": [{"slug": "gpt-6-astra"}]},
        public_base=PUBLIC,
    )
    updated = (home / ".codex" / "skills" / MANAGED_SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    assert "stale" not in updated
    assert "minking-media.ps1" in updated
    assert not (home / ".codex" / "skills" / f"{MANAGED_SKILL_NAME}-2").exists()
    agents_again = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert agents_again.count("# >>> minking media block") == 1

    detected = {item["id"]: item for item in detect_harnesses(home=home, public_base=PUBLIC)}
    assert detected["codex"]["mode"] == "cloud"
    assert detected["workbuddy"]["mode"] == "cloud"
    assert detected["claude_code"]["mode"] == "cloud"
    assert detected["grok"]["mode"] == "cloud"
    assert detected["zcode"]["mode"] == "cloud"

    recipes = {item["id"]: item for item in harness_catalog(public_base=PUBLIC)}
    restore_harness(recipes["codex"], home=home, profile_root=profile)
    restore_harness(recipes["workbuddy"], home=home, profile_root=profile)
    restore_harness(recipes["grok"], home=home, profile_root=profile)
    restore_harness(recipes["claude_code"], home=home, profile_root=profile)
    restore_harness(recipes["zcode"], home=home, profile_root=profile)

    restored_auth = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert restored_auth == {"tokens": {"access_token": "chatgpt-official"}}
    for name in (".workbuddy", ".codebuddy"):
        restored_wb = json.loads((home / name / "models.json").read_text(encoding="utf-8"))
        assert [item["id"] for item in restored_wb["models"]] == ["local-ollama"]
    restored_claude = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert restored_claude["env"]["ANTHROPIC_API_KEY"] == "official-claude"
    assert "ANTHROPIC_BASE_URL" not in restored_claude["env"]
    restored_zcode = json.loads((home / ".zcode" / "v2" / "provider_config.json").read_text(encoding="utf-8"))
    assert [item["providerId"] for item in restored_zcode["config"]["providerConfigRules"]["providerRules"]] == ["zhipu"]
    assert "sk-ts-test" not in (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    restored_grok = (home / ".grok" / "config.toml").read_text(encoding="utf-8")
    assert 'default = "grok-4.6"' in restored_grok
    assert "minking managed block" not in restored_grok
    assert "sk-ts-test" not in restored_grok
    assert not (home / ".codex" / "skills" / MANAGED_SKILL_NAME).exists()
    assert not (home / ".workbuddy" / "skills" / MANAGED_SKILL_NAME).exists()
    assert not (home / ".codebuddy" / "skills" / MANAGED_SKILL_NAME).exists()
    assert (home / ".workbuddy" / "skills" / "workbuddy-checkin" / "SKILL.md").read_text(encoding="utf-8") == "# keep-me\n"
    assert not (home / ".codex" / "AGENTS.md").exists() or "minking media block" not in (
        home / ".codex" / "AGENTS.md"
    ).read_text(encoding="utf-8")


def test_zcode_provider_id_is_model_slug(tmp_path):
    home = tmp_path / "home"
    live = home / ".zcode" / "v2"
    live.mkdir(parents=True)
    (live / "provider_config.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "config": {
                    "providerOrder": ["zhipu", "minkingapi"],
                    "providerConfigRules": {
                        "providerRules": [
                            {"providerId": "zhipu"},
                            {
                                "providerId": "minkingapi",
                                "providerName": "MinKing AI",
                                "config": {
                                    "group": "standard-personal",
                                    "api": {"type": "openai-chat-completions", "baseUrl": "https://portal.example/v1"},
                                    "personalModelIds": ["old-model"],
                                },
                            },
                        ]
                    },
                    "defaultModelSelection": {"providerId": "minkingapi", "modelId": "old-model"},
                },
            }
        ),
        encoding="utf-8",
    )
    apply_zcode(
        home=home,
        profile_root=tmp_path / "profiles",
        api_key="sk-ts-test",
        public_base=PUBLIC,
        models=["gpt-6-astra", "grok-4.6"],
    )
    payload = json.loads((live / "provider_config.json").read_text(encoding="utf-8"))
    rules = payload["config"]["providerConfigRules"]["providerRules"]
    ids = [item["providerId"] for item in rules]
    assert ids == ["zhipu", "gpt-6-astra", "grok-4.6"]
    astra = next(item for item in rules if item["providerId"] == "gpt-6-astra")
    assert astra["providerName"] == "gpt-6-astra"
    assert astra["config"]["personalModelIds"] == ["gpt-6-astra"]
    assert astra["config"]["modelOrder"] == ["gpt-6-astra"]
    assert payload["config"]["defaultModelSelection"] == {"providerId": "gpt-6-astra", "modelId": "gpt-6-astra"}
    assert payload["config"]["providerOrder"] == ["zhipu", "gpt-6-astra", "grok-4.6"]
    detected = {item["id"]: item for item in detect_harnesses(home=home, public_base=PUBLIC)}
    assert detected["zcode"]["mode"] == "cloud"


def test_official_snapshot_not_overwritten(tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    _seed(home)
    recipe = next(item for item in harness_catalog(public_base=PUBLIC) if item["id"] == "codex")
    snapshot_harness(recipe, home=home, profile_root=profile)
    apply_codex(
        home=home,
        profile_root=profile,
        api_key="sk-ts-first",
        model="gpt-6-astra",
        catalog={"models": [{"slug": "gpt-6-astra"}]},
        public_base=PUBLIC,
    )
    apply_codex(
        home=home,
        profile_root=profile,
        api_key="sk-ts-second",
        model="gpt-6-astra",
        catalog={"models": [{"slug": "gpt-6-astra"}]},
        public_base=PUBLIC,
    )
    snap_auth = json.loads((profile / "codex" / "official" / "auth.json").read_text(encoding="utf-8"))
    assert snap_auth == {"tokens": {"access_token": "chatgpt-official"}}
    backups = sorted((profile / "codex" / "backups").iterdir())
    assert len(backups) == 2
    first = json.loads((backups[0] / "auth.json").read_text(encoding="utf-8"))
    second = json.loads((backups[1] / "auth.json").read_text(encoding="utf-8"))
    assert first == {"tokens": {"access_token": "chatgpt-official"}}
    assert second["OPENAI_API_KEY"] == "sk-ts-first"
    restore_harness(recipe, home=home, profile_root=profile)
    live_auth = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert live_auth == {"tokens": {"access_token": "chatgpt-official"}}
    versions = list_restore_versions("codex", profile_root=profile)
    assert versions[0]["id"] == backups[-1].name
    assert versions[-1] == {"id": "official", "label": "首次接入前的配置", "path": str(profile / "codex" / "official")}
    chosen = restore_harness(recipe, home=home, profile_root=profile, version=backups[-1].name)
    assert chosen["ok"] is True and chosen["version"] == backups[-1].name
    chosen_auth = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert chosen_auth["OPENAI_API_KEY"] == "sk-ts-first"
    rejected = restore_harness(recipe, home=home, profile_root=profile, version="../official")
    assert rejected["ok"] is False


def test_codex_connect_keeps_windows_sandbox(tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text(
        "\n".join([
            'model_provider = "openai"',
            'model = "gpt-5.4"',
            "",
            "[projects]",
            'e_drive = "E:/work"',
            "",
            "[windows]",
            'sandbox = "elevated"',
            "",
        ]),
        encoding="utf-8",
    )
    apply_codex(
        home=home,
        profile_root=profile,
        api_key="sk-ts-test",
        model="gpt-6-astra",
        catalog={"models": [{"slug": "gpt-6-astra"}]},
        public_base=PUBLIC,
    )
    config = (codex / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "minkingapi"' in config
    assert 'model = "gpt-6-astra"' in config
    assert "[projects]" in config
    assert 'e_drive = "E:/work"' in config
    assert 'sandbox = "elevated"' in config
    assert config.count("[windows]") == 1
    assert "[model_providers.minkingapi]" in config

    fresh = tmp_path / "fresh"
    apply_codex(
        home=fresh,
        profile_root=profile,
        api_key="sk-ts-test",
        model="gpt-6-astra",
        catalog={"models": [{"slug": "gpt-6-astra"}]},
        public_base=PUBLIC,
    )
    fresh_config = (fresh / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert '[windows]\nsandbox = "unelevated"' in fresh_config.replace("\r\n", "\n")


def test_managed_skill_source_uses_meipass_when_frozen(tmp_path, monkeypatch):
    bundled = tmp_path / "_internal" / "minking_desktop" / "skills" / MANAGED_SKILL_NAME
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("# bundled\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert managed_skill_source() == bundled


def test_apply_harness_writes_full_picker_catalog(tmp_path):
    home = tmp_path / "home"
    _seed(home)
    catalog = {
        "models": [
            {
                "slug": "gpt-6-astra",
                "display_name": "GPT-6 Astra",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 1,
                "context_window": 1000000,
                "supported_reasoning_levels": [{"effort": "medium", "description": "balanced"}],
                "experimental_supported_tools": ["image_generation"],
                "input_modalities": ["text", "image"],
                "shell_type": "unified_exec",
                "comp_hash": "ts-catalog",
            }
        ]
    }
    result = apply_harness(
        "codex",
        home=home,
        profile_root=tmp_path / "profiles",
        api_key="sk-ts-test",
        public_base=PUBLIC,
        models=["gpt-6-astra"],
        catalog=catalog,
    )
    assert result["ok"] is True
    assert result["session_sync"] is True
    assert "配置已写入" in result["message"]
    assert "会话文件没有移动" in result["message"]
    written = json.loads((home / ".codex" / "codex-models.json").read_text(encoding="utf-8"))
    assert written == catalog
    config = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "%userprofile%" not in config.lower()
    assert f'model_catalog_json = "{(home / ".codex" / "codex-models.json").as_posix()}"' in config


def test_apply_workbuddy_replaces_stub_with_luna_shaped_models(tmp_path):
    home = tmp_path / "home"
    _seed(home)
    stub = json.dumps(
        {
            "models": [
                {
                    "id": "gpt-5.6-luna",
                    "name": "gpt-5.6-luna",
                    "vendor": "user",
                    "url": "http://10.1.102.36:8787/v1",
                    "apiKey": "sk-ts-old",
                    "supportsToolCall": True,
                    "supportsImages": True,
                    "supportsReasoning": True,
                },
                {
                    "id": "minking-default",
                    "name": "MinKing AI",
                    "vendor": "MinKing",
                    "url": "https://portal.example/v1/chat/completions",
                    "apiKey": "sk-ts-old",
                },
                {"id": "local-ollama", "name": "Local"},
            ],
            "availableModels": ["minking-default"],
        }
    )
    for name in (".workbuddy", ".codebuddy"):
        (home / name / "models.json").write_text(stub, encoding="utf-8")
    apply_workbuddy(
        home=home,
        profile_root=tmp_path / "profiles",
        api_key="sk-ts-new",
        public_base=PUBLIC,
        models=["gpt-5.6-luna", "grok-4.6"],
    )
    for name in (".workbuddy", ".codebuddy"):
        payload = json.loads((home / name / "models.json").read_text(encoding="utf-8"))
        ids = [item["id"] for item in payload["models"]]
        assert ids == ["local-ollama", "gpt-5.6-luna", "grok-4.6"]
        luna = payload["models"][1]
        assert luna["url"] == "https://portal.example/v1"
        assert luna["apiKey"] == "sk-ts-new"
        assert luna["supportsReasoning"] is True
        assert "availableModels" not in payload


def test_apply_workbuddy_writes_both_even_if_only_codebuddy_exists(tmp_path):
    home = tmp_path / "home"
    codebuddy = home / ".codebuddy"
    codebuddy.mkdir(parents=True)
    (codebuddy / "models.json").write_text(
        json.dumps({"models": [{"id": "local-ollama", "name": "Local"}], "availableModels": ["local-ollama"]}),
        encoding="utf-8",
    )
    apply_workbuddy(
        home=home,
        profile_root=tmp_path / "profiles",
        api_key="sk-ts-new",
        public_base=PUBLIC,
        models=["gpt-5.6-luna"],
    )
    for name in (".workbuddy", ".codebuddy"):
        payload = json.loads((home / name / "models.json").read_text(encoding="utf-8"))
        assert any(item.get("id") == "gpt-5.6-luna" for item in payload["models"])
    restored = restore_harness(
        next(item for item in harness_catalog(public_base=PUBLIC) if item["id"] == "workbuddy"),
        home=home,
        profile_root=tmp_path / "profiles",
    )
    assert restored["ok"] is True
    assert json.loads((codebuddy / "models.json").read_text(encoding="utf-8"))["models"][0]["id"] == "local-ollama"
    assert not (home / ".workbuddy" / "models.json").exists()


def test_local_codex_catalog_keeps_official_windows_and_fills_server_shape():
    from minking_desktop.harness import local_codex_catalog_entry

    bare = local_codex_catalog_entry("codex/gpt-6-astra", None, priority=1)
    assert bare["slug"] == "codex/gpt-6-astra"
    assert bare["display_name"]
    assert bare["context_window"] == 2_560_000
    assert bare["max_context_window"] == 2_560_000
    grok = local_codex_catalog_entry("grok/grok-4.7", None, priority=3)
    assert grok["context_window"] == 50_000_000
    assert grok["max_context_window"] == 50_000_000
    assert bare["supported_reasoning_levels"]
    assert bare["shell_type"] == "unified_exec"
    assert bare["use_responses_lite"] is False
    official = local_codex_catalog_entry(
        "codex/gpt-6-astra",
        {
            "context_window": 272000,
            "max_context_window": 872000,
            "use_responses_lite": True,
            "comp_hash": "3000",
            "slug": "should-not-replace",
        },
        priority=2,
    )
    assert official["slug"] == "codex/gpt-6-astra"
    assert official["context_window"] == 272000
    assert official["max_context_window"] == 872000
    assert official["use_responses_lite"] is True
    assert official["comp_hash"] == "3000"
    assert official["supported_reasoning_levels"]


def test_packed_catalog_ignores_slug_only_objects():
    packed = packed_codex_catalog({"models": [{"slug": "gpt-6-astra"}]}, ["gpt-6-astra", "grok-4.6"])
    assert packed == {"models": [{"slug": "gpt-6-astra"}, {"slug": "grok-4.6"}]}


def _rollout(provider: str, thread_id: str) -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": thread_id, "model_provider": provider, "cwd": "/work"},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "turn_context",
                    "payload": {"model": "gpt-5", "model_provider": provider},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {"type": "message", "content": [{"text": f"model_provider stays {provider}"}]},
                },
                ensure_ascii=False,
            ),
            "",
        ]
    )


def _thread_db(path: Path, rows: list[tuple[str, str]]) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE threads (id TEXT, model_provider TEXT)")
    connection.executemany("INSERT INTO threads (id, model_provider) VALUES (?, ?)", rows)
    connection.commit()
    connection.close()


def _providers_in(codex: Path) -> list[str]:
    found: list[str] = []
    for path in (codex / "sessions").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            payload = item.get("payload") if isinstance(item, dict) else None
            if item.get("type") in {"session_meta", "turn_context"} and isinstance(payload, dict):
                found.append(str(payload.get("model_provider")))
            elif item.get("type") == "response_item" and isinstance(payload, dict):
                found.append(payload["content"][0]["text"])
    import sqlite3

    connection = sqlite3.connect(codex / "state_5.sqlite")
    found.extend(row[0] for row in connection.execute("SELECT model_provider FROM threads ORDER BY id"))
    connection.close()
    index = codex / "session_index.jsonl"
    if index.is_file():
        for line in index.read_text(encoding="utf-8").splitlines():
            if line.strip():
                found.append(str(json.loads(line).get("model_provider")))
    return found


def test_codex_sessions_follow_the_active_route_without_leaving_home(tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text(
        'model_provider = "other-relay"\n\n[model_providers.other-relay]\nbase_url = "https://relay.example/v1"\nwire_api = "responses"\n',
        encoding="utf-8",
    )
    (codex / "auth.json").write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"sk-old"}\n', encoding="utf-8")
    (codex / "sessions").mkdir()
    (codex / "sessions" / "old.jsonl").write_text(_rollout("openai", "old"), encoding="utf-8")
    (codex / "sessions" / "relay.jsonl").write_text(_rollout("other-relay", "relay"), encoding="utf-8")
    (codex / "session_index.jsonl").write_text(
        '{"id":"old","model_provider":"openai"}\n{"id":"relay","model_provider":"other-relay"}\n',
        encoding="utf-8",
    )
    _thread_db(codex / "state_5.sqlite", [("old", "openai"), ("relay", "other-relay")])
    (codex / "skills").mkdir()
    (codex / "skills" / "keep.txt").write_text("shared", encoding="utf-8")

    apply_codex(
        home=home,
        profile_root=profile,
        api_key="sk-ts-test",
        model="gpt-6-astra",
        catalog={"models": [{"slug": "gpt-6-astra", "display_name": "GPT-6 Astra"}]},
        public_base=PUBLIC,
    )
    assert (codex / "sessions" / "old.jsonl").is_file()
    assert (codex / "sessions" / "relay.jsonl").is_file()
    assert (codex / "state_5.sqlite").is_file()
    assert (codex / "skills" / "keep.txt").read_text(encoding="utf-8") == "shared"
    assert set(_providers_in(codex)) == {
        "openai",
        "other-relay",
        "model_provider stays openai",
        "model_provider stays other-relay",
    }
    assert not (profile / "codex" / "route-version.json").exists()

    synced = sync_codex_sessions(home=home, profile_root=profile)
    assert synced["auth_mode"] == "apikey"
    assert synced["swapped"] is False
    assert "没有移动" in synced["message"]
    assert not (profile / "codex" / "session-chatgpt").exists()
    assert not (profile / "codex" / "session-apikey").exists()
    assert (codex / "sessions" / "old.jsonl").is_file()
    assert (codex / "skills" / "keep.txt").read_text(encoding="utf-8") == "shared"
    assert set(_providers_in(codex)) == {
        "minkingapi",
        "model_provider stays openai",
        "model_provider stays other-relay",
    }
    version = json.loads((profile / "codex" / "route-version.json").read_text(encoding="utf-8"))
    assert version["provider"] == "minkingapi"
    assert version["base_url"] == PUBLIC
    assert version["auth_mode"] == "apikey"
    assert version["wire_api"] == "responses"
    assert "sk-ts-test" not in json.dumps(version)
    assert "sk-old" not in json.dumps(version)

    recipe = next(item for item in harness_catalog(public_base=PUBLIC) if item["id"] == "codex")
    restored = restore_harness(recipe, home=home, profile_root=profile)
    assert restored["ok"] is True
    assert (codex / "sessions" / "old.jsonl").is_file()
    assert set(_providers_in(codex)) == {
        "minkingapi",
        "model_provider stays openai",
        "model_provider stays other-relay",
    }
    sync_codex_sessions(home=home, profile_root=profile)
    assert (codex / "state_5.sqlite").is_file()
    assert (codex / "sessions" / "old.jsonl").is_file()
    assert (codex / "sessions" / "relay.jsonl").is_file()
    assert not (profile / "codex" / "session-chatgpt").exists()
    assert not (profile / "codex" / "session-apikey").exists()
    assert set(_providers_in(codex)) == {
        "other-relay",
        "model_provider stays openai",
        "model_provider stays other-relay",
    }
    restored_version = json.loads((profile / "codex" / "route-version.json").read_text(encoding="utf-8"))
    assert restored_version["provider"] == "other-relay"
    assert restored_version["base_url"] == "https://relay.example/v1"
    assert restored_version["auth_mode"] == "apikey"
    assert "sk-old" not in json.dumps(restored_version)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_codex_apikey_sync_rewrites_route_fields_in_place(tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    config = (
        'model_provider = "minkingapi"\n'
        'model = "grok-4.6"\n'
        'model_catalog_json = "codex-models.json"\n'
        "\n"
        "[model_providers.minkingapi]\n"
        'base_url = "https://portal.example/v1"\n'
        'wire_api = "responses"\n'
    )
    auth = '{"OPENAI_API_KEY":"sk-test"}\n'
    (codex / "config.toml").write_text(config, encoding="utf-8")
    (codex / "auth.json").write_text(auth, encoding="utf-8")
    (codex / "codex-models.json").write_text(
        json.dumps(
            {"models": [{"slug": "gpt-6-astra"}, {"slug": "gemini-3.8-flash"}, {"slug": "custom-model"}]}
        ),
        encoding="utf-8",
    )
    rollout = codex / "sessions" / "2026" / "09" / "23" / "rollout-api.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "thread",
                    "model_provider": "openai",
                    "developer_instructions": {"model": "leave-dev", "model_provider": "leave-provider"},
                    "base_instructions": {
                        "text": "developer_instructions model_provider stays",
                        "provenance": {"type": "model", "model": "grok-4.7", "note": "keep-note"},
                    },
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "model": "gpt-6-astra",
                    "model_provider": "openai",
                    "collaboration_mode": {"settings": {"model": "grok-4.7", "mode": "plan"}},
                },
            },
            {
                "type": "turn_context",
                "payload": {"model": "gemini-3.8-flash-high", "model_provider": "openai"},
            },
            {
                "type": "turn_context",
                "payload": {"model": "grok-4.7", "model_provider": "openai"},
            },
            {
                "type": "turn_context",
                "payload": {"model": "custom-model-extra-low", "model_provider": "openai"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "message": "model_provider stays in the event text",
                    "thread_settings": {
                        "model": "gemini-3.8-flash-high",
                        "model_provider_id": "openai",
                        "collaboration_mode": {"settings": {"model": "gpt-6-astra"}},
                    },
                },
            },
            {
                "type": "world_state",
                "payload": {
                    "state": {
                        "model": "grok-4.7",
                        "developer_instructions": {"model": "do-not-touch-world"},
                        "collaboration_mode": {
                            "model": "gemini-3.8-flash-high",
                            "settings": {"model": "gpt-6-astra", "mode": "plan"},
                        },
                    }
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "content": [{"text": "model_provider stays in the message"}],
                    "developer_instructions": "model_provider stays in the message",
                    "arguments": {"model": "grok-4.7", "model_provider": "openai"},
                    "tools": [{"name": "schema", "model": "tool-schema-model"}],
                },
            },
        ],
    )
    (codex / "session_index.jsonl").write_text(
        '{"id":"thread","model_provider":"openai","model":"grok-4.7"}\n{"id":"plain","cwd":"/work"}\n',
        encoding="utf-8",
    )
    (codex / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "electron-persisted-atom-state": json.dumps(
                    {
                        "composer-recent-model-configurations-v1": [
                            {"model": "grok-4.7", "effort": "high"},
                            {"model": "gpt-6-astra"},
                        ],
                        "mcp-catalog": [{"model": "do-not-touch", "model_provider": "leave"}],
                    },
                    ensure_ascii=False,
                ),
                "tools": [{"model": "schema-model"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    import sqlite3

    database = codex / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE threads (id TEXT, model_provider TEXT, model TEXT)")
    connection.execute("CREATE TABLE notes (id TEXT, model_provider TEXT, model TEXT)")
    connection.executemany(
        "INSERT INTO threads (id, model_provider, model) VALUES (?, ?, ?)",
        [("keep", "openai", "gpt-6-astra"), ("drop", "openai", "grok-4.7"), ("effort", "other", "gemini-3.8-flash-high")],
    )
    connection.execute(
        "INSERT INTO notes (id, model_provider, model) VALUES (?, ?, ?)",
        ("n", "openai", "grok-4.7"),
    )
    connection.commit()
    connection.close()

    result = sync_codex_sessions(home=home, profile_root=profile)
    assert result["ok"] is True
    assert result["auth_mode"] == "apikey"
    assert result["swapped"] is False
    assert result["message"] == "现有对话已跟随当前登录的服务商和模型，会话文件没有移动。"
    assert rollout.is_file()
    assert not (profile / "codex" / "session-chatgpt").exists()
    assert not (profile / "codex" / "session-apikey").exists()
    assert (codex / "config.toml").read_text(encoding="utf-8") == config
    assert (codex / "auth.json").read_text(encoding="utf-8") == auth
    rows = _read_jsonl(rollout)
    meta = rows[0]["payload"]
    assert meta["model_provider"] == "minkingapi"
    assert meta["developer_instructions"] == {"model": "leave-dev", "model_provider": "leave-provider"}
    assert meta["base_instructions"]["text"] == "developer_instructions model_provider stays"
    assert meta["base_instructions"]["provenance"] == {
        "type": "model",
        "model": "grok-4.6",
        "note": "keep-note",
    }
    kept = rows[1]["payload"]
    assert kept["model"] == "gpt-6-astra"
    assert kept["model_provider"] == "minkingapi"
    assert kept["collaboration_mode"]["settings"]["model"] == "grok-4.6"
    assert kept["collaboration_mode"]["settings"]["mode"] == "plan"
    assert rows[2]["payload"]["model"] == "gemini-3.8-flash"
    assert rows[3]["payload"]["model"] == "grok-4.6"
    assert rows[4]["payload"]["model"] == "custom-model"
    settings = rows[5]["payload"]["thread_settings"]
    assert settings["model"] == "gemini-3.8-flash"
    assert settings["model_provider_id"] == "minkingapi"
    assert settings["collaboration_mode"]["settings"]["model"] == "gpt-6-astra"
    assert rows[5]["payload"]["message"] == "model_provider stays in the event text"
    state = rows[6]["payload"]["state"]
    assert state["model"] == "grok-4.6"
    assert state["developer_instructions"] == {"model": "do-not-touch-world"}
    assert state["collaboration_mode"]["model"] == "gemini-3.8-flash"
    assert state["collaboration_mode"]["settings"]["model"] == "gpt-6-astra"
    message = rows[7]["payload"]
    assert message["content"][0]["text"] == "model_provider stays in the message"
    assert message["developer_instructions"] == "model_provider stays in the message"
    assert message["arguments"] == {"model": "grok-4.7", "model_provider": "openai"}
    assert message["tools"] == [{"name": "schema", "model": "tool-schema-model"}]
    index = _read_jsonl(codex / "session_index.jsonl")
    assert index[0]["model_provider"] == "minkingapi"
    assert index[0]["model"] == "grok-4.7"
    assert "model_provider" not in index[1]
    global_state = json.loads((codex / ".codex-global-state.json").read_text(encoding="utf-8"))
    atom = json.loads(global_state["electron-persisted-atom-state"])
    assert atom["composer-recent-model-configurations-v1"][0] == {"model": "grok-4.6", "effort": "high"}
    assert atom["composer-recent-model-configurations-v1"][1]["model"] == "gpt-6-astra"
    assert atom["mcp-catalog"] == [{"model": "do-not-touch", "model_provider": "leave"}]
    assert global_state["tools"] == [{"model": "schema-model"}]
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT model_provider, model FROM threads ORDER BY id").fetchall() == [
        ("minkingapi", "grok-4.6"),
        ("minkingapi", "gemini-3.8-flash"),
        ("minkingapi", "gpt-6-astra"),
    ]
    assert connection.execute("SELECT model_provider, model FROM notes").fetchall() == [("minkingapi", "grok-4.7")]
    connection.close()

    again = sync_codex_sessions(home=home, profile_root=profile)
    assert again["swapped"] is False
    assert rollout.is_file()
    assert _read_jsonl(rollout)[1]["payload"]["model"] == "gpt-6-astra"
    assert not (profile / "codex" / "session-apikey").exists()


def test_codex_apikey_sync_uses_first_catalog_slug_when_config_has_no_model(tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text('model_provider = "minkingapi"\n', encoding="utf-8")
    (codex / "auth.json").write_text('{"OPENAI_API_KEY":"sk-test"}\n', encoding="utf-8")
    (codex / "codex-models.json").write_text(
        json.dumps({"models": [{"slug": "gemini-3.8-flash"}, {"slug": "gpt-6-astra"}]}),
        encoding="utf-8",
    )
    rollout = codex / "sessions" / "fallback.jsonl"
    _write_jsonl(rollout, [{"type": "turn_context", "payload": {"model": "grok-4.7", "model_provider": "openai"}}])

    sync_codex_sessions(home=home, profile_root=profile)
    payload = _read_jsonl(rollout)[0]["payload"]
    assert payload["model"] == "gemini-3.8-flash"
    assert payload["model_provider"] == "minkingapi"
    assert rollout.is_file()


def test_codex_chatgpt_sync_keeps_official_models_and_rewrites_minking_slugs(tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    config = 'model = "gpt-6-sol"\n'
    auth = '{"tokens":{"access_token":"official"}}\n'
    (codex / "config.toml").write_text(config, encoding="utf-8")
    (codex / "auth.json").write_text(auth, encoding="utf-8")
    rollout = codex / "sessions" / "2026" / "09" / "23" / "rollout-login.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "thread",
                    "model_provider": "minkingapi",
                    "developer_instructions": "model_provider stays in the message",
                },
            },
            {"type": "turn_context", "payload": {"model": "gpt-6-astra", "model_provider": "minkingapi"}},
            {"type": "turn_context", "payload": {"model": "grok-4.7", "model_provider": "minkingapi"}},
            {
                "type": "event_msg",
                "payload": {
                    "thread_settings": {
                        "model": "grok-4.7",
                        "model_provider_id": "minkingapi",
                        "collaboration_mode": {"settings": {"model": "gpt-6-astra"}},
                    }
                },
            },
            {
                "type": "world_state",
                "payload": {"state": {"model": "claude-opus", "collaboration_mode": {"model": "grok-4.7"}}},
            },
            {
                "type": "response_item",
                "payload": {"type": "message", "content": [{"text": "model_provider stays in the message"}]},
            },
        ],
    )

    result = sync_codex_sessions(home=home, profile_root=profile)
    assert result["auth_mode"] == "chatgpt"
    assert result["swapped"] is False
    assert "没有移动" in result["message"]
    assert "另一套" not in result["message"]
    assert rollout.is_file()
    assert not (profile / "codex" / "session-chatgpt").exists()
    assert not (profile / "codex" / "session-apikey").exists()
    assert (codex / "config.toml").read_text(encoding="utf-8") == config
    assert (codex / "auth.json").read_text(encoding="utf-8") == auth
    rows = _read_jsonl(rollout)
    assert rows[0]["payload"]["model_provider"] == "openai"
    assert rows[0]["payload"]["developer_instructions"] == "model_provider stays in the message"
    assert rows[1]["payload"] == {"model": "gpt-6-astra", "model_provider": "openai"}
    assert rows[2]["payload"] == {"model": "gpt-6-sol", "model_provider": "openai"}
    settings = rows[3]["payload"]["thread_settings"]
    assert settings["model"] == "gpt-6-sol"
    assert settings["model_provider_id"] == "openai"
    assert settings["collaboration_mode"]["settings"]["model"] == "gpt-6-astra"
    assert rows[4]["payload"]["state"]["model"] == "gpt-6-sol"
    assert rows[4]["payload"]["state"]["collaboration_mode"]["model"] == "gpt-6-sol"
    assert rows[5]["payload"]["content"][0]["text"] == "model_provider stays in the message"

    (codex / "config.toml").write_text('model = "claude-opus"\n', encoding="utf-8")
    rows[2]["payload"]["model"] = "gemini-3-pro"
    _write_jsonl(rollout, rows)
    again = sync_codex_sessions(home=home, profile_root=profile)
    assert again["swapped"] is False
    assert rollout.is_file()
    rewritten = _read_jsonl(rollout)
    assert rewritten[1]["payload"]["model"] == "gpt-6-astra"
    assert rewritten[2]["payload"]["model"] == "gpt-5.6-sol"
    assert rewritten[2]["payload"]["model_provider"] == "openai"
    assert not (profile / "codex" / "session-chatgpt").exists()


def test_codex_sync_copies_missing_rollout_from_the_other_bucket(tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "profiles"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")
    (codex / "auth.json").write_text('{"tokens":{"access_token":"official"}}\n', encoding="utf-8")
    (profile / "codex").mkdir(parents=True)
    (profile / "codex" / "session-active.json").write_text('{"auth_mode":"chatgpt"}\n', encoding="utf-8")
    rollout_name = "rollout-2026-09-22T11-00-30-01a0c70e-cbc5-7611-a2af-b6d28cb473bd.jsonl"
    expected = codex / "sessions" / "2026" / "09" / "22" / rollout_name
    parked = profile / "codex" / "session-apikey" / "sessions" / "2026" / "09" / "22" / rollout_name
    parked.parent.mkdir(parents=True)
    parked.write_text("rollout-body", encoding="utf-8")
    import sqlite3

    database = codex / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, model_provider TEXT)")
    connection.execute(
        "INSERT INTO threads (id, rollout_path, model_provider) VALUES (?, ?, ?)",
        ("01a0c70e-cbc5-7611-a2af-b6d28cb473bd", str(expected), "openai"),
    )
    connection.commit()
    connection.close()

    result = sync_codex_sessions(home=home, profile_root=profile)
    assert result["restored"] == 1
    assert expected.read_text(encoding="utf-8") == "rollout-body"
    assert parked.read_text(encoding="utf-8") == "rollout-body"


def test_apply_dispatcher_unknown_refuses(tmp_path):
    result = apply_harness(
        "trae",
        home=tmp_path / "home",
        profile_root=tmp_path / "profiles",
        api_key="sk-ts-test",
        public_base=PUBLIC,
    )
    assert result["ok"] is False
    assert "未知工具" in result["error"]
