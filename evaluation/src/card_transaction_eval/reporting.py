"""Turn evidence JSON into evaluation reports and the evaluation test matrix.

Outputs (evaluation/reports/): evaluation_report.{json,md,html}, evaluation_summary.csv,
evaluation_cases.csv, evaluation_test_matrix.{csv,md}. The Markdown report follows the
hackathon evaluation-documentation outline: testing methodology, performance metrics,
security considerations, lessons learned and recommendations.
"""
from __future__ import annotations

import csv
import html
from collections import Counter

from .utils import ERROR, FAIL, WARN, read_json, short, utc_now, write_json

SUITES = [
    ("preflight", "preflight.json", "Preflight readiness"),
    ("unit_tests", "unit_tests.json", "Offline unit and contract tests"),
    ("custom_functional", "custom_functional.json", "Custom MCP functional tests"),
    ("sql_reconciliation", "sql_reconciliation.json", "SQL reconciliation"),
    ("security_negative", "security_negative.json", "Security and negative tests"),
    ("performance", "performance.json", "Performance"),
    ("genie_benchmark", "genie_benchmark.json", "Genie benchmark"),
    ("hybrid_routing", "hybrid_routing.json", "Hybrid routing worksheet"),
    ("llm_judge", "llm_judge.json", "LLM-as-judge agent critique"),
    ("risk_scoring", "risk_scoring.json", "Rule-based fraud risk scoring"),
]

SCRIPT_FOR_SUITE = {
    "preflight": "00_preflight.py", "unit_tests": "run_all.py (unit stage)",
    "custom_functional": "01_custom_mcp_functional.py", "sql_reconciliation": "02_sql_reconciliation.py",
    "genie_benchmark": "03_genie_benchmark.py", "security_negative": "04_security_negative.py",
    "performance": "05_performance.py", "hybrid_routing": "06_hybrid_routing_worksheet.py",
    "llm_judge": "10_llm_judge.py", "risk_scoring": "11_risk_scoring.py",
}

# (case-id prefix, category, method, expected result); the longest matching prefix wins.
MATRIX_RULES = [
    ("PF-", "Readiness", "Configuration / API / SQL probe", "Dependency configured and reachable"),
    ("RS-F", "Risk scoring - functional", "MCP run_risk_sql with reference SQL", "Matches pinned scores / ranking"),
    ("RS-R", "Risk scoring - accuracy", "Indicators vs independent Databricks SQL", "Flags and ranking counts agree"),
    ("RS-S", "Risk scoring - guardrails", "MCP run_risk_sql with unsafe SQL", "Rejected, 0 SQL statements, no stack trace"),
    ("RS-P", "Risk scoring - performance", "Timed ranking SQL / agent run", "Within configured thresholds"),
    ("RS-ST", "Risk scoring - agent stability", "Chat agent (PAV) ranking question, repeated", "Reference top five every run"),
    ("RS-E", "Risk scoring - efficacy (labels)", "evaluate_risk_indicators on card samples", "Fraud rate rises with score (advisory)"),
    ("UT-", "Unit and contract", "pytest (offline, SQLite backend, stubbed LLM)", "All tests pass"),
    ("FN-", "Functional", "MCP call_tool against the live warehouse", "Well-formed, bounded, correctly filtered response"),
    ("RC-", "Accuracy", "MCP output vs independent Databricks SQL", "Counts and ids exact; amounts within 0.01"),
    ("SEC-AUTH", "Security - authentication", "HTTP POST to deployed /mcp", "HTTP 302/401/403; no MCP session"),
    ("SEC-IN", "Security - input validation", "MCP call_tool with invalid input", "MCP error, 0 SQL statements executed, no stack trace"),
    ("SEC-INJ", "Security - injection", "MCP call_tool with injection payload", "Payload bound as a literal; 0 rows returned"),
    ("SEC-LBL", "Security - data minimisation", "Scan tool catalogue and outputs", "No Is_Fraud or evaluation_labels data"),
    ("SEC-SQL-P", "Security - NL-to-SQL guardrail", "validate_sql() positive control", "Valid SELECT on the allowed table accepted"),
    ("SEC-SQL", "Security - NL-to-SQL guardrail", "validate_sql() / enforce_limit()", "Attack pattern rejected; row cap enforced"),
    ("SEC-LP", "Security - least privilege", "SHOW GRANTS for the app service principal", "SELECT only on clean table; nothing on label table"),
    ("PERF-", "Performance", "1 cold + N warm MCP calls", "0 errors and warm p95 <= threshold"),
    ("GB-", "Genie benchmark", "Genie conversation API vs reference SQL", "Numeric answer matches reference SQL"),
    ("HR-", "Hybrid routing", "Manual reviewer worksheet", "Reviewer records A/B correctness and preferred route"),
    ("LJ-CTRL", "LLM judge - calibration", "Known-bad answer scored by the judges", "Blocking judges answer 'no'"),
    ("LJ-", "LLM judge - agent critique", "PAV agent run, then MLflow make_judge on a different model",
     "Grounded and policy-compliant (blocking); good tool use and answer quality (advisory)"),
    ("RUN-ERR", "Harness", "Suite runner", "Suite completes"),
]

LESSONS = [
    "Pin `mcp>=1.10,<2`: the server uses `FastMCP`, which is not available in mcp 2.x.",
    "Behind the Databricks Apps reverse proxy, FastMCP's DNS-rebinding Host check returns 421; it is disabled "
    "because the Apps proxy already enforces OAuth, and `stateless_http` is enabled for the proxy.",
    "FastMCP serves only `/mcp`, so the app URL showed \"Not Found\"; a `/` status page and `/health` JSON were added.",
    "Databricks Apps accept OAuth tokens only (notebook/PAT tokens get 401). The evaluation therefore runs the same "
    "server code in-process against the live warehouse and probes the deployed app's authentication gate separately.",
    "Unity Catalog model services must be called through AI Gateway (`/ai-gateway/mlflow/v1/chat/completions`), not "
    "`/serving-endpoints/<name>`.",
    "The fraud label is split into a separate evaluation-labels table and the app service principal has SELECT on the "
    "clean table only, so label isolation does not depend on prompt or validator behaviour alone.",
    "The offline test suite had drifted from the code layout (`src/card_transaction_mcp/` vs `src/`), and the "
    "evaluation scripts referenced a package that did not exist; both are now fixed and run together.",
]

RECOMMENDATIONS = [
    "Describe every tool parameter with Pydantic `Annotated[..., Field(description=..., ge=..., le=...)]` so input "
    "schemas carry descriptions and bounds for agents (see FN-01 `parameters_without_description`).",
    "Return friendly validation messages (e.g. 'start must be a valid calendar date') instead of raw Python errors.",
    "Reuse SQL connections (pool or long-lived session): each backend query opens a new connection and the "
    "profile/report tools issue three to four statements.",
    "Parse generated SQL with a real parser (e.g. sqlglot) so every table reference, including comma joins, is checked.",
    "Run this evaluation as a Lakeflow Job after each deployment, and add an authenticated HTTP run (mcp_mode=http "
    "with an M2M OAuth token) against the deployed endpoint.",
    "Complete the hybrid routing worksheet with domain reviewers to confirm the tool-vs-Genie routing rules.",
]


def _esc(v) -> str:
    return ("" if v is None else str(v)).replace("|", "\\|").replace("\n", " ")


def _table(headers, rows) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(_esc(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _detail(case, n=170) -> str:
    skip = {"arguments", "records", "genie_rows", "tools", "columns", "tool", "sql", "checks", "judges", "answer"}
    d = {k: v for k, v in (case.get("details") or {}).items() if k not in skip and v not in (None, [], {})}
    return short(d, n) if d else ""


def _rule(case_id):
    matches = [r for r in MATRIX_RULES if case_id.startswith(r[0])]
    return max(matches, key=lambda r: len(r[0])) if matches else ("", "Other", "", "")


def test_matrix_rows(loaded) -> list:
    rows = []
    for key, fname, title in SUITES:
        for c in loaded.get(key, {}).get("cases", []):
            _, category, method, expected = _rule(c["case_id"])
            rows.append({"test_id": c["case_id"], "suite": title, "script": SCRIPT_FOR_SUITE.get(key, ""),
                         "category": category, "test_case": c["name"], "method": method,
                         "expected_result": expected, "actual_status": c["status"],
                         "duration_ms": c.get("duration_ms"), "evidence_file": f"evidence/{fname}",
                         "observed": _detail(c, 220)})
    return rows


def _overall(loaded) -> str:
    if not loaded:
        return "no_evidence"
    statuses = [s.get("status") for s in loaded.values()]
    if any(s in (FAIL, ERROR) for s in statuses):
        return "fail"
    return "pass_with_findings" if WARN in statuses else "pass"


def _findings(loaded):
    return [(key, c) for key, data in loaded.items() for c in data.get("cases", [])
            if c["status"] in (FAIL, ERROR, WARN)]


def _markdown(config, loaded, meta, overall) -> str:
    pf = loaded.get("preflight", {}).get("metrics", {})
    rows_n = pf.get("clean_table_rows")
    tools = next((c["details"].get("tools", []) for c in loaded.get("preflight", {}).get("cases", [])
                  if c["case_id"] == "PF-05"), [])
    md = ["# Financial Transaction Analysis MCP - Evaluation Report", "",
          f"Generated {utc_now()} - overall result: **{overall}**", ""]

    md += ["## 1. System under test", "", _table(["Item", "Value"], [
        ("MCP server", "Card Transaction Credit Card Intelligence (FastMCP, streamable HTTP, Databricks App)"),
        ("Deployed endpoint", config.custom_mcp_url),
        ("Evaluation transport", pf.get("transport")),
        ("Tools", ", ".join(tools)),
        ("Data", f"{config.clean_table} ({rows_n:,} rows)" if isinstance(rows_n, int) else config.clean_table),
        ("SQL warehouse", config.sql_warehouse_id),
        ("NL-to-SQL model", config.llm_endpoint),
        ("Genie spaces", ", ".join(f"{k}={v}" for k, v in config.genie_spaces.items())),
        ("Benchmark year", config.benchmark_year),
        ("Python / mcp", f"{meta.get('python', 'n/a')} / {meta.get('package_versions', {}).get('mcp', 'n/a')}"),
    ]), ""]

    md += ["## 2. Testing methodology", ""]
    for key, _, title in SUITES:
        if key in loaded:
            md.append(f"* **{title}** ({len(loaded[key].get('cases', []))} cases): {loaded[key].get('description', '')}")
    md += ["", "Each case records its inputs, observed outputs, status (pass / fail / error / warn / skipped / "
           "manual_review) and duration in `evaluation/evidence/*.json`; the full list is in "
           "`evaluation_test_matrix.md`.", ""]

    md += ["## 3. Results summary", ""]
    rows = []
    for key, _, title in SUITES:
        if key in loaded:
            c = loaded[key].get("metrics", {}).get("case_counts", {})
            rows.append((title, loaded[key].get("status"), c.get("pass", 0), c.get("fail", 0) + c.get("error", 0),
                         c.get("warn", 0), c.get("skipped", 0), c.get("manual_review", 0)))
    md += [_table(["Suite", "Status", "Pass", "Fail/Error", "Warn", "Skipped", "Manual"], rows), ""]
    ut = loaded.get("unit_tests", {}).get("metrics", {}).get("totals")
    if ut:
        md += [f"Unit tests: {ut['passed']} passed, {ut['failed']} failed, {ut['errors']} errors, "
               f"{ut['skipped']} skipped of {ut['tests']}.", ""]

    for key, heading in (("custom_functional", "## 4. Functional results"),
                         ("sql_reconciliation", "## 5. SQL reconciliation results")):
        cases = loaded.get(key, {}).get("cases", [])
        md += [heading, "", _table(["ID", "Case", "Status", "ms", "Evidence"],
                                   [(c["case_id"], c["name"], c["status"], c.get("duration_ms"), _detail(c)) for c in cases])
               if cases else "_Not run._", ""]

    md += ["## 6. Performance metrics", ""]
    perf = loaded.get("performance", {}).get("metrics", {})
    if perf.get("per_tool"):
        md += [_table(["Workload", "Calls", "Errors", "Cold ms", "p50 ms", "p95 ms", "Mean ms", "Max ms", "SQL stmts/call"],
                      [(k, v.get("calls"), v.get("errors"), v.get("cold_ms"), v.get("p50_ms"), v.get("p95_ms"),
                        v.get("mean_ms"), v.get("max_ms"), v.get("sql_statements_per_call"))
                       for k, v in perf["per_tool"].items()]), ""]
        ow = perf.get("overall_warm", {})
        md += [f"Overall: {perf.get('total_calls')} calls, error rate {perf.get('overall_error_rate')}, "
               f"warm p50 {ow.get('p50_ms')} ms, warm p95 {ow.get('p95_ms')} ms.", ""]
        md += [f"_{n}_" for n in loaded["performance"].get("notes", [])] + [""]
    else:
        md += ["_Not run._", ""]
    gl = loaded.get("genie_benchmark", {}).get("metrics", {}).get("latency")
    if gl and gl.get("n"):
        md += [f"Genie end-to-end answer latency: p50 {gl['p50_ms']} ms, max {gl['max_ms']} ms (n={gl['n']}).", ""]

    md += ["## 7. Security considerations", "",
           "* **Authentication**: the Databricks Apps OAuth gateway fronts `/mcp`; unauthenticated and invalid-token "
           "requests are rejected (SEC-AUTH, PF-06).",
           "* **Least privilege**: the app service principal should hold SELECT only on the clean table and nothing on "
           "the label table (SEC-LP).",
           "* **Data minimisation**: the fraud ground-truth label is held in a separate table and must not appear in the "
           "clean table, tool catalogue or tool outputs (PF-04, SEC-LBL).",
           "* **Injection resistance**: values are bound parameters; `group_by` uses an allow-list; table names are "
           "regex-validated (SEC-IN, SEC-INJ).",
           "* **NL-to-SQL guardrails**: single SELECT/WITH, allow-listed table, keyword deny-list, no comments, enforced "
           "row LIMIT, with UC grants as defence in depth (SEC-SQL).",
           "* **Bounded outputs**: every tool caps rows and fan-out (<=200 rows, <=50 related users).",
           "* **Secrets**: no PATs in the app (app identity via SDK unified auth); the evaluation reads tokens only from "
           "the environment and records only their presence.",
           "* **Human in the loop**: transaction reports are `human_review_required`; review cues are not fraud "
           "determinations.", ""]
    cases = loaded.get("security_negative", {}).get("cases", [])
    md += [_table(["ID", "Case", "Status", "Evidence"],
                  [(c["case_id"], c["name"], c["status"], _detail(c, 140)) for c in cases]) if cases else "_Not run._", ""]

    md += ["## 8. Genie benchmark", ""]
    cases = loaded.get("genie_benchmark", {}).get("cases", [])
    md += [_table(["ID", "Question", "Status", "Reference", "Genie rows", "ms"],
                  [(c["case_id"], c["name"], c["status"], c["details"].get("reference_value"),
                    short(c["details"].get("genie_rows"), 80), c.get("duration_ms")) for c in cases])
           if cases else "_Not run._", ""]
    hr = loaded.get("hybrid_routing", {})
    if hr:
        m = hr.get("metrics", {})
        md += [f"Hybrid routing worksheet: {m.get('reviewed', 0)}/{m.get('questions', 0)} questions reviewed "
               f"(`{m.get('worksheet', '')}`).", ""]

    lj = loaded.get("llm_judge", {})
    if lj.get("cases"):
        m = lj.get("metrics", {})
        md += ["## 8a. LLM-as-judge agent critique", "",
               f"Agent `{m.get('agent_model')}` judged by `{m.get('judge_model')}`; pass rate per judge: "
               + ", ".join(f"{k} {v}" for k, v in (m.get("judge_pass_rate") or {}).items()) + ".", ""]
        md += [_table(["ID", "Question", "Status", "PAV verdict", "Judges (critique)"],
                      [(c["case_id"], c["name"], c["status"], c["details"].get("pav_verdict", ""),
                        "; ".join(f"{n}={j.get('value')}: {short(j.get('rationale') or j.get('error') or '', 160)}"
                                  for n, j in (c["details"].get("judges") or {}).items()))
                       for c in lj["cases"]]), ""]
        if m.get("pav_verifier_passed_but_judge_failed"):
            md += ["PAV verifier passed but a blocking judge failed: "
                   + ", ".join(m["pav_verifier_passed_but_judge_failed"]) + ".", ""]

    md += ["## 9. Lessons learned and recommendations", "", "Lessons learned:", ""]
    md += [f"* {x}" for x in LESSONS] + ["", "Findings from this run:", ""]
    md += ([f"* `{c['case_id']}` ({key}, {c['status']}): {c['name']} - {_detail(c, 220)}" for key, c in _findings(loaded)]
           or ["* No failed, errored or warning cases."]) + [""]
    md += ["Recommendations:", ""] + [f"* {x}" for x in RECOMMENDATIONS] + [""]

    md += ["## 10. Limitations", "",
           "* In-process mode exercises the same server code and the live warehouse over an in-memory MCP transport; "
           "the Apps proxy hop is covered only by the authentication probes unless mcp_mode=http is run with an OAuth token.",
           "* LLM-backed results (ask_transactions, Genie) are non-deterministic and should be re-run before submission.",
           "* Latency is measured from a serverless notebook and includes SQL warehouse queueing.", ""]
    return "\n".join(md)


def _html(md: str) -> str:
    try:
        import markdown
        body = markdown.markdown(md, extensions=["tables"])
    except Exception:
        body = f"<pre>{html.escape(md)}</pre>"
    style = ("body{font-family:Segoe UI,Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45;color:#1b1b1b}"
             "table{border-collapse:collapse;margin:8px 0 20px;font-size:13px}th,td{border:1px solid #ccc;padding:4px 8px;"
             "vertical-align:top}th{background:#0A2240;color:#fff}h1,h2{color:#0A2240}code{background:#f3f3f3;padding:1px 4px}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>MCP Evaluation Report</title>"
            f"<style>{style}</style></head><body>{body}</body></html>")


def _write_test_matrix(out, matrix, overall) -> dict:
    cols = ["test_id", "suite", "script", "category", "test_case", "method", "expected_result",
            "actual_status", "duration_ms", "evidence_file", "observed"]
    csv_path = out / "evaluation_test_matrix.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(matrix)
    statuses = ["pass", "fail", "error", "warn", "skipped", "manual_review"]
    by_cat = Counter((m["category"], m["actual_status"]) for m in matrix)
    cats = list(dict.fromkeys(m["category"] for m in matrix))
    lines = ["# Financial Transaction Analysis MCP - Evaluation Test Matrix", "",
             f"Generated {utc_now()} - {len(matrix)} test cases - overall result: **{overall}**", "",
             "## Coverage by category", "",
             _table(["Category"] + statuses + ["Total"],
                    [[c] + [by_cat.get((c, s), 0) for s in statuses] + [sum(by_cat.get((c, s), 0) for s in statuses)]
                     for c in cats]),
             "", "## Test cases", "",
             _table(["ID", "Category", "Script", "Test case", "Method", "Expected result", "Status", "ms", "Observed"],
                    [(m["test_id"], m["category"], m["script"], m["test_case"], m["method"], m["expected_result"],
                      m["actual_status"], m["duration_ms"], m["observed"]) for m in matrix]), ""]
    md_path = out / "evaluation_test_matrix.md"
    md_path.write_text("\n".join(lines))
    return {"test_matrix_csv": csv_path, "test_matrix_md": md_path}


def generate_reports(config) -> dict:
    ev, out = config.evidence_dir, config.reports_dir
    out.mkdir(parents=True, exist_ok=True)
    loaded = {key: read_json(ev / fname) for key, fname, _ in SUITES if (ev / fname).exists()}
    meta_path = ev / "deployment_runtime_metadata.json"
    meta = read_json(meta_path) if meta_path.exists() else {}
    overall = _overall(loaded)

    report = {"generated_at_utc": utc_now(), "overall_status": overall, "metadata": meta,
              "suites": {k: {"status": v.get("status"), "case_counts": v.get("metrics", {}).get("case_counts"),
                             "metrics": {m: x for m, x in v.get("metrics", {}).items() if m != "config"}}
                         for k, v in loaded.items()}}
    paths = {"json": write_json(out / "evaluation_report.json", report)}

    paths["summary_csv"] = out / "evaluation_summary.csv"
    with paths["summary_csv"].open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["suite", "title", "status", "pass", "fail", "error", "warn", "skipped", "manual_review"])
        for key, _, title in SUITES:
            if key in loaded:
                c = loaded[key].get("metrics", {}).get("case_counts", {})
                w.writerow([key, title, loaded[key].get("status")] +
                           [c.get(s, 0) for s in ("pass", "fail", "error", "warn", "skipped", "manual_review")])

    paths["cases_csv"] = out / "evaluation_cases.csv"
    with paths["cases_csv"].open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["suite", "case_id", "name", "status", "duration_ms", "evidence"])
        for key, data in loaded.items():
            for c in data.get("cases", []):
                w.writerow([key, c["case_id"], c["name"], c["status"], c.get("duration_ms"), _detail(c, 400)])

    paths.update(_write_test_matrix(out, test_matrix_rows(loaded), overall))

    md = _markdown(config, loaded, meta, overall)
    paths["markdown"] = out / "evaluation_report.md"
    paths["markdown"].write_text(md)
    paths["html"] = out / "evaluation_report.html"
    paths["html"].write_text(_html(md))
    return paths
