"""Trading engine: schedules signal trades, applies martingale on loss.

Timing rules (per the user's signal format):
- Signal time = candle expiry. We OPEN the trade 60 seconds before that,
  with a 60-second duration, so the option expires exactly at signal_time.
- If a trade loses, the martingale trade opens at the *next* candle
  (i.e., signal_time + 60 * level seconds) at 2x the previous amount.
- Max 2 martingales after the original (so: $1 -> $2 -> $4 worst case).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import config
from binomo_client import BinomoClient, TradeResult
from db import conn_ctx, db_log, get_trade_settings, kv_get, kv_set, save_trade_settings

log = logging.getLogger("engine")

TRADE_DURATION_SEC = 60
LEAD_SECONDS = 2           # place the API call this many seconds before open_time
RESULT_POLL_BUFFER = 3     # start polling this many seconds after expiry


@dataclass
class PendingSignal:
    id: int
    signal_time: datetime  # tz-aware
    direction: str
    martingale_level: int


class BotEngine:
    def __init__(self) -> None:
        self.tz = ZoneInfo(config.TIMEZONE)
        self.client: Optional[BinomoClient] = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._running_signal_id: int | None = None
        self._running_lock = threading.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        if not config.BINOMO_EMAIL or not config.BINOMO_PASSWORD:
            db_log("ERROR", "BINOMO_EMAIL / BINOMO_PASSWORD not set in .env")
            return
        self.client = BinomoClient(
            email=config.BINOMO_EMAIL,
            password=config.BINOMO_PASSWORD,
            demo=(config.ACCOUNT_TYPE == "demo"),
        )
        try:
            await self.client.connect()
        except Exception as e:
            db_log("ERROR", f"Binomo connect failed: {e}")
            self.client = None
            return
        self._reset_orphan_running()
        kv_set("engine_state", "running")
        db_log("INFO", f"Engine started (account={config.ACCOUNT_TYPE}, asset={config.ASSET})")
        self._task = asyncio.create_task(self._loop())

    def _reset_orphan_running(self) -> None:
        """On (re)start, any row left in 'running' is from a previous session;
        mark it 'skipped' so it doesn't block new work."""
        with conn_ctx() as conn:
            with conn:
                cur = conn.execute(
                    "UPDATE signals SET status='skipped' WHERE status='running'"
                )
                if cur.rowcount:
                    db_log("INFO", f"Reset {cur.rowcount} orphaned running signal(s) to skipped")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
            self.client = None
        kv_set("engine_state", "stopped")
        db_log("INFO", "Engine stopped")

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                db_log("ERROR", f"engine tick error: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = datetime.now(self.tz)

        self._mark_expired(now)

        sig = self._next_due(now)
        if sig is None:
            return
        open_time = sig.signal_time - timedelta(minutes=1) + timedelta(
            minutes=sig.martingale_level
        )
        if (open_time - now).total_seconds() > LEAD_SECONDS + 0.5:
            return

        with self._running_lock:
            if self._running_signal_id is not None:
                return
            self._running_signal_id = sig.id
            self._mark_signal_running(sig.id)

        try:
            await self._execute_signal(sig, open_time)
        finally:
            with self._running_lock:
                self._running_signal_id = None

    # ------------------------------------------------------------------
    # signal selection
    # ------------------------------------------------------------------
    def _next_due(self, now: datetime) -> PendingSignal | None:
        with conn_ctx() as conn:
            rows = conn.execute(
                "SELECT id, signal_time, direction, martingale_level "
                "FROM signals WHERE status IN ('pending','running') "
                "ORDER BY signal_time ASC LIMIT 20"
            ).fetchall()

        upcoming: list[PendingSignal] = []
        for r in rows:
            try:
                st = datetime.fromisoformat(r["signal_time"])
            except ValueError:
                continue
            if st.tzinfo is None:
                st = st.replace(tzinfo=self.tz)
            upcoming.append(PendingSignal(
                id=int(r["id"]),
                signal_time=st,
                direction=str(r["direction"]),
                martingale_level=int(r["martingale_level"] or 0),
            ))

        for s in upcoming:
            open_time = s.signal_time - timedelta(minutes=1) + timedelta(minutes=s.martingale_level)
            final_expiry = open_time + timedelta(seconds=TRADE_DURATION_SEC)
            if final_expiry + timedelta(seconds=30) < now:
                continue
            return s
        return None

    def _mark_expired(self, now: datetime) -> None:
        cutoff = (now - timedelta(minutes=5)).isoformat()
        with conn_ctx() as conn:
            with conn:
                conn.execute(
                    "UPDATE signals SET status='skipped' "
                    "WHERE status='pending' AND signal_time < ?",
                    (cutoff,),
                )

    def _mark_signal_running(self, sid: int) -> None:
        with conn_ctx() as conn:
            with conn:
                conn.execute(
                    "UPDATE signals SET status='running' WHERE id = ?", (sid,)
                )

    # ------------------------------------------------------------------
    # trade execution
    # ------------------------------------------------------------------
    async def _execute_signal(self, sig: PendingSignal, open_time: datetime) -> None:
        now = datetime.now(self.tz)
        wait = (open_time - now).total_seconds()
        if wait > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
                return
            except asyncio.TimeoutError:
                pass

        s = get_trade_settings()
        starting_amount = self._starting_amount_for(sig, s)
        amount = round(starting_amount * (s["martingale_multiplier"] ** sig.martingale_level), 2)
        expiry = open_time + timedelta(seconds=TRADE_DURATION_SEC)

        db_log(
            "TRADE",
            f"Placing L{sig.martingale_level} {sig.direction} ${amount} "
            f"for signal #{sig.id} @ {open_time.strftime('%H:%M:%S')} "
            f"(expires {expiry.strftime('%H:%M:%S')})",
        )

        trade_id = self._insert_trade(
            signal_id=sig.id, level=sig.martingale_level,
            direction=sig.direction, amount=amount,
            open_time=open_time, expiry=expiry,
        )

        balance_before = await self.client.get_balance(fresh=True)
        if balance_before is None:
            db_log("ERROR", f"Signal #{sig.id}: couldn't read balance before trade")

        res = await self.client.place(
            sig.direction, amount, config.ASSET, TRADE_DURATION_SEC,
        )

        if not res.ok:
            db_log("ERROR", f"Signal #{sig.id} L{sig.martingale_level} failed: {res.error}")
            self._update_trade(trade_id, result="error", error=res.error or "unknown")
            self._finalize_signal_loss(sig)
            return

        self._update_trade(trade_id, binomo_trade_id=res.trade_id)

        poll_wait = (expiry - datetime.now(self.tz)).total_seconds() + RESULT_POLL_BUFFER
        if poll_wait > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll_wait)
                return
            except asyncio.TimeoutError:
                pass

        balance_after = await self.client.get_balance(fresh=True)
        status, pnl_delta = _resolve_by_balance_diff(balance_before, balance_after, amount)
        db_log(
            "INFO",
            f"Signal #{sig.id} L{sig.martingale_level}: balance {balance_before} -> "
            f"{balance_after} (delta {pnl_delta:+.2f}) -> {status}",
        )

        self._update_trade(trade_id, result=status, payout=pnl_delta)

        if status == "win":
            self._finalize_signal_win(sig, level=sig.martingale_level, pnl_delta=pnl_delta)
            db_log("TRADE", f"Signal #{sig.id} WON at L{sig.martingale_level} (+${pnl_delta:.2f})")
        elif status == "loss":
            self._finalize_signal_loss(sig, pnl_delta=pnl_delta)
        elif status == "draw":
            self._finalize_signal_draw(sig)
        else:
            db_log("INFO", f"Signal #{sig.id} L{sig.martingale_level}: result unknown (balance read failed)")
            self._advance_after_unknown(sig)

    # ------------------------------------------------------------------
    # state transitions
    # ------------------------------------------------------------------
    def _finalize_signal_win(self, sig: PendingSignal, level: int, pnl_delta: float) -> None:
        with conn_ctx() as conn:
            with conn:
                conn.execute(
                    "UPDATE signals SET status='won', won_at_level=?, "
                    "total_pnl=COALESCE(total_pnl,0)+? WHERE id = ?",
                    (level, pnl_delta, sig.id),
                )
        self._update_recovery_after_close(sig.id)

    def _finalize_signal_loss(self, sig: PendingSignal, pnl_delta: float = 0.0) -> None:
        max_mg = get_trade_settings()["max_martingale"]
        if sig.martingale_level >= max_mg:
            with conn_ctx() as conn:
                with conn:
                    conn.execute(
                        "UPDATE signals SET status='lost', "
                        "total_pnl=COALESCE(total_pnl,0)+? WHERE id = ?",
                        (pnl_delta, sig.id),
                    )
            db_log("TRADE", f"Signal #{sig.id} LOST after {sig.martingale_level} martingale(s)")
            self._update_recovery_after_close(sig.id)
        else:
            next_level = sig.martingale_level + 1
            with conn_ctx() as conn:
                with conn:
                    conn.execute(
                        "UPDATE signals SET status='pending', martingale_level=?, "
                        "total_pnl=COALESCE(total_pnl,0)+? WHERE id = ?",
                        (next_level, pnl_delta, sig.id),
                    )
            db_log("INFO", f"Signal #{sig.id} loss -> martingale L{next_level}")

    def _finalize_signal_draw(self, sig: PendingSignal) -> None:
        with conn_ctx() as conn:
            with conn:
                conn.execute(
                    "UPDATE signals SET status='skipped' WHERE id = ?", (sig.id,)
                )
        db_log("INFO", f"Signal #{sig.id} draw (refund) — skipping martingale")
        self._update_recovery_after_close(sig.id)

    def _advance_after_unknown(self, sig: PendingSignal) -> None:
        """Treat unknown result conservatively — mark signal as skipped, no martingale."""
        with conn_ctx() as conn:
            with conn:
                conn.execute(
                    "UPDATE signals SET status='error' WHERE id = ?", (sig.id,)
                )

    # ------------------------------------------------------------------
    # trade record helpers
    # ------------------------------------------------------------------
    def _insert_trade(
        self, *, signal_id: int, level: int, direction: str,
        amount: float, open_time: datetime, expiry: datetime,
    ) -> int:
        with conn_ctx() as conn:
            with conn:
                cur = conn.execute(
                    "INSERT INTO trades (signal_id, level, direction, amount, asset, "
                    "open_time, expiry_time, result) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (signal_id, level, direction, amount, config.ASSET,
                     open_time.isoformat(), expiry.isoformat()),
                )
                return int(cur.lastrowid)

    def _update_trade(
        self, trade_id: int, *,
        binomo_trade_id: str | None = None,
        result: str | None = None,
        payout: float | None = None,
        error: str | None = None,
    ) -> None:
        fields = []
        values: list = []
        if binomo_trade_id is not None:
            fields.append("binomo_trade_id=?"); values.append(binomo_trade_id)
        if result is not None:
            fields.append("result=?"); values.append(result)
        if payout is not None:
            fields.append("payout=?"); values.append(payout)
        if error is not None:
            fields.append("error=?"); values.append(error)
        if not fields:
            return
        values.append(trade_id)
        with conn_ctx() as conn:
            with conn:
                conn.execute(f"UPDATE trades SET {', '.join(fields)} WHERE id = ?", values)

    # ------------------------------------------------------------------
    # loss-recovery sizing
    # ------------------------------------------------------------------
    def _starting_amount_for(self, sig: PendingSignal, s: dict) -> float:
        """Pick the L0 dollar amount for this signal, and persist it on the row
        so subsequent martingales use the same starting amount."""
        with conn_ctx() as conn:
            row = conn.execute(
                "SELECT starting_amount FROM signals WHERE id = ?", (sig.id,)
            ).fetchone()
            existing = row["starting_amount"] if row and row["starting_amount"] is not None else None
        if existing and existing > 0:
            return float(existing)

        base = float(s["base_amount"])
        if s.get("loss_recovery"):
            rb = float(s.get("recovery_base") or 0.0)
            amount = max(base, rb) if rb > 0 else base
        else:
            amount = base

        with conn_ctx() as conn:
            with conn:
                conn.execute(
                    "UPDATE signals SET starting_amount = ? WHERE id = ?",
                    (amount, sig.id),
                )
        return amount

    def _update_recovery_after_close(self, sig_id: int) -> None:
        """Recompute recovery_base from the just-closed signal's net P&L.

        Sizing formula: if the next signal wins at L0, its payout should cover
        the previous loss. For a binary option paying `payout_ratio * stake`
        on a win, that means:
            stake = |loss| / payout_ratio
        On a win or draw: clear recovery_base to 0.
        """
        s = get_trade_settings()
        if not s.get("loss_recovery"):
            return
        with conn_ctx() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(payout), 0) AS net FROM trades WHERE signal_id = ?",
                (sig_id,),
            ).fetchone()
        net = float(row["net"] if row and row["net"] is not None else 0.0)
        if net < 0:
            payout = max(0.01, float(s.get("payout_ratio") or 0.8))
            new_base = round(-net / payout, 2)
        else:
            new_base = 0.0
        save_trade_settings({"recovery_base": new_base})
        db_log(
            "INFO",
            f"recovery_base now ${new_base:.2f} "
            f"(signal #{sig_id} net ${net:+.2f}, payout {s.get('payout_ratio')})"
        )

    # ------------------------------------------------------------------
    # introspection for dashboard
    # ------------------------------------------------------------------
    def running_signal_id(self) -> int | None:
        with self._running_lock:
            return self._running_signal_id


def _resolve_by_balance_diff(
    before: float | None, after: float | None, stake: float
) -> tuple[str, float]:
    """Classify trade outcome from before/after balances.

    BinomoAPI 2.0.1 has no result-polling API, so we infer the outcome:
    - balance went UP  -> WIN (payout is the positive delta)
    - balance went DOWN by ~stake -> LOSS
    - balance unchanged -> DRAW (refund)
    - either side missing -> unknown
    """
    if before is None or after is None:
        return "unknown", 0.0
    delta = round(after - before, 2)
    if delta >= 0.01:
        return "win", delta
    if delta <= -0.01:
        return "loss", delta
    return "draw", 0.0
