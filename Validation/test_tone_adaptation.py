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

    def test_next_speaker_does_not_inherit_previous_rudeness(self):
        result = {"choices": [{"message": {"content": "Glad I could help!"}}]}
        with patch("cloud_ai.post", return_value=result) as post:
            tone_adaptation.social_reply("You are useless.")
            tone_adaptation.social_reply("Thanks, Cale!")
        first = post.call_args_list[0].args[1]["messages"]
        second = post.call_args_list[1].args[1]["messages"]
        self.assertEqual(first[-1]["content"], "You are useless.")
        self.assertEqual(second[-1]["content"], "Thanks, Cale!")
        self.assertNotIn("useless", str(second))
        self.assertIn("Reset your tone every turn", second[0]["content"])


if __name__ == "__main__":
    unittest.main()
