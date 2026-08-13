# iPhone push notifications (§23)

The backend forwards every NotificationCenter event to your iPhone. The public dashboard
(gigbay.de) is **not** involved and holds **no** tokens — pushing happens on the trading machine
only. Secrets live in the backend environment, never in the repo or the frontend.

Pick **one** (or both). ntfy is the fastest; Telegram is the most private.

---

## Option A — ntfy (fastest, free, no account)

1. Install **ntfy** from the App Store.
2. Choose a **long, secret topic name** (this is your only access control), e.g.
   `atp-trading-9f3k2m-7q1z` (make it unguessable).
3. In the ntfy app: **Subscribe to topic** → enter that exact name.
4. On the **trading machine**, set the backend environment:
   ```bash
   export NTFY_TOPIC="atp-trading-9f3k2m-7q1z"
   export NOTIFY_MIN_SEVERITY="warning"   # info | warning | critical (default warning)
   ```
   (Optional self-hosted/authenticated server: `NTFY_SERVER=https://your.ntfy.host` and
   `NTFY_TOKEN=...`.)
5. Build the notification center from the environment when you start the backend:
   ```python
   from atp.dashboard.notifications import NotificationCenter
   nc = NotificationCenter.from_env()      # picks up NTFY_/TELEGRAM_ env vars
   ```
   Pass `nc` into `DashboardContext(..., notifications=nc)` / your live runner.

> Anyone who learns the topic name can read/post to it — keep it secret, or self-host with a token.

---

## Option B — Telegram (most private)

1. Install **Telegram** on the iPhone.
2. Talk to **@BotFather** → `/newbot` → get the **bot token**.
3. Send your new bot any message, then get your **chat id** (e.g. open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`).
4. On the **trading machine**:
   ```bash
   export TELEGRAM_BOT_TOKEN="123456:ABC..."
   export TELEGRAM_CHAT_ID="987654321"
   export NOTIFY_MIN_SEVERITY="warning"
   ```
5. Same wiring: `NotificationCenter.from_env()`.

Only your chat receives the messages.

---

## What you receive

Each push is `<emoji> [SEVERITY] <kind>: <message>`, filtered by `NOTIFY_MIN_SEVERITY`.
Severities: `info` · `warning` · `critical` (ntfy maps these to default/high/urgent priority).

**Today the backend actually emits:** emergency-stop, resume, and trading-risk-config changes.
The other kinds (trade opened/closed, risk-halt, reconciliation break, broker disconnect,
data-feed loss, model decay, …) are defined and will push once wired into the live engines — a
follow-up backend task, no IBKR change.

## Always-on watchdog (automatic alerts)

`examples/notify_watchdog.py` is a tiny always-on monitor that pushes to your phone on real
**state changes** (never spam). By default it makes **no IBKR API calls** — it only checks
whether the IB Gateway port is reachable, and alerts when that flips:

- 🚨 *IB Gateway NOT reachable (port 4002)* — the gateway went down.
- ⚠️ *IB Gateway reachable again* — it came back.

Opt-in market-data alerts (read-only snapshot, no orders): set `WATCHDOG_MARKET_DATA=1` to also
get *"AAPL: market data now AVAILABLE"* / *"lost"* per instrument — useful to be pinged the moment
US-equity real-time data finally activates.

### Run it automatically (macOS LaunchAgent)

Installed at `~/Library/LaunchAgents/com.atp.notify-watchdog.plist` (a filled copy of
`deploy/com.atp.notify-watchdog.plist.template`). It starts at login and restarts on crash.

```bash
# start / stop / restart
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.atp.notify-watchdog.plist
launchctl bootout   gui/$(id -u)/com.atp.notify-watchdog
# logs
tail -f ~/Library/Logs/atp-notify-watchdog.log
```

To enable market-data alerts: set `WATCHDOG_MARKET_DATA` to `1` in the plist, then bootout+bootstrap.

> macOS python.org note: if pushes fail with `CERTIFICATE_VERIFY_FAILED`, install CA certs once:
> `python3 -m pip install certifi` (the sink uses it automatically). No verification is ever disabled.

## Safety

- Push delivery is **best-effort**: a failed send is logged and never interrupts trading.
- Tokens/topics are read **only** from the backend environment — never committed, never sent to
  the browser. The frontend and Vercel deployment are unchanged by this feature.
- This does not enable execution or live trading. IBKR stays PAPER / READ-ONLY / NO EXECUTION.
