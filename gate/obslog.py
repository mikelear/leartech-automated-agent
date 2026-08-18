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

_CLUSTER_CONTEXT_ENV = {
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
    """Ambient run fields stamped on every log record.

    Identity fields (``run_id`` + ``namespace``) come from the identity
    snapshot — see the ``_CLUSTER_CONTEXT_ENV`` comment above for why the
    strip forces us to read there instead of :data:`os.environ`. Wrapped in
    a defensive try so a broken import (circular / missing module in a
    partial install) can never take a log line down with it — a Loki record
    without the ambient fields is still better than a raised emit that
    disappears the record entirely (the incident this whole change fixes).
    """
    fields: dict[str, str] = {}
    try:
        from gate import identity

        fields.update(identity.ambient_log_fields())
    except Exception as exc:  # noqa: BLE001 — logging must never raise; drop identity fields on any error
        _ = exc  # noqa: F841 — bound purely to satisfy the "log the exception" lint
    for key, env in _CLUSTER_CONTEXT_ENV.items():
        value = os.environ.get(env)
        if value:
            fields[key] = value
    return fields


def emit(level: str, event: str, msg: str, *, logger: str = 'agent', **fields: object) -> None:
    """Emit one structured JSON log record (event-specific ``fields`` with None
    dropped; ``level`` normalised to a known level).

    Fully failure-proof: any exception raised by the internal formatting /
    serialization / handler machinery is caught and rewritten as a plain
    stderr breadcrumb. A forensic signal that dies when things go wrong is
    worse than none — its ABSENCE reads as "this did not happen" (see the
    ``targetpr_backstop_fired`` incident this design fixes). Every caller
    can rely on ``emit`` never propagating an exception.
    """
    try:
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
        _logger.log(_LEVELS[lvl], json.dumps(record, default=str))
    except Exception as exc:  # noqa: BLE001 — logging is best-effort; never raise to callers
        try:
            sys.stderr.write(
                f'obslog.emit failed for event={event!r} level={level!r}: {exc!r}\n',
            )
        except Exception as inner:  # noqa: BLE001 — even stderr write is best-effort in a broken interpreter
            _ = inner  # noqa: F841


def info(event: str, msg: str, *, logger: str = 'agent', **fields: object) -> None:
    emit('INFO', event, msg, logger=logger, **fields)


def warning(event: str, msg: str, *, logger: str = 'agent', **fields: object) -> None:
    emit('WARN', event, msg, logger=logger, **fields)


def error(event: str, msg: str, *, logger: str = 'agent', **fields: object) -> None:
    emit('ERROR', event, msg, logger=logger, **fields)
