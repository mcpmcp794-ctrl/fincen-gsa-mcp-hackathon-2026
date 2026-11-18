"""Tool contract through the MCP protocol (in-memory transport, SQLite backend)."""
import asyncio
import json

from mcp.shared.memory import create_connected_server_and_client_session

from fraud_transaction_mcp.app import create_server
from fraud_transaction_mcp.config import Settings


def _call(backend, tool, args):
    async def run():
        server = create_server(backend, Settings())
        async with create_connected_server_and_client_session(server._mcp_server) as s:
            r = await s.call_tool(tool, args)
            if r.isError:
                return True, r.content[0].text
            return False, (r.structuredContent or json.loads(r.content[0].text))
    return asyncio.run(run())


def test_search_amount_filters(backend):
    err, out = _call(backend, 'search_transactions', {'min_amount': 150, 'max_amount': 160, 'limit': 50})
    assert not err
    assert out['returned'] == 11 and out['truncated'] is False
    assert all(150 <= r['amount'] <= 160 for r in out['records'])


def test_search_legacy_arguments_still_work(backend):
    err, out = _call(backend, 'search_transactions', {'user_id': '1', 'limit': 3})
    assert not err and out['returned'] == 3 and out['truncated'] is True


def test_search_rejects_inverted_amounts(backend):
    err, _ = _call(backend, 'search_transactions', {'min_amount': 10, 'max_amount': 5})
    assert err


def test_get_table_schema_has_no_label(backend):
    err, out = _call(backend, 'get_table_schema', {})
    assert not err and out['table'] == backend.transactions
    names = [c['name'] for c in out['columns']]
    assert 'amount' in names and not any('fraud' in n.lower() for n in names)
