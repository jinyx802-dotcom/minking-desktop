# Runtime hook: runs before the frozen app imports webview/pythonnet.
import os
import sys

if sys.platform == "darwin":
    os.environ.setdefault("PYWEBVIEW_GUI", "cocoa")
elif sys.platform == "win32":
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
    os.environ.setdefault("PYWEBVIEW_GUI", "edgechromium")

if sys.platform == "win32":
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
