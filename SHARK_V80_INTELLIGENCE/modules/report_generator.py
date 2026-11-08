"""
Weekly / monthly performance report generator (Markdown, PDF, CSV, JSON).

Sections (fixed order)
  1 Reporting period                    8 Layering analysis
  2 Data sources and completeness       9 Risk compliance
  3 Executive summary (+ STATUS)       10 Execution quality
  4 Overall performance                11 Anomaly detection
  5 Profitability statistics           12 Comparison with previous period
  6 Drawdown analysis                  13 Three major findings
  7 Strategy performance               14 Recommended next actions

When no trading data exists for the period a DATA MISSING report is produced
instead: it contains no metrics, lists the missing files and asks the user to
upload a fresh MT5 report. SVPI has no broker connection and never claims one.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

import config
from modules.analysis_service import month_bounds, run_analysis, week_bounds
from modules.audit_logger import audit
from modules.common import (STATUS_ATTENTION, STATUS_DATA_MISSING, STATUS_STABLE, STATUS_STOP,
                            format_duration, format_metric, metrics_to_dict)
from modules.database import Database

DISCLAIMER = ("SVPI is an independent, read-only audit tool. It has NO connection to any MT5 "
              "terminal or broker account, does not modify the Expert Advisor and never "
              "executes trades. All figures are computed deterministically from the files "
              "listed under 'Data sources'.")
NO_ACCESS = ("SVPI cannot obtain MT5 reports automatically - it has no broker or terminal "
             "connection. Please export the reports from MT5 and place them in "
             "data/incoming (or upload them on the Data Management page).")

STATUS_COLORS = {STATUS_STABLE: "#16a34a", STATUS_ATTENTION: "#d97706",
                 STATUS_STOP: "#dc2626", STATUS_DATA_MISSING: "#6b7280"}


def _fmt_val(v, digits=2):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if isinstance(v, float):
        if math.isinf(v):
            return "∞"
        return f"{v:,.{digits}f}"
    return str(v)


def _metric_rows(d: dict, labels: dict | None = None) -> list[list[str]]:
    rows = []
    for k, m in d.items():
        if m is None:
            continue
        label = (labels or {}).get(k, k.replace("_", " ").capitalize())
        md = m.to_dict() if hasattr(m, "to_dict") else m
        rows.append([label, format_metric(md), md.get("status", ""),
                     " | ".join(x for x in (md.get("basis"), md.get("note")) if x)])
    return rows


def _df_rows(df: pd.DataFrame, cols: list[str], digits=2) -> list[list[str]]:
    if df is None or df.empty:
        return []
    out = []
    for rec in df[cols].astype(object).to_dict(orient="records"):
        row = []
        for c in cols:
            v = rec[c]
            if hasattr(v, "item"):
                v = v.item()
            row.append(v if isinstance(v, str) else str(v) if isinstance(v, (int, bool))
                       else _fmt_val(v, digits))
        out.append(row)
    return out


def _criteria_text(c: dict) -> str:
    if not c:
        return ""
    return (f"Status rules: STOP if a verified breach of {', '.join(c['critical_checks'])} or "
            f"drawdown ≥ {c['severe_drawdown_pct']}%; NEEDS ATTENTION if a verified breach of "
            f"{', '.join(c['warning_checks'])}, any UNVERIFIABLE check, < "
            f"{c['min_trades_for_verdict']} closed trades, profit-factor decline ≥ "
            f"{c['pf_decline_attention_pct']}% or a HIGH-severity finding; otherwise STABLE.")


# ---------------------------------------------------------------- structure
def build_report_structure(res: dict) -> dict:
    """Ordered list of sections, each with paragraphs and tables (renderer-neutral)."""
    m, st = res["meta"], res["status"]
    perf = res["performance"]
    S = []

    def sec(title, paras=None, tables=None):
        S.append({"title": title, "paras": paras or [], "tables": tables or []})

    title = f"SHARK V80 {m['period_type']} PERFORMANCE REPORT"
    acct = m["account_id"]
    ai = m.get("account_info") or {}
    sec("1. Reporting period", [
        f"Account: {acct}" + (f" ({ai.get('currency')}, {ai.get('server')})"
                              if ai.get("currency") else ""),
        f"Period: {m['period_start']} → {m['period_end']} (end exclusive), "
        f"{m['period_days']:g} days, broker server time (assumed {m['server_timezone_assumption']}).",
        f"Generated: {m['generated_at']} ({m['report_timezone']} schedule).",
    ] + (["*** SYNTHETIC TEST DATA - NOT A REAL PERFORMANCE REPORT ***"] if m.get("synthetic")
         else []))

    comp = res["completeness"]
    sec("2. Data sources and completeness", [
        f"Completeness score: {comp['score_pct']:.0f}% of data sources present. Missing data "
        "limits what can be verified; it is not evidence of poor trading performance.",
        f"Trades reconstructed from: {m.get('trade_source') or 'n/a'}; balance basis: "
        f"{m.get('balance_basis')}; partial-close detection: "
        f"{res['reconstruction'].get('partial_close_detection')}.",
    ], [{"header": ["Data source", "Present", "Detail", "Affects if missing"],
         "rows": [[i["source"], "YES" if i["present"] else "NO", i["detail"],
                   i["affects_if_missing"]] for i in comp["items"]]}])

    P, ST = perf["profitability"], perf["statistics"]
    reasons = st["stop_reasons"] + st["attention_reasons"]
    sec("3. Executive summary", [
        f"STATUS: {st['status']}",
        *(f"• {r}" for r in reasons[:8]),
        *(st.get("info") or []),
        f"Closed trades: {ST['total_closed_trades'].value}; net profit {P['net_profit'].fmt()}; "
        f"profit factor {P['profit_factor'].fmt()}; win rate {ST['win_rate'].fmt()}.",
        _criteria_text(st.get("criteria", {})),
    ])

    dm = res["drawdown"]["metrics"]
    sec("4. Overall performance", [], [{"header": ["Metric", "Value", "Status", "Basis / note"],
                                        "rows": _metric_rows({
                                            "net_profit": P["net_profit"],
                                            "total_return_pct": P["total_return_pct"],
                                            "profit_factor": P["profit_factor"],
                                            "win_rate": ST["win_rate"],
                                            "total_closed_trades": ST["total_closed_trades"],
                                            "max_balance_drawdown_pct":
                                                dm["max_balance_drawdown_pct"],
                                            "max_equity_drawdown_pct":
                                                dm["max_equity_drawdown_pct"],
                                        })}])
    sec("5. Profitability statistics", ["; ".join(perf["notes"])] if perf["notes"] else [],
        [{"header": ["Metric", "Value", "Status", "Basis / note"],
          "rows": _metric_rows(P) + _metric_rows(ST) + _metric_rows(perf.get("costs", {}))}])

    dd = res["drawdown"]["period"]
    eps = dd["balance"].get("episodes_detail", []) if isinstance(dd["balance"], dict) else []
    sec("6. Drawdown analysis", [
        "Balance drawdown is measured on realised (closed-trade) balance with deposits and "
        "withdrawals removed. Equity drawdown is shown only when equity observations were "
        "imported. The closed-trade estimate is a LOWER BOUND and is never presented as actual "
        "equity drawdown.",
        dd["equity"].get("note", ""),
    ], [{"header": ["Metric", "Value", "Status", "Basis / note"], "rows": _metric_rows(dm)}]
        + ([{"header": ["Episode start", "End", "Max %", "Max money"],
             "rows": [[e["start"], e["end"], _fmt_val(e["max_pct"], 3),
                       _fmt_val(e["max_money"])] for e in eps]}] if eps else []))

    s = res["strategy"]
    cls = res["classification"]
    paras = [f"Classification coverage: {_fmt_val(cls.get('coverage_pct'), 0)}% "
             f"(sources: {cls.get('by_source')}).",
             "Strategies are identified only from EA logs, EA comments or a user-supplied "
             "magic-number map - never from profit or direction."]
    if not cls.get("complete"):
        paras.append("CLASSIFICATION INCOMPLETE: per-strategy figures exclude UNCLASSIFIED "
                     "trades.")
    if s.get("most_profit"):
        paras.append(f"Most profit: {s['most_profit']['strategy']} "
                     f"({_fmt_val(s['most_profit']['net_profit'])}).")
    if s.get("most_risk"):
        paras.append(f"Most risk: {s['most_risk']['strategy']} "
                     f"({s['most_risk']['basis']}: {_fmt_val(s['most_risk']['gross_loss'])}).")
    if s.get("most_drawdown_contribution"):
        d = s["most_drawdown_contribution"]
        paras.append(f"Largest drawdown contribution: {d['strategy']} "
                     f"({_fmt_val(d['contribution'])}; {d['basis']}).")
    tbl = s["table"].copy()
    if not tbl.empty:
        tbl["avg_holding"] = tbl["avg_holding_s"].map(
            lambda v: format_duration(v) if pd.notna(v) else "—")
    sec("7. Strategy performance (BREAKOUT vs MOMENTUM vs PULLBACK)", paras,
        [{"header": ["Strategy", "Trades", "Win %", "PF", "Net", "Expectancy", "Avg win",
                     "Avg loss", "DD contrib.", "Avg hold"],
          "rows": _df_rows(tbl, ["strategy", "trades", "win_rate_pct", "profit_factor",
                                 "net_profit", "expectancy", "avg_win", "avg_loss",
                                 "dd_window_contribution", "avg_holding"])}])

    lay = res["layering"]
    lp = []
    if lay.get("available"):
        lp = [f"Basket identification: {lay.get('basket_methods')}; "
              f"{lay.get('inferred_share_pct', 0):.0f}% of positions in INFERRED baskets "
              "(time-overlap grouping, not EA identifiers).",
              f"Max layer count: {lay.get('max_layer_count')}; multi-layer baskets: "
              f"{lay.get('multi_layer_baskets')}; average time between layers: "
              f"{format_duration(lay.get('avg_seconds_between_layers'))}.",
              f"Max simultaneous positions: {lay['exposure'].get('max_positions')} "
              f"(volume {lay['exposure'].get('max_volume')}).",
              f"Signal confirmation check for added layers: {lay.get('confirmation_check')}."]
        if "layer2plus_loss_share_pct" in lay:
            lp.append(f"Layers 2+: {lay['layer2plus_loss_share_pct']:.1f}% of gross loss vs "
                      f"{lay['layer2plus_volume_share_pct']:.1f}% of volume → "
                      f"{'DISPROPORTIONATE' if lay['disproportionate'] else 'proportionate'}.")
    tables = []
    if lay.get("available") and not lay["layers"].empty:
        tables.append({"header": ["Layer", "Trades", "Net", "Win %", "PF", "Profit contrib %",
                                  "Loss share %", "Volume share %", "Risk contrib %"],
                       "rows": _df_rows(lay["layers"], ["layer", "trades", "net_profit",
                                                        "win_rate_pct", "profit_factor",
                                                        "profit_contribution_pct",
                                                        "loss_share_pct", "volume_share_pct",
                                                        "risk_contribution_pct"])})
        bt = lay.get("by_layering_type")
        if bt is not None and not bt.empty:
            tables.append({"header": ["Layering type", "Baskets", "Net", "Win %"],
                           "rows": _df_rows(bt, ["layering_type", "baskets", "net_profit",
                                                 "win_rate_pct"])})
    sec("8. Layering analysis", lp or ["No baskets in period."], tables)

    risk = res["risk"]
    sec("9. Risk compliance", [
        f"Reference configuration: {m['risk_reference'].get('source')} (approved baseline - not "
        "proof of the EA's actual settings).",
        "BREACH = proven by records; UNVERIFIABLE = required data missing (not compliance); "
        "COMPLIANT = verified with sufficient data.",
    ] + ([f"EA .set configuration: {res['ea_config']['origin']}"] if res.get("ea_config") else []),
        [{"header": ["Check", "Verdict", "Summary", "Basis"],
          "rows": [[c["check"], c["verdict"], c["summary"], c["basis"]]
                   for c in risk["checks"]]},
         {"header": ["Risk metric", "Value", "Status", "Basis / note"],
          "rows": _metric_rows(risk["metrics"])}])

    ex = res["execution"]
    sec("10. Execution quality",
        ["Measured only where reference data exists; broker execution quality is not "
         "estimated without it."] + (["Transaction costs are MATERIAL relative to gross "
                                      "profit."] if ex.get("costs_material") else []),
        [{"header": ["Metric", "Value", "Status", "Basis / note"],
          "rows": _metric_rows(ex["metrics"])}])

    an = res["anomalies"]
    an_f = [f for f in res["findings"] if f["category"] == "anomaly"]
    sec("11. Anomaly detection", list(an.get("info", [])) + (
        ["No anomalies detected."] if not an_f else []),
        [{"header": ["Severity", "FACT", "HYPOTHESES (require verification)"],
          "rows": [[f["severity"], f["fact"], " / ".join(f["hypotheses"]) or "—"]
                   for f in an_f]}] if an_f else [])

    cmpd = res["comparison"]
    if cmpd.get("available"):
        rows = [[r["metric"], _fmt_val(r["previous"]), _fmt_val(r["current"]),
                 _fmt_val(r["abs_change"]),
                 _fmt_val(r["pct_change"], 1) + "%" if r["pct_change"] is not None else "—",
                 r["direction"]] for r in cmpd["rows"]]
        cp = [f"Compared with {cmpd['label']}: {cmpd['previous_period']['start']} → "
              f"{cmpd['previous_period']['end']}.",
              f"Profitability trend: {cmpd.get('profitability_trend', 'n/a')}; risk indicators "
              f"increasing: {', '.join(cmpd.get('risk_increasing') or []) or 'none'}."]
        cp += [f"LIMITATION: {x}" for x in cmpd.get("limitations", [])]
        sec(f"12. Comparison with {cmpd['label']}", cp,
            [{"header": ["Metric", "Previous", "Current", "Δ", "Δ %", "Direction"],
              "rows": rows}])
    else:
        sec("12. Comparison with previous period", [cmpd.get("note", "Not available.")])

    sec("13. Three major findings", [f"{i}. {t}" for i, t in
                                     enumerate(res["major_findings"], 1)] or ["None."])
    sec("14. Recommended next actions", [
        f"{i}. {a['action']}  [Test: {a['success_test']}]"
        for i, a in enumerate(res["next_actions"], 1)] + [
        "SVPI never changes the EA, never executes trades and never recommends increasing "
        "lot size."])
    return {"title": title, "status": st["status"], "sections": S, "meta": m,
            "disclaimer": DISCLAIMER}


def build_data_missing_structure(db: Database, account_id: str, start, end,
                                 period_type: str, reason: str = "") -> dict:
    cov = db.data_coverage(account_id) if account_id else {}
    last = (cov.get("deals") or {}).get("last") or (cov.get("report_positions") or {}).get("last")
    missing = [
        f"MT5 Detailed Account History report (HTML) or deal-history CSV covering "
        f"{pd.Timestamp(start).date()} → {(pd.Timestamp(end) - pd.Timedelta(days=1)).date()} "
        "(REQUIRED)",
        "Equity/balance history CSV for the same period (needed to verify daily/weekly locks "
        "and equity drawdown)",
        "EA Experts/Journal logs for the period (strategy classification, execution quality)",
        "EA .set file currently in use (configuration drift check)",
    ]
    S = [
        {"title": "STATUS: DATA MISSING", "paras": [
            "No trading records are available for this reporting period, so NO performance "
            "metrics have been produced. Missing data is not evidence of good or bad trading "
            "performance.", NO_ACCESS], "tables": []},
        {"title": "1. Reporting period", "paras": [
            f"Account: {account_id or 'not yet known'}",
            f"Period: {start} → {end} (end exclusive), broker server time."], "tables": []},
        {"title": "2. Missing files", "paras": [f"• {x}" for x in missing], "tables": []},
        {"title": "3. Data currently held", "paras": [
            f"Latest trading record in the database: {last or 'none'}.",
            f"Coverage: {json.dumps(cov, default=str)}"] + ([reason] if reason else []),
         "tables": []},
        {"title": "4. Required action", "paras": [
            "Upload the latest MT5 report: in MT5 open Toolbox → History, right-click → "
            "Report → HTML (or export deals to CSV), then place the file in data/incoming or "
            "upload it on the Data Management page, and re-run the weekly report."],
         "tables": []},
    ]
    meta = {"account_id": account_id, "period_type": period_type, "period_start": str(start),
            "period_end": str(end), "data_missing": True, "missing_files": missing,
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "synthetic": config.IS_SYNTHETIC_ENV}
    return {"title": f"SHARK V80 {period_type} PERFORMANCE REPORT - DATA MISSING",
            "status": STATUS_DATA_MISSING, "sections": S, "meta": meta,
            "disclaimer": DISCLAIMER}


# ------------------------------------------------------------------ render
def render_markdown(rep: dict) -> str:
    out = [f"# {rep['title']}", "", f"**STATUS: {rep['status']}**", ""]
    if rep["meta"].get("synthetic"):
        out += ["> ⚠ SYNTHETIC TEST DATA — NOT A REAL PERFORMANCE REPORT", ""]
    for s in rep["sections"]:
        out += [f"## {s['title']}", ""]
        out += [p for p in s["paras"] if p] + [""]
        for t in s["tables"]:
            if not t["rows"]:
                continue
            out.append("| " + " | ".join(t["header"]) + " |")
            out.append("|" + "---|" * len(t["header"]))
            for r in t["rows"]:
                out.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ")
                                              for c in r) + " |")
            out.append("")
    out += ["---", f"_{rep['disclaimer']}_", ""]
    return "\n".join(out)


_PDF_REPL = {"≥": ">=", "≤": "<=", "∞": "inf", "→": "->", "χ²": "chi2", "Σ": "sum ", "·": "*",
             "–": "-", "—": "-", "≈": "~", "×": "x", "Δ": "chg", "•": "-", "⚠": "!", "…": "..."}


def pdf_safe(text: str) -> str:
    s = str(text)
    for a, b in _PDF_REPL.items():
        s = s.replace(a, b)
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_pdf(rep: dict, path: Path, curve: pd.DataFrame | None = None) -> Path:
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=8.5, leading=11)
    cell = ParagraphStyle("c", parent=body, fontSize=7, leading=8.5)
    head = ParagraphStyle("hd", parent=cell, textColor=colors.white)
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=16, textColor=colors.HexColor(
        "#0b1220"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11.5,
                        textColor=colors.HexColor("#0e7490"), spaceBefore=8)
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=12 * mm,
                            rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=pdf_safe(rep["title"]), author="SVPI")
    story = [Paragraph(pdf_safe(rep["title"]), h1)]
    col = colors.HexColor(STATUS_COLORS.get(rep["status"], "#6b7280"))
    banner = Table([[Paragraph(f"<b>STATUS: {pdf_safe(rep['status'])}</b>",
                               ParagraphStyle("s", parent=body, textColor=colors.white,
                                              fontSize=12, leading=15))]],
                   colWidths=[doc.width])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), col),
                                ("TOPPADDING", (0, 0), (-1, -1), 6),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [banner, Spacer(1, 4)]
    if rep["meta"].get("synthetic"):
        story.append(Paragraph("<b>SYNTHETIC TEST DATA - NOT A REAL PERFORMANCE REPORT</b>",
                               ParagraphStyle("w", parent=body, textColor=colors.red)))
    if curve is not None and len(curve) >= 2:
        d = Drawing(doc.width, 120)
        lp = LinePlot()
        lp.x, lp.y, lp.width, lp.height = 40, 15, doc.width - 60, 95
        t0 = curve["time"].iloc[0]
        base_pl = float(curve["pl"].iloc[0])
        pts = [((t - t0).total_seconds() / 3600, float(v) - base_pl) for t, v in
               zip(curve["time"], curve["pl"])]
        lp.data = [pts]
        lp.lines[0].strokeColor = colors.HexColor("#0e7490")
        lp.xValueAxis.labelTextFormat = "%dh"
        d.add(lp)
        story += [Paragraph("Realised trading P/L in account currency (deposits/withdrawals "
                            "excluded), hours from period start", body), d]
    for s in rep["sections"]:
        block = [Paragraph(pdf_safe(s["title"]), h2)]
        block += [Paragraph(pdf_safe(p), body) for p in s["paras"] if p]
        for t in s["tables"]:
            if not t["rows"]:
                continue
            data = [[Paragraph(f"<b>{pdf_safe(h)}</b>", head) for h in t["header"]]]
            data += [[Paragraph(pdf_safe(c), cell) for c in r] for r in t["rows"]]
            n = len(t["header"])
            widths = [doc.width / n] * n
            if n == 4 and t["header"][2] == "Summary":
                widths = [doc.width * x for x in (0.15, 0.11, 0.52, 0.22)]
            elif n == 4:
                widths = [doc.width * x for x in (0.22, 0.14, 0.12, 0.52)]
            tbl = Table(data, colWidths=widths, repeatRows=1)
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111a2e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            block += [Spacer(1, 3), tbl]
        story.append(KeepTogether(block[:3]))
        story += block[3:]
    story += [Spacer(1, 8), Paragraph(f"<i>{pdf_safe(rep['disclaimer'])}</i>", body)]
    doc.build(story)
    return path


def metrics_csv(res: dict) -> str:
    rows = []
    flat = res["flat_metrics"]
    for k, v in flat.items():
        if k == "strategy" or not isinstance(v, dict):
            continue
        rows.append({"metric": k, **{x: v.get(x) for x in ("value", "status", "unit", "basis",
                                                           "note")}})
    for s, v in flat.get("strategy", {}).items():
        for mk, mv in v.items():
            rows.append({"metric": f"strategy.{s}.{mk}", "value": mv, "status": "CALCULATED"})
    for c in res["risk"]["checks"]:
        rows.append({"metric": f"check.{c['check']}", "value": c["verdict"], "status": "CHECK",
                     "basis": c["basis"], "note": c["summary"]})
    rows.append({"metric": "report.status", "value": res["status"]["status"]})
    return pd.DataFrame(rows).to_csv(index=False)


# ------------------------------------------------------------------ public
def _out_dir(period_type: str, account_id: str) -> Path:
    base = config.WEEKLY_REPORTS_DIR if period_type == "WEEKLY" else config.MONTHLY_REPORTS_DIR
    d = base / str(account_id or "UNKNOWN")
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_report(db: Database, account_id: str, start, end, period_type: str = "WEEKLY",
                    cfg=None, force: bool = False, pdf: bool = True,
                    with_ai: bool = False) -> dict:
    """Run analysis and write the report files. Returns a summary dict."""
    cfg = cfg or config.load_config()
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    res = run_analysis(db, account_id, start, end, period_type, cfg)
    tag = "SYNTHETIC_" if config.IS_SYNTHETIC_ENV else ""
    stem = f"{tag}SVPI_{period_type}_{account_id}_{start:%Y%m%d}_{end:%Y%m%d}"
    out = _out_dir(period_type, account_id)

    if res.get("data_missing"):
        return generate_data_missing_report(db, account_id, start, end, period_type,
                                            reason="No trading records found in the database "
                                                   "for this period.")

    existing = db.find_report(account_id, period_type, start, end, res["fingerprint"])
    if existing and not force:
        audit("REPORT_DUPLICATE_SKIPPED", {"report_id": existing["id"]}, db=db)
        return {"report_id": existing["id"], "status": existing["status"], "duplicate": True,
                "md_path": existing["md_path"], "pdf_path": existing["pdf_path"],
                "csv_path": existing["csv_path"], "result": res}

    rep = build_report_structure(res)
    if with_ai:
        from modules.ai_analyst import run_ai_analysis
        ai = run_ai_analysis(res, cfg)
        rep["sections"].append({"title": "Appendix: AI analyst interpretation (non-binding)",
                                "paras": ai["paras"], "tables": []})
    md_path = out / f"{stem}.md"
    md_path.write_text(render_markdown(rep), encoding="utf-8")
    csv_path = out / f"{stem}_metrics.csv"
    csv_path.write_text(metrics_csv(res), encoding="utf-8")
    trades = res["trades_period"]
    if not trades.empty:
        trades.drop(columns=[c for c in ("raw_json",) if c in trades]).to_csv(
            out / f"{stem}_trades.csv", index=False)
    json_path = out / f"{stem}.json"
    json_path.write_text(json.dumps({"report": rep, "metrics": res["flat_metrics"]},
                                    default=str, indent=1), encoding="utf-8")
    pdf_path = None
    if pdf:
        curve = res["drawdown"]["period"].get("curve")
        pdf_path = render_pdf(rep, out / f"{stem}.pdf", curve)
    report_id = db.save_report(
        account_id=account_id, period_type=period_type, period_start=start, period_end=end,
        status=res["status"]["status"], is_data_missing=False, fingerprint=res["fingerprint"],
        metrics=res["flat_metrics"],
        report={"meta": res["meta"], "status": res["status"],
                "sections": metrics_to_dict(rep["sections"]),
                "major_findings": res["major_findings"], "next_actions": res["next_actions"]},
        md_path=md_path, pdf_path=pdf_path, csv_path=csv_path)
    audit("REPORT_GENERATED", {"report_id": report_id, "account": account_id,
                               "period": [str(start), str(end)], "status":
                                   res["status"]["status"]}, db=db)
    return {"report_id": report_id, "status": res["status"]["status"], "duplicate": False,
            "md_path": str(md_path), "pdf_path": str(pdf_path) if pdf_path else None,
            "csv_path": str(csv_path), "result": res}


def generate_data_missing_report(db: Database, account_id: str | None, start, end,
                                 period_type: str = "WEEKLY", reason: str = "",
                                 pdf: bool = True) -> dict:
    rep = build_data_missing_structure(db, account_id, start, end, period_type, reason)
    out = _out_dir(period_type, account_id or "UNKNOWN")
    tag = "SYNTHETIC_" if config.IS_SYNTHETIC_ENV else ""
    stem = (f"{tag}SVPI_{period_type}_{account_id or 'UNKNOWN'}_{pd.Timestamp(start):%Y%m%d}_"
            f"{pd.Timestamp(end):%Y%m%d}_DATA_MISSING")
    md_path = out / f"{stem}.md"
    md_path.write_text(render_markdown(rep), encoding="utf-8")
    pdf_path = render_pdf(rep, out / f"{stem}.pdf") if pdf else None
    report_id = db.save_report(
        account_id=account_id or "UNKNOWN", period_type=period_type, period_start=start,
        period_end=end, status=STATUS_DATA_MISSING, is_data_missing=True, fingerprint="",
        metrics={}, report={"meta": rep["meta"], "sections": rep["sections"]},
        md_path=md_path, pdf_path=pdf_path)
    audit("DATA_MISSING_REPORT", {"report_id": report_id, "account": account_id,
                                  "missing": rep["meta"]["missing_files"]}, db=db)
    return {"report_id": report_id, "status": STATUS_DATA_MISSING, "duplicate": False,
            "md_path": str(md_path), "pdf_path": str(pdf_path) if pdf_path else None,
            "missing_files": rep["meta"]["missing_files"], "result": None}


def last_complete_week(now=None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Most recent full Monday-Sunday week ending on or before ``now``.

    The weekly job runs Sunday 20:00 Malaysia time; the week containing that
    Sunday is reported (it is complete apart from the last few hours, which is
    stated in the report period)."""
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now()
    start, end = week_bounds(now)
    if now.weekday() == 6:  # Sunday run reports the current week
        return start, end
    return start - pd.Timedelta(days=7), start


__all__ = ["generate_report", "generate_data_missing_report", "build_report_structure",
           "render_markdown", "render_pdf", "last_complete_week", "month_bounds", "week_bounds"]
