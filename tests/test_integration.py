"""Opt-in live Databricks integration. Run only with RUN_DATABRICKS_INTEGRATION=1.

Unlike the other tests, which use the in-memory SQLite fixture, this module
connects to a real Databricks SQL Warehouse through ``DatabricksBackend``. The
whole module is skipped unless ``RUN_DATABRICKS_INTEGRATION=1`` is set, so the
default test run never needs credentials or network access.

Prerequisites: approved credentials (app OAuth identity or ``DATABRICKS_TOKEN``),
``DATABRICKS_SERVER_HOSTNAME``/``DATABRICKS_HOST``, and
``DATABRICKS_HTTP_PATH``/``DATABRICKS_WAREHOUSE_ID``.
"""
import os
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_DATABRICKS_INTEGRATION') != '1',
    reason='Requires approved Databricks credentials and SQL Warehouse access')


def test_clean_table_and_query():
    """End-to-end smoke test: connect, query the configured clean table, and get a valid row count.

    Passing shows that authentication, warehouse resolution, table access and
    row-to-dict conversion all work against the live workspace.
    """
    from fraud_transaction_mcp.backend import DatabricksBackend
    b = DatabricksBackend()
    rows = b.query(f'SELECT COUNT(*) AS n FROM {b.transactions}')
    assert rows and rows[0]['n'] >= 0
