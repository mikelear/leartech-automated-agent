"""Local YAML validator — same Pydantic checks the runtime applies.

Operators run this via ``scripts/validate_initiative.sh <path>`` to
pre-flight a draft initiative YAML before POSTing it to the catalog,
without spinning up Python imports themselves.

Mirrors the service-side ``POST /initiatives/_validate`` endpoint: same
loader, same error surface — just printed to stdout/stderr instead of
returned as JSON. Exit code conveys verdict (0 = valid, 1 = invalid,
2 = usage).
"""

from __future__ import annotations

import sys
from pathlib import Path

from gate.initiatives.loader import load_initiative


def main() -> None:
    if len(sys.argv) != 2:
        print('Usage: python -m gate.initiatives.validate_cli <path>', file=sys.stderr)
        sys.exit(2)
    try:
        i = load_initiative(Path(sys.argv[1]))
    except Exception as exc:  # noqa: BLE001 — surface every loader failure as a 1-exit
        print(f'FAIL: {exc}', file=sys.stderr)
        sys.exit(1)
    print(f'OK: {i.name}')
    for r in i.repos:
        print(f'  repo={r.qualified_repo} branch={r.branch}')


if __name__ == '__main__':
    main()
