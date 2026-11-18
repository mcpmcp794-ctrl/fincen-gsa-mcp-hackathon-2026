"""Shared pytest fixtures for the Financial Transaction Analysis MCP test suite.

This module provides an in-memory SQLite stand-in for the Databricks SQL
backend so the analytics functions in ``src/fraud_transaction_mcp/analytics.py`` can be tested
quickly and deterministically, with no Databricks credentials or network access.

The ``MemoryBackend`` honours the same contract as ``DatabricksBackend``:
a ``transactions`` attribute naming the table, and a ``query(sql, params)``
method that returns ``list[dict]`` using ``?`` placeholders.

Synthetic fixture data (200 rows in ``card_transaction_clean``):

* ``transaction_id``: ``TX0000`` to ``TX0199`` (unique).
* ``user_id``: ``'1'`` to ``'5'`` assigned round-robin (``i % 5 + 1``), so each
  user has exactly 40 transactions.
* ``card_id``: ``'0'`` or ``'1'`` (``i % 2``); ``card_key`` is ``"<user>::<card>"``.
* ``transaction_time``: 80 rows on 2019-03-01, 80 on 2019-03-02 and 40 on 2019-03-03.
* ``amount``: 1.0 to 200.0 (``i + 1``), so every amount is positive and below
  the 500.0 ``HIGH_AMOUNT`` review threshold.
* ``use_chip``: ``'Online Transaction'`` for every 4th row, otherwise ``'Chip Transaction'``.
* ``merchant_id``: ``M0`` to ``M3`` (``i % 4``); all rows are in Richmond, VA, MCC 5411.
* ``Errors``: ``'Bad PIN'`` on ``TX0009`` only; empty string elsewhere.
* ``source_file``: ``workspace.default.card_transaction``; ``source_row_number`` is 1 to 200.
* There is no ``Is_Fraud`` column, matching the production clean table, which
  excludes the fraud label.
"""
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from datetime import datetime, timedelta

# Make the fraud_transaction_mcp package (src/ layout) importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fraud_transaction_mcp.analytics import FIELDS  # noqa: E402

class MemoryBackend:
    """In-memory SQLite backend that mimics ``DatabricksBackend`` for unit tests.

    Builds a ``card_transaction_clean`` table with the same columns as
    ``fraud_transaction_mcp.analytics.FIELDS`` and fills it with 200 deterministic synthetic
    rows (see the module docstring for the data layout).
    """
    transactions = "card_transaction_clean"
    def __init__(self):
        """Create the in-memory database, the clean transaction table, and the 200 seed rows."""
        # Server tools run in worker threads (app._run_sync_tools_in_threads), so the
        # connection is shared across threads and guarded by a lock.
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.lock = threading.Lock()
        self.conn.execute("""CREATE TABLE card_transaction_clean (
            transaction_id TEXT PRIMARY KEY, user_id TEXT, card_id TEXT, card_key TEXT,
            transaction_time TEXT, amount REAL, use_chip TEXT, merchant_id TEXT,
            merchant_city TEXT, merchant_state TEXT, zip_code TEXT, mcc TEXT,
            Errors TEXT, source_file TEXT, source_row_number INTEGER)""")
        rows = []
        for i in range(200):
            user = str((i % 5) + 1)
            card = str(i % 2)
            ts = datetime(2019, 3, 1) + timedelta(days=i // 80)
            rows.append((f"TX{i:04d}", user, card, f"{user}::{card}",
                         ts.strftime("%Y-%m-%d %H:%M:%S"), float(i + 1),
                         "Online Transaction" if i % 4 == 0 else "Chip Transaction",
                         f"M{i % 4}", "Richmond", "VA", "23220", "5411",
                         "Bad PIN" if i == 9 else "", "workspace.default.card_transaction", i + 1))
        self.conn.executemany(f"INSERT INTO card_transaction_clean VALUES ({','.join('?' for _ in FIELDS)})", rows)
        self.conn.commit()
    def query(self, statement, params=()):
        """Run a parameterised SQL statement and return the rows as a list of column-name-keyed dicts."""
        with self.lock:
            cur = self.conn.execute(statement, params)
            return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]

@pytest.fixture
def backend():
    """Provide a fresh ``MemoryBackend`` for each test and close its connection afterwards.

    Each test gets its own isolated database, so tests cannot affect each other.
    """
    b = MemoryBackend()
    yield b
    b.conn.close()
