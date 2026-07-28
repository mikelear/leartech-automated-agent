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

# Gateway routing (S13 pattern). When AI_GATEWAY_URL + AI_GATEWAY_API_KEY are set,
# vision review routes through leartech-ai-gateway (/v1/chat/completions, OpenAI
# image_url + tool-calling) to a vision-capable model — GATEWAY_VISION_MODEL, default
# the gateway's gpt-4o (logical `azure-openai`, proven vision over the OpenAI seam).
# Unset → direct Anthropic (laptop mode), unchanged. Additive; no flag day.
GATEWAY_URL = os.environ.get('AI_GATEWAY_URL', '').rstrip('/')
GATEWAY_KEY = os.environ.get('AI_GATEWAY_API_KEY', '')
GATEWAY_VISION_MODEL = os.environ.get('GATEWAY_VISION_MODEL', 'azure-openai')


def _gateway_configured() -> bool:
    return bool(GATEWAY_URL and GATEWAY_KEY)

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


# Same schema in OpenAI function-calling shape (gateway seam). function.parameters
# IS the JSON schema, identical to Anthropic's input_schema — reuse it.
REPORT_ANOMALIES_TOOL_OPENAI: dict[str, Any] = {
    'type': 'function',
    'function': {
        'name': REPORT_ANOMALIES_TOOL['name'],
        'description': REPORT_ANOMALIES_TOOL['description'],
        'parameters': REPORT_ANOMALIES_TOOL['input_schema'],
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
    # credentials for EITHER path: the gateway (in-cluster) or a direct Anthropic
    # key (laptop). Named api_key_present for back-compat; true if either is usable.
    api_key_present: bool

    @property
    def ok(self) -> bool:
        return self.ffmpeg_path is not None and self.api_key_present

    def missing(self) -> list[str]:
        m: list[str] = []
        if self.ffmpeg_path is None:
            m.append('ffmpeg (install: `brew install ffmpeg`)')
        if not self.api_key_present:
            m.append('AI_GATEWAY_URL + AI_GATEWAY_API_KEY (in-cluster) OR ANTHROPIC_API_KEY '
                     '(laptop — fetch from cluster, see memory/anthropic_api_key.md)')
        return m


def check_prerequisites() -> Prerequisites:
    # Either credential path counts — otherwise this silently skips the criterion
    # in-cluster, where the gateway (not ANTHROPIC_API_KEY) is what's configured.
    return Prerequisites(
        ffmpeg_path=shutil.which('ffmpeg'),
        api_key_present=_gateway_configured() or bool(os.environ.get('ANTHROPIC_API_KEY')),
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


def _intro_text(spec_name: str, frames: list[bytes], expected_flow: str | None) -> str:
    lines = [
        f'Playwright spec: `{spec_name}`',
        f'Frames sampled: {len(frames)} (in temporal order, oldest first).',
    ]
    if expected_flow:
        lines += ['', 'Expected user flow:', expected_flow]
    lines += ['', 'Inspect each frame in order. Use the `report_anomalies` tool to return your verdict.']
    return '\n'.join(lines)


def build_user_message(spec_name: str, frames: list[bytes], expected_flow: str | None) -> list[dict[str, Any]]:
    """Anthropic content blocks — text intro + frames as image blocks (direct SDK path)."""
    blocks: list[dict[str, Any]] = [{'type': 'text', 'text': _intro_text(spec_name, frames, expected_flow)}]
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


def build_openai_user_message(spec_name: str, frames: list[bytes], expected_flow: str | None) -> list[dict[str, Any]]:
    """OpenAI multimodal content — text intro + frames as image_url data URIs (gateway path)."""
    blocks: list[dict[str, Any]] = [{'type': 'text', 'text': _intro_text(spec_name, frames, expected_flow)}]
    for frame in frames:
        b64 = base64.standard_b64encode(frame).decode('ascii')
        blocks.append({'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}})
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


def parse_openai_tool_call(resp: dict[str, Any], spec_name: str) -> VideoVerdict:
    """Walk an OpenAI chat-completion response for the report_anomalies tool_call
    (the gateway seam's structured-output shape) and build a VideoVerdict."""
    choices = resp.get('choices') or []
    tool_calls = (choices[0].get('message', {}) if choices else {}).get('tool_calls') or []
    for tc in tool_calls:
        fn = tc.get('function', {})
        if fn.get('name') == 'report_anomalies':
            args = json.loads(fn.get('arguments') or '{}')
            return VideoVerdict(
                spec_name=spec_name,
                anomalies_found=bool(args.get('anomalies_found', False)),
                summary=str(args.get('summary', '')),
                flagged_frames=tuple(int(i) for i in args.get('flagged_frames', [])),
            )
    raise RuntimeError(f'No report_anomalies tool_call in response: {json.dumps(resp)[:200]}')


def review_video_gateway(spec_name: str, frames: list[bytes], expected_flow: str | None, model: str) -> VideoVerdict:
    """Route vision review through leartech-ai-gateway (OpenAI seam): image_url
    frames + OpenAI function-calling for guaranteed JSON. Egress at the gateway."""
    import httpx

    body = {
        'model': model,
        'max_tokens': 1024,
        'messages': [
            {'role': 'system', 'content': VIDEO_REVIEW_SYSTEM_PROMPT},
            {'role': 'user', 'content': build_openai_user_message(spec_name, frames, expected_flow)},
        ],
        'tools': [REPORT_ANOMALIES_TOOL_OPENAI],
        'tool_choice': {'type': 'function', 'function': {'name': 'report_anomalies'}},
    }
    r = httpx.post(
        f'{GATEWAY_URL}/v1/chat/completions',
        headers={'Authorization': f'Bearer {GATEWAY_KEY}', 'Content-Type': 'application/json'},
        json=body, timeout=180,
    )
    r.raise_for_status()
    return parse_openai_tool_call(r.json(), spec_name)


def review_video_anthropic(spec_name: str, frames: list[bytes], expected_flow: str | None, model: str) -> VideoVerdict:
    """Direct Anthropic SDK path (laptop mode / no gateway)."""
    from anthropic import Anthropic

    client = Anthropic()
    messages = [{'role': 'user', 'content': build_user_message(spec_name, frames, expected_flow)}]
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=VIDEO_REVIEW_SYSTEM_PROMPT,
        tools=cast(Any, [REPORT_ANOMALIES_TOOL]),
        tool_choice=cast(Any, {'type': 'tool', 'name': 'report_anomalies'}),
        messages=cast(Any, messages),
    )
    blocks = [block.model_dump() for block in response.content]
    return parse_tool_use_response(blocks, spec_name)


def review_video(
    spec_name: str,
    frames: list[bytes],
    expected_flow: str | None = None,
    *,
    model: str | None = None,
) -> VideoVerdict:
    """Send frames for vision + structured review; return the VideoVerdict.

    Routes through leartech-ai-gateway when AI_GATEWAY_URL + AI_GATEWAY_API_KEY are
    set (gpt-4o via the OpenAI seam, egress at the gateway), else the direct
    Anthropic SDK. Gate with check_prerequisites().
    """
    if _gateway_configured():
        return review_video_gateway(spec_name, frames, expected_flow, model or GATEWAY_VISION_MODEL)
    return review_video_anthropic(spec_name, frames, expected_flow, model or DEFAULT_MODEL)
