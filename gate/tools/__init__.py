"""Primitives the criteria layer depends on. Each module is also wrappable as an MCP server (see gate/mcp_servers/)."""

from gate.tools.ai_review import AIReviewVerdict, read_ai_review_verdicts
from gate.tools.coverage import CoverageReport, read_coverage_from_pr_comments, read_coverage_threshold
from gate.tools.pipelines import PipelineCheck, list_pr_checks
from gate.tools.playwright_artifacts import (
    Artifact,
    PlaywrightRun,
    download_artifact,
    head_artifact,
    is_fragile_text_selector,
    read_playwright_runs,
)
from gate.tools.playwright_specs import SpecCoverage, inventory_specs, parse_spec
from gate.tools.pr_context import PRContext, load_pr_context
from gate.tools.pr_diff import added_files, added_lines, fetch_pr_diff
from gate.tools.spec_suggester import (
    SpecSuggestion,
    is_anthropic_key_present,
    suggest_spec,
)
from gate.tools.triggers import (
    Trigger,
    angular_template_consumers,
    diff_triggers,
    fetch_triggers_yaml,
    go_service_template_consumers,
    golden_template_for,
    parse_triggers_yaml,
)
from gate.tools.ui_surface_diff import (
    UISurfaceDelta,
    compute_ui_surface_delta,
    selectors_from_added_lines,
)
from gate.tools.video_review import (
    Prerequisites,
    VideoVerdict,
    check_prerequisites,
    extract_frames,
    review_video,
)

__all__ = [
    'PRContext',
    'load_pr_context',
    'PipelineCheck',
    'list_pr_checks',
    'CoverageReport',
    'read_coverage_threshold',
    'read_coverage_from_pr_comments',
    'AIReviewVerdict',
    'read_ai_review_verdicts',
    'fetch_pr_diff',
    'added_files',
    'added_lines',
    'Artifact',
    'PlaywrightRun',
    'read_playwright_runs',
    'download_artifact',
    'head_artifact',
    'is_fragile_text_selector',
    'VideoVerdict',
    'Prerequisites',
    'check_prerequisites',
    'extract_frames',
    'review_video',
    'Trigger',
    'parse_triggers_yaml',
    'fetch_triggers_yaml',
    'golden_template_for',
    'angular_template_consumers',
    'go_service_template_consumers',
    'diff_triggers',
    'UISurfaceDelta',
    'compute_ui_surface_delta',
    'selectors_from_added_lines',
    'SpecCoverage',
    'inventory_specs',
    'parse_spec',
    'SpecSuggestion',
    'suggest_spec',
    'is_anthropic_key_present',
]
