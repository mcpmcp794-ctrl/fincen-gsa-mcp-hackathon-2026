"""Server configuration loaded from environment variables or a .env file.

Every field can be overridden by an environment variable of the same name
(upper- or lower-case). Copy `.env.example` to `.env` for local development.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """MCP server settings. Override any field via environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Transport: "stdio" locally; app.py switches to streamable HTTP automatically
    # when Databricks Apps injects DATABRICKS_APP_PORT (or a host injects PORT).
    mcp_transport: str = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    # Single source of truth for these defaults: backend.py, nl_sql.py and routes.py read
    # them from here (env vars CARD_TRANSACTION_TABLE / LLM_ENDPOINT override).
    # Label-free analysis table read by every analysis tool.
    card_transaction_table: str = "workspace.default.card_transaction_mcp_clean"
    # Model used by ask_transactions (UC model service via AI Gateway, or a serving endpoint).
    llm_endpoint: str = "models.ai.gpt-5-6-luna"

    # Genie space that answers fraud-label questions (counts, rates, labelled transactions).
    # The agent prompts refer users there because this server has no fraud labels.
    fraud_genie_space_id: str = "01f1dc44f22413fe82320128338353b5"
    fraud_genie_space_title: str = "Card Transaction Insights and Fraud Detection Agent"

    # Evaluation only. When true, the evaluate_review_signals tool is registered.
    # It joins the restricted label table and returns aggregate metrics only.
    # The app service principal also needs SELECT on evaluation_label_table.
    enable_evaluation_tools: bool = False
    evaluation_label_table: str = "workspace.default.card_transaction_evaluation_labels"


def get_settings() -> Settings:
    """Fresh settings from the current environment."""
    return Settings()


# [CLEANUP 2026-11-17] Unused module-level instance: every caller uses get_settings().
# settings = get_settings()
