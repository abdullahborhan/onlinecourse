"""
Analysis orchestrator: runs every deterministic engine for one account and one
reporting period and returns a single result dictionary consumed by the
dashboard, the report generator and the AI analyst.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

import numpy as np
import pandas as pd

import config
from modules.anomaly_detector import detect_anomalies
from modules.common import (BREACH, SEVERITY_ORDER, STATUS_DATA_MISSING, UNCLASSIFIED,
                            UNVERIFIABLE, metrics_to_dict)
from modules.comparison_engine import compare, historical_strategy_baseline
from modules.database import Database
from modules.drawdown_engine import compute_drawdowns, drawdown_metrics
from modules.execution_quality import analyze_execution
from modules.layering_analyzer import analyze_layering, assign_baskets
from modules.performance_engine import closed_trades, compute_performance, period_breakdown
from modules.risk_analyzer import analyze_risk, opening_balance
from modules.status_engine import determine_status
from modules.strategy_analyzer import classify_strategies, strategy_comparison
from modules.trade_reconstruction import add_risk_at_entry, point_values, reconstruct_trades


def week_bounds(any_day) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Monday 00:00 to next Monday 00:00 (server time) containing ``any_day``."""
    d = pd.Timestamp(any_day).normalize()
    start = d - pd.Timedelta(days=d.weekday())
    return start, start + pd.Timedelta(days=7)


def month_bounds(any_day) -> tuple[pd.Timestamp, pd.Timestamp]:
    d = pd.Timestamp(any_day)
    start = pd.Timestamp(year=d.year, month=d.month, day=1)
    return start, start + pd.offsets.MonthBegin(1)


def build_trade_frame(db: Database, account_id: str, cfg, end=None) -> dict:
    """Load all data for an account (up to ``end``) and build enriched trades."""
    deals = db.load_deals(account_id, end=end)
    rpos = db.load_report_positions(account_id)
    if end is not None and not rpos.empty:
        rpos = rpos[rpos["open_time"] < pd.Timestamp(end)]
        rpos.loc[rpos["close_time"] >= pd.Timestamp(end), ["close_time", "close_price"]] = None
    equity = db.load_equity(account_id, end=end)
    events = db.load_ea_events(account_id)
    trades, recon = reconstruct_trades(deals, rpos)
    pv = point_values(trades)
    if not trades.empty:
        trades = add_risk_at_entry(trades, pv)
        trades, cls = classify_strategies(trades, events, cfg.analysis.strategy_comment_patterns,
                                          cfg.analysis.magic_strategy_map)
        trades = assign_baskets(trades, cfg.analysis.basket_id_pattern,
                                cfg.analysis.layer_pattern)
    else:
        cls = {"total": 0, "classified": 0, "coverage_pct": None, "complete": False,
               "by_source": {}, "conflicts": []}
    return {"deals": deals, "report_positions": rpos, "equity": equity, "events": events,
            "trades": trades, "reconstruction": recon, "point_values": pv,
            "classification": cls}


def _completeness(deals_p, trades_p, eq_p, ev_p, ea_cfg, cls, recon, bal_basis) -> dict:
    items = []

    def add(name, present, detail, affects):
        items.append({"source": name, "present": bool(present), "detail": detail,
                      "affects_if_missing": affects})
    add("Trade history (deals or positions)", not trades_p.empty,
        f"{len(trades_p)} position(s) with activity in period",
        "All performance metrics")
    add("Deal-level history with position IDs", not deals_p.empty and
        deals_p["position_id"].notna().any(),
        f"{len(deals_p)} deal(s) in period",
        "Partial-close detection, deal-level balance curve")
    add("Account balance level", bal_basis in ("VERIFIED", "DEAL_BALANCE_COLUMN",
                                               "DERIVED", "DERIVED_FROM_INCEPTION"),
        f"basis: {bal_basis}", "Return %, drawdown %, daily/weekly loss %")
    add("Equity history", not eq_p.empty, f"{len(eq_p)} observation(s) in period",
        "Equity drawdown, daily/weekly lock verification (otherwise UNVERIFIABLE)")
    add("EA logs", not ev_p.empty, f"{len(ev_p)} log line(s) in period",
        "Spread, slippage, rejections, signal confirmation, log-based classification")
    add("EA .set configuration", ea_cfg is not None,
        f"origin: {ea_cfg['origin']}" if ea_cfg else "not uploaded",
        "Configuration drift vs approved baseline")
    if not trades_p.empty:
        cc = trades_p["costs_complete"].astype(bool).mean() * 100
        add("Commission/swap/fee", cc == 100, f"{cc:.0f}% of positions complete",
            "Net profit accuracy")
        sl_known = (trades_p["sl_status"] != "UNKNOWN").mean() * 100
        add("Stop-loss data", sl_known == 100, f"{sl_known:.0f}% of positions",
            "Risk per trade, basket risk, SL compliance")
    add("Strategy identification", cls.get("complete", False),
        f"{(cls.get('coverage_pct') or 0):.0f}% classified",
        "Per-strategy statistics")
    missing = [i for i in items if not i["present"]]
    return {"items": items, "missing": missing,
            "score_pct": (len(items) - len(missing)) / len(items) * 100 if items else 0}


def _flat_metrics(perf, ddm, risk_m, strat, layering, exec_m, period_trades) -> dict:
    P, S = perf["profitability"], perf["statistics"]
    flat = {k: v for k, v in {**P, **S}.items()}
    flat.update(ddm)
    flat.update(risk_m)
    flat["transaction_costs"] = exec_m.get("transaction_costs")
    if not period_trades.empty:
        c = period_trades[period_trades["status"] == "CLOSED"]
        if not c.empty and c["volume"].sum() > 0:
            flat["buy_volume_share_pct"] = float(c.loc[c["direction"] == "buy", "volume"].sum()
                                                 / c["volume"].sum() * 100)
    b = layering.get("baskets")
    if isinstance(b, pd.DataFrame) and not b.empty:
        flat["avg_layers_per_basket"] = float(b["layers"].mean())
    flat = metrics_to_dict(flat)
    flat["strategy"] = {}
    tbl = strat.get("table")
    if isinstance(tbl, pd.DataFrame) and not tbl.empty:
        for _, r in tbl.iterrows():
            flat["strategy"][r["strategy"]] = {
                "trades": int(r["trades"]),
                "net_profit": None if pd.isna(r["net_profit"]) else float(r["net_profit"]),
                "profit_factor": None if pd.isna(r["profit_factor"]) or
                np.isinf(r["profit_factor"]) else float(r["profit_factor"]),
                "win_rate": None if pd.isna(r["win_rate_pct"]) else float(r["win_rate_pct"])}
    return flat


ACTIONS = {
    "MISSING_EQUITY": ("Export an equity history (time, balance, equity at ≤15-minute "
                       "intervals) for the period and upload it, so daily/weekly locks and "
                       "equity drawdown can be verified.",
                       "Re-run the report; lock checks change from UNVERIFIABLE to "
                       "COMPLIANT/BREACH."),
    "MISSING_LOGS": ("Upload the EA Experts/Journal logs for the period (keep the original "
                     "YYYYMMDD.log file names).",
                     "Spread, slippage, rejection and signal-confirmation metrics become "
                     "available."),
    "MISSING_SET": ("Upload the EA .set file currently loaded on the chart and confirm the "
                    "parameter mapping on the Data Management page.",
                    "Configuration drift table shows MATCH for every approved parameter."),
    "CLASSIFICATION": ("Ensure the EA writes its strategy tag (BRK/MOM/PB) in the order comment "
                       "and/or upload logs with tickets; alternatively supply a magic-number "
                       "map.", "Strategy classification coverage reaches 100%."),
    "BREACH_MAX_LAYERS": ("Compare the EA's MaxLayers parameter with the approved value (3) and "
                          "review the baskets listed in the layering section.",
                          "No basket exceeds 3 layers in the next report."),
    "BREACH_DAILY_LOSS_LOCK": ("Review EA logs for the listed days to determine why entries "
                               "continued after the daily loss lock level.",
                               "Zero entries after lock in subsequent reports."),
    "BREACH_DAILY_HARD_STOP": ("Suspend live trading until the hard-stop mechanism is verified "
                               "on a demo account.", "Demo replay shows trading halts at -3%."),
    "BREACH_MARTINGALE": ("Check the lot-sizing parameters for any multiplier after losses; "
                          "approved configuration has martingale DISABLED.",
                          "No volume escalation after losses in next report."),
    "BREACH_STOP_LOSS_PRESENCE": ("Investigate why the listed positions had no stop loss at "
                                  "entry (EA setting, broker stop level rejection).",
                                  "100% of positions carry an SL at entry."),
    "BREACH_MAX_BASKET_RISK": ("Recalculate layer lot sizes against the approved 0.50/0.25/0.25% "
                               "allocation and check the lot-step rounding.",
                               "All baskets ≤ 1.00% SL-implied risk."),
    "BREACH_WEEKLY_LOSS_LOCK": ("Suspend live trading and review the week's trades before "
                                "re-enabling.", "Weekly realised loss stays below 5%."),
    "BREACH_MAX_EQUITY_DRAWDOWN": ("Suspend live trading; perform a full review of exposure and "
                                   "strategy performance on demo.",
                                   "Drawdown recovers and no further breach on demo."),
    "BREACH_LOT_ESCALATION": ("Review position sizing for the listed trades.",
                              "Volumes stay within 2x layer median."),
    "SMALL_SAMPLE": ("Continue collecting data before drawing strategy conclusions; avoid "
                     "parameter changes based on this period alone.",
                     f"Reach ≥ {config.AnalysisConfig().min_trades_for_verdict} closed trades."),
    "COSTS_MATERIAL": ("Quantify cost per trade by strategy and test whether wider targets "
                       "or fewer trades improve net expectancy (on demo/backtest).",
                       "Costs fall below the materiality threshold."),
}


def build_actions(checks, findings, completeness, n_trades, cfg) -> list[dict]:
    keys = []
    miss = {m["source"] for m in completeness["missing"]}
    for c in checks:
        if c["verdict"] == BREACH:
            keys.append("BREACH_" + c["check"].upper())
    codes = {f.get("code") for f in findings}
    if "COSTS_MATERIAL" in codes:
        keys.append("COSTS_MATERIAL")
    if "Equity history" in miss:
        keys.append("MISSING_EQUITY")
    if "Strategy identification" in miss:
        keys.append("CLASSIFICATION")
    if "EA logs" in miss:
        keys.append("MISSING_LOGS")
    if "EA .set configuration" in miss:
        keys.append("MISSING_SET")
    if n_trades < cfg.analysis.min_trades_for_verdict:
        keys.append("SMALL_SAMPLE")
    out, seen = [], set()
    for k in keys:
        if k in ACTIONS and k not in seen:
            seen.add(k)
            out.append({"key": k, "action": ACTIONS[k][0], "success_test": ACTIONS[k][1]})
    return out


def major_findings(checks, findings, perf, completeness) -> list[str]:
    ranked = []
    for c in checks:
        if c["verdict"] == BREACH:
            ranked.append((5, f"Risk breach – {c['check']}: {c['summary']}"))
    for f in findings:
        if f.get("category") == "risk":
            continue
        ranked.append((SEVERITY_ORDER.get(f["severity"], 0), f["fact"]))
    unver = [c["check"] for c in checks if c["verdict"] == UNVERIFIABLE]
    if unver:
        ranked.append((2.5, "Verification incomplete for: " + ", ".join(unver)
                       + " (missing data - not evidence of poor performance)."))
    np_ = perf["profitability"].get("net_profit")
    pf = perf["profitability"].get("profit_factor")
    if np_ is not None and np_.available:
        ranked.append((2, f"Net profit for the period: {np_.fmt()} with profit factor "
                          f"{pf.fmt() if pf else 'n/a'} over "
                          f"{perf['statistics']['total_closed_trades'].value} closed trades."))
    ranked.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, text in ranked:
        if text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) == 3:
            break
    return out


def run_analysis(db: Database, account_id: str, start, end, period_type: str = "WEEKLY",
                 cfg=None, compare_with_db: bool = True) -> dict:
    cfg = cfg or config.load_config()
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    period_days = (end - start).total_seconds() / 86400
    base = build_trade_frame(db, account_id, cfg, end=end)
    deals, trades, equity, events = base["deals"], base["trades"], base["equity"], base["events"]

    deals_p = deals[(deals["time"] >= start) & (deals["time"] < end)] if not deals.empty \
        else deals
    eq_p = equity[(equity["time"] >= start) & (equity["time"] < end)] if not equity.empty \
        else equity
    ev_p = events[(events["time"] >= start) & (events["time"] < end)] \
        if not events.empty and events["time"].notna().any() else events.iloc[0:0] \
        if not events.empty else events
    if trades.empty:
        closed_p = activity = trades
    else:
        closed_p = closed_trades(trades, start, end)
        activity = trades[((trades["close_time"] >= start) & (trades["close_time"] < end)) |
                          ((trades["open_time"] >= start) & (trades["open_time"] < end))]
    ea_cfg = db.active_ea_config()

    meta = {"app": config.APP_NAME, "version": config.APP_VERSION, "account_id": account_id,
            "account_info": db.account_info(account_id), "period_type": period_type,
            "period_start": str(start), "period_end": str(end), "period_days": period_days,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "server_timezone_assumption": cfg.server_timezone,
            "report_timezone": cfg.report_timezone, "environment": config.ENVIRONMENT,
            "synthetic": config.IS_SYNTHETIC_ENV,
            "risk_reference": cfg.risk.to_dict()}

    if closed_p.empty and activity.empty and deals_p.empty:
        return {"meta": {**meta, "data_missing": True}, "data_missing": True,
                "status": {"status": STATUS_DATA_MISSING, "stop_reasons": [],
                           "attention_reasons": ["No trading records for this period."],
                           "info": []},
                "coverage": db.data_coverage(account_id)}

    start_balance = opening_balance(deals, start) if not deals.empty else np.nan
    bal_basis = trades["balance_basis"].iloc[0] if not trades.empty else "UNAVAILABLE"

    dd_period = compute_drawdowns(deals, trades, equity, start, end,
                                  cfg.analysis.drawdown_episode_min_pct)
    dd_full = compute_drawdowns(deals, trades, equity, None, end,
                                cfg.analysis.drawdown_episode_min_pct)
    ddm = drawdown_metrics(dd_period)
    perf = compute_performance(
        closed_p, eps=cfg.analysis.breakeven_epsilon,
        start_balance=None if np.isnan(start_balance) else start_balance,
        period_days=period_days,
        max_dd_money=dd_period["balance"].get("max_dd_money")
        if dd_period["balance"].get("status") == "CALCULATED" else None,
        balance_basis=bal_basis)
    layering = analyze_layering(trades, base["point_values"], cfg.risk, events, activity)
    risk = analyze_risk(deals=deals, trades_all=trades, period_trades=activity, equity=equity,
                        start=start, end=end, risk_cfg=cfg.risk, analysis_cfg=cfg.analysis,
                        baskets=layering.get("baskets"), drawdown_full=dd_full,
                        exposure=layering.get("exposure", {}))
    strat = strategy_comparison(activity, cfg.analysis.breakeven_epsilon, dd_period["balance"])
    execq = analyze_execution(activity, events, start, end, cfg.analysis.cost_materiality_pct)

    history = closed_trades(trades, None, start) if not trades.empty else trades
    hist_reports = db.historical_reports(account_id, period_type, start) if compare_with_db \
        else []
    hist_dd = [((r.get("metrics") or {}).get("max_balance_drawdown_pct") or {}).get("value")
               for r in hist_reports]
    cur_dd = dd_period["balance"].get("max_dd_pct")
    anomalies = detect_anomalies(
        closed_p, history, period_days=period_days, max_layers=cfg.risk.max_layers,
        z_thr=cfg.analysis.anomaly_z_threshold, min_hist=cfg.analysis.anomaly_min_history_trades,
        alpha=cfg.analysis.win_rate_alpha, eps=cfg.analysis.breakeven_epsilon,
        current_dd_pct=cur_dd, historical_dd_pcts=hist_dd, exposure=layering.get("exposure"))

    cls = base["classification"]
    if not activity.empty:
        cov = float((activity["strategy"] != UNCLASSIFIED).mean() * 100)
        cls_period = {**cls, "coverage_pct": cov, "complete": cov == 100.0,
                      "total": int(len(activity)),
                      "classified": int((activity["strategy"] != UNCLASSIFIED).sum())}
    else:
        cls_period = cls
    completeness = _completeness(deals_p, activity, eq_p, ev_p, ea_cfg, cls_period,
                                 base["reconstruction"], bal_basis)
    flat = _flat_metrics(perf, ddm, risk["metrics"], strat, layering, execq["metrics"], activity)

    meta.update({
        "equity_available": not eq_p.empty,
        "classification_coverage_pct": cls_period.get("coverage_pct"),
        "costs_complete": bool(closed_p["costs_complete"].astype(bool).all())
        if not closed_p.empty else None,
        "trade_source": ",".join(sorted(activity["source"].unique())) if not activity.empty
        else None,
        "start_balance": None if np.isnan(start_balance) else start_balance,
        "balance_basis": bal_basis,
    })

    comparison = {"available": False, "note": "Comparison not requested."}
    if compare_with_db:
        prev = db.previous_report(account_id, period_type, start)
        comparison = compare(flat, prev["metrics"] if prev else None, meta,
                             (prev or {}).get("report", {}).get("meta"),
                             label="previous week" if period_type == "WEEKLY"
                             else "previous month" if period_type == "MONTHLY"
                             else "previous period")
        comparison["historical_strategy_baseline"] = historical_strategy_baseline(
            [r["metrics"] for r in hist_reports])

    all_findings = []
    for group in (risk["findings"], layering.get("findings", []), strat["findings"],
                  execq["findings"], anomalies["findings"], comparison.get("findings", [])):
        all_findings += [f.to_dict() if hasattr(f, "to_dict") else f for f in group]
    pf_change = None
    for r in comparison.get("rows", []):
        if r["key"] == "profit_factor":
            pf_change = r["pct_change"]
    status = determine_status(
        checks=risk["checks"], findings=all_findings,
        closed_trades=int(len(closed_p)), dd_pct=cur_dd,
        equity_dd_pct=dd_full["equity"].get("max_dd_pct")
        if dd_full["equity"].get("status") == "CALCULATED" else None,
        pf_change_pct=pf_change, classification_complete=cls_period.get("complete", False),
        status_cfg=cfg.status, min_trades=cfg.analysis.min_trades_for_verdict)

    result = {
        "meta": meta, "data_missing": False, "status": status,
        "completeness": completeness, "reconstruction": base["reconstruction"],
        "point_values": base["point_values"], "classification": cls_period,
        "performance": perf, "drawdown": {"period": dd_period, "full": dd_full,
                                          "metrics": ddm},
        "risk": risk, "layering": layering, "strategy": strat, "execution": execq,
        "anomalies": anomalies, "comparison": comparison, "findings": all_findings,
        "major_findings": major_findings(risk["checks"], all_findings, perf, completeness),
        "next_actions": build_actions(risk["checks"], all_findings, completeness,
                                      int(len(closed_p)), cfg),
        "flat_metrics": flat, "ea_config": ea_cfg,
        "daily": period_breakdown(closed_p, "D"),
        "trades_period": activity, "closed_trades_period": closed_p, "trades_all": trades,
        "equity_period": eq_p,
    }
    result["fingerprint"] = fingerprint(result)
    return result


def fingerprint(result: dict) -> str:
    """Hash of the inputs that determine the report (for duplicate detection)."""
    t = result.get("trades_period")
    payload = {"meta": {k: result["meta"].get(k) for k in ("account_id", "period_start",
                                                           "period_end")},
               "trades": t[["position_id", "net_profit", "status"]].astype(str).values.tolist()
               if isinstance(t, pd.DataFrame) and not t.empty else [],
               "eq": int(len(result.get("equity_period", []))),
               "risk": result["meta"].get("risk_reference")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
