# Databricks Apps deployment

App name: `fraudulent-transaction-mcp`. Source path: this repo root (`app.yaml` must be at the root).

## Resources (app settings)

| Resource key | Type | Permission | Used by |
| --- | --- | --- | --- |
| `sql-warehouse` | SQL warehouse `50d2bd4ddf65e0f5` | CAN_USE | `DATABRICKS_WAREHOUSE_ID` |
| `model` | UC model service `models.ai.gpt-5-6-luna` | EXECUTE | `ask_transactions` |

Unity Catalog grants for the app service principal: `USE CATALOG workspace`, `USE SCHEMA
workspace.default`, `SELECT` on `workspace.default.card_transaction_mcp_clean`. Grant nothing on the
label table unless the deployment is for evaluation (`ENABLE_EVALUATION_TOOLS=true`).

## Deploy

```bash
databricks apps create fraudulent-transaction-mcp        # first time; then add the resources
databricks apps deploy fraudulent-transaction-mcp \
  --source-code-path /Workspace/Users/<you>/fraudulent-transaction-mcp
```

## Smoke test

* `https://fraudulent-transaction-mcp-7474654188777746.aws.databricksapps.com/health` returns `status: ok` (requires Databricks login in a browser).
* Unauthenticated `POST /mcp` returns 302/401 (expected; the Apps proxy enforces OAuth).

## Pointing the client at this server

`client/app.yaml` sets `MCP_SERVER_URL` to this app. Give the client app's service principal
CAN_USE on `fraudulent-transaction-mcp` before deploying a client from this folder.
