"""The voice upgrade runs independently of the desktop window."""

import errno
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import natural_voice


class NaturalVoiceTests(unittest.TestCase):
    def test_symlink_loop_in_model_path_is_not_ready(self):
        for check_name in ("ready", "whisper_ready"):
            with self.subTest(check=check_name):
                with patch.object(
                    Path, "stat",
                    side_effect=OSError(errno.ELOOP, "Too many symbolic links"),
                ):
                    self.assertFalse(getattr(natural_voice, check_name)())

    def test_directory_metadata_is_not_ready_after_stale_file_check(self):
        path = SimpleNamespace(
            is_file=lambda: True,
            stat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR, st_size=4096),
        )
        self.assertFalse(natural_voice._nonempty_file(path))

    def test_file_removed_during_readiness_check_is_not_ready(self):
        for check_name, names in (
            ("ready", ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")),
            ("whisper_ready", ("config.json", "model.bin")),
        ):
            with self.subTest(check=check_name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                voice = root / "voice"
                whisper = root / "Media/models/whisper"
                model_folder = voice if check_name == "ready" else whisper
                model_folder.mkdir(parents=True)
                for name in names:
                    (model_folder / name).write_bytes(b"weights")
                target = model_folder / names[0]
                original_stat = Path.stat
                removed = []

                def remove_before_stat(path, *args, **kwargs):
                    if path == target and not removed:
                        path.unlink()
                        removed.append(path)
                    return original_stat(path, *args, **kwargs)

                with (
                    patch.object(natural_voice, "ROOT", root),
                    patch.object(natural_voice, "VOICE", voice),
                    patch.object(Path, "stat", remove_before_stat),
                ):
                    self.assertFalse(getattr(natural_voice, check_name)())
                self.assertEqual(removed, [target])

    def test_non_directory_model_ancestor_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / "Media"
            media.mkdir()
            # Neither model path can exist when its shared ancestor is a file.
            (media / "models").write_bytes(b"not a directory")
            with (
                patch.object(natural_voice, "ROOT", root),
                patch.object(natural_voice, "VOICE", media / "models/natural_voice"),
            ):
                self.assertFalse(natural_voice.ready())
                self.assertFalse(natural_voice.whisper_ready())

    def test_empty_voice_file_triggers_repair_before_completion(self):
        names = ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
        for empty_name in names:
            with self.subTest(empty_file=empty_name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                media_python = root / "python.exe"
                media_python.touch()
                voice = root / "voice"
                voice.mkdir()
                for name in names:
                    (voice / name).write_bytes(b"" if name == empty_name else b"weights")
                status = root / "voice-status.json"
                fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None)

                def run_command(command, **kwargs):
                    if "--download-voice" in command:
                        for name in names:
                            (voice / name).write_bytes(b"repaired weights")
                    return SimpleNamespace(returncode=0)

                with (
                    patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                    patch.object(natural_voice, "ROOT", root),
                    patch.object(natural_voice, "MEDIA_PYTHON", media_python),
                    patch.object(natural_voice, "STATUS", status),
                    patch.object(natural_voice, "VOICE", voice),
                    patch.object(natural_voice, "whisper_ready", return_value=True),
                    patch.object(natural_voice.subprocess, "run", side_effect=run_command) as run,
                ):
                    self.assertFalse(natural_voice.ready())
                    natural_voice.install()
                    self.assertTrue(natural_voice.ready())
                self.assertEqual(run.call_count, 2)
                self.assertIn("--download-voice", run.call_args_list[1].args[0])
                self.assertEqual(json.loads(status.read_text())["status"], "complete")

    def test_missing_locking_module_reports_error_without_starting_install(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "voice-status.json"
            status.write_text(json.dumps({"status": "working"}))
            with (
                patch.dict(sys.modules, {"msvcrt": None}),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice.subprocess, "run") as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "error")
            self.assertIn("msvcrt", status.read_text())
            self.assertFalse((status.parent / "natural-voice.lock").exists())
            run.assert_not_called()

    def test_progress_directory_failure_reports_error_without_starting_install(self):
        failure = OSError(errno.ENOSPC, "No space for progress directory")
        fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=MagicMock())
        with (
            patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
            patch.object(Path, "mkdir", side_effect=failure),
            patch.object(Path, "open") as open_file,
            patch.object(natural_voice, "save_download_status") as save_status,
            patch.object(natural_voice.subprocess, "run") as run,
        ):
            natural_voice.install()
        save_status.assert_called_once_with(
            natural_voice.STATUS, "error", str(failure)[:400], 0
        )
        open_file.assert_not_called()
        fake_msvcrt.locking.assert_not_called()
        run.assert_not_called()

    def test_lock_open_failure_replaces_stale_working_status_with_error(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "voice-status.json"
            status.write_text(json.dumps({"status": "working"}))
            lock_path = status.parent / "natural-voice.lock"
            original_open = Path.open
            fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None)

            def fail_lock_open(path, *args, **kwargs):
                if path == lock_path:
                    raise PermissionError(errno.EACCES, "Lock file is not writable")
                return original_open(path, *args, **kwargs)

            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(natural_voice, "STATUS", status),
                patch.object(Path, "open", fail_lock_open),
                patch.object(natural_voice.subprocess, "run") as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "error")
            self.assertIn("Lock file is not writable", status.read_text())
            run.assert_not_called()

    def test_lock_flush_failure_replaces_stale_working_status_with_error(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "voice-status.json"
            status.write_text(json.dumps({"status": "working"}))
            lock_path = status.parent / "natural-voice.lock"
            original_open = Path.open
            events = []
            lock = MagicMock()
            lock.__enter__.return_value = lock
            lock.tell.return_value = 0
            lock.write.side_effect = lambda data: events.append("write")
            def fail_flush():
                events.append("flush")
                raise OSError(errno.ENOSPC, "No space for lock byte")
            lock.flush.side_effect = fail_flush
            fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=MagicMock(
                side_effect=lambda *args: events.append("lock")))

            def open_lock(path, *args, **kwargs):
                if path == lock_path:
                    return lock
                return original_open(path, *args, **kwargs)

            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(natural_voice, "STATUS", status),
                patch.object(Path, "open", open_lock),
                patch.object(natural_voice.subprocess, "run") as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "error")
            self.assertIn("No space for lock byte", status.read_text())
            lock.write.assert_called_once_with(b"1")
            fake_msvcrt.locking.assert_called_once_with(lock.fileno.return_value, fake_msvcrt.LK_NBLCK, 1)
            self.assertEqual(events, ["lock", "write", "flush"])
            run.assert_not_called()

    def test_failed_lock_write_does_not_raise_again_during_close(self):
        failure = OSError(errno.ENOSPC, "No space for lock byte")

        events = []

        class FullDisk(io.BytesIO):
            def fileno(self):
                return 42  # Only the fake msvcrt receives this neutral descriptor.

            def write(self, data):
                events.append("write")
                raise failure

        raw = FullDisk()
        fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=MagicMock(
            side_effect=lambda *args: events.append("lock")))

        def open_lock(path, mode, **kwargs):
            self.assertEqual(mode, "a+b")
            # Buffered files retry a failed flush when their context closes.
            return raw if kwargs.get("buffering") == 0 else io.BufferedRandom(raw)

        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "voice-status.json"
            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(natural_voice, "STATUS", status),
                patch.object(Path, "open", open_lock),
                patch.object(natural_voice, "save_download_status") as save_status,
                patch.object(natural_voice.subprocess, "run") as run,
            ):
                natural_voice.install()
            save_status.assert_called_once_with(status, "error", str(failure)[:400], 0)
            self.assertTrue(raw.closed)
            fake_msvcrt.locking.assert_called_once_with(42, fake_msvcrt.LK_NBLCK, 1)
            self.assertEqual(events, ["lock", "write"])
            run.assert_not_called()

    def test_contended_empty_lock_does_not_overwrite_active_progress(self):
        contention = PermissionError(errno.EACCES, "Lock held by another installer")
        lock = MagicMock()
        lock.__enter__.return_value = lock
        lock.tell.return_value = 0
        # Another installer can hold a byte-range lock before writing its byte.
        lock.write.side_effect = contention
        fake_msvcrt = SimpleNamespace(
            LK_NBLCK=1, locking=MagicMock(side_effect=contention)
        )
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "voice-status.json"
            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(natural_voice, "STATUS", status),
                patch.object(Path, "open", return_value=lock),
                patch.object(natural_voice, "save_download_status") as save_status,
                patch.object(natural_voice.subprocess, "run") as run,
            ):
                natural_voice.install()
        fake_msvcrt.locking.assert_called_once_with(
            lock.fileno.return_value, fake_msvcrt.LK_NBLCK, 1
        )
        lock.write.assert_not_called()
        save_status.assert_not_called()
        run.assert_not_called()

    def test_unexpected_lock_failure_replaces_stale_working_status_with_error(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "voice-status.json"
            status.write_text(json.dumps({"status": "working"}))
            failure = OSError(errno.EBADF, "Invalid lock descriptor")

            def fail_lock(*args):
                raise failure

            fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=fail_lock)
            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice.subprocess, "run") as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "error")
            self.assertIn("Invalid lock descriptor", status.read_text())
            run.assert_not_called()

    def test_missing_media_python_replaces_stale_working_status_with_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            status = root / "voice-status.json"
            status.write_text(json.dumps({"status": "working"}))
            fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None)
            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(natural_voice, "MEDIA_PYTHON", root / "missing-python.exe"),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice.subprocess, "run") as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "error")
            self.assertIn("bundled media Python executable is missing", status.read_text())
            run.assert_not_called()

    def test_existing_install_gets_engine_then_verified_weights(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media_python = root / "python.exe"
            media_python.touch()
            status = root / "voice-status.json"
            with (
                patch.object(natural_voice, "ROOT", root),
                patch.object(natural_voice, "MEDIA_PYTHON", media_python),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice, "ready", side_effect=[False, True]),
                patch.object(natural_voice, "whisper_ready", return_value=True),
                patch.object(
                    natural_voice.subprocess,
                    "run",
                    side_effect=[
                        SimpleNamespace(returncode=1),
                        SimpleNamespace(returncode=0),
                        SimpleNamespace(returncode=0),
                        SimpleNamespace(returncode=0),
                    ],
                ) as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "complete")
            self.assertEqual(run.call_count, 4)
            self.assertIn("kokoro-onnx==0.6.1", run.call_args_list[1].args[0])
            self.assertEqual(run.call_args_list[2].args[0],
                             [str(media_python), "-c", "import kokoro_onnx"])
            self.assertIn("--download-voice", run.call_args_list[3].args[0])

    def test_successful_pip_does_not_hide_persistent_engine_import_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media_python = root / "python.exe"
            media_python.touch()
            status = root / "voice-status.json"
            fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None)
            import_command = [str(media_python), "-c", "import kokoro_onnx"]
            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(natural_voice, "MEDIA_PYTHON", media_python),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice, "ready", return_value=True),
                patch.object(natural_voice, "whisper_ready", return_value=True),
                patch.object(
                    natural_voice.subprocess,
                    "run",
                    side_effect=[
                        SimpleNamespace(returncode=1),
                        SimpleNamespace(returncode=0),
                        natural_voice.subprocess.CalledProcessError(1, import_command),
                    ],
                ) as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "error")
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args_list[2].args[0], import_command)
            self.assertTrue(run.call_args_list[2].kwargs["check"])

    def test_empty_whisper_file_triggers_repair_before_completion(self):
        names = ("config.json", "model.bin")
        for empty_name in names:
            with self.subTest(empty_file=empty_name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                media_python = root / "python.exe"
                media_python.touch()
                whisper = root / "Media/models/whisper"
                whisper.mkdir(parents=True)
                for name in names:
                    (whisper / name).write_bytes(b"" if name == empty_name else b"data")
                status = root / "voice-status.json"
                fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None)

                def run_command(command, **kwargs):
                    if "--download-whisper" in command:
                        for name in names:
                            (whisper / name).write_bytes(b"repaired data")
                    return SimpleNamespace(returncode=0)

                with (
                    patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                    patch.object(natural_voice, "ROOT", root),
                    patch.object(natural_voice, "MEDIA_PYTHON", media_python),
                    patch.object(natural_voice, "STATUS", status),
                    patch.object(natural_voice, "ready", return_value=True),
                    patch.object(natural_voice.subprocess, "run", side_effect=run_command) as run,
                ):
                    self.assertFalse(natural_voice.whisper_ready())
                    natural_voice.install()
                    self.assertTrue(natural_voice.whisper_ready())
                self.assertEqual(run.call_count, 2)
                self.assertIn("--download-whisper", run.call_args_list[1].args[0])
                self.assertEqual(json.loads(status.read_text())["status"], "complete")

    def test_missing_whisper_weights_are_restored_without_redownloading_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media_python = root / "python.exe"
            media_python.touch()
            status = root / "voice-status.json"
            with (
                patch.object(natural_voice, "ROOT", root),
                patch.object(natural_voice, "MEDIA_PYTHON", media_python),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice, "ready", return_value=True),
                patch.object(natural_voice, "whisper_ready", side_effect=[False, True]),
                patch.object(natural_voice.subprocess, "run",
                             side_effect=[SimpleNamespace(returncode=0),
                                          SimpleNamespace(returncode=0)]) as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "complete")
            self.assertEqual(run.call_count, 2)
            self.assertIn("--download-whisper", run.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
