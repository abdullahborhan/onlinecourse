"""
CSV reader for MT5 trade history exports, position lists and equity/balance
history files. Delimiter and decimal convention are detected automatically.
"""
from __future__ import annotations

import csv
import io

import pandas as pd

from modules.normalizer import detect_csv_kind


def sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
        return max(counts, key=counts.get)


def read_csv_text(text: str) -> tuple[pd.DataFrame, dict]:
    """Read CSV text into a DataFrame of raw strings (no type coercion)."""
    delim = sniff_delimiter(text)
    df = pd.read_csv(io.StringIO(text), sep=delim, dtype=str, keep_default_na=False,
                     skipinitialspace=True, engine="python")
    # Duplicate header names: pandas renames "Time" -> "Time.1"; normaliser maps "time1".
    df.columns = [str(c).strip() for c in df.columns]
    df = df.loc[:, [c for c in df.columns if not c.lower().startswith("unnamed")]]
    info = {"delimiter": delim, "decimal_comma": delim == ";", "rows": len(df),
            "columns": list(df.columns), "kind": detect_csv_kind(df.columns)}
    return df, info
