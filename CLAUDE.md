# leartech-automated-agent

## IMPORTANT — provider portability is a standing design constraint
This agent currently uses the **Claude Agent SDK** + `anthropic` SDK, but it is being
moved to call Claude **through `leartech-ai-gateway`**, and **it will run on
non-Anthropic models in the future.** A provider switch WILL happen.

**Before making any architecture decision that touches the LLM/agent runtime, tools, or
model calls, read [`AI-GATEWAY-AND-PORTABILITY.md`](./AI-GATEWAY-AND-PORTABILITY.md).**

Short version:
- Isolate the LLM/agent-runtime behind **one seam** — no scattered `claude_agent_sdk` /
  `anthropic` imports in business logic.
- Tools via **standard client-side MCP**, not Anthropic in-process/server-side MCP.
- Anthropic-specific features (thinking, `pause_turn`, `cache_control`) are **opt-in
  capabilities, not assumptions**.
- Always go through the **gateway**; don't hardcode provider APIs or model ids.

If a change would make the agent harder to run on a non-Anthropic model, stop and
reconsider — or record the coupling and why.
