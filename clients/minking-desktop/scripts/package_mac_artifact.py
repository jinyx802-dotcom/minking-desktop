"""Zip a built MinKingAI.app with ditto so resource forks survive."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dist" / "MinKingAI.app"
ARCHIVE = ROOT / "artifacts" / "minking-desktop-macos.zip"


def main() -> int:
    if platform.system() != "Darwin":
        raise SystemExit("package_mac_artifact.py runs on macOS")
    if not APP.is_dir():
        raise SystemExit(f"missing {APP}")
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.unlink(missing_ok=True)
    subprocess.run(
        ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(APP), str(ARCHIVE)],
        check=True,
    )
    print(ARCHIVE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
