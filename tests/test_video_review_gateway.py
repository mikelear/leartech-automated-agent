"""Unit tests for the gateway (OpenAI-seam) path of gate.tools.video_review —
the S13 migration. Pure/mocked; the live vision call is exercised by the gate run.
"""

from __future__ import annotations

import pytest

import gate.tools.video_review as vr
from gate.tools.video_review import (
    REPORT_ANOMALIES_TOOL_OPENAI,
    build_openai_user_message,
    parse_openai_tool_call,
)


def test_build_openai_user_message_uses_image_url_blocks():
    blocks = build_openai_user_message("login.spec.ts", [b"PNGBYTES", b"MORE"], "log in then see dashboard")
    assert blocks[0]["type"] == "text" and "login.spec.ts" in blocks[0]["text"]
    imgs = [b for b in blocks if b["type"] == "image_url"]
    assert len(imgs) == 2
    assert imgs[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_tool_schema_reuses_json_schema():
    fn = REPORT_ANOMALIES_TOOL_OPENAI["function"]
    assert REPORT_ANOMALIES_TOOL_OPENAI["type"] == "function"
    assert fn["name"] == "report_anomalies"
    assert set(fn["parameters"]["required"]) == {"anomalies_found", "summary", "flagged_frames"}


def test_parse_openai_tool_call():
    resp = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "report_anomalies",
                      "arguments": '{"anomalies_found": true, "summary": "blank page persisted", "flagged_frames": [2, 3]}'}}]}}]}
    v = parse_openai_tool_call(resp, "spec")
    assert v.anomalies_found and not v.passed
    assert v.summary == "blank page persisted" and v.flagged_frames == (2, 3)


def test_parse_openai_tool_call_missing_raises():
    with pytest.raises(RuntimeError):
        parse_openai_tool_call({"choices": [{"message": {}}]}, "spec")


def test_review_video_routes_to_gateway_when_configured(monkeypatch):
    monkeypatch.setattr(vr, "GATEWAY_URL", "http://gw")
    monkeypatch.setattr(vr, "GATEWAY_KEY", "sk-lt-x")
    captured = {}

    def fake_gateway(spec, frames, flow, model):
        captured["model"] = model
        return vr.VideoVerdict(spec_name=spec, anomalies_found=False, summary="ok", flagged_frames=())

    monkeypatch.setattr(vr, "review_video_gateway", fake_gateway)
    v = vr.review_video("spec", [b"x"])
    assert captured["model"] == vr.GATEWAY_VISION_MODEL and v.passed


def test_check_prerequisites_gateway_counts_as_credentials(monkeypatch):
    monkeypatch.setattr(vr, "GATEWAY_URL", "http://gw")
    monkeypatch.setattr(vr, "GATEWAY_KEY", "sk-lt-x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # in-cluster: no ANTHROPIC_API_KEY, but the gateway is configured → credentials present
    assert vr.check_prerequisites().api_key_present
