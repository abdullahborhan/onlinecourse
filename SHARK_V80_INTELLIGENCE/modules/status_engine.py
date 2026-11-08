"""
Report status determination from explicit, configurable criteria.

TEMPORARILY STOP LIVE TRADING
    * a VERIFIED breach of any check listed in StatusCriteria.critical_checks
    * closed-trade (or equity) drawdown >= StatusCriteria.severe_drawdown_pct
    * an anomaly with a severity in StatusCriteria.stop_anomaly_severities
NEEDS ATTENTION
    * a VERIFIED breach of any StatusCriteria.warning_checks
    * any key check UNVERIFIABLE (incomplete verification)
    * fewer closed trades than AnalysisConfig.min_trades_for_verdict
    * profit factor decline > pf_decline_attention_pct vs previous period
    * anomaly/finding severity in attention_anomaly_severities
    * strategy classification incomplete
STABLE
    * none of the above.

Missing data never produces STOP on its own; it yields NEEDS ATTENTION with
the reason "incomplete verification", never a claim of poor performance.
"""
from __future__ import annotations

from modules.common import (BREACH, SEVERITY_ORDER, STATUS_ATTENTION, STATUS_STABLE,
                            STATUS_STOP, UNVERIFIABLE)


def determine_status(*, checks: list[dict], findings: list[dict], closed_trades: int,
                     dd_pct: float | None, equity_dd_pct: float | None, pf_change_pct,
                     classification_complete: bool, status_cfg, min_trades: int) -> dict:
    stop, attention, info = [], [], []
    for c in checks:
        if c["verdict"] == BREACH and c["check"] in status_cfg.critical_checks:
            stop.append(f"Verified critical breach: {c['check']} - {c['summary']}")
        elif c["verdict"] == BREACH and c["check"] in status_cfg.warning_checks:
            attention.append(f"Verified breach: {c['check']} - {c['summary']}")
        elif c["verdict"] == BREACH:
            attention.append(f"Breach: {c['check']} - {c['summary']}")
        elif c["verdict"] == UNVERIFIABLE:
            attention.append(f"Incomplete verification: {c['check']} cannot be verified with "
                             "the data supplied.")
    for label, v in (("equity", equity_dd_pct), ("closed-trade balance", dd_pct)):
        if v is not None and v >= status_cfg.severe_drawdown_pct:
            stop.append(f"Severe drawdown: {label} drawdown {v:.2f}% ≥ "
                        f"{status_cfg.severe_drawdown_pct}%.")
            break
    for f in findings:
        sev = f.get("severity", "INFO")
        if f.get("category") == "risk":
            continue  # already represented by checks
        if sev in status_cfg.stop_anomaly_severities:
            stop.append(f"Critical finding: {f['fact']}")
        elif sev in status_cfg.attention_anomaly_severities:
            attention.append(f"Finding ({sev}): {f['fact']}")
    if closed_trades < min_trades:
        attention.append(f"Small sample: {closed_trades} closed trades (< {min_trades}); "
                         "statistics are not yet reliable.")
    if pf_change_pct is not None and pf_change_pct <= -status_cfg.pf_decline_attention_pct:
        attention.append(f"Profit factor declined {abs(pf_change_pct):.1f}% vs previous period.")
    if not classification_complete:
        attention.append("Strategy classification incomplete.")
    status = STATUS_STOP if stop else STATUS_ATTENTION if attention else STATUS_STABLE
    if status == STATUS_STABLE:
        info.append("No material risk breaches; all key checks verified; sufficient sample.")
    return {"status": status, "stop_reasons": stop, "attention_reasons": attention,
            "info": info,
            "criteria": {
                "critical_checks": list(status_cfg.critical_checks),
                "warning_checks": list(status_cfg.warning_checks),
                "severe_drawdown_pct": status_cfg.severe_drawdown_pct,
                "pf_decline_attention_pct": status_cfg.pf_decline_attention_pct,
                "min_trades_for_verdict": min_trades,
            }}


def worst_severity(findings: list[dict]) -> str:
    return max((f.get("severity", "INFO") for f in findings),
               key=lambda s: SEVERITY_ORDER.get(s, 0), default="INFO")
