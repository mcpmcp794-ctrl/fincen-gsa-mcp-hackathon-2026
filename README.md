# Fraudulent Transaction MCP

MCP server for read-only analysis of synthetic TabFormer credit-card transactions on
Databricks, merged from two FinCEN projects and laid out per the
[GSA-TTS MCP hackathon template](https://github.com/GSA-TTS/mcp-hackathon-template).

* Base: Jenna Zhou, `Financial_Transaction_Analysis_MCP` (analytics, HTTP server, client, evaluation).
* Merged in from Sudhir Atul, `transformer-source-v6`: sqlglot SQL validation, amount filters on
  search, a table-schema tool, a hard row cap on query results.

Deployed app: `fraudulent-transaction-mcp` -> `https://fraudulent-transaction-mcp-7474654188777746.aws.databricksapps.com/mcp` (streamable HTTP, Databricks OAuth).

## Repo structure

```
fraudulent-transaction-mcp/
├── app.yaml                     # Databricks Apps config (copy of deploy/databricks/app.yaml)
├── main.py                      # Local entry point
├── pyproject.toml / requirements.txt
├── server.json                  # MCP registry metadata
├── .env.example
├── src/fraud_transaction_mcp/
│   ├── app.py                   # Thin entry: builds FastMCP, picks transport
│   ├── config.py                # pydantic-settings (env vars / .env)
│   ├── models.py                # Column contract of the label-free table
│   ├── routes.py                # /, /health, /version
│   ├── analytics.py             # Deterministic, label-free analysis (Jenna)
│   ├── nl_sql.py                # NL -> SQL for ask_transactions
│   ├── sql_guard.py             # sqlglot + text validation of generated SQL
│   ├── backend.py               # Databricks SQL warehouse adapter
│   ├── evaluation_metrics.py    # EVALUATION ONLY: label-aware aggregate metrics
│   ├── tools/                   # One file per tool
│   ├── prompts/                 # PAV workflow (pav_workflow.py + planner/actor/verifier .md), review_transaction, profile_cardholder
│   └── resources/               # card-transactions://schema, ://data-notes
├── client/                      # Streamlit LLM client (card-txn-llm-client) + .streamlit/config.toml
├── tests/                       # Offline unit/contract tests (SQLite backend)
├── evaluation/                  # Evaluation harness, evidence and reports (Jenna)
├── databricks/                  # Data-prep notebooks and Genie SQL views
├── data/source_config.json
├── docs/README.md               # Architecture, merge decisions, label policy
└── deploy/databricks/           # app.yaml, deploy README, .env.example
```

## Tools

| Tool | Purpose |
| --- | --- |
| `search_transactions` | Filter by user_id, card_key, merchant_id, start/end, **min_amount/max_amount** (new) |
| `summarize_transactions` | Aggregate by merchant, mcc, chip, state, user, card or month |
| `get_cardholder_profile` | Stats, top merchants, recent purchases for one user |
| `merchant_relationships` | Users sharing merchants (not fund flows) |
| `generate_transaction_report` | Review report with deterministic cues (not fraud determinations) |
| `ask_transactions` | NL question -> validated read-only SELECT -> rows |
| `get_table_schema` | Column catalogue of the analysis table (new) |
| `evaluate_review_signals` | **Evaluation only**, off by default (`ENABLE_EVALUATION_TOOLS=true`) |

The six original tool names and parameters are unchanged, so existing clients keep working.

## Plan -> Act -> Verify (PAV) workflow

The server registers the stage prompts; the chat client controls the flow.

| Prompt | Template | Role |
| --- | --- | --- |
| `pav_plan(research_question, max_steps)` | `prompts/planner-agent-instructions.md` | Hypotheses + tool-mapped plan (no tool calls) |
| `pav_act(research_question, plan, feedback, max_steps)` | `prompts/actor-agent-instructions.md` | Executes the plan with the MCP tools, reports traceable findings |
| `pav_verify(research_question, plan, findings, max_steps)` | `prompts/verifier-agent-instructions.md` | Re-checks findings with a different tool, writes the answer, ends with `VERDICT: PASS` or `VERDICT: REVISE` |
| `investigate_transactions(research_question)` | inline | One-message PAV starter for generic MCP clients |

`client/llm_client.py::ask_pav_async` fetches these prompts with `get_prompt` and runs
PLAN -> ACT -> VERIFY; on `REVISE` the verifier's `FEEDBACK:` goes back to ACT (up to
`PAV_MAX_REVISIONS`, default 1). If the server does not expose the PAV prompts the client
falls back to the single tool loop. Set `PAV_WORKFLOW=false` (or use the sidebar toggle)
to use the single loop. Adapted from `/Shared/paysim-fraud-analyzer/src/paysim-mcp/prompts`
with PaySim-only tools and fraud-label references removed.

## Fraud-label policy

Analysis never sees fraud labels: tools read `workspace.default.card_transaction_clean`
(no `Is_Fraud`), and the SQL guard rejects any query naming the label table or column.
Labels live in `workspace.default.card_transaction_evaluation_labels` and are used only by
the `evaluation/` harness and the opt-in `evaluate_review_signals` tool, which returns
aggregate confusion-matrix metrics, never per-transaction labels.

## Run and test locally

```bash
pip install -r requirements.txt pytest uvicorn httpx
python -m pytest -q                     # tests/ + client/tests/
cp .env.example .env && python main.py  # stdio
```

## Deploy

See [deploy/databricks/README.md](deploy/databricks/README.md).
