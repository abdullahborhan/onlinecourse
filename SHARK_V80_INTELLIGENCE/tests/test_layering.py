"""Phase 5 - layering intelligence (synthetic data only)."""
import pandas as pd
import pytest

from modules.analysis_service import build_trade_frame, run_analysis
from modules.layering_analyzer import EXPLICIT, INFERRED, simultaneous_exposure
from modules.mt5_importer import MT5Importer
from tests import synthetic_data as syn
from tests.test_risk import RISK_DEPOSITS, RISK_POSITIONS, WEEK

import config


@pytest.fixture
def base(db):
    MT5Importer(db).import_bytes(syn.deals_csv().encode(), "d_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    return db, build_trade_frame(db, syn.ACCOUNT, config.load_config())


def test_explicit_baskets_and_layers(base):
    _, b = base
    t = b["trades"].set_index("position_id")
    assert t.loc["1001", "basket_id"] == t.loc["1002", "basket_id"] == "B:A1"
    assert t.loc["1001", "basket_method"] == EXPLICIT
    assert (t.loc["1001", "layer"], t.loc["1002", "layer"]) == (1, 2)
    assert t.loc["1002", "layer_source"] == "COMMENT"
    # 1006 has no comment -> inferred singleton basket
    assert t.loc["1006", "basket_method"] == INFERRED


def test_layer_stats_and_continuation(db):
    MT5Importer(db).import_bytes(syn.deals_csv().encode(), "d_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    res = run_analysis(db, syn.ACCOUNT, "2026-01-05", "2026-01-12", compare_with_db=False)
    lay = res["layering"]
    ls = lay["layers"].set_index("layer")
    assert ls.loc[1, "trades"] == 5 and ls.loc[2, "trades"] == 1
    assert ls.loc[2, "net_profit"] == pytest.approx(7.25)
    bk = lay["baskets"].set_index("basket_id")
    # Layer 2 of A1 entered at 1010 > 1000 on a buy: added WITH the move
    assert bk.loc["B:A1", "layering_type"] == "CONTINUATION"
    assert bk.loc["B:A1", "avg_seconds_between_layers"] == 20 * 60
    assert lay["max_layer_count"] == 2


def test_stacking_escalation_and_against_layering(db):
    MT5Importer(db).import_bytes(syn.deals_csv(RISK_POSITIONS, RISK_DEPOSITS).encode(),
                                 "risk_SYNTHETIC.csv", account_id="RSK")
    res = run_analysis(db, "RSK", *WEEK, compare_with_db=False)
    codes = {f["code"] for f in res["findings"]}
    assert {"EXCESSIVE_STACKING", "LAYER_VOLUME_ESCALATION"} <= codes
    b = res["layering"]["baskets"]
    four = b[b["layers"] == 4].iloc[0]
    assert four["method"] == INFERRED
    assert four["layering_type"] == "AGAINST"
    # floating P/L of existing layers when S2 was added: (995-1000)*0.5 = -2.5
    assert four["adds"][0]["floating_pl_at_add"] == pytest.approx(-2.5)
    assert res["layering"]["confirmation_check"] == "UNVERIFIABLE"


def test_simultaneous_exposure():
    t = pd.DataFrame({
        "open_time": pd.to_datetime(["2026-01-01 10:00", "2026-01-01 10:05", "2026-01-01 10:10",
                                     "2026-01-01 11:00"]),
        "close_time": pd.to_datetime(["2026-01-01 10:30", "2026-01-01 10:30", "2026-01-01 11:00",
                                      "2026-01-01 11:30"]),
        "volume": [0.5, 0.25, 0.25, 1.0], "direction": ["buy", "buy", "sell", "buy"]})
    e = simultaneous_exposure(t)
    assert e["max_positions"] == 3
    assert e["max_volume"] == pytest.approx(1.0)
    assert e["max_buy_positions"] == 2
    # close at 11:00 happens before the open at 11:00 -> no 2-position overlap counted there
