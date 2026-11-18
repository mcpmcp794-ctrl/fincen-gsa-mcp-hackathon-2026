"""Performance: per-tool latency, error rate and SQL statements per call."""
from __future__ import annotations

from .mcp_session import CountingBackend, call_tool, open_session, sample_entities, sql_backend, transport_label
from .utils import FAIL, PASS, WARN, SuiteResult, latency_stats, quiet_logs

DESCRIPTION = ("Each workload runs once cold, then N warm iterations through one MCP session. Reported: "
               "min/p50/p95/mean/max latency, error rate and SQL statements per call. A workload passes "
               "with zero errors and warm p95 under the configured threshold.")


async def run_performance_suite(config) -> SuiteResult:
    quiet_logs()
    r = SuiteResult("performance", description=DESCRIPTION)
    y = config.benchmark_year
    sample = sample_entities(config)
    u, tx = sample["user_id"], sample["transaction_id"]
    workloads = [
        ("PERF-01", "search_transactions", {"user_id": u, "start": f"{y}-01-01", "end": f"{y}-12-31", "limit": 30}, False),
        ("PERF-02", "summarize_transactions", {"group_by": "merchant", "start": f"{y}-01-01", "end": f"{y}-12-31", "limit": 20}, False),
        ("PERF-03", "summarize_transactions", {"group_by": "month", "limit": 100}, False),
        ("PERF-04", "get_cardholder_profile", {"user_id": u}, False),
        ("PERF-05", "merchant_relationships", {"user_id": u}, False),
        ("PERF-06", "generate_transaction_report", {"transaction_id": tx}, False),
    ]
    if config.enable_llm:
        workloads.append(("PERF-07", "ask_transactions", {"question": "What was the total transaction amount per year?", "limit": 20}, True))

    counting = CountingBackend(sql_backend(config)) if config.mcp_mode == "inprocess" else None
    per_tool, all_warm, total_calls, total_errors = {}, [], 0, 0
    async with open_session(config, backend=counting) as s:
        for case_id, tool, args, is_llm in workloads:
            iterations = config.performance_llm_iterations if is_llm else config.performance_iterations
            threshold = config.p95_llm_threshold_ms if is_llm else config.p95_threshold_ms
            label = tool + (f"[{args['group_by']}, {y if 'start' in args else 'all years'}]" if "group_by" in args else "")
            cold = await call_tool(s, tool, args)
            warm, errors, statements = [], int(cold["is_error"]), []
            for _ in range(iterations):
                before = len(counting.statements) if counting else 0
                res = await call_tool(s, tool, args)
                if counting:
                    statements.append(len(counting.statements) - before)
                errors += int(res["is_error"])
                warm.append(res["latency_ms"])
            stats = latency_stats(warm)
            stats.update({"cold_ms": cold["latency_ms"], "calls": iterations + 1, "errors": errors,
                          "error_rate": round(errors / (iterations + 1), 3),
                          "sql_statements_per_call": max(statements) if statements else None,
                          "p95_threshold_ms": threshold})
            per_tool[label] = stats
            all_warm.extend(warm)
            total_calls += iterations + 1
            total_errors += errors
            status = FAIL if errors else (PASS if stats["p95_ms"] <= threshold else WARN)
            r.add(case_id, f"Latency and reliability: {label}", status, stats["p50_ms"], **stats)
    r.metrics.update({"transport": transport_label(config), "per_tool": per_tool,
                      "overall_warm": latency_stats(all_warm), "total_calls": total_calls,
                      "total_errors": total_errors,
                      "overall_error_rate": round(total_errors / total_calls, 4) if total_calls else None})
    r.notes.append("Latency is measured from the evaluation client and is dominated by SQL warehouse execution; "
                   "each backend query opens a new SQL connection. In-process mode excludes the Apps proxy hop.")
    return r.finish()
