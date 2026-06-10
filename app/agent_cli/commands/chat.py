"""``leartech-agent chat`` — interactive REPL against Orchestrator's ``/chat``.

The orchestrator's ``POST /chat`` endpoint maintains conversation state
across turns via a ``conversation_id``. This REPL keeps that id in
memory for the lifetime of the session, so successive operator messages
share context (verified ~$0.015/turn in staging — see initiative goal).

Slash commands inside the REPL:

* ``:exit`` / ``:q`` / ``exit`` / ``quit`` — leave the REPL
* ``:save [path]`` — write the transcript to disk (default: ``./chat-<conv_id>.md``)
* ``:new`` — start a fresh conversation (drops the current conversation_id)
* ``:cost`` — show running session cost (sum of per-turn ``cost_usd``)
* ``:help`` — list these commands

Design notes:

* We deliberately *don't* introduce an httpx-async client here — the
  REPL is synchronous user-prompt → HTTP roundtrip → render, and the
  extra complexity of an event loop adds no value when only one
  request is in flight at a time.
* ``prompt_toolkit`` is the de-facto Python REPL toolkit (history,
  multiline edit, paste). It's imported lazily so other CLI commands
  (which don't need it) don't pay the ~30ms import cost.
* Errors from the Orchestrator (5xx, connection refused, timeout) are
  rendered to the console but do NOT exit the REPL — operators stay
  in-session and can retry after a redeploy.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import httpx
from rich.markdown import Markdown
from rich.panel import Panel

from app.agent_cli.config import load_config, resolve_url
from app.agent_cli.render import console

# Connect+read timeout for /chat. Orchestrator turns occasionally take
# >30s when Claude is under contention; 120s keeps the REPL responsive
# in the common case while still surfacing genuinely dead endpoints.
_CHAT_TIMEOUT_SECONDS = 120.0

# Network/transport errors that should not kill the REPL. ``httpx``
# raises subclasses of ``httpx.HTTPError``; we still catch ``OSError``
# defensively in case a non-httpx layer (e.g. DNS) leaks one.
_TRANSPORT_ERRORS = (httpx.HTTPError, OSError)

# REPL prompt — kept simple so terminals without ANSI render cleanly.
_PROMPT = 'you> '


@dataclass
class _Transcript:
    """In-memory log of turns for ``:save`` and ``:cost``."""

    conversation_id: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: _dt.datetime.now(_dt.UTC).isoformat().replace('+00:00', 'Z'))

    def total_cost(self) -> float:
        return sum(float(t.get('cost_usd', 0.0) or 0.0) for t in self.turns)

    def to_markdown(self) -> str:
        """Render the transcript as a portable markdown document."""
        lines = [
            '# leartech-agent chat transcript',
            '',
            f'- Started: {self.started_at}',
            f'- Conversation ID: {self.conversation_id or "(none)"}',
            f'- Turns: {len(self.turns)}',
            f'- Total cost: ${self.total_cost():.4f}',
            '',
        ]
        for i, turn in enumerate(self.turns, 1):
            lines.extend(
                [
                    f'## Turn {i}',
                    '',
                    f'**You:** {turn.get("user", "")}',
                    '',
                    f'**Assistant:** {turn.get("assistant", "")}',
                    '',
                    f'_cost: ${float(turn.get("cost_usd", 0.0) or 0.0):.4f}_',
                    '',
                ]
            )
        return '\n'.join(lines)


def _post_chat(
    client: httpx.Client,
    *,
    message: str,
    conversation_id: str | None,
) -> dict[str, Any]:
    """Single ``POST /chat`` call. Returns the parsed JSON body."""
    response = client.post(
        '/chat',
        json={'message': message, 'conversation_id': conversation_id},
        timeout=_CHAT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f'POST /chat: expected JSON object, got {type(body).__name__}')
    return body


def _render_reply(reply: str) -> None:
    """Render the assistant's reply as a markdown panel (graceful on plain text)."""
    console.print(Panel(Markdown(reply), title='assistant', border_style='green'))


def _handle_slash(
    command: str,
    transcript: _Transcript,
) -> tuple[bool, _Transcript]:
    """Dispatch a slash command. Returns ``(should_continue, transcript)``.

    Slash commands never raise — even bad input prints a hint and lets
    the operator keep going.
    """
    parts = command.strip().split(maxsplit=1)
    head = parts[0]
    arg = parts[1] if len(parts) > 1 else ''
    if head in {':exit', ':q', 'exit', 'quit'}:
        console.print('[dim]bye.[/dim]')
        return False, transcript
    if head == ':help':
        console.print(
            Panel(
                ':exit / :q          leave the REPL\n'
                ':save [path]        write transcript to file (default ./chat-<id>.md)\n'
                ':new                start a fresh conversation (drops conv_id)\n'
                ':cost               show running session cost\n'
                ':help               this panel',
                title='REPL commands',
                border_style='cyan',
            )
        )
        return True, transcript
    if head == ':cost':
        console.print(
            f'[bold]Conversation:[/bold] {transcript.conversation_id or "(none yet)"}\n'
            f'[bold]Turns:[/bold] {len(transcript.turns)}\n'
            f'[bold]Total cost:[/bold] ${transcript.total_cost():.4f}'
        )
        return True, transcript
    if head == ':new':
        new = _Transcript()
        console.print('[yellow]Started a fresh conversation.[/yellow]')
        return True, new
    if head == ':save':
        target = Path(arg) if arg else Path.cwd() / f'chat-{transcript.conversation_id or "draft"}.md'
        target.write_text(transcript.to_markdown(), encoding='utf-8')
        console.print(f'[green]✓[/green] Transcript saved to {target}')
        return True, transcript
    console.print(f'[red]Unknown command {head!r}.[/red] Try `:help`.')
    return True, transcript


def _read_user_input() -> str | None:
    """Read one line from the operator.

    Lazy-imports prompt_toolkit so other CLI commands don't pay the
    import cost. Returns ``None`` on EOF / Ctrl-D so the REPL can exit
    cleanly. Empty input returns the empty string and the caller
    decides whether to skip.
    """
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
    except ImportError:
        # Graceful fallback for stripped-down install footprints (e.g. a
        # container that explicitly omitted prompt_toolkit). The REPL
        # still works, just without arrow-key history.
        try:
            return input(_PROMPT)
        except EOFError:
            return None

    session: PromptSession[str] = PromptSession(history=InMemoryHistory())
    try:
        result: str = session.prompt(_PROMPT)
        return result
    except (EOFError, KeyboardInterrupt):
        return None


@click.command()
@click.option('--continue', 'continue_id', help='Resume a prior conversation by id.')
@click.option(
    '--cluster',
    default=None,
    help='Cluster key (e.g. gcp-staging, az-staging). Defaults to config default_cluster.',
)
@click.option(
    '--orch-url',
    default=None,
    help='Explicit orchestrator base URL. Overrides --cluster + env + config file.',
)
def chat(continue_id: str | None, cluster: str | None, orch_url: str | None) -> None:
    """Open an interactive chat REPL against the orchestrator's POST /chat.

    The conversation_id is maintained across turns so the orchestrator's
    /chat endpoint preserves context. Use ``:new`` mid-session to drop
    it; ``:exit`` to quit.
    """
    cfg = load_config()
    try:
        base_url = resolve_url('orch_url', flag_value=orch_url, cluster=cluster, config=cfg)
    except ValueError as exc:
        console.print(f'[red]Config error:[/red] {exc}')
        raise SystemExit(1) from exc

    transcript = _Transcript(conversation_id=continue_id)
    cluster_name = cluster or cfg.default_cluster
    console.print(
        Panel(
            f'Orchestrator: {base_url}\n'
            f'Cluster: {cluster_name}\n'
            f'Conversation: {continue_id or "(new — id assigned after first turn)"}\n\n'
            f'Slash commands: [bold]:exit :save :new :cost :help[/bold]',
            title='leartech-agent chat',
            border_style='cyan',
        )
    )

    with httpx.Client(base_url=base_url, timeout=_CHAT_TIMEOUT_SECONDS) as client:
        while True:
            msg = _read_user_input()
            if msg is None:
                # Ctrl-D / EOF — same effect as `:exit`.
                console.print('[dim]bye.[/dim]')
                return
            text = msg.strip()
            if not text:
                continue
            if text.startswith(':') or text in {'exit', 'quit'}:
                cont, transcript = _handle_slash(text, transcript)
                if not cont:
                    return
                continue
            try:
                body = _post_chat(
                    client,
                    message=text,
                    conversation_id=transcript.conversation_id,
                )
            except _TRANSPORT_ERRORS as exc:
                # Stay in-REPL; surface a one-liner. Don't dump tracebacks
                # — operators see this regularly during cluster rollouts
                # and the noise hurts more than it helps.
                console.print(f'[red]/chat unreachable:[/red] {exc.__class__.__name__}: {exc}')
                continue
            except (json.JSONDecodeError, ValueError) as exc:
                console.print(f'[red]/chat returned malformed body:[/red] {exc}')
                continue

            transcript.conversation_id = body.get('conversation_id') or transcript.conversation_id
            turn = body.get('turn', {}) or {}
            reply = turn.get('content') or turn.get('text') or turn.get('message') or ''
            transcript.turns.append(
                {
                    'user': text,
                    'assistant': reply,
                    'cost_usd': turn.get('cost_usd', 0.0),
                }
            )
            _render_reply(reply if isinstance(reply, str) else json.dumps(reply, indent=2))
