"""Verify simple questions and sustained projects choose different local models."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_route import PROJECT_REQUEST, model_order, task_size
from long_tasks import project_prompt


class AutoRouteTests(unittest.TestCase):
    def test_current_business_contact_uses_tool_capable_middle_tier(self):
        self.assertEqual(
            task_size(
                [
                    {
                        "role": "user",
                        "content": "What's Pure Michigan Lawn Care's number?",
                    }
                ]
            ),
            "balanced",
        )
        self.assertEqual(
            task_size([{"role": "user", "content": "whats the shop's phone number"}]),
            "balanced",
        )

    def test_stable_short_question_uses_fast_tier(self):
        self.assertEqual(
            task_size([{"role": "user", "content": "How do I create a folder?"}]),
            "quick",
        )

    def test_short_tool_tasks_do_not_use_fast_tier(self):
        for request in (
            "Read the attached PDF and summarize it.",
            "Search the web for the current rules.",
            "Make an image of a beach.",
            "Create a photo of a beach.",
            "Analyze my CSV data.",
            "Can you inspect this screenshot?",
        ):
            with self.subTest(request=request):
                self.assertEqual(task_size([{"role": "user", "content": request}]),
                                 "balanced")

    def test_media_part_and_short_project_request_escalate(self):
        self.assertEqual(task_size([{"role": "user", "content": [
            {"type": "text", "text": "What is in this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ]}]), "balanced")
        self.assertEqual(task_size([{"role": "user", "content": "Make me an app."}]),
                         "deep")

    def test_short_implementation_requests_use_deep_tier(self):
        for request in (
            "Add a new tool to the app.",
            "Improve its logic and capabilities.",
            "Install the missing runtime.",
            "Update the website UI.",
        ):
            with self.subTest(request=request):
                self.assertEqual(task_size([{"role": "user", "content": request}]),
                                 "deep")

    def test_project_wrapper_does_not_force_every_task_to_large_model(self):
        def wrapped(brief):
            return [
                {
                    "role": "user",
                    "content": project_prompt(
                        {"title": "Task", "folder": "C:/Tasks/test", "brief": brief}
                    ),
                }
            ]

        self.assertEqual(task_size(wrapped("How do I create a folder?")), "quick")
        self.assertEqual(
            task_size(wrapped("What's Pure Michigan Lawn Care's number?")),
            "balanced",
        )
        self.assertEqual(task_size(wrapped("Build and test a recipe app.")), "deep")
        self.assertEqual(task_size(wrapped("Build a weather app.")), "deep")

    def test_project_sizing_uses_original_request_not_worker_guidance(self):
        prompt = project_prompt({
            "title": "Task", "folder": "C:/Tasks/test",
            "brief": "How do I create a folder?",
        })
        self.assertEqual(PROJECT_REQUEST.search(prompt).group(1).strip(),
                         "How do I create a folder?")
        self.assertEqual(task_size([{"role": "user", "content": prompt}]), "quick")

    def test_build_and_saved_project_use_deep_tier(self):
        self.assertEqual(
            task_size([{"role": "user", "content": "Build and test a recipe app."}]),
            "deep",
        )
        self.assertEqual(
            task_size(
                [
                    {
                        "role": "user",
                        "content": "USER PROJECT: Recipe app\nSaved progress in .local-ai/PROGRESS.md",
                    }
                ]
            ),
            "deep",
        )

    def test_tool_heavy_work_escalates_without_project_marker(self):
        messages = [{"role": "user", "content": "Analyze the attached data."}]
        for _ in range(4):
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "read_file"}}],
                }
            )
        self.assertEqual(task_size(messages), "deep")

    def test_short_music_request_stays_fast_through_tool_discovery(self):
        messages = [{"role": "user", "content": (
            "Make a 10-second instrumental beach guitar jingle using your local music tool."
        )}]
        self.assertEqual(task_size(messages), "quick")
        for _ in range(2):
            messages.append({
                "role": "assistant",
                "tool_calls": [{"function": {"name": "mcp__toolbox__toolbox_search"}}],
            })
            self.assertEqual(task_size(messages), "quick")
        messages.append({
            "role": "assistant",
            "tool_calls": [{"function": {"name": "mcp__toolbox__toolbox_call"}}],
        })
        self.assertEqual(task_size(messages), "quick")
        for _ in range(5):
            messages.append({
                "role": "assistant",
                "tool_calls": [{"function": {"name": "mcp__toolbox__toolbox_schema"}}],
            })
        self.assertEqual(task_size(messages), "balanced")

    def test_ranking_respects_available_model_sizes(self):
        models = {"fast": 9, "balanced": 35, "main": 79}
        self.assertEqual(
            model_order("quick", models, "main", "fast", "balanced")[0], "fast"
        )
        self.assertEqual(
            model_order("balanced", models, "main", "fast", "balanced")[0], "balanced"
        )
        self.assertEqual(
            model_order("deep", models, "main", "fast", "balanced")[0], "main"
        )
        models["main"] = 9
        self.assertEqual(
            model_order("deep", models, "main", "fast", "balanced")[0], "balanced"
        )
        models["main"] = 4
        self.assertEqual(
            model_order("quick", models, "main", "fast", "balanced")[0], "main"
        )
        models["other"] = 70
        self.assertEqual(
            model_order("deep", models, "main", "fast", "balanced")[0], "other"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
