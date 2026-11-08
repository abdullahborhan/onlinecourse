"""
Position lifecycle reconstruction.

MT5 *deals* are not trades. A position is opened by one or more IN deals and
closed by one or more OUT / OUT_BY deals (partial closes). This module groups
deals by position id and rebuilds one row per position.

Sources (in priority order)
---------------------------
1. ``DEALS``            - deals with a position id (CSV exports, EA logs).
2. ``REPORT_POSITIONS`` - the MT5 HTML report "Positions" table. Used for
                          positions not covered by (1). MT5 aggregates partial
                          closes in this table, so partial-close detection is
                          UNAVAILABLE for these rows.

Entry deals are never counted as closed trades. A position counts as a CLOSED
trade only when its closed volume equals its opened volume.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from modules.normalizer import TRADE_TYPES

VOL_EPS = 1e-9
COSTS = ("commission", "swap", "fee")

TRADE_COLUMNS = [
    "position_id", "account_id", "symbol", "direction", "open_time", "close_time", "volume",
    "volume_closed", "entry_price", "exit_price", "sl", "tp", "sl_status", "profit",
    "commission", "swap", "fee", "net_profit", "costs_complete", "n_exits", "partial_close",
    "status", "magic", "comment", "exit_reason", "source", "entry_known", "duration_s",
    "balance_at_open", "balance_basis", "open_order_id",
]


def _exit_reason(comments) -> str:
    text = " ".join(str(c) for c in comments if c)
    if re.search(r"\[sl|stop ?loss|\bsl\b", text, re.I):
        return "SL"
    if re.search(r"\[tp|take ?profit|\btp\b", text, re.I):
        return "TP"
    if re.search(r"\[so|stop ?out", text, re.I):
        return "STOP_OUT"
    return "UNKNOWN"


def _nansum_or_nan(s: pd.Series) -> float:
    return float(s.sum()) if s.notna().any() else np.nan


def _sl_status(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "UNKNOWN"
    return "NOT_SET" if float(v) == 0.0 else "SET"


def infer_entries(deals: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Infer IN/OUT for deals that lack an entry flag but have a position id.

    The first deal of a position defines its direction; same-direction deals
    are IN, opposite-direction deals are OUT. Returns (deals, n_inferred).
    """
    d = deals.copy()
    need = d["entry"].isna() & d["position_id"].notna() & d["deal_type"].isin(TRADE_TYPES)
    if not need.any():
        return d, 0
    first_dir = (d[d["position_id"].notna() & d["deal_type"].isin(TRADE_TYPES)]
                 .sort_values(["time", "deal_id"]).groupby("position_id")["deal_type"].first())
    fd = d.loc[need, "position_id"].map(first_dir)
    d.loc[need, "entry"] = np.where(d.loc[need, "deal_type"] == fd, "IN", "OUT")
    return d, int(need.sum())


def balance_series(deals: pd.DataFrame) -> tuple[pd.Series | None, str]:
    """Account balance after each deal, indexed by deal time.

    Uses the report's Balance column when present (VERIFIED). Otherwise it is
    DERIVED by cumulating all cash flows, which is only valid when the history
    starts with the initial deposit; else None.
    """
    if deals.empty:
        return None, "UNAVAILABLE"
    d = deals.sort_values(["time", "deal_id"], kind="stable")
    if "balance" in d.columns and d["balance"].notna().mean() > 0.95:
        return pd.Series(d["balance"].values, index=d["time"].values).dropna(), "VERIFIED"
    if d.iloc[0]["deal_type"] == "balance":
        delta = d[["profit", *COSTS]].fillna(0).sum(axis=1)
        return pd.Series(delta.cumsum().values, index=d["time"].values), "DERIVED"
    return None, "UNAVAILABLE"


def _balance_before(bal: pd.Series | None, t) -> float:
    if bal is None or pd.isna(t):
        return np.nan
    prior = bal[bal.index < np.datetime64(t)]
    return float(prior.iloc[-1]) if len(prior) else np.nan


def reconstruct_from_deals(deals: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    rep = {"positions": 0, "closed": 0, "open": 0, "partially_closed": 0, "orphan_exits": 0,
           "partial_close_positions": 0, "entries_inferred": 0, "reversal_deals": 0,
           "deals_without_position_id": 0}
    trade = deals[deals["deal_type"].isin(TRADE_TYPES)].copy()
    if trade.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS), rep
    rep["deals_without_position_id"] = int(trade["position_id"].isna().sum())
    trade = trade[trade["position_id"].notna()]
    if trade.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS), rep
    trade, rep["entries_inferred"] = infer_entries(trade)
    rep["reversal_deals"] = int((trade["entry"] == "INOUT").sum())
    bal, basis = balance_series(deals)

    rows = []
    for pid, g in trade.sort_values(["time", "deal_id"], kind="stable").groupby("position_id",
                                                                                sort=False):
        ins = g[g["entry"].isin(["IN", "INOUT"])]
        outs = g[g["entry"].isin(["OUT", "OUT_BY", "INOUT"])]
        entry_known = not ins.empty
        vol_in = float(ins["volume"].sum()) if entry_known else np.nan
        vol_out = float(outs["volume"].sum()) if not outs.empty else 0.0
        if entry_known:
            direction = ins.iloc[0]["deal_type"]
            entry_price = float((ins["price"] * ins["volume"]).sum() / ins["volume"].sum())
            open_time = ins["time"].min()
        else:
            # Opened before the imported history: direction is opposite of the exit deal
            direction = "buy" if outs.iloc[0]["deal_type"] == "sell" else "sell"
            entry_price = np.nan
            open_time = pd.NaT
            rep["orphan_exits"] += 1
        exit_price = float((outs["price"] * outs["volume"]).sum() / vol_out) if vol_out > 0 \
            else np.nan
        close_time = outs["time"].max() if not outs.empty else pd.NaT
        if not entry_known:
            status = "CLOSED"  # realised exit; entry outside data window
        elif vol_out <= VOL_EPS:
            status = "OPEN"
        elif vol_out + VOL_EPS < vol_in:
            status = "PARTIALLY_CLOSED"
        else:
            status = "CLOSED"
        costs = {c: _nansum_or_nan(g[c]) if c in g else np.nan for c in COSTS}
        profit = float(g["profit"].fillna(0).sum())
        net = profit + sum(0.0 if np.isnan(v) else v for v in costs.values())
        first_in = ins.iloc[0] if entry_known else outs.iloc[0]
        sl = first_in.get("sl") if entry_known else np.nan
        tp = first_in.get("tp") if entry_known else np.nan
        n_exits = int(len(outs))
        rows.append({
            "position_id": str(pid), "account_id": g.iloc[0]["account_id"],
            "symbol": first_in["symbol"], "direction": direction, "open_time": open_time,
            "close_time": close_time if status != "OPEN" else pd.NaT,
            "volume": vol_in if entry_known else vol_out, "volume_closed": vol_out,
            "entry_price": entry_price, "exit_price": exit_price, "sl": sl, "tp": tp,
            "sl_status": _sl_status(sl) if entry_known else "UNKNOWN",
            "profit": profit, **costs, "net_profit": net,
            "costs_complete": all(not np.isnan(v) for v in costs.values()),
            "n_exits": n_exits, "partial_close": n_exits > 1,
            "status": status, "magic": first_in.get("magic"),
            "comment": first_in.get("comment"),
            "exit_reason": _exit_reason(outs["comment"].tolist()) if not outs.empty else None,
            "source": "DEALS", "entry_known": entry_known,
            "balance_at_open": _balance_before(bal, open_time), "balance_basis": basis,
            "open_order_id": first_in.get("order_id") if entry_known else None,
        })
    df = pd.DataFrame(rows)
    rep["positions"] = len(df)
    rep["closed"] = int((df["status"] == "CLOSED").sum())
    rep["open"] = int((df["status"] == "OPEN").sum())
    rep["partially_closed"] = int((df["status"] == "PARTIALLY_CLOSED").sum())
    rep["partial_close_positions"] = int(df["partial_close"].sum())
    return df, rep


def reconstruct_from_report_positions(pos: pd.DataFrame, deals: pd.DataFrame | None
                                      ) -> pd.DataFrame:
    if pos is None or pos.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    bal, basis = balance_series(deals) if deals is not None and not deals.empty \
        else (None, "UNAVAILABLE")
    # Enrich comment / magic from the opening deal: MT5 position id == opening order ticket
    in_comment, out_comments = {}, {}
    if deals is not None and not deals.empty:
        ind = deals[(deals["entry"] == "IN") & deals["order_id"].notna()]
        in_comment = dict(zip(ind["order_id"].astype(str), ind["comment"]))
    rows = []
    for _, p in pos.iterrows():
        closed = pd.notna(p["close_time"])
        costs = {c: p.get(c, np.nan) for c in COSTS}
        costs = {c: (np.nan if v is None else float(v)) for c, v in costs.items()}
        net = float(p["profit"]) + sum(0.0 if np.isnan(v) else v for v in costs.values())
        comment = p.get("comment") or in_comment.get(str(p["position_id"]))
        rows.append({
            "position_id": str(p["position_id"]), "account_id": p["account_id"],
            "symbol": p["symbol"], "direction": p["direction"], "open_time": p["open_time"],
            "close_time": p["close_time"], "volume": p["volume"],
            "volume_closed": p["volume"] if closed else 0.0,
            "entry_price": p["open_price"], "exit_price": p["close_price"],
            "sl": p["sl"], "tp": p["tp"],
            # In MT5 HTML an empty S/L cell means no stop loss was set
            "sl_status": "SET" if pd.notna(p["sl"]) and float(p["sl"]) != 0 else "NOT_SET",
            "profit": float(p["profit"]), **costs, "net_profit": net,
            # MT5 HTML positions have no Fee column; fee is therefore unknown
            "costs_complete": all(not np.isnan(costs[c]) for c in ("commission", "swap")),
            "n_exits": np.nan, "partial_close": None,
            "status": "CLOSED" if closed else "OPEN", "magic": p.get("magic"),
            "comment": comment, "exit_reason": None, "source": "REPORT_POSITIONS",
            "entry_known": True, "balance_at_open": _balance_before(bal, p["open_time"]),
            "balance_basis": basis, "open_order_id": str(p["position_id"]),
        })
    return pd.DataFrame(rows)


def reconstruct_trades(deals: pd.DataFrame, report_positions: pd.DataFrame | None = None
                       ) -> tuple[pd.DataFrame, dict]:
    """Build the canonical position table from all available sources."""
    deals = deals if deals is not None else pd.DataFrame()
    if not deals.empty:
        from_deals, rep = reconstruct_from_deals(deals)
    else:
        from_deals, rep = pd.DataFrame(columns=TRADE_COLUMNS), {"positions": 0}
    covered = set(from_deals["position_id"]) if not from_deals.empty else set()
    rp = report_positions if report_positions is not None else pd.DataFrame()
    if not rp.empty:
        rp = rp[~rp["position_id"].astype(str).isin(covered)]
    from_rep = reconstruct_from_report_positions(rp, deals)
    # Fill missing SL/TP on deal-based positions from the report positions table
    if report_positions is not None and not report_positions.empty and not from_deals.empty:
        rpi = report_positions.set_index(report_positions["position_id"].astype(str))
        for col in ("sl", "tp"):
            miss = from_deals[col].isna() & from_deals["position_id"].isin(rpi.index)
            if miss.any():
                from_deals.loc[miss, col] = from_deals.loc[miss, "position_id"].map(rpi[col])
        from_deals["sl_status"] = [
            s if s != "UNKNOWN" or pd.isna(v) else ("NOT_SET" if float(v) == 0 else "SET")
            for s, v in zip(from_deals["sl_status"], from_deals["sl"])]
    frames = [f for f in (from_deals, from_rep) if not f.empty]
    trades = pd.concat(frames, ignore_index=True) if frames else \
        pd.DataFrame(columns=TRADE_COLUMNS)
    if not trades.empty:
        trades["duration_s"] = (trades["close_time"] - trades["open_time"]).dt.total_seconds()
        trades = trades.sort_values(["close_time", "open_time", "position_id"],
                                    na_position="last", kind="stable").reset_index(drop=True)
    rep["report_position_rows_used"] = int(len(from_rep))
    # Fees present on deals cannot be attributed to MT5 HTML positions (no position id)
    if len(from_rep) and not deals.empty and "fee" in deals:
        td = deals[deals["deal_type"].isin(TRADE_TYPES)]
        fee_total = float(td["fee"].fillna(0).sum())
        if abs(fee_total) > 1e-9:
            trades.loc[trades["source"] == "REPORT_POSITIONS", "costs_complete"] = False
            rep["unattributed_fees"] = fee_total
            rep["note"] = (f"Deals carry fees totalling {fee_total:.2f} that cannot be attributed "
                           "to positions (MT5 HTML deals lack position ids); position net "
                           "profit excludes them.")
    rep["sources"] = sorted(trades["source"].unique().tolist()) if not trades.empty else []
    rep["partial_close_detection"] = (
        "AVAILABLE" if rep.get("positions") and not len(from_rep) else
        "PARTIAL" if rep.get("positions") else "UNAVAILABLE")
    return trades, rep


def point_values(trades: pd.DataFrame) -> dict:
    """Account-currency value of a 1.0 price move for 1.0 lot, per symbol.

    Derived empirically from closed trades' own profit / (price move x volume),
    so no contract specification is assumed. Reported with its dispersion; a
    symbol is only usable when the estimates agree (CV < 2%).
    """
    out = {}
    t = trades[(trades["status"] == "CLOSED") & trades["entry_known"].astype(bool)
               & trades["exit_price"].notna() & trades["entry_price"].notna()].copy()
    if t.empty:
        return out
    sign = np.where(t["direction"] == "buy", 1.0, -1.0)
    move = (t["exit_price"] - t["entry_price"]) * sign * t["volume_closed"]
    t = t.assign(_move=move)
    t = t[t["_move"].abs() > 1e-9]
    for sym, g in t.groupby("symbol"):
        vals = g["profit"] / g["_move"]
        med = float(vals.median())
        cv = float(vals.std(ddof=0) / abs(med)) if len(vals) > 1 and med else 0.0
        out[sym] = {"value_per_point_per_lot": med, "samples": int(len(vals)), "cv": cv,
                    "usable": bool(len(vals) >= 1 and cv < 0.02)}
    return out


def add_risk_at_entry(trades: pd.DataFrame, pv: dict) -> pd.DataFrame:
    """Risk (money and % of balance at open) implied by the stop loss distance."""
    t = trades.copy()
    risk_money, risk_pct = [], []
    for _, r in t.iterrows():
        info = pv.get(r["symbol"])
        if r["sl_status"] != "SET" or not info or not info["usable"] or \
                pd.isna(r["entry_price"]):
            risk_money.append(np.nan)
            risk_pct.append(np.nan)
            continue
        m = abs(float(r["entry_price"]) - float(r["sl"])) * float(r["volume"]) * \
            info["value_per_point_per_lot"]
        risk_money.append(m)
        b = r["balance_at_open"]
        risk_pct.append(m / b * 100 if pd.notna(b) and b > 0 else np.nan)
    t["risk_money"] = risk_money
    t["risk_pct"] = risk_pct
    return t
