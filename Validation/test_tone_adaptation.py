"""Tone changes stay on the local model and leave a usable fallback."""

import unittest
from unittest.mock import patch

import tone_adaptation


class ToneAdaptationTests(unittest.TestCase):
    def test_social_reply_uses_fast_local_model(self):
        result = {"choices": [{"message": {"content": "Well, that was rude. What do you need?"}}]}
        with patch("cloud_ai.post", return_value=result) as post:
            answer = tone_adaptation.social_reply("You are useless.")
        self.assertEqual(answer, "Well, that was rude. What do you need?")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/v1/chat/completions")
        messages = post.call_args.args[1]["messages"]
        self.assertIn("You are useless.", messages[-1]["content"])
        self.assertIn("rude", messages[0]["content"])

    def test_model_failure_leaves_a_short_reply(self):
        with patch("cloud_ai.post", side_effect=OSError("offline")):
            self.assertEqual(tone_adaptation.social_reply("Hello"),
                             "I heard you. What do you need?")


if __name__ == "__main__":
    unittest.main()
