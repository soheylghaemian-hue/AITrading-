"""Market Universe Scanner tests (§6): the hierarchical funnel filters and ranks correctly."""

from atp.core.enums import AssetClass
from atp.scanner import MarketUniverseScanner, ScanCandidate, ScanConfig


def _c(sym, adv, vol, mom, rel=1.0, spread=5.0):
    return ScanCandidate(f"{sym}:equity", sym, AssetClass.EQUITY, adv, vol, mom, rel, spread)


def test_liquidity_filter_drops_thin_names():
    scanner = MarketUniverseScanner(ScanConfig(min_adv=1000, max_spread_bps=50))
    res = scanner.scan([
        _c("LIQUID", adv=5000, vol=0.02, mom=0.05),
        _c("THIN", adv=100, vol=0.02, mom=0.05),          # below min_adv
        _c("WIDE", adv=5000, vol=0.02, mom=0.05, spread=200),  # spread too wide
    ])
    assert res.universe == 3 and res.after_liquidity == 1
    assert [c.symbol for c in res.selected] == ["LIQUID"]


def test_volatility_band_filters():
    scanner = MarketUniverseScanner(ScanConfig(min_volatility=0.01, max_volatility=0.05))
    res = scanner.scan([
        _c("OK", 5000, vol=0.03, mom=0.05),
        _c("DEAD", 5000, vol=0.001, mom=0.05),            # too quiet
        _c("WILD", 5000, vol=0.20, mom=0.05),             # too volatile
    ])
    assert res.after_volatility == 1
    assert [c.symbol for c in res.selected] == ["OK"]


def test_momentum_and_anomaly_filter():
    scanner = MarketUniverseScanner(ScanConfig(min_abs_momentum=0.03, min_rel_volume=1.5))
    res = scanner.scan([
        _c("MOVER", 5000, 0.03, mom=0.08, rel=2.0),
        _c("FLAT", 5000, 0.03, mom=0.001, rel=2.0),       # no momentum
        _c("NOVOL", 5000, 0.03, mom=0.08, rel=1.0),       # no volume anomaly
    ])
    assert res.after_momentum == 1
    assert res.selected[0].symbol == "MOVER"


def test_ranking_and_top_n():
    scanner = MarketUniverseScanner(ScanConfig(top_n=2))
    res = scanner.scan([
        _c("A", 5000, 0.03, mom=0.02),
        _c("B", 5000, 0.03, mom=0.10),                    # strongest momentum
        _c("C", 5000, 0.03, mom=0.06, rel=3.0),           # boosted by volume anomaly
    ])
    assert len(res.selected) == 2                          # top_n cap
    assert res.selected[0].symbol in ("B", "C")            # highest score first


def test_funnel_counts_reported():
    scanner = MarketUniverseScanner(ScanConfig(min_adv=1000, min_abs_momentum=0.02))
    res = scanner.scan([_c("A", 5000, 0.03, 0.05), _c("B", 100, 0.03, 0.05)])
    f = res.funnel()
    assert f["universe"] == 2 and f["after_liquidity"] == 1 and f["selected"] == 1
