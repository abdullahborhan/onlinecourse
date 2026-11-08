"""Phase 2 - importers (synthetic data only)."""
import pandas as pd
import pytest

import config
from modules.html_parser import parse_mt5_html
from modules.mt5_importer import (DUPLICATE_FILE, REJECTED, MT5Importer, detect_kind,
                                  import_incoming_directory)
from modules.parsing_utils import parse_number, parse_timestamps, read_text
from tests import synthetic_data as syn


def test_parse_number_variants():
    assert parse_number("1 234.56") == 1234.56
    assert parse_number("-12.50") == -12.5
    assert parse_number("1.234,56") == 1234.56
    assert parse_number("1,234.56") == 1234.56
    assert parse_number("0.10 / 0.10") == 0.10
    assert parse_number("") is None  # missing never becomes 0
    assert parse_number(None) is None


@pytest.mark.parametrize("values,expected", [
    (["2026.01.05 10:00:00", "2026.01.06 11:30:15"], "2026-01-05 10:00:00"),
    (["2026-01-05 10:00:00", "2026-01-06 11:30:15"], "2026-01-05 10:00:00"),
    (["2026-01-05T10:00:00", "2026-01-06T11:30:15"], "2026-01-05 10:00:00"),
    (["2026.01.05 10:00", "2026.01.06 11:30"], "2026-01-05 10:00:00"),
    (["25/01/2026 10:00", "26/01/2026 11:30"], "2026-01-25 10:00:00"),
    (["01/25/2026 10:00", "01/26/2026 11:30"], "2026-01-25 10:00:00"),
    (["1767607200", "1767610800"], "2026-01-05 10:00:00"),
])
def test_timestamp_formats(values, expected):
    parsed, info = parse_timestamps(pd.Series(values))
    assert str(parsed.iloc[0]) == expected
    assert info["failed"] == 0


def test_ambiguous_timestamps_flagged():
    _, info = parse_timestamps(pd.Series(["03/04/2026 10:00", "05/06/2026 11:00"]))
    assert info["ambiguous"] is True
    assert "ambiguous" in info["warning"]


def test_utf16_decode():
    text, enc = read_text("Account: 1".encode("utf-16"))
    assert text == "Account: 1" and enc == "utf-16"


def test_csv_deals_import_and_duplicates(db):
    imp = MT5Importer(db)
    data = syn.deals_csv().encode()
    res = imp.import_bytes(data, "deals_SYNTHETIC.csv", account_id=syn.ACCOUNT)
    assert res.ok, res.errors
    assert res.kind == "csv_deals"
    total = len(syn.build_deals())
    assert res.records["deals"]["new"] == total
    # Same file again -> refused as duplicate file
    res2 = imp.import_bytes(data, "deals_SYNTHETIC.csv", account_id=syn.ACCOUNT)
    assert res2.status == DUPLICATE_FILE
    # Overlapping file (different bytes, same deals) -> rows de-duplicated by deal id
    res3 = imp.import_bytes(data + b"\n", "deals_copy_SYNTHETIC.csv", account_id=syn.ACCOUNT)
    assert res3.ok
    assert res3.records["deals"] == {"new": 0, "duplicate": total}
    assert len(db.load_deals(syn.ACCOUNT)) == total
    # Balance continuity of the synthetic set is intact
    assert res.validation["deals"]["balance_continuity"]["mismatches"] == 0


def test_missing_required_column_rejected(db):
    imp = MT5Importer(db)
    res = imp.import_bytes(syn.deals_csv(drop_columns=["Profit"]).encode(),
                           "no_profit_SYNTHETIC.csv", account_id=syn.ACCOUNT)
    assert res.status == REJECTED
    assert any("profit" in e for e in res.errors)
    assert db.load_deals(syn.ACCOUNT).empty


def test_missing_commission_kept_null(db):
    imp = MT5Importer(db)
    res = imp.import_bytes(syn.deals_csv(drop_columns=["Commission"]).encode(),
                           "nocomm_SYNTHETIC.csv", account_id=syn.ACCOUNT)
    assert res.ok
    assert any("Commission not provided" in w for w in res.warnings)
    assert db.load_deals(syn.ACCOUNT)["commission"].isna().all()


def test_invalid_files_rejected(db):
    imp = MT5Importer(db)
    assert imp.import_bytes(b"", "empty.csv").status == REJECTED
    assert imp.import_bytes(b"hello world", "x.pdf").status == REJECTED
    res = imp.import_bytes(b"a,b,c\n1,2,3\n", "junk_SYNTHETIC.csv")
    assert res.status == REJECTED


def test_synthetic_refused_in_production(db, monkeypatch):
    monkeypatch.setattr(config, "IS_SYNTHETIC_ENV", False)
    res = MT5Importer(db).import_bytes(syn.deals_csv().encode(), "deals.csv", account_id="X")
    assert res.status == REJECTED
    assert "SYNTHETIC" in res.errors[0]


def test_html_report_parse_and_import(db):
    data = syn.mt5_html()
    text, _ = read_text(data)
    parsed = parse_mt5_html(text)
    assert parsed["account"]["account_id"] == syn.ACCOUNT
    assert parsed["account"]["currency"] == "USD"
    assert set(parsed["tables"]) >= {"positions", "deals"}
    assert detect_kind("ReportHistory.html", text) == "mt5_html"

    res = MT5Importer(db).import_bytes(data, "ReportHistory_SYNTHETIC.html")
    assert res.ok, res.errors
    assert res.account_id == syn.ACCOUNT
    assert res.records["positions"]["new"] == len(syn.POSITIONS)
    # Totals row is excluded, deals count matches
    assert res.records["deals"]["new"] == len(syn.build_deals())
    cc = res.validation["results_cross_check"]
    assert cc["checked"] and cc["match"]
    pos = db.load_report_positions(syn.ACCOUNT)
    p = pos.set_index("position_id").loc["1005"]
    assert p["close_price"] == pytest.approx(1285.0)
    assert p["profit"] == pytest.approx(15.0)
    # Missing SL in report stays NULL, not 0
    assert pd.isna(pos.set_index("position_id").loc["1006", "sl"])


def test_equity_log_and_set_import(db):
    imp = MT5Importer(db)
    r1 = imp.import_bytes(syn.equity_csv().encode(), "equity_SYNTHETIC.csv",
                          account_id=syn.ACCOUNT)
    assert r1.kind == "csv_equity" and r1.ok
    r2 = imp.import_bytes(syn.ea_log().encode(), "20260105_SYNTHETIC.log",
                          account_id=syn.ACCOUNT)
    assert r2.kind == "ea_log" and r2.ok
    ev = db.load_ea_events(syn.ACCOUNT)
    assert (ev["event_type"] == "REJECTION").sum() == 1
    assert set(ev["ticket"].dropna()) == {"1001", "1002"}
    assert str(ev["time"].iloc[0]).startswith("2026-01-05 09:59:58")
    r3 = imp.import_bytes(syn.set_file(looser=True).encode(), "shark_SYNTHETIC.set")
    assert r3.kind == "ea_set" and r3.ok
    drift = {d["parameter"]: d["verdict"] for d in r3.validation["drift_vs_approved"]}
    assert drift["daily_loss_lock_pct"] == "LOOSER_THAN_APPROVED"
    assert drift["max_layers"] == "MATCH"
    assert drift["martingale_enabled"] == "MATCH"
    assert db.active_ea_config()["origin"] == "SET_FILE_UNCONFIRMED"


def test_incoming_directory_workflow(db):
    (config.INCOMING_DIR / "deals_SYNTHETIC.csv").write_text(syn.deals_csv())
    (config.INCOMING_DIR / "bad.csv").write_text("x,y\n1,2\n")
    results = import_incoming_directory(db)
    st = {r.filename: r.status for r in results}
    assert st["bad.csv"] == REJECTED
    assert (config.PROCESSED_DIR / "deals_SYNTHETIC.csv").exists()
    assert (config.INCOMING_DIR / "bad.csv").exists()  # rejected stays for user attention
    assert any(config.ARCHIVE_DIR.iterdir())
