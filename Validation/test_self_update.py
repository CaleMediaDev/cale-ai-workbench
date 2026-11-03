"""Release updates protect user data and require a published digest."""

import io
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import self_update


class SelfUpdateTests(unittest.TestCase):
    def test_standalone_music_render_blocks_source_swap_until_worker_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "Toolbox/python/Scripts/python.exe"
            command = [str(python), str(root / "music_backend.py"),
                       "--run-job", "a" * 32]
            worker = MagicMock(info={"exe": str(python), "cmdline": command})
            with (patch.object(self_update, "ROOT", root),
                  patch("long_tasks.projects", return_value=[]),
                  patch("psutil.process_iter", return_value=[worker])):
                self.assertTrue(self_update.active_tasks())

    def test_paused_native_worker_blocks_source_swap_until_process_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "Toolbox/python/Scripts/python.exe"
            command = [str(python), str(root / "native_agent.py"), "run", "native-1"]
            worker = MagicMock(info={"exe": str(python), "cmdline": command})
            with (patch.object(self_update, "ROOT", root),
                  patch("long_tasks.projects", return_value=[{"status": "paused"}]),
                  patch("psutil.process_iter", return_value=[worker])):
                self.assertTrue(self_update.active_tasks())

    def test_paused_native_task_resumes_after_update_without_hermes_board(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task_id = "native-1"
            path = root / "Updates/resume-after-update.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"version": "0.5.351", "tasks": {task_id: str(root / "page")}}))
            project = {"id": task_id, "status": "paused", "engine": "native",
                       "folder": str(root / "page"), "card": ""}
            with (patch.object(self_update, "ROOT", root),
                  patch.object(self_update, "current_version", return_value=(0, 5, 351)),
                  patch("long_tasks.projects", return_value=[project]),
                  patch("long_tasks.board", side_effect=AssertionError("Hermes used")),
                  patch("native_agent.process_alive", return_value=False),
                  patch("long_tasks.request") as request):
                self.assertEqual(self_update.resume_projects_after_update(), 1)
            request.assert_called_once_with(task_id, "resume", "Continuing after Workbench update")

    def test_update_retires_only_its_paused_native_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task_id = "native-1"
            path = root / "Updates/resume-after-update.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"version": "0.5.351", "tasks": {task_id: str(root / "page")}}))
            python = root / "Toolbox/python/Scripts/python.exe"
            worker = MagicMock(info={"exe": str(python), "cmdline": [
                str(python), str(root / "native_agent.py"), "run", task_id]})
            unrelated = MagicMock(info={"exe": str(python), "cmdline": [
                str(python), str(root / "native_agent.py"), "run", "other-task"]})
            with (patch.object(self_update, "ROOT", root),
                  patch("long_tasks.projects", return_value=[{"id": task_id,
                      "status": "paused", "folder": str(root / "page")}]),
                  patch("psutil.process_iter", return_value=[worker, unrelated])):
                self.assertEqual(self_update.retire_update_paused_workers(), 1)
            worker.terminate.assert_called_once()
            unrelated.terminate.assert_not_called()

    def test_verified_update_queues_pause_only_for_active_projects(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            active = {"id": "a" * 12, "status": "working", "folder": str(root / "Projects/a")}
            already_paused = {"id": "b" * 12, "status": "paused", "folder": str(root / "Projects/b")}
            with (patch.object(self_update, "ROOT", root),
                  patch("long_tasks.projects", return_value=[active, already_paused]),
                  patch("long_tasks.request") as request):
                self.assertEqual(self_update.pause_projects_for_update((0, 5, 255)), [active["id"]])
                self.assertEqual(self_update.pause_projects_for_update((0, 5, 255)), [active["id"]])
            request.assert_called_once_with(active["id"], "pause",
                                            "Pausing for a verified Workbench update")
            saved = json.loads((root / "Updates/resume-after-update.json").read_text())
            self.assertEqual(saved["version"], "0.5.255")
            self.assertEqual(list(saved["tasks"]), [active["id"]])

    def test_update_resume_waits_for_new_version_and_retired_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "Updates/resume-after-update.json"
            path.parent.mkdir()
            pid = "a" * 12
            path.write_text(json.dumps({"version": "0.5.255", "tasks": {pid: str(root / "Projects/a")}}))
            project = {"id": pid, "status": "paused", "card": "t_abc12345"}
            with (patch.object(self_update, "ROOT", root),
                  patch.object(self_update, "current_version", return_value=(0, 5, 254)),
                  patch("long_tasks.projects", return_value=[project]),
                  patch("long_tasks.request") as request):
                self.assertEqual(self_update.resume_projects_after_update(), 0)
                request.assert_not_called()
            with (patch.object(self_update, "ROOT", root),
                  patch.object(self_update, "current_version", return_value=(0, 5, 255)),
                  patch("long_tasks.STATE", root / "LongTasks"),
                  patch("long_tasks.projects", return_value=[project]),
                  patch("long_tasks.board", return_value={"runs": [{"started_at": 1,
                      "ended_at": time.time() - 200}]}),
                  patch("long_tasks.request") as request):
                self.assertEqual(self_update.resume_projects_after_update(), 1)
            request.assert_called_once_with(pid, "resume", "Continuing after Workbench update")
            self.assertTrue(path.exists())
            with (patch.object(self_update, "ROOT", root),
                  patch.object(self_update, "current_version", return_value=(0, 5, 255)),
                  patch("long_tasks.projects", return_value=[{**project, "status": "working"}])):
                self.assertEqual(self_update.resume_projects_after_update(), 0)
            self.assertFalse(path.exists())

    def test_update_only_stops_managed_worker_in_paused_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "Projects/a"
            project.mkdir(parents=True)
            path = root / "Updates/resume-after-update.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"version": "0.5.255",
                                        "tasks": {"a" * 12: str(project)}}))
            executable = root / "data/tools/python.exe"
            worker = MagicMock(info={"exe": str(executable),
                                     "cmdline": [str(executable), "-m", "agent.managed_worker"]})
            worker.cwd.return_value = str(project)
            unrelated = MagicMock(info={"exe": str(executable),
                                        "cmdline": [str(executable), "-m", "agent.managed_worker"]})
            unrelated.cwd.return_value = str(root / "Projects/other")
            with (patch.object(self_update, "ROOT", root),
                  patch("long_tasks.projects", return_value=[
                      {"id": "a" * 12, "status": "paused", "folder": str(project)}]),
                  patch("psutil.process_iter", return_value=[worker, unrelated])):
                self.assertEqual(self_update.retire_update_paused_workers(), 1)
            worker.terminate.assert_called_once()
            unrelated.terminate.assert_not_called()

    def test_failed_update_opens_resume_gate_for_old_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".cale-ai-install").write_text("Cale AI Workbench 0.5.254")
            path = root / "Updates/resume-after-update.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"version": "0.5.255", "tasks": {"a" * 12: "folder"}}))
            with patch.object(self_update, "ROOT", root):
                self_update.allow_resume_after_failed_update()
            self.assertEqual(json.loads(path.read_text())["version"], "0.5.254")

    def test_unfinished_setup_prevents_source_swap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".cale-ai-install").write_text("Cale AI Workbench setup pending")
            with patch.object(self_update, "ROOT", root):
                self.assertTrue(self_update.active_tasks())
            (root / ".cale-ai-install").write_text("Cale AI Workbench 0.5.240")
            (root / "LongTasks").mkdir()
            (root / "LongTasks/install-progress.json").write_text('{"status":"working"}')
            with patch.object(self_update, "ROOT", root):
                self.assertTrue(self_update.active_tasks())

    def test_release_check_bypasses_stale_feed_cache(self):
        with patch.object(self_update, "urlopen", return_value=io.BytesIO(b"[]")) as open_url:
            self.assertEqual(self_update.fetch_json(self_update.RELEASES), [])
        request = open_url.call_args.args[0]
        self.assertIn("&checked_at=", request.full_url)
        self.assertEqual(request.get_header("Cache-control"), "no-cache")

    def test_orphaned_terminal_run_no_longer_blocks_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "Projects/song"
            project.mkdir(parents=True)
            state_db = root / "data/state.db"
            board_db = root / "data/kanban/boards/long-tasks/kanban.db"
            board_db.parent.mkdir(parents=True)
            started = time.time() - 1000
            with closing(sqlite3.connect(state_db)) as db:
                db.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, started_at REAL)")
                db.execute("CREATE TABLE session_admissions (target_session_id TEXT, seq INTEGER, payload_json TEXT)")
                db.execute("INSERT INTO sessions VALUES ('session-1', ?, ?)", (str(project), started))
                db.execute("INSERT INTO session_admissions VALUES ('session-1', 1, ?)", (
                    json.dumps({"kanban_card_v1": {"board": "long-tasks", "run_id": 20,
                                                   "task_id": "card-1"}}),
                ))
                db.commit()
            with closing(sqlite3.connect(board_db)) as db:
                db.execute("CREATE TABLE task_runs (id INTEGER, task_id TEXT, ended_at REAL)")
                db.execute("INSERT INTO task_runs VALUES (20, 'card-1', ?)", (time.time() - 400,))
                db.commit()
            executable = root / "data/tools/python.exe"
            worker = MagicMock(info={"exe": str(executable),
                                     "cmdline": [str(executable), "-m", "agent.managed_worker"]})
            worker.pid = 123
            worker.create_time.return_value = started
            worker.cwd.return_value = str(project)
            worker.cmdline.return_value = worker.info["cmdline"]
            with (patch.object(self_update, "ROOT", root),
                  patch("long_tasks.projects", return_value=[{"status": "paused"}]),
                  patch("psutil.process_iter", return_value=[worker])):
                self.assertFalse(self_update.active_tasks())
            worker.terminate.assert_called_once()
            self.assertIn("run 20", (root / "Updates/orphan-recovery.log").read_text())

    def test_live_board_run_is_never_retired_for_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "Projects/song"
            project.mkdir(parents=True)
            state_db = root / "data/state.db"
            board_db = root / "data/kanban/boards/long-tasks/kanban.db"
            board_db.parent.mkdir(parents=True)
            started = time.time() - 1000
            with closing(sqlite3.connect(state_db)) as db:
                db.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, started_at REAL)")
                db.execute("CREATE TABLE session_admissions (target_session_id TEXT, seq INTEGER, payload_json TEXT)")
                db.execute("INSERT INTO sessions VALUES ('session-1', ?, ?)", (str(project), started))
                db.execute("INSERT INTO session_admissions VALUES ('session-1', 1, ?)", (
                    json.dumps({"kanban_card_v1": {"board": "long-tasks", "run_id": 20,
                                                   "task_id": "card-1"}}),
                ))
                db.commit()
            with closing(sqlite3.connect(board_db)) as db:
                db.execute("CREATE TABLE task_runs (id INTEGER, task_id TEXT, ended_at REAL)")
                db.execute("INSERT INTO task_runs VALUES (20, 'card-1', NULL)")
                db.commit()
            executable = root / "data/tools/python.exe"
            worker = MagicMock(info={"exe": str(executable),
                                     "cmdline": [str(executable), "-m", "agent.managed_worker"]})
            worker.create_time.return_value = started
            worker.cwd.return_value = str(project)
            with (patch.object(self_update, "ROOT", root),
                  patch("long_tasks.projects", return_value=[{"status": "paused"}]),
                  patch("psutil.process_iter", return_value=[worker])):
                self.assertTrue(self_update.active_tasks())
            worker.terminate.assert_not_called()

    def test_two_updaters_cannot_hold_the_update_lock_together(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            self_update, "ROOT", Path(folder)
        ):
            with self_update.update_lock():
                with self.assertRaises(TimeoutError):
                    with self_update.update_lock(timeout=0):
                        pass

    def test_failed_update_reopens_old_desktop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".cale-ai-install").write_text("Cale AI Workbench 0.4.0")
            with (patch.object(self_update, "ROOT", root),
                  patch.object(self_update, "wait_for_exit"),
                  patch.object(self_update, "update_lock", return_value=nullcontext()),
                  patch.object(self_update, "apply", side_effect=OSError("disk full")),
                  patch.object(self_update, "reopen_desktop") as reopen):
                with self.assertRaisesRegex(OSError, "disk full"):
                    self_update.run_update(root / "release.zip", (0, 5, 0), 123)
            reopen.assert_called_once()
            self.assertIn("disk full", (root / "Updates/last-error.txt").read_text())

    def test_second_updater_reopens_desktop_after_release_already_applied(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".cale-ai-install").write_text("Cale AI Workbench 0.5.0")
            with (patch.object(self_update, "ROOT", root),
                  patch.object(self_update, "wait_for_exit"),
                  patch.object(self_update, "update_lock", return_value=nullcontext()),
                  patch.object(self_update, "apply") as apply,
                  patch.object(self_update, "reopen_desktop") as reopen):
                self_update.run_update(root / "release.zip", (0, 5, 0), 123)
            apply.assert_not_called()
            reopen.assert_called_once()

    def test_paused_task_waits_for_its_real_worker_before_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / "data/tools/python.exe"
            executable.parent.mkdir(parents=True)
            worker = MagicMock(info={
                "exe": str(executable),
                "cmdline": [str(executable), "-m", "agent.managed_worker"],
            })
            with patch.object(self_update, "ROOT", root), patch(
                "long_tasks.projects", return_value=[{"status": "paused"}]
            ), patch("psutil.process_iter", return_value=[worker]):
                self.assertTrue(self_update.active_tasks())
            worker.info["exe"] = str(root.parent / "other-install/python.exe")
            with patch.object(self_update, "ROOT", root), patch(
                "long_tasks.projects", return_value=[{"status": "paused"}]
            ), patch("psutil.process_iter", return_value=[worker]):
                self.assertFalse(self_update.active_tasks())

    def test_update_installs_new_media_pin_before_swapping_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "Media/python/Scripts/python.exe"
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b"")
            (root / "media-requirements.txt").write_text("old-package==1\n")
            archive_path = root / "release.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("media-requirements.txt", "old-package==1\npypdfium2==5.14.0\n")
            with patch.object(self_update, "ROOT", root), patch.object(
                self_update.subprocess, "run", return_value=MagicMock(returncode=0)
            ) as install, ZipFile(archive_path) as archive:
                self_update.sync_new_media_requirements(archive)
            self.assertEqual(install.call_args.args[0][-1], "pypdfium2==5.14.0")

    def test_update_installs_new_toolbox_pin_before_swapping_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "Toolbox/python/Scripts/python.exe"
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b"")
            (root / "requirements-windows.txt").write_text("old-package==1\n")
            archive_path = root / "release.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("requirements-windows.txt",
                                 "old-package==1\nopenai==3.42.0\n")
            with patch.object(self_update, "ROOT", root), patch.object(
                self_update.subprocess, "run", return_value=MagicMock(returncode=0)
            ) as install, ZipFile(archive_path) as archive:
                self_update.sync_new_toolbox_requirements(archive)
            self.assertEqual(install.call_args.args[0][-1], "openai==3.42.0")
            self.assertEqual(install.call_args.args[0][0], str(runtime))

    def test_toolbox_dependency_failure_keeps_old_source_and_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".cale-ai-install").write_text("Cale AI Workbench 0.5.344")
            (root / "long_tasks.py").write_text("old")
            (root / "requirements-windows.txt").write_text("old-package==1\n")
            runtime = root / "Toolbox/python/Scripts/python.exe"
            runtime.parent.mkdir(parents=True)
            runtime.touch()
            archive_path = root / "release.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("install-files.json", json.dumps(
                    ["long_tasks.py", "requirements-windows.txt"]))
                archive.writestr("long_tasks.py", "new")
                archive.writestr("requirements-windows.txt",
                                 "old-package==1\nopenai==3.42.0\n")
            with (patch.object(self_update, "ROOT", root),
                  patch.object(self_update, "active_tasks", return_value=False),
                  patch.object(self_update.subprocess, "run", return_value=MagicMock(
                      returncode=1, stderr="package unavailable"))):
                with self.assertRaisesRegex(RuntimeError, "package unavailable"):
                    self_update.apply(archive_path, (0, 5, 345))
            self.assertEqual((root / "long_tasks.py").read_text(), "old")
            self.assertEqual((root / ".cale-ai-install").read_text(),
                             "Cale AI Workbench 0.5.344")

    def test_update_restarts_only_its_own_router_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cloud = root / "Cloud"
            cloud.mkdir()
            (cloud / "router-pid.txt").write_text("1234")
            process = MagicMock()
            process.cmdline.return_value = [
                "pythonw.exe", str(root / "cloud_ai.py"), "--serve"
            ]
            with (patch.object(self_update, "ROOT", root),
                  patch("psutil.Process", return_value=process),
                  patch.object(self_update.subprocess, "run",
                               return_value=MagicMock(returncode=0)) as start):
                self_update.restart_router_after_update()
            process.terminate.assert_called_once()
            start.assert_called_once()
            self.assertEqual(start.call_args.args[0][-2:],
                             [str(root / "cloud_ai.py"), "--ensure"])

    def test_candidate_needs_digest_and_newer_version(self):
        asset = {
            "name": "Cale-AI-Workbench-0.5.0.zip",
            "digest": "sha256:" + "a" * 64,
            "size": 123,
            "browser_download_url": "https://github.com/CaleMediaDev/cale-ai-workbench/releases/download/v0.5.0/Cale-AI-Workbench-0.5.0.zip",
        }
        release = {"tag_name": "v0.5.0", "assets": [asset]}
        self.assertEqual(
            self_update.release_candidate([release], (0, 4, 1))[0], (0, 5, 0)
        )
        self.assertIsNone(self_update.release_candidate([release], (0, 5, 0)))
        self.assertIsNone(
            self_update.release_candidate(
                [{"tag_name": "v0.5.0", "assets": [{**asset, "digest": ""}]}], (0, 4, 1)
            )
        )

    def test_manifest_rejects_project_or_secret_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "release.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "install-files.json", json.dumps(["Projects/secret.txt"])
                )
                archive.writestr("Projects/secret.txt", "bad")
            with ZipFile(archive_path) as archive:
                with self.assertRaises(ValueError):
                    self_update.validated_manifest(archive)

    def test_manifest_allows_current_source_count_but_keeps_a_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "release.zip"
            names = [f"source-{index:03}.py" for index in range(202)]
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("install-files.json", json.dumps(names))
                for name in names:
                    archive.writestr(name, "# source")
            with ZipFile(archive_path) as archive:
                self.assertEqual(len(self_update.validated_manifest(archive)), 203)
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("install-files.json", json.dumps(names +
                                 [f"extra-{index}.py" for index in range(49)]))
            with ZipFile(archive_path) as archive:
                with self.assertRaisesRegex(ValueError, "manifest"):
                    self_update.validated_manifest(archive)

    def test_apply_preserves_user_project_and_makes_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".cale-ai-install").write_text("Cale AI Workbench 0.4.0")
            (root / "long_tasks.py").write_text("old")
            (root / "Projects").mkdir()
            (root / "Projects/work.txt").write_text("my work")
            archive_path = root / "release.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("install-files.json", json.dumps(["long_tasks.py"]))
                archive.writestr("long_tasks.py", "new")
            with (
                patch.object(self_update, "ROOT", root),
                patch.object(self_update, "active_tasks", return_value=False),
            ):
                backup = self_update.apply(archive_path, (0, 5, 0))
            self.assertEqual((root / "long_tasks.py").read_text(), "new")
            self.assertEqual((root / "Projects/work.txt").read_text(), "my work")
            self.assertEqual((backup / "long_tasks.py").read_text(), "old")
            self.assertIn("0.5.0", (root / ".cale-ai-install").read_text())

    def test_failed_version_commit_rolls_back_source_and_keeps_old_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / ".cale-ai-install"
            marker.write_text("Cale AI Workbench 0.4.0")
            source = root / "long_tasks.py"
            source.write_text("old")
            archive_path = root / "release.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("install-files.json", json.dumps(["long_tasks.py"]))
                archive.writestr("long_tasks.py", "new")
            with (patch.object(self_update, "ROOT", root),
                  patch.object(self_update, "active_tasks", return_value=False),
                  patch.object(self_update, "write_version_marker", side_effect=OSError("disk full"))):
                with self.assertRaisesRegex(OSError, "disk full"):
                    self_update.apply(archive_path, (0, 5, 0))
            self.assertEqual(source.read_text(), "old")
            self.assertEqual(marker.read_text(), "Cale AI Workbench 0.4.0")

    def test_version_marker_replace_failure_keeps_previous_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / ".cale-ai-install"
            marker.write_text("Cale AI Workbench 0.4.0")
            with patch.object(self_update.os, "replace", side_effect=OSError("busy")):
                with self.assertRaisesRegex(OSError, "busy"):
                    self_update.write_version_marker(root, (0, 5, 0))
            self.assertEqual(marker.read_text(), "Cale AI Workbench 0.4.0")
            self.assertFalse((root / ".cale-ai-install.updating").exists())

    def test_design_tools_migrate_without_replacing_existing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "data/config.yaml"
            config.parent.mkdir()
            original = "platform_toolsets:\n  cli:\n  - mcp-toolbox\nmcp_servers:\n  toolbox: {}\nskills:\n  disabled: []\n"
            config.write_text(original, encoding="utf-8")
            backup = root / "backup"
            self_update.add_design_tools_to_config(root, backup)
            updated = config.read_text(encoding="utf-8")
            self.assertIn("  - mcp-design\n", updated)
            self.assertIn("  design:\n", updated)
            self.assertIn("  toolbox: {}\n", updated)
            self.assertEqual((backup / "data/config.yaml").read_text(), original)
            self.assertIsNone(self_update.add_design_tools_to_config(root, backup))

    def test_existing_32k_model_config_is_upgraded_without_other_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "data/config.yaml"
            config.parent.mkdir()
            original = (
                "model:\n  default: qwen3-coder-local\n  context_length: 32768\n"
                "fallback_providers: []\nplatform_toolsets:\n  cli:\n  - mcp-toolbox\n"
                "  - mcp-design\nmcp_servers:\n  design: {}\nskills:\n  disabled: []\n"
            )
            config.write_text(original, encoding="utf-8")
            backup = root / "backup"
            self_update.add_design_tools_to_config(root, backup)
            updated = config.read_text(encoding="utf-8")
            self.assertIn("context_length: 65536", updated)
            self.assertIn("default: qwen3-coder-local", updated)
            self.assertEqual((backup / "data/config.yaml").read_text(), original)

    def test_short_planning_uses_local_small_model_without_changing_custom_choice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "data/config.yaml"
            config.parent.mkdir()
            config.write_text(
                "auxiliary:\n  triage_specifier:\n    provider: custom\n"
                "    model: qwen3-coder-local\n    timeout: 900\n"
                "  kanban_decomposer:\n    provider: custom\n"
                "    model: my-custom-model\n    timeout: 900\n"
                "platform_toolsets:\n  cli:\n  - mcp-toolbox\n  - mcp-design\n"
                "mcp_servers:\n  design: {}\nskills:\n  disabled: []\n",
                encoding="utf-8",
            )
            self_update.add_design_tools_to_config(root, root / "backup")
            text = config.read_text(encoding="utf-8")
            self.assertIn("triage_specifier:\n    provider: custom\n    model: cale-vision", text)
            self.assertIn("kanban_decomposer:\n    provider: custom\n    model: my-custom-model", text)


if __name__ == "__main__":
    unittest.main()
