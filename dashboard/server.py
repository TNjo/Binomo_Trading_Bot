from __future__ import annotations

import asyncio
import logging
import secrets
import sys
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from db import conn_ctx, db_log, get_trade_settings, init_db, kv_get, save_trade_settings  # noqa: E402
from engine import BotEngine  # noqa: E402
from signal_parser import parse_signals  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Binomo Signal Bot")

_security = HTTPBasic()


def _auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    if not config.DASHBOARD_PASSWORD:
        return credentials.username or "anon"
    user_ok = secrets.compare_digest(credentials.username, config.DASHBOARD_USER)
    pass_ok = secrets.compare_digest(credentials.password, config.DASHBOARD_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


INDEX_FILE = Path(__file__).parent / "index.html"

engine = BotEngine()


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    db_log("INFO", "Dashboard started")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await engine.stop()


@app.get("/")
def root(_: str = Depends(_auth)):
    if not INDEX_FILE.exists():
        raise HTTPException(404, "index.html missing")
    return FileResponse(str(INDEX_FILE))


@app.get("/api/health")
def health():
    return {"status": "ok"}


class SignalPayload(BaseModel):
    text: str
    for_date: str | None = None  # YYYY-MM-DD, defaults to today in user's tz


@app.post("/api/signals/paste")
def api_paste_signals(payload: SignalPayload, _: str = Depends(_auth)):
    if payload.for_date:
        try:
            fdate = date_cls.fromisoformat(payload.for_date)
        except ValueError:
            raise HTTPException(400, "for_date must be YYYY-MM-DD")
    else:
        from zoneinfo import ZoneInfo
        fdate = datetime.now(ZoneInfo(config.TIMEZONE)).date()

    entries = parse_signals(payload.text, fdate, config.TIMEZONE)
    if not entries:
        raise HTTPException(400, "No valid signal lines found")

    inserted = 0
    skipped = 0
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with conn_ctx() as conn:
        with conn:
            for e in entries:
                st_iso = e.signal_time.isoformat()
                exists = conn.execute(
                    "SELECT id FROM signals WHERE signal_time = ? AND direction = ?",
                    (st_iso, e.direction),
                ).fetchone()
                if exists:
                    skipped += 1
                    continue
                conn.execute(
                    "INSERT INTO signals (signal_time, direction, status, martingale_level, created_at) "
                    "VALUES (?, ?, 'pending', 0, ?)",
                    (st_iso, e.direction, now_iso),
                )
                inserted += 1
    db_log("INFO", f"Signals pasted: +{inserted}, skipped {skipped} (dup)")
    return {"inserted": inserted, "skipped_duplicates": skipped, "date": fdate.isoformat()}


@app.get("/api/settings")
def api_get_settings(_: str = Depends(_auth)):
    return get_trade_settings()


@app.post("/api/settings")
def api_save_settings(payload: dict, _: str = Depends(_auth)):
    try:
        if "base_amount" in payload and payload["base_amount"] != "":
            v = float(payload["base_amount"])
            if v <= 0:
                raise HTTPException(400, "base_amount must be > 0")
        if "max_martingale" in payload and payload["max_martingale"] != "":
            v = int(payload["max_martingale"])
            if v < 0:
                raise HTTPException(400, "max_martingale must be >= 0")
        if "martingale_multiplier" in payload and payload["martingale_multiplier"] != "":
            v = float(payload["martingale_multiplier"])
            if v <= 1.0:
                raise HTTPException(400, "martingale_multiplier must be > 1.0")
        if "payout_ratio" in payload and payload["payout_ratio"] != "":
            v = float(payload["payout_ratio"])
            if not (0.1 <= v <= 1.0):
                raise HTTPException(400, "payout_ratio must be between 0.1 and 1.0")
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"invalid value: {e}")
    updated = save_trade_settings(payload)
    db_log("INFO", f"Settings updated: {updated}")
    return updated


@app.post("/api/engine/start")
async def api_engine_start(_: str = Depends(_auth)):
    try:
        error = await engine.start()
    except Exception as e:
        import traceback
        tb = traceback.format_exc().splitlines()
        brief = f"{type(e).__name__}: {e}"
        db_log("ERROR", f"engine.start() raised: {brief}")
        for line in tb[-6:]:
            db_log("ERROR", line)
        return {"running": engine.is_running(), "error": brief}
    return {"running": engine.is_running(), "error": error}


@app.post("/api/engine/stop")
async def api_engine_stop(_: str = Depends(_auth)):
    await engine.stop()
    return {"running": engine.is_running()}


@app.post("/api/signals/clear_pending")
def api_clear_pending(_: str = Depends(_auth)):
    running_id = engine.running_signal_id()
    with conn_ctx() as conn:
        with conn:
            if running_id is not None:
                conn.execute(
                    "DELETE FROM signals WHERE status IN ('pending','running') AND id != ?",
                    (running_id,),
                )
            else:
                conn.execute(
                    "DELETE FROM signals WHERE status IN ('pending','running')"
                )
    return {"ok": True, "protected_running_id": running_id}


@app.post("/api/signals/clear_all")
def api_clear_all(_: str = Depends(_auth)):
    running_id = engine.running_signal_id()
    with conn_ctx() as conn:
        with conn:
            if running_id is not None:
                conn.execute("DELETE FROM trades WHERE signal_id != ?", (running_id,))
                conn.execute("DELETE FROM signals WHERE id != ?", (running_id,))
            else:
                conn.execute("DELETE FROM trades")
                conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM logs")
    return {"ok": True, "protected_running_id": running_id}


@app.get("/api/status")
def api_status(_: str = Depends(_auth)):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(config.TIMEZONE)
    now_local = datetime.now(tz).isoformat()

    with conn_ctx() as conn:
        total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        wins = conn.execute("SELECT COUNT(*) FROM signals WHERE status='won'").fetchone()[0]
        losses = conn.execute("SELECT COUNT(*) FROM signals WHERE status='lost'").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status IN ('pending','running')"
        ).fetchone()[0]
        pnl_row = conn.execute("SELECT COALESCE(SUM(total_pnl), 0) FROM signals").fetchone()

        mg_row = conn.execute(
            "SELECT COALESCE(won_at_level, -1) AS lv, COUNT(*) AS n "
            "FROM signals WHERE status='won' GROUP BY won_at_level"
        ).fetchall()
        mg_dist = {int(r["lv"]): int(r["n"]) for r in mg_row}

        running = None
        running_row = conn.execute(
            "SELECT id, signal_time, direction, martingale_level "
            "FROM signals WHERE status='running' LIMIT 1"
        ).fetchone()
        if running_row:
            running = dict(running_row)

    closed = wins + losses
    win_rate = round((wins / closed) * 100, 1) if closed else 0.0
    settings = get_trade_settings()

    return {
        "engine_running": engine.is_running(),
        "now_local": now_local,
        "timezone": config.TIMEZONE,
        "asset": config.ASSET,
        "account_type": config.ACCOUNT_TYPE,
        "base_amount": settings["base_amount"],
        "max_martingale": settings["max_martingale"],
        "martingale_multiplier": settings["martingale_multiplier"],
        "loss_recovery": settings["loss_recovery"],
        "recovery_base": settings["recovery_base"],
        "payout_ratio": settings["payout_ratio"],
        "total_signals": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate": win_rate,
        "total_pnl": round(float(pnl_row[0]), 2),
        "martingale_distribution": {
            "original": mg_dist.get(0, 0),
            "mg1": mg_dist.get(1, 0),
            "mg2": mg_dist.get(2, 0),
        },
        "running_signal": running,
    }


@app.get("/api/signals")
def api_signals(limit: int = 500, _: str = Depends(_auth)):
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT id, signal_time, direction, status, martingale_level, "
            "won_at_level, total_pnl, starting_amount "
            "FROM signals ORDER BY signal_time ASC LIMIT ?",
            (limit,),
        ).fetchall()
        trades_by_sig: dict[int, list[dict]] = {}
        if rows:
            ids = [r["id"] for r in rows]
            placeholders = ",".join(["?"] * len(ids))
            trade_rows = conn.execute(
                f"SELECT signal_id, level, amount, result, payout "
                f"FROM trades WHERE signal_id IN ({placeholders}) ORDER BY level ASC",
                ids,
            ).fetchall()
            for t in trade_rows:
                trades_by_sig.setdefault(t["signal_id"], []).append({
                    "level": int(t["level"]),
                    "amount": float(t["amount"]),
                    "result": t["result"],
                    "payout": float(t["payout"] or 0.0),
                })
    out = []
    for r in rows:
        d = dict(r)
        d["trades"] = trades_by_sig.get(d["id"], [])
        out.append(d)
    return out


@app.get("/api/trades")
def api_trades(signal_id: int | None = None, limit: int = 200, _: str = Depends(_auth)):
    with conn_ctx() as conn:
        if signal_id is not None:
            rows = conn.execute(
                "SELECT * FROM trades WHERE signal_id = ? ORDER BY id ASC",
                (signal_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/logs")
def api_logs(limit: int = 100, _: str = Depends(_auth)):
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/balance")
async def api_balance(_: str = Depends(_auth)):
    if engine.client is None:
        return {"balance": None, "note": "engine not running"}
    try:
        bal = await engine.client.get_balance(fresh=True)
        return {"balance": round(float(bal), 2) if bal is not None else None}
    except Exception as e:
        return {"balance": None, "error": str(e)}


def main() -> None:
    import uvicorn
    init_db()
    uvicorn.run(
        "dashboard.server:app",
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
