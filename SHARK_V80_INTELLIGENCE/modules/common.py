"""
Shared primitives for SVPI.

Every metric is wrapped in a ``Metric`` so that its epistemic status travels
with the number. A missing input never silently becomes zero.

Status vocabulary
-----------------
VERIFIED     A value read directly from imported records (e.g. a deal's profit).
CALCULATED   Deterministically computed from verified records.
ESTIMATED    Computed from an approximation that is explicitly labelled
             (e.g. closed-trade balance curve used in place of equity).
UNDEFINED    Mathematically undefined (e.g. profit factor with zero gross loss).
UNAVAILABLE  The imported data does not contain the information required.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

VERIFIED = "VERIFIED"
CALCULATED = "CALCULATED"
ESTIMATED = "ESTIMATED"
UNDEFINED = "UNDEFINED"
UNAVAILABLE = "UNAVAILABLE"

# Compliance verdicts
COMPLIANT = "COMPLIANT"
BREACH = "BREACH"
UNVERIFIABLE = "UNVERIFIABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"

# Strategy labels
BREAKOUT = "BREAKOUT"
MOMENTUM = "MOMENTUM"
PULLBACK = "PULLBACK"
UNCLASSIFIED = "UNCLASSIFIED"
STRATEGIES = (BREAKOUT, MOMENTUM, PULLBACK)

# Report status
STATUS_STABLE = "STABLE"
STATUS_ATTENTION = "NEEDS ATTENTION"
STATUS_STOP = "TEMPORARILY STOP LIVE TRADING"
STATUS_DATA_MISSING = "DATA MISSING"


@dataclass
class Metric:
    value: Any
    status: str = CALCULATED
    unit: str = ""
    basis: str = ""
    note: str = ""

    @property
    def available(self) -> bool:
        return self.status not in (UNAVAILABLE,) and self.value is not None

    def to_dict(self) -> dict:
        v = self.value
        if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
            v = "INF" if math.isinf(v) and v > 0 else ("-INF" if math.isinf(v) else None)
        return {"value": _jsonable(v), "status": self.status, "unit": self.unit,
                "basis": self.basis, "note": self.note}

    def fmt(self, digits: int = 2) -> str:
        return format_metric(self, digits)


def unavailable(reason: str, unit: str = "") -> Metric:
    return Metric(None, UNAVAILABLE, unit=unit, note=reason)


def undefined(reason: str, value: Any = None, unit: str = "") -> Metric:
    return Metric(value, UNDEFINED, unit=unit, note=reason)


def _jsonable(v: Any) -> Any:
    try:
        import numpy as np
        import pandas as pd
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            f = float(v)
            return None if math.isnan(f) else f
        if isinstance(v, (pd.Timestamp,)):
            return v.isoformat()
        if isinstance(v, (pd.Timedelta,)):
            return v.total_seconds()
    except Exception:  # pragma: no cover
        pass
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def format_metric(m: Metric | dict | None, digits: int = 2) -> str:
    if m is None:
        return "UNAVAILABLE"
    if isinstance(m, dict):
        value, status, unit = m.get("value"), m.get("status"), m.get("unit", "")
    else:
        value, status, unit = m.value, m.status, m.unit
    if status == UNAVAILABLE or value is None:
        return "UNAVAILABLE"
    if value in ("INF", float("inf")):
        return "∞ (undefined: no losses)"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int,)) and unit not in ("%", "ccy"):
        s = f"{value:,}"
    elif isinstance(value, float):
        if unit == "s":
            return format_duration(value)
        s = f"{value:,.{digits}f}"
    else:
        s = str(value)
    if unit == "%":
        s += "%"
    elif unit and unit not in ("ccy", "count", "s"):
        s += f" {unit}"
    if status == ESTIMATED:
        s += " (est.)"
    return s


def format_duration(seconds: float | None) -> str:
    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return "UNAVAILABLE"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.2f}h"
    return f"{seconds / 86400:.2f}d"


def metrics_to_dict(d: dict) -> dict:
    """Recursively convert Metric objects inside a nested structure to dicts."""
    if isinstance(d, Metric):
        return d.to_dict()
    if isinstance(d, dict):
        return {str(k): metrics_to_dict(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [metrics_to_dict(x) for x in d]
    return _jsonable(d)


@dataclass
class Finding:
    """A FACT supported by records, optionally with HYPOTHESES needing verification."""
    category: str
    severity: str  # INFO | LOW | MEDIUM | HIGH | CRITICAL
    fact: str
    hypotheses: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    code: str = ""

    def to_dict(self) -> dict:
        return {"category": self.category, "severity": self.severity, "fact": self.fact,
                "hypotheses": list(self.hypotheses), "evidence": metrics_to_dict(self.evidence),
                "code": self.code}


SEVERITY_ORDER = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
