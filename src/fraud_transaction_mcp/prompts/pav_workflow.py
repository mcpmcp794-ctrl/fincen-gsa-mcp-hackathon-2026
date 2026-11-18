"""MCP prompts for the Plan -> Act -> Verify (PAV) investigation workflow.

The agent instructions live next to this module as Markdown templates
(``planner-agent-instructions.md``, ``actor-agent-instructions.md``,
``verifier-agent-instructions.md``) with ``{{placeholder}}`` variables.

Registered prompts (prompt names are a contract with the chat client):

* ``pav_plan``    -> Planner Agent instructions (no tools; produces the plan)
* ``pav_act``     -> Act Agent instructions (executes the plan with MCP tools)
* ``pav_verify``  -> Verify Agent instructions (re-checks findings, writes the answer,
                     ends with ``VERDICT: PASS`` or ``VERDICT: REVISE``)
* ``investigate_transactions`` -> one-message PAV starter for generic MCP clients

The chat client (``client/llm_client.py::ask_pav_async``) fetches the three stage
prompts with ``get_prompt`` and controls the flow: plan -> act -> verify, with at
most one act/verify revision when the verifier returns ``VERDICT: REVISE``.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from ..config import get_settings

PROMPT_DIR = Path(__file__).resolve().parent
TEMPLATES = {
    "plan": "planner-agent-instructions.md",
    "act": "actor-agent-instructions.md",
    "verify": "verifier-agent-instructions.md",
}
PAV_PROMPTS = {"plan": "pav_plan", "act": "pav_act", "verify": "pav_verify"}
DEFAULT_MAX_STEPS = {"plan": 6, "act": 8, "verify": 4}
# [CLEANUP 2026-11-17] Verdict parsing is done by the chat client
# (client/llm_client.py parse_verdict/strip_verdict); the server copies below were
# only used by tests.
# VERDICT_PASS, VERDICT_REVISE = "PASS", "REVISE"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
# _VERDICT = re.compile(r"VERDICT:\s*(PASS|REVISE)", re.I)
# _FEEDBACK = re.compile(r"^\s*FEEDBACK:\s*(.+)$", re.I | re.M)


@lru_cache(maxsize=None)
def load_template(stage: str) -> str:
    """Raw Markdown template for a PAV stage ('plan', 'act' or 'verify')."""
    if stage not in TEMPLATES:
        raise ValueError(f"Unknown PAV stage: {stage!r}")
    return (PROMPT_DIR / TEMPLATES[stage]).read_text(encoding="utf-8")


def template_variables(stage: str) -> set[str]:
    return set(_PLACEHOLDER.findall(load_template(stage)))


def fraud_referral(settings=None) -> str:
    """Where fraud-label questions go: the Fraud Detection Genie space (title + link or ID)."""
    s = settings or get_settings()
    host = (os.getenv("DATABRICKS_HOST") or "").strip().rstrip("/")
    if host and not host.startswith("http"):
        host = f"https://{host}"
    where = f"{host}/genie/rooms/{s.fraud_genie_space_id}" if host else f"Genie space ID {s.fraud_genie_space_id}"
    return f'the Fraud Detection Genie space "{s.fraud_genie_space_title}" ({where})'


def render(stage: str, **values) -> str:
    """Fill a stage template. Every placeholder must be supplied (empty string allowed);
    ``fraud_referral`` defaults to :func:`fraud_referral`."""
    values.setdefault("max_steps", DEFAULT_MAX_STEPS[stage])
    values.setdefault("fraud_referral", fraud_referral())
    missing = template_variables(stage) - set(values)
    if missing:
        raise ValueError(f"Missing values for {stage} prompt: {sorted(missing)}")
    # Substitute in one pass so user text containing '{{...}}' is never re-expanded.
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]).strip(), load_template(stage))


# [CLEANUP 2026-11-17] Unused at runtime (client has its own copies).
# def parse_verdict(text: str) -> tuple[str, str]:
#     """Return (verdict, feedback) from Verify Agent output. A missing verdict counts as PASS
#     so a formatting slip never loops; the last VERDICT line wins."""
#     verdicts = _VERDICT.findall(text or "")
#     verdict = verdicts[-1].upper() if verdicts else VERDICT_PASS
#     feedback = _FEEDBACK.findall(text or "")
#     return verdict, (feedback[-1].strip() if feedback else "")
#
#
# def strip_verdict(text: str) -> str:
#     """Verify Agent output without the trailing FEEDBACK / VERDICT lines (for display)."""
#     lines = [ln for ln in (text or "").splitlines()
#              if not _VERDICT.search(ln) and not _FEEDBACK.match(ln)]
#     return "\n".join(lines).strip()


def _steps(value, stage: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_STEPS[stage]
    return max(1, min(n, 15))


def register(mcp) -> None:
    """Register the PAV stage prompts and the one-shot starter."""

    @mcp.prompt(name=PAV_PROMPTS["plan"])
    def pav_plan(research_question: str, max_steps: str = "6") -> str:
        """PAV stage 1: Planner Agent instructions for a research question (no tool calls)."""
        return render("plan", research_question=research_question,
                      max_steps=_steps(max_steps, "plan"))

    @mcp.prompt(name=PAV_PROMPTS["act"])
    def pav_act(research_question: str, plan: str, feedback: str = "",
                max_steps: str = "8") -> str:
        """PAV stage 2: Act Agent instructions to execute a plan with the MCP tools."""
        return render("act", research_question=research_question, plan=plan,
                      feedback=feedback or "None.", max_steps=_steps(max_steps, "act"))

    @mcp.prompt(name=PAV_PROMPTS["verify"])
    def pav_verify(research_question: str, plan: str, findings: str,
                   max_steps: str = "4") -> str:
        """PAV stage 3: Verify Agent instructions to re-check findings and write the answer."""
        return render("verify", research_question=research_question, plan=plan,
                      findings=findings, max_steps=_steps(max_steps, "verify"))

    @mcp.prompt()
    def investigate_transactions(research_question: str) -> str:
        """Run a full Plan -> Act -> Verify investigation in one conversation."""
        return (
            f"Investigate the following question using the Plan -> Act -> Verify workflow:\n\n"
            f"{research_question}\n\n"
            "**PLAN:** Before calling any tool, write hypotheses and a short numbered plan that "
            "maps each step to one tool (search_transactions, summarize_transactions, "
            "get_cardholder_profile, merchant_relationships, generate_transaction_report, "
            "get_table_schema; ask_transactions only when no specific tool fits). For any fraud "
            "assessment, likelihood or ranking question, call get_risk_scoring_guidance, write the "
            "scoring SQL yourself and run it with run_risk_sql; do not refer it elsewhere.\n\n"
            "**ACT:** Execute the plan. Report each finding with its exact figures and the tool "
            "call it came from. Never invent numbers.\n\n"
            "**VERIFY:** Re-check each key finding with a different tool or aggregation, mark it "
            "Confirmed / Corrected / Unverified, then answer using only verified figures. Fraud "
            "labels are not available; review cues and risk scores are not fraud determinations and a human "
            f"must decide. Refer only questions about recorded fraud labels (labelled counts, rates or "
            f"label values) to {fraud_referral()} instead of estimating them.")
