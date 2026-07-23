"""Structured JSON logging → Loki/Grafana as the first troubleshooting stop.

One schema across agent components. Seam-agnostic: Phase-B's runtime calls these
SAME functions, so instrumentation survives the refactor (see
memory project_runtime_seam_refactor_plan). Lean Phase A wires only the STABLE
run boundaries (run_start / run_end); deeper per-tool events land in Phase B.

Built on the stdlib ``logging`` module (org standard) with a DEDICATED handler
whose formatter is just ``%(message)s`` and ``propagate=False`` — so each record
is emitted as ONE pure-JSON line on stderr (no ``LEVEL:name:`` prefix that would
break Loki's ``| json`` parser, and no duplicate copy via the root logger).
stderr → pod log → Alloy → Loki, queryable in Grafana:
  `{namespace="jx-staging"} | json | level="ERROR"`  /  `event="run_end"`.

Schema (stable — panels + LogQL depend on it):
  time    ISO8601 UTC
  level   ERROR | WARN | INFO | DEBUG
  logger  component, e.g. agent.initiative / agent.mcp_bridge
  event   stable name: run_start | run_end | tool_call | tool_result | retry | ...
  msg     human-readable
  + ambient run context from env (run_id, namespace, cluster, version)
  + event-specific fields (exit_code, targetPR, turns, cost_usd, tool, ok, ...)

Kept dependency-free (stdlib only) so ANY component — including the
provider-neutral stdio bridge — can import it without coupling.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

# Ambient run context, injected into the Job pod by the controller's jobspawn.
# Absent on laptop/preview → simply omitted from the record (no crash).
_CONTEXT_ENV = {
    'run_id': 'LEARTECH_RUN_ID',
    'namespace': 'AGENT_RUN_NAMESPACE',
    'cluster': 'CLUSTER',
    'version': 'VERSION',
}

_LEVELS = {
    'ERROR': logging.ERROR,
    'WARN': logging.WARNING,
    'INFO': logging.INFO,
    'DEBUG': logging.DEBUG,
}

_logger = logging.getLogger('leartech.obslog')
_configured = False


def _ensure_configured() -> None:
    """Attach a dedicated stderr handler that emits ONLY the message (our JSON).

    Lazy + idempotent. ``propagate=False`` so records don't ALSO go through the
    root logger's formatted handler (which would emit a prefixed duplicate and
    break Loki JSON parsing).
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(message)s'))
    _logger.addHandler(handler)
    _logger.setLevel(logging.DEBUG)
    _logger.propagate = False
    _configured = True


def _context() -> dict[str, str]:
    return {key: os.environ[env] for key, env in _CONTEXT_ENV.items() if os.environ.get(env)}


def emit(level: str, event: str, msg: str, *, logger: str = 'agent', **fields: object) -> None:
    """Emit one structured JSON log record (event-specific ``fields`` with None
    dropped; ``level`` normalised to a known level)."""
    _ensure_configured()
    lvl = level.upper()
    if lvl not in _LEVELS:
        lvl = 'INFO'
    record: dict[str, object] = {
        'time': datetime.now(UTC).isoformat(),
        'level': lvl,
        'logger': logger,
        'event': event,
        'msg': msg,
    }
    record.update(_context())
    record.update({k: v for k, v in fields.items() if v is not None})
    # default=str so a stray non-JSON value degrades to its repr instead of raising.
    _logger.log(_LEVELS[lvl], json.dumps(record, default=str))


def info(event: str, msg: str, *, logger: str = 'agent', **fields: object) -> None:
    emit('INFO', event, msg, logger=logger, **fields)


def warning(event: str, msg: str, *, logger: str = 'agent', **fields: object) -> None:
    emit('WARN', event, msg, logger=logger, **fields)


def error(event: str, msg: str, *, logger: str = 'agent', **fields: object) -> None:
    emit('ERROR', event, msg, logger=logger, **fields)
