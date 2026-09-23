import io
import zipfile

from app.client_skills_pack import (
    BUNDLED_SKILLS_ROOT,
    install_skill_zip,
    list_client_skills,
    resolve_skill_dir,
    restore_uploaded_skill,
    zip_client_skill,
)
from app.codex_gateway import GatewayError
from app.config import settings
from conftest import ADMIN_HEADERS


def _zip_bytes(*pairs: tuple[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in pairs:
            archive.writestr(name, text)
    return buffer.getvalue()


def test_list_and_zip_minking_media():
    skills = list_client_skills()
    names = {item["name"] for item in skills}
    assert "minking-media" in names
    media = next(item for item in skills if item["name"] == "minking-media")
    payload, digest = zip_client_skill("minking-media")
    assert digest == media["sha256"]
    assert payload[:2] == b"PK"
    assert media["source"] == "bundled"


def test_skill_markdown_omits_openai_base_url():
    text = (BUNDLED_SKILLS_ROOT / "minking-media" / "SKILL.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "openai_base_url" not in lowered
    assert "openai_api_key" not in lowered
    assert "authorization: bearer" not in lowered
    assert ".codex/.env" not in lowered


def test_resolve_skill_dir_rejects_bad_names():
    for name in ("../secrets", "a/b", "", "Not Valid", "minking-media/../x"):
        try:
            resolve_skill_dir(name)
            raise AssertionError(name)
        except GatewayError as exc:
            assert exc.status == 404
            assert exc.code == "skill_not_found"


def test_upload_override_wins_then_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    original = next(item for item in list_client_skills() if item["name"] == "minking-media")
    blob = _zip_bytes(
        ("minking-media/SKILL.md", "---\nname: minking-media\n---\n# from-upload\n"),
        ("minking-media/scripts/ok.sh", "echo ok\n"),
    )
    installed = install_skill_zip(blob)
    assert installed["source"] == "uploaded"
    assert installed["bundled"] is True
    listed = next(item for item in list_client_skills() if item["name"] == "minking-media")
    assert listed["source"] == "uploaded"
    assert listed["sha256"] == installed["sha256"]
    assert listed["sha256"] != original["sha256"]
    text = (resolve_skill_dir("minking-media") / "SKILL.md").read_text(encoding="utf-8")
    assert "# from-upload" in text
    restored = restore_uploaded_skill("minking-media")
    assert restored["source"] == "bundled"
    assert next(item for item in list_client_skills() if item["name"] == "minking-media")["sha256"] == original["sha256"]


def test_upload_rejects_zip_slip():
    blob = _zip_bytes(("../evil.md", "nope\n"))
    try:
        install_skill_zip(blob, name="minking-media")
        raise AssertionError("expected invalid zip")
    except GatewayError as exc:
        assert exc.status == 400
        assert exc.code == "invalid_skill_zip"


def test_admin_upload_list_download_restore(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    listed = client.get("/admin/api/client-skills", headers=ADMIN_HEADERS)
    assert listed.status_code == 200, listed.text
    names = {item["name"] for item in listed.json()["skills"]}
    assert "minking-media" in names
    blob = _zip_bytes(("SKILL.md", "---\nname: minking-media\n---\n# admin-upload\n"))
    uploaded = client.post(
        "/admin/api/client-skills",
        files={"file": ("minking-media.zip", blob, "application/zip")},
        headers=ADMIN_HEADERS,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["skill"]["source"] == "uploaded"
    zipped = client.get("/admin/api/client-skills/minking-media", headers=ADMIN_HEADERS)
    assert zipped.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        assert archive.read("SKILL.md").decode("utf-8").startswith("---")
        assert b"admin-upload" in archive.read("SKILL.md")
    restored = client.delete("/admin/api/client-skills/minking-media", headers=ADMIN_HEADERS)
    assert restored.status_code == 200, restored.text
    assert restored.json()["skill"]["source"] == "bundled"


def test_admin_can_publish_desktop_exe(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    missing = client.get("/admin/api/desktop-package", headers=ADMIN_HEADERS)
    assert missing.status_code == 200
    assert missing.json()["available"] is False
    rejected = client.post(
        "/admin/api/desktop-package",
        files={"file": ("notes.txt", b"not-an-exe", "application/octet-stream")},
        headers=ADMIN_HEADERS,
    )
    assert rejected.status_code == 400
    payload = b"MZ" + b"minking-desktop"
    uploaded = client.post(
        "/admin/api/desktop-package",
        files={"file": ("MinKingAI.exe", payload, "application/vnd.microsoft.portable-executable")},
        headers=ADMIN_HEADERS,
    )
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()["package"]
    assert body["available"] is True
    assert body["bytes"] == len(payload)
    downloaded = client.get("/download/MinKingAI.exe")
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    page = client.get("/admin", follow_redirects=False)
    assert "desktop-package-form" in page.text
