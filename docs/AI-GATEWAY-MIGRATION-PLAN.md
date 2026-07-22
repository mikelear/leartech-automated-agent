# AI-gateway migration — the executable plan

Companion to [`AI-GATEWAY-AND-PORTABILITY.md`](../AI-GATEWAY-AND-PORTABILITY.md)
(the standing *constraint*). This doc is the *plan*: what we do, in what order,
what it costs, and what's already proven. Grounded in the ai-bus session work
(`~/leartech/ai-bus/PLAN-agent-gateway-migration.md`, `PLAN-agent-portability.md`,
`PLAN-gateway-parity.md`, `ARCHITECTURE.md`).

## The one thing to internalise: there are TWO independent seams

Moving to the gateway is about the **LLM seam**. It does **not**, by itself, fix
the **tool/MCP seam** (the `open_pr` auth bug). Don't conflate them.

| Seam | Mechanism | What the gateway repoint does |
|---|---|---|
| **LLM seam** | `/v1/messages` passthrough | Routes every LLM call through the gateway. Forwards the raw Anthropic body + `anthropic-beta`/`anthropic-version` verbatim → **zero feature loss** with Anthropic. |
| **Tool/MCP seam** | `open_pr` et al. over remote MCP | **Untouched.** Passthrough still runs `claude_agent_sdk` → the Claude Code CLI, which is the component that won't forward our static MCP `Authorization` header. Fixed in Phase 2, not Phase 1. |

## What's LIVE and proven (as of the ai-bus review, 2026-07-22)
- Gateway **v0.0.23** live on both clusters.
- **`/v1/messages` passthrough** live (v0.0.19) — forwards `anthropic-beta` + `anthropic-version`.
- **Virtual-key auth** live (v0.0.14): `sk-lt-…` keys → tenant + `model_allowlist` + `budget_micros` + `rate_limit_rpm`.
- **Metering / cost / rate-limit / spend-cap** live (v0.0.15–17) → TimescaleDB `usage_event`.
- **Auto routing + fallback + circuit-break** live (v0.0.18).
- ✅ **ai-review-worker already migrated (S13, v0.0.23)** — all 4 reviewers route through the gateway, metered + priced; a real review returned 98/100. This is our proof the passthrough repoint works end-to-end.

## Capability matrix — Anthropic stays the choice
On the **passthrough path with an Anthropic model we lose nothing**; we only gain.

| Feature the agent uses | Passthrough (Anthropic) | On a non-Anthropic switch |
|---|---|---|
| `claude_agent_sdk.query()` loop | ✅ works (raw body forwarded) | ❌ rewrite (SDK *is* the Anthropic runtime) |
| `create_sdk_mcp_server` in-process shims | ✅ works | ❌ re-express as standard MCP |
| Remote MCP (pr_context/tekton/jx3_flow) | ✅ works (same as today) | ✅ portable (MCP is neutral) |
| `tool_choice` forcing (video_review, spec_suggester) | ✅ works | ✅ portable → OpenAI `tool_calls` |
| Vision / image blocks (video_review) | ✅ works | ✅ portable (any vision model) |
| streaming, `max_turns`, `permission_mode` | ✅ works | SDK constructs → own loop |
| thinking / `pause_turn` / `cache_control` / `messages.parse` | n/a — **agent doesn't use them** | — |

**We gain immediately:** per-agent metering, budget caps, model allowlist, a
**model catalog** (rotate Claude versions via SQL, not a redeploy), central
routing/fallback, key rotation without pod restarts.

## LiteLLM — platform concern, NOT a client dependency
Decided in ai-bus `ARCHITECTURE.md`: LiteLLM is a **Tier-2 egress adapter inside
the gateway** (Go request path; Python egress-only; designed, not yet built) —
**not** embedded in clients, **not** in the hot path. This is the correct call
*and* it serves our "minimum Python in the agent" rule: the messy 100-provider
Python lives in the platform; the agent stays a thin gateway caller. If we ever
own a client-side tool loop (Phase 3), it targets the gateway's
`/v1/chat/completions` — it does **not** import LiteLLM's router.

## The sequence

### Phase 1 — repoint the LLM seam (config-only, zero feature loss) ← START HERE
Entirely mechanical; proven by ai-review-worker. aa-side changes (this PR):
- Chart: optional `ANTHROPIC_BASE_URL` env, gated on `agent.aiGateway.baseUrl`
  (unset → direct Anthropic, unchanged; safe for previews).
- `_JOB_FORWARDED_ENV_KEYS`: forward `ANTHROPIC_BASE_URL` (+ tool-model overrides)
  so spawned Jobs route through the gateway too.
- De-hardcode the two `claude-sonnet-4-6` tool models → env-configurable.

Gateway/GitOps side (runbook, needs control-plane access — not in this repo):
1. Mint an **agent virtual key** with a `model_allowlist` (e.g. `["claude-opus","claude-sonnet"]`), `budget_micros`, `rate_limit_rpm`.
2. Provision it via **ESO** into the agent namespace as the provider-neutral secret
   **`leartech-ai-gateway-key`** / key **`AI_GATEWAY_API_KEY`** (the chart already
   materialises this name; see "Naming" below). Concretely: point the cloud SOURCE
   — `externalSecrets.gcp.secrets[].key` (GSM) / `externalSecrets.azure.secrets[].key`
   (Vault) — at the virtual key instead of the current raw-Anthropic key. The chart
   default k8s name/key are already neutral, so nothing else moves.
3. Per-cluster GitOps overlay: set `agent.aiGateway.baseUrl: http://leartech-ai-gateway.ai-gateway.svc:8080`.
   ⚠️ If a cluster's `configs/leartech-automated-agent.yaml` overrides
   `externalSecrets.*.secrets` or `secrets.aiGatewayKey` with the OLD name, update
   it in lockstep — otherwise the deployment's secretKeyRef (now
   `leartech-ai-gateway-key`) won't resolve. `optional: true` on the ref means the
   pod still boots (LLM calls fail loudly) rather than crashlooping if it lags.
4. Verify: run one initiative → a `usage_event` row appears, cost priced, agent behaviour unchanged. Roll back = unset `baseUrl`.

### Naming — what's neutral, what's SDK-fixed (read this before renaming anything)
| Name | Owner | Renameable? |
|---|---|---|
| `ANTHROPIC_BASE_URL` (env) | Anthropic/Claude SDK | ❌ SDK reads this exact name. Value = the gateway URL. Free to rename only in **Phase 3** (own runtime). |
| `ANTHROPIC_API_KEY` (env) | Anthropic/Claude SDK | ❌ SDK reads this exact name. Value = a gateway **virtual key** (sk-lt-…), not a raw Anthropic key. Phase 3 only. |
| `leartech-ai-gateway-key` / `AI_GATEWAY_API_KEY` (k8s secret + key) | Ours | ✅ Provider-neutral. Renamed off `ai-review-api-keys` / `CLAUDE_API_KEY` so the agent stops piggybacking ai-review's secret. |
| `secrets.aiGatewayKey` (chart value) | Ours | ✅ Was `secrets.anthropicApiKey`. |
| `agent.aiGateway.baseUrl` (chart value) | Ours | ✅ Neutral from the start. |
| `LEARTECH_JOB_LLM_SECRET_NAME/KEY` (env) | Ours | ✅ Was `LEARTECH_JOB_ANTHROPIC_SECRET_*`; the old names still work as a back-compat fallback. |
| `LEARTECH_VIDEO_REVIEW_MODEL` / `LEARTECH_SPEC_SUGGESTER_MODEL` / `LEARTECH_AGENT_MODEL` (env) | Ours | ✅ Model ids are config, never hardcoded. |

**Rule for future sessions:** the two `ANTHROPIC_*` env names are load-bearing SDK
contract — do not rename them until Phase 3. Everything else is ours and is now
provider-neutral; keep it that way.

### Phase 2 — the tool/MCP seam (fixes `open_pr` + portability down-payment)
Own the MCP client via the **standard `mcp` library** (proven in-cluster to honour
our bearer → `/mcp/pr_context` 200). This decouples tools from the Claude CLI —
fixing the header-not-forwarded bug — and is the portability step (tools become
provider-neutral). This is where `create_sdk_mcp_server` shims retire. Bigger than
Phase 1; sequence it after the repoint is stable. See
`memory/project_mcp_discovery_source_of_truth.md` for the proven root cause.

### Phase 3 — two-backend runtime seam (only when a non-Anthropic model is chosen)
Add the `openai-compat` backend (`/v1/chat/completions` + a client-side tool
loop). Lossy (MCP connector / thinking / pause_turn don't translate — none are
load-bearing here). Anthropic-passthrough stays the full-feature default.
