# Fraudulent Transaction MCP

MCP server for read-only analysis of synthetic TabFormer credit-card transactions on
Databricks, merged from two FinCEN projects and laid out per the
[GSA-TTS MCP hackathon template](https://github.com/GSA-TTS/mcp-hackathon-template).

* Base: Jenna Zhou, `Financial_Transaction_Analysis_MCP` (analytics, HTTP server, client, evaluation).
* Merged in from Sudhir Atul, `transformer-source-v6`: sqlglot SQL validation, amount filters on
  search, a table-schema tool, a hard row cap on query results.
* Added since the merge: Plan -> Act -> Verify (PAV) prompts, agent-driven rule-based risk
  scoring, concurrent tool execution with a pooled SQL backend, and an LLM-as-judge and
  risk-scoring evaluation suite.

Deployed apps (Databricks Apps, OAuth-protected):

| App | Source | URL |
| --- | --- | --- |
| `fraudulent-transaction-mcp` | repo root | `https://fraudulent-transaction-mcp-7474654188777746.aws.databricksapps.com/mcp` (streamable HTTP, stateless) plus `/health`, `/version` |
| `fraudulent-transaction-chat` | `client/` | `https://fraudulent-transaction-chat-7474654188777746.aws.databricksapps.com` (Streamlit chat client) |

Data (all in `workspace.default`, rebuilt from `card_transaction` by the `*_mcp` notebooks):

* `card_transaction_mcp_clean`: label-free analysis table (16,911,573 rows) read by every tool.
* `card_transaction_mcp_evaluation_labels`: fraud labels, evaluation only.
* `card_transaction_mcp_with_id`, `card_transaction_mcp_rejected_rows`: data-prep outputs.
* Genie views `card_transaction_mcp_{cardholder_summary,merchant_summary,daily_card_spend,customer_merchant_edges}` (`databricks/sql/02_genie_views.sql`).

## Repo structure

```
fraudulent-transaction-mcp/
├── app.yaml                     # Databricks Apps config (copy of deploy/databricks/app.yaml)
├── main.py                      # Local entry point
├── pyproject.toml / requirements.txt
├── server.json                  # MCP registry metadata
├── .env.example
├── .github/workflows/ci.yml     # pytest on push / pull request
├── Card Transaction Insights and Behavioral Analysis Agent (MCP tables)  # Genie space used by the evaluation
├── src/fraud_transaction_mcp/
│   ├── app.py                   # Thin entry: builds FastMCP, picks transport
│   ├── config.py                # pydantic-settings (env vars / .env)
│   ├── models.py                # Column contract of the label-free table
│   ├── routes.py                # /, /health, /version
│   ├── analytics.py             # Deterministic, label-free analysis (Jenna)
│   ├── nl_sql.py                # NL -> SQL for ask_transactions
│   ├── sql_guard.py             # sqlglot + text validation of generated SQL
│   ├── backend.py               # Databricks SQL warehouse adapter with a persistent connection pool
│   ├── risk_indicators.md       # Risk-scoring method: definitions + thresholds (single source)
│   ├── risk_scoring.py          # Parses risk_indicators.md, validates and checks risk SQL
│   ├── evaluation_metrics.py    # EVALUATION ONLY: label-aware aggregate metrics
│   ├── tools/                   # One file per tool
│   ├── prompts/                 # PAV workflow (pav_workflow.py + planner/actor/verifier .md), investigation.py
│   └── resources/               # card-transactions://schema, ://data-notes
├── client/                      # Streamlit chat client (fraudulent-transaction-chat), llm_client.py, tests/
├── tests/                       # Offline unit/contract tests (SQLite backend)
├── evaluation/                  # Evaluation harness (scripts 00-11, run_evaluation notebook), evidence, reports
├── databricks/                  # Data-prep notebooks (00/01 *_mcp) and Genie SQL views
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
| `get_risk_scoring_guidance` | Returns the rule-based risk method (`risk_indicators.md`) and its parsed thresholds |
| `run_risk_sql` | Runs agent-written risk-scoring SQL (one read-only SELECT, CTEs allowed, scoped WHERE, max 200 rows); returns a compact `summary`, rows and `score_check` |
| `evaluate_review_signals` | **Evaluation only**, off by default (`ENABLE_EVALUATION_TOOLS=true`) |
| `evaluate_risk_indicators` | **Evaluation only**, off by default: efficacy of the risk indicators against labels (aggregate) |

The six original tool names and parameters are unchanged, so existing clients keep working.
Sync tools run in worker threads, so concurrent tool calls overlap; the backend keeps a
pool of warehouse connections (`SQL_POOL_SIZE`, default 4).

Prompts: `pav_plan`, `pav_act`, `pav_verify`, `investigate_transactions`,
`review_transaction`, `profile_cardholder`.

## Risk scoring

Fraud-assessment questions (score, rank or assess fraud risk of transactions, cards or
cardholders) are answered by the app itself. The Planner calls `get_risk_scoring_guidance`,
the Actor writes SQL and runs it with `run_risk_sql`, and the Verifier re-checks a flagged
indicator with a different tool. Four point-in-time 0/1 indicators are computed against
each card's own earlier history:

* `amount_anomaly`: amount at or above the card's earlier 95th percentile (min 10 earlier transactions)
* `velocity_anomaly`: at least 5 transactions in 24 hours and more than 3x the card's earlier daily rate
* `location_channel_change`: first use of this channel (`use_chip`) or location
* `merchant_novelty`: first use of this `merchant_id`

`risk_score = sum / 4`; bands: high >= 0.75, medium >= 0.5, otherwise low. Scoring windows
are at most 31 days; rankings return per-user or per-card counts of high/medium
transactions. Scores are investigative aids for human review, not fraud determinations.
Thresholds are edited only in the `parameters` block of `risk_indicators.md`.

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
to use the single loop. The deployed client uses `PAV_MAX_REVISIONS=1` and
`PAV_VERIFY_ROUNDS=1`; same-turn tool calls run concurrently (`MAX_PARALLEL_TOOL_CALLS`,
default 4) and LLM calls time out after `LLM_TIMEOUT_SECONDS` (default 120).
Adapted from `/Shared/paysim-fraud-analyzer/src/paysim-mcp/prompts` with PaySim-only tools
and fraud-label references removed.

## Fraud-label policy

Analysis never sees fraud labels: tools read `workspace.default.card_transaction_mcp_clean`
(no `Is_Fraud`), and the SQL guard rejects any query naming the label table or column.
Labels live in `workspace.default.card_transaction_mcp_evaluation_labels` and are used only by
the `evaluation/` harness and the opt-in `evaluate_review_signals` and
`evaluate_risk_indicators` tools, which return aggregate metrics, never per-transaction labels.

Only questions asking for *recorded* labels (labelled fraud counts, rates, or the label of a
transaction) are referred, with no tool calls, to the Genie space
"Card Transaction Insights and Fraud Detection Agent" (`FRAUD_GENIE_SPACE_ID`). Fraud
risk assessment and ranking questions are answered with the risk scores above.

## Evaluation

`evaluation/run_evaluation` (notebook) or `evaluation/scripts/run_all.py` runs preflight,
unit, functional, SQL reconciliation, security-negative, performance, Genie benchmark,
LLM-as-judge (`10_llm_judge.py`) and risk-scoring (`11_risk_scoring.py`) suites, then writes
evidence to `evaluation/evidence/` and reports to `evaluation/reports/`. Skip suites with
`--skip-unit`, `--skip-custom`, `--skip-genie`, `--skip-performance`, `--skip-judge`,
`--skip-risk`. Settings are in `evaluation/eval_config.yaml`.

## Run and test locally

```bash
pip install -r requirements.txt pytest uvicorn httpx
python -m pytest -q                     # tests/ + client/tests/ (160 pass, 1 skipped)
cp .env.example .env && python main.py  # stdio
```

## Deploy

See [deploy/databricks/README.md](deploy/databricks/README.md) for the MCP app's resources,
grants and smoke test. The checklist below takes a fresh clone to two running apps.

### From clone to deployed

**0. Prerequisites**

* A Unity Catalog schema where you can create tables (the repo uses `workspace.default`).
* A source table in TabFormer format with exactly these 15 columns: `User, Card, Year, Month,
  Day, Time, Amount, Use Chip, Merchant Name, Merchant City, Merchant State, Zip, MCC,
  Errors, Is_Fraud` (the repo uses `workspace.default.card_transaction`).
* A SQL warehouse you can use, and EXECUTE on a chat model (UC model service such as
  `models.ai.gpt-5-6-luna`, or a serving endpoint).
* Permission to create Databricks Apps and to grant SELECT on the tables you build.

**1. Clone and test**

Clone into a Databricks Git folder (or a local checkout), then run the offline tests
(see *Run and test locally*). They use SQLite and need no workspace access.

**2. Replace workspace-specific values**

The repo is wired to this workspace. Change these values if your workspace, catalog/schema,
warehouse or models are different:

| Value | Where |
| --- | --- |
| Table names (`workspace.default.card_transaction*`) | `databricks/notebooks/00_*`, `01_*` (config cell), `databricks/sql/02_genie_views.sql`, `app.yaml`, `deploy/databricks/app.yaml`, `.env.example`, `config.py` defaults, `client/app.yaml` (`TABLE_NAME`), `evaluation/eval_config.yaml` |
| SQL warehouse ID (`50d2bd4ddf65e0f5`) | app resource `sql-warehouse` (step 4), `evaluation/eval_config.yaml` |
| Model (`models.ai.gpt-5-6-luna`) | `app.yaml` and `client/app.yaml` (`LLM_ENDPOINT`, `MODEL_OPTIONS`), `evaluation/eval_config.yaml` |
| MCP server URL | `client/app.yaml` (`MCP_SERVER_URL`), `client/llm_client.py` (`DEFAULT_MCP_URL`), `server.json`, `evaluation/eval_config.yaml` (`custom_mcp_url`); set after step 4 |
| Fraud Genie space ID | `config.py` default, or add `FRAUD_GENIE_SPACE_ID` (and `FRAUD_GENIE_SPACE_TITLE`) to `app.yaml`; `evaluation/eval_config.yaml` (`genie_spaces.fraud`) |
| Behavioral Genie space ID | `evaluation/eval_config.yaml` (`genie_spaces.behavioral`) |
| App service principal | `evaluation/eval_config.yaml` (`app_service_principal`); set after step 4 |

Keep `app.yaml` and `deploy/databricks/app.yaml` identical.

**3. Build the data (run once)**

1. Run `databricks/notebooks/00_initialize_transaction_ids_mcp`. It creates
   `card_transaction_mcp_with_id` with persistent `transaction_id`s and fails if the source
   schema is not the 15 columns above.
2. Run `databricks/notebooks/01_prepare_card_transaction_mcp`. It writes
   `card_transaction_mcp_clean` (no labels), `card_transaction_mcp_evaluation_labels` and
   `card_transaction_mcp_rejected_rows`. It will not overwrite existing tables unless you set
   `REBUILD_DERIVED_TABLES = True`.
3. Run `databricks/sql/02_genie_views.sql` on the warehouse to create the four
   `card_transaction_mcp_*` Genie views.
4. Optional: create the Genie spaces. The fraud-label referral space reads the labelled
   source table, and the behavioral space (saved in this repo as
   `Card Transaction Insights and Behavioral Analysis Agent (MCP tables)`) reads
   `card_transaction_mcp_clean` and the views. Record their IDs for step 2.

**4. Deploy the MCP server app**

1. Create the app `fraudulent-transaction-mcp` (`databricks apps create`), or use another name
   and update the URLs.
2. Add the resources: SQL warehouse with key `sql-warehouse` (CAN_USE) and model service with
   key `model` (EXECUTE).
3. Grant the app's service principal `USE CATALOG`, `USE SCHEMA` and `SELECT` on
   `card_transaction_mcp_clean`. Do not grant anything on the label table unless
   `ENABLE_EVALUATION_TOOLS=true`.
4. Deploy with `databricks apps deploy` using the repo root as the source code path
   (`app.yaml` must be at the root).
5. Check `https://<mcp-app-url>/health` in a browser. It should return `status: ok`.
   Copy `https://<mcp-app-url>/mcp` and the service principal ID into the places listed in
   step 2.

**5. Deploy the chat client app**

1. Set `MCP_SERVER_URL` in `client/app.yaml` to the URL from step 4.
2. Create the app `fraudulent-transaction-chat`. Add one model resource per entry in
   `MODEL_OPTIONS`: `model` and `model-2` are UC model services (EXECUTE), `model-3` is the
   serving endpoint `databricks-gpt-6-luna` (CAN_QUERY).
3. Give the chat app's service principal CAN_USE on the MCP app. Without this grant every
   tool call fails.
4. Deploy with `client/` as the source code path.
5. Give users CAN_USE on the chat app. Then ask a question such as "Top 5 users by fraud
   risk in January 2020" to check the tools and the PAV loop end to end.

**6. Optional: evaluation**

1. Point `evaluation/eval_config.yaml` at your tables, warehouse, Genie spaces and MCP URL.
2. `mcp_mode: inprocess` (default) needs only your own SELECT on the clean and label tables.
   `mcp_mode: http` needs `CUSTOM_MCP_OAUTH_TOKEN`, or an evaluation service principal whose
   `client_id` / `client_secret` are stored in the `eval_sp_secret_scope` secret scope and that
   has CAN_USE on the MCP app.
3. Run `evaluation/run_evaluation` (or `evaluation/scripts/run_all.py`).
