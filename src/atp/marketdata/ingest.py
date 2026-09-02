"""WP4 — the persistent, fault-tolerant market-data ingest orchestrator.

Pulls read-only data for ONLY the WP3-`VERIFIED` instruments of the WP2 catalogue through the narrow
`MarketDataProvider` interface and persists it fail-closed:

  * an unmapped / unentitled / unclear instrument is rendered NO_DATA — never a fabricated or presumed-
    realtime value; a provider's REALTIME claim is downgraded unless the account is genuinely entitled AND
    the instrument is VERIFIED;
  * the CURRENT quote is updated monotonically (a late / out-of-order / backward packet never overwrites a
    newer one) while the immutable history and bars are append-only and duplicate-safe;
  * per-provider / per-market / per-instrument error isolation — one failure never aborts the rest;
  * a resumable, observable run with append-only audit events and progress/error counters.

SAFETY: read-only market data only. No orders, no execution, no account, no subscription purchase, no new
credentials, no real network in CI (use `StubMarketDataProvider`). AUTONOMOUS=DISABLED · EXECUTION=DISABLED ·
IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .model import (
    AdjustmentPolicy,
    BarObservation,
    CorporateAction,
    DataStatus,
    QuoteObservation,
    classify_bar_quality,
    classify_data_status,
    classify_quality,
    dec_text,
    utc_ts,
)
from .provider_base import (
    InstrumentRef,
    MarketDataEntitlementError,
    MarketDataProvider,
    MarketDataUnavailableError,
)


@dataclass(frozen=True, slots=True)
class IngestConfig:
    exchange: str | None = None
    limit: int = 1000
    max_age_s: float = 30.0
    fetch_bars: bool = False
    bar_interval: str = "1d"
    bar_start: str = ""
    bar_end: str = ""
    fetch_corporate_actions: bool = False
    ca_start: str = ""
    ca_end: str = ""


@dataclass(slots=True)
class IngestSummary:
    run_id: str
    status: str
    processed: int = 0
    quotes_written: int = 0
    bars_written: int = 0
    history_appended: int = 0
    no_data: int = 0
    skipped: int = 0
    error: int = 0
    completed_markets: list = field(default_factory=list)
    failed_markets: list = field(default_factory=list)


def ingest_request_checksum(run_label: str, provider: str, exchange: str | None) -> str:
    payload = {"label": run_label, "provider": provider, "exchange": exchange or "*",
               "tag": "atp.market-data-ingest.request.v1"}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _make_clock(now: str | None, clock: Callable[[], str] | None) -> Callable[[], str]:
    """A UTC-ISO clock: an explicit callable, else a FIXED value (deterministic tests), else the wall clock."""
    if clock is not None:
        return clock
    if now is not None:
        fixed = utc_ts(now)
        return lambda: fixed
    return lambda: utc_ts(datetime.now(UTC))


def _ref(row) -> InstrumentRef:
    return InstrumentRef(
        instrument_id=row.instrument_id, symbol=row.symbol, exchange=row.exchange,
        currency=row.trading_currency, asset_class=row.asset_class, con_id=row.con_id,
        primary_exchange=row.primary_exchange, verified=(row.qualification_status == "VERIFIED"))


def _has_price(q) -> bool:
    return any(dec_text(v) is not None for v in (q.bid, q.ask, q.last))


def _obs_record(obs: QuoteObservation) -> dict:
    return {
        "instrument_id": obs.instrument_id, "provider": obs.provider,
        "provider_instrument_id": obs.provider_instrument_id, "bid": obs.bid, "ask": obs.ask, "last": obs.last,
        "mid": obs.mid, "spread": obs.spread, "bid_size": obs.bid_size, "ask_size": obs.ask_size,
        "volume": obs.volume, "reference_price": obs.reference_price, "previous_close": obs.previous_close,
        "data_currency": obs.data_currency, "source_ts": obs.source_ts, "receive_ts": obs.receive_ts,
        "latency_ms": obs.latency_ms, "data_status": obs.data_status,
        "entitlement_status": obs.entitlement_status, "license": obs.license,
        "quality_status": obs.quality_status, "adjustment_policy": obs.adjustment_policy,
        "corporate_action_version": obs.corporate_action_version, "provenance_checksum": obs.checksum,
    }


def ingest_market_data(store, provider: MarketDataProvider, *, run_label: str,
                       config: IngestConfig | None = None, run_id: str | None = None,
                       now: str | None = None, clock: Callable[[], str] | None = None,
                       sleep: Callable[[float], None] | None = None) -> IngestSummary:
    """Run (or start fresh) a market-data ingest pass for one provider. Each call is its own run; resumability
    comes from re-selecting the catalogue (never re-entering a live run row). Freshness is judged against a
    per-instrument `clock()` reading (the REAL time at each fetch), NEVER a run-start-frozen timestamp, so a
    quote that goes stale during a long/rate-limited pass is not mislabeled REALTIME. `now` (a fixed string,
    for deterministic tests) or `clock` (a callable) may pin it; production uses the wall clock."""
    config = config or IngestConfig()
    do_sleep = sleep or time.sleep
    tick = _make_clock(now, clock)
    checksum = ingest_request_checksum(run_label, provider.name, config.exchange)
    run_id = run_id or uuid.uuid4().hex
    store.md_create_run(run_id=run_id, request_checksum=checksum, run_label=run_label,
                        provider=provider.name, kind="quote")
    store.md_advance_run_status(run_id, "PLANNED", "RUNNING")

    if not provider.configured:
        # Fail-closed: an unconfigured provider (no existing credentials/usage rights) fetches nothing.
        store.md_append_run_event(run_id, event={"id": f"{run_id}-e1", "seq": 1, "provider": provider.name,
                                                 "event_type": "PROVIDER_NOT_CONFIGURED", "severity": "ERROR",
                                                 "reason": "provider is not configured"})
        store.md_finalize_run(run_id, status="FAILED", failure_code="PROVIDER_NOT_CONFIGURED",
                              failure_reason="provider is not configured")
        return _summary(store.md_get_run(run_id))

    instruments = store.md_select_verified_instruments(exchange=config.exchange, limit=config.limit)
    markets: dict[str, list] = {}
    for row in instruments:
        markets.setdefault(row.exchange or "", []).append(row)
    store.md_set_planned_markets(run_id, sorted(markets))
    seq = store.md_max_event_seq(run_id)
    rate = provider.rate_limit_info()

    for market in sorted(markets):
        rows = sorted(markets[market], key=lambda r: r.instrument_id)
        try:
            for row in rows:
                seq = _process_one(store, provider, _ref(row), market, run_id, seq, config, tick)
                do_sleep(rate.min_interval_s)
            seq += 1
            store.md_record_market(run_id, market=market, market_status="COMPLETED",
                                   event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                          "market": market, "event_type": "MARKET_OK", "severity": "INFO"})
        except Exception as exc:  # noqa: BLE001 — per-market isolation: never let one market abort the rest
            seq += 1
            store.md_record_market(run_id, market=market, market_status="FAILED",
                                   event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                          "market": market, "event_type": "MARKET_ERROR", "severity": "ERROR",
                                          "reason": f"{type(exc).__name__}: {exc}"})

    run = store.md_get_run(run_id)
    completed = json.loads(run.completed_markets_json)
    failed = json.loads(run.failed_markets_json)
    if failed and completed:
        final, failure = "PARTIAL", ("PARTIAL_INGEST", f"{len(failed)} market(s) failed")
    elif failed:
        final, failure = "FAILED", ("ALL_MARKETS_FAILED", "every market failed")
    else:
        final, failure = "COMPLETED", (None, None)
    store.md_finalize_run(run_id, status=final, failure_code=failure[0], failure_reason=failure[1])
    return _summary(store.md_get_run(run_id))


def _process_one(store, provider: MarketDataProvider, ref: InstrumentRef, market: str, run_id: str,
                 seq: int, config: IngestConfig, tick: Callable[[], str]) -> int:
    """Qualify + fetch + persist ONE instrument, fully isolated (never raises). Returns the new event seq.
    `now` is read FRESH per instrument (`tick()`), so freshness reflects the real fetch time, not run start."""
    now = tick() or utc_ts(datetime.now(UTC))
    try:
        mapping = provider.map_instrument(ref)
        try:
            ent = provider.probe_entitlement(ref)
        except MarketDataEntitlementError:
            ent = None
        pid = mapping.provider_instrument_id if mapping else None
        if ent is not None:
            store.md_upsert_provider_entitlement({
                "instrument_id": ref.instrument_id, "provider": provider.name, "provider_instrument_id": pid,
                "entitlement_status": ent.entitlement_status.value, "license": ent.license.value,
                "realtime_available": ent.realtime_available, "available": ent.available,
                "capabilities_json": json.dumps(list(ent.capabilities)), "reason": ent.reason,
                "last_checked_at": now})

        if mapping is None:
            seq += 1
            store.md_bump_counter(run_id, "skipped_count",
                                  event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "market": market,
                                         "instrument_id": ref.instrument_id, "event_type": "UNMAPPED",
                                         "severity": "WARN", "status": DataStatus.NO_DATA.value,
                                         "reason": "provider cannot map instrument"})
            return seq

        quote = provider.get_quote(ref)
        entitled = bool(ent and ent.realtime_available)
        if quote is None or not _has_price(quote):
            seq += 1
            store.md_bump_counter(run_id, "no_data_count",
                                  event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "market": market,
                                         "instrument_id": ref.instrument_id, "event_type": "NO_DATA",
                                         "severity": "INFO", "status": DataStatus.NO_DATA.value,
                                         "reason": "no usable quote"})
            return seq

        data_status = classify_data_status(
            declared=quote.declared_status, entitled=entitled, verified=ref.verified,
            has_price=True, source_ts=quote.source_ts, now=now, max_age_s=config.max_age_s)
        quality = classify_quality(quote.bid, quote.ask, quote.last)
        obs = QuoteObservation(
            instrument_id=ref.instrument_id, provider=provider.name, provider_instrument_id=pid,
            bid=quote.bid, ask=quote.ask, last=quote.last, bid_size=quote.bid_size, ask_size=quote.ask_size,
            volume=quote.volume, reference_price=quote.reference_price, previous_close=quote.previous_close,
            data_currency=quote.data_currency, source_ts=utc_ts(quote.source_ts),  # NULL, never fabricated
            receive_ts=utc_ts(quote.receive_ts) or now, data_status=data_status.value,
            entitlement_status=(ent.entitlement_status.value if ent else "NOT_ENTITLED"),
            license=(ent.license.value if ent else "NONE"), quality_status=quality.value,
            adjustment_policy=AdjustmentPolicy.RAW.value, corporate_action_version=0)

        if data_status is DataStatus.NO_DATA or obs.source_ts is None:
            seq += 1
            store.md_bump_counter(run_id, "no_data_count",
                                  event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "market": market,
                                         "instrument_id": ref.instrument_id, "event_type": "NO_DATA",
                                         "severity": "INFO", "status": DataStatus.NO_DATA.value,
                                         "reason": "unusable/undated quote"})
            return seq

        # Persist the quote AND mark the instrument processed AND record QUOTE_OK — atomically, so a written
        # quote is always counted as processed (quotes_written can never exceed processed).
        seq += 1
        store.md_record_quote(_obs_record(obs), run_id=run_id,
                              event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                     "market": market, "instrument_id": ref.instrument_id,
                                     "event_type": "QUOTE_OK", "severity": "INFO",
                                     "status": data_status.value, "reason": obs.checksum})
        # Bars / corporate actions are best-effort EXTRAS: a failure here must not re-classify the already-
        # recorded instrument as an error (which would double-count it), so it is isolated separately.
        if config.fetch_bars or config.fetch_corporate_actions:
            try:
                if config.fetch_bars:
                    _ingest_bars(store, provider, ref, pid, run_id, ent, now, config)
                if config.fetch_corporate_actions:
                    _ingest_corporate_actions(store, provider, ref, pid, run_id, now, config)
            except Exception:  # noqa: BLE001, S110 — extras are best-effort; the quote is already recorded
                pass
        return seq
    except MarketDataUnavailableError as exc:
        seq += 1
        store.md_bump_counter(run_id, "error_count",
                              event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "market": market,
                                     "instrument_id": ref.instrument_id, "event_type": "PROVIDER_ERROR",
                                     "severity": "ERROR", "reason": f"{exc.code}: {exc}"})
        return seq
    except Exception as exc:  # noqa: BLE001 — per-instrument isolation: one instrument never aborts the run
        seq += 1
        store.md_bump_counter(run_id, "error_count",
                              event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "market": market,
                                     "instrument_id": ref.instrument_id, "event_type": "ERROR",
                                     "severity": "ERROR", "reason": f"{type(exc).__name__}: {exc}"})
        return seq


def _ingest_bars(store, provider, ref, pid, run_id, ent, now, config) -> None:
    bars = provider.get_bars(ref, interval=config.bar_interval, start=config.bar_start, end=config.bar_end)
    for b in bars:
        obs = BarObservation(
            instrument_id=ref.instrument_id, provider=provider.name, provider_instrument_id=pid,
            interval=b.interval, ts=utc_ts(b.ts), open=b.open, high=b.high, low=b.low, close=b.close,
            volume=b.volume, trade_count=b.trade_count, data_currency=b.data_currency,  # NULL, never fabricated
            source_ts=utc_ts(b.source_ts), receive_ts=utc_ts(b.receive_ts) or now,
            data_status=DataStatus.END_OF_DAY.value if b.declared_status is DataStatus.END_OF_DAY
            else DataStatus.DELAYED.value,
            entitlement_status=(ent.entitlement_status.value if ent else "NOT_ENTITLED"),
            license=(ent.license.value if ent else "NONE"),
            quality_status=classify_bar_quality(b.open, b.high, b.low, b.close, b.volume).value,
            adjustment_policy=b.adjustment_policy.value, corporate_action_version=b.corporate_action_version)
        if obs.ts is None:
            continue
        store.md_append_bar({
            "instrument_id": obs.instrument_id, "provider": obs.provider, "provider_instrument_id": pid,
            "interval": obs.interval, "ts": obs.ts, "open": obs.open, "high": obs.high, "low": obs.low,
            "close": obs.close, "volume": obs.volume, "trade_count": obs.trade_count,
            "data_currency": obs.data_currency, "source_ts": obs.source_ts, "receive_ts": obs.receive_ts,
            "latency_ms": None, "data_status": obs.data_status, "entitlement_status": obs.entitlement_status,
            "license": obs.license, "quality_status": obs.quality_status,
            "adjustment_policy": obs.adjustment_policy,
            "corporate_action_version": obs.corporate_action_version, "provenance_checksum": obs.checksum,
        }, run_id=run_id)


def _ingest_corporate_actions(store, provider, ref, pid, run_id, now, config) -> None:
    for ca in provider.get_corporate_actions(ref, start=config.ca_start, end=config.ca_end):
        obs = CorporateAction(
            instrument_id=ref.instrument_id, provider=provider.name, action_type=ca.action_type,
            effective_date=ca.effective_date, corporate_action_version=ca.corporate_action_version,
            ex_date=ca.ex_date, ratio=ca.ratio, cash_amount=ca.cash_amount, currency=ca.currency)
        store.md_append_corporate_action({
            "instrument_id": obs.instrument_id, "provider": obs.provider, "action_type": obs.action_type,
            "effective_date": obs.effective_date, "corporate_action_version": obs.corporate_action_version,
            "ex_date": obs.ex_date, "ratio": obs.ratio, "cash_amount": obs.cash_amount,
            "currency": obs.currency, "provenance_checksum": obs.checksum,
        }, run_id=run_id)


def _summary(run) -> IngestSummary:
    return IngestSummary(
        run_id=run.run_id, status=run.status, processed=run.processed_count,
        quotes_written=run.quotes_written_count, bars_written=run.bars_written_count,
        history_appended=run.history_appended_count, no_data=run.no_data_count, skipped=run.skipped_count,
        error=run.error_count, completed_markets=json.loads(run.completed_markets_json),
        failed_markets=json.loads(run.failed_markets_json))
