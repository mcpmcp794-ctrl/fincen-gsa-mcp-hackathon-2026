"""Resources submodule: register_resources(mcp) aggregator."""

from __future__ import annotations

from . import schema


def register_resources(mcp) -> None:
    schema.register(mcp)


__all__ = ["register_resources"]
