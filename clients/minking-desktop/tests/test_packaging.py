from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]


def test_mac_workflow_matches_the_known_packaging_facts():
    workflow = (REPO / ".github" / "workflows" / "build-minking-desktop.yml").read_text(encoding="utf-8")
    spec = (ROOT / "MinKingAI.spec").read_text(encoding="utf-8")
    hook = (ROOT / "hooks" / "pyi_rth_minking.py").read_text(encoding="utf-8")
    assert "runs-on: macos-latest" in workflow
    assert "windows-latest" not in workflow
    assert "workflow_dispatch" in workflow
    sign = (ROOT / "scripts" / "sign_mac_app.sh").read_text(encoding="utf-8")
    entitlements = (ROOT / "entitlements.plist").read_text(encoding="utf-8")
    assert "sign_mac_app.sh" in workflow
    assert "--pack-smoke" in workflow
    assert "codesign --force --deep --sign -" in sign
    assert "notarytool" in sign
    assert "--options runtime" in sign
    assert "SIGN_MODE=self-signed" in sign or "self-signed" in sign
    assert "com.apple.security.network.client" in entitlements
    assert "com.apple.security.network.server" in entitlements
    assert "app-sandbox" not in entitlements
    assert "NSAppTransportSecurity" not in spec
    assert "BUNDLE(" in spec
    assert "cn.minking.desktop" in spec
    assert "certifi" in spec
    assert "PYWEBVIEW_GUI\", \"cocoa\"" in hook
    assert "CFBundleURLSchemes" in spec
    assert "minking" in spec
    clipboard = (ROOT / "minking_desktop" / "clipboard.py").read_text(encoding="utf-8")
    secrets = (ROOT / "minking_desktop" / "secrets.py").read_text(encoding="utf-8")
    assert '["pbcopy"]' in clipboard
    assert "_macos_protect" in secrets
    assert "pythonnet" in workflow
    assert "req-mac.txt" in workflow


def test_mac_appdata_uses_application_support(monkeypatch, tmp_path):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr("minking_desktop.paths._is_macos", lambda: True)
    monkeypatch.setattr("minking_desktop.paths.user_home", lambda home=None: home or tmp_path)
    from minking_desktop.paths import appdata_root, localappdata_root, workbuddy_credential_files

    home = tmp_path / "home"
    assert appdata_root() == tmp_path / "Library" / "Application Support" / "MinKing"
    assert localappdata_root(home=home) == home / "Library" / "Application Support"
    files = workbuddy_credential_files(home=home, localappdata=localappdata_root(home=home))
    assert files[0] == home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
