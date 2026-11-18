"""Project-layout and data-source configuration tests.

These are static checks: they read files and inspect the folder structure,
and never run notebooks or connect to Databricks. They confirm that:

* The preparation notebook reads the Unity Catalog source table
  (``workspace.default.card_transaction``) with ``spark.read.table`` and does
  not load CSV files. No local dataset is needed.
* The code follows the GSA MCP template layout (``src/fraud_transaction_mcp``),
  and the old flat ``src/`` modules are gone.
"""
from pathlib import Path


def test_prep_notebook_reads_uc_source():
    """``01_prepare_card_transaction_mcp.py`` reads ``workspace.default.card_transaction`` from UC.

    Checks ``SOURCE_TABLE``, ``spark.read.table(SOURCE_TABLE)`` and that no ``.csv(`` is used.
    """
    root = Path(__file__).resolve().parents[1]
    text = (root / 'databricks/notebooks/01_prepare_card_transaction_mcp.py').read_text()
    assert "SOURCE_TABLE = 'workspace.default.card_transaction'" in text
    assert "raw = spark.read.table(SOURCE_TABLE)" in text
    assert '.csv(' not in text


def test_template_package_layout():
    """The server is the ``src/fraud_transaction_mcp`` package; old flat modules are gone."""
    root = Path(__file__).resolve().parents[1]
    pkg = root / 'src/fraud_transaction_mcp'
    for rel in ('app.py', 'config.py', 'routes.py', 'models.py', 'backend.py',
                'tools/__init__.py', 'prompts/__init__.py', 'resources/__init__.py'):
        assert (pkg / rel).exists(), rel
    for legacy in ('server.py', 'databricks_server.py', 'databricks_backend.py', 'card_transaction_mcp'):
        assert not (root / 'src' / legacy).exists(), legacy
    assert (root / 'app.yaml').read_text() == (root / 'deploy/databricks/app.yaml').read_text()
