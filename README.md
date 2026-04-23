# Binomo Signal Bot (Demo Account)

Automated Binomo binary-options bot driven by a pasted daily signal list.
Trades on **CRYPTO IDX**, 1-minute timeframe, martingale up to 2x on loss.

## How it works

- You paste the day's signal list (e.g. `09:25 S` = PUT at 09:25).
- For each signal the bot **opens the trade 1 minute before** the signal
  time with a 60-second expiry — so it expires exactly at the signal minute.
- On **loss**, it martingales on the next candle at 2x the previous amount,
  up to 2 martingales (so worst case: `$1 → $2 → $4` for base `$1`).
- On **win**, it moves to the next signal.
- Runs in Sri Lanka time (`Asia/Colombo`, GMT+5:30) by default.

## Install

```bash
cd binomo_bot
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env        # then edit .env with your Binomo creds
```

Set in `.env`:

```
BINOMO_EMAIL=you@example.com
BINOMO_PASSWORD=secret
ACCOUNT_TYPE=demo
BASE_AMOUNT=1.0
MAX_MARTINGALE=2
ASSET=Crypto IDX
DASHBOARD_PASSWORD=pick_one
```

## Run

```bash
python main.py
```

Open http://localhost:8000 (login with `DASHBOARD_USER` / `DASHBOARD_PASSWORD`).

## Using the dashboard

1. **Date**: pick the day the signals are for (defaults to today).
2. Paste the signal block into the textarea, e.g.:

   ```
   00:05 S
   00:10 B
   00:15 S
   ...
   ```

3. Click **Add Signals** — duplicates for the same time+direction are skipped.
4. Click **Start Bot**. The engine logs in to Binomo, connects, and begins
   firing trades at each signal's open time (1 min before signal time).
5. Watch the dashboard:
   - **Running Signal** card shows the active trade.
   - **Won on: `original / MG1 / MG2`** shows where each win landed.
   - **Signals** table shows every signal with status (`pending`, `running`,
     `won`, `lost`, `skipped`, `error`), current martingale level, and PnL.

## Notes / known caveats

- The `BinomoAPI` package from chipadevteam has several forks. `binomo_client.py`
  tries a few common method names (`call`/`put`, `Call`/`Put`, `login_sync`/`login`,
  `check_win`/`get_result`, etc.) so it tolerates minor API differences. If your
  installed fork uses different names, edit `binomo_client.py` — it's the only
  place with vendor-specific code.
- Binomo may be geo-restricted and may require an account that has not been
  flagged. This bot does not bypass those restrictions.
- Currency & asset string format ("Crypto IDX" vs "CRYPTO_IDX" vs asset-id)
  also varies by fork. If your first trade rejects, check logs and adjust
  the `ASSET` env value.
- If a trade's result comes back `unknown` (network or result-polling issue),
  the signal is marked `error` and **no martingale is taken** — the bot moves on
  to the next signal rather than guessing.

## Files

- `signal_parser.py` — parses `HH:MM B|S` lines with midnight wrap handling
- `binomo_client.py` — thin wrapper over the `BinomoAPI` package (edit here if API signatures differ)
- `engine.py` — scheduler + martingale state machine
- `db.py` — SQLite schema (signals, trades, logs)
- `dashboard/server.py` — FastAPI HTTP API + static page server
- `dashboard/index.html` — single-page UI
- `main.py` — runs the FastAPI app
