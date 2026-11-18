from __future__ import annotations

import hashlib
import importlib.metadata
# import json  # [CLEANUP 2026-11-17] unused import
import os
import platform
import subprocess
from pathlib import Path

from _bootstrap import PROJECT_ROOT
from card_transaction_eval.config import load_config
from card_transaction_eval.utils import utc_now, write_json


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def main() -> None:
    config = load_config()
    source_files = [
        PROJECT_ROOT / "src/fraud_transaction_mcp/analytics.py",
        PROJECT_ROOT / "src/fraud_transaction_mcp/app.py",
        PROJECT_ROOT / "src/fraud_transaction_mcp/sql_guard.py",
        PROJECT_ROOT / "src/fraud_transaction_mcp/tools/__init__.py",
        PROJECT_ROOT / "src/fraud_transaction_mcp/backend.py",
        PROJECT_ROOT / "src/fraud_transaction_mcp/routes.py",
        PROJECT_ROOT / "src/fraud_transaction_mcp/nl_sql.py",
        PROJECT_ROOT / "app.yaml",
        PROJECT_ROOT / "client/llm_client.py",
        PROJECT_ROOT / "databricks/sql/02_genie_views.sql",
    ]
    payload = {
        "captured_at_utc": utc_now(),
        "git_commit": git_commit(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "package_versions": {
            "mcp": version("mcp"),
            "databricks-sql-connector": version("databricks-sql-connector"),
            "databricks-sdk": version("databricks-sdk"),
            "httpx": version("httpx"),
            "pydantic": version("pydantic"),
        },
        "configured_resources": {
            "custom_mcp_url": config.custom_mcp_url,
            "genie_mcp_url": config.genie_mcp_url,
            "sql_hostname": config.sql_hostname,
            "sql_http_path": config.sql_http_path,
            "clean_table": config.clean_table,
            "label_table": config.label_table,
        },
        "source_file_sha256": {
            str(path.relative_to(PROJECT_ROOT)): file_sha256(path)
            for path in source_files
            if path.exists()
        },
        "credential_presence": {
            "custom_mcp_oauth_token_present": bool(config.custom_oauth_token),
            "genie_mcp_oauth_token_present": bool(config.genie_oauth_token),
            "sql_token_present": bool(config.sql_token),
            "oauth_client_id_present": bool(os.getenv("DATABRICKS_CLIENT_ID")),
            "oauth_client_secret_present": bool(os.getenv("DATABRICKS_CLIENT_SECRET")),
        },
    }
    path = write_json(config.evidence_dir / "deployment_runtime_metadata.json", payload)
    print(path)


if __name__ == "__main__":
    main()
