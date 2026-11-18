"""Static checks on the MCP server surface and fraud-label isolation.

These tests read source files without starting the server. They confirm that:

* every public analysis tool has its own module under ``tools/`` (one tool per file),
* the entry point serves ``streamable-http`` (needed by Databricks Apps),
* analysis code never refers to evaluation labels or ``Is_Fraud``; only the
  evaluation-only module and its tool may.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / 'src/fraud_transaction_mcp'
TOOLS = ['search_transactions', 'summarize_transactions', 'get_cardholder_profile',
         'merchant_relationships', 'generate_transaction_report', 'ask_transactions',
         'get_table_schema', 'get_risk_scoring_guidance', 'run_risk_sql']
EVALUATION_ONLY = {'evaluation_metrics.py', 'evaluate_review_signals.py',
                   'evaluate_risk_indicators.py', 'config.py'}


def test_tool_names_and_server_entrypoint():
    """Each tool is defined in ``tools/<name>.py``; renaming one would break MCP clients."""
    for tool in TOOLS:
        assert f'def {tool}(' in (PKG / f'tools/{tool}.py').read_text()
    assert 'streamable-http' in (PKG / 'app.py').read_text()


def test_analysis_code_never_references_labels():
    """Only evaluation-only modules (and config naming the label table) mention labels."""
    for path in PKG.rglob('*.py'):
        if path.name in EVALUATION_ONLY:
            continue
        text = path.read_text()
        if path.name == 'sql_guard.py':
            # The guard names the terms only in order to reject them.
            text = text.replace('is_fraud|evaluation_label', '')
        # The setting *name* that routes the label table to the evaluation tool is allowed.
        text = text.replace('evaluation_label_table', '')
        assert 'evaluation_label' not in text.lower(), path
        assert 'is_fraud' not in text.lower(), path


def test_analytics_never_query_evaluation_labels():
    text = (PKG / 'analytics.py').read_text()
    assert 'evaluation_labels' not in text
    assert 'Is_Fraud' not in text
