"""Evaluation-only metrics: correct confusion matrix, aggregate output only."""
import asyncio
import json

import pytest

from fraud_transaction_mcp import evaluation_metrics
from fraud_transaction_mcp.app import create_server
from fraud_transaction_mcp.config import Settings

LABELS = 'card_transaction_evaluation_labels'


@pytest.fixture
def labelled(backend):
    # Fraud = every 10th row (TX0000, TX0010, ...). 20 of 200 rows.
    backend.conn.execute(f'CREATE TABLE {LABELS} (transaction_id TEXT, Is_Fraud INTEGER)')
    backend.conn.executemany(f'INSERT INTO {LABELS} VALUES (?, ?)',
                             [(f'TX{i:04d}', 1 if i % 10 == 0 else 0) for i in range(200)])
    return backend


def _expected(threshold):
    tp = fp = fn = tn = 0
    for i in range(200):
        flagged = (i + 1) >= threshold or i % 4 == 0 or i == 9   # amount / online / error
        fraud = i % 10 == 0
        tp += flagged and fraud; fp += flagged and not fraud
        fn += (not flagged) and fraud; tn += (not flagged) and not fraud
    return {'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn}


@pytest.mark.parametrize('threshold', [500.0, 150.0])
def test_confusion_matrix_matches_manual_count(labelled, threshold):
    out = evaluation_metrics.evaluate_review_signals(labelled, LABELS, '2019-03-01', '2019-03-03', threshold)
    assert out['confusion_matrix'] == _expected(threshold)
    assert out['transactions'] == 200 and out['fraud_rate'] == 0.1


def test_date_filter_applies(labelled):
    out = evaluation_metrics.evaluate_review_signals(labelled, LABELS, '2019-03-03', '2019-03-03')
    assert out['transactions'] == 40


def test_output_has_no_per_transaction_data(labelled):
    out = evaluation_metrics.evaluate_review_signals(labelled, LABELS, '2019-03-01', '2019-03-03')
    assert 'TX0' not in json.dumps(out)


@pytest.mark.parametrize('args', [(None, '2019-03-03'), ('2019-03-01', None)])
def test_requires_date_range(labelled, args):
    with pytest.raises(ValueError, match='required'):
        evaluation_metrics.evaluate_review_signals(labelled, LABELS, *args)


def test_rejects_unsafe_label_table(labelled):
    with pytest.raises(ValueError, match='Unsafe'):
        evaluation_metrics.evaluate_review_signals(labelled, 'x; DROP TABLE y', '2019-03-01', '2019-03-02')


def test_tool_through_mcp_when_enabled(labelled):
    from mcp.shared.memory import create_connected_server_and_client_session
    server = create_server(labelled, Settings(enable_evaluation_tools=True, evaluation_label_table=LABELS))

    async def run():
        async with create_connected_server_and_client_session(server._mcp_server) as s:
            return await s.call_tool('evaluate_review_signals', {'start': '2019-03-01', 'end': '2019-03-03'})
    r = asyncio.run(run())
    assert not r.isError
    data = r.structuredContent or json.loads(r.content[0].text)
    assert data['confusion_matrix'] == _expected(500.0)
    assert 'TX0' not in json.dumps(data)
