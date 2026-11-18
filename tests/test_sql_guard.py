"""sqlglot-based validation of LLM-generated SQL (merged from transformer-source-v6)."""
import pytest

from fraud_transaction_mcp.nl_sql import validate_sql

T = 'workspace.default.card_transaction_clean'

ALLOWED = [
    f'SELECT COUNT(*) FROM {T}',
    f'SELECT merchant_state, SUM(amount) AS s FROM {T} GROUP BY merchant_state ORDER BY s DESC LIMIT 5;',
    f'WITH m AS (SELECT user_id, COUNT(*) n FROM {T} GROUP BY user_id) SELECT * FROM m ORDER BY n DESC LIMIT 3',
    f'SELECT * FROM {T} WHERE amount > (SELECT AVG(amount) FROM {T}) LIMIT 10',
    f'select count(*) from WORKSPACE.default.Card_Transaction_Clean',
]

REJECTED = [
    ('', 'Empty'),
    (f'SELECT * FROM {T}; DROP TABLE x', 'single statement'),
    (f'SELECT * FROM {T} -- hi', 'comments'),
    (f'DELETE FROM {T}', 'Only SELECT'),
    (f'SELECT * FROM {T} UNION SELECT * FROM {T}', 'Only SELECT'),
    ('SELECT * FROM workspace.default.card_transaction', 'not allowed'),
    ('SELECT * FROM workspace.default.card_transaction_evaluation_labels', 'Fraud labels'),
    (f'SELECT c.*, l.Is_Fraud FROM {T} c JOIN other.s.labels l ON 1=1', 'Fraud labels'),
    (f'SELECT * FROM {T} c JOIN workspace.default.secret_table s ON 1=1', 'not allowed'),
    ('SELECT * FROM card_transaction_clean', 'not allowed'),
    ("SELECT * FROM read_files('/Volumes/x')", 'disallowed keyword'),
    ('SELECT 1', f'must read from {T}'),
    (f'WITH x AS (SELECT * FROM other_db.t) SELECT * FROM x', 'not allowed'),
]


@pytest.mark.parametrize('sql', ALLOWED)
def test_allows_read_only_selects(sql):
    assert validate_sql(sql, T) == sql.strip().rstrip(';').strip()


@pytest.mark.parametrize('sql,message', REJECTED)
def test_rejects_unsafe_sql(sql, message):
    with pytest.raises(ValueError, match=message):
        validate_sql(sql, T)
