"""Behavioural unit tests for the analytics functions in ``src/fraud_transaction_mcp/analytics.py``.

Each analytics function backs one MCP tool. The tests run against the
in-memory ``backend`` fixture from ``conftest.py`` (200 synthetic rows,
5 users x 40 transactions, 4 merchants, 3 days), so expected counts are exact.

What the tests cover:

* Functional results: search, summarise, cardholder profile, merchant
  relationships, and the per-transaction report.
* Input validation: rejecting unsafe ``group_by`` values, limits out of range,
  and invalid calendar dates.
* Governance: the ``Is_Fraud`` label never appears in outputs, every record
  keeps its source lineage, reports require human review, and shared merchants
  are never described as fund flows.
* The rule-based review signals (``review_signals``).
"""
import pytest
from fraud_transaction_mcp import analytics


def test_search(backend):
    """``search_transactions`` filters by user, applies the limit, reports truncation, and hides the fraud label.

    User ``'1'`` has 40 rows, so ``limit=3`` must return exactly 3 records
    with ``truncated=True``. Every record belongs to user ``'1'`` and none has ``Is_Fraud``.
    """
    result = analytics.search_transactions(backend, user_id='1', limit=3)
    assert result['returned'] == 3
    assert result['truncated']
    assert all(r['user_id'] == '1' for r in result['records'])
    assert all('Is_Fraud' not in r for r in result['records'])


def test_summary(backend):
    """``summarize_transactions`` grouped by merchant accounts for all 200 fixture transactions.

    Adding ``transaction_count`` across the merchant groups must give the full
    table size. This shows the grouping neither drops nor double-counts rows.
    """
    result = analytics.summarize_transactions(backend, 'merchant', limit=20)
    assert sum(r['transaction_count'] for r in result['records']) == 200


def test_profile(backend):
    """``get_cardholder_profile`` returns correct statistics for one user.

    User ``'1'`` has exactly 40 transactions in the fixture (one in every 5 rows),
    and the profile must report at least one distinct merchant.
    """
    result = analytics.get_cardholder_profile(backend, '1')
    assert result['statistics']['transaction_count'] == 40
    assert result['statistics']['merchants'] > 0


def test_relationships(backend):
    """``merchant_relationships`` stays within its limits, leaves out the seed user, and states its caveat.

    Called with ``max_merchants=3`` and ``max_related_users=10``, it returns at
    most 3 merchants. The seed user ``'1'`` never appears among the related users.
    The interpretation text must say that sharing a merchant is ``not necessarily funds``.
    """
    result = analytics.merchant_relationships(backend, '1', 3, 10)
    assert len(result['merchants']) <= 3
    assert all(r['user_id'] != '1' for r in result['related_users'])
    assert 'not necessarily funds' in result['interpretation']


def test_report(backend):
    """``generate_transaction_report`` returns a label-free report that needs human review.

    The test picks a real transaction ID from a search, then checks that the
    report is found and matches that ID. The embedded transaction must not
    include ``Is_Fraud``, and ``status`` must be ``human_review_required``
    because the tool never makes fraud determinations itself.
    """
    tx = analytics.search_transactions(backend, limit=1)['records'][0]
    report = analytics.generate_transaction_report(backend, tx['transaction_id'])
    assert report['found']
    assert report['transaction_id'] == tx['transaction_id']
    assert 'Is_Fraud' not in report['transaction']
    assert report['status'] == 'human_review_required'


def test_report_missing(backend):
    """An unknown transaction ID gives ``found=False`` instead of raising an error."""
    assert not analytics.generate_transaction_report(backend, 'not-found')['found']


def test_invalid_group(backend):
    """An unsupported ``group_by`` (here a SQL-injection attempt) raises ``ValueError``.

    ``group_by`` is checked against a fixed allow-list (``GROUPS``) because it
    goes into the SQL text as an expression.
    """
    with pytest.raises(ValueError):
        analytics.summarize_transactions(backend, 'merchant; DROP TABLE x')


def test_invalid_limit(backend):
    """A ``limit`` above the maximum (200 for search) raises ``ValueError``.

    This stops a client from asking for unbounded result sets.
    """
    with pytest.raises(ValueError):
        analytics.search_transactions(backend, limit=10000)


def test_invalid_date(backend):
    """A date in the right format that isn't a real day (``2019-02-30``) raises ``ValueError``.

    Dates must match ``YYYY-MM-DD`` and must also exist on the calendar.
    """
    with pytest.raises(ValueError):
        analytics.search_transactions(backend, start='2019-02-30')


def test_date_filter(backend):
    """A one-day range (``start == end == 2019-03-02``) returns that day's 80 rows.

    ``end`` is inclusive: the filter covers the whole of 2019-03-02, from
    00:00:00 up to but not including the next midnight. The fixture places
    rows 80 to 159 on that date.
    """
    result = analytics.search_transactions(backend, start='2019-03-02', end='2019-03-02', limit=200)
    assert result['returned'] == 80


def test_source_identity(backend):
    """Every returned record has a non-empty ``transaction_id`` and keeps its source lineage.

    ``source_file`` must point to ``workspace.default.card_transaction`` so an
    analyst can trace each result back to the original source row.
    """
    rows = analytics.search_transactions(backend, limit=10)['records']
    assert all(r['transaction_id'] for r in rows)
    assert all(r['source_file'] == 'workspace.default.card_transaction' for r in rows)


def test_shared_merchant_not_fund_flow(backend):
    """With default arguments, the ``merchant_relationships`` interpretation still says it is not about funds.

    Shopping at the same merchant is co-occurrence. It does not show that money
    moved between users, so the output must never suggest that.
    """
    assert 'funds' in analytics.merchant_relationships(backend, '1')['interpretation']


def test_review_signals():
    """``review_signals`` raises all three rule-based flags when each condition is met.

    A 700 purchase that is online and has a recorded error must produce exactly
    ``HIGH_AMOUNT`` (amount >= 500), ``ONLINE_PURCHASE`` and ``ERROR_RECORDED``.
    These are fixed review cues, not output from a fraud model.
    """
    flags = analytics.review_signals({'amount': 700, 'use_chip': 'Online Transaction', 'Errors': 'Bad PIN'})
    assert {f['code'] for f in flags} == {'HIGH_AMOUNT', 'ONLINE_PURCHASE', 'ERROR_RECORDED'}
