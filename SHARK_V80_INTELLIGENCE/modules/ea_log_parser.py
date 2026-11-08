"""
Parser for EA journal / Experts logs and EA transaction logs.

Extracts, where present in the text:
* timestamp (full timestamp, or time-of-day + date from a YYYYMMDD file name)
* strategy keyword (BREAKOUT / MOMENTUM / PULLBACK) via configurable patterns
* ticket / position / order numbers
* signal id, layer number, spread, requested price
* event type (SIGNAL, ORDER, CLOSE, REJECTION, LOCK, INFO)

Nothing is inferred beyond what the text states.
"""
from __future__ import annotations

import re

import pandas as pd

from modules.normalizer import map_columns

_TS_FULL = re.compile(r"(\d{4}[.\-/]\d{2}[.\-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)")
_TS_TIME = re.compile(r"(?<![\d.:])(\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?![\d:])")
_DATE_IN_NAME = re.compile(r"(20\d{2})(\d{2})(\d{2})")
_TICKET = re.compile(r"(?:ticket|position|pos|order|deal)\s*[#:=]?\s*#?(\d{3,})", re.I)
_HASH_TICKET = re.compile(r"#(\d{4,})")
_SIGNAL = re.compile(r"(?:signal[ _-]?id|sig[ _-]?id|signal)\s*[#:=]\s*([A-Za-z0-9_\-]+)", re.I)
_LAYER = re.compile(r"\blayer\s*[#:=]?\s*([1-9])\b|\bL([1-9])\b", re.I)
_SPREAD = re.compile(r"spread\s*[=:]\s*([\d.]+)", re.I)
_REQ_PRICE = re.compile(r"(?:req(?:uested)?[ _]?price|request(?:ed)? at)\s*[=:]?\s*([\d.]+)", re.I)

_EVENT_RULES = [
    ("REJECTION", re.compile(r"reject|requote|failed|no money|invalid (stops|volume|price)|"
                             r"off quotes|trade disabled|market closed|retcode\s*=\s*(?!10009|10008)\d+|"
                             r"error\s*\d+", re.I)),
    ("LOCK", re.compile(r"lock|hard stop|target reached|trading (halted|paused|stopped)|"
                        r"daily (loss|profit) limit|weekly (loss|limit)", re.I)),
    ("CLOSE", re.compile(r"\bclos(e|ed|ing)\b|take profit|stop loss hit|\bsl hit|\btp hit", re.I)),
    ("ORDER", re.compile(r"\b(open(ed)?|placed|filled|deal #|order #|position #|buy|sell)\b", re.I)),
    ("SIGNAL", re.compile(r"signal", re.I)),
]

DEFAULT_STRATEGY_PATTERNS = {
    "BREAKOUT": r"(breakout|\bbrk\b|\bbo\b)",
    "MOMENTUM": r"(momentum|scalp|\bmom\b)",
    "PULLBACK": r"(pullback|pull[ _-]?back|\bpb\b)",
}


def _classify_event(msg: str) -> str:
    for name, rx in _EVENT_RULES:
        if rx.search(msg):
            return name
    return "INFO"


def _strategy(msg: str, patterns: dict) -> str | None:
    hits = [s for s, p in patterns.items() if re.search(p, msg, re.I)]
    return hits[0] if len(hits) == 1 else None  # ambiguous lines stay unclassified


def _parse_lines(text: str, filename: str, patterns: dict) -> list[dict]:
    date_hint = None
    m = _DATE_IN_NAME.search(filename or "")
    if m:
        date_hint = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        ts = None
        mf = _TS_FULL.search(line)
        if mf:
            ts = pd.to_datetime(mf.group(1).replace(".", "-", 2).replace("/", "-"),
                                errors="coerce")
            msg = line[mf.end():].strip(" \t|-")
        else:
            mt = _TS_TIME.search(line)
            if mt and date_hint:
                ts = pd.to_datetime(f"{date_hint} {mt.group(1)}", errors="coerce")
            msg = line[mt.end():].strip(" \t|-") if mt else line
        # Drop "EA name (Symbol,TF)" prefix noise but keep it in message for traceability
        tm = _TICKET.search(msg) or _HASH_TICKET.search(msg)
        lm = _LAYER.search(msg)
        sp = _SPREAD.search(msg)
        rp = _REQ_PRICE.search(msg)
        sg = _SIGNAL.search(msg)
        rows.append({
            "time": ts if ts is not None and not pd.isna(ts) else None,
            "event_type": _classify_event(msg),
            "strategy": _strategy(msg, patterns),
            "signal_id": sg.group(1) if sg else None,
            "ticket": tm.group(1) if tm else None,
            "layer": int(lm.group(1) or lm.group(2)) if lm else None,
            "spread": float(sp.group(1)) if sp else None,
            "requested_price": float(rp.group(1)) if rp else None,
            "message": msg[:1000],
        })
    return rows


def parse_ea_log(text: str, filename: str = "", patterns: dict | None = None
                 ) -> tuple[pd.DataFrame, dict]:
    """Parse EA journal/expert text logs or EA CSV transaction logs."""
    patterns = patterns or DEFAULT_STRATEGY_PATTERNS
    info = {"format": "text_log", "lines": 0, "with_time": 0, "with_strategy": 0,
            "with_ticket": 0, "issues": []}
    first = text.lstrip()[:500].lower()
    if "," in first.split("\n")[0] and any(k in first.split("\n")[0]
                                            for k in ("strategy", "event", "ticket")):
        from modules.csv_parser import read_csv_text
        raw, _ = read_csv_text(text)
        mapping = map_columns(raw.columns, {
            "time": ["time", "datetime", "timestamp"], "strategy": ["strategy", "setup"],
            "ticket": ["ticket", "position", "positionid", "order"],
            "signal_id": ["signalid", "signal"], "layer": ["layer"],
            "event_type": ["event", "eventtype", "action"], "message": ["message", "msg", "text"],
            "spread": ["spread"], "requested_price": ["requestedprice", "reqprice"]})
        info["format"] = "csv_log"
        df = pd.DataFrame()
        df["time"] = pd.to_datetime(raw[mapping["time"]], errors="coerce") \
            if "time" in mapping else pd.NaT
        for c in ("strategy", "ticket", "signal_id", "event_type", "message"):
            df[c] = raw[mapping[c]].replace("", None) if c in mapping else None
        df["strategy"] = df["strategy"].map(lambda v: _strategy(str(v), patterns) if v else None)
        df["layer"] = pd.to_numeric(raw[mapping["layer"]], errors="coerce") \
            if "layer" in mapping else None
        for c in ("spread", "requested_price"):
            df[c] = pd.to_numeric(raw[mapping[c]], errors="coerce") if c in mapping else None
        if df["message"].isna().all():
            df["message"] = raw.astype(str).agg(" | ".join, axis=1)
        df["event_type"] = df["event_type"].fillna(df["message"].map(
            lambda m: _classify_event(str(m))))
    else:
        df = pd.DataFrame(_parse_lines(text, filename, patterns))
    if df.empty:
        info["issues"].append("No log lines found.")
        return df, info
    df["ticket"] = df["ticket"].map(lambda v: None if v is None or pd.isna(v) else
                                    str(v).split(".")[0])
    info["lines"] = int(len(df))
    info["with_time"] = int(df["time"].notna().sum())
    info["with_strategy"] = int(df["strategy"].notna().sum())
    info["with_ticket"] = int(df["ticket"].notna().sum())
    if info["with_time"] == 0:
        info["issues"].append("No timestamps found; if this is a terminal log, keep the "
                              "original YYYYMMDD.log file name so the date can be read.")
    no_time = df["time"].isna().sum()
    if no_time:
        info["issues"].append(f"{int(no_time)} line(s) without a timestamp were kept but "
                              "cannot be matched in time.")
    return df, info
