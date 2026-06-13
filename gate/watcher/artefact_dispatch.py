"""Watcher-level dispatcher: prefer structured artefact parsing, fall back to log heuristics.

v6p0.6 step 1 of 4 wiring. Before the existing step-log heuristic dispatcher
(:mod:`gate.agent.step_failure_diagnosis`) sees a failed Tekton check, this
module asks: "Does this gate emit a known artefact type? If so, can we
fetch + parse it into a structured ``GateFailure``?"

If yes — the watcher iteration loop consumes the structured payload (richer
data than the log tail alone). If no — fall through to the heuristic
dispatcher, which is the existing safety net.

## Inputs / outputs

The dispatcher is intentionally pure (like the iteration-loop decision
module): given a gate name + a way to fetch the artefact content, return
either a :class:`GateFailure` (structured-parse hit) or ``None`` (caller
falls through). No I/O or side-effects in this module itself; the
artefact-fetch callable is injected so tests can drive the path without
real cluster access.

## Soft-fail contract

If the artefact-fetch callable raises (kubectl timeout, missing PVC,
GCS auth issue), we log at WARN and return ``None`` so the heuristic
dispatcher takes over. We never want a transient fetch error to abort
the entire watcher cycle.

## Step-2-of-4 integration point

The actual artefact-storage convention is left out of scope for step 1
(see the initiative's "Out of scope" section). Step 2 will land the
fetch layer — a small adapter that maps ``(gate, pipelinerun_name, cluster)
-> bytes`` via either:

- Tekton ``Results`` (the canonical Tekton 0.20+ way), OR
- A PVC mount the gate task wrote to, OR
- A GCS bucket some catalog tasks already push artefacts to.

When that lands, :func:`dispatch_structured_failure` doesn't change — only
the callable callers pass in changes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gate.tools.parsers import (
    GateFailure,
    parse_gate_artefact,
    resolve_artefact_type,
)

logger = logging.getLogger(__name__)


#: Signature for the artefact-fetch callable injected by the orchestrator.
#:
#: Inputs: ``(gate, pipelinerun_name, cluster)``.
#: Output: the artefact bytes (or empty bytes when unavailable).
#:
#: We pass bytes (not str) because some artefacts are binary-ish — gzipped
#: SARIF, base64-padded JUnit XML — and we don't want the orchestrator to
#: have to know which is which. Parsers handle decode themselves.
ArtefactFetcher = Callable[[str, str, str], bytes]


def dispatch_structured_failure(
    *,
    gate: str,
    pipelinerun_name: str,
    cluster: str,
    artefact_fetcher: ArtefactFetcher,
    raw_log_tail: str = '',
) -> GateFailure | None:
    """Try to produce a structured :class:`GateFailure` for ``gate``.

    Workflow:

    1. Resolve ``gate`` → artefact_type via the registry's
       :func:`gate.tools.parsers.resolve_artefact_type`. ``None`` → return
       ``None`` (caller falls back to the heuristic dispatcher).
    2. Call ``artefact_fetcher(gate, pipelinerun_name, cluster)`` to get
       raw bytes. Any exception → log + return ``None``.
    3. Pass the bytes through the parser registry (via
       :func:`gate.tools.parsers.parse_gate_artefact`). Returns a populated
       ``GateFailure`` even when ``findings`` is empty — the caller
       distinguishes "empty findings + non-empty raw_log_tail" from "fetch
       failed → None".

    ``raw_log_tail`` is preserved on the GateFailure so downstream
    prompt-rendering can fall through to log-tail rendering when the
    artefact was readable but contained no actionable issues (e.g. a
    gate failed for an off-spec reason a structured parser can't capture).
    """
    artefact_type = resolve_artefact_type(gate)
    if artefact_type is None:
        logger.debug(
            'artefact_dispatch: no artefact mapping for gate %r; falling back to log-tail heuristic',
            gate,
        )
        return None

    try:
        content = artefact_fetcher(gate, pipelinerun_name, cluster)
    except Exception as exc:  # noqa: BLE001 — soft-fail per module contract
        logger.warning(
            'artefact_dispatch: fetch for %s on %s/%s failed: %s; falling back to log-tail heuristic',
            gate,
            cluster,
            pipelinerun_name,
            exc,
        )
        return None

    if not content:
        logger.debug(
            'artefact_dispatch: empty content for %s on %s/%s; falling back to log-tail heuristic',
            gate,
            cluster,
            pipelinerun_name,
        )
        return None

    return parse_gate_artefact(
        gate=gate,
        artefact_type=artefact_type,
        content=content,
        raw_log_tail=raw_log_tail,
    )


__all__ = [
    'ArtefactFetcher',
    'dispatch_structured_failure',
]
