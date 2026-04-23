from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR") or ROOT)
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "trades.db"
LOG_PATH = DATA_DIR / "bot.log"


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


BINOMO_EMAIL = _str("BINOMO_EMAIL")
BINOMO_PASSWORD = _str("BINOMO_PASSWORD")

ACCOUNT_TYPE = _str("ACCOUNT_TYPE", "demo").lower()

ASSET = _str("ASSET", "Crypto IDX")

BASE_AMOUNT = _float("BASE_AMOUNT", 1.0)
MAX_MARTINGALE = _int("MAX_MARTINGALE", 2)
MARTINGALE_MULTIPLIER = _float("MARTINGALE_MULTIPLIER", 2.0)

PAYOUT_RATIO = _float("PAYOUT_RATIO", 0.8)

TIMEZONE = _str("TIMEZONE", "Asia/Colombo")

DASHBOARD_USER = _str("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = _str("DASHBOARD_PASSWORD", "")
DASHBOARD_HOST = _str("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = _int("PORT", _int("DASHBOARD_PORT", 8000))
