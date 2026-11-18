# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# DBTITLE 1,About this notebook
# MAGIC %md
# MAGIC ## Card Transaction MCP - post-deployment evaluation
# MAGIC Runs `evaluation/scripts/run_all.py` in this notebook's Python process, so the suites use this notebook's Databricks credentials. A `%sh` or `!python` subprocess does not inherit them.
# MAGIC
# MAGIC Settings come from `evaluation/eval_config.yaml`; you can override any key with an `EVAL_<KEY>` environment variable. Results go to `evaluation/evidence/` and reports to `evaluation/reports/`, including `evaluation_test_matrix.md`.
# MAGIC
# MAGIC The LLM-as-judge suite (`10_llm_judge.py`, evidence `llm_judge.json`) runs the Plan -> Act -> Verify chat agent on the hybrid-routing questions and has `judge_endpoint` critique each answer. It needs `mlflow>=3` and `openai`, and takes several minutes; set `skip_judge` to `true` to leave it out.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install -q "mcp>=1.10,<2" "databricks-sql-connector>=3.4" "databricks-sdk>=0.30" "pydantic-settings>=2.0" "sqlglot>=25" openai "mlflow>=3" uvicorn pytest pyyaml
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Options
dbutils.widgets.dropdown("skip_unit", "false", ["true", "false"])
dbutils.widgets.dropdown("skip_genie", "false", ["true", "false"])
dbutils.widgets.dropdown("skip_performance", "false", ["true", "false"])
dbutils.widgets.dropdown("skip_judge", "false", ["true", "false"])
dbutils.widgets.text("config", "")

# COMMAND ----------

# DBTITLE 1,Run all suites
import os, sys
from pathlib import Path

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
SCRIPTS = Path("/Workspace" + nb_path).parent / "scripts"
os.chdir(SCRIPTS)
sys.path.insert(0, str(SCRIPTS))
# Drop modules cached from an earlier run so code edits are picked up.
for m in [m for m in sys.modules if m.split(".")[0] in ("card_transaction_eval", "src", "run_all", "_bootstrap")]:
    del sys.modules[m]

import run_all
argv = [f"--{k.replace('_', '-')}" for k in ("skip_unit", "skip_genie", "skip_performance", "skip_judge")
        if dbutils.widgets.get(k) == "true"]
if dbutils.widgets.get("config"):
    argv += ["--config", dbutils.widgets.get("config")]
exit_code = run_all.main(argv)
print("exit code:", exit_code)
if exit_code == 2:
    raise RuntimeError("Databricks authentication unavailable; see the Auth line above")

# COMMAND ----------

# DBTITLE 1,Test matrix summary
import pandas as pd
reports = SCRIPTS.parent / "reports"
display(pd.read_csv(reports / "evaluation_summary.csv"))
display(pd.read_csv(reports / "evaluation_test_matrix.csv"))