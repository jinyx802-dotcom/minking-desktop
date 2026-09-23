from __future__ import annotations

import time

from minking_desktop.protocol import InstanceLock, parse_deep_link


def test_parse_import_link():
    parsed = parse_deep_link("minking://import")
    assert parsed is not None
    assert parsed["action"] == "import"
    assert parse_deep_link("https://example.com") is None
    assert parse_deep_link("minking://open")["action"] == "open"
    imported = parse_deep_link("minking://import?base=https%3A%2F%2Fportal.example%2Fv1&email=ada%40example.com")
    assert imported["action"] == "import"
    assert imported["base"] == "https://portal.example/v1"
    assert imported["email"] == "ada@example.com"
    assert "sk-" not in imported["url"] or "sk-ts-" not in imported.get("base", "")


def test_instance_lock_offer_ack_and_hwnd(tmp_path):
    seen: list[dict] = []
    lock = InstanceLock(appdata=tmp_path, on_message=seen.append)
    assert lock.read_info() is None
    assert lock.offer({"cmd": "show"}) is False
    lock.serve()
    info = lock.read_info()
    assert info is not None
    assert int(info["port"]) > 0
    other = InstanceLock(appdata=tmp_path, on_message=lambda _m: None)
    assert other.offer({"cmd": "show"}) is True
    deadline = time.time() + 2
    while time.time() < deadline and not seen:
        time.sleep(0.05)
    assert seen[0]["cmd"] == "show"
    lock.update_hwnd(4242)
    assert lock.read_info()["hwnd"] == 4242
    lock.close()
    assert lock.read_info() is None
