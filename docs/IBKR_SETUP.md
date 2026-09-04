# IBKR Setup — what to provide when the account is live (§12/§17)

The IBKR adapter (`atp.brokers.ibkr.IBKRBroker`, `atp.live.feed.IBKRMarketFeed`) is fully
implemented and its IB⇄atp mapping is unit-tested. It cannot be validated end-to-end without a
running gateway. This is the checklist to connect real data — nothing here is faked; until the
gateway answers, live calls simply return `DATA_NOT_AVAILABLE`-style errors, never invented data.

## 1. Prerequisites (you)

- **IBKR account** — start with the **paper** account (separate credentials from live).
- **IB Gateway** (headless, recommended) or **TWS**, running and logged in.
- In Gateway/TWS: *Configure → Settings → API → Settings*:
  - ✅ Enable ActiveX and Socket Clients
  - ✅ (recommended) Read-Only API off only when you actually want to trade
  - Add the host running `atp` to **Trusted IPs** (e.g. `127.0.0.1`)
  - Note the **Socket port**.
- `pip install -e ".[live]"` (installs `ib_async`).

## 2. Ports (default)

| Application | Paper | Live |
|-------------|-------|------|
| IB Gateway  | 4002  | 4001 |
| TWS         | 7497  | 7496 |

Set them in `IBKRConfig(host, port, client_id, account, readonly)`.

## 3. Market-data subscriptions (you — depends on what we trade)

IBKR market data is **subscription-gated per asset class/exchange**. Decide and enable in
*Account → Market Data Subscriptions*. Typical:

- **US equities/ETFs**: *US Securities Snapshot and Futures Value Bundle* or *NASDAQ/NYSE
  (Network A/B/C) real-time*.
- **US futures/commodities**: *CME/CBOT/NYMEX/COMEX real-time*.
- **FX**: IDEALPRO is included with an account; no extra data sub for majors.
- **Options**: *OPRA (US Options)*.
- **Indices**: the relevant index feed (e.g. *CBOE*).

Without a subscription IBKR returns delayed or no data → the Data Quality Engine flags it as
stale/missing → **NO TRADE** (by design).

## 4. What only you can provide

Per the decision rule (§19), these are the inputs the system cannot infer:

- **IBKR paper + live credentials** (entered into the Gateway, never into `atp` source).
- **Which market-data subscriptions** to enable (cost vs. universe).
- **Capital mandate** — the account size the desk may manage (`SystemConfig.capital`).
- **Trading limits** — risk per trade, daily-loss limit, max leverage, max positions
  (`SystemConfig` / `RiskLimits`).
- **Allowed asset classes / instrument universe** to scan.
- Any **external data-provider API keys** (if we add non-IBKR feeds).

## 5. Step-by-step (tomorrow, in order)

1. **Start IB Gateway** and log in to the **paper** account.
2. **Enable the API**: *Configure → Settings → API → Settings* → ✅ *Enable ActiveX and Socket
   Clients*; add `127.0.0.1` to *Trusted IPs*; confirm the socket **port = 4002**.
3. **Enable market-data subscriptions** for the first test universe (§3 above). For a minimal
   first run, US equities real-time (or delayed) is enough.
4. **Install the live extra** on the machine running `atp`:
   ```bash
   pip install -e ".[live]"     # ib_async
   ```
5. **Run the READ-ONLY smoke test** (sends NO orders):
   ```bash
   PYTHONPATH=src python3 examples/ibkr_smoke.py --port 4002
   PYTHONPATH=src python3 examples/ibkr_smoke.py --port 4002 --symbols AAPL,EUR.USD
   ```
   It only: connects, confirms the account, reads the account summary + cash, reads positions,
   reads open orders, checks connection status, and requests a small, fixed set of market-data
   snapshots. **It cannot place orders** (the capability is not in the script).
6. If the smoke test passes, proceed to `docs/IBKR_E2E_TEST_PLAN.md` (phases A → R).

## 6. Wiring the live feed/broker (after the smoke test passes)

```
IBKR account → IB Gateway → IBKRMarketFeed / IBKRBroker → DataQualityEngine → FeatureEngine
→ RegimeEngine → 9 AI traders → MasterPortfolioManager → RiskEngine → (paper) Execution
→ Journal → Reconciliation → Dashboard
```

Swap `ReplayFeed`→`IBKRMarketFeed` and `PaperBroker`→`IBKRBroker` in `build_paper_stack` / the
live runner; everything upstream is unchanged. The desk must run in **paper/autonomous** mode
first — no live orders until the paper end-to-end test is signed off.

## 7. Security (never violate)

IBKR credentials, API keys and secrets **never** appear in chat, source code, GitHub, docs, or
the frontend. They live **only** in the local IB Gateway / secure environment (§28). The
dashboard emergency-stop token is read from `ATP_DASHBOARD_TOKEN` (env), never hard-coded.
