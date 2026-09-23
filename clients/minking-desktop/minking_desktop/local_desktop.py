"""Native EXE window + tray + an in-process local HTTP service."""
from __future__ import annotations

import http.client
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time

import uvicorn

from minking_desktop import __version__
from minking_desktop.clipboard import copy_text
from minking_desktop.local_api import create_app, local_key
from minking_desktop.official import read_object
from minking_desktop.paths import appdata_root
from minking_desktop.protocol import InstanceLock
from minking_desktop.windowing import (find_window_hwnd, handoff_existing_instance, load_pythonnet,
    native_hwnd, prepare_windows_webview, restore_window, show_error_dialog, wake_webview, webview2_installed)

TITLE = "MinKing AI · 本地模型"
UI = Path(__file__).resolve().parent / "ui" / "local"


class LocalService:
    def __init__(self, *, home=None, appdata=None, api_key=None):
        self.home = home or Path.home()
        self.root = appdata or appdata_root()
        self.key = api_key or local_key(self.root)
        saved = read_object(self.root / "local-desktop.json")
        port = saved.get("port", 18787)
        self.port = port if type(port) is int and 1024 <= port <= 65535 else 18787
        self.server = None
        self.thread = None
        self._socket = None
        self._lock = threading.RLock()

    @property
    def running(self):
        return bool(self.server and self.server.started and self.thread and self.thread.is_alive())

    def start(self):
        with self._lock:
            if self.running:
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", self.port))
            except OSError:
                sock.close()
                raise RuntimeError(f"端口 {self.port} 已被占用，请换一个本地端口") from None
            self._socket = sock
            for name in ("httpx", "httpcore", "transfer_station.errors"):
                logging.getLogger(name).disabled = True
            app = create_app(home=self.home, appdata=self.root, api_key=self.key)
            self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                access_log=False, log_level="critical", log_config=None, timeout_graceful_shutdown=2))
            self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]},
                                           daemon=True, name="minking-local-api")
            self.thread.start()
            deadline = time.monotonic() + 15
            while self.thread.is_alive() and not self.server.started and time.monotonic() < deadline:
                time.sleep(.05)
            if not self.running:
                self.stop()
                raise RuntimeError("本地 HTTP 服务启动失败")

    def stop(self):
        with self._lock:
            if self.server:
                self.server.should_exit = True
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=5)
            if self._socket:
                self._socket.close()
                self._socket = None

    def change_port(self, port):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("端口必须是 1024–65535 之间的整数")
        with self._lock:
            old, was_running = self.port, self.running
            self.stop()
            self.port = port
            try:
                self.start()
            except Exception:
                self.port = old
                if was_running:
                    self.start()
                raise
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / "local-desktop.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"port": port}), encoding="utf-8")
            tmp.replace(path)

    def request(self, path, data=None, method="GET"):
        if not self.running:
            return {"ok": False, "error": "HTTP 服务已暂停，请先启动服务"}
        # Bridge callers can only address this process's own API surface.
        if not isinstance(path, str) or not path.startswith(("/api/", "/v1/")) or ".." in path or "?" in path:
            return {"ok": False, "error": "接口路径无效"}
        if method not in {"GET", "POST", "PUT"}:
            return {"ok": False, "error": "请求方法无效"}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=200)
        try:
            encoded = None if data is None else json.dumps(data).encode("utf-8")
            connection.request(method, path, encoded, {"Authorization": "Bearer " + self.key,
                                                       "Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read(64 * 1024 * 1024 + 1))
            if response.status >= 400:
                return {"ok": False, "error": (result.get("error") or {}).get("message", "本地接口调用失败"),
                        "status": response.status}
            return {"ok": True, "data": result}
        except (OSError, ValueError, http.client.HTTPException):
            return {"ok": False, "error": "本地服务连接失败或请求超时"}
        finally:
            connection.close()


class DesktopBridge:
    """Expose only native actions, never window objects or filesystem access."""
    def __init__(self, service):
        self._service = service
        self._cached = {"accounts": [], "config": {"default_model": "", "enabled_providers": []}}

    def state(self):
        service = self._service
        if service.running:
            result = service.request("/api/bootstrap")
            if result["ok"]:
                self._cached = result["data"]
        return {**self._cached, "api_key": service.key,
                "base_url": f"http://127.0.0.1:{service.port}/v1", "port": service.port,
                "service_running": service.running, "version": __version__}

    def request(self, path, data=None, method="GET"):
        return self._service.request(path, data, method)

    def control(self, action, port=None):
        try:
            if action == "start":
                self._service.start()
            elif action == "stop":
                self._service.stop()
            elif action == "port":
                self._service.change_port(port)
            else:
                return {"ok": False, "error": "未知操作"}
            return {"ok": True, "data": self.state()}
        except (RuntimeError, ValueError, OSError) as exc:
            # Only our public validation messages are returned.
            message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "无法保存本地配置"
            return {"ok": False, "error": message}

    def copy(self, value):
        try:
            if not isinstance(value, str) or len(value) > 1_000_000:
                return {"ok": False, "error": "内容太长，无法复制"}
            copy_text(value)
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "复制失败，请重试"}


def run():
    service = LocalService()
    bridge = DesktopBridge(service)
    window, tray = None, None
    quitting = False

    def show():
        if window:
            hwnd = native_hwnd(window) or find_window_hwnd(TITLE)
            if hwnd:
                restore_window(hwnd, os.getpid())
            window.show()
            window.restore()
            wake_webview(window)

    def quit_app():
        nonlocal quitting
        quitting = True
        if tray:
            tray.stop()
        service.stop()
        if window:
            window.destroy()

    def incoming(message):
        if message.get("cmd") == "quit":
            quit_app()
        else:
            show()

    lock = InstanceLock(appdata=service.root / "local-instance", on_message=incoming)
    if handoff_existing_instance(title=TITLE, lock=lock, payload={"cmd": "show"}):
        return 0
    lock.serve()
    try:
        if os.name == "nt":
            prepare_windows_webview()
            if not webview2_installed():
                raise RuntimeError("请安装 Microsoft Edge WebView2 Runtime 后重新打开客户端")
            load_pythonnet()
        import webview
        try:
            service.start()
        except RuntimeError:
            # Keep the desktop usable so the user can choose an available port.
            pass
        window = webview.create_window(TITLE, str(UI / "index.html"), js_api=bridge,
            width=1240, height=840, min_size=(980, 680), background_color="#F5F7F4")

        def shown():
            hwnd = native_hwnd(window)
            if hwnd:
                lock.update_hwnd(hwnd)

        def closing():
            if quitting or tray is None:
                return True
            threading.Timer(.05, window.hide).start()
            return False

        window.events.shown += shown
        window.events.closing += closing

        def start_tray():
            nonlocal tray
            if os.environ.get("MINKING_NO_TRAY") == "1":
                return
            try:
                import pystray
                from PIL import Image
                with Image.open(UI.parent / "brand-mark.png") as picture:
                    icon = picture.copy()
                tray = pystray.Icon("MinKingLocal", icon, TITLE, menu=pystray.Menu(
                    pystray.MenuItem("打开主窗口", lambda *_: show(), default=True),
                    pystray.MenuItem("退出并停止 HTTP 服务", lambda *_: quit_app())))
                tray.run()
            except Exception:
                tray = None

        threading.Thread(target=start_tray, daemon=True, name="minking-local-tray").start()
        webview.start(gui="edgechromium" if os.name == "nt" else None, debug=False, private_mode=True)
        return 0
    except Exception:
        detail = (
            "客户端启动失败。请重新打开 MinKingAI.app。"
            if sys.platform == "darwin"
            else "客户端启动失败。请检查 WebView2 Runtime 是否已安装，或重新下载完整 EXE。"
        )
        show_error_dialog(TITLE, detail)
        return 1
    finally:
        service.stop()
        if tray:
            tray.stop()
        lock.close()
