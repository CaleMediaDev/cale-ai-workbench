"""A stopped first-run setup retries without interrupting a running worker."""

import json
import time
from unittest.mock import MagicMock, patch

import psutil

import setup_recovery


def test_extracted_installer_pid_is_recognized_without_duplicate_repair(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# installed copy")
    extracted = tmp_path / "extracted" / "Install-Backend.ps1"
    extracted.parent.mkdir()
    extracted.write_text("# running copy")
    now = time.time()
    progress = {
        "status": "working", "current": "Installing tools", "updated": now - 3600,
        "pid": 4321, "installer_started": now - 3700,
    }
    (state / "install-progress.json").write_text(json.dumps(progress))
    worker = MagicMock()
    worker.cmdline.return_value = ["powershell.exe", "-NoProfile", "-File", str(extracted),
                                   "-Destination", str(tmp_path)]
    worker.create_time.return_value = progress["installer_started"] - 1
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    with patch("psutil.Process", return_value=worker), patch.object(
        setup_recovery.subprocess, "Popen", side_effect=AssertionError("duplicate repair")
    ):
        assert setup_recovery._installer_running(progress)
        assert not setup_recovery.recover()
    # Slow PowerShell startup is still a live install, not an abandoned one.
    worker.create_time.return_value = progress["installer_started"] - 120
    with patch("psutil.Process", return_value=worker):
        assert setup_recovery._installer_running(progress)
    worker.cmdline.return_value[-1] = str(tmp_path / "other-install")
    with patch("psutil.Process", return_value=worker):
        assert not setup_recovery._installer_running(progress)
    worker.cmdline.return_value[-1] = str(tmp_path)
    worker.create_time.return_value = now + 10
    with patch("psutil.Process", return_value=worker):
        assert not setup_recovery._installer_running(progress)
    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(4321)):
        assert not setup_recovery._installer_running(progress)


def test_legacy_installer_detection_respects_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    worker = MagicMock()
    command = ["powershell.exe", "-File", str(tmp_path / "Install-Backend.ps1")]
    progress = {"status": "working", "updated": time.time() - 3600}
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    with patch("psutil.process_iter", return_value=[worker]):
        worker.cmdline.return_value = command
        assert setup_recovery._installer_running(progress)
        worker.cmdline.return_value = command + ["-Destination", str(tmp_path)]
        assert setup_recovery._installer_running(progress)
        worker.cmdline.return_value = command + ["-Destination", str(tmp_path / "other-install")]
        assert not setup_recovery._installer_running(progress)
        assert setup_recovery._stalled(progress)


def test_first_run_retry_process_prevents_duplicate_repair(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# installed copy")
    progress = {
        "status": "error", "current": "Setup needs attention: temporary download failure",
        "updated": time.time() - 1000,
    }
    (state / "install-progress.json").write_text(json.dumps(progress))
    runner = MagicMock()
    runner.cmdline.return_value = ["powershell.exe", "-File", str(tmp_path / "extracted" / "Install-Retry.ps1"),
                                   "-Destination", str(tmp_path), "-Profile", "auto"]
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    with patch("psutil.process_iter", return_value=[runner]), patch.object(
        setup_recovery.subprocess, "Popen", side_effect=AssertionError("duplicate repair")
    ):
        assert setup_recovery._retry_runner_running()
        assert not setup_recovery.recover()
        assert not setup_recovery.repair_exhausted(progress)
    runner.cmdline.return_value[-3] = str(tmp_path / "someone-else")
    with patch("psutil.process_iter", return_value=[runner]):
        assert not setup_recovery._retry_runner_running()


def test_failed_status_waits_for_its_still_running_installer(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    (state / "install-progress.json").write_text(json.dumps({
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": time.time() - 1000,
    }), encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: True)
    with patch.object(setup_recovery.subprocess, "Popen", side_effect=AssertionError("duplicate repair")):
        assert not setup_recovery.recover()


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


def test_failed_launch_preserves_final_retry_and_backoff(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    # Exercise recovery independently of the host's Windows locking module.
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        LK_NBLCK=1, LK_UNLCK=2, locking=lambda *args: None,
    ))
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    (state / "install-progress.json").write_text(json.dumps({
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": 1,
    }), encoding="utf-8")
    record = state / "base-install-repair.json"
    record.write_text(json.dumps({"attempts": 2, "started": 1}), encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    clock = [10000]
    monkeypatch.setattr(setup_recovery.time, "time", lambda: clock[0])
    with patch.object(setup_recovery.subprocess, "Popen", side_effect=OSError("launch failed")) as launch:
        assert not setup_recovery.recover()
        assert json.loads(record.read_text()) == {"attempts": 2, "started": 10000}
        assert not setup_recovery.recover()
        assert launch.call_count == 1

    clock[0] += setup_recovery.RETRY_DELAYS[2]
    with patch.object(setup_recovery.subprocess, "Popen") as launch:
        assert setup_recovery.recover()
        launch.assert_called_once()
    assert json.loads(record.read_text())["attempts"] == 3


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


def test_bom_prefixed_completion_clears_retry_count(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    progress_path = state / "install-progress.json"
    progress_path.write_text(json.dumps({"status": "complete"}), encoding="utf-8-sig")
    record = state / "base-install-repair.json"
    record.write_text('{"attempts":3}', encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)

    assert not setup_recovery.recover()
    assert not record.exists()
    assert setup_recovery._read(progress_path) == {"status": "complete"}


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
