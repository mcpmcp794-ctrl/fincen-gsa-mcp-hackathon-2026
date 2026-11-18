"""Hybrid routing worksheet: Option A (custom MCP tools) vs Option B (Genie) per question.

Writes evaluation/evidence/hybrid_routing_worksheet.csv for reviewers. On later runs,
a worksheet with reviewer entries is summarised instead of being overwritten.
"""
from __future__ import annotations

import csv

from .mcp_session import sample_entities
from .utils import MANUAL, PASS, SuiteResult

DESCRIPTION = ("Manual comparison worksheet: for each question a reviewer records the Option A (custom "
               "MCP tool) and Option B (Genie space) answers, whether each is correct, and the preferred route.")

QUESTIONS = [
    ("HR-01", "What were the top 10 merchants by transaction count in {y}?", "A", "summarize_transactions"),
    ("HR-02", "Give me a spending profile for cardholder {u}.", "A", "get_cardholder_profile"),
    ("HR-03", "Which other cardholders shop at the same merchants as user {u}?", "A", "merchant_relationships"),
    ("HR-04", "Prepare a review report for transaction {tx}.", "A", "generate_transaction_report"),
    ("HR-05", "List the 20 most recent transactions for user {u} in {y}.", "A", "search_transactions"),
    ("HR-06", "How did monthly online spending change during {y}?", "A or B", "ask_transactions / Behavioral Genie"),
    ("HR-07", "Which states had the highest average transaction amount in {y}?", "A or B", "ask_transactions / Behavioral Genie"),
    ("HR-08", "How many fraudulent transactions were there in {y}?", "B", "Fraud Detection Genie (labels are excluded from MCP tools)"),
    ("HR-09", "Which merchant category codes have the highest fraud rate?", "B", "Fraud Detection Genie"),
    ("HR-10", "Is transaction {tx} fraud?", "A + human review", "Risk score for its card (get_risk_scoring_guidance + run_risk_sql); no automated determination"),
    ("HR-11", "Rank the top five users by likelihood of fraudulent card transactions.", "A", "get_risk_scoring_guidance + run_risk_sql (ranking)"),
    ("HR-12", "Assess the fraud risk of transactions on card {c} in June {y}.", "A", "get_risk_scoring_guidance + run_risk_sql"),
    ("HR-13", "Is transaction {tx} suspicious?", "A + human review", "Risk score for its card (get_risk_scoring_guidance + run_risk_sql)"),
]
COLUMNS = ["question_id", "question", "expected_route", "expected_tool_or_space",
           "option_a_custom_mcp_answer", "option_a_correct", "option_b_genie_answer", "option_b_correct",
           "preferred_option", "reviewer", "reviewer_notes"]


def create_hybrid_worksheet(config):
    r = SuiteResult("hybrid_routing", description=DESCRIPTION)
    path = config.evidence_dir / "hybrid_routing_worksheet.csv"
    if path.exists():
        with path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        if any((x.get("preferred_option") or "").strip() for x in rows):
            yes = lambda v: (v or "").strip().upper() in ("Y", "YES", "TRUE", "1")  # noqa: E731
            for x in rows:
                reviewed = bool((x.get("preferred_option") or "").strip())
                r.add(x["question_id"], x["question"], PASS if reviewed else MANUAL,
                      expected_route=x.get("expected_route"), preferred_option=x.get("preferred_option"),
                      option_a_correct=x.get("option_a_correct"), option_b_correct=x.get("option_b_correct"))
            r.metrics.update({"reviewed": sum(1 for c in r.cases if c.status == PASS), "questions": len(rows),
                              "option_a_correct": sum(yes(x.get("option_a_correct")) for x in rows),
                              "option_b_correct": sum(yes(x.get("option_b_correct")) for x in rows),
                              "worksheet": str(path)})
            return r.finish(), path
    try:
        s = sample_entities(config)
    except Exception:
        s = {"user_id": "<user_id>", "transaction_id": "<transaction_id>", "card_key": "<card_key>"}
    y, u, tx, c = config.benchmark_year, s["user_id"], s["transaction_id"], s.get("card_key")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for qid, q, route, target in QUESTIONS:
            question = q.format(y=y, u=u, tx=tx, c=c)
            writer.writerow({"question_id": qid, "question": question, "expected_route": route,
                             "expected_tool_or_space": target})
            r.add(qid, question, MANUAL, expected_route=route, expected_tool_or_space=target)
    r.metrics.update({"worksheet": str(path), "questions": len(QUESTIONS), "reviewed": 0})
    r.notes.append("Fill the option_* and preferred_option columns, then re-run 06/07 to summarise.")
    return r.finish(), path
