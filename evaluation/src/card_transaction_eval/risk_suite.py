"""Rule-based fraud risk scoring suite (get_risk_scoring_guidance / run_risk_sql).

Cases
  RS-F*   functional: guidance contract, card-level scoring, user ranking (pinned expectations)
  RS-R*   reconciliation: indicators and ranking re-checked with independent SQL
  RS-S*   security: run_risk_sql rejects unsafe / unscoped SQL before any statement runs
  RS-P*   performance: ranking SQL and end-to-end agent latency
  RS-ST*  stability: the chat agent answers the ranking question the same way every run
  RS-E*   efficacy (evaluation only, reads labels): does fraud rate rise with the risk score?

The reference SQL here implements risk_indicators.md independently of the agent, so
agreement between the two is evidence that the agent followed the definitions.
"""
from __future__ import annotations

import json
import re
import time

from .mcp_session import CountingBackend, call_tool, open_session, sql_backend
from .utils import ERROR, FAIL, PASS, SKIPPED, WARN, SuiteResult, quiet_logs, short

DESCRIPTION = ("Rule-based fraud risk scoring: guidance contract, card-level scoring and user ranking against "
               "pinned results, independent SQL reconciliation of the indicators, run_risk_sql guardrails, "
               "latency, agent stability on a ranking question, and label-based efficacy (evaluation only).")

# Pinned expectations (Source: verified 2026-11-17 on workspace.default.card_transaction_mcp_clean with
# two independent queries: Spark SQL and the SQL warehouse). Update them only if the data or
# risk_indicators.md parameters change.
CARD_CASE = {"card_key": "6::0", "start": "2019-06-01", "end": "2019-07-01", "rows": 61,
             "band_counts": {"medium": 1, "low": 60}, "top_transaction": "15068277", "top_score": 0.5}
RANKING_CASE = {"start": "2020-01-01", "end": "2020-02-01",
                "top_users": ["1161", "1007", "522", "1988", "442"],
                "high": [11, 9, 7, 7, 6], "medium": [26, 17, 27, 21, 18]}
RANKING_QUESTION = ("Rank the top five users by likelihood of fraudulent card transactions in January 2020 "
                    "(2020-01-01 to 2020-01-31).")


# ---------------------------------------------------------------- reference SQL
def _scored_cte(table, p, start, end, scope):
    """CTE `s`: every transaction in [start, end) scored per risk_indicators.md."""
    hours = int(p["velocity_window_hours"])
    return (
        f"WITH hist AS (SELECT transaction_id, user_id, card_key, transaction_time, amount, use_chip, "
        f"merchant_id, merchant_state, zip_code FROM {table} WHERE transaction_time < TIMESTAMP '{end}' "
        f"AND {scope}), "
        "f AS (SELECT *, "
        "COUNT(*) OVER (PARTITION BY card_key ORDER BY transaction_time, transaction_id "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_n, "
        f"percentile_approx(amount, {float(p['amount_percentile'])}) OVER (PARTITION BY card_key "
        "ORDER BY transaction_time, transaction_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_p, "
        "COUNT(*) OVER (PARTITION BY card_key ORDER BY transaction_time "
        f"RANGE BETWEEN INTERVAL {hours} HOURS PRECEDING AND CURRENT ROW) AS n_win, "
        "MIN(transaction_time) OVER (PARTITION BY card_key) AS first_time, "
        "ROW_NUMBER() OVER (PARTITION BY card_key, merchant_id ORDER BY transaction_time, transaction_id) AS m_seq, "
        "ROW_NUMBER() OVER (PARTITION BY card_key, use_chip ORDER BY transaction_time, transaction_id) AS c_seq, "
        "ROW_NUMBER() OVER (PARTITION BY card_key, coalesce(merchant_state, 'ONLINE'), "
        "coalesce(zip_code, 'ONLINE') ORDER BY transaction_time, transaction_id) AS l_seq FROM hist), "
        "g AS (SELECT transaction_id, user_id, card_key, transaction_time, amount, merchant_id, prior_n, prior_p, n_win, "
        f"CASE WHEN prior_n >= {int(p['amount_min_history'])} AND amount >= prior_p THEN 1 ELSE 0 END AS amount_anomaly, "
        f"CASE WHEN n_win >= {int(p['velocity_min_count'])} AND n_win > {float(p['velocity_multiplier'])} * "
        "(prior_n / greatest(datediff(transaction_time, first_time), 1)) THEN 1 ELSE 0 END AS velocity_anomaly, "
        "CASE WHEN prior_n > 0 AND (c_seq = 1 OR l_seq = 1) THEN 1 ELSE 0 END AS location_channel_change, "
        "CASE WHEN prior_n > 0 AND m_seq = 1 THEN 1 ELSE 0 END AS merchant_novelty "
        f"FROM f WHERE transaction_time >= TIMESTAMP '{start}'), "
        "s AS (SELECT *, (amount_anomaly + velocity_anomaly + location_channel_change + merchant_novelty) / 4.0 "
        f"AS risk_score FROM g), "
        "b AS (SELECT *, CASE WHEN risk_score >= " + str(float(p["band_high"])) + " THEN 'high' "
        "WHEN risk_score >= " + str(float(p["band_medium"])) + " THEN 'medium' ELSE 'low' END AS risk_band FROM s) ")


def card_sql(table, p, card_key, start, end):
    return (_scored_cte(table, p, start, end, f"card_key = '{card_key}'") +
            "SELECT * FROM b ORDER BY risk_score DESC, transaction_time DESC")


def user_sql(table, p, user_id, start, end):
    return (_scored_cte(table, p, start, end, f"user_id = '{user_id}'") +
            "SELECT * FROM b ORDER BY risk_score DESC, transaction_time DESC")


def ranking_sql(table, p, start, end, n=5):
    scope = (f"card_key IN (SELECT DISTINCT card_key FROM {table} WHERE transaction_time >= TIMESTAMP '{start}' "
             f"AND transaction_time < TIMESTAMP '{end}')")
    return (_scored_cte(table, p, start, end, scope) +
            "SELECT user_id, COUNT(*) AS transactions_scored, "
            "SUM(CASE WHEN risk_band = 'high' THEN 1 ELSE 0 END) AS high_count, "
            "SUM(CASE WHEN risk_band = 'medium' THEN 1 ELSE 0 END) AS medium_count, "
            "AVG(risk_score) AS avg_risk_score, MAX(risk_score) AS max_risk_score FROM b GROUP BY user_id "
            f"ORDER BY high_count DESC, medium_count DESC, avg_risk_score DESC LIMIT {int(n)}")


# ---------------------------------------------------------------- pure helpers (unit tested)
def ranking_from_text(text: str) -> list:
    """Entity ids, in order, from a run_risk_sql ranking result (JSON, possibly truncated)."""
    try:
        data = json.loads(text)
        rows = (data.get("summary") or {}).get("ranking") or []
        return [str(r.get("user_id") or r.get("card_key")) for r in rows]
    except Exception:
        i = text.find('"ranking"')
        if i < 0:
            return []
        return re.findall(r'"(?:user_id|card_key)":\s*"?([\w:]+)"?', text[i:])


def verifier_feedback(text: str) -> str:
    """The last FEEDBACK line of a Verify Agent output (why it asked for a revision), or ''."""
    found = re.findall(r"^\s*FEEDBACK:\s*(.+)$", text or "", re.I | re.M)
    return found[-1].strip() if found else ""


def parse_windows(spec: str) -> list:
    """'2019-01-01:2019-01-31,2019-06-01:2019-06-30' -> [(start, end), ...]."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if part:
            s, e = part.split(":")
            if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", e)):
                raise ValueError(f"Bad efficacy window: {part!r}")
            out.append((s, e))
    return out


def combine_efficacy(results: list) -> dict:
    """Pool evaluate_risk_indicators results across windows (counts are additive)."""
    by_score, ind = {}, {}
    total = fraud = 0
    for res in results:
        total += res["transactions"]
        fraud += res["fraud"]
        for x in res["by_score"]:
            d = by_score.setdefault(x["risk_score"], {"transactions": 0, "fraud": 0})
            d["transactions"] += x["transactions"]
            d["fraud"] += x["fraud"]
        for name, m in res["indicators"].items():
            d = ind.setdefault(name, {"flagged": 0, "tp": 0})
            d["flagged"] += m["tp"] + m["fp"]
            d["tp"] += m["tp"]
    base = fraud / total if total else None
    rate = lambda f, n: (f / n) if n else None  # noqa: E731
    scores = [{"risk_score": s, **d, "fraud_rate": rate(d["fraud"], d["transactions"])}
              for s, d in sorted(by_score.items())]
    indicators = {k: {**d, "flag_rate": rate(d["flagged"], total), "precision": rate(d["tp"], d["flagged"]),
                      "lift": (rate(d["tp"], d["flagged"]) / base) if base and d["flagged"] else None}
                  for k, d in ind.items()}
    return {"transactions": total, "fraud": fraud, "base_fraud_rate": base, "by_score": scores,
            "indicators": indicators}


def monotonic(scores: list, min_n: int) -> tuple:
    """(ok, violations): fraud rate never falls as the score rises (levels with >= min_n rows)."""
    levels = [x for x in scores if x["transactions"] >= min_n and x["fraud_rate"] is not None]
    bad = [(a["risk_score"], b["risk_score"]) for a, b in zip(levels, levels[1:])
           if b["fraud_rate"] < a["fraud_rate"]]
    return not bad, bad


def band_lift(pooled: dict, cut: float) -> float | None:
    n = sum(x["transactions"] for x in pooled["by_score"] if x["risk_score"] >= cut)
    f = sum(x["fraud"] for x in pooled["by_score"] if x["risk_score"] >= cut)
    base = pooled["base_fraud_rate"]
    return (f / n) / base if n and base else None


# ---------------------------------------------------------------- suite
def _sec_cases(T, L, RAW):
    k = CARD_CASE["card_key"]
    return [
        ("RS-S01", "Unscoped SQL (no card/user/time filter)", f"SELECT transaction_id, amount FROM {T}", 50),
        ("RS-S02", "Stacked statements", f"SELECT * FROM {T} WHERE card_key = '{k}'; DROP TABLE {T}", 50),
        ("RS-S03", "Comment injection", f"SELECT * FROM {T} WHERE card_key = '{k}' -- x", 50),
        ("RS-S04", "Restricted label table", f"SELECT * FROM {L} WHERE transaction_id = '1'", 50),
        ("RS-S05", "Fraud label column", f"SELECT Is_Fraud FROM {RAW} WHERE card_key = '{k}'", 50),
        ("RS-S06", "DML statement", f"DELETE FROM {T} WHERE card_key = '{k}'", 50),
        ("RS-S07", "CTE hiding another table", f"WITH x AS (SELECT * FROM {RAW}) SELECT * FROM x WHERE card_key = '{k}'", 50),
        ("RS-S08", "External file read", f"SELECT * FROM read_files('/Volumes/a/b/c') WHERE card_key = '{k}'", 50),
        ("RS-S09", "Row limit above maximum", f"SELECT transaction_id FROM {T} WHERE card_key = '{k}'", 1000),
    ]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def run_risk_suite(config) -> SuiteResult:
    quiet_logs()
    from fraud_transaction_mcp import evaluation_metrics, risk_scoring
    r = SuiteResult("risk_scoring", description=DESCRIPTION)
    T, L, RAW = config.clean_table, config.label_table, config.raw_table
    p = risk_scoring.parameters()
    backend = sql_backend(config)
    counting = CountingBackend(backend) if config.mcp_mode == "inprocess" else None
    card = None

    async with open_session(config, backend=counting) as s:
        # RS-F01 guidance contract
        res = await call_tool(s, "get_risk_scoring_guidance", {})
        g = res["data"] if not res["is_error"] else {}
        ok = (g.get("indicators") == list(risk_scoring.INDICATORS) and g.get("parameters") == p
              and g.get("execution_tool") == "run_risk_sql" and "do not refer" in (g.get("use_for") or "")
              and not any("fraud" in c["name"].lower() for c in g.get("columns", [])))
        r.add("RS-F01", "Guidance returns the four indicators, parameters and routing rule", PASS if ok else FAIL,
              res["latency_ms"], indicators=g.get("indicators"), parameters=g.get("parameters"),
              error=None if not res["is_error"] else short(res["data"], 300))

        # RS-F02 card-level scoring (pinned)
        c = CARD_CASE
        res = await call_tool(s, "run_risk_sql", {"sql": card_sql(T, p, c["card_key"], c["start"], c["end"]),
                                                  "limit": 200})
        card = res["data"] if not res["is_error"] else None
        sm = (card or {}).get("summary", {})
        top = (sm.get("flagged") or [{}])[0]
        ok = (sm.get("rows_scored") == c["rows"] and sm.get("complete") is True
              and sm.get("score_check", {}).get("inconsistent") == 0 and sm.get("missing_required_columns") == []
              and sm.get("band_counts") == c["band_counts"] and str(top.get("transaction_id")) == c["top_transaction"]
              and _num(top.get("risk_score")) == c["top_score"])
        r.add("RS-F02", f"Card {c['card_key']} {c['start']}..{c['end']} scores match pinned result",
              PASS if ok else (ERROR if res["is_error"] else FAIL), res["latency_ms"],
              expected=c, observed={k: sm.get(k) for k in ("rows_scored", "complete", "score_check", "band_counts",
                                                           "indicator_counts")} | {"top": top},
              error=short(res["data"], 300) if res["is_error"] else None)

        # RS-F03 population ranking (pinned) + RS-P01 latency
        k = RANKING_CASE
        res = await call_tool(s, "run_risk_sql", {"sql": ranking_sql(T, p, k["start"], k["end"]), "limit": 5})
        rk = (res["data"] or {}).get("summary", {}) if not res["is_error"] else {}
        rows = rk.get("ranking") or []
        ids = [str(x.get("user_id")) for x in rows]
        ok = (rk.get("output") == "ranking" and rk.get("aggregate_check", {}).get("inconsistent") == 0
              and rk.get("missing_ranking_columns") == [] and ids == k["top_users"]
              and [int(x["high_count"]) for x in rows] == k["high"]
              and [int(x["medium_count"]) for x in rows] == k["medium"])
        r.add("RS-F03", f"User ranking {k['start']}..{k['end']} matches pinned top five",
              PASS if ok else (ERROR if res["is_error"] else FAIL), res["latency_ms"],
              expected=k, observed=rows, error=short(res["data"], 300) if res["is_error"] else None)
        limit_ms = config.risk_ranking_threshold_ms
        r.add("RS-P01", f"Population ranking SQL within {limit_ms / 1000:.0f}s",
              PASS if res["latency_ms"] <= limit_ms and not res["is_error"] else WARN, res["latency_ms"],
              threshold_ms=limit_ms)
        r.metrics["ranking_latency_ms"] = res["latency_ms"]

        # RS-R03 ranking reconciliation: re-score the top user transaction by transaction
        if rows:
            u, top_row = ids[0], rows[0]
            res = await call_tool(s, "run_risk_sql", {"sql": user_sql(T, p, u, k["start"], k["end"]), "limit": 200})
            us = (res["data"] or {}).get("summary", {}) if not res["is_error"] else {}
            bands = us.get("band_counts") or {}
            ok = (us.get("complete") is True and us.get("rows_scored") == int(top_row["transactions_scored"])
                  and bands.get("high", 0) == int(top_row["high_count"])
                  and bands.get("medium", 0) == int(top_row["medium_count"]))
            r.add("RS-R03", f"Top-ranked user {u} re-scored per transaction matches the ranking row",
                  PASS if ok else FAIL, res["latency_ms"], ranking_row=top_row,
                  transaction_level={"rows_scored": us.get("rows_scored"), "band_counts": bands,
                                     "complete": us.get("complete")})
        else:
            r.add("RS-R03", "Top-ranked user re-scored per transaction", SKIPPED, reason="no ranking rows")

        # RS-S* guardrails: rejected, no stack trace, no SQL executed
        for cid, name, sql, limit in _sec_cases(T, L, RAW):
            before = len(counting.statements) if counting else None
            res = await call_tool(s, "run_risk_sql", {"sql": sql, "limit": limit})
            ran = (len(counting.statements) - before) if counting else None
            text = str(res["data"])
            ok = res["is_error"] and "Traceback" not in text and ran in (0, None)
            r.add(cid, f"run_risk_sql rejects: {name}", PASS if ok else FAIL, res["latency_ms"],
                  rejected=res["is_error"], sql_statements_executed=ran, message=short(text, 200))
        res = await call_tool(s, "run_risk_sql", {"sql": f"SELECT transaction_id, amount FROM {T} WHERE card_key = "
                                                         f"'{CARD_CASE['card_key']}' LIMIT 5", "limit": 5})
        ok = not res["is_error"] and 0 < (res["data"] or {}).get("returned", 0) <= 5
        r.add("RS-S10", "Positive control: scoped read-only SELECT is accepted and row-capped",
              PASS if ok else FAIL, res["latency_ms"], message=short(res["data"], 200))

    # RS-R01 / RS-R02 independent per-transaction checks on the RS-F02 rows
    recs = (card or {}).get("records") or []
    if recs:
        _reconcile_indicators(r, backend, T, p, recs)
    else:
        for cid in ("RS-R01", "RS-R02"):
            r.add(cid, "Independent indicator check", SKIPPED, reason="RS-F02 returned no records")

    await _stability(r, config)
    _efficacy(r, config, backend, evaluation_metrics, risk_scoring)
    return r.finish()


def _reconcile_indicators(r, backend, T, p, recs):
    key = CARD_CASE["card_key"]
    hist = [x for x in recs if (_num(x.get("prior_n")) or 0) > 0]
    # Merchant novelty: every flagged row plus up to 3 unflagged rows, checked by direct count.
    sample = [x for x in hist if int(x["merchant_novelty"]) == 1] + \
             [x for x in hist if int(x["merchant_novelty"]) == 0][:3]
    t0, bad = time.perf_counter(), []
    for x in sample:
        n = backend.query(f"SELECT COUNT(*) AS n FROM {T} WHERE card_key = ? AND merchant_id = ? AND "
                          "(transaction_time < ? OR (transaction_time = ? AND transaction_id < ?))",
                          [key, str(x["merchant_id"]), str(x["transaction_time"]), str(x["transaction_time"]),
                           str(x["transaction_id"])])[0]["n"]
        if (int(n) == 0) != (int(x["merchant_novelty"]) == 1):
            bad.append({"transaction_id": x["transaction_id"], "flag": x["merchant_novelty"], "earlier_visits": n})
    r.add("RS-R01", "Merchant novelty flags match a direct count of earlier visits",
          PASS if sample and not bad else (SKIPPED if not sample else FAIL), (time.perf_counter() - t0) * 1000,
          checked=len(sample), mismatches=bad)

    # Amount anomaly: exact percentile of earlier amounts (the tool uses percentile_approx).
    elig = [x for x in recs if (_num(x.get("prior_n")) or 0) >= p["amount_min_history"]]
    sample = [x for x in elig if int(x["amount_anomaly"]) == 1] + \
             [x for x in elig if int(x["amount_anomaly"]) == 0][:3]
    t0, bad = time.perf_counter(), []
    for x in sample:
        q = backend.query(f"SELECT percentile(amount, {float(p['amount_percentile'])}) AS q FROM {T} WHERE card_key = ? "
                          "AND (transaction_time < ? OR (transaction_time = ? AND transaction_id < ?))",
                          [key, str(x["transaction_time"]), str(x["transaction_time"]),
                           str(x["transaction_id"])])[0]["q"]
        expected = int(float(x["amount"]) >= float(q))
        if expected != int(x["amount_anomaly"]):
            bad.append({"transaction_id": x["transaction_id"], "amount": x["amount"], "exact_percentile": q,
                        "approx_percentile": x.get("prior_p"), "flag": x["amount_anomaly"]})
    # Approximate vs exact percentile can differ right at the boundary: mismatch is a warning.
    r.add("RS-R02", "Amount anomaly flags agree with the exact earlier-amount percentile",
          PASS if sample and not bad else (SKIPPED if not sample else WARN), (time.perf_counter() - t0) * 1000,
          checked=len(sample), mismatches=bad)


async def _stability(r, config):
    runs = int(config.risk_stability_runs)
    if not config.enable_llm or runs <= 0:
        for cid in ("RS-ST01", "RS-P02"):
            r.add(cid, "Agent stability / end-to-end latency", SKIPPED, reason="enable_llm false or risk_stability_runs 0")
        return
    from .judge_suite import _client_module
    try:
        llm_client = _client_module(config)
    except Exception as exc:
        for cid in ("RS-ST01", "RS-P02"):
            r.add(cid, "Agent stability / end-to-end latency", SKIPPED, reason=short(repr(exc), 200))
        return
    tops, times, revisions = [], [], []
    for i in range(1, runs + 1):
        t0 = time.perf_counter()
        try:
            async with open_session(config) as session:
                chat = await llm_client.ask_pav_async(RANKING_QUESTION, model=config.llm_endpoint, session=session)
        except Exception as exc:
            r.add(f"RS-ST01-{i}", f"Agent run {i}: ranking question", ERROR, (time.perf_counter() - t0) * 1000,
                  error=short(f"{type(exc).__name__}: {exc}", 400))
            continue
        ms = (time.perf_counter() - t0) * 1000
        calls = [c for c in chat.tool_calls if c.name == "run_risk_sql" and not c.is_error]
        rank_calls = [c for c in calls if '"ranking"' in c.result]
        top = ranking_from_text(rank_calls[-1].result)[:5] if rank_calls else []
        agent_sql = (rank_calls[-1].arguments or {}).get("sql") if rank_calls else None
        referred = not calls and "genie" in (chat.answer or "").lower()
        ok = chat.verdict == "PASS" and top == RANKING_CASE["top_users"] and not referred
        tops.append(top)
        times.append(ms)
        revisions.append(chat.revisions)
        r.add(f"RS-ST01-{i}", f"Agent run {i}: answers the ranking question with the reference top five",
              PASS if ok else FAIL, ms, verdict=chat.verdict, revisions=chat.revisions, top_users=top,
              expected=RANKING_CASE["top_users"], referred_elsewhere=referred,
              tool_calls=[c.name for c in chat.tool_calls], agent_ranking_sql=agent_sql,
              verifier_feedback=verifier_feedback(getattr(chat, "verification", "")),
              answer=chat.answer, verification=getattr(chat, "verification", ""),
              plan=getattr(chat, "plan", ""))
    same = bool(tops) and all(t == tops[0] for t in tops) and len(tops) == runs
    r.add("RS-ST01", f"Same top five across {runs} agent runs", PASS if same else FAIL, runs=runs, top_users=tops)
    limit = config.risk_pav_threshold_ms
    ok = bool(times) and max(times) <= limit and max(revisions) <= 1
    r.add("RS-P02", f"Agent answers within {limit / 1000:.0f}s with at most 1 revision", PASS if ok else WARN,
          max(times) if times else None, threshold_ms=limit, times_ms=[round(t) for t in times], revisions=revisions)


def _efficacy(r, config, backend, evaluation_metrics, risk_scoring):
    windows = parse_windows(config.risk_efficacy_windows)
    if not windows:
        r.add("RS-E01", "Efficacy against fraud labels", SKIPPED, reason="risk_efficacy_windows is empty")
        return
    results = []
    for i, (s, e) in enumerate(windows, 1):
        t0 = time.perf_counter()
        try:
            res = evaluation_metrics.evaluate_risk_indicators(backend, config.label_table, s, e,
                                                              int(config.risk_efficacy_max_cards))
        except Exception as exc:
            r.add(f"RS-E01-{i}", f"Efficacy window {s}..{e}", ERROR, (time.perf_counter() - t0) * 1000,
                  error=short(f"{type(exc).__name__}: {exc}", 400))
            continue
        results.append(res)
        r.add(f"RS-E01-{i}", f"Efficacy window {s}..{e} scored against labels", PASS,
              (time.perf_counter() - t0) * 1000, transactions=res["transactions"], fraud=res["fraud"],
              by_score=res["by_score"])
    if not results:
        return
    pooled = combine_efficacy(results)
    r.metrics["efficacy"] = pooled
    ok, bad = monotonic(pooled["by_score"], int(config.risk_efficacy_min_n))
    r.add("RS-E02", "Fraud rate never falls as the risk score rises", PASS if ok else WARN,
          min_transactions_per_level=config.risk_efficacy_min_n, violations=bad, by_score=pooled["by_score"],
          base_fraud_rate=pooled["base_fraud_rate"])
    p = risk_scoring.parameters()
    lift = band_lift(pooled, p["band_high"])
    bar = config.risk_min_high_lift
    r.add("RS-E03", f"High band fraud rate is at least {bar:g}x the base rate",
          PASS if lift is not None and lift >= bar else WARN, high_band_lift=lift, required=bar,
          medium_or_high_lift=band_lift(pooled, p["band_medium"]))
    cap = config.risk_max_indicator_flag_rate
    loose = {k: v["flag_rate"] for k, v in pooled["indicators"].items() if (v["flag_rate"] or 0) > cap}
    r.add("RS-E04", f"No indicator flags more than {cap:.0%} of transactions", PASS if not loose else WARN,
          over_cap=loose, indicators=pooled["indicators"])
    r.notes.append("RS-E* are advisory (warn, not fail): they decide whether to tune thresholds or move to the "
                   "z-score method, not whether the deployment is correct.")
