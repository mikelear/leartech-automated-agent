"""Provider seam for one-shot LLM completions (runtime-seam refactor, Phase B1).

The agent's one-shot LLM calls (spec_suggester, video_review, self_retrospect)
each imported ``anthropic`` and built their own client. This module is the SINGLE
seam they now call — the ONLY ``anthropic`` import site for one-shot completions —
so a future provider switch (ai-gateway / openai-compat, Phase D) is a change
here, not across the callers (see memory project_agent_provider_portability).

``complete()`` wraps the Anthropic Messages API. The client reads
``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_API_KEY`` from the env, so it already routes
through leartech-ai-gateway when the repoint is active — no caller change needed.

The return is the Anthropic ``Message`` (callers still read ``.content``); fully
neutralising the response shape is Phase D (openai-compat), where this seam grows
a second backend. For now it centralises the import, client, and config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # type-only import — no runtime anthropic coupling outside complete()
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
    # The SDK's create() is a heavily-overloaded, strictly-typed method; our
    # generic kwargs dict satisfies the TypedDict params at runtime but mypy can't
    # match an overload from a dict-splat (call-overload) and infers Any (no-any-
    # return). Justified suppression — the seam intentionally wraps the typed API.
    return client.messages.create(**kwargs)  # type: ignore[call-overload, no-any-return]
