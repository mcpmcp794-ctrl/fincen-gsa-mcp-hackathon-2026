"""Plan -> Act -> Verify prompt templates and their MCP registration."""
import asyncio
import re

import pytest

from fraud_transaction_mcp.app import create_server
from fraud_transaction_mcp.config import Settings
from fraud_transaction_mcp.prompts import PROMPT_NAMES, pav_workflow
from fraud_transaction_mcp.tools import ANALYSIS_TOOLS

STAGES = ('plan', 'act', 'verify')
EXPECTED_VARS = {
    'plan': {'research_question', 'max_steps', 'fraud_referral'},
    'act': {'research_question', 'plan', 'feedback', 'max_steps', 'fraud_referral'},
    'verify': {'research_question', 'plan', 'findings', 'max_steps', 'fraud_referral'},
}
FRAUD_SPACE = '01f1dc44f22413fe82320128338353b5'


def _prompt_text(server, name, args):
    res = asyncio.run(server.get_prompt(name, args))
    return '\n'.join(m.content.text for m in res.messages)


@pytest.mark.parametrize('stage', STAGES)
def test_template_files_exist_with_expected_placeholders(stage):
    assert (pav_workflow.PROMPT_DIR / pav_workflow.TEMPLATES[stage]).is_file()
    assert pav_workflow.template_variables(stage) == EXPECTED_VARS[stage]


@pytest.mark.parametrize('stage', STAGES)
def test_templates_are_label_free_and_reference_only_real_tools(stage):
    text = pav_workflow.load_template(stage)
    assert 'is_fraud' not in text.lower() and 'evaluation_label' not in text.lower()
    # PaySim-only tools from the source template must not leak into this server's prompts.
    for paysim_tool in ('test_hypothesis', 'evaluate_detection_rule', 'account_profile',
                        'summarize_cash_flow', 'detect_transaction_patterns'):
        assert paysim_tool not in text
    called = set(re.findall(r'\b([a-z_]+)\(', text))
    assert called <= set(ANALYSIS_TOOLS), called - set(ANALYSIS_TOOLS)


def test_planner_lists_every_analysis_tool():
    text = pav_workflow.load_template('plan')
    for tool in ANALYSIS_TOOLS:
        assert f'**{tool}(' in text, tool


def test_render_fills_every_placeholder_and_does_not_reexpand_user_text():
    out = pav_workflow.render('act', research_question='Q {{plan}}', plan='P1', feedback='')
    assert '{{' not in out.replace('Q {{plan}}', '')
    assert 'Q {{plan}}' in out and 'P1' in out
    assert 'at most 8 tool calls' in out  # default act max_steps


def test_render_rejects_missing_values():
    with pytest.raises(ValueError, match='plan'):
        pav_workflow.render('verify', research_question='q', findings='f')


# [CLEANUP 2026-11-17] pav_workflow.parse_verdict / strip_verdict are commented out
# (runtime parsing lives in client/llm_client.py, covered by client tests).
# @pytest.mark.parametrize('text, expected', [
#     ('Answer...\nVERDICT: PASS', ('PASS', '')),
#     ('x\nFEEDBACK: rerun step 2 with end=2019-12-31\nVERDICT: REVISE',
#      ('REVISE', 'rerun step 2 with end=2019-12-31')),
#     ('no verdict line at all', ('PASS', '')),
#     ('verdict: revise\nlater VERDICT: PASS', ('PASS', '')),
# ])
# def test_parse_verdict(text, expected):
#     assert pav_workflow.parse_verdict(text) == expected
#
#
# def test_strip_verdict_removes_control_lines():
#     out = pav_workflow.strip_verdict('**Answer:** 42\nFEEDBACK: x\nVERDICT: REVISE')
#     assert out == '**Answer:** 42'


def test_prompt_names_match_registered_prompts(backend):
    server = create_server(backend, Settings())
    assert {p.name for p in asyncio.run(server.list_prompts())} == set(PROMPT_NAMES)


def test_pav_prompts_render_through_mcp(backend):
    server = create_server(backend, Settings())
    plan = _prompt_text(server, 'pav_plan', {'research_question': 'Monthly spend for user 1 in 2019',
                                             'max_steps': '3'})
    assert 'Planner Agent' in plan and 'Monthly spend for user 1 in 2019' in plan
    assert 'Never plan more than 3 tool calls' in plan

    act = _prompt_text(server, 'pav_act', {'research_question': 'q', 'plan': 'STEP-1 search'})
    assert 'Act Agent' in act and 'STEP-1 search' in act and 'None.' in act  # empty feedback

    verify = _prompt_text(server, 'pav_verify', {'research_question': 'q', 'plan': 'p',
                                                 'findings': 'FINDING-A', 'max_steps': 'oops'})
    assert 'Verify Agent' in verify and 'FINDING-A' in verify
    assert 'at most 4 tool calls' in verify  # invalid max_steps falls back to the default
    assert 'VERDICT: PASS' in verify and 'VERDICT: REVISE' in verify


def test_fraud_referral_link_and_fallback(monkeypatch):
    monkeypatch.setenv('DATABRICKS_HOST', 'dbc-test.cloud.databricks.com/')
    assert pav_workflow.fraud_referral() == (
        'the Fraud Detection Genie space "Card Transaction Insights and Fraud Detection Agent" '
        f'(https://dbc-test.cloud.databricks.com/genie/rooms/{FRAUD_SPACE})')
    monkeypatch.delenv('DATABRICKS_HOST')
    assert f'(Genie space ID {FRAUD_SPACE})' in pav_workflow.fraud_referral()
    monkeypatch.setenv('FRAUD_GENIE_SPACE_ID', 'abc123')
    assert '(Genie space ID abc123)' in pav_workflow.fraud_referral()


@pytest.mark.parametrize('stage', STAGES)
def test_every_stage_refers_only_recorded_label_questions_to_genie(stage, monkeypatch):
    monkeypatch.setenv('DATABRICKS_HOST', 'https://dbc-test.cloud.databricks.com')
    args = {'research_question': 'How many fraudulent transactions in 2019?', 'plan': 'p',
            'feedback': '', 'findings': 'f'}
    out = pav_workflow.render(stage, **{k: args[k] for k in EXPECTED_VARS[stage] & set(args)})
    assert f'https://dbc-test.cloud.databricks.com/genie/rooms/{FRAUD_SPACE}' in out
    # Recorded-label questions still go to Genie; fraud assessment stays in this app.
    assert 'recorded' in out.lower() and 'run_risk_sql' in out and '{{' not in out


def test_investigate_transactions_starter(backend):
    text = _prompt_text(create_server(backend, Settings()), 'investigate_transactions',
                        {'research_question': 'Top MCCs in 2018'})
    assert 'Top MCCs in 2018' in text
    for stage in ('PLAN', 'ACT', 'VERIFY'):
        assert f'**{stage}:**' in text


def test_entity_prompts_keep_their_contract(backend):
    server = create_server(backend, Settings())
    review = _prompt_text(server, 'review_transaction', {'transaction_id': 'TX0001'})
    assert 'TX0001' in review and 'generate_transaction_report' in review and 'VERIFY' in review
    profile = _prompt_text(server, 'profile_cardholder', {'user_id': '3'})
    assert 'get_cardholder_profile' in profile and 'merchant_relationships' in profile
