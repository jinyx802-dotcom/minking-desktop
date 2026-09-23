# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all
from webview.__pyinstaller import get_hook_dirs as webview_hook_dirs


def _conda_ssl_binaries():
    """Conda's _ssl.pyd needs libssl/libcrypto from Library\\bin. PyInstaller misses them."""
    import sys

    names = []
    try:
        import pefile
    except ImportError:
        pefile = None
    dll_dirs = [
        Path(sys.base_prefix) / "DLLs",
        Path(sys.prefix) / "DLLs",
    ]
    if pefile is not None:
        for dll_dir in dll_dirs:
            for pyd_name in ("_ssl.pyd", "_hashlib.pyd"):
                pyd = dll_dir / pyd_name
                if not pyd.is_file():
                    continue
                pe = pefile.PE(str(pyd))
                if not hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
                    continue
                for entry in pe.DIRECTORY_ENTRY_IMPORT:
                    dll = entry.dll.decode("ascii", "replace")
                    lower = dll.lower()
                    if lower.startswith("libssl") or lower.startswith("libcrypto"):
                        names.append(dll)
    if not names:
        names = ["libssl-3-x64.dll", "libcrypto-3-x64.dll", "libssl-3.dll", "libcrypto-3.dll"]
    roots = [
        Path(sys.base_prefix) / "Library" / "bin",
        Path(sys.base_prefix) / "DLLs",
        Path(sys.prefix) / "Library" / "bin",
    ]
    found = []
    seen = set()
    for root in roots:
        for name in names:
            path = root / name
            key = name.lower()
            if path.is_file() and key not in seen:
                found.append((str(path), "."))
                seen.add(key)
    if not found:
        raise SystemExit(
            "MinKingAI.spec: libssl/libcrypto DLLs were not found next to conda Python. "
            "The frozen EXE would fail import ssl on machines without Miniconda."
        )
    return found


datas = [('minking_desktop/ui', 'minking_desktop/ui'), ('minking_desktop/skills', 'minking_desktop/skills')]
is_windows = sys.platform == "win32"
is_mac = sys.platform == "darwin"
if is_windows:
    from pythonnet._pyinstaller import get_hook_dirs as pythonnet_hook_dirs

    binaries = list(_conda_ssl_binaries())
    hookspath = pythonnet_hook_dirs() + webview_hook_dirs()
    hiddenimports = [
        'pystray',
        'PIL',
        'webview',
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
        'webview.platforms.win32',
        'webview.state',
        'webview.models',
        'pythonnet',
        'clr',
        'clr_loader',
        'bottle',
        'proxy_tools',
        'typing_extensions',
        'cffi',
        'ssl',
        'certifi',
    ]
    collect_packages = ('webview', 'pystray', 'pythonnet', 'clr_loader', 'cffi', 'certifi')
else:
    binaries = []
    hookspath = webview_hook_dirs()
    hiddenimports = [
        'pystray',
        'PIL',
        'webview',
        'webview.platforms.cocoa',
        'webview.state',
        'webview.models',
        'bottle',
        'proxy_tools',
        'certifi',
        'ssl',
    ]
    collect_packages = ('webview', 'pystray', 'certifi')
for package in collect_packages:
    tmp_ret = collect_all(package)
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]


a = Analysis(
    ['run.py'],
    pathex=['.', '../..'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=hookspath,
    hooksconfig={},
    runtime_hooks=['hooks/pyi_rth_minking.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if is_mac:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='MinKingAI',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name='MinKingAI',
    )
    app = BUNDLE(
        coll,
        name='MinKingAI.app',
        icon=None,
        bundle_identifier='cn.minking.desktop',
        info_plist={
            'CFBundleName': 'MinKing AI',
            'CFBundleDisplayName': 'MinKing AI',
            'NSHighResolutionCapable': True,
            'CFBundleURLTypes': [
                {
                    'CFBundleURLName': 'MinKing Protocol',
                    'CFBundleURLSchemes': ['minking'],
                }
            ],
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='MinKingAI',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=['minking_desktop/ui/favicon.ico'],
    )
