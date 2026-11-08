"""Historical comparison engine (synthetic data only)."""
import pytest

from modules.analysis_service import run_analysis
from modules.comparison_engine import compare
from modules.mt5_importer import MT5Importer
from tests import synthetic_data as syn


def _m(**kw):
    return {k: {"value": v} for k, v in kw.items()}


META = {"period_type": "WEEKLY", "period_days": 7.0, "equity_available": False,
        "classification_coverage_pct": 100.0, "costs_complete": True, "trade_source": "DEALS",
        "period_start": "2026-01-12", "period_end": "2026-01-19"}
PREV_META = {**META, "period_start": "2026-01-05", "period_end": "2026-01-12"}


def test_changes_and_directions():
    cur = _m(net_profit=50.0, profit_factor=1.2, max_balance_drawdown_pct=4.0, win_rate=40.0)
    prev = _m(net_profit=100.0, profit_factor=2.0, max_balance_drawdown_pct=2.0, win_rate=55.0)
    out = compare(cur, prev, META, PREV_META, "previous week")
    r = {x["key"]: x for x in out["rows"]}
    assert r["net_profit"]["abs_change"] == pytest.approx(-50.0)
    assert r["net_profit"]["pct_change"] == pytest.approx(-50.0)
    assert r["net_profit"]["direction"] == "DECLINING"
    assert r["max_balance_drawdown_pct"]["direction"] == "DECLINING"  # higher DD = worse
    assert out["profitability_trend"] == "DECLINING"
    assert "Max balance drawdown %" in out["risk_increasing"]
    codes = {f.code for f in out["findings"]}
    assert {"PROFITABILITY_DECLINE", "RISK_INCREASE"} <= codes
    assert out["limitations"] == []


def test_unavailable_metric_not_compared():
    out = compare(_m(net_profit=None), _m(net_profit=10.0), META, PREV_META)
    r = {x["key"]: x for x in out["rows"]}
    assert r["net_profit"]["direction"] == "N/A" and r["net_profit"]["abs_change"] is None


def test_incompatible_periods_flagged():
    cur_meta = {**META, "period_days": 30.0, "equity_available": True,
                "classification_coverage_pct": 50.0, "period_start": "2026-01-10"}
    out = compare(_m(net_profit=1.0), _m(net_profit=2.0), cur_meta, PREV_META)
    text = " ".join(out["limitations"])
    assert "Different period lengths" in text
    assert "overlap" in text
    assert "Equity observations available in only one period" in text
    assert "classification coverage differs" in text


def test_strategy_deterioration():
    cur = {"strategy": {"BREAKOUT": {"profit_factor": 0.8, "net_profit": -5.0}}}
    prev = {"strategy": {"BREAKOUT": {"profit_factor": 1.6, "net_profit": 20.0}}}
    out = compare(cur, prev, META, PREV_META)
    assert "BREAKOUT_DETERIORATION" in {f.code for f in out["findings"]}


def test_no_previous_report():
    out = compare(_m(net_profit=1.0), None, META, None, "previous week")
    assert out["available"] is False and "No stored previous week" in out["note"]


def test_week_vs_previous_week_from_database(db):
    MT5Importer(db).import_bytes(syn.deals_csv().encode(), "d_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    w1 = run_analysis(db, syn.ACCOUNT, "2026-01-05", "2026-01-12")
    db.save_report(account_id=syn.ACCOUNT, period_type="WEEKLY", period_start="2026-01-05",
                   period_end="2026-01-12", status=w1["status"]["status"],
                   is_data_missing=False, fingerprint=w1["fingerprint"],
                   metrics=w1["flat_metrics"], report={"meta": w1["meta"]})
    w2 = run_analysis(db, syn.ACCOUNT, "2026-01-12", "2026-01-19")
    c = w2["comparison"]
    assert c["available"]
    r = {x["key"]: x for x in c["rows"]}
    assert r["net_profit"]["previous"] == pytest.approx(24.75)
    assert r["net_profit"]["current"] == pytest.approx(1.25)
    assert r["net_profit"]["abs_change"] == pytest.approx(-23.5)
    assert r["total_closed_trades"]["current"] == 3
