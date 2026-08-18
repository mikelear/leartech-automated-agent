"""Provider seam for one-shot LLM completions — the single ``anthropic`` import site,
so a provider switch is a change here rather than across callers. Used by ba_agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anthropic.types import Message


def complete(
    *,
    model: str,
    max_tokens: int,
    messages: list[dict[str, object]],
    system: str | None = None,
    tools: list[dict[str, object]] | None = None,
    tool_choice: dict[str, object] | None = None,
) -> Message:
    """One-shot LLM completion behind the provider seam.

    ``messages``/``tools``/``tool_choice`` are the Anthropic Messages-API shapes
    (heterogeneous literal dicts — incl. vision image blocks); the SDK validates
    them structurally at runtime.
    """
    from anthropic import Anthropic

    client = Anthropic()
    kwargs: dict[str, object] = {'model': model, 'max_tokens': max_tokens, 'messages': messages}
    if system is not None:
        kwargs['system'] = system
    if tools is not None:
        kwargs['tools'] = tools
    if tool_choice is not None:
        kwargs['tool_choice'] = tool_choice
    return client.messages.create(**kwargs)  # type: ignore[call-overload, no-any-return]
