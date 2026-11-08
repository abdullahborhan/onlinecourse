"""Phase 4 - performance analytics against hand-calculated synthetic expectations."""
import math

import pandas as pd
import pytest

from modules.common import UNAVAILABLE, UNDEFINED
from modules.mt5_importer import MT5Importer
from modules.performance_engine import closed_trades, compute_performance, profit_factor, streaks
from modules.trade_reconstruction import reconstruct_trades
from tests import synthetic_data as syn

W1 = ("2026-01-05", "2026-01-12")
W2 = ("2026-01-12", "2026-01-19")


@pytest.fixture
def trades(db):
    MT5Importer(db).import_bytes(syn.deals_csv().encode(), "d_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    t, _ = reconstruct_trades(db.load_deals(syn.ACCOUNT))
    return t


def test_week1_profitability(trades):
    e = syn.EXPECTED["week1"]
    w1 = closed_trades(trades, *W1)
    perf = compute_performance(w1, start_balance=10_000.0, period_days=7)
    P, S = perf["profitability"], perf["statistics"]
    assert P["net_profit"].value == pytest.approx(e["net_profit"])
    assert P["gross_profit"].value == pytest.approx(e["gross_profit"])
    assert P["gross_loss"].value == pytest.approx(e["gross_loss"])
    assert P["profit_factor"].value == pytest.approx(e["profit_factor"])
    assert P["total_return_pct"].value == pytest.approx(e["total_return_pct"])
    assert P["largest_winning_trade"].value == pytest.approx(e["largest_win"])
    assert P["largest_losing_trade"].value == pytest.approx(e["largest_loss"])
    assert P["average_profit_per_trade"].value == pytest.approx(e["net_profit"] / 6)
    assert P["expectancy"].value == pytest.approx(e["net_profit"] / 6)
    assert S["total_closed_trades"].value == e["trades"]
    assert S["winning_trades"].value == e["wins"]
    assert S["losing_trades"].value == e["losses"]
    assert S["breakeven_trades"].value == e["breakeven"]
    assert S["win_rate"].value == pytest.approx(e["win_rate"])
    assert S["win_rate_excl_breakeven"].value == pytest.approx(60.0)
    assert S["average_trades_per_day"].value == pytest.approx(6 / 7)
    assert perf["costs"]["commission"].value == pytest.approx(e["commission"])


def test_week2_and_streaks(trades):
    w2 = closed_trades(trades, *W2)
    perf = compute_performance(w2)
    assert perf["profitability"]["net_profit"].value == pytest.approx(1.25)
    assert perf["statistics"]["longest_losing_streak"].value == 2
    assert perf["profitability"]["total_return_pct"].status == UNAVAILABLE


def test_profit_factor_undefined_when_no_losses():
    pf = profit_factor(50.0, 0.0)
    assert pf.status == UNDEFINED and math.isinf(pf.value)
    assert profit_factor(0.0, 0.0).value is None


def test_streaks_breakeven_resets():
    assert streaks(["WIN", "WIN", "BREAKEVEN", "WIN", "LOSS", "LOSS", "LOSS"]) == (2, 3)


def test_empty_period_is_unavailable_not_zero(trades):
    perf = compute_performance(closed_trades(trades, "2027-01-01", "2027-01-08"))
    assert perf["statistics"]["total_closed_trades"].value == 0
    assert perf["profitability"]["net_profit"].status == UNAVAILABLE
    assert perf["profitability"]["profit_factor"].status == UNAVAILABLE


def test_breakeven_epsilon():
    t = pd.DataFrame({"net_profit": [0.004, -0.004, 5.0], "costs_complete": [True] * 3,
                      "entry_known": [True] * 3, "duration_s": [60.0] * 3,
                      "close_time": pd.to_datetime(["2026-01-05"] * 3),
                      "commission": [0.0] * 3, "swap": [0.0] * 3, "fee": [0.0] * 3})
    s = compute_performance(t, eps=0.005)["statistics"]
    assert s["breakeven_trades"].value == 2 and s["winning_trades"].value == 1
    s0 = compute_performance(t, eps=0.0)["statistics"]
    assert s0["breakeven_trades"].value == 0 and s0["losing_trades"].value == 1


def test_missing_commission_annotated(db):
    MT5Importer(db).import_bytes(syn.deals_csv(drop_columns=["Commission"]).encode(),
                                 "nc_SYNTHETIC.csv", account_id=syn.ACCOUNT)
    t, _ = reconstruct_trades(db.load_deals(syn.ACCOUNT))
    perf = compute_performance(closed_trades(t, *W1))
    assert perf["costs"]["commission"].status == UNAVAILABLE
    assert "lack commission" in perf["profitability"]["net_profit"].note
    # net = gross profit only (30 - week2 excluded): week1 profits 20+7.5-10+0+15-5 = 27.5
    assert perf["profitability"]["net_profit"].value == pytest.approx(27.5)
