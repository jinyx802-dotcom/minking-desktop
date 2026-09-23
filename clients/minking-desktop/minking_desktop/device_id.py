"""Stable computer id for the one-time signup reward.

Virtual network adapters are ignored on purpose. VPN, Hyper-V, WSL, and
Clash TUN interfaces come and go, so a MAC address is not a computer.
Windows uses the installation MachineGuid plus the system-volume serial.
macOS uses the platform UUID. The raw values are hashed before they leave
the machine and are not written to logs.
"""
from __future__ import annotations

import hashlib
import os
import sys


class DeviceIdError(RuntimeError):
    pass


def device_fingerprint() -> str:
    material = _material()
    return hashlib.sha256(b"minking-device-v1\n" + material.encode("utf-8")).hexdigest()


def _material() -> str:
    try:
        if sys.platform == "win32":
            return "win\n" + _windows_guid() + "\n" + _windows_volume()
        if sys.platform == "darwin":
            return "mac\n" + _mac_uuid()
        return "posix\n" + _posix_machine_id()
    except OSError as exc:
        raise DeviceIdError("无法读取这台电脑的标识") from exc


def _windows_guid() -> str:
    import winreg

    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
        value, _kind = winreg.QueryValueEx(key, "MachineGuid")
    text = str(value).strip().lower()
    if not text:
        raise OSError("empty machine guid")
    return text


def _windows_volume() -> str:
    import ctypes

    serial = ctypes.c_uint32()
    root = (os.environ.get("SystemDrive") or "C:") + "\\"
    ok = ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, ctypes.byref(serial), None, None, None, 0)
    if not ok:
        raise OSError("volume serial unavailable")
    return f"{int(serial.value):08x}"


def _mac_uuid() -> str:
    import subprocess

    raw = subprocess.check_output(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        text=True,
        timeout=5,
    )
    for line in raw.splitlines():
        if "IOPlatformUUID" not in line or "=" not in line:
            continue
        text = line.split("=", 1)[1].strip().strip('"').lower()
        if text:
            return text
    raise OSError("platform uuid unavailable")


def _posix_machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        if os.path.isfile(path):
            text = open(path, encoding="utf-8").read().strip().lower()
            if text:
                return text
    raise OSError("machine id unavailable")
