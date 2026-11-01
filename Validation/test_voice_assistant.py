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
            voice_assistant.say("Task completed")
        self.assertEqual(call.call_args.args, ("speak",))
        self.assertEqual(call.call_args.kwargs["text"], "Task completed")
        self.assertTrue(call.call_args.kwargs["play"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
