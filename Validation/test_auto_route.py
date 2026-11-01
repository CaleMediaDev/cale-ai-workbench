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

    def test_short_build_uses_middle_model_after_initial_planning(self):
        messages = [{"role": "user", "content": "Build and test a recipe app."}]
        self.assertEqual(task_size(messages), "deep")
        messages.append({
            "role": "assistant", "tool_calls": [{"function": {"name": "read_file"}}],
        })
        self.assertEqual(task_size(messages), "balanced")
        for _ in range(6):
            messages.append({
                "role": "assistant", "tool_calls": [{"function": {"name": "write_file"}}],
            })
        self.assertEqual(task_size(messages), "balanced")
        messages[0]["content"] = "Build a production-ready multi-page recipe app."
        self.assertEqual(task_size(messages), "deep")

    def test_saved_project_brief_uses_same_step_routing_after_compaction(self):
        prompt = project_prompt({
            "title": "Recipe app", "folder": "C:/Tasks/recipe",
            "brief": "Build and test a recipe app.",
        })
        messages = [{"role": "user", "content": prompt}]
        self.assertEqual(task_size(messages), "deep")
        messages.append({
            "role": "assistant", "tool_calls": [{"function": {"name": "read_file"}}],
        })
        self.assertEqual(task_size(messages), "balanced")

    def test_large_worker_guidance_keeps_the_original_short_request(self):
        prompt = project_prompt(
            {"title": "Help", "folder": "C:/Tasks/help",
             "brief": "How do I create a folder?"},
            "\nPrevious supervisor result:\n" + "Progress details. " * 1500,
        )
        self.assertGreater(len(prompt), 16000)
        self.assertEqual(task_size([{"role": "user", "content": prompt}]), "quick")

    def test_oversized_original_project_stays_deep_with_bounded_preview(self):
        prompt = project_prompt(
            {"title": "Large build", "folder": "C:/Tasks/large",
             "brief": "Build an app with these requirements:\n" + "feature detail\n" * 1600},
        )
        self.assertLess(len(prompt), 16000)
        self.assertIn("characters omitted", prompt)
        self.assertEqual(task_size([{"role": "user", "content": prompt}]), "deep")

    def test_late_followup_can_escalate_a_short_original_project(self):
        prompt = project_prompt(
            {"title": "Help", "folder": "C:/Tasks/help",
             "brief": "How do I create a folder?"},
            "\nPrevious supervisor result:\n" + "Routine status. " * 1500
            + "\nRecent user follow-ups (read the full file):\n"
            + "Request 9: Build a production-ready multi-page file manager app.\n"
            + "Previous supervisor result:\nWaiting",
        )
        self.assertGreater(len(prompt), 16000)
        self.assertEqual(task_size([{"role": "user", "content": prompt}]), "deep")

    def test_large_followup_escalates_small_persistent_project(self):
        prompt = project_prompt(
            {"title": "Help", "folder": "C:/Tasks/help", "brief": "How do I create a folder?"},
            "\nRecent user follow-ups (read the full file):\n"
            "Request 8: Build a production-ready multi-page file manager app.\n"
            "Previous supervisor result:\nWaiting",
        )
        self.assertEqual(task_size([{"role": "user", "content": prompt}]), "deep")

    def test_new_direct_user_instruction_escalates_saved_project(self):
        prompt = project_prompt({
            "title": "Help", "folder": "C:/Tasks/help", "brief": "How do I create a folder?",
        })
        self.assertEqual(task_size([
            {"role": "user", "content": prompt},
            {"role": "user", "content": "Build a production-ready multi-page file manager app."},
        ]), "deep")

    def test_kanban_protocol_nudge_does_not_escalate_small_music_work(self):
        prompt = project_prompt({
            "title": "Michigan song", "folder": "C:/Tasks/music",
            "brief": "Make a rap song about Michigan with music, one minute long.",
        }, "\nRecent user follow-ups (read the full file):\n"
           "Request 19: add vocals\nPrevious supervisor result:\nWorking")
        messages = [{"role": "user", "content": prompt}]
        for _ in range(4):
            messages.append({
                "role": "assistant",
                "tool_calls": [{"function": {"name": "mcp__toolbox__toolbox_call"}}],
            })
        self.assertEqual(task_size(messages), "balanced")
        messages.append({"role": "user", "content": (
            "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
            "terminal state for the board. Finish the remaining deliverable. "
            "Call kanban_request_review or kanban_block. Repeated protocol violations "
            "will block this task and require manual intervention.]"
        )})
        self.assertEqual(task_size(messages), "balanced")
        messages.insert(-1, {"role": "user", "content": (
            "Build a production-ready multi-page music website too."
        )})
        self.assertEqual(task_size(messages), "deep")

    def test_short_tool_work_stays_on_middle_model_without_project_marker(self):
        messages = [{"role": "user", "content": "Analyze the attached data."}]
        for _ in range(4):
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "read_file"}}],
                }
            )
        self.assertEqual(task_size(messages), "balanced")

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

    def test_music_shortcut_does_not_downgrade_app_or_video_work(self):
        self.assertEqual(
            task_size([{"role": "user", "content": "Create a music app."}]),
            "deep",
        )
        self.assertEqual(
            task_size([{"role": "user", "content": "Build a production-ready music website."}]),
            "deep",
        )
        self.assertEqual(
            task_size([{"role": "user", "content": "Make a music video from this song."}]),
            "balanced",
        )

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

    def test_approved_newer_middle_model_wins_within_same_size_band(self):
        models = {"old": 34.7, "new": 35.5, "fast": 9, "main": 79}
        self.assertEqual(model_order("balanced", models, "main", "fast", "new")[0], "new")
        self.assertEqual(model_order("balanced", models, "main", "fast", "fast")[0], "old")


if __name__ == "__main__":
    unittest.main(verbosity=2)
