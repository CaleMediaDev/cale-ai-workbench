"""Existing installations add new local dependencies once, in the background."""

import json
import time

import runtime_features


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


def test_verified_tools_clear_stale_failed_setup(tmp_path, monkeypatch):
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({"status": "failed", "current": "pip missing", "owner": "runtime_features"}), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ready", lambda: True)
    assert not runtime_features.start_background()
    assert json.loads(progress.read_text(encoding="utf-8"))["status"] == "complete"


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
