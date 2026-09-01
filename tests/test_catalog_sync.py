from atp.instruments.catalog_sync import build_us_snapshot


def test_build_us_snapshot_reports_discovered_but_not_verified_or_ready(tmp_path):
    nasdaq = tmp_path / "nasdaq.txt"
    nasdaq.write_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "AAPL|Apple Inc.|Q|N|N|100|N|N\n"
        "QQQ|Invesco QQQ|G|N|N|100|Y|N\n",
        encoding="utf-8",
    )
    other = tmp_path / "other.txt"
    other.write_text(
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        "SPY|SPDR S&P 500 ETF|P|SPY|Y|100|N|SPY\n",
        encoding="utf-8",
    )
    region = build_us_snapshot(nasdaq, other)["regions"]["USA"]
    assert region["discovered"] == 3
    assert region["ibkr_verified"] == 0
    assert region["ready"] == 0
    assert region["by_type"] == {"ETF": 2, "STK": 1}
