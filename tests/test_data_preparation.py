"""Static checks on the Databricks data-preparation notebooks.

These tests read the source of the notebooks under ``databricks/notebooks/``
without running them, and check key design decisions:

* ``00_initialize_transaction_ids_mcp.py`` adds stable transaction IDs with an
  identity column, keeps the exact source schema, and is non-destructive.
* ``01_prepare_card_transaction_mcp.py`` reads the UC source table, keeps the
  fraud label out of the clean table, avoids content-hash IDs, and does not
  rebuild derived tables by default.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_source_exact_schema_and_id_initialization():
    """The ID-initialisation notebook uses an identity column, keeps the source schema, and never drops tables.

    Expected in ``00_initialize_transaction_ids_mcp.py``:

    * ``GENERATED ALWAYS AS IDENTITY``, so IDs are stable and assigned by Delta.
    * The exact source column names, including ``'Errors','Is_Fraud'``.
    * ``exceptAll``, used to confirm that no rows were lost or changed.
    * No ``DROP TABLE`` statement in any casing.
    """
    text = (ROOT / 'databricks/notebooks/00_initialize_transaction_ids_mcp.py').read_text()
    assert 'GENERATED ALWAYS AS IDENTITY' in text
    assert "'Errors','Is_Fraud'" in text
    assert 'exceptAll' in text
    assert 'DROP TABLE' not in text.upper()


def test_preparation_does_not_use_content_hash_or_expose_labels():
    """The preparation notebook reads UC, avoids hash-based IDs, and keeps ``Is_Fraud`` out of the clean table.

    Expected in ``01_prepare_card_transaction_mcp.py``:

    * It reads ``spark.read.table(SOURCE_TABLE)``.
    * It uses no ``_content_hash`` and no ``sha2(``, so IDs come from the
      identity column instead of a hash of row content.
    * It references ``Errors`` and ``Is_Fraud`` through the quoting helper
      ``q()``, so the column names stay exactly as in the source.
    * It asserts that ``'Is_Fraud'`` is not among ``CLEAN_TABLE`` columns.
    * ``REBUILD_DERIVED_TABLES = False`` by default, so reruns don't overwrite
      downstream tables by accident.
    """
    text = (ROOT / 'databricks/notebooks/01_prepare_card_transaction_mcp.py').read_text()
    assert 'spark.read.table(SOURCE_TABLE)' in text
    assert '_content_hash' not in text
    assert 'sha2(' not in text
    assert "F.col(q('Errors'))" in text
    assert "F.col(q('Is_Fraud'))" in text
    assert "'Is_Fraud' not in spark.table(CLEAN_TABLE).columns" in text
    assert 'REBUILD_DERIVED_TABLES = False' in text
