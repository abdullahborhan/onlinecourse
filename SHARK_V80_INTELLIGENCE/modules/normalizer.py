"""
Canonical schema normalisation for MT5 deals, positions, orders and equity.

Raw tables from HTML or CSV are mapped onto canonical columns through an alias
table. Values that are absent in the source stay NULL (None/NaN). Each
normaliser returns ``(dataframe, issues)`` where ``issues`` is a list of
human-readable validation messages.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from modules.parsing_utils import norm_header, parse_numbers, parse_timestamps

DEAL_ALIASES = {
    "deal_id": ["deal", "dealticket", "dealid", "ticket", "dealno"],
    "time": ["time", "dealtime", "datetime", "timestamp", "date", "executiontime"],
    "symbol": ["symbol", "instrument", "market"],
    "deal_type": ["type", "dealtype", "action", "side"],
    "entry": ["direction", "entry", "dealentry", "inout"],
    "volume": ["volume", "lots", "lot", "size", "qty", "quantity"],
    "price": ["price", "dealprice", "executionprice"],
    "order_id": ["order", "orderticket", "orderid"],
    "position_id": ["position", "positionid", "positionticket", "posid", "positionidentifier"],
    "magic": ["magic", "magicnumber", "expert", "expertid", "eaid"],
    "commission": ["commission", "commissions", "comm"],
    "swap": ["swap", "swaps", "rollover"],
    "fee": ["fee", "fees"],
    "profit": ["profit", "pl", "pnl", "grossprofit"],
    "balance": ["balance"],
    "sl": ["sl", "stoploss"],
    "tp": ["tp", "takeprofit"],
    "comment": ["comment", "comments", "note", "remark"],
}
DEAL_REQUIRED = ["deal_id", "time", "deal_type", "volume", "price", "profit"]

POSITION_ALIASES = {
    "position_id": ["position", "positionid", "ticket", "positionticket", "posid"],
    "open_time": ["opentime", "timeopen", "entrytime", "time"],
    "symbol": ["symbol", "instrument"],
    "direction": ["type", "direction", "side"],
    "volume": ["volume", "lots", "size"],
    "open_price": ["openprice", "entryprice", "priceopen", "price"],
    "sl": ["sl", "stoploss"],
    "tp": ["tp", "takeprofit"],
    "close_time": ["closetime", "timeclose", "exittime", "time1"],
    "close_price": ["closeprice", "exitprice", "priceclose", "price1"],
    "commission": ["commission"],
    "swap": ["swap"],
    "fee": ["fee"],
    "profit": ["profit", "pl", "pnl"],
    "magic": ["magic", "magicnumber"],
    "comment": ["comment", "comments"],
}
POSITION_REQUIRED = ["position_id", "open_time", "direction", "volume", "open_price",
                     "close_time", "close_price", "profit"]

ORDER_ALIASES = {
    "order_id": ["order", "ticket", "orderid"],
    "open_time": ["opentime", "time", "setuptime"],
    "symbol": ["symbol"],
    "order_type": ["type", "ordertype"],
    "volume_initial": ["volume", "lots"],
    "price": ["price"],
    "sl": ["sl", "stoploss"],
    "tp": ["tp", "takeprofit"],
    "done_time": ["time1", "donetime", "filltime"],
    "state": ["state", "status"],
    "comment": ["comment"],
}

EQUITY_ALIASES = {
    "time": ["time", "datetime", "date", "timestamp"],
    "balance": ["balance"],
    "equity": ["equity"],
}

ENTRY_MAP = {"in": "IN", "out": "OUT", "inout": "INOUT", "in/out": "INOUT", "outby": "OUT_BY",
             "out by": "OUT_BY", "0": "IN", "1": "OUT", "2": "INOUT", "3": "OUT_BY",
             "entry": "IN", "exit": "OUT", "open": "IN", "close": "OUT"}
TYPE_MAP = {"buy": "buy", "sell": "sell", "balance": "balance", "credit": "credit",
            "charge": "charge", "correction": "correction", "bonus": "bonus",
            "commission": "commission", "deposit": "balance", "withdrawal": "balance",
            "0": "buy", "1": "sell", "2": "balance", "3": "credit", "4": "charge",
            "5": "correction", "6": "bonus", "7": "commission", "dealtypebuy": "buy",
            "dealtypesell": "sell", "long": "buy", "short": "sell"}
TRADE_TYPES = {"buy", "sell"}


def map_columns(columns, aliases: dict) -> dict:
    """Return {canonical: original_column} using the alias table (first match wins)."""
    normed = {}
    for c in columns:
        normed.setdefault(norm_header(c), c)
    mapping = {}
    used = set()
    for canon, alist in aliases.items():
        for a in alist:
            if a in normed and normed[a] not in used:
                mapping[canon] = normed[a]
                used.add(normed[a])
                break
    return mapping


def _raw_json(df: pd.DataFrame) -> pd.Series:
    return pd.Series([json.dumps({str(k): (None if pd.isna(v) else str(v))
                                  for k, v in row.items()}, ensure_ascii=False)
                      for row in df.to_dict(orient="records")], index=df.index)


def _norm_entry(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip().lower()
    if s == "":
        return None
    return ENTRY_MAP.get(s, ENTRY_MAP.get(s.replace(" ", ""), "UNKNOWN"))


def _norm_type(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip().lower()
    if s in TYPE_MAP:
        return TYPE_MAP[s]
    key = s.replace(" ", "").replace("_", "")
    if key in TYPE_MAP:
        return TYPE_MAP[key]
    if s.startswith("buy"):
        return "buy"
    if s.startswith("sell"):
        return "sell"
    return s


def _str_id(series: pd.Series) -> pd.Series:
    def f(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        s = str(v).strip()
        if s == "" or s.lower() in {"nan", "none"}:
            return None
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    return series.map(f).astype("object")


def normalize_deals(raw: pd.DataFrame, account_id: str, source_file_id: int | None,
                    decimal_comma: bool = False) -> tuple[pd.DataFrame, list, dict]:
    issues: list[str] = []
    mapping = map_columns(raw.columns, DEAL_ALIASES)
    missing = [c for c in DEAL_REQUIRED if c not in mapping]
    meta = {"mapping": mapping, "missing_required": missing, "timestamp": None,
            "optional_missing": [c for c in DEAL_ALIASES if c not in mapping
                                 and c not in DEAL_REQUIRED]}
    if missing:
        issues.append(f"Missing required deal columns: {', '.join(missing)}")
        return pd.DataFrame(), issues, meta

    out = pd.DataFrame(index=raw.index)
    out["account_id"] = account_id
    out["deal_id"] = _str_id(raw[mapping["deal_id"]])
    times, tinfo = parse_timestamps(raw[mapping["time"]])
    meta["timestamp"] = tinfo
    out["time"] = times
    if tinfo.get("warning"):
        issues.append(tinfo["warning"])
    out["symbol"] = raw[mapping["symbol"]].astype("object") if "symbol" in mapping else None
    out["deal_type"] = raw[mapping["deal_type"]].map(_norm_type)
    out["entry"] = raw[mapping["entry"]].map(_norm_entry) if "entry" in mapping else None
    for col in ("volume", "price", "commission", "swap", "fee", "profit", "balance", "sl", "tp"):
        out[col] = parse_numbers(raw[mapping[col]], decimal_comma) if col in mapping else np.nan
    for col in ("order_id", "position_id", "magic"):
        out[col] = _str_id(raw[mapping[col]]) if col in mapping else None
    out["comment"] = raw[mapping["comment"]].astype("object") if "comment" in mapping else None
    out["source_file_id"] = source_file_id
    out["raw_json"] = _raw_json(raw)

    # Clean comment/symbol blanks to None
    for col in ("comment", "symbol"):
        out[col] = out[col].map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v))
                                or str(v).strip() == "" else str(v).strip())

    # SL/TP of 0 in MT5 means "not set" (not a price of zero) -> keep 0 as "not set" marker
    # Rows that are not deals (summary/total rows) have no time AND no deal id
    bad = out["time"].isna() | out["deal_id"].isna()
    if bad.any():
        issues.append(f"{int(bad.sum())} row(s) without a valid time or deal ticket were "
                      "excluded (e.g. summary/total rows).")
        out = out[~bad]
    return out.reset_index(drop=True), issues, meta


def normalize_positions(raw: pd.DataFrame, account_id: str, source_file_id: int | None,
                        decimal_comma: bool = False) -> tuple[pd.DataFrame, list, dict]:
    issues: list[str] = []
    mapping = map_columns(raw.columns, POSITION_ALIASES)
    missing = [c for c in POSITION_REQUIRED if c not in mapping]
    meta = {"mapping": mapping, "missing_required": missing}
    if missing:
        issues.append(f"Missing required position columns: {', '.join(missing)}")
        return pd.DataFrame(), issues, meta
    out = pd.DataFrame(index=raw.index)
    out["account_id"] = account_id
    out["position_id"] = _str_id(raw[mapping["position_id"]])
    for tcol in ("open_time", "close_time"):
        t, info = parse_timestamps(raw[mapping[tcol]])
        out[tcol] = t
        meta[f"{tcol}_format"] = info
        if info.get("warning"):
            issues.append(f"{tcol}: {info['warning']}")
    out["symbol"] = raw[mapping["symbol"]].astype("object") if "symbol" in mapping else None
    out["direction"] = raw[mapping["direction"]].map(_norm_type)
    for col in ("volume", "open_price", "sl", "tp", "close_price", "commission", "swap", "fee",
                "profit"):
        out[col] = parse_numbers(raw[mapping[col]], decimal_comma) if col in mapping else np.nan
    out["magic"] = _str_id(raw[mapping["magic"]]) if "magic" in mapping else None
    out["comment"] = raw[mapping["comment"]].astype("object") if "comment" in mapping else None
    out["comment"] = out["comment"].map(
        lambda v: None if v is None or (isinstance(v, float) and np.isnan(v))
        or str(v).strip() == "" else str(v).strip())
    out["source_file_id"] = source_file_id
    out["raw_json"] = _raw_json(raw)
    bad = out["open_time"].isna() | out["position_id"].isna()
    if bad.any():
        issues.append(f"{int(bad.sum())} position row(s) without open time/ticket excluded.")
        out = out[~bad]
    bad_dir = ~out["direction"].isin(TRADE_TYPES)
    if bad_dir.any():
        issues.append(f"{int(bad_dir.sum())} position row(s) with unknown direction excluded.")
        out = out[~bad_dir]
    return out.reset_index(drop=True), issues, meta


def normalize_orders(raw: pd.DataFrame, account_id: str, source_file_id: int | None,
                     decimal_comma: bool = False) -> tuple[pd.DataFrame, list, dict]:
    issues: list[str] = []
    mapping = map_columns(raw.columns, ORDER_ALIASES)
    if "order_id" not in mapping or "open_time" not in mapping:
        issues.append("Orders table missing order ticket or time; orders skipped.")
        return pd.DataFrame(), issues, {"mapping": mapping}
    out = pd.DataFrame(index=raw.index)
    out["account_id"] = account_id
    out["order_id"] = _str_id(raw[mapping["order_id"]])
    out["open_time"], _ = parse_timestamps(raw[mapping["open_time"]])
    if "done_time" in mapping:
        out["done_time"], _ = parse_timestamps(raw[mapping["done_time"]])
    else:
        out["done_time"] = pd.NaT
    out["symbol"] = raw[mapping["symbol"]] if "symbol" in mapping else None
    out["order_type"] = raw[mapping["order_type"]].astype(str).str.lower() \
        if "order_type" in mapping else None
    if "volume_initial" in mapping:
        vol_raw = raw[mapping["volume_initial"]].astype(str)
        out["volume_initial"] = parse_numbers(vol_raw.str.split("/").str[0], decimal_comma)
        out["volume_filled"] = parse_numbers(vol_raw.str.split("/").str[-1], decimal_comma)
    for col in ("price", "sl", "tp"):
        out[col] = parse_numbers(raw[mapping[col]], decimal_comma) if col in mapping else np.nan
    out["state"] = raw[mapping["state"]].astype(str).str.lower() if "state" in mapping else None
    out["comment"] = raw[mapping["comment"]] if "comment" in mapping else None
    out["source_file_id"] = source_file_id
    out["raw_json"] = _raw_json(raw)
    out = out[out["open_time"].notna() & out["order_id"].notna()]
    return out.reset_index(drop=True), issues, {"mapping": mapping}


def normalize_equity(raw: pd.DataFrame, account_id: str, source_file_id: int | None,
                     decimal_comma: bool = False) -> tuple[pd.DataFrame, list, dict]:
    issues: list[str] = []
    mapping = map_columns(raw.columns, EQUITY_ALIASES)
    if "time" not in mapping or "equity" not in mapping:
        issues.append("Equity history requires 'time' and 'equity' columns.")
        return pd.DataFrame(), issues, {"mapping": mapping}
    out = pd.DataFrame(index=raw.index)
    out["account_id"] = account_id
    out["time"], tinfo = parse_timestamps(raw[mapping["time"]])
    if tinfo.get("warning"):
        issues.append(tinfo["warning"])
    out["equity"] = parse_numbers(raw[mapping["equity"]], decimal_comma)
    out["balance"] = parse_numbers(raw[mapping["balance"]], decimal_comma) \
        if "balance" in mapping else np.nan
    out["source_file_id"] = source_file_id
    bad = out["time"].isna() | out["equity"].isna()
    if bad.any():
        issues.append(f"{int(bad.sum())} equity row(s) without valid time/equity excluded.")
        out = out[~bad]
    return out.reset_index(drop=True), issues, {"mapping": mapping, "timestamp": tinfo}


def detect_csv_kind(columns) -> str:
    keys = {norm_header(c) for c in columns}
    has = lambda *names: any(n in keys for n in names)  # noqa: E731
    if has("equity") and has("time", "datetime", "date", "timestamp") and not has("price", "symbol"):
        return "equity"
    if has("closetime", "timeclose", "exittime", "time1") and \
            has("closeprice", "exitprice", "priceclose", "price1"):
        return "positions"
    if has("deal", "dealticket", "dealid", "ticket") and has("type", "dealtype", "action", "side"):
        return "deals"
    return "unknown"
