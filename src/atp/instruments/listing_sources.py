"""Parsers for official exchange listing files.

Listing rows are candidates, not tradeable contracts.  They receive a stable IBKR ``conId``
only after qualification by :mod:`atp.instruments.ibkr_catalog`.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True, slots=True)
class ListingCandidate:
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    description: str
    lot_size: float = 1.0
    source: str = ""


_US_EXCHANGES = {
    "A": "NYSEAMER",
    "N": "NYSE",
    "P": "ARCA",
    "Z": "BATS",
    "V": "IEX",
}


def read_nasdaq_listings(path: str | Path) -> list[ListingCandidate]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return parse_nasdaq_listings(handle)


def read_other_us_listings(path: str | Path) -> list[ListingCandidate]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return parse_other_us_listings(handle)


def parse_nasdaq_listings(handle: TextIO) -> list[ListingCandidate]:
    rows = csv.DictReader(handle, delimiter="|")
    result = []
    for row in rows:
        symbol = (row.get("Symbol") or "").strip()
        if not symbol or symbol.startswith("File Creation Time") or row.get("Test Issue") == "Y":
            continue
        result.append(
            ListingCandidate(
                symbol=symbol,
                sec_type="ETF" if row.get("ETF") == "Y" else "STK",
                exchange="NASDAQ",
                currency="USD",
                description=(row.get("Security Name") or "").strip(),
                lot_size=_number(row.get("Round Lot Size"), 1.0),
                source="NASDAQ Trader nasdaqlisted",
            )
        )
    return result


def parse_other_us_listings(handle: TextIO) -> list[ListingCandidate]:
    rows = csv.DictReader(handle, delimiter="|")
    result = []
    for row in rows:
        symbol = (row.get("ACT Symbol") or "").strip()
        if not symbol or symbol.startswith("File Creation Time") or row.get("Test Issue") == "Y":
            continue
        exchange_code = (row.get("Exchange") or "").strip()
        result.append(
            ListingCandidate(
                symbol=symbol,
                sec_type="ETF" if row.get("ETF") == "Y" else "STK",
                exchange=_US_EXCHANGES.get(exchange_code, exchange_code),
                currency="USD",
                description=(row.get("Security Name") or "").strip(),
                lot_size=_number(row.get("Round Lot Size"), 1.0),
                source="NASDAQ Trader otherlisted",
            )
        )
    return result


def deduplicate_listings(rows: list[ListingCandidate]) -> list[ListingCandidate]:
    unique = {(row.symbol, row.exchange, row.sec_type, row.currency): row for row in rows}
    return sorted(unique.values(), key=lambda row: (row.exchange, row.symbol, row.sec_type))


def _number(value: str | None, fallback: float) -> float:
    try:
        return float(value or fallback)
    except (TypeError, ValueError):
        return fallback

