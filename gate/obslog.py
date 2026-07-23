"""Structured JSON logging → Loki/Grafana as the first troubleshooting stop.

One schema across agent components. Seam-agnostic: Phase-B's runtime calls these
SAME functions, so instrumentation survives the refactor (see
memory project_runtime_seam_refactor_plan). Lean Phase A wires only the STABLE
run boundaries (run_start / run_end); deeper per-tool events land in Phase B where
the message loop is rewritten.

Each record is one JSON object on stderr → pod log → Alloy → Loki, queryable in
Grafana:  `{namespace="jx-staging"} | json | level="ERROR"`  /  `event="run_end"`.

Schema (stable — panels + LogQL depend on it):
  time    ISO8601 UTC
  level   ERROR | WARN | INFO | DEBUG
  logger  component, e.g. agent.initiative / agent.mcp_bridge
  event   stable name: run_start | run_end | tool_call | tool_result | retry | ...
  msg     human-readable
  + ambient run context from env (run_id, namespace, cluster, version)
  + event-specific fields (exit_code, targetPR, turns, cost_usd, tool, ok, ...)

Kept dependency-free (json/os/sys/datetime) so ANY component — including the
provider-neutral stdio bridge — can import it without coupling.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

# Ambient run context, injected into the Job pod by the controller's jobspawn.
# Absent on laptop/preview → simply omitted from the record (no crash).
_CONTEXT_ENV = {
    'run_id': 'LEARTECH_RUN_ID',
    'namespace': 'AGENT_RUN_NAMESPACE',
    'cluster': 'CLUSTER',
    'version': 'VERSION',
}

_VALID_LEVELS = ('ERROR', 'WARN', 'INFO', 'DEBUG')


def _context() -> dict[str, str]:
    return {key: os.environ[env] for key, env in _CONTEXT_ENV.items() if os.environ.get(env)}


def emit(level: str, event: str, msg: str, *, logger: str = 'agent', **fields: Any) -> None:
    """Emit one structured JSON log line on stderr.

    ``fields`` are event-specific (None values dropped so absent data doesn't
    clutter the record / LogQL). ``level`` is normalised to a known level.
    """
    lvl = level.upper()
    if lvl not in _VALID_LEVELS:
        lvl = 'INFO'
    record: dict[str, Any] = {
        'time': datetime.now(UTC).isoformat(),
        'level': lvl,
        'logger': logger,
        'event': event,
        'msg': msg,
    }
    record.update(_context())
    record.update({k: v for k, v in fields.items() if v is not None})
    # default=str so a stray non-JSON value degrades to its repr instead of
    # raising and losing the log line.
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)


def info(event: str, msg: str, *, logger: str = 'agent', **fields: Any) -> None:
    emit('INFO', event, msg, logger=logger, **fields)


def warning(event: str, msg: str, *, logger: str = 'agent', **fields: Any) -> None:
    emit('WARN', event, msg, logger=logger, **fields)


def error(event: str, msg: str, *, logger: str = 'agent', **fields: Any) -> None:
    emit('ERROR', event, msg, logger=logger, **fields)
