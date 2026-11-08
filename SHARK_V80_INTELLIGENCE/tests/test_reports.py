"""Phase 8/9 - report generation and weekly workflow (synthetic data only)."""
from pathlib import Path

import pandas as pd

import config
from modules.report_generator import generate_report, last_complete_week
from modules.mt5_importer import MT5Importer
from modules.weekly_workflow import run_weekly
from tests import synthetic_data as syn

SECTIONS = ["1. Reporting period", "2. Data sources and completeness", "3. Executive summary",
            "4. Overall performance", "5. Profitability statistics", "6. Drawdown analysis",
            "7. Strategy performance", "8. Layering analysis", "9. Risk compliance",
            "10. Execution quality", "11. Anomaly detection", "12. Comparison with",
            "13. Three major findings", "14. Recommended next actions"]


def _load(db):
    MT5Importer(db).import_bytes(syn.deals_csv().encode(), "d_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)


def test_weekly_report_files_and_sections(db):
    _load(db)
    r = generate_report(db, syn.ACCOUNT, "2026-01-05", "2026-01-12")
    md = Path(r["md_path"]).read_text(encoding="utf-8")
    for s in SECTIONS:
        assert s in md, s
    assert "STATUS: NEEDS ATTENTION" in md
    assert "SYNTHETIC TEST DATA" in md
    assert Path(r["pdf_path"]).read_bytes()[:4] == b"%PDF"
    csv = pd.read_csv(r["csv_path"])
    assert csv.loc[csv["metric"] == "net_profit", "value"].astype(float).iloc[0] == 24.75
    assert "SYNTHETIC_" in Path(r["md_path"]).name
    # Duplicate generation with unchanged inputs is skipped
    r2 = generate_report(db, syn.ACCOUNT, "2026-01-05", "2026-01-12")
    assert r2["duplicate"] and r2["report_id"] == r["report_id"]
    # Forced regeneration supersedes the previous version
    r3 = generate_report(db, syn.ACCOUNT, "2026-01-05", "2026-01-12", force=True)
    assert r3["report_id"] != r["report_id"]
    assert len(db.list_reports(syn.ACCOUNT)) == 1


def test_second_week_compares_with_first(db):
    _load(db)
    generate_report(db, syn.ACCOUNT, "2026-01-05", "2026-01-12", pdf=False)
    r = generate_report(db, syn.ACCOUNT, "2026-01-12", "2026-01-19", pdf=False)
    md = Path(r["md_path"]).read_text(encoding="utf-8")
    assert "12. Comparison with previous week" in md
    assert "| Net profit | 24.75 | 1.25 | -23.50 |" in md


def test_data_missing_report_has_no_metrics(db):
    _load(db)
    r = generate_report(db, syn.ACCOUNT, "2026-03-02", "2026-03-09")
    assert r["status"] == "DATA MISSING"
    md = Path(r["md_path"]).read_text(encoding="utf-8")
    assert "NO performance metrics" in md
    assert "MT5 Detailed Account History" in md
    assert "cannot obtain MT5 reports automatically" in md
    assert "Net profit" not in md and "Profit factor" not in md
    assert "2026-01-13" in md  # latest record held is stated


def test_last_complete_week():
    s, e = last_complete_week("2026-01-11 12:00")  # Sunday run
    assert (str(s), str(e)) == ("2026-01-05 00:00:00", "2026-01-12 00:00:00")
    s, e = last_complete_week("2026-01-14 09:00")  # Wednesday manual run
    assert (str(s), str(e)) == ("2026-01-05 00:00:00", "2026-01-12 00:00:00")


def test_workflow_without_new_data_requests_upload(db):
    summary = run_weekly(db=db, now="2026-01-11 12:00", pdf=False, monthly=False)
    assert summary["reports"] == []
    assert len(summary["data_missing"]) == 1
    md = Path(summary["data_missing"][0]).read_text(encoding="utf-8")
    assert "DATA MISSING" in md and "Upload the latest MT5 report" in md
    assert "NO performance metrics" in md


def test_workflow_imports_and_reports(db):
    (config.INCOMING_DIR / "deals_SYNTHETIC.csv").write_text(syn.deals_csv())
    summary = run_weekly(db=db, now="2026-01-18 12:00", pdf=False, monthly=False)
    assert summary["imports"][0]["status"] in ("IMPORTED", "IMPORTED_WITH_WARNINGS")
    periods = {tuple(r["period"]) for r in summary["reports"]}
    assert ("2026-01-12 00:00:00", "2026-01-19 00:00:00") in periods
    # previous week back-filled so the comparison has a baseline
    assert ("2026-01-05 00:00:00", "2026-01-12 00:00:00") in periods
    assert summary["data_missing"] == []
    # next week with no new data -> DATA MISSING
    s2 = run_weekly(db=db, now="2026-01-25 12:00", pdf=False, monthly=False)
    assert len(s2["data_missing"]) == 1
    assert "No new files were imported" in Path(s2["data_missing"][0]).read_text()


def test_cli_exit_codes(db):
    import run_weekly
    args = ["--now", "2026-01-11T12:00", "--no-pdf", "--no-monthly", "--account", syn.ACCOUNT]
    assert run_weekly.main(args) == 2  # empty database -> DATA MISSING
    (config.INCOMING_DIR / "deals_SYNTHETIC.csv").write_text(syn.deals_csv())
    assert run_weekly.main(args) == 0
    assert run_weekly.main(["--week", "2026-01-05", "--account", syn.ACCOUNT, "--no-pdf"]) == 0
    assert db.accounts() == [syn.ACCOUNT]  # account-less CSV attributed via --account
