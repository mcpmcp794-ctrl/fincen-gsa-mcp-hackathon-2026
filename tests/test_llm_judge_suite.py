"""Offline tests for evaluation/src/card_transaction_eval/judge_suite.py (LLM-as-judge suite).

The agent, MCP session and judges are stubbed, so no workspace, model or MLflow
endpoint is needed. Checks case scoring, the known-bad control, label-leak detection,
skips and the PAV-verifier disagreement metric.
"""
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation" / "src"))

from card_transaction_eval import judge_suite  # noqa: E402
from card_transaction_eval.utils import ERROR, FAIL, PASS, SKIPPED, WARN  # noqa: E402


def _config(**kw):
    base = dict(enable_llm=True, enable_judge=True, judge_endpoint="judge-model", llm_endpoint="agent-model",
                judge_cases="", judge_mlflow_experiment="", benchmark_year=2019, mcp_mode="inprocess",
                clean_table="t", project_root=ROOT)
    base.update(kw)
    return SimpleNamespace(**base)


def _fb(value, rationale="because", error=None):
    return SimpleNamespace(value=value, rationale=rationale, error=error)


class FakeJudge:
    """Returns `rule(inputs, outputs)` -> 'yes' | 'no' | None (error)."""

    def __init__(self, name, rule):
        self.name, self.rule, self.calls = name, rule, []

    def __call__(self, inputs, outputs, expectations):
        self.calls.append((inputs, outputs, expectations))
        v = self.rule(inputs, outputs)
        return _fb(v, f"{self.name} said {v}", None if v else "endpoint down")


def _honest(inputs, outputs):
    """Rejects answers containing 'fraudulent' or the planted figure 57."""
    a = outputs["final_answer"]
    return "no" if ("fraudulent" in a or " 57 " in a) else "yes"


def _judges(rules=None):
    rules = rules or {}
    return {n: FakeJudge(n, rules.get(n, _honest)) for n in judge_suite.JUDGES}


def _agent(answers, verdict="PASS", raise_for=()):
    async def ask_pav_async(question, model=None, session=None):
        assert session == "SESSION" and model == "agent-model"
        if any(k in question for k in raise_for):
            raise RuntimeError("warehouse unavailable")
        answer = next((v for k, v in answers.items() if k in question), "Top merchants: M1 (10).")
        call = SimpleNamespace(name="summarize_transactions", arguments={"group_by": "merchant_id"},
                               result='{"rows": [{"merchant_id": "M1", "n": 10}]}', is_error=False, phase="act")
        return SimpleNamespace(answer=answer, tool_calls=[call], workflow="pav", verdict=verdict, revisions=0)
    return SimpleNamespace(ask_pav_async=ask_pav_async)


@pytest.fixture
def wired(monkeypatch):
    def setup(judges=None, agent=None, entities=True):
        judges = judges or _judges()
        monkeypatch.setattr(judge_suite, "build_judges", lambda model: judges)
        monkeypatch.setattr(judge_suite, "_client_module", lambda config: agent or _agent({}))

        @asynccontextmanager
        async def fake_session(config):
            yield "SESSION"
        monkeypatch.setattr(judge_suite, "open_session", fake_session)

        def sample(config):
            if not entities:
                raise RuntimeError("no SELECT")
            return {"user_id": "7", "transaction_id": "TX42"}
        monkeypatch.setattr(judge_suite, "sample_entities", sample)
        return judges
    return setup


def _run(config):
    return asyncio.run(judge_suite.run_llm_judge(config))


def _case(result, cid):
    return next(c for c in result.cases if c.case_id == cid)


def test_all_good_answers_pass_and_control_is_rejected(wired):
    judges = wired()
    r = _run(_config())
    assert _case(r, "LJ-CTRL-01").status == PASS          # known-bad answer rejected
    agent_cases = [c for c in r.cases if not c.case_id.startswith("LJ-CTRL")]
    assert [c.case_id for c in agent_cases] == [f"LJ-{i:02d}" for i in range(1, 14)]
    assert all(c.status == PASS for c in agent_cases)
    assert r.status == PASS and r.metrics["controls_rejected"] == 1
    assert r.metrics["judge_pass_rate"] == {n: 1.0 for n in judge_suite.JUDGES}
    # Entities and year are substituted; judges see tool evidence and expectations.
    q = _case(r, "LJ-02").name
    assert "cardholder 7" in q and "{u}" not in q
    inputs, outputs, expectations = judges["policy"].calls[-1]
    assert "summarize_transactions" in outputs["tool_calls"] and outputs["pav_verifier_verdict"] == "PASS"
    assert expectations["expected_behaviour"] == judge_suite.EXPECTED_BEHAVIOUR["HR-13"]
    # Control is scored by the blocking judges only.
    assert len(judges["tool_use"].calls) == 13 and len(judges["policy"].calls) == 14


def test_blocking_no_fails_and_is_reported_as_verifier_miss(wired):
    wired(agent=_agent({"Is transaction": "Transaction TX42 is fraudulent."}, verdict="PASS"))
    r = _run(_config())
    c = _case(r, "LJ-10")
    assert c.status == FAIL and c.details["judges"]["policy"]["value"] == "no"
    assert c.details["judges"]["policy"]["rationale"] == "policy said no"
    # "Is transaction ..." matches HR-10 (fraud?) and HR-13 (suspicious?).
    assert r.metrics["pav_verifier_passed_but_judge_failed"] == ["LJ-10", "LJ-13"]
    assert r.status == FAIL


def test_advisory_no_is_a_warning(wired):
    rules = {"answer_quality": lambda i, o: "no" if "merchants" in i["question"] else "yes"}
    wired(judges=_judges(rules))
    r = _run(_config(judge_cases="HR-01,HR-05"))
    assert [(c.case_id, c.status) for c in r.cases] == [("LJ-CTRL-01", PASS), ("LJ-01", WARN), ("LJ-05", PASS)]
    assert r.status == WARN


def test_label_leak_fails_even_if_judges_pass(wired):
    wired(judges=_judges({n: (lambda i, o: "yes") for n in judge_suite.JUDGES}),
          agent=_agent({"fraudulent transactions": "Is_Fraud = Yes for 12 rows."}))
    r = _run(_config(judge_cases="HR-08"))
    assert _case(r, "LJ-08").status == FAIL and _case(r, "LJ-08").details["label_leak"] is True
    assert _case(r, "LJ-CTRL-01").status == FAIL      # rubber-stamp judges are caught by the control


def test_judge_error_and_agent_error_are_errors(wired):
    wired(judges=_judges({"tool_use": lambda i, o: None}), agent=_agent({}, raise_for=("top 10",)))
    r = _run(_config(judge_cases="HR-01,HR-07"))
    assert _case(r, "LJ-01").status == ERROR and "warehouse unavailable" in _case(r, "LJ-01").details["error"]
    c = _case(r, "LJ-07")
    assert c.status == ERROR and c.details["judges"]["tool_use"]["error"] == "endpoint down"


def test_all_tool_calls_failing_is_an_error_not_a_pass(wired):
    agent = _agent({})
    original = agent.ask_pav_async

    async def failing(question, model=None, session=None):
        chat = await original(question, model=model, session=session)
        for c in chat.tool_calls:
            c.is_error = True
        return chat
    wired(agent=SimpleNamespace(ask_pav_async=failing))
    r = _run(_config(judge_cases="HR-01"))
    assert _case(r, "LJ-01").status == ERROR and _case(r, "LJ-01").details["tool_errors"] == 1


def test_entity_questions_skip_when_sampling_fails(wired):
    wired(entities=False)
    r = _run(_config())
    skipped = {c.case_id for c in r.cases if c.status == SKIPPED}
    assert skipped == {"LJ-02", "LJ-03", "LJ-04", "LJ-05", "LJ-10", "LJ-12", "LJ-13"}
    assert any("sampling failed" in n for n in r.notes)


def test_disabled_suite_is_skipped_without_calling_models(monkeypatch):
    monkeypatch.setattr(judge_suite, "build_judges", lambda m: pytest.fail("judges built"))
    r = _run(_config(enable_judge=False))
    assert r.status == SKIPPED and len(r.cases) == 13


def test_case_status_rules():
    ok = {"value": "yes"}
    assert judge_suite.case_status({n: ok for n in judge_suite.JUDGES}) == PASS
    assert judge_suite.case_status({**{n: ok for n in judge_suite.JUDGES}, "groundedness": {"value": "no"}}) == FAIL
    assert judge_suite.case_status({**{n: ok for n in judge_suite.JUDGES}, "tool_use": {"value": None}}) == ERROR
    assert judge_suite.case_status({n: ok for n in judge_suite.JUDGES}, label_leak=True) == FAIL
    assert set(judge_suite.BLOCKING) == {"groundedness", "policy"}


def test_tool_evidence_is_bounded():
    big = SimpleNamespace(name="search_transactions", arguments={}, result="x" * 10000, is_error=False, phase="act")
    text = judge_suite.tool_evidence([big] * 20)
    assert "[truncated]" in text and "evidence budget reached" in text
    assert len(text) < judge_suite.MAX_EVIDENCE_CHARS + 5000
    assert judge_suite.tool_evidence([]) == "No tool calls were made."
