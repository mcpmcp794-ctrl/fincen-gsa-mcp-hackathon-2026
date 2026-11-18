"""Static governance checks on the Genie views SQL (``databricks/sql/02_genie_views.sql``).

The views in that script are what the Genie agents query. These tests read
the SQL text and check that the views are built only from the clean, label-free
table, so Genie users can never see fraud ground-truth labels.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_genie_views_use_only_clean_data():
    """The Genie views read ``card_transaction_mcp_clean`` and never use evaluation labels or ``Is_Fraud``.

    SQL comment lines (starting with ``--``) are ignored. In the executable SQL
    that remains, every ``FROM`` reads ``workspace.default.card_transaction_mcp_clean``,
    every view uses the ``card_transaction_mcp_`` prefix, and neither ``evaluation_labels``
    nor ``Is_Fraud`` appears.
    """
    sql = (ROOT / 'databricks/sql/02_genie_views.sql').read_text()
    code = '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
    sources = re.findall(r'\bFROM\s+([\w.]+)', code, re.I)
    views = re.findall(r'\bVIEW\s+([\w.]+)', code, re.I)
    assert sources and set(sources) == {'workspace.default.card_transaction_mcp_clean'}
    assert views and all(v.startswith('workspace.default.card_transaction_mcp_') for v in views)
    assert 'evaluation_labels' not in code.lower()
    assert 'is_fraud' not in code.lower()
