"""Refresh official exchange listings into a durable, read-only catalogue snapshot."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlretrieve

from .listing_sources import deduplicate_listings, read_nasdaq_listings, read_other_us_listings

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_US_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
DEFAULT_OUTPUT = "/opt/atp/data/market-catalog.json"


def build_us_snapshot(nasdaq_file: str | Path, other_file: str | Path) -> dict:
    rows = deduplicate_listings(
        read_nasdaq_listings(nasdaq_file) + read_other_us_listings(other_file)
    )
    by_exchange = Counter(row.exchange for row in rows)
    by_type = Counter(row.sec_type for row in rows)
    return {
        "status": "DISCOVERED",
        "generated_at": datetime.now(UTC).isoformat(),
        "regions": {
            "USA": {
                "discovered": len(rows),
                "ibkr_verified": 0,
                "ready": 0,
                "by_exchange": dict(sorted(by_exchange.items())),
                "by_type": dict(sorted(by_type.items())),
                "sources": ["NASDAQ Trader nasdaqlisted", "NASDAQ Trader otherlisted"],
            }
        },
    }


def refresh(output: str | Path = DEFAULT_OUTPUT) -> dict:
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="atp-market-catalog-") as work:
        nasdaq = Path(work) / "nasdaqlisted.txt"
        other = Path(work) / "otherlisted.txt"
        urlretrieve(NASDAQ_LISTED_URL, nasdaq)
        urlretrieve(OTHER_US_LISTED_URL, other)
        snapshot = build_us_snapshot(nasdaq, other)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent, delete=False) as tmp:
        json.dump(snapshot, tmp, sort_keys=True, separators=(",", ":"))
        tmp.write("\n")
        temporary = tmp.name
    os.replace(temporary, destination)
    return snapshot


def main() -> None:
    output = os.environ.get("ATP_MARKET_CATALOG_PATH", DEFAULT_OUTPUT)
    snapshot = refresh(output)
    print(json.dumps(snapshot, sort_keys=True))


if __name__ == "__main__":
    main()
