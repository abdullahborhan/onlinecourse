"""
Quantitative performance engine (deterministic; no AI involvement).

Definitions (see docs/METRICS.md)
---------------------------------
* Trade net result = profit + commission + swap + fee (costs that are not
  provided by the source are excluded and the metric is annotated).
* WIN: net > epsilon; LOSS: net < -epsilon; BREAKEVEN: |net| <= epsilon.
* Gross profit = sum of WIN nets; gross loss = sum of LOSS nets (negative).
* Profit factor = gross profit / |gross loss|; UNDEFINED (∞) when gross loss = 0.
* Win rate = wins / all closed trades (breakevens included in denominator).
* Streaks are over closed trades ordered by close time; a breakeven trade
  ends both a winning and a losing streak.
* Only CLOSED positions within the period (by close time) are trades.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from modules.common import (CALCULATED, UNAVAILABLE, VERIFIED, Metric, unavailable, undefined)


def closed_trades(trades: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame(columns=getattr(trades, "columns", []))
    t = trades[trades["status"] == "CLOSED"]
    if start is not None:
        t = t[t["close_time"] >= pd.Timestamp(start)]
    if end is not None:
        t = t[t["close_time"] < pd.Timestamp(end)]
    return t.sort_values(["close_time", "position_id"], kind="stable").reset_index(drop=True)


def outcome(net: pd.Series, eps: float) -> pd.Series:
    return np.where(net > eps, "WIN", np.where(net < -eps, "LOSS", "BREAKEVEN"))


def streaks(outcomes) -> tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for o in outcomes:
        if o == "WIN":
            cur_w += 1
            cur_l = 0
        elif o == "LOSS":
            cur_l += 1
            cur_w = 0
        else:
            cur_w = cur_l = 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    return best_w, best_l


def profit_factor(gp: float, gl: float) -> Metric:
    if gl == 0:
        if gp > 0:
            return undefined("No losing trades: profit factor is infinite (undefined).",
                             float("inf"))
        return undefined("No winning or losing trades.")
    return Metric(gp / abs(gl), CALCULATED, basis="gross profit / |gross loss|")


def compute_performance(trades: pd.DataFrame, *, eps: float = 0.0, start_balance=None,
                        period_days: float | None = None, max_dd_money=None,
                        balance_basis: str = "UNAVAILABLE") -> dict:
    """Profitability and trading statistics for a set of CLOSED trades."""
    n = int(len(trades))
    res: dict = {"profitability": {}, "statistics": {}, "notes": []}
    P, S = res["profitability"], res["statistics"]
    S["total_closed_trades"] = Metric(n, CALCULATED, "count",
                                      basis="closed positions (not deals)")
    if n == 0:
        res["notes"].append("No closed trades in this period.")
        for k in ("net_profit", "gross_profit", "gross_loss", "profit_factor", "total_return_pct",
                  "average_profit_per_trade", "expectancy", "average_winning_trade",
                  "average_losing_trade", "largest_winning_trade", "largest_losing_trade",
                  "recovery_factor", "payoff_ratio"):
            P[k] = unavailable("No closed trades in period.")
        for k in ("winning_trades", "losing_trades", "breakeven_trades"):
            S[k] = Metric(0, CALCULATED, "count")
        for k in ("win_rate", "loss_rate", "win_rate_excl_breakeven", "average_trade_duration",
                  "longest_winning_streak", "longest_losing_streak", "average_trades_per_day"):
            S[k] = unavailable("No closed trades in period.")
        return res

    net = trades["net_profit"].astype(float)
    out = outcome(net, eps)
    wins, losses = net[out == "WIN"], net[out == "LOSS"]
    nw, nl, nb = len(wins), len(losses), int((out == "BREAKEVEN").sum())
    gp, gl = float(wins.sum()), float(losses.sum())
    incomplete = int((~trades["costs_complete"].astype(bool)).sum())
    cost_note = (f"{incomplete} trade(s) lack commission/swap/fee data in source; their net "
                 "excludes the missing costs.") if incomplete else ""
    if cost_note:
        res["notes"].append(cost_note)

    P["net_profit"] = Metric(float(net.sum()), CALCULATED, "ccy",
                             basis="Σ(profit+commission+swap+fee)", note=cost_note)
    P["gross_profit"] = Metric(gp, CALCULATED, "ccy", basis="Σ net of winning trades")
    P["gross_loss"] = Metric(gl, CALCULATED, "ccy", basis="Σ net of losing trades")
    P["profit_factor"] = profit_factor(gp, gl)
    if start_balance is not None and not (isinstance(start_balance, float)
                                          and math.isnan(start_balance)) and start_balance > 0:
        P["total_return_pct"] = Metric(float(net.sum()) / start_balance * 100, CALCULATED, "%",
                                       basis=f"net profit / balance at period start "
                                             f"({start_balance:,.2f}; {balance_basis})")
    else:
        P["total_return_pct"] = unavailable("Balance at period start is not available.", "%")
    P["average_profit_per_trade"] = Metric(float(net.mean()), CALCULATED, "ccy")
    P["expectancy"] = Metric(float(nw / n * (wins.mean() if nw else 0)
                                   + nl / n * (losses.mean() if nl else 0)), CALCULATED, "ccy",
                             basis="P(win)·avg win + P(loss)·avg loss (breakeven contributes 0)")
    P["average_winning_trade"] = Metric(float(wins.mean()), CALCULATED, "ccy") if nw else \
        undefined("No winning trades.")
    P["average_losing_trade"] = Metric(float(losses.mean()), CALCULATED, "ccy") if nl else \
        undefined("No losing trades.")
    P["largest_winning_trade"] = Metric(float(wins.max()), VERIFIED, "ccy") if nw else \
        undefined("No winning trades.")
    P["largest_losing_trade"] = Metric(float(losses.min()), VERIFIED, "ccy") if nl else \
        undefined("No losing trades.")
    P["payoff_ratio"] = Metric(float(wins.mean() / abs(losses.mean())), CALCULATED,
                               basis="avg win / |avg loss| (realised reward:risk)") \
        if nw and nl else undefined("Requires both winning and losing trades.")
    if max_dd_money is None:
        P["recovery_factor"] = unavailable("Drawdown unavailable.")
    elif max_dd_money <= 0:
        P["recovery_factor"] = undefined("No drawdown in period.")
    else:
        P["recovery_factor"] = Metric(float(net.sum()) / max_dd_money, CALCULATED,
                                      basis="net profit / max balance drawdown (closed-trade)")

    S["winning_trades"] = Metric(nw, CALCULATED, "count")
    S["losing_trades"] = Metric(nl, CALCULATED, "count")
    S["breakeven_trades"] = Metric(nb, CALCULATED, "count",
                                   note=f"|net| <= {eps} counted as breakeven")
    S["win_rate"] = Metric(nw / n * 100, CALCULATED, "%",
                           basis="wins / all closed trades (incl. breakeven)")
    S["loss_rate"] = Metric(nl / n * 100, CALCULATED, "%",
                            basis="losses / all closed trades (incl. breakeven)")
    S["win_rate_excl_breakeven"] = Metric(nw / (nw + nl) * 100, CALCULATED, "%") if nw + nl \
        else undefined("Only breakeven trades.")
    dur = trades.loc[trades["entry_known"].astype(bool), "duration_s"].dropna()
    S["average_trade_duration"] = Metric(float(dur.mean()), CALCULATED, "s") if len(dur) else \
        unavailable("Entry times unavailable.", "s")
    bw, bl = streaks(out)
    S["longest_winning_streak"] = Metric(bw, CALCULATED, "count")
    S["longest_losing_streak"] = Metric(bl, CALCULATED, "count")
    if period_days and period_days > 0:
        S["average_trades_per_day"] = Metric(n / period_days, CALCULATED,
                                             basis=f"closed trades / {period_days:g} calendar days")
    else:
        S["average_trades_per_day"] = unavailable("Period length unknown.")
    active = trades["close_time"].dt.normalize().nunique()
    S["average_trades_per_active_day"] = Metric(n / active, CALCULATED,
                                                basis=f"{active} day(s) with closed trades")
    costs = {}
    for c in ("commission", "swap", "fee"):
        v = trades[c]
        costs[c] = Metric(float(v.sum()), CALCULATED, "ccy",
                          note=f"{int(v.isna().sum())} trade(s) missing {c}"
                          if v.isna().any() else "") if v.notna().any() else \
            unavailable(f"{c} not provided by the source.", "ccy")
    res["costs"] = costs
    res["status_summary"] = UNAVAILABLE if n == 0 else CALCULATED
    return res


def period_breakdown(trades: pd.DataFrame, freq: str = "D") -> pd.DataFrame:
    """Net P/L, trade count, wins and losses per calendar period (by close time)."""
    if trades.empty:
        return pd.DataFrame(columns=["period", "net_profit", "trades", "wins", "losses"])
    t = trades.copy()
    t["period"] = t["close_time"].dt.to_period(freq).dt.start_time
    g = t.groupby("period")
    return pd.DataFrame({
        "net_profit": g["net_profit"].sum(),
        "trades": g.size(),
        "wins": g["net_profit"].apply(lambda s: int((s > 0).sum())),
        "losses": g["net_profit"].apply(lambda s: int((s < 0).sum())),
    }).reset_index()
