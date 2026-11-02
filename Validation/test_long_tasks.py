"""Behavior checks for the companion (no model or native board writes)."""

import contextlib
import importlib.util
import io
from urllib.error import HTTPError
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("long_tasks", ROOT / "long_tasks.py")
task_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_module)


class LongTaskTests(unittest.TestCase):
    def test_missing_configured_main_model_recovers_before_task_dispatch(self):
        calls = []

        def response(request, timeout=0):
            url = request.full_url if hasattr(request, "full_url") else request
            calls.append(url)
            if url.endswith("/api/show") and calls.count(url) == 1:
                raise HTTPError(url, 404, "missing", {}, None)
            if url.endswith("/api/show"):
                return io.BytesIO(json.dumps({"parameters": "num_ctx 65536\n",
                                              "capabilities": ["tools"]}).encode())
            return io.BytesIO(b'{"models":[]}')

        with (patch("urllib.request.urlopen", side_effect=response),
              patch("model_library.repair_main_alias", return_value=True) as repair):
            task_module.ensure_ollama("cale-local")
        repair.assert_called_once()
        self.assertEqual(calls.count("http://127.0.0.1:11434/api/show"), 2)

    def test_existing_main_without_tools_is_repaired_before_task_dispatch(self):
        calls = []

        def response(request, timeout=0):
            url = request.full_url if hasattr(request, "full_url") else request
            calls.append(url)
            if url.endswith("/api/show"):
                capabilities = [] if calls.count(url) == 1 else ["tools"]
                return io.BytesIO(json.dumps({
                    "parameters": "num_ctx 65536\n", "capabilities": capabilities,
                }).encode())
            return io.BytesIO(b'{"models":[]}')

        with (patch("urllib.request.urlopen", side_effect=response),
              patch("model_library.repair_main_alias", return_value=True) as repair):
            task_module.ensure_ollama("cale-local")
        repair.assert_called_once()
        self.assertEqual(calls.count("http://127.0.0.1:11434/api/show"), 2)

    def test_existing_main_without_tools_reports_missing_replacement(self):
        def response(request, timeout=0):
            url = request.full_url if hasattr(request, "full_url") else request
            if url.endswith("/api/show"):
                return io.BytesIO(b'{"parameters":"num_ctx 65536\\n","capabilities":[]}')
            return io.BytesIO(b'{"models":[]}')

        with (patch("urllib.request.urlopen", side_effect=response),
              patch("model_library.repair_main_alias", return_value=False)):
            with self.assertRaisesRegex(RuntimeError, "cannot use tools"):
                task_module.ensure_ollama("cale-local")

    def test_hidden_model_activity_window_does_not_block_automatic_update(self):
        class Window:
            def __init__(self, visible, kind="Toplevel"):
                self.visible, self.kind = visible, kind

            def winfo_class(self):
                return self.kind

            def winfo_viewable(self):
                return self.visible

        hidden_usage = Window(False)
        preview = Window(True)
        visible_dialog = Window(True)
        self.assertFalse(task_module.update_dialog_open([hidden_usage, preview], (preview,)))
        self.assertTrue(task_module.update_dialog_open(
            [hidden_usage, preview, visible_dialog], (preview,)))
        self.assertFalse(task_module.update_dialog_open(
            [hidden_usage, preview, visible_dialog], (preview, visible_dialog)))

    def test_worker_handoff_starts_after_process_exit_and_file_settle(self):
        import psutil

        started = time.time() - 20
        snapshot = {
            "runs": [{"id": 7, "started_at": started, "ended_at": started + 10}],
            "events": [
                {"run_id": 7, "kind": "spawned", "payload": {"pid": 1234}},
                {"run_id": 7, "kind": "worker_bound", "payload": {"pid": 5678}},
            ],
        }
        with patch("psutil.Process", side_effect=psutil.NoSuchProcess(1234)):
            self.assertFalse(task_module.worker_handoff_settled(snapshot, started + 12))
            self.assertTrue(task_module.worker_handoff_settled(snapshot, started + 15))
        with patch("psutil.Process") as process:
            process.return_value.create_time.return_value = started + 1
            self.assertFalse(task_module.worker_handoff_settled(snapshot, started + 15))
            process.return_value.create_time.return_value = started - 1000
            self.assertTrue(task_module.worker_handoff_settled(snapshot, started + 15))

    def test_old_board_without_worker_events_keeps_conservative_handoff(self):
        started = time.time() - 200
        snapshot = {"runs": [{"started_at": started, "ended_at": started + 20}]}
        self.assertFalse(task_module.worker_handoff_settled(snapshot, started + 30))
        self.assertTrue(task_module.worker_handoff_settled(snapshot, started + 170))

    def test_song_versions_keep_takes_and_hide_short_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "audio"
            audio.mkdir()
            takes = Path(folder) / "takes"
            takes.mkdir()
            first = audio / "take-1.wav"
            second = audio / "take-2.mp3"
            check = audio / "final-chorus-check.wav"
            third = takes / "03-full-song.wav"
            short_test = takes / "04-vocal-test.wav"
            for path in (first, second, check, third, short_test):
                path.write_bytes(b"draft")
            self.assertEqual(set(task_module.song_versions(folder)), {first, second, third})

    def test_missing_vocals_retry_uses_saved_lyrics_and_stops_after_one(self):
        import music_backend

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "song.mp3"
            output.write_bytes(b"saved audio")
            task = {"id": "country-task"}
            job = {
                "status": "error", "current": "The song played, but sustained vocals were not detected.",
                "vocal_retry": 0, "instrumental": False, "source_path": "",
                "lyrics": "country road and summer skies " * 5,
                "output_path": str(output), "prompt": "make a country song",
                "duration_seconds": 30,
            }
            with (patch.object(music_backend, "start_music_job", return_value={"job_id": "retry-1"}) as start,
                  patch.object(task_module, "update") as update):
                self.assertTrue(task_module.retry_song_without_vocals(task, job))
            self.assertIn("clear lead singing vocals", start.call_args.args[0])
            self.assertEqual(start.call_args.kwargs["lyrics"], job["lyrics"].strip())
            self.assertEqual(start.call_args.kwargs["vocal_retry"], 1)
            self.assertEqual(update.call_args.kwargs["card"], "music-job:retry-1")
            job["vocal_retry"] = 1
            self.assertFalse(task_module.retry_song_without_vocals(task, job))

    def test_blocked_rap_without_music_tool_recovers_to_direct_renderer(self):
        task = {
            "id": "rap-task", "status": "needs_input", "card": "t-rap",
            "brief": "make a 3 min rap with heavy bass about Cale Media",
            "detail": "The agent had no music tool.",
        }
        snapshot = {
            "task": {"status": "blocked"},
            "runs": [{"summary": "No music-generation tool was available.",
                      "started_at": 1, "ended_at": 2}],
        }
        with (patch.object(task_module, "board", side_effect=[snapshot, None]) as board,
              patch.object(task_module, "_start_music_project") as start):
            self.assertTrue(task_module.recover_blocked_music_project(task))
        self.assertEqual(board.call_args_list[-1].args, ("archive", "t-rap"))
        self.assertEqual(start.call_args.args[1]["duration_seconds"], 180)

    def test_desktop_lock_prevents_duplicate_window_and_focuses_existing(self):
        import msvcrt

        with (tempfile.TemporaryDirectory() as folder,
              patch.object(task_module, "STATE", Path(folder)),
              patch.object(task_module, "gui") as gui,
              patch.object(task_module, "focus_existing_window") as focus,
              patch.object(msvcrt, "locking", side_effect=OSError("busy"))):
            task_module.gui_single_instance()
        gui.assert_not_called()
        focus.assert_called_once()

    def test_desktop_lock_releases_after_window_closes(self):
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(task_module, "STATE", Path(folder)),
              patch.object(task_module, "gui") as gui):
            task_module.gui_single_instance()
            task_module.gui_single_instance()
        self.assertEqual(gui.call_count, 2)

    def test_spoken_replay_outcome_is_saved_for_chat_after_playback(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            task_module, "STATE", Path(folder)
        ):
            message = task_module.record_replay_outcome(18, True)
            self.assertEqual(message, "Finished reading that aloud.")
            self.assertEqual(task_module.chat_history()[-1]["content"], message)
            self.assertEqual((Path(folder) / "voice-replay-served.txt").read_text(), "18")

    def test_failed_replay_is_visible_without_marking_it_played(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            task_module, "STATE", Path(folder)
        ):
            message = task_module.record_replay_outcome(19, False)
            self.assertIn("couldn't finish", message)
            self.assertEqual(task_module.chat_history()[-1]["content"], message)
            self.assertFalse((Path(folder) / "voice-replay-served.txt").exists())

    def test_idle_launch_applies_hermes_source_fix_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "app/.git").mkdir(parents=True)
            (root / "hermes-read-consistency.patch").write_text("patch")
            (root / "hermes-host-read-consistency.patch").write_text("patch")
            replies = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
                       subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
            with (patch.object(task_module, "ROOT", root),
                  patch.object(task_module, "projects", return_value=[]),
                  patch.object(task_module.shutil, "which", return_value="git"),
                  patch.object(task_module.subprocess, "run", side_effect=replies) as run):
                task_module.sync_hermes_source_patch()
            self.assertEqual(run.call_count, 4)
            self.assertIn("--reverse", run.call_args_list[0].args[0])
            self.assertEqual(run.call_args_list[-1].args[0][-1], str(root / "hermes-host-read-consistency.patch"))

    def test_busy_tasks_defer_hermes_source_fix(self):
        with (patch.object(task_module, "projects", return_value=[{"status": "working"}]),
              patch.object(task_module.subprocess, "run") as run):
            task_module.sync_hermes_source_patch()
        run.assert_not_called()

    def test_stale_gateway_restarts_without_interrupting_live_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            (root / "data/config.yaml").write_text("model: {}")
            with (patch.object(task_module, "ROOT", root),
                  patch.object(task_module, "hermes", side_effect=[
                      "Gateway running (PID: 123)", "Restarted"
                  ]) as cli,
                  patch("psutil.Process") as process):
                process.return_value.create_time.return_value = 1
                task_module.ensure_gateway_current()
            self.assertEqual(cli.call_args.args, ("gateway", "restart", "--detach"))

    def test_live_gateway_session_defers_update_without_service_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            (root / "data/config.yaml").write_text("model: {}")
            state = root / "state"
            state.mkdir()
            refusal = RuntimeError(
                "Refusing to restart the gateway: 1 live session(s) would be interrupted."
            )
            with (patch.object(task_module, "ROOT", root),
                  patch.object(task_module, "STATE", state),
                  patch.object(task_module, "hermes", side_effect=[
                      "Gateway running (PID: 123)", refusal,
                      "Gateway running (PID: 123)", "Restarted",
                  ]),
                  patch("psutil.Process") as process):
                process.return_value.create_time.return_value = 1
                task_module.ensure_gateway_current()
                self.assertEqual(
                    json.loads((state / "gateway-update-pending.json").read_text())["reason"],
                    "live_session",
                )
                task_module.ensure_gateway_current()
            self.assertFalse((state / "gateway-update-pending.json").exists())

    def test_other_gateway_restart_errors_still_surface(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            (root / "data/config.yaml").write_text("model: {}")
            with (patch.object(task_module, "ROOT", root),
                  patch.object(task_module, "STATE", root),
                  patch.object(task_module, "hermes", side_effect=[
                      "Gateway running (PID: 123)", RuntimeError("bad configuration"),
                  ]),
                  patch("psutil.Process") as process):
                process.return_value.create_time.return_value = 1
                with self.assertRaisesRegex(RuntimeError, "bad configuration"):
                    task_module.ensure_gateway_current()

    def test_idle_health_repairs_all_services_even_if_one_fails(self):
        import cloud_ai

        with (patch.object(task_module, "ensure_ollama", side_effect=OSError("offline")) as ollama,
              patch.object(cloud_ai, "ensure_router") as router,
              patch.object(task_module, "ensure_gateway_current") as gateway):
            with self.assertRaisesRegex(RuntimeError, "Ollama.*offline"):
                task_module.ensure_idle_services()
        ollama.assert_called_once_with(None)
        router.assert_called_once_with()
        gateway.assert_called_once_with()

    def test_idle_ollama_health_does_not_rewrite_model(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"models":[]}')) as fetch:
            task_module.ensure_ollama(None)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("/api/tags", fetch.call_args.args[0])

    def test_stale_monitor_recovers_only_when_idle_and_identity_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "LongTasks"
            state.mkdir()
            heartbeat = state / "heartbeat.txt"
            heartbeat.write_text("healthy")
            os.utime(heartbeat, (1000, 1000))
            (state / "monitor-process.json").write_text(
                json.dumps({"pid": 123, "created": 500})
            )
            with (patch.object(task_module, "ROOT", root),
                  patch.object(task_module, "STATE", state),
                  patch("psutil.Process") as process):
                worker = process.return_value
                worker.create_time.return_value = 500
                worker.cmdline.return_value = [
                    str(root / "Toolbox/python/Scripts/pythonw.exe"),
                    str(root / "long_tasks.py"), "--monitor",
                ]
                self.assertFalse(task_module.monitor_restart_needed(now=1100))
                self.assertFalse(task_module.monitor_restart_needed(
                    now=1700, tasks_active=True))
                worker.terminate.assert_not_called()
                self.assertTrue(task_module.monitor_restart_needed(
                    now=1700, tasks_active=False))
                worker.terminate.assert_called_once_with()
                worker.wait.assert_called_once_with(timeout=10)

    def test_stale_pid_is_never_terminated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "LongTasks"
            state.mkdir()
            (state / "monitor-process.json").write_text(
                json.dumps({"pid": 123, "created": 500})
            )
            with (patch.object(task_module, "ROOT", root),
                  patch.object(task_module, "STATE", state),
                  patch("psutil.Process") as process):
                process.return_value.create_time.return_value = 900
                self.assertTrue(task_module.monitor_restart_needed(now=1700))
                process.return_value.terminate.assert_not_called()

    def test_supervisor_can_dispatch_ready_work_when_gateway_misses_it(self):
        with (patch.object(task_module, "projects", return_value=[{"status": "working"}]),
              patch.object(task_module, "board", return_value={"spawned": []}) as board):
            task_module.dispatch_ready_fallback()
        self.assertEqual(board.call_args.args, ("dispatch", "--max", "1", "--json"))

    def test_legacy_main_alias_is_repaired_to_64k_without_repulling_weights(self):
        requests = []

        def response(request, timeout=0):
            url = request.full_url if hasattr(request, "full_url") else request
            requests.append(url)
            if url.endswith("/api/show"):
                return io.BytesIO(json.dumps({"parameters": "num_ctx 32768\n",
                                                "capabilities": ["tools"]}).encode())
            return io.BytesIO(b'{"status":"success"}')

        with patch("urllib.request.urlopen", side_effect=response):
            task_module.ensure_ollama("qwen3-coder-local")
        self.assertEqual(requests[-1], "http://127.0.0.1:11434/api/create")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = patch.object(task_module, "STATE", self.base / "state")
        self.state.start()
        self.pid = task_module.add_project(
            "fixture", str(self.base / "project"), "Build and test a working fixture"
        )

    def tearDown(self):
        self.state.stop()
        self.temp.cleanup()

    def item(self):
        return task_module.projects()[0]

    def test_song_projects_default_to_local_ai_for_planning_too(self):
        song_id = task_module.add_project(
            "anniversary song", str(self.base / "anniversary"),
            "Create a melodic dubstep song with female vocals and a romantic chorus",
        )
        song = next(p for p in task_module.projects() if p["id"] == song_id)
        self.assertEqual(song["ai_route"], "local_only")
        self.assertEqual(self.item()["ai_route"], "automatic")

    def test_finished_time_survives_later_acceptance_and_resets_for_revision(self):
        created = self.item()["created"]
        with patch.object(task_module.time, "time", return_value=created + 60):
            task_module.update(self.pid, status="ready_for_review")
        first_finish = self.item()["finished"]
        with patch.object(task_module.time, "time", return_value=created + 3600):
            task_module.update(self.pid, status="accepted", detail="Approved later")
        self.assertEqual(self.item()["finished"], first_finish)
        self.assertEqual(self.item()["updated"], created + 3600)
        with patch.object(task_module.time, "time", return_value=created + 3700):
            task_module.update(self.pid, status="working", detail="Revising")
        self.assertEqual(self.item()["finished"], 0)
        with patch.object(task_module.time, "time", return_value=created + 4000):
            task_module.update(self.pid, status="ready_for_review")
        self.assertEqual(self.item()["finished"], created + 4000)

    def test_existing_database_gains_finished_time_for_completed_tasks(self):
        db = self.base / "state/projects.db"
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("ALTER TABLE projects RENAME TO prior_projects")
            conn.execute("""CREATE TABLE projects AS SELECT id,title,folder,brief,card,phase,
                status,detail,failures,generation,fingerprint,updated,created
                FROM prior_projects""")
            conn.execute("DROP TABLE prior_projects")
            conn.execute("UPDATE projects SET status='ready_for_review',updated=created+25")
        with task_module.database() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
        self.assertIn("finished", columns)
        self.assertEqual(self.item()["finished"], self.item()["updated"])

    def test_new_project_has_recovery_notes_without_replacing_existing_notes(self):
        notes = self.base / "project" / ".local-ai"
        self.assertIn("not written milestones", (notes / "PLAN.md").read_text())
        self.assertIn("No work has been completed", (notes / "PROGRESS.md").read_text())
        existing = self.base / "other" / ".local-ai"
        existing.mkdir(parents=True)
        (existing / "PLAN.md").write_text("Keep this plan", encoding="utf-8")
        (existing / "PROGRESS.md").write_text("Keep this progress", encoding="utf-8")
        task_module.add_project("reuse", str(existing.parent), "Continue")
        self.assertEqual((existing / "PLAN.md").read_text(), "Keep this plan")
        self.assertEqual((existing / "PROGRESS.md").read_text(), "Keep this progress")

    def test_direct_music_job_finishes_on_task_board_without_hermes(self):
        job_id = "a" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}) as start:
            pid = task_module.add_music_project(
                "make a 10-second instrumental jingle",
                {"prompt": "beach guitar", "duration_seconds": 10, "instrumental": True},
                str(self.base / "music-one"),
            )
            self.assertEqual(start.call_args.kwargs["lyrics"], "")
        task = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(task_module.music_job_id(task), job_id)
        output = Path(task["folder"]) / "song.mp3"
        output.write_bytes(b"A" * 8192)
        with patch("music_backend.music_job_status", return_value={
            "status": "complete", "instrumental": True,
            "result": {"path": str(output)},
        }), patch("music_backend.audio_file_ok", return_value=True), patch.object(task_module, "board") as board:
            task_module.advance_project(task)
            board.assert_not_called()
        complete = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(complete["status"], "ready_for_review")
        self.assertEqual(task_module.task_progress(complete)[0], 100)
        self.assertTrue((Path(task["folder"]) / ".local-ai" / "PROGRESS.md").is_file())

    def test_changed_music_artifact_is_rechecked_instead_of_needing_help(self):
        job_id = "1" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make an instrumental techno song",
                {"prompt": "instrumental techno", "duration_seconds": 30,
                 "instrumental": True}, str(self.base / "music-recheck"),
            )
        task = next(p for p in task_module.projects() if p["id"] == pid)
        output = Path(task["folder"]) / "song.mp3"
        output.write_bytes(b"A" * 8192)
        job = {"status": "complete", "instrumental": True,
               "result": {"path": str(output)}}
        with patch("music_backend.music_job_status", return_value=job), patch(
            "music_backend.audio_file_ok", return_value=True
        ):
            task_module.advance_project(task)
            reviewed = next(p for p in task_module.projects() if p["id"] == pid)
            self.assertEqual(reviewed["status"], "ready_for_review")
            (Path(task["folder"]) / "song-video.mp4").write_bytes(b"new video")
            task_module.advance_project(reviewed)
            checking = next(p for p in task_module.projects() if p["id"] == pid)
            self.assertEqual(checking["status"], "working")
            task_module.advance_project(checking)
        again = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(again["status"], "ready_for_review")
        self.assertEqual(again["fingerprint"], task_module.fingerprint(Path(task["folder"])))

    def test_old_stale_media_review_status_recovers_automatically(self):
        job_id = "2" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make an instrumental song",
                {"prompt": "instrumental", "duration_seconds": 30,
                 "instrumental": True}, str(self.base / "music-old-stale"),
            )
        task = next(p for p in task_module.projects() if p["id"] == pid)
        output = Path(task["folder"]) / "song.mp3"
        output.write_bytes(b"A" * 8192)
        task_module.update(pid, status="needs_input",
                           detail="Music files changed after review. Check them before accepting.")
        task_module.advance_project(next(p for p in task_module.projects() if p["id"] == pid))
        self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"],
                         "working")
        with patch("music_backend.music_job_status", return_value={
            "status": "complete", "instrumental": True, "result": {"path": str(output)},
        }), patch("music_backend.audio_file_ok", return_value=True):
            task_module.advance_project(next(p for p in task_module.projects() if p["id"] == pid))
        self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"],
                         "ready_for_review")

    def test_direct_music_passes_exact_user_lyrics_to_local_job(self):
        import chat_assistant

        words = "[Verse]\nWe made it home\n[Chorus]\nSing it again"
        request = "Make a 90-second soul song with these lyrics:\n" + words
        with patch("music_backend.start_music_job", return_value={"job_id": "e" * 32}) as start:
            task_module.add_music_project(
                request, chat_assistant.music_request(request), str(self.base / "music-lyrics"),
            )
        self.assertEqual(start.call_args.kwargs["lyrics"], words)
        self.assertRegex(start.call_args.kwargs["task_id"], r"^[a-f0-9]{12}$")
        self.assertEqual(start.call_args.args[0], "Make a 90-second soul song")

    def test_direct_vocal_music_cannot_finish_without_verified_vocals(self):
        job_id = "f" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make a 60-second rap song", {"prompt": "rap song", "duration_seconds": 60,
                                               "instrumental": False},
                str(self.base / "music-vocals"),
            )
        task = next(p for p in task_module.projects() if p["id"] == pid)
        output = Path(task["folder"]) / "song.mp3"
        output.write_bytes(b"A" * 8192)
        with patch("music_backend.music_job_status", return_value={
            "status": "complete", "instrumental": False, "duration_seconds": 60,
            "result": {"path": str(output)},
        }), patch("music_backend.audio_file_ok", return_value=True), patch(
            "music_backend.vocal_evidence", return_value={"passed": False}
        ):
            task_module.advance_project(task)
        current = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(current["status"], "needs_input")
        self.assertIn("too few clear vocals", current["detail"])

    def test_direct_music_stop_uses_exact_job_and_preserves_project_files(self):
        job_id = "b" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make music", {"prompt": "piano", "duration_seconds": 10, "instrumental": True},
                str(self.base / "music-two"),
            )
        task = next(p for p in task_module.projects() if p["id"] == pid)
        with patch("music_backend.stop_music_job", return_value={"status": "stopped"}) as stop, patch.object(
            task_module, "board"
        ) as board:
            task_module.request(pid, "stop")
            task_module.process_requests()
            stop.assert_called_once_with(job_id)
            board.assert_not_called()
        self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"], "stopped")
        self.assertTrue(Path(task["folder"]).is_dir())

    def test_direct_music_pause_and_continue_keep_same_job(self):
        job_id = "d" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make music", {"prompt": "piano", "duration_seconds": 10, "instrumental": True},
                str(self.base / "music-three"),
            )
        with patch("music_backend.pause_music_job") as pause, patch(
            "music_backend.resume_music_job"
        ) as resume:
            task_module.request(pid, "pause")
            task_module.process_requests()
            self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"], "paused")
            pause.assert_called_once_with(job_id)
            task_module.request(pid, "resume")
            task_module.process_requests()
            self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"], "working")
            resume.assert_called_once_with(job_id)

    def test_empty_music_revision_keeps_render_running(self):
        job_id = "f" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make music", {"prompt": "piano", "duration_seconds": 10,
                               "instrumental": True}, str(self.base / "music-empty-note"),
            )
        with patch("music_backend.stop_music_job") as stop:
            task_module.request(pid, "note", " ")
            task_module.process_requests()
            stop.assert_not_called()
        task = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(task["status"], "working")
        self.assertEqual(task_module.music_job_id(task), job_id)

    def test_add_vocals_uses_direct_music_job_and_preserves_original(self):
        job_id = "a" * 32
        revised_id = "b" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make an instrumental Michigan song", {"prompt": "instrumental Michigan song", "duration_seconds": 10,
                               "instrumental": True}, str(self.base / "music-revision"),
            )
        task = next(p for p in task_module.projects() if p["id"] == pid)
        original = Path(task["folder"]) / "song.mp3"
        original.write_bytes(b"original audio")
        plan = Path(task["folder"]) / ".local-ai/PLAN.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                        encoding="utf-8")
        with patch("music_backend.stop_music_job") as stop, patch(
            "music_backend.music_job_status", return_value={
                "output_path": str(original), "prompt": "instrumental Michigan song",
                "duration_seconds": 10, "lyrics": "", "instrumental": True,
            }
        ), patch("music_backend.audio_file_ok", return_value=True), patch(
            "music_backend.start_music_job", return_value={"job_id": revised_id}
        ) as start, patch.object(task_module, "board") as board:
            task_module.request(pid, "note", "add vocals")
            task_module.process_requests()
            stop.assert_called_once_with(job_id)
            board.assert_not_called()
        self.assertEqual(start.call_args.kwargs["task_id"], pid)
        self.assertFalse(start.call_args.kwargs["instrumental"])
        self.assertEqual(start.call_args.kwargs["lyrics"], "")
        self.assertNotIn("instrumental", start.call_args.args[0])
        self.assertEqual(Path(start.call_args.args[1]).name, "song-with-vocals.mp3")
        self.assertEqual(original.read_bytes(), b"original audio")
        revised = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(revised["status"], "working")
        self.assertEqual(task_module.music_job_id(revised), revised_id)
        self.assertIn("[x] Render the requested song", plan.read_text(encoding="utf-8"))
        self.assertIn("[ ] Apply the latest user request", plan.read_text(encoding="utf-8"))
        output = Path(start.call_args.args[1])
        output.write_bytes(b"new vocal audio")
        with patch("music_backend.music_job_status", return_value={
            "status": "complete", "instrumental": False,
            "vocal_check": {"passed": True}, "duration_seconds": 10,
            "result": {"path": str(output)},
        }), patch("music_backend.audio_file_ok", return_value=True):
            task_module.advance_project(revised)
        ready = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(ready["status"], "ready_for_review")
        self.assertIn("[x] Apply the latest user request", plan.read_text(encoding="utf-8"))
        self.assertIn("song-with-vocals.mp3", (plan.parent / "PROGRESS.md").read_text())

    def test_sparse_vocal_chop_render_finishes_without_full_song_transcription(self):
        job_id = "9" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make dubstep with vocal chops",
                {"prompt": "dubstep with minimal vocal chops", "duration_seconds": 30,
                 "instrumental": False}, str(self.base / "chop-song"),
            )
        task = next(p for p in task_module.projects() if p["id"] == pid)
        output = Path(task["folder"]) / "song.mp3"
        output.write_bytes(b"saved audio")
        with (patch("music_backend.music_job_status", return_value={
                  "status": "complete", "prompt": "dubstep with minimal vocal chops",
                  "lyrics": "", "instrumental": False, "source_path": "",
                  "duration_seconds": 30, "result": {"path": str(output)},
              }),
              patch("music_backend.audio_file_ok", return_value=True),
              patch("music_backend.vocal_evidence") as vocal_check):
            task_module.advance_project(task)
        vocal_check.assert_not_called()
        ready = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(ready["status"], "ready_for_review")
        self.assertEqual(task_module.task_progress(ready), (100, "Finished"))

    def test_complex_music_revision_still_uses_agent(self):
        job_id = "c" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make a song", {"prompt": "song", "duration_seconds": 30,
                                 "instrumental": False}, str(self.base / "music-complex"),
            )
        with patch("music_backend.stop_music_job"), patch.object(task_module, "board"):
            task_module.request(pid, "note", "keep the exact beat but change the chorus")
            task_module.process_requests()
        task = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(task["card"], "")
        self.assertEqual(task["status"], "working")

    def test_techno_instrument_feedback_starts_new_render_and_preserves_original(self):
        job_id = "d" * 32
        revised_id = "e" * 32
        with patch("music_backend.start_music_job", return_value={"job_id": job_id}):
            pid = task_module.add_music_project(
                "make a techno song", {"prompt": "techno song", "duration_seconds": 30,
                                        "instrumental": True}, str(self.base / "techno-revision"),
            )
        task = next(p for p in task_module.projects() if p["id"] == pid)
        original = Path(task["folder"]) / "song.mp3"
        original.write_bytes(b"original music")
        with patch("music_backend.stop_music_job"), patch(
            "music_backend.music_job_status", return_value={
                "output_path": str(original), "prompt": "techno song", "lyrics": "",
                "duration_seconds": 30, "instrumental": True,
            }
        ), patch("music_backend.start_music_job", return_value={"job_id": revised_id}) as start:
            task_module.request(pid, "note", "There are guitars in the techno")
            task_module.process_requests()
        self.assertIn("synth bass", start.call_args.args[0])
        self.assertNotIn("guitar", start.call_args.args[0])
        self.assertEqual(Path(start.call_args.args[1]).name, "song-revision-2.mp3")
        self.assertEqual(original.read_bytes(), b"original music")
        revised = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(revised["status"], "working")
        self.assertEqual(task_module.music_job_id(revised), revised_id)

    def test_direct_image_task_reaches_review_without_a_board_card(self):
        from PIL import Image

        job_id = "e" * 32
        with patch("quick_image.start_image_job", return_value={"job_id": job_id}):
            pid = task_module.add_image_project(
                "make an image of a dog", "a dog", str(self.base / "image-one"),
            )
        task = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(task_module.image_job_id(task), job_id)
        output = Path(task["folder"]) / "image.png"
        Image.new("RGB", (64, 64), "orange").save(output)
        with patch("quick_image.image_job_status", return_value={
            "status": "complete", "result": {"path": str(output)},
        }), patch.object(task_module, "board") as board:
            task_module.advance_project(task)
            board.assert_not_called()
        complete = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(complete["status"], "ready_for_review")
        self.assertEqual(task_module.task_progress(complete)[0], 100)
        self.assertTrue((Path(task["folder"]) / ".local-ai/PROGRESS.md").is_file())

    def test_empty_image_revision_keeps_render_running(self):
        job_id = "c" * 32
        with patch("quick_image.start_image_job", return_value={"job_id": job_id}):
            pid = task_module.add_image_project(
                "make an image of a dog", "a dog", str(self.base / "image-empty-note"),
            )
        with patch("quick_image.hold_image_job") as hold:
            task_module.request(pid, "note", " ")
            task_module.process_requests()
            hold.assert_not_called()
        task = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(task["status"], "working")
        self.assertEqual(task_module.image_job_id(task), job_id)

    def verify_script(self, text):
        (Path(self.item()["folder"]) / ".local-ai/verify.ps1").write_text(
            text, encoding="utf-8"
        )

    def test_persistence_and_overlap_guard(self):
        self.assertEqual(task_module.projects()[0]["id"], self.pid)
        self.assertTrue((self.base / "project/.local-ai/memory.sqlite3").is_file())
        with self.assertRaises(ValueError):
            task_module.add_project("bad", str(self.base / "project/child"), "overlap")
        with task_module.database() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_plain_language_task_can_name_itself(self):
        pid = task_module.add_project(
            "",
            str(self.base / "second"),
            "Make a simple recipe app\nwith large buttons",
        )
        item = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(item["title"], "Make a simple recipe app")
        self.assertTrue((Path(item["folder"]) / ".local-ai").is_dir())

    def test_chat_recalls_older_user_detail_without_trusting_old_answers(self):
        task_module.save_chat("user", "My dog's name is Rufus.")
        task_module.save_chat("assistant", "Your dog's name is Wrong.")
        for number in range(12):
            task_module.save_chat("user", f"Unrelated message {number}")
            task_module.save_chat("assistant", f"Reply {number}")
        context = task_module.chat_context("What is my dog's name?", recent=4)
        self.assertEqual(len(context), 5)
        self.assertIn("Rufus", context[0]["content"])
        self.assertIn("may be outdated", context[0]["content"])
        self.assertNotIn("Wrong", str(context))
        self.assertEqual(context[-1]["content"], "Reply 11")
        import chat_assistant
        with (patch("cloud_ai.STATE", self.base / "state"),
              patch("cloud_ai.post", return_value={"choices": [{"message": {"content": "Rufus"}}]}) as model):
            self.assertEqual(chat_assistant.local_answer("What is my dog's name?", context), "Rufus")
        self.assertTrue(any("Rufus" in item["content"] for item in model.call_args.args[1]["messages"]))

        # The insert trigger also indexes messages added after migration.
        task_module.save_chat("user", "My favorite color is teal.")
        for number in range(4):
            task_module.save_chat("assistant", f"Other reply {number}")
        later = task_module.chat_context("What is my favorite color?", recent=2)
        self.assertIn("teal", later[0]["content"])

    def test_task_saved_during_setup_starts_after_setup_finishes(self):
        task_module.update(self.pid, status="paused")
        progress = task_module.STATE / "install-progress.json"
        progress.write_text('{"status":"working"}', encoding="utf-8")
        pid = task_module.add_project(
            "", str(self.base / "second"), "Build a website", waiting_for_setup=True
        )
        with patch.object(task_module, "create_card") as create:
            task_module.tick()
            create.assert_not_called()
            self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"], "waiting_for_setup")
            progress.write_text('{"status":"complete"}', encoding="utf-8")
            task_module.tick()
            self.assertEqual(create.call_count, 1)
            self.assertEqual(create.call_args.args[0]["id"], pid)
            self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"], "queued")

    def test_optional_tool_update_does_not_hold_core_tasks(self):
        self.assertTrue(task_module.setup_blocks_tasks({"status": "working"}))
        self.assertFalse(task_module.setup_blocks_tasks({
            "status": "working", "owner": "runtime_features"
        }))
        task_module.update(self.pid, status="paused")
        progress = task_module.STATE / "install-progress.json"
        progress.write_text(json.dumps({
            "status": "working", "owner": "runtime_features",
            "current": "Adding the local website crawler",
        }), encoding="utf-8")
        pid = task_module.add_project(
            "", str(self.base / "optional-tools"), "Build a website", waiting_for_setup=True
        )
        with patch.object(task_module, "create_card") as create:
            task_module.tick()
        create.assert_called_once()
        self.assertEqual(create.call_args.args[0]["id"], pid)
        self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"], "queued")

    def test_first_launch_readiness_waits_for_setup_and_first_file_scan(self):
        progress = task_module.STATE / "install-progress.json"
        progress.write_text(json.dumps({
            "status": "working", "current": "Downloading your main AI model",
        }), encoding="utf-8")
        ready, message = task_module.first_launch_readiness(task_module.STATE)
        self.assertFalse(ready)
        self.assertIn("Downloading your main AI model", message)

        progress.write_text('{"status":"complete"}', encoding="utf-8")
        ready, message = task_module.first_launch_readiness(task_module.STATE)
        self.assertFalse(ready)
        self.assertIn("index", message.lower())

        import file_catalog
        (task_module.STATE / file_catalog.STATUS_NAME).write_text(json.dumps({
            "status": "working", "files": 1234,
        }), encoding="utf-8")
        ready, message = task_module.first_launch_readiness(task_module.STATE)
        self.assertFalse(ready)
        self.assertIn("1,234", message)

        (task_module.STATE / file_catalog.FIRST_SCAN_NAME).write_text("complete")
        ready, message = task_module.first_launch_readiness(task_module.STATE)
        self.assertTrue(ready)
        self.assertEqual(message, "")

    def test_optional_tool_failure_releases_previously_waiting_task(self):
        task_module.update(self.pid, status="paused")
        progress = task_module.STATE / "install-progress.json"
        progress.write_text(json.dumps({
            "status": "failed", "owner": "runtime_features", "attempts": 3,
            "current": "Optional browser tool update failed",
        }), encoding="utf-8")
        pid = task_module.add_project(
            "", str(self.base / "optional-failure"), "Write a document",
            waiting_for_setup=True,
        )
        with patch.object(task_module, "create_card") as create:
            task_module.tick()
        create.assert_called_once()
        self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"], "queued")

    def test_music_saved_during_setup_keeps_direct_music_route(self):
        task_module.update(self.pid, status="paused")
        progress = task_module.STATE / "install-progress.json"
        progress.write_text('{"status":"working"}', encoding="utf-8")
        spec = {"prompt": "Michigan rap", "lyrics": "", "duration_seconds": 180,
                "instrumental": False}
        pid = task_module.add_project(
            "", str(self.base / "song"), "Make a 3 minute rap song",
            waiting_for_setup=True, pending_media={"kind": "music", "spec": spec},
        )
        import music_backend
        with (patch.object(task_module, "create_card") as create,
              patch.object(music_backend, "start_music_job", return_value={"job_id": "song-1"}) as start):
            task_module.tick()
            start.assert_not_called()
            progress.write_text('{"status":"complete"}', encoding="utf-8")
            task_module.tick()
            start.assert_called_once()
            create.assert_not_called()
        item = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(item["card"], "music-job:song-1")
        self.assertEqual(item["status"], "working")
        self.assertFalse((Path(item["folder"]) / ".local-ai/pending-media.json").exists())

    def test_direct_music_card_uses_worker_stage_progress(self):
        import music_backend

        task_module.update(self.pid, card="music-job:abc123", status="working")
        for percent, stage in ((5, "Starting local music"),
                               (35, "Generating music locally"),
                               (75, "Checking the vocals"),
                               (88, "Making a video with captions")):
            with patch.object(music_backend, "music_job_status", return_value={
                "status": "working", "progress": percent, "current": stage,
            }):
                self.assertEqual(task_module.task_progress(self.item()), (percent, stage))
        # A stale worker record cannot make an unfinished task look finished.
        with patch.object(music_backend, "music_job_status", return_value={
            "status": "working", "progress": 100, "current": "Writing the video",
        }):
            self.assertEqual(task_module.task_progress(self.item())[0], 99)

    def test_direct_music_card_shows_measured_eta(self):
        import music_backend

        with patch.object(music_backend, "music_job_status", return_value={
            "status": "working", "requested_at": 1000, "estimated_seconds": 90,
        }), patch.object(task_module.task_eta.time, "time", return_value=1030):
            self.assertEqual(task_module.music_task_countdown("abc123"),
                             "About 1m 00s left · estimate from recent songs")

    def test_image_saved_during_setup_keeps_direct_image_route(self):
        task_module.update(self.pid, status="paused")
        progress = task_module.STATE / "install-progress.json"
        progress.write_text('{"status":"working"}', encoding="utf-8")
        pid = task_module.add_project(
            "", str(self.base / "image"), "Make an image of a dog",
            waiting_for_setup=True, pending_media={"kind": "image", "prompt": "a dog"},
        )
        import quick_image
        with (patch.object(task_module, "create_card") as create,
              patch.object(quick_image, "start_image_job", return_value={"job_id": "image-1"}) as start):
            task_module.tick()
            start.assert_not_called()
            progress.write_text('{"status":"complete"}', encoding="utf-8")
            task_module.tick()
            start.assert_called_once()
            create.assert_not_called()
        item = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(item["card"], "image-job:image-1")
        self.assertEqual(item["status"], "working")

    def test_waiting_task_shows_setup_failure(self):
        progress = task_module.STATE / "install-progress.json"
        task_module.update(self.pid, status="waiting_for_setup")
        for status in ("failed", "error"):
            progress.write_text(
                json.dumps({"status": status, "current": "Python tool installation failed"}),
                encoding="utf-8",
            )
            _, step = task_module.task_progress(self.item())
            self.assertIn("needs attention", step)
            self.assertIn("Python tool installation failed", step)

    def test_exhausted_setup_reports_failure_then_auto_resumes_after_repair(self):
        task_module.update(self.pid, status="paused")
        progress = task_module.STATE / "install-progress.json"
        progress.write_text(json.dumps({
            "status": "error", "current": "Setup needs attention: AI model installation failed",
        }), encoding="utf-8")
        pid = task_module.add_project(
            "", str(self.base / "setup-delayed"), "Build a website", waiting_for_setup=True,
        )
        with patch.object(task_module, "create_card") as create:
            with patch.object(task_module.setup_recovery, "repair_exhausted", return_value=True):
                task_module.tick()
            item = next(p for p in task_module.projects() if p["id"] == pid)
            self.assertEqual(item["status"], "needs_input")
            self.assertIn("automatic retries", item["detail"])
            self.assertIn("automatic retries", task_module.task_progress(item)[1])
            create.assert_not_called()
            progress.write_text('{"status":"complete"}', encoding="utf-8")
            task_module.tick()
            item = next(p for p in task_module.projects() if p["id"] == pid)
            self.assertEqual(item["status"], "queued")
            create.assert_called_once()

    def test_followup_to_setup_task_stays_waiting(self):
        progress = task_module.STATE / "install-progress.json"
        progress.write_text('{"status":"working"}', encoding="utf-8")
        pid = task_module.add_project(
            "", str(self.base / "second"), "Build a website", waiting_for_setup=True
        )
        task_module.request(pid, "note", "Add large buttons")
        task_module.process_requests()
        updated = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(updated["status"], "waiting_for_setup")
        self.assertEqual(updated["brief"], "Build a website")
        self.assertIn("Add large buttons", (Path(updated["folder"]) / ".local-ai/USER-REQUESTS.md").read_text())

    def test_new_worker_receives_saved_plan_and_latest_progress(self):
        notes = Path(self.item()["folder"]) / ".local-ai"
        (self.base / "project/existing.py").write_text(
            "def existing_beach_planner(): return 1", encoding="utf-8"
        )
        (notes / "PLAN.md").write_text("Milestone: build search", encoding="utf-8")
        (notes / "PROGRESS.md").write_text(
            "x" * 6500 + "\nNext concrete step: test search", encoding="utf-8"
        )
        with patch.object(task_module, "board", return_value={"id": "card-1"}) as board:
            task_module.create_card(self.item())
        args = board.call_args.args
        body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.assertIn("Milestone: build search", body)
        self.assertIn("Next concrete step: test search", body)
        self.assertNotIn("x" * 6500, body)
        self.assertTrue(task_module.project_memory.search(
            self.base / "project", "existing_beach_planner", kind="file"
        ))

    def test_recovery_keeps_early_acceptance_checks_and_latest_progress(self):
        notes = Path(self.item()["folder"]) / ".local-ai"
        (notes / "PLAN.md").write_text(
            "Acceptance: retain offline mode\n" + "middle\n" * 1500 + "Last milestone: packaging",
            encoding="utf-8",
        )
        (notes / "PROGRESS.md").write_text(
            "Initial setup complete\n" + "past work\n" * 1500 + "Next concrete step: run installer",
            encoding="utf-8",
        )
        snapshot = task_module.recovery_snapshot(self.item()["folder"])
        self.assertIn("Acceptance: retain offline mode", snapshot)
        self.assertIn("Last milestone: packaging", snapshot)
        self.assertIn("Next concrete step: run installer", snapshot)
        self.assertIn("characters omitted; re-read PLAN.md", snapshot)
        self.assertLess(len(snapshot), 13000)

    def test_recovery_of_large_notes_still_keeps_first_and_latest_requirements(self):
        notes = Path(self.item()["folder"]) / ".local-ai"
        (notes / "PLAN.md").write_text(
            "First requirement: work offline\n" + "old milestone\n" * 100_000
            + "Final requirement: export the app", encoding="utf-8",
        )
        (notes / "PROGRESS.md").write_text(
            "Original setup\n" + "completed work\n" * 100_000
            + "Next action: run the installer", encoding="utf-8",
        )
        snapshot = task_module.recovery_snapshot(self.item()["folder"])
        self.assertIn("First requirement: work offline", snapshot)
        self.assertIn("Final requirement: export the app", snapshot)
        self.assertIn("Next action: run the installer", snapshot)
        self.assertIn("large file truncated", snapshot)
        self.assertLess(len(snapshot), 13_000)

    def test_deferred_model_update_retries_after_active_task_finishes(self):
        state = self.base / "model-status"
        state.mkdir()
        (state / "auto-model-worker.json").write_text('{"status":"complete"}')
        (state / "optional-model-wide.json").write_text('{"status":"waiting_tasks"}')
        self.assertFalse(task_module.deferred_model_retry_ready(
            state, [{"status": "working"}]
        ))
        self.assertTrue(task_module.deferred_model_retry_ready(
            state, [{"status": "ready_for_review"}]
        ))

    def test_token_legend_keeps_full_distinct_model_names_on_the_graph(self):
        models = [
            {"model": "qwen3-coder-local", "tokens": 267100},
            {"model": "qwen3-coder-permissive", "tokens": 34606},
            {"model": "provider/extra-long-model-name-with-many-parts", "tokens": 1200},
        ]
        height, width, entries = task_module.token_legend_layout(models, 560)
        self.assertGreater(height, 150)
        self.assertGreater(width, 0)
        self.assertEqual(
            [entry["label"].replace("\n", "") for entry in entries],
            [item["model"] for item in models],
        )
        self.assertEqual(len({(entry["x"], entry["y"]) for entry in entries}), 3)

    def test_fresh_placeholder_notes_do_not_bloat_first_worker_prompt(self):
        self.assertEqual(task_module.recovery_snapshot(self.item()["folder"]), "")

    def test_new_worker_receives_relevant_durable_memory(self):
        import project_memory

        project_memory.save(
            self.item()["folder"], "decision", "Fixture database choice",
            "Use SQLite for the working fixture database.",
        )
        with patch.object(task_module, "board", return_value={"id": "card-1"}) as board:
            task_module.create_card(self.item())
        args = board.call_args.args
        body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.assertIn("task progress bar reads these marks", body)
        self.assertIn("Current task:", body)
        self.assertIn("Fixture database choice", body)
        self.assertIn("project_memory_search", body)
        self.assertIn("a lyrics file is not proof", body)
        self.assertIn("never attach an empty placeholder", body)

    def test_real_failing_check_cannot_pass(self):
        self.verify_script('Write-Output "actual check failed"\nexit 7\n')
        ok, text = task_module.verify_project(self.item())
        self.assertFalse(ok)
        self.assertIn("exit code: 7", text)

    def test_missing_check_cannot_pass(self):
        self.assertFalse(task_module.verify_project(self.item())[0])

    def test_finished_song_gets_independent_audio_check_without_worker_script(self):
        task_module.update(self.pid, brief="Make a 0.1 minute rap song with vocals")
        project = self.base / "project"
        with wave.open(str(project / "song_final.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * (6 * 8000))
        (project / "lyrics.txt").write_text("Michigan and Cale Media make the beat. " * 8)
        with (patch("music_backend.audio_file_ok", return_value=True),
              patch("music_backend.vocal_evidence", return_value={
                  "passed": True, "recognized_words": 45,
              })):
            passed, evidence = task_module.verify_project(self.item())
        self.assertTrue(passed)
        self.assertIn("6.0s", evidence)
        self.assertIn("45 recognized", evidence)

    def test_song_without_saved_audio_fails_instead_of_claiming_completion(self):
        task_module.update(self.pid, brief="Make a 3 minute rap song")
        passed, evidence = task_module.verify_project(self.item())
        self.assertFalse(passed)
        self.assertIn("No finished audio", evidence)

    def test_finished_mp3_uses_duration_probe(self):
        task_module.update(self.pid, brief="Make a 0.1 minute instrumental song")
        (self.base / "project/song_final.mp3").write_bytes(b"encoded")
        with (patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="ffmpeg"),
              patch.object(task_module.subprocess, "run", return_value=subprocess.CompletedProcess(
                  [], 1, "", "Duration: 00:00:06.00, start: 0.000000, bitrate: 128 kb/s"
              )),
              patch("music_backend.audio_file_ok", return_value=True)):
            passed, evidence = task_module.verify_project(self.item())
        self.assertTrue(passed)
        self.assertIn("song_final.mp3", evidence)

    def test_finished_scratch_audio_is_recovered_into_project(self):
        root = self.base / "app"
        scratch = root / "data/cache/scratch"
        scratch.mkdir(parents=True)
        source = scratch / "song_final.wav"
        source.write_bytes(b"RIFF" + b"audio" * 100)
        snapshot = {"runs": [{"metadata": json.dumps({"artifacts": [str(source)]})}]}
        with patch.object(task_module, "ROOT", root):
            task_module.recover_media_artifacts(self.item(), snapshot)
        self.assertEqual((self.base / "project/song_final.wav").read_bytes(), source.read_bytes())

    def test_finished_scratch_image_and_video_are_recovered_but_external_files_are_not(self):
        root = self.base / "app"
        scratch = root / "data/cache/scratch"
        scratch.mkdir(parents=True)
        image = scratch / "beach.png"
        video = scratch / "preview.mp4"
        external = self.base / "private.png"
        image.write_bytes(b"image output")
        video.write_bytes(b"video output")
        external.write_bytes(b"outside scratch")
        snapshot = {"runs": [{"metadata": {"artifacts": [
            str(image), str(video), str(external),
        ]}}]}
        with patch.object(task_module, "ROOT", root):
            task_module.recover_media_artifacts(self.item(), snapshot)
        project = self.base / "project"
        self.assertEqual((project / "beach.png").read_bytes(), image.read_bytes())
        self.assertEqual((project / "preview.mp4").read_bytes(), video.read_bytes())
        self.assertFalse((project / "private.png").exists())

    def test_hermes_preserved_media_is_recovered_only_from_its_own_card(self):
        root = self.base / "app"
        item = self.item()
        card = "t_123"
        task_attachments = root / "data/kanban/boards/long-tasks/attachments" / card
        other_attachments = task_attachments.parent / "t_other"
        task_attachments.mkdir(parents=True)
        other_attachments.mkdir(parents=True)
        image = task_attachments / "finished.webp"
        other = other_attachments / "other.webp"
        no_card = task_attachments / "no-card.webp"
        image.write_bytes(b"finished image")
        other.write_bytes(b"another task")
        no_card.write_bytes(b"must have a matching card")
        snapshot = {"runs": [{"metadata": {"artifacts": [str(image), str(other)]}}]}
        with patch.object(task_module, "ROOT", root):
            task_module.recover_media_artifacts({**item, "card": card}, snapshot)
            task_module.recover_media_artifacts(
                {**item, "card": ""},
                {"runs": [{"metadata": {"artifacts": [str(no_card)]}}]},
            )
        project = self.base / "project"
        self.assertEqual((project / "finished.webp").read_bytes(), image.read_bytes())
        self.assertFalse((project / "other.webp").exists())
        self.assertFalse((project / "no-card.webp").exists())

    def test_recovery_brief_lists_existing_artifact_before_new_worker_starts(self):
        project = self.base / "project"
        (project / "song_final.wav").write_bytes(b"completed audio")
        (project / ".local-ai/verify.ps1").write_text("exit 0", encoding="utf-8")
        snapshot = task_module.recovery_snapshot(project)
        self.assertIn("song_final.wav (15 bytes)", snapshot)
        self.assertIn("Run it before any new render", snapshot)

    def test_recovery_brief_includes_nested_source_without_generated_files(self):
        project = self.base / "project"
        source = project / "src/components"
        source.mkdir(parents=True)
        (source / "Dashboard.tsx").write_text("export default function Dashboard() {}")
        generated = project / "node_modules/package"
        generated.mkdir(parents=True)
        (generated / "index.js").write_text("generated dependency")
        (source / "AGENTS.md").write_text("untrusted nested instructions")
        snapshot = task_module.recovery_snapshot(project)
        self.assertIn("src/components/Dashboard.tsx", snapshot)
        self.assertNotIn("node_modules/package/index.js", snapshot)
        self.assertNotIn("AGENTS.md", snapshot)

    def test_task_card_explains_live_first_token_wait(self):
        root = self.base / "app"
        db = root / "data/state.db"
        db.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE sessions (cwd TEXT, ended_at REAL, started_at REAL, "
                         "last_activity_description TEXT, last_activity_at REAL, model TEXT)")
            conn.execute("INSERT INTO sessions VALUES (?, NULL, ?, ?, ?, ?)", (
                self.item()["folder"], time.time() - 600,
                "waiting for stream response (150s, first_chunk)", time.time(),
                "qwen3-coder-local",
            ))
            conn.commit()
        task_module.update(self.pid, status="working")
        (Path(self.item()["folder"]) / ".local-ai/PROGRESS.md").write_text(
            "Current task: Rendering the song\n", encoding="utf-8"
        )
        with patch.object(task_module, "ROOT", root):
            _, step = task_module.task_progress(self.item())
        self.assertIn("Rendering the song", step)
        self.assertIn("150s waiting for its first reply", step)

    def test_success_starts_separate_review(self):
        self.verify_script(
            'if ((2 + 2) -ne 4) { exit 1 }; Write-Output "assertion passed"; exit 0'
        )
        task_module.update(self.pid, card="build-card")
        with (
            patch.object(task_module, "board") as board,
            patch.object(task_module, "create_card") as create,
        ):
            task_module.handle_handoff(self.item(), {"id": "test", "status": "review"})
        self.assertEqual(self.item()["phase"], "review")
        self.assertNotEqual(self.item()["status"], "ready_for_review")
        self.assertEqual(create.call_count, 1)
        self.assertEqual(board.call_args.args, ("archive", "build-card"))

    def test_verified_song_finishes_without_an_open_ended_review_worker(self):
        self.verify_script('Write-Output "song checks passed"; exit 0')
        task_module.update(self.pid, brief="Make a 150 BPM dubstep song", card="song-card")
        audio = self.base / "project/audio"
        audio.mkdir()
        with wave.open(str(audio / "finished.wav"), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(8000)
            recording.writeframes(b"\x01\x00" * 8000)
        with (
            patch.object(task_module, "board") as board,
            patch.object(task_module, "create_card") as create,
        ):
            task_module.handle_handoff(self.item(), {"id": "song-card", "status": "review"})
        finished = self.item()
        self.assertEqual(board.call_args.args, ("archive", "song-card"))
        create.assert_not_called()
        self.assertEqual(finished["status"], "ready_for_review")
        self.assertEqual(finished["card"], "")
        self.assertEqual(task_module.task_progress(finished), (100, "Finished"))
        self.assertGreater(finished["finished"], 0)

    def test_audio_handoff_finds_saved_takes(self):
        task_module.update(self.pid, brief="Make a song with clear female singing")
        project = self.base / "project"
        takes = project / "takes"
        takes.mkdir()
        with wave.open(str(takes / "final.wav"), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(8000)
            recording.writeframes(b"\x01\x00" * (8000 * 6))
        (project / "lyrics.txt").write_text("sing " * 25, encoding="utf-8")
        with (patch("music_backend.audio_file_ok", return_value=True),
              patch("music_backend.vocal_evidence", return_value={
                  "passed": True, "recognized_words": 22,
              })):
            passed, evidence = task_module.verify_audio_handoff(self.item())
        self.assertTrue(passed)
        self.assertIn("final.wav", evidence)

    def test_audio_handoff_skips_newer_vocal_test(self):
        task_module.update(self.pid, brief="Make a song with clear female singing")
        project = self.base / "project"
        takes = project / "takes"
        takes.mkdir()
        for name in ("01-full-song.wav", "02-vocal-test.wav"):
            with wave.open(str(takes / name), "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(8000)
                recording.writeframes(b"\x01\x00" * (8000 * 6))
        (project / "lyrics.txt").write_text("sing " * 25, encoding="utf-8")
        with (patch("music_backend.audio_file_ok", return_value=True),
              patch("music_backend.vocal_evidence", return_value={
                  "passed": True, "recognized_words": 22,
              }) as vocals):
            passed, evidence = task_module.verify_audio_handoff(self.item())
        self.assertTrue(passed)
        self.assertIn("01-full-song.wav", evidence)
        self.assertEqual(vocals.call_args.args[0].name, "01-full-song.wav")

    def test_review_requires_fresh_passing_checks(self):
        self.verify_script('Write-Output "assertions passed"; exit 0')
        task_module.update(self.pid, phase="review", card="test")
        (self.base / "project/code.py").write_text(
            "def beach_invoice_total(): return 42", encoding="utf-8"
        )
        task_module.handle_handoff(self.item(), {"id": "test", "status": "review"})
        self.assertEqual(self.item()["status"], "ready_for_review")
        self.assertTrue(task_module.project_memory.search(
            self.base / "project", "beach_invoice_total", kind="file"
        ))
        (self.base / "project/code.py").write_text("changed", encoding="utf-8")
        with patch.object(task_module, "board"), patch.object(task_module, "create_card") as create:
            task_module.tick()
        self.assertEqual(self.item()["status"], "working")
        self.assertEqual(self.item()["phase"], "review")
        self.assertEqual(self.item()["fingerprint"], "")
        self.assertEqual(create.call_count, 1)
        self.assertIn("Preserve user edits", create.call_args.args[1])

    def test_finished_task_shows_full_progress_without_rating_controls(self):
        task_module.update(self.pid, status="ready_for_review")
        self.assertEqual(task_module.task_progress(self.item()), (100, "Finished"))
        labels = [label for label, _ in task_module.task_controls_for("ready_for_review")]
        self.assertEqual(labels, ["Reuse prompt", "Delete"])
        self.assertEqual(task_module.task_controls_for("accepted"),
                         task_module.task_controls_for("ready_for_review"))

    def test_listening_review_finishes_verified_audio_and_freezes_timer(self):
        task_module.update(
            self.pid, brief="Make a 0.1 minute dubstep banger with minimal vocal chops",
            card="song-card",
            status="needs_input",
        )
        folder = Path(self.item()["folder"])
        (folder / "Audio").mkdir()
        with wave.open(str(folder / "Audio/final.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * (6 * 8000))
        snapshot = {
            "task": {"status": "blocked"},
            "latest_summary": "Review handoff was rejected; please listen to the finished audio.",
            "runs": [{"started_at": time.time() - 300, "ended_at": time.time() - 200}],
        }
        with (patch.object(task_module, "board") as board,
              patch.object(task_module, "verify_project", return_value=(True, "3 checks passed")),
              patch("music_backend.audio_file_ok", return_value=True)):
            self.assertTrue(task_module.recover_blocked_audio_review(self.item(), snapshot))
        board.assert_called_once_with("archive", "song-card")
        finished = self.item()
        self.assertEqual(task_module.task_progress(finished), (100, "Finished"))
        self.assertEqual(finished["status"], "ready_for_review")
        self.assertGreater(finished["finished"], 0)
        self.assertIn("not independently confirmed", finished["detail"])
        self.assertFalse(task_module.recover_blocked_audio_review(finished, snapshot))

    def test_subjective_song_review_blocker_finishes_from_saved_master(self):
        self.verify_script('Write-Output "audio checks passed"; exit 0')
        task_module.update(
            self.pid, brief="Make a dubstep song with female vocals",
            phase="review", card="review-card", status="needs_input",
        )
        audio = self.base / "project/audio"
        audio.mkdir()
        with wave.open(str(audio / "master.wav"), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(8000)
            recording.writeframes(b"\x01\x00" * 8000)
        snapshot = {
            "task": {"status": "blocked"},
            "latest_summary": "Please listen to saved versions and identify which saved version you like best.",
            "runs": [{"started_at": time.time() - 300, "ended_at": time.time() - 200}],
        }
        with (patch.object(task_module, "board") as board,
              patch.object(task_module, "verify_project", return_value=(True, "checks passed"))):
            self.assertTrue(task_module.recover_blocked_audio_review(self.item(), snapshot))
        board.assert_called_once_with("archive", "review-card")
        self.assertEqual(self.item()["status"], "ready_for_review")

    def test_subjective_song_review_stays_open_when_checks_fail(self):
        self.verify_script('exit 1')
        task_module.update(
            self.pid, brief="Make a dubstep song", phase="review",
            card="review-card", status="needs_input",
        )
        audio = self.base / "project/audio"
        audio.mkdir()
        (audio / "master.mp3").write_bytes(b"saved but unverified")
        snapshot = {
            "task": {"status": "blocked"},
            "latest_summary": "Please listen and choose your favorite take.",
            "runs": [{"started_at": time.time() - 300, "ended_at": time.time() - 200}],
        }
        with (patch.object(task_module, "board") as board,
              patch.object(task_module, "verify_project", return_value=(False, "failed"))):
            self.assertFalse(task_module.recover_blocked_audio_review(self.item(), snapshot))
        board.assert_not_called()
        self.assertEqual(self.item()["status"], "needs_input")

    def test_concurrent_audio_completion_does_not_report_archive_failure(self):
        task_module.update(
            self.pid, brief="Make a 0.1 minute dubstep banger with vocal chops",
            card="song-card", status="needs_input",
        )
        folder = Path(self.item()["folder"])
        (folder / "Audio").mkdir()
        with wave.open(str(folder / "Audio/final.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * (6 * 8000))
        snapshot = {
            "task": {"status": "blocked"},
            "latest_summary": "Review handoff was rejected; listen to the finished audio.",
            "runs": [{"started_at": time.time() - 300, "ended_at": time.time() - 200}],
        }

        def archive(*args, **kwargs):
            self.assertEqual(args, ("archive", "song-card"))
            task_module.update(self.pid, status="ready_for_review", card="", detail="Finished")
            raise RuntimeError("cannot archive song-card")

        with (patch.object(task_module, "board", side_effect=archive),
              patch.object(task_module, "verify_project", return_value=(True, "checks passed")),
              patch("music_backend.audio_file_ok", return_value=True)):
            self.assertTrue(task_module.recover_blocked_audio_review(self.item(), snapshot))
        self.assertEqual(self.item()["status"], "ready_for_review")
        self.assertEqual(self.item()["detail"], "Finished")

    def test_stale_supervisor_error_does_not_overwrite_new_completion(self):
        task_module.update(self.pid, card="song-card", status="working")

        def concurrent_completion(_task):
            task_module.update(self.pid, card="", status="ready_for_review", detail="Finished")
            raise RuntimeError("old board card disappeared")

        with patch.object(task_module, "advance_project", side_effect=concurrent_completion):
            task_module.tick()
        self.assertEqual(self.item()["status"], "ready_for_review")
        self.assertEqual(self.item()["detail"], "Finished")

    def test_unverified_or_unrelated_blocker_stays_blocked(self):
        task_module.update(self.pid, card="build-card", status="needs_input")
        snapshot = {
            "task": {"status": "blocked"},
            "latest_summary": "Review handoff was rejected; please listen to the result.",
            "runs": [{"started_at": time.time() - 300, "ended_at": time.time() - 200}],
        }
        with patch.object(task_module, "board") as board:
            self.assertFalse(task_module.recover_blocked_audio_review(self.item(), snapshot))
        board.assert_not_called()
        self.assertEqual(self.item()["status"], "needs_input")

    def test_new_live_board_run_clears_stale_help_state(self):
        task_module.update(self.pid, card="build-card", status="needs_input",
                           detail="Old blocker")
        blocked = self.item()
        snapshot = {
            "task": {"status": "running"},
            "runs": [{"status": "blocked", "started_at": blocked["updated"] - 5,
                      "ended_at": blocked["updated"] - 2},
                     {"status": "running", "started_at": blocked["updated"] + 2,
                      "ended_at": None}],
        }
        with (patch.object(task_module, "board", return_value=snapshot),
              patch.object(task_module, "recover_blocked_music_project", return_value=False),
              patch.object(task_module, "recover_blocked_audio_review", return_value=False)):
            task_module.advance_project(blocked)
        resumed = self.item()
        self.assertEqual(resumed["status"], "working")
        self.assertIn("working again", resumed["detail"])

    def test_old_board_run_does_not_clear_newer_blocker(self):
        task_module.update(self.pid, card="build-card", status="needs_input",
                           detail="Current blocker")
        blocked = self.item()
        snapshot = {
            "task": {"status": "running"},
            "runs": [{"status": "running", "started_at": blocked["updated"] - 10,
                      "ended_at": None}],
        }
        with (patch.object(task_module, "board", return_value=snapshot),
              patch.object(task_module, "recover_blocked_music_project", return_value=False),
              patch.object(task_module, "recover_blocked_audio_review", return_value=False)):
            task_module.advance_project(blocked)
        self.assertEqual(self.item()["status"], "needs_input")

    def test_old_changed_project_review_recovers_without_user_action(self):
        task_module.update(
            self.pid, card="old-review", phase="review", status="needs_input",
            detail="Files changed after verification. Resume to check them again.",
        )
        with patch.object(task_module, "board"), patch.object(task_module, "create_card") as create:
            task_module.tick()
        self.assertEqual(self.item()["status"], "working")
        self.assertEqual(self.item()["generation"], 1)
        self.assertEqual(create.call_count, 1)

    def test_three_failures_stop_automatic_repairs(self):
        self.verify_script("exit 3")
        with (
            patch.object(task_module, "board") as board,
            patch.object(task_module, "create_card") as create,
        ):
            for _ in range(3):
                task_module.handle_handoff(
                    self.item(), {"id": "test", "status": "review"}
                )
        self.assertEqual(self.item()["failures"], 3)
        self.assertEqual(self.item()["status"], "needs_input")
        self.assertEqual(create.call_count, 2)
        board.assert_not_called()

    def test_verified_build_resets_failures_before_independent_review(self):
        task_module.update(self.pid, card="build-card", failures=2)
        with (
            patch.object(task_module, "verify_project",
                         side_effect=[(True, "build checks passed"),
                                      (False, "review found a missing behavior")]),
            patch.object(task_module, "board"),
            patch.object(task_module, "create_card") as create,
        ):
            task_module.handle_handoff(self.item(), {"id": "build-card", "status": "review"})
            self.assertEqual(self.item()["phase"], "review")
            self.assertEqual(self.item()["failures"], 0)
            task_module.handle_handoff(self.item(), {"id": "review-card", "status": "review"})
        self.assertEqual(self.item()["failures"], 1)
        self.assertEqual(self.item()["status"], "working")
        self.assertEqual(create.call_count, 2)

    def test_verified_task_archives_ready_card_from_older_generation_once(self):
        folder = self.base / "project"
        task_module.update(
            self.pid, card="current-review-card", phase="review",
            status="ready_for_review", fingerprint=task_module.fingerprint(folder),
        )
        listing = [
            {"id": "ready-card", "status": "ready", "workspace_path": str(folder)},
            {"id": "current-review-card", "status": "review", "workspace_path": str(folder)},
            {"id": "other-card", "status": "ready", "workspace_path": str(self.base / "other")},
        ]
        with (
            patch.object(task_module, "REVIEW_PROJECTS_CHECKED", {}),
            patch.object(task_module, "board", side_effect=[listing, "Archived"]) as board,
        ):
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "ready_for_review")
        self.assertEqual([call.args[0] for call in board.call_args_list], ["list", "archive"])
        self.assertEqual(board.call_args.args[1], "ready-card")

    def test_verified_task_blocks_running_worker_before_archiving(self):
        folder = self.base / "project"
        task_module.update(
            self.pid, card="running-card", phase="review",
            status="ready_for_review", fingerprint=task_module.fingerprint(folder),
        )
        states = iter(("running", "triage"))

        def board_call(command, *args, **kwargs):
            if command == "list":
                return [{"id": "running-card", "status": next(states),
                         "workspace_path": str(folder)}]
            return "ok"

        with (
            patch.object(task_module, "REVIEW_PROJECTS_CHECKED", {}),
            patch.object(task_module, "board", side_effect=board_call) as board,
        ):
            task_module.tick()
            task_module.tick()
        self.assertEqual(
            [call.args[0] for call in board.call_args_list],
            ["list", "block", "list", "archive"],
        )

    def test_new_review_generation_rechecks_old_cards(self):
        folder = self.base / "project"
        old = {
            **self.item(), "card": "review-0", "generation": 0,
        }
        newer = {**old, "card": "review-1", "generation": 1}
        listings = [
            [{"id": "review-0", "status": "review", "workspace_path": str(folder)}],
            [{"id": "review-0", "status": "ready", "workspace_path": str(folder)}],
        ]

        def board_call(command, *args, **kwargs):
            return listings.pop(0) if command == "list" else "Archived"

        with (
            patch.object(task_module, "REVIEW_PROJECTS_CHECKED", {}),
            patch.object(task_module, "board", side_effect=board_call) as board,
        ):
            task_module.retire_review_cards(old)
            task_module.retire_review_cards(old)
            task_module.retire_review_cards(newer)
        self.assertEqual(
            [call.args[0] for call in board.call_args_list],
            ["list", "list", "archive"],
        )

    def test_review_feedback_starts_a_new_saved_generation(self):
        task_module.update(self.pid, card="review-card", phase="review", status="ready_for_review")
        task_module.request(self.pid, "note", "Add a larger search box")
        with patch.object(
            task_module, "board", return_value={"task": {"status": "review"}}
        ) as board:
            task_module.process_requests()
        updated = self.item()
        self.assertEqual(updated["card"], "")
        self.assertEqual(updated["generation"], 1)
        self.assertEqual(updated["status"], "working")
        self.assertEqual(updated["brief"], "Build and test a working fixture")
        self.assertIn("Add a larger search box", (Path(updated["folder"]) / ".local-ai/USER-REQUESTS.md").read_text())
        self.assertFalse(any(call.args[0] == "reopen-review" for call in board.call_args_list))

    def test_chat_revision_reopens_an_accepted_project(self):
        task_module.update(self.pid, card="archived-card", phase="review", status="accepted")
        task_module.request(self.pid, "note", "Make the finished app easier to use")
        with patch.object(
            task_module, "board", return_value={"task": {"status": "archived"}}
        ):
            task_module.process_requests()
        updated = self.item()
        self.assertEqual(updated["status"], "working")
        self.assertEqual(updated["phase"], "build")
        self.assertEqual(updated["generation"], 1)
        self.assertEqual(updated["finished"], 0)
        self.assertIn("easier to use", (Path(updated["folder"]) /
                      ".local-ai/USER-REQUESTS.md").read_text())

    def test_thumbs_down_revises_task_and_teaches_future_similar_work(self):
        import feedback_learning

        task_module.update(self.pid, title="Techno app design",
                           brief="Build a techno music design app", card="review-card",
                           phase="review", status="ready_for_review")
        task_module.request(self.pid, "feedback_down", "Use cleaner controls and no guitar imagery")
        with patch.object(task_module, "board", return_value={"task": {"status": "review"}}):
            task_module.process_requests()
        revised = self.item()
        self.assertEqual(revised["status"], "working")
        self.assertEqual(revised["generation"], 1)
        self.assertIn("no guitar imagery", (Path(revised["folder"]) /
                      ".local-ai/USER-REQUESTS.md").read_text())
        learned = feedback_learning.for_task(task_module.STATE, self.pid)
        self.assertEqual(learned[0]["rating"], -1)
        self.assertIn("cleaner controls", learned[0]["note"])

    def test_thumbs_up_accepts_and_records_local_success_example(self):
        import feedback_learning

        folder = Path(self.item()["folder"])
        task_module.update(self.pid, status="ready_for_review",
                           fingerprint=task_module.fingerprint(folder))
        task_module.request(self.pid, "accept")
        task_module.process_requests()
        self.assertEqual(self.item()["status"], "accepted")
        reviews = feedback_learning.for_task(task_module.STATE, self.pid)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["rating"], 1)

    def test_failed_review_repair_card_carries_failure_evidence(self):
        self.verify_script('Write-Output "missing behavior"; exit 3')
        task_module.update(self.pid, card="old-review", phase="review")
        with patch.object(task_module, "board", return_value={"id": "repair-card"}) as board:
            task_module.handle_handoff(
                self.item(), {"id": "old-review", "status": "review"}
            )
        self.assertEqual(self.item()["card"], "repair-card")
        self.assertEqual(self.item()["generation"], 1)
        args = board.call_args.args
        body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.assertIn("Build and test a working fixture", body)
        self.assertIn("missing behavior", body)
        self.assertIn("REVIEW PASS", body)

    def test_pause_queued_work_survives_ticks(self):
        task_module.request(self.pid, "pause")
        with patch.object(task_module, "board") as board:
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "paused")
        board.assert_not_called()

    def test_stop_keeps_files_and_prevents_restart(self):
        task_module.request(self.pid, "stop")
        with patch.object(task_module, "board") as board:
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "stopped")
        self.assertTrue((self.base / "project/AGENTS.md").exists())
        board.assert_not_called()

    def test_delete_removes_task_but_preserves_project_files(self):
        task_module.request(self.pid, "delete")
        with patch.object(task_module, "board") as board:
            task_module.tick()
            task_module.tick()
        self.assertEqual(task_module.projects(), [])
        self.assertTrue((self.base / "project/AGENTS.md").exists())
        board.assert_not_called()

    def test_progress_uses_saved_checklist_and_current_step(self):
        folder = self.base / "project/.local-ai"
        (folder / "PLAN.md").write_text(
            "- [x] Plan\n- [ ] Build\n- [ ] Test\n", encoding="utf-8"
        )
        (folder / "PROGRESS.md").write_text(
            "Current task: Drafting an outline\nNext step: Build the homepage\n", encoding="utf-8"
        )
        percent, step = task_module.task_progress(self.item())
        self.assertEqual(percent, 33)
        self.assertEqual(step, "Build the homepage")
        task_module.update(self.pid, status="accepted")
        self.assertEqual(task_module.task_progress(self.item())[0], 100)

    def test_newer_root_progress_is_visible_and_recovered(self):
        project = self.base / "project"
        standard = project / ".local-ai/PROGRESS.md"
        standard.write_text("Current task: Old step\n", encoding="utf-8")
        alternate = project / "PROGRESS.md"
        alternate.write_text("=== Current task: Rendering the vocal test ===\n", encoding="utf-8")
        newer = standard.stat().st_mtime + 2
        os.utime(alternate, (newer, newer))
        self.assertEqual(task_module.task_progress(self.item())[1], "Rendering the vocal test")
        self.assertIn("Rendering the vocal test", task_module.recovery_snapshot(project))

    def test_blocker_requires_explicit_resume(self):
        task_module.update(self.pid, card="test", status="working")
        with patch.object(
            task_module,
            "board",
            return_value={
                "task": {"status": "blocked"},
                "comments": [{"body": "need input"}],
            },
        ):
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "needs_input")
        task_module.request(self.pid, "resume", "Use SQLite")
        with patch.object(
            task_module, "board", return_value={"task": {"status": "blocked"}}
        ) as board:
            task_module.process_requests()
        self.assertEqual(self.item()["status"], "working")
        self.assertEqual(self.item()["brief"], "Build and test a working fixture")
        self.assertIn("Use SQLite", (self.base / "project/.local-ai/USER-REQUESTS.md").read_text())
        self.assertTrue(any(c.args[0] == "unblock" for c in board.call_args_list))

    def test_goal_turn_budget_rolls_to_new_worker_after_old_run_exits(self):
        task_module.update(self.pid, card="old-card", status="working")
        snapshot = {
            "task": {"status": "blocked"},
            "events": [{"kind": "blocked", "payload": {
                "reason": "Goal-mode worker exhausted its turn budget (1000/1000) without completing the task."
            }}],
            "runs": [{"started_at": 100, "ended_at": 0}],
        }
        with patch.object(task_module, "board", return_value=snapshot) as board:
            task_module.advance_project(self.item())
            self.assertEqual(self.item()["card"], "old-card")
            self.assertEqual(self.item()["status"], "working")
            board.assert_called_once()
            snapshot["runs"][0]["ended_at"] = time.time()
            task_module.advance_project(self.item())
            self.assertEqual(self.item()["card"], "old-card")
            snapshot["runs"][0]["ended_at"] = time.time() - 200
            task_module.advance_project(self.item())
        self.assertEqual(self.item()["card"], "")
        self.assertEqual(self.item()["generation"], 1)
        self.assertEqual(self.item()["phase"], "build")
        self.assertEqual(self.item()["status"], "working")
        self.assertEqual(board.call_args.args, ("archive", "old-card"))
        with patch.object(task_module, "board", return_value={"id": "new-card"}) as create:
            task_module.advance_project(self.item())
        self.assertEqual(self.item()["card"], "new-card")
        args = create.call_args.args
        self.assertIn("local-long-", args[args.index("--idempotency-key") + 1])
        self.assertIn("Continuing from saved progress", Path(args[args.index("--body-file") + 1]).read_text())

    def test_other_blocks_do_not_roll_over_automatically(self):
        task_module.update(self.pid, card="old-card", status="working")
        snapshot = {
            "task": {"status": "blocked"},
            "events": [
                {"kind": "blocked", "payload": {"reason": "Goal-mode worker exhausted its turn budget (1000/1000) without completing the task."}},
                {"kind": "unblocked", "payload": {}},
                {"kind": "blocked", "payload": {"reason": "Need a decision about the design."}},
            ],
        }
        with patch.object(task_module, "board", return_value=snapshot):
            task_module.advance_project(self.item())
        self.assertEqual(self.item()["status"], "needs_input")
        self.assertEqual(self.item()["generation"], 0)

    def test_many_followups_remain_durable_without_filling_worker_context(self):
        for number in range(12):
            task_module.request(self.pid, "note", f"Requirement {number}: " + "detail " * 400)
        task_module.process_requests()
        self.assertEqual(self.item()["brief"], "Build and test a working fixture")
        history = (self.base / "project/.local-ai/USER-REQUESTS.md").read_text()
        for number in range(12):
            self.assertIn(f"Requirement {number}:", history)
        with patch.object(task_module, "board", return_value={"id": "card-1"}) as board:
            task_module.create_card(self.item())
        args = board.call_args.args
        prompt = Path(args[args.index("--body-file") + 1]).read_text()
        self.assertIn("USER-REQUESTS.md", prompt)
        self.assertIn("Requirement 11:", prompt)
        self.assertNotIn("Requirement 0:", prompt)
        self.assertLess(len(prompt), 15_000)

    def test_large_original_request_is_saved_verbatim_without_filling_context(self):
        brief = "Start with a desktop app.\n" + ("middle requirement: preserve data\n" * 9000)
        brief += "Finish with offline voice control."
        project = self.base / "large"
        (project / ".local-ai").mkdir(parents=True)
        existing = project / ".local-ai/ORIGINAL-REQUEST.md"
        existing.write_text("Existing project note", encoding="utf-8")
        pid = task_module.add_project("large brief", str(project), brief)
        item = next(project for project in task_module.projects() if project["id"] == pid)
        original = task_module.original_request_path(item["folder"], pid)
        self.assertEqual(original.read_text(encoding="utf-8"), brief)
        with patch.object(task_module, "board", return_value={"id": "large-card"}) as board:
            task_module.create_card(item)
        args = board.call_args.args
        prompt = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.assertLess(len(prompt), 18_000)
        self.assertIn("Read the full file in\nmanageable sections", prompt)
        self.assertIn("Start with a desktop app", prompt)
        self.assertIn("Finish with offline voice control", prompt)
        self.assertEqual(original.read_text(encoding="utf-8"), brief)
        self.assertEqual(existing.read_text(encoding="utf-8"), "Existing project note")

    def test_legacy_followups_compact_only_when_queue_matches(self):
        legacy = "Build and test a working fixture\n\nUSER FOLLOW-UP:\nAdd search"
        task_module.update(self.pid, brief=legacy)
        task_module.request(self.pid, "note", "Add search")
        with task_module.database() as conn:
            conn.execute("UPDATE requests SET status='done'")
        item = self.item()
        task_module.sync_user_requests(item)
        self.assertEqual(self.item()["brief"], "Build and test a working fixture")
        self.assertIn("Add search", (self.base / "project/.local-ai/USER-REQUESTS.md").read_text())
        task_module.update(self.pid, brief=legacy + " with extra detail")
        task_module.sync_user_requests(self.item())
        self.assertEqual(self.item()["brief"], legacy + " with extra detail")

    def test_scheduled_card_waits_for_automatic_dispatch(self):
        task_module.update(self.pid, card="scheduled-card", status="working")
        with patch.object(
            task_module,
            "board",
            return_value={"task": {"status": "scheduled"}},
        ):
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "working")
        self.assertIn("scheduled", self.item()["detail"])

    def test_one_task_failure_does_not_starve_later_tasks(self):
        second = task_module.add_project(
            "second", str(self.base / "second"), "Build another fixture"
        )

        def create(project):
            if project["id"] == self.pid:
                raise RuntimeError("temporary board outage")
            task_module.update(project["id"], card="second-card", status="working")

        with patch.object(task_module, "create_card", side_effect=create) as cards:
            task_module.tick()
        self.assertEqual(cards.call_count, 2)
        self.assertIn("Supervisor will retry", self.item()["detail"])
        self.assertEqual(next(p for p in task_module.projects() if p["id"] == second)["card"], "second-card")

    def test_service_startup_failure_still_processes_pause_request(self):
        task_module.request(self.pid, "pause")

        def unavailable():
            raise RuntimeError("model service offline")

        error = task_module.supervisor_step(unavailable)
        self.assertEqual(self.item()["status"], "paused")
        self.assertIn("model service offline", error)
        self.assertIn("Model service retrying", (task_module.STATE / "service-status.txt").read_text())

    def test_pause_is_applied_while_service_preparation_is_still_running(self):
        started = threading.Event()
        release = threading.Event()
        dispatched = threading.Event()
        errors = []

        def slow_prepare():
            started.set()
            if not release.wait(5):
                raise RuntimeError("test preparation timed out")

        def run_supervisor():
            try:
                with patch.object(task_module, "tick", side_effect=dispatched.set):
                    task_module.supervisor_step(slow_prepare)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run_supervisor)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            task_module.request(self.pid, "pause")
            deadline = time.monotonic() + 3
            while self.item()["status"] != "paused" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(self.item()["status"], "paused")
            self.assertFalse(dispatched.is_set())
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertTrue(dispatched.is_set())

    def test_supervisor_records_task_progress_without_ui(self):
        task_module.update(self.pid, card="fixture", status="working")
        with patch.object(task_module, "tick"), patch.object(
            task_module, "task_progress", return_value=(35, "Building")
        ), patch.object(task_module.task_eta, "countdown") as countdown:
            task_module.supervisor_step()
        countdown.assert_called_once_with(
            task_module.STATE / "projects.db", self.pid, 35, "working"
        )

    def test_accept_rejects_stale_files(self):
        task_module.update(self.pid, status="ready_for_review", fingerprint="old")
        task_module.request(self.pid, "accept")
        task_module.process_requests()
        self.assertNotEqual(self.item()["status"], "accepted")
        with task_module.database() as db:
            self.assertEqual(
                db.execute("SELECT status FROM requests").fetchone()[0], "error"
            )

    def test_same_size_edit_with_restored_timestamp_invalidates_review(self):
        source = self.base / "project/source.txt"
        source.write_text("first", encoding="utf-8")
        original = source.stat()
        checked = task_module.fingerprint(self.base / "project")
        source.write_text("other", encoding="utf-8")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertNotEqual(task_module.fingerprint(self.base / "project"), checked)
        task_module.update(self.pid, status="ready_for_review", fingerprint=checked)
        task_module.request(self.pid, "accept")
        task_module.process_requests()
        self.assertNotEqual(self.item()["status"], "accepted")

    def test_ready_review_avoids_rehashing_unchanged_project_every_tick(self):
        folder = self.base / "project"
        source = folder / "source.txt"
        source.write_text("first", encoding="utf-8")
        task = self.item()
        task["fingerprint"] = task_module.fingerprint(folder)
        task_module.REVIEW_FILE_STATE.pop(task["id"], None)
        with patch.object(task_module, "fingerprint", wraps=task_module.fingerprint) as digest, patch.object(
            task_module.time, "monotonic", side_effect=[100, 101, 702]
        ):
            self.assertFalse(task_module.review_files_changed(task))
            self.assertFalse(task_module.review_files_changed(task))
            self.assertFalse(task_module.review_files_changed(task))
        self.assertEqual(digest.call_count, 2)
        source.write_text("changed", encoding="utf-8")
        self.assertTrue(task_module.review_files_changed(task))

    def test_ready_review_detects_new_file_without_waiting_for_full_interval(self):
        folder = self.base / "project"
        task = self.item()
        task["fingerprint"] = task_module.fingerprint(folder)
        task_module.REVIEW_FILE_STATE.pop(task["id"], None)
        self.assertFalse(task_module.review_files_changed(task))
        (folder / "new.txt").write_text("new", encoding="utf-8")
        self.assertTrue(task_module.review_files_changed(task))

    def test_check_that_edits_project_cannot_pass_as_final(self):
        source = self.base / "project/source.txt"
        source.write_text("before", encoding="utf-8")
        self.verify_script(
            f'Set-Content -LiteralPath "{source}" -Value "after"\nexit 0\n'
        )
        passed, evidence = task_module.verify_project(self.item())
        self.assertFalse(passed)
        self.assertIn("changed during verification", evidence)

    def test_old_review_checksum_gets_fresh_check_before_migration(self):
        self.verify_script('Write-Output "real check passed"; exit 0')
        old = task_module.legacy_fingerprint(self.base / "project")
        task_module.update(self.pid, status="ready_for_review", fingerprint=old)
        with patch.object(task_module, "board") as board:
            task_module.tick()
        board.assert_not_called()
        self.assertEqual(self.item()["status"], "ready_for_review")
        self.assertTrue(self.item()["fingerprint"].startswith("content-v2:"))

    def test_pausing_agent_task_also_holds_its_separate_music_render(self):
        import music_backend

        folder = self.item()["folder"]
        task_module.request(self.pid, "pause")
        with patch.object(music_backend, "hold_project_jobs", return_value=1) as hold:
            task_module.process_requests()
        hold.assert_called_once_with(self.pid, folder, paused=True)
        self.assertEqual(self.item()["status"], "paused")

        task_module.request(self.pid, "resume")
        with patch.object(music_backend, "resume_project_jobs", return_value=1) as resume:
            task_module.process_requests()
        resume.assert_called_once_with(self.pid, folder)
        self.assertEqual(self.item()["status"], "working")

    def test_paused_card_is_archived_after_its_run_ends(self):
        task_module.update(self.pid, status="paused", card="paused-card")
        ended = {"task": {"status": "triage"}, "runs": [
            {"started_at": 100, "ended_at": 200},
        ]}
        with patch.object(task_module, "board", return_value=ended) as board:
            task_module.tick()
        self.assertEqual(self.item()["status"], "paused")
        board.assert_any_call("archive", "paused-card")

    def test_paused_card_waits_for_active_run_before_archiving(self):
        task_module.update(self.pid, status="paused", card="paused-card")
        running = {"task": {"status": "triage"}, "runs": [
            {"started_at": 100, "ended_at": None},
        ]}
        with patch.object(task_module, "board", return_value=running) as board:
            task_module.tick()
        self.assertFalse(any(call.args[0] == "archive" for call in board.call_args_list))

    def test_resume_waits_for_active_paused_worker_to_retire(self):
        task_module.update(self.pid, card="active-card", status="working")
        running = {"task": {"status": "running"}, "runs": [
            {"status": "running", "started_at": 100, "ended_at": None},
        ]}
        with patch.object(task_module, "board", return_value=running):
            task_module.request(self.pid, "pause")
            task_module.process_requests()
        self.assertEqual(self.item()["status"], "paused")

        blocked_but_alive = {"task": {"status": "blocked"}, "runs": [
            {"status": "running", "started_at": 100, "ended_at": None},
        ]}
        with patch.object(task_module, "board", return_value=blocked_but_alive) as board:
            task_module.request(self.pid, "resume")
            task_module.process_requests()
        self.assertEqual(self.item()["status"], "paused")
        self.assertFalse(any(call.args[0] == "unblock" for call in board.call_args_list))
        with task_module.database() as conn:
            result = conn.execute(
                "SELECT status,result FROM requests ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(result["status"], "error")
        self.assertIn("still retiring", result["result"])

        retired = {"task": {"status": "blocked"}, "runs": [
            {"status": "blocked", "started_at": 100, "ended_at": 101},
        ]}
        with patch.object(task_module, "board", return_value=retired) as board:
            task_module.request(self.pid, "resume")
            task_module.process_requests()
        self.assertEqual(self.item()["status"], "working")
        self.assertTrue(any(call.args[0] == "unblock" for call in board.call_args_list))

    def test_stop_during_review_migration_is_not_overwritten(self):
        self.verify_script('Write-Output "real check passed"; exit 0')
        old = task_module.legacy_fingerprint(self.base / "project")
        task_module.update(self.pid, status="ready_for_review", fingerprint=old)

        def interrupted(_project):
            task_module.request(self.pid, "stop")
            return False, "Stopped by user"

        with patch.object(task_module, "verify_project", side_effect=interrupted):
            task_module.tick()
        self.assertEqual(self.item()["status"], "stopped")

    def test_timeout_fails_instead_of_hanging(self):
        self.verify_script("Start-Sleep -Seconds 60; exit 0")
        passed, evidence = task_module.verify_project(self.item(), timeout=0.3)
        self.assertFalse(passed)
        self.assertIn("exit code: 124", evidence)

    def test_pause_cancels_verification_without_failure_retry(self):
        self.verify_script("Start-Sleep -Seconds 60; exit 0")
        task_module.update(self.pid, card="test")
        task_module.request(self.pid, "pause")
        with patch.object(
            task_module, "board", return_value={"task": {"status": "review"}}
        ):
            task_module.handle_handoff(self.item(), {"id": "test", "status": "review"})
        self.assertEqual(self.item()["status"], "paused")
        self.assertEqual(self.item()["failures"], 0)

    def test_supervisor_crash_retires_owned_check(self):
        import win32api
        import win32event

        marker = self.base / "check-pid.txt"
        self.verify_script(
            f'$PID | Set-Content -LiteralPath "{marker}"\nStart-Sleep -Seconds 60\n'
        )
        helper = self.base / "helper.py"
        helper.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0,{str(ROOT)!r})\nimport long_tasks as task_module\n"
            f"task_module.STATE=Path({str(self.base / 'state')!r})\ntask_module.verify_project(task_module.projects()[0])\n",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(helper)],
            creationflags=task_module.HIDE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        handle = None
        try:
            deadline = time.monotonic() + 15
            child_pid = None
            while time.monotonic() < deadline:
                try:
                    child_pid = int(marker.read_text(encoding="ascii").strip())
                    break
                except (FileNotFoundError, PermissionError, ValueError):
                    # Windows can expose the new file before PowerShell closes
                    # its write handle; existence alone does not mean readable.
                    pass
                time.sleep(0.05)
            self.assertIsNotNone(child_pid, "Child verification never started")
            handle = win32api.OpenProcess(0x00100000, False, child_pid)  # SYNCHRONIZE
            process.terminate()
            process.wait(timeout=5)
            self.assertEqual(
                win32event.WaitForSingleObject(handle, 5000), win32event.WAIT_OBJECT_0
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            if handle:
                win32event.WaitForSingleObject(handle, 5000)
                handle.Close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
