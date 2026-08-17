"""§ R3.1A — READ-ONLY legacy integrity diagnostic (never repairs/rewrites/deletes legacy rows).

Proves and reports the audited defect: legacy `ai_governance_results` rows can reference `ai_predictions`
that were never persisted (the prediction snapshotter skips NO-DATA/low-score states while governance is
still recorded), and the aggregate prediction count can diverge from the per-symbol sums (the NVDA
governance-vs-prediction discrepancy). This is pure observation — the new R3.1A schema's FK + single-tx
write make the defect impossible in research rows going forward.
"""
from __future__ import annotations


def reconcile_legacy(store) -> dict:
    gov = store._all("SELECT prediction_id, symbol FROM ai_governance_results")
    pred_ids = {r[0] for r in store._all("SELECT id FROM ai_predictions")}
    orphans = [{"prediction_id": pid, "symbol": sym} for pid, sym in gov if pid not in pred_ids]

    pred_by_sym = {r[0]: int(r[1]) for r in
                   store._all("SELECT symbol, COUNT(*) FROM ai_predictions GROUP BY symbol")}
    gov_by_sym = {r[0]: int(r[1]) for r in
                  store._all("SELECT symbol, COUNT(*) FROM ai_governance_results GROUP BY symbol")}
    agg_pred = int((store._one("SELECT COUNT(*) FROM ai_predictions") or [0])[0])

    per_symbol = []
    for sym in sorted(set(pred_by_sym) | set(gov_by_sym)):
        p, gcount = pred_by_sym.get(sym, 0), gov_by_sym.get(sym, 0)
        per_symbol.append({"symbol": sym, "predictions": p, "governance": gcount,
                           "governance_without_predictions": gcount > 0 and p == 0})
    return {
        "governance_orphans": orphans,
        "governance_orphan_count": len(orphans),
        "aggregate_prediction_count": agg_pred,
        "sum_per_symbol_predictions": sum(pred_by_sym.values()),
        "aggregate_mismatch": agg_pred != sum(pred_by_sym.values()),
        "per_symbol": per_symbol,
        "nvda_governance_count": gov_by_sym.get("NVDA", 0),
        "nvda_prediction_count": pred_by_sym.get("NVDA", 0),
        "note": "READ-ONLY diagnostic; legacy rows are never modified. New R3.1A rows enforce a snapshot FK "
                "+ atomic write so a decision/outcome without a persisted snapshot is impossible.",
    }
