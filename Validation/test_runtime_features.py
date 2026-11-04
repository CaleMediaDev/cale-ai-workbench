"""Existing installations add new local dependencies once, in the background."""

import json
import time

import pytest

import runtime_features


@pytest.mark.parametrize("previous", [None, [], "invalid metadata", 42])
def test_progress_recovers_from_non_object_json(tmp_path, monkeypatch, previous):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)

    runtime_features.progress(8, "Repairing the local Python package installer")

    current = json.loads(progress.read_text(encoding="utf-8"))
    assert current["attempts"] == 0
    assert current["percent"] == 8
    assert current["owner"] == "runtime_features"


@pytest.mark.parametrize("attempts", [float("inf"), float("-inf")])
def test_progress_recovers_from_infinite_attempt_count(tmp_path, monkeypatch, attempts):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({"attempts": attempts}), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)

    runtime_features.progress(0, "Tool update needs attention: installation failed", "failed")

    current = json.loads(progress.read_text(encoding="utf-8"))
    assert current["attempts"] == 0
    assert current["status"] == "failed"
    assert current["current"] == "Tool update needs attention: installation failed"
    assert current["owner"] == "runtime_features"


@pytest.mark.parametrize("previous", [None, [], "invalid metadata", 42])
def test_background_setup_recovers_from_non_object_json(tmp_path, monkeypatch, previous):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    launches = []
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))

    assert runtime_features.start_background()

    assert len(launches) == 1
    current = json.loads(progress.read_text(encoding="utf-8"))
    assert current["status"] == "working"
    assert current["attempts"] == 1
    assert current["owner"] == "runtime_features"


@pytest.mark.parametrize("attempts", [None, "invalid", [], {}])
def test_background_setup_recovers_from_invalid_attempt_count(tmp_path, monkeypatch, attempts):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({
        "status": "failed", "updated": 0,
        "attempts": attempts, "owner": "runtime_features",
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    launches = []
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))

    assert runtime_features.start_background()

    assert len(launches) == 1
    current = json.loads(progress.read_text(encoding="utf-8"))
    assert current["status"] == "working"
    assert current["attempts"] == 1
    assert current["owner"] == "runtime_features"


def test_ready_requires_marker_package_and_matching_companion(tmp_path, monkeypatch):
    marker = tmp_path / "features.done"
    monkeypatch.setattr(runtime_features, "MARKER", marker)
    monkeypatch.setattr(runtime_features.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_features.chrome_connector, "installed_version", lambda: "0.2.6")
    assert not runtime_features.ready()
    marker.write_text("ready", encoding="utf-8")
    assert runtime_features.ready()
    monkeypatch.setattr(runtime_features.chrome_connector, "installed_version", lambda: "0.2.4")
    assert not runtime_features.ready()


def test_background_update_is_not_started_twice(tmp_path, monkeypatch):
    marker = tmp_path / "features.done"
    progress = tmp_path / "install-progress.json"
    monkeypatch.setattr(runtime_features, "MARKER", marker)
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    launches = []
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))
    assert runtime_features.start_background()
    assert json.loads(progress.read_text(encoding="utf-8"))["status"] == "working"
    assert not runtime_features.start_background()
    assert len(launches) == 1


def test_background_launch_failure_records_failed_progress_and_can_retry(tmp_path, monkeypatch):
    progress = tmp_path / "install-progress.json"
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    monkeypatch.setattr(runtime_features.time, "time", lambda: 1000)
    launches = []

    def launch(*args, **kwargs):
        launches.append(args)
        if len(launches) == 1:
            raise OSError("Unable to launch installer")

    monkeypatch.setattr(runtime_features.subprocess, "Popen", launch)

    with pytest.raises(OSError, match="Unable to launch installer"):
        runtime_features.start_background()

    current = json.loads(progress.read_text(encoding="utf-8"))
    assert current["status"] == "failed"
    assert current["percent"] == 0
    assert current["attempts"] == 1
    assert current["owner"] == "runtime_features"
    assert current["current"] == "Tool update needs attention: Unable to launch installer"
    assert not runtime_features.start_background()
    assert len(launches) == 1

    monkeypatch.setattr(runtime_features.time, "time", lambda: 1031)
    assert runtime_features.start_background()
    assert len(launches) == 2
    current = json.loads(progress.read_text(encoding="utf-8"))
    assert current["status"] == "working"
    assert current["attempts"] == 2


def test_verified_tools_clear_stale_failed_setup(tmp_path, monkeypatch):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({"status": "failed", "current": "pip missing", "owner": "runtime_features"}), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ready", lambda: True)
    assert not runtime_features.start_background()
    assert json.loads(progress.read_text(encoding="utf-8"))["status"] == "complete"


def test_verified_tools_clear_stale_working_setup(tmp_path, monkeypatch):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({"status": "working", "current": "Installing tools", "owner": "runtime_features"}), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ready", lambda: True)
    assert not runtime_features.start_background()
    assert json.loads(progress.read_text(encoding="utf-8"))["status"] == "complete"


def test_dead_feature_installer_is_restarted_after_short_delay(tmp_path, monkeypatch):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({
        "status": "working", "updated": time.time() - 91,
        "attempts": 1, "owner": "runtime_features", "pid": 12345,
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    monkeypatch.setattr(runtime_features, "_installer_running", lambda pid: False)
    launches = []
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))
    assert runtime_features.start_background()
    assert len(launches) == 1
    assert json.loads(progress.read_text(encoding="utf-8"))["attempts"] == 2


@pytest.mark.parametrize("pid", [float("inf"), float("-inf")])
def test_background_setup_recovers_from_overflowing_pid(tmp_path, monkeypatch, pid):
    import psutil

    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({
        "status": "working", "updated": 900,
        "attempts": 1, "owner": "runtime_features", "pid": pid,
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    monkeypatch.setattr(runtime_features.time, "time", lambda: 1000)
    monkeypatch.setattr(psutil, "Process", lambda *args: pytest.fail("Invalid PID must not reach process lookup"))
    launches = []
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))

    assert runtime_features.start_background()

    assert len(launches) == 1
    current = json.loads(progress.read_text(encoding="utf-8"))
    assert current["status"] == "working"
    assert current["attempts"] == 2


def test_live_feature_installer_is_not_restarted(tmp_path, monkeypatch):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({
        "status": "working", "updated": time.time() - 3600,
        "attempts": 1, "owner": "runtime_features", "pid": 12345,
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    monkeypatch.setattr(runtime_features, "_installer_running", lambda pid: True)
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: 1 / 0)
    assert not runtime_features.start_background()


@pytest.mark.parametrize("matching_script", [True, False])
def test_installer_detection_uses_process_working_directory(tmp_path, monkeypatch, matching_script):
    import psutil

    script = runtime_features.Path(runtime_features.__file__).resolve()
    installer_cwd = script.parent if matching_script else tmp_path

    class InstallerProcess:
        def cmdline(self):
            return ["python", script.name, "--install"]

        def cwd(self):
            return str(installer_cwd)

    monkeypatch.setattr(psutil, "Process", lambda pid: InstallerProcess())
    # The checker and installer need not share a working directory.
    monkeypatch.chdir(tmp_path if matching_script else script.parent)

    assert runtime_features._installer_running(12345) is matching_script


def test_missing_pip_is_bootstrapped(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_features, "PROGRESS", tmp_path / "install-progress.json")
    calls = []

    def run(args, **_):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("No module named pip")

    monkeypatch.setattr(runtime_features, "_run", run)
    runtime_features._ensure_pip("python.exe")
    assert [args[2] for args in calls] == ["pip", "ensurepip", "pip"]


@pytest.mark.parametrize("updated", [None, "invalid timestamp", [], {}, float("nan"), float("inf")])
def test_failed_setup_recovers_from_invalid_timestamp(tmp_path, monkeypatch, updated):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({
        "status": "failed", "updated": updated,
        "attempts": 1, "owner": "runtime_features",
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    monkeypatch.setattr(runtime_features.time, "time", lambda: 1000)
    launches = []
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))

    assert runtime_features.start_background()

    assert len(launches) == 1
    current = json.loads(progress.read_text(encoding="utf-8"))
    assert current["status"] == "working"
    assert current["attempts"] == 2
    assert current["updated"] == 1000


@pytest.mark.parametrize("status", ["failed", "working"])
def test_setup_recovers_from_future_timestamp(tmp_path, monkeypatch, status):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({
        "status": status, "updated": 1000000,
        "attempts": 1, "owner": "runtime_features", "pid": 12345,
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    monkeypatch.setattr(runtime_features.time, "time", lambda: 1000)
    monkeypatch.setattr(runtime_features, "_installer_running", lambda pid: False)
    launches = []
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))

    assert runtime_features.start_background()

    assert len(launches) == 1
    current = json.loads(progress.read_text(encoding="utf-8"))
    assert current["status"] == "working"
    assert current["attempts"] == 2
    assert current["updated"] == 1000


def test_failed_setup_retries_with_backoff_and_limit(tmp_path, monkeypatch):
    progress = tmp_path / "install-progress.json"
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    launches = []
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))
    progress.write_text(json.dumps({"status": "failed", "updated": time.time(), "attempts": 1, "owner": "runtime_features"}))
    assert not runtime_features.start_background()
    assert not launches
    progress.write_text(json.dumps({"status": "failed", "updated": time.time() - 31, "attempts": 1, "owner": "runtime_features"}))
    assert runtime_features.start_background()
    assert json.loads(progress.read_text(encoding="utf-8"))["attempts"] == 2
    assert len(launches) == 1
    progress.write_text(json.dumps({"status": "failed", "updated": time.time() - 100, "attempts": 3, "owner": "runtime_features"}))
    assert not runtime_features.start_background()
    assert len(launches) == 1


def test_base_installer_progress_is_not_overwritten(tmp_path, monkeypatch):
    progress = tmp_path / "install-progress.json"
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ready", lambda: True)
    progress.write_text(json.dumps({"status": "working", "current": "Downloading model files"}))
    assert not runtime_features.start_background()
    assert json.loads(progress.read_text(encoding="utf-8"))["status"] == "working"
