"""Phase 10 - AI analyst guardrails (no network: the client is a fake)."""
from types import SimpleNamespace

import pytest

import config
from modules.ai_analyst import build_fact_sheet, run_ai_analysis, validate_ai_text
from modules.analysis_service import run_analysis
from modules.mt5_importer import MT5Importer
from tests import synthetic_data as syn


class FakeClient:
    def __init__(self, text, stop="end_turn"):
        self.calls = []
        resp = SimpleNamespace(stop_reason=stop,
                               content=[SimpleNamespace(type="text", text=text)])
        create = lambda **kw: (self.calls.append(kw), resp)[1]  # noqa: E731
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


@pytest.fixture
def res(db):
    MT5Importer(db).import_bytes(syn.deals_csv().encode(), "d_SYNTHETIC.csv",
                                 account_id=syn.ACCOUNT)
    return run_analysis(db, syn.ACCOUNT, "2026-01-05", "2026-01-12", compare_with_db=False)


def _cfg(enabled=True):
    cfg = config.load_config()
    cfg.ai.external_ai_enabled = enabled
    return cfg


def test_fact_sheet_has_no_raw_data(res):
    sheet = build_fact_sheet(res)
    text = str(sheet)
    assert syn.ACCOUNT not in text
    assert "1001" not in text  # no tickets
    assert "trades_period" not in sheet
    assert sheet["metrics"]["net_profit"]["value"] == pytest.approx(24.75)


def test_offline_without_consent_sends_nothing(res):
    client = FakeClient("FACT: ok")
    out = run_ai_analysis(res, _cfg(True), consent=False, client=client)
    assert out["source"] == "OFFLINE_RULE_BASED" and client.calls == []
    out = run_ai_analysis(res, _cfg(False), consent=True, client=client)
    assert client.calls == []
    assert any(p.startswith("FACT: Net profit 24.75") for p in out["paras"])
    assert any("should not be increased" in p for p in out["paras"])


def test_external_valid_output_accepted(res):
    client = FakeClient("FACT: Net profit was 24.75 with a win rate of 50.00%.\n"
                        "HYPOTHESIS: The pullback loss may reflect a trend day.")
    out = run_ai_analysis(res, _cfg(), consent=True, client=client)
    assert out["source"].startswith("EXTERNAL_AI")
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default" and kw["output_config"] == {"effort": "medium"}
    assert "SYN001" not in kw["messages"][0]["content"]


@pytest.mark.parametrize("text,reason", [
    ("FACT: Net profit was 999.99.", "numbers not present"),
    ("TEST: Increase the lot size on breakout trades to 1.0 lots.", "increasing lot"),
    ("FACT: I accessed the MT5 account and confirmed balance.", "MT5/broker access"),
    ("TEST: I changed the EA configuration to fix this.", "change EA"),
])
def test_guardrails_reject(res, text, reason):
    out = run_ai_analysis(res, _cfg(), consent=True, client=FakeClient(text))
    assert out["source"] == "OFFLINE_RULE_BASED"
    assert reason in " ".join(out["validation"]["violations"])


def test_refusal_and_errors_fall_back(res):
    out = run_ai_analysis(res, _cfg(), consent=True, client=FakeClient("", stop="refusal"))
    assert out["source"] == "OFFLINE_RULE_BASED" and "refusal" in out["note"]


def test_validator_tolerates_small_list_numbers(res):
    sheet = build_fact_sheet(res)
    assert validate_ai_text("1. FACT: three strategies, layer 2.", sheet)["ok"]
