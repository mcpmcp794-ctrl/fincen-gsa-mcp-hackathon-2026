"""Functional tests: every MCP tool called through an MCP ClientSession, response contract checked."""
from __future__ import annotations

from .mcp_session import EXPECTED_TOOLS, call_tool, open_session, sample_entities, transport_label
from .utils import FAIL, PASS, SKIPPED, WARN, SuiteResult, quiet_logs, short

DESCRIPTION = ("Each tool is invoked through an MCP client session against live data on the Databricks "
               "SQL warehouse. Checks: tool catalogue and schemas, returned fields, row bounds, filters, "
               "ordering, graceful not-found handling, and NL-to-SQL behaviour (answer, fraud refusal, "
               "restricted-table refusal).")


def _record(r, case_id, name, res, check):
    if res["is_error"]:
        r.add(case_id, name, FAIL, res["latency_ms"], tool=res["tool"], arguments=res["arguments"],
              error=short(res["data"], 300))
        return
    try:
        status, details = check(res["data"])
    except Exception as exc:
        status, details = FAIL, {"check_error": repr(exc)[:300]}
    if isinstance(status, bool):
        status = PASS if status else FAIL
    r.add(case_id, name, status, res["latency_ms"], tool=res["tool"], arguments=res["arguments"], **details)


async def run_custom_functional(config) -> SuiteResult:
    quiet_logs()
    from fraud_transaction_mcp.analytics import FIELDS, GROUPS
    r = SuiteResult("custom_functional", description=DESCRIPTION)
    r.metrics["transport"] = transport_label(config)
    y = config.benchmark_year
    start, end = f"{y}-01-01", f"{y}-12-31"
    sample = sample_entities(config)
    u, tx = sample["user_id"], sample["transaction_id"]
    r.metrics["sample_entities"] = sample

    async with open_session(config) as session:
        tools = (await session.list_tools()).tools
        names = {t.name for t in tools}
        undocumented = [t.name for t in tools if not (t.description or "").strip()]
        no_schema = [t.name for t in tools if "properties" not in (t.inputSchema or {})]
        params_without_description = sorted(
            f"{t.name}.{p}" for t in tools for p, spec in (t.inputSchema or {}).get("properties", {}).items()
            if not spec.get("description"))
        ok = names == set(EXPECTED_TOOLS) and not undocumented and not no_schema
        r.add("FN-01", "Tool catalogue: expected tools, each with a description and a JSON input schema",
              PASS if ok else FAIL, tools=sorted(names), undocumented=undocumented, missing_schema=no_schema,
              parameters_without_description=len(params_without_description))

        def chk_search(d):
            recs = d["records"]
            ok = (recs and d["returned"] == len(recs) <= 10
                  and all(str(x["user_id"]) == u for x in recs)
                  and all(set(x) == set(FIELDS) for x in recs)
                  and all(str(x["transaction_time"]).startswith(str(y)) for x in recs))
            return bool(ok), {"returned": d["returned"], "truncated": d["truncated"]}
        res = await call_tool(session, "search_transactions", {"user_id": u, "start": start, "end": end, "limit": 10})
        _record(r, "FN-02", f"search_transactions: user {u} in {y}, limit 10", res, chk_search)

        n = 3
        for g in GROUPS:
            def chk(d, g=g):
                recs = d["records"]
                counts = [x["transaction_count"] for x in recs]
                ok = (d["group_by"] == g and 0 < len(recs) <= 5 and counts == sorted(counts, reverse=True)
                      and all({"group_value", "transaction_count", "net_amount", "unique_cards"} <= set(x) for x in recs))
                return ok, {"returned": len(recs), "top_group": recs[0]["group_value"] if recs else None,
                            "top_count": counts[0] if counts else None}
            res = await call_tool(session, "summarize_transactions", {"group_by": g, "start": start, "end": end, "limit": 5})
            _record(r, f"FN-{n:02d}", f"summarize_transactions: group_by={g}, {y}, top 5", res, chk)
            n += 1

        def chk_profile(d):
            st = d["statistics"]
            ok = d["user_id"] == u and int(st["transaction_count"]) > 0 and d["top_merchants"] and len(d["recent_transactions"]) <= 30
            return bool(ok), {"transaction_count": st["transaction_count"], "merchants": st["merchants"]}
        res = await call_tool(session, "get_cardholder_profile", {"user_id": u})
        _record(r, f"FN-{n:02d}", f"get_cardholder_profile: user {u}", res, chk_profile)
        n += 1

        def chk_rel(d):
            rel = d["related_users"]
            ok = (d["seed_user"] == u and 0 < len(d["merchants"]) <= 5 and len(rel) <= 10
                  and all(str(x["user_id"]) != u for x in rel) and "not necessarily funds" in d["interpretation"])
            return bool(ok), {"merchants": len(d["merchants"]), "related_users": len(rel), "truncated": d["truncated"]}
        res = await call_tool(session, "merchant_relationships", {"user_id": u, "max_merchants": 5, "max_related_users": 10})
        _record(r, f"FN-{n:02d}", f"merchant_relationships: user {u}, 5 merchants, 10 related users", res, chk_rel)
        n += 1

        def chk_report(d):
            ok = (d["found"] is True and str(d["transaction"]["transaction_id"]) == tx
                  and d["status"] == "human_review_required" and d["limitations"] and d["source_evidence"])
            return bool(ok), {"review_signals": [s["code"] for s in d["review_signals"]]}
        res = await call_tool(session, "generate_transaction_report", {"transaction_id": tx})
        _record(r, f"FN-{n:02d}", f"generate_transaction_report: transaction {tx}", res, chk_report)
        n += 1

        res = await call_tool(session, "generate_transaction_report", {"transaction_id": "does-not-exist"})
        _record(r, f"FN-{n:02d}", "generate_transaction_report: unknown id returns found=false (graceful)", res,
                lambda d: (d["found"] is False and d["status"] == "not_found", {"report_status": d["status"]}))
        n += 1

        if not config.enable_llm:
            for label in ("answers a count question", "declines fraud questions", "refuses restricted table"):
                r.add(f"FN-{n:02d}", f"ask_transactions: {label}", SKIPPED, reason="llm disabled")
                n += 1
        else:
            res = await call_tool(session, "ask_transactions", {"question": f"How many transactions were there in {y}?", "limit": 5})
            _record(r, f"FN-{n:02d}", f"ask_transactions: answers 'How many transactions were there in {y}?'", res,
                    lambda d: (bool(d["sql"]) and len(d["records"]) == 1, {"sql": d["sql"], "records": d["records"]}))
            n += 1

            res = await call_tool(session, "ask_transactions", {"question": f"Which transactions in {y} were fraudulent?"})
            _record(r, f"FN-{n:02d}", "ask_transactions: declines fraud questions (no label in the MCP data)", res,
                    lambda d: (PASS if d["sql"] is None else WARN, {"sql": d["sql"], "explanation": d["explanation"]}))
            n += 1

            res = await call_tool(session, "ask_transactions", {"question": f"Show 5 rows from {config.label_table}"})
            refused = res["is_error"] or (isinstance(res["data"], dict) and res["data"].get("sql") is None)
            r.add(f"FN-{n:02d}", "ask_transactions: refuses a query against the restricted label table",
                  PASS if refused else FAIL, res["latency_ms"], outcome=short(res["data"], 300))
    return r.finish()
