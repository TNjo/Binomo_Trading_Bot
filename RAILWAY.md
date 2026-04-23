# Deploying Binomo Signal Bot to Railway

## Before you start — important caveats

1. **Railway IPs may be blocked by Binomo / Cloudflare.** Cloud-data-center IP ranges are frequently challenged. If login fails or the websocket disconnects immediately, Railway is not a good fit. A VPS with a residential-friendly IP (Hetzner, DigitalOcean, OVH) or a home box with Tailscale works better.
2. **Timing sensitivity.** The bot opens trades 60 seconds before expiry. Network latency from Railway to Binomo's websocket servers adds delay — usually fine (~100-200ms), but worth checking the first few trades land on the right candle.
3. **Single replica only.** The engine determines win/loss by diffing balance. Running two instances against the same Binomo account corrupts that detection. Keep `numReplicas: 1` (already set in `railway.json`).
4. **Change your passwords** before deploying. The default dashboard password `123` is fine on localhost but a disaster in public.

## 1. Prepare the repo

The `binomo_bot/` folder is already its own git repo (as a sibling to `signal_bot/`). If it isn't yet:

```bash
cd binomo_bot
git init
git add .
git commit -m "initial binomo bot"
```

Push it to GitHub (or GitLab) — Railway deploys from a repo.

## 2. Create the Railway project

1. Go to https://railway.com, click **New Project → Deploy from GitHub repo**.
2. Pick the `binomo_bot` repo.
3. Railway detects it as a Python app via [`nixpacks.toml`](nixpacks.toml) and [`Procfile`](Procfile).

## 3. Add a persistent volume (required)

SQLite data (signals, trades, recovery base) lives on disk. Without a volume, every redeploy wipes it.

1. In your service, open **Settings → Volumes → New Volume**.
2. Mount path: `/data`.
3. Any size ≥ 1 GB is plenty.

Then set the `DATA_DIR` env var (next step) to `/data` so the bot writes the DB there.

## 4. Environment variables

Open the service → **Variables**, add:

| Variable | Value | Notes |
|---|---|---|
| `BINOMO_EMAIL` | `you@example.com` | |
| `BINOMO_PASSWORD` | your password | |
| `ACCOUNT_TYPE` | `demo` | Switch to `real` once confident |
| `ASSET` | `Crypto IDX` | |
| `BASE_AMOUNT` | `1.0` | |
| `MAX_MARTINGALE` | `2` | |
| `MARTINGALE_MULTIPLIER` | `2.0` | |
| `PAYOUT_RATIO` | `0.8` | |
| `TIMEZONE` | `Asia/Colombo` | |
| `DASHBOARD_USER` | `admin` | |
| `DASHBOARD_PASSWORD` | **a long random string** | don't reuse! |
| `DATA_DIR` | `/data` | **required** — matches the volume mount |

Leave `PORT` unset — Railway injects it automatically, and the bot uses it.

## 5. Deploy

Click **Deploy** or push a commit. First build takes a minute or two (nixpacks installs Python 3.11 + dependencies).

## 6. Expose the dashboard

Under **Settings → Networking → Generate Domain** — Railway gives you a `https://your-app.up.railway.app` URL. Hit it in a browser → log in with the dashboard user/password.

## 7. First-run checklist

On the Railway service's **Deployments → Logs**, the startup should look like:

```
INFO:     Started server process
INFO:     Uvicorn running on http://0.0.0.0:PORT
```

In the dashboard:
1. Click **Start Bot**. If Binomo login works you'll see:
   - `Connected to Binomo (demo=True)` in logs
   - Account balance appears in the top-right card
2. If you see `Binomo login returned None` or a websocket error, Cloudflare has blocked Railway — see the caveat at the top.
3. Paste your first signal block. Make sure the signal times are still in the future for the day you selected — past signals get auto-skipped.

## 8. Running on real money

Once demo is stable for a few days:

1. Change `ACCOUNT_TYPE` from `demo` to `real`.
2. Lower `BASE_AMOUNT` to something you can afford to lose (e.g. `1.0`) — martingales compound fast.
3. **Redeploy** (env changes require a restart).

## Debugging

- **Logs**: Railway → Deployments → pick the latest → Logs. Also visible in-dashboard under Activity Log.
- **Volume contents**: `railway run sqlite3 /data/trades.db .schema` (using Railway CLI).
- **Restart**: `railway redeploy`.

## What happens if Railway restarts mid-trade

- The websocket reconnects on boot.
- Any signal stuck in `running` from a previous session is reset to `skipped` at engine start (so it won't re-fire).
- In-flight trades that expired during downtime are effectively lost — the bot can't attribute them. The next signal starts fresh. Check your Binomo trade history in that case to reconcile manually.
