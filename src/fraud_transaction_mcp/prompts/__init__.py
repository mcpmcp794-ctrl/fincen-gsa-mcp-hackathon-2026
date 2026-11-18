"""Prompts submodule: register_prompts(mcp) aggregator.

* pav_workflow  -> Plan -> Act -> Verify stage prompts (pav_plan, pav_act, pav_verify)
                   and the investigate_transactions starter
* investigation -> single-entity starters (review_transaction, profile_cardholder)
"""

from __future__ import annotations

from . import investigation, pav_workflow
from .pav_workflow import PAV_PROMPTS

# Registered prompt names (contract with the chat client and /health).
PROMPT_NAMES = [PAV_PROMPTS["plan"], PAV_PROMPTS["act"], PAV_PROMPTS["verify"],
                "investigate_transactions", "review_transaction", "profile_cardholder"]


def register_prompts(mcp) -> None:
    pav_workflow.register(mcp)
    investigation.register(mcp)


__all__ = ["register_prompts", "PAV_PROMPTS", "PROMPT_NAMES"]
