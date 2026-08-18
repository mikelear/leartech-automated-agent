"""Tests for DEFAULT_MODEL env-var configuration in gate.agent.main."""

from __future__ import annotations

import importlib
from typing import Any

import pytest


@pytest.mark.parametrize(
    'env_value,expected',
    [
        ('claude-haiku-4-5', 'claude-haiku-4-5'),
        ('claude-sonnet-4-6', 'claude-sonnet-4-6'),
        ('claude-opus-4-7', 'claude-opus-4-7'),
    ],
)
def test_default_model_uses_env_var_when_set(monkeypatch: Any, env_value: str, expected: str) -> None:
    """When LEARTECH_AGENT_MODEL is set, DEFAULT_MODEL uses that value."""
    monkeypatch.setenv('LEARTECH_AGENT_MODEL', env_value)

    import gate.agent.main

    importlib.reload(gate.agent.main)
    assert gate.agent.main.DEFAULT_MODEL == expected


def test_default_model_is_opus_when_env_var_unset(monkeypatch: Any) -> None:
    """When LEARTECH_AGENT_MODEL is unset, DEFAULT_MODEL defaults to claude-opus-4-7."""
    monkeypatch.delenv('LEARTECH_AGENT_MODEL', raising=False)

    import gate.agent.main

    importlib.reload(gate.agent.main)
    assert gate.agent.main.DEFAULT_MODEL == 'claude-opus-4-7'
