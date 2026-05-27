"""Smoke tests for Dockerfile.runtime — verifies file structure
invariants the variant rebase + Job-spawn primitive will rely on."""

from __future__ import annotations

import re
from pathlib import Path

RUNTIME_DOCKERFILE = Path(__file__).parents[1] / 'Dockerfile.runtime'


def test_runtime_dockerfile_exists() -> None:
    assert RUNTIME_DOCKERFILE.exists(), 'Dockerfile.runtime must exist at repo root'


def test_runtime_dockerfile_has_no_entrypoint() -> None:
    """Runtime image is a base for variants — must not set ENTRYPOINT."""
    contents = RUNTIME_DOCKERFILE.read_text()
    lines = [line.strip() for line in contents.splitlines() if not line.strip().startswith('#') and line.strip()]
    entrypoints = [line for line in lines if re.match(r'^ENTRYPOINT\b', line, re.I)]
    assert not entrypoints, f'runtime image must not set ENTRYPOINT, got: {entrypoints}'


def test_runtime_dockerfile_copies_gate_app_initiatives() -> None:
    contents = RUNTIME_DOCKERFILE.read_text()
    for required_dir in ('gate/', 'app/', 'initiatives/'):
        assert required_dir in contents, f'Dockerfile.runtime must COPY {required_dir}'


def test_runtime_dockerfile_from_arg_default_is_agent_base() -> None:
    """The ARG BASE_IMAGE default must point at leartech-agent-base
    (not python:3.13-slim) — release builds must FROM the proper base."""
    contents = RUNTIME_DOCKERFILE.read_text()
    assert 'ARG BASE_IMAGE=ghcr.io/mikelear/leartech-agent-base' in contents
