#!/usr/bin/env bash
# Validate an initiative YAML locally (no service round-trip).
#
# Runs the same Pydantic checks the runtime applies — useful for
# pre-flight verification of a draft YAML before POSTing it to the
# /initiatives/catalog endpoint or committing it under initiatives/.
#
# Usage: scripts/validate_initiative.sh <path-to-yaml>
# Exit codes: 0 = valid, 1 = invalid, 2 = usage.
set -euo pipefail
if [ -z "${1:-}" ]; then
  echo "Usage: $0 <path-to-yaml>" >&2
  exit 2
fi
uv run python -m gate.initiatives.validate_cli "$1"
