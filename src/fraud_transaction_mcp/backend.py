"""Databricks SQL adapter used by every tool (backend contract: .transactions, .query).

Requires databricks-sql-connector and databricks-sdk. In Databricks Apps,
prefer app service identity / OAuth. PATs are only an optional local dev fallback.

Connections persist for the life of the server process: each query borrows an
open connection from a small pool and gives it back, so a PAV run (and every
question after it) reuses the same warehouse session instead of reconnecting per
tool call. The pool opens connections lazily: sequential tool calls share ONE
connection; a second one opens only when tool calls run at the same time
(connector connections are not thread-safe, PEP 249 threadsafety=1).
  SQL_POOL_SIZE  max open connections (default 4; 1 = exactly one, queries queue)
"""
import atexit
import os
import re
import threading
from datetime import date, datetime
from decimal import Decimal

from .config import get_settings

# Hard cap on rows fetched per statement (defense in depth; tools also LIMIT).
MAX_RESULT_ROWS = 5000


def _table_name(settings=None):
    """Configured analysis table (config.Settings.card_transaction_table), validated as an identifier."""
    table = (settings or get_settings()).card_transaction_table
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}',table):
        raise ValueError('Unsafe CARD_TRANSACTION_TABLE')
    return table


def _json_value(value):
    if isinstance(value, (datetime,date)):
        return value.isoformat(sep=' ') if isinstance(value,datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _connection_target():
    """Resolve SQL warehouse host and HTTP path.

    Explicit DATABRICKS_SERVER_HOSTNAME / DATABRICKS_HTTP_PATH win (local dev).
    In Databricks Apps, fall back to the auto-injected DATABRICKS_HOST and the
    DATABRICKS_WAREHOUSE_ID provided by the app's sql-warehouse resource.
    """
    host = os.getenv('DATABRICKS_SERVER_HOSTNAME') or os.getenv('DATABRICKS_HOST')
    if not host:
        raise RuntimeError('Set DATABRICKS_SERVER_HOSTNAME or DATABRICKS_HOST')
    host = host.replace('https://', '').replace('http://', '').rstrip('/')
    path = os.getenv('DATABRICKS_HTTP_PATH')
    if not path:
        warehouse_id = os.getenv('DATABRICKS_WAREHOUSE_ID')
        if not warehouse_id:
            raise RuntimeError('Set DATABRICKS_HTTP_PATH or DATABRICKS_WAREHOUSE_ID')
        path = f'/sql/1.0/warehouses/{warehouse_id}'
    return host, path


def _connect():
    """Open one Databricks SQL connection (called only when the pool needs a new one)."""
    from databricks import sql
    host, path = _connection_target()
    token = os.getenv('DATABRICKS_TOKEN')
    if token:
        auth = {'access_token': token}
    else:
        # Databricks SDK unified auth uses the deployed App's identity. The connector
        # calls the returned header factory per request, so OAuth tokens refresh on a
        # long-lived connection.
        from databricks.sdk import WorkspaceClient
        config = WorkspaceClient().config
        auth = {'credentials_provider': lambda: config.authenticate}
    return sql.connect(server_hostname=host, http_path=path, **auth)


def _is_connection_error(exc):
    """True when the connection itself is unusable (dropped, expired session), not a SQL error."""
    try:
        from databricks.sql import exc as dbx
    except ImportError:  # connector not installed (unit tests)
        return False
    if isinstance(exc, (dbx.RequestError, dbx.SessionAlreadyClosedError,
                        dbx.CursorAlreadyClosedError, dbx.InterfaceError)):
        return True
    return isinstance(exc, dbx.Error) and 'session' in str(exc).lower() and (
        'invalid' in str(exc).lower() or 'closed' in str(exc).lower()
        or 'expired' in str(exc).lower())


class ConnectionPool:
    """Thread-safe pool of persistent connections, opened lazily up to `max_size`."""

    def __init__(self, connect=_connect, max_size=None):
        self._connect = connect
        self.max_size = max(1, int(max_size or os.getenv('SQL_POOL_SIZE', '4')))
        self._slots = threading.BoundedSemaphore(self.max_size)
        self._lock = threading.Lock()
        self._idle = []     # LIFO: reuse the most recently used connection first
        self.opened = 0     # connections opened over the pool's lifetime (diagnostics)

    def acquire(self):
        self._slots.acquire()
        try:
            with self._lock:
                if self._idle:
                    return self._idle.pop()
            conn = self._connect()
            with self._lock:
                self.opened += 1
            return conn
        except BaseException:
            self._slots.release()
            raise

    def release(self, conn, broken=False):
        try:
            if broken:
                _close_quietly(conn)
            else:
                with self._lock:
                    self._idle.append(conn)
        finally:
            self._slots.release()

    def close(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            _close_quietly(conn)


def _close_quietly(conn):
    try:
        conn.close()
    except Exception:
        pass


class DatabricksBackend:
    def __init__(self, settings=None, pool=None):
        self.transactions = _table_name(settings)
        self.pool = pool or ConnectionPool()
        atexit.register(self.pool.close)

    def query(self, statement, params=()):
        # One retry, only when the persistent connection itself went bad (e.g. the
        # warehouse session expired while idle); SQL errors are raised as-is.
        for attempt in (1, 2):
            conn = self.pool.acquire()
            try:
                with conn.cursor() as cur:
                    cur.execute(statement, parameters=list(params))
                    names = [d[0] for d in cur.description] if cur.description else []
                    # Tools apply SQL LIMIT; this cap guards against unbounded results.
                    rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
            except Exception as e:
                broken = _is_connection_error(e)
                self.pool.release(conn, broken=broken)
                if broken and attempt == 1:
                    continue
                raise
            self.pool.release(conn)
            break
        if len(rows) > MAX_RESULT_ROWS:
            raise ValueError(f'Result exceeds {MAX_RESULT_ROWS} rows')
        return [{k:_json_value(v) for k,v in zip(names,row)} for row in rows]
