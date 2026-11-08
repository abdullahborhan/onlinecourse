"""
Execution quality analyser.

Only measures what the imported data supports:
* Commission / swap / fee   - from deals (VERIFIED when present).
* Spread                    - only from EA log lines that state the spread.
* Entry slippage            - only where an EA log states the requested price
                              for the same ticket (reference price required).
* Exit deviation            - for exits MT5 marks as [sl]/[tp]: fill vs order level.
* Rejections                - only from EA/journal logs.
* Execution delay           - signal log line -> order log line (both log clock).
Everything else is UNAVAILABLE. Broker execution quality is never estimated
without reference data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from modules.common import CALCULATED, VERIFIED, Finding, Metric, unavailable


def analyze_execution(period_trades: pd.DataFrame, ea_events: pd.DataFrame | None,
                      start, end, materiality_pct: float) -> dict:
    out: dict = {"metrics": {}, "findings": [], "by_strategy": pd.DataFrame()}
    M = out["metrics"]
    t = period_trades[period_trades["status"] == "CLOSED"] if not period_trades.empty \
        else period_trades

    # ---------------- transaction costs
    if t.empty:
        M["transaction_costs"] = unavailable("No closed trades.")
    else:
        avail = {c: t[c].notna().any() for c in ("commission", "swap", "fee")}
        costs = float(sum(t[c].fillna(0).sum() for c in avail if avail[c]))
        pre = float(t["profit"].sum())
        M["commission_total"] = Metric(float(t["commission"].sum()), VERIFIED, "ccy") \
            if avail["commission"] else unavailable("Commission not in source.", "ccy")
        M["swap_total"] = Metric(float(t["swap"].sum()), VERIFIED, "ccy") \
            if avail["swap"] else unavailable("Swap not in source.", "ccy")
        M["fee_total"] = Metric(float(t["fee"].sum()), VERIFIED, "ccy") \
            if avail["fee"] else unavailable("Fee not in source.", "ccy")
        M["transaction_costs"] = Metric(costs, CALCULATED, "ccy",
                                        basis="Σ available commission+swap+fee",
                                        note="" if all(avail.values()) else
                                        "Some cost types not provided: "
                                        + ", ".join(k for k, v in avail.items() if not v))
        M["profit_before_costs"] = Metric(pre, CALCULATED, "ccy")
        gross_pre = float(t.loc[t["profit"] > 0, "profit"].sum())
        if gross_pre > 0:
            drag = abs(costs) / gross_pre * 100
            M["cost_pct_of_gross_profit"] = Metric(drag, CALCULATED, "%",
                                                   basis="|costs| / gross profit before costs")
            material = drag >= materiality_pct or (pre > 0 and pre + costs <= 0)
            out["costs_material"] = bool(material)
            if material:
                out["findings"].append(Finding(
                    "execution", "MEDIUM",
                    f"Transaction costs ({costs:.2f}) equal {drag:.1f}% of gross profit before "
                    f"costs; materiality threshold {materiality_pct}%."
                    + (" Costs turn a pre-cost profit into a net loss." if pre > 0 and
                       pre + costs <= 0 else ""),
                    ["High trade frequency with small targets amplifies fixed costs."],
                    {"costs": costs, "profit_before_costs": pre}, "COSTS_MATERIAL"))
        if "strategy" in t:
            by = t.groupby("strategy").agg(trades=("position_id", "count"),
                                           profit_before_costs=("profit", "sum"),
                                           commission=("commission", "sum"),
                                           swap=("swap", "sum"), net=("net_profit", "sum"))
            out["by_strategy"] = by.reset_index()

    # ---------------- exit deviation on SL/TP exits
    if not t.empty and t["exit_reason"].isin(["SL", "TP"]).any():
        rows = []
        for _, r in t[t["exit_reason"].isin(["SL", "TP"])].iterrows():
            level = r["sl"] if r["exit_reason"] == "SL" else r["tp"]
            if pd.isna(level) or level == 0 or pd.isna(r["exit_price"]):
                continue
            sign = 1 if r["direction"] == "buy" else -1
            rows.append((r["exit_price"] - level) * sign)  # negative = adverse
        if rows:
            M["avg_exit_deviation_points"] = Metric(float(np.mean(rows)), CALCULATED, "pts",
                                                    basis="fill vs SL/TP level; negative = "
                                                          "adverse")
    else:
        M["avg_exit_deviation_points"] = unavailable("Exit reasons ([sl]/[tp]) not in source.")

    # ---------------- log-based measures
    ev = ea_events if ea_events is not None else pd.DataFrame()
    if not ev.empty:
        ev = ev[(ev["time"] >= pd.Timestamp(start)) & (ev["time"] < pd.Timestamp(end))] \
            if ev["time"].notna().any() else ev.iloc[0:0]
    if ev.empty:
        for k in ("average_spread", "order_rejections", "avg_entry_slippage_points",
                  "avg_execution_delay_s"):
            M[k] = unavailable("No EA/journal logs imported for this period.")
        out["log_coverage"] = False
        return out
    out["log_coverage"] = True
    sp = ev["spread"].dropna()
    M["average_spread"] = Metric(float(sp.mean()), VERIFIED, "pts",
                                 basis=f"{len(sp)} logged spread values") if len(sp) else \
        unavailable("Logs do not state spread.")
    if len(sp):
        M["max_spread"] = Metric(float(sp.max()), VERIFIED, "pts")
    rej = int((ev["event_type"] == "REJECTION").sum())
    M["order_rejections"] = Metric(rej, VERIFIED, "count", basis="REJECTION events in logs")
    if rej:
        out["findings"].append(Finding("execution", "LOW" if rej < 5 else "MEDIUM",
                                       f"{rej} order rejection/error line(s) in EA logs.",
                                       ["Requotes or invalid stops during fast markets."],
                                       {"rejections": rej}, "ORDER_REJECTIONS"))
    req = ev[ev["requested_price"].notna() & ev["ticket"].notna()]
    if not req.empty and not t.empty:
        tt = period_trades.set_index(period_trades["position_id"].astype(str))
        slips = []
        for tk, px in zip(req["ticket"].astype(str), req["requested_price"]):
            if tk in tt.index and pd.notna(tt.loc[tk, "entry_price"]):
                sign = 1 if tt.loc[tk, "direction"] == "buy" else -1
                slips.append((tt.loc[tk, "entry_price"] - px) * sign)  # positive = adverse
        M["avg_entry_slippage_points"] = Metric(float(np.mean(slips)), CALCULATED, "pts",
                                                basis=f"{len(slips)} fills vs logged requested "
                                                      "price; positive = adverse") \
            if slips else unavailable("No requested prices matched to tickets.")
    else:
        M["avg_entry_slippage_points"] = unavailable("Logs do not state requested prices.")
    sig = ev[ev["event_type"] == "SIGNAL"].sort_values("time")
    orders = ev[(ev["event_type"] == "ORDER") & ev["ticket"].notna()].sort_values("time")
    delays = []
    for _, o in orders.iterrows():
        prior = sig[(sig["time"] <= o["time"]) &
                    (sig["time"] >= o["time"] - pd.Timedelta(seconds=60))]
        if o["strategy"]:
            prior = prior[prior["strategy"] == o["strategy"]]
        if not prior.empty:
            delays.append((o["time"] - prior["time"].iloc[-1]).total_seconds())
    M["avg_execution_delay_s"] = Metric(float(np.mean(delays)), CALCULATED, "s",
                                        basis=f"{len(delays)} signal→order pairs (log clock)") \
        if delays else unavailable("No signal/order log pairs found.")
    return out
