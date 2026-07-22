# AI gateway & provider portability — READ BEFORE MAKING ARCHITECTURE DECISIONS

**Heads-up for any session working on `leartech-automated-agent`:** this agent is being
moved to call Claude **through `leartech-ai-gateway`** (not the Anthropic API directly),
and **we intend to run it on non-Anthropic models in the future.** A provider switch
**will happen.** Do not bake in architecture decisions that assume Anthropic — understand
the gateway first.

This is not a "someday maybe." Treat provider-portability as a **standing design
constraint**, the same way you'd treat "must run in K8s."

> **The executable plan** (phases, capability matrix, what's proven-live, the
> Phase-1 runbook) lives in [`docs/AI-GATEWAY-MIGRATION-PLAN.md`](docs/AI-GATEWAY-MIGRATION-PLAN.md).
> Key point it makes: the gateway repoint fixes the **LLM seam** only — it does
> **not** fix the `open_pr` tool/MCP seam (that's Phase 2).

## What's changing (and why it affects your design)
- **Today:** the agent uses the **Claude Agent SDK** (`claude_agent_sdk.query`,
  `create_sdk_mcp_server`) + raw **`anthropic` SDK** helpers, calling Anthropic directly.
- **In flight:** repoint to the gateway via `ANTHROPIC_BASE_URL` (metered + governed;
  no feature loss). Still an Anthropic model.
- **Future:** run the agent on a **non-Anthropic** model (DeepSeek, self-hosted qwen,
  Kimi, or anything behind LiteLLM) via the gateway's OpenAI-compatible path. This is a
  **runtime swap**, not a URL change — the Claude Agent SDK *is* the Anthropic-coupled
  runtime.

## The rule: isolate the LLM/agent-runtime behind ONE seam
So a future switch is a config flag, not a rewrite:
- **No scattered `claude_agent_sdk` / `anthropic` imports** in business logic. One module
  owns the runtime; everything else calls a neutral interface. (Today these are spread
  across `gate/agent/initiative.py`, `gate/agent/main.py`, `gate/tools/spec_suggester.py`,
  `gate/tools/video_review.py`, `gate/agent/self_retrospect.py` — consolidating is the
  direction of travel.)
- **Tools via standard, client-side MCP** — not Anthropic's in-process
  `create_sdk_mcp_server` or its server-side MCP connector. MCP is provider-neutral; the
  same tools then work on any backend.
- **Anthropic-specific features are opt-in capabilities, not assumptions.** Code that
  assumes extended thinking / `pause_turn` / `cache_control` always exist WILL break on a
  switch. Feature-detect or degrade.
- **Always go through the gateway**, never a provider API directly — that's what makes
  model choice a config decision.
- **Don't hardcode model ids** in logic — use the gateway's logical model names.

## What a non-Anthropic switch costs (so you know what NOT to depend on)
| Anthropic feature | On a switch | Portable? |
|---|---|---|
| Server-side MCP connector | Lost | Use client-side MCP instead |
| In-process SDK MCP servers | Claude-SDK-specific | Re-express as standard MCP |
| Extended / adaptive thinking | Lost / differs | No |
| `pause_turn` continuation | Lost | Handle in your own loop |
| `cache_control` prompt caching | Lost / differs | No (provider-specific) |
| tool_use content blocks | Portable | Maps to OpenAI `tool_calls` |
| `messages.parse` structured output | Portable-ish | `response_format` / `json_schema` |
| `max_turns` / `permission_mode` | Claude-SDK construct | Your own loop |

**Tools + structured output are portable; the agentic RUNTIME features are not.**

## Files to read (in the ai-bus repo) BEFORE deciding architecture
Full design + rationale:
- `~/leartech/ai-bus/PLAN-agent-portability.md` — the capability matrix + the
  two-backend runtime-seam design (**start here**).
- `~/leartech/ai-bus/PLAN-agent-gateway-migration.md` — the migration onto the gateway.
- `~/leartech/ai-bus/PLAN-gateway-parity.md` — provider registry, JSON mode, prompt
  caching, LiteLLM-as-one-provider-row.

The gateway itself (`~/leartech/ai-bus/skeleton/leartech-ai-gateway/`):
- `internal/passthrough/anthropic.go` — the Anthropic `/v1/messages` passthrough
  (Anthropic-shaped path; what's forwarded verbatim).
- `internal/adapter/openai.go` — the generic OpenAI-compat adapter + tools passthrough
  (**the target for the openai-compat backend**).
- `internal/api/handlers.go`, `internal/api/types.go` — request/response shapes, tools.
- `internal/router/router.go` — logical→provider model resolution.
- `~/leartech/ai-bus/ARCHITECTURE.md`, `~/leartech/ai-bus/INTERFACES.md` — gateway shape.

If a change would make the agent *harder* to run on a non-Anthropic model, stop and
reconsider — or at least record the coupling you're adding and why.
