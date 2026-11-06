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


def test_overflowing_installer_identity_does_not_crash_stall_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    worker = MagicMock()
    worker.cmdline.return_value = [
        "powershell.exe", "-File", str(tmp_path / "Install-Backend.ps1"),
        "-Destination", str(tmp_path),
    ]
    worker.create_time.return_value = 1
    with patch("psutil.Process", return_value=worker), patch(
        "psutil.process_iter", return_value=[]
    ):
        for identity in (
            {"pid": float("inf")},
            {"pid": 4321, "installer_started": 10 ** 400},
        ):
            progress = {"status": "working", "updated": 1, **identity}
            assert setup_recovery._installer_running(progress) is False
            assert setup_recovery._stalled(progress) is True


def test_nonfinite_installer_identity_does_not_hide_a_stopped_installer(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    worker = MagicMock()
    worker.cmdline.return_value = [
        "powershell.exe", "-File", str(tmp_path / "extracted" / "Install-Backend.ps1"),
        "-Destination", str(tmp_path),
    ]
    worker.create_time.return_value = 5000
    with patch("psutil.Process", return_value=worker), patch(
        "psutil.process_iter", return_value=[]
    ):
        for field in ("installer_started", "updated"):
            for value in ("NaN", "Infinity", "-Infinity"):
                progress = {
                    "status": "working", "pid": 4321,
                    "installer_started": 0, "updated": 1,
                    field: value,
                }
                assert setup_recovery._installer_running(progress) is False
                if field == "installer_started":
                    assert setup_recovery._stalled(progress) is True


def test_replacement_installer_is_detected_before_it_updates_progress(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    script = tmp_path / "Install-Backend.ps1"
    script.write_text("# fixture", encoding="utf-8")
    progress = {
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": time.time() - 1000, "pid": 4321,
    }
    (state / "install-progress.json").write_text(json.dumps(progress), encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    replacement = MagicMock()
    replacement.cmdline.return_value = [
        "powershell.exe", "-File", str(script), "-Destination", str(tmp_path),
    ]
    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(4321)), patch(
        "psutil.process_iter", return_value=[replacement]
    ), patch.object(
        setup_recovery.subprocess, "Popen", side_effect=AssertionError("duplicate repair")
    ):
        assert setup_recovery._installer_running(progress)
        assert not setup_recovery.recover()
        replacement.cmdline.return_value[-1] = str(tmp_path / "other-install")
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


def test_failed_repair_launch_returns_false_and_releases_lock(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    (state / "install-progress.json").write_text(json.dumps({
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": 1,
    }), encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    locking = MagicMock()
    locking.LK_NBLCK = 1
    locking.LK_UNLCK = 2
    with patch.dict("sys.modules", {"msvcrt": locking}), patch.object(
        setup_recovery.subprocess, "Popen", side_effect=OSError("launch unavailable")
    ) as launch:
        assert setup_recovery.recover() is False
        launch.assert_called_once()
        assert locking.locking.call_count == 2
        assert locking.locking.call_args.args[1:] == (locking.LK_UNLCK, 1)
        # A failed launch still consumes its attempt and observes the backoff.
        assert setup_recovery.recover() is False
        launch.assert_called_once()
    assert json.loads((state / "base-install-repair.json").read_text()) == {
        "attempts": 1, "started": 10000,
    }
    assert "launch unavailable" in (state / "base-install-repair.log").read_text()


def test_unavailable_repair_lock_returns_false_without_consuming_attempt(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress_path = state / "install-progress.json"
    progress_text = json.dumps({
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": 1,
    })
    progress_path.write_text(progress_text, encoding="utf-8")
    # A directory cannot be opened as the recovery lock file.
    (state / "base-install-repair.lock").mkdir()
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    locking = MagicMock()
    with patch.dict("sys.modules", {"msvcrt": locking}), patch.object(
        setup_recovery.subprocess, "Popen"
    ) as launch:
        assert setup_recovery.recover() is False
        launch.assert_not_called()
        locking.locking.assert_not_called()
    assert progress_path.read_text(encoding="utf-8") == progress_text
    assert not (state / "base-install-repair.json").exists()


def test_repair_lock_initialization_failure_does_not_consume_attempt(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress_path = state / "install-progress.json"
    progress_text = json.dumps({
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": 1,
    })
    progress_path.write_text(progress_text, encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    lock = MagicMock()
    lock.__enter__.return_value = lock
    lock.tell.return_value = 0
    lock.flush.side_effect = OSError("disk full")
    path_type = type(state)
    original_open = path_type.open

    def open_with_failed_lock(path, *args, **kwargs):
        if path == state / "base-install-repair.lock":
            return lock
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "open", open_with_failed_lock)
    locking = MagicMock()
    with patch.dict("sys.modules", {"msvcrt": locking}), patch.object(
        setup_recovery.subprocess, "Popen"
    ) as launch:
        assert setup_recovery.recover() is False
        launch.assert_not_called()
        locking.locking.assert_not_called()
    lock.write.assert_called_once_with(b"1")
    lock.flush.assert_called_once()
    lock.__exit__.assert_called_once()
    assert progress_path.read_text(encoding="utf-8") == progress_text
    assert not (state / "base-install-repair.json").exists()


def test_unavailable_repair_log_returns_false_and_releases_lock(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    (state / "install-progress.json").write_text(json.dumps({
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": 1,
    }), encoding="utf-8")
    # A directory cannot be opened as an append-only log file.
    (state / "base-install-repair.log").mkdir()
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    locking = MagicMock()
    locking.LK_NBLCK = 1
    locking.LK_UNLCK = 2
    with patch.dict("sys.modules", {"msvcrt": locking}), patch.object(
        setup_recovery.subprocess, "Popen"
    ) as launch:
        assert setup_recovery.recover() is False
        launch.assert_not_called()
        assert locking.locking.call_count == 2
        assert locking.locking.call_args.args[1:] == (locking.LK_UNLCK, 1)
        # Preserve the existing attempt consumption and retry backoff.
        assert setup_recovery.recover() is False
        assert locking.locking.call_count == 2
    assert json.loads((state / "base-install-repair.json").read_text()) == {
        "attempts": 1, "started": 10000,
    }


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


def test_invalid_base_repair_attempts_do_not_crash_or_launch(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress = {
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": 1,
    }
    progress_path = state / "install-progress.json"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    record_path = state / "base-install-repair.json"
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    locking = MagicMock()
    locking.LK_NBLCK = 1
    locking.LK_UNLCK = 2
    with patch.dict("sys.modules", {"msvcrt": locking}), patch.object(
        setup_recovery.subprocess, "Popen"
    ) as launch:
        for attempts in (None, "invalid", {}, [], float("inf")):
            record = {"attempts": attempts, "started": 1}
            record_text = json.dumps(record)
            record_path.write_text(record_text, encoding="utf-8")
            assert setup_recovery.recover() is False
            assert setup_recovery.repair_exhausted(progress) is False
            # A valid initial record may become invalid before the locked read.
            with patch.object(setup_recovery, "_read", side_effect=[
                progress, {"attempts": 0, "started": 1}, progress, record,
            ]):
                assert setup_recovery.recover() is False
            assert record_path.read_text(encoding="utf-8") == record_text
            assert json.loads(progress_path.read_text(encoding="utf-8")) == progress
        launch.assert_not_called()
    assert locking.locking.call_count == 10
    assert locking.locking.call_args.args[1:] == (locking.LK_UNLCK, 1)


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


def test_invalid_progress_timestamp_does_not_crash_or_restart_setup(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress_path = state / "install-progress.json"
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    with patch.object(setup_recovery.subprocess, "Popen") as launch:
        for updated in (None, "not-a-timestamp", {}, []):
            progress = {
                "status": "working", "current": "Downloading the AI model",
                "updated": updated,
            }
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
            assert setup_recovery.recover() is False
            assert json.loads(progress_path.read_text(encoding="utf-8")) == progress
        launch.assert_not_called()
    assert not (state / "base-install-repair.json").exists()


def test_overflowing_working_timestamp_does_not_trigger_recovery(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress_path = state / "install-progress.json"
    progress = {
        "status": "working", "current": "Downloading the AI model",
        "updated": 10 ** 400,
    }
    progress_text = json.dumps(progress)
    progress_path.write_text(progress_text, encoding="utf-8")
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    with patch.object(setup_recovery.subprocess, "Popen") as launch:
        assert setup_recovery._stalled(progress) is False
        assert setup_recovery.recover() is False
        launch.assert_not_called()
    assert progress_path.read_text(encoding="utf-8") == progress_text
    assert not (state / "base-install-repair.json").exists()


def test_nonfinite_working_timestamp_does_not_trigger_recovery(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress_path = state / "install-progress.json"
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    with patch.object(setup_recovery.subprocess, "Popen") as launch:
        for updated in ("-Infinity", "Infinity", "NaN"):
            progress = {
                "status": "working", "current": "Downloading the AI model",
                "updated": updated,
            }
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
            assert setup_recovery._stalled(progress) is False
            assert setup_recovery.recover() is False
            assert json.loads(progress_path.read_text(encoding="utf-8")) == progress
        launch.assert_not_called()
    assert not (state / "base-install-repair.json").exists()


def test_invalid_failure_timestamp_does_not_crash_recovery(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress_path = state / "install-progress.json"
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    locking = MagicMock()
    locking.LK_NBLCK = 1
    locking.LK_UNLCK = 2
    valid = {
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": 1,
    }
    read = setup_recovery._read
    with patch.dict("sys.modules", {"msvcrt": locking}), patch.object(
        setup_recovery.subprocess, "Popen"
    ) as launch:
        for updated in (None, "not-a-timestamp", {}, []):
            invalid = {**valid, "updated": updated}
            progress_path.write_text(json.dumps(invalid), encoding="utf-8")
            # Invalid on the initial read.
            assert setup_recovery.recover() is False
            # Valid initially, but invalid when reread under the repair lock.
            with patch.object(setup_recovery, "_read", side_effect=[valid, {}, invalid, {}]):
                assert setup_recovery.recover() is False
            assert read(progress_path) == invalid
        launch.assert_not_called()
    assert locking.locking.call_count == 8
    assert locking.locking.call_args.args[1:] == (locking.LK_UNLCK, 1)
    assert not (state / "base-install-repair.json").exists()


def test_nonfinite_or_overflowing_retry_timestamps_do_not_launch_repair(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / "Install-Backend.ps1").write_text("# fixture", encoding="utf-8")
    progress_path = state / "install-progress.json"
    record_path = state / "base-install-repair.json"
    monkeypatch.setattr(setup_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(setup_recovery, "STATE", state)
    monkeypatch.setattr(setup_recovery.time, "time", lambda: 10000)
    monkeypatch.setattr(setup_recovery, "_installer_running", lambda progress: False)
    monkeypatch.setattr(setup_recovery, "_retry_runner_running", lambda: False)
    valid_progress = {
        "status": "error", "current": "Setup needs attention: download failed",
        "updated": 1,
    }
    valid_record = {"attempts": 0, "started": 1}
    locking = MagicMock()
    locking.LK_NBLCK = 1
    locking.LK_UNLCK = 2
    with patch.dict("sys.modules", {"msvcrt": locking}), patch.object(
        setup_recovery.subprocess, "Popen"
    ) as launch:
        for field in ("updated", "started"):
            for value in ("NaN", "-Infinity", "Infinity", 10 ** 400):
                progress = dict(valid_progress)
                record = dict(valid_record)
                (progress if field == "updated" else record)[field] = value
                progress_path.write_text(json.dumps(progress), encoding="utf-8")
                record_path.write_text(json.dumps(record), encoding="utf-8")
                assert setup_recovery.recover() is False
                # The same invalid state may appear only after taking the lock.
                with patch.object(setup_recovery, "_read", side_effect=[
                    valid_progress, valid_record, progress, record,
                ]):
                    assert setup_recovery.recover() is False
                assert json.loads(progress_path.read_text(encoding="utf-8")) == progress
                assert json.loads(record_path.read_text(encoding="utf-8")) == record
        launch.assert_not_called()
    assert locking.locking.call_count == 16
    assert locking.locking.call_args.args[1:] == (locking.LK_UNLCK, 1)


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


def test_feature_repair_exhaustion_handles_invalid_attempt_counts():
    failed = {"owner": "runtime_features", "status": "failed"}
    for attempts in (None, "invalid", {}, [], float("inf"), float("nan")):
        assert setup_recovery.repair_exhausted({**failed, "attempts": attempts}) is False
    assert setup_recovery.repair_exhausted(failed) is False
    assert setup_recovery.repair_exhausted({**failed, "attempts": 2}) is False
    assert setup_recovery.repair_exhausted({**failed, "attempts": 3}) is True
    assert setup_recovery.repair_exhausted({**failed, "attempts": "3"}) is True
    assert setup_recovery.repair_exhausted({
        **failed, "status": "working", "attempts": 3,
    }) is False


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
