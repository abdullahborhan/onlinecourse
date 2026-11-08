"""
Low-level parsing helpers shared by every importer.

* ``read_text``      - decode bytes (MT5 writes UTF-16 LE with BOM by default).
* ``parse_number``   - locale tolerant numeric parsing; blanks -> None (never 0).
* ``parse_timestamps`` - detects the broker timestamp format for a whole column
  and reports ambiguity (e.g. 03/04/2026 could be 3 April or 4 March).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

import numpy as np
import pandas as pd

_NUM_CLEAN = re.compile(r"[^\d,.\-+eE]")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(data: bytes) -> tuple[str, str]:
    """Decode file bytes; returns (text, encoding)."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    # UTF-16 without BOM: many NUL bytes in alternate positions
    if len(data) > 4 and data[1:200:2].count(0) > len(data[1:200:2]) * 0.6:
        return data.decode("utf-16-le", errors="replace"), "utf-16-le"
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"  # pragma: no cover


def parse_number(value, decimal_comma: bool = False):
    """Parse a number from MT5/CSV text. Returns None for blank/non-numeric input."""
    if value is None:
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        if isinstance(value, float) and np.isnan(value):
            return None
        return float(value)
    s = str(value).strip().replace(" ", " ").replace("−", "-")
    if s == "" or s in {"-", "—", "n/a", "N/A", "None", "nan", "NaN"}:
        return None
    # "0.10 / 0.10" (MT5 order volume) -> first value
    if " / " in s:
        s = s.split(" / ")[0]
    s = s.replace(" ", "")
    s = _NUM_CLEAN.sub("", s)
    if s in {"", "-", "+"}:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if decimal_comma or re.fullmatch(r"-?\d+,\d{1,2}", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_numbers(series: pd.Series, decimal_comma: bool = False) -> pd.Series:
    return series.map(lambda v: parse_number(v, decimal_comma)).astype("float64")


_FORMATS = [
    "%Y.%m.%d %H:%M:%S.%f", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y.%m.%d",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
]
_AMBIGUOUS_PAIRS = [
    ("%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"),
    ("%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M"),
    ("%d/%m/%Y", "%m/%d/%Y"),
    ("%d-%m-%Y %H:%M:%S", "%m-%d-%Y %H:%M:%S"),
    ("%d-%m-%Y %H:%M", "%m-%d-%Y %H:%M"),
]


def _try_format(values: pd.Series, fmt: str) -> pd.Series:
    return pd.to_datetime(values, format=fmt, errors="coerce")


def parse_timestamps(series: pd.Series, dayfirst_default: bool = True
                     ) -> tuple[pd.Series, dict]:
    """Parse a column of broker timestamps.

    Returns the parsed series (NaT for unparseable cells) and an info dict with
    the detected ``format``, ``failed`` count and an ``ambiguous`` flag.
    """
    info = {"format": None, "failed": 0, "ambiguous": False, "warning": None}
    raw = series.astype("string").str.strip()
    raw = raw.where(raw.notna() & (raw != ""), None)
    nonnull = raw.dropna()
    if nonnull.empty:
        info["failed"] = int(series.size)
        return pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]"), info

    # Unix epoch
    if nonnull.str.fullmatch(r"\d{9,13}(\.\d+)?").all():
        nums = pd.to_numeric(raw, errors="coerce")
        unit = "ms" if nums.dropna().median() > 1e11 else "s"
        out = pd.to_datetime(nums, unit=unit, errors="coerce")
        info["format"] = f"epoch_{unit}"
        info["failed"] = int(out[raw.notna()].isna().sum())
        return out, info

    # Unambiguous formats (year-first or dotted day-first) are combined row by row so a file
    # mixing e.g. "2026.01.05 10:00:00" and "2026.01.05 10:00" parses completely.
    parsed = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
    used = []
    for fmt in _FORMATS:
        todo = raw.notna() & parsed.isna()
        if not todo.any():
            break
        p = _try_format(raw[todo], fmt)
        if p.notna().any():
            parsed.loc[p.index[p.notna()]] = p[p.notna()]
            used.append(fmt)
    ok = parsed[raw.notna()].notna().mean()
    best = (" | ".join(used), ok, parsed) if ok >= 0.98 and used else None
    if best is None:
        for dfmt, mfmt in _AMBIGUOUS_PAIRS:
            pd_ = _try_format(raw, dfmt)
            pm_ = _try_format(raw, mfmt)
            okd = pd_[raw.notna()].notna().mean()
            okm = pm_[raw.notna()].notna().mean()
            if okd < 0.98 and okm < 0.98:
                continue
            if okd >= 0.98 and okm >= 0.98:
                info["ambiguous"] = True
                chosen = (dfmt, okd, pd_) if dayfirst_default else (mfmt, okm, pm_)
                info["warning"] = (
                    "Timestamp day/month order is ambiguous (all days <= 12). Interpreted as "
                    f"{'DAY-first' if dayfirst_default else 'MONTH-first'}; verify against MT5.")
                best = chosen
            elif okd >= okm:
                best = (dfmt, okd, pd_)
            else:
                best = (mfmt, okm, pm_)
            break
    if best is None:
        # No reliable format: never guess (guessing can silently swap day and month).
        info["format"] = None
        info["failed"] = int(raw.notna().sum())
        sample = ", ".join(str(x) for x in nonnull.head(3))
        info["warning"] = f"Unrecognised timestamp format (e.g. {sample})."
        return pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]"), info

    fmt, _, parsed = best
    info["format"] = fmt
    info["failed"] = int(parsed[raw.notna()].isna().sum())
    if info["failed"]:
        bad = ", ".join(str(x) for x in raw[raw.notna() & parsed.isna()].head(3))
        info["warning"] = (info.get("warning") or "") + \
            f" {info['failed']} timestamp(s) could not be parsed (e.g. {bad})."
    # Drop tz if any (store naive broker server time)
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_localize(None)
    return parsed, info


def norm_header(h: str) -> str:
    """Normalise a column header: lowercase, alnum only."""
    return re.sub(r"[^a-z0-9]", "", str(h).lower())


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
