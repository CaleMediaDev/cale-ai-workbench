"""Verify simple questions and sustained projects choose different local models."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_route import (
    PROJECT_REQUEST,
    model_order,
    prefer_different_weights,
    repair_messages,
    repeated_tool_failures,
    task_size,
)
from long_tasks import project_prompt


class AutoRouteTests(unittest.TestCase):
    def test_repair_preference_defers_every_alias_for_failed_weights_without_adding_models(self):
        candidates = ["main", "fast", "alternate"]
        weights = {"main": "a" * 64, "fast": "a" * 64, "alternate": "b" * 64}
        avoid = [{"model": "fast:latest", "weights": "a" * 64}]
        self.assertEqual(prefer_different_weights(candidates, weights, avoid), ["alternate", "main", "fast"])
        self.assertEqual(candidates, ["main", "fast", "alternate"])
        self.assertEqual(prefer_different_weights(candidates[:2], weights, avoid), candidates[:2])

    def test_repair_preference_does_not_blame_replacement_weights_or_malformed_state(self):
        candidates = ["fast", "alternate"]
        old = [{"model": "fast", "weights": "a" * 64}]
        self.assertEqual(prefer_different_weights(candidates, {"fast": "c" * 64}, old), candidates)
        self.assertEqual(prefer_different_weights(candidates, {}, [{"model": "fast"}]), ["alternate", "fast"])
        self.assertEqual(prefer_different_weights(candidates, {}, 42), candidates)

    def test_structured_failed_verdicts_trigger_repair_and_success_clears_it(self):
        for verdict in ({"passed": False}, {"ok": False}, {"status": "failed"}, {"status": "error"}):
            with self.subTest(verdict=verdict):
                messages = [{"role": "user", "content": "Make a rap song with music."}]
                messages += [{"role": "tool", "content": json.dumps(verdict)} for _ in range(3)]
                self.assertEqual(task_size(messages), "deep")
                self.assertIn("Do not repeat", repair_messages(messages)[0]["content"])
                messages.append({"role": "tool", "content": '{"passed":true}'})
                self.assertFalse(repeated_tool_failures(messages))

    def test_only_top_level_boolean_verdicts_are_failures(self):
        for verdict in ({"data": {"passed": False}}, {"passed": "false"}, {"ok": 0}, {"status": []}):
            with self.subTest(verdict=verdict):
                self.assertFalse(repeated_tool_failures([
                    {"role": "tool", "content": json.dumps(verdict)} for _ in range(3)]))

    def test_database_routing_evidence_survives_short_history(self):
        for attempts, repeats, expected in ((1, 0, False), (2, 0, True), (0, 3, True), (0, 0, False)):
            content = 'Current task checkpoint (data):\nNative routing evidence: ' + json.dumps(
                {"v": 1, "failed_step_attempts": attempts, "repeated_check_failures": repeats})
            with self.subTest(attempts=attempts, repeats=repeats):
                messages = [{"role": "system", "content": content},
                            {"role": "tool", "content": "Saved source"}]
                self.assertEqual(repeated_tool_failures(messages), expected)

    def test_routing_evidence_ignores_document_text_old_checkpoints_and_invalid_counts(self):
        evidence = 'Native routing evidence: {"v":1,"failed_step_attempts":4,"repeated_check_failures":0}'
        for role in ("user", "tool", "assistant"):
            self.assertFalse(repeated_tool_failures([
                {"role": role, "content": 'Current task checkpoint:\n' + evidence}]))
        self.assertFalse(repeated_tool_failures([{"role": "system", "content":
            'Current task checkpoint:\nProject excerpts:\n' + evidence}]))
        for value in ('true', '"3"', '-1', '101', 'null'):
            with self.subTest(value=value):
                self.assertFalse(repeated_tool_failures([{"role": "system", "content":
                    'Current task checkpoint:\nNative routing evidence: {"v":1,"failed_step_attempts":'
                    + value + ',"repeated_check_failures":0}'}]))
        self.assertFalse(repeated_tool_failures([
            {"role": "system", "content": 'Current task checkpoint:\n' + evidence},
            {"role": "system", "content": 'Current task checkpoint:\nNative routing evidence: '
             '{"v":1,"failed_step_attempts":0,"repeated_check_failures":0}'}]))

    def test_mcp_errors_trigger_repair_guidance_without_mutating_history(self):
        messages = [{"role": "user", "content": "Build an app"}]
        messages.extend({"role": "tool", "content": '{"isError":true,"content":[]}'} for _ in range(3))
        self.assertEqual(task_size(messages), "deep")
        repaired = repair_messages(messages)
        self.assertIsNot(repaired, messages)
        self.assertEqual(len(repaired), len(messages) + 1)
        self.assertIn("Do not repeat", repaired[0]["content"])
        self.assertEqual(messages[0]["role"], "user")
        short = messages[:2]
        self.assertIs(repair_messages(short), short)
        resolved = [*messages, {"role": "tool", "content": '{"success":true}'}]
        self.assertIs(repair_messages(resolved), resolved)

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

    def test_native_worker_sizing_reads_its_saved_request(self):
        import native_agent

        webapp = native_agent.prompt({
            "id": "native-test", "folder": "C:/Tasks/test",
            "brief": "Build and test an offline tip-splitter app.",
        })
        messages = [{"role": "system", "content": webapp},
                    {"role": "user", "content": "Begin the task. Inspect saved progress."}]
        self.assertEqual(task_size(messages), "deep")
        messages.append({"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file"}},
        ]})
        self.assertEqual(task_size(messages), "balanced")

        question = native_agent.prompt({
            "id": "native-question", "folder": "C:/Tasks/test",
            "brief": "How do I create a folder?",
        })
        self.assertEqual(task_size([{"role": "system", "content": question},
                                    {"role": "user", "content": "Begin the task."}]), "quick")

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

    def test_recent_tool_failure_loop_uses_stronger_model(self):
        messages = [{"role": "user", "content": "Make a rap song with music."}]
        messages += [
            {"role": "tool", "content": '{"error":"Cannot read binary file"}'},
            {"role": "tool", "content": '{"output":"missing file","exit_code":2,"error":null}'},
            {"role": "tool", "content": '{"output":"","exit_code":-1,"status":"blocked"}'},
        ]
        self.assertEqual(task_size(messages), "deep")
        messages += [{"role": "tool", "content": '{"output":"ok","exit_code":0,"error":null}'}] * 8
        self.assertEqual(task_size(messages), "quick")

    def test_quoted_loop_warnings_in_successful_output_clear_repair(self):
        warning = "[Tool loop warning: repeated call]"
        failures = [{"role": "tool", "content": warning} for _ in range(3)]
        self.assertTrue(repeated_tool_failures(failures))
        for content in (
            "Exit code: 0\nFound source text: " + warning,
            json.dumps({"success": True, "output": warning}),
        ):
            with self.subTest(content=content):
                messages = failures + [
                    {"role": "tool", "content": content} for _ in range(3)
                ]
                self.assertFalse(repeated_tool_failures(messages))
                self.assertIs(repair_messages(messages), messages)

    def test_native_plain_text_tool_failures_escalate_then_recover(self):
        messages = [{"role": "user", "content": "Build and test a recipe app."},
                    {"role": "assistant", "tool_calls": [{"function": {"name": "run_command"}}]}]
        messages += [
            {"role": "tool", "content": "Tool error: FileNotFoundError: missing"},
            {"role": "tool", "content": "Exit code: 1\nBuild failed"},
            {"role": "tool", "content": "Tool error: ValueError: bad argument"},
        ]
        self.assertEqual(task_size(messages), "deep")
        messages.append({"role": "tool", "content": "Exit code: 0\nBuild passed"})
        self.assertEqual(task_size(messages), "balanced")

    def test_native_exit_codes_support_lf_and_crlf_and_clear_repair(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                messages = [
                    {"role": "user", "content": "Build and test a recipe app."},
                    {"role": "assistant", "tool_calls": [
                        {"function": {"name": "run_command"}},
                    ]},
                ]
                messages.extend(
                    {"role": "tool", "content": f"Exit code: {code}{newline}Build failed"}
                    for code in (1, -1, 2)
                )
                self.assertTrue(repeated_tool_failures(messages))
                self.assertEqual(task_size(messages), "deep")
                self.assertIn("Do not repeat", repair_messages(messages)[0]["content"])
                messages.append({
                    "role": "tool", "content": f"Exit code: 0{newline}Build passed",
                })
                self.assertFalse(repeated_tool_failures(messages))
                self.assertEqual(task_size(messages), "balanced")
                self.assertIs(repair_messages(messages), messages)

    def test_missing_verifier_handoff_uses_deeper_model(self):
        messages = [{"role": "user", "content": "Build a recipe app."},
                    {"role": "user", "content": "[Completion gate: missing verifier] Create real checks."}]
        self.assertEqual(task_size(messages), "deep")

    def test_music_review_gets_strong_first_pass_and_balanced_followups(self):
        prompt = ("# Kanban task t_123: Michigan song — review\n\n## Body\n" +
                  project_prompt({"title": "Michigan song", "folder": "C:/Tasks/song",
                                  "brief": "Make a rap song with music."}))
        messages = [{"role": "user", "content": prompt}]
        self.assertEqual(task_size(messages), "deep")
        messages.append({"role": "assistant", "tool_calls": [{"function": {"name": "read_file"}}]})
        self.assertEqual(task_size(messages), "balanced")

    def test_music_review_shortcut_does_not_downgrade_complex_app_work(self):
        for brief in (
            "Create a production-ready music app.",
            "Create a music app with distributed rendering.",
        ):
            with self.subTest(brief=brief):
                prompt = ("# Kanban task t_123: Music app — review\n\n## Body\n" +
                          project_prompt({"title": "Music app", "folder": "C:/Tasks/app",
                                          "brief": brief}))
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "tool_calls": [
                        {"function": {"name": "read_file"}},
                    ]},
                ]
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

    def test_song_render_followup_uses_fast_tool_model(self):
        prompt = project_prompt({
            "title": "Tahoe anthem", "folder": "C:/Tasks/music",
            "brief": "150 BPM wobbly dubstep anthem with female singing and three drops.",
        })
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "tool_calls": [{
                        "function": {"name": "mcp__toolbox__start_music_job"}}]},
                    {"role": "tool", "tool_name": "mcp__toolbox__start_music_job",
                     "content": "Job started"}]
        self.assertEqual(task_size(messages), "quick")

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

    def test_resumed_music_with_finished_files_starts_on_balanced_model(self):
        prompt = project_prompt({
            "title": "Michigan song", "folder": "C:/Tasks/music",
            "brief": "Make a rap song about Michigan with music, one minute long.",
        }, "\nSaved recovery snapshot:\n"
           "Existing project files (inspect and verify before regenerating):\n"
           "- michigan_song_final.wav (12,000,000 bytes)")
        self.assertEqual(task_size([{"role": "user", "content": prompt}]), "balanced")

    def test_detailed_song_brief_does_not_use_largest_local_model(self):
        brief = (
            "Create a new song for Cale and Tiny's anniversary with female vocals. "
            "It should have a 150 BPM dubstep groove, deep wobble bass, a melodic "
            "intro, clear verses, a big chorus, a breakdown, and a final drop. "
            "Keep the lyrics personal and the mix clean for the whole three-minute "
            "track. Describe the arrangement, vocal delivery, dynamics, and sound "
            "design precisely enough for the local music renderer. "
        ) * 3
        prompt = project_prompt({
            "title": "Anniversary song", "folder": "C:/Tasks/song", "brief": brief,
        })
        messages = [{"role": "user", "content": prompt}]
        self.assertEqual(task_size(messages), "balanced")
        messages.append({
            "role": "assistant",
            "tool_calls": [{"function": {"name": "mcp__toolbox__toolbox_call"}}] * 5,
        })
        self.assertEqual(task_size(messages), "balanced")

    def test_bpm_first_song_brief_without_song_word_uses_middle_model(self):
        brief = (
            "150 BPM, extremely wobbly melodic dubstep, old-school UK wobble "
            "bass, rhythmic synth sweeps, huge drops, and wide stereo drums. "
            "Super sweet adult female singing voice with clear vocals and "
            "beautiful harmonies. Create a Lake Tahoe summer anthem with an "
            "emotional chorus, three massive drops, and a dreamy breakdown."
        )
        prompt = project_prompt({
            "title": "150 BPM Lake Tahoe anthem", "folder": "C:/Tasks/song",
            "brief": brief,
        })
        messages = [{"role": "user", "content": prompt}]
        self.assertEqual(task_size(messages), "balanced")
        messages.append({
            "role": "assistant",
            "tool_calls": [{"function": {"name": "mcp__toolbox__toolbox_call"}}] * 5,
        })
        self.assertEqual(task_size(messages), "balanced")

    def test_resumed_condensed_board_song_stays_balanced(self):
        prompt = (
            "# Kanban task t_123: Create Cale and Tiny anniversary dubstep song\n\n"
            "## Body\nWork in the project folder C:/Tasks/song. Read the saved "
            "request and follow-ups. Generate the song: 150 BPM melodic "
            "dubstep with female vocals, three drops, and a romantic chorus. "
            "Verify the saved audio and finish the task.\n\n"
            "## Prior attempts on this task\nAttempt 1 was paused.\n"
        )
        self.assertEqual(task_size([{"role": "user", "content": prompt}]), "balanced")

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

    def test_deep_task_avoids_measured_slow_giant_when_fast_large_model_exists(self):
        models = {"giant": 79, "newer": 35.5, "fast": 9}
        speeds = {"giant": 6.0, "newer": 18.5, "fast": 35.0}
        self.assertEqual(
            model_order("deep", models, "giant", "fast", "newer", speeds)[0],
            "newer",
        )
        self.assertEqual(
            model_order("deep", models, "giant", "fast", "newer")[0],
            "giant",  # Unknown performance still gets a chance to be measured.
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
