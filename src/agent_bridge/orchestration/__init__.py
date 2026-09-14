"""Standalone orchestration surface for stage routing and local work.

This package is deliberately separate from :mod:`agent_bridge.mcp_server`.
The consultation server therefore retains its existing tool set and security
contract.
"""

from .config import OrchestrationConfig, load

__all__ = ["OrchestrationConfig", "load"]
