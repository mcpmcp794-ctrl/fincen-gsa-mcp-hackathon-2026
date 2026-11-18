"""Shared data contract: columns of the label-free analysis table.

Used by the NL-to-SQL prompt, the get_table_schema tool and the schema resource.
The fraud label is deliberately absent; it lives in a separate restricted table.
"""

from __future__ import annotations

COLUMNS = [
    ("transaction_id", "string", "unique transaction id"),
    ("user_id", "string", "cardholder id"),
    ("card_id", "string", "card index within the user"),
    ("card_key", "string", "globally unique card key (user_id + card_id)"),
    ("transaction_time", "timestamp", "purchase timestamp, data covers 2010-01 to 2020-02"),
    ("amount", "decimal(18,2)", "purchase amount in USD; negative values are refunds/credits"),
    ("use_chip", "string", "channel: 'Swipe Transaction', 'Chip Transaction', 'Online Transaction'"),
    ("merchant_id", "string", "merchant id (no merchant names exist)"),
    ("merchant_city", "string", "merchant city; ONLINE for online purchases"),
    ("merchant_state", "string", "US state code or country name; null for online"),
    ("zip_code", "string", "merchant zip code"),
    ("mcc", "string", "merchant category code"),
    ("Errors", "string", "comma-separated processing errors, null if none"),
    ("source_file", "string", "lineage: source file"),
    ("source_row_number", "bigint", "lineage: source row number"),
]

DATA_NOTES = [
    "Synthetic TabFormer credit-card purchases; not bank-to-bank transfers.",
    "Amounts are signed: SUM(amount) is net activity, not gross spending.",
    "There are no merchant names; merchants are identified by merchant_id.",
    "The fraud ground-truth label is excluded from all analysis tools.",
]
