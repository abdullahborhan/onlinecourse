"""
SHARK V80 strategy classification and comparison.

Classification sources, in priority order (never from profit or direction):
1. EA_LOG     - EA log line with the position/order ticket and a strategy tag.
2. COMMENT    - EA strategy comment matched by configurable patterns.
3. MAGIC_MAP  - user-supplied magic-number -> strategy map (empty by default).
Otherwise UNCLASSIFIED. Conflicts between sources are reported.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from modules.common import (BREAKOUT, MOMENTUM, PULLBACK, STRATEGIES, UNCLASSIFIED, Finding)
from modules.drawdown_engine import _drawdown_stats
from modules.performance_engine import compute_performance


def _from_comment(comment, patterns: dict):
    if not comment or not isinstance(comment, str):
        return None
    hits = [s for s, p in patterns.items() if re.search(p, comment, re.I)]
    return hits[0] if len(hits) == 1 else ("CONFLICT" if len(hits) > 1 else None)


def classify_strategies(trades: pd.DataFrame, ea_events: pd.DataFrame | None,
                        patterns: dict, magic_map: dict) -> tuple[pd.DataFrame, dict]:
    t = trades.copy()
    log_map: dict[str, set] = {}
    if ea_events is not None and not ea_events.empty:
        ev = ea_events[ea_events["ticket"].notna() & ea_events["strategy"].notna()]
        for tk, s in zip(ev["ticket"].astype(str), ev["strategy"]):
            log_map.setdefault(tk, set()).add(s)
    strategies, sources, conflicts = [], [], []
    for _, r in t.iterrows():
        cands = []
        for key in (str(r["position_id"]), str(r.get("open_order_id"))):
            if key in log_map:
                cands.append(("EA_LOG", log_map[key]))
                break
        c = _from_comment(r.get("comment"), patterns)
        m = magic_map.get(str(r.get("magic"))) if r.get("magic") is not None else None
        chosen, src = None, None
        if cands:
            s = cands[0][1]
            if len(s) == 1:
                chosen, src = next(iter(s)), "EA_LOG"
            else:
                conflicts.append({"position_id": r["position_id"], "reason":
                                  f"EA log tags {sorted(s)}"})
        if chosen is None and c and c != "CONFLICT":
            chosen, src = c, "COMMENT"
        elif c == "CONFLICT":
            conflicts.append({"position_id": r["position_id"], "reason": "comment matches "
                              "several strategies"})
        if chosen is None and m in STRATEGIES:
            chosen, src = m, "MAGIC_MAP"
        if chosen and src == "EA_LOG" and c and c not in ("CONFLICT", chosen):
            conflicts.append({"position_id": r["position_id"],
                              "reason": f"EA log={chosen} vs comment={c} (log used)"})
        strategies.append(chosen or UNCLASSIFIED)
        sources.append(src or "NONE")
    t["strategy"] = strategies
    t["strategy_source"] = sources
    n = len(t)
    classified = int((t["strategy"] != UNCLASSIFIED).sum())
    rep = {"total": n, "classified": classified,
           "coverage_pct": classified / n * 100 if n else None,
           "by_source": t["strategy_source"].value_counts().to_dict(),
           "conflicts": conflicts,
           "complete": n > 0 and classified == n}
    return t, rep


def strategy_comparison(period_trades: pd.DataFrame, eps: float, account_dd: dict | None
                        ) -> dict:
    """Per-strategy statistics and BREAKOUT vs MOMENTUM vs PULLBACK comparison."""
    closed = period_trades[period_trades["status"] == "CLOSED"] if not period_trades.empty \
        else period_trades
    out: dict = {"strategies": {}, "table": pd.DataFrame(), "findings": []}
    peak = trough = None
    if account_dd and account_dd.get("status") == "CALCULATED" and account_dd.get("peak_time") \
            is not None:
        peak, trough = account_dd["peak_time"], account_dd["trough_time"]
    rows = []
    for s in (*STRATEGIES, UNCLASSIFIED):
        g = closed[closed["strategy"] == s] if not closed.empty else closed
        perf = compute_performance(g, eps=eps)
        stats = {"perf": perf, "trades": int(len(g))}
        if len(g):
            curve = g.sort_values("close_time")
            st = _drawdown_stats(curve["close_time"], curve["net_profit"].cumsum(),
                                 pd.Series([0.0] * len(curve)), 0.0)
            stats["own_max_dd_money"] = st["max_dd_money"]
            if peak is not None:
                w = g[(g["close_time"] > pd.Timestamp(peak)) &
                      (g["close_time"] <= pd.Timestamp(trough))]
                stats["dd_window_contribution"] = float(w["net_profit"].sum())
            stats["gross_loss"] = float(g.loc[g["net_profit"] < 0, "net_profit"].sum())
            stats["risk_money_total"] = float(g["risk_money"].sum()) \
                if "risk_money" in g and g["risk_money"].notna().any() else None
        out["strategies"][s] = stats
        P, S = perf["profitability"], perf["statistics"]
        rows.append({
            "strategy": s, "trades": len(g),
            "win_rate_pct": S["win_rate"].value if len(g) else np.nan,
            "profit_factor": P["profit_factor"].value if len(g) else np.nan,
            "net_profit": P["net_profit"].value if len(g) else np.nan,
            "expectancy": P["expectancy"].value if len(g) else np.nan,
            "avg_win": P["average_winning_trade"].value if len(g) else np.nan,
            "avg_loss": P["average_losing_trade"].value if len(g) else np.nan,
            "own_max_dd": stats.get("own_max_dd_money", np.nan),
            "dd_window_contribution": stats.get("dd_window_contribution", np.nan),
            "gross_loss": stats.get("gross_loss", np.nan),
            "avg_holding_s": S["average_trade_duration"].value if len(g) else np.nan,
        })
    table = pd.DataFrame(rows)
    out["table"] = table
    known = table[table["strategy"].isin(STRATEGIES) & (table["trades"] > 0)]
    if not known.empty:
        best = known.loc[known["net_profit"].idxmax()]
        out["most_profit"] = {"strategy": best["strategy"], "net_profit": best["net_profit"]}
        if known["gross_loss"].notna().any() and known["gross_loss"].min() < 0:
            risk = known.loc[known["gross_loss"].idxmin()]
            out["most_risk"] = {"strategy": risk["strategy"], "gross_loss": risk["gross_loss"],
                                "basis": "largest gross loss in period"}
        if known["dd_window_contribution"].notna().any():
            dd = known.loc[known["dd_window_contribution"].idxmin()]
            if dd["dd_window_contribution"] < 0:
                out["most_drawdown_contribution"] = {
                    "strategy": dd["strategy"],
                    "contribution": dd["dd_window_contribution"],
                    "basis": "net P/L of trades closed inside the account's max balance "
                             "drawdown window"}
    unc = table.loc[table["strategy"] == UNCLASSIFIED, "trades"].iloc[0]
    if unc:
        out["findings"].append(Finding(
            "strategy", "MEDIUM",
            f"Strategy classification is INCOMPLETE: {unc} trade(s) could not be attributed to "
            "BREAKOUT, MOMENTUM or PULLBACK.",
            ["EA comments may be truncated by the broker.",
             "Upload EA logs containing tickets, or provide a magic-number map."],
            {"unclassified_trades": int(unc)}, "CLASSIFICATION_INCOMPLETE"))
    for s in STRATEGIES:
        r = table[table["strategy"] == s].iloc[0]
        if r["trades"] >= 10 and pd.notna(r["profit_factor"]) and r["profit_factor"] < 1:
            out["findings"].append(Finding(
                "strategy", "MEDIUM",
                f"{s} is unprofitable this period: PF {r['profit_factor']:.2f} over "
                f"{int(r['trades'])} trades, net {r['net_profit']:.2f}.",
                [f"Market regime may not suit {s.lower()} entries this period."], {},
                f"{s}_UNPROFITABLE"))
    return out


__all__ = ["classify_strategies", "strategy_comparison", "BREAKOUT", "MOMENTUM", "PULLBACK"]
