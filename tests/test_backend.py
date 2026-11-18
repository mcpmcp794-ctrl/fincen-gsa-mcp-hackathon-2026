"""Unit tests for the helper functions in ``src/fraud_transaction_mcp/backend.py``.

These tests check the two pure helpers behind ``DatabricksBackend`` without
opening a real Databricks SQL connection:

* ``_table_name()`` reads the table to query from ``config.Settings.card_transaction_table``
  (env ``CARD_TRANSACTION_TABLE``), and must reject values that could inject SQL.
* ``_json_value()`` converts values returned by the SQL connector (``Decimal``,
  ``datetime``) into JSON-serialisable Python types for MCP tool responses.
"""
from fraud_transaction_mcp.backend import ConnectionPool, DatabricksBackend, _table_name, _json_value
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime
import time
import pytest


def test_table_default(monkeypatch):
    """With ``CARD_TRANSACTION_TABLE`` unset, the backend defaults to the clean (label-free) table.

    The default must be ``workspace.default.card_transaction_mcp_clean`` so the MCP
    server never reads the raw table that contains the ``Is_Fraud`` label.
    """
    monkeypatch.delenv('CARD_TRANSACTION_TABLE',raising=False)
    assert _table_name()=='workspace.default.card_transaction_mcp_clean'


def test_table_rejects_sql(monkeypatch):
    """A table name that contains SQL (``'transactions; DROP TABLE x'``) raises ``ValueError``.

    The table name goes into the SQL text as an identifier and can't be a bound
    parameter, so it must match ``catalog.schema.table`` exactly. This guards against SQL injection.
    """
    monkeypatch.setenv('CARD_TRANSACTION_TABLE','transactions; DROP TABLE x')
    with pytest.raises(ValueError):
        _table_name()


def test_json_types():
    """``_json_value`` turns ``Decimal`` into ``float`` and ``datetime`` into an ISO-8601 string.

    The SQL connector returns these types, and JSON can't encode them
    directly. Without this conversion, MCP tool responses would fail to serialise.
    """
    assert _json_value(Decimal('12.20')) == 12.2
    assert _json_value(datetime(2020,1,2,3,4)).startswith('2020-01-02')


# --------------------------------------------------------------------------
# Persistent connection pool (no real warehouse: fake connections)
# --------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description = conn, [('n',)]
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def execute(self, statement, parameters=None):
        self.conn.executed.append(statement)
        if self.conn.fail:
            raise self.conn.fail.pop(0)
        time.sleep(self.conn.delay)
    def fetchmany(self, n):
        return [(1,)]


class FakeConn:
    def __init__(self, delay=0.0, fail=None):
        self.delay, self.fail, self.executed, self.closed = delay, list(fail or []), [], False
    def cursor(self):
        return FakeCursor(self)
    def close(self):
        self.closed = True


def _backend(max_size=4, delay=0.0, fail_first=None):
    conns = []
    def connect():
        conns.append(FakeConn(delay, fail_first if not conns else None))
        return conns[-1]
    b = DatabricksBackend(pool=ConnectionPool(connect, max_size=max_size))
    return b, conns


def test_sequential_queries_reuse_one_connection():
    """A PAV run's tool calls (one after another) all use the same open connection."""
    b, conns = _backend()
    for _ in range(5):
        assert b.query('SELECT 1') == [{'n': 1}]
    assert len(conns) == 1 and len(conns[0].executed) == 5 and not conns[0].closed


def test_concurrent_queries_open_at_most_pool_size():
    """Parallel tool calls get their own connection, capped at SQL_POOL_SIZE, then reused."""
    b, conns = _backend(max_size=2, delay=0.2)
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda _: b.query('SELECT 1'), range(4)))
    assert len(conns) == 2
    b.query('SELECT 1')
    assert len(conns) == 2  # no new connection afterwards


def test_pool_size_one_is_a_single_connection(monkeypatch):
    monkeypatch.setenv('SQL_POOL_SIZE', '1')
    conns = []
    b = DatabricksBackend(pool=ConnectionPool(lambda: conns.append(FakeConn(0.05)) or conns[-1]))
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(lambda _: b.query('SELECT 1'), range(3)))
    assert len(conns) == 1 and len(conns[0].executed) == 3


def test_broken_connection_is_replaced_and_retried_once():
    """An expired/dropped session is closed, reopened and the query retried once."""
    dbx = pytest.importorskip('databricks.sql.exc')
    b, conns = _backend(fail_first=[dbx.RequestError('connection reset')])
    assert b.query('SELECT 1') == [{'n': 1}]
    assert len(conns) == 2 and conns[0].closed and not conns[1].closed


def test_sql_error_keeps_connection_and_is_not_retried():
    """A bad query is the caller's error: no retry, the connection stays in the pool."""
    b, conns = _backend(fail_first=[ValueError('syntax error')])
    with pytest.raises(ValueError):
        b.query('SELEC 1')
    assert len(conns[0].executed) == 1 and not conns[0].closed
    b.query('SELECT 1')
    assert len(conns) == 1
