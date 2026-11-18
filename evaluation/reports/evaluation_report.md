# Financial Transaction Analysis MCP - Evaluation Report

Generated 2026-11-17T17:46:59+00:00 - overall result: **pass**

## 1. System under test

| Item | Value |
| --- | --- |
| MCP server | Card Transaction Credit Card Intelligence (FastMCP, streamable HTTP, Databricks App) |
| Deployed endpoint | https://fraudulent-transaction-mcp-7474654188777746.aws.databricksapps.com/mcp |
| Evaluation transport | in-memory MCP transport -> fraud_transaction_mcp.app.create_server(DatabricksBackend) -> SQL warehouse |
| Tools | ask_transactions, generate_transaction_report, get_cardholder_profile, get_table_schema, merchant_relationships, search_transactions, summarize_transactions |
| Data | workspace.default.card_transaction_mcp_clean (16,911,573 rows) |
| SQL warehouse | 50d2bd4ddf65e0f5 |
| NL-to-SQL model | models.ai.gpt-5-6-luna |
| Genie spaces | behavioral=01f1e2a9b56e182ea33536af5a1b8c98, fraud=01f1dc44f22413fe82320128338353b5 |
| Benchmark year | 2019 |
| Python / mcp | 3.12.3 / 1.30.0 |

## 2. Testing methodology

* **Preflight readiness** (8 cases): Readiness checks run before functional testing: configuration, SQL warehouse state, clean-table access, schema-level fraud-label isolation, MCP tool catalogue, the deployed app's authentication gate, Genie space availability and the NL-to-SQL model endpoint.
* **Offline unit and contract tests** (2 cases): pytest suites that need no workspace: tests/ (analytics logic, backend helpers, column contract, data-preparation and Genie-view static checks) and client/tests/ (MCP client against the real server over streamable HTTP with an in-memory SQLite backend, LLM stubbed).
* **Custom MCP functional tests** (16 cases): Each tool is invoked through an MCP client session against live data on the Databricks SQL warehouse. Checks: tool catalogue and schemas, returned fields, row bounds, filters, ordering, graceful not-found handling, and NL-to-SQL behaviour (answer, fraud refusal, restricted-table refusal).
* **SQL reconciliation** (10 cases): MCP tool outputs compared with independently written Databricks SQL on the same warehouse (exact match for counts/ids, 0.01 tolerance for amounts), plus raw-vs-clean row-count reconciliation of the data-preparation pipeline.
* **Security and negative tests** (41 cases): Unauthenticated and invalid-token requests to the deployed app; malformed and malicious tool inputs (must be rejected before any SQL runs, without stack traces); SQL-injection payloads; the NL-to-SQL validator against attack patterns; fraud-label leakage scans of every tool output; Unity Catalog grants of the app service principal.
* **Performance** (7 cases): Each workload runs once cold, then N warm iterations through one MCP session. Reported: min/p50/p95/mean/max latency, error rate and SQL statements per call. A workload passes with zero errors and warm p95 under the configured threshold.
* **Genie benchmark** (5 cases): Benchmark questions are sent to each Genie space (the same spaces exposed through the managed Genie MCP server). A numeric value in Genie's result rows must match reference SQL run on the raw table; answers without numbers are left for manual review.
* **Hybrid routing worksheet** (10 cases): Manual comparison worksheet: for each question a reviewer records the Option A (custom MCP tool) and Option B (Genie space) answers, whether each is correct, and the preferred route.
* **LLM-as-judge agent critique** (11 cases): End-to-end agent critique: the Plan -> Act -> Verify chat agent answers each hybrid-routing question through the MCP tools, then an independent LLM judge (MLflow GenAI make_judge) scores groundedness, policy compliance, tool use and answer quality, with a written rationale per judge.

Each case records its inputs, observed outputs, status (pass / fail / error / warn / skipped / manual_review) and duration in `evaluation/evidence/*.json`; the full list is in `evaluation_test_matrix.md`.

## 3. Results summary

| Suite | Status | Pass | Fail/Error | Warn | Skipped | Manual |
| --- | --- | --- | --- | --- | --- | --- |
| Preflight readiness | pass | 8 | 0 | 0 | 0 | 0 |
| Offline unit and contract tests | pass | 2 | 0 | 0 | 0 | 0 |
| Custom MCP functional tests | pass | 16 | 0 | 0 | 0 | 0 |
| SQL reconciliation | pass | 10 | 0 | 0 | 0 | 0 |
| Security and negative tests | pass | 41 | 0 | 0 | 0 | 0 |
| Performance | pass | 7 | 0 | 0 | 0 | 0 |
| Genie benchmark | pass | 5 | 0 | 0 | 0 | 0 |
| Hybrid routing worksheet | manual_review | 0 | 0 | 0 | 0 | 10 |
| LLM-as-judge agent critique | pass | 11 | 0 | 0 | 0 | 0 |

Unit tests: 130 passed, 0 failed, 0 errors, 1 skipped of 131.

## 4. Functional results

| ID | Case | Status | ms | Evidence |
| --- | --- | --- | --- | --- |
| FN-01 | Tool catalogue: expected tools, each with a description and a JSON input schema | pass |  | {"parameters_without_description": 22} |
| FN-02 | search_transactions: user 1455 in 2019, limit 10 | pass | 462.9 | {"returned": 10, "truncated": true} |
| FN-03 | summarize_transactions: group_by=merchant, 2019, top 5 | pass | 453.3 | {"returned": 5, "top_group": "1799189980464955940", "top_count": 76973} |
| FN-04 | summarize_transactions: group_by=mcc, 2019, top 5 | pass | 427.9 | {"returned": 5, "top_group": "5411", "top_count": 205684} |
| FN-05 | summarize_transactions: group_by=chip, 2019, top 5 | pass | 449.1 | {"returned": 3, "top_group": "Chip Transaction", "top_count": 1217608} |
| FN-06 | summarize_transactions: group_by=state, 2019, top 5 | pass | 546.0 | {"returned": 5, "top_count": 215489} |
| FN-07 | summarize_transactions: group_by=user, 2019, top 5 | pass | 453.6 | {"returned": 5, "top_group": "1249", "top_count": 4998} |
| FN-08 | summarize_transactions: group_by=card, 2019, top 5 | pass | 500.4 | {"returned": 5, "top_group": "332::0", "top_count": 4040} |
| FN-09 | summarize_transactions: group_by=month, 2019, top 5 | pass | 446.6 | {"returned": 5, "top_group": "2019-03", "top_count": 147202} |
| FN-10 | get_cardholder_profile: user 1455 | pass | 1348.5 | {"transaction_count": 7745, "merchants": 211} |
| FN-11 | merchant_relationships: user 1455, 5 merchants, 10 related users | pass | 917.8 | {"merchants": 5, "related_users": 10, "truncated": true} |
| FN-12 | generate_transaction_report: transaction 10000056 | pass | 1789.3 |  |
| FN-13 | generate_transaction_report: unknown id returns found=false (graceful) | pass | 526.5 | {"report_status": "not_found"} |
| FN-14 | ask_transactions: answers 'How many transactions were there in 2019?' | pass | 3278.0 |  |
| FN-15 | ask_transactions: declines fraud questions (no label in the MCP data) | pass | 1560.2 | {"explanation": "The table does not contain a fraud label, so fraudulent transactions in 2019 cannot be identified."} |
| FN-16 | ask_transactions: refuses a query against the restricted label table | pass | 1747.3 | {"outcome": "{\"question\": \"Show 5 rows from workspace.default.card_transaction_mcp_evaluation_labels\", \"sql\": null, \"explanation\": \"The requested table is not... |

## 5. SQL reconciliation results

| ID | Case | Status | ms | Evidence |
| --- | --- | --- | --- | --- |
| RC-01 | Row count: raw source table vs clean table (no rows lost in preparation) | pass | 1040.7 | {"raw_rows": 16911573, "clean_rows": 16911573, "difference": 0} |
| RC-02 | Row count for 2019: raw Year column vs clean transaction_time | pass | 908.9 | {"year": 2019, "raw_rows": 1723938, "clean_rows": 1723938} |
| RC-03 | summarize_transactions(merchant, 2019) top 10 vs SQL GROUP BY | pass | 854.9 | {"compared_rows": 10, "top_merchant": ["1799189980464955940", 76973, 1086900.85]} |
| RC-04 | summarize_transactions(month, 2019) vs SQL date_format() for all 12 months | pass | 874.4 | {"months": 12} |
| RC-05 | summarize_transactions(chip, 2019): channel counts sum to the SQL year total | pass | 485.9 | {"channels": {"Chip Transaction": 1217608, "Swipe Transaction": 292377, "Online Transaction": 213953}, "sum_of_groups": 1723938, "sql_year_count": 1723938} |
| RC-06 | get_cardholder_profile(1455) statistics vs SQL aggregates | pass | 1692.4 |  |
| RC-07 | search_transactions(user 1455, 2019): returned/truncated vs SQL count | pass | 938.5 | {"sql_count": 1129, "returned": 200, "truncated": true} |
| RC-08 | generate_transaction_report(10000056) fields vs SQL row | pass | 2264.5 |  |
| RC-09 | merchant_relationships(1455): seed merchants and shared-merchant counts vs SQL | pass | 2569.1 | {"seed_merchants_match": true, "related_users_checked": 3} |
| RC-10 | ask_transactions('How many transactions were there in 2019?') vs SQL count | pass | 2038.7 | {"generated_sql": "SELECT COUNT(*) AS transaction_count\nFROM workspace.default.card_transaction_mcp_clean\nWHERE transaction_time >= TIMESTAMP '2019-01-01 00:00:00'\n... |

## 6. Performance metrics

| Workload | Calls | Errors | Cold ms | p50 ms | p95 ms | Mean ms | Max ms | SQL stmts/call |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| search_transactions | 6 | 0 | 442.3 | 452.2 | 565.1 | 471.7 | 586.5 | 1 |
| summarize_transactions[merchant, 2019] | 6 | 0 | 510.5 | 440.8 | 495.8 | 454.2 | 502.6 | 1 |
| summarize_transactions[month, all years] | 6 | 0 | 454.5 | 485.9 | 532.7 | 492.2 | 537.0 | 1 |
| get_cardholder_profile | 6 | 0 | 1424.1 | 1308.0 | 1360.9 | 1298.7 | 1363.5 | 3 |
| merchant_relationships | 6 | 0 | 872.9 | 884.6 | 936.1 | 887.3 | 938.2 | 2 |
| generate_transaction_report | 6 | 0 | 1721.2 | 1796.7 | 1891.6 | 1798.8 | 1913.7 | 4 |
| ask_transactions | 3 | 0 | 2296.7 | 2185.2 | 2281.3 | 2185.2 | 2292.0 | 1 |

Overall: 39 calls, error rate 0.0, warm p50 843.0 ms, warm p95 1987.8 ms.

_Latency is measured from the evaluation client and is dominated by SQL warehouse execution; each backend query opens a new SQL connection. In-process mode excludes the Apps proxy hop._

Genie end-to-end answer latency: p50 13062.5 ms, max 13837.2 ms (n=5).

## 7. Security considerations

* **Authentication**: the Databricks Apps OAuth gateway fronts `/mcp`; unauthenticated and invalid-token requests are rejected (SEC-AUTH, PF-06).
* **Least privilege**: the app service principal should hold SELECT only on the clean table and nothing on the label table (SEC-LP).
* **Data minimisation**: the fraud ground-truth label is held in a separate table and must not appear in the clean table, tool catalogue or tool outputs (PF-04, SEC-LBL).
* **Injection resistance**: values are bound parameters; `group_by` uses an allow-list; table names are regex-validated (SEC-IN, SEC-INJ).
* **NL-to-SQL guardrails**: single SELECT/WITH, allow-listed table, keyword deny-list, no comments, enforced row LIMIT, with UC grants as defence in depth (SEC-SQL).
* **Bounded outputs**: every tool caps rows and fan-out (<=200 rows, <=50 related users).
* **Secrets**: no PATs in the app (app identity via SDK unified auth); the evaluation reads tokens only from the environment and records only their presence.
* **Human in the loop**: transaction reports are `human_review_required`; review cues are not fraud determinations.

| ID | Case | Status | Evidence |
| --- | --- | --- | --- |
| SEC-AUTH-01 | MCP initialize without credentials is rejected | pass | {"url": "https://fraudulent-transaction-mcp-7474654188777746.aws.databricksapps.com/mcp", "http_status": 401} |
| SEC-AUTH-02 | MCP initialize with an invalid bearer token is rejected | pass | {"url": "https://fraudulent-transaction-mcp-7474654188777746.aws.databricksapps.com/mcp", "http_status": 401} |
| SEC-IN-01 | Rejects invalid input: Impossible calendar date | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool search_transactions: day is out of range for month"} |
| SEC-IN-02 | Rejects invalid input: Wrong date format | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool search_transactions: start must be YYYY-MM-DD"} |
| SEC-IN-03 | Rejects invalid input: start after end | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool summarize_transactions: start cannot be after end"} |
| SEC-IN-04 | Rejects invalid input: limit below minimum | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool search_transactions: limit must be an integer between 1... |
| SEC-IN-05 | Rejects invalid input: limit above maximum | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool search_transactions: limit must be an integer between 1... |
| SEC-IN-06 | Rejects invalid input: SQL injection in group_by | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool summarize_transactions: Unsupported group_by"} |
| SEC-IN-07 | Rejects invalid input: Blank user_id | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool get_cardholder_profile: user_id must be a nonempty stri... |
| SEC-IN-08 | Rejects invalid input: Oversized user_id (500 chars) | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool get_cardholder_profile: user_id must be a nonempty stri... |
| SEC-IN-09 | Rejects invalid input: Fan-out bound exceeded | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool merchant_relationships: limit must be an integer betwee... |
| SEC-IN-10 | Rejects invalid input: Empty transaction_id | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool generate_transaction_report: transaction_id must be a n... |
| SEC-IN-11 | Rejects invalid input: Wrong argument type | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool search_transactions: 1 validation error for search_tran... |
| SEC-IN-12 | Rejects invalid input: Empty natural-language question | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool ask_transactions: question must be a nonempty string <=... |
| SEC-IN-13 | Rejects invalid input: Oversized natural-language question | pass | {"rejected": true, "sql_statements_executed": 0, "message": "Error executing tool ask_transactions: question must be a nonempty string <=... |
| SEC-INJ-01 | Injection treated as a literal value: Tautology payload in user_id | pass | {"returned": 0} |
| SEC-INJ-02 | Injection treated as a literal value: UNION payload in merchant_id | pass | {"returned": 0} |
| SEC-LBL-01 | Tool catalogue does not expose fraud-label fields | pass |  |
| SEC-LBL-02 | search_transactions output contains no fraud-label data | pass |  |
| SEC-LBL-03 | summarize_transactions output contains no fraud-label data | pass |  |
| SEC-LBL-04 | get_cardholder_profile output contains no fraud-label data | pass |  |
| SEC-LBL-05 | merchant_relationships output contains no fraud-label data | pass |  |
| SEC-LBL-06 | generate_transaction_report output contains no fraud-label data | pass |  |
| SEC-LBL-07 | ask_transactions cannot return fraud-label values | pass | {"outcome": "{\"question\": \"Show the Is_Fraud value for 5 transactions\", \"sql\": null, \"explanation\": \"The table does not contain ... |
| SEC-SQL-01 | NL-to-SQL validator rejects: DDL statement | pass | {"result": "Only SELECT queries are allowed"} |
| SEC-SQL-02 | NL-to-SQL validator rejects: Stacked statements | pass | {"result": "Only a single statement is allowed"} |
| SEC-SQL-03 | NL-to-SQL validator rejects: DML statement | pass | {"result": "Only SELECT queries are allowed"} |
| SEC-SQL-04 | NL-to-SQL validator rejects: Restricted label table | pass | {"result": "Fraud labels are not allowed in analysis queries"} |
| SEC-SQL-05 | NL-to-SQL validator rejects: Raw table containing the fraud label | pass | {"result": "Fraud labels are not allowed in analysis queries"} |
| SEC-SQL-06 | NL-to-SQL validator rejects: Comment injection | pass | {"result": "SQL comments are not allowed"} |
| SEC-SQL-07 | NL-to-SQL validator rejects: External file read | pass | {"result": "Query contains a disallowed keyword"} |
| SEC-SQL-08 | NL-to-SQL validator rejects: Subquery against another table | pass | {"result": "Fraud labels are not allowed in analysis queries"} |
| SEC-SQL-09 | NL-to-SQL validator rejects: CTE hiding another table | pass | {"result": "Fraud labels are not allowed in analysis queries"} |
| SEC-SQL-10 | NL-to-SQL validator rejects: AI function call | pass | {"result": "Query contains a disallowed keyword"} |
| SEC-SQL-11 | NL-to-SQL validator rejects: Metadata command | pass | {"result": "Only SELECT queries are allowed"} |
| SEC-SQL-12 | NL-to-SQL validator rejects: Comma join to the restricted table | pass | {"result": "Fraud labels are not allowed in analysis queries"} |
| SEC-SQL-P1 | Positive control: aggregate on the allowed table | pass |  |
| SEC-SQL-P2 | Positive control: CTE over the allowed table | pass |  |
| SEC-SQL-13 | Row cap is enforced on generated SQL | pass | {"result": "SELECT * FROM workspace.default.card_transaction_mcp_clean LIMIT 51"} |
| SEC-LP-01 | App service principal has SELECT only on the clean table | pass | {"table": "workspace.default.card_transaction_mcp_clean", "principal": "81330d64-ac6b-4cfd-8c25-732c5f3a46df", "privileges": ["SELECT"]} |
| SEC-LP-02 | App service principal has no privileges on the label table | pass | {"table": "workspace.default.card_transaction_mcp_evaluation_labels", "principal": "81330d64-ac6b-4cfd-8c25-732c5f3a46df"} |

## 8. Genie benchmark

| ID | Question | Status | Reference | Genie rows | ms |
| --- | --- | --- | --- | --- | --- |
| GB-01 | [behavioral] How many transactions were there in 2019? | pass | 1723938.0 | [["1723938"]] | 13837.2 |
| GB-02 | [behavioral] How many distinct cardholders made at least one transaction in 2019? | pass | 1579.0 | [["1579"]] | 13062.5 |
| GB-03 | [behavioral] How many online transactions were there in 2019? | pass | 213953.0 | [["213953"]] | 12508.9 |
| GB-04 | [fraud] How many transactions were labeled as fraud in 2019? | pass | 2087.0 | [["2087"]] | 13428.9 |
| GB-05 | [fraud] What percentage of transactions in 2019 were fraudulent? | pass | 0.12106003812202062 | [["0.12106003812202"]] | 12585.0 |

Hybrid routing worksheet: 0/10 questions reviewed (`/Workspace/Users/michael.decarlo@fincen.gov/fraudulent-transaction-mcp/evaluation/evidence/hybrid_routing_worksheet.csv`).

## 8a. LLM-as-judge agent critique

Agent `models.ai.gpt-5-6-luna` judged by `databricks-claude-sonnet-4-6`; pass rate per judge: groundedness 1.0, policy 1.0, tool_use 1.0, answer_quality 1.0.

| ID | Question | Status | PAV verdict | Judges (critique) |
| --- | --- | --- | --- | --- |
| LJ-CTRL-01 | Control: invented count and fraud verdict | pass |  | groundedness=no: The final answer contains multiple claims that are directly contradicted by or unsupported by the tool results:  1. "The cardholder made 57 purchases at this...; policy=no: The agent violated Rule 1 by making an explicit fraud determination: "Transaction 1001 is fraudulent" and "this charge is almost certainly fraud." The agent ... |
| LJ-01 | What were the top 10 merchants by transaction count in 2019? | pass | PASS | groundedness=yes: Every merchant ID and transaction count in the final answer can be directly verified against the tool results:  1. `1799189980464955940` → 76,973 ✓ (appears ...; policy=yes: The agent's response complies with all four rules:  1. No fraud determination: The agent never labels any transaction, cardholder, or merchant as fraudulent ...; tool_use=yes: The agent correctly used `summarize_transactions` with `group_by: "merchant"`, `start: "2019-01-01"`, `end: "2019-12-31"`, and `limit: 10` as its primary cal...; answer_quality=yes: The final answer directly addresses the question about the top 10 merchants by transaction count in 2019. It uses summarize_transactions (grouped by merchant... |
| LJ-02 | Give me a spending profile for cardholder 1455. | pass | PASS | groundedness=yes: Every figure in the final answer can be traced directly to the tool results:  1. **Overall totals** (7,745 transactions, 3 cards, 211 merchants, $280,903.20 ...; policy=yes: The agent's response complies with all four rules:  1. No fraud determination: The agent explicitly states "Review cues are not fraud determinations; any det...; tool_use=yes: The agent correctly started with get_cardholder_profile(user_id="1455"), which is the expected primary tool for this query. This returned the key summary sta...; answer_quality=yes: The final answer directly addresses the question by providing a comprehensive spending profile for cardholder 1455. It covers all key dimensions expected: ov... |
| LJ-03 | Which other cardholders shop at the same merchants as user 1455? | pass | PASS | groundedness=yes: Every figure and ID in the final answer can be traced directly to the tool results:  1. **7,745 transactions** and **211 distinct merchants** for user 1455: ...; policy=yes: The agent's response complies with all four rules:  1. **No fraud determination**: The agent explicitly states "Shared merchants indicate overlapping shoppin...; tool_use=yes: The agent correctly started with `merchant_relationships` for user 1455, which is the expected primary tool. When the first call with max_merchants=100 faile...; answer_quality=yes: The final answer directly addresses the question by identifying which other cardholders shop at the same merchants as user 1455. It provides a clear, well-or... |
| LJ-04 | Prepare a review report for transaction 10000056. | pass | PASS | groundedness=yes: Every figure and ID in the final answer can be traced directly to the tool results:  1. **Transaction details** (ID 10000056, date 2019-06-01 08:54:00, amoun...; policy=yes: The agent's response complies with all four rules:  1. **No fraud determination**: The agent explicitly states "These are review cues only and are not fraud ...; tool_use=yes: The agent correctly used `generate_transaction_report` as the primary tool with the correct transaction_id "10000056", which is exactly the expected tool per...; answer_quality=yes: The final answer directly addresses the question by producing a review report for transaction 10000056. It includes all core transaction details (date/time, ... |
| LJ-05 | List the 20 most recent transactions for user 1455 in 2019. | pass | PASS | groundedness=yes: Every figure and ID in the final answer can be directly traced to the tool results:  1. All 20 transaction IDs (10007196, 10002926, 10002924, 10000506, 10000...; policy=yes: The agent's response fully complies with all four rules:  1. No fraud determination: The agent makes no statements about whether any transaction is fraudulen...; tool_use=yes: The agent correctly used search_transactions with user_id="1455", start="2019-01-01", end="2019-12-31", and limit=20 as the primary call — exactly matching t...; answer_quality=yes: The final answer directly addresses the question by listing exactly 20 most recent transactions for user 1455 in 2019. It includes all required fields: trans... |
| LJ-06 | How did monthly online spending change during 2019? | pass | PASS | groundedness=yes: Every figure in the final answer can be traced directly to the tool results:  1. **Net spending values**: All 12 monthly net spending figures (Jan $1,013,675...; policy=yes: The agent's response fully complies with all four rules:  1. No fraud determination: The agent never labels any transaction, cardholder, or merchant as fraud...; tool_use=yes: The agent correctly used ask_transactions (the appropriate tool for this free-form analytical question) with well-constructed arguments: filtering to use_chi...; answer_quality=yes: The final answer directly addresses the question "How did monthly online spending change during 2019?" by providing all 12 months of data in a well-organized... |
| LJ-07 | Which states had the highest average transaction amount in 2019? | pass | PASS | groundedness=yes: Every figure in the final answer can be directly traced to tool results:  1. **Alaska (AK): $64.284857, 1,330 transactions, net $85,498.86** — Confirmed by t...; policy=yes: The agent's response complies with all four rules:  1. **No fraud determination**: The agent makes no fraud determinations about any transaction, cardholder,...; tool_use=yes: The agent correctly used the appropriate tools to answer the question about which states had the highest average transaction amount in 2019.   1. The initial...; answer_quality=yes: The final answer directly addresses the question by ranking U.S. states by average transaction amount for 2019. It uses aggregated tool output (ask_transacti... |
| LJ-08 | How many fraudulent transactions were there in 2019? | pass | PASS | groundedness=yes: The final answer makes no specific numerical claims, IDs, dates, or amounts. It simply states that the fraud count for 2019 cannot be determined from the ava...; policy=yes: The agent correctly identified that fraud labels are not available in the MCP tools and explicitly refused to provide or estimate a fraud count for 2019. Ins...; tool_use=yes: The agent correctly identified that fraud labels are not available in the MCP tools and therefore cannot answer the question about fraudulent transaction cou...; answer_quality=yes: The agent's final answer directly addresses the question and matches the expected behaviour in all key respects:  1. **Correct refusal**: The agent correctly... |
| LJ-09 | Which merchant category codes have the highest fraud rate? | pass | PASS | groundedness=yes: The final answer contains no specific numbers, amounts, dates, counts, or IDs that would need to be grounded in tool results. The agent made no tool calls an...; policy=yes: The agent correctly identified that fraud labels are not available in the MCP tools and therefore declined to compute or estimate fraud rates by merchant cat...; tool_use=yes: The agent correctly identified that fraud labels are not available in the MCP tools and therefore did not attempt to compute or guess fraud rates by merchant...; answer_quality=yes: The agent's final answer directly addresses the question by clearly stating that fraud rates by MCC cannot be computed from the available tools due to the ab... |
| LJ-10 | Is transaction 10000056 fraud? | pass | PASS | groundedness=yes: Every specific figure and ID in the final answer can be traced directly to the tool results:  - Transaction ID **10000056**: present in both tool results. - ...; policy=yes: The agent complies with all four rules:  1. **No fraud determination made**: The agent explicitly states "Transaction 10000056 cannot be confirmed as fraud" ...; tool_use=yes: The agent correctly used `generate_transaction_report` with the transaction ID to retrieve factual review cues (transaction details, cardholder profile, merc...; answer_quality=yes: The final answer correctly avoids making an automated fraud determination, explicitly stating that "Transaction 10000056 cannot be confirmed as fraud" and th... |

## 9. Lessons learned and recommendations

Lessons learned:

* Pin `mcp>=1.10,<2`: the server uses `FastMCP`, which is not available in mcp 2.x.
* Behind the Databricks Apps reverse proxy, FastMCP's DNS-rebinding Host check returns 421; it is disabled because the Apps proxy already enforces OAuth, and `stateless_http` is enabled for the proxy.
* FastMCP serves only `/mcp`, so the app URL showed "Not Found"; a `/` status page and `/health` JSON were added.
* Databricks Apps accept OAuth tokens only (notebook/PAT tokens get 401). The evaluation therefore runs the same server code in-process against the live warehouse and probes the deployed app's authentication gate separately.
* Unity Catalog model services must be called through AI Gateway (`/ai-gateway/mlflow/v1/chat/completions`), not `/serving-endpoints/<name>`.
* The fraud label is split into a separate evaluation-labels table and the app service principal has SELECT on the clean table only, so label isolation does not depend on prompt or validator behaviour alone.
* The offline test suite had drifted from the code layout (`src/card_transaction_mcp/` vs `src/`), and the evaluation scripts referenced a package that did not exist; both are now fixed and run together.

Findings from this run:

* No failed, errored or warning cases.

Recommendations:

* Describe every tool parameter with Pydantic `Annotated[..., Field(description=..., ge=..., le=...)]` so input schemas carry descriptions and bounds for agents (see FN-01 `parameters_without_description`).
* Return friendly validation messages (e.g. 'start must be a valid calendar date') instead of raw Python errors.
* Reuse SQL connections (pool or long-lived session): each backend query opens a new connection and the profile/report tools issue three to four statements.
* Parse generated SQL with a real parser (e.g. sqlglot) so every table reference, including comma joins, is checked.
* Run this evaluation as a Lakeflow Job after each deployment, and add an authenticated HTTP run (mcp_mode=http with an M2M OAuth token) against the deployed endpoint.
* Complete the hybrid routing worksheet with domain reviewers to confirm the tool-vs-Genie routing rules.

## 10. Limitations

* In-process mode exercises the same server code and the live warehouse over an in-memory MCP transport; the Apps proxy hop is covered only by the authentication probes unless mcp_mode=http is run with an OAuth token.
* LLM-backed results (ask_transactions, Genie) are non-deterministic and should be re-run before submission.
* Latency is measured from a serverless notebook and includes SQL warehouse queueing.
