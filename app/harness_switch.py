"""Local snapshot/restore for coding harness configs. Never uploads official tokens."""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.desktop_recipes import harness_catalog, public_v1_url
from app.providers.codex_catalog import codex_catalog_toml_path

ZCODE_PROVIDER_ID = "minkingapi"
GROK_PROVIDER_ID = "minkingapi"


def _zcode_slugs(models: list[str] | None, model: str | None) -> list[str]:
    slugs: list[str] = []
    for item in models or []:
        text = item.strip() if isinstance(item, str) else ""
        if text and text not in slugs:
            slugs.append(text)
    if not slugs and isinstance(model, str) and model.strip():
        slugs.append(model.strip())
    return slugs or ["gpt-5.5"]


def _zcode_base_url(item: dict[str, Any]) -> str:
    config = item.get("config")
    api = config.get("api") if isinstance(config, dict) else {}
    return str(api.get("baseUrl") or "").rstrip("/") if isinstance(api, dict) else ""


def _zcode_ours(item: Any, *, base_url: str) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("providerId") == ZCODE_PROVIDER_ID:
        return True
    return bool(base_url) and _zcode_base_url(item) == base_url.rstrip("/")


def _zcode_rule(slug: str, *, api_key: str, base_url: str) -> dict[str, Any]:
    return {
        "providerId": slug,
        "providerName": slug,
        "enabled": True,
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": api_key},
            "api": {"type": "openai-chat-completions", "baseUrl": base_url},
            "personalModelIds": [slug],
            "modelOrder": [slug],
        },
    }


def _merge_zcode_payload(
    payload: dict[str, Any],
    *,
    api_key: str,
    base_url: str,
    slugs: list[str],
) -> dict[str, Any]:
    payload["schemaVersion"] = 1 if payload.get("schemaVersion") in (None, 1) else payload.get("schemaVersion")
    config = payload.get("config")
    if not isinstance(config, dict):
        config = {}
        payload["config"] = config
    rules_wrap = config.get("providerConfigRules")
    if not isinstance(rules_wrap, dict):
        rules_wrap = {}
        config["providerConfigRules"] = rules_wrap
    existing = [item for item in rules_wrap.get("providerRules") or [] if isinstance(item, dict)]
    removed_ids = {item.get("providerId") for item in existing if _zcode_ours(item, base_url=base_url)}
    removed_ids.add(ZCODE_PROVIDER_ID)
    slug_set = set(slugs)
    kept = [
        item
        for item in existing
        if not _zcode_ours(item, base_url=base_url) and item.get("providerId") not in slug_set
    ]
    rules = kept + [_zcode_rule(slug, api_key=api_key, base_url=base_url) for slug in slugs]
    rules_wrap["providerRules"] = rules
    model_rules = config.get("modelConfigRules")
    if not isinstance(model_rules, dict):
        model_rules = {"providerModelRules": [], "manualProviderModelRules": []}
        config["modelConfigRules"] = model_rules
    provider_model_rules = [
        item
        for item in model_rules.get("providerModelRules") or []
        if isinstance(item, dict) and item.get("providerId") not in removed_ids and item.get("providerId") not in slug_set
    ]
    provider_model_rules.extend(
        {"modelId": slug, "providerId": slug, "config": {"enabled": True}} for slug in slugs
    )
    model_rules["providerModelRules"] = provider_model_rules
    if not isinstance(model_rules.get("manualProviderModelRules"), list):
        model_rules["manualProviderModelRules"] = []
    order = [
        item
        for item in config.get("providerOrder") or []
        if isinstance(item, str) and item not in removed_ids and item not in slug_set
    ]
    order.extend(slug for slug in slugs if slug not in order)
    config["providerOrder"] = order
    current = config.get("defaultModelSelection")
    keep_default = (
        isinstance(current, dict)
        and current.get("providerId") in slug_set
        and current.get("modelId") in slug_set
    ) or (
        isinstance(current, dict)
        and current.get("providerId") not in removed_ids
        and current.get("providerId") not in slug_set
    )
    if not keep_default:
        config["defaultModelSelection"] = {"providerId": slugs[0], "modelId": slugs[0]}
    return payload
BACKUP_KEEP = 20
_GROK_MANAGED = re.compile(
    r"\n?# >>> minking managed block.*?# <<< minking managed block\n?",
    re.DOTALL,
)
_MEDIA_BLOCK = re.compile(
    r"\n?# >>> minking media block.*?# <<< minking media block\n?",
    re.DOTALL,
)
MANAGED_SKILL_NAME = "minking-media"
MANAGED_SKILL_MARKER = ".minking-managed"
_CODEX_MEDIA_BLOCK = (
    "# >>> minking media block\n"
    "## MinKing media\n"
    "This is the primary image/video path while MinKing is connected. "
    "It overrides the Image Generation & Editing section and the bundled imagegen skill.\n"
    "If the user asks to generate or edit images or videos, immediately run "
    "`%USERPROFILE%/.codex/skills/minking-media/scripts/minking-media.ps1` "
    "(Windows) or `minking-media.sh`. The image/video prompt must be English; "
    "translate Chinese requests first. Do not ask for API keys. "
    "Do not use WorkBuddy ImageGen/VideoGen, Codex `scripts/image_gen.py`, "
    "or a first-try `POST /v1/responses` with `image_generation`. "
    "Use native `image_gen` only if that tool is actually in the current session tool list. "
    "See that SKILL.md.\n"
    "# <<< minking media block\n"
)
_HARNESS_SKILL_KEYS = {
    "codex": ("codex",),
    "grok": ("grok",),
    "claude_code": ("claude_code",),
    "workbuddy": ("workbuddy", "codebuddy"),
}


def expand_user_path(value: str, *, home: Path) -> Path:
    replaced = value.replace("%USERPROFILE%", str(home))
    replaced = replaced.replace("%LOCALAPPDATA%", str(home / "AppData" / "Local"))
    replaced = replaced.replace("%APPDATA%", str(home / "AppData" / "Roaming"))
    replaced = replaced.replace("$HOME", str(home))
    return Path(os.path.expandvars(replaced))


def _recipe(harness_id: str, *, messages_ready: bool = True) -> dict[str, Any]:
    return next(item for item in harness_catalog(messages_ready=messages_ready) if item["id"] == harness_id)


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def detect_harnesses(*, home: Path, messages_ready: bool = True) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for recipe in harness_catalog(messages_ready=messages_ready):
        detect = recipe.get("detect") or {}
        paths = detect.get("windows") if os.name == "nt" else detect.get("posix")
        if not isinstance(paths, list):
            paths = detect.get("posix") or []
        matches = [str(expand_user_path(item, home=home)) for item in paths if expand_user_path(item, home=home).exists()]
        found.append(
            {
                "id": recipe["id"],
                "display_name": recipe["display_name"],
                "one_click": recipe["one_click"],
                "installed": bool(matches),
                "paths": matches,
            }
        )
    return found


def _live_dir(recipe: dict[str, Any], *, home: Path) -> Path:
    mapping = recipe.get("live_dir") or {}
    raw = mapping.get("windows") if os.name == "nt" else mapping.get("posix")
    if not isinstance(raw, str):
        raw = mapping.get("posix") or mapping.get("windows")
    if not isinstance(raw, str):
        raise ValueError(f"recipe {recipe.get('id')} has no live_dir")
    return expand_user_path(raw, home=home)


def _os_path_list(mapping: dict[str, Any] | None) -> list[str]:
    if not isinstance(mapping, dict):
        return []
    raws = mapping.get("windows") if os.name == "nt" else mapping.get("posix")
    if not isinstance(raws, list):
        raws = mapping.get("posix") or mapping.get("windows") or []
    return [item for item in raws if isinstance(item, str)]


def _recipe_dirs(recipe: dict[str, Any], *, home: Path) -> list[Path]:
    dirs = [_live_dir(recipe, home=home)]
    for item in _os_path_list(recipe.get("sync_dirs")):
        path = expand_user_path(item, home=home)
        if path not in dirs:
            dirs.append(path)
    return dirs


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def managed_skill_source() -> Path:
    here = Path(__file__).resolve().parent
    desktop = here.parent / "clients" / "minking-desktop" / "minking_desktop" / "skills" / MANAGED_SKILL_NAME
    if (desktop / "SKILL.md").is_file():
        return desktop
    return here / "client_skills" / MANAGED_SKILL_NAME


def _skill_targets(home: Path) -> list[tuple[str, Path]]:
    root = Path(home)
    return [
        ("codex", root / ".codex" / "skills" / MANAGED_SKILL_NAME),
        ("grok", root / ".grok" / "skills" / MANAGED_SKILL_NAME),
        ("claude_code", root / ".claude" / "skills" / MANAGED_SKILL_NAME),
        ("cursor", root / ".cursor" / "skills" / MANAGED_SKILL_NAME),
        ("workbuddy", root / ".workbuddy" / "skills" / MANAGED_SKILL_NAME),
        ("codebuddy", root / ".codebuddy" / "skills" / MANAGED_SKILL_NAME),
    ]


def _install_skill_dir(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns(MANAGED_SKILL_MARKER, "__pycache__"),
    )
    (dest / MANAGED_SKILL_MARKER).write_text("1\n", encoding="utf-8")


def _upsert_codex_media_block(home: Path) -> None:
    path = Path(home) / ".codex" / "AGENTS.md"
    if not path.parent.is_dir():
        return
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    cleaned = _MEDIA_BLOCK.sub("\n", current).rstrip()
    text = (cleaned + "\n\n" + _CODEX_MEDIA_BLOCK).strip() + "\n"
    path.write_text(text, encoding="utf-8")


def _remove_codex_media_block(home: Path) -> None:
    path = Path(home) / ".codex" / "AGENTS.md"
    if not path.is_file():
        return
    current = path.read_text(encoding="utf-8")
    cleaned = _MEDIA_BLOCK.sub("\n", current).strip()
    if cleaned:
        path.write_text(cleaned + "\n", encoding="utf-8")
    else:
        path.unlink()


def sync_managed_skill(*, home: Path, extra: tuple[str, ...] = ()) -> None:
    source = managed_skill_source()
    if not (source / "SKILL.md").is_file():
        return
    wanted = set(extra)
    if "workbuddy" in wanted:
        wanted.add("codebuddy")
    for key, dest in _skill_targets(home):
        app_dir = dest.parent.parent
        if key in wanted or app_dir.is_dir():
            _install_skill_dir(source, dest)
    if (Path(home) / ".codex").is_dir() or "codex" in wanted:
        _upsert_codex_media_block(home)


def remove_managed_skill(*, home: Path, harness_id: str | None = None) -> None:
    keys = _HARNESS_SKILL_KEYS.get(harness_id) if harness_id else None
    for key, dest in _skill_targets(home):
        if keys is not None and key not in keys:
            continue
        if (dest / MANAGED_SKILL_MARKER).is_file():
            shutil.rmtree(dest, ignore_errors=True)
    if harness_id in {None, "codex"}:
        _remove_codex_media_block(home)


def snapshot_harness(recipe: dict[str, Any], *, home: Path, profile_root: Path) -> Path:
    snap = profile_root / str(recipe["id"]) / "official"
    snap.mkdir(parents=True, exist_ok=True)
    marker = snap / ".complete"
    if marker.exists():
        return snap
    names = recipe.get("snapshot_files") or []
    if recipe.get("sync_dirs"):
        for directory in _recipe_dirs(recipe, home=home):
            for name in names:
                source = directory / name
                if source.is_file():
                    _copy_file(source, snap / directory.name / name)
    else:
        live = _live_dir(recipe, home=home)
        for name in names:
            source = live / name
            if source.is_file():
                _copy_file(source, snap / name)
    marker.write_text("1", encoding="utf-8")
    return snap


def backup_before_sync(recipe: dict[str, Any], *, home: Path, profile_root: Path) -> Path | None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = profile_root / str(recipe["id"]) / "backups"
    dest = root / stamp
    suffix = 2
    while dest.exists():
        dest = root / f"{stamp}-{suffix}"
        suffix += 1
    dest.mkdir(parents=True, exist_ok=True)
    names = recipe.get("snapshot_files") or []
    copied = 0
    if recipe.get("sync_dirs"):
        for directory in _recipe_dirs(recipe, home=home):
            for name in names:
                source = directory / name
                if source.is_file():
                    _copy_file(source, dest / directory.name / name)
                    copied += 1
    else:
        live = _live_dir(recipe, home=home)
        for name in names:
            source = live / name
            if source.is_file():
                _copy_file(source, dest / name)
                copied += 1
    if copied == 0:
        shutil.rmtree(dest, ignore_errors=True)
        return None
    _prune_backups(root, keep=BACKUP_KEEP)
    return dest


def _prune_backups(root: Path, keep: int) -> None:
    if keep < 1 or not root.is_dir():
        return
    dirs = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name)
    for old in dirs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def restore_harness(recipe: dict[str, Any], *, home: Path, profile_root: Path) -> None:
    snap = profile_root / str(recipe["id"]) / "official"
    if not (snap / ".complete").exists():
        return
    names = recipe.get("snapshot_files") or []
    if recipe.get("sync_dirs"):
        dirs = _recipe_dirs(recipe, home=home)
        primary = dirs[0].name
        for directory in dirs:
            for name in names:
                nested = snap / directory.name / name
                legacy = snap / name
                target = directory / name
                if nested.is_file():
                    _copy_file(nested, target)
                elif (
                    directory.name != primary
                    and legacy.is_file()
                    and not (snap / primary / name).is_file()
                ):
                    _copy_file(legacy, target)
                elif target.exists():
                    target.unlink()
        remove_managed_skill(home=home, harness_id=str(recipe["id"]))
        return
    live = _live_dir(recipe, home=home)
    live.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = snap / name
        target = live / name
        if source.is_file():
            _copy_file(source, target)
        elif target.exists():
            target.unlink()
    remove_managed_skill(home=home, harness_id=str(recipe["id"]))


def _toml_table_name(line: str) -> str | None:
    stripped = line.strip()
    if len(stripped) < 3 or not stripped.startswith("[") or stripped.startswith("[[") or not stripped.endswith("]"):
        return None
    return stripped[1:-1].strip().strip('"')


def _toml_assignment_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("[") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def _merge_codex_config(existing: str, assignments: dict[str, str], provider_lines: list[str]) -> str:
    """Update the MinKing provider and keep the user's other Codex tables."""
    lines = existing.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    blocks: list[tuple[str | None, list[str]]] = []
    name: str | None = None
    body: list[str] = []
    for line in lines:
        header = _toml_table_name(line)
        if header is None:
            body.append(line)
            continue
        blocks.append((name, body))
        name = header
        body = [line]
    blocks.append((name, body))
    if blocks[0][0] is not None:
        blocks.insert(0, (None, []))

    seen: set[str] = set()
    rewritten: list[str] = []
    for line in blocks[0][1]:
        key = _toml_assignment_key(line)
        if key in assignments:
            if key not in seen:
                rewritten.append(f"{key} = {assignments[key]}")
                seen.add(key)
            continue
        rewritten.append(line)
    missing = [f"{key} = {assignments[key]}" for key in assignments if key not in seen]
    if missing:
        if rewritten and rewritten[-1].strip():
            rewritten.append("")
        rewritten.extend(missing)
    blocks[0] = (None, rewritten)

    provider_name = "model_providers.minkingapi"
    provider_block = [f"[{provider_name}]", *provider_lines]
    merged: list[tuple[str | None, list[str]]] = []
    replaced = False
    has_windows = False
    for block_name, block_body in blocks:
        if block_name == provider_name:
            merged.append((block_name, provider_block))
            replaced = True
            continue
        if block_name == "windows":
            has_windows = True
            if not any(_toml_assignment_key(item) == "sandbox" for item in block_body):
                block_body = [block_body[0], 'sandbox = "unelevated"', *block_body[1:]]
        merged.append((block_name, block_body))
    if not replaced:
        merged.append((provider_name, ["", *provider_block]))
    if not has_windows:
        merged.append(("windows", ["", "[windows]", 'sandbox = "unelevated"']))
    output: list[str] = []
    for _, block_body in merged:
        output.extend(block_body)
    return "\n".join(output).strip() + "\n"


def _codex_config(model: str, base_url: str, *, home: Path, existing: str = "") -> str:
    from app.providers.codex_catalog import context_window_for

    quoted_model = json.dumps(model, ensure_ascii=False)
    quoted_base = json.dumps(base_url)
    quoted_catalog = json.dumps(codex_catalog_toml_path(home), ensure_ascii=False)
    window = context_window_for(model)
    return _merge_codex_config(
        existing,
        {
            "model_provider": '"minkingapi"',
            "model": quoted_model,
            "review_model": quoted_model,
            "model_context_window": str(window),
            "disable_response_storage": "true",
            "model_catalog_json": quoted_catalog,
        },
        [
            'name = "MinKing API Composite"',
            f"base_url = {quoted_base}",
            'env_key = "OPENAI_API_KEY"',
            'wire_api = "responses"',
            "requires_openai_auth = false",
            "supports_websockets = false",
        ],
    )


def apply_codex(*, home: Path, profile_root: Path, api_key: str, model: str, catalog: dict[str, Any]) -> None:
    recipe = _recipe("codex")
    snapshot_harness(recipe, home=home, profile_root=profile_root)
    backup_before_sync(recipe, home=home, profile_root=profile_root)
    live = _live_dir(recipe, home=home)
    live.mkdir(parents=True, exist_ok=True)
    base = public_v1_url()
    config_path = live / "config.toml"
    existing_config = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    config_path.write_text(_codex_config(model, base, home=home, existing=existing_config), encoding="utf-8")
    (live / "codex-models.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    (live / ".env").write_text(f"OPENAI_API_KEY={api_key}\nOPENAI_BASE_URL={base}\n", encoding="utf-8")
    (live / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": api_key}, ensure_ascii=False), encoding="utf-8")
    sync_managed_skill(home=home, extra=("codex",))


def _workbuddy_custom_model(slug: str, *, api_key: str, base_url: str) -> dict[str, Any]:
    return {
        "id": slug,
        "name": slug,
        "vendor": "user",
        "url": base_url,
        "apiKey": api_key,
        "supportsToolCall": True,
        "supportsImages": True,
        "supportsReasoning": True,
    }


def _workbuddy_ours(item: dict[str, Any], *, slugs: set[str], base_url: str) -> bool:
    ident = str(item.get("id") or "")
    vendor = str(item.get("vendor") or "")
    url = str(item.get("url") or "").rstrip("/")
    base = base_url.rstrip("/")
    if ident == "minking-default" or ident in slugs or vendor == "MinKing":
        return True
    return url in {base, f"{base}/chat/completions"}


def apply_workbuddy(
    *,
    home: Path,
    profile_root: Path,
    api_key: str,
    model: str | None = None,
    models: list[str] | None = None,
) -> None:
    recipe = _recipe("workbuddy")
    snapshot_harness(recipe, home=home, profile_root=profile_root)
    backup_before_sync(recipe, home=home, profile_root=profile_root)
    slugs = [item for item in (models or []) if isinstance(item, str) and item]
    if not slugs and model:
        slugs = [model]
    if not slugs:
        slugs = ["gpt-5.5"]
    v1 = public_v1_url()
    ours = set(slugs)
    for directory in _recipe_dirs(recipe, home=home):
        path = directory / "models.json"
        payload = _load_json_object(path)
        kept = [
            item
            for item in payload.get("models") or []
            if isinstance(item, dict) and not _workbuddy_ours(item, slugs=ours, base_url=v1)
        ]
        injected = [_workbuddy_custom_model(slug, api_key=api_key, base_url=v1) for slug in slugs]
        injected_ids = {
            str(item.get("id") or "")
            for item in payload.get("models") or []
            if isinstance(item, dict) and _workbuddy_ours(item, slugs=ours, base_url=v1)
        }
        foreign: list[str] = []
        seen: set[str] = set()
        for item in payload.get("availableModels") or []:
            if not isinstance(item, str) or not item or item in seen:
                continue
            seen.add(item)
            if item in ours or item in injected_ids or item == "minking-default":
                continue
            foreign.append(item)
        payload["models"] = kept + injected
        if foreign:
            payload["availableModels"] = foreign + [item for item in slugs if item not in foreign]
        else:
            payload.pop("availableModels", None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_managed_skill(home=home, extra=("workbuddy",))


CLAUDE_TIER_MODELS = {
    "opus": ("gpt-5.6-sol", "GPT-5.6 Sol"),
    "sonnet": ("grok-4.6", "Grok 4.6"),
    "haiku": ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    "fable": ("gpt-6-astra", "GPT-6 Astra"),
}


def _claude_code_env(existing: dict[str, Any] | None, *, api_key: str, base_url: str) -> dict[str, Any]:
    env = dict(existing or {})
    env["ANTHROPIC_API_KEY"] = api_key
    env["ANTHROPIC_BASE_URL"] = base_url
    opus, opus_name = CLAUDE_TIER_MODELS["opus"]
    sonnet, sonnet_name = CLAUDE_TIER_MODELS["sonnet"]
    haiku, haiku_name = CLAUDE_TIER_MODELS["haiku"]
    fable, fable_name = CLAUDE_TIER_MODELS["fable"]
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = opus
    env["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"] = opus_name
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = sonnet
    env["ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"] = sonnet_name
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME"] = haiku_name
    env["ANTHROPIC_DEFAULT_FABLE_MODEL"] = fable
    env["ANTHROPIC_DEFAULT_FABLE_MODEL_NAME"] = fable_name
    env["ANTHROPIC_MODEL"] = sonnet
    env["ANTHROPIC_REASONING_MODEL"] = opus
    return env


def apply_claude_code(*, home: Path, profile_root: Path, api_key: str) -> None:
    recipe = _recipe("claude_code")
    snapshot_harness(recipe, home=home, profile_root=profile_root)
    backup_before_sync(recipe, home=home, profile_root=profile_root)
    live = _live_dir(recipe, home=home)
    live.mkdir(parents=True, exist_ok=True)
    path = live / "settings.json"
    settings_payload = _load_json_object(path)
    env = settings_payload.get("env")
    if not isinstance(env, dict):
        env = {}
    root = public_v1_url()
    base = root[:-3] if root.endswith("/v1") else root
    settings_payload["env"] = _claude_code_env(env, api_key=api_key, base_url=base)
    settings_payload["model"] = "sonnet"
    path.write_text(json.dumps(settings_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_managed_skill(home=home, extra=("claude_code",))


def apply_zcode(
    *,
    home: Path,
    profile_root: Path,
    api_key: str,
    model: str | None = None,
    models: list[str] | None = None,
) -> None:
    recipe = _recipe("zcode")
    snapshot_harness(recipe, home=home, profile_root=profile_root)
    backup_before_sync(recipe, home=home, profile_root=profile_root)
    live = _live_dir(recipe, home=home)
    live.mkdir(parents=True, exist_ok=True)
    path = live / "provider_config.json"
    payload = _load_json_object(path)
    slugs = _zcode_slugs(models, model)
    _merge_zcode_payload(
        payload,
        api_key=api_key,
        base_url=public_v1_url(),
        slugs=slugs,
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _grok_cli_slugs(slugs: list[str]) -> list[str]:
    kept = []
    for slug in slugs:
        name = slug.lower()
        if (
            "imagine-image" in name
            or "imagine-video" in name
            or "hunyuan-image" in name
            or "flash-image" in name
            or name.startswith("gpt-image")
            or name.startswith("hy-image")
        ):
            continue
        kept.append(slug)
    return kept or list(slugs)


def _grok_default_slug(slugs: list[str]) -> str:
    if "grok-4.6" in slugs:
        return "grok-4.6"
    return slugs[0] if slugs else "grok-4.6"


def _set_toml_models_default(text: str, default: str) -> str:
    quoted = json.dumps(default)
    pattern = re.compile(r"(?ms)^(\[models\][^\n]*\n)(.*?)(?=^\[|\Z)")
    match = pattern.search(text)
    if not match:
        suffix = "" if text.endswith("\n") or not text else "\n"
        return text + suffix + f"\n[models]\ndefault = {quoted}\n"
    header, body = match.group(1), match.group(2)
    if re.search(r"(?m)^default\s*=", body):
        body = re.sub(r"(?m)^default\s*=\s*.*$", f"default = {quoted}", body, count=1)
    else:
        body = f"default = {quoted}\n" + body
    return text[: match.start()] + header + body + text[match.end() :]


def grok_managed_block(
    *,
    base_url: str,
    api_key: str,
    slugs: list[str],
    catalog: dict[str, Any] | None = None,
) -> str:
    names: dict[str, str] = {}
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and item.get("slug"):
                names[str(item["slug"])] = str(item.get("display_name") or item["slug"])
    from app.providers.codex_catalog import GROK_CONTEXT_WINDOW

    windows: dict[str, int] = {}
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and item.get("slug") and isinstance(item.get("context_window"), int):
                windows[str(item["slug"])] = item["context_window"]
    grok_window = GROK_CONTEXT_WINDOW
    lines = [
        "# >>> minking managed block",
        f"[model_providers.{GROK_PROVIDER_ID}]",
        f"base_url = {json.dumps(base_url)}",
        'api_backend = "responses"',
        f"api_key = {json.dumps(api_key)}",
        "",
    ]
    for slug in slugs:
        key = f"minking-{slug}"
        display = names.get(slug) or f"MinKing {slug}"
        lines.extend(
            [
                f"[model.{json.dumps(key)}]",
                f"model = {json.dumps(slug)}",
                f"model_provider = {json.dumps(GROK_PROVIDER_ID)}",
                f"name = {json.dumps(display)}",
                f"context_window = {windows.get(slug, grok_window)}",
                "",
            ]
        )
    lines.append("# <<< minking managed block")
    return "\n".join(lines) + "\n"


def merge_grok_config(
    text: str,
    *,
    base_url: str,
    api_key: str,
    slugs: list[str],
    catalog: dict[str, Any] | None = None,
) -> str:
    cleaned = _GROK_MANAGED.sub("\n", text or "").rstrip() + "\n"
    usable = _grok_cli_slugs(slugs)
    if not usable:
        usable = ["grok-4.6"]
    default = f"minking-{_grok_default_slug(usable)}"
    cleaned = _set_toml_models_default(cleaned, default)
    return cleaned.rstrip() + "\n\n" + grok_managed_block(
        base_url=base_url, api_key=api_key, slugs=usable, catalog=catalog
    )


def apply_grok(
    *,
    home: Path,
    profile_root: Path,
    api_key: str,
    models: list[str] | None = None,
    catalog: dict[str, Any] | None = None,
) -> None:
    recipe = _recipe("grok")
    snapshot_harness(recipe, home=home, profile_root=profile_root)
    backup_before_sync(recipe, home=home, profile_root=profile_root)
    live = _live_dir(recipe, home=home)
    live.mkdir(parents=True, exist_ok=True)
    path = live / "config.toml"
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    slugs = [item for item in (models or []) if isinstance(item, str) and item]
    path.write_text(
        merge_grok_config(
            current,
            base_url=public_v1_url(),
            api_key=api_key,
            slugs=slugs,
            catalog=catalog,
        ),
        encoding="utf-8",
    )
    sync_managed_skill(home=home, extra=("grok",))
