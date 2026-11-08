"""
Automated weekly monitoring workflow (run by Windows Task Scheduler every
Sunday 20:00 Asia/Kuala_Lumpur, or manually).

1.  Back up the database.
2.  Check data/incoming and detect new files.
3.  Validate and import valid files (rejected files stay in incoming).
4.  Identify the reporting period (latest complete week, server time).
5.  For each account: if records exist for the period -> calculate metrics,
    compare with previous reports, generate the weekly report; if not ->
    generate a DATA MISSING report naming the missing files.
6.  Back-fill weeks that have data but no report yet (up to 8 weeks).
7.  Optionally generate the monthly report for the last completed month.
8.  Record audit logs and send an (optional) upload-request notification.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import config
from modules.analysis_service import month_bounds, week_bounds
from modules.audit_logger import audit, get_logger
from modules.database import Database
from modules.mt5_importer import REJECTED, import_incoming_directory
from modules.notifications import send_notification
from modules.report_generator import (generate_data_missing_report, generate_report,
                                      last_complete_week)

log = get_logger("workflow")


def server_now(cfg, now=None) -> pd.Timestamp:
    """Current time expressed in broker server time (naive)."""
    if now is not None:
        return pd.Timestamp(now)
    return pd.Timestamp(datetime.now(ZoneInfo(cfg.server_timezone)).replace(tzinfo=None))


def run_weekly(db: Database | None = None, cfg=None, now=None, account: str | None = None,
               do_import: bool = True, force: bool = False, monthly: bool = True,
               with_ai: bool = False, pdf: bool = True) -> dict:
    cfg = cfg or config.load_config()
    config.ensure_directories()
    db = db or Database()
    summary: dict = {"started": datetime.now().isoformat(timespec="seconds"),
                     "imports": [], "reports": [], "data_missing": [], "notifications": []}
    try:
        summary["backup"] = str(db.backup())
    except Exception as exc:  # noqa: BLE001
        summary["backup_error"] = str(exc)

    if do_import:
        results = import_incoming_directory(db, cfg, account_id=account)
        summary["imports"] = [{"file": r.filename, "kind": r.kind, "status": r.status,
                               "account": r.account_id, "records": r.records,
                               "errors": r.errors[:3]} for r in results]
    new_data = any(i["status"] not in (REJECTED, "DUPLICATE_FILE") for i in summary["imports"])
    rejected = [i for i in summary["imports"] if i["status"] == REJECTED]

    now_s = server_now(cfg, now)
    start, end = last_complete_week(now_s)
    summary["reporting_period"] = [str(start), str(end)]
    accounts = [account] if account else [a for a in db.accounts() if a != "UNSPECIFIED"] or \
        db.accounts()

    if not accounts:
        r = generate_data_missing_report(db, None, start, end, "WEEKLY",
                                         reason="The database contains no trading records.",
                                         pdf=pdf)
        summary["data_missing"].append(r["md_path"])
        accounts = []

    for acct in accounts:
        cov = db.data_coverage(acct)
        last = max(filter(None, [cov["deals"]["last"], cov["report_positions"]["last"]]),
                   default=None)
        last_ts = pd.Timestamp(last) if last else None
        # Back-fill earlier weeks with data but no report
        if last_ts is not None:
            wk_start = week_bounds(last_ts)[0]
            for i in range(8, 0, -1):
                bs = start - pd.Timedelta(days=7 * i)
                be = bs + pd.Timedelta(days=7)
                if be <= start and bs <= wk_start and bs >= week_bounds(
                        cov["deals"]["first"] or cov["report_positions"]["first"] or bs)[0]:
                    if db.find_report(acct, "WEEKLY", bs, be) is None:
                        r = generate_report(db, acct, bs, be, "WEEKLY", cfg, pdf=pdf)
                        if r["status"] != "DATA MISSING":
                            summary["reports"].append({"account": acct, "period": [str(bs),
                                                                                  str(be)],
                                                       "status": r["status"], "backfill": True,
                                                       "md": r["md_path"]})
        if last_ts is None or last_ts < start:
            reason = (f"Latest trading record for account {acct}: {last or 'none'}. "
                      + ("No new files were imported in this run." if not new_data else
                         "Files imported in this run do not cover the reporting period."))
            if rejected:
                reason += f" {len(rejected)} file(s) were REJECTED - see import log."
            r = generate_data_missing_report(db, acct, start, end, "WEEKLY", reason, pdf=pdf)
            summary["data_missing"].append(r["md_path"])
            summary["notifications"].append(send_notification(
                cfg, f"SVPI: fresh MT5 report required for account {acct}",
                f"No trading data for {start.date()} to {(end - pd.Timedelta(days=1)).date()}.\n"
                + "Missing files:\n- " + "\n- ".join(r["missing_files"]) + "\n\n"
                "SVPI cannot obtain MT5 reports automatically. Please export them from MT5 and "
                "place them in data/incoming."))
        else:
            r = generate_report(db, acct, start, end, "WEEKLY", cfg, force=force, pdf=pdf,
                                with_ai=with_ai)
            summary["reports"].append({"account": acct, "period": [str(start), str(end)],
                                       "status": r["status"], "duplicate": r["duplicate"],
                                       "md": r["md_path"], "pdf": r["pdf_path"],
                                       "data_through": str(last_ts)})
        if monthly:
            ms, me = month_bounds(start)
            prev_ms, prev_me = month_bounds(ms - pd.Timedelta(days=1))
            if last_ts is not None and last_ts >= prev_ms and \
                    db.find_report(acct, "MONTHLY", prev_ms, prev_me) is None and \
                    now_s >= prev_me:
                r = generate_report(db, acct, prev_ms, prev_me, "MONTHLY", cfg, pdf=pdf)
                summary["reports"].append({"account": acct, "period": [str(prev_ms),
                                                                      str(prev_me)],
                                           "status": r["status"], "type": "MONTHLY",
                                           "md": r["md_path"]})
    summary["finished"] = datetime.now().isoformat(timespec="seconds")
    audit("WEEKLY_WORKFLOW", summary, db=db)
    try:
        (config.LOG_DIR / f"weekly_run_{datetime.now():%Y%m%d_%H%M%S}.json").write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8")
    except OSError:  # pragma: no cover
        pass
    return summary
