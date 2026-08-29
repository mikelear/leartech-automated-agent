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

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
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


_VERDICT_KEYS = ('status', 'verdict', 'merged', 'remaining_seconds')


def _verdict_fields(text: str) -> dict[str, object]:
    """Lift a tool result's verdict-bearing scalars out of the payload.

    ``detail`` is clipped at _MAX, and the fields that decide a run sit AFTER the
    bulky ones: wait_for_terminal returns ``checks`` (9 rows, ~2k chars) before
    ``status``, so every one of a run's wait results logged an identical 2013-char
    prefix with the verdict cut off. The outcome had to be read from the MCP
    server's own logs instead, which breaks "no outcome is decided from a value
    whose provenance isn't in Loki".

    Generic on purpose: any tool returning these keys gets them promoted, with no
    per-tool coupling. Never raises — a logging helper must not break a run.
    """
    stripped = text.strip()
    if not stripped.startswith('{'):
        return {}
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: parsed[k] for k in _VERDICT_KEYS if isinstance(parsed.get(k), str | int | float | bool)}


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


ADVERTISED_TOOLS_EVENT = 'agent_advertised_tools'


def log_advertised_tools(
    mcp_servers: Mapping[str, object] | Iterable[str] | str | Path | None,
    allowed_tools: Iterable[str],
    *,
    logger: str = 'agent.initiative',
) -> None:
    """Emit ONE structured record naming every MCP server + every allowed tool the
    agent has been wired with, so a later reader can compute which advertised
    tools were never called (advertised − called = never-used).

    Emitted at INFO — deliberately NOT DEBUG, because the controller runs at INFO
    and a DEBUG record is the same as no record (leartech-orchestrator-controller
    has three ``internal/controller`` comments recording exactly that mistake
    costing weeks). The `agent_advertised_tools` event is the wire contract; the
    fields ``mcp_servers`` + ``allowed_tools`` (sorted list[str]) are keyed on by
    the recorder that computes never-called sets, so their names + shape are
    stable across releases.

    Loki query — advertised set for one run id:
        {namespace=~".+"} | json | event="agent_advertised_tools" | run_id="<id>"

    Called EXACTLY ONCE per run (at run start). Duplicating the emission would
    inflate the never-called cardinality on the recorder side; the caller is
    expected to fire this next to its ``run_start`` line, not per turn / tool.

    ``mcp_servers`` accepts the shapes the Agent-SDK's own union defines:
    a mapping keyed by agent-facing server name (e.g. ``leartech-pr-context``),
    a plain iterable of server-name strings, or a config-file path string
    (treated as opaque — no keys to enumerate). ``None`` is accepted as a
    convenience for callers that pass ``options.mcp_servers or None``. The
    record always carries the run_id via obslog's ambient ``_context()`` — no
    extra parameter needed.
    """
    if isinstance(mcp_servers, Mapping):
        server_names = sorted(str(s) for s in mcp_servers.keys())
    elif isinstance(mcp_servers, str | Path) or mcp_servers is None:
        # SDK allows a config-file path — treat as opaque (no keys to enumerate).
        server_names = []
    else:
        try:
            server_names = sorted(str(s) for s in mcp_servers)
        except TypeError:
            server_names = []
    tool_names = sorted(str(t) for t in allowed_tools)
    obslog.info(
        ADVERTISED_TOOLS_EVENT,
        f'agent advertised {len(server_names)} MCP server(s) and {len(tool_names)} allowed tool(s)',
        logger=logger,
        mcp_servers=server_names,
        allowed_tools=tool_names,
        mcp_server_count=len(server_names),
        allowed_tool_count=len(tool_names),
    )


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
        **_verdict_fields(text),
    )
