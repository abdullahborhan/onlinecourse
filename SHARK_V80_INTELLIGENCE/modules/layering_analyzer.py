"""
Layering intelligence engine.

Basket identification (never "3 positions = 3 trades" by assumption)
--------------------------------------------------------------------
1. EXPLICIT  - basket id / layer number written by the EA in the comment
               (patterns configurable in AnalysisConfig).
2. INFERRED_OVERLAP - positions on the same account, symbol, direction and
               magic that overlap in time (a new position opens while another
               is still open) form one basket; layers are numbered by open time.
               This is labelled as an inference in every output.

Because each layer's entry price is known, the floating P/L of the existing
layers at the moment a new layer is added is derived exactly from that price
(same symbol). This distinguishes adding WITH the move (continuation) from
adding AGAINST the move (averaging into a losing basket).
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from modules.common import Finding
from modules.performance_engine import profit_factor

EXPLICIT = "EXPLICIT"
INFERRED = "INFERRED_OVERLAP"


def _extract(pattern: str, text) -> str | None:
    if not text or not isinstance(text, str):
        return None
    m = re.search(pattern, text, re.I)
    return m.group(1) if m else None


def assign_baskets(trades: pd.DataFrame, basket_pattern: str, layer_pattern: str
                   ) -> pd.DataFrame:
    t = trades.copy()
    if t.empty:
        for c in ("basket_id", "layer", "basket_method"):
            t[c] = pd.Series(dtype="object")
        return t
    t["basket_id"] = t["comment"].map(lambda c: _extract(basket_pattern, c))
    t["layer_explicit"] = t["comment"].map(lambda c: _extract(layer_pattern, c))
    t["basket_method"] = np.where(t["basket_id"].notna(), EXPLICIT, None)
    t.loc[t["basket_id"].notna(), "basket_id"] = "B:" + t.loc[t["basket_id"].notna(),
                                                               "basket_id"].astype(str)

    # Inferred overlap grouping for the rest
    rest = t[t["basket_id"].isna() & t["open_time"].notna()].copy()
    rest["_magic"] = rest["magic"].fillna("?")
    for key, g in rest.sort_values("open_time").groupby(["symbol", "direction", "_magic"],
                                                        dropna=False):
        current, current_end, n = None, None, 0
        for idx, r in g.iterrows():
            end = r["close_time"] if pd.notna(r["close_time"]) else pd.Timestamp.max
            if current is None or r["open_time"] >= current_end:
                n += 1
                current = f"I:{key[0]}:{key[1]}:{key[2]}:{r['position_id']}"
                current_end = end
            else:
                current_end = max(current_end, end)
            t.at[idx, "basket_id"] = current
            t.at[idx, "basket_method"] = INFERRED
    # Positions with unknown entry time form singleton baskets
    miss = t["basket_id"].isna()
    t.loc[miss, "basket_id"] = "U:" + t.loc[miss, "position_id"].astype(str)
    t.loc[miss, "basket_method"] = "UNKNOWN_ENTRY"

    t = t.sort_values(["open_time", "position_id"], na_position="first", kind="stable")
    t["layer_by_time"] = t.groupby("basket_id").cumcount() + 1
    t["layer"] = pd.to_numeric(t["layer_explicit"], errors="coerce").fillna(t["layer_by_time"]
                                                                            ).astype(int)
    t["layer_source"] = np.where(t["layer_explicit"].notna(), "COMMENT", "OPEN_ORDER")
    return t.drop(columns=["layer_explicit"]).sort_index()


def simultaneous_exposure(trades: pd.DataFrame) -> dict:
    """Max concurrent positions and volume (overall and per direction)."""
    t = trades[trades["open_time"].notna()]
    if t.empty:
        return {"available": False}
    ev = []
    for _, r in t.iterrows():
        end = r["close_time"] if pd.notna(r["close_time"]) else pd.Timestamp.max
        ev.append((r["open_time"], 1, r["volume"], r["direction"], r.get("risk_money", np.nan)))
        ev.append((end, -1, -r["volume"], r["direction"], -r.get("risk_money", np.nan)
                   if pd.notna(r.get("risk_money", np.nan)) else np.nan))
    ev.sort(key=lambda e: (e[0], e[1]))  # closes before opens at the same timestamp
    cnt = vol = 0.0
    by_dir = {"buy": 0, "sell": 0}
    best = {"max_positions": 0, "max_volume": 0.0, "max_buy_positions": 0,
            "max_sell_positions": 0, "time_of_max": None}
    for time, d, v, direction, _ in ev:
        cnt += d
        vol += v
        by_dir[direction] = by_dir.get(direction, 0) + d
        if cnt > best["max_positions"]:
            best["max_positions"], best["time_of_max"] = int(cnt), str(time)
        best["max_volume"] = max(best["max_volume"], round(vol, 8))
        best["max_buy_positions"] = max(best["max_buy_positions"], by_dir.get("buy", 0))
        best["max_sell_positions"] = max(best["max_sell_positions"], by_dir.get("sell", 0))
    best["available"] = True
    return best


def basket_table(trades: pd.DataFrame, point_vals: dict) -> pd.DataFrame:
    rows = []
    for bid, g in trades.groupby("basket_id"):
        g = g.sort_values(["open_time", "position_id"], na_position="first")
        first = g.iloc[0]
        sign = 1.0 if first["direction"] == "buy" else -1.0
        vpp = (point_vals.get(first["symbol"]) or {}).get("value_per_point_per_lot")
        adds = []
        for i in range(1, len(g)):
            prev, cur = g.iloc[:i], g.iloc[i]
            if pd.isna(cur["entry_price"]) or prev["entry_price"].isna().any():
                adds.append(None)
                continue
            move = (cur["entry_price"] - prev["entry_price"]) * sign  # favourable if > 0
            floating_pts = float((move * prev["volume"]).sum())
            adds.append({"layer": int(cur["layer"]),
                         "against": bool(floating_pts < 0),
                         "floating_pl_at_add": floating_pts * vpp if vpp else None,
                         "seconds_after_prev": float((cur["open_time"] - g.iloc[i - 1][
                             "open_time"]).total_seconds()) if pd.notna(cur["open_time"])
                         else None,
                         "existing_risk_pct": float(prev["risk_pct"].sum())
                         if "risk_pct" in prev and prev["risk_pct"].notna().all() else None,
                         "volume_increase": bool(cur["volume"] > g.iloc[i - 1]["volume"] + 1e-9)})
        known_adds = [a for a in adds if a]
        rp = g["risk_pct"] if "risk_pct" in g else pd.Series([np.nan] * len(g))
        rows.append({
            "basket_id": bid, "method": first["basket_method"], "symbol": first["symbol"],
            "direction": first["direction"], "strategy": first.get("strategy"),
            "layers": int(len(g)), "max_layer_number": int(g["layer"].max()),
            "open_time": g["open_time"].min(), "close_time": g["close_time"].max(),
            "total_volume": float(g["volume"].sum()), "net_profit": float(g["net_profit"].sum()),
            "all_closed": bool((g["status"] == "CLOSED").all()),
            "basket_risk_pct": float(rp.sum()) if rp.notna().all() and len(rp) else np.nan,
            "risk_complete": bool(rp.notna().all()),
            "layering_type": ("SINGLE" if len(g) == 1 else
                              "AGAINST" if any(a["against"] for a in known_adds) else
                              "CONTINUATION" if known_adds else "UNKNOWN"),
            "adds": known_adds,
            "avg_seconds_between_layers": float(np.mean([a["seconds_after_prev"]
                                                         for a in known_adds
                                                         if a["seconds_after_prev"] is not None]))
            if known_adds else np.nan,
            "volume_escalation": any(a["volume_increase"] for a in known_adds),
            "position_ids": ",".join(g["position_id"].astype(str)),
        })
    return pd.DataFrame(rows)


def layer_stats(trades: pd.DataFrame) -> pd.DataFrame:
    closed = trades[trades["status"] == "CLOSED"]
    if closed.empty:
        return pd.DataFrame()
    total_net = closed["net_profit"].sum()
    gross_loss = closed.loc[closed["net_profit"] < 0, "net_profit"].sum()
    total_risk = closed["risk_money"].sum() if "risk_money" in closed else np.nan
    rows = []
    for layer, g in closed.groupby("layer"):
        net = g["net_profit"]
        gp, gl = float(net[net > 0].sum()), float(net[net < 0].sum())
        pf = profit_factor(gp, gl)
        rows.append({
            "layer": int(layer), "trades": int(len(g)), "net_profit": float(net.sum()),
            "win_rate_pct": float((net > 0).mean() * 100),
            "avg_net": float(net.mean()),
            "profit_factor": pf.value if pf.value is not None else np.nan,
            "profit_contribution_pct": float(net.sum() / total_net * 100) if total_net else np.nan,
            "loss_share_pct": float(gl / gross_loss * 100) + 0.0 if gross_loss else 0.0,
            "volume_share_pct": float(g["volume"].sum() / closed["volume"].sum() * 100),
            "risk_contribution_pct": float(g["risk_money"].sum() / total_risk * 100)
            if "risk_money" in g and g["risk_money"].notna().all() and total_risk
            else np.nan,
            "avg_volume": float(g["volume"].mean()),
        })
    return pd.DataFrame(rows)


def analyze_layering(trades: pd.DataFrame, point_vals: dict, risk_cfg, ea_events=None,
                     period_trades: pd.DataFrame | None = None) -> dict:
    """Full layering analysis for the trades in ``period_trades`` (default: all)."""
    pt = period_trades if period_trades is not None else trades
    out: dict = {"findings": []}
    if pt is None or pt.empty:
        out["available"] = False
        return out
    out["available"] = True
    out["basket_methods"] = pt["basket_method"].value_counts().to_dict()
    inferred_share = float((pt["basket_method"] != EXPLICIT).mean() * 100)
    out["inferred_share_pct"] = inferred_share
    # A basket is evaluated with ALL its layers, including ones opened in earlier periods
    full = trades[trades["basket_id"].isin(set(pt["basket_id"]))] if trades is not None \
        and not trades.empty else pt
    baskets = basket_table(full, point_vals)
    out["baskets"] = baskets
    out["layers"] = layer_stats(pt)
    out["exposure"] = simultaneous_exposure(pt)
    out["max_layer_count"] = int(baskets["layers"].max()) if not baskets.empty else 0

    multi = baskets[baskets["layers"] > 1]
    out["multi_layer_baskets"] = int(len(multi))
    out["avg_seconds_between_layers"] = float(multi["avg_seconds_between_layers"].mean()) \
        if not multi.empty else np.nan
    by_type = baskets.groupby("layering_type").agg(
        baskets=("basket_id", "count"), net_profit=("net_profit", "sum"),
        win_rate_pct=("net_profit", lambda s: float((s > 0).mean() * 100)))
    out["by_layering_type"] = by_type.reset_index()

    F = out["findings"]
    basis = " (baskets INFERRED from time overlap)" if inferred_share > 0 else ""
    # 1. Excessive stacking
    over = baskets[baskets["layers"] > risk_cfg.max_layers]
    if not over.empty:
        F.append(Finding("layering", "CRITICAL",
                         f"{len(over)} basket(s) exceeded the approved maximum of "
                         f"{risk_cfg.max_layers} layers (max observed "
                         f"{int(over['layers'].max())}){basis}.",
                         ["EA max-layer parameter may differ from approved value.",
                          "Basket inference may have merged independent signals - verify "
                          "with EA logs."], {"baskets": over["basket_id"].tolist()},
                         "EXCESSIVE_STACKING"))
    # 2. Adding while basket risk already high
    high = []
    for _, b in baskets.iterrows():
        for a in b["adds"]:
            if a.get("existing_risk_pct") is not None and \
                    a["existing_risk_pct"] >= risk_cfg.max_basket_risk_pct - 1e-9:
                high.append(b["basket_id"])
    if high:
        F.append(Finding("layering", "HIGH",
                         f"{len(set(high))} basket(s) added a layer when existing SL-implied "
                         f"basket risk was already ≥ {risk_cfg.max_basket_risk_pct}%.",
                         ["Layer risk sizing may not account for existing exposure."],
                         {"baskets": sorted(set(high))}, "ADD_AT_HIGH_RISK"))
    # 3. Layering without momentum confirmation (needs EA logs)
    ev = ea_events if ea_events is not None else pd.DataFrame()
    if multi.empty:
        out["confirmation_check"] = "NOT_APPLICABLE"
    elif ev.empty or ev["ticket"].isna().all():
        out["confirmation_check"] = "UNVERIFIABLE"
        out["confirmation_note"] = ("No EA log entries with tickets: cannot verify that added "
                                    "layers had a momentum/signal confirmation.")
    else:
        sig_tickets = set(ev.loc[ev["event_type"].isin(["SIGNAL", "ORDER"]) &
                                 ev["strategy"].notna(), "ticket"].dropna().astype(str))
        added = pt[(pt["layer"] > 1)]
        unconfirmed = added[~added["position_id"].astype(str).isin(sig_tickets)]
        out["confirmation_check"] = "CHECKED"
        out["unconfirmed_layers"] = int(len(unconfirmed))
        if len(unconfirmed):
            F.append(Finding("layering", "MEDIUM",
                             f"{len(unconfirmed)} added layer(s) have no matching signal/order "
                             "log entry with a strategy tag.",
                             ["Logs may be incomplete for the period.",
                              "Layers may be added on a timer/price-step rather than a signal."],
                             {"positions": unconfirmed["position_id"].tolist()[:50]},
                             "UNCONFIRMED_LAYER"))
    # 4. Increasing cumulative exposure (later layer larger than previous)
    esc = baskets[baskets["volume_escalation"]]
    if not esc.empty:
        F.append(Finding("layering", "HIGH",
                         f"{len(esc)} basket(s) added a layer with LARGER volume than the "
                         "previous layer (approved allocation is decreasing: "
                         f"{'/'.join(str(x) for x in risk_cfg.layer_risk_pct)}%).",
                         ["Possible martingale-style sizing.",
                          "Lot rounding at small balances can also cause this."],
                         {"baskets": esc["basket_id"].tolist()}, "LAYER_VOLUME_ESCALATION"))
    # 5. Repeated layering during losing conditions
    against = baskets[baskets["layering_type"] == "AGAINST"]
    out["against_baskets"] = int(len(against))
    if len(multi):
        share = len(against) / len(multi) * 100
        out["against_share_pct"] = share
        if len(against) >= 3 or (len(multi) >= 2 and share > 50):
            lost = against[against["net_profit"] < 0]
            F.append(Finding("layering", "MEDIUM",
                             f"{len(against)} of {len(multi)} multi-layer baskets ({share:.0f}%) "
                             "added layers while existing layers were in floating loss; "
                             f"{len(lost)} of those baskets closed at a net loss.",
                             ["Pullback strategy may intentionally average into retracements - "
                              "confirm against EA design."],
                             {"against": len(against), "lost": len(lost)}, "LAYER_IN_LOSS"))
    # Does layering increase drawdown disproportionately?
    ls = out["layers"]
    if not ls.empty and (ls["layer"] > 1).any():
        l2 = ls[ls["layer"] > 1]
        loss_share = float(l2["loss_share_pct"].sum())
        vol_share = float(l2["volume_share_pct"].sum())
        out["layer2plus_loss_share_pct"] = loss_share
        out["layer2plus_volume_share_pct"] = vol_share
        out["disproportionate"] = bool(loss_share > 1.5 * vol_share and loss_share > 0)
        if out["disproportionate"]:
            F.append(Finding("layering", "MEDIUM",
                             f"Layers 2+ carry {loss_share:.1f}% of gross loss but only "
                             f"{vol_share:.1f}% of volume - losses are disproportionate.",
                             ["Added layers may be entered late in moves (worse entries)."],
                             {}, "DISPROPORTIONATE_LAYER_LOSS"))
    return out
