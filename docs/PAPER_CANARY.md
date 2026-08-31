# Durable Paper Canary

The Durable Paper Canary is a deliberately narrow, database-backed paper-execution path. It is not
live trading and it is not a general paper broker. The first release supports one configured equity,
long-only `MARKET` orders, multiplier `1`, deterministic full fills, and exactly one active canary run.

The feature is disabled by default. Starting the global runtime never creates, activates, or resumes a
canary. A Trading Core restart reconciles a previously running canary and leaves it
`READY_FOR_ARM`; an operator must explicitly activate it again.

## Safety boundary

- PostgreSQL is authoritative for the run, intent, authorization, fill, cash, positions, events, and
  reconciliation result. Redis and the Control process are not execution authorities.
- Exactly one long-lived owner runs inside Trading Core. Control sends authenticated commands over a
  fixed loopback-only adapter and never constructs the canary runtime.
- Every new order requires the global runtime to be `RUNNING`, kill/risk/daily-loss gates to be open,
  and a fresh `MASSIVE` `REALTIME` `READY` quote whose matching database health row is current.
- Identical retries return the existing durable fill. A reused decision with different content is a
  conflict. Recovery cancels ambiguous nonterminal work and never resubmits it.
- No IBKR, live-broker, `PaperBroker`, scheduler, slicing algorithm, or callback cost model is used by
  this path.

## Required server configuration

All values are server-owned. Clients cannot supply the instrument, quote, prices, fees, risk token,
configuration, or source commit.

```text
ATP_DURABLE_PAPER_CANARY_ENABLED=true
BROKER_EXECUTION_ENABLED=false
ATP_PAPER_CANARY_INTERNAL_TOKEN=<separate high-entropy internal token>
ATP_PAPER_CANARY_OWNER_PORT=9112
ATP_COMMIT_REF=<exact 40-character lowercase deployed commit SHA>
ATP_PAPER_CANARY_CONFIG_JSON=<canonical compact JSON>
```

Both feature flags are literal and case-sensitive. Any value other than the exact pair above disables
new create/activate/submit commands. Recovery, stop, and database status remain available as
risk-reducing operations. The internal token must be shared only by the Trading Core and Control
services.

`ATP_PAPER_CANARY_CONFIG_JSON` must be the exact canonical representation produced by
`PaperCanaryConfig.canonical_json()`. Example shape (limits are illustrative, not a recommendation):

```json
{"asset_class":"EQUITY","commission_per_unit":"0.01000000","instrument":"AAPL","max_daily_turnover":"9000.00000000","max_gross_notional":"5000.00000000","max_order_notional":"1000.00000000","max_orders":5,"min_commission":"1.00000000","mode":"paper","quote_max_age_s":"60.00000000","slippage_bps":"5.00000000","starting_cash":"10000.00000000","tag":"atp.paper-canary.config.v1"}
```

`starting_cash` may not exceed the canonical Risk Control capital. Configuration or deployed-commit
drift blocks activation and new orders; it never blocks recovery, stop, or replay of an already filled
decision.

## Operator flow

All Control requests require `Authorization: Bearer <ATP_CONTROL_TOKEN>`.

1. Ensure database migrations through version 22 are applied and the global runtime/risk state is
   healthy.
2. Create a run with `POST /control/paper-canary/create` and `{"run_id":"..."}`.
3. Activate it with `POST /control/paper-canary/activate` and the exact confirmation phrase.
4. Submit only `run_id`, `decision_id`, `side`, and decimal-string `quantity` to
   `POST /control/paper-canary/submit`.
5. Inspect durable state at `GET /paper-canary/status/{run_id}`.
6. Finish with `POST /control/paper-canary/stop`. Use `recover` after an interrupted process; recovery
   never returns the run to `RUNNING`.

The feature must remain disabled until the exact deployed commit has passed the SQLite crash/retry
matrix, the real PostgreSQL concurrency/constraint suite without skips, and the service loopback tests.
This document does not authorize live or real-money trading.
