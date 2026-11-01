"""The PC-wide switch must be explicit and restore only its saved adapters."""

from types import SimpleNamespace

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
