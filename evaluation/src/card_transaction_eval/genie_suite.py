"""Genie benchmark: questions to the Behavioral and Fraud Genie spaces, scored against reference SQL."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from .mcp_session import sql_backend
from .utils import (ERROR, FAIL, MANUAL, PASS, SKIPPED, SuiteResult, Timer, close, latency_stats,
                    numbers_in, quiet_logs, short, workspace_client)

DESCRIPTION = ("Benchmark questions are sent to each Genie space (the same spaces exposed through the "
               "managed Genie MCP server). A numeric value in Genie's result rows must match reference SQL "
               "run on the raw table; answers without numbers are left for manual review.")

# (case_id, question, reference SQL, abs tolerance, alternative scale factor)
QUESTIONS = {
    "behavioral": [
        ("GB-01", "How many transactions were there in {y}?", "SELECT COUNT(*) FROM {RAW} WHERE Year = {y}", 0.5, None),
        ("GB-02", "How many distinct cardholders made at least one transaction in {y}?",
         "SELECT COUNT(DISTINCT User) FROM {RAW} WHERE Year = {y}", 0.5, None),
        ("GB-03", "How many online transactions were there in {y}?",
         "SELECT COUNT(*) FROM {RAW} WHERE Year = {y} AND `Use Chip` = 'Online Transaction'", 0.5, None),
    ],
    "fraud": [
        ("GB-04", "How many transactions were labeled as fraud in {y}?",
         "SELECT COUNT(*) FROM {RAW} WHERE Year = {y} AND Is_Fraud = 'Yes'", 0.5, None),
        ("GB-05", "What percentage of transactions in {y} were fraudulent?",
         "SELECT 100.0 * AVG(CASE WHEN Is_Fraud = 'Yes' THEN 1 ELSE 0 END) FROM {RAW} WHERE Year = {y}", 0.005, 0.01),
    ],
}


def _extract(w, space_id, msg) -> dict:
    status = getattr(msg, "status", None)
    out = {"status": str(getattr(status, "value", status)), "genie_sql": None, "genie_text": None,
           "genie_rows": [], "rows_error": None}
    conversation_id = getattr(msg, "conversation_id", None)
    message_id = getattr(msg, "message_id", None) or getattr(msg, "id", None)
    for att in getattr(msg, "attachments", None) or []:
        text = getattr(att, "text", None)
        if text is not None and getattr(text, "content", None):
            out["genie_text"] = text.content
        query = getattr(att, "query", None)
        if query is not None:
            out["genie_sql"] = getattr(query, "query", None)
            try:
                res = w.genie.get_message_attachment_query_result(space_id, conversation_id, message_id, att.attachment_id)
                sr = res.statement_response
                out["genie_rows"] = (sr.result.data_array or []) if sr and sr.result else []
            except Exception as exc:
                out["rows_error"] = repr(exc)[:300]
    return out


async def run_genie_benchmark(config) -> SuiteResult:
    quiet_logs()
    r = SuiteResult("genie_benchmark", description=DESCRIPTION)
    r.metrics["genie_mcp_url"] = config.genie_mcp_url
    if not config.enable_genie:
        r.add("GB-00", "Genie benchmark", SKIPPED, reason="genie disabled")
        return r.finish()
    w = workspace_client()
    if w is None:
        r.add("GB-00", "Genie benchmark", SKIPPED, reason="Databricks SDK authentication unavailable")
        return r.finish()
    b, y = sql_backend(config), config.benchmark_year
    latencies = []
    for space_key, items in QUESTIONS.items():
        space_id = config.genie_spaces.get(space_key)
        for case_id, question, ref_sql, tol, alt in items:
            question, ref_sql = question.format(y=y), ref_sql.format(y=y, RAW=config.raw_table)
            name = f"[{space_key}] {question}"
            if not space_id:
                r.add(case_id, name, SKIPPED, reason=f"no space configured for {space_key}")
                continue
            try:
                expected = float(list(b.query(ref_sql)[0].values())[0])
                with Timer() as t:
                    msg = await asyncio.to_thread(w.genie.start_conversation_and_wait, space_id, question,
                                                  timeout=timedelta(seconds=config.genie_timeout_s))
                latencies.append(t.ms)
                got = _extract(w, space_id, msg)
                nums = numbers_in(got["genie_rows"])
                matched = any(close(v, expected, abs_tol=tol) or (alt and close(v, expected * alt, abs_tol=tol * alt))
                              for v in nums)
                status = PASS if matched else (FAIL if nums else MANUAL)
                r.add(case_id, name, status, t.ms, space_id=space_id, reference_sql=ref_sql,
                      reference_value=expected, genie_sql=got["genie_sql"], genie_rows=got["genie_rows"][:10],
                      genie_text=short(got["genie_text"], 400) if got["genie_text"] else None,
                      message_status=got["status"], rows_error=got["rows_error"])
            except Exception as exc:
                r.add(case_id, name, ERROR, space_id=space_id, reference_sql=ref_sql, error=repr(exc)[:400])
    if latencies:
        r.metrics["latency"] = latency_stats(latencies)
    return r.finish()
