"""Reusable analyst prompts (single-entity starters, structured as Plan -> Act -> Verify).

For open-ended questions use the PAV workflow prompts in ``pav_workflow.py``.
"""

from __future__ import annotations


def register(mcp) -> None:
    @mcp.prompt()
    def review_transaction(transaction_id: str) -> str:
        """Walk through a review of one transaction using the MCP tools."""
        return (
            f"Review transaction {transaction_id} using Plan -> Act -> Verify.\n"
            "PLAN: list what you need to establish (the transaction, the cardholder's usual "
            "behaviour, review cues).\n"
            "ACT: call generate_transaction_report, then summarize the transaction, the "
            "cardholder's usual behaviour and any review cues.\n"
            "VERIFY: confirm the key figures with search_transactions or "
            "get_cardholder_profile before answering. Review cues are not fraud "
            "determinations; state that a human must decide.")

    @mcp.prompt()
    def profile_cardholder(user_id: str) -> str:
        """Profile a cardholder's spending and merchant overlap."""
        return (
            f"Profile cardholder {user_id} using Plan -> Act -> Verify.\n"
            "PLAN: list what you need to establish (spending, top merchants, overlapping users).\n"
            "ACT: call get_cardholder_profile, then merchant_relationships. Describe spending, "
            "top merchants and users with overlapping merchants.\n"
            "VERIFY: confirm the headline totals with search_transactions or "
            "summarize_transactions before answering. Shared merchants do not imply shared "
            "funds or fraud.")
