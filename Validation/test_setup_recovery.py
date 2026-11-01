"""A stopped first-run setup retries without interrupting a running worker."""

import json
import time

import setup_recovery


def test_base_setup_retries_with_backoff_and_limit(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress = state / "install-progress.json"
    progress.write_text(json.dumps({
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": time.time() - 1000,
    }), encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    launches = []
    monkeypatch.setattr(setup_recovery.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))
    assert setup_recovery.recover()
    assert len(launches) == 1
    assert "-NoLaunch" in launches[0][0]
    assert not setup_recovery.recover()
    record = state / "base-install-repair.json"
    for attempt in (2, 3):
        record.write_text(json.dumps({"attempts": attempt - 1, "started": time.time() - 1000}))
        assert setup_recovery.recover()
    assert not setup_recovery.recover()
    assert len(launches) == 3


def test_base_setup_repair_ignores_active_or_feature_setup(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress = state / "install-progress.json"
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery.subprocess, "Popen", lambda *args, **kwargs: 1 / 0)
    for status, current in (("working", "Downloading your main AI model"),
                            ("failed", "Tool update needs attention: pip missing")):
        progress.write_text(json.dumps({"status": status, "current": current}))
        assert not setup_recovery.recover()


def test_success_clears_retry_count(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (state / "install-progress.json").write_text('{"status":"complete"}')
    record = state / "base-install-repair.json"
    record.write_text('{"attempts":3}')
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    assert not setup_recovery.recover()
    assert not record.exists()


def test_stopped_installer_turns_stale_work_into_retry(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress_path = state / "install-progress.json"
    progress_path.write_text(json.dumps({
        "status": "working", "current": "Downloading the AI model",
        "updated": time.time() - setup_recovery.STALE_SECONDS - 30,
        "pid": 12345,
    }), encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    launches = []
    monkeypatch.setattr(setup_recovery.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))
    assert not setup_recovery.recover()  # Show the failure, then honor the retry backoff.
    assert json.loads(progress_path.read_text())["status"] == "error"
    progress = json.loads(progress_path.read_text())
    progress["updated"] = time.time() - 60
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    assert setup_recovery.recover()
    assert len(launches) == 1


def test_slow_live_installer_is_not_restarted(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    (state / "install-progress.json").write_text(json.dumps({
        "status": "working", "current": "Downloading the AI model",
        "updated": time.time() - 3600,
    }), encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: True)
    monkeypatch.setattr(setup_recovery.subprocess, "Popen", lambda *args, **kwargs: 1 / 0)
    assert not setup_recovery.recover()
    assert json.loads((state / "install-progress.json").read_text())["status"] == "working"


def test_slow_optional_tool_install_is_not_mistaken_for_dead_base_installer(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress_path = state / "install-progress.json"
    progress_path.write_text(json.dumps({
        "owner": "runtime_features", "status": "working",
        "current": "Downloading the crawler's local browser",
        "updated": time.time() - 3600, "pid": 12345,
    }), encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery.subprocess, "Popen", lambda *args, **kwargs: 1 / 0)
    assert not setup_recovery.recover()
    assert json.loads(progress_path.read_text())["status"] == "working"
    assert not (state / "base-install-repair.json").exists()


def test_repair_exhaustion_requires_final_failure_and_dead_process(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (state / "base-install-repair.json").write_text('{"attempts":3}', encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    failed = {"status": "error", "current": "Setup needs attention: download failed"}
    assert setup_recovery.repair_exhausted(failed)
    assert not setup_recovery.repair_exhausted({**failed, "status": "working"})
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: True)
    assert not setup_recovery.repair_exhausted(failed)
    assert setup_recovery.repair_exhausted({
        "owner": "runtime_features", "status": "failed", "attempts": 3,
    })
