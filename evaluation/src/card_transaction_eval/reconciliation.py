"""SQL reconciliation: MCP tool outputs vs independently written Databricks SQL.

Reference queries deliberately use different SQL than the tools (year() and
date_format() instead of the tools' range predicates and SUBSTR), so a shared
bug in predicate construction would surface as a mismatch.
"""
from __future__ import annotations

from .mcp_session import call_tool, open_session, sample_entities, sql_backend
from .utils import ERROR, FAIL, PASS, SKIPPED, SuiteResult, Timer, close, numbers_in, quiet_logs, short

DESCRIPTION = ("MCP tool outputs compared with independently written Databricks SQL on the same "
               "warehouse (exact match for counts/ids, 0.01 tolerance for amounts), plus raw-vs-clean "
               "row-count reconciliation of the data-preparation pipeline.")


async def _case(r, case_id, name, fn):
    with Timer() as t:
        try:
            status, details = await fn()
        except Exception as exc:  # recorded so one broken case cannot hide the others
            status, details = ERROR, {"error": repr(exc)[:400]}
    r.add(case_id, name, status, t.ms, **details)


def _data(res):
    if res["is_error"]:
        raise RuntimeError(f"{res['tool']} returned an MCP error: {short(res['data'], 300)}")
    return res["data"]


async def run_reconciliation(config) -> SuiteResult:
    quiet_logs()
    r = SuiteResult("sql_reconciliation", description=DESCRIPTION)
    b = sql_backend(config)
    T, RAW, y = config.clean_table, config.raw_table, config.benchmark_year
    start, end = f"{y}-01-01", f"{y}-12-31"
    sample = sample_entities(config, b)
    u, tx = sample["user_id"], sample["transaction_id"]
    r.metrics["sample_entities"] = sample
    year_count = {}

    def scalar(sql, params=()):
        return list(b.query(sql, list(params))[0].values())[0]

    def clean_year_count():
        if "clean" not in year_count:
            year_count["clean"] = int(scalar(f"SELECT COUNT(*) FROM {T} WHERE year(transaction_time) = ?", [y]))
        return year_count["clean"]

    async def rc01():
        raw, clean = int(scalar(f"SELECT COUNT(*) FROM {RAW}")), int(scalar(f"SELECT COUNT(*) FROM {T}"))
        return (PASS if raw == clean else FAIL), {"raw_rows": raw, "clean_rows": clean, "difference": raw - clean}
    await _case(r, "RC-01", "Row count: raw source table vs clean table (no rows lost in preparation)", rc01)

    async def rc02():
        raw = int(scalar(f"SELECT COUNT(*) FROM {RAW} WHERE Year = ?", [y]))
        clean = clean_year_count()
        return (PASS if raw == clean else FAIL), {"year": y, "raw_rows": raw, "clean_rows": clean}
    await _case(r, "RC-02", f"Row count for {y}: raw Year column vs clean transaction_time", rc02)

    async with open_session(config) as s:
        async def rc03():
            d = _data(await call_tool(s, "summarize_transactions", {"group_by": "merchant", "start": start, "end": end, "limit": 10}))
            ref = b.query(f"SELECT merchant_id AS g, COUNT(*) AS n, SUM(amount) AS s FROM {T} "
                          "WHERE year(transaction_time) = ? GROUP BY merchant_id ORDER BY n DESC, g LIMIT 10", [y])
            tool = [(str(x["group_value"]), int(x["transaction_count"]), float(x["net_amount"])) for x in d["records"]]
            sql = [(str(x["g"]), int(x["n"]), float(x["s"])) for x in ref]
            mism = [{"rank": i + 1, "tool": a, "sql": c} for i, (a, c) in enumerate(zip(tool, sql))
                    if a[0] != c[0] or a[1] != c[1] or not close(a[2], c[2])]
            ok = len(tool) == len(sql) == 10 and not mism
            return (PASS if ok else FAIL), {"compared_rows": len(sql), "mismatches": mism[:5], "top_merchant": sql[0] if sql else None}
        await _case(r, "RC-03", f"summarize_transactions(merchant, {y}) top 10 vs SQL GROUP BY", rc03)

        async def rc04():
            d = _data(await call_tool(s, "summarize_transactions", {"group_by": "month", "start": start, "end": end, "limit": 100}))
            ref = b.query(f"SELECT date_format(transaction_time, 'yyyy-MM') AS g, COUNT(*) AS n, SUM(amount) AS s "
                          f"FROM {T} WHERE year(transaction_time) = ? GROUP BY 1", [y])
            tool = {str(x["group_value"]): (int(x["transaction_count"]), float(x["net_amount"])) for x in d["records"]}
            sql = {str(x["g"]): (int(x["n"]), float(x["s"])) for x in ref}
            mism = [{"month": m, "tool": tool.get(m), "sql": v} for m, v in sorted(sql.items())
                    if m not in tool or tool[m][0] != v[0] or not close(tool[m][1], v[1])]
            extra = sorted(set(tool) - set(sql))
            ok = len(sql) == 12 and not mism and not extra
            return (PASS if ok else FAIL), {"months": len(sql), "mismatches": mism[:5], "unexpected_months": extra}
        await _case(r, "RC-04", f"summarize_transactions(month, {y}) vs SQL date_format() for all 12 months", rc04)

        async def rc05():
            d = _data(await call_tool(s, "summarize_transactions", {"group_by": "chip", "start": start, "end": end, "limit": 100}))
            total = sum(int(x["transaction_count"]) for x in d["records"])
            expected = clean_year_count()
            return (PASS if total == expected else FAIL), {
                "channels": {str(x["group_value"]): x["transaction_count"] for x in d["records"]},
                "sum_of_groups": total, "sql_year_count": expected}
        await _case(r, "RC-05", f"summarize_transactions(chip, {y}): channel counts sum to the SQL year total", rc05)

        async def rc06():
            d = _data(await call_tool(s, "get_cardholder_profile", {"user_id": u}))
            st = d["statistics"]
            ref = b.query(f"SELECT COUNT(*) AS n, COUNT(DISTINCT card_key) AS cards, COUNT(DISTINCT merchant_id) AS m, "
                          "SUM(amount) AS s, CAST(MIN(transaction_time) AS STRING) AS f, "
                          f"CAST(MAX(transaction_time) AS STRING) AS l FROM {T} WHERE user_id = ?", [u])[0]
            checks = {"transaction_count": int(st["transaction_count"]) == int(ref["n"]),
                      "cards": int(st["cards"]) == int(ref["cards"]),
                      "merchants": int(st["merchants"]) == int(ref["m"]),
                      "net_spend": close(st["net_spend"], ref["s"]),
                      "first_activity": str(st["first_activity"])[:19] == str(ref["f"])[:19],
                      "last_activity": str(st["last_activity"])[:19] == str(ref["l"])[:19]}
            return (PASS if all(checks.values()) else FAIL), {"checks": checks, "tool": st, "sql": ref}
        await _case(r, "RC-06", f"get_cardholder_profile({u}) statistics vs SQL aggregates", rc06)

        async def rc07():
            n = int(scalar(f"SELECT COUNT(*) FROM {T} WHERE user_id = ? AND year(transaction_time) = ?", [u, y]))
            d = _data(await call_tool(s, "search_transactions", {"user_id": u, "start": start, "end": end, "limit": 200}))
            ok = d["returned"] == min(n, 200) and bool(d["truncated"]) == (n > 200)
            return (PASS if ok else FAIL), {"sql_count": n, "returned": d["returned"], "truncated": d["truncated"]}
        await _case(r, "RC-07", f"search_transactions(user {u}, {y}): returned/truncated vs SQL count", rc07)

        async def rc08():
            d = _data(await call_tool(s, "generate_transaction_report", {"transaction_id": tx}))
            ref = b.query(f"SELECT user_id, merchant_id, amount, CAST(transaction_time AS STRING) AS ts, source_row_number "
                          f"FROM {T} WHERE transaction_id = ?", [tx])
            t_ = d.get("transaction") or {}
            ref0 = ref[0] if ref else {}
            checks = {"found": d.get("found") is True and len(ref) == 1,
                      "user_id": str(t_.get("user_id")) == str(ref0.get("user_id")),
                      "merchant_id": str(t_.get("merchant_id")) == str(ref0.get("merchant_id")),
                      "amount": close(t_.get("amount"), ref0.get("amount")),
                      "transaction_time": str(t_.get("transaction_time"))[:19] == str(ref0.get("ts"))[:19],
                      "source_row_number": str(t_.get("source_row_number")) == str(ref0.get("source_row_number"))}
            return (PASS if all(checks.values()) else FAIL), {"checks": checks}
        await _case(r, "RC-08", f"generate_transaction_report({tx}) fields vs SQL row", rc08)

        async def rc09():
            d = _data(await call_tool(s, "merchant_relationships", {"user_id": u, "max_merchants": 5, "max_related_users": 10}))
            ref_m = b.query(f"SELECT merchant_id, COUNT(*) AS n FROM {T} WHERE user_id = ? "
                            "GROUP BY merchant_id ORDER BY n DESC, merchant_id LIMIT 5", [u])
            tool_m = [(str(x["merchant_id"]), int(x["purchase_count"])) for x in d["merchants"]]
            sql_m = [(str(x["merchant_id"]), int(x["n"])) for x in ref_m]
            names = [m for m, _ in sql_m]
            placeholders = ",".join("?" * len(names))
            mism = []
            for ru in d["related_users"][:3]:
                ref = b.query(f"SELECT COUNT(DISTINCT merchant_id) AS sm, COUNT(*) AS st FROM {T} "
                              f"WHERE user_id = ? AND merchant_id IN ({placeholders})", [str(ru["user_id"])] + names)[0]
                if int(ref["sm"]) != int(ru["shared_merchants"]) or int(ref["st"]) != int(ru["shared_merchant_transactions"]):
                    mism.append({"user_id": ru["user_id"], "tool": ru, "sql": ref})
            ok = tool_m == sql_m and not mism and d["related_users"] and all(str(x["user_id"]) != u for x in d["related_users"])
            return (PASS if ok else FAIL), {"seed_merchants_match": tool_m == sql_m,
                                            "related_users_checked": min(3, len(d["related_users"])), "mismatches": mism}
        await _case(r, "RC-09", f"merchant_relationships({u}): seed merchants and shared-merchant counts vs SQL", rc09)

        if not config.enable_llm:
            r.add("RC-10", "ask_transactions year count vs SQL", SKIPPED, reason="llm disabled")
        else:
            async def rc10():
                d = _data(await call_tool(s, "ask_transactions", {"question": f"How many transactions were there in {y}?", "limit": 5}))
                expected = clean_year_count()
                ok = any(close(v, expected, abs_tol=0.5) for v in numbers_in(d.get("records")))
                return (PASS if ok else FAIL), {"generated_sql": d.get("sql"), "records": d.get("records"), "sql_year_count": expected}
            await _case(r, "RC-10", f"ask_transactions('How many transactions were there in {y}?') vs SQL count", rc10)
    return r.finish()
