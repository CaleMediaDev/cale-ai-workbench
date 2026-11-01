"""Voice control uses the local media worker and bounds recorded clips."""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice_assistant


class VoiceTests(unittest.TestCase):
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
        ), patch.dict(sys.modules, {"cloud_ai": fake}):
            spoken = voice_assistant.rephrase_status(
                "Recipe app. Checking files.", ["Still checking files"],
                ("Recipe app", "checking files"),
            )
        self.assertEqual(spoken, "Recipe app: checking files, one folder at a time.")
        self.assertEqual(calls[0][0], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(calls[0][1]["model"], "cale-fast")
        self.assertLessEqual(calls[0][2]["timeout"], 12)

    def test_status_rephrasing_rejects_missing_fact(self):
        fake = ModuleType("cloud_ai")
        fake.FAST_MODEL = "cale-fast"
        fake.post = lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": "I'm making excellent progress!"}}]
        }
        with TemporaryDirectory() as folder, patch.object(
            voice_assistant, "ROOT", Path(folder)
        ), patch.dict(sys.modules, {"cloud_ai": fake}):
            spoken = voice_assistant.rephrase_status(
                "Recipe app. Checking files.", required=("Recipe app", "Checking files")
            )
        self.assertEqual(spoken, "Recipe app. Checking files.")

    def test_music_render_keeps_status_speech_from_reloading_gpu_model(self):
        from types import SimpleNamespace

        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "LongTasks"
            state.mkdir()
            (state / "music-gpu-active-123.json").write_text('{"pid":123}')
            with patch.object(voice_assistant, "ROOT", root), patch(
                "psutil.pid_exists", return_value=True
            ), patch.dict(sys.modules, {"cloud_ai": SimpleNamespace()}):
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

    def test_speech_uses_local_worker(self):
        with patch("cale_voice.ready", return_value=False), patch.object(
            voice_assistant, "media_call", return_value={}
        ) as call:
            voice_assistant.say("Task completed", rate=2)
        self.assertEqual(call.call_args.args, ("speak",))
        self.assertEqual(call.call_args.kwargs["text"], "Task completed")
        self.assertTrue(call.call_args.kwargs["play"])
        self.assertEqual(call.call_args.kwargs["rate"], 2)

    def test_long_chat_reply_is_spoken_without_truncation(self):
        message = " ".join(f"word{index}" for index in range(150))
        chunks = voice_assistant.speech_chunks(message)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 280 for chunk in chunks))
        self.assertEqual(" ".join(chunks), message)

    def test_spoken_replay_can_preempt_a_long_ordinary_reply(self):
        import queue

        pending = queue.PriorityQueue()
        pending.put((0, 2, "ordinary reply"))
        self.assertFalse(voice_assistant.urgent_speech_waiting(pending, 0))
        pending.put((-2, 3, "requested replay"))
        self.assertTrue(voice_assistant.urgent_speech_waiting(pending, 0))
        self.assertFalse(voice_assistant.urgent_speech_waiting(pending, -2))
        self.assertEqual(pending.get()[2], "requested replay")

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
