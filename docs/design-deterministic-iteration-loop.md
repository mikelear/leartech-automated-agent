# Design: the agent keeps its judgement, Python takes the process

Status: proposed, not implemented. Delete this file when the work lands — the code and
`tests/` are the record afterwards.

## Problem

The iteration loop is expressed as English instructions in a 367-line system prompt and
executed by the model calling tools. Most of those instructions are control flow, not
judgement: ensure the branch exists, commit the files you touched, after each push call
`wait_for_first_failure_or_all_pass`, stop on `all_passed`, on `some_failed` call
`step_status` then `step_logs`, cancel superseded runs on force-push, respect the
iteration budget, post the sticky.

Three consequences, all observed:

1. **Python reverse-engineers state it should own.** `terminal_all_passed_seen` is set by
   inspecting the model's tool results, because the only record of "did the gate go green"
   is the transcript. Everything built on top of that — the verdict gate, the exit-code
   normalisation, the `gh pr list` break-glass in `_resolve_pr_number` — exists to
   compensate for not owning the loop.
2. **Procedure in prose rots silently.** The prompt instructed the agent to call
   `classify_step_failure` and `rebase_branch_on_base` after both were deleted, to run
   `gate.tools.e2e_coverage.evaluate_e2e_coverage` after that module was deleted, and to
   read logs via a shell script under a developer's home directory. Nothing failed; the
   model routed around it, spending turns and tokens.
3. **Tools built for the job go unused.** `resolve_pr` and `pr_gate_snapshot` answer
   "does the PR exist, what is its state, what happened to its checks" in one call. Neither
   is in `allowed_tools` or mentioned in the prompt, so the agent shells out to `gh`
   instead.

`infra-go` is the precedent: the release-verify checks were deterministic, moved to Go, and
the 542-line Python relay became dead code. This is the same move one layer up. It is
possible now and was not before, because `pr_gate_snapshot` did not exist when the loop was
written — the model was the only thing that could see Tekton and GitHub.

## How Python calls a Go MCP server

No new mechanism, and no duplicated tool definitions. `gate/mcp_servers/call.py` is an MCP
CLIENT: it mints the `aud=leartech-mcp` token, discovers mounts from the host's `/mcps`,
then opens a streamable-HTTP MCP session and calls the tool. MCP is a client/server
protocol — the client does not have to be a model. What Python loses by calling directly is
model-mediated tool use, which is exactly the point for a deterministic step.

Both callers already coexist against the same servers today: Python calls
`post_pr_handoff` this way, while `open_pr` and the jx3-flow / tekton tools reach the LLM
through `stdio_bridge`. The tools stay MCP tools, exposed to the agent as MCPs. Python
calls the same tool when it needs an answer it can act on without a turn.

## Boundary

The agent stays an agent. It keeps its MCP tools and its own reasoning inside an iteration —
reading logs, deciding whether a failure is its own, choosing and making the fix, writing
its summary. What it stops carrying is PROCESS.

**Python owns the frame:**

- ENTRY — establish state before the model is invoked, and read back the predecessor's
  handoff so the run starts informed.
- EXIT — the verdict and the exit code, from state it observed itself rather than scraped.
- BOOKKEEPING — iteration budget, superseded-run cancellation, the handoff write.

**The agent owns the work inside the frame:** given the situation Python established (PR
state, which checks failed, the logs, the goal), decide what to do and do it, with the same
MCP tools it has now.

**The prompt keeps judgement and loses procedure.** Out: which wait tool to call when, the
branch/commit/push mechanics, cancel-on-force-push, budget arithmetic, the stop rule. In:
what a good fix looks like, what is out of scope, when to escalate, the hard rules about
pushing.

## State machine

    ESTABLISH ──> (no PR) ──> IMPLEMENT ──> COMMIT ──> WAIT
         │                                              │
         └──────> (PR exists) ──> ASSESS ───────────────┤
                                    │                   │
                                    └──> IMPLEMENT ──────┘
    WAIT ──> all_passed ──> FINALISE ──> done
         ──> first_failure ──> ASSESS
         ──> timeout / unknown ──> ESCALATE
    any state ──> budget exhausted ──> ESCALATE

**ESTABLISH** — one `pr_gate_snapshot` call. Returns whether a PR exists, its state, the
per-check status and the Keeper merge status. If a PR exists, also read its handoff comment
so the run starts from what its predecessor concluded rather than from nothing. Replaces
`_resolve_pr_number`'s `gh pr list` subprocess and `_detect_resume_context`'s branch
probing.

**ASSESS** — Python GATHERS, the agent decides. For each failed check it calls
`step_status` then `step_logs` and hands over which step failed and what its log says.
Working out WHY it failed, whether it is the agent's own change, and what the fix is stays
with the agent — that is judgement, and a step-name lookup table would be exactly the
"lots of code checking what kind of failure this is" that we are removing elsewhere.

The one thing Python decides here is whether there is anything to hand over at all: no
failed checks means WAIT, and a wait that returns neither a failure nor all-green (timeout,
no checks fired) means ESCALATE, because there is no evidence for the agent to reason from.

**IMPLEMENT** — the agent's turn. It is handed the situation, not a procedure: the goal,
the diff so far, which checks failed and their logs. It reasons and edits with its existing
tools, commits and pushes, and says what it did. Python does not script these steps; it
supplies the inputs and records the outcome.

**Superseded runs** are cancelled by Python on each new push, because that is bookkeeping
with one right answer and no judgement in it.

**WAIT** — `wait_for_first_failure_or_all_pass`. Fail-fast is the in-loop primitive: one
failure is enough, because the next action is a new commit regardless. `wait_for_terminal`
is used once, in FINALISE, before declaring green.

**FINALISE** — `wait_for_terminal` confirms every required check is terminal and green,
post the summary sticky, stop. Do not wait for merge; Tide merges and the controller stops
the agent.

**ESCALATE** — post a sticky naming the failing step, the evidence read, and why it could
not be placed. Exit non-zero so the step recycles.

## What this deletes

- `terminal_all_passed_seen` and `_tool_result_reports_all_passed` — Python observes the
  wait result itself at the frame boundary instead of scraping the transcript.
- `_resolve_pr_number` and its `gh pr list` subprocesses; `status.targetPR` stays as the
  authoritative record, written by `open_pr`.
- `_detect_resume_context` / `_remote_branch_exists` — ESTABLISH covers it.
- The verdict gate and exit-code normalisation collapse into the state machine's own
  terminal states.
- Roughly 200 of the prompt's 367 lines.

## Risks, and how they are handled

**Novel situations.** Today the model improvises through anything unexpected. A state
machine cannot, so unrecognised failures must escalate with evidence. This is a behaviour
change: the agent will stop more often instead of quietly trying things. That is the
intended trade — an escalation with a log is worth more than an unexplained green.

**Over-constraining the agent.** The failure mode of this design is taking away reasoning
the agent needs. It keeps its MCP tools and its own turn budget within an iteration; only
the loop's shape and the terminal decision move out. If a change would stop the agent
reading something or deciding something, it is out of scope for this work.

**Loss of prompt flexibility.** Changing loop behaviour becomes a code change with a test
rather than a prompt edit. That is the point, but it does mean the feedback loop for
behaviour changes runs through CI.

## Acceptance

- The loop is unit-testable with a fake tool-caller: assert the state transitions for
  all_passed, first_failure, timeout, no-PR, PR-exists-and-failing, and budget-exhausted,
  with no LLM involved.
- `pr_gate_snapshot` and `resolve_pr` appear in `allowed_tools` and in the ESTABLISH path.
- The prompt contains no loop-control instructions; `tests/test_prompt_contract.py` gains a
  rule that it names no tool the loop owns.
- A real run on a Go repo reaches green, with fewer turns spent on control flow than
  today — verifiable in Loki by counting `tool_call` events per `run_id` and comparing
  against a current run.

## Sequencing

After the Plan run proves the current decommissioning works end to end. This changes a
known-good baseline; doing both at once would mean debugging two things.
