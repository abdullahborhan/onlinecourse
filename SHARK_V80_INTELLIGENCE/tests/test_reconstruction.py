"""Phase 3 - trade reconstruction (synthetic data only)."""
import numpy as np
import pandas as pd
import pytest

from modules.mt5_importer import MT5Importer
from modules.trade_reconstruction import add_risk_at_entry, point_values, reconstruct_trades
from tests import synthetic_data as syn


@pytest.fixture
def loaded(db):
    MT5Importer(db).import_bytes(syn.deals_csv().encode(), "d_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    return db.load_deals(syn.ACCOUNT)


def test_entry_deals_not_counted_as_trades(loaded):
    trades, rep = reconstruct_trades(loaded)
    trade_deals = loaded[loaded["deal_type"].isin(["buy", "sell"])]
    assert len(trade_deals) == 19  # 9 IN + 10 OUT deals
    assert len(trades) == len(syn.POSITIONS) == 9
    assert rep["closed"] == 9


def test_partial_close_reconstructed(loaded):
    trades, rep = reconstruct_trades(loaded)
    p = trades.set_index("position_id").loc["1005"]
    assert p["n_exits"] == 2 and bool(p["partial_close"])
    assert p["exit_price"] == pytest.approx(1285.0)
    assert p["profit"] == pytest.approx(15.0)
    assert p["net_profit"] == pytest.approx(14.0)
    assert rep["partial_close_positions"] == 1


def test_net_includes_costs_and_balance_at_open(loaded):
    trades, _ = reconstruct_trades(loaded)
    nets = trades.set_index("position_id")["net_profit"]
    assert nets["1001"] == pytest.approx(19.5)
    assert nets["1003"] == pytest.approx(-10.5)
    assert trades.set_index("position_id").loc["1001", "balance_at_open"] == 10_000.0
    assert (trades["balance_basis"] == "VERIFIED").all()


def test_sl_status(loaded):
    trades, _ = reconstruct_trades(loaded)
    t = trades.set_index("position_id")
    assert t.loc["1006", "sl_status"] == "NOT_SET"
    assert t.loc["1001", "sl_status"] == "SET"


def test_open_and_partially_closed_positions():
    base = dict(account_id="A", symbol="V80", magic=None, comment=None, commission=np.nan,
                swap=np.nan, fee=np.nan, balance=np.nan, sl=np.nan, tp=np.nan, order_id=None)
    deals = pd.DataFrame([
        dict(base, deal_id="1", time=pd.Timestamp("2026-01-05 10:00"), deal_type="buy",
             entry="IN", volume=1.0, price=100.0, position_id="P1", profit=0.0),
        dict(base, deal_id="2", time=pd.Timestamp("2026-01-05 11:00"), deal_type="sell",
             entry="OUT", volume=0.4, price=110.0, position_id="P1", profit=4.0),
        dict(base, deal_id="3", time=pd.Timestamp("2026-01-05 12:00"), deal_type="sell",
             entry="IN", volume=1.0, price=100.0, position_id="P2", profit=0.0),
        # orphan exit: position opened before the data window
        dict(base, deal_id="4", time=pd.Timestamp("2026-01-05 13:00"), deal_type="sell",
             entry="OUT", volume=0.5, price=90.0, position_id="P0", profit=-3.0),
    ])
    trades, rep = reconstruct_trades(deals)
    t = trades.set_index("position_id")
    assert t.loc["P1", "status"] == "PARTIALLY_CLOSED"
    assert t.loc["P2", "status"] == "OPEN"
    assert t.loc["P0", "status"] == "CLOSED" and not t.loc["P0", "entry_known"]
    assert t.loc["P0", "direction"] == "buy"
    assert rep["orphan_exits"] == 1
    # Missing costs remain NaN and are flagged
    assert np.isnan(t.loc["P1", "commission"]) and not t.loc["P1", "costs_complete"]


def test_entry_inferred_when_missing(db):
    df = syn.build_deals().drop(columns=["Entry"])
    MT5Importer(db).import_bytes(df.to_csv(index=False).encode(), "ne_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    trades, rep = reconstruct_trades(db.load_deals(syn.ACCOUNT))
    assert rep["entries_inferred"] == 19
    assert len(trades) == 9
    assert trades["net_profit"].sum() == pytest.approx(syn.EXPECTED["all"]["net_profit"])


def test_html_only_uses_report_positions(db):
    MT5Importer(db).import_bytes(syn.mt5_html(), "r_SYNTHETIC.html")
    trades, rep = reconstruct_trades(db.load_deals(syn.ACCOUNT),
                                     db.load_report_positions(syn.ACCOUNT))
    assert len(trades) == 9
    assert set(trades["source"]) == {"REPORT_POSITIONS"}
    assert rep["partial_close_detection"] == "UNAVAILABLE"
    assert trades["partial_close"].isna().all()
    # comment enriched from opening deal (order == position id)
    assert trades.set_index("position_id").loc["1001", "comment"] == "SV80 BRK L1 bsk=A1"
    assert trades["net_profit"].sum() == pytest.approx(26.0)


def test_point_value_and_risk(loaded):
    trades, _ = reconstruct_trades(loaded)
    pv = point_values(trades)
    assert pv[syn.SYMBOL]["value_per_point_per_lot"] == pytest.approx(1.0)
    assert pv[syn.SYMBOL]["usable"]
    t = add_risk_at_entry(trades, pv).set_index("position_id")
    # 1001: |1000-980| * 0.5 lot * 1.0 = 10 -> 0.1% of 10,000
    assert t.loc["1001", "risk_money"] == pytest.approx(10.0)
    assert t.loc["1001", "risk_pct"] == pytest.approx(0.1)
    assert np.isnan(t.loc["1006", "risk_pct"])  # no SL -> risk unavailable
