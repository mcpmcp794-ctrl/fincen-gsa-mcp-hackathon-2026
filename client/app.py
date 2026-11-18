"""Streamlit chat UI: natural language -> LLM -> Card Transaction MCP tools -> Databricks SQL.

Dark "Transaction Intelligence" layout: FinCEN seal + model picker + conversation
history in the sidebar, Chat and Analytics dashboard tabs, collapsible MCP execution
details per answer, and interactive charts (auto bar/line/area/pie) for tool results.

Chat answers use the Plan -> Act -> Verify workflow (llm_client.ask_pav_async) by
default; the sidebar toggle (default from PAV_WORKFLOW) switches to the single loop.
"""
import asyncio
import base64
import json
import os
import re
import time

import altair as alt
import pandas as pd
import streamlit as st

from llm_client import (DEFAULT_MCP_URL, DEFAULT_MODEL, _model_kwargs, _tool_result_text,
                        ask_async, ask_pav_async, list_tools_async, make_llm_client,
                        open_mcp_session, pav_enabled)

DEFAULT = os.getenv('LLM_ENDPOINT', DEFAULT_MODEL)
TRANSPORT = os.getenv('MCP_TRANSPORT', 'http')
MCP_URL = os.getenv('MCP_SERVER_URL', DEFAULT_MCP_URL)
TABLE = os.getenv('TABLE_NAME', 'workspace.default.card_transaction_mcp_clean')
# Models attached to this app as resources (must stay in sync with app resources).
MODEL_OPTIONS = [m.strip() for m in os.getenv(
    'MODEL_OPTIONS',
    'models.ai.gpt-5-6-luna,models.ai.gpt-5-6-terra,databricks-gpt-6-luna').split(',') if m.strip()]
if DEFAULT not in MODEL_OPTIONS:
    MODEL_OPTIONS.insert(0, DEFAULT)

APP_TITLE = os.getenv('APP_TITLE', 'IBM Card Transaction Intelligence')
# Chart colours tuned for the dark theme (names kept for the chart helpers).
NAVY, GOLD, SLATE = '#4C8DF6', '#F2B33D', '#9AA7BA'
PALETTE = [NAVY, GOLD, '#7FB2FF', '#E57373', '#81C784', '#BA68C8', SLATE, '#4DD0E1']
HERE = os.path.dirname(os.path.abspath(__file__))


def _logo_data_uri():
    """LOGO_URL env (image URL) > assets/fincen-logo.png|jpg > bundled PNG seal. """
    if os.getenv('LOGO_URL'):
        return os.environ['LOGO_URL']
    for name, mime in (('fincen-logo.png', 'image/png'), ('fincen-logo.jpg', 'image/jpeg')):
        path = os.path.join(HERE, 'assets', name)
        if os.path.exists(path):
            with open(path, 'rb') as f:
                return f'data:{mime};base64,' + base64.b64encode(f.read()).decode()
    return ''


LOGO = _logo_data_uri()


def _model_label(name):
    """models.ai.gpt-5-6-luna -> '5.6 Luna'; databricks-gpt-6-luna -> '6 Luna'."""
    base = name.split('.')[-1].replace('databricks-', '').replace('gpt-', '')
    parts = base.split('-')
    nums = [p for p in parts if p.isdigit()]
    words = [p.title() for p in parts if not p.isdigit()]
    return ' '.join(filter(None, ['.'.join(nums), ' '.join(words)])) or name


st.set_page_config(page_title=APP_TITLE, page_icon='🏛️', layout='wide')

st.markdown("""
<style>
  .block-container { padding-top: 2.2rem; max-width: 1250px; }
  [data-testid="stSidebar"] { background: #1F2A3C; }
  [data-testid="stSidebar"] .stButton > button { width: 100%; background: transparent;
      border: 1px solid #3A475C; }
  [data-testid="stSidebar"] .stButton > button:hover { border-color: #4C8DF6; color: #4C8DF6; }
  [data-testid="stSidebar"] hr { border-color: #3A475C; }
  .fc-seal { display: block; width: 104px; margin: 4px auto 10px auto; }  /* centered in sidebar */
  .fc-side-title { font-size: 1.45rem; font-weight: 700; margin: 4px 0 14px 0; }
  .fc-title { font-size: 2.7rem; font-weight: 800; line-height: 1.15; margin: 0 0 .35rem 0; }
  .fc-sub { color: #AEB8C8; font-size: .9rem; margin-bottom: 1.1rem; }
  .stTabs [data-baseweb="tab-list"] { border-bottom: 1px solid #273247; }
  [data-testid="stChatMessage"] { background: #141C2B; border: 1px solid #273247;
      border-radius: 10px; padding: 14px 18px; margin-bottom: .75rem; }
  [data-testid="stChatInput"] { margin-bottom: .9rem; }
  .fc-elapsed { color: #7F8AA0; font-size: .78rem; margin-top: .55rem; }
  [data-testid="stExpander"] details { border-color: #2C3850; }
  [data-testid="stMetric"] { background: #141C2B; border: 1px solid #273247; border-radius: 10px;
      padding: 10px 14px; }
</style>
""", unsafe_allow_html=True)

ss = st.session_state
if 'convs' not in ss:
    ss.convs = {'Chat 1': []}  # name -> [{'role', 'content', 'tool_calls'?}]
    ss.current = 'Chat 1'

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    if LOGO:
        st.markdown(f'<img class="fc-seal" src="{LOGO}" alt="FinCEN">', unsafe_allow_html=True)
    st.markdown('<div class="fc-side-title">Transaction Intelligence</div>', unsafe_allow_html=True)
    model = st.selectbox('SI model', MODEL_OPTIONS, index=MODEL_OPTIONS.index(DEFAULT),
                         format_func=_model_label)
    st.markdown(f'Endpoint: `{model}`')
    use_pav = st.toggle('Plan → Act → Verify workflow', value=pav_enabled(),
                        help='Plan the tool calls, execute them, then independently verify '
                             'the findings before answering. Off = single tool loop.')
    show_details = st.toggle('Show MCP execution details', value=True)
    st.divider()

    if st.button('＋ New conversation', key='new_conv'):
        n = len(ss.convs) + 1
        while f'Chat {n}' in ss.convs:
            n += 1
        ss.convs[f'Chat {n}'] = []
        ss.current = f'Chat {n}'
        st.rerun()
    names = list(ss.convs)
    picked = st.selectbox('Conversation history', names, index=names.index(ss.current))
    if picked != ss.current:
        ss.current = picked
        st.rerun()
    if st.button('Clear current conversation', key='clear_conv'):
        ss.convs[ss.current] = []
        st.rerun()
    st.divider()

    if st.button('Verify model connection', key='verify'):
        try:
            llm = make_llm_client(model=model)
            resp = llm.chat.completions.create(
                model=model, max_tokens=100, **_model_kwargs(model),
                messages=[{'role': 'user', 'content': 'Reply with exactly: Connection successful'}])
            st.success(resp.choices[0].message.content or '(empty reply)')
        except Exception as e:
            st.error(f'Model check failed: {e}')

    with st.expander('Configuration'):
        max_rounds = st.slider('Max tool rounds (per phase)', 1, 15, int(os.getenv('MAX_TOOL_ROUNDS', '8')))
        max_revisions = st.slider('Max verify revisions', 0, 3, int(os.getenv('PAV_MAX_REVISIONS', '1')),
                                  disabled=not use_pav)
        show_charts = st.toggle('Show charts', value=True)
        st.markdown(f'Table: `{TABLE}`')
        st.markdown(f'MCP transport: `{TRANSPORT}`')
        if TRANSPORT == 'http':
            st.markdown(f'MCP server: `{MCP_URL}`')
        if st.button('List MCP tools', key='list_tools'):
            try:
                for name, desc in asyncio.run(list_tools_async()):
                    st.markdown(f'**{name}**: {(desc or "").splitlines()[0] if desc else ""}')
            except Exception as e:
                st.error(f'MCP connection failed: {e}')

messages = ss.convs[ss.current]

# ---------------------------------------------------------------- header
st.markdown(f'<div class="fc-title">{APP_TITLE}</div>'
            f'<div class="fc-sub">Natural language → SI model → MCP ({TRANSPORT}) → Databricks SQL</div>',
            unsafe_allow_html=True)


# ---------------------------------------------------------------- rendering
def _parse_result(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    # FastMCP may wrap non-dict returns as {'result': ...}
    if isinstance(data, dict) and set(data) == {'result'}:
        data = data['result']
    return data if isinstance(data, dict) else None


DATE_HINT = re.compile(r'(date|month|year|day|week|time|period|quarter)', re.I)


def _prepare(records):
    """DataFrame with numeric-looking text ('$1,234.50', '12') converted to numbers."""
    df = pd.DataFrame(records)
    for col in df.columns:
        if df[col].dtype == object:
            cleaned = df[col].astype(str).str.replace(r'[$,%\s]', '', regex=True)
            num = pd.to_numeric(cleaned, errors='coerce')
            if num.notna().mean() >= 0.9 and not DATE_HINT.search(str(col)):
                df[col] = num
    return df


def _chart_spec(df):
    """Pick (x, y, kind) for an automatic chart, or None if the data is not chartable."""
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    others = [c for c in df.columns if c not in nums]
    if not nums or len(df) < 2:
        return None
    # Prefer a measure that is not an id-like column
    measures = [c for c in nums if not re.search(r'(^|_)id$|_id\b|year$', str(c), re.I)] or nums
    y = measures[0]
    time_cols = [c for c in df.columns if DATE_HINT.search(str(c)) and c != y]
    if time_cols:
        return time_cols[0], y, 'line'
    if others:
        return others[0], y, 'bar'
    return None


def _render_chart(df, key):
    spec = _chart_spec(df)
    if not spec:
        return False
    x, y, auto_kind = spec
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    c1, c2, c3 = st.columns([1, 1, 1])
    kind = c1.selectbox('Chart type', ['auto', 'bar', 'line', 'area', 'pie'], key=f'{key}_kind')
    y = c2.selectbox('Measure', nums, index=nums.index(y), key=f'{key}_y')
    x = c3.selectbox('Dimension', [c for c in df.columns if c != y],
                     index=[c for c in df.columns if c != y].index(x) if x != y else 0, key=f'{key}_x')
    kind = auto_kind if kind == 'auto' else kind
    data = df[[x, y]].dropna()
    is_time = bool(DATE_HINT.search(str(x)))
    if is_time:
        parsed = pd.to_datetime(data[x].astype(str), errors='coerce')
        if parsed.notna().mean() >= 0.9:
            data = data.assign(**{x: parsed}).sort_values(x)
    if pd.api.types.is_datetime64_any_dtype(data[x]):
        x_type = 'temporal'
    elif pd.api.types.is_numeric_dtype(data[x]) and is_time:
        x_type = 'ordinal'
    else:
        x_type = 'nominal'
    tooltip = [alt.Tooltip(x, type=x_type), alt.Tooltip(y, format=',.2f')]
    if kind == 'pie':
        data = data.sort_values(y, ascending=False).head(10)
        chart = alt.Chart(data).mark_arc(innerRadius=60).encode(
            theta=alt.Theta(y, type='quantitative'),
            color=alt.Color(x, type='nominal', scale=alt.Scale(range=PALETTE)), tooltip=tooltip)
    elif kind == 'bar' and x_type == 'nominal':
        data = data.sort_values(y, ascending=False).head(25)
        chart = alt.Chart(data).mark_bar(color=NAVY, cornerRadiusEnd=3).encode(
            y=alt.Y(x, type='nominal', sort='-x', title=x),
            x=alt.X(y, type='quantitative', title=y), tooltip=tooltip)
        chart = chart + chart.mark_text(align='left', dx=3, color=SLATE).encode(
            text=alt.Text(y, format=',.0f'))
    else:
        base = alt.Chart(data).encode(x=alt.X(x, type=x_type, title=x),
                                      y=alt.Y(y, type='quantitative', title=y), tooltip=tooltip)
        if kind == 'area':
            chart = base.mark_area(color=NAVY, opacity=0.25, line={'color': NAVY})
        elif kind == 'bar':
            chart = base.mark_bar(color=NAVY)
        else:
            chart = base.mark_line(color=NAVY, strokeWidth=2.5) + base.mark_point(color=GOLD, filled=True, size=45)
    st.altair_chart(chart.properties(height=360).configure_view(stroke=None)
                    .configure_axis(labelColor=SLATE, titleColor='#E6EAF2', gridColor='#273247'),
                    use_container_width=True)
    # quick stats for the analysis
    s = data[y]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric('Rows', f'{len(data):,}')
    m2.metric(f'Total {y}', f'{s.sum():,.2f}')
    m3.metric(f'Average {y}', f'{s.mean():,.2f}')
    top = data.loc[s.idxmax(), x] if len(s) else ''
    m4.metric(f'Max {y}', f'{s.max():,.2f}', help=f'at {top}')
    return True


def _render_tool_calls(calls, msg_key='live'):
    if not calls:
        return
    # Verify-phase calls re-check Act results; chart only the Act evidence when present.
    if any(c.get('phase', 'act') != 'verify' for c in calls):
        calls = [c for c in calls if c.get('phase', 'act') != 'verify']
    for i, c in enumerate(calls):
        data = None if c['is_error'] else _parse_result(c['result'])
        if not data:
            continue
        records = data.get('records')
        if isinstance(records, list) and records:
            df = _prepare(records)
            st.caption(f"`{c['name']}` returned {len(df)} rows"
                       + (' (truncated)' if data.get('truncated') else ''))
            if show_charts and _chart_spec(df):
                tab_chart, tab_table = st.tabs(['Chart', 'Table'])
                with tab_chart:
                    try:
                        _render_chart(df, key=f'{msg_key}_{i}')
                    except Exception as e:  # never lose the answer because of a chart
                        st.warning(f'Could not draw chart: {e}')
                with tab_table:
                    st.dataframe(df, use_container_width=True)
            else:
                st.dataframe(df, use_container_width=True)
            st.download_button('Download CSV', df.to_csv(index=False).encode(),
                               file_name=f"{c['name']}.csv", mime='text/csv',
                               key=f'{msg_key}_{i}_csv')


def _render_details(calls):
    """Collapsible 'Completed: <tool>' panel with arguments, generated SQL and raw output."""
    if not calls or not show_details:
        return
    names = ', '.join(dict.fromkeys(c['name'] for c in calls))
    failed = any(c['is_error'] for c in calls)
    label = f"{'Completed with errors' if failed else 'Completed'}: {names}"
    with st.expander(label, expanded=False, icon=':material/error:' if failed else ':material/check:'):
        for c in calls:
            phase = f" · {c['phase']}" if c.get('phase') else ''
            st.markdown(f"**{c['name']}**{phase}" + (' (error)' if c['is_error'] else ''))
            st.caption('Arguments')
            st.code(json.dumps(c['arguments'], indent=2), language='json')
            data = None if c['is_error'] else _parse_result(c['result'])
            if data and data.get('sql'):
                st.caption('Generated SQL')
                st.code(data['sql'], language='sql')
            st.caption('Raw MCP output')
            st.code(c['result'][:4000], language='json')


def _render_workflow(m):
    """Plan and verification panels for Plan -> Act -> Verify answers."""
    if m.get('workflow') != 'pav':
        return
    verdict = m.get('verdict') or 'PASS'
    revs = m.get('revisions') or 0
    st.caption(f"Plan → Act → Verify · verdict **{verdict}**"
               + (f' after {revs} revision(s)' if revs else ''))
    if m.get('plan'):
        with st.expander('Plan', expanded=False, icon=':material/checklist:'):
            st.markdown(m['plan'])
    if show_details and m.get('findings'):
        with st.expander('Act findings (before verification)', expanded=False,
                         icon=':material/construction:'):
            st.markdown(m['findings'])


def _format_elapsed(seconds):
    """12.3 -> 'Thought for 12 seconds'; 75 -> 'Thought for 1 minute 15 seconds'."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"Thought for {s} second{'' if s == 1 else 's'}"
    m, s = divmod(s, 60)
    text = f"Thought for {m} minute{'' if m == 1 else 's'}"
    return text + (f" {s} second{'' if s == 1 else 's'}" if s else '')


def _render_elapsed(seconds):
    """Subtle end-to-end response time (question sent -> answer returned) under an answer."""
    if seconds is None:
        return
    st.markdown(f'<div class="fc-elapsed">{_format_elapsed(seconds)}</div>', unsafe_allow_html=True)


def _render_message(m, key):
    with st.chat_message(m['role']):
        if m['role'] == 'assistant':
            _render_workflow(m)
            _render_details(m.get('tool_calls'))
        st.markdown(m['content'])
        if m['role'] == 'assistant':
            _render_tool_calls(m.get('tool_calls'), msg_key=key)
            _render_elapsed(m.get('elapsed_s'))


# ---------------------------------------------------------------- dashboard data
def _call_tool(tool, arguments):
    async def go():
        async with open_mcp_session() as session:
            r = await session.call_tool(tool, arguments)
            return _tool_result_text(r), bool(getattr(r, 'isError', False))
    text, is_error = asyncio.run(go())
    if is_error:
        raise RuntimeError(text)
    return _parse_result(text) or {}


@st.cache_data(ttl=900, show_spinner=False)
def _summary(group_by, start, end, limit):
    data = _call_tool('summarize_transactions',
                      {'group_by': group_by, 'start': start, 'end': end, 'limit': limit})
    df = _prepare(data.get('records') or [])
    if 'group_value' in df.columns:
        df['group_value'] = df['group_value'].astype(str)
    return df


def _dark(chart, height=300):
    return (chart.properties(height=height).configure_view(stroke=None)
            .configure_axis(labelColor=SLATE, titleColor='#E6EAF2', gridColor='#273247'))


def _bar(df, label, value, title, n=10):
    d = df.sort_values(value, ascending=False).head(n)
    st.markdown(f'**{title}**')
    st.altair_chart(_dark(alt.Chart(d).mark_bar(color=NAVY, cornerRadiusEnd=3).encode(
        y=alt.Y('group_value:N', sort='-x', title=label),
        x=alt.X(f'{value}:Q', title=value.replace('_', ' ')),
        tooltip=['group_value', alt.Tooltip(f'{value}:Q', format=',.2f')])), use_container_width=True)


def render_dashboard():
    c1, c2 = st.columns([1, 3])
    year = c1.selectbox('Year', list(range(2019, 2009, -1)), key='dash_year')
    start, end = f'{year}-01-01', f'{year}-12-31'
    if not c2.button('Load dashboard', key='dash_load') and ss.get('dash_loaded') != year:
        st.info('Pick a year and click Load dashboard. Figures come from the summarize_transactions MCP tool.')
        return
    ss.dash_loaded = year
    try:
        with st.spinner(f'Querying {year} through MCP...'):
            month = _summary('month', start, end, 12).sort_values('group_value')
            mcc = _summary('mcc', start, end, 15)
            state = _summary('state', start, end, 15)
            chip = _summary('chip', start, end, 10)
    except Exception as e:
        st.error(f'Dashboard query failed: {e}')
        return
    if month.empty:
        st.warning(f'No transactions found for {year}.')
        return
    total_txn = month['transaction_count'].sum()
    net = month['net_amount'].sum()
    busiest = month.loc[month['transaction_count'].idxmax(), 'group_value']
    k1, k2, k3, k4 = st.columns(4)
    k1.metric('Transactions', f'{total_txn:,.0f}')
    k2.metric('Net amount', f'{net:,.2f}')
    k3.metric('Average per transaction', f'{(net / total_txn if total_txn else 0):,.2f}')
    k4.metric('Busiest month', busiest)

    m = month.assign(month=pd.to_datetime(month['group_value'] + '-01', errors='coerce'))
    a, b = st.columns(2)
    with a:
        st.markdown('**Monthly transaction count**')
        base = alt.Chart(m).encode(x=alt.X('month:T', title='month'),
                                   y=alt.Y('transaction_count:Q', title='transactions'),
                                   tooltip=['group_value', alt.Tooltip('transaction_count:Q', format=',')])
        st.altair_chart(_dark(base.mark_line(color=NAVY, strokeWidth=2.5)
                              + base.mark_point(color=GOLD, filled=True, size=45)), use_container_width=True)
    with b:
        st.markdown('**Monthly net amount**')
        st.altair_chart(_dark(alt.Chart(m).mark_area(color=NAVY, opacity=0.3, line={'color': NAVY}).encode(
            x=alt.X('month:T', title='month'), y=alt.Y('net_amount:Q', title='net amount'),
            tooltip=['group_value', alt.Tooltip('net_amount:Q', format=',.2f')])), use_container_width=True)
    a, b = st.columns(2)
    with a:
        if not mcc.empty:
            _bar(mcc, 'MCC', 'transaction_count', 'Top merchant categories (MCC)')
    with b:
        if not state.empty:
            _bar(state, 'state', 'transaction_count', 'Top merchant states')
    if not chip.empty:
        st.markdown('**Transactions by entry method**')
        st.altair_chart(_dark(alt.Chart(chip).mark_arc(innerRadius=70).encode(
            theta='transaction_count:Q',
            color=alt.Color('group_value:N', title='method', scale=alt.Scale(range=PALETTE)),
            tooltip=['group_value', alt.Tooltip('transaction_count:Q', format=',')]), 280),
            use_container_width=True)
    with st.expander('Monthly data'):
        st.dataframe(month, use_container_width=True)

# ---------------------------------------------------------------- views
# A top-level view switcher (not st.tabs): Streamlit pins st.chat_input to the
# bottom of the page only when it is called at the top level of the script,
# not inside tabs, columns, expanders or other containers.
VIEW_CHAT, VIEW_DASH = '💬 Chat', '📊 Analytics dashboard'
if hasattr(st, 'segmented_control'):
    view = st.segmented_control('View', [VIEW_CHAT, VIEW_DASH], default=VIEW_CHAT, key='view',
                                label_visibility='collapsed')
else:  # older Streamlit without segmented_control
    view = st.radio('View', [VIEW_CHAT, VIEW_DASH], horizontal=True, key='view',
                    label_visibility='collapsed')
view = view or VIEW_CHAT  # deselecting the active segment returns None

if view == VIEW_CHAT:
    st.info('Try: Show monthly transaction count and total amount for 2019, '
            'or show the profile for user 123.')
    for idx, m in enumerate(messages):
        _render_message(m, key=f'{ss.current}_{idx}')
    # Top level (no `with` container) so Streamlit pins the input to the bottom;
    # new messages below render in the main flow, above the pinned input.
    question = st.chat_input('Ask about transactions, merchants, or cardholders...')
    if question:
        history = [{'role': m['role'], 'content': m['content']} for m in messages][-10:]
        messages.append({'role': 'user', 'content': question})
        _render_message(messages[-1], key=f'{ss.current}_{len(messages) - 1}')
        started = time.perf_counter()
        try:
            if use_pav:
                with st.spinner('Planning, executing through MCP and verifying...'):
                    result = asyncio.run(ask_pav_async(question, history=history, model=model,
                                                       max_rounds=max_rounds,
                                                       max_revisions=max_revisions))
            else:
                with st.spinner('Generating SQL and executing through MCP...'):
                    result = asyncio.run(ask_async(question, history=history, model=model,
                                                   max_rounds=max_rounds))
            calls = [{'name': c.name, 'arguments': c.arguments, 'result': c.result,
                      'is_error': c.is_error, 'phase': c.phase} for c in result.tool_calls]
            messages.append({'role': 'assistant', 'content': result.answer or '_No answer returned._',
                             'tool_calls': calls, 'workflow': result.workflow,
                             'plan': result.plan, 'findings': result.findings,
                             'verdict': result.verdict, 'revisions': result.revisions,
                             'elapsed_s': time.perf_counter() - started})
            _render_message(messages[-1], key=f'{ss.current}_{len(messages) - 1}')
        except Exception as e:
            with st.chat_message('assistant'):
                st.error(f'Request failed: {e}')
                _render_elapsed(time.perf_counter() - started)
else:
    render_dashboard()
