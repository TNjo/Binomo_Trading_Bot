"""Entry point — runs the FastAPI dashboard + engine (engine starts via dashboard)."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dashboard.server import main  # noqa: E402


if __name__ == "__main__":
    main()
