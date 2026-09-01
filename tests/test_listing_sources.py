from io import StringIO

from atp.instruments.listing_sources import (
    deduplicate_listings,
    parse_nasdaq_listings,
    parse_other_us_listings,
)


def test_parses_complete_nasdaq_file_and_excludes_test_rows():
    source = StringIO(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "AAPL|Apple Inc.|Q|N|N|100|N|N\n"
        "QQQ|Invesco QQQ Trust|G|N|N|100|Y|N\n"
        "ZZTEST|Test Security|G|Y|N|100|N|N\n"
        "File Creation Time: 0901202612:00|||||||\n"
    )
    rows = parse_nasdaq_listings(source)
    assert [(row.symbol, row.sec_type, row.exchange) for row in rows] == [
        ("AAPL", "STK", "NASDAQ"),
        ("QQQ", "ETF", "NASDAQ"),
    ]


def test_parses_other_us_exchanges_and_maps_official_exchange_codes():
    source = StringIO(
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        "A|Agilent Technologies|N|A|N|100|N|A\n"
        "SPY|SPDR S&P 500 ETF|P|SPY|Y|100|N|SPY\n"
    )
    rows = parse_other_us_listings(source)
    assert [(row.symbol, row.sec_type, row.exchange) for row in rows] == [
        ("A", "STK", "NYSE"),
        ("SPY", "ETF", "ARCA"),
    ]


def test_deduplicates_only_identical_venue_contract_candidates():
    source = StringIO(
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        "A|Agilent|N|A|N|100|N|A\n"
        "A|Agilent|N|A|N|100|N|A\n"
    )
    rows = parse_other_us_listings(source)
    assert len(deduplicate_listings(rows)) == 1
