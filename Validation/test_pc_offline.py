"""The PC-wide switch must be explicit and restore only its saved adapters."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pc_offline


def test_toggle_requests_elevation_and_changes_mode_from_record(tmp_path, monkeypatch):
    calls = []
    shell = SimpleNamespace(ShellExecuteW=lambda *args: calls.append(args) or 42)
    monkeypatch.setattr(pc_offline, "ctypes", SimpleNamespace(windll=SimpleNamespace(shell32=shell)))
    monkeypatch.setattr(pc_offline, "STATE", tmp_path / "offline-network.json")
    monkeypatch.setattr(pc_offline, "SCRIPT", tmp_path / "pc_offline.ps1")
    assert pc_offline.toggle() == "offline"
    assert "-Mode offline" in calls[-1][3]
    pc_offline.STATE.write_text("{}", encoding="utf-8")
    assert pc_offline.toggle() == "online"
    assert "-Mode online" in calls[-1][3]


def test_offline_status_requires_completed_switch_and_no_active_physical_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(pc_offline, "STATE", tmp_path / "offline-network.json")
    assert pc_offline.status() == "online"
    pc_offline.STATE.write_text(json.dumps({"names": ["Ethernet"], "phase": "switching"}))
    with patch.object(pc_offline.subprocess, "run") as run:
        assert pc_offline.status() == "unverified"
        run.assert_not_called()
    pc_offline.STATE.write_text(json.dumps({"names": ["Ethernet"], "phase": "offline"}))
    with patch.object(pc_offline.subprocess, "run", return_value=SimpleNamespace(
        returncode=0, stdout='["Ethernet"]'
    )):
        assert pc_offline.status() == "unverified"
    with patch.object(pc_offline.subprocess, "run", return_value=SimpleNamespace(
        returncode=0, stdout="[]"
    )):
        assert pc_offline.status() == "physical_off"
    # Even an unverified switch keeps the router on local models only.
    assert pc_offline.offline()
