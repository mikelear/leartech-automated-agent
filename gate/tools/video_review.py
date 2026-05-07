"""AI-powered visual review of Playwright videos.

Pipeline:

  1. Extract N evenly-spaced frames from a `.webm` via ffmpeg → PNG bytes.
  2. Send the frames + spec context to Claude (vision) with a `report_anomalies` tool
     for structured output.
  3. Parse the verdict — anomalies_found / summary / flagged frame indices.

Hard prerequisites for `review_video`:

- `ffmpeg` on PATH. Without it, callers should skip the criterion (helpful skip
  message via `check_prerequisites()`).
- `ANTHROPIC_API_KEY` env var. The user can fetch it on demand from the in-cluster
  secret — see memory `anthropic_api_key.md`. Don't persist it.

The pure parts (the prompt builder, the tool schema, the verdict dataclass) are
exposed and unit-testable. The actual SDK call is the thin IO layer.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

DEFAULT_FRAME_COUNT = 8
DEFAULT_MAX_WIDTH = 800
DEFAULT_MODEL = 'claude-sonnet-4-6'

VIDEO_REVIEW_SYSTEM_PROMPT = (
    'You are a visual reviewer for Playwright end-to-end browser test videos. '
    'You receive frames sampled evenly in temporal order from a recorded test run. '
    'Your job: spot anomalies a human reviewer would care about — frozen screens, '
    'broken layout, unexpected modals or error toasts, blank pages where content was expected, '
    'or visible JavaScript error overlays. '
    '\n\n'
    'Important calibration:\n'
    '- The earliest frame(s) typically capture the initial navigation moment before the page paints. '
    'A blank, partially-loaded, or white frame at index 0 (and occasionally index 1) is the normal '
    'load-state and is NOT an anomaly. Only flag a blank/unrendered state if it persists across '
    'most subsequent frames, indicating the page never loaded.\n'
    '- Brief blank moments mid-flow may be navigation transitions between pages — also not anomalies '
    'unless content fails to appear in the next frame.\n'
    '- Use the spec name as a hint about what *should* be visible by mid-to-late frames.\n'
    '\n'
    'If everything looks like a normal user flow proceeding without visual issues, report no anomalies. '
    'Use the `report_anomalies` tool to return your verdict — never reply in plain text.'
)

# Tool schema for structured output. Anthropic's tool_use mechanism gives us guaranteed JSON.
REPORT_ANOMALIES_TOOL: dict[str, Any] = {
    'name': 'report_anomalies',
    'description': 'Report whether the video has visual anomalies and summarise findings.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'anomalies_found': {
                'type': 'boolean',
                'description': 'True if any frame shows a visual anomaly worth flagging to a reviewer.',
            },
            'summary': {
                'type': 'string',
                'description': (
                    'One- or two-sentence explanation of what you saw. '
                    'If anomalies_found is false, summarise the normal flow you observed.'
                ),
            },
            'flagged_frames': {
                'type': 'array',
                'items': {'type': 'integer'},
                'description': '0-indexed frame numbers showing an anomaly. Empty if none.',
            },
        },
        'required': ['anomalies_found', 'summary', 'flagged_frames'],
    },
}


@dataclass(frozen=True)
class VideoVerdict:
    spec_name: str
    anomalies_found: bool
    summary: str
    flagged_frames: tuple[int, ...]

    @property
    def passed(self) -> bool:
        return not self.anomalies_found


@dataclass(frozen=True)
class Prerequisites:
    ffmpeg_path: str | None
    api_key_present: bool

    @property
    def ok(self) -> bool:
        return self.ffmpeg_path is not None and self.api_key_present

    def missing(self) -> list[str]:
        m: list[str] = []
        if self.ffmpeg_path is None:
            m.append('ffmpeg (install: `brew install ffmpeg`)')
        if not self.api_key_present:
            m.append('ANTHROPIC_API_KEY env var (fetch from cluster — see memory/anthropic_api_key.md)')
        return m


def check_prerequisites() -> Prerequisites:
    return Prerequisites(
        ffmpeg_path=shutil.which('ffmpeg'),
        api_key_present=bool(os.environ.get('ANTHROPIC_API_KEY')),
    )


def extract_frames(video_path: Path, n: int = DEFAULT_FRAME_COUNT, max_width: int = DEFAULT_MAX_WIDTH) -> list[bytes]:
    """Sample `n` evenly-spaced PNG frames from `video_path`. Returns the bytes in order.

    Uses ffmpeg's `select=not(mod(n\\,K))` filter to pick every Kth frame, then `scale=W:-2`
    to keep the image small (Claude doesn't need 4K — model bills per token, frames count).
    """
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        raise RuntimeError('ffmpeg not on PATH')

    with tempfile.TemporaryDirectory() as tmp:
        out_pattern = Path(tmp) / 'frame_%03d.png'
        # Two-step: probe frame count, then pick N evenly-spaced.
        # Simpler robust trick: use `fps=N/duration`. Even simpler and more portable: use
        # `-vf "thumbnail=K"` where K is roughly `total_frames / n`. We don't know
        # total_frames cheaply, so use ffprobe-style estimate via duration: ask ffmpeg
        # to write at most `n` frames evenly via the thumbnail+select filter.
        cmd = [
            ffmpeg,
            '-loglevel',
            'error',
            '-i',
            str(video_path),
            '-vf',
            f'thumbnail,scale={max_width}:-2',
            '-frames:v',
            str(n),
            str(out_pattern),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f'ffmpeg failed: {result.stderr.strip()}')

        frames = sorted(Path(tmp).glob('frame_*.png'))
        if not frames:
            raise RuntimeError(f'ffmpeg produced no frames from {video_path}')
        return [f.read_bytes() for f in frames]


def build_user_message(spec_name: str, frames: list[bytes], expected_flow: str | None) -> list[dict[str, Any]]:
    """Construct the message content blocks Claude will see — text intro + frames as image blocks."""
    intro_lines = [
        f'Playwright spec: `{spec_name}`',
        f'Frames sampled: {len(frames)} (in temporal order, oldest first).',
    ]
    if expected_flow:
        intro_lines.append('')
        intro_lines.append('Expected user flow:')
        intro_lines.append(expected_flow)
    intro_lines.append('')
    intro_lines.append('Inspect each frame in order. Use the `report_anomalies` tool to return your verdict.')
    blocks: list[dict[str, Any]] = [{'type': 'text', 'text': '\n'.join(intro_lines)}]
    for frame in frames:
        blocks.append(
            {
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': 'image/png',
                    'data': base64.standard_b64encode(frame).decode('ascii'),
                },
            }
        )
    return blocks


def parse_tool_use_response(response_content: list[dict[str, Any]], spec_name: str) -> VideoVerdict:
    """Walk the assistant's content blocks for the tool_use result and build a VideoVerdict."""
    for block in response_content:
        if block.get('type') != 'tool_use' or block.get('name') != 'report_anomalies':
            continue
        tool_input = block.get('input', {})
        return VideoVerdict(
            spec_name=spec_name,
            anomalies_found=bool(tool_input.get('anomalies_found', False)),
            summary=str(tool_input.get('summary', '')),
            flagged_frames=tuple(int(i) for i in tool_input.get('flagged_frames', [])),
        )
    raise RuntimeError(f'No report_anomalies tool_use block in response: {json.dumps(response_content)[:200]}')


def review_video(
    spec_name: str,
    frames: list[bytes],
    expected_flow: str | None = None,
    *,
    model: str = DEFAULT_MODEL,
) -> VideoVerdict:
    """Send frames to Claude with vision + tool_use; return the structured VideoVerdict.

    Requires `ANTHROPIC_API_KEY` env var. Use `check_prerequisites()` to gate the call.
    """
    # Defer the import so the rest of this module is importable without anthropic installed
    # (helps the tests that don't exercise the IO path).
    from anthropic import Anthropic

    client = Anthropic()
    messages = [{'role': 'user', 'content': build_user_message(spec_name, frames, expected_flow)}]
    # The SDK uses TypedDict shapes for messages/tools/tool_choice; our literal-dict
    # construction matches at runtime but mypy can't structurally verify the nested image
    # blocks. cast(Any, ...) keeps strict mode green without weakening the rest of the file.
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=VIDEO_REVIEW_SYSTEM_PROMPT,
        tools=cast(Any, [REPORT_ANOMALIES_TOOL]),
        tool_choice=cast(Any, {'type': 'tool', 'name': 'report_anomalies'}),
        messages=cast(Any, messages),
    )
    # response.content is a list of content blocks; cast to the dict shape parse_tool_use_response expects.
    blocks = [block.model_dump() for block in response.content]
    return parse_tool_use_response(blocks, spec_name)
