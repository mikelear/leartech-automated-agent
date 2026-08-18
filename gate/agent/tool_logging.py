"""Structured logging of the agent's tool trajectory (command + result).

WHY: the SDK loop previously logged only the tool *name* (``→ Bash``) to stderr,
so operators were blind to WHAT the agent actually ran — e.g. how it authenticated
to a private ``gs://`` artifact was invisible until someone shelled into the (now
gone) pod and read the Claude CLI transcript. This surfaces the command and a
truncated result as structured ``tool_call`` / ``tool_result`` events (the stable
names ``obslog`` already reserves), so the trajectory is queryable in Loki:

    {namespace="jx-staging"} | json | event="tool_call"

REDACTION: Bash commands and their output routinely contain secrets — the agent
does ``head /tmp/gcp-credentials.json``, ``curl -H "Authorization: Bearer ..."``,
etc. We redact BEFORE emitting: exact values of known secret env vars, plus
pattern-based catches (PEM private-key blocks, GCP/GitHub/OpenAI tokens, JWTs) as
a backstop for shapes the env-value pass can't match (e.g. a PEM re-serialised
with real newlines vs the escaped ``\\n`` in the env JSON).

Deliberately NOT the full transcript (that is Claude-CLI-specific and couples us
to the provider) — just the provider-neutral command/result summary.
"""

from __future__ import annotations

import os
import re
from typing import Any

from gate import obslog

_SECRET_ENVS = (
    'GH_TOKEN',
    'GIT_TOKEN',
    'ANTHROPIC_API_KEY',
    'AI_GATEWAY_API_KEY',
    'LEARTECH_AUTH_CLIENT_SECRET',
    'GOOGLE_APPLICATION_CREDENTIALS_JSON',
)

_REDACTED = '***REDACTED***'

_SECRET_PATTERNS = (
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.DOTALL),
    re.compile(r'ya29\.[A-Za-z0-9._\-]+'),
    re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}'),
    re.compile(r'sk-[A-Za-z0-9_\-]{20,}'),
    re.compile(r'eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+'),
)

_MAX = 2000


def redact(text: str) -> str:
    """Return ``text`` with known-secret env values and secret-shaped substrings
    replaced by a placeholder. Safe on non-str (coerced) and empty input."""
    if not text:
        return text
    out = str(text)
    for env in _SECRET_ENVS:
        val = os.environ.get(env, '')
        if val and len(val) >= 8:
            out = out.replace(val, _REDACTED)
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out


def _clip(text: str) -> str:
    text = redact(text)
    if len(text) > _MAX:
        return text[:_MAX] + f'…[+{len(text) - _MAX} chars]'
    return text


def _summarise_input(tool: str, tool_input: Any) -> str:
    """Compact one-line-ish view of a tool's input. Bash → the command; other
    tools → their most salient field (path/pattern/url) falling back to a redacted
    JSON-ish repr."""
    if not isinstance(tool_input, dict):
        return _clip(str(tool_input))
    for key in ('command', 'file_path', 'path', 'pattern', 'url', 'query'):
        if key in tool_input and tool_input[key]:
            return _clip(str(tool_input[key]))
    return _clip(str(tool_input))


def log_tool_call(tool: str, tool_input: Any) -> None:
    """Emit a structured ``tool_call`` event carrying the (redacted, truncated)
    command/input so the agent's trajectory is visible in Loki, not just the pod
    transcript."""
    detail = _summarise_input(tool, tool_input)
    obslog.info('tool_call', f'{tool}: {detail}', logger='agent.initiative', tool=tool, detail=detail)


def log_tool_result(tool: str | None, content: Any, *, is_error: bool = False) -> None:
    """Emit a structured ``tool_result`` event with the (redacted, truncated)
    output. ``content`` is the SDK ``ToolResultBlock.content`` — a str or a list
    of ``{type,text}`` blocks."""
    if isinstance(content, list):
        text = '\n'.join(str(part.get('text', '')) for part in content if isinstance(part, dict))
    else:
        text = '' if content is None else str(content)
    detail = _clip(text)
    level = 'WARN' if is_error else 'INFO'
    obslog.emit(
        level,
        'tool_result',
        f'{tool or "tool"} {"error" if is_error else "ok"}: {detail}',
        logger='agent.initiative',
        tool=tool,
        ok=not is_error,
        detail=detail,
    )
