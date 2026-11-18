"""Root entry point for local development.

    PYTHONPATH=src python main.py        # stdio (Claude Desktop / Code, MCP Inspector)
    MCP_TRANSPORT=streamable-http PYTHONPATH=src python main.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fraud_transaction_mcp.app import main  # noqa: E402

if __name__ == "__main__":
    main()
