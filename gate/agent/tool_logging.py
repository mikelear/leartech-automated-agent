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


# Bash tool results carry ``Exit code N`` on the very first line when the command
# failed (observed shape: ``Exit code 2\nls: cannot access '/x': No such file…``).
# Successful commands do NOT prefix ``Exit code 0`` — they just return raw output —
# so the pattern itself gates against firing on a green run.
_BASH_EXIT_RE = re.compile(r'\AExit code (\d+)\s*(?:\n|\Z)')

# Bound the promoted error line well below _MAX so a promoted field cannot itself
# grow to 2000 chars and just move the same problem. 240 fits typical shell/CLI
# diagnostics ("ls: cannot access '/x': No such file or directory",
# "fatal: not a git repository", "npm ERR! code E404") with headroom.
_BASH_ERROR_MAX = 240


def _bash_failure_fields(text: str) -> dict[str, object]:
    """Lift a failed Bash result's exit status and identifying error out of the payload.

    Mirrors :func:`_verdict_fields` deliberately — same helper style, same
    snake_case field names spliced into the emit call, promotion strictly
    BEFORE the clip. The reason we can't just read them out of ``detail`` after
    the fact is the same as the verdict-fields case: a long failing command
    (a chatty ``pytest``, a verbose ``kaniko`` build, a firehose ``curl -v``)
    fills the 2000-char window with partial stdout, and the actual diagnostic
    line is the FIRST casualty of clipping.

    Bash-specific format: the CLI prefixes failing runs with ``Exit code N`` on
    line 1 followed by the interleaved stdout/stderr. Successful commands emit
    raw output with no such prefix, so the regex gate self-guards against
    firing on green runs (no ``is_error`` parameter needed; matches the
    format-only guard in ``_verdict_fields``).

    Choice of message line — LAST non-blank line, not first:
    - Shells and CLI tools emit their conclusive diagnostic AFTER any partial
      stdout ("ls: cannot access ...", "fatal: ...", "error: ...",
      "FAILED tests/foo.py::test_bar - AssertionError: ..."). Taking the last
      non-blank line captures that conclusive line for the common case.
    - The clip cuts the END of the output, so pre-promoting the last line is
      what makes it survive at all — the exact lesson ``_verdict_fields``
      encoded. Taking the first line would have been safe-from-clipping too,
      but would surface e.g. an env-dump preamble instead of the actual fail.
    - For single-diagnostic failures (``ls /missing``) the "first line after
      Exit code" and "last non-blank line" coincide, so the choice only
      matters for the multi-line case, where LAST is the right one.

    Field names (wire contract — the recorder keys on these; keep stable):
    - ``exit_code`` (int) — the numeric shell exit status
    - ``error``     (str) — the bounded, redacted, identifying diagnostic line

    Bounded to :data:`_BASH_ERROR_MAX` chars so this promotion doesn't just
    relocate the 2000-char clipping problem. Never raises — a logging helper
    must not break a run.
    """
    m = _BASH_EXIT_RE.match(text)
    if not m:
        return {}
    try:
        exit_code = int(m.group(1))
    except (ValueError, TypeError):
        return {}
    remainder = text[m.end() :]
    error_line = ''
    for line in reversed(remainder.splitlines()):
        stripped_line = line.strip()
        if stripped_line:
            error_line = stripped_line
            break
    error_line = redact(error_line)
    if len(error_line) > _BASH_ERROR_MAX:
        error_line = error_line[:_BASH_ERROR_MAX] + f'…[+{len(error_line) - _BASH_ERROR_MAX} chars]'
    return {'exit_code': exit_code, 'error': error_line}


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
    # Promote BEFORE clipping — the failure diagnostic lives at the end of the
    # payload for the same reason `_verdict_fields` promotes `status`: the
    # 2000-char clip cuts the tail, and the tail is what identifies the failure.
    bash_fields = _bash_failure_fields(text) if is_error else {}
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
        **bash_fields,
    )
