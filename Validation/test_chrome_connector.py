"""A local, matching Chrome companion is required for pairing."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import chrome_connector


def test_pairing_uses_matching_local_companion(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: hermes-connector\nversion: 0.2.6\n", encoding="utf-8")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)
    monkeypatch.setattr(chrome_connector, "ROOT", tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="secret-code\n")

    monkeypatch.setattr(chrome_connector.subprocess, "run", run)
    assert chrome_connector.pairing_code() == "secret-code"
    assert Path(calls[0][0][1]) == plugin / "broker.py"
    assert calls[0][1]["capture_output"]


def test_wrong_companion_version_cannot_pair(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("version: 0.2.4\n", encoding="utf-8")
    monkeypatch.setattr(chrome_connector, "PLUGIN", plugin)
    with pytest.raises(RuntimeError, match="0.2.6"):
        chrome_connector.pairing_code()
