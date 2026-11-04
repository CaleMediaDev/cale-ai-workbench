"""Tone changes stay on the local model and leave a usable fallback."""

import unittest
from unittest.mock import patch

import tone_adaptation


class ToneAdaptationTests(unittest.TestCase):
    def test_empty_input_does_not_require_the_model_dependency(self):
        with patch.dict("sys.modules", {"cloud_ai": None}):
            for utterance in ("", " \t\r\n "):
                with self.subTest(utterance=repr(utterance)):
                    self.assertEqual(tone_adaptation.social_reply(utterance),
                                     "I'm listening. What's up?")

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

    def test_malformed_model_response_leaves_a_short_reply(self):
        for result in ({"choices": None}, {"choices": [{"message": None}]}):
            with self.subTest(result=result):
                with patch("cloud_ai.post", return_value=result):
                    self.assertEqual(tone_adaptation.social_reply("Hello"),
                                     "I heard you. What do you need?")

    def test_non_text_content_is_not_spoken_as_python_or_json(self):
        for content in ({"text": "Hello"}, ["Hello"], 42, True, None):
            with self.subTest(content=content):
                result = {"choices": [{"message": {"content": content}}]}
                with patch("cloud_ai.post", return_value=result):
                    self.assertEqual(tone_adaptation.social_reply("Hello"),
                                     "I heard you. What do you need?")

    def test_line_breaks_are_normalized_without_replacing_the_model_reply(self):
        for separator in ("\n", "\r", "\r\n", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                content = "Hello!" + separator + "What do you need?"
                result = {"choices": [{"message": {"content": content}}]}
                with patch("cloud_ai.post", return_value=result):
                    self.assertEqual(tone_adaptation.social_reply("Hello"), "Hello! What do you need?")

    def test_empty_and_oversized_normalized_replies_use_the_bounded_fallback(self):
        for content in (" \r\n\u2028 ", "Long words " * 50):
            result = {"choices": [{"message": {"content": content}}]}
            with patch("cloud_ai.post", return_value=result):
                self.assertEqual(tone_adaptation.social_reply("Hello"), "I heard you. What do you need?")

    def test_word_limit_is_enforced_even_below_the_character_limit(self):
        for word_count in (34, 35, 40):
            with self.subTest(word_count=word_count):
                content = " ".join(["Hi"] * word_count)
                self.assertLess(len(content), 240)
                result = {"choices": [{"message": {"content": content}}]}
                with patch("cloud_ai.post", return_value=result):
                    answer = tone_adaptation.social_reply("Hello")
                expected = content if word_count < 35 else "I heard you. What do you need?"
                self.assertEqual(answer, expected)

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
