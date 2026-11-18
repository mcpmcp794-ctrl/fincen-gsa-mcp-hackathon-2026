"""Evaluation configuration.

Precedence (lowest -> highest): built-in DEFAULTS, an optional YAML/JSON file
(``--config`` / ``EVAL_CONFIG`` / ``evaluation/eval_config.yaml``), then
``EVAL_<KEY>`` environment variables. Secrets are read only from the environment
and never written to evidence (only their presence is recorded).

Environment secrets:
  CUSTOM_MCP_OAUTH_TOKEN  OAuth token for the deployed MCP app (mcp_mode=http). When unset,
                          a token is minted from an evaluation service principal (OAuth M2M):
                          EVAL_SP_CLIENT_ID / EVAL_SP_CLIENT_SECRET, or the keys client_id /
                          client_secret in the secret scope named by eval_sp_secret_scope.
  GENIE_MCP_OAUTH_TOKEN   optional, recorded for metadata only
  DATABRICKS_TOKEN        optional SQL fallback for local runs
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# <project>/evaluation/src/card_transaction_eval/config.py
EVAL_ROOT = Path(__file__).resolve().parents[2]      # <project>/evaluation
PROJECT_ROOT = EVAL_ROOT.parent                       # <project>
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}")

DEFAULTS = {
    # inprocess: real server code + real SQL warehouse over an in-memory MCP transport.
    # http: the deployed Databricks App over streamable HTTP (needs an OAuth token).
    "mcp_mode": "inprocess",
    "custom_mcp_url": "https://fraudulent-transaction-mcp-7474654188777746.aws.databricksapps.com/mcp",
    "sql_warehouse_id": "50d2bd4ddf65e0f5",
    "clean_table": "workspace.default.card_transaction_clean",
    "raw_table": "workspace.default.card_transaction",
    "label_table": "workspace.default.card_transaction_evaluation_labels",
    "llm_endpoint": "models.ai.gpt-5-6-luna",
    "app_service_principal": "4ce49d10-a772-445c-a2c3-a2a097c7e9c0",
    "genie_spaces": {"behavioral": "01f1da1b073e175183f19e882132d64d",
                     "fraud": "01f1dc44f22413fe82320128338353b5"},
    "enable_llm": True,
    "enable_genie": True,
    "benchmark_year": 2019,
    "performance_iterations": 5,
    "performance_llm_iterations": 2,
    "p95_threshold_ms": 15000.0,
    "p95_llm_threshold_ms": 45000.0,
    "genie_timeout_s": 300,
    "evidence_dir": "evaluation/evidence",
    "reports_dir": "evaluation/reports",
    # LLM-as-judge suite (10_llm_judge): judge model should differ from llm_endpoint.
    "enable_judge": True,
    "judge_endpoint": "databricks-claude-sonnet-4-6",
    "judge_cases": "",                 # comma-separated HR ids (e.g. "HR-01,HR-08"); empty = all
    "judge_mlflow_experiment": "",     # optional MLflow experiment path to log judge runs
    # mcp_mode=http: secret scope holding the evaluation service principal's OAuth
    # client_id / client_secret (the SP needs CAN_USE on the MCP app).
    "eval_sp_secret_scope": "",
    # Rule-based risk scoring suite (11_risk_scoring).
    "enable_risk_suite": True,
    "risk_ranking_threshold_ms": 90000.0,    # population ranking SQL (one month)
    "risk_pav_threshold_ms": 180000.0,       # end-to-end agent answer to the ranking question
    "risk_stability_runs": 3,                # agent runs of the ranking question (0 = skip)
    # Efficacy (reads the label table): comma-separated start:end windows, each <= 31 days.
    "risk_efficacy_windows": "2019-01-01:2019-01-31,2019-06-01:2019-06-30,2019-11-01:2019-11-30",
    "risk_efficacy_max_cards": 2000,
    "risk_efficacy_min_n": 200,              # min transactions for a score level to count in RS-E02
    "risk_min_high_lift": 3.0,               # RS-E03 bar: high-band fraud rate / base rate
    "risk_max_indicator_flag_rate": 0.10,    # RS-E04 bar: share of transactions one indicator flags
}


@dataclass
class EvalConfig:
    mcp_mode: str
    custom_mcp_url: str
    sql_warehouse_id: str
    clean_table: str
    raw_table: str
    label_table: str
    llm_endpoint: str
    app_service_principal: str
    genie_spaces: dict
    enable_llm: bool
    enable_genie: bool
    benchmark_year: int
    performance_iterations: int
    performance_llm_iterations: int
    p95_threshold_ms: float
    p95_llm_threshold_ms: float
    genie_timeout_s: int
    evidence_dir: Path
    reports_dir: Path
    enable_judge: bool
    judge_endpoint: str
    judge_cases: str
    judge_mlflow_experiment: str
    eval_sp_secret_scope: str
    enable_risk_suite: bool
    risk_ranking_threshold_ms: float
    risk_pav_threshold_ms: float
    risk_stability_runs: int
    risk_efficacy_windows: str
    risk_efficacy_max_cards: int
    risk_efficacy_min_n: int
    risk_min_high_lift: float
    risk_max_indicator_flag_rate: float
    sql_hostname: str | None = None
    sql_http_path: str | None = None
    sql_token: str | None = None
    custom_oauth_token: str | None = None
    genie_oauth_token: str | None = None
    config_source: str = "defaults"
    project_root: Path = PROJECT_ROOT
    eval_root: Path = EVAL_ROOT

    @property
    def app_base_url(self) -> str:
        return self.custom_mcp_url.rsplit("/mcp", 1)[0]

    @property
    def genie_mcp_url(self) -> str | None:
        if not self.sql_hostname or not self.genie_spaces:
            return None
        return f"https://{self.sql_hostname}/api/2.0/mcp/genie/{next(iter(self.genie_spaces.values()))}"

    def public_dict(self) -> dict:
        """Configuration safe to write to evidence (no secrets)."""
        hidden = {"sql_token", "custom_oauth_token", "genie_oauth_token"}
        out = {k: (str(v) if isinstance(v, Path) else v) for k, v in self.__dict__.items() if k not in hidden}
        out.update({f"{k}_present": bool(getattr(self, k)) for k in sorted(hidden)})
        out["genie_mcp_url"] = self.genie_mcp_url
        return out


def _read_file(path: Path) -> dict:
    text = path.read_text()
    if path.suffix.lower() == ".json":
        return json.loads(text) or {}
    import yaml
    return yaml.safe_load(text) or {}


def _coerce(value, default):
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, dict):
        return json.loads(value) if isinstance(value, str) else dict(value)
    return str(value)


def _resolve_host() -> str | None:
    host = os.getenv("DATABRICKS_SERVER_HOSTNAME") or os.getenv("DATABRICKS_HOST")
    if not host:
        try:
            from databricks.sdk import WorkspaceClient
            host = WorkspaceClient().config.host
        except Exception:
            host = None
    return host.replace("https://", "").replace("http://", "").rstrip("/") if host else None


def _sp_credentials(scope: str) -> tuple[str | None, str | None]:
    """Evaluation SP (client_id, client_secret) from the environment, else the secret scope."""
    cid, secret = os.getenv("EVAL_SP_CLIENT_ID"), os.getenv("EVAL_SP_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    if not scope:
        return None, None
    import base64
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    get = lambda key: base64.b64decode(w.secrets.get_secret(scope, key).value).decode()  # noqa: E731
    return get("client_id"), get("client_secret")


def mint_sp_token(host: str, client_id: str, client_secret: str) -> str:
    """OAuth M2M access token (about 1 hour) for a service principal."""
    from databricks.sdk.core import Config
    cfg = Config(host=f"https://{host}", client_id=client_id, client_secret=client_secret,
                 auth_type="oauth-m2m")
    return cfg.authenticate()["Authorization"].split(" ", 1)[1]


def check_auth(config: EvalConfig) -> tuple[bool, str]:
    """Verify workspace authentication before running live suites.

    Returns (ok, message). Uses DATABRICKS_TOKEN if set, otherwise Databricks SDK
    unified auth (notebook/job runtime, OAuth M2M via DATABRICKS_CLIENT_ID/SECRET,
    or a CLI profile).
    """
    try:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient(host=f"https://{config.sql_hostname}", token=config.sql_token) if config.sql_token \
            else WorkspaceClient()
        me = w.current_user.me()
        return True, f"authenticated as {me.user_name or me.display_name}"
    except Exception as exc:
        return False, (
            "Databricks authentication is not available in this process "
            f"({type(exc).__name__}: {str(exc)[:160]}). Run the evaluation from the "
            "evaluation/run_evaluation notebook or a Lakeflow Job task, or set DATABRICKS_HOST plus "
            "DATABRICKS_CLIENT_ID/DATABRICKS_CLIENT_SECRET (OAuth M2M) or a CLI profile. A shell "
            "subprocess started from a notebook (%sh, !python) does not inherit notebook credentials.")


def load_config(path: str | None = None) -> EvalConfig:
    values = dict(DEFAULTS)
    sources = ["defaults"]
    candidate = path or os.getenv("EVAL_CONFIG")
    file_path = Path(candidate) if candidate else EVAL_ROOT / "eval_config.yaml"
    if candidate and not file_path.exists():
        raise FileNotFoundError(f"Evaluation config not found: {file_path}")
    if file_path.exists():
        data = _read_file(file_path)
        unknown = sorted(set(data) - set(DEFAULTS))
        if unknown:
            raise ValueError(f"Unknown keys in {file_path.name}: {unknown}")
        values.update(data)
        sources.append(file_path.name)
    for key in DEFAULTS:
        env = os.getenv(f"EVAL_{key.upper()}")
        if env is not None:
            values[key] = env
            sources.append(f"EVAL_{key.upper()}")
    values = {k: _coerce(v, DEFAULTS[k]) for k, v in values.items()}

    if values["mcp_mode"] not in ("inprocess", "http"):
        raise ValueError("mcp_mode must be 'inprocess' or 'http'")
    for key in ("clean_table", "raw_table", "label_table"):
        if not _IDENT.fullmatch(values[key]):
            raise ValueError(f"Unsafe table identifier for {key}")
    if not re.fullmatch(r"[A-Za-z0-9]+", values["sql_warehouse_id"]):
        raise ValueError("Invalid sql_warehouse_id")
    if not 2010 <= values["benchmark_year"] <= 2020:
        raise ValueError("benchmark_year must be within the dataset range 2010-2020")
    for key in ("evidence_dir", "reports_dir"):
        p = Path(values[key])
        values[key] = p if p.is_absolute() else PROJECT_ROOT / p

    host = _resolve_host()
    http_path = os.getenv("DATABRICKS_HTTP_PATH") or f"/sql/1.0/warehouses/{values['sql_warehouse_id']}"
    config = EvalConfig(**values, sql_hostname=host, sql_http_path=http_path,
                        sql_token=os.getenv("DATABRICKS_TOKEN"),
                        custom_oauth_token=os.getenv("CUSTOM_MCP_OAUTH_TOKEN"),
                        genie_oauth_token=os.getenv("GENIE_MCP_OAUTH_TOKEN"),
                        config_source="+".join(sources))
    if config.mcp_mode == "http" and not config.custom_oauth_token:
        cid, secret = _sp_credentials(config.eval_sp_secret_scope)
        if cid and secret and host:
            config.custom_oauth_token = mint_sp_token(host, cid, secret)
            config.config_source += "+eval_sp_token"
    if config.mcp_mode == "http" and not config.custom_oauth_token:
        raise ValueError("mcp_mode=http requires CUSTOM_MCP_OAUTH_TOKEN or evaluation service principal "
                         "credentials (EVAL_SP_CLIENT_ID/SECRET or eval_sp_secret_scope); Databricks Apps "
                         "accept OAuth tokens only")

    # The server code under test (src/) reads its settings from the environment.
    if host and not os.getenv("DATABRICKS_HOST"):
        os.environ["DATABRICKS_HOST"] = f"https://{host}"
    os.environ["DATABRICKS_WAREHOUSE_ID"] = config.sql_warehouse_id
    os.environ["CARD_TRANSACTION_TABLE"] = config.clean_table
    os.environ["LLM_ENDPOINT"] = config.llm_endpoint
    return config
