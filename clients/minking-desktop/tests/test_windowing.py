from __future__ import annotations

import os

from minking_desktop.windowing import handoff_existing_instance, prepare_windows_webview, wake_webview


class FakeLock:
    def __init__(self, info=None, offer_ok=True, path=None):
        self.info = dict(info or {})
        self.offers = []
        self.offer_ok = offer_ok
        self.path = path

    def read_info(self):
        return dict(self.info)

    def offer(self, payload):
        self.offers.append(payload)
        return self.offer_ok


def test_wake_webview_resizes_even_when_live_size_is_zero():
    calls = []

    class Window:
        initial_width = 960
        initial_height = 640

        @property
        def width(self):
            raise RuntimeError("hidden")

        @property
        def height(self):
            raise RuntimeError("hidden")

        def resize(self, width, height):
            calls.append((width, height))

        def evaluate_js(self, script):
            calls.append(script)

    wake_webview(Window())
    assert calls[:2] == [(960, 641), (960, 640)]
    assert isinstance(calls[2], str)


def test_handoff_restores_hwnd_from_instance_json(monkeypatch):
    monkeypatch.setattr("minking_desktop.windowing.hwnd_belongs_to_pid", lambda hwnd, pid: hwnd == 44 and pid == 9)
    monkeypatch.setattr("minking_desktop.windowing.find_window_hwnd", lambda title: 0)
    monkeypatch.setattr("minking_desktop.windowing.window_pid", lambda hwnd: 0)
    monkeypatch.setattr("minking_desktop.windowing.is_minking_pid", lambda pid: pid == 9)
    restored = []
    lock = FakeLock({"pid": 9, "hwnd": 44, "port": 1})
    assert handoff_existing_instance(
        title="MinKing AI",
        lock=lock,
        payload={"cmd": "show"},
        restore=lambda hwnd, pid: restored.append((hwnd, pid)) or True,
        sleeper=lambda _s: None,
    )
    assert restored == [(44, 9)]
    assert lock.offers[0]["cmd"] == "show"


def test_handoff_ignores_explorer_window_with_same_title(monkeypatch):
    monkeypatch.setattr("minking_desktop.windowing.hwnd_belongs_to_pid", lambda hwnd, pid: True)
    monkeypatch.setattr("minking_desktop.windowing.find_window_hwnd", lambda title: 531732)
    monkeypatch.setattr("minking_desktop.windowing.window_pid", lambda hwnd: 10060)
    monkeypatch.setattr("minking_desktop.windowing.is_minking_pid", lambda pid: False)
    restored = []
    lock = FakeLock(offer_ok=False)
    assert (
        handoff_existing_instance(
            title="MinKing AI",
            lock=lock,
            payload={"cmd": "show"},
            restore=lambda hwnd, pid: restored.append((hwnd, pid)) or True,
            sleeper=lambda _s: None,
        )
        is False
    )
    assert restored == []


def test_handoff_returns_false_when_no_instance(monkeypatch):
    monkeypatch.setattr("minking_desktop.windowing.hwnd_belongs_to_pid", lambda hwnd, pid: False)
    monkeypatch.setattr("minking_desktop.windowing.find_window_hwnd", lambda title: 0)
    monkeypatch.setattr("minking_desktop.windowing.window_pid", lambda hwnd: 0)
    monkeypatch.setattr("minking_desktop.windowing.is_minking_pid", lambda pid: False)
    lock = FakeLock(offer_ok=False)
    assert (
        handoff_existing_instance(
            title="MinKing AI",
            lock=lock,
            payload={"cmd": "show"},
            restore=lambda hwnd, pid: False,
            sleeper=lambda _s: None,
        )
        is False
    )


def test_handoff_steals_socket_without_window(monkeypatch, tmp_path):
    monkeypatch.setattr("minking_desktop.windowing.hwnd_belongs_to_pid", lambda hwnd, pid: False)
    monkeypatch.setattr("minking_desktop.windowing.find_window_hwnd", lambda title: 0)
    monkeypatch.setattr("minking_desktop.windowing.window_pid", lambda hwnd: 0)
    monkeypatch.setattr("minking_desktop.windowing.pid_is_running", lambda pid: True)
    monkeypatch.setattr("minking_desktop.windowing.is_minking_pid", lambda pid: True)
    killed = []
    path = tmp_path / "instance.json"
    path.write_text("{}", encoding="utf-8")
    lock = FakeLock({"pid": 12345, "hwnd": 0, "port": 1}, path=path)
    assert (
        handoff_existing_instance(
            title="MinKing AI",
            lock=lock,
            payload={"cmd": "show"},
            restore=lambda hwnd, pid: False,
            terminate=lambda pid: killed.append(pid) or True,
            sleeper=lambda _s: None,
        )
        is False
    )
    assert killed == [12345]
    assert lock.offers[0]["cmd"] == "show"
    assert lock.offers[-1]["cmd"] == "quit"
    assert not path.exists()


def test_handoff_macos_returns_after_socket_offer(monkeypatch, tmp_path):
    monkeypatch.setattr("minking_desktop.windowing._is_windows", lambda: False)
    monkeypatch.setattr("minking_desktop.windowing.hwnd_belongs_to_pid", lambda hwnd, pid: False)
    monkeypatch.setattr("minking_desktop.windowing.find_window_hwnd", lambda title: 0)
    killed = []
    path = tmp_path / "instance.json"
    path.write_text('{"pid": 9}', encoding="utf-8")
    lock = FakeLock({"pid": 9, "hwnd": 0, "port": 1}, path=path)
    assert (
        handoff_existing_instance(
            title="MinKing AI",
            lock=lock,
            payload={"cmd": "show"},
            restore=lambda hwnd, pid: False,
            terminate=lambda pid: killed.append(pid) or True,
            sleeper=lambda _s: None,
        )
        is True
    )
    assert lock.offers == [{"cmd": "show"}]
    assert killed == []
    assert path.is_file()


def test_macos_error_dialog_uses_osascript(monkeypatch):
    monkeypatch.setattr("minking_desktop.windowing._is_windows", lambda: False)
    monkeypatch.setattr("minking_desktop.windowing._is_macos", lambda: True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    from minking_desktop.windowing import show_error_dialog

    show_error_dialog('MinKing "AI"', "line one\nline two")
    assert calls[0][:2] == ["osascript", "-e"]
    assert "display dialog" in calls[0][2]
    assert "MinKingAI.exe" not in calls[0][2]


def test_prepare_windows_webview_sets_netfx(monkeypatch):
    monkeypatch.delenv("PYTHONNET_RUNTIME", raising=False)
    monkeypatch.delenv("PYWEBVIEW_GUI", raising=False)
    prepare_windows_webview()
    assert os.environ["PYTHONNET_RUNTIME"] == "netfx"
    assert os.environ["PYWEBVIEW_GUI"] == "edgechromium"
