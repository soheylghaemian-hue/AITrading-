"""Demo: macro rates model, FX carry and the macro cycle bias (§5/§8).

    PYTHONPATH=src python3 examples/macro_demo.py

Sets up a small policy-rate table, shows the carry (rate differential) across FX pairs, and the
two rate-driven specialists reacting: FX carry longs the high-yielder, and the macro specialist
leans risk-on into an easing cycle. Numbers are computed from the rates — nothing faked.
"""

from __future__ import annotations

from datetime import datetime, timezone

from atp.core.enums import AssetClass, Regime
from atp.core.events import Instrument
from atp.features.engine import FeatureSet
from atp.macro import RatesTable
from atp.strategy.fx_carry import FXCarryStrategy
from atp.strategy.macro import MacroStrategy

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _fs(instrument, trend=0.0):
    return FeatureSet(instrument=instrument, ts=T0, price=100.0, n_bars=50, ready=True,
                      sma_fast=100.0, sma_slow=100.0, close_std=1.0, trend=trend, ret=0.0,
                      realized_vol=0.01, vol_percentile=0.5, rel_volume=1.0)


def main() -> None:
    rates = RatesTable()
    # Simulate an easing USD over several meetings, plus other-currency levels.
    for x in (0.055, 0.050, 0.045, 0.040):
        rates.set_rate("USD", x)
    rates.set_rate("AUD", 0.045)
    rates.set_rate("JPY", 0.001)
    rates.set_rate("EUR", 0.035)

    print("=" * 60)
    print("  Macro rates & carry (§5)")
    print("=" * 60)
    for ccy in ("USD", "AUD", "JPY", "EUR"):
        print(f"    {ccy} policy rate: {rates.rate(ccy):.2%}")
    print(f"    USD rate trend: {rates.trend('USD'):+.2%}  (easing)")
    print("  carry (base vs quote):")
    for base, quote in (("AUD", "JPY"), ("AUD", "USD"), ("EUR", "USD")):
        print(f"    long {base}/{quote}: {rates.carry(base, quote):+.2%}")

    print("-" * 60)
    print("  FX-carry specialist (§8)")
    audjpy = Instrument("AUD", AssetClass.FX, currency="JPY")
    carry_sig = FXCarryStrategy(rates, min_carry=0.005).generate(_fs(audjpy), Regime.RANGE)
    print(f"    AUD/JPY (carry {rates.carry('AUD','JPY'):+.2%}) => "
          f"{carry_sig.action.value.upper() if carry_sig else 'none'}: "
          f"{carry_sig.rationale if carry_sig else '-'}")

    print("-" * 60)
    print("  Macro specialist (§8): USD easing => risk-on for US equities")
    spx = Instrument("SPX", AssetClass.INDEX, currency="USD")
    macro_sig = MacroStrategy(rates, trend_threshold=0.005).generate(_fs(spx), Regime.RANGE)
    print(f"    SPX => {macro_sig.action.value.upper() if macro_sig else 'none'}: "
          f"{macro_sig.rationale if macro_sig else '-'}")
    print("=" * 60)
    print("  Rates fed by a macro feed in production; carry & cycle are transparent heuristics.")


if __name__ == "__main__":
    main()
