"""Agent-driven risk scoring: guidance, guarded execution of agent SQL, evaluation."""
import asyncio
import json

import pytest
import sqlglot
from mcp.shared.memory import create_connected_server_and_client_session

from fraud_transaction_mcp import evaluation_metrics, risk_scoring
from fraud_transaction_mcp.app import create_server
from fraud_transaction_mcp.config import Settings

SUM = '(amount_anomaly + velocity_anomaly + location_channel_change + merchant_novelty)'


def _scoring_sql(divisor='4.0', where="WHERE card_key = '1::0'"):
    """SQLite-compatible stand-in for SQL an Actor would write (merchant novelty only)."""
    return (
        'WITH h AS (SELECT transaction_id, card_key, transaction_time, amount, merchant_id, '
        'ROW_NUMBER() OVER (PARTITION BY card_key, merchant_id ORDER BY transaction_time, transaction_id) AS m_seq, '
        'COUNT(*) OVER (PARTITION BY card_key ORDER BY transaction_time, transaction_id '
        'ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_n '
        f'FROM card_transaction_clean {where}), '
        's AS (SELECT transaction_id, card_key, transaction_time, amount, 0 AS amount_anomaly, '
        '0 AS velocity_anomaly, 0 AS location_channel_change, '
        'CASE WHEN prior_n > 0 AND m_seq = 1 THEN 1 ELSE 0 END AS merchant_novelty FROM h) '
        f'SELECT *, {SUM} / {divisor} AS risk_score, '
        f"CASE WHEN {SUM} / 4.0 >= 0.75 THEN 'high' WHEN {SUM} / 4.0 >= 0.5 THEN 'medium' "
        "ELSE 'low' END AS risk_band FROM s ORDER BY risk_score DESC, transaction_time DESC")


def _call(backend, tool, args, settings=None):
    async def run():
        server = create_server(backend, settings or Settings())
        async with create_connected_server_and_client_session(server._mcp_server) as s:
            r = await s.call_tool(tool, args)
            if r.isError:
                return True, r.content[0].text
            return False, (r.structuredContent or json.loads(r.content[0].text))
    return asyncio.run(run())


def test_parameters_come_from_markdown():
    p = risk_scoring.parameters()
    assert p['amount_percentile'] == 0.95 and p['velocity_min_count'] == 5
    assert p['band_medium'] == 0.5 and p['band_high'] == 0.75
    assert set(p) == risk_scoring.PARAMETER_KEYS


def test_parameters_reject_missing_block():
    with pytest.raises(ValueError):
        risk_scoring.parameters('# no block here')


@pytest.mark.parametrize('score,expected', [(0, 'low'), (0.25, 'low'), (0.5, 'medium'),
                                            (0.75, 'high'), (1.0, 'high')])
def test_bands(score, expected):
    assert risk_scoring.band(score) == expected


def test_guidance_tool_computes_nothing_and_lists_four_indicators(backend):
    err, out = _call(backend, 'get_risk_scoring_guidance', {})
    assert not err
    assert out['indicators'] == ['amount_anomaly', 'velocity_anomaly',
                                 'location_channel_change', 'merchant_novelty']
    assert out['score_formula'].endswith('/ 4.0')
    assert out['execution_tool'] == 'run_risk_sql' and out['table'] == backend.transactions
    assert 'records' not in out and 'Point-in-time' in out['definitions_markdown']
    assert not any('fraud' in c['name'].lower() for c in out['columns'])


def test_guidance_rejects_unknown_method(backend):
    err, _ = _call(backend, 'get_risk_scoring_guidance', {'method': 'zscore'})
    assert err


def test_run_risk_sql_scores_agent_sql(backend):
    err, out = _call(backend, 'run_risk_sql', {'sql': _scoring_sql(), 'limit': 50})
    assert not err, out
    assert out['returned'] == 20 and out['truncated'] is False
    assert out['missing_required_columns'] == []
    assert out['score_check'] == {'checked': 20, 'inconsistent': 0, 'examples': []}
    flagged = [r for r in out['records'] if r['merchant_novelty'] == 1]
    # card 1::0 = TX0000 (M0), TX0010 (M2), TX0020 (M0), ...: only TX0010 is a new merchant.
    assert [r['transaction_id'] for r in flagged] == ['TX0010']
    assert out['band_counts'] == {'low': 20}


def test_guidance_accepts_method_aliases(backend):
    err, out = _call(backend, 'get_risk_scoring_guidance', {'method': 'rule_based_card_transaction_risk'})
    assert not err and out['method'] == 'rule_based'


def test_run_risk_sql_summary_comes_first_and_is_complete(backend):
    out = risk_scoring.run_risk_sql(backend, _scoring_sql(), 50)
    assert list(out)[0] == 'summary' and list(out)[-1] == 'sql'
    s = out['summary']
    assert s['complete'] is True and s['rows_scored'] == 20
    assert s['indicator_counts'] == {'amount_anomaly': 0, 'velocity_anomaly': 0,
                                     'location_channel_change': 0, 'merchant_novelty': 1}
    assert s['flagged_count'] == 1 and s['flagged'][0]['transaction_id'] == 'TX0010'
    assert s['max_risk_score'] == 0.25
    # The summary must survive the client's evidence truncation (3000 chars).
    assert json.dumps(out, default=str).index('"records"') > len(json.dumps(s, default=str))
    assert len(json.dumps(s, default=str)) < 3000


def _ranking_sql(extra=''):
    return (
        'WITH h AS (SELECT user_id, transaction_id, ROW_NUMBER() OVER (PARTITION BY card_key, '
        'merchant_id ORDER BY transaction_time, transaction_id) AS m_seq, COUNT(*) OVER (PARTITION BY '
        'card_key ORDER BY transaction_time, transaction_id ROWS BETWEEN UNBOUNDED PRECEDING AND '
        "1 PRECEDING) AS prior_n FROM card_transaction_clean WHERE transaction_time >= '2019-03-01'), "
        's AS (SELECT user_id, CASE WHEN prior_n > 0 AND m_seq = 1 THEN 1 ELSE 0 END / 4.0 AS r FROM h) '
        'SELECT user_id, COUNT(*) AS transactions_scored, '
        'SUM(CASE WHEN r >= 0.75 THEN 1 ELSE 0 END) AS high_count, '
        'SUM(CASE WHEN r >= 0.5 AND r < 0.75 THEN 1 ELSE 0 END) AS medium_count, '
        f'AVG(r) AS avg_risk_score, MAX(r) AS max_risk_score{extra} FROM s GROUP BY user_id '
        'ORDER BY high_count DESC, medium_count DESC, avg_risk_score DESC LIMIT 5')


def test_run_risk_sql_accepts_population_ranking(backend):
    err, out = _call(backend, 'run_risk_sql', {'sql': _ranking_sql()})
    assert not err, out
    s = out['summary']
    assert s['output'] == 'ranking' and s['entity'] == 'user_id' and s['rows_returned'] == 5
    assert s['missing_ranking_columns'] == [] and s['aggregate_check']['inconsistent'] == 0
    assert sum(r['transactions_scored'] for r in s['ranking']) == 200
    assert all(r['max_risk_score'] == 0.25 for r in s['ranking'])


def test_ranking_detected_even_with_extra_score_columns(backend):
    out = risk_scoring.run_risk_sql(backend, _ranking_sql(', NULL AS risk_score, NULL AS merchant_novelty'), 3)
    s = out['summary']
    assert s['output'] == 'ranking' and s['aggregate_check']['inconsistent'] == 0
    assert s['complete'] is True and s['more_entities_available'] is True and s['rows_returned'] == 3


def test_ranking_reports_missing_columns():
    rows = [{'user_id': '1', 'transactions_scored': 3, 'avg_risk_score': 0.2}]
    b = _FakeBackend(rows)
    b.transactions = 'card_transaction_clean'
    out = risk_scoring.run_risk_sql(
        b, "SELECT user_id FROM card_transaction_clean WHERE user_id = '1'", 5)
    assert out['summary']['missing_ranking_columns'] == ['high_count', 'medium_count', 'max_risk_score']


def test_ranking_flags_inconsistent_aggregates():
    rows = [{'user_id': '1', 'transactions_scored': 2, 'high_count': 2, 'medium_count': 1,
             'avg_risk_score': 0.9, 'max_risk_score': 0.5}]
    b = _FakeBackend(rows)
    b.transactions = 'card_transaction_clean'
    out = risk_scoring.run_risk_sql(
        b, "SELECT user_id FROM card_transaction_clean WHERE user_id = '1'", 5)
    assert out['summary']['aggregate_check']['inconsistent'] == 1


def test_guidance_says_to_answer_assessment_questions_here(backend):
    out = risk_scoring.get_risk_scoring_guidance(backend.transactions)
    assert 'do not refer' in out['use_for'] and 'user_id or card_key' in out['ranking_output_columns']
    assert 'Ranking output' in out['definitions_markdown']


def test_prompts_route_assessment_to_risk_scoring_not_referral():
    from fraud_transaction_mcp.prompts import pav_workflow
    plan = pav_workflow.load_template('plan')
    assert 'Fraud assessment, likelihood or ranking questions are NOT referrals' in plan
    assert 'which transactions are fraudulent, or fraud by any dimension' not in plan
    verify = pav_workflow.load_template('verify')
    assert 'referred such a question elsewhere instead of scoring it' in verify


def test_run_risk_sql_flags_inconsistent_scores(backend):
    out = risk_scoring.run_risk_sql(backend, _scoring_sql(divisor='3.0'), 50)
    assert out['score_check']['inconsistent'] == 1
    assert out['score_check']['examples'][0]['transaction_id'] == 'TX0010'


def test_run_risk_sql_reports_missing_columns(backend):
    sql = "SELECT transaction_id, amount FROM card_transaction_clean WHERE card_key = '1::0'"
    out = risk_scoring.run_risk_sql(backend, sql, 5)
    assert 'risk_score' in out['missing_required_columns'] and out['score_check']['checked'] == 0


@pytest.mark.parametrize('sql', [
    'SELECT * FROM card_transaction_clean',
    "SELECT * FROM card_transaction_clean WHERE card_key = '1::0'; DROP TABLE card_transaction_clean",
    "SELECT * FROM card_transaction_clean WHERE card_key = '1::0' -- note",
    "SELECT transaction_id FROM other_table WHERE card_key = '1::0'",
    "SELECT is_fraud FROM card_transaction_clean WHERE card_key = '1::0'",
    "DELETE FROM card_transaction_clean WHERE card_key = '1::0'",
])
def test_run_risk_sql_rejects_unsafe_or_unscoped(backend, sql):
    err, _ = _call(backend, 'run_risk_sql', {'sql': sql})
    assert err


def test_run_risk_sql_caps_rows(backend):
    out = risk_scoring.run_risk_sql(backend, _scoring_sql(), 5)
    assert out['returned'] == 5 and out['truncated'] is True
    with pytest.raises(ValueError):
        risk_scoring.run_risk_sql(backend, _scoring_sql(), 201)


def test_reference_sql_parses_as_one_databricks_select():
    sql = evaluation_metrics.reference_risk_sql('c.s.t', 'c.s.labels', 100)
    tree = sqlglot.parse(sql, read='databricks')
    assert len(tree) == 1 and sql.count('?') == 4
    assert 'percentile_approx(amount, 0.95)' in sql and 'INTERVAL 24 HOURS' in sql


class _FakeBackend:
    transactions = 'c.s.t'

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, sql, params=()):
        self.calls.append((sql, list(params)))
        return self.rows


def test_evaluate_risk_indicators_aggregates():
    rows = [
        {'risk_score': 0.0, 'n': 900, 'fraud': 1, 'a': 0, 'v': 0, 'l': 0, 'm': 0,
         'a_fraud': 0, 'v_fraud': 0, 'l_fraud': 0, 'm_fraud': 0},
        {'risk_score': 0.25, 'n': 80, 'fraud': 2, 'a': 40, 'v': 0, 'l': 20, 'm': 20,
         'a_fraud': 2, 'v_fraud': 0, 'l_fraud': 0, 'm_fraud': 0},
        {'risk_score': 0.5, 'n': 20, 'fraud': 3, 'a': 20, 'v': 0, 'l': 0, 'm': 20,
         'a_fraud': 3, 'v_fraud': 0, 'l_fraud': 0, 'm_fraud': 3},
    ]
    b = _FakeBackend(rows)
    out = evaluation_metrics.evaluate_risk_indicators(b, 'c.s.labels', '2019-01-01', '2019-01-31', 100)
    assert b.calls[0][1] == ['2019-01-01 00:00:00', '2019-02-01 00:00:00',
                             '2019-02-01 00:00:00', '2019-01-01 00:00:00']
    assert out['transactions'] == 1000 and out['fraud'] == 6 and out['base_fraud_rate'] == 0.006
    assert out['bands']['low'] == {'transactions': 980, 'fraud': 3, 'fraud_rate': 0.003061,
                                   'lift': 0.510167}
    assert out['bands']['medium']['fraud'] == 3
    amt = out['indicators']['amount_anomaly']
    assert (amt['tp'], amt['fp'], amt['fn'], amt['tn']) == (5, 55, 1, 939)
    med = out['thresholds']['medium_or_high']
    assert (med['tp'], med['fp'], med['fn'], med['tn']) == (3, 17, 3, 977)


@pytest.mark.parametrize('args', [
    ('2019-01-01', '2019-03-01', 100),
    ('2019-01-01', '2019-01-31', 0),
    ('2019-01-01', '2019-01-31', 2001),
    (None, '2019-01-31', 100),
])
def test_evaluate_risk_indicators_rejects_bad_args(args):
    with pytest.raises(ValueError):
        evaluation_metrics.evaluate_risk_indicators(_FakeBackend([]), 'c.s.labels', *args)


def test_risk_tools_never_reference_labels():
    from pathlib import Path
    pkg = Path(risk_scoring.__file__).parent
    for f in ('risk_scoring.py', 'risk_indicators.md', 'tools/run_risk_sql.py',
              'tools/get_risk_scoring_guidance.py'):
        text = (pkg / f).read_text().lower()
        assert 'is_fraud' not in text and 'evaluation_label' not in text, f
