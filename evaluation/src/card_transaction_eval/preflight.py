"""Preflight: configuration, data access, MCP tool catalogue and dependency readiness."""
from __future__ import annotations

from .mcp_session import EXPECTED_TOOLS, open_session, sql_backend, transport_label
from .utils import (ERROR, FAIL, PASS, SKIPPED, WARN, SuiteResult, Timer, http_status,
                    quiet_logs, workspace_client)

DESCRIPTION = ("Readiness checks run before functional testing: configuration, SQL warehouse state, "
               "clean-table access, schema-level fraud-label isolation, MCP tool catalogue, the deployed "
               "app's authentication gate, Genie space availability and the NL-to-SQL model endpoint.")


async def run_preflight(config) -> SuiteResult:
    quiet_logs()
    r = SuiteResult("preflight", description=DESCRIPTION)
    r.metrics["config"] = config.public_dict()
    r.metrics["transport"] = transport_label(config)

    missing = [k for k in ("sql_hostname", "sql_http_path", "clean_table") if not getattr(config, k)]
    r.add("PF-01", "Evaluation configuration resolved", FAIL if missing else PASS,
          missing=missing, mcp_mode=config.mcp_mode, source=config.config_source)

    w = workspace_client()
    if w is None:
        r.add("PF-02", "SQL warehouse state", SKIPPED, reason="Databricks SDK authentication unavailable")
    else:
        try:
            state = w.warehouses.get(config.sql_warehouse_id).state
            state = getattr(state, "value", str(state))
            r.add("PF-02", "SQL warehouse state", PASS if state == "RUNNING" else WARN,
                  warehouse_id=config.sql_warehouse_id, state=state)
        except Exception as exc:
            r.add("PF-02", "SQL warehouse state", ERROR, error=repr(exc)[:300])

    try:
        with Timer() as t:
            rows = sql_backend(config).query(f"SELECT COUNT(*) AS n FROM {config.clean_table}")
        n = int(rows[0]["n"])
        r.metrics["clean_table_rows"] = n
        r.add("PF-03", "Databricks SQL connectivity; clean table readable", PASS if n > 0 else FAIL,
              t.ms, table=config.clean_table, rows=n)
    except Exception as exc:
        r.add("PF-03", "Databricks SQL connectivity; clean table readable", ERROR, error=repr(exc)[:300])

    if w is None:
        r.add("PF-04", "Clean table schema excludes the fraud label", SKIPPED, reason="no SDK auth")
    else:
        try:
            cols = [c.name for c in w.tables.get(config.clean_table).columns]
            leaked = [c for c in cols if "fraud" in c.lower()]
            r.add("PF-04", "Clean table schema excludes the fraud label", FAIL if leaked else PASS,
                  columns=cols, leaked=leaked)
        except Exception as exc:
            r.add("PF-04", "Clean table schema excludes the fraud label", ERROR, error=repr(exc)[:300])

    try:
        with Timer() as t:
            async with open_session(config) as session:
                tools = (await session.list_tools()).tools
        names = sorted(x.name for x in tools)
        missing_tools = sorted(set(EXPECTED_TOOLS) - set(names))
        r.add("PF-05", "MCP server initializes and lists the expected tools",
              FAIL if missing_tools else PASS, t.ms, transport=transport_label(config), tools=names,
              missing=missing_tools, unexpected=sorted(set(names) - set(EXPECTED_TOOLS)))
    except Exception as exc:
        r.add("PF-05", "MCP server initializes and lists the expected tools", ERROR, error=repr(exc)[:300])

    url = config.app_base_url + "/health"
    token = config.custom_oauth_token
    code, err = http_status(url, token=token)
    if token:
        status, name = (PASS if code == 200 else FAIL), "Deployed Databricks App /health (authenticated)"
    else:
        status = PASS if code in (302, 401, 403) else (WARN if code is None else FAIL)
        name = "Deployed Databricks App is reachable and rejects unauthenticated requests"
    r.add("PF-06", name, status, url=url, http_status=code, authenticated=bool(token), error=err or None)

    if not config.enable_genie or w is None:
        r.add("PF-07", "Genie spaces available", SKIPPED,
              reason="genie disabled" if not config.enable_genie else "no SDK auth")
    else:
        found, errors = {}, {}
        for key, space_id in config.genie_spaces.items():
            try:
                found[key] = getattr(w.genie.get_space(space_id), "title", None) or space_id
            except Exception as exc:
                errors[key] = repr(exc)[:200]
        r.add("PF-07", "Genie spaces available", FAIL if errors else PASS, spaces=found, errors=errors)

    if not config.enable_llm:
        r.add("PF-08", "NL-to-SQL model endpoint responds", SKIPPED, reason="llm disabled")
    else:
        try:
            from fraud_transaction_mcp import nl_sql
            with Timer() as t:
                sql, _ = nl_sql._parse(nl_sql._call_llm("How many transactions are in the table?", config.clean_table))
            r.add("PF-08", "NL-to-SQL model endpoint responds", PASS if sql else WARN, t.ms,
                  endpoint=config.llm_endpoint, generated_sql=sql)
        except Exception as exc:
            r.add("PF-08", "NL-to-SQL model endpoint responds", ERROR, endpoint=config.llm_endpoint,
                  error=repr(exc)[:300])
    return r.finish()
