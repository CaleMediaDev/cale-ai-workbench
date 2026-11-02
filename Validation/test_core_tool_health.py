"""Core tool health uses runnable checks and repairs only after setup."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core_tool_health


def test_missing_checks_executables_and_ghidra(tmp_path, monkeypatch):
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.write_text("present")
    monkeypatch.setattr(core_tool_health, "_runnable",
                        lambda path, *args: path.name != "gh.exe")
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    assert core_tool_health.missing(tmp_path, fresh=True) == ["GitHub CLI"]
    ghidra.unlink()
    assert core_tool_health.missing(tmp_path, fresh=True) == ["GitHub CLI", "Ghidra"]


def test_browser_runtime_failure_is_visible_even_when_node_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(core_tool_health, "_runnable", lambda path, *args: True)
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: False)
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.write_text("present")
    assert core_tool_health.missing(tmp_path, fresh=True) == ["Local browser runtime"]


def test_background_repair_waits_for_first_install(tmp_path, monkeypatch):
    monkeypatch.setattr(core_tool_health, "_last_repair", None)
    monkeypatch.setattr(core_tool_health, "_repair_thread", None)
    with patch.object(core_tool_health, "missing", side_effect=AssertionError("too early")):
        assert not core_tool_health.start_background(tmp_path)
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench setup pending")
    with patch.object(core_tool_health, "missing", side_effect=AssertionError("too early")):
        assert not core_tool_health.start_background(tmp_path)
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    progress = tmp_path / "LongTasks/install-progress.json"
    progress.parent.mkdir()
    progress.write_text('{"status":"working"}')
    with patch.object(core_tool_health, "missing", side_effect=AssertionError("too early")):
        assert not core_tool_health.start_background(tmp_path)


def test_background_repair_runs_once_and_rechecks(tmp_path, monkeypatch):
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    progress = tmp_path / "LongTasks/install-progress.json"
    progress.parent.mkdir()
    progress.write_text('{"status":"complete"}')
    monkeypatch.setattr(core_tool_health, "_last_repair", None)
    monkeypatch.setattr(core_tool_health, "_repair_thread", None)
    launches = []

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False

    def run(args, **kwargs):
        launches.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(core_tool_health.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(core_tool_health.subprocess, "run", run)
    with patch.object(core_tool_health, "missing", side_effect=[["GitHub CLI"], []]):
        assert core_tool_health.start_background(tmp_path)
    assert launches[0][-2:] == ["-Destination", str(tmp_path)]
    assert json.loads((tmp_path / "LongTasks/core-tool-repair.json").read_text())["status"] == "complete"
    with patch.object(core_tool_health, "missing", side_effect=AssertionError("cooldown")):
        assert not core_tool_health.start_background(tmp_path)
