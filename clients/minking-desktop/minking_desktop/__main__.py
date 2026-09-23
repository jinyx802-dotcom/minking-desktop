import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[3]
if (repo / "app" / "providers").is_dir() and str(repo) not in sys.path:
    sys.path.insert(0, str(repo))

from minking_desktop.app import run

raise SystemExit(run())
