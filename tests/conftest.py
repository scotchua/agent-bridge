"""Pytest entry: isolate operator settings before any test module imports."""

import sys
from pathlib import Path

import operator_isolation  # noqa: F401

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_bridge import claude_token


@pytest.fixture(autouse=True)
def _no_real_claude_keychain(monkeypatch):
    """No test may invoke macOS ``security`` or expose its value to a child."""
    monkeypatch.setattr(claude_token, "_reader", lambda: None)
