"""Offline tests for evaluation/src/card_transaction_eval/risk_suite.py helpers and wiring."""
import json
import sys
from pathlib import Path

import pytest
import sqlglot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation" / "src"))

from card_transaction_eval import hybrid_suite, judge_suite, reporting, risk_suite  # noqa: E402
from card_transaction_eval.config import DEFAULTS, EvalConfig  # noqa: E402
from fraud_transaction_mcp import risk_scoring  # noqa: E402
from fraud_transaction_mcp.sql_guard import validate_select  # noqa: E402

T = "workspace.default.card_transaction_mcp_clean"
P = risk_scoring.parameters()


@pytest.mark.parametrize("sql", [
    risk_suite.card_sql(T, P, "6::0", "2019-06-01", "2019-07-01"),
    risk_suite.user_sql(T, P, "1161", "2020-01-01", "2020-02-01"),
    risk_suite.ranking_sql(T, P, "2020-01-01", "2020-02-01"),
])
def test_reference_sql_passes_the_run_risk_sql_guard(sql):
    assert validate_select(sql, T)
    risk_scoring._check_scope(sql)
    assert len(sqlglot.parse(sql, read="databricks")) == 1


def test_reference_sql_uses_markdown_parameters():
    sql = risk_suite.card_sql(T, P, "6::0", "2019-06-01", "2019-07-01")
    assert "percentile_approx(amount, 0.95)" in sql and "INTERVAL 24 HOURS" in sql
    assert "prior_n >= 10" in sql and "n_win >= 5" in sql and "3.0 *" in sql
    assert ">= 0.75 THEN 'high'" in sql and ">= 0.5 THEN 'medium'" in sql


def test_ranking_from_text_json_and_truncated():
    full = {"summary": {"output": "ranking", "ranking": [{"user_id": "1161"}, {"user_id": "1007"}]}}
    text = json.dumps(full)
    assert risk_suite.ranking_from_text(text) == ["1161", "1007"]
    assert risk_suite.ranking_from_text(text[:-5] + "... [truncated]") == ["1161", "1007"]
    assert risk_suite.ranking_from_text('{"records": []}') == []


def test_verifier_feedback():
    text = "**Answer:** x\nFEEDBACK: first\nmore\nFEEDBACK: re-run the ranking with LIMIT 5\nVERDICT: REVISE"
    assert risk_suite.verifier_feedback(text) == "re-run the ranking with LIMIT 5"
    assert risk_suite.verifier_feedback("VERDICT: PASS") == "" and risk_suite.verifier_feedback(None) == ""


def test_stability_case_keeps_full_answer_and_verification(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from card_transaction_eval.utils import SuiteResult
    long_answer = "A" * 5000
    ranking = json.dumps({"summary": {"output": "ranking", "ranking": [{"user_id": u} for u in
                                                                       risk_suite.RANKING_CASE["top_users"]]}})

    async def ask_pav_async(question, model=None, session=None):
        call = SimpleNamespace(name="run_risk_sql", arguments={"sql": "SELECT 1"}, result=ranking, is_error=False)
        return SimpleNamespace(answer=long_answer, tool_calls=[call], verdict="REVISE", revisions=1,
                               verification="Unverified\nFEEDBACK: re-score user 1161\nVERDICT: REVISE", plan="P")

    @asynccontextmanager
    async def fake_session(config):
        yield "S"
    monkeypatch.setattr(risk_suite, "open_session", fake_session)
    monkeypatch.setattr("card_transaction_eval.judge_suite._client_module",
                        lambda config: SimpleNamespace(ask_pav_async=ask_pav_async))
    cfg = SimpleNamespace(enable_llm=True, risk_stability_runs=1, llm_endpoint="m", risk_pav_threshold_ms=1e9)
    r = SuiteResult("x")
    asyncio.run(risk_suite._stability(r, cfg))
    d = r.cases[0].details
    assert d["answer"] == long_answer and d["verifier_feedback"] == "re-score user 1161"
    assert d["verification"].endswith("VERDICT: REVISE") and d["plan"] == "P" and d["agent_ranking_sql"] == "SELECT 1"
    assert r.cases[0].status == "fail"          # REVISE fails the run even with the right top five


def test_parse_windows():
    assert risk_suite.parse_windows("2019-01-01:2019-01-31, 2019-06-01:2019-06-30") == [
        ("2019-01-01", "2019-01-31"), ("2019-06-01", "2019-06-30")]
    assert risk_suite.parse_windows("") == []
    with pytest.raises(ValueError):
        risk_suite.parse_windows("2019-01-01:jan")


def _res(by_score, ind):
    return {"transactions": sum(x["transactions"] for x in by_score),
            "fraud": sum(x["fraud"] for x in by_score), "by_score": by_score, "indicators": ind}


def test_combine_efficacy_monotonic_and_lift():
    a = _res([{"risk_score": 0.0, "transactions": 900, "fraud": 1},
              {"risk_score": 0.75, "transactions": 100, "fraud": 4}],
             {"amount_anomaly": {"tp": 4, "fp": 96}})
    b = _res([{"risk_score": 0.0, "transactions": 900, "fraud": 1},
              {"risk_score": 0.75, "transactions": 100, "fraud": 4}],
             {"amount_anomaly": {"tp": 4, "fp": 96}})
    pooled = risk_suite.combine_efficacy([a, b])
    assert pooled["transactions"] == 2000 and pooled["fraud"] == 10
    assert pooled["by_score"][1] == {"risk_score": 0.75, "transactions": 200, "fraud": 8, "fraud_rate": 0.04}
    assert risk_suite.monotonic(pooled["by_score"], 100) == (True, [])
    assert risk_suite.band_lift(pooled, 0.75) == pytest.approx(8.0)
    ind = pooled["indicators"]["amount_anomaly"]
    assert ind["flagged"] == 200 and ind["flag_rate"] == 0.1 and ind["lift"] == pytest.approx(8.0)


def test_monotonic_detects_drop_and_ignores_small_levels():
    scores = [{"risk_score": 0.0, "transactions": 1000, "fraud": 10, "fraud_rate": 0.01},
              {"risk_score": 0.25, "transactions": 500, "fraud": 2, "fraud_rate": 0.004},
              {"risk_score": 1.0, "transactions": 5, "fraud": 0, "fraud_rate": 0.0}]
    ok, bad = risk_suite.monotonic(scores, 100)
    assert not ok and bad == [(0.0, 0.25)]


def test_config_has_risk_keys():
    names = set(EvalConfig.__dataclass_fields__)
    for key in ("enable_risk_suite", "risk_stability_runs", "risk_efficacy_windows", "risk_min_high_lift"):
        assert key in DEFAULTS and key in names
    assert risk_suite.parse_windows(DEFAULTS["risk_efficacy_windows"])


def test_routing_questions_and_expectations():
    ids = {q[0]: q for q in hybrid_suite.QUESTIONS}
    for qid in ("HR-11", "HR-12", "HR-13"):
        assert qid in ids and "run_risk_sql" in ids[qid][3] and qid in judge_suite.EXPECTED_BEHAVIOUR
    assert "run_risk_sql" in ids["HR-10"][3]
    assert ids["HR-08"][2] == "B" and ids["HR-09"][2] == "B"   # recorded-label questions still referred
    for qid, q, *_ in hybrid_suite.QUESTIONS:
        q.format(y=2019, u="1", tx="2", c="1::0")


def test_report_lists_risk_suite():
    assert ("risk_scoring", "risk_scoring.json", "Rule-based fraud risk scoring") in reporting.SUITES
    assert reporting._rule("RS-ST01-1")[1] == "Risk scoring - agent stability"
    assert reporting._rule("RS-S03")[1] == "Risk scoring - guardrails"
