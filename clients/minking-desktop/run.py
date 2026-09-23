"""Launcher so the minking:// protocol can start the client without -m."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPO = ROOT.parents[1]
if (REPO / "app" / "providers").is_dir() and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from minking_desktop.app import run

if __name__ == "__main__":
    raise SystemExit(run())
