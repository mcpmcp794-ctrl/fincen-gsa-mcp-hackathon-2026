"""Static resources describing the analysis data."""

from __future__ import annotations

import json

from ..models import COLUMNS, DATA_NOTES


def register(mcp) -> None:
    @mcp.resource("card-transactions://schema")
    def transaction_schema() -> str:
        """Columns of the label-free card transaction table."""
        return json.dumps([{"name": n, "type": t, "description": d} for n, t, d in COLUMNS])

    @mcp.resource("card-transactions://data-notes")
    def data_notes() -> str:
        """Interpretation notes and limitations of the data."""
        return "\n".join(f"- {n}" for n in DATA_NOTES)
