from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo


_LINE_RE = re.compile(
    r"^\s*(\d{1,2})\s*[:.]\s*(\d{1,2})\s+([BSbs])\s*$"
)

_BUY = {"B", "CALL", "UP"}
_SELL = {"S", "PUT", "DOWN"}


@dataclass
class SignalEntry:
    signal_time: datetime   # tz-aware, expiry/target time in user's TZ
    direction: str          # "CALL" or "PUT"


def _normalize_direction(token: str) -> str | None:
    t = token.strip().upper()
    if t in _BUY:
        return "CALL"
    if t in _SELL:
        return "PUT"
    return None


def parse_signals(
    text: str,
    for_date: date,
    tz_name: str = "Asia/Colombo",
) -> list[SignalEntry]:
    """Parse a block of "HH:MM B|S" lines for the given date in tz_name.

    Lines that don't match are silently skipped. Midnight wraparound:
    times <= the earliest seen time roll over to the next day.
    """
    tz = ZoneInfo(tz_name)
    out: list[SignalEntry] = []
    last_minutes = -1
    day_offset = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if not m:
            parts = line.split()
            if len(parts) == 2 and ":" in parts[0]:
                try:
                    h, mi = parts[0].split(":")
                    direction = _normalize_direction(parts[1])
                    if direction is None:
                        continue
                    h_i = int(h)
                    m_i = int(mi)
                except ValueError:
                    continue
            else:
                continue
        else:
            h_i = int(m.group(1))
            m_i = int(m.group(2))
            direction = _normalize_direction(m.group(3))
            if direction is None:
                continue

        if not (0 <= h_i <= 23 and 0 <= m_i <= 59):
            continue

        total_minutes = h_i * 60 + m_i
        if last_minutes >= 0 and total_minutes < last_minutes:
            day_offset += 1
        last_minutes = total_minutes

        dt = datetime(
            for_date.year, for_date.month, for_date.day, h_i, m_i, 0, tzinfo=tz
        ) + timedelta(days=day_offset)
        out.append(SignalEntry(signal_time=dt, direction=direction))

    return out


def signal_to_open_time(signal_time: datetime) -> datetime:
    """Signal lists the expiry minute; open 1 minute before."""
    return signal_time - timedelta(minutes=1)


def filter_future(
    entries: Iterable[SignalEntry], now: datetime, lead_seconds: int = 5
) -> list[SignalEntry]:
    """Keep only entries whose OPEN time is far enough in the future."""
    cutoff = now + timedelta(seconds=lead_seconds)
    return [e for e in entries if signal_to_open_time(e.signal_time) >= cutoff]
