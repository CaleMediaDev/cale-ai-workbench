"""The optional voice model must release GPU memory after speech ends."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest

import cale_voice


@pytest.mark.parametrize("program,arguments,owned", [
    ("venv/python.exe", ["venv/python.exe", "WORKER"], True),
    ("base/python.exe", ["base/python.exe", "WORKER"], True),
    ("base/python.exe", ["venv/python.exe", "WORKER"], True),
    ("notepad.exe", ["notepad.exe", "WORKER"], False),
    ("Code.exe", ["Code.exe", "--reuse-window", "WORKER"], False),
    ("python.exe", ["python.exe", "OTHER", "WORKER"], False),
    ("python.exe", ["python.exe", "-c", "WORKER"], False),
    ("python.exe", ["python.exe", "-m", "other_module", "WORKER"], False),
    ("python.exe", ["python.exe", "-u", "WORKER"], False),
    ("notepad.exe", ["python.exe", "WORKER"], False),
    (None, ["python.exe", "WORKER"], False),
    ("python.exe", ["python.exe", "cale_voice_worker.py"], False),
])
def test_gpu_release_owns_only_a_python_worker_script_entrypoint(tmp_path, program, arguments, owned):
    worker = str(tmp_path / "cale_voice_worker.py")
    command = [worker if value == "WORKER" else str(tmp_path / "other.py") if value == "OTHER" else value
               for value in arguments]
    process = Mock(pid=100, info={"exe": str(tmp_path / program) if program else None, "cmdline": command})
    process.children.return_value = []
    with patch.object(cale_voice, "ROOT", tmp_path), \
         patch("psutil.process_iter", return_value=[process]) as processes, \
         patch("os.getpid", return_value=500), patch("psutil.wait_procs", return_value=([], [])):
        cale_voice.release_for_gpu_task()
    processes.assert_called_once_with(["pid", "cmdline", "exe"])
    if owned:
        process.children.assert_called_once_with(recursive=True)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
    else:
        process.children.assert_not_called()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        process.wait.assert_not_called()


def test_gpu_release_still_stops_parent_when_children_are_inaccessible(tmp_path):
    import psutil
    own = Mock(pid=100, info={"exe": str(tmp_path / "python.exe"), "cmdline": ["python.exe", str(tmp_path / "cale_voice_worker.py")]})
    own.children.side_effect = psutil.AccessDenied(pid=100)
    with patch.object(cale_voice, "ROOT", tmp_path), patch("psutil.process_iter", return_value=[own]), \
         patch("os.getpid", return_value=500), patch("psutil.wait_procs", return_value=([], [])):
        cale_voice.release_for_gpu_task()
    own.terminate.assert_called_once_with()
    own.wait.assert_called_once_with(timeout=5)


def test_gpu_release_waits_for_killed_descendants_before_returning(tmp_path):
    own = Mock(pid=100, info={"exe": str(tmp_path / "python.exe"), "cmdline": ["python.exe", str(tmp_path / "cale_voice_worker.py")]})
    child = Mock()
    own.children.return_value = [child]
    with patch.object(cale_voice, "ROOT", tmp_path), patch("psutil.process_iter", return_value=[own]), \
         patch("os.getpid", return_value=500), \
         patch("psutil.wait_procs", side_effect=[([], [child]), ([child], [])]) as wait:
        cale_voice.release_for_gpu_task()
    child.kill.assert_called_once_with()
    assert len(wait.call_args_list) == 2
    assert all(call.args == ([child],) and call.kwargs == {"timeout": 3} for call in wait.call_args_list)


def test_gpu_release_waits_for_parent_after_forced_kill(tmp_path):
    import psutil
    own = Mock(pid=100, info={"exe": str(tmp_path / "python.exe"), "cmdline": ["python.exe", str(tmp_path / "cale_voice_worker.py")]})
    own.children.return_value = []
    own.wait.side_effect = [psutil.TimeoutExpired(5, pid=100), 0]
    with patch.object(cale_voice, "ROOT", tmp_path), patch("psutil.process_iter", return_value=[own]), \
         patch("os.getpid", return_value=500), patch("psutil.wait_procs", return_value=([], [])):
        cale_voice.release_for_gpu_task()
    own.kill.assert_called_once_with()
    assert own.wait.call_count == 2
    own.wait.assert_called_with(timeout=5)


def test_voice_ready_rejects_a_stale_marker_with_wrong_dependencies():
    with TemporaryDirectory() as folder:
        root = Path(folder)
        python = root / "Media/python/Scripts/python.exe"
        reference = root / "assets/reference.wav"
        marker = root / "LongTasks/cale-voice-ready.json"
        for path in (python, reference, marker):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        packages = root / "Media/python/Lib/site-packages"
        for name, version in cale_voice.VOICE_PACKAGES.items():
            (packages / f"{name}-{version}.dist-info").mkdir(parents=True)
        with (patch.object(cale_voice, "ROOT", root),
              patch.object(cale_voice, "MEDIA_PYTHON", python),
              patch.object(cale_voice, "REFERENCE", reference),
              patch.object(cale_voice, "MARKER", marker)):
            assert cale_voice.ready()
            (packages / "transformers-4.57.3.dist-info").rename(
                packages / "transformers-4.57.1.dist-info"
            )
            assert not cale_voice.ready()


@pytest.mark.parametrize("error", [
    OSError("worker pipe failed"),
    ValueError("I/O operation on closed file"),
])
def test_worker_stream_errors_reach_the_caller(error):
    stream = Mock()
    stream.readline.side_effect = error
    with pytest.raises(type(error)) as raised:
        cale_voice._line_with_timeout(stream, 1)
    assert raised.value is error
    stream.readline.assert_called_once_with()


def test_idle_release_ignores_newer_speech():
    with patch.object(cale_voice, "_stop_worker") as stop, patch.object(
        cale_voice, "_generation", 12
    ):
        cale_voice._release_after_idle(11)
        stop.assert_not_called()
        cale_voice._release_after_idle(12)
        stop.assert_called_once()


def test_stopping_launcher_also_stops_gpu_child():
    launcher = Mock(pid=100)
    launcher.poll.return_value = None
    child = Mock()
    with patch.object(cale_voice, "_worker", launcher), patch(
        "psutil.Process"
    ) as process, patch("psutil.wait_procs", return_value=([], [])):
        process.return_value.children.return_value = [child]
        cale_voice._stop_worker()
    child.terminate.assert_called_once()
    launcher.terminate.assert_called_once()
    assert cale_voice._worker is None


def test_stopping_worker_waits_for_forcibly_killed_gpu_child():
    worker = Mock(pid=100)
    worker.poll.return_value = None
    child = Mock()

    def wait_for_children(children, timeout):
        assert children == [child]
        assert timeout == 3
        if child.kill.called:
            return [child], []
        return [], [child]

    with (patch.object(cale_voice, "_worker", worker),
          patch("psutil.Process") as process,
          patch("psutil.wait_procs", side_effect=wait_for_children) as wait):
        process.return_value.children.return_value = [child]
        cale_voice._stop_worker()
        assert cale_voice._worker is None
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        assert wait.call_count == 2
        wait.assert_called_with([child], timeout=3)


def test_stopping_worker_kills_it_if_termination_times_out():
    worker = Mock(pid=100)
    worker.poll.return_value = None
    worker.wait.side_effect = [
        cale_voice.subprocess.TimeoutExpired("voice-worker", 3),
        0,
    ]
    with (patch.object(cale_voice, "_worker", worker),
          patch("psutil.Process") as process,
          patch("psutil.wait_procs", return_value=([], []))):
        process.return_value.children.return_value = []
        cale_voice._stop_worker()
        assert cale_voice._worker is None
    worker.terminate.assert_called_once_with()
    worker.kill.assert_called_once_with()
    assert worker.wait.call_count == 2
    worker.wait.assert_called_with(timeout=3)


@pytest.mark.parametrize("child_survives", [False, True])
def test_music_releases_only_this_installations_voice_worker(tmp_path, child_survives):
    own = Mock(pid=100, info={"exe": str(tmp_path / "python.exe"), "cmdline": ["python.exe", str(tmp_path / "cale_voice_worker.py")]})
    other = Mock(pid=101, info={"exe": str(tmp_path / "python.exe"), "cmdline": ["python.exe", str(tmp_path / "other.py")]})
    child = Mock()
    own.children.return_value = [child]
    alive = [child] if child_survives else []
    with (patch.object(cale_voice, "ROOT", tmp_path),
          patch("psutil.process_iter", return_value=[own, other]),
          patch("os.getpid", return_value=500),
          patch("psutil.wait_procs", return_value=([], alive)) as wait_procs):
        cale_voice.release_for_gpu_task()
    own.terminate.assert_called_once()
    own.wait.assert_called_once_with(timeout=5)
    own.children.assert_called_once_with(recursive=True)
    child.terminate.assert_called_once_with()
    assert wait_procs.call_count == (2 if child_survives else 1)
    wait_procs.assert_called_with([child], timeout=3)
    if child_survives:
        child.kill.assert_called_once_with()
    else:
        child.kill.assert_not_called()
    other.terminate.assert_not_called()
    other.children.assert_not_called()


@pytest.mark.parametrize("lines", [
    ["null"],
    ["[]"],
    ['{"ready": true}', "null"],
    ['{"ready": true}', "[]"],
])
def test_non_object_worker_messages_trigger_cleanup(tmp_path, lines):
    with (patch.object(cale_voice, "ROOT", tmp_path),
          patch.object(cale_voice, "ready", return_value=True),
          patch.object(cale_voice, "_worker", None),
          patch.object(cale_voice, "_idle_timer", None),
          patch.object(cale_voice, "_generation", 0),
          patch.object(cale_voice.subprocess, "Popen"),
          patch.object(cale_voice, "_line_with_timeout", side_effect=lines),
          patch.object(cale_voice, "_stop_worker") as stop,
          patch.object(cale_voice.threading, "Timer") as timer):
        with pytest.raises(RuntimeError, match="could not finish this update"):
            cale_voice.speak("Hello", tmp_path / "speech.wav", play=False)
        stop.assert_called_once_with()
        timer.assert_not_called()


@pytest.mark.parametrize("answer", [
    '{}',
    '{"path": null}',
    '{"path": ""}',
    '{"path": 123}',
])
@pytest.mark.parametrize("play", [False, True])
def test_invalid_audio_path_triggers_cleanup(tmp_path, answer, play):
    with (patch.object(cale_voice, "ROOT", tmp_path),
          patch.object(cale_voice, "ready", return_value=True),
          patch.object(cale_voice, "_worker", None),
          patch.object(cale_voice, "_idle_timer", None),
          patch.object(cale_voice, "_generation", 0),
          patch.object(cale_voice.subprocess, "Popen"),
          patch.object(cale_voice, "_line_with_timeout",
                       side_effect=['{"ready": true}', answer]),
          patch.object(cale_voice, "_stop_worker") as stop,
          patch.object(cale_voice.threading, "Timer") as timer):
        with pytest.raises(RuntimeError, match="could not finish this update"):
            cale_voice.speak("Hello", tmp_path / "speech.wav", play=play)
        stop.assert_called_once_with()
        timer.assert_not_called()


@pytest.fixture
def clone_probe_runtime(tmp_path, monkeypatch):
    import json
    import wave
    root = tmp_path / "runtime"
    python = root / "Media/python/Scripts/python.exe"
    reference = root / "assets/cale-voice-reference.wav"
    marker = root / "LongTasks/cale-voice-ready.json"
    for path in (python, reference, marker, root / "cale_voice.py", root / "cale_voice_worker.py"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"local voice fixture")
    packages = root / "Media/python/Lib/site-packages"
    for name, version in cale_voice.VOICE_PACKAGES.items():
        metadata = packages / f"{name}-{version}.dist-info/METADATA"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(name + " " + version)
    snapshot = root / "cached-model"
    snapshot.mkdir()
    (snapshot / "config.json").write_text('{"model_type":"fixture"}')
    (snapshot / "model.safetensors").write_bytes(b"local weights")
    for name, value in {"ROOT": root, "MEDIA_PYTHON": python, "REFERENCE": reference,
                        "MARKER": marker, "CONTROLLER_SCRIPT": root / "cale_voice.py",
                        "WORKER_SCRIPT": root / "cale_voice_worker.py"}.items():
        monkeypatch.setattr(cale_voice, name, value)
    monkeypatch.setattr(cale_voice, "_probe_busy", lambda: False)
    monkeypatch.setattr(cale_voice, "release_for_gpu_task", Mock())
    def wav(path, mode="valid"):
        import math
        import struct
        if mode == "silent":
            samples = b"\0\0" * 1600
        elif mode == "dc":
            samples = struct.pack("<h", 8000) * 1600
        elif mode == "quiet":
            samples = b"\1\0\xff\xff" * 800
        else:
            samples = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * n / 16000))) for n in range(1600))
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
            output.writeframes(b"" if mode == "empty" else samples)
        if mode == "truncated":
            path.write_bytes(path.read_bytes()[:-100])
    def process(mode="valid", mutate=None):
        def start(command, **kwargs):
            target = Path(command[-2])
            before = cale_voice._voice_fingerprint(str(snapshot))
            if mode == "corrupt":
                target.write_bytes(b"not wave data")
            else:
                wav(target, mode)
            if mutate:
                mutate()
            answer = {"path": str(target), "engine": "Cale's local voice",
                      "model_path": str(snapshot), "fingerprint": before}
            child = Mock(returncode=0)
            child.poll.return_value = 0
            child.communicate.return_value = (json.dumps(answer), None)
            return child
        return start
    return root, snapshot, wav, process


def test_clone_probe_requires_actual_complete_non_silent_render_before_cache_pass(clone_probe_runtime, monkeypatch):
    root, snapshot, wav, process = clone_probe_runtime
    assert not cale_voice.capability_ready()
    launch = Mock(side_effect=process())
    monkeypatch.setattr(cale_voice.subprocess, "Popen", launch)
    report = cale_voice.run_probe()
    assert report["status"] == "passed" and report["engine"] == "Cale's local voice"
    assert report["audio"]["frames"] == 1600 and cale_voice.capability_ready()
    command = launch.call_args.args[0]
    assert command[:3] == [str(cale_voice.MEDIA_PYTHON), str(cale_voice.WORKER_SCRIPT), "--probe"]
    assert command[-1] == str(root)
    assert launch.call_args.kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    assert not Path(command[-2]).exists()  # Evidence metadata survives, temporary audio does not.


def test_finished_invalid_clone_probe_does_not_inspect_or_stop_a_reusable_pid(clone_probe_runtime, monkeypatch):
    root, snapshot, wav, process = clone_probe_runtime
    monkeypatch.setattr(cale_voice.subprocess, "Popen", process("empty"))
    stop = Mock(side_effect=AssertionError("The owned process has already exited"))
    monkeypatch.setattr("self_diagnostics._stop_check", stop)
    assert cale_voice.run_probe()["status"] == "failed"
    stop.assert_not_called()


@pytest.mark.parametrize("mode", ["empty", "truncated", "silent", "corrupt", "dc", "quiet"])
def test_clone_probe_rejects_invalid_render_without_false_ready(clone_probe_runtime, monkeypatch, mode):
    root, snapshot, wav, process = clone_probe_runtime
    monkeypatch.setattr(cale_voice.subprocess, "Popen", process(mode))
    monkeypatch.setattr("self_diagnostics._stop_check", Mock())
    report = cale_voice.run_probe()
    assert report["status"] == "failed" and not cale_voice.capability_ready()


def test_clone_fingerprint_accepts_real_size_vocabulary_and_tracks_changes(clone_probe_runtime):
    import os
    root, snapshot, wav, process = clone_probe_runtime
    vocabulary = snapshot / "vocab.json"
    vocabulary.write_bytes(b" " * 2_776_833)
    first = cale_voice._voice_fingerprint(str(snapshot))
    assert len(first) == 64
    original = vocabulary.stat()
    os.utime(vocabulary, ns=(original.st_atime_ns, original.st_mtime_ns + 10_000_000))
    assert cale_voice._voice_fingerprint(str(snapshot)) != first


@pytest.mark.parametrize("configuration", [None, "[]", "broken json", "{}" + " " * 2_000_000])
def test_clone_fingerprint_requires_valid_bounded_configuration(clone_probe_runtime, configuration):
    root, snapshot, wav, process = clone_probe_runtime
    config = snapshot / "config.json"
    if configuration is None:
        config.unlink()
    else:
        config.write_text(configuration, encoding="utf-8")
    with pytest.raises(ValueError):
        cale_voice._voice_fingerprint(str(snapshot))


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_probe_pcm_requires_audible_variation_in_a_channel(tmp_path, width):
    import math
    import wave
    path = tmp_path / "probe.wav"
    scale = 1 << (width * 8 - 1)
    def encode(value):
        return bytes([value + 128]) if width == 1 else value.to_bytes(width, "little", signed=True)
    def write(samples):
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2); output.setsampwidth(width); output.setframerate(16000)
            output.writeframes(samples)
    # Opposing DC offsets differ between channels but contain no sound.
    dc = int(scale * 0.25)
    write((encode(dc) + encode(-dc)) * 1600)
    with pytest.raises(ValueError, match="audible variation"):
        cale_voice._checked_probe_wav(path)
    # One audible channel is enough; the other may be silent.
    write(b"".join(encode(int(scale * .25 * math.sin(2 * math.pi * 220 * n / 16000))) + encode(0) for n in range(1600)))
    assert cale_voice._checked_probe_wav(path)["frames"] == 1600


@pytest.mark.parametrize("changed", ["reference", "controller", "worker", "model_config", "model_weights", "python", "package"])
def test_clone_capability_cache_invalidates_changed_voice_dependencies(clone_probe_runtime, monkeypatch, changed):
    root, snapshot, wav, process = clone_probe_runtime
    monkeypatch.setattr(cale_voice.subprocess, "Popen", process())
    assert cale_voice.run_probe()["status"] == "passed" and cale_voice.capability_ready()
    paths = {"reference": cale_voice.REFERENCE, "controller": cale_voice.CONTROLLER_SCRIPT,
        "worker": cale_voice.WORKER_SCRIPT, "model_config": snapshot / "config.json",
        "model_weights": snapshot / "model.safetensors", "python": cale_voice.MEDIA_PYTHON,
        "package": root / "Media/python/Lib/site-packages/qwen_tts-0.1.1.dist-info/METADATA"}
    paths[changed].write_bytes(paths[changed].read_bytes() + b"changed")
    assert not cale_voice.capability_ready()


def test_clone_probe_rejects_dependencies_changed_during_render(clone_probe_runtime, monkeypatch):
    root, snapshot, wav, process = clone_probe_runtime
    monkeypatch.setattr(cale_voice.subprocess, "Popen", process(mutate=lambda: cale_voice.REFERENCE.write_bytes(b"changed reference")))
    monkeypatch.setattr("self_diagnostics._stop_check", Mock())
    assert cale_voice.run_probe()["status"] == "failed" and not cale_voice.capability_ready()


@pytest.mark.parametrize("record", [{}, {"status": "passed"}, {"format": 1, "status": "passed", "engine": "Cale's local voice"}])
def test_clone_capability_missing_or_incomplete_attestation_is_not_ready(clone_probe_runtime, record):
    import json
    root, snapshot, wav, process = clone_probe_runtime
    record = {**record, "model_path": str(snapshot), "fingerprint": cale_voice._voice_fingerprint(str(snapshot))}
    cale_voice._save_probe(record)
    assert not cale_voice.capability_ready()
    (root / "LongTasks/cale-voice-probe.json").write_text("invalid json")
    assert not cale_voice.capability_ready()


def test_clone_probe_defers_and_cancels_when_user_work_needs_the_pc(clone_probe_runtime, monkeypatch):
    root, snapshot, wav, process = clone_probe_runtime
    launch = Mock()
    monkeypatch.setattr(cale_voice.subprocess, "Popen", launch)
    monkeypatch.setattr(cale_voice, "_probe_busy", lambda: True)
    assert cale_voice.run_probe()["status"] == "deferred"
    launch.assert_not_called()
    child = Mock(returncode=None)
    child.poll.return_value = None
    launch.return_value = child
    busy = Mock(side_effect=[False, True])
    monkeypatch.setattr(cale_voice, "_probe_busy", busy)
    stop = Mock()
    monkeypatch.setattr("self_diagnostics._stop_check", stop)
    assert cale_voice.run_probe()["status"] == "deferred"
    stop.assert_called_once_with(child)
    assert not cale_voice.capability_ready()


def test_clone_probe_scheduling_is_async_and_never_marks_ready_before_render(clone_probe_runtime, monkeypatch):
    root, snapshot, wav, process = clone_probe_runtime
    launch = Mock(return_value=Mock(pid=1234))
    monkeypatch.setattr(cale_voice.subprocess, "Popen", launch)
    assert cale_voice.schedule_probe()
    assert not cale_voice.capability_ready()
    command = launch.call_args.args[0]
    assert command[1:] == [str(cale_voice.CONTROLLER_SCRIPT), "--probe", "--runtime-root", str(root)]
    assert launch.call_args.kwargs["creationflags"] == cale_voice.HIDE | getattr(cale_voice.subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    assert not cale_voice.schedule_probe()
    assert launch.call_count == 1


def test_clone_capability_survives_unrelated_release_and_mic_or_deep_check_failure(clone_probe_runtime, monkeypatch):
    import json
    import voice_assistant
    root, snapshot, wav, process = clone_probe_runtime
    monkeypatch.setattr(cale_voice.subprocess, "Popen", process())
    assert cale_voice.run_probe()["status"] == "passed"
    (root / ".cale-ai-install").write_text("An unrelated new release")
    (root / "LongTasks/install-progress.json").write_text(json.dumps({"status": "complete"}))
    (root / "LongTasks/self-check.json").write_text(json.dumps({"status": "failed"}))
    (root / "LongTasks/readiness-probes.json").write_text(json.dumps({"voice": {"ok": False, "microphone": False}}))
    monkeypatch.setattr(voice_assistant, "ROOT", root)
    assert cale_voice.capability_ready() and voice_assistant.speech_ready()


def test_clone_probe_rejects_another_speech_engine_even_with_valid_audio(clone_probe_runtime, monkeypatch):
    import json
    root, snapshot, wav, process = clone_probe_runtime
    original = process()
    def fallback(command, **kwargs):
        child = original(command, **kwargs)
        answer = json.loads(child.communicate.return_value[0])
        answer["engine"] = "Kokoro natural voice"
        child.communicate.return_value = (json.dumps(answer), None)
        return child
    monkeypatch.setattr(cale_voice.subprocess, "Popen", fallback)
    monkeypatch.setattr("self_diagnostics._stop_check", Mock())
    assert cale_voice.run_probe()["status"] == "failed" and not cale_voice.capability_ready()


@pytest.fixture
def hf_clone_probe_runtime(clone_probe_runtime):
    import json
    import shutil
    root, cached, wav, _process = clone_probe_runtime
    repository = root / "hf/hub/models--Qwen--Qwen3-TTS-12Hz-0.6B-Base"
    old = repository / "snapshots" / ("a" * 40)
    new = repository / "snapshots" / ("b" * 40)
    for snapshot in (old, new):
        shutil.copytree(cached, snapshot)
    ref = repository / "refs/main"
    ref.parent.mkdir(parents=True)
    ref.write_text(old.name, encoding="ascii")
    def render(snapshot, mutate=None):
        def start(command, **_kwargs):
            target = Path(command[-2])
            before = cale_voice._voice_fingerprint(str(snapshot))
            wav(target)
            if mutate:
                mutate()
            answer = {"path": str(target), "engine": "Cale's local voice",
                      "model_path": str(snapshot), "fingerprint": before}
            child = Mock(returncode=0)
            child.poll.return_value = 0
            child.communicate.return_value = (json.dumps(answer), None)
            return child
        return start
    return root, old, new, ref, render


def test_clone_cache_requires_current_hf_main_revision_even_when_old_snapshot_remains(hf_clone_probe_runtime, monkeypatch):
    root, old, new, ref, render = hf_clone_probe_runtime
    launch = Mock(side_effect=render(old))
    monkeypatch.setattr(cale_voice.subprocess, "Popen", launch)
    assert cale_voice.run_probe()["status"] == "passed"
    assert cale_voice.capability_ready()
    ref.write_text(new.name, encoding="ascii")
    assert old.is_dir() and new.is_dir()
    assert not cale_voice.capability_ready()
    launch.side_effect = render(new)
    report = cale_voice.run_probe()
    assert report["status"] == "passed" and report["model_path"] == str(new)
    assert launch.call_count == 2 and cale_voice.capability_ready()
    assert old.is_dir()


@pytest.mark.parametrize("revision", [None, b"", b"not-a-commit", b"a" * 129, b"a" * 40 + b" extra", b"../" + b"a" * 40])
def test_clone_cache_rejects_missing_malformed_or_oversized_hf_main_ref(hf_clone_probe_runtime, monkeypatch, revision):
    root, old, new, ref, render = hf_clone_probe_runtime
    launch = Mock(side_effect=render(old))
    monkeypatch.setattr(cale_voice.subprocess, "Popen", launch)
    assert cale_voice.run_probe()["status"] == "passed"
    if revision is None:
        ref.unlink()
    else:
        ref.write_bytes(revision)
    assert not cale_voice.capability_ready()
    launch.assert_called_once()


def test_clone_probe_rejects_hf_main_revision_changed_during_actual_render(hf_clone_probe_runtime, monkeypatch):
    root, old, new, ref, render = hf_clone_probe_runtime
    launch = Mock(side_effect=render(old, lambda: ref.write_text(new.name, encoding="ascii")))
    monkeypatch.setattr(cale_voice.subprocess, "Popen", launch)
    report = cale_voice.run_probe()
    assert report["status"] == "failed"
    assert not cale_voice.capability_ready()
    assert old.is_dir() and new.is_dir()



def test_clone_fingerprint_includes_bounded_hf_selection_file_digest(hf_clone_probe_runtime):
    _root, old, _new, ref, _render = hf_clone_probe_runtime
    first = cale_voice._voice_fingerprint(str(old))
    ref.write_bytes(old.name.encode("ascii") + b"\n")
    second = cale_voice._voice_fingerprint(str(old))
    assert second != first
    ref.write_bytes(old.name.encode("ascii") + b"\r\n")
    assert cale_voice._voice_fingerprint(str(old)) not in (first, second)
