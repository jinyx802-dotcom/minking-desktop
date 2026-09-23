"""Single-instance socket plus optional minking:// protocol registration."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from minking_desktop.paths import instance_path

HOST = "127.0.0.1"
PROTOCOL = "minking"


def parse_deep_link(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    raw = value.strip()
    if not raw.lower().startswith("minking:"):
        return None
    parsed = urlparse(raw)
    action = (parsed.netloc or parsed.path or "open").strip("/").lower() or "open"
    query = parse_qs(parsed.query)
    base = (query.get("base") or [""])[0].strip()
    email = (query.get("email") or [""])[0].strip()
    result = {"url": raw, "action": action}
    if base:
        result["base"] = base
    if email:
        result["email"] = email
    return result


def register_protocol(command: str) -> None:
    if os.name != "nt":
        return
    import winreg

    root = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\minking")
    try:
        winreg.SetValueEx(root, None, 0, winreg.REG_SZ, "URL:MinKing Protocol")
        winreg.SetValueEx(root, "URL Protocol", 0, winreg.REG_SZ, "")
        command_key = winreg.CreateKey(root, r"shell\open\command")
        try:
            winreg.SetValueEx(command_key, None, 0, winreg.REG_SZ, command)
        finally:
            winreg.CloseKey(command_key)
        icon_key = winreg.CreateKey(root, "DefaultIcon")
        try:
            winreg.SetValueEx(icon_key, None, 0, winreg.REG_SZ, command.split(" ")[0].strip('"'))
        finally:
            winreg.CloseKey(icon_key)
    finally:
        winreg.CloseKey(root)


def launch_command() -> str:
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return f'"{exe}" "%1"'
    script = str(Path(__file__).resolve().parent.parent / "run.py")
    return f'"{exe}" "{script}" "%1"'


class InstanceLock:
    def __init__(self, *, appdata: Path | None, on_message: Callable[[dict], None]) -> None:
        self.path = instance_path(appdata=appdata)
        self.on_message = on_message
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._hwnd = 0

    def read_info(self) -> dict | None:
        if not self.path.is_file():
            return None
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return info if isinstance(info, dict) else None

    def _write_info(self, **extra: object) -> None:
        info = self.read_info() or {}
        info.update(extra)
        info["pid"] = os.getpid()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(info), encoding="utf-8")

    def update_hwnd(self, hwnd: int) -> None:
        self._hwnd = int(hwnd or 0)
        info = self.read_info()
        if not info or int(info.get("pid") or 0) != os.getpid():
            return
        try:
            port = int(info["port"])
        except (KeyError, TypeError, ValueError):
            return
        self._write_info(hwnd=self._hwnd, port=port)

    def _alive(self, port: int) -> bool:
        try:
            with socket.create_connection((HOST, port), timeout=0.4) as sock:
                sock.sendall(b'{"cmd":"ping"}\n')
                sock.recv(64)
            return True
        except OSError:
            return False

    def offer(self, payload: dict) -> bool:
        info = self.read_info()
        if not info:
            return False
        try:
            port = int(info["port"])
        except (KeyError, TypeError, ValueError):
            return False
        if not self._alive(port):
            return False
        try:
            with socket.create_connection((HOST, port), timeout=1.5) as sock:
                sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                sock.settimeout(1.5)
                try:
                    sock.recv(256)
                except OSError:
                    pass
            return True
        except OSError:
            return False

    def serve(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, 0))
        server.listen(4)
        self._server = server
        port = int(server.getsockname()[1])
        self._write_info(port=port, hwnd=self._hwnd)

        def loop() -> None:
            while True:
                try:
                    conn, _addr = server.accept()
                except OSError:
                    return
                with conn:
                    try:
                        raw = conn.recv(4096).decode("utf-8", errors="replace")
                        message = json.loads(raw.splitlines()[0])
                    except (OSError, json.JSONDecodeError, IndexError):
                        continue
                    try:
                        conn.sendall(b'{"ok":true}\n')
                    except OSError:
                        pass
                    if message.get("cmd") == "ping":
                        continue
                    self.on_message(message)

        self._thread = threading.Thread(target=loop, daemon=True, name="minking-instance")
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self.path.is_file():
            try:
                info = json.loads(self.path.read_text(encoding="utf-8"))
                if int(info.get("pid") or 0) == os.getpid():
                    self.path.unlink()
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
