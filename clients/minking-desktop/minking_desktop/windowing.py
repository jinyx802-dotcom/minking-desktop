"""Restore a hidden Win32 window from the process that just received a user click."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("minking_desktop")

SW_SHOW = 5
SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
MB_ICONERROR = 0x00000010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
_SAFE_EXE_NAMES = {"minkingai.exe", "python.exe", "pythonw.exe"}


def _user32():
    import ctypes

    return ctypes.windll.user32


def _kernel32():
    import ctypes

    return ctypes.windll.kernel32


def find_window_hwnd(title: str) -> int:
    if os.name != "nt" or not title:
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW(None, title)
    return int(hwnd) if hwnd else 0


def window_pid(hwnd: int) -> int:
    if os.name != "nt" or not hwnd:
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    if not user32.IsWindow(hwnd):
        return 0
    out = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(out))
    return int(out.value)


def hwnd_belongs_to_pid(hwnd: int, pid: int) -> bool:
    if not hwnd or not pid:
        return False
    return window_pid(hwnd) == int(pid)


def native_hwnd(window: Any) -> int:
    native = getattr(window, "native", None)
    if native is None:
        return 0
    handle = getattr(native, "Handle", None)
    if handle is None:
        return 0
    try:
        to_int = getattr(handle, "ToInt64", None) or getattr(handle, "ToInt32", None)
        if callable(to_int):
            return int(to_int())
        return int(handle)
    except Exception:
        return 0


def _window_size(window: Any) -> tuple[int, int]:
    for width_name, height_name in (("width", "height"), ("initial_width", "initial_height")):
        try:
            width = int(getattr(window, width_name, 0) or 0)
            height = int(getattr(window, height_name, 0) or 0)
        except Exception:
            continue
        if width > 0 and height > 0:
            return width, height
    return 0, 0


def _repaint_native_webview(window: Any) -> None:
    """Ask the WinForms WebView2 control to draw again after the form was hidden."""
    native = getattr(window, "native", None)
    if native is None:
        return

    def repaint() -> int:
        browser = getattr(native, "browser", None)
        control = getattr(browser, "webview", None) if browser is not None else None
        if control is not None:
            try:
                core = control.CoreWebView2
                resume = getattr(core, "Resume", None)
                if callable(resume):
                    resume()
            except Exception:
                pass
            try:
                control.Visible = False
                control.Visible = True
            except Exception:
                pass
        invalidate = getattr(native, "Invalidate", None)
        if callable(invalidate):
            try:
                invalidate(True)
            except Exception:
                pass
        return 0

    try:
        if getattr(native, "InvokeRequired", False) and callable(getattr(native, "Invoke", None)):
            from System import Func, Int32

            native.Invoke(Func[Int32](repaint))
        else:
            repaint()
    except Exception:
        logger.info("wake_webview_native_failed")


def wake_webview(window: Any) -> None:
    """WebView2 stays white after the window was hidden until it gets a resize."""
    if window is None:
        return
    _repaint_native_webview(window)
    width, height = _window_size(window)
    resize = getattr(window, "resize", None)
    if callable(resize) and width > 0 and height > 0:
        try:
            resize(width, height + 1)
            resize(width, height)
        except Exception:
            logger.info("wake_webview_resize_failed")
    script = getattr(window, "evaluate_js", None)
    if callable(script):
        try:
            script("void(document.documentElement && document.documentElement.getBoundingClientRect())")
        except Exception:
            logger.info("wake_webview_script_failed")


def restore_window(hwnd: int, pid: int = 0) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    kernel32 = _kernel32()
    hwnd_t = wintypes.HWND(hwnd)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    if not user32.IsWindow(hwnd_t):
        return False
    try:
        user32.ShowWindow(hwnd_t, SW_RESTORE)
        user32.ShowWindow(hwnd_t, SW_SHOW)
        user32.AllowSetForegroundWindow(int(pid or -1))
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        target_tid = user32.GetWindowThreadProcessId(hwnd_t, None)
        current_tid = kernel32.GetCurrentThreadId()
        if fg_tid and fg_tid != current_tid:
            user32.AttachThreadInput(current_tid, fg_tid, True)
        if target_tid and target_tid != current_tid:
            user32.AttachThreadInput(current_tid, target_tid, True)
        user32.BringWindowToTop(hwnd_t)
        user32.SetForegroundWindow(hwnd_t)
        user32.SetWindowPos(
            hwnd_t,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        user32.SetWindowPos(
            hwnd_t,
            HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        user32.RedrawWindow.argtypes = [
            wintypes.HWND,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        user32.RedrawWindow.restype = wintypes.BOOL
        user32.RedrawWindow(hwnd_t, None, None, 0x0001 | 0x0004 | 0x0080 | 0x0100)
        if fg_tid and fg_tid != current_tid:
            user32.AttachThreadInput(current_tid, fg_tid, False)
        if target_tid and target_tid != current_tid:
            user32.AttachThreadInput(current_tid, target_tid, False)
    except Exception:
        logger.info("restore_window_failed hwnd=%s", hwnd)
        return False
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    return bool(user32.IsWindowVisible(hwnd_t) or user32.IsWindow(hwnd_t))


def process_image_path(pid: int) -> str:
    if os.name != "nt" or not pid:
        return ""
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def is_minking_pid(pid: int) -> bool:
    name = Path(process_image_path(pid)).name.lower()
    return name in _SAFE_EXE_NAMES


def pid_is_running(pid: int) -> bool:
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    STILL_ACTIVE = 259
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def terminate_pid(pid: int) -> bool:
    if os.name != "nt" or not pid or pid == os.getpid():
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def steal_stale_instance(
    lock: Any,
    *,
    terminate: Callable[[int], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    info = lock.read_info() if hasattr(lock, "read_info") else None
    info = info or {}
    pid = int(info.get("pid") or 0)
    try:
        lock.offer({"cmd": "quit"})
    except Exception:
        pass
    sleeper(0.35)
    killer = terminate or terminate_pid
    if pid and pid != os.getpid() and pid_is_running(pid) and is_minking_pid(pid):
        logger.info("terminate_stale_instance pid=%s", pid)
        killer(pid)
        sleeper(0.2)
    path = getattr(lock, "path", None)
    if path is not None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def _owned_by_minking(hwnd: int, pid: int) -> bool:
    if not hwnd or not pid or pid == os.getpid():
        return False
    if not is_minking_pid(pid):
        return False
    return hwnd_belongs_to_pid(hwnd, pid)


def _is_windows() -> bool:
    return os.name == "nt"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def handoff_existing_instance(
    *,
    title: str,
    lock: Any,
    payload: dict,
    restore: Callable[[int, int], bool] | None = None,
    terminate: Callable[[int], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Restore a live instance. True means this process should exit."""
    restorer = restore or restore_window

    def try_restore(target_hwnd: int, target_pid: int) -> bool:
        if not _owned_by_minking(target_hwnd, target_pid):
            return False
        return bool(restorer(target_hwnd, target_pid))

    info = lock.read_info() if hasattr(lock, "read_info") else None
    info = info if isinstance(info, dict) else {}
    pid = int(info.get("pid") or 0)
    hwnd = int(info.get("hwnd") or 0)
    if try_restore(hwnd, pid):
        lock.offer(payload)
        return True
    found = find_window_hwnd(title)
    found_pid = window_pid(found) if found else 0
    if try_restore(found, found_pid):
        lock.offer(payload)
        return True
    if not lock.offer(payload):
        return False
    if not _is_windows():
        # Cocoa has no Win32 hwnd. A live socket already showed the window.
        return True
    for _ in range(8):
        sleeper(0.15)
        info = lock.read_info() if hasattr(lock, "read_info") else None
        info = info if isinstance(info, dict) else {}
        pid = int(info.get("pid") or 0)
        hwnd = int(info.get("hwnd") or 0)
        if try_restore(hwnd, pid):
            return True
        found = find_window_hwnd(title)
        found_pid = window_pid(found) if found else 0
        if found_pid == pid and try_restore(found, found_pid):
            return True
    logger.info("stale_instance_no_window")
    steal_stale_instance(lock, terminate=terminate, sleeper=sleeper)
    return False


def webview2_installed() -> bool:
    if os.name != "nt":
        return True
    import winreg

    keys = (
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in keys:
            try:
                with winreg.OpenKey(hive, path) as handle:
                    build, _kind = winreg.QueryValueEx(handle, "pv")
                text = str(build or "0").strip()
                if text and text != "0.0.0.0" and text != "0":
                    return True
            except OSError:
                continue
    return False


def _apple_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", "\\n")
    )


def show_error_dialog(title: str, message: str) -> None:
    if _is_windows():
        try:
            _user32().MessageBoxW(None, message, title, MB_ICONERROR)
        except Exception:
            logger.info("message_box_failed")
        return
    if not _is_macos():
        return
    script = (
        f'display dialog "{_apple_string(message[:800])}" '
        f'with title "{_apple_string(title[:120])}" '
        'buttons {"好"} default button "好" with icon stop'
    )
    try:
        import subprocess

        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    except OSError:
        logger.info("message_box_failed")


def prepare_windows_webview() -> None:
    """Force .NET Framework for pythonnet and give windowed EXEs a stdout/stderr."""
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
    os.environ.setdefault("PYWEBVIEW_GUI", "edgechromium")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")


def load_pythonnet() -> str:
    """Load pythonnet on Windows with netfx before pywebview tries coreclr."""
    if os.name != "nt":
        return ""
    prepare_windows_webview()
    os.environ["PYTHONNET_RUNTIME"] = "netfx"
    import pythonnet

    pythonnet.load("netfx")
    import clr  # noqa: F401

    return "netfx"
