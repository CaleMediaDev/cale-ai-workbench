"""Core tool health uses runnable checks and repairs only after setup."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core_tool_health
import pytest


@pytest.fixture(autouse=True)
def healthy_agent_facts(tmp_path):
    facts = tmp_path / "data/installs/current/facts.json"
    facts.parent.mkdir(parents=True)
    facts.write_text(json.dumps({"packages": {"venv": {
        "environment": str(tmp_path / "data/agent-venv")
    }}}), encoding="utf-8")
    return facts


@pytest.mark.parametrize("record", [None, "{invalid json"])
def test_missing_agent_facts_cannot_cache_ready_health(
        tmp_path, monkeypatch, healthy_agent_facts, record):
    if record is None:
        healthy_agent_facts.unlink()
    else:
        healthy_agent_facts.write_text(record, encoding="utf-8")
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.touch()
    monkeypatch.setattr(core_tool_health, "_health_cache", (None, (), 0.0))
    monkeypatch.setattr(core_tool_health, "_runnable", lambda path, *args: True)
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)

    assert core_tool_health.missing(tmp_path, fresh=True) == ["Agent model SDK"]
    with patch.object(core_tool_health, "_runnable",
                      side_effect=AssertionError("expected cached health")):
        assert core_tool_health.missing(tmp_path) == ["Agent model SDK"]


@pytest.mark.parametrize("environment", ["", "   "])
def test_empty_agent_environment_cannot_cache_ready_health(
        tmp_path, monkeypatch, healthy_agent_facts, environment):
    healthy_agent_facts.write_text(json.dumps({"packages": {"venv": {
        "environment": environment
    }}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.touch()
    monkeypatch.setattr(core_tool_health, "_health_cache", (None, (), 0.0))
    # Even if an unrelated interpreter would pass, invalid install facts must
    # not identify it as the managed agent environment.
    monkeypatch.setattr(core_tool_health, "_runnable", lambda path, *args: True)
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)

    assert core_tool_health._agent_python(tmp_path) is None
    assert core_tool_health.missing(tmp_path, fresh=True) == ["Agent model SDK"]
    with patch.object(core_tool_health, "_runnable",
                      side_effect=AssertionError("expected cached health")):
        assert core_tool_health.missing(tmp_path) == ["Agent model SDK"]


@pytest.mark.parametrize("environment", [".", "data/agent-venv"])
def test_relative_agent_environment_cannot_cache_ready_health(
        tmp_path, monkeypatch, healthy_agent_facts, environment):
    healthy_agent_facts.write_text(json.dumps({"packages": {"venv": {
        "environment": environment
    }}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    unrelated_python = Path(environment) / "Scripts/python.exe"
    unrelated_python.parent.mkdir(parents=True, exist_ok=True)
    unrelated_python.touch()
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.touch()
    monkeypatch.setattr(core_tool_health, "_health_cache", (None, (), 0.0))
    monkeypatch.setattr(core_tool_health, "_runnable", lambda path, *args: True)
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)

    assert core_tool_health._agent_python(tmp_path) is None
    assert core_tool_health.missing(tmp_path, fresh=True) == ["Agent model SDK"]
    with patch.object(core_tool_health, "_runnable",
                      side_effect=AssertionError("expected cached health")):
        assert core_tool_health.missing(tmp_path) == ["Agent model SDK"]


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


def test_health_cache_distinguishes_relative_roots_after_chdir(tmp_path, monkeypatch):
    healthy = tmp_path / "healthy/installation"
    broken = tmp_path / "broken/installation"
    for root in (healthy, broken):
        ghidra = root / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
        ghidra.parent.mkdir(parents=True)
        ghidra.touch()

    monkeypatch.setattr(core_tool_health, "_health_cache", (None, (), 0.0))
    monkeypatch.setattr(core_tool_health.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(core_tool_health, "_agent_python",
                        lambda root: root / "data/agent-venv/Scripts/python.exe")
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)
    broken_gh = broken / "Toolbox/gh_2.103.3_windows_amd64/bin/gh.exe"
    monkeypatch.setattr(core_tool_health, "_runnable",
                        lambda path, *args: path != broken_gh)

    monkeypatch.chdir(healthy.parent)
    assert core_tool_health.missing(Path("installation")) == []
    monkeypatch.chdir(broken.parent)
    assert core_tool_health.missing(Path("installation")) == ["GitHub CLI"]


def test_cache_lookup_keeps_results_from_its_own_installation(tmp_path, monkeypatch):
    broken = tmp_path / "broken"
    healthy = tmp_path / "healthy"
    for root in (broken, healthy):
        ghidra = root / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
        ghidra.parent.mkdir(parents=True)
        ghidra.touch()

    monkeypatch.setattr(core_tool_health, "_health_cache", (None, (), 0.0))
    monkeypatch.setattr(core_tool_health.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(core_tool_health, "_agent_python",
                        lambda root: root / "data/agent-venv/Scripts/python.exe")
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    broken_gh = broken / "Toolbox/gh_2.103.3_windows_amd64/bin/gh.exe"
    monkeypatch.setattr(core_tool_health, "_runnable",
                        lambda path, *args: path != broken_gh)
    assert core_tool_health.missing(broken, fresh=True) == ["GitHub CLI"]

    publications = []

    def interleaved_clock():
        # Publish another root's results after cache eligibility has begun,
        # as an overlapping probe could do. Avoid recursion in its timestamp.
        monkeypatch.setattr(core_tool_health.time, "monotonic", lambda: 1000.0)
        publications.append(core_tool_health.missing(healthy, fresh=True))
        return 1000.0

    monkeypatch.setattr(core_tool_health.time, "monotonic", interleaved_clock)
    assert core_tool_health.missing(broken) == ["GitHub CLI"]
    assert publications == [[]]
    assert core_tool_health.missing(healthy) == []


def test_older_probe_cannot_overwrite_newer_healthy_cache(tmp_path, monkeypatch):
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.touch()
    monkeypatch.setattr(core_tool_health, "_health_cache", (None, (), 0.0))
    monkeypatch.setattr(core_tool_health, "_health_generation", 0)
    monkeypatch.setattr(core_tool_health.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(core_tool_health, "_agent_python",
                        lambda root: root / "data/agent-venv/Scripts/python.exe")
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)
    interleaved = []

    def runnable(path, *args):
        if path.name == "gh.exe" and not interleaved:
            # The older probe observed failure before a newer probe observed
            # recovery, but resumes and publishes only after the newer one.
            interleaved.append(True)
            assert core_tool_health.missing(tmp_path, fresh=True) == []
            return False
        return True

    monkeypatch.setattr(core_tool_health, "_runnable", runnable)
    assert core_tool_health.missing(tmp_path, fresh=True) == ["GitHub CLI"]
    assert interleaved == [True]
    with patch.object(core_tool_health, "_runnable",
                      side_effect=AssertionError("expected cached health")):
        assert core_tool_health.missing(tmp_path) == []


def test_slow_probe_does_not_extend_cached_failure_lifetime(tmp_path, monkeypatch):
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.touch()
    monkeypatch.setattr(core_tool_health, "_health_cache", (None, (), 0.0))
    monkeypatch.setattr(core_tool_health, "_health_generation", 0)
    clock = [1000.0]
    monkeypatch.setattr(core_tool_health.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(core_tool_health, "_agent_python",
                        lambda root: root / "data/agent-venv/Scripts/python.exe")
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)
    node_checks = []

    def runnable(path, *args):
        if path.name == "node.exe":
            node_checks.append(path)
            return len(node_checks) > 1
        if path.name == "gh.exe" and len(node_checks) == 1:
            # Node recovers while a subsequent dependency probe is slow.
            clock[0] += core_tool_health.CHECK_INTERVAL / 2
        return True

    monkeypatch.setattr(core_tool_health, "_runnable", runnable)
    assert core_tool_health.missing(tmp_path, fresh=True) == ["Node.js"]
    assert core_tool_health.missing(tmp_path) == ["Node.js"]
    assert len(node_checks) == 1

    clock[0] = 1000.0 + core_tool_health.CHECK_INTERVAL
    assert core_tool_health.missing(tmp_path) == []
    assert len(node_checks) == 2


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
    facts.parent.mkdir(parents=True, exist_ok=True)
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
    assert (toolbox, ("-I", "-c", core_tool_health.TOOLBOX_SDK_PROBE)) in checked


def test_sdk_health_ignores_unrelated_python_modules(tmp_path, monkeypatch):
    import sys

    # This unrelated package would satisfy both SDK imports through either
    # the desktop working directory or inherited PYTHONPATH.
    package = tmp_path / "openai"
    for directory in (package, package / "types", package / "types/shared_params"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").touch()
    (package / "types/shared_params/response_format_json_object.py").write_text(
        "class ResponseFormatJSONObject: pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    ghidra = tmp_path / "Toolbox/ghidra_12.1.4_PUBLIC/ghidraRun.bat"
    ghidra.parent.mkdir(parents=True)
    ghidra.touch()
    monkeypatch.setattr(core_tool_health, "_health_cache", (None, (), 0.0))
    monkeypatch.setattr(core_tool_health, "_browser_works", lambda root, node: True)
    monkeypatch.setattr(core_tool_health, "_agent_launcher_works", lambda root: True)
    runnable = core_tool_health._runnable
    sdk_probes = []

    def probe(path, *args):
        if "-c" in args:
            sdk_probes.append(path)
            # Exclude the test runner's installed packages, so this test does
            # not depend on whether its interpreter has openai installed.
            return runnable(Path(sys.executable), "-S", *args)
        return True

    monkeypatch.setattr(core_tool_health, "_runnable", probe)
    expected = ["Agent model SDK", "Toolbox model SDK"]
    assert core_tool_health.missing(tmp_path, fresh=True) == expected
    assert len(sdk_probes) == 2
    with patch.object(core_tool_health, "_runnable",
                      side_effect=AssertionError("expected cached health")):
        assert core_tool_health.missing(tmp_path) == expected


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


def test_long_repair_timeout_starts_cooldown_at_completion(tmp_path, monkeypatch):
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    monkeypatch.setattr(core_tool_health, "_last_repair", None)
    monkeypatch.setattr(core_tool_health, "_repair_thread", None)
    clock = [1000.0]
    monkeypatch.setattr(core_tool_health.time, "monotonic", lambda: clock[0])

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False

    def timeout(args, **kwargs):
        clock[0] += 901
        raise core_tool_health.subprocess.TimeoutExpired(args, 900)

    monkeypatch.setattr(core_tool_health.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(core_tool_health.subprocess, "run", timeout)
    with patch.object(core_tool_health, "missing", return_value=["GitHub CLI"]):
        assert core_tool_health.start_background(tmp_path)
    assert json.loads((tmp_path / "LongTasks/core-tool-repair.json").read_text())["status"] == "failed"

    with patch.object(core_tool_health, "missing", side_effect=AssertionError("cooldown")):
        assert not core_tool_health.start_background(tmp_path)
        clock[0] += core_tool_health.CHECK_INTERVAL - 1
        assert not core_tool_health.start_background(tmp_path)

    clock[0] += 1
    with patch.object(core_tool_health, "missing", return_value=[]) as missing:
        assert not core_tool_health.start_background(tmp_path)
        missing.assert_called_once_with(tmp_path)


def test_initial_status_failure_starts_cooldown_at_completion(tmp_path, monkeypatch):
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    monkeypatch.setattr(core_tool_health, "_last_repair", None)
    monkeypatch.setattr(core_tool_health, "_repair_thread", None)
    clock = [1000.0]
    monkeypatch.setattr(core_tool_health.time, "monotonic", lambda: clock[0])
    statuses = []

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False

    def status(root, state, detail):
        statuses.append(state)
        if state == "working":
            # A stalled filesystem write can consume the admission cooldown.
            clock[0] += core_tool_health.CHECK_INTERVAL + 1
            raise OSError("status storage unavailable")

    monkeypatch.setattr(core_tool_health.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(core_tool_health, "_status", status)
    with patch.object(core_tool_health.subprocess, "run",
                      side_effect=AssertionError("must not execute repairs")):
        with patch.object(core_tool_health, "missing", return_value=["GitHub CLI"]):
            assert core_tool_health.start_background(tmp_path)
        assert statuses == ["working", "failed"]
        assert core_tool_health._last_repair == clock[0]
        with patch.object(core_tool_health, "missing",
                          side_effect=AssertionError("cooldown")):
            assert not core_tool_health.start_background(tmp_path)
            clock[0] += core_tool_health.CHECK_INTERVAL - 1
            assert not core_tool_health.start_background(tmp_path)

    clock[0] += 1
    with patch.object(core_tool_health, "missing", return_value=[]) as missing:
        assert not core_tool_health.start_background(tmp_path)
        missing.assert_called_once_with(tmp_path)


def test_overlapping_background_checks_launch_only_one_repair(tmp_path, monkeypatch):
    from threading import Event, Thread

    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    monkeypatch.setattr(core_tool_health, "_last_repair", None)
    monkeypatch.setattr(core_tool_health, "_repair_thread", None)
    probing = Event()
    release_probe = Event()
    launches = []
    results = []
    errors = []
    checks = []

    def missing(root):
        checks.append(root)
        if len(checks) == 1:
            probing.set()
            if not release_probe.wait(5):
                raise AssertionError("probe was not released")
        return ["GitHub CLI"]

    class InertRepairThread:
        def __init__(self, target, **kwargs):
            pass

        def start(self):
            launches.append(self)

        def is_alive(self):
            return True

    def first_call():
        try:
            results.append(core_tool_health.start_background(tmp_path))
        except Exception as exc:
            errors.append(exc)

    caller = Thread(target=first_call)
    monkeypatch.setattr(core_tool_health, "missing", missing)
    monkeypatch.setattr(core_tool_health.threading, "Thread", InertRepairThread)
    caller.start()
    try:
        assert probing.wait(5)
        assert not core_tool_health.start_background(tmp_path)
    finally:
        release_probe.set()
        caller.join(5)

    assert not caller.is_alive()
    assert errors == []
    assert results == [True]
    assert len(checks) == 1
    assert len(launches) == 1


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
