"""
AI quantitative analyst layer (interpretation only).

Guarantees
----------
* Every number comes from deterministic Python engines. The AI receives an
  aggregated FACT SHEET (metrics, check verdicts, findings) - never raw
  statements, trade lists, account numbers or credentials.
* The external model is used only if (a) external AI is enabled in settings,
  (b) the caller passes explicit consent for this call, and (c) the optional
  ``anthropic`` package and credentials are available. Otherwise a fully
  offline, rule-based analyst produces the interpretation.
* Output guardrails: AI text that contains numbers not present in the fact
  sheet, recommends increasing lot size/risk, or claims MT5/broker access or
  configuration changes is REJECTED and replaced by the offline analysis.
  The AI can never override calculated metrics.
"""
from __future__ import annotations

import json
import re

from modules.audit_logger import audit, get_logger
from modules.common import metrics_to_dict

log = get_logger("ai")

SYSTEM_PROMPT = """You are the quantitative analyst inside SHARK V80 Performance Intelligence, an \
independent, read-only audit tool for an MT5 Expert Advisor trading Deriv Volatility 80.

You receive a JSON fact sheet produced by deterministic Python code. Interpret it for the \
account owner:
- Explain the main performance changes and compare BREAKOUT, MOMENTUM and PULLBACK.
- Explain risk concentration, compliance verdicts and anomalies.
- Suggest practical improvements that can be tested on a demo account or backtest.
- Name the data that is missing and what it would allow to verify.

Rules:
- Use only numbers that appear in the fact sheet, copied exactly. Do not compute new figures.
- Label every statement as FACT (directly supported by the fact sheet) or HYPOTHESIS \
(a possible explanation that needs verification).
- UNVERIFIABLE means data is missing; never describe it as compliant or as a breach.
- Never recommend increasing lot size, volume or risk.
- You have no access to MT5, the broker account or the EA; never imply otherwise, never \
propose changing the EA configuration yourself, and never suggest executing trades.
- Write plain text paragraphs, each starting with "FACT:", "HYPOTHESIS:", "MISSING DATA:" \
or "TEST:"."""

FORBIDDEN = [
    (re.compile(r"\b(increase|raise|double|scale up|boost)\b[^.]{0,40}\b(lot|lots|volume|"
                r"position size|risk)\b", re.I), "recommends increasing lot size/risk"),
    (re.compile(r"\b(I|we) (have )?(accessed|connected to|logged into|checked) (the )?"
                r"(MT5|broker|account|terminal)", re.I), "claims MT5/broker access"),
    (re.compile(r"\b(I|we) (have )?(changed|modified|updated|set) (the )?(EA|expert advisor|"
                r"\.set|configuration)", re.I), "claims to change EA configuration"),
    (re.compile(r"\b(I|we) (have )?(placed|opened|closed|executed) (a |the )?(trade|order|"
                r"position)", re.I), "claims to execute trades"),
]
_NUM = re.compile(r"(?<![A-Za-z0-9_])-?\d[\d,]*\.?\d*")


# ------------------------------------------------------------------ facts
def build_fact_sheet(res: dict) -> dict:
    """Aggregated, anonymised facts only (no trades, no account id)."""
    flat = res.get("flat_metrics", {})
    metrics = {k: {"value": v.get("value"), "status": v.get("status"), "unit": v.get("unit")}
               for k, v in flat.items() if isinstance(v, dict) and "status" in v}
    return {
        "period": {"type": res["meta"]["period_type"], "days": res["meta"]["period_days"]},
        "status": res["status"]["status"],
        "status_reasons": res["status"]["stop_reasons"] + res["status"]["attention_reasons"],
        "metrics": metrics,
        "strategy": flat.get("strategy", {}),
        "classification_coverage_pct": res.get("classification", {}).get("coverage_pct"),
        "risk_checks": [{"check": c["check"], "verdict": c["verdict"], "summary": c["summary"]}
                        for c in res["risk"]["checks"]],
        "findings": [{"severity": f["severity"], "category": f["category"], "fact": f["fact"],
                      "hypotheses": f["hypotheses"]} for f in res.get("findings", [])],
        "comparison": [{"metric": r["metric"], "previous": r["previous"],
                        "current": r["current"], "direction": r["direction"]}
                       for r in res.get("comparison", {}).get("rows", [])],
        "comparison_limitations": res.get("comparison", {}).get("limitations", []),
        "missing_data": [m["source"] for m in res.get("completeness", {}).get("missing", [])],
        "approved_risk_reference": res["meta"].get("risk_reference"),
    }


def _fact_numbers(sheet: dict) -> set[float]:
    text = json.dumps(metrics_to_dict(sheet), default=str)
    out = set()
    for m in _NUM.findall(text):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def validate_ai_text(text: str, sheet: dict) -> dict:
    """Check AI output against guardrails. Returns {'ok', 'violations', 'unverified_numbers'}."""
    allowed = _fact_numbers(sheet)
    unverified = []
    for m in _NUM.findall(text):
        s = m.replace(",", "")
        try:
            v = float(s)
        except ValueError:
            continue
        if abs(v) <= 3 and float(v).is_integer():
            continue  # list numbering / "three findings" / layer 1-3
        if not any(abs(round(v, 2) - a) <= 0.01 or abs(round(v, 1) - round(a, 1)) < 1e-9
                   for a in allowed):
            unverified.append(m)
    violations = [msg for rx, msg in FORBIDDEN if rx.search(text)]
    if unverified:
        violations.append(f"numbers not present in the fact sheet: {sorted(set(unverified))[:10]}")
    return {"ok": not violations, "violations": violations,
            "unverified_numbers": sorted(set(unverified))}


# --------------------------------------------------------------- offline
def offline_analysis(sheet: dict) -> list[str]:
    """Deterministic interpretation that never needs a network connection."""
    paras = []
    m = sheet["metrics"]

    def v(k):
        x = m.get(k, {})
        return None if x.get("status") == "UNAVAILABLE" else x.get("value")
    paras.append(f"FACT: Report status is {sheet['status']}.")
    if v("net_profit") is not None:
        paras.append(f"FACT: Net profit {v('net_profit'):.2f} from "
                     f"{v('total_closed_trades')} closed trades; profit factor "
                     f"{m.get('profit_factor', {}).get('value')}; win rate "
                     f"{v('win_rate'):.2f}%.")
    strat = {s: d for s, d in sheet.get("strategy", {}).items() if d.get("trades")}
    known = {s: d for s, d in strat.items() if s != "UNCLASSIFIED" and
             d.get("net_profit") is not None}
    if known:
        best = max(known, key=lambda s: known[s]["net_profit"])
        worst = min(known, key=lambda s: known[s]["net_profit"])
        paras.append(f"FACT: {best} contributed the highest net profit "
                     f"({known[best]['net_profit']:.2f}); {worst} the lowest "
                     f"({known[worst]['net_profit']:.2f}).")
        if known[worst]["net_profit"] < 0:
            paras.append(f"HYPOTHESIS: {worst} entries may be poorly suited to recent market "
                         "conditions; verify on the trade list and EA logs before acting.")
    for c in sheet["risk_checks"]:
        if c["verdict"] == "BREACH":
            paras.append(f"FACT: Risk check {c['check']} is a verified BREACH - {c['summary']}")
    unv = [c["check"] for c in sheet["risk_checks"] if c["verdict"] == "UNVERIFIABLE"]
    if unv:
        paras.append("MISSING DATA: " + ", ".join(unv) + " cannot be verified with the "
                     "uploaded data; this is not evidence of either compliance or breach.")
    for f in sheet["findings"]:
        if f["severity"] in ("HIGH", "CRITICAL") and f["category"] != "risk":
            paras.append(f"FACT: {f['fact']}")
            for h in f["hypotheses"][:2]:
                paras.append(f"HYPOTHESIS: {h}")
    dec = [r for r in sheet["comparison"] if r["direction"] == "DECLINING"]
    if dec:
        paras.append("FACT: Declining vs previous period: " +
                     ", ".join(r["metric"] for r in dec) + ".")
    for x in sheet.get("missing_data", []):
        paras.append(f"MISSING DATA: {x}.")
    paras.append("TEST: Validate any parameter idea on a demo account or backtest over at "
                 "least the same number of trades before considering it for live trading; "
                 "lot size should not be increased on the basis of this report.")
    return paras


# -------------------------------------------------------------- external
def _call_claude(sheet: dict, cfg, client=None) -> str:
    """Single Messages API call. ``client`` may be injected (tests)."""
    if client is None:
        import anthropic  # optional dependency (requirements-ai.txt)
        client = anthropic.Anthropic()
    kwargs = dict(
        model=cfg.ai.model,
        max_tokens=cfg.ai.max_tokens,
        system=SYSTEM_PROMPT,
        output_config={"effort": cfg.ai.effort},
        messages=[{"role": "user", "content":
                   "Fact sheet (JSON):\n" + json.dumps(metrics_to_dict(sheet), default=str)}],
    )
    if cfg.ai.use_refusal_fallback:
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        response = client.messages.create(**kwargs)
    if response.stop_reason == "refusal":
        raise RuntimeError("AI model declined the request (refusal).")
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    if response.stop_reason == "max_tokens":
        log.warning("AI output truncated at max_tokens.")
    return text


def run_ai_analysis(res: dict, cfg, consent: bool = False, client=None) -> dict:
    """Return {'source', 'paras', 'validation', 'note'} - never raises."""
    sheet = build_fact_sheet(res)
    offline = offline_analysis(sheet)
    if not (cfg.ai.external_ai_enabled and consent):
        return {"source": "OFFLINE_RULE_BASED", "paras": offline, "validation": None,
                "note": "External AI disabled or not consented for this request; offline "
                        "deterministic interpretation shown. No data left this computer."}
    try:
        text = _call_claude(sheet, cfg, client)
    except Exception as exc:  # noqa: BLE001 - fall back, never break reporting
        log.warning("External AI unavailable: %s", exc)
        return {"source": "OFFLINE_RULE_BASED", "paras": offline, "validation": None,
                "note": f"External AI unavailable ({type(exc).__name__}: {exc}); offline "
                        "interpretation shown."}
    audit("AI_ANALYSIS_EXTERNAL", {"model": cfg.ai.model, "fields_sent": list(sheet)})
    val = validate_ai_text(text, sheet)
    if not val["ok"]:
        return {"source": "OFFLINE_RULE_BASED", "paras": offline, "validation": val,
                "rejected_ai_text": text,
                "note": "AI output REJECTED by guardrails (" + "; ".join(val["violations"])
                        + "); offline interpretation shown instead."}
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    return {"source": f"EXTERNAL_AI ({cfg.ai.model})", "paras": paras, "validation": val,
            "note": "AI interpretation of deterministic metrics; it does not override them."}
