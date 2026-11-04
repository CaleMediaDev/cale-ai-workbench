"""Persistent, project-scoped memory and full-text indexing behavior."""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import project_memory as memory


def test_legacy_extracted_hits_are_invalidated_lazily_without_scanning_or_erasing_notes(tmp_path):
    import pytest

    for operation in ("get", "search"):
        root = tmp_path / operation
        root.mkdir()
        source = root / "old.pdf"
        other = root / "unrelated.pdf"
        source.write_bytes(b"original bytes")
        other.write_bytes(b"unrelated bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        memory.index_document_text(root, source, "stale zebra evidence", digest, revision="v2-vision0")
        memory.index_document_text(root, other, "unrelated evidence",
                                   hashlib.sha256(other.read_bytes()).hexdigest(), revision="v2-vision1")
        note = memory.save(root, "note", "User note", "User-authored content stays", source_path="old.pdf")
        text = root / "keep.txt"
        text.write_text("Original plain file", encoding="utf-8")
        memory.index_file(root, text)
        # Even a concurrently rebuilt current-version row for this same source
        # is preserved: invalidation only owns known legacy derived records.
        with memory.connection(root) as conn:
            memory._put(conn, "document:old.pdf:current", "document", "Current extraction",
                        "current verified content", "extractor:v3-vision0", "old.pdf", digest)
        with patch("file_chat.document_text", side_effect=AssertionError("Memory lookup must not extract")), \
             patch.object(Path, "rglob", side_effect=AssertionError("Memory lookup must not scan")):
            if operation == "get":
                with pytest.raises(ValueError, match="inspect_project_file"):
                    memory.get(root, "document:old.pdf:1")
            else:
                assert memory.search(root, "stale zebra", kind="document") == []
        with memory.connection(root) as conn:
            identifiers = {row[0] for row in conn.execute("SELECT id FROM memories")}
            fts = {row[0] for row in conn.execute("SELECT id FROM memory_fts")}
        assert "document:old.pdf:1" not in identifiers | fts
        assert {note["id"], "file:keep.txt", "document:unrelated.pdf:1", "document:old.pdf:current"} <= identifiers
        assert identifiers == fts
        assert memory.get(root, note["id"])["body"] == "User-authored content stays"
        assert memory.get(root, "document:old.pdf:current")["body"] == "current verified content"


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "project"
        self.folder.mkdir()

    def test_unreadable_project_file_does_not_abort_code_index_or_leak_stale_text(self):
        busy = self.folder / "a.py"
        kept = self.folder / "b.py"
        busy.write_text("old_private_phrase", encoding="utf-8")
        kept.write_text("useful_component", encoding="utf-8")
        memory.index(self.folder, include_code=True)
        original_read = Path.read_bytes

        def read(path):
            if path == busy:
                raise PermissionError("editor is replacing this file")
            return original_read(path)

        with patch.object(Path, "read_bytes", read):
            self.assertEqual(memory.search(self.folder, "old_private_phrase"), [])
            result = memory.index(self.folder, include_code=True)
            self.assertEqual(result["skipped"], 1)
        self.assertEqual(memory.search(self.folder, "useful_component")[0]["source_path"],
                         "b.py")

    def test_unresolvable_recalled_file_is_evicted_without_aborting_search(self):
        source = self.folder / "app.py"
        source.write_text("loopmarker obsolete evidence", encoding="utf-8")
        memory.index_file(self.folder, source)
        kept = memory.save(self.folder, "note", "Kept note", "loopmarker valid evidence")
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == source:
                raise RuntimeError("Symlink loop")
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            found = memory.search(self.folder, "loopmarker")

        self.assertEqual([row["id"] for row in found], [kept["id"]])
        with memory.connection(self.folder) as conn:
            for table in ("memories", "memory_fts"):
                self.assertEqual(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE id=?",
                    ("file:app.py",),
                ).fetchone()[0], 0)

    def test_unresolvable_document_source_is_evicted_without_aborting_recall(self):
        source = self.folder / "manual.pdf"
        source.write_bytes(b"original document bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        kept = memory.save(self.folder, "note", "Kept note", "loopmarker valid evidence")
        identifier = "document:manual.pdf:1"
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == source:
                raise RuntimeError("Symlink loop")
            return original_resolve(path, *args, **kwargs)

        for operation in ("get", "search"):
            with self.subTest(operation=operation):
                memory.index_document_text(
                    self.folder, source, "loopmarker obsolete evidence", digest,
                    revision="v3-vision0",
                )
                with patch.object(Path, "resolve", resolve):
                    if operation == "get":
                        with self.assertRaisesRegex(ValueError, "source file was removed"):
                            memory.get(self.folder, identifier)
                    else:
                        found = memory.search(self.folder, "loopmarker")
                        self.assertEqual([row["id"] for row in found], [kept["id"]])
                with memory.connection(self.folder) as conn:
                    for table in ("memories", "memory_fts"):
                        self.assertEqual(conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE id=?",
                            (identifier,),
                        ).fetchone()[0], 0)
                self.assertEqual(memory.get(self.folder, kept["id"])["body"],
                                 "loopmarker valid evidence")

    def test_second_edit_during_search_hides_old_project_memory(self):
        source = self.folder / "app.py"
        source.write_text("old_widget", encoding="utf-8")
        memory.index(self.folder, include_code=True)
        source.write_text("new_widget", encoding="utf-8")
        with patch.object(memory, "index_file", side_effect=FileNotFoundError):
            self.assertEqual(memory.search(self.folder, "old_widget"), [])

    def test_database_creates_automatically_and_persists_typed_memory(self):
        path = Path(memory.initialize(self.folder))
        self.assertTrue(path.is_file())
        saved = memory.save(
            self.folder, "skill", "Ship a desktop app",
            "Build, run behavior tests, package, and record the release.",
            tags="release tests",
        )
        self.assertEqual(memory.get(self.folder, saved["id"])["body"],
                         "Build, run behavior tests, package, and record the release.")
        self.assertEqual(memory.search(self.folder, "release", kind="skill")[0]["id"], saved["id"])
        memory.save(self.folder, "skill", "Ship an app", "Build, test, and package.",
                    memory_id=saved["id"])
        self.assertEqual(memory.get(self.folder, saved["id"])["title"], "Ship an app")
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_file_index_refreshes_content_and_removes_deleted_files(self):
        source = self.folder / "README.md"
        source.write_text("The app uses offline recipe search.", encoding="utf-8")
        self.assertEqual(memory.index(self.folder)["indexed"], 1)
        self.assertEqual(memory.index(self.folder)["unchanged"], 1)
        self.assertEqual(memory.search(self.folder, "recipe")[0]["source_path"], "README.md")
        old = source.stat()
        source.write_text("The app uses offline budget search.", encoding="utf-8")
        os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertTrue(memory.search(self.folder, "budget"))
        self.assertFalse(memory.search(self.folder, "recipe"))
        source.unlink()
        self.assertFalse(memory.search(self.folder, "budget"))

    def test_incremental_index_removes_defaults_that_become_invalid(self):
        source = self.folder / "README.md"
        other = self.folder / "app.py"
        other.write_text("preserved code record", encoding="utf-8")
        memory.index_file(self.folder, other)

        for invalid_data in (b"binary\x00content", b"x" * 256_001):
            with self.subTest(size=len(invalid_data)):
                source.write_text("obsolete searchable passage", encoding="utf-8")
                memory.index(self.folder)
                source.write_bytes(invalid_data)

                result = memory.index(self.folder)
                self.assertEqual(result["removed"], 1)
                # Check storage before search can lazily remove stale hits.
                with memory.connection(self.folder) as conn:
                    for table in ("memories", "memory_fts"):
                        self.assertEqual(conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE id=?",
                            ("file:README.md",),
                        ).fetchone()[0], 0)
                        self.assertEqual(conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE id=?",
                            ("file:app.py",),
                        ).fetchone()[0], 1)

    def test_incremental_alias_invalidation_uses_canonical_id_for_both_memory_tables(self):
        source = self.folder / "notes.md"
        alias = self.folder / "README.md"
        alias.write_text("alias placeholder", encoding="utf-8")
        other = self.folder / "app.py"
        other.write_text("preserved source record", encoding="utf-8")
        memory.index_file(self.folder, other)
        resolve, read_bytes = Path.resolve, Path.read_bytes
        def canonical(path, *args, **kwargs):
            return resolve(source, *args, **kwargs) if path == alias else resolve(path, *args, **kwargs)
        def read(path):
            return read_bytes(source) if path == alias else read_bytes(path)
        with patch.object(Path, "resolve", canonical), patch.object(Path, "read_bytes", read):
            for invalid_data in (b"binary\x00content", b"x" * 256_001):
                with self.subTest(size=len(invalid_data)):
                    source.write_text("obsolete canonical alias passage", encoding="utf-8")
                    memory.index(self.folder)
                    with memory.connection(self.folder) as conn:
                        self.assertEqual(conn.execute("SELECT source_path FROM memories WHERE id='file:notes.md'").fetchone()[0], "notes.md")
                    source.write_bytes(invalid_data)
                    result = memory.index(self.folder)
                    self.assertEqual(result["removed"], 1)
                    # Inspect raw storage before recall can lazily remove stale content.
                    with memory.connection(self.folder) as conn:
                        for table in ("memories", "memory_fts"):
                            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE id='file:notes.md'").fetchone()[0], 0)
                            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE id='file:app.py'").fetchone()[0], 1)

    def test_incremental_inspected_alias_skips_external_and_unresolvable_targets(self):
        other = self.folder / "app.py"
        other.write_text("preserve uninspected source", encoding="utf-8")
        memory.index_file(self.folder, other)
        alias = self.folder / "README.md"
        alias.write_text("alias placeholder", encoding="utf-8")
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("outside private text", encoding="utf-8")
        resolve = Path.resolve
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                def canonical(path, *args, **kwargs):
                    if path == alias:
                        if unavailable:
                            raise OSError("Alias target cannot be resolved")
                        return resolve(outside, *args, **kwargs)
                    return resolve(path, *args, **kwargs)
                with patch.object(Path, "resolve", canonical):
                    result = memory.index(self.folder)
                self.assertEqual(result["removed"], 0)
                with memory.connection(self.folder) as conn:
                    self.assertEqual([row[0] for row in conn.execute("SELECT id FROM memories WHERE kind='file'")], ["file:app.py"])
                    self.assertEqual([row[0] for row in conn.execute("SELECT id FROM memory_fts")], ["file:app.py"])

    def test_save_cannot_overwrite_an_indexed_document_chunk(self):
        source = self.folder / "manual.pdf"
        source.write_bytes(b"document bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        memory.index_document_text(self.folder, source, "distinctive zebra passage", digest)
        identifier = "document:manual.pdf:1"
        original = memory.get(self.folder, identifier)

        with self.assertRaisesRegex(ValueError, "Only an existing saved memory"):
            memory.save(self.folder, "note", "Replacement", "Unrelated content",
                        memory_id=identifier)

        self.assertEqual(memory.get(self.folder, identifier), original)
        found = memory.search(self.folder, "distinctive zebra", kind="document")
        self.assertEqual([row["id"] for row in found], [identifier])

    def test_document_chunks_are_searchable_and_source_checked(self):
        source = self.folder / "manual.pdf"
        source.write_bytes(b"first document bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        text = "alpha " * 3000 + "distinctive zebra passage " + "omega " * 3000
        result = memory.index_document_text(self.folder, source, text, digest)
        self.assertGreater(result["parts"], 1)
        self.assertFalse(memory.index_document_text(
            self.folder, source, text, digest)["updated"])
        found = memory.search(self.folder, "distinctive zebra", kind="document")
        self.assertEqual(found[0]["source_path"], "manual.pdf")
        self.assertIn("distinctive zebra", memory.get(self.folder, found[0]["id"])["body"])
        self.assertTrue(memory.index_document_text(
            self.folder, source, "revised extractor text", digest, revision="v2"
        )["updated"])
        self.assertFalse(memory.search(self.folder, "distinctive zebra", kind="document"))
        revised = memory.search(self.folder, "revised extractor", kind="document")
        self.assertEqual(revised[0]["source_path"], "manual.pdf")
        source.unlink()
        with self.assertRaisesRegex(ValueError, "source file was removed"):
            memory.get(self.folder, revised[0]["id"])
        self.assertFalse(memory.search(self.folder, "revised extractor", kind="document"))

    def test_code_scan_is_bounded_and_excludes_secrets(self):
        (self.folder / "app.py").write_text("def make_invoice(): return 42", encoding="utf-8")
        (self.folder / ".env").write_text("SECRET_TOKEN=private", encoding="utf-8")
        (self.folder / "credentials.json").write_text('{"token":"private"}', encoding="utf-8")
        (self.folder / "node_modules").mkdir()
        (self.folder / "node_modules" / "module.js").write_text("hidden_package_marker", encoding="utf-8")
        result = memory.index(self.folder, include_code=True)
        self.assertEqual(result["indexed"], 1)
        self.assertTrue(memory.search(self.folder, "invoice", kind="file"))
        self.assertFalse(memory.search(self.folder, "private"))
        self.assertFalse(memory.search(self.folder, "hidden_package_marker"))

    def test_alias_eligibility_checks_resolved_filename_without_symlink_privilege(self):
        source = self.folder / "credentials.json"
        source.write_text('{"value":"excludedaliasmarker"}', encoding="utf-8")
        alias = self.folder / "ordinary.md"
        root = self.folder.resolve()
        with patch.object(Path, "resolve", return_value=source):
            self.assertFalse(memory._eligible(root, alias))

    def test_code_scan_rejects_symlink_to_excluded_filename(self):
        source = self.folder / "credentials.json"
        source.write_text('{"value":"excludedaliasmarker"}', encoding="utf-8")
        alias = self.folder / "ordinary.md"
        try:
            alias.symlink_to(source)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        result = memory.index(self.folder, include_code=True)
        self.assertEqual(result["indexed"], 0)
        self.assertEqual(memory.search(self.folder, "excludedaliasmarker"), [])
        with self.assertRaises(ValueError):
            memory.index_file(self.folder, alias)

    def test_project_scan_and_explicit_index_reject_nested_secret_folder(self):
        private = self.folder / "secrets"
        private.mkdir()
        source = private / "ordinary-notes.md"
        source.write_text("hidden-project-key", encoding="utf-8")
        memory.index(self.folder, include_code=True)
        self.assertFalse(memory.search(self.folder, "hidden-project-key"))
        with self.assertRaises(ValueError):
            memory.index_file(self.folder, source)

    def test_recalled_code_file_refreshes_after_edit_and_deletion(self):
        source = self.folder / "app.py"
        source.write_text("def old_recipe(): return 1", encoding="utf-8")
        memory.index(self.folder, include_code=True)
        identifier = "file:app.py"
        self.assertTrue(memory.search(self.folder, "old_recipe", kind="file"))
        before = source.stat()
        source.write_text("def new_budget(): return 2", encoding="utf-8")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertIn("new_budget", memory.get(self.folder, identifier)["body"])
        self.assertFalse(memory.search(self.folder, "old_recipe", kind="file"))
        self.assertTrue(memory.search(self.folder, "new_budget", kind="file"))
        source.write_text("def fresh_invoice(): return 3", encoding="utf-8")
        self.assertFalse(memory.search(self.folder, "new_budget", kind="file"))
        self.assertIn("fresh_invoice", memory.get(self.folder, identifier)["body"])
        source.unlink()
        with self.assertRaisesRegex(ValueError, "source file was removed"):
            memory.get(self.folder, identifier)
        self.assertFalse(memory.search(self.folder, "fresh_invoice", kind="file"))

    def test_code_scan_uses_canonical_identity_for_symlink_aliases(self):
        source = self.folder / "implementation.py"
        source.write_text("originalmarker", encoding="utf-8")
        alias = self.folder / "alias.py"
        try:
            alias.symlink_to(source)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        memory.index(self.folder, include_code=True)
        found = memory.search(self.folder, "originalmarker", kind="file")
        self.assertEqual([row["id"] for row in found], ["file:implementation.py"])
        self.assertEqual(found[0]["source_path"], "implementation.py")

        alias.write_text("replacementmarker", encoding="utf-8")
        self.assertEqual(memory.get(self.folder, found[0]["id"])["body"],
                         "replacementmarker")
        self.assertEqual(memory.search(self.folder, "originalmarker", kind="file"), [])
        refreshed = memory.search(self.folder, "replacementmarker", kind="file")
        self.assertEqual([row["id"] for row in refreshed], ["file:implementation.py"])
        with memory.connection(self.folder) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE kind='file'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM memory_fts"
            ).fetchone()[0], 1)

    def test_recalled_file_replaced_by_alias_evicts_obsolete_identity(self):
        source = self.folder / "app.py"
        target = self.folder / "implementation.py"
        identifier = "file:app.py"
        for replacement in ("originalmarker", "replacementmarker"):
            with self.subTest(replacement=replacement):
                source.write_text("originalmarker", encoding="utf-8")
                memory.index_file(self.folder, source)
                target.write_text(replacement, encoding="utf-8")
                source.unlink()
                try:
                    source.symlink_to(target)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"Symlinks unavailable: {exc}")
                try:
                    with self.assertRaisesRegex(ValueError, "source file was removed"):
                        memory.get(self.folder, identifier)
                    with memory.connection(self.folder) as conn:
                        for table in ("memories", "memory_fts"):
                            self.assertEqual(conn.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE id=?",
                                (identifier,),
                            ).fetchone()[0], 0)
                    memory.index_file(self.folder, source)
                    found = memory.search(self.folder, replacement, kind="file")
                    self.assertEqual([row["id"] for row in found],
                                     ["file:implementation.py"])
                finally:
                    source.unlink()

    def test_old_identifier_does_not_match_new_identifier_by_shared_word(self):
        source = self.folder / "app.py"
        source.write_text("def old_widget(): return 1", encoding="utf-8")
        memory.index(self.folder, include_code=True)
        source.write_text("def new_widget(): return 2", encoding="utf-8")
        self.assertFalse(memory.search(self.folder, "old_widget", kind="file"))
        self.assertTrue(memory.search(self.folder, "new_widget", kind="file"))

    def test_memory_is_isolated_and_source_cannot_escape_project(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        (other / "outside.txt").write_text("outside", encoding="utf-8")
        saved = memory.save(self.folder, "decision", "Use SQLite", "Keep fast lookup local.")
        self.assertEqual(memory.search(other, "SQLite"), [])
        with self.assertRaises(ValueError):
            memory.get(other, saved["id"])
        with self.assertRaises(ValueError):
            memory.save(self.folder, "note", "Bad source", "No", source_path=str(other / "outside.txt"))
        with self.assertRaises(ValueError):
            memory.save(self.folder, "file", "Fake", "No")

    def test_search_preserves_unicode_terms_without_casefold_expansion(self):
        saved = memory.save(self.folder, "note", "Travel directions",
                            "Follow the Straße to the station.")
        for query in ("Straße", "straße"):
            with self.subTest(query=query):
                found = memory.search(self.folder, query, kind="note")
                self.assertEqual([row["id"] for row in found], [saved["id"]])

    def test_fallback_filters_single_term_hits_before_result_limit(self):
        for number in range(6):
            memory.save(self.folder, "note", f"Alpha {number}", "Short entry.")
        saved = memory.save(
            self.folder, "note", "Relevant entry",
            "alpha beta " + "background " * 1000,
        )

        found = memory.search(self.folder, "alpha beta gamma", kind="note", limit=1)

        self.assertEqual([row["id"] for row in found], [saved["id"]])

    def test_search_treats_query_as_text_not_fts_syntax(self):
        memory.save(self.folder, "component", "Search API", "The database stores app components.")
        self.assertTrue(memory.search(self.folder, '"Search" OR component*'))
        self.assertEqual(memory.search(self.folder, "unrelatedword"), [])

    def test_tool_scope_requires_registered_exact_project_folder(self):
        import toolbox_mcp

        state = Path(self.temp.name) / "LongTasks"
        state.mkdir()
        with closing(sqlite3.connect(state / "projects.db")) as conn:
            conn.execute("CREATE TABLE projects(folder TEXT)")
            conn.execute("INSERT INTO projects(folder) VALUES (?)", (str(self.folder),))
            conn.commit()
        with patch.object(toolbox_mcp, "ROOT", Path(self.temp.name)):
            self.assertEqual(toolbox_mcp.registered_project(str(self.folder)), self.folder)
            other = Path(self.temp.name) / "other"
            other.mkdir()
            with self.assertRaises(ValueError):
                toolbox_mcp.registered_project(str(other))


def test_common_pair_survives_high_ranked_rare_single_term_hits(tmp_path):
    import project_memory

    # Rare single-term notes can outrank a longer, eligible two-term match.
    for number in range(20):
        project_memory.save(tmp_path, "note", f"Alpha {number}", "Short entry.")
        project_memory.save(tmp_path, "note", f"Beta {number}", "Short entry.")
    for number in range(6):
        project_memory.save(tmp_path, "note", f"Gamma {number}", "Short entry.")
    wanted = project_memory.save(tmp_path, "note", "Relevant entry", "alpha beta " + "background " * 1000)
    found = project_memory.search(tmp_path, "alpha beta gamma", kind="note", limit=1)
    assert [row["id"] for row in found] == [wanted["id"]]


if __name__ == "__main__":
    unittest.main(verbosity=2)
