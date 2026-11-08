"""
Drawdown engine.

Three distinct, never-conflated measures:

1. Balance drawdown (CALCULATED)  - from the realised balance curve (deal-level
   Balance column, or cumulative closed-trade results).
2. Equity drawdown   (CALCULATED)  - only from imported equity observations;
   accuracy is limited by their sampling interval. UNAVAILABLE otherwise.
3. Closed-trade equity estimate (ESTIMATED) - the balance drawdown re-labelled
   as a LOWER BOUND for equity drawdown, because floating (intratrade) losses
   are not observable from closed trades. Never presented as actual equity DD.

Deposits/withdrawals are removed before measuring, so a withdrawal is not a
drawdown and a deposit is not a recovery. Percentages are relative to the
account balance/equity at the peak.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from modules.common import CALCULATED, ESTIMATED, UNAVAILABLE, Metric, unavailable
from modules.normalizer import TRADE_TYPES


def _drawdown_stats(times: pd.Series, level: pd.Series, flows_cum: pd.Series,
                    min_episode_pct: float) -> dict:
    """Generic drawdown on ``level`` (account value) with cumulative external flows."""
    times = pd.to_datetime(pd.Series(times)).reset_index(drop=True)
    level = pd.Series(level, dtype="float64").reset_index(drop=True)
    flows_cum = pd.Series(flows_cum, dtype="float64").reset_index(drop=True)
    pl = level - flows_cum  # trading performance curve, flow-neutral
    run_max = pl.cummax()
    dd_money = run_max - pl
    # A new peak starts only when the curve strictly exceeds its previous maximum
    new_peak = pl > run_max.shift(1).fillna(-np.inf)
    peak_idx = pd.Series(pl.index, index=pl.index).where(new_peak).ffill().astype(int)
    peak_level = level.iloc[peak_idx.values].reset_index(drop=True) if len(level) else level
    # % relative to the account value at the peak (adjusted for later flows)
    flow_since_peak = flows_cum - flows_cum.iloc[peak_idx.values].reset_index(drop=True)
    denom = (peak_level + flow_since_peak).where(lambda s: s > 0)
    dd_pct = (dd_money / denom * 100).fillna(0.0)

    if dd_money.empty or dd_money.max() <= 0:
        return {"max_dd_money": 0.0, "max_dd_pct": 0.0, "peak_time": None, "trough_time": None,
                "recovery_time": None, "recovered": True, "recovery_duration_s": 0.0,
                "episodes": 0, "episodes_detail": [], "current_dd_money": 0.0,
                "current_dd_pct": 0.0, "observations": int(len(level))}

    i_trough = int(dd_pct.idxmax()) if denom.notna().any() else int(dd_money.idxmax())
    i_peak = int(peak_idx.iloc[i_trough])
    after = pl.iloc[i_trough:]
    rec = after[after >= run_max.iloc[i_trough] - 1e-9]
    recovered = not rec.empty
    rec_time = times.iloc[rec.index[0]] if recovered else None

    # Episodes: from a peak until the curve regains it
    episodes, cur = [], None
    for i in range(len(pl)):
        if dd_money.iloc[i] > 1e-9:
            if cur is None:
                cur = {"start": times.iloc[int(peak_idx.iloc[i])], "max_pct": 0.0,
                       "max_money": 0.0}
            cur["max_pct"] = max(cur["max_pct"], float(dd_pct.iloc[i]))
            cur["max_money"] = max(cur["max_money"], float(dd_money.iloc[i]))
        elif cur is not None:
            cur["end"] = times.iloc[i]
            episodes.append(cur)
            cur = None
    if cur is not None:
        cur["end"] = None
        episodes.append(cur)
    significant = [e for e in episodes if e["max_pct"] >= min_episode_pct]

    return {
        "max_dd_money": float(dd_money.iloc[i_trough]),
        "max_dd_pct": float(dd_pct.iloc[i_trough]),
        "max_dd_money_any": float(dd_money.max()),
        "peak_time": times.iloc[i_peak], "trough_time": times.iloc[i_trough],
        "recovery_time": rec_time, "recovered": recovered,
        "recovery_duration_s": float((rec_time - times.iloc[i_trough]).total_seconds())
        if recovered else None,
        "episodes": len(significant), "episodes_all": len(episodes),
        "episodes_detail": [{"start": str(e["start"]), "end": str(e["end"]) if e["end"] else
                             "ONGOING", "max_pct": round(e["max_pct"], 4),
                             "max_money": round(e["max_money"], 2)} for e in significant],
        "current_dd_money": float(dd_money.iloc[-1]), "current_dd_pct": float(dd_pct.iloc[-1]),
        "observations": int(len(level)),
    }


def balance_curve(deals: pd.DataFrame, trades: pd.DataFrame,
                  start=None, end=None) -> tuple[pd.DataFrame, str]:
    """Realised balance curve with cumulative external cash flows.

    Returns (DataFrame[time, balance, flows_cum, pl], basis).
    """
    if deals is not None and not deals.empty and deals["balance"].notna().mean() > 0.95:
        d = deals.sort_values(["time", "deal_id"], kind="stable").copy()
        d["flow"] = np.where(~d["deal_type"].isin(TRADE_TYPES),
                             d[["profit", "commission", "swap", "fee"]].fillna(0).sum(axis=1), 0.0)
        d["flows_cum"] = d["flow"].cumsum()
        curve = d[["time", "balance", "flows_cum"]].copy()
        basis = "DEAL_BALANCE_COLUMN"
    elif deals is not None and not deals.empty and deals.iloc[0]["deal_type"] == "balance":
        d = deals.sort_values(["time", "deal_id"], kind="stable").copy()
        delta = d[["profit", "commission", "swap", "fee"]].fillna(0).sum(axis=1)
        d["balance"] = delta.cumsum()
        d["flows_cum"] = np.where(~d["deal_type"].isin(TRADE_TYPES), delta, 0.0).cumsum()
        curve = d[["time", "balance", "flows_cum"]].copy()
        basis = "DERIVED_FROM_INCEPTION"
    elif trades is not None and not trades.empty:
        t = trades[trades["status"] == "CLOSED"].sort_values("close_time")
        curve = pd.DataFrame({"time": t["close_time"], "balance": t["net_profit"].cumsum(),
                              "flows_cum": 0.0})
        if not curve.empty:  # anchor at 0 so a first losing trade counts as drawdown
            t0 = pd.Timestamp(start) if start is not None else \
                t["close_time"].min() - pd.Timedelta(seconds=1)
            curve = pd.concat([pd.DataFrame({"time": [t0], "balance": [0.0],
                                             "flows_cum": [0.0]}), curve], ignore_index=True)
        basis = "CUMULATIVE_PL_ONLY"  # no balance level -> % unavailable
    else:
        return pd.DataFrame(columns=["time", "balance", "flows_cum", "pl"]), "UNAVAILABLE"
    if start is not None or end is not None:
        before = curve[curve["time"] < pd.Timestamp(start)] if start is not None else curve.iloc[0:0]
        sel = curve.copy()
        if start is not None:
            sel = sel[sel["time"] >= pd.Timestamp(start)]
        if end is not None:
            sel = sel[sel["time"] < pd.Timestamp(end)]
        if not before.empty:  # anchor at the balance at period start
            anchor = before.iloc[[-1]].copy()
            anchor["time"] = pd.Timestamp(start)
            sel = pd.concat([anchor, sel], ignore_index=True)
        curve = sel
    curve = curve.reset_index(drop=True)
    curve["pl"] = curve["balance"] - curve["flows_cum"]
    return curve, basis


def compute_drawdowns(deals: pd.DataFrame, trades: pd.DataFrame, equity: pd.DataFrame | None,
                      start=None, end=None, min_episode_pct: float = 1.0) -> dict:
    out: dict = {}
    curve, basis = balance_curve(deals, trades, start, end)
    out["balance_curve_basis"] = basis
    if curve.empty or len(curve) < 2:
        out["balance"] = {"status": UNAVAILABLE, "note": "Fewer than two balance observations."}
    else:
        st = _drawdown_stats(curve["time"], curve["balance"], curve["flows_cum"], min_episode_pct)
        pct_ok = basis != "CUMULATIVE_PL_ONLY"
        st["status"] = CALCULATED
        st["basis"] = basis
        if not pct_ok:
            st["max_dd_pct"] = None
            st["current_dd_pct"] = None
            st["note"] = "Account balance level unknown: drawdown % unavailable."
        out["balance"] = st
    out["curve"] = curve

    # Absolute drawdown (MT5 definition): initial balance minus lowest balance below it
    if not curve.empty and basis != "CUMULATIVE_PL_ONLY":
        initial = float(curve["pl"].iloc[0])
        low = float(curve["pl"].min())
        out["absolute_drawdown"] = Metric(max(0.0, initial - low), CALCULATED, "ccy",
                                          basis="flow-adjusted balance vs period start")
    else:
        out["absolute_drawdown"] = unavailable("No balance level available.", "ccy")

    # Equity drawdown: only from real equity observations
    eq = equity if equity is not None else pd.DataFrame()
    if not eq.empty:
        e = eq.copy()
        if start is not None:
            e = e[e["time"] >= pd.Timestamp(start)]
        if end is not None:
            e = e[e["time"] < pd.Timestamp(end)]
    else:
        e = eq
    if e is not None and len(e) >= 2:
        e = e.sort_values("time").reset_index(drop=True)
        flows = pd.Series(0.0, index=e.index)
        if deals is not None and not deals.empty:
            ops = deals[~deals["deal_type"].isin(TRADE_TYPES)].sort_values("time")
            if not ops.empty:
                amt = ops[["profit", "commission", "swap", "fee"]].fillna(0).sum(axis=1)
                cum = pd.Series(amt.cumsum().values, index=ops["time"].values)
                base = cum[cum.index < e["time"].iloc[0]]
                base_v = float(base.iloc[-1]) if len(base) else 0.0
                flows = e["time"].map(lambda t: float(cum[cum.index <= t].iloc[-1])
                                      if (cum.index <= t).any() else 0.0) - base_v
        st = _drawdown_stats(e["time"], e["equity"], flows, min_episode_pct)
        gaps = e["time"].diff().dt.total_seconds()
        st["status"] = CALCULATED
        st["basis"] = "EQUITY_OBSERVATIONS"
        st["sampling_median_s"] = float(gaps.median()) if len(gaps) > 1 else None
        st["note"] = ("Measured on imported equity snapshots; drawdown between snapshots is "
                      "not observed.")
        out["equity"] = st
    else:
        out["equity"] = {"status": UNAVAILABLE,
                         "note": "No equity observations imported for this period. True "
                                 "intratrade equity drawdown cannot be reconstructed from "
                                 "trade history."}

    # Closed-trade estimate, explicitly labelled
    bal = out["balance"]
    if bal.get("status") == CALCULATED:
        out["equity_estimate_closed_trade"] = {
            "status": ESTIMATED, "max_dd_money": bal["max_dd_money"],
            "max_dd_pct": bal.get("max_dd_pct"),
            "note": "Closed-trade estimate = LOWER BOUND of equity drawdown. Floating losses "
                    "of open positions are not included. Not actual account equity drawdown."}
    else:
        out["equity_estimate_closed_trade"] = {"status": UNAVAILABLE}
    return out


def drawdown_metrics(dd: dict) -> dict:
    """Flatten drawdown results into Metric objects for reports."""
    b, e, est = dd["balance"], dd["equity"], dd["equity_estimate_closed_trade"]
    m = {}
    if b.get("status") == CALCULATED:
        m["max_balance_drawdown"] = Metric(b["max_dd_money"], CALCULATED, "ccy", basis=b["basis"])
        m["max_balance_drawdown_pct"] = Metric(b["max_dd_pct"], CALCULATED, "%",
                                               basis=b["basis"]) if b.get("max_dd_pct") is not None \
            else unavailable(b.get("note", ""), "%")
        m["balance_dd_peak_time"] = Metric(str(b["peak_time"]) if b["peak_time"] is not None
                                           else None, CALCULATED)
        m["balance_dd_trough_time"] = Metric(str(b["trough_time"]) if b["trough_time"] is not None
                                             else None, CALCULATED)
        m["recovery_duration"] = Metric(b["recovery_duration_s"], CALCULATED, "s") \
            if b["recovered"] else Metric("NOT RECOVERED", CALCULATED,
                                          note="Balance has not regained its peak.")
        m["drawdown_frequency"] = Metric(b["episodes"], CALCULATED, "count",
                                         note="Episodes deeper than configured minimum %.")
    else:
        for k in ("max_balance_drawdown", "max_balance_drawdown_pct", "recovery_duration",
                  "drawdown_frequency"):
            m[k] = unavailable(b.get("note", "No balance curve."))
    if e.get("status") == CALCULATED:
        m["max_equity_drawdown"] = Metric(e["max_dd_money"], CALCULATED, "ccy", basis=e["basis"],
                                          note=e["note"])
        m["max_equity_drawdown_pct"] = Metric(e["max_dd_pct"], CALCULATED, "%", basis=e["basis"],
                                              note=e["note"])
    else:
        m["max_equity_drawdown"] = unavailable(e["note"], "ccy")
        m["max_equity_drawdown_pct"] = unavailable(e["note"], "%")
    if est.get("status") == ESTIMATED:
        m["closed_trade_equity_dd_estimate_pct"] = Metric(est["max_dd_pct"], ESTIMATED, "%",
                                                          note=est["note"])
    else:
        m["closed_trade_equity_dd_estimate_pct"] = unavailable("No closed-trade curve.", "%")
    m["absolute_drawdown"] = dd["absolute_drawdown"]
    return m
