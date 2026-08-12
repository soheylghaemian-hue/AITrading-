# IBKR Market-Data Subscriptions — what's missing (Phase 2A)

Verified against the running paper gateway (127.0.0.1:4002, read-only) on the last smoke run.
The system never fabricates a quote: an unsubscribed instrument shows `DATA_NOT_AVAILABLE` with
the reason, not a fake price (§33).

## Current verified status

| Symbol | Asset class | Exchange | Status | Reason |
|--------|-------------|----------|--------|--------|
| EUR.USD | FX | IDEALPRO | **DATA_AVAILABLE** | IDEALPRO FX is included with the account — no extra subscription |
| AAPL | Equity | NASDAQ (SMART) | **DATA_NOT_AVAILABLE** | IBKR Error 10089 — market-data subscription required |
| NVDA | Equity | NASDAQ (SMART) | **DATA_NOT_AVAILABLE** | IBKR Error 10089 — market-data subscription required |
| SPY  | Equity | ARCA/NYSE (SMART) | **DATA_NOT_AVAILABLE** | IBKR Error 10089 — market-data subscription required |

## The concrete packages required (do NOT buy blindly — these are the exact ones)

To turn AAPL / NVDA / SPY from `DATA_NOT_AVAILABLE` into `DATA_AVAILABLE`, enable in
**Client Portal → Settings → Account Settings → Market Data Subscriptions**:

1. **US Securities Snapshot and Futures Value Bundle** — ~USD 10/month, **waived** when monthly
   commissions ≥ USD 30. Covers snapshot NBBO for US stocks (NYSE/NASDAQ/AMEX) — sufficient for
   the snapshot-based validation and for AAPL/NVDA/SPY here.
2. **US Equity and Options Add-On Streaming Bundle** — ~USD 4.50/month (non-professional).
   Adds real-time *streaming* NBBO for US equities/options. Needed only when we move from
   snapshots to continuous streaming quotes.

Individual alternatives (instead of the bundle, if you want the minimum):
- **NASDAQ (Network C/UTP)** — AAPL, NVDA. ~USD 1.50/month non-pro.
- **NYSE (Network A/CTA)** — SPY (also trades on ARCA). ~USD 1.50/month non-pro.
- **NYSE American, BATS, Regional (Network B)** — broader US coverage.

Later asset classes (not needed for Phase 2A):
- **OPRA (US Options)** — for options market data.
- **CME/CBOT/NYMEX/COMEX real-time** — for US futures/commodities.

## Two gotchas specific to PAPER accounts

1. **Paper accounts do not hold their own subscriptions.** Real-time data is *shared* from the
   associated live account. In the live account's Market Data Subscriptions, enable
   **"Share real-time market data with paper trading account"**. Without a funded/subscribed
   live account, US real-time will stay `DATA_NOT_AVAILABLE`.
2. **Free alternative — delayed data.** Without any subscription, delayed (15-min) data is
   available by requesting market-data type 3 (`reqMarketDataType(3)`). We deliberately do **not**
   silently fall back to delayed: the dashboard would then show `DELAYED` (a distinct state), so
   the operator always knows the quote is not real-time. Enabling delayed is a conscious choice.

## What this means operationally right now

- **FX (EUR.USD and other IDEALPRO majors): ready today** — real read-only data flows.
- **US equities: blocked on the subscription above** — the system correctly reports
  `DATA_NOT_AVAILABLE`, the AI agents return `NO DATA` for those instruments (no guessing), and
  no order is or can be generated (read-only, execution disabled).
