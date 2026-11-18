"""mcp_mode=http token resolution in evaluation config (no network: minting is stubbed)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation" / "src"))

from card_transaction_eval import config as eval_config  # noqa: E402


@pytest.fixture
def http_env(monkeypatch, tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("mcp_mode: http\neval_sp_secret_scope: ''\n")
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc-test.cloud.databricks.com")
    for k in ("CUSTOM_MCP_OAUTH_TOKEN", "EVAL_SP_CLIENT_ID", "EVAL_SP_CLIENT_SECRET", "EVAL_EVAL_SP_SECRET_SCOPE"):
        monkeypatch.delenv(k, raising=False)
    minted = []
    monkeypatch.setattr(eval_config, "mint_sp_token", lambda host, cid, sec: minted.append((host, cid, sec)) or "SP-TOKEN")
    return str(cfg), minted


def test_explicit_token_wins(http_env, monkeypatch):
    path, minted = http_env
    monkeypatch.setenv("CUSTOM_MCP_OAUTH_TOKEN", "USER-TOKEN")
    monkeypatch.setenv("EVAL_SP_CLIENT_ID", "cid")
    monkeypatch.setenv("EVAL_SP_CLIENT_SECRET", "sec")
    c = eval_config.load_config(path)
    assert c.custom_oauth_token == "USER-TOKEN" and minted == []


def test_sp_env_credentials_mint_a_token(http_env, monkeypatch):
    path, minted = http_env
    monkeypatch.setenv("EVAL_SP_CLIENT_ID", "cid")
    monkeypatch.setenv("EVAL_SP_CLIENT_SECRET", "sec")
    c = eval_config.load_config(path)
    assert c.custom_oauth_token == "SP-TOKEN" and minted == [("dbc-test.cloud.databricks.com", "cid", "sec")]
    assert "eval_sp_token" in c.config_source
    pub = c.public_dict()
    assert "SP-TOKEN" not in str(pub) and pub["custom_oauth_token_present"] is True


def test_sp_secret_scope_is_read(http_env, monkeypatch):
    path, minted = http_env
    seen = []
    monkeypatch.setattr(eval_config, "_sp_credentials", lambda scope: seen.append(scope) or ("cid2", "sec2"))
    monkeypatch.setenv("EVAL_EVAL_SP_SECRET_SCOPE", "fraud-mcp-eval")
    c = eval_config.load_config(path)
    assert seen == ["fraud-mcp-eval"] and c.custom_oauth_token == "SP-TOKEN"


def test_http_without_any_credentials_fails_clearly(http_env):
    path, minted = http_env
    with pytest.raises(ValueError, match="eval_sp_secret_scope"):
        eval_config.load_config(path)


def test_inprocess_never_reads_sp_credentials(http_env, monkeypatch, tmp_path):
    _, minted = http_env
    cfg = tmp_path / "i.yaml"
    cfg.write_text("mcp_mode: inprocess\neval_sp_secret_scope: fraud-mcp-eval\n")
    monkeypatch.setattr(eval_config, "_sp_credentials", lambda scope: pytest.fail("read credentials"))
    c = eval_config.load_config(str(cfg))
    assert c.custom_oauth_token is None and minted == []
