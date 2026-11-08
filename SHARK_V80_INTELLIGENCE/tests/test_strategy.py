"""Phase 6 - strategy classification/comparison and anomaly statistics (synthetic)."""
import numpy as np
import pandas as pd
import pytest

from modules.anomaly_detector import detect_anomalies, prob_streak_at_least
from modules.analysis_service import run_analysis
from modules.common import UNCLASSIFIED
from modules.mt5_importer import MT5Importer
from modules.strategy_analyzer import classify_strategies
from tests import synthetic_data as syn

import config

PATTERNS = config.AnalysisConfig().strategy_comment_patterns


def _trades(comments, magic=None):
    return pd.DataFrame({"position_id": [str(i) for i in range(len(comments))],
                         "open_order_id": [str(i) for i in range(len(comments))],
                         "comment": comments, "magic": magic or [None] * len(comments)})


def test_classification_sources_and_conflicts():
    events = pd.DataFrame({"ticket": ["0", "1"], "strategy": ["MOMENTUM", "PULLBACK"]})
    t, rep = classify_strategies(
        _trades(["SV80 BRK L1", "SV80 BRK", "pullback entry", None, "brk pb"],
                magic=[None, None, None, "777", None]),
        events, PATTERNS, {"777": "MOMENTUM"})
    assert list(t["strategy"]) == ["MOMENTUM", "PULLBACK", "PULLBACK", "MOMENTUM", UNCLASSIFIED]
    assert list(t["strategy_source"]) == ["EA_LOG", "EA_LOG", "COMMENT", "MAGIC_MAP", "NONE"]
    reasons = " ".join(c["reason"] for c in rep["conflicts"])
    assert "EA log=MOMENTUM vs comment=BREAKOUT" in reasons
    assert "several strategies" in reasons
    assert rep["coverage_pct"] == pytest.approx(80.0) and not rep["complete"]


def test_never_classified_from_profit_or_direction():
    t, _ = classify_strategies(_trades([None, "", "manual"]), None, PATTERNS, {})
    assert (t["strategy"] == UNCLASSIFIED).all()


def test_strategy_comparison(db):
    MT5Importer(db).import_bytes(syn.deals_csv().encode(), "d_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    res = run_analysis(db, syn.ACCOUNT, "2026-01-05", "2026-01-12", compare_with_db=False)
    s = res["strategy"]
    tbl = s["table"].set_index("strategy")
    assert tbl.loc["BREAKOUT", "trades"] == 2
    assert tbl.loc["BREAKOUT", "net_profit"] == pytest.approx(26.75)
    assert tbl.loc["MOMENTUM", "net_profit"] == pytest.approx(14.0)
    assert tbl.loc["PULLBACK", "net_profit"] == pytest.approx(-10.5)
    assert tbl.loc[UNCLASSIFIED, "trades"] == 1
    assert s["most_profit"]["strategy"] == "BREAKOUT"
    assert s["most_risk"]["strategy"] == "PULLBACK"
    # Max balance DD window (05 11:00 -> 06 10:00) is caused by the pullback loss
    assert s["most_drawdown_contribution"]["strategy"] == "PULLBACK"
    assert "CLASSIFICATION_INCOMPLETE" in {f["code"] for f in res["findings"]}


def test_streak_probability():
    assert prob_streak_at_least(3, 3, 0.5) == pytest.approx(0.125)
    assert prob_streak_at_least(4, 2, 0.5) == pytest.approx(0.5)
    assert prob_streak_at_least(2, 3, 0.9) == 0.0


def _frame(nets, start, strategy="BREAKOUT", vol=0.5):
    n = len(nets)
    close = pd.date_range(start, periods=n, freq="2h")
    return pd.DataFrame({"status": "CLOSED", "net_profit": nets, "close_time": close,
                         "open_time": close - pd.Timedelta(minutes=30), "volume": vol,
                         "duration_s": 1800.0, "position_id": [f"{start}{i}" for i in range(n)],
                         "strategy": strategy})


def test_win_rate_drop_and_mix_change_detected():
    rng = np.random.default_rng(7)
    hist = _frame(np.where(rng.random(120) < 0.6, 10.0, -8.0), "2026-01-01")
    cur = _frame([10.0] * 2 + [-8.0] * 18, "2026-03-01", strategy="PULLBACK")
    out = detect_anomalies(cur, hist, period_days=7, max_layers=3, z_thr=3.0, min_hist=30,
                           alpha=0.05)
    codes = {f.code for f in out["findings"]}
    assert {"WIN_RATE_DROP", "PERFORMANCE_DETERIORATION", "STRATEGY_MIX_CHANGE",
            "ABNORMAL_LOSING_STREAK"} <= codes
    f = next(x for x in out["findings"] if x.code == "WIN_RATE_DROP")
    assert f.hypotheses and "manipulation" not in f.fact.lower()


def test_insufficient_history_reported_not_flagged():
    cur = _frame([10.0, -5.0, 3.0], "2026-03-01")
    out = detect_anomalies(cur, cur.iloc[0:0], period_days=7, max_layers=3, z_thr=3.0,
                           min_hist=30, alpha=0.05)
    assert out["findings"] == []
    assert "baseline unavailable" in out["info"][0]


def test_lot_escalation_anomaly():
    hist = _frame([5.0, -3.0] * 30, "2026-01-01", vol=0.5)
    cur = _frame([5.0, -3.0, 2.0], "2026-03-01", vol=0.5)
    cur.loc[2, "volume"] = 3.0
    out = detect_anomalies(cur, hist, period_days=7, max_layers=3, z_thr=3.0, min_hist=30,
                           alpha=0.05)
    assert "LOT_ESCALATION" in {f.code for f in out["findings"]}
