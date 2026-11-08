"""
MT5 Expert Advisor .set file parser and configuration-drift audit.

A .set file is "Name=Value" (optionally "Name=Value||start||step||stop||Y").
Parameter names are EA-specific and unknown to SVPI, so they are mapped to
risk concepts with configurable patterns. Each mapping is reported with the
source key so the user can confirm or correct it; nothing is silently assumed.
"""
from __future__ import annotations

import re

from config import RiskConfig

# canonical field -> (include regex, exclude regex)
PARAM_PATTERNS = {
    "max_layers": (r"max.*(layer|position|trade|order)s?$|^(layers|maxlayers)$", r"risk|lot"),
    "layer1_risk_pct": (r"(layer|lyr|l)[_ ]?1.*risk|risk.*(layer|lyr|l)[_ ]?1", r""),
    "layer2_risk_pct": (r"(layer|lyr|l)[_ ]?2.*risk|risk.*(layer|lyr|l)[_ ]?2", r""),
    "layer3_risk_pct": (r"(layer|lyr|l)[_ ]?3.*risk|risk.*(layer|lyr|l)[_ ]?3", r""),
    "max_basket_risk_pct": (r"(basket|total|max).*risk", r"layer|lyr|daily|weekly"),
    "daily_hard_stop_pct": (r"hard.*stop|daily.*hard", r""),
    "daily_loss_lock_pct": (r"daily.*(loss|dd|drawdown)", r"hard"),
    "weekly_loss_lock_pct": (r"weekly.*(loss|dd|drawdown)", r""),
    "max_equity_drawdown_pct": (r"(max|equity|account).*(dd|drawdown)", r"daily|weekly"),
    "daily_profit_target_pct": (r"daily.*(profit|target|tp)", r""),
    "martingale_enabled": (r"martingale|lot.?multiplier|use.?mart", r""),
    "risk_profile": (r"risk.?(profile|mode|level)", r""),
}


def parse_set_file(text: str) -> tuple[dict, dict]:
    """Return (raw_params, info)."""
    raw: dict[str, str] = {}
    info = {"lines": 0, "parameters": 0, "issues": []}
    for line in text.splitlines():
        info["lines"] += 1
        line = line.strip()
        if not line or line.startswith(";") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split("||")[0].strip()
        raw[k.strip()] = v
    info["parameters"] = len(raw)
    if not raw:
        info["issues"].append("No Name=Value parameters found; not a valid .set file.")
    return raw, info


def _to_value(field: str, v: str):
    s = str(v).strip().lower()
    if field == "martingale_enabled":
        return s in {"true", "1", "yes", "y", "on"}
    if field == "risk_profile":
        return str(v).strip().upper()
    try:
        x = float(s)
    except ValueError:
        return None
    if field == "max_layers":
        return int(x)
    return x


def map_set_params(raw: dict) -> dict:
    """Map raw EA parameters onto SVPI risk fields.

    Returns {field: {"value", "source_key", "ambiguous_keys"}} only for fields
    with a match. Ambiguous matches (several keys) are reported, not resolved.
    """
    mapped: dict = {}
    for field, (inc, exc) in PARAM_PATTERNS.items():
        keys = [k for k in raw if re.search(inc, k, re.I) and not (exc and re.search(exc, k, re.I))]
        if not keys:
            continue
        values = [(k, _to_value(field, raw[k])) for k in keys]
        values = [(k, v) for k, v in values if v is not None]
        if not values:
            continue
        mapped[field] = {"value": values[0][1], "source_key": values[0][0],
                         "ambiguous_keys": [k for k, _ in values[1:]]}
    return mapped


def compare_to_baseline(mapped: dict, baseline: RiskConfig) -> list[dict]:
    """Configuration drift between the EA's actual .set values and the approved baseline."""
    expected = {
        "max_layers": baseline.max_layers,
        "layer1_risk_pct": baseline.layer_risk_pct[0],
        "layer2_risk_pct": baseline.layer_risk_pct[1],
        "layer3_risk_pct": baseline.layer_risk_pct[2],
        "max_basket_risk_pct": baseline.max_basket_risk_pct,
        "daily_loss_lock_pct": baseline.daily_loss_lock_pct,
        "daily_hard_stop_pct": baseline.daily_hard_stop_pct,
        "weekly_loss_lock_pct": baseline.weekly_loss_lock_pct,
        "max_equity_drawdown_pct": baseline.max_equity_drawdown_pct,
        "daily_profit_target_pct": baseline.daily_profit_target_pct,
        "martingale_enabled": baseline.martingale_enabled,
        "risk_profile": baseline.risk_profile,
    }
    looser_if_greater = {"max_layers", "layer1_risk_pct", "layer2_risk_pct", "layer3_risk_pct",
                         "max_basket_risk_pct", "daily_loss_lock_pct", "daily_hard_stop_pct",
                         "weekly_loss_lock_pct", "max_equity_drawdown_pct"}
    out = []
    for field, exp in expected.items():
        if field not in mapped:
            out.append({"parameter": field, "approved": exp, "actual": None,
                        "source_key": None, "verdict": "NOT_FOUND_IN_SET_FILE"})
            continue
        act = mapped[field]["value"]
        if isinstance(exp, (int, float)) and not isinstance(exp, bool):
            if abs(float(act) - float(exp)) < 1e-9:
                verdict = "MATCH"
            elif field in looser_if_greater and float(act) > float(exp):
                verdict = "LOOSER_THAN_APPROVED"
            else:
                verdict = "DIFFERENT"
        else:
            verdict = "MATCH" if act == exp else (
                "LOOSER_THAN_APPROVED" if field == "martingale_enabled" and act else "DIFFERENT")
        out.append({"parameter": field, "approved": exp, "actual": act,
                    "source_key": mapped[field]["source_key"],
                    "ambiguous_keys": mapped[field]["ambiguous_keys"], "verdict": verdict})
    return out
