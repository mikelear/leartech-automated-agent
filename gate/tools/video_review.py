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
from typing import Any

DEFAULT_FRAME_COUNT = 8
DEFAULT_MAX_WIDTH = 800

# Gateway routing (S13 pattern). Vision review ALWAYS routes through
# leartech-ai-gateway (/v1/chat/completions, OpenAI image_url + tool-calling) to a
# vision-capable model — GATEWAY_VISION_MODEL, default the gateway's gpt-4o (logical
# `azure-openai`, proven vision over the OpenAI seam), authed with the dedicated
# `agent-gate` key. Fail-closed: if AI_GATEWAY_URL + AI_GATEWAY_API_KEY are unset,
# review_video errors — there is NO direct-to-Anthropic path (everything via the
# gateway; see Hub/status/ai-client-architecture.md).
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
    # The gateway is the ONLY credential path (fail-closed — no direct Anthropic).
    # Named api_key_present for back-compat; true when the gateway is configured.
    api_key_present: bool

    @property
    def ok(self) -> bool:
        return self.ffmpeg_path is not None and self.api_key_present

    def missing(self) -> list[str]:
        m: list[str] = []
        if self.ffmpeg_path is None:
            m.append('ffmpeg (install: `brew install ffmpeg`)')
        if not self.api_key_present:
            m.append(
                'AI_GATEWAY_URL + AI_GATEWAY_API_KEY (vision review is gateway-only; '
                'point AI_GATEWAY_URL at the ai-gateway — no direct-Anthropic path)'
            )
        return m


def check_prerequisites() -> Prerequisites:
    # Gateway-only: without it the criterion is skipped rather than falling back to
    # a direct provider (everything goes through the ai-gateway).
    return Prerequisites(
        ffmpeg_path=shutil.which('ffmpeg'),
        api_key_present=_gateway_configured(),
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


def build_openai_user_message(spec_name: str, frames: list[bytes], expected_flow: str | None) -> list[dict[str, Any]]:
    """OpenAI multimodal content — text intro + frames as image_url data URIs (gateway path)."""
    blocks: list[dict[str, Any]] = [{'type': 'text', 'text': _intro_text(spec_name, frames, expected_flow)}]
    for frame in frames:
        b64 = base64.standard_b64encode(frame).decode('ascii')
        blocks.append({'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}})
    return blocks


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
        json=body,
        timeout=180,
    )
    r.raise_for_status()
    return parse_openai_tool_call(r.json(), spec_name)


def review_video(
    spec_name: str,
    frames: list[bytes],
    expected_flow: str | None = None,
    *,
    model: str | None = None,
) -> VideoVerdict:
    """Send frames for vision + structured review; return the VideoVerdict.

    Routes ALWAYS through leartech-ai-gateway (gpt-4o via the OpenAI seam, egress at
    the gateway, authed with the `agent-gate` key). Fail-closed: if the gateway is
    not configured (AI_GATEWAY_URL + AI_GATEWAY_API_KEY) this raises — there is no
    direct-to-provider path. Gate with check_prerequisites() to skip cleanly.
    """
    if not _gateway_configured():
        raise RuntimeError(
            'video review requires the ai-gateway (AI_GATEWAY_URL + AI_GATEWAY_API_KEY); '
            'there is no direct-Anthropic path — everything routes through the gateway'
        )
    return review_video_gateway(spec_name, frames, expected_flow, model or GATEWAY_VISION_MODEL)
