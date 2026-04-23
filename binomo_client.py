"""Async wrapper over BinomoAPI 2.0.1 (chipadevteam).

Important: this version of BinomoAPI has no trade-result polling API.
Win/loss is determined by the engine via balance-diff:
capture balance before the trade, capture again after expiry, and compare.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("binomo_client")


@dataclass
class TradeResult:
    ok: bool
    trade_id: Optional[str] = None
    raw: Any = None
    error: Optional[str] = None


class BinomoClient:
    def __init__(self, email: str, password: str, demo: bool = True) -> None:
        self.email = email
        self.password = password
        self.demo = demo
        self._api: Any = None

    async def connect(self) -> None:
        try:
            from BinomoAPI import BinomoAPI  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "BinomoAPI package not installed. Run `pip install BinomoAPI`."
            ) from e

        login = BinomoAPI.login(self.email, self.password)
        if login is None:
            raise RuntimeError(
                "Binomo login returned None — check BINOMO_EMAIL / BINOMO_PASSWORD."
            )

        self._api = BinomoAPI.create_from_login(
            login, demo=self.demo, enable_logging=False
        )
        try:
            await self._api.connect()
        except Exception as e:
            log.warning("WebSocket connect warning: %s", e)
        log.info("Connected to Binomo (demo=%s)", self.demo)

    async def get_balance(self, fresh: bool = False) -> Optional[float]:
        """Return demo-account balance in USD.

        BinomoAPI caches balance for 5 minutes, so balance-diff based
        win/loss detection is broken by default. Passing fresh=True
        invalidates the cache before the read.
        """
        if self._api is None:
            return None
        if fresh:
            try:
                self._api._cached_balance = None
                self._api._cached_balance_timestamp = None
            except Exception:
                pass
        try:
            bal = await self._api.get_balance()
        except Exception as e:
            log.warning("get_balance failed: %s", e)
            return None
        if bal is None:
            return None
        amt = getattr(bal, "amount", None)
        if amt is None and isinstance(bal, dict):
            amt = bal.get("amount")
        return float(amt) if amt is not None else None

    async def place(
        self,
        direction: str,
        amount: float,
        asset: str,
        duration_sec: int,
    ) -> TradeResult:
        direction = direction.upper()
        if direction not in ("CALL", "PUT"):
            return TradeResult(ok=False, error=f"bad direction {direction!r}")
        if self._api is None:
            return TradeResult(ok=False, error="client not connected")

        fn = (
            self._api.place_call_option if direction == "CALL"
            else self._api.place_put_option
        )
        amount_cents = int(round(float(amount) * 100))
        if amount_cents <= 0:
            return TradeResult(ok=False, error=f"amount {amount} rounds to 0 cents")
        try:
            raw = await fn(
                asset=asset,
                duration_seconds=int(duration_sec),
                amount=amount_cents,
                use_demo=self.demo,
            )
        except Exception as e:
            return TradeResult(ok=False, error=f"{direction} raised: {e}")

        tid = None
        if isinstance(raw, dict):
            for key in ("trade_id", "tradeId", "id", "deal_id", "ref"):
                if key in raw and raw[key]:
                    tid = str(raw[key])
                    break
        return TradeResult(ok=True, trade_id=tid, raw=raw)

    async def close(self) -> None:
        if self._api is not None:
            try:
                await self._api.close()
            except Exception as e:
                log.debug("close error: %s", e)
            self._api = None
