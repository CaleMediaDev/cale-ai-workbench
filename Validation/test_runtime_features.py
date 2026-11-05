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


@pytest.mark.parametrize("prefix", [
    ["other.py"],
    ["-m", "other_module"],
    ["-c", "pass"],
])
def test_installer_path_in_another_programs_arguments_does_not_block_repair(
    tmp_path, monkeypatch, prefix,
):
    import psutil

    script = runtime_features.Path(runtime_features.__file__).resolve()

    class UnrelatedProcess:
        def cmdline(self):
            return ["python", *prefix, str(script), "--install"]

        def cwd(self):
            return str(tmp_path)

    monkeypatch.setattr(psutil, "Process", lambda pid: UnrelatedProcess())
    progress = tmp_path / "install-progress.json"
    progress.write_text(json.dumps({
        "status": "working", "updated": 900,
        "attempts": 1, "owner": "runtime_features", "pid": 12345,
    }), encoding="utf-8")
    monkeypatch.setattr(runtime_features, "PROGRESS", progress)
    monkeypatch.setattr(runtime_features, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_features, "ready", lambda: False)
    monkeypatch.setattr(runtime_features.time, "time", lambda: 1000)
    launches = []
    monkeypatch.setattr(runtime_features.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))

    assert runtime_features.start_background()
    assert len(launches) == 1
    assert json.loads(progress.read_text(encoding="utf-8"))["attempts"] == 2


@pytest.mark.parametrize("script_arguments", [[], ["--install"]])
def test_installer_flag_must_follow_script(monkeypatch, script_arguments):
    import psutil

    script = str(runtime_features.Path(runtime_features.__file__).resolve())

    class Process:
        def cmdline(self):
            # Python consumes the first --install as the value of -W.
            return ["python", "-W", "--install", script, *script_arguments]

        def cwd(self):
            pytest.fail("An absolute script path must not require cwd lookup")

    monkeypatch.setattr(psutil, "Process", lambda pid: Process())

    assert runtime_features._installer_running(12345) is bool(script_arguments)


@pytest.mark.parametrize("prefix", [
    ["-uW", "ignore"],
    ["-uX", "utf8"],
    ["-uWignore"],
    ["-uXutf8"],
])
def test_installer_detection_handles_bundled_value_taking_options(monkeypatch, prefix):
    import psutil

    script = str(runtime_features.Path(runtime_features.__file__).resolve())

    class InstallerProcess:
        def cmdline(self):
            return ["python", *prefix, script, "--install"]

        def cwd(self):
            pytest.fail("An absolute script path must not require cwd lookup")

    monkeypatch.setattr(psutil, "Process", lambda pid: InstallerProcess())

    assert runtime_features._installer_running(12345)


@pytest.mark.parametrize("stderr, stdout, expected", [
    (" \n", "Package installation failed\n", "Package installation failed"),
    (" \n", "\t", "unknown error"),
    ("Specific stderr error\n", "Other stdout error", "Specific stderr error"),
    (" \n", "x" * 600, "x" * 500),
])
def test_run_preserves_useful_failure_details(monkeypatch, stderr, stdout, expected):
    from types import SimpleNamespace

    monkeypatch.setattr(
        runtime_features.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr=stderr, stdout=stdout,
        ),
    )

    with pytest.raises(RuntimeError) as error:
        runtime_features._run(["python", "-m", "pip", "--version"])

    assert str(error.value) == expected


@pytest.mark.parametrize("returncode", [0, 1])
def test_run_tolerates_output_outside_parent_encoding(monkeypatch, returncode):
    import sys

    real_run = runtime_features.subprocess.run

    def run_with_ascii_encoding(*args, **kwargs):
        # Simulate a parent locale that cannot decode the child's output.
        return real_run(*args, encoding="ascii", **kwargs)

    monkeypatch.setattr(runtime_features.subprocess, "run", run_with_ascii_encoding)
    command = [
        sys.executable, "-c",
        "import os, sys; os.write(2, b'failure: \\xff'); sys.exit(" + str(returncode) + ")",
    ]

    if returncode:
        with pytest.raises(RuntimeError) as error:
            runtime_features._run(command)
        assert str(error.value) == "failure: \ufffd"
    else:
        runtime_features._run(command)


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


@pytest.mark.parametrize("absolute", [True, False])
def test_installer_absolute_path_does_not_require_access_to_process_cwd(monkeypatch, absolute):
    import psutil
    from unittest.mock import Mock
    script = runtime_features.Path(runtime_features.__file__).resolve()
    process = Mock()
    process.cmdline.return_value = ["python", "-u", str(script) if absolute else script.name, "--install"]
    process.cwd.side_effect = psutil.AccessDenied(12345)
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    assert runtime_features._installer_running(12345) is absolute
    if absolute:
        process.cwd.assert_not_called()
    else:
        process.cwd.assert_called_once_with()


@pytest.mark.parametrize("prefix", [
    ["--check-hash-based-pycs", "always"],
    ["--check-hash-based-pycs=never"],
    ["-W", "ignore", "-X", "utf8", "-u"],
    ["--"],
])
def test_review_installer_valid_python_flags_preserve_absolute_script_without_cwd(tmp_path, monkeypatch, prefix):
    import psutil
    script = str(runtime_features.Path(runtime_features.__file__).resolve())
    class Installer:
        def cmdline(self):
            return ["python", *prefix, script, "--install"]
        def cwd(self):
            raise psutil.AccessDenied(12345)
    monkeypatch.setattr(psutil, "Process", lambda pid: Installer())
    assert runtime_features._installer_running(12345)


@pytest.mark.parametrize("prefix", [["-cpass"], ["-mother_module"], ["-"]])
def test_review_installer_inline_or_module_attached_options_are_not_script(tmp_path, monkeypatch, prefix):
    import psutil
    script = str(runtime_features.Path(runtime_features.__file__).resolve())
    class OtherProgram:
        def cmdline(self):
            return ["python", *prefix, script, "--install"]
        def cwd(self):
            return str(tmp_path)
    monkeypatch.setattr(psutil, "Process", lambda pid: OtherProgram())
    assert not runtime_features._installer_running(12345)
