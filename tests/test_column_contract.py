"""Column contract tests for the fields the analytics layer selects and exposes.

``fraud_transaction_mcp.analytics.FIELDS`` is the single list of columns that MCP tools return.
These tests pin down two parts of that contract:

* The source error column is spelled ``Errors`` (capital E), matching the
  Databricks table, and the review-signal logic reads that exact key.
* The fraud ground-truth label (``Is_Fraud`` in any casing) is never part of
  the exposed fields, so it can't leak to an agent or user.
"""
from fraud_transaction_mcp.analytics import FIELDS, review_signals

def test_errors_field_is_capitalized():
    """``FIELDS`` contains ``Errors`` exactly as spelled in the source table, not a lower-case ``errors``."""
    assert "Errors" in FIELDS
    assert "errors" not in FIELDS

def test_label_is_restricted():
    """The fraud label is not in ``FIELDS`` in either casing (``Is_Fraud`` or ``is_fraud``).

    Leaving the label out stops tools from revealing the answer key that is
    kept back for evaluation.
    """
    assert "Is_Fraud" not in FIELDS
    assert "is_fraud" not in FIELDS

def test_error_signal():
    """A non-empty ``Errors`` value triggers the ``ERROR_RECORDED`` flag, even for a small amount.

    This confirms that ``review_signals`` reads the capitalised ``Errors`` key
    used throughout the column contract.
    """
    codes = {f["code"] for f in review_signals({"amount": 12, "Errors": "Bad PIN"})}
    assert "ERROR_RECORDED" in codes
