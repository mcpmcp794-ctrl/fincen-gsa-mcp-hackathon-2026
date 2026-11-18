"""LLM client for the Card Transaction MCP server.

Connects a chat model on Databricks Model Serving (OpenAI-compatible chat
completions with tool calling) to the MCP tools exposed by the
`fraudulent-transaction-mcp` Databricks App.

Transports
----------
* http  (default): streamable HTTP to MCP_SERVER_URL (the deployed app's /mcp).
* stdio: spawns `python -m fraud_transaction_mcp.app` (src/ on PYTHONPATH) from the project root. Useful for
  notebook / local testing without the deployed app.

Environment variables
---------------------
LLM_ENDPOINT     UC model service (AI Gateway) or serving endpoint name
                 (default models.ai.gpt-5-6-luna)
REASONING_EFFORT GPT-5.x only; must be 'none' when tools are used (default none)
MCP_TRANSPORT    http | stdio (default http)
MCP_SERVER_URL   MCP endpoint URL (default: deployed fraudulent-transaction-mcp app)
MAX_TOOL_ROUNDS  max LLM <-> tool iterations per phase (default 8)
PAV_WORKFLOW     true | false: use the Plan -> Act -> Verify workflow (default true)
PAV_MAX_REVISIONS  extra act/verify passes when the verifier says REVISE (default 1)
PAV_VERIFY_ROUNDS  max tool iterations in the verify phase (default 4)
MAX_PARALLEL_TOOL_CALLS  tool calls from one LLM turn run concurrently, up to this many (default 4)

Plan -> Act -> Verify
---------------------
`ask_pav_async` fetches the stage instructions from the MCP server prompts
`pav_plan`, `pav_act` and `pav_verify` (the server is the single source of truth)
and runs: PLAN (LLM, no tools) -> ACT (LLM + MCP tools) -> VERIFY (LLM + MCP
tools, ends with VERDICT: PASS | REVISE). On REVISE the verifier's FEEDBACK is
sent back to ACT, up to PAV_MAX_REVISIONS times. If the server does not expose
the PAV prompts, it falls back to the single tool loop (`ask_async`).

Auth uses Databricks unified auth (databricks.sdk Config): the app service
principal inside Databricks Apps, notebook / CLI credentials elsewhere.
"""
import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

DEFAULT_MCP_URL = 'https://fraudulent-transaction-mcp-7474654188777746.aws.databricksapps.com/mcp'
DEFAULT_MODEL = 'models.ai.gpt-5-6-luna'


def _model_kwargs(model):
    """Per-model extra params. GPT-5.x reasoning endpoints reject function tools on
    /chat/completions unless reasoning_effort='none' (verified for gpt-5-6-luna)."""
    if 'gpt-5' in model or 'gpt-6' in model:
        return {'extra_body': {'reasoning_effort': os.getenv('REASONING_EFFORT', 'none')}}
    return {}
MAX_TOOL_RESULT_CHARS = 20000
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYSTEM_PROMPT = """You are a credit-card transaction analyst assistant.
You answer questions about synthetic card transactions (TabFormer data) using
ONLY the provided MCP tools; never invent transactions, users, merchants or numbers.

Tools:
- search_transactions: find purchases by user_id, card_key, merchant_id, start/end date (YYYY-MM-DD),
  min_amount/max_amount.
- summarize_transactions: aggregate by an approved dimension (group_by) over a date range.
- get_cardholder_profile: spending history and merchants for one user_id (optionally card_id).
- merchant_relationships: users who shop at overlapping merchants (NOT fund-flow tracing).
- generate_transaction_report: review report for one transaction_id.
- get_table_schema: columns and meanings of the transaction table.
- ask_transactions: free-form question -> generated read-only SQL -> rows. Use when no specific tool fits.
- get_risk_scoring_guidance: definitions of the four 0/1 fraud risk indicators (risk_score = sum / 4).
- run_risk_sql: run the risk scoring SELECT you write from that guidance; checks each row's score.

Rules:
- Call tools to get facts, then answer concisely and cite the IDs and figures returned.
- If a tool returns an error, explain it and, if sensible, retry with corrected arguments.
- If the question cannot be answered with these tools, say so.
- Do not make fraud determinations; describe patterns factually. Risk scores are risk indicators
  for human review, not fraud determinations.
- For fraud assessment, likelihood or ranking questions (e.g. top users by fraud risk), use
  get_risk_scoring_guidance and run_risk_sql and answer with the risk scores; do not refer them.
- Fraud labels are not available in these tools. Only for recorded fraud labels (labelled counts,
  rates or label values), say so and refer the user to the Fraud Detection Genie space
  "Card Transaction Insights and Fraud Detection Agent" (Genie space ID
  01f1dc44f22413fe82320128338353b5); do not estimate them or offer substitute metrics.
- For trend or comparison questions, return aggregated rows (e.g. one row per month, category
  or state with a numeric measure) so the UI can chart them, then give a short analysis:
  overall total, highest/lowest values, notable changes or outliers.
- Data covers 2010-01 to 2020-02.
"""


PAV_PROMPTS = {'plan': 'pav_plan', 'act': 'pav_act', 'verify': 'pav_verify'}
_VERDICT = re.compile(r'VERDICT:\s*(PASS|REVISE)', re.I)
_FEEDBACK = re.compile(r'^\s*FEEDBACK:\s*(.+)$', re.I | re.M)
MAX_EVIDENCE_CHARS = 3000      # per tool call, when handing Act evidence to Verify
MAX_EVIDENCE_TOTAL = 16000


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    result: str
    is_error: bool = False
    phase: str = 'act'


@dataclass
class ChatResult:
    answer: str
    tool_calls: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    workflow: str = 'single'   # 'pav' or 'single'
    plan: str = ''
    findings: str = ''
    verification: str = ''
    verdict: str = ''          # PASS | REVISE ('' for single)
    revisions: int = 0


# --------------------------------------------------------------------------
# Auth / LLM
# --------------------------------------------------------------------------
def _config():
    from databricks.sdk.core import Config
    return Config()


def _bearer_token(cfg):
    headers = cfg.authenticate()
    auth = headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        raise RuntimeError('Databricks auth did not return a bearer token')
    return auth.split(' ', 1)[1]


def _llm_base_path(model):
    """UC model services (catalog.schema.name, e.g. models.ai.gpt-5-6-luna) are served
    through AI Gateway; plain endpoint names through /serving-endpoints."""
    return '/ai-gateway/mlflow/v1' if '.' in model else '/serving-endpoints'


def make_llm_client(cfg=None, token=None, model=None):
    """OpenAI SDK client pointed at Databricks AI Gateway / Model Serving."""
    from openai import OpenAI
    cfg = cfg or _config()
    model = model or os.getenv('LLM_ENDPOINT', DEFAULT_MODEL)
    # Bounded calls: the SDK default (600 s x 3 attempts) makes a stuck call look like a hang.
    return OpenAI(api_key=token or _bearer_token(cfg),
                  base_url=cfg.host.rstrip('/') + _llm_base_path(model),
                  timeout=float(os.getenv('LLM_TIMEOUT_SECONDS', '120')), max_retries=1)


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------
@asynccontextmanager
async def open_mcp_session(transport=None, url=None, token=None):
    """Yield an initialized mcp.ClientSession over http or stdio."""
    from mcp import ClientSession
    transport = (transport or os.getenv('MCP_TRANSPORT', 'http')).lower()
    if transport == 'stdio':
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(
            p for p in (os.path.join(PROJECT_ROOT, 'src'), env.get('PYTHONPATH')) if p)
        params = StdioServerParameters(command=sys.executable,
                                       args=['-m', 'fraud_transaction_mcp.app'],
                                       env=env, cwd=PROJECT_ROOT)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == 'http':
        from mcp.client.streamable_http import streamablehttp_client
        url = url or os.getenv('MCP_SERVER_URL', DEFAULT_MCP_URL)
        token = token or _bearer_token(_config())
        async with streamablehttp_client(url, headers={'Authorization': f'Bearer {token}'}) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        raise ValueError(f'Unsupported MCP_TRANSPORT: {transport}')


def mcp_tools_to_openai(tools):
    """Convert MCP tool definitions to OpenAI function-calling tool specs."""
    specs = []
    for t in tools:
        schema = dict(t.inputSchema or {'type': 'object', 'properties': {}})
        schema.setdefault('type', 'object')
        schema.setdefault('properties', {})
        specs.append({'type': 'function', 'function': {
            'name': t.name, 'description': (t.description or '')[:1024], 'parameters': schema}})
    return specs


def _tool_result_text(result):
    structured = getattr(result, 'structuredContent', None)
    if structured is not None:
        text = json.dumps(structured, default=str)
    else:
        parts = [getattr(c, 'text', None) or str(c) for c in (result.content or [])]
        text = '\n'.join(parts)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '... [truncated]'
    return text


@asynccontextmanager
async def _use_session(session, transport=None, url=None, token=None):
    """Yield `session` if given (caller owns it), otherwise open a new MCP session."""
    if session is not None:
        yield session
    else:
        async with open_mcp_session(transport, url, token) as opened:
            yield opened


async def list_tools_async(transport=None, url=None):
    async with open_mcp_session(transport, url) as session:
        return [(t.name, t.description) for t in (await session.list_tools()).tools]


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------
def _clients(model, llm=None, token=None):
    """(llm, token) from Databricks unified auth unless both are injected (tests)."""
    if llm is not None and token is not None:
        return llm, token
    cfg = _config()
    token = token or _bearer_token(cfg)
    return llm or make_llm_client(cfg, token, model), token


async def _complete(llm, model, messages, max_tokens, tools=None):
    kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, **_model_kwargs(model))
    if tools:
        kwargs['tools'] = tools
    resp = await asyncio.to_thread(llm.chat.completions.create, **kwargs)
    return resp.choices[0].message


async def _call_tool(session, tc, limit):
    """Run one requested tool call; never raises (errors become the tool result)."""
    args = {}
    try:
        args = json.loads(tc.function.arguments or '{}')
        async with limit:
            result = await session.call_tool(tc.function.name, arguments=args)
        return args, _tool_result_text(result), bool(result.isError)
    except Exception as e:  # bad JSON args, unknown tool, transport error
        return args, f'Tool error: {e}', True


async def _tool_loop(session, llm, model, tools, messages, max_rounds, max_tokens, trace,
                     phase='act'):
    """LLM <-> MCP tool loop. Appends to `messages` and `trace`; returns the final text.

    Tool calls requested in the same LLM turn are independent (the model chose them
    all before seeing any result), so they run concurrently; results are appended in
    the order the model requested them. Calls that depend on an earlier result come
    in a later turn, so rounds stay sequential.
    """
    limit = asyncio.Semaphore(max(1, int(os.getenv('MAX_PARALLEL_TOOL_CALLS', '4'))))
    for _ in range(max_rounds):
        msg = await _complete(llm, model, messages, max_tokens, tools)
        if not msg.tool_calls:
            return msg.content or ''
        messages.append({'role': 'assistant', 'content': msg.content or '', 'tool_calls': [
            {'id': tc.id, 'type': 'function',
             'function': {'name': tc.function.name, 'arguments': tc.function.arguments or '{}'}}
            for tc in msg.tool_calls]})
        outcomes = await asyncio.gather(*(_call_tool(session, tc, limit) for tc in msg.tool_calls))
        for tc, (args, text, is_error) in zip(msg.tool_calls, outcomes):
            trace.append(ToolCallRecord(tc.function.name, args, text, is_error, phase))
            messages.append({'role': 'tool', 'tool_call_id': tc.id, 'content': text})
    # Tool budget exhausted: force a final answer without tools.
    messages.append({'role': 'user', 'content':
                     'Tool budget reached. Answer now using only the results above.'})
    return (await _complete(llm, model, messages, max_tokens)).content or ''


async def ask_async(question, history=None, model=None, transport=None, url=None,
                    max_rounds=None, max_tokens=2000, llm=None, token=None, session=None):
    """Answer `question` with one LLM tool loop (no planning / verification phases).

    history: optional prior [{'role': 'user'|'assistant', 'content': str}, ...]
    """
    model = model or os.getenv('LLM_ENDPOINT', DEFAULT_MODEL)
    max_rounds = int(max_rounds or os.getenv('MAX_TOOL_ROUNDS', '8'))
    llm, token = _clients(model, llm, token)

    async def run(session):
        tools = mcp_tools_to_openai((await session.list_tools()).tools)
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
        messages += list(history or [])
        messages.append({'role': 'user', 'content': question})
        trace = []
        answer = await _tool_loop(session, llm, model, tools, messages, max_rounds, max_tokens, trace)
        return ChatResult(answer=answer, tool_calls=trace, messages=messages)

    if session is not None:
        return await run(session)
    async with open_mcp_session(transport, url, token) as session:
        return await run(session)


# --------------------------------------------------------------------------
# Plan -> Act -> Verify
# --------------------------------------------------------------------------
def pav_enabled():
    return os.getenv('PAV_WORKFLOW', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def parse_verdict(text):
    """(verdict, feedback) from Verify output; missing verdict counts as PASS (no loops)."""
    verdicts = _VERDICT.findall(text or '')
    feedback = _FEEDBACK.findall(text or '')
    return ((verdicts[-1].upper() if verdicts else 'PASS'),
            (feedback[-1].strip() if feedback else ''))


def strip_verdict(text):
    return '\n'.join(ln for ln in (text or '').splitlines()
                     if not _VERDICT.search(ln) and not _FEEDBACK.match(ln)).strip()


async def get_prompt_text(session, name, arguments):
    """Render an MCP server prompt to plain text (MCP prompt arguments are strings)."""
    res = await session.get_prompt(name, arguments={k: str(v) for k, v in arguments.items()})
    parts = []
    for m in res.messages:
        content = m.content
        parts.append(getattr(content, 'text', None) or (content if isinstance(content, str) else ''))
    return '\n\n'.join(p for p in parts if p)


async def server_supports_pav(session):
    try:
        names = {p.name for p in (await session.list_prompts()).prompts}
    except Exception:  # server without prompt support
        return False
    return set(PAV_PROMPTS.values()) <= names


def _evidence_digest(calls):
    """Compact record of Act-phase tool calls so Verify can trace every figure."""
    if not calls:
        return '\n\n### Tool evidence from the Act phase\nNo tool calls were made.'
    out, used = ['\n\n### Tool evidence from the Act phase'], 0
    for i, c in enumerate(calls, 1):
        body = c.result if len(c.result) <= MAX_EVIDENCE_CHARS else (
            c.result[:MAX_EVIDENCE_CHARS] + '... [truncated]')
        if used + len(body) > MAX_EVIDENCE_TOTAL:
            out.append(f'{i}. {c.name} ... [omitted: evidence budget reached]')
            continue
        used += len(body)
        status = 'ERROR' if c.is_error else 'ok'
        out.append(f'{i}. {c.name}({json.dumps(c.arguments, default=str)}) [{status}]\n{body}')
    return '\n'.join(out)


async def ask_pav_async(question, history=None, model=None, transport=None, url=None,
                        max_rounds=None, max_tokens=2000, max_revisions=None,
                        verify_rounds=None, llm=None, token=None, session=None):
    """Answer `question` with the Plan -> Act -> Verify workflow.

    PLAN uses the server prompt `pav_plan` (no tools), ACT `pav_act` (tools), VERIFY
    `pav_verify` (tools; returns the final answer and a VERDICT). Falls back to
    `ask_async` when the MCP server does not expose the PAV prompts.

    session: optional already-initialized mcp.ClientSession (e.g. the evaluation
    harness's in-memory session); when omitted one is opened from transport/url.
    """
    model = model or os.getenv('LLM_ENDPOINT', DEFAULT_MODEL)
    max_rounds = int(max_rounds or os.getenv('MAX_TOOL_ROUNDS', '8'))
    verify_rounds = int(verify_rounds or os.getenv('PAV_VERIFY_ROUNDS', '4'))
    max_revisions = int(os.getenv('PAV_MAX_REVISIONS', '1') if max_revisions is None else max_revisions)
    llm, token = _clients(model, llm, token)

    async with _use_session(session, transport, url, token) as session:
        if not await server_supports_pav(session):
            return await ask_async(question, history, model, max_rounds=max_rounds,
                                   max_tokens=max_tokens, llm=llm, token=token, session=session)
        tools = mcp_tools_to_openai((await session.list_tools()).tools)
        trace, transcript = [], []

        # ---- PLAN (no tools; history lets the planner resolve follow-up questions)
        plan_sys = await get_prompt_text(session, PAV_PROMPTS['plan'],
                                         {'research_question': question, 'max_steps': max_rounds})
        plan_msgs = [{'role': 'system', 'content': plan_sys}] + list(history or []) + [
            {'role': 'user', 'content': question}]
        plan = (await _complete(llm, model, plan_msgs, max_tokens)).content or ''
        transcript += plan_msgs + [{'role': 'assistant', 'content': plan}]

        feedback, findings, verification, verdict, revision = '', '', '', 'PASS', 0
        for revision in range(max_revisions + 1):
            phase = 'act' if revision == 0 else f'act (revision {revision})'
            # ---- ACT
            act_sys = await get_prompt_text(session, PAV_PROMPTS['act'], {
                'research_question': question, 'plan': plan, 'feedback': feedback,
                'max_steps': max_rounds})
            act_msgs = [{'role': 'system', 'content': act_sys},
                        {'role': 'user', 'content': 'Execute the plan now and report your findings.'}]
            start = len(trace)
            findings = await _tool_loop(session, llm, model, tools, act_msgs, max_rounds,
                                        max_tokens, trace, phase)
            transcript += act_msgs + [{'role': 'assistant', 'content': findings}]
            # ---- VERIFY
            verify_sys = await get_prompt_text(session, PAV_PROMPTS['verify'], {
                'research_question': question, 'plan': plan,
                'findings': findings + _evidence_digest(trace[start:]),
                'max_steps': verify_rounds})
            verify_msgs = [{'role': 'system', 'content': verify_sys},
                           {'role': 'user', 'content': 'Verify the findings and write the final answer.'}]
            verification = await _tool_loop(session, llm, model, tools, verify_msgs, verify_rounds,
                                            max_tokens, trace, 'verify')
            transcript += verify_msgs + [{'role': 'assistant', 'content': verification}]
            verdict, fb = parse_verdict(verification)
            if verdict == 'PASS' or revision == max_revisions:
                break
            feedback = fb or 'Re-run or correct the steps the verifier marked Unverified.'

        answer = strip_verdict(verification) or findings
        if verdict == 'REVISE':
            answer = ('_Some findings could not be fully verified; treat them as provisional._'
                      '\n\n' + answer)
        return ChatResult(answer=answer, tool_calls=trace, messages=transcript, workflow='pav',
                          plan=plan, findings=findings, verification=verification,
                          verdict=verdict, revisions=revision)


def ask(question, **kwargs):
    """Synchronous single-loop wrapper. In notebooks use `await ask_async(...)`."""
    return asyncio.run(ask_async(question, **kwargs))


def ask_pav(question, **kwargs):
    """Synchronous Plan -> Act -> Verify wrapper. In notebooks use `await ask_pav_async(...)`."""
    return asyncio.run(ask_pav_async(question, **kwargs))


# [CLEANUP 2026-11-17] Unused sync wrapper: app.py calls list_tools_async directly.
# def list_tools(**kwargs):
#     return asyncio.run(list_tools_async(**kwargs))


if __name__ == '__main__':
    q = ' '.join(sys.argv[1:]) or 'Summarize transactions by merchant for 2019.'
    r = ask_pav(q) if pav_enabled() else ask(q)
    if r.plan:
        print('[plan]\n' + r.plan)
    for c in r.tool_calls:
        print(f'[{c.phase}] {c.name}({json.dumps(c.arguments)}) error={c.is_error}')
    if r.verdict:
        print(f'[verdict] {r.verdict} (revisions={r.revisions})')
    print(r.answer)
