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
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)
    assert core_tool_health.missing(tmp_path, fresh=True) == ["GitHub CLI"]
    ghidra.unlink()
    assert core_tool_health.missing(tmp_path, fresh=True) == ["GitHub CLI", "Ghidra"]


def test_browser_runtime_failure_is_visible_even_when_node_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(core_tool_health, "_runnable", lambda path, *args: True)
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: False)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.write_text("present")
    assert core_tool_health.missing(tmp_path, fresh=True) == ["Local browser runtime"]


def test_agent_sdk_probe_uses_managed_install_facts(tmp_path, monkeypatch):
    facts = tmp_path / "data/installs/current/facts.json"
    facts.parent.mkdir(parents=True)
    agent_env = tmp_path / "data/agent-venv"
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(agent_env)}}}))
    assert core_tool_health._agent_python(tmp_path) == agent_env / "Scripts/python.exe"
    monkeypatch.setattr(core_tool_health, "_runnable",
                        lambda path, *args: "ResponseFormatJSONObject" not in " ".join(args))
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.touch()
    assert "Agent model SDK" in core_tool_health.missing(tmp_path, fresh=True)


def test_toolbox_sdk_probe_catches_broken_music_runtime(tmp_path, monkeypatch):
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.touch()
    toolbox = tmp_path / "Toolbox/python/Scripts/python.exe"
    checked = []

    def runnable(path, *args):
        checked.append((path, args))
        return path != toolbox

    monkeypatch.setattr(core_tool_health, "_runnable", runnable)
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)
    assert core_tool_health.missing(tmp_path, fresh=True) == ["Toolbox model SDK"]
    assert (toolbox, ("-c", core_tool_health.TOOLBOX_SDK_PROBE)) in checked


def test_agent_launcher_probe_uses_cale_home_and_reports_failure(tmp_path, monkeypatch):
    launcher = tmp_path / "data/bin/hermes.exe"
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    seen = {}

    def run(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(core_tool_health.subprocess, "run", run)
    assert not core_tool_health._agent_launcher_works(tmp_path)
    assert seen["args"] == [str(launcher), "--help"]
    assert seen["kwargs"]["env"]["HERMES_HOME"] == str(tmp_path / "data")
    monkeypatch.setattr(core_tool_health, "_runnable", lambda path, *args: True)
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.touch()
    assert core_tool_health.missing(tmp_path, fresh=True) == ["Agent launcher"]


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


def test_background_repair_ignores_non_object_progress(tmp_path, monkeypatch):
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    progress = tmp_path / "LongTasks/install-progress.json"
    progress.parent.mkdir()
    monkeypatch.setattr(core_tool_health, "_last_repair", None)
    monkeypatch.setattr(core_tool_health, "_repair_thread", None)

    for record in (None, [], "complete", 1):
        progress.write_text(json.dumps(record), encoding="utf-8")
        with patch.object(core_tool_health, "missing", return_value=[]) as missing:
            assert not core_tool_health.start_background(tmp_path)
            missing.assert_called_once_with(tmp_path)


def test_background_repair_resolves_relative_installation_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "installation"
    root.mkdir()
    (root / ".cale-ai-install").write_text("Cale AI Workbench 1")
    script = root / "Repair-CoreTools.ps1"
    script.touch()
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
        launches.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(core_tool_health.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(core_tool_health.subprocess, "run", run)
    with patch.object(core_tool_health, "missing", side_effect=[["GitHub CLI"], []]):
        assert core_tool_health.start_background(Path("installation"))

    assert len(launches) == 1
    args, kwargs = launches[0]
    assert kwargs["cwd"] == root
    assert Path(args[args.index("-File") + 1]) == script
    assert args[-2:] == ["-Destination", str(root)]
    assert json.loads((root / "LongTasks/core-tool-repair.json").read_text())["status"] == "complete"


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
