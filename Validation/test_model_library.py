"""Check automatic model selection without downloading any model weights."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configure_install
import model_library as library


class AutoModelTests(unittest.TestCase):
    def setUp(self):
        # The installed self-check runs beside real user tasks. Most model
        # fixtures describe an idle PC; keep them independent of whichever
        # project happens to start while pytest is running. Tests of active
        # task behavior override this patch with their own project list.
        self.projects_patch = patch("long_tasks.projects", return_value=[])
        self.projects_patch.start()
        self.addCleanup(self.projects_patch.stop)

    def test_missing_vision_alias_is_rebuilt_only_from_local_vision_weights(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            created = []

            def post(path, body, **_kwargs):
                model = body.get("model")
                if path == "/api/show" and model == "cale-vision":
                    if not created:
                        raise OSError("alias missing")
                    return {"capabilities": ["vision"], "parameters": "num_ctx 8192"}
                if path == "/api/show" and model == "cale-fast":
                    return {"capabilities": ["tools", "vision"]}
                if path == "/api/create":
                    created.append(body)
                    return {"status": "success"}
                raise OSError("model unavailable")

            with (patch.object(library, "ROOT", root),
                  patch("long_tasks.projects", return_value=[]),
                  patch.object(library, "main_source_record", return_value={}),
                  patch.object(library, "post", side_effect=post)):
                self.assertTrue(library.repair_vision_alias())
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]["from"], "cale-fast")
            self.assertEqual(created[0]["model"], "cale-vision")
            self.assertEqual(json.loads((root / "LongTasks/vision-repair.json").read_text())["status"],
                             "complete")

    def test_vision_repair_waits_while_tasks_are_active(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (patch.object(library, "ROOT", root),
                  patch("long_tasks.projects", return_value=[{"status": "working"}]),
                  patch.object(library, "post") as post):
                self.assertFalse(library.repair_vision_alias())
            post.assert_not_called()
            self.assertEqual(json.loads((root / "LongTasks/vision-repair.json").read_text())["status"],
                             "waiting_tasks")

    def test_vision_repair_rejects_text_only_and_remote_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def post(path, body, **_kwargs):
                if path == "/api/create":
                    self.fail("text-only or remote weights must not become the image reader")
                if body["model"] == "cale-vision":
                    raise OSError("alias missing")
                if body["model"] == "cale-fast":
                    return {"capabilities": ["tools"]}
                return {"capabilities": ["vision"], "remote_host": "example.org"}

            with (patch.object(library, "ROOT", root),
                  patch("long_tasks.projects", return_value=[]),
                  patch.object(library, "main_source_record", return_value={}),
                  patch.object(library, "post", side_effect=post)):
                self.assertFalse(library.repair_vision_alias())
            self.assertEqual(json.loads((root / "LongTasks/vision-repair.json").read_text())["status"],
                             "waiting_models")

    def test_light_install_reuses_main_weights_for_vision_and_fast_route(self):
        self.assertEqual(configure_install.auxiliary_source("light", "qwen3.5:4b"),
                         "qwen3.5:4b")
        self.assertEqual(configure_install.auxiliary_source("compact", "qwen3.5:9b"),
                         "qwen3.5:9b")
        self.assertEqual(configure_install.auxiliary_source("large", "qwen3-coder-next:q4_K_M"),
                         "qwen3.5:9b")

    def test_installer_claims_a_source_only_when_ollama_did_not_have_its_tag(self):
        tags = {"models": [{"name": "qwen3.5:9b"}]}
        with patch.object(configure_install, "urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(json.dumps(tags).encode())):
            self.assertTrue(configure_install.source_already_installed("qwen3.5:9b"))
            self.assertFalse(configure_install.source_already_installed("qwen3.5:35b"))

    def test_retired_main_source_cleanup_requires_owned_source_and_ready_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (patch.object(library, "ROOT", root),
                  patch.object(library, "CATALOG", {"main": {"alias": "cale-local"}}),
                  patch("long_tasks.projects", return_value=[]),
                  patch.object(library, "post", return_value={
                      "capabilities": ["tools"], "parameters": "num_ctx 131072\n"}),
                  patch.object(library, "ensure_main_standard", return_value=True) as standard,
                  patch.object(library, "owned_sources", return_value={"old:large"}),
                  patch.object(library, "delete_model") as delete):
                library.record_main_source("new:balanced", False)
                library.queue_retired_main_source("old:large")
                library.queue_retired_main_source("user:personal")
                self.assertEqual(library.cleanup_retired_main_sources(), 1)
                self.assertEqual(json.loads((root / "LongTasks/retired-main-sources.json").read_text()), [])
            standard.assert_called_once()
            delete.assert_called_once_with("old:large")

    def test_retired_main_source_waits_for_active_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (patch.object(library, "ROOT", root),
                  patch("long_tasks.projects", return_value=[{"status": "working"}]),
                  patch.object(library, "delete_model") as delete):
                library.queue_retired_main_source("old:large")
                self.assertEqual(library.cleanup_retired_main_sources(), 0)
            delete.assert_not_called()

    def test_retired_main_source_waits_if_replacement_record_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (patch.object(library, "ROOT", root),
                  patch("long_tasks.projects", return_value=[]),
                  patch.object(library, "ensure_main_standard") as standard,
                  patch.object(library, "delete_model") as delete):
                library.queue_retired_main_source("old:large")
                self.assertEqual(library.cleanup_retired_main_sources(), 0)
                self.assertEqual(json.loads((root / "LongTasks/retired-main-sources.json").read_text()),
                                 ["old:large"])
            standard.assert_not_called()
            delete.assert_not_called()

    def test_main_replacement_queues_only_its_recorded_owned_predecessor(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog = {"main": {"alias": "cale-local"},
                       "balanced": {"alias": "cale-balanced", "source": "new:balanced"}}
            tags = {"models": [{"name": "cale-balanced:latest", "size": 21 * 2**30}]}

            def post(path, _body, **_kwargs):
                return ({"capabilities": ["tools"], "parameters": "num_ctx 65536\n"}
                        if path == "/api/show" else {"status": "success"})

            with (patch.object(library, "ROOT", root),
                  patch.object(library, "CATALOG", catalog),
                  patch.object(library, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
                  patch.object(library, "model_ready", return_value=True),
                  patch("model_fitness.suitable", return_value=True),
                  patch("model_fitness.measured_speed", return_value=9),
                  patch.object(library, "owned_sources", return_value={"old:large", "new:balanced"}),
                  patch.object(library, "post", side_effect=post),
                  patch("context_window.sync", return_value=65536)):
                library.record_main_source("old:large", True)
                self.assertTrue(library.repair_main_alias(128, 12))
                self.assertEqual(library.main_source_record(),
                                 {"source": "new:balanced", "owned": True})
                self.assertEqual(json.loads((root / "LongTasks/retired-main-sources.json").read_text()),
                                 ["old:large"])

    def test_missing_main_alias_is_recreated_from_a_verified_suitable_model(self):
        catalog = {
            "main": {"alias": "cale-local"},
            "fast": {"alias": "cale-fast", "auto": True},
            "balanced": {"alias": "cale-balanced", "auto": True},
            "fallback": {"alias": "cale-fallback", "fallback_only": True},
        }
        tags = {"models": [
            {"name": "cale-fast:latest", "size": 6 * 2**30},
            {"name": "cale-balanced:latest", "size": 21 * 2**30},
            {"name": "cale-fallback:latest", "size": 7 * 2**30},
        ]}
        calls = []

        def post(path, body, **_kwargs):
            calls.append((path, body))
            if path == "/api/show":
                return {"capabilities": ["tools"], "parameters": "num_ctx 65536\n"}
            return {"status": "success"}

        with (patch.object(library, "CATALOG", catalog),
              patch.object(library, "urlopen", side_effect=lambda *_args, **_kwargs:
                           io.BytesIO(json.dumps(tags).encode())),
              patch.object(library, "model_ready", return_value=True),
              patch("model_fitness.suitable", return_value=True),
              patch("model_fitness.measured_speed", side_effect=lambda alias: 30 if alias == "cale-fast" else None),
              patch.object(library, "post", side_effect=post),
              patch("context_window.sync", return_value=65536) as sync):
            self.assertTrue(library.repair_main_alias(128, 12))
        created = next(body for path, body in calls if path == "/api/create")
        self.assertEqual(created["model"], "cale-local")
        self.assertEqual(created["from"], "cale-balanced")
        sync.assert_called_once_with(library.ROOT, "cale-local")

    def test_existing_main_upgrades_only_to_distinct_measured_faster_weights(self):
        old_blob, new_blob = "a" * 64, "b" * 64
        catalog = {
            "main": {"alias": "cale-local"},
            "old": {"alias": "cale-qwen35", "source": "qwen3.5:35b", "auto": True},
            "new": {"alias": "cale-qwen36", "source": "qwen3.6:35b", "auto": True},
        }
        tags = {"models": [
            {"name": "cale-local:latest", "size": 21 * 2**30},
            {"name": "cale-qwen35:latest", "size": 21 * 2**30},
            {"name": "cale-qwen36:latest", "size": 22 * 2**30},
        ]}
        created = []

        def post(path, body, **_kwargs):
            if path == "/api/create":
                created.append(body)
                return {}
            alias = body["model"]
            blob = new_blob if alias == "cale-qwen36" or alias == "cale-local" and created else old_blob
            return {"capabilities": ["completion", "tools"],
                    "parameters": "num_ctx 262144\n",
                    "model_info": {"qwen.context_length": 262144},
                    "modelfile": "FROM C:\\models\\sha256-" + blob + "\n"}

        with (patch.object(library, "CATALOG", catalog),
              patch.object(library, "urlopen", side_effect=lambda *_args, **_kwargs:
                           io.BytesIO(json.dumps(tags).encode())),
              patch.object(library, "model_ready", return_value=True),
              patch.object(library, "alias_update_waits_for_tasks", return_value=False),
              patch("model_fitness.suitable", return_value=True),
              patch("model_fitness.measured_speed", side_effect=lambda alias: {
                  "cale-qwen35": 9.1, "cale-qwen36": 18.5,
              }.get(alias)),
              patch.object(library, "post", side_effect=post),
              patch.object(library, "main_source_record", return_value={}),
              patch.object(library, "owned_sources", return_value=set()),
              patch.object(library, "record_main_source"),
              patch("context_window.sync", return_value=262144) as sync,
              patch.object(library, "ensure_main_standard", return_value=True) as routine):
            self.assertTrue(library.repair_main_alias(128, 12))
            self.assertFalse(library.repair_main_alias(128, 12))
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["from"], "cale-qwen36")
        self.assertEqual(created[0]["parameters"]["num_ctx"], 262144)
        sync.assert_called_once_with(library.ROOT, "cale-local")
        routine.assert_called_once()

    def test_main_repair_uses_fresh_configured_alias_after_catalog_was_imported(self):
        catalog = {"main": {"alias": "cale-local"},
                   "fast": {"alias": "cale-fast", "auto": True}}
        tags = {"models": [{"name": "cale-fast:latest", "size": 6 * 2**30}]}
        calls = []

        def post(path, body, **_kwargs):
            calls.append((path, body))
            return ({"capabilities": ["tools"], "parameters": "num_ctx 65536\n"}
                    if path == "/api/show" else {"status": "success"})

        with (patch.object(library, "CATALOG", catalog),
              patch.object(library, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch.object(library, "model_ready", return_value=True),
              patch("model_fitness.suitable", return_value=True),
              patch("model_fitness.measured_speed", return_value=20),
              patch.object(library, "post", side_effect=post),
              patch.object(library, "record_main_source"),
              patch.object(library, "owned_sources", return_value=set()),
              patch("context_window.sync", return_value=65536)):
            self.assertTrue(library.repair_main_alias(32, 12, alias="qwen3-coder-local"))
        created = next(body for path, body in calls if path == "/api/create")
        self.assertEqual(created["model"], "qwen3-coder-local")
        self.assertEqual(catalog["main"]["alias"], "qwen3-coder-local")

    def test_existing_main_without_tools_is_replaced_by_tool_capable_model(self):
        catalog = {"main": {"alias": "cale-local"},
                   "fast": {"alias": "cale-fast", "auto": True}}
        tags = {"models": [
            {"name": "cale-local:latest", "size": 7 * 2**30},
            {"name": "cale-fast:latest", "size": 6 * 2**30},
        ]}
        created = []

        def post(path, body, **_kwargs):
            if path == "/api/create":
                created.append(body)
                return {"status": "success"}
            return {"capabilities": ([] if body["model"] == "cale-local" and not created
                                     else ["tools"]), "parameters": "num_ctx 65536\n"}

        with (patch.object(library, "CATALOG", catalog),
              patch.object(library, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch.object(library, "model_ready", return_value=True),
              patch.object(library, "alias_update_waits_for_tasks", return_value=False),
              patch("model_fitness.suitable", return_value=True),
              patch.object(library, "post", side_effect=post),
              patch("context_window.sync", return_value=65536)):
            self.assertTrue(library.repair_main_alias(32, 12))
        self.assertEqual(created[0]["model"], "cale-local")
        self.assertEqual(created[0]["from"], "cale-fast")

    def test_slow_main_is_kept_when_no_verified_replacement_exists(self):
        catalog = {"main": {"alias": "cale-local"},
                   "fast": {"alias": "cale-fast", "auto": True}}
        tags = {"models": [{"name": "cale-local:latest", "size": 50 * 2**30},
                            {"name": "cale-fast:latest", "size": 6 * 2**30}]}
        with (patch.object(library, "CATALOG", catalog),
              patch.object(library, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch("model_fitness.suitable", side_effect=lambda alias, *_: alias != "cale-local"),
              patch.object(library, "model_ready", return_value=False),
              patch.object(library, "alias_update_waits_for_tasks", return_value=False),
              patch.object(library, "post") as post):
            self.assertFalse(library.repair_main_alias(128, 12))
        post.assert_not_called()

    def test_main_standard_alias_shares_weights_without_shortening_full_alias(self):
        calls = []

        def fake_post(path, payload, **_kwargs):
            calls.append((path, payload))
            if path == "/api/create":
                return {}
            return {"capabilities": ["tools"], "parameters":
                    "num_ctx 65536\n" if payload["model"] == library.STANDARD_MAIN_ALIAS
                    else "num_ctx 262144\n"}

        with (patch("long_tasks.projects", return_value=[]),
              patch.object(library, "post", side_effect=fake_post),
              patch.object(library, "CATALOG", {"main": {"alias": "qwen3-coder-local"}})):
            self.assertTrue(library.ensure_main_standard())
        create = next(payload for path, payload in calls if path == "/api/create")
        self.assertEqual(create["from"], "qwen3-coder-local")
        self.assertEqual(create["model"], library.STANDARD_MAIN_ALIAS)
        self.assertEqual(create["parameters"]["num_ctx"], 65536)

    def test_main_standard_alias_waits_for_active_project(self):
        with (patch("long_tasks.projects", return_value=[{"status": "working"}]),
              patch.object(library, "post") as post):
            self.assertFalse(library.ensure_main_standard())
        post.assert_not_called()

    def test_agent_model_needs_64k_context(self):
        details = {"capabilities": ["tools"], "parameters": "num_ctx 32768\n"}
        with patch.object(library, "post", return_value=details):
            self.assertFalse(library.model_ready("qwen35"))
        details["parameters"] = "num_ctx 65536\n"
        with patch.object(library, "post", return_value=details):
            self.assertTrue(library.model_ready("qwen35"))

    def test_ready_model_uses_the_native_window_when_memory_allows(self):
        details = {
            "capabilities": ["tools"],
            "parameters": "num_ctx 65536\n",
            "model_info": {"qwen.context_length": 98304},
        }
        with (
            patch.object(library, "post", return_value=details),
            patch("context_window.model_bytes", return_value=7 * 2**30),
            patch("context_window.physical_ram_bytes", return_value=64 * 2**30),
            patch.object(library, "probe_tool_use", return_value=True),
        ):
            self.assertFalse(library.model_ready("qwen35"))
            details["parameters"] = "num_ctx 98304\n"
            self.assertTrue(library.model_ready("qwen35"))

    def test_fast_model_keeps_its_small_tested_window(self):
        self.assertEqual(library.CATALOG["fast"]["context_policy"], "fixed")
        details = {
            "capabilities": ["tools"],
            "parameters": "num_ctx 262144\n",
            "model_info": {"qwen.context_length": 262144},
        }
        with (
            patch.object(library, "post", return_value=details),
            patch.object(library, "candidate_approved", return_value=True),
        ):
            self.assertFalse(library.model_ready("fast"))
            details["parameters"] = "num_ctx 65536\n"
            self.assertTrue(library.model_ready("fast"))

    def test_recommendation_can_require_more_than_64k_context(self):
        document = {"schema": 1, "models": {"wide": {
            "label": "Wide model", "alias": "cale-wide-model", "source": "qwen3.6:35b",
            "origin": "https://ollama.com/library/qwen3.6", "license": "Apache-2.0",
            "download_gb": 23, "ram_gb": 48, "context": 131072, "auto": True,
        }}}
        with patch.object(library, "CATALOG", {"main": {"alias": "base"}}):
            self.assertEqual(
                library.validated_recommendations(document)["wide"]["context"], 131072
            )

    def test_small_derestricted_model_is_auto_candidate_after_tool_probe(self):
        model = library.CATALOG["permissive_small"]
        self.assertEqual(model["ram_gb"], 16)
        self.assertTrue(model["candidate"])
        self.assertTrue(model["fallback_only"])
        self.assertTrue(model.get("auto", True))
        with tempfile.TemporaryDirectory() as folder, patch.object(library, "ROOT", Path(folder)):
            self.assertFalse(library.candidate_approved(model))
            library.approve_candidate(model)
            self.assertTrue(library.candidate_approved(model))

    def test_non_object_approvals_fail_closed_and_can_be_repaired(self):
        model = {"alias": "cale-test-model", "source": "test:latest", "candidate": True}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "LongTasks/model-approvals.json"
            path.parent.mkdir()
            with patch.object(library, "ROOT", root):
                for value in (None, [], ["unexpected"], "unexpected", 42, True):
                    with self.subTest(value=value):
                        path.write_text(json.dumps(value), encoding="utf-8")
                        self.assertFalse(library.candidate_approved(model))
                        library.approve_candidate(model)
                        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                                         {model["alias"]: model["source"]})
                        self.assertTrue(library.candidate_approved(model))

    def test_recommendation_cannot_replace_bundled_candidate(self):
        document = {
            "schema": 1,
            "models": {"permissive_small": {
                "label": "Unexpected replacement", "alias": "other-model",
                "source": "qwen3.5:9b", "origin": "https://ollama.com/library/qwen3.5",
                "license": "Apache-2.0", "download_gb": 7,
                "ram_gb": 16, "context": 32768,
            }},
        }
        self.assertNotIn("permissive_small", library.validated_recommendations(document))

    def test_recommendation_cannot_reuse_a_working_alias(self):
        document = {
            "schema": 1,
            "models": {
                "first_new": {
                    "label": "New model", "alias": "cale-qwen35",
                    "source": "qwen3.6:35b", "origin": "https://ollama.com/library/qwen3.6",
                    "license": "Apache-2.0", "download_gb": 23, "ram_gb": 48,
                    "context": 65536, "auto": True,
                },
                "second_new": {
                    "label": "Another model", "alias": "cale-shared-new",
                    "source": "qwen3.6:35b", "origin": "https://ollama.com/library/qwen3.6",
                    "license": "Apache-2.0", "download_gb": 23, "ram_gb": 48,
                    "context": 65536, "auto": True,
                },
                "third_new": {
                    "label": "Duplicate alias", "alias": "cale-shared-new",
                    "source": "qwen3.5:35b", "origin": "https://ollama.com/library/qwen3.5",
                    "license": "Apache-2.0", "download_gb": 22, "ram_gb": 48,
                    "context": 65536, "auto": True,
                },
            },
        }
        accepted = library.validated_recommendations(document)
        self.assertEqual(list(accepted), ["second_new"])

    def test_existing_feed_profile_can_refresh_metadata_without_restarting(self):
        old = {"alias": "cale-feed-model", "source": "qwen3.5:35b"}
        document = {"schema": 1, "models": {"feed_model": {
            "label": "Updated description", "alias": "cale-feed-model",
            "source": "qwen3.5:35b", "origin": "https://ollama.com/library/qwen3.5",
            "license": "Apache-2.0", "download_gb": 22, "ram_gb": 48,
            "context": 65536, "auto": True,
        }}}
        with patch.object(library, "CATALOG", {"main": {"alias": "base"}, "feed_model": old}):
            accepted = library.validated_recommendations(document)
            library.apply_recommendations(accepted)
            self.assertEqual(library.CATALOG["feed_model"]["label"], "Updated description")
            self.assertEqual(library.CATALOG["main"]["alias"], "base")

    def test_new_weights_cannot_replace_an_approved_profile_in_place(self):
        old = {"alias": "cale-feed-model", "source": "qwen3.5:35b"}
        document = {"schema": 1, "models": {"feed_model": {
            "label": "Newer weights", "alias": "cale-feed-model",
            "source": "qwen3.6:35b", "origin": "https://ollama.com/library/qwen3.6",
            "license": "Apache-2.0", "download_gb": 23, "ram_gb": 48,
            "context": 65536, "auto": True,
        }}}
        with patch.object(library, "CATALOG", {"main": {"alias": "base"}, "feed_model": old}):
            with self.assertRaisesRegex(ValueError, "changed incompatibly"):
                library.validated_recommendations(document)
            self.assertEqual(library.CATALOG["feed_model"]["source"], "qwen3.5:35b")

    def test_explicitly_empty_feed_retires_old_recommendations(self):
        document = {"schema": 1, "models": {}}
        catalog = {
            "main": {"alias": "base"},
            "feed_model": {"alias": "cale-feed-model", "source": "qwen3.5:35b"},
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "ROOT", Path(folder)),
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "urlopen", return_value=io.BytesIO(json.dumps(document).encode())),
        ):
            self.assertEqual(library.refresh_catalog(), {})
            self.assertNotIn("feed_model", catalog)
            self.assertIn("main", catalog)

    def test_bad_feed_update_keeps_last_cached_working_profile(self):
        old_model = {"alias": "cale-feed-model", "source": "qwen3.5:35b"}
        old_feed = {"schema": 1, "models": {"feed_model": {
            "label": "Working", "alias": "cale-feed-model",
            "source": "qwen3.5:35b", "origin": "https://ollama.com/library/qwen3.5",
            "license": "Apache-2.0", "download_gb": 22, "ram_gb": 48,
            "context": 65536, "auto": True,
        }}}
        bad_feed = json.loads(json.dumps(old_feed))
        bad_feed["models"]["feed_model"]["source"] = "qwen3.6:35b"
        bad_feed["models"]["feed_model"]["origin"] = "https://ollama.com/library/qwen3.6"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / "LongTasks/model-recommendations.json"
            cache.parent.mkdir()
            cache.write_text(json.dumps(old_feed))
            catalog = {"main": {"alias": "base"}, "feed_model": old_model}
            with (
                patch.object(library, "ROOT", root),
                patch.object(library, "CATALOG", catalog),
                patch.object(library, "urlopen", return_value=io.BytesIO(json.dumps(bad_feed).encode())),
            ):
                library.refresh_catalog()
            self.assertEqual(catalog["feed_model"]["source"], "qwen3.5:35b")
            self.assertEqual(json.loads(cache.read_text()), old_feed)

    def test_failed_source_probe_preserves_existing_alias(self):
        model = {
            "label": "New", "alias": "cale-existing", "source": "qwen3.6:35b",
            "download_gb": 23, "ram_gb": 24, "context": 65536,
            "candidate": True, "tool_reasoning_effort": "none",
        }
        with (
            patch.object(library, "CATALOG", {"new": model}),
            patch.object(library, "total_ram_gb", return_value=64),
            patch.object(library, "urlopen", return_value=io.BytesIO(b'{"status":"success"}\n')),
            patch.object(library, "post", return_value={"capabilities": ["tools"]}) as post,
            patch.object(library, "probe_tool_use", side_effect=RuntimeError("bad tool call")) as probe,
            patch.object(library, "approve_candidate") as approve,
        ):
            with self.assertRaisesRegex(RuntimeError, "bad tool call"):
                library.install("new", lambda _message: None)
        probe.assert_called_once_with("qwen3.6:35b", "none")
        self.assertEqual(post.call_args.args[0], "/api/show")
        self.assertEqual(post.call_args.args[1], {"model": "qwen3.6:35b"})
        approve.assert_not_called()

    def test_successful_candidate_probes_source_before_alias_creation(self):
        model = {
            "label": "New", "alias": "cale-new-model", "source": "qwen3.6:35b",
            "download_gb": 23, "ram_gb": 24, "context": 65536,
            "candidate": True,
        }
        calls = []

        def fake_post(path, payload, **_kwargs):
            calls.append((path, payload["model"]))
            return {"capabilities": ["tools"]}

        def fake_probe(model_name, _reasoning):
            calls.append(("probe", model_name))

        with (
            patch.object(library, "CATALOG", {"new": model}),
            patch.object(library, "total_ram_gb", return_value=64),
            patch.object(library, "urlopen", return_value=io.BytesIO(b'{"status":"success"}\n')),
            patch.object(library, "post", side_effect=fake_post),
            patch.object(library, "probe_tool_use", side_effect=fake_probe),
            patch.object(library, "approve_candidate") as approve,
        ):
            self.assertEqual(library.install("new", lambda _message: None), "cale-new-model")
        self.assertEqual(calls, [
            ("/api/show", "qwen3.6:35b"),
            ("probe", "qwen3.6:35b"),
            ("/api/create", "cale-new-model"),
            ("/api/show", "cale-new-model"),
        ])
        approve.assert_called_once_with(model)

    def test_install_creates_optional_alias_with_memory_aware_native_context(self):
        model = {
            "label": "Wide", "alias": "cale-wide", "source": "qwen3.6:35b",
            "download_gb": 23, "ram_gb": 48, "context": 65536,
        }
        calls = []

        def fake_post(path, payload, **_kwargs):
            calls.append((path, payload))
            return {"capabilities": ["tools"], "model_info": {"qwen.context_length": 98304}}

        with (
            patch.object(library, "CATALOG", {"wide": model}),
            patch.object(library, "total_ram_gb", return_value=64),
            patch.object(library, "urlopen", return_value=io.BytesIO(b'{"status":"success"}\n')),
            patch.object(library, "post", side_effect=fake_post),
            patch("context_window.model_bytes", return_value=23 * 2**30),
            patch("context_window.physical_ram_bytes", return_value=64 * 2**30),
            patch.object(library, "probe_tool_use", return_value=True),
        ):
            self.assertEqual(library.install("wide", lambda _message: None), "cale-wide")
        create = next(payload for path, payload in calls if path == "/api/create")
        self.assertEqual(create["parameters"]["num_ctx"], 98304)

    def test_thinking_model_probe_reaches_the_tool_call(self):
        response = {
            "message": {
                "tool_calls": [
                    {"function": {"name": "multiply", "arguments": {"a": 7, "b": 13}}}
                ]
            }, "eval_count": 64, "eval_duration": 2_000_000_000,
        }
        with patch.object(library, "post", return_value=response) as post:
            self.assertTrue(library.probe_tool_use("cale-qwen36", "none"))
        request = post.call_args.args[1]
        self.assertIs(request["think"], False)
        self.assertEqual(request["options"]["num_predict"], 512)

    def test_slow_tool_capable_model_fails_before_alias_creation(self):
        response = {"message": {"tool_calls": [
            {"function": {"name": "multiply", "arguments": {"a": 7, "b": 13}}}
        ]}, "eval_count": 64, "eval_duration": 32_000_000_000}
        with patch.object(library, "post", return_value=response) as post:
            with self.assertRaisesRegex(library.SlowModelError, "2.0 tokens/s"):
                library.probe_tool_use("slow-source")
        post.assert_called_once()

    def test_short_tool_call_gets_generation_speed_probe(self):
        tool = {"message": {"tool_calls": [
            {"function": {"name": "multiply", "arguments": {"a": 7, "b": 13}}}
        ]}, "eval_count": 8, "eval_duration": 1_000_000_000}
        generation = {"eval_count": 80, "eval_duration": 4_000_000_000}
        with patch.object(library, "post", side_effect=[tool, generation]) as post:
            self.assertTrue(library.probe_tool_use("fast-source"))
        self.assertEqual(post.call_args_list[1].args[0], "/api/generate")

    def test_slow_probe_is_not_retried_on_every_startup(self):
        catalog = {"main": {"alias": "base"},
                   "slow": {"alias": "cale-slow", "source": "slow:latest",
                            "label": "Slow", "download_gb": 7, "ram_gb": 24}}
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(library, "CATALOG", catalog),
              patch.object(library, "total_ram_gb", return_value=32),
              patch("model_fitness.gpu_vram_gb", return_value=12),
              patch("model_fitness.measured_speed", return_value=None),
              patch.object(library, "model_ready", return_value=False),
              patch.object(library, "source_cached", return_value=True),
              patch.object(library, "ensure_main_standard", return_value=False),
              patch.object(library, "repair_main_alias", return_value=False),
              patch.object(library, "cleanup_retired_main_sources"),
              patch.object(library.shutil, "disk_usage",
                           return_value=SimpleNamespace(free=40 * 2**30)),
              patch.object(library, "install", side_effect=library.SlowModelError(
                  "Model generated 2.0 tokens/s on this PC")) as install):
            library.auto_install(folder, refresh=False)
            library.auto_install(folder, refresh=False)
            state = json.loads((Path(folder) / "optional-model-slow.json").read_text())
        self.assertEqual(state["status"], "skipped_slow")
        install.assert_called_once()

    def test_recommendation_can_specify_tool_reasoning(self):
        candidate = json.loads((ROOT / "models/recommendations.json").read_text())
        with patch.object(library, "CATALOG", {"main": {"alias": "base"}}):
            accepted = library.validated_recommendations(candidate)
        self.assertEqual(accepted["qwen36"]["tool_reasoning_effort"], "none")

    def test_only_models_that_fit_ram_and_disk_are_downloaded(self):
        catalog = {
            "main": {"alias": "base"},
            "small": {"label": "Small", "download_gb": 8, "ram_gb": 24},
            "large": {"label": "Large", "download_gb": 50, "ram_gb": 96},
        }
        calls = []

        def fake_install(profile, progress):
            calls.append(profile)
            progress("downloading 8.0 / 8.0 GB")

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=32),
            patch.object(library, "model_ready", side_effect=[False, True]),
            patch.object(
                library.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=40 * 2**30),
            ),
            patch.object(library, "install", side_effect=fake_install),
        ):
            library.auto_install(folder, refresh=False)
            library.auto_install(folder, refresh=False)
            state = json.loads((Path(folder) / "optional-model-small.json").read_text())
            self.assertEqual(state["status"], "complete")
            worker = json.loads((Path(folder) / "auto-model-worker.json").read_text())
            self.assertEqual(worker["catalog"], library.catalog_signature())
            self.assertEqual(calls, ["small"])
            self.assertFalse((Path(folder) / "optional-model-large.json").exists())

    def test_low_disk_defers_download(self):
        catalog = {
            "main": {"alias": "base"},
            "small": {"label": "Small", "download_gb": 8, "ram_gb": 24},
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=32),
            patch.object(library, "model_ready", return_value=False),
            patch.object(
                library.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=10 * 2**30),
            ),
            patch.object(library, "install") as install,
        ):
            library.auto_install(folder, refresh=False)
            state = json.loads((Path(folder) / "optional-model-small.json").read_text())
            self.assertEqual(state["status"], "waiting_storage")
            install.assert_not_called()

    def test_cached_weights_can_receive_wider_alias_without_full_download_space(self):
        catalog = {
            "main": {"alias": "base"},
            "wide": {"label": "Wide", "alias": "cale-wide", "source": "qwen3.6:35b",
                     "download_gb": 23, "ram_gb": 48},
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=64),
            patch.object(library, "model_ready", return_value=False),
            patch.object(library, "source_cached", return_value=True),
            patch.object(library.shutil, "disk_usage", return_value=SimpleNamespace(free=3 * 2**30)),
            patch.object(library, "install") as install,
        ):
            library.auto_install(folder, refresh=False)
            install.assert_called_once()

    def test_existing_alias_is_not_rebuilt_during_an_active_task(self):
        catalog = {
            "main": {"alias": "base"},
            "wide": {"label": "Wide", "alias": "cale-wide", "source": "qwen3.6:35b",
                     "download_gb": 23, "ram_gb": 48},
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=64),
            patch.object(library, "model_ready", return_value=False),
            patch.object(library, "alias_update_waits_for_tasks", return_value=True),
            patch.object(library, "install") as install,
        ):
            library.auto_install(folder, refresh=False)
            install.assert_not_called()
            status = json.loads((Path(folder) / "optional-model-wide.json").read_text())
            self.assertEqual(status["status"], "waiting_tasks")

    def test_existing_alias_detects_active_project_before_rebuild(self):
        item = {"alias": "cale-wide"}
        with patch.object(library, "post", return_value={}), patch(
            "long_tasks.projects", return_value=[{"status": "working"}]
        ):
            self.assertTrue(library.alias_update_waits_for_tasks(item))
        with patch.object(library, "post", return_value={}), patch(
            "long_tasks.projects", return_value=[{"status": "ready_for_review"}]
        ):
            self.assertFalse(library.alias_update_waits_for_tasks(item))

    def test_absent_optional_alias_waits_while_a_song_uses_the_gpu(self):
        catalog = {
            "main": {"alias": "cale-local"},
            "new": {"label": "New", "alias": "cale-new", "source": "new:latest",
                    "download_gb": 7, "ram_gb": 24},
        }
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(library, "CATALOG", catalog),
              patch.object(library, "total_ram_gb", return_value=32),
              patch("model_fitness.gpu_vram_gb", return_value=12),
              patch.object(library, "model_ready", return_value=False),
              patch("long_tasks.projects", return_value=[{"status": "working"}]),
              patch.object(library, "install") as install,
              patch.object(library, "repair_main_alias") as repair):
            library.auto_install(folder, refresh=False)
            state = json.loads((Path(folder) / "optional-model-new.json").read_text())
        self.assertEqual(state["status"], "waiting_tasks")
        install.assert_not_called()
        repair.assert_not_called()

    def test_stale_complete_status_is_reinstalled(self):
        catalog = {
            "main": {"alias": "base"},
            "small": {
                "label": "Small",
                "alias": "small-local",
                "download_gb": 8,
                "ram_gb": 24,
            },
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=32),
            patch.object(library, "model_ready", return_value=False),
            patch.object(library, "install") as install,
            patch.object(
                library.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=40 * 2**30),
            ),
        ):
            status = Path(folder) / "optional-model-small.json"
            library.save_download_status(status, "complete", "Old result", 100)
            library.auto_install(folder, refresh=False)
            install.assert_called_once()
            self.assertEqual(json.loads(status.read_text())["status"], "complete")

    def test_existing_model_needs_no_download_or_extra_space(self):
        catalog = {
            "main": {"alias": "base"},
            "small": {
                "label": "Small",
                "alias": "small-local",
                "download_gb": 8,
                "ram_gb": 24,
            },
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=32),
            patch.object(library, "model_ready", return_value=True),
            patch.object(library, "install") as install,
            patch.object(library.shutil, "disk_usage") as disk_usage,
        ):
            library.auto_install(folder, refresh=False)
            install.assert_not_called()
            disk_usage.assert_not_called()
            self.assertEqual(
                json.loads((Path(folder) / "optional-model-small.json").read_text())[
                    "status"
                ],
                "complete",
            )

    def test_download_status_creates_missing_ancestor_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "missing" / "nested" / "download.json"
            with patch.object(library.time, "time", return_value=1234):
                library.save_download_status(
                    status, "working", "Downloading model", 25,
                    {"source": "example:latest"},
                )
            self.assertEqual(json.loads(status.read_text(encoding="utf-8")), {
                "status": "working",
                "current": "Downloading model",
                "percent": 25,
                "updated": 1234,
                "source": "example:latest",
            })
            self.assertEqual(list(status.parent.iterdir()), [status])

    def test_custom_ollama_store_controls_disk_check(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(library.os.environ, {"OLLAMA_MODELS": folder}):
                self.assertEqual(library.model_store_path(), Path(folder))

    def test_manual_experimental_model_is_not_auto_downloaded(self):
        catalog = {
            "main": {"alias": "base"},
            "experimental": {
                "label": "Experimental",
                "alias": "experimental-local",
                "download_gb": 8,
                "ram_gb": 24,
                "auto": False,
            },
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=128),
            patch.object(library, "install") as install,
        ):
            library.auto_install(folder, refresh=False)
            install.assert_not_called()
            self.assertFalse(
                (Path(folder) / "optional-model-experimental.json").exists()
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
