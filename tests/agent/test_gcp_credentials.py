"""Runtime GCS-credential bridge (gate.agent.gcp_credentials).

Guards the fix for the artifact-read failure: the controller projects the SA key
as GOOGLE_APPLICATION_CREDENTIALS_JSON; GCP tooling wants a file via
GOOGLE_APPLICATION_CREDENTIALS. The runtime must bridge env→file for every agent.
"""

from __future__ import annotations

import os
from pathlib import Path

from gate.agent.gcp_credentials import materialize_gcp_credentials


def test_materializes_json_to_file(monkeypatch, tmp_path):
    monkeypatch.setattr('gate.agent.gcp_credentials.tempfile.gettempdir', lambda: str(tmp_path))
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS_JSON', '{"type":"service_account","x":1}')
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)

    path = materialize_gcp_credentials()

    assert path is not None
    assert os.environ['GOOGLE_APPLICATION_CREDENTIALS'] == path
    p = Path(path)
    assert p.read_text() == '{"type":"service_account","x":1}'
    assert oct(p.stat().st_mode)[-3:] == '600', 'credential file must be 0600'


def test_noop_when_env_absent(monkeypatch):
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS_JSON', raising=False)
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)

    assert materialize_gcp_credentials() is None
    assert 'GOOGLE_APPLICATION_CREDENTIALS' not in os.environ


def test_noop_when_file_already_configured(monkeypatch):
    # An operator/base image that already mounted a key file must win — don't clobber.
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS_JSON', '{"x":1}')
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', '/pre/existing/key.json')

    assert materialize_gcp_credentials() is None
    assert os.environ['GOOGLE_APPLICATION_CREDENTIALS'] == '/pre/existing/key.json'
