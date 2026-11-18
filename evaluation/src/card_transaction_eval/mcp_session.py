"""MCP client sessions against the server under test, plus shared data helpers."""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

EXPECTED_TOOLS = [
    "search_transactions",
    "summarize_transactions",
    "get_cardholder_profile",
    "merchant_relationships",
    "generate_transaction_report",
    "ask_transactions",
    "get_table_schema",
    "get_risk_scoring_guidance",
    "run_risk_sql",
]


class CountingBackend:
    """Wraps the real backend and records every SQL statement it executes
    (used to prove rejected inputs never reach the SQL warehouse)."""

    def __init__(self, inner):
        self.inner = inner
        self.transactions = inner.transactions
        self.statements = []

    def query(self, statement, params=()):
        self.statements.append(statement)
        return self.inner.query(statement, params)


def sql_backend(config):
    """The production Databricks SQL adapter (src/fraud_transaction_mcp/backend.py)."""
    from fraud_transaction_mcp.backend import DatabricksBackend
    return DatabricksBackend()


def transport_label(config) -> str:
    if config.mcp_mode == "http":
        return f"streamable-http -> {config.custom_mcp_url}"
    return "in-memory MCP transport -> fraud_transaction_mcp.app.create_server(DatabricksBackend) -> SQL warehouse"


@asynccontextmanager
async def open_session(config, backend=None, token=None):
    """Yield an initialized mcp.ClientSession.

    inprocess: the real FastMCP server and tool code with the real Databricks SQL
    backend over the SDK's in-memory transport (MCP protocol exercised end to end;
    only the network hop is removed).
    http: the deployed Databricks App at custom_mcp_url (OAuth bearer token).
    """
    from mcp import ClientSession
    if config.mcp_mode == "http":
        from mcp.client.streamable_http import streamablehttp_client
        tok = token if token is not None else config.custom_oauth_token
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        async with streamablehttp_client(config.custom_mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        from mcp.shared.memory import create_connected_server_and_client_session
        from fraud_transaction_mcp.app import create_server
        server = create_server(backend or sql_backend(config))
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            yield session


def parse_result(result):
    """(is_error, data) from a CallToolResult; JSON text content is decoded."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None and not result.isError:
        data = structured["result"] if isinstance(structured, dict) and set(structured) == {"result"} else structured
        return False, data
    text = "\n".join(getattr(c, "text", "") for c in (result.content or []) if getattr(c, "type", "") == "text")
    try:
        data = json.loads(text)
    except Exception:
        data = text
    return bool(result.isError), data


async def call_tool(session, tool: str, arguments: dict) -> dict:
    """Call one tool; protocol-level exceptions are reported as errors, not raised."""
    t0 = time.perf_counter()
    try:
        result = await session.call_tool(tool, arguments)
        is_error, data = parse_result(result)
    except Exception as exc:
        is_error, data = True, f"{type(exc).__name__}: {exc}"
    return {"tool": tool, "arguments": arguments, "is_error": is_error, "data": data,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}


_SAMPLES: dict = {}


def sample_entities(config, backend=None) -> dict:
    """A deterministic real cardholder/transaction from June of benchmark_year (cached)."""
    key = (config.clean_table, config.benchmark_year)
    if key not in _SAMPLES:
        b = backend or sql_backend(config)
        y = config.benchmark_year
        rows = b.query(
            f"SELECT user_id, card_id, card_key, transaction_id, merchant_id FROM {config.clean_table} "
            "WHERE transaction_time >= ? AND transaction_time < ? ORDER BY transaction_id LIMIT 1",
            [f"{y}-06-01", f"{y}-07-01"])
        if not rows:
            raise RuntimeError(f"No transactions found in June {y} for sampling")
        _SAMPLES[key] = {k: str(v) for k, v in rows[0].items()}
    return _SAMPLES[key]
