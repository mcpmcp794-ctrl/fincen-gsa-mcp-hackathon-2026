"""Tests for client/llm_client.py (Plan -> Act -> Verify orchestration).

Runs the real MCP server (fraud_transaction_mcp.app.create_server) in-process over streamable
HTTP, backed by an in-memory SQLite table, and drives llm_client against it with a scripted LLM.
No Databricks workspace, SQL warehouse, or real LLM is needed.

Run from the project root:
    PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider client/tests
"""
import asyncio
import json
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

CLIENT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CLIENT_DIR.parent
for p in (str(CLIENT_DIR), str(PROJECT_ROOT), str(PROJECT_ROOT / 'src')):
    if p not in sys.path:
        sys.path.insert(0, p)

uvicorn = pytest.importorskip('uvicorn')
pytest.importorskip('mcp')

import llm_client  # noqa: E402
from fraud_transaction_mcp.analytics import FIELDS  # noqa: E402
from fraud_transaction_mcp.app import create_server  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
class MemoryBackend:
    """SQLite stand-in for DatabricksBackend (same .transactions / .query contract)."""
    transactions = 'card_transaction_clean'

    def __init__(self):
        self.statements = []  # every SQL statement executed, for assertions
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.lock = threading.Lock()
        self.conn.execute(f'CREATE TABLE card_transaction_clean ({", ".join(FIELDS)})')
        rows = []
        for i in range(40):
            user, card = str(i % 4 + 1), str(i % 2)
            rows.append((f'TX{i:04d}', user, card, f'{user}::{card}',
                         f'2019-03-{i % 28 + 1:02d} 12:00:00', float(i + 1),
                         'Online Transaction' if i % 5 == 0 else 'Chip Transaction',
                         f'M{i % 3}', 'Richmond', 'VA' if i % 2 else 'CA', '23220', '5411',
                         None, 'test', i + 1))
        self.conn.executemany(
            f'INSERT INTO card_transaction_clean VALUES ({",".join("?" for _ in FIELDS)})', rows)

    def query(self, statement, params=()):
        with self.lock:
            self.statements.append(statement)
            cur = self.conn.execute(statement, list(params))
            names = [c[0] for c in cur.description] if cur.description else []
            return [dict(zip(names, r)) for r in cur.fetchall()]


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _serve(asgi_app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(asgi_app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, 'test server did not start'
    return server, thread, port


@pytest.fixture(scope='module')
def mcp_url():
    srv = create_server(MemoryBackend())
    srv.settings.stateless_http = True
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        srv.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    except ImportError:
        pass
    server, thread, port = _serve(srv.streamable_http_app())
    yield f'http://127.0.0.1:{port}/mcp'
    server.should_exit = True
    thread.join(timeout=5)


# --------------------------------------------------------------------------
# Plan -> Act -> Verify orchestration (llm_client.ask_pav_async), LLM scripted
# --------------------------------------------------------------------------
class ScriptedLLM:
    """OpenAI-shaped stub. Picks its reply from the stage named in the system prompt:
    Planner -> plan text; Act / Verify -> one tool call, then a text report."""

    def __init__(self, verdicts=('PASS',)):
        self.verdicts = list(verdicts)
        self.systems = []   # system prompt of every request, in order
        self.tool_kw = []   # whether tools were offered, per request
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    @staticmethod
    def _reply(content=None, tool=None, args=None):
        calls = None
        if tool:
            calls = [SimpleNamespace(id=f'call_{tool}', function=SimpleNamespace(
                name=tool, arguments=json.dumps(args or {})))]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=calls))])

    def create(self, model, messages, max_tokens, tools=None, **kwargs):
        system = messages[0]['content']
        self.systems.append(system)
        self.tool_kw.append(bool(tools))
        after_tool = messages[-1]['role'] == 'tool'
        stage = _stage(system)
        if stage == 'Planner Agent':
            return self._reply('**Analytical Plan:** 1. summarize_transactions group_by=state')
        if stage == 'Act Agent':
            if not after_tool:
                return self._reply(tool='summarize_transactions', args={'group_by': 'state', 'limit': 5})
            return self._reply('**Findings:** 1. CA and VA each have 20 transactions.')
        if stage == 'Verify Agent':
            if not after_tool:
                return self._reply(tool='search_transactions', args={'user_id': '1', 'limit': 1})
            verdict = self.verdicts.pop(0) if self.verdicts else 'PASS'
            tail = ('FEEDBACK: re-run step 1 with limit=10\nVERDICT: REVISE'
                    if verdict == 'REVISE' else 'VERDICT: PASS')
            return self._reply(f'**Answer:** CA and VA each have 20 transactions.\n'
                               f'**Verification Summary:** 1. Confirmed\n{tail}')
        return self._reply('single-loop answer')


def _stage(system):
    """Stage named in the template heading ('# Planner Agent — ...'), else None."""
    first = system.splitlines()[0] if system else ''
    return next((s for s in ('Planner Agent', 'Act Agent', 'Verify Agent') if first.startswith(f'# {s}')),
                None)


def _pav(mcp_url, llm, **kw):
    return asyncio.run(llm_client.ask_pav_async(
        'How many transactions per state?', url=mcp_url, transport='http',
        model='test-model', llm=llm, token='test', **kw))


def test_pav_runs_plan_act_verify_with_server_prompts(mcp_url):
    llm = ScriptedLLM()
    r = _pav(mcp_url, llm, max_revisions=1)
    assert r.workflow == 'pav' and r.verdict == 'PASS' and r.revisions == 0
    # Stage order and the instructions come from the MCP server prompts.
    stages = [_stage(s) for s in llm.systems]
    assert stages == ['Planner Agent', 'Act Agent', 'Act Agent', 'Verify Agent', 'Verify Agent']
    assert llm.tool_kw[0] is False  # planner gets no tools
    assert 'summarize_transactions group_by=state' in llm.systems[1]  # plan handed to Act
    verify_sys = llm.systems[3]
    assert 'CA and VA each have 20 transactions' in verify_sys  # Act findings handed to Verify
    assert 'Tool evidence from the Act phase' in verify_sys and '"transaction_count": 20' in verify_sys
    # Real tool calls hit the server, tagged by phase.
    assert [(c.name, c.phase, c.is_error) for c in r.tool_calls] == [
        ('summarize_transactions', 'act', False), ('search_transactions', 'verify', False)]
    assert r.answer.startswith('**Answer:**') and 'VERDICT' not in r.answer


def test_pav_revises_once_with_verifier_feedback(mcp_url):
    llm = ScriptedLLM(verdicts=['REVISE', 'PASS'])
    r = _pav(mcp_url, llm, max_revisions=1)
    assert r.verdict == 'PASS' and r.revisions == 1
    second_act = [s for s in llm.systems if _stage(s) == 'Act Agent'][-1]
    assert 're-run step 1 with limit=10' in second_act
    assert [c.phase for c in r.tool_calls] == ['act', 'verify', 'act (revision 1)', 'verify']


def test_pav_stops_after_max_revisions_and_flags_answer(mcp_url):
    llm = ScriptedLLM(verdicts=['REVISE', 'REVISE', 'REVISE'])
    r = _pav(mcp_url, llm, max_revisions=0)
    assert r.verdict == 'REVISE' and r.revisions == 0
    assert sum(_stage(s) == 'Verify Agent' for s in llm.systems) == 2  # one verify pass (2 LLM turns)
    assert 'could not be fully verified' in r.answer


def test_pav_falls_back_to_single_loop_without_server_prompts(mcp_url, monkeypatch):
    async def no_pav(session):
        return False
    monkeypatch.setattr(llm_client, 'server_supports_pav', no_pav)
    llm = ScriptedLLM()
    r = _pav(mcp_url, llm)
    assert r.workflow == 'single' and r.answer == 'single-loop answer'
    assert llm.systems == [llm_client.SYSTEM_PROMPT]


def test_pav_uses_caller_session():
    """The evaluation harness passes its own (in-memory) MCP session."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        server = create_server(MemoryBackend())
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            return await llm_client.ask_pav_async('How many transactions per state?', model='test-model',
                                                  llm=ScriptedLLM(), token='test', session=session)
    r = asyncio.run(go())
    assert r.workflow == 'pav' and r.verdict == 'PASS'
    assert [(c.name, c.is_error) for c in r.tool_calls] == [
        ('summarize_transactions', False), ('search_transactions', False)]


def test_pav_env_toggle(monkeypatch):
    monkeypatch.delenv('PAV_WORKFLOW', raising=False)
    assert llm_client.pav_enabled() is True
    monkeypatch.setenv('PAV_WORKFLOW', 'false')
    assert llm_client.pav_enabled() is False


def test_verdict_parser_and_prompt_names():
    from fraud_transaction_mcp.prompts import pav_workflow
    assert llm_client.parse_verdict('x\nFEEDBACK: fix y\nVERDICT: REVISE') == ('REVISE', 'fix y')
    assert llm_client.parse_verdict('no verdict line') == ('PASS', '')
    assert llm_client.parse_verdict('verdict: revise\nlater VERDICT: PASS') == ('PASS', '')
    assert llm_client.strip_verdict('**Answer:** 42\nFEEDBACK: x\nVERDICT: REVISE') == '**Answer:** 42'
    # Client and server agree on the PAV prompt names.
    assert llm_client.PAV_PROMPTS == pav_workflow.PAV_PROMPTS


def test_tool_calls_from_one_turn_run_in_parallel_and_keep_order():
    """Three tool calls requested together overlap, and results stay in request order."""
    delays = {'a': 0.3, 'b': 0.1, 'c': 0.2}
    running = {'now': 0, 'peak': 0}

    class FakeSession:
        async def call_tool(self, name, arguments):
            if name == 'boom':
                raise RuntimeError('transport down')
            running['now'] += 1
            running['peak'] = max(running['peak'], running['now'])
            await asyncio.sleep(delays[name])
            running['now'] -= 1
            return SimpleNamespace(structuredContent={'tool': name, **arguments}, isError=False)

    def call(name, i):
        return SimpleNamespace(id=f'id_{i}', function=SimpleNamespace(
            name=name, arguments=json.dumps({'i': i})))

    replies = iter([[call('a', 0), call('b', 1), call('boom', 2), call('c', 3)], None])

    def create(model, messages, max_tokens, tools=None, **kw):
        calls = next(replies)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None if calls else 'done', tool_calls=calls))])

    llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    messages, trace = [{'role': 'user', 'content': 'q'}], []
    t0 = time.perf_counter()
    answer = asyncio.run(llm_client._tool_loop(FakeSession(), llm, 'test-model', [{}], messages,
                                               4, 100, trace))
    elapsed = time.perf_counter() - t0
    assert answer == 'done'
    assert running['peak'] == 3                  # a, b, c ran at the same time
    assert elapsed < 0.5, elapsed                # serial would be >= 0.6 s
    assert [r.name for r in trace] == ['a', 'b', 'boom', 'c']
    assert [m['tool_call_id'] for m in messages if m['role'] == 'tool'] == ['id_0', 'id_1', 'id_2', 'id_3']
    assert trace[2].is_error and 'transport down' in trace[2].result
    assert json.loads(trace[3].result) == {'tool': 'c', 'i': 3}


def test_parallel_tool_calls_respect_limit(monkeypatch):
    monkeypatch.setenv('MAX_PARALLEL_TOOL_CALLS', '1')
    running = {'now': 0, 'peak': 0}

    class FakeSession:
        async def call_tool(self, name, arguments):
            running['now'] += 1
            running['peak'] = max(running['peak'], running['now'])
            await asyncio.sleep(0.02)
            running['now'] -= 1
            return SimpleNamespace(structuredContent={}, isError=False)

    calls = [SimpleNamespace(id=f'id_{i}', function=SimpleNamespace(name='t', arguments='{}'))
             for i in range(3)]
    replies = iter([calls, None])
    llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: (lambda c: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None if c else 'done', tool_calls=c))]))(next(replies)))))
    asyncio.run(llm_client._tool_loop(FakeSession(), llm, 'm', [{}], [], 4, 100, []))
    assert running['peak'] == 1
