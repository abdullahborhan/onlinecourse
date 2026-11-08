"""Regression tests for defects found in QA acceptance review (synthetic data only)."""
import pandas as pd
import pytest

from modules.analysis_service import run_analysis
from modules.common import BREACH, UNVERIFIABLE
from modules.drawdown_engine import compute_drawdowns
from modules.mt5_importer import REJECTED, MT5Importer
from modules.risk_analyzer import lot_escalation_check
from modules.trade_reconstruction import reconstruct_trades
from tests import synthetic_data as syn

P = syn.pos
W = ("2026-01-05", "2026-01-12")


def _checks(res):
    return {c["check"]: c for c in res["risk"]["checks"]}


def _imp(db, positions, deposits, acct, equity=None):
    deals = syn.build_deals(positions, deposits)
    imp = MT5Importer(db)
    r = imp.import_bytes(deals.to_csv(index=False).encode(), f"{acct}_SYNTHETIC.csv",
                         account_id=acct)
    assert r.ok, r.errors
    if equity is not None:
        r = imp.import_bytes(equity.encode(), f"{acct}_eq_SYNTHETIC.csv", account_id=acct)
        assert r.ok, r.errors
    return deals


def test_withdrawal_is_not_a_trading_loss(db):
    """QA #1: withdrawal produced false daily/weekly BREACH and STOP."""
    pos = [P("A1", "buy", 0.5, "2026-01-05 10:00:00", 1000, 990, "2026-01-05 11:00:00", 1004),
           P("A2", "buy", 0.5, "2026-01-05 13:00:00", 1000, 990, "2026-01-05 14:00:00", 1004)]
    dep = [("2026-01-05 00:00:00", 10_000.0, "Deposit"),
           ("2026-01-05 12:00:00", -1_000.0, "Withdrawal")]
    deals = syn.build_deals(pos, dep)
    _imp(db, pos, dep, "WD", syn.dense_equity_csv(deals, "2026-01-05", "2026-01-12"))
    res = run_analysis(db, "WD", *W, compare_with_db=False)
    ch = _checks(res)
    for name in ("daily_loss_lock", "daily_hard_stop", "weekly_loss_lock", "max_equity_drawdown"):
        assert ch[name]["verdict"] != BREACH, ch[name]
    assert res["status"]["status"] != "TEMPORARILY STOP LIVE TRADING"


def test_closed_trade_dd_breach_not_masked_by_partial_equity(db):
    """QA #2: equity data for a later quiet day made a 12% closed-trade DD 'COMPLIANT'."""
    pos = [P("B1", "buy", 1.0, "2026-01-05 10:00:00", 1000, 0.0, "2026-01-05 11:00:00", 0.0),
           P("B2", "buy", 0.1, "2026-01-07 10:00:00", 1000, 990, "2026-01-07 11:00:00", 1001)]
    pos[0]["sl"] = 990.0
    pos[0]["exits"] = [("2026-01-05 11:00:00", 1.0, -200.0, 0.0)]  # loss of 1,200 = 12%
    dep = [("2026-01-05 00:00:00", 10_000.0, "Deposit")]
    deals = syn.build_deals(pos, dep)
    _imp(db, pos, dep, "DD", syn.dense_equity_csv(deals, "2026-01-07", "2026-01-08"))
    c = _checks(run_analysis(db, "DD", *W, compare_with_db=False))["max_equity_drawdown"]
    assert c["verdict"] == BREACH


def test_lock_needs_coverage_of_trading_window(db):
    """QA #3: a good median gap hid a 22-hour hole in equity coverage."""
    pos = [P("C1", "buy", 0.5, "2026-01-05 09:00:00", 1000, 980, "2026-01-05 10:00:00", 1010),
           P("C2", "buy", 0.5, "2026-01-05 17:00:00", 1000, 980, "2026-01-05 18:00:00", 1010)]
    dep = [("2026-01-05 00:00:00", 10_000.0, "Deposit")]
    rows = [{"Time": t.strftime("%Y.%m.%d %H:%M"), "Balance": 10_000.0, "Equity": 10_000.0}
            for t in pd.date_range("2026-01-05 00:00", "2026-01-05 01:00", freq="1min")]
    rows.append({"Time": "2026.01.05 23:55", "Balance": 10_010.0, "Equity": 10_010.0})
    eq = pd.DataFrame(rows).to_csv(index=False) + f"# {syn.MARKER}\n"
    _imp(db, pos, dep, "COV", eq)
    ch = _checks(run_analysis(db, "COV", *W, compare_with_db=False))
    assert ch["daily_loss_lock"]["verdict"] == UNVERIFIABLE
    assert ch["daily_hard_stop"]["verdict"] == UNVERIFIABLE


def test_mixed_seconds_timestamps_parse_correctly(db):
    """QA #4: mixed with/without-seconds timestamps were date-swapped or dropped."""
    df = syn.build_deals()
    df.loc[df.index % 2 == 1, "Time"] = df.loc[df.index % 2 == 1, "Time"].str[:-3]
    r = MT5Importer(db).import_bytes(df.to_csv(index=False).encode(), "mix_SYNTHETIC.csv",
                                     account_id="MIX")
    assert r.ok, r.errors
    d = db.load_deals("MIX")
    assert len(d) == len(df)
    assert d["time"].min() == pd.Timestamp("2026-01-05 00:00:00")
    assert d["time"].max() < pd.Timestamp("2026-01-14")


def test_unparseable_timestamp_rejects_file(db):
    df = syn.build_deals()
    df.loc[5, "Time"] = "not-a-date"
    r = MT5Importer(db).import_bytes(df.to_csv(index=False).encode(), "bad_SYNTHETIC.csv",
                                     account_id="BAD")
    assert r.status == REJECTED and any("unparseable" in e for e in r.errors)


def test_basket_counts_layers_from_previous_period(db):
    """QA #5: an earlier still-open layer was ignored by max_layers."""
    l1 = dict(pid="L1", dir="buy", vol=0.5, t_open="2026-01-02 10:00:00", p_open=1000.0,
              sl=950.0, tp=0.0, comment="", exits=[])
    rest = [P(f"L{i}", "buy", 0.25, f"2026-01-06 1{i}:00:00", 1000, 950,
              "2026-01-06 19:00:00", 1005) for i in (2, 3, 4)]
    _imp(db, [l1] + rest, [("2026-01-01 00:00:00", 10_000.0, "Deposit")], "LAY")
    c = _checks(run_analysis(db, "LAY", *W, compare_with_db=False))["max_layers"]
    assert c["verdict"] == BREACH and c["evidence"]["max_layers_observed"] == 4


def test_unattributable_html_fees_are_flagged(db):
    """QA #6: HTML deal fees silently dropped from net profit."""
    MT5Importer(db).import_bytes(syn.mt5_html(), "r_SYNTHETIC.html")
    deals = db.load_deals(syn.ACCOUNT)
    deals.loc[deals["entry"] == "OUT", "fee"] = -1.0
    trades, rep = reconstruct_trades(deals, db.load_report_positions(syn.ACCOUNT))
    assert rep["unattributed_fees"] == pytest.approx(-10.0)
    assert not trades["costs_complete"].any()


def test_blank_exit_profit_rejected(db):
    """QA #7: a blank Profit on an exit deal became 0."""
    df = syn.build_deals()
    i = df.index[(df["Position"] == "1003") & (df["Entry"] == "out")][0]
    df["Profit"] = df["Profit"].astype(object)
    df.loc[i, "Profit"] = ""
    r = MT5Importer(db).import_bytes(df.to_csv(index=False).encode(), "bp_SYNTHETIC.csv",
                                     account_id="BP")
    assert r.status == REJECTED and any("blank Profit" in e for e in r.errors)


def test_lot_escalation_uses_prior_history_only():
    """QA #8: baseline included the period's own trades."""
    t = pd.DataFrame({"position_id": [f"p{i}" for i in range(9)], "layer": 1,
                      "open_time": pd.date_range("2026-01-01", periods=9, freq="D"),
                      "volume": [0.1] * 4 + [1.0] * 5})
    start = pd.Timestamp("2026-01-05")
    period = t[t["open_time"] >= start]
    assert lot_escalation_check(t, period, start)["verdict"] == UNVERIFIABLE  # only 4 prior
    t2 = pd.concat([pd.DataFrame({"position_id": ["p0b"], "layer": [1], "volume": [0.1],
                                  "open_time": [pd.Timestamp("2025-12-31")]}), t])
    assert lot_escalation_check(t2, period, start)["verdict"] == BREACH


def test_pl_only_drawdown_starts_at_zero():
    """QA #9: first losing trade was not counted as drawdown."""
    trades = pd.DataFrame({"status": ["CLOSED"] * 3, "net_profit": [-300.0, -200.0, 100.0],
                           "close_time": pd.date_range("2026-01-05", periods=3, freq="h")})
    dd = compute_drawdowns(pd.DataFrame(), trades, None)
    assert dd["balance"]["max_dd_money"] == pytest.approx(500.0)
