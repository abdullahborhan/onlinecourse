"""
Data validation for imported records.

Produces a structured validation report used by the importer, the dashboard
(Data Management page) and the weekly report ("Data sources and completeness").
Validation never "repairs" data by inventing values; it reports problems.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from modules.normalizer import TRADE_TYPES
from modules.parsing_utils import parse_number

COST_COLS = ("commission", "swap", "fee")


def _missing_summary(df: pd.DataFrame, cols) -> dict:
    out = {}
    for c in cols:
        if c not in df.columns:
            out[c] = "COLUMN_ABSENT"
        else:
            n = int(df[c].isna().sum())
            out[c] = "COMPLETE" if n == 0 else ("ALL_MISSING" if n == len(df) else f"{n}_MISSING")
    return out


def validate_deals(df: pd.DataFrame) -> dict:
    rep: dict = {"rows": int(len(df)), "errors": [], "warnings": [], "info": []}
    if df.empty:
        rep["errors"].append("No deal rows.")
        return rep
    trade = df[df["deal_type"].isin(TRADE_TYPES)]
    nontrade = df[~df["deal_type"].isin(TRADE_TYPES)]
    rep["trade_deals"] = int(len(trade))
    rep["balance_operations"] = int(len(nontrade))
    rep["deal_types"] = df["deal_type"].value_counts(dropna=False).to_dict()
    rep["entries"] = trade["entry"].value_counts(dropna=False).to_dict() \
        if "entry" in trade else {}
    rep["period_start"] = df["time"].min()
    rep["period_end"] = df["time"].max()
    rep["field_completeness"] = _missing_summary(
        trade, ["entry", "position_id", "magic", "commission", "swap", "fee", "sl", "tp",
                "comment", "balance"])

    dups = df["deal_id"].duplicated(keep="first")
    rep["duplicate_deal_ids_in_file"] = int(dups.sum())
    if dups.any():
        rep["warnings"].append(f"{int(dups.sum())} duplicate deal ticket(s) inside the file "
                               "(first occurrence kept).")

    unknown_types = set(df["deal_type"].dropna()) - TRADE_TYPES - {
        "balance", "credit", "charge", "correction", "bonus", "commission"}
    if unknown_types:
        rep["warnings"].append(f"Unrecognised deal types: {sorted(unknown_types)}")

    if not trade.empty:
        bad_vol = trade["volume"].isna() | (trade["volume"] <= 0)
        if bad_vol.any():
            rep["errors"].append(f"{int(bad_vol.sum())} trade deal(s) with missing/non-positive "
                                 "volume.")
        if trade["entry"].isna().all():
            rep["warnings"].append("Deal entry (IN/OUT) not provided: entry/exit will be inferred "
                                   "from position direction where a position id exists.")
        if trade["position_id"].isna().all():
            rep["warnings"].append("Position ID not provided: positions cannot be reconstructed "
                                   "from deals alone; the report's Positions table is required.")
        for c in COST_COLS:
            if trade[c].isna().all():
                rep["warnings"].append(f"{c.capitalize()} not provided in source: net profit "
                                       f"excludes {c} and is labelled accordingly.")
        unk = trade["entry"].eq("UNKNOWN").sum() if "entry" in trade else 0
        if unk:
            rep["warnings"].append(f"{int(unk)} deal(s) with unrecognised entry direction.")

    rep["balance_continuity"] = check_balance_continuity(df)
    bc = rep["balance_continuity"]
    if bc.get("checked") and bc.get("mismatches", 0) > 0:
        rep["warnings"].append(
            f"Balance continuity: {bc['mismatches']} break(s) detected - deals may be missing "
            "from the file or the file may be a partial period.")
    return rep


def check_balance_continuity(df: pd.DataFrame, tol: float = 0.011) -> dict:
    """balance[i] should equal balance[i-1] + profit + commission + swap + fee."""
    if "balance" not in df.columns or df["balance"].isna().all():
        return {"checked": False, "reason": "No balance column"}
    d = df.sort_values(["time", "deal_id"], kind="stable")
    d = d[d["balance"].notna()]
    if len(d) < 2:
        return {"checked": False, "reason": "Too few rows"}
    delta = d[["profit", "commission", "swap", "fee"]].fillna(0).sum(axis=1)
    expected = d["balance"].shift(1) + delta
    diff = (d["balance"] - expected).abs()
    mism = diff.iloc[1:] > tol
    return {"checked": True, "mismatches": int(mism.sum()),
            "max_abs_difference": float(diff.iloc[1:].max()) if len(diff) > 1 else 0.0,
            "first_break_time": str(d["time"].iloc[1:][mism.values].min()) if mism.any() else None}


def validate_positions(df: pd.DataFrame) -> dict:
    rep: dict = {"rows": int(len(df)), "errors": [], "warnings": [], "info": []}
    if df.empty:
        rep["errors"].append("No position rows.")
        return rep
    rep["period_start"] = df["open_time"].min()
    rep["period_end"] = df["close_time"].max()
    rep["field_completeness"] = _missing_summary(
        df, ["sl", "tp", "commission", "swap", "fee", "magic", "comment"])
    neg = (df["close_time"] < df["open_time"]).sum()
    if neg:
        rep["errors"].append(f"{int(neg)} position(s) close before they open.")
    still_open = df["close_time"].isna().sum()
    if still_open:
        rep["info"].append(f"{int(still_open)} position(s) without close time (open).")
    dup = df["position_id"].duplicated().sum()
    rep["duplicate_position_ids_in_file"] = int(dup)
    if dup:
        rep["warnings"].append(f"{int(dup)} duplicate position id(s) inside file.")
    return rep


def validate_equity(df: pd.DataFrame) -> dict:
    rep: dict = {"rows": int(len(df)), "errors": [], "warnings": [], "info": []}
    if df.empty:
        rep["errors"].append("No equity observations.")
        return rep
    rep["period_start"] = df["time"].min()
    rep["period_end"] = df["time"].max()
    if (df["equity"] <= 0).any():
        rep["warnings"].append("Non-positive equity values present.")
    gaps = df.sort_values("time")["time"].diff().dt.total_seconds()
    rep["median_sampling_seconds"] = float(gaps.median()) if len(gaps) > 1 else None
    rep["max_gap_hours"] = float(gaps.max() / 3600) if len(gaps) > 1 else None
    if rep["max_gap_hours"] and rep["max_gap_hours"] > 24:
        rep["warnings"].append(f"Equity history has gaps up to {rep['max_gap_hours']:.1f}h; "
                               "intraday drawdown inside gaps is not observed.")
    return rep


def cross_check_results(deals: pd.DataFrame, results: dict) -> dict:
    """Compare deal totals with the totals MT5 states in the report 'Results' section."""
    out: dict = {"checked": False}
    if deals.empty or not results:
        return out
    stated = parse_number(results.get("Total Net Profit"))
    if stated is None:
        return out
    trade = deals[deals["deal_type"].isin(TRADE_TYPES)]
    computed = float(trade[["profit", "commission", "swap", "fee"]].fillna(0).sum().sum())
    out = {"checked": True, "stated_total_net_profit": stated,
           "computed_total_net_profit": round(computed, 2),
           "difference": round(computed - stated, 2),
           "match": bool(np.isclose(computed, stated, atol=0.02))}
    return out


def merge_reports(*reports: dict) -> dict:
    out = {"errors": [], "warnings": [], "info": []}
    for r in reports:
        for k in ("errors", "warnings", "info"):
            out[k].extend(r.get(k, []))
    return out
