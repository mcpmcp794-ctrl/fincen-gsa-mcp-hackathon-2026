# Architecture and merge decisions

## Request flow

```
card-txn-llm-client (Streamlit, client/)  --OAuth-->  fraudulent-transaction-mcp (/mcp)
  LLM tool-calling loop (llm_client.py)                FastMCP -> tools/* -> analytics / nl_sql
                                                        -> backend.py -> SQL warehouse
                                                        -> workspace.default.card_transaction_mcp_clean
```

## Why Jenna's project is the base

* It is a real network MCP server (streamable HTTP at /mcp, OAuth via the Apps proxy) already
  consumed by `card-txn-llm-client`; Sudhir's server only ran as a stdio subprocess inside his
  Streamlit app.
* Deterministic analytics with typed, bounded inputs and an offline test suite.
* A full evaluation harness with evidence (functional, reconciliation, security, performance, Genie).
* Deliberate fraud-label isolation.

## What came from transformer-source-v6

| Item | Where |
| --- | --- |
| sqlglot AST validation (one SELECT, only the allowed table) | `sql_guard.py` (CTEs/subqueries over the allowed table remain allowed) |
| Amount filters on search | `tools/search_transactions.py` (`min_amount`, `max_amount`) |
| Table-schema tool | `tools/get_table_schema.py` (static catalogue, no DESCRIBE) |
| Hard row cap on results | `backend.py` (`MAX_RESULT_ROWS`) |

Not carried over: the stdio-per-call MCP client, the in-app SQL generation (it runs in the
server as `ask_transactions`), the open `execute_sql` tool, merchant-name search (the clean table
has no merchant names), and the Streamlit UI (Jenna's client is the single interface). The token
refresh issue in v6 `app.py` does not apply: the client and server obtain tokens per request.

## Compatibility rules

* Do not rename or remove the analysis tools or their parameters; only add optional ones.
* Keep `/mcp`, `/health` (`status: ok`) and the root `app.yaml`.

## Genie spaces

Genie spaces are workspace objects and are not copied as files. The evaluation config references
the existing spaces: behavioral `01f1da1b073e175183f19e882132d64d`, fraud `01f1dc44f22413fe82320128338353b5`.
