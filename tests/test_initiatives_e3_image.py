"""Tests for Phase E.3 — per-initiative `image:` override.

E.3 adds an OPTIONAL ``image: str | None`` field to the Initiative model.
When set, ``_pick_image_for_initiative`` returns it verbatim, short-circuiting
language-based routing (E.2), repo auto-detection (E.1), and the
``LEARTECH_INITIATIVE_DEFAULT_IMAGE`` env fallback (D.4.4).

Free-form string — no format validation, because operators may target private
mirrors, digest pins (`@sha256:...`), pinned tags, debug builds, etc.

These tests pin:

1. Parse-time: YAML with ``image: <ref>`` round-trips to ``loaded.image``.
2. Picker contract: ``image_override=...`` short-circuits before any env
   lookup, regardless of the ``language`` argument.
3. Precedence: override wins over both ``language`` and the env default.
4. Normalisation: empty string is treated the same as ``None`` (env applies).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.routers.initiatives import _pick_image_for_initiative
from gate.initiatives.loader import load_initiative


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / 'init.yaml'
    path.write_text(textwrap.dedent(body))
    return path


# ---------------------------------------------------------------------------
# Schema — parsing the `image:` field from YAML
# ---------------------------------------------------------------------------


def test_image_field_set_when_present(tmp_path: Path) -> None:
    """Parsing a YAML with ``image: <ref>`` populates ``loaded.image``."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        image: my.registry.example/foo:bar
        """,
    )
    init = load_initiative(path)
    assert init.image == 'my.registry.example/foo:bar'


def test_image_field_defaults_to_none_when_omitted(tmp_path: Path) -> None:
    """When ``image:`` is absent the field is None (not required)."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        """,
    )
    init = load_initiative(path)
    assert init.image is None


def test_image_field_accepts_digest_pin(tmp_path: Path) -> None:
    """Digest-pinned references (``@sha256:...``) must round-trip without
    format validation — operators use them for reproducible deploys."""
    digest = 'ghcr.io/leartech/agent@sha256:' + 'a' * 64
    path = _write(
        tmp_path,
        f"""
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        image: {digest}
        """,
    )
    init = load_initiative(path)
    assert init.image == digest


def test_image_field_empty_string_treated_as_none(tmp_path: Path) -> None:
    """An empty ``image: ''`` (or whitespace-only) is normalised to None so
    downstream consumers only see one shape for "no image declared"."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        image: ''
        """,
    )
    init = load_initiative(path)
    assert init.image is None


def test_image_field_whitespace_only_treated_as_none(tmp_path: Path) -> None:
    """Whitespace-only ``image: '   '`` normalises to None too."""
    path = _write(
        tmp_path,
        """
        name: x
        repo: leartech-auth-ui
        branch: agent/x
        goal: do a thing
        image: '   '
        """,
    )
    init = load_initiative(path)
    assert init.image is None


# ---------------------------------------------------------------------------
# Image picker — _pick_image_for_initiative with image_override
# ---------------------------------------------------------------------------


def test_image_override_short_circuits_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """E.3: an image_override is returned verbatim, regardless of env default."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/default:1.0')
    assert (
        _pick_image_for_initiative('any-name', image_override='my.registry.example/foo:bar')
        == 'my.registry.example/foo:bar'
    )


def test_image_override_works_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """E.3: image_override means we don't need the env at all — RuntimeError must
    NOT fire if the override is set, even when the default env is missing.

    This is the whole point of the escape hatch: the operator pins an image
    explicitly, so the env-default-required-or-raise contract is bypassed.
    """
    monkeypatch.delenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', raising=False)
    assert (
        _pick_image_for_initiative('any-name', image_override='ghcr.io/foo/pinned:debug') == 'ghcr.io/foo/pinned:debug'
    )


def test_image_override_wins_over_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """E.3: image_override wins over language (E.2) AND env default (D.4.4).

    Today's stub returns the env default for any language value, so this also
    pins the contract that future E.1 routing must respect the override.
    """
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/default:1.0')
    # Even with a known language that E.1 might route differently,
    # the override always wins.
    assert (
        _pick_image_for_initiative(
            'any-name',
            language='angular',
            image_override='ghcr.io/foo/experimental:abc123',
        )
        == 'ghcr.io/foo/experimental:abc123'
    )
    # Same with unknown language.
    assert (
        _pick_image_for_initiative(
            'any-name',
            language='kotlin',
            image_override='ghcr.io/foo/experimental:abc123',
        )
        == 'ghcr.io/foo/experimental:abc123'
    )


def test_image_override_none_falls_through_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """E.3: image_override=None preserves D.4.4 behaviour — env default is used."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/default:1.0')
    assert _pick_image_for_initiative('any-name', image_override=None) == 'ghcr.io/foo/default:1.0'


def test_image_override_empty_string_falls_through_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.3: an empty-string override is treated the same as None — env fallback
    applies. The loader normalises empty YAML values to None, but this also
    guards callers that bypass the loader."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/default:1.0')
    assert _pick_image_for_initiative('any-name', image_override='') == 'ghcr.io/foo/default:1.0'


def test_image_override_whitespace_only_falls_through_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.3: a whitespace-only override does not count as a real override.
    Defensive — the loader already normalises whitespace to None, this pins
    the picker's behaviour for callers that bypass the loader."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/default:1.0')
    assert _pick_image_for_initiative('any-name', image_override='   ') == 'ghcr.io/foo/default:1.0'


def test_image_override_raises_only_when_both_override_and_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.3: the RuntimeError contract still applies when there's truly no image
    source. Empty override + unset env = raise (same as D.4.2 incident guard)."""
    monkeypatch.delenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', raising=False)
    with pytest.raises(RuntimeError, match='LEARTECH_INITIATIVE_DEFAULT_IMAGE'):
        _pick_image_for_initiative('any-name', image_override=None)
    with pytest.raises(RuntimeError, match='LEARTECH_INITIATIVE_DEFAULT_IMAGE'):
        _pick_image_for_initiative('any-name', image_override='')
