"""Voice control uses the local media worker and bounds recorded clips."""

import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cale_voice
import voice_assistant


class VoiceTests(unittest.TestCase):
    def test_read_aloud_hold_explains_muted_and_preparing_states(self):
        with patch.object(voice_assistant, "speech_ready", return_value=False):
            self.assertIn("muted", voice_assistant.speech_hold_reason(True))
            self.assertIn("still preparing", voice_assistant.speech_hold_reason(False))
        with patch.object(voice_assistant, "speech_ready", return_value=True):
            self.assertEqual(voice_assistant.speech_hold_reason(False), "")

    def test_clone_only_speech_never_plays_before_readiness(self):
        with (patch.object(voice_assistant, "speech_ready", return_value=False),
              patch.object(voice_assistant, "media_call") as fallback,
              patch.object(voice_assistant, "_play_file") as play,
              patch.object(cale_voice, "speak") as clone):
            with self.assertRaisesRegex(RuntimeError, "staying quiet"):
                voice_assistant.say(voice_assistant.WAKE_ACK, require_clone=True)
        fallback.assert_not_called()
        play.assert_not_called()
        clone.assert_not_called()

    def test_clone_only_speech_never_falls_back_after_clone_failure(self):
        with (patch.object(voice_assistant, "speech_ready", return_value=True),
              patch.object(voice_assistant, "_heavy_gpu_active", return_value=False),
              patch.object(cale_voice, "ready", return_value=True),
              patch.object(cale_voice, "speak", side_effect=RuntimeError("clone failed")),
              patch.object(voice_assistant, "media_call") as fallback):
            with self.assertRaisesRegex(RuntimeError, "clone failed"):
                voice_assistant.say("Your task is ready", require_clone=True)
        fallback.assert_not_called()

    def test_speech_readiness_requires_setup_and_actual_clone_probe_not_deep_suite_or_mic(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "LongTasks"
            state.mkdir()
            setup = state / "install-progress.json"
            setup.write_text(json.dumps({"status": "complete"}))
            (state / "self-check.json").write_text(json.dumps({"status": "failed", "version": "older release"}))
            (state / "readiness-probes.json").write_text(json.dumps({"voice": {"ok": False, "microphone": False}}))
            with (patch.object(voice_assistant, "ROOT", root),
                  patch.object(cale_voice, "ready", return_value=True),
                  patch.object(cale_voice, "capability_ready", return_value=True),
                  patch.object(cale_voice, "schedule_probe") as schedule):
                self.assertTrue(voice_assistant.speech_ready())
                (root / ".cale-ai-install").write_text("A newer unrelated release")
                self.assertTrue(voice_assistant.speech_ready())
                schedule.assert_not_called()
                setup.write_text('{}')
                self.assertFalse(voice_assistant.speech_ready())
                setup.write_text(json.dumps({"status": "complete"}))
                with patch.object(cale_voice, "capability_ready", return_value=False):
                    self.assertFalse(voice_assistant.speech_ready())
                    schedule.assert_called_once()
                with patch.object(cale_voice, "ready", return_value=False):
                    self.assertFalse(voice_assistant.speech_ready())

    def test_mute_interrupts_current_spoken_reply_without_muting_microphone(self):
        import queue

        mic = threading.Event()
        muted = threading.Event()
        cancel = voice_assistant.SpeechCancel(mic, queue.PriorityQueue(), 0, muted)
        self.assertFalse(cancel.is_set())
        muted.set()
        self.assertTrue(cancel.wait(0.1))
        self.assertFalse(mic.is_set())

    def test_microphone_server_handles_multiple_requests_in_one_process(self):
        server = Path(__file__).resolve().parents[1] / "media_worker.py"
        result = subprocess.run(
            [sys.executable, str(server), "--voice-server", str(os.getpid())],
            input='{"action":"unknown"}\n{"action":"unknown"}\n',
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertTrue(all("error" in response for response in responses))

    def test_microphone_calls_reuse_persistent_worker(self):
        from unittest.mock import Mock

        with TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "Media/python/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            worker = Mock()
            worker.poll.return_value = None
            with patch.object(voice_assistant, "ROOT", root), patch.object(
                voice_assistant.subprocess, "Popen", return_value=worker
            ) as launch, patch("cale_voice._line_with_timeout", side_effect=[
                '{"input_device":"Mic"}', '{"text":"hello"}',
            ]):
                self.assertEqual(
                    voice_assistant._persistent_mic_call("microphone_status", {})["input_device"],
                    "Mic",
                )
                self.assertEqual(
                    voice_assistant._persistent_mic_call("record_audio", {"seconds": 8})["text"],
                    "hello",
                )
                launch.assert_called_once()
            with patch.object(voice_assistant, "_stop_media_process"):
                voice_assistant._stop_mic_worker()

    def test_microphone_worker_recovers_from_non_object_json(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "Media/python/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            for response in ('null', '[]', '"unexpected"', '42'):
                with self.subTest(response=response):
                    broken, replacement = Mock(), Mock()
                    broken.poll.return_value = None
                    replacement.poll.return_value = None
                    with (
                        patch.object(voice_assistant, "ROOT", root),
                        patch.object(voice_assistant, "_mic_worker", None),
                        patch.object(voice_assistant.subprocess, "Popen",
                                     side_effect=[broken, replacement]) as launch,
                        patch.object(voice_assistant, "_stop_media_process") as stop,
                        patch.object(cale_voice, "_line_with_timeout",
                                     side_effect=[response, '{"input_device":"Mic"}']),
                    ):
                        self.assertIsNone(voice_assistant._persistent_mic_call(
                            "microphone_status", {}))
                        stop.assert_called_once_with(broken)
                        self.assertIsNone(voice_assistant._mic_worker)
                        self.assertEqual(voice_assistant._persistent_mic_call(
                            "microphone_status", {}), {"input_device": "Mic"})
                        self.assertEqual(launch.call_count, 2)

    def test_one_shot_media_call_rejects_non_object_json(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "Media/python/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            with (
                patch.object(voice_assistant, "ROOT", root),
                patch.object(voice_assistant, "_persistent_mic_call", return_value=None),
                patch.object(voice_assistant.subprocess, "run") as run,
            ):
                for response in ('null', '[]', '"unexpected"', '42', 'true'):
                    with self.subTest(response=response):
                        run.return_value = Mock(stdout=response, returncode=0)
                        with self.assertRaisesRegex(
                            RuntimeError, "The local voice service did not respond"
                        ):
                            voice_assistant.media_call("microphone_status")
                run.return_value = Mock(stdout='{"input_device":"Mic"}', returncode=0)
                self.assertEqual(voice_assistant.media_call("microphone_status"),
                                 {"input_device": "Mic"})

    def test_status_rephrasing_uses_fast_local_model_and_keeps_facts(self):
        fake = ModuleType("cloud_ai")
        fake.FAST_MODEL = "cale-fast"
        calls = []

        def answer(url, payload, **options):
            calls.append((url, payload, options))
            return {"choices": [{"message": {"content": "Recipe app: checking files, one folder at a time."}}]}

        fake.post = answer
        with TemporaryDirectory() as folder, patch.object(
            voice_assistant, "ROOT", Path(folder)
        ), patch.dict(sys.modules, {"cloud_ai": fake}), patch.object(
            voice_assistant, "_fast_model_loaded", return_value=True
        ):
            spoken = voice_assistant.rephrase_status(
                "Recipe app. Checking files.", ["Still checking files"],
                ("Recipe app", "checking files"),
            )
        self.assertEqual(spoken, "Recipe app: checking files, one folder at a time.")
        self.assertEqual(calls[0][0], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(calls[0][1]["model"], "cale-fast")
        self.assertLessEqual(calls[0][2]["timeout"], 12)

    def test_status_rephrasing_falls_back_for_malformed_model_responses(self):
        fake = ModuleType("cloud_ai")
        fake.FAST_MODEL = "cale-fast"
        fake.post = Mock()
        responses = (
            None,
            {"choices": None},
            {"choices": [None]},
            {"choices": [{"message": None}]},
            {"choices": [{"message": []}]},
        )
        with (
            patch.dict(sys.modules, {"cloud_ai": fake}),
            patch.object(voice_assistant, "_heavy_gpu_active", return_value=False),
            patch.object(voice_assistant, "_fast_model_loaded", return_value=True),
        ):
            for response in responses:
                with self.subTest(response=response):
                    fake.post.return_value = response
                    self.assertEqual(
                        voice_assistant.rephrase_status(
                            "Recipe app. Checking files.",
                            required=("Recipe app", "Checking files"),
                        ),
                        "Recipe app: Checking files.",
                    )
        self.assertEqual(fake.post.call_count, len(responses))

    def test_status_rephrasing_rejects_missing_fact(self):
        fake = ModuleType("cloud_ai")
        fake.FAST_MODEL = "cale-fast"
        fake.post = lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": "I'm making excellent progress!"}}]
        }
        with TemporaryDirectory() as folder, patch.object(
            voice_assistant, "ROOT", Path(folder)
        ), patch.dict(sys.modules, {"cloud_ai": fake}), patch.object(
            voice_assistant, "_fast_model_loaded", return_value=True
        ):
            spoken = voice_assistant.rephrase_status(
                "Recipe app. Checking files.", required=("Recipe app", "Checking files")
            )
        self.assertEqual(spoken, "Recipe app: Checking files.")

    def test_cold_local_model_does_not_delay_repeated_status_speech(self):
        fake = ModuleType("cloud_ai")
        fake.FAST_MODEL = "cale-fast"
        fake.post = Mock(side_effect=AssertionError("Cold model should not load"))
        with TemporaryDirectory() as folder, patch.object(
            voice_assistant, "ROOT", Path(folder)
        ), patch.dict(sys.modules, {"cloud_ai": fake}), patch.object(
            voice_assistant, "_fast_model_loaded", return_value=False
        ):
            first = voice_assistant.rephrase_status(
                "Recipe app. Checking files.", required=("Recipe app", "Checking files")
            )
            second = voice_assistant.rephrase_status(
                "Recipe app. Checking files.", recent=(first,),
                required=("Recipe app", "Checking files")
            )
        self.assertNotEqual(first, second)
        self.assertIn("Recipe app", first)
        self.assertIn("Checking files", second)
        fake.post.assert_not_called()

    def test_loaded_model_check_accepts_latest_alias_and_fails_closed(self):
        with patch.object(voice_assistant, "urlopen", return_value=io.BytesIO(
            b'{"models":[{"name":"cale-fast:latest"}]}')):
            self.assertTrue(voice_assistant._fast_model_loaded("cale-fast"))
        with patch.object(voice_assistant, "urlopen", side_effect=OSError):
            self.assertFalse(voice_assistant._fast_model_loaded("cale-fast"))

    def test_malformed_model_status_preserves_immediate_speech_fallback(self):
        fake = ModuleType("cloud_ai")
        fake.FAST_MODEL = "cale-fast"
        fake.post = Mock(side_effect=AssertionError("Malformed status must not start the model"))
        for response in (b'null', b'[]', b'{"models":[null]}',
                         b'{"models":[42]}'):
            with (self.subTest(response=response),
                patch.dict(sys.modules, {"cloud_ai": fake}),
                patch.object(voice_assistant, "_heavy_gpu_active", return_value=False),
                patch.object(voice_assistant, "urlopen",
                             return_value=io.BytesIO(response)),
            ):
                self.assertEqual(
                    voice_assistant.rephrase_status(
                        "Recipe app. Checking files.",
                        required=("Recipe app", "Checking files"),
                    ),
                    "Recipe app: Checking files.",
                )
        fake.post.assert_not_called()

    def test_loaded_model_check_requires_matching_explicit_tag(self):
        cases = (
            ("cale-fast:small", "cale-fast:large", False),
            ("cale-fast:small", "cale-fast", False),
            ("cale-fast:latest", "cale-fast:large", False),
            ("cale-fast:small", "cale-fast:small", True),
            ("cale-fast", "cale-fast:latest", True),
            ("cale-fast:latest", "cale-fast", True),
        )
        for loaded, requested, expected in cases:
            with self.subTest(loaded=loaded, requested=requested):
                response = json.dumps({"models": [{"name": loaded}]}).encode()
                with patch.object(voice_assistant, "urlopen",
                                  return_value=io.BytesIO(response)):
                    self.assertEqual(
                        voice_assistant._fast_model_loaded(requested), expected
                    )

    def test_gpu_activity_ignores_malformed_lease_pids(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "LongTasks"
            state.mkdir()
            for kind in ("music", "image"):
                (state / f"{kind}-gpu-active-invalid.json").write_text('{}')
            with (patch.object(voice_assistant, "ROOT", root),
                  patch("psutil.Process") as process):
                self.assertFalse(voice_assistant._heavy_gpu_active())
                process.assert_not_called()

    def test_music_render_keeps_status_speech_from_reloading_gpu_model(self):
        from types import SimpleNamespace

        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "LongTasks"
            state.mkdir()
            (state / "music-gpu-active-123.json").write_text('{"pid":123}')
            with patch.object(voice_assistant, "ROOT", root), patch(
                "psutil.Process"
            ) as process, patch.dict(sys.modules, {"cloud_ai": SimpleNamespace()}):
                process.return_value.is_running.return_value = True
                process.return_value.create_time.return_value = time.time() - 5
                self.assertEqual(
                    voice_assistant.rephrase_status("Recipe app. Checking files."),
                    "Recipe app. Checking files.",
                )

    def test_listen_returns_trimmed_local_transcript(self):
        with patch.object(
            voice_assistant, "media_call", return_value={"text": "  Hello  "}
        ) as call:
            self.assertEqual(voice_assistant.listen(8), "Hello")
        call.assert_called_once_with("record_audio", seconds=8)

    def test_microphone_preflight_uses_local_worker_without_recording(self):
        with patch.object(voice_assistant, "media_call", return_value={
            "input_device": "Fifine Microphone", "device_index": 1,
        }) as call:
            self.assertEqual(voice_assistant.microphone_status()["input_device"],
                             "Fifine Microphone")
        call.assert_called_once_with("microphone_status")

    def test_speech_uses_local_worker(self):
        with patch("cale_voice.ready", return_value=False), patch.object(
            voice_assistant, "media_call", return_value={"path": "speech.wav"}
        ) as call, patch.object(voice_assistant, "_play_file") as play:
            voice_assistant.say("Task completed", rate=2)
        self.assertEqual(call.call_args.args, ("speak",))
        self.assertEqual(call.call_args.kwargs["text"], "Task completed")
        self.assertFalse(call.call_args.kwargs["play"])
        self.assertEqual(call.call_args.kwargs["rate"], 2)
        play.assert_called_once_with("speech.wav", None)

    def test_failed_voice_clone_reaches_offline_fallback(self):
        with patch("cale_voice.ready", return_value=True), patch(
            "cale_voice.speak", side_effect=RuntimeError("clone unavailable")
        ), patch.object(voice_assistant, "media_call", return_value={
            "engine": "Windows offline speech", "path": "fallback.wav"
        }) as fallback, patch.object(voice_assistant, "_play_file") as play:
            result = voice_assistant.say("Your task is ready")
        self.assertEqual(result["engine"], "Windows offline speech")
        self.assertEqual(fallback.call_args.args, ("speak",))
        self.assertFalse(fallback.call_args.kwargs["play"])
        play.assert_called_once_with("fallback.wav", None)

    def test_music_render_keeps_voice_clone_from_reloading_gpu(self):
        with patch("cale_voice.ready", return_value=True), patch.object(
            voice_assistant, "_heavy_gpu_active", return_value=True
        ), patch("cale_voice.speak") as clone, patch.object(
            voice_assistant, "media_call", return_value={"path": "fallback.wav"}
        ), patch.object(voice_assistant, "_play_file"):
            voice_assistant.say("Song progress")
        clone.assert_not_called()

    def test_fallback_synthesis_stops_for_microphone_turn(self):
        from subprocess import TimeoutExpired
        from unittest.mock import Mock

        with TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "Media/python/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            voice = root / "Media/models/natural_voice"
            voice.mkdir(parents=True)
            (voice / "kokoro-v1.0.int8.onnx").touch()
            (voice / "voices-v1.0.bin").touch()
            cancel = threading.Event()

            def wait_for_voice(**_kwargs):
                time.sleep(0.02)
                raise TimeoutExpired("media worker", 0.1)

            process = Mock()
            process.communicate.side_effect = wait_for_voice
            timer = threading.Timer(0.1, cancel.set)
            timer.start()
            try:
                with patch.object(voice_assistant, "ROOT", root), patch.object(
                    voice_assistant.subprocess, "Popen", return_value=process
                ), patch.object(voice_assistant, "_stop_media_process") as stop:
                    with self.assertRaises(cale_voice.SpeechInterrupted):
                        voice_assistant.media_call("speak", cancel=cancel,
                                                   text="hello", play=False)
                stop.assert_called_once_with(process)
            finally:
                timer.join()

    def test_cancellable_media_worker_stops_after_communication_failure(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "Media/python/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            failure = OSError("media pipe failed")
            process = Mock()
            process.communicate.side_effect = failure
            with (
                patch.object(voice_assistant, "ROOT", root),
                patch.object(voice_assistant.subprocess, "Popen", return_value=process),
                patch.object(voice_assistant, "_stop_media_process") as stop,
            ):
                with self.assertRaises(OSError) as raised:
                    voice_assistant.media_call(
                        "record_audio", cancel=threading.Event(), seconds=8
                    )
                self.assertIs(raised.exception, failure)
                process.communicate.assert_called_once()
                stop.assert_called_once_with(process)

    def test_fallback_playback_stops_for_microphone_turn(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "speech.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 16000)
            cancel = threading.Event()
            timer = threading.Timer(0.1, cancel.set)
            timer.start()
            try:
                with patch("winsound.PlaySound") as play:
                    with self.assertRaises(cale_voice.SpeechInterrupted):
                        voice_assistant._play_file(path, cancel)
                self.assertEqual(play.call_args.args, (None, 0))
            finally:
                timer.join()

    def test_voice_generation_stops_promptly_for_a_microphone_turn(self):
        release = threading.Event()
        cancel = threading.Event()

        class SlowVoice:
            def readline(self):
                release.wait(2)
                return '{"path":"unused.wav"}'

        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(cale_voice.SpeechInterrupted):
                cale_voice._line_with_timeout(SlowVoice(), 5, cancel)
        finally:
            release.set()
            timer.join()
        self.assertLess(time.monotonic() - started, 1)

    def test_interrupted_voice_does_not_fall_back_and_speak_over_the_mic(self):
        with TemporaryDirectory() as folder, patch.object(
            voice_assistant, "ROOT", Path(folder)
        ), patch("cale_voice.ready", return_value=True), patch(
            "cale_voice.speak", side_effect=cale_voice.SpeechInterrupted
        ) as clone, patch.object(voice_assistant, "media_call") as fallback:
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(cale_voice.SpeechInterrupted):
                voice_assistant.say("Read my reply", cancel=cancel)
        self.assertIs(clone.call_args.kwargs["cancel"], cancel)
        fallback.assert_not_called()

    def test_long_chat_reply_is_spoken_without_truncation(self):
        message = " ".join(f"word{index}" for index in range(150))
        chunks = voice_assistant.speech_chunks(message)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 280 for chunk in chunks))
        self.assertEqual(" ".join(chunks), message)

    def test_verse_lines_remain_separate_speech_chunks(self):
        verse = "The tide rolls in\nThe beat begins\nWe build again"
        self.assertEqual(voice_assistant.speech_chunks(verse), [verse])

    def test_spoken_replay_can_preempt_a_long_ordinary_reply(self):
        import queue

        pending = queue.PriorityQueue()
        pending.put((0, 2, "ordinary reply"))
        self.assertFalse(voice_assistant.urgent_speech_waiting(pending, 0))
        pending.put((-2, 3, "requested replay"))
        self.assertTrue(voice_assistant.urgent_speech_waiting(pending, 0))
        self.assertFalse(voice_assistant.urgent_speech_waiting(pending, -2))
        self.assertEqual(pending.get()[2], "requested replay")

    def test_spoken_replay_interrupts_active_speech_without_a_mic_turn(self):
        import queue

        pending = queue.PriorityQueue()
        mic = threading.Event()
        cancel = voice_assistant.SpeechCancel(mic, pending, 0)
        self.assertFalse(cancel.is_set())
        self.assertFalse(cancel.wait(timeout=0.01))
        pending.put((-2, 1, "requested replay"))
        self.assertTrue(cancel.is_set())
        self.assertTrue(cancel.wait(timeout=0.01))
        replay_cancel = voice_assistant.SpeechCancel(mic, pending, -2)
        self.assertFalse(replay_cancel.is_set())
        mic.set()
        self.assertTrue(replay_cancel.is_set())

    def test_requested_replay_stops_current_audio_playback(self):
        import queue

        with TemporaryDirectory() as folder:
            path = Path(folder) / "speech.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 16000)
            pending = queue.PriorityQueue()
            cancel = voice_assistant.SpeechCancel(threading.Event(), pending, 0)
            timer = threading.Timer(0.1, lambda: pending.put((-2, 1, "requested replay")))
            timer.start()
            try:
                with patch("winsound.PlaySound") as play:
                    with self.assertRaises(cale_voice.SpeechInterrupted):
                        voice_assistant._play_file(path, cancel)
                self.assertEqual(play.call_args.args, (None, 0))
            finally:
                timer.join()

    def test_handsfree_requires_name_and_recognizes_task_controls(self):
        self.assertIsNone(
            voice_assistant.spoken_command("pause the task", require_wake=True)
        )
        self.assertIsNone(
            voice_assistant.spoken_command("Cale, pause the task", require_wake=True)
        )
        self.assertIsNone(
            voice_assistant.spoken_command(
                "Okay Cale, pause the task", require_wake=True
            )
        )
        self.assertEqual(
            voice_assistant.spoken_command(
                "Hey Kale, pause the task", require_wake=True
            ),
            ("pause", ""),
        )
        # The installed offline transcriber produced this exact spelling
        # when asked to recognize "Hey Cale, can you hear me?".
        self.assertEqual(
            voice_assistant.spoken_command(
                "Hey, Cal, can you hear me?", require_wake=True
            ),
            ("hearing", ""),
        )
        self.assertEqual(
            voice_assistant.spoken_command(
                "Hey Cale, what's the status?", require_wake=True
            ),
            ("status", ""),
        )
        self.assertEqual(
            voice_assistant.spoken_command("Build a recipe app"),
            ("note", "Build a recipe app"),
        )
        self.assertEqual(
            voice_assistant.spoken_command("Hey Cale can you hear me", require_wake=True),
            ("hearing", ""),
        )
        self.assertEqual(
            voice_assistant.spoken_command("Hey, Cale, can you hear me?", require_wake=True),
            ("hearing", ""),
        )

    def test_wake_phrase_can_span_two_microphone_clips(self):
        listener = voice_assistant.WakeSequence(timeout=12)
        self.assertIsNone(listener.consume("open my files", now=100))
        self.assertIsNone(listener.consume("Hey, Cale!", now=101))
        self.assertEqual(listener.consume("can you hear me?", now=108), ("hearing", ""))
        self.assertIsNone(listener.consume("pause the task", now=109))

    def test_split_wake_expires_and_mic_off_resets_it(self):
        listener = voice_assistant.WakeSequence(timeout=12)
        self.assertIsNone(listener.consume("Hey Cale", now=100))
        self.assertIsNone(listener.consume("stop the task", now=113))
        self.assertIsNone(listener.consume("Hey Cale", now=120))
        listener.reset()
        self.assertIsNone(listener.consume("stop the task", now=121))
        self.assertEqual(listener.consume("Hey Cale, stop the task", now=122), ("stop", ""))

    def test_split_wake_allows_time_for_spoken_acknowledgement(self):
        listener = voice_assistant.WakeSequence()
        self.assertIsNone(listener.consume("Hey Cale", now=100))
        self.assertEqual(listener.consume("can you hear me", now=119), ("hearing", ""))

    def test_whisper_phonetic_wake_spelling_reaches_hearing_command(self):
        self.assertEqual(
            voice_assistant.spoken_command("Hey Kaeln, can you hear me?", require_wake=True),
            ("hearing", ""),
        )
        self.assertEqual(
            voice_assistant.WakeSequence().consume("Hey Kaeln, can you hear me?"),
            ("hearing", ""),
        )
        self.assertIsNone(voice_assistant.spoken_command("Hey Kaleb, can you hear me?", require_wake=True))

    def test_split_wake_window_renews_after_spoken_acknowledgement(self):
        listener = voice_assistant.WakeSequence(timeout=24)
        self.assertIsNone(listener.consume("Hey Cale", now=100))
        listener.renew_after_ack(now=122)
        self.assertEqual(listener.consume("can you hear me", now=139), ("hearing", ""))

    def test_fixed_wake_reply_reuses_cloned_voice_audio(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import cale_voice

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "reference.wav"
            reference.write_bytes(b"voice")

            def synthesize(_message, output, cancel=None):
                Path(output).write_bytes(b"wave" * 300)
                return {"path": str(output), "engine": "Cale's local voice"}

            with (
                patch.object(voice_assistant, "ROOT", root),
                patch.object(cale_voice, "REFERENCE", reference),
                patch.object(cale_voice, "ready", return_value=True),
                patch.object(cale_voice, "speak", side_effect=synthesize) as speak,
                patch("winsound.PlaySound") as playback,
            ):
                first = voice_assistant.say(voice_assistant.WAKE_ACK)
                second = voice_assistant.say(voice_assistant.WAKE_ACK)
            self.assertEqual(first["engine"], "Cale's local voice")
            self.assertEqual(second["engine"], "Cale's local voice (cached)")
            self.assertEqual(speak.call_count, 1)
            playback.assert_called_once()

    def test_corrupt_cached_wake_reply_is_regenerated_with_clone(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "reference.wav"
            reference.write_bytes(b"voice")
            cached = root / "Media" / "voice-cache" / (
                f"wake-v1-{reference.stat().st_mtime_ns}.wav"
            )
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"invalid WAV" * 200)
            fresh_audio = b"fresh audio" * 200
            cancel = threading.Event()

            def synthesize(_message, output, cancel=None):
                Path(output).write_bytes(fresh_audio)
                return {"path": str(output), "engine": "Cale's local voice"}

            with (
                patch.object(voice_assistant, "ROOT", root),
                patch.object(voice_assistant, "speech_ready", return_value=True),
                patch.object(voice_assistant, "_heavy_gpu_active", return_value=False),
                patch.object(cale_voice, "REFERENCE", reference),
                patch.object(cale_voice, "ready", return_value=True),
                patch.object(cale_voice, "speak", side_effect=synthesize) as clone,
                patch.object(voice_assistant, "_play_file",
                             side_effect=wave.Error("invalid WAV header")) as play,
                patch.object(voice_assistant, "media_call") as fallback,
            ):
                result = voice_assistant.say(
                    voice_assistant.WAKE_ACK, cancel=cancel, require_clone=True
                )
            self.assertEqual(result["engine"], "Cale's local voice")
            play.assert_called_once_with(cached, cancel)
            clone.assert_called_once()
            self.assertIs(clone.call_args.kwargs["cancel"], cancel)
            fallback.assert_not_called()
            self.assertEqual(cached.read_bytes(), fresh_audio)

    def test_cached_wake_playback_runtime_error_regenerates_clone(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "reference.wav"
            reference.write_bytes(b"voice")
            cached = root / "Media" / "voice-cache" / (
                f"wake-v1-{reference.stat().st_mtime_ns}.wav"
            )
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"cached audio" * 200)
            fresh_audio = b"fresh audio" * 200

            def synthesize(_message, output, cancel=None):
                Path(output).write_bytes(fresh_audio)
                return {"path": str(output), "engine": "clone"}

            winsound = Mock()
            winsound.PlaySound.side_effect = RuntimeError("Failed to play sound")
            with (
                patch.dict(sys.modules, {"winsound": winsound}),
                patch.object(voice_assistant, "ROOT", root),
                patch.object(voice_assistant, "speech_ready", return_value=True),
                patch.object(voice_assistant, "_heavy_gpu_active", return_value=False),
                patch.object(cale_voice, "REFERENCE", reference),
                patch.object(cale_voice, "ready", return_value=True),
                patch.object(cale_voice, "speak", side_effect=synthesize) as clone,
                patch.object(voice_assistant, "media_call") as fallback,
            ):
                result = voice_assistant.say(
                    voice_assistant.WAKE_ACK, require_clone=True
                )
            self.assertEqual(result["engine"], "clone")
            winsound.PlaySound.assert_called_once_with(
                str(cached), winsound.SND_FILENAME
            )
            clone.assert_called_once()
            fallback.assert_not_called()
            self.assertEqual(cached.read_bytes(), fresh_audio)

    def test_truncated_cached_wav_is_regenerated_after_eof(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "reference.wav"
            reference.write_bytes(b"voice")
            cached = root / "Media" / "voice-cache" / (
                f"wake-v1-{reference.stat().st_mtime_ns}.wav"
            )
            cached.parent.mkdir(parents=True)
            # A valid RIFF/WAVE prefix followed by a truncated fmt chunk payload.
            cached.write_bytes(
                b"RIFF" + (4096).to_bytes(4, "little") + b"WAVE"
                + b"JUNK" + (1000).to_bytes(4, "little") + b"\0" * 1000
                + b"fmt " + (16).to_bytes(4, "little") + b"\x01"
            )
            self.assertGreater(cached.stat().st_size, 1000)
            with self.assertRaises(EOFError):
                with wave.open(str(cached), "rb"):
                    pass
            fresh_audio = b"fresh audio" * 200

            def synthesize(_message, output, cancel=None):
                Path(output).write_bytes(fresh_audio)
                return {"path": str(output), "engine": "clone"}

            winsound = Mock()
            with (
                patch.dict(sys.modules, {"winsound": winsound}),
                patch.object(voice_assistant, "ROOT", root),
                patch.object(voice_assistant, "speech_ready", return_value=True),
                patch.object(voice_assistant, "_heavy_gpu_active", return_value=False),
                patch.object(cale_voice, "REFERENCE", reference),
                patch.object(cale_voice, "ready", return_value=True),
                patch.object(cale_voice, "speak", side_effect=synthesize) as clone,
                patch.object(voice_assistant, "media_call") as fallback,
            ):
                result = voice_assistant.say(
                    voice_assistant.WAKE_ACK,
                    cancel=threading.Event(), require_clone=True,
                )
            self.assertEqual(result["engine"], "clone")
            clone.assert_called_once()
            fallback.assert_not_called()
            winsound.PlaySound.assert_not_called()
            self.assertEqual(cached.read_bytes(), fresh_audio)

    def test_bundled_wake_reply_plays_without_model_startup(self):
        import hashlib
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import cale_voice

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            assets = root / "assets"
            assets.mkdir()
            (assets / "cale-voice-reference.wav").write_bytes(b"original voice")
            (assets / "cale-wake-ack.wav").write_bytes(b"wave" * 300)
            digest = hashlib.sha256(b"original voice").hexdigest()
            with (
                patch.object(voice_assistant, "ROOT", root),
                patch.object(voice_assistant, "WAKE_REFERENCE_SHA256", digest),
                patch.object(cale_voice, "ready") as ready,
                patch("winsound.PlaySound") as playback,
            ):
                result = voice_assistant.say(voice_assistant.WAKE_ACK)
            self.assertEqual(result["engine"], "Cale's local voice (bundled)")
            ready.assert_not_called()
            playback.assert_called_once()

    def test_unreadable_bundled_reference_falls_back_to_synthesis(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            assets = root / "assets"
            assets.mkdir()
            (assets / "cale-voice-reference.wav").write_bytes(b"original voice")
            (assets / "cale-wake-ack.wav").write_bytes(b"wave" * 300)
            fallback_result = {"path": "fallback.wav", "engine": "offline"}
            with (
                patch.object(voice_assistant, "ROOT", root),
                patch.object(Path, "read_bytes",
                             side_effect=PermissionError("reference is unreadable")) as read,
                patch.object(cale_voice, "ready", return_value=False),
                patch.object(voice_assistant, "media_call",
                             return_value=fallback_result) as synthesize,
                patch.object(voice_assistant, "_play_file") as play,
            ):
                result = voice_assistant.say(voice_assistant.WAKE_ACK)
            self.assertEqual(result, fallback_result)
            read.assert_called_once()
            synthesize.assert_called_once()
            self.assertEqual(synthesize.call_args.args, ("speak",))
            self.assertEqual(synthesize.call_args.kwargs["text"], voice_assistant.WAKE_ACK)
            play.assert_called_once_with("fallback.wav", None)

    def test_corrupt_bundled_acknowledgement_falls_back_to_synthesis(self):
        import hashlib

        with TemporaryDirectory() as folder:
            root = Path(folder)
            assets = root / "assets"
            assets.mkdir()
            reference = b"original voice"
            (assets / "cale-voice-reference.wav").write_bytes(reference)
            bundled = assets / "cale-wake-ack.wav"
            bundled.write_bytes(b"not a WAV" * 200)
            cancel = threading.Event()
            fallback_result = {"path": "fallback.wav", "engine": "offline"}
            with (
                patch.object(voice_assistant, "ROOT", root),
                patch.object(voice_assistant, "WAKE_REFERENCE_SHA256",
                             hashlib.sha256(reference).hexdigest()),
                patch.object(cale_voice, "ready", return_value=False),
                patch.object(voice_assistant, "media_call",
                             return_value=fallback_result) as synthesize,
                patch.object(voice_assistant, "_play_file",
                             side_effect=[wave.Error("invalid WAV header"), None]) as play,
            ):
                result = voice_assistant.say(voice_assistant.WAKE_ACK, cancel=cancel)
            self.assertEqual(result, fallback_result)
            synthesize.assert_called_once()
            self.assertEqual(synthesize.call_args.args, ("speak",))
            self.assertEqual(synthesize.call_args.kwargs["text"], voice_assistant.WAKE_ACK)
            self.assertIs(synthesize.call_args.kwargs["cancel"], cancel)
            self.assertEqual(play.call_count, 2)
            self.assertEqual(play.call_args_list[0].args, (bundled, cancel))
            self.assertEqual(play.call_args_list[1].args, ("fallback.wav", cancel))

    def test_bundled_read_aloud_reply_starts_before_voice_model(self):
        import hashlib

        import cale_voice

        with TemporaryDirectory() as folder:
            root = Path(folder)
            assets = root / "assets"
            assets.mkdir()
            (assets / "cale-voice-reference.wav").write_bytes(b"original voice")
            (assets / "cale-reading-ack.wav").write_bytes(b"wave" * 300)
            digest = hashlib.sha256(b"original voice").hexdigest()
            with (
                patch.object(voice_assistant, "ROOT", root),
                patch.object(voice_assistant, "WAKE_REFERENCE_SHA256", digest),
                patch.object(cale_voice, "ready") as ready,
                patch("winsound.PlaySound") as playback,
            ):
                result = voice_assistant.say(voice_assistant.REPLAY_ACK)
            self.assertEqual(result["engine"], "Cale's local voice (bundled)")
            ready.assert_not_called()
            playback.assert_called_once()

    def test_saved_rap_is_cleaned_for_speech(self):
        self.assertEqual(
            voice_assistant.spoken_text("**The Rap**\n\n*Great Lakes*\n---\nLine one"),
            "The Rap\n\nGreat Lakes\n\nLine one",
        )

    def test_idle_and_stuck_voice_escalate_without_polling_spam(self):
        first = voice_assistant.background_prompt("stuck", 120, -1, "Recipe app")
        self.assertEqual(first[0], 0)
        self.assertIsNone(
            voice_assistant.background_prompt("stuck", 121, 0, "Recipe app")
        )
        later = voice_assistant.background_prompt("stuck", 1200, 1, "Recipe app")
        self.assertEqual(later[0], 2)
        self.assertGreater(later[2], first[2])
        self.assertIn("bored", voice_assistant.background_prompt("idle", 120, -1)[1])
        self.assertIn(
            "mic is off", voice_assistant.background_prompt("idle", 120, -1)[1]
        )
        self.assertNotIn(
            "mic is off",
            voice_assistant.background_prompt("idle", 120, -1, mic_off=False)[1],
        )

    def test_personality_varies_across_launches_and_keeps_real_status(self):
        first = voice_assistant.opening_line("working", "Recipe app", 0)
        second = voice_assistant.opening_line("working", "Recipe app", 1)
        self.assertNotEqual(first, second)
        self.assertIn("Recipe app", second)
        self.assertNotEqual(
            voice_assistant.progress_line("Recipe app", "Checking files", 0),
            voice_assistant.progress_line("Recipe app", "Checking files", 1),
        )
        idle_first = voice_assistant.background_prompt("idle", 120, variation=0)
        idle_next = voice_assistant.background_prompt("idle", 120, variation=1)
        self.assertNotEqual(idle_first[1], idle_next[1])
        self.assertIn("mic is off", idle_next[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
