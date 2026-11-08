"""Phase 1 - configuration and database (synthetic data only)."""
import json

import pandas as pd
import pytest

import config
from modules.database import RESET_CONFIRMATION


def _deals(n=3, source_id=1):
    return pd.DataFrame({
        "account_id": ["T1"] * n,
        "deal_id": [str(100 + i) for i in range(n)],
        "time": pd.date_range("2026-01-05 10:00", periods=n, freq="h"),
        "symbol": ["Volatility 80 Index"] * n,
        "deal_type": ["buy"] * n,
        "entry": ["IN"] * n,
        "volume": [0.5] * n,
        "price": [100.0] * n,
        "commission": [None] * n,
        "profit": [0.0] * n,
        "source_file_id": [source_id] * n,
    })


def _src(db, sha="abc"):
    return db.register_source_file(filename="f.csv", sha256=sha, file_kind="csv_deals",
                                   size_bytes=10, account_id="T1", status="IMPORTED",
                                   validation={}, archive_path=None, is_synthetic=True)


def test_schema_created(db):
    with db.connect() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("deals", "report_positions", "orders", "equity_points", "ea_events", "ea_configs",
              "reports", "audit_log", "source_files", "account_info"):
        assert t in tables
    assert db.environment() == "test"


def test_duplicate_deals_ignored_and_null_preserved(db):
    sid = _src(db)
    new, dup = db.insert_deals(_deals(3, sid))
    assert (new, dup) == (3, 0)
    new, dup = db.insert_deals(_deals(3, sid))
    assert (new, dup) == (0, 3)
    df = db.load_deals("T1")
    assert len(df) == 3
    # Missing commission stays NULL, never becomes 0
    assert df["commission"].isna().all()


def test_duplicate_file_sha_rejected(db):
    _src(db, "same")
    with pytest.raises(Exception):
        _src(db, "same")
    assert db.find_source_by_sha("same")["filename"] == "f.csv"


def test_reset_requires_confirmation(db):
    sid = _src(db)
    db.insert_deals(_deals(2, sid))
    with pytest.raises(PermissionError):
        db.reset_all_data("yes")
    assert len(db.load_deals("T1")) == 2
    backup = db.reset_all_data(RESET_CONFIRMATION)
    assert backup.exists()
    assert db.load_deals("T1").empty


def test_report_supersede_and_previous(db):
    for start, end in [("2026-01-05", "2026-01-12"), ("2026-01-12", "2026-01-19")]:
        db.save_report(account_id="T1", period_type="WEEKLY", period_start=start,
                       period_end=end, status="STABLE", is_data_missing=False,
                       fingerprint="x", metrics={"a": 1}, report={})
    db.save_report(account_id="T1", period_type="WEEKLY", period_start="2026-01-12",
                   period_end="2026-01-19", status="NEEDS ATTENTION", is_data_missing=False,
                   fingerprint="y", metrics={"a": 2}, report={})
    reports = db.list_reports("T1")
    assert len(reports) == 2  # superseded hidden
    prev = db.previous_report("T1", "WEEKLY", "2026-01-12")
    assert prev["period_start"].startswith("2026-01-05")


def test_config_roundtrip(tmp_path):
    cfg = config.load_config()
    assert cfg.risk.layer_risk_pct == (0.50, 0.25, 0.25)
    assert cfg.risk.martingale_enabled is False
    cfg.risk.daily_loss_lock_pct = 1.5
    cfg.risk.source = "USER_PROVIDED"
    path = config.save_config(cfg)
    loaded = config.load_config(path)
    assert loaded.risk.daily_loss_lock_pct == 1.5
    assert loaded.risk.source == "USER_PROVIDED"
    assert json.loads(path.read_text())["risk"]["max_layers"] == 3
