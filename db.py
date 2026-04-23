from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_time TEXT NOT NULL,          -- ISO8601 with tz offset (user's zone)
    direction TEXT NOT NULL,            -- CALL | PUT
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|won|lost|skipped|error
    martingale_level INTEGER DEFAULT 0, -- how many martingales taken so far
    won_at_level INTEGER,               -- null if not won; 0=original, 1=M1, 2=M2
    total_pnl REAL DEFAULT 0.0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(signal_time);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    level INTEGER NOT NULL,             -- 0 original, 1 M1, 2 M2
    direction TEXT NOT NULL,
    amount REAL NOT NULL,
    asset TEXT NOT NULL,
    open_time TEXT NOT NULL,            -- when we asked Binomo to place it
    expiry_time TEXT NOT NULL,
    binomo_trade_id TEXT,
    result TEXT DEFAULT 'pending',      -- pending|win|loss|draw|unknown|error
    payout REAL DEFAULT 0.0,
    error TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades(signal_id);
CREATE INDEX IF NOT EXISTS idx_trades_result ON trades(result);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        with conn:
            conn.executescript(SCHEMA)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
            if "starting_amount" not in cols:
                conn.execute("ALTER TABLE signals ADD COLUMN starting_amount REAL")
    finally:
        conn.close()


@contextmanager
def conn_ctx() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def db_log(level: str, message: str) -> None:
    with conn_ctx() as conn:
        with conn:
            conn.execute(
                "INSERT INTO logs (timestamp, level, message) VALUES (?, ?, ?)",
                (datetime.utcnow().isoformat(timespec="seconds"), level, message),
            )


def kv_get(key: str, default: str | None = None) -> str | None:
    with conn_ctx() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with conn_ctx() as conn:
        with conn:
            conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def get_trade_settings() -> dict:
    """Live trade-sizing settings. Falls back to env defaults in config.py."""
    import config
    base = kv_get("base_amount")
    mg = kv_get("max_martingale")
    mult = kv_get("martingale_multiplier")
    recovery = kv_get("loss_recovery")
    recovery_base = kv_get("recovery_base")
    payout = kv_get("payout_ratio")
    return {
        "base_amount": float(base) if base else float(config.BASE_AMOUNT),
        "max_martingale": int(mg) if mg else int(config.MAX_MARTINGALE),
        "martingale_multiplier": float(mult) if mult else float(config.MARTINGALE_MULTIPLIER),
        "loss_recovery": (recovery == "1"),
        "recovery_base": float(recovery_base) if recovery_base else 0.0,
        "payout_ratio": float(payout) if payout else float(config.PAYOUT_RATIO),
    }


def save_trade_settings(d: dict) -> dict:
    allowed = {"base_amount", "max_martingale", "martingale_multiplier", "payout_ratio"}
    for k, v in d.items():
        if k in allowed and v is not None and v != "":
            kv_set(k, str(v))
    if "loss_recovery" in d:
        kv_set("loss_recovery", "1" if d["loss_recovery"] in (True, "true", "1", 1) else "0")
    if "recovery_base" in d and d["recovery_base"] is not None:
        try:
            v = max(0.0, float(d["recovery_base"]))
            kv_set("recovery_base", str(v))
        except (TypeError, ValueError):
            pass
    return get_trade_settings()
