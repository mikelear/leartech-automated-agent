# Design: Python owns the iteration loop, the LLM owns the change

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

## Boundary

**Python owns the loop.** Establish state, branch, commit, push, wait, fetch the failing
step's log, decide iterate / escalate / stop, cancel superseded runs, count iterations,
post the handoff, compute the verdict.

**The LLM owns one bounded task per iteration:** given the goal, the diff so far, and the
failing step with its log, change the code. It keeps `Read`, `Edit`, `Write`, `Grep`,
`Glob` and `Bash` (so it can run the language image's `make` targets and inspect the repo).
It loses the loop-control tools, because it is no longer driving the loop.

The prompt then carries only judgement: what a good fix looks like, what is out of scope,
when to escalate, the hard rules about pushing. Procedure moves to code.

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

**ASSESS** — for each failed check, `step_status` then `step_logs`. Deterministic
classification on the step name where the mapping is unambiguous (git-clone conflict →
rebase; ruff/mypy/pytest → hand to the LLM with the log; kaniko / image pull / OOM /
security-scan → escalate). Anything unrecognised escalates with the evidence rather than
being guessed at.

**IMPLEMENT** — the bounded LLM call. One task, one failure (or the initial goal), a turn
budget of its own. Returns a summary of what it changed.

**COMMIT** — deterministic: stage the paths the LLM touched, conventional message, push.
`--force-with-lease` only after a rebase. Cancel superseded PipelineRuns for the PR.

**WAIT** — `wait_for_first_failure_or_all_pass`. Fail-fast is the in-loop primitive: one
failure is enough, because the next action is a new commit regardless. `wait_for_terminal`
is used once, in FINALISE, before declaring green.

**FINALISE** — `wait_for_terminal` confirms every required check is terminal and green,
post the summary sticky, stop. Do not wait for merge; Tide merges and the controller stops
the agent.

**ESCALATE** — post a sticky naming the failing step, the evidence read, and why it could
not be placed. Exit non-zero so the step recycles.

## What this deletes

- `terminal_all_passed_seen` and `_tool_result_reports_all_passed` — the loop knows the
  status because it made the call.
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

**Fixes that need exploration.** The bounded call keeps real tools and its own turn budget,
so a fix requiring three files and a local test run still works. What it cannot do is
decide when the loop ends.

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
- A real run on a Go repo reaches green with one LLM call per failing iteration, verifiable
  in Loki by counting `tool_call` events per `run_id`.

## Sequencing

After the Plan run proves the current decommissioning works end to end. This changes a
known-good baseline; doing both at once would mean debugging two things.
