"""Voice control uses the local media worker and bounds recorded clips."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice_assistant


class VoiceTests(unittest.TestCase):
    def test_listen_returns_trimmed_local_transcript(self):
        with patch.object(
            voice_assistant, "media_call", return_value={"text": "  Hello  "}
        ) as call:
            self.assertEqual(voice_assistant.listen(8), "Hello")
        call.assert_called_once_with("record_audio", seconds=8)

    def test_speech_uses_local_worker(self):
        with patch.object(voice_assistant, "media_call", return_value={}) as call:
            voice_assistant.say("Task completed", rate=2)
        self.assertEqual(call.call_args.args, ("speak",))
        self.assertEqual(call.call_args.kwargs["text"], "Task completed")
        self.assertTrue(call.call_args.kwargs["play"])
        self.assertEqual(call.call_args.kwargs["rate"], 2)

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
