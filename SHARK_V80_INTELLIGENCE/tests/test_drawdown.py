"""Phase 4 - drawdown engine (synthetic data only)."""
import pandas as pd
import pytest

from modules.common import CALCULATED, ESTIMATED, UNAVAILABLE
from modules.drawdown_engine import _drawdown_stats, compute_drawdowns, drawdown_metrics
from modules.mt5_importer import MT5Importer
from modules.trade_reconstruction import reconstruct_trades
from tests import synthetic_data as syn

W1 = ("2026-01-05", "2026-01-12")


@pytest.fixture
def data(db):
    MT5Importer(db).import_bytes(syn.deals_csv().encode(), "d_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    deals = db.load_deals(syn.ACCOUNT)
    trades, _ = reconstruct_trades(deals)
    return db, deals, trades


def test_balance_drawdown_week1(data):
    _, deals, trades = data
    dd = compute_drawdowns(deals, trades, None, *W1)
    b = dd["balance"]
    assert b["status"] == CALCULATED
    assert b["max_dd_money"] == pytest.approx(10.5)
    assert b["max_dd_pct"] == pytest.approx(10.5 / 10_026.75 * 100)
    assert str(b["peak_time"]) == "2026-01-05 11:00:00"
    assert str(b["trough_time"]) == "2026-01-06 10:00:00"
    assert str(b["recovery_time"]) == "2026-01-08 15:00:00"
    # No equity data -> equity DD UNAVAILABLE, estimate labelled ESTIMATED
    assert dd["equity"]["status"] == UNAVAILABLE
    est = dd["equity_estimate_closed_trade"]
    assert est["status"] == ESTIMATED and "LOWER BOUND" in est["note"]
    m = drawdown_metrics(dd)
    assert m["max_equity_drawdown_pct"].status == UNAVAILABLE
    assert m["closed_trade_equity_dd_estimate_pct"].status == ESTIMATED


def test_equity_drawdown_from_observations(data):
    db, deals, trades = data
    MT5Importer(db).import_bytes(syn.equity_csv().encode(), "eq_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    dd = compute_drawdowns(deals, trades, db.load_equity(syn.ACCOUNT), *W1)
    e = dd["equity"]
    assert e["status"] == CALCULATED
    assert e["max_dd_money"] == pytest.approx(60.0)
    assert e["max_dd_pct"] == pytest.approx(60.0 / 10_026.75 * 100)
    # Equity DD deeper than closed-trade estimate (estimate is a lower bound)
    assert e["max_dd_money"] > dd["balance"]["max_dd_money"]


def test_deposit_is_not_recovery_and_withdrawal_not_drawdown():
    t = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"])
    level = pd.Series([1000.0, 900.0, 1400.0, 900.0])  # loss 100, deposit 500, withdraw 500
    flows = pd.Series([0.0, 0.0, 500.0, 0.0])          # cumulative flows
    flows.iloc[3] = 0.0  # +500 then -500
    st = _drawdown_stats(t, level, flows, 1.0)
    assert st["max_dd_money"] == pytest.approx(100.0)
    assert st["recovered"] is False  # deposit did not count as recovery


def test_drawdown_frequency():
    t = pd.date_range("2026-01-01", periods=7, freq="D")
    level = pd.Series([100.0, 95.0, 101.0, 100.5, 102.0, 90.0, 92.0])
    st = _drawdown_stats(t, level, pd.Series([0.0] * 7), min_episode_pct=1.0)
    assert st["episodes"] == 2  # 5% and 11.8% episodes; 0.5% episode ignored
    assert st["episodes_all"] == 3
    assert st["max_dd_pct"] == pytest.approx(12 / 102 * 100)


def test_cumulative_pl_only_has_no_percent():
    trades = pd.DataFrame({"status": ["CLOSED"] * 3, "net_profit": [10.0, -5.0, 2.0],
                           "close_time": pd.date_range("2026-01-05", periods=3, freq="h")})
    dd = compute_drawdowns(pd.DataFrame(), trades, None)
    assert dd["balance"]["max_dd_money"] == pytest.approx(5.0)
    assert dd["balance"]["max_dd_pct"] is None
    assert drawdown_metrics(dd)["max_balance_drawdown_pct"].status == UNAVAILABLE
