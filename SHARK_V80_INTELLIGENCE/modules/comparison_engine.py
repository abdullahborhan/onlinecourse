"""
Historical comparison engine.

Compares the current period's key metrics with a previous comparable period
(stored in SQLite), reporting absolute and percentage changes, the direction
(IMPROVING / DECLINING / UNCHANGED) and explicit compatibility limitations.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from modules.common import Finding

# metric key -> (label, higher_is_better)
KEY_METRICS = {
    "net_profit": ("Net profit", True),
    "profit_factor": ("Profit factor", True),
    "win_rate": ("Win rate %", True),
    "expectancy": ("Expectancy / trade", True),
    "total_return_pct": ("Total return %", True),
    "total_closed_trades": ("Closed trades", None),
    "max_balance_drawdown_pct": ("Max balance drawdown %", False),
    "max_equity_drawdown_pct": ("Max equity drawdown %", False),
    "average_risk_per_trade_pct": ("Avg risk / trade %", False),
    "max_simultaneous_positions": ("Max simultaneous positions", False),
    "max_consecutive_losses": ("Max consecutive losses", False),
    "transaction_costs": ("Transaction costs", True),  # less negative is better
    "buy_volume_share_pct": ("Buy volume share %", None),
    "avg_layers_per_basket": ("Avg layers per basket", None),
}


def _num(v):
    if isinstance(v, dict):
        v = v.get("value")
    if v in (None, "INF", "-INF") or isinstance(v, str):
        return None
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


def compatibility(cur_meta: dict, prev_meta: dict) -> list[str]:
    lim = []
    if cur_meta.get("period_type") != prev_meta.get("period_type"):
        lim.append("Different period types.")
    cd, pd_ = cur_meta.get("period_days"), prev_meta.get("period_days")
    if cd and pd_ and abs(cd - pd_) > 0.01:
        lim.append(f"Different period lengths ({cd:g} vs {pd_:g} days); counts and totals are "
                   "not directly comparable.")
    if cur_meta.get("period_start") and prev_meta.get("period_end") and \
            pd.Timestamp(prev_meta["period_end"]) > pd.Timestamp(cur_meta["period_start"]):
        lim.append("Periods overlap; shared trades appear in both.")
    if cur_meta.get("equity_available") != prev_meta.get("equity_available"):
        lim.append("Equity observations available in only one period: equity drawdown not "
                   "comparable.")
    cc, pc = cur_meta.get("classification_coverage_pct"), prev_meta.get(
        "classification_coverage_pct")
    if cc is not None and pc is not None and abs(cc - pc) > 10:
        lim.append(f"Strategy classification coverage differs ({cc:.0f}% vs {pc:.0f}%).")
    if cur_meta.get("costs_complete") != prev_meta.get("costs_complete"):
        lim.append("Cost data completeness differs; net profit bases differ.")
    if cur_meta.get("trade_source") != prev_meta.get("trade_source"):
        lim.append("Trades reconstructed from different source types.")
    return lim


def compare(cur: dict, prev: dict | None, cur_meta: dict, prev_meta: dict | None,
            label: str = "previous period") -> dict:
    out = {"available": prev is not None, "rows": [], "limitations": [], "findings": [],
           "label": label}
    if prev is None:
        out["note"] = f"No stored {label} report to compare with."
        return out
    out["limitations"] = compatibility(cur_meta, prev_meta or {})
    out["previous_period"] = {"start": (prev_meta or {}).get("period_start"),
                              "end": (prev_meta or {}).get("period_end")}
    rows = []
    for k, (name, better) in KEY_METRICS.items():
        c, p = _num(cur.get(k)), _num(prev.get(k))
        if c is None or p is None:
            rows.append({"metric": name, "key": k, "current": c, "previous": p, "abs_change": None,
                         "pct_change": None, "direction": "N/A"})
            continue
        diff = c - p
        pct = diff / abs(p) * 100 if p != 0 else None
        if better is None or abs(diff) < 1e-12:
            direction = "UNCHANGED" if abs(diff) < 1e-12 else "CHANGED"
        else:
            direction = "IMPROVING" if (diff > 0) == better else "DECLINING"
        rows.append({"metric": name, "key": k, "current": c, "previous": p, "abs_change": diff,
                     "pct_change": pct, "direction": direction})
    out["rows"] = rows
    R = {r["key"]: r for r in rows}

    prof = [R[k]["direction"] for k in ("net_profit", "profit_factor", "expectancy")
            if R[k]["direction"] in ("IMPROVING", "DECLINING")]
    if prof:
        out["profitability_trend"] = "IMPROVING" if prof.count("IMPROVING") > len(prof) / 2 else \
            "DECLINING" if prof.count("DECLINING") > len(prof) / 2 else "MIXED"
    risk_up = [R[k]["metric"] for k in ("max_balance_drawdown_pct", "max_equity_drawdown_pct",
                                        "average_risk_per_trade_pct", "max_simultaneous_positions",
                                        "max_consecutive_losses")
               if R[k]["direction"] == "DECLINING"]
    out["risk_increasing"] = risk_up
    if out.get("profitability_trend") == "DECLINING":
        out["findings"].append(Finding("comparison", "MEDIUM",
                                       f"Profitability declined vs {label}: " + "; ".join(
                                           f"{R[k]['metric']} {R[k]['previous']:.2f} → "
                                           f"{R[k]['current']:.2f}"
                                           for k in ("net_profit", "profit_factor", "expectancy")
                                           if R[k]["direction"] == "DECLINING") + ".",
                                       [], {}, "PROFITABILITY_DECLINE"))
    if risk_up:
        out["findings"].append(Finding("comparison", "MEDIUM",
                                       f"Risk indicators increased vs {label}: "
                                       + ", ".join(risk_up) + ".", [], {}, "RISK_INCREASE"))
    bs = R["buy_volume_share_pct"]
    if bs["abs_change"] is not None and abs(bs["abs_change"]) >= 20:
        out["findings"].append(Finding("comparison", "LOW",
                                       f"Exposure pattern changed: buy volume share "
                                       f"{bs['previous']:.0f}% → {bs['current']:.0f}%.", [], {},
                                       "EXPOSURE_PATTERN_CHANGE"))

    # Strategy deterioration
    cs, ps = cur.get("strategy", {}), prev.get("strategy", {})
    strat_rows = []
    for s in sorted(set(cs) | set(ps)):
        c_pf, p_pf = _num(cs.get(s, {}).get("profit_factor")), _num(ps.get(s, {}).get(
            "profit_factor"))
        c_np, p_np = _num(cs.get(s, {}).get("net_profit")), _num(ps.get(s, {}).get("net_profit"))
        strat_rows.append({"strategy": s, "pf_current": c_pf, "pf_previous": p_pf,
                           "net_current": c_np, "net_previous": p_np})
        if c_pf is not None and p_pf is not None and p_pf >= 1 > c_pf:
            out["findings"].append(Finding("comparison", "MEDIUM",
                                           f"{s} deteriorated: profit factor {p_pf:.2f} → "
                                           f"{c_pf:.2f}.", [], {}, f"{s}_DETERIORATION"))
    out["strategy_rows"] = strat_rows
    if out["limitations"]:
        out["findings"].append(Finding("comparison", "INFO",
                                       "Comparison limitations: " + " ".join(out["limitations"]),
                                       [], {}, "COMPARISON_LIMITATIONS"))
    return out


def historical_strategy_baseline(history_metrics: list[dict]) -> dict:
    """Mean profit factor / net per strategy across stored historical reports."""
    acc: dict = {}
    for m in history_metrics:
        for s, v in (m.get("strategy") or {}).items():
            pf = _num(v.get("profit_factor"))
            if pf is not None:
                acc.setdefault(s, []).append(pf)
    return {s: {"mean_profit_factor": float(np.mean(v)), "periods": len(v)}
            for s, v in acc.items()}
