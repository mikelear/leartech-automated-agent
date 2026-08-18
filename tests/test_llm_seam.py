"""Tests for gate.llm — the one-shot LLM provider seam (Phase B1).

Local CoS: complete() forwards to the Anthropic Messages API and returns its
response, AND the ``anthropic`` package is imported ONLY in the seam (so a
provider switch is a change in one place). The loop's claude_agent_sdk usage is
separate (Phase B2) and out of scope here.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

from gate import llm

_REPO = pathlib.Path(__file__).resolve().parent.parent


def test_complete_forwards_to_anthropic_and_returns_response() -> None:
    fake_client = MagicMock()
    fake_client.messages.create.return_value = 'RESPONSE'
    with patch('anthropic.Anthropic', return_value=fake_client):
        out = llm.complete(
            model='claude-opus-4-8',
            max_tokens=1024,
            system='sys',
            tools=[{'name': 't'}],
            tool_choice={'type': 'tool', 'name': 't'},
            messages=[{'role': 'user', 'content': 'hi'}],
        )
    assert out == 'RESPONSE'
    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs['model'] == 'claude-opus-4-8'
    assert kwargs['max_tokens'] == 1024
    assert kwargs['system'] == 'sys'
    assert kwargs['messages'] == [{'role': 'user', 'content': 'hi'}]


def test_complete_omits_optional_params_when_none() -> None:
    fake_client = MagicMock()
    with patch('anthropic.Anthropic', return_value=fake_client):
        llm.complete(model='m', max_tokens=8, messages=[{'role': 'user', 'content': 'x'}])
    kwargs = fake_client.messages.create.call_args.kwargs
    assert 'system' not in kwargs
    assert 'tools' not in kwargs
    assert 'tool_choice' not in kwargs


def test_anthropic_imported_only_in_the_seam() -> None:
    """The one-shot anthropic import lives ONLY in gate/llm.py — the whole point
    of the seam (portability). Guards against a caller re-importing anthropic."""
    offenders = []
    for path in (_REPO / 'gate').rglob('*.py'):
        if path.name == 'llm.py':
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(('import anthropic', 'from anthropic import', 'from anthropic.')):
                offenders.append(f'{path.relative_to(_REPO)}:{i}: {stripped}')
    assert not offenders, 'anthropic imported outside gate/llm.py:\n' + '\n'.join(offenders)
