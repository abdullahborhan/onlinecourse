"""Phase 5 - risk compliance engine (synthetic scenarios with known verdicts)."""
import pytest

from modules.analysis_service import run_analysis
from modules.common import BREACH, COMPLIANT, UNVERIFIABLE
from modules.mt5_importer import MT5Importer
from tests import synthetic_data as syn

P = syn.pos
RISK_POSITIONS = [
    # Day 1 (2026-02-02): loss lock reached at 10:00 (-200 = -2%), trading continues
    P("R1", "buy", 1.0, "2026-02-02 09:00:00", 1000, 900, "2026-02-02 10:00:00", 800),
    P("R2", "buy", 0.5, "2026-02-02 11:00:00", 1000, 990, "2026-02-02 12:00:00", 900),
    # martingale-like escalation after losses (0.5 -> 2.0 -> 4.0)
    P("R3", "buy", 2.0, "2026-02-02 12:30:00", 1000, 995, "2026-02-02 13:00:00", 990),
    P("R4", "buy", 4.0, "2026-02-02 13:30:00", 1000, 999, "2026-02-02 14:00:00", 1000.5),
    # hard stop: cumulative -318 = -3.18%
    P("R5", "sell", 0.5, "2026-02-02 14:30:00", 1000, 1100, "2026-02-02 15:00:00", 1100),
    # Day 2: one inferred basket with 4 layers, averaging down, last layer larger
    P("S1", "buy", 0.5, "2026-02-03 09:00:00", 1000, 950, "2026-02-03 10:00:00", 1010),
    P("S2", "buy", 0.25, "2026-02-03 09:05:00", 995, 950, "2026-02-03 10:00:00", 1010),
    P("S3", "buy", 0.25, "2026-02-03 09:10:00", 990, 950, "2026-02-03 10:00:00", 1010),
    P("S4", "buy", 0.5, "2026-02-03 09:15:00", 985, 950, "2026-02-03 10:00:00", 1010),
]
RISK_DEPOSITS = [("2026-02-02 00:00:00", 10_000.0, "Initial deposit")]
WEEK = ("2026-02-02", "2026-02-09")


def _checks(res):
    return {c["check"]: c for c in res["risk"]["checks"]}


@pytest.fixture
def risk_result(db):
    MT5Importer(db).import_bytes(syn.deals_csv(RISK_POSITIONS, RISK_DEPOSITS).encode(),
                                 "risk_SYNTHETIC.csv", account_id="RSK")
    return run_analysis(db, "RSK", *WEEK, compare_with_db=False)


def test_daily_loss_lock_breach(risk_result):
    c = _checks(risk_result)["daily_loss_lock"]
    assert c["verdict"] == BREACH
    day = c["evidence"]["breach_days"][0]
    assert day["day"] == "2026-02-02"
    assert day["entries_after_lock"] == 4  # R2..R5 opened after the lock level


def test_hard_stop_breach(risk_result):
    assert _checks(risk_result)["daily_hard_stop"]["verdict"] == BREACH


def test_martingale_detected(risk_result):
    c = _checks(risk_result)["martingale"]
    assert c["verdict"] == BREACH
    assert {x["position_id"] for x in c["evidence"]["cases"]} == {"R3", "R4"}


def test_max_layers_breach_on_inferred_basket(risk_result):
    c = _checks(risk_result)["max_layers"]
    assert c["verdict"] == BREACH
    assert c["evidence"]["max_layers_observed"] == 4
    assert "INFERRED" in c["basis"]


def test_layer_risk_over_allocation(risk_result):
    c = _checks(risk_result)["max_basket_risk"]
    assert c["verdict"] == BREACH
    over = {x["position_id"] for x in c["evidence"]["layers_over_allocation"]}
    assert "R1" in over  # 100 / 10,000 = 1.0% vs 0.5% allocation


def test_unverifiable_without_equity(risk_result):
    ch = _checks(risk_result)
    # weekly loss stays below 5% in realised data: cannot be proven without equity
    assert ch["weekly_loss_lock"]["verdict"] == UNVERIFIABLE
    assert ch["max_equity_drawdown"]["verdict"] == UNVERIFIABLE
    assert ch["stop_loss_presence"]["verdict"] == COMPLIANT
    assert risk_result["status"]["status"] == "TEMPORARILY STOP LIVE TRADING"


def test_risk_metrics(risk_result):
    m = risk_result["risk"]["metrics"]
    assert m["max_layer_count"].value == 4
    assert m["max_consecutive_losses"].value == 3
    assert m["largest_loss"].value == pytest.approx(-200.0)
    assert m["worst_daily_realised_loss_pct"].value == pytest.approx(3.18)


@pytest.fixture
def clean_week(db):
    positions = [p for p in syn.POSITIONS if p["pid"] in {"1001", "1002", "1003", "1004", "1005"}]
    deals = syn.build_deals(positions, syn.DEPOSITS[:1])
    imp = MT5Importer(db)
    imp.import_bytes(deals.to_csv(index=False).encode(), "clean_SYNTHETIC.csv",
                     account_id="CLN")
    return db, imp, deals


def test_equity_verified_compliance(clean_week):
    db, imp, deals = clean_week
    imp.import_bytes(syn.dense_equity_csv(deals, "2026-01-05", "2026-01-12").encode(),
                     "eq_SYNTHETIC.csv", account_id="CLN")
    ch = _checks(run_analysis(db, "CLN", "2026-01-05", "2026-01-12", compare_with_db=False))
    for name in ("daily_loss_lock", "daily_hard_stop", "weekly_loss_lock", "max_equity_drawdown",
                 "stop_loss_presence", "max_basket_risk", "max_layers", "martingale"):
        assert ch[name]["verdict"] == COMPLIANT, (name, ch[name])


def test_equity_lock_breach_detected(clean_week):
    db, imp, deals = clean_week
    # Equity dips 250 (≈2.5%) at 10:05 on 2026-01-05; position 1002 opens at 10:20
    imp.import_bytes(syn.dense_equity_csv(deals, "2026-01-05", "2026-01-12",
                                          dips={"2026-01-05 10:05:00": 250.0}).encode(),
                     "eqdip_SYNTHETIC.csv", account_id="CLN")
    c = _checks(run_analysis(db, "CLN", "2026-01-05", "2026-01-12",
                             compare_with_db=False))["daily_loss_lock"]
    assert c["verdict"] == BREACH
    assert c["evidence"]["breach_days"][0]["basis"] == "equity"


def test_sparse_equity_is_not_enough(clean_week):
    db, imp, deals = clean_week
    imp.import_bytes(syn.dense_equity_csv(deals, "2026-01-05", "2026-01-12", freq="h").encode(),
                     "eqh_SYNTHETIC.csv", account_id="CLN")
    c = _checks(run_analysis(db, "CLN", "2026-01-05", "2026-01-12",
                             compare_with_db=False))["daily_loss_lock"]
    assert c["verdict"] == UNVERIFIABLE
    assert "coverage gap" in c["evidence"]["unverifiable_days"][0]["reason"]
