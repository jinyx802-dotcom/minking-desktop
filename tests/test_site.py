from __future__ import annotations

from pathlib import Path

from app import __version__


TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "site.html"


def test_home_is_marketing_site_not_admin_redirect(client):
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200
    assert home.headers.get("location") in {None, ""}
    assert "text/html" in home.headers.get("content-type", "")
    assert "MinKing" in home.text
    assert "下载" in home.text
    assert "/admin" not in (home.headers.get("location") or "")
    assert f"/static/site.css?v={__version__}" in home.text
    assert "登录工作台" in home.text
    assert "安装包尚未发布" in home.text


def test_admin_login_page_still_served(client):
    page = client.get("/admin", follow_redirects=False)
    assert page.status_code == 200
    assert "MinKing AI" in page.text
    assert "进入后台" in page.text
    assert page.text.count(f"服务版本 v{__version__}") >= 2


def test_public_pages_show_service_version_not_only_codex(client):
    home = client.get("/", follow_redirects=False)
    assert f"服务版本 v{__version__}" in home.text
    portal = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "portal.html").read_text(encoding="utf-8")
    assert "服务版本 v{{ version }}" in portal
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["version"] == __version__


def test_v1_site_and_download_pages(client):
    site = client.get("/v1/site", follow_redirects=False)
    assert site.status_code == 200
    assert "MinKing" in site.text
    assert "下载" in site.text
    assert f"/v1/static/site.css?v={__version__}" in site.text
    assert 'href="/v1/portal"' in site.text

    download = client.get("/download", follow_redirects=False)
    assert download.status_code == 200
    assert 'data-focus="download"' in download.text
    assert "安装包尚未发布" in download.text

    v1_download = client.get("/v1/download", follow_redirects=False)
    assert v1_download.status_code == 200
    assert "MinKing" in v1_download.text
    assert f"/v1/static/site.js?v={__version__}" in v1_download.text


def test_missing_exe_is_404_without_traceback(client):
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 200
    assert "安装包尚未发布" in page.text

    missing = client.get("/download/MinKingAI.exe", follow_redirects=False)
    assert missing.status_code == 404
    assert "Traceback" not in missing.text
    assert "traceback" not in missing.text.lower()
    content_type = missing.headers.get("content-type", "")
    assert "json" in content_type or "html" in content_type
    assert missing.json()["error"]["code"] == "not_found"

    missing_sha = client.get("/download/MinKingAI.exe.sha256", follow_redirects=False)
    assert missing_sha.status_code == 404
    assert "Traceback" not in missing_sha.text

    denied = client.get("/download/.env", follow_redirects=False)
    assert denied.status_code == 404
    assert "Traceback" not in denied.text


def test_site_html_has_no_transfer_station_string():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "Transfer Station" not in text
    assert "transfer-station" not in text
    assert "龙雨轩辉" not in text
    assert "MinKing" in text
    assert "下载" in text
    assert "无需安装" in text
    assert "MinKingAI.zip" not in text


def test_published_exe_and_sha256(client, tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir(parents=True, exist_ok=True)
    payload = b"MZ-minking-onefile"
    digest = "a" * 64
    (folder / "MinKingAI.exe").write_bytes(payload)
    (folder / "MinKingAI.exe.sha256").write_text(f"{digest}  MinKingAI.exe\n", encoding="utf-8")

    page = client.get("/", follow_redirects=False)
    assert page.status_code == 200
    assert "安装包尚未发布" not in page.text
    assert "/download/MinKingAI.exe" in page.text
    assert "无需安装" in page.text
    assert digest in page.text

    exe_resp = client.get("/download/MinKingAI.exe", follow_redirects=False)
    assert exe_resp.status_code == 200
    assert exe_resp.content == payload
    assert "attachment" in exe_resp.headers.get("content-disposition", "")

    v1_exe = client.get("/v1/download/MinKingAI.exe", follow_redirects=False)
    assert v1_exe.status_code == 200
    assert v1_exe.content == payload

    sha = client.get("/download/MinKingAI.exe.sha256", follow_redirects=False)
    assert sha.status_code == 200
    assert digest.encode() in sha.content
