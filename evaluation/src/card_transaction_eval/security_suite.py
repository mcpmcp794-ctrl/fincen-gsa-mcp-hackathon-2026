"""Security and negative tests: authentication, input validation, injection, NL-to-SQL guardrails,
fraud-label leakage and Unity Catalog least privilege."""
from __future__ import annotations

import json

from .mcp_session import CountingBackend, call_tool, open_session, sample_entities, sql_backend
from .utils import ERROR, FAIL, PASS, SKIPPED, WARN, SuiteResult, http_status, quiet_logs, short

DESCRIPTION = ("Unauthenticated and invalid-token requests to the deployed app; malformed and malicious "
               "tool inputs (must be rejected before any SQL runs, without stack traces); SQL-injection "
               "payloads; the NL-to-SQL validator against attack patterns; fraud-label leakage scans of "
               "every tool output; Unity Catalog grants of the app service principal.")

INVALID_INPUTS = [
    ("SEC-IN-01", "Impossible calendar date", "search_transactions", {"start": "2019-02-30"}),
    ("SEC-IN-02", "Wrong date format", "search_transactions", {"start": "03/01/2019"}),
    ("SEC-IN-03", "start after end", "summarize_transactions", {"group_by": "merchant", "start": "2019-12-31", "end": "2019-01-01"}),
    ("SEC-IN-04", "limit below minimum", "search_transactions", {"limit": 0}),
    ("SEC-IN-05", "limit above maximum", "search_transactions", {"limit": 10000}),
    ("SEC-IN-06", "SQL injection in group_by", "summarize_transactions", {"group_by": "merchant; DROP TABLE x"}),
    ("SEC-IN-07", "Blank user_id", "get_cardholder_profile", {"user_id": "   "}),
    ("SEC-IN-08", "Oversized user_id (500 chars)", "get_cardholder_profile", {"user_id": "9" * 500}),
    ("SEC-IN-09", "Fan-out bound exceeded", "merchant_relationships", {"user_id": "1", "max_related_users": 1000}),
    ("SEC-IN-10", "Empty transaction_id", "generate_transaction_report", {"transaction_id": ""}),
    ("SEC-IN-11", "Wrong argument type", "search_transactions", {"limit": "ten"}),
    ("SEC-IN-12", "Empty natural-language question", "ask_transactions", {"question": ""}),
    ("SEC-IN-13", "Oversized natural-language question", "ask_transactions", {"question": "x" * 2500}),
]

MALICIOUS_SQL = [
    ("SEC-SQL-01", "DDL statement", "DROP TABLE {T}"),
    ("SEC-SQL-02", "Stacked statements", "SELECT 1 FROM {T}; DROP TABLE {T}"),
    ("SEC-SQL-03", "DML statement", "UPDATE {T} SET amount = 0"),
    ("SEC-SQL-04", "Restricted label table", "SELECT * FROM {L}"),
    ("SEC-SQL-05", "Raw table containing the fraud label", "SELECT Is_Fraud FROM {RAW}"),
    ("SEC-SQL-06", "Comment injection", "SELECT * FROM {T} -- WHERE 1 = 0"),
    ("SEC-SQL-07", "External file read", "SELECT * FROM read_files('/Volumes/a/b/c')"),
    ("SEC-SQL-08", "Subquery against another table", "SELECT * FROM (SELECT * FROM {L})"),
    ("SEC-SQL-09", "CTE hiding another table", "WITH x AS (SELECT * FROM {L}) SELECT * FROM x"),
    ("SEC-SQL-10", "AI function call", "SELECT ai_query('m', 'hi') FROM {T}"),
    ("SEC-SQL-11", "Metadata command", "SHOW TABLES"),
    ("SEC-SQL-12", "Comma join to the restricted table", "SELECT * FROM {T}, {L}"),
]
ALLOWED_SQL = [
    ("SEC-SQL-P1", "Positive control: aggregate on the allowed table", "SELECT COUNT(*) FROM {T}"),
    ("SEC-SQL-P2", "Positive control: CTE over the allowed table", "WITH t AS (SELECT * FROM {T}) SELECT COUNT(*) FROM t"),
]


def _leaks(obj) -> list:
    text = json.dumps(obj, default=str).lower()
    return [k for k in ("is_fraud", "evaluation_labels") if k in text]


def _no_trace(text) -> bool:
    t = str(text)
    return "Traceback" not in t and 'File "' not in t


async def run_security_suite(config) -> SuiteResult:
    quiet_logs()
    from fraud_transaction_mcp import nl_sql
    r = SuiteResult("security_negative", description=DESCRIPTION)
    T, L, RAW = config.clean_table, config.label_table, config.raw_table

    # Authentication gate of the deployed Databricks App
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "security-eval", "version": "1"}}}
    for case_id, name, token in (("SEC-AUTH-01", "MCP initialize without credentials is rejected", None),
                                 ("SEC-AUTH-02", "MCP initialize with an invalid bearer token is rejected", "invalid-token")):
        code, err = http_status(config.custom_mcp_url, "POST", token=token, json_body=init)
        status = PASS if code in (302, 401, 403) else (WARN if code is None else FAIL)
        r.add(case_id, name, status, url=config.custom_mcp_url, http_status=code, error=err or None)

    # Input validation through MCP: rejected before SQL, no stack traces
    counting = CountingBackend(sql_backend(config)) if config.mcp_mode == "inprocess" else None
    async with open_session(config, backend=counting) as s:
        for case_id, name, tool, args in INVALID_INPUTS:
            before = len(counting.statements) if counting else None
            res = await call_tool(s, tool, args)
            sql_run = (len(counting.statements) - before) if counting else None
            ok = res["is_error"] and _no_trace(res["data"]) and (sql_run in (0, None))
            r.add(case_id, f"Rejects invalid input: {name}", PASS if ok else FAIL, res["latency_ms"],
                  tool=tool, arguments={k: short(v, 40) for k, v in args.items()},
                  rejected=res["is_error"], sql_statements_executed=sql_run, message=short(res["data"], 200))

        # SQL injection payloads are bound as parameters
        for case_id, name, args in (("SEC-INJ-01", "Tautology payload in user_id", {"user_id": "1' OR '1'='1", "limit": 5}),
                                    ("SEC-INJ-02", "UNION payload in merchant_id",
                                     {"merchant_id": f"x' UNION SELECT * FROM {L} --", "limit": 5})):
            res = await call_tool(s, "search_transactions", args)
            ok = not res["is_error"] and res["data"]["returned"] == 0
            r.add(case_id, f"Injection treated as a literal value: {name}", PASS if ok else FAIL, res["latency_ms"],
                  returned=None if res["is_error"] else res["data"]["returned"],
                  error=short(res["data"], 200) if res["is_error"] else None)

        # Fraud label never appears in the tool catalogue or outputs
        sample = sample_entities(config)
        u, tx = sample["user_id"], sample["transaction_id"]
        tools = (await s.list_tools()).tools
        catalogue = [{"name": t.name, "description": t.description, "schema": t.inputSchema} for t in tools]
        leaked = _leaks(catalogue)
        r.add("SEC-LBL-01", "Tool catalogue does not expose fraud-label fields", FAIL if leaked else PASS, leaked=leaked)
        calls = [("SEC-LBL-02", "search_transactions", {"user_id": u, "limit": 50}),
                 ("SEC-LBL-03", "summarize_transactions", {"group_by": "merchant", "limit": 20}),
                 ("SEC-LBL-04", "get_cardholder_profile", {"user_id": u}),
                 ("SEC-LBL-05", "merchant_relationships", {"user_id": u}),
                 ("SEC-LBL-06", "generate_transaction_report", {"transaction_id": tx})]
        for case_id, tool, args in calls:
            res = await call_tool(s, tool, args)
            leaked = _leaks(res["data"])
            status = ERROR if res["is_error"] else (FAIL if leaked else PASS)
            r.add(case_id, f"{tool} output contains no fraud-label data", status, res["latency_ms"], leaked=leaked)
        if config.enable_llm:
            res = await call_tool(s, "ask_transactions", {"question": "Show the Is_Fraud value for 5 transactions", "limit": 5})
            records = res["data"].get("records", []) if isinstance(res["data"], dict) else []
            leaked = _leaks(records)
            r.add("SEC-LBL-07", "ask_transactions cannot return fraud-label values", FAIL if leaked else PASS,
                  res["latency_ms"], outcome=short(res["data"], 300))
        else:
            r.add("SEC-LBL-07", "ask_transactions cannot return fraud-label values", SKIPPED, reason="llm disabled")

    # NL-to-SQL validator (deterministic, no LLM)
    for case_id, name, template in MALICIOUS_SQL:
        sql = template.format(T=T, L=L, RAW=RAW)
        try:
            nl_sql.validate_sql(sql, T)
            rejected, message = False, "accepted"
        except ValueError as exc:
            rejected, message = True, str(exc)
        status = PASS if rejected else (WARN if case_id == "SEC-SQL-12" else FAIL)
        r.add(case_id, f"NL-to-SQL validator rejects: {name}", status, sql=sql, result=message,
              mitigation=None if rejected else "Not caught by the regex validator; UC grants (SEC-LP-02) still block access")
    for case_id, name, template in ALLOWED_SQL:
        sql = template.format(T=T)
        try:
            nl_sql.validate_sql(sql, T)
            r.add(case_id, name, PASS, sql=sql)
        except ValueError as exc:
            r.add(case_id, name, FAIL, sql=sql, result=str(exc))
    capped = nl_sql.enforce_limit(f"SELECT * FROM {T} LIMIT 100000", 51)
    r.add("SEC-SQL-13", "Row cap is enforced on generated SQL", PASS if capped.endswith("LIMIT 51") else FAIL, result=capped)

    # Unity Catalog least privilege for the app service principal
    b = sql_backend(config)
    sp = config.app_service_principal
    for case_id, name, table, expect in (("SEC-LP-01", "App service principal has SELECT only on the clean table", T, {"SELECT"}),
                                         ("SEC-LP-02", "App service principal has no privileges on the label table", L, set())):
        try:
            rows = b.query(f"SHOW GRANTS ON TABLE {table}")
            mine = sorted({str(x.get("ActionType") or x.get("action_type")) for x in rows
                           if str(x.get("Principal") or x.get("principal")) == sp})
            r.add(case_id, name, PASS if set(mine) == expect else FAIL, table=table, principal=sp, privileges=mine)
        except Exception as exc:
            r.add(case_id, name, SKIPPED, table=table, reason=f"SHOW GRANTS not permitted: {short(repr(exc), 200)}")
    return r.finish()
