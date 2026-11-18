"""Template smoke tests: the server imports and every submodule registers."""
import asyncio

import pytest
from mcp.server.fastmcp import FastMCP

from fraud_transaction_mcp.app import SERVER_NAME, create_server
from fraud_transaction_mcp.config import Settings

ANALYSIS = {'search_transactions', 'summarize_transactions', 'get_cardholder_profile',
            'merchant_relationships', 'generate_transaction_report', 'ask_transactions',
            'get_table_schema', 'get_risk_scoring_guidance', 'run_risk_sql'}


def _tool_names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def test_server_has_name(backend):
    assert create_server(backend, Settings()).name == SERVER_NAME


def test_default_transport_is_stdio(monkeypatch):
    monkeypatch.delenv('MCP_TRANSPORT', raising=False)
    assert Settings(_env_file=None).mcp_transport == 'stdio'


def test_evaluation_tools_disabled_by_default(monkeypatch, backend):
    monkeypatch.delenv('ENABLE_EVALUATION_TOOLS', raising=False)
    settings = Settings(_env_file=None)
    assert settings.enable_evaluation_tools is False
    assert _tool_names(create_server(backend, settings)) == ANALYSIS


def test_evaluation_tool_registered_only_when_enabled(backend):
    names = _tool_names(create_server(backend, Settings(enable_evaluation_tools=True)))
    assert names == ANALYSIS | {'evaluate_review_signals', 'evaluate_risk_indicators'}


def test_submodules_register_without_error(backend):
    from fraud_transaction_mcp.prompts import register_prompts
    from fraud_transaction_mcp.resources import register_resources
    from fraud_transaction_mcp.routes import register_routes
    from fraud_transaction_mcp.tools import register_tools
    mcp = FastMCP('test')
    register_tools(mcp, backend, Settings())
    register_prompts(mcp)
    register_resources(mcp)
    register_routes(mcp, Settings())


def test_prompts_and_resources_listed(backend):
    server = create_server(backend, Settings())
    assert {p.name for p in asyncio.run(server.list_prompts())} == {
        'pav_plan', 'pav_act', 'pav_verify', 'investigate_transactions',
        'review_transaction', 'profile_cardholder'}
    uris = {str(r.uri) for r in asyncio.run(server.list_resources())}
    assert uris == {'card-transactions://schema', 'card-transactions://data-notes'}


@pytest.mark.parametrize('route', ['/health', '/version', '/'])
def test_http_routes(backend, route):
    from starlette.testclient import TestClient
    app = create_server(backend, Settings()).streamable_http_app()
    with TestClient(app) as client:
        r = client.get(route)
    assert r.status_code == 200
    if route == '/health':
        body = r.json()
        assert body['status'] == 'ok' and set(body['tools']) == ANALYSIS
        assert {'pav_plan', 'pav_act', 'pav_verify'} <= set(body['prompts'])
        assert body['workflow'] == 'plan-act-verify'
        assert body['evaluation_tools_enabled'] is False


def test_concurrent_tool_calls_overlap_on_server(backend):
    """Sync tools run in worker threads, so two slow tool calls do not queue behind each other."""
    import time

    from mcp.shared.memory import create_connected_server_and_client_session

    class SlowBackend:
        transactions = backend.transactions
        def query(self, statement, params=()):
            time.sleep(0.3)
            return backend.query(statement, params)

    args = {'user_id': '1', 'limit': 5}

    async def run():
        server = create_server(SlowBackend(), Settings())
        async with create_connected_server_and_client_session(server._mcp_server) as s:
            t0 = time.perf_counter()
            first = await s.call_tool('search_transactions', args)
            one = time.perf_counter() - t0
            t0 = time.perf_counter()
            pair = await asyncio.gather(s.call_tool('search_transactions', args),
                                        s.call_tool('search_transactions', args))
            both = time.perf_counter() - t0
            return first, pair, one, both

    first, pair, one, both = asyncio.run(run())
    assert not first.isError and not any(r.isError for r in pair)
    assert pair[0].structuredContent == pair[1].structuredContent == first.structuredContent
    assert both < 1.6 * one, (one, both)   # serial execution would take ~2x
