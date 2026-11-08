"""
Risk management compliance engine and risk metrics.

Verdicts
--------
BREACH        - imported records prove the approved limit was violated.
COMPLIANT     - the records needed to verify the rule exist and show no breach.
UNVERIFIABLE  - required data (e.g. intraday equity) is missing; the absence
                of a breach in the available data is NOT presented as compliance.

A lock "working" is never inferred from losses simply staying below a
threshold: lock compliance requires evidence that no new position was opened
after the threshold was reached, measured on the basis the data supports.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from modules.common import (BREACH, CALCULATED, COMPLIANT, NOT_APPLICABLE, UNVERIFIABLE,
                            Finding, Metric, unavailable)
from modules.normalizer import TRADE_TYPES
from modules.performance_engine import streaks, outcome


def _check(name, verdict, summary, basis="", evidence=None, severity=None):
    return {"check": name, "verdict": verdict, "summary": summary, "basis": basis,
            "evidence": evidence or {}, "severity": severity}


def _balance_lookup(deals: pd.DataFrame):
    """Return (sorted balance series indexed by time, flows series) or (None, None)."""
    if deals is None or deals.empty:
        return None, None
    d = deals.sort_values(["time", "deal_id"], kind="stable")
    if d["balance"].notna().mean() > 0.95:
        bal = pd.Series(d["balance"].values, index=d["time"].values)
    elif d.iloc[0]["deal_type"] == "balance":
        bal = pd.Series(d[["profit", "commission", "swap", "fee"]].fillna(0).sum(axis=1)
                        .cumsum().values, index=d["time"].values)
    else:
        return None, None
    ops = d[~d["deal_type"].isin(TRADE_TYPES)]
    flows = pd.Series(ops[["profit", "commission", "swap", "fee"]].fillna(0).sum(axis=1).values,
                      index=ops["time"].values)
    return bal, flows


def _value_before(series: pd.Series | None, t):
    if series is None:
        return np.nan
    prior = series[series.index < np.datetime64(pd.Timestamp(t))]
    return float(prior.iloc[-1]) if len(prior) else np.nan


def opening_balance(deals: pd.DataFrame, t) -> float:
    """Balance at time ``t``: last balance before t; if none, the balance after the
    leading cash-flow-only deals (e.g. the initial deposit at exactly t) that occur
    before the first trading deal."""
    bal, _ = _balance_lookup(deals)
    v = _value_before(bal, t)
    if not np.isnan(v):
        return v
    if bal is None:
        return np.nan
    d = deals.sort_values(["time", "deal_id"], kind="stable")
    d = d[d["time"] >= pd.Timestamp(t)]
    lead = []
    for i, (_, r) in enumerate(d.iterrows()):
        if r["deal_type"] in TRADE_TYPES:
            break
        lead.append(i)
    if not lead:
        return np.nan
    return float(bal[bal.index >= np.datetime64(pd.Timestamp(t))].iloc[lead[-1]])


def _realised_events(deals: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    """Realised P/L events (time, amount): deal-level when available, else trade closes."""
    if deals is not None and not deals.empty:
        t = deals[deals["deal_type"].isin(TRADE_TYPES)]
        amt = t[["profit", "commission", "swap", "fee"]].fillna(0).sum(axis=1)
        ev = pd.DataFrame({"time": t["time"], "amount": amt})
        return ev[ev["amount"] != 0].sort_values("time")
    c = trades[trades["status"] == "CLOSED"]
    return pd.DataFrame({"time": c["close_time"], "amount": c["net_profit"]}).sort_values("time")


def _lock_scan(events, opens, start_value, threshold_pct, sign: int, day_start, day_end):
    """Find when cumulative realised P/L crossed ±threshold and entries after it."""
    e = events[(events["time"] >= day_start) & (events["time"] < day_end)]
    cum = e["amount"].cumsum()
    worst = float(cum.min()) if len(cum) else 0.0
    best = float(cum.max()) if len(cum) else 0.0
    level = threshold_pct / 100 * start_value
    hit = cum[(cum <= -level) if sign < 0 else (cum >= level)]
    hit_time = e.loc[hit.index[0], "time"] if len(hit) else None
    after = opens[(opens > hit_time) & (opens < day_end)] if hit_time is not None else []
    return {"worst_pct": -worst / start_value * 100 if start_value else np.nan,
            "best_pct": best / start_value * 100 if start_value else np.nan,
            "end_pct": float(cum.iloc[-1]) / start_value * 100 if len(cum) and start_value
            else 0.0,
            "hit_time": str(hit_time) if hit_time is not None else None,
            "entries_after_hit": int(len(after))}


def _equity_day_scan(equity, opens, start_eq, threshold_pct, sign, day_start, day_end):
    e = equity[(equity["time"] >= day_start) & (equity["time"] < day_end)]
    if e.empty or not start_eq:
        return None
    chg = (e["equity"] - start_eq) / start_eq * 100
    hit = e[(chg <= -threshold_pct) if sign < 0 else (chg >= threshold_pct)]
    hit_time = hit["time"].iloc[0] if not hit.empty else None
    after = opens[(opens > hit_time) & (opens < day_end)] if hit_time is not None else []
    return {"worst_pct": float(-chg.min()), "best_pct": float(chg.max()),
            "hit_time": str(hit_time) if hit_time is not None else None,
            "entries_after_hit": int(len(after))}


def daily_table(deals, trades, equity, start, end, risk_cfg) -> pd.DataFrame:
    """Per-day realised and (if available) equity-based loss/profit figures."""
    bal, _ = _balance_lookup(deals)
    events = _realised_events(deals, trades)
    opens = trades["open_time"].dropna() if not trades.empty else pd.Series(dtype="datetime64[ns]")
    days = pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end) - pd.Timedelta("1ns"),
                         freq="D")
    eq = equity if equity is not None else pd.DataFrame()
    rows = []
    for d in days:
        d_end = d + pd.Timedelta(days=1)
        has_activity = ((events["time"] >= d) & (events["time"] < d_end)).any() or \
            ((opens >= d) & (opens < d_end)).any()
        if not has_activity:
            continue
        sb = opening_balance(deals, d)
        row = {"day": d.date().isoformat(), "start_balance": sb,
               "trades_opened": int(((opens >= d) & (opens < d_end)).sum())}
        if not np.isnan(sb) and sb > 0:
            lk = _lock_scan(events, opens, sb, risk_cfg.daily_loss_lock_pct, -1, d, d_end)
            hs = _lock_scan(events, opens, sb, risk_cfg.daily_hard_stop_pct, -1, d, d_end)
            pt = _lock_scan(events, opens, sb, risk_cfg.daily_profit_target_pct, 1, d, d_end)
            row.update({"realised_worst_pct": lk["worst_pct"], "realised_best_pct": pt["best_pct"],
                        "realised_end_pct": lk["end_pct"],
                        "loss_lock_hit": lk["hit_time"], "entries_after_loss_lock":
                            lk["entries_after_hit"],
                        "hard_stop_hit": hs["hit_time"],
                        "profit_target_hit": pt["hit_time"],
                        "entries_after_profit_target": pt["entries_after_hit"]})
        else:
            row.update({"realised_end_pct": np.nan})
        if not eq.empty:
            prior = eq[eq["time"] < d]
            in_day = eq[(eq["time"] >= d) & (eq["time"] < d_end)]
            start_eq = float(prior["equity"].iloc[-1]) if not prior.empty else \
                (float(in_day["equity"].iloc[0]) if not in_day.empty else np.nan)
            if not np.isnan(start_eq) and not in_day.empty:
                el = _equity_day_scan(eq, opens, start_eq, risk_cfg.daily_loss_lock_pct, -1, d,
                                      d_end)
                eh = _equity_day_scan(eq, opens, start_eq, risk_cfg.daily_hard_stop_pct, -1, d,
                                      d_end)
                ep = _equity_day_scan(eq, opens, start_eq, risk_cfg.daily_profit_target_pct, 1,
                                      d, d_end)
                gaps = in_day["time"].diff().dt.total_seconds().dropna()
                row.update({"equity_start": start_eq, "equity_worst_pct": el["worst_pct"],
                            "equity_best_pct": ep["best_pct"],
                            "equity_loss_lock_hit": el["hit_time"],
                            "equity_entries_after_loss_lock": el["entries_after_hit"],
                            "equity_hard_stop_hit": eh["hit_time"],
                            "equity_profit_target_hit": ep["hit_time"],
                            "equity_entries_after_profit_target": ep["entries_after_hit"],
                            "equity_sampling_s": float(gaps.median()) if len(gaps) else np.nan})
        rows.append(row)
    return pd.DataFrame(rows)


def _day_verdicts(days: pd.DataFrame, risk_cfg, kind: str) -> dict:
    """kind: 'loss_lock' | 'hard_stop' | 'profit_lock'."""
    if days.empty:
        return _check(f"daily_{kind}", NOT_APPLICABLE, "No trading days in period.")
    max_s = risk_cfg.max_equity_sampling_s_for_lock_verification
    breaches, unverifiable, compliant = [], [], []
    for _, d in days.iterrows():
        has_eq = "equity_sampling_s" in d and pd.notna(d.get("equity_sampling_s"))
        eq_ok = has_eq and d["equity_sampling_s"] <= max_s
        if kind == "loss_lock":
            real_b = d.get("entries_after_loss_lock", 0) or 0
            eq_b = d.get("equity_entries_after_loss_lock", 0) if has_eq else 0
            if real_b > 0 or (eq_b or 0) > 0:
                breaches.append({"day": d["day"], "entries_after_lock": int(max(real_b, eq_b or 0)),
                                 "lock_hit": d.get("equity_loss_lock_hit") or
                                 d.get("loss_lock_hit"),
                                 "basis": "equity" if (eq_b or 0) > 0 else "realised"})
                continue
        elif kind == "hard_stop":
            if (d.get("hard_stop_hit") is not None and pd.notna(d.get("hard_stop_hit"))) or \
                    (has_eq and d.get("equity_hard_stop_hit") is not None
                     and pd.notna(d.get("equity_hard_stop_hit"))):
                breaches.append({"day": d["day"],
                                 "realised_worst_pct": d.get("realised_worst_pct"),
                                 "equity_worst_pct": d.get("equity_worst_pct")})
                continue
        elif kind == "profit_lock":
            if not risk_cfg.profit_target_is_lock:
                continue
            real_b = d.get("entries_after_profit_target", 0) or 0
            eq_b = d.get("equity_entries_after_profit_target", 0) if has_eq else 0
            if real_b > 0 or (eq_b or 0) > 0:
                breaches.append({"day": d["day"], "entries_after_target": int(max(real_b,
                                                                                  eq_b or 0)),
                                 "target_hit": d.get("equity_profit_target_hit") or
                                 d.get("profit_target_hit")})
                continue
        if pd.isna(d.get("start_balance")) and not has_eq:
            unverifiable.append({"day": d["day"], "reason": "no opening balance or equity"})
        elif not eq_ok:
            unverifiable.append({"day": d["day"],
                                 "reason": "no intraday equity" if not has_eq else
                                 f"equity sampling {d['equity_sampling_s']:.0f}s > {max_s}s"})
        else:
            compliant.append(d["day"])
    thresholds = {"loss_lock": risk_cfg.daily_loss_lock_pct,
                  "hard_stop": risk_cfg.daily_hard_stop_pct,
                  "profit_lock": risk_cfg.daily_profit_target_pct}
    name = f"daily_{kind}" if kind != "profit_lock" else "daily_profit_lock"
    ev = {"threshold_pct": thresholds[kind], "breach_days": breaches,
          "unverifiable_days": unverifiable, "verified_days": compliant}
    if kind == "profit_lock" and not risk_cfg.profit_target_is_lock:
        return _check(name, NOT_APPLICABLE, "Profit target configured as non-locking.",
                      evidence=ev)
    if breaches:
        what = {"loss_lock": "new positions opened after the daily loss lock level was reached",
                "hard_stop": "daily loss reached the hard stop level",
                "profit_lock": "new positions opened after the daily profit target was reached"
                }[kind]
        basis = "realised deals" + (" + equity observations"
                                    if "equity_sampling_s" in days.columns else "")
        return _check(name, BREACH, f"{len(breaches)} day(s): {what}.", basis, ev)
    if unverifiable:
        if kind == "profit_lock":
            best = days["realised_best_pct"].max() if "realised_best_pct" in days else np.nan
            extra = f" (best realised intraday gain {best:.2f}%)" if pd.notna(best) else ""
        else:
            worst = days["realised_worst_pct"].max() if "realised_worst_pct" in days else np.nan
            extra = f" (worst realised intraday loss {worst:.2f}%)" if pd.notna(worst) else ""
        return _check(name, UNVERIFIABLE,
                      f"{len(unverifiable)} trading day(s) cannot be verified without intraday "
                      f"equity data. No breach is visible in realised P/L{extra}, but this does "
                      "not prove the lock operated.", "realised deals only", ev)
    return _check(name, COMPLIANT, f"All {len(compliant)} trading day(s) verified against "
                  "intraday equity observations.", "equity observations", ev)


def weekly_loss_check(deals, trades, equity, start, end, risk_cfg) -> dict:
    sb = opening_balance(deals, start)
    if np.isnan(sb) or sb <= 0:
        return _check("weekly_loss_lock", UNVERIFIABLE,
                      "Balance at period start unknown: weekly loss % cannot be computed.")
    events = _realised_events(deals, trades)
    opens = trades["open_time"].dropna()
    scan = _lock_scan(events, opens, sb, risk_cfg.weekly_loss_lock_pct, -1, pd.Timestamp(start),
                      pd.Timestamp(end))
    ev = {"start_balance": sb, "worst_cumulative_loss_pct": scan["worst_pct"],
          "period_end_pct": scan["end_pct"], "lock_hit": scan["hit_time"],
          "entries_after_lock": scan["entries_after_hit"],
          "threshold_pct": risk_cfg.weekly_loss_lock_pct}
    if scan["hit_time"]:
        msg = (f"Cumulative realised loss reached {scan['worst_pct']:.2f}% (limit "
               f"{risk_cfg.weekly_loss_lock_pct}%)")
        if scan["entries_after_hit"]:
            msg += f"; {scan['entries_after_hit']} position(s) opened afterwards"
        return _check("weekly_loss_lock", BREACH, msg + ".", "realised deals", ev)
    has_eq = equity is not None and not equity.empty and \
        ((equity["time"] >= pd.Timestamp(start)) & (equity["time"] < pd.Timestamp(end))).any()
    if not has_eq:
        return _check("weekly_loss_lock", UNVERIFIABLE,
                      f"Worst realised cumulative loss {max(scan['worst_pct'], 0):.2f}% is below "
                      f"the {risk_cfg.weekly_loss_lock_pct}% limit, but floating losses are not "
                      "observable without equity data.", "realised deals only", ev)
    e = equity[(equity["time"] >= pd.Timestamp(start)) & (equity["time"] < pd.Timestamp(end))]
    worst_eq = float((sb - e["equity"].min()) / sb * 100)
    ev["equity_worst_pct"] = worst_eq
    if worst_eq >= risk_cfg.weekly_loss_lock_pct:
        return _check("weekly_loss_lock", BREACH, f"Equity fell {worst_eq:.2f}% below the "
                      "period-start balance.", "equity observations", ev)
    return _check("weekly_loss_lock", COMPLIANT, f"Worst equity loss {worst_eq:.2f}% vs "
                  f"{risk_cfg.weekly_loss_lock_pct}% limit.", "equity observations", ev)


def martingale_check(trades: pd.DataFrame, factor: float, period_trades: pd.DataFrame) -> dict:
    t = trades[trades["open_time"].notna()].sort_values("open_time")
    pt_ids = set(period_trades["position_id"])
    flagged = []
    for _, r in t[t["position_id"].isin(pt_ids)].iterrows():
        prev = t[(t["close_time"] < r["open_time"]) & (t["symbol"] == r["symbol"])
                 & (t["layer"] == r["layer"])]
        if prev.empty:
            continue
        last = prev.sort_values("close_time").iloc[-1]
        if last["net_profit"] < 0 and r["volume"] >= last["volume"] * factor - 1e-9:
            flagged.append({"position_id": r["position_id"], "volume": r["volume"],
                            "previous_loss_position": last["position_id"],
                            "previous_volume": last["volume"]})
    if len(flagged) >= 2:
        return _check("martingale", BREACH,
                      f"{len(flagged)} position(s) increased volume ≥{factor}x immediately after "
                      "a loss on the same layer - martingale-like sizing (approved: DISABLED).",
                      "trade volumes", {"cases": flagged[:20]})
    if flagged:
        return _check("martingale", COMPLIANT,
                      "One isolated volume increase after a loss (not a repeated pattern); "
                      "listed for review.", "trade volumes", {"cases": flagged})
    return _check("martingale", COMPLIANT, "No volume escalation after losses detected.",
                  "trade volumes")


def lot_escalation_check(trades: pd.DataFrame, period_trades: pd.DataFrame) -> dict:
    hist = trades[trades["open_time"].notna()]
    if len(hist) < 5:
        return _check("lot_escalation", UNVERIFIABLE, "Too few trades for a volume baseline.")
    flagged = []
    for layer, g in hist.groupby("layer"):
        med = g["volume"].median()
        cur = period_trades[(period_trades["layer"] == layer) & (period_trades["volume"] > 2 * med)]
        flagged += [{"position_id": p, "volume": v, "layer_median": med}
                    for p, v in zip(cur["position_id"], cur["volume"])]
    if flagged:
        return _check("lot_escalation", BREACH, f"{len(flagged)} position(s) used more than "
                      "2x the historical median volume for their layer.", "trade volumes",
                      {"cases": flagged[:20]})
    return _check("lot_escalation", COMPLIANT, "No unexpected lot escalation versus history.",
                  "trade volumes")


def analyze_risk(*, deals, trades_all, period_trades, equity, start, end, risk_cfg,
                 analysis_cfg, baskets: pd.DataFrame | None, drawdown_full: dict,
                 exposure: dict) -> dict:
    out: dict = {"checks": [], "metrics": {}, "findings": [], "daily": pd.DataFrame()}
    C, M = out["checks"], out["metrics"]
    pt = period_trades

    days = daily_table(deals, trades_all, equity, start, end, risk_cfg)
    out["daily"] = days
    C.append(_day_verdicts(days, risk_cfg, "loss_lock"))
    C.append(_day_verdicts(days, risk_cfg, "hard_stop"))
    C.append(_day_verdicts(days, risk_cfg, "profit_lock"))
    C.append(weekly_loss_check(deals, trades_all, equity, start, end, risk_cfg))

    # Max layers
    if baskets is None or baskets.empty:
        C.append(_check("max_layers", NOT_APPLICABLE, "No baskets in period."))
    else:
        mx = int(baskets["layers"].max())
        inferred = (baskets["method"] != "EXPLICIT").any()
        basis = "baskets " + ("partly INFERRED from time overlap" if inferred else
                              "from EA comment identifiers")
        if mx > risk_cfg.max_layers:
            C.append(_check("max_layers", BREACH, f"Maximum basket size {mx} exceeds approved "
                            f"{risk_cfg.max_layers}.", basis, {"max_layers_observed": mx}))
        else:
            C.append(_check("max_layers", COMPLIANT, f"Maximum basket size {mx} ≤ "
                            f"{risk_cfg.max_layers}.", basis, {"max_layers_observed": mx}))

    # Basket risk + per-layer risk allocation
    if baskets is None or baskets.empty:
        C.append(_check("max_basket_risk", NOT_APPLICABLE, "No baskets in period."))
    else:
        over = baskets[baskets["basket_risk_pct"] > risk_cfg.max_basket_risk_pct *
                       (1 + risk_cfg.risk_tolerance_rel)]
        incomplete = baskets[~baskets["risk_complete"]]
        layer_over = []
        if "risk_pct" in pt:
            for i, alloc in enumerate(risk_cfg.layer_risk_pct, start=1):
                lo = pt[(pt["layer"] == i) & (pt["risk_pct"] > alloc *
                                              (1 + risk_cfg.risk_tolerance_rel))]
                layer_over += [{"position_id": p, "layer": i, "risk_pct": round(r, 4),
                                "allocation_pct": alloc}
                               for p, r in zip(lo["position_id"], lo["risk_pct"])]
        ev = {"baskets_over_limit": over["basket_id"].tolist(),
              "max_basket_risk_pct": float(baskets["basket_risk_pct"].max())
              if baskets["basket_risk_pct"].notna().any() else None,
              "baskets_risk_unknown": int(len(incomplete)), "layers_over_allocation": layer_over}
        if not over.empty or layer_over:
            C.append(_check("max_basket_risk", BREACH,
                            f"{len(over)} basket(s) above {risk_cfg.max_basket_risk_pct}% and "
                            f"{len(layer_over)} layer(s) above their allocation (SL-implied, "
                            f"tolerance +{risk_cfg.risk_tolerance_rel * 100:.0f}%).",
                            "SL distance × volume × empirical point value / balance at open", ev))
        elif len(incomplete):
            C.append(_check("max_basket_risk", UNVERIFIABLE,
                            f"SL-implied risk unknown for {len(incomplete)} basket(s) (missing SL, "
                            "balance or point value).", "SL-implied risk", ev))
        else:
            C.append(_check("max_basket_risk", COMPLIANT, "All baskets within risk limits.",
                            "SL-implied risk", ev))

    # Max equity drawdown (account history up to period end)
    eqdd = drawdown_full.get("equity", {})
    baldd = drawdown_full.get("balance", {})
    lim = risk_cfg.max_equity_drawdown_pct
    if eqdd.get("status") == CALCULATED:
        v = eqdd["max_dd_pct"]
        C.append(_check("max_equity_drawdown", BREACH if v >= lim else COMPLIANT,
                        f"Observed equity drawdown {v:.2f}% vs limit {lim}%.",
                        "equity observations", {"max_dd_pct": v}))
    elif baldd.get("status") == CALCULATED and baldd.get("max_dd_pct") is not None:
        v = baldd["max_dd_pct"]
        if v >= lim:
            C.append(_check("max_equity_drawdown", BREACH,
                            f"Closed-trade balance drawdown alone is {v:.2f}% (≥ {lim}%); equity "
                            "drawdown is at least this deep.", "closed-trade balance",
                            {"balance_dd_pct": v}))
        else:
            C.append(_check("max_equity_drawdown", UNVERIFIABLE,
                            f"Closed-trade drawdown {v:.2f}% is below {lim}%, but equity "
                            "drawdown (incl. floating losses) is not observable.",
                            "closed-trade balance (lower bound)", {"balance_dd_pct": v}))
    else:
        C.append(_check("max_equity_drawdown", UNVERIFIABLE, "No drawdown data available."))

    C.append(martingale_check(trades_all, analysis_cfg.lot_escalation_factor, pt)
             if not pt.empty else _check("martingale", NOT_APPLICABLE, "No trades."))

    # Stop loss presence
    if pt.empty:
        C.append(_check("stop_loss_presence", NOT_APPLICABLE, "No trades."))
    else:
        sl = pt["sl_status"].value_counts().to_dict()
        if sl.get("NOT_SET"):
            ids = pt.loc[pt["sl_status"] == "NOT_SET", "position_id"].tolist()
            C.append(_check("stop_loss_presence", BREACH,
                            f"{sl['NOT_SET']} position(s) had no stop loss at entry.",
                            "SL recorded on entry deal / report", {"positions": ids[:50],
                                                                   "counts": sl}))
        elif sl.get("UNKNOWN"):
            C.append(_check("stop_loss_presence", UNVERIFIABLE,
                            f"SL not recorded for {sl['UNKNOWN']} position(s).", "",
                            {"counts": sl}))
        else:
            C.append(_check("stop_loss_presence", COMPLIANT, "Every position had a stop loss.",
                            "SL recorded on entry deal / report", {"counts": sl}))

    C.append(lot_escalation_check(trades_all, pt) if not pt.empty
             else _check("lot_escalation", NOT_APPLICABLE, "No trades."))

    # ------------------------------------------------------------ metrics
    closed = pt[pt["status"] == "CLOSED"] if not pt.empty else pt
    if "risk_pct" in pt and pt["risk_pct"].notna().any():
        M["average_risk_per_trade_pct"] = Metric(float(pt["risk_pct"].mean()), CALCULATED, "%",
                                                 basis="SL-implied risk at entry",
                                                 note=f"{int(pt['risk_pct'].isna().sum())} "
                                                      "trade(s) without computable risk")
    else:
        M["average_risk_per_trade_pct"] = unavailable("SL, balance or point value unavailable.",
                                                      "%")
    if baskets is not None and not baskets.empty and baskets["basket_risk_pct"].notna().any():
        M["max_risk_per_basket_pct"] = Metric(float(baskets["basket_risk_pct"].max()), CALCULATED,
                                              "%", basis="Σ SL-implied layer risk")
    else:
        M["max_risk_per_basket_pct"] = unavailable("Basket risk not computable.", "%")
    rr = pt[(pt["sl_status"] == "SET") & pt["tp"].notna() & (pt["tp"] != 0)
            & pt["entry_price"].notna()] if not pt.empty else pt
    if len(rr):
        planned = ((rr["tp"] - rr["entry_price"]).abs() / (rr["entry_price"] - rr["sl"]).abs())
        M["planned_risk_reward"] = Metric(float(planned.replace(np.inf, np.nan).mean()),
                                          CALCULATED, basis="|TP-entry| / |entry-SL| (planned)")
    else:
        M["planned_risk_reward"] = unavailable("Requires SL and TP at entry.")
    if len(closed):
        net = closed["net_profit"]
        losses = net[net < 0]
        M["average_loss"] = Metric(float(losses.mean()), CALCULATED, "ccy") if len(losses) else \
            Metric(None, "UNDEFINED", note="No losses")
        M["largest_loss"] = Metric(float(losses.min()), CALCULATED, "ccy") if len(losses) else \
            Metric(None, "UNDEFINED", note="No losses")
        _, bl = streaks(outcome(net, analysis_cfg.breakeven_epsilon))
        M["max_consecutive_losses"] = Metric(bl, CALCULATED, "count")
        by_dir = closed.groupby("direction").agg(trades=("position_id", "count"),
                                                 volume=("volume", "sum"),
                                                 net=("net_profit", "sum"))
        out["exposure_by_direction"] = by_dir.reset_index()
        for d, r in by_dir.iterrows():
            M[f"exposure_{d}_volume_share_pct"] = Metric(
                float(r["volume"] / by_dir["volume"].sum() * 100), CALCULATED, "%")
        gl = float(losses.sum())
        if "strategy" in closed and gl < 0:
            conc = closed[closed["net_profit"] < 0].groupby("strategy")["net_profit"].sum() / gl
            out["loss_concentration_by_strategy"] = (conc * 100).round(2).to_dict()
            M["risk_concentration_top_strategy_pct"] = Metric(float(conc.max() * 100), CALCULATED,
                                                              "%", basis=f"share of gross loss: "
                                                              f"{conc.idxmax()}")
        if baskets is not None and not baskets.empty and gl < 0:
            worst_b = baskets["net_profit"].min()
            M["largest_basket_loss_share_pct"] = Metric(
                float(min(worst_b, 0) / gl * 100), CALCULATED, "%",
                basis="largest single basket loss / gross loss")
    if exposure.get("available"):
        M["max_simultaneous_positions"] = Metric(exposure["max_positions"], CALCULATED, "count")
        M["max_simultaneous_volume"] = Metric(exposure["max_volume"], CALCULATED, "lots")
    M["max_layer_count"] = Metric(int(baskets["layers"].max()), CALCULATED, "count") \
        if baskets is not None and not baskets.empty else unavailable("No baskets.")
    if not days.empty and "realised_end_pct" in days and days["realised_end_pct"].notna().any():
        M["worst_daily_realised_loss_pct"] = Metric(float(-days["realised_end_pct"].min()),
                                                    CALCULATED, "%",
                                                    basis="realised P/L / day start balance")
    else:
        M["worst_daily_realised_loss_pct"] = unavailable("Day start balance unavailable.", "%")
    wk = [c for c in C if c["check"] == "weekly_loss_lock"][0]["evidence"]
    if "period_end_pct" in wk:
        M["period_realised_change_pct"] = Metric(float(wk["period_end_pct"]), CALCULATED, "%",
                                                 basis="realised P/L / period start balance")

    for c in C:
        if c["verdict"] == BREACH:
            out["findings"].append(Finding("risk", "HIGH", f"[{c['check']}] {c['summary']}",
                                           [], c["evidence"], code=f"BREACH_{c['check'].upper()}"))
    return out
