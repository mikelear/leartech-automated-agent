---
id: port-bash-tools-to-python
title: Port bash tools to Python when JSON output is clean and dep is cross-repo
captured_at: 2026-05-04T13:30:00Z
source:
  type: agent_run
  reference: pipelines_py_native_port
  observer: mike@leartech
  latency_to_capture: hours
category: architecture
applies_to: []
status: encoded
encoded_in:
  - gate/tools/pipelines.py
---

When a vendored / external bash tool has a clean JSON output mode and binds the
runner to a specific laptop layout (e.g. `~/leartech/Hub/scripts/...`), porting the
**parser** (not the whole script) gets you 80% of the value with 20% of the LOC.

**Concrete instance**: `pr-pipelines.sh --json` was ~80 lines doing `gh pr view --json
statusCheckRollup` + jq munging + URL-to-cluster mapping. Native port:

- 1 `_gh()` call replacing the script subprocess
- `parse_target_url()` — pure URL parser
- `parse_status_check_rollup()` — pure rollup parser
- 6 unit tests pinning the URL/cluster shape

**What we kept**: `parse_pipelines_json` (tolerant of trailing junk) for callers that
still feed raw script output. Don't break working consumers.

**What we left in bash**: the script's `--logs` (kubectl pod log dump) and `--watch`
(polling loop) paths. They're not load-bearing for the gate; can stay until needed.

**Generic lesson for cross-repo deps**: identify which paths your code actually
exercises. Port those to native code (vendored or rewritten); leave the rest as
external dependencies until they're proven necessary.
