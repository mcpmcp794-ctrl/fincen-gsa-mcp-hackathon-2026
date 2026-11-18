"""MCP server entry point (kept thin, per the GSA MCP template).

It only creates the FastMCP instance, registers tools / prompts / resources /
routes from the submodules, and picks the transport:
  * Databricks Apps (DATABRICKS_APP_PORT) or PORT -> streamable HTTP at /mcp
  * MCP_TRANSPORT=streamable-http                 -> HTTP on MCP_HOST:MCP_PORT
  * otherwise                                     -> stdio (local clients)
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .config import get_settings
from .prompts import register_prompts
from .resources import register_resources
from .routes import register_routes
from .tools import register_tools

SERVER_NAME = "Fraudulent Transaction MCP"
INSTRUCTIONS = (
    "Read-only analysis of synthetic TabFormer credit-card transactions on Databricks. "
    "Prefer the specific tools; use ask_transactions for anything else. Fraud labels "
    "are not available for analysis; review cues are not fraud determinations. "
    "Work in a Plan -> Act -> Verify loop: plan the tool calls, execute them, then "
    "re-check key figures with a different tool before answering. The pav_plan, "
    "pav_act and pav_verify prompts provide the instructions for each stage.")


def create_server(backend=None, settings=None):
    """Build the FastMCP server. `backend` must expose .transactions and .query(sql, params)."""
    settings = settings or get_settings()
    if backend is None:
        from .backend import DatabricksBackend
        backend = DatabricksBackend()
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    register_tools(mcp, backend, settings)
    _run_sync_tools_in_threads(mcp)
    register_prompts(mcp)
    register_resources(mcp)
    register_routes(mcp, settings)
    return mcp


def _run_sync_tools_in_threads(mcp):
    """Run the (sync) tool functions in worker threads instead of on the event loop.

    FastMCP calls a sync tool directly on the event loop, so one slow SQL query
    blocked every other request: parallel tool calls from the client would still
    run one after another. Wrapping each sync tool lets concurrent calls overlap
    (the backend pool gives each running query its own connection). Tool names,
    parameters and schemas are unchanged (they come from the original function).
    """
    import functools

    import anyio

    for tool in mcp._tool_manager.list_tools():
        if tool.is_async:
            continue

        async def run_in_thread(*args, _fn=tool.fn, **kwargs):
            return await anyio.to_thread.run_sync(functools.partial(_fn, *args, **kwargs))

        tool.fn = run_in_thread
        tool.is_async = True


def _configure_http(mcp, host, port):
    mcp.settings.host = host
    mcp.settings.port = port
    # Stateless HTTP works best behind the Databricks Apps reverse proxy.
    mcp.settings.stateless_http = True
    # FastMCP's localhost-only Host-header check would reject proxied requests
    # (421 Invalid Host header). The Apps proxy enforces OAuth authentication.
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)
    except ImportError:
        pass


def main() -> None:
    settings = get_settings()
    mcp = create_server(settings=settings)
    platform_port = os.getenv("DATABRICKS_APP_PORT") or os.getenv("PORT")
    if platform_port:
        _configure_http(mcp, "0.0.0.0", int(platform_port))
        mcp.run(transport="streamable-http")
    elif settings.mcp_transport == "streamable-http":
        _configure_http(mcp, settings.mcp_host, settings.mcp_port)
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
