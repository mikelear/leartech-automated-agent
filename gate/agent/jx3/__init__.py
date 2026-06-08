"""JX3-flow runtime: shared rules consumed by the MCP server + calibration.

The `rules.py` module is the SINGLE SOURCE OF TRUTH for the
machine-executable description of "where is this PR in the JX3 flow."
The sister calibration markdown (`gate/agent/calibrations/jx3-full-flow.md`,
landing in a separate initiative) is the human-readable reflection of
the same rules. The drift-detection test in
`tests/test_jx3_calibration_matches_rules.py` keeps them in sync.
"""
