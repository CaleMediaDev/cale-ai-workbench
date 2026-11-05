"""Persistent, project-scoped memory and full-text indexing behavior."""

import hashlib
import pytest
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import project_memory as memory
import subprocess
import sys
import long_tasks
import quick_calculator


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

    def test_document_reindex_replaces_changed_text_with_same_source_and_revision(self):
        source = self.folder / "manual.pdf"
        source.write_bytes(b"unchanged document bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        original = "obsoletezebra " + "x" * memory.DOCUMENT_CHUNK_CHARS
        corrected = "correctedzebra " + "y" * memory.DOCUMENT_CHUNK_CHARS
        first = memory.index_document_text(self.folder, source, original, digest)

        result = memory.index_document_text(self.folder, source, corrected, digest)

        self.assertEqual(result["parts"], first["parts"])
        self.assertTrue(result["updated"])
        self.assertEqual(memory.search(self.folder, "obsoletezebra", kind="document"), [])
        found = memory.search(self.folder, "correctedzebra", kind="document")
        self.assertEqual([row["id"] for row in found], ["document:manual.pdf:1"])
        with memory.connection(self.folder) as conn:
            bodies = [row[0] for row in conn.execute(
                "SELECT body FROM memories WHERE kind='document' ORDER BY id"
            )]
            self.assertEqual("".join(bodies), corrected)
            for table in ("memories", "memory_fts"):
                self.assertEqual(conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0], result["parts"])
        self.assertFalse(memory.index_document_text(
            self.folder, source, corrected, digest)["updated"])

    def test_document_chunk_boundary_preserves_searchable_word(self):
        source = self.folder / "boundary.pdf"
        source.write_bytes(b"document bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        prefix = " " * (memory.DOCUMENT_CHUNK_CHARS - 5)
        text = prefix + "boundaryzebra passage"

        result = memory.index_document_text(self.folder, source, text, digest)

        self.assertEqual(result["parts"], 2)
        found = memory.search(self.folder, "boundaryzebra", kind="document")
        self.assertEqual([row["id"] for row in found],
                         ["document:boundary.pdf:2"])
        with memory.connection(self.folder) as conn:
            bodies = [row[0] for row in conn.execute(
                "SELECT body FROM memories WHERE kind='document' ORDER BY id"
            )]
        self.assertEqual("".join(bodies), text)
        self.assertTrue(all(len(body) <= memory.DOCUMENT_CHUNK_CHARS
                            for body in bodies))
        self.assertFalse(memory.index_document_text(
            self.folder, source, text, digest)["updated"])

    def test_document_chunk_boundary_preserves_combining_mark_word(self):
        source = self.folder / "accent.pdf"
        source.write_bytes(b"document bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        word = "cafe\u0301marker"
        # Place the fixed boundary immediately before, then after, the accent.
        for offset in (4, 5):
            with self.subTest(offset=offset):
                text = " " * (memory.DOCUMENT_CHUNK_CHARS - offset) + word
                result = memory.index_document_text(self.folder, source, text, digest)
                self.assertEqual(result["parts"], 2)
                found = memory.search(self.folder, word, kind="document")
                self.assertEqual([row["id"] for row in found],
                                 ["document:accent.pdf:2"])
                with memory.connection(self.folder) as conn:
                    bodies = [row[0] for row in conn.execute(
                        "SELECT body FROM memories WHERE kind='document' ORDER BY id"
                    )]
                self.assertEqual("".join(bodies), text)
                self.assertTrue(all(len(body) <= memory.DOCUMENT_CHUNK_CHARS
                                    for body in bodies))

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




if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_recovery_artifact_guard_keeps_new_notes_inside_project(tmp_path):
    import pytest
    root=tmp_path/'project';root.mkdir()
    assert memory._artifact_project_root(root)==root.resolve()
    with pytest.raises((ValueError,FileNotFoundError)):
        memory._artifact_project_root(tmp_path/'missing')


def test_recovery_artifact_guard_rejects_real_notes_junction(tmp_path):
    import pytest
    import subprocess
    external=tmp_path/'external';external.mkdir()
    root=tmp_path/'project';root.mkdir()
    notes=root/'.local-ai'
    if os.name=='nt':
        result=subprocess.run(['cmd','/c','mklink','/J',str(notes),str(external)],capture_output=True)
        assert result.returncode==0
    else:
        notes.symlink_to(external,target_is_directory=True)
    try:
        with pytest.raises(ValueError,match='own project'):
            memory._artifact_project_root(root)
        import task_context
        with pytest.raises(ValueError,match='own project'):
            task_context.recovery_episode(root,'a'*64,None,trigger='read_stall')
        assert not (external/'memory.sqlite3').exists()
    finally:
        if os.name=='nt':notes.rmdir()
        else:notes.unlink()


def test_recovery_artifact_guard_rejects_database_link(tmp_path):
    import pytest
    root=tmp_path/'project';root.mkdir()
    external=tmp_path/'external';external.mkdir()
    notes=root/'.local-ai';notes.mkdir()
    target=external/'state.sqlite3';target.write_text('unmodified')
    try:
        (notes/'memory.sqlite3').symlink_to(target)
    except OSError:
        pytest.skip('File symlinks unavailable; real directory junction guard already verified')
    with pytest.raises(ValueError,match='own project'):
        memory._artifact_project_root(root)
    assert target.read_text()=='unmodified'


# Scoped host evidence port: original accepted85552a5 regressions.

def test_verification_manifest_covers_external_js_and_marks_omitted_inputs_limited(tmp_path,monkeypatch):
    monkeypatch.setattr(memory,"_VERIFICATION_MAX_BYTES",2*1024*1024)
    root=tmp_path/'app';root.mkdir();(root/'index.html').write_text('<script src="app.js"></script>')
    script=root/'app.js';script.write_text('let n=1')
    first=memory.verification_source_state(root)
    assert first['scope']=='full_project_source'
    script.write_text('let n=2')
    assert memory.verification_source_state(root)['sha256']!=first['sha256']
    (root/'large.mov').write_bytes(b'x'*(2*1024*1024+1))
    limited=memory.verification_source_state(root)
    assert limited['scope']=='limited_dependency_manifest' and limited['omitted']>=1
    assert limited['bytes']<2*1024*1024



def test_verification_receipts_capture_real_pass_but_reject_other_project_and_corrupt_cache(tmp_path):
    import json
    import native_agent
    from unittest.mock import patch
    root=tmp_path/'one';root.mkdir();(root/'.local-ai').mkdir()
    (root/'logic.py').write_text('def twice(value): return value * 2')
    project={'folder':str(root),'brief':'Implement twice(value) in logic.py','id':'receipt'}
    args={'path':'logic.py','function':'twice','cases':[{'args':[3],'expected':6}]}
    import long_tasks
    runtime=tmp_path/'runtime';runtime.mkdir()
    with memory.verification_context(project),patch.object(long_tasks,'STATE',runtime),patch.object(long_tasks,'projects',return_value=[project]):
        verdict=json.loads(native_agent.execute_tool(root,'check_python_function',args))
    assert verdict['passed']
    rows=memory.current_check_receipts(root,project,checker_file=native_agent.__file__)
    assert rows and rows[0]['verification_level']=='acceptance_passed'
    assert rows[0]['training_eligible'] is False
    assert len(rows[0]['report_sha256'])==64 and rows[0]['planning_basis']['brief']
    other=tmp_path/'two';other.mkdir();(other/'.local-ai').mkdir()
    (other/'logic.py').write_bytes((root/'logic.py').read_bytes())
    import shutil
    shutil.copy2(root/'.local-ai/memory.sqlite3',other/'.local-ai/memory.sqlite3')
    assert memory.current_check_receipts(other,{**project,'folder':str(other)},checker_file=native_agent.__file__)==[]
    with memory.connection(root) as conn:
        conn.execute("UPDATE verification_receipts SET payload=?",(' '*1000000,))
    assert memory.current_check_receipts(root,project,checker_file=native_agent.__file__)==[]



def test_verification_receipt_wont_attest_hot_replaced_unloaded_checker(tmp_path):
    root=tmp_path/'project';root.mkdir()
    project={'folder':str(root),'brief':'Check a local operation'}
    runner=tmp_path/'checker.py';runner.write_text('# implementation one')
    loaded=hashlib.sha256(runner.read_bytes()).hexdigest()
    runner.write_text('# implementation two was never loaded')
    # Isolate the source-snapshot guard after an already-trusted execution
    # binding. Actual execution/permit positives are tested independently.
    with memory.verification_context(project),patch.object(memory,'_consume_host_check_permit',return_value=True) as trusted:
        source=memory.verification_source_state(root);basis=memory.verification_basis(root)
        assert memory.record_host_check_receipt(root,'test',{}, {'passed':True},source,
            checker_file=runner,basis_before=basis,loaded_checker_sha256=loaded) is None
        assert trusted.call_count==1
    assert memory.current_check_receipts(root,project,checker_file=runner)==[]


def test_verification_streams_complete_assets_above_old_two_mib_cap(tmp_path,monkeypatch):
    asset=tmp_path/'sprite.bin';data=b'x'*(3*1024*1024+7);asset.write_bytes(data)
    original=Path.open;sizes=[]
    class Reader:
        def __init__(self,stream):self.stream=stream
        def __enter__(self):return self
        def __exit__(self,*args):return self.stream.__exit__(*args)
        def fileno(self):return self.stream.fileno()
        def read(self,size=-1):
            sizes.append(size);assert 0<=size<=1024*1024,'source hashing must stream at most1MiB'
            return self.stream.read(size)
    def opened(path,mode='r',*args,**kwargs):
        stream=original(path,mode,*args,**kwargs)
        return Reader(stream) if path==asset and mode=='rb' else stream
    monkeypatch.setattr(Path,'open',opened)
    observed=memory.verification_source_state(tmp_path)
    assert observed['scope']=='full_project_source' and observed['bytes']==len(data)
    expected=hashlib.sha256(json.dumps([['sprite.bin',hashlib.sha256(data).hexdigest()]],separators=(',',':')).encode()).hexdigest()
    assert observed['sha256']==expected and sizes


def test_verification_complete_manifest_exceeds_old_directory_and_file_caps(tmp_path):
    assets=tmp_path/'assets';assets.mkdir()
    for index in range(2050):(assets/f'{index:05}.txt').write_bytes(b'x')
    observed=memory.verification_source_state(tmp_path)
    assert observed['scope']=='full_project_source' and observed['files']==2050 and observed['bytes']==2050


@pytest.mark.parametrize('budget,reason', [('_VERIFICATION_MAX_BYTES','byte_budget'),
    ('_VERIFICATION_MAX_FILES','file_budget'),('_VERIFICATION_MAX_VISITED','visited_budget'),
    ('_VERIFICATION_MAX_PATH_BYTES','path_budget'),('_VERIFICATION_MAX_SECONDS','elapsed_budget')])
def test_verification_budget_exhaustion_is_limited_never_a_full_digest(tmp_path,monkeypatch,budget,reason):
    (tmp_path/'one.txt').write_bytes(b'abc');(tmp_path/'two.txt').write_bytes(b'def')
    monkeypatch.setattr(memory,budget,0 if budget=='_VERIFICATION_MAX_SECONDS' else 1)
    result=memory.verification_source_state(tmp_path)
    assert result['scope']=='limited_dependency_manifest' and result['omitted']>=1
    assert reason in result['limitations']


def test_verification_rehashes_same_size_source_when_mtime_is_restored(tmp_path):
    source=tmp_path/'source.js';source.write_bytes(b'const value=1;');original=source.stat()
    before=memory.verification_source_state(tmp_path)
    source.write_bytes(b'const value=2;');os.utime(source,ns=(original.st_atime_ns,original.st_mtime_ns))
    after=memory.verification_source_state(tmp_path)
    assert before['scope']==after['scope']=='full_project_source'
    assert before['sha256']!=after['sha256'] and before['bytes']==after['bytes']


def test_verification_unavailable_file_and_runtime_are_explicit_limited(tmp_path,monkeypatch):
    source=tmp_path/'source.js';source.write_bytes(b'const value=1;')
    runtime=tmp_path/'node_modules';runtime.mkdir()
    original=Path.open
    def opened(path,mode='r',*args,**kwargs):
        if path==source and mode=='rb':raise PermissionError('temporary read failure')
        return original(path,mode,*args,**kwargs)
    monkeypatch.setattr(Path,'open',opened)
    result=memory.verification_source_state(tmp_path)
    assert result['scope']=='limited_dependency_manifest'
    assert {'unavailable_path','excluded_runtime'}<=set(result['limitations'])


def test_verification_detects_file_mutation_during_stream(tmp_path,monkeypatch):
    source=tmp_path/'source.bin';source.write_bytes(b'x'*(2*1024*1024));initial=source.stat()
    original=Path.open;changed=[]
    class Reader:
        def __init__(self,stream):self.stream=stream
        def __enter__(self):return self
        def __exit__(self,*args):return self.stream.__exit__(*args)
        def fileno(self):return self.stream.fileno()
        def read(self,size):
            data=self.stream.read(size)
            if data and not changed:
                changed.append(True)
                with original(source,'r+b') as writer:writer.write(b'y')
                os.utime(source,ns=(initial.st_atime_ns,initial.st_mtime_ns+1_000_000_000))
            return data
    def opened(path,mode='r',*args,**kwargs):
        stream=original(path,mode,*args,**kwargs)
        return Reader(stream) if path==source and mode=='rb' else stream
    monkeypatch.setattr(Path,'open',opened)
    result=memory.verification_source_state(tmp_path)
    assert changed and result['scope']=='limited_dependency_manifest'
    assert 'changed_file' in result['limitations'] and result['files']==0


def test_verification_detects_directory_listing_changes(tmp_path,monkeypatch):
    source=tmp_path/'source.js';source.write_bytes(b'x');initial=tmp_path.stat();original=Path.open;changed=[]
    def opened(path,mode='r',*args,**kwargs):
        if path==source and mode=='rb' and not changed:
            changed.append(True);(tmp_path/'late.js').write_bytes(b'y')
            os.utime(tmp_path,ns=(initial.st_atime_ns,initial.st_mtime_ns+1_000_000_000))
        return original(path,mode,*args,**kwargs)
    monkeypatch.setattr(Path,'open',opened)
    result=memory.verification_source_state(tmp_path)
    assert changed and result['scope']=='limited_dependency_manifest'
    assert 'changed_directory' in result['limitations']


def test_application_source_names_only_intentional_runtime_exclusions(tmp_path,monkeypatch):
    (tmp_path/'index.html').write_text('<p>App</p>')
    (tmp_path/'node_modules').mkdir()
    first=memory.verification_source_state(tmp_path)
    assert first['scope']=='application_source'
    assert first['excluded_runtime_roots']==['node_modules']
    (tmp_path/'Toolbox').mkdir()
    second=memory.verification_source_state(tmp_path)
    assert second['scope']=='application_source' and first['sha256']!=second['sha256']
    monkeypatch.setattr(memory,'_VERIFICATION_MAX_BYTES',1)
    limited=memory.verification_source_state(tmp_path)
    assert limited['scope']=='limited_dependency_manifest' and 'byte_budget' in limited['limitations']


def test_application_source_exclusion_metadata_overflow_is_limited(tmp_path):
    for i in range(129):
        parent=tmp_path/str(i);parent.mkdir();(parent/'node_modules').mkdir()
    observed=memory.verification_source_state(tmp_path)
    assert observed['scope']=='limited_dependency_manifest'
    assert 'excluded_runtime_budget' in observed['limitations']


@pytest.mark.parametrize('runtime_excluded', [False, True])
def test_verification_rechecks_earlier_file_after_later_asset_streams(tmp_path, monkeypatch, runtime_excluded):
    """A normal content edit does not change its parent's directory identity."""
    early = tmp_path / 'a.js'
    later = tmp_path / 'z.bin'
    early.write_bytes(b'const value=1;')
    later.write_bytes(b'x' * (2 * 1024 * 1024))
    if runtime_excluded:
        (tmp_path / 'node_modules').mkdir()
    baseline = memory.verification_source_state(tmp_path)
    assert baseline['scope'] == ('application_source' if runtime_excluded else 'full_project_source')
    parent_before = memory._verification_file_identity(tmp_path.stat())
    source_before = early.stat()
    original_open = Path.open
    original_scandir = os.scandir
    changed = []

    class OrderedScan:
        def __init__(self, path):
            self.scan = original_scandir(path)
        def __enter__(self):
            return iter(sorted(self.scan, key=lambda item: item.name))
        def __exit__(self, *args):
            self.scan.close()

    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            data = self.stream.read(size)
            if data and not changed:
                changed.append(True)
                with original_open(early, 'r+b') as writer:
                    writer.write(b'const value=2;')
                os.utime(early, ns=(source_before.st_atime_ns, source_before.st_mtime_ns + 1_000_000_000))
            return data

    def opened(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return Reader(stream) if path == later and mode == 'rb' else stream

    monkeypatch.setattr(os, 'scandir', OrderedScan)
    monkeypatch.setattr(Path, 'open', opened)
    observed = memory.verification_source_state(tmp_path)
    assert changed and memory._verification_file_identity(tmp_path.stat()) == parent_before
    assert observed['sha256'] == baseline['sha256'], 'The streamed early bytes were the old snapshot'
    assert observed['scope'] == 'limited_dependency_manifest', observed
    assert 'changed_file' in observed['limitations']


@pytest.mark.parametrize('failure', ['unavailable', 'elapsed'])
def test_verification_final_file_recheck_fails_closed_without_reading_again(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    source = tmp_path / 'source.js'
    source.write_bytes(b'const value=1;')
    original_stat = Path.stat
    original_open = Path.open
    stats = []
    reads = []
    clock = [0.0]

    def stated(path, *args, **kwargs):
        if path == source:
            stats.append(path)
            if len(stats) == 3:
                if failure == 'unavailable':
                    raise PermissionError('File became unavailable after hashing')
                clock[0] = 61.0
        return original_stat(path, *args, **kwargs)

    def opened(path, mode='r', *args, **kwargs):
        if path == source and mode == 'rb':
            reads.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', stated)
    monkeypatch.setattr(Path, 'open', opened)
    monkeypatch.setattr(memory, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    observed = memory.verification_source_state(tmp_path)
    assert len(stats) == 3 and reads == [source]
    assert observed['scope'] == 'limited_dependency_manifest'
    assert ('unavailable_path' if failure == 'unavailable' else 'elapsed_budget') in observed['limitations']


_PORTED_EXPRESSION_CASES = frozenset(['test_tested_expression_registration_and_reuse_uses_no_model_or_sql_completion', 'test_failed_expression_case_is_not_registered_and_tampered_records_never_run', 'test_expression_helper_request_changes_and_verification_race_are_not_committed', 'test_expression_helper_interpreter_upgrade_and_invocation_request_race_discard_result', 'test_expression_helpers_reject_real_directory_junction_escape_before_database_write', 'test_expression_registration_rejects_interpreter_changed_during_passed_cases', 'test_expression_registration_rejects_already_hot_replaced_interpreter', 'test_expression_invocation_rejects_interpreter_changed_during_execution', 'test_helper_oversized_stored_cases_are_rejected_before_json_decode', 'test_helper_discovery_rejects_tampered_parameter_data_without_exporting_literals', 'test_helper_discovery_and_invocation_reject_consistently_hashed_invalid_case_shape', 'test_expression_helper_attestation_and_registration_survive_fresh_process'])


@pytest.fixture(autouse=True)
def _ported_expression_owner_context(request,tmp_path,monkeypatch):
    """Give the earlier pure-DSL unit tests an explicit synthetic task owner.

    These tests exercise interpreter/record invariants. Separate integration
    tests below use the real task registry and test missing/deleted ownership.
    """
    if request.node.name not in _PORTED_EXPRESSION_CASES:return
    actual=memory.verification_basis
    def basis(folder,project=None):
        root=Path(folder).resolve()
        assert root.is_relative_to(tmp_path.resolve())
        return actual(root,project or {'id':'helper-fixture','folder':str(root),'brief':'Owned helper fixture'})
    monkeypatch.setattr(memory,'_helper_request_identity',lambda folder:memory._verification_digest(basis(folder)))

def example_expression_helper(root):
    return memory.register_expression_helper(root, "invoice_total", {"amounts": "numbers", "tax": "number"},
        'round(sum(amounts) * (1 + tax), 2)', [
            {"inputs": {"amounts": [10, 5], "tax": 0.06}, "expected": 15.9},
            {"inputs": {"amounts": [], "tax": 0}, "expected": 0}])


def test_tested_expression_registration_and_reuse_uses_no_model_or_sql_completion(tmp_path):
    import quick_calculator
    import task_context
    root = tmp_path / "helper-project"; root.mkdir()
    active = task_context.plan_steps(root, [{"phase": "Core", "title": "Build app", "check_command": "exit 0"}])["active"]
    before = task_context.step_status(root)
    record = example_expression_helper(root)
    assert record["registered"] and record["verified_cases"] == 2
    assert "check_sha256" not in record  # Examples are not an active acceptance receipt.
    with patch("cloud_ai.Router.complete", side_effect=AssertionError("Helper must not call a model")), patch("subprocess.run", side_effect=AssertionError("Helper must not execute a process")):
        value = memory.run_expression_helper(root, record["id"], {"amounts": [100, 25], "tax": 0.1})
    assert value["value"] == 137.5 and value["inference_calls"] == 0 and value["task_completion"] is False
    assert task_context.step_status(root) == before
    found = memory.find_expression_helpers(root, "invoice")
    assert "uses" not in found["helpers"][0] and found["helpers"][0]["current_request"] is True
    assert "cases" not in found["helpers"][0] and "expression" not in found["helpers"][0]
    other = tmp_path / "other-project"; other.mkdir()
    assert memory.find_expression_helpers(other)["helpers"] == []


def test_failed_expression_case_is_not_registered_and_tampered_records_never_run(tmp_path):
    import pytest
    import json
    failed = memory.register_expression_helper(tmp_path, "wrong", {"x": "number"}, "x + 1", [{"inputs": {"x": 2}, "expected": 99}])
    assert failed["registered"] is False and memory.find_expression_helpers(tmp_path)["helpers"] == []
    record = example_expression_helper(tmp_path)
    path=next((tmp_path/'.local-ai/expression-helpers/v1/records').iterdir())
    raw=json.loads(path.read_bytes());raw['definition']['expression']='0'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="changed"):
        memory.run_expression_helper(tmp_path, record["id"], {"amounts": [10], "tax": 0.06})


def test_expression_helper_request_changes_and_verification_race_are_not_committed(tmp_path):
    import pytest
    import quick_calculator
    record = example_expression_helper(tmp_path)
    requests = tmp_path / ".local-ai/USER-REQUESTS.md"
    requests.write_text("New requirement")
    assert memory.find_expression_helpers(tmp_path)["helpers"][0]["current_request"] is False
    with pytest.raises(ValueError, match="instructions.*changed|instructions changed"):
        memory.run_expression_helper(tmp_path, record["id"], {"amounts": [10], "tax": 0.06})
    second = tmp_path / "raced";second.mkdir()
    actual = quick_calculator.run_helper
    def changed(*args):
        result = actual(*args)
        (second / ".local-ai/USER-REQUESTS.md").write_text("Changed during cases")
        return result
    with patch.object(quick_calculator, "run_helper", side_effect=changed), pytest.raises(ValueError, match="instructions.*changed|instructions changed"):
        example_expression_helper(second)
    assert memory.find_expression_helpers(second)["helpers"] == []


def test_expression_helper_interpreter_upgrade_and_invocation_request_race_discard_result(tmp_path):
    import pytest
    import quick_calculator
    record = example_expression_helper(tmp_path)
    changed = tmp_path / "upgraded-interpreter.py"; changed.write_text("new version")
    with patch.object(quick_calculator, "__file__", str(changed)), pytest.raises(ValueError, match="interpreter changed"):
        memory.run_expression_helper(tmp_path, record["id"], {"amounts": [1], "tax": 0})
    actual = quick_calculator.run_helper
    def raced(*args):
        result = actual(*args)
        (tmp_path / ".local-ai/USER-REQUESTS.md").write_text("Changed during invocation")
        return result
    with patch.object(quick_calculator, "run_helper", side_effect=raced), pytest.raises(ValueError, match="instructions.*changed|during invocation"):
        memory.run_expression_helper(tmp_path, record["id"], {"amounts": [1], "tax": 0})
    assert "uses" not in memory.find_expression_helpers(tmp_path)["helpers"][0]


def test_expression_helpers_reject_real_directory_junction_escape_before_database_write(tmp_path):
    import pytest
    root=tmp_path/'project';outside=tmp_path/'outside';root.mkdir();outside.mkdir()
    notes=root/'.local-ai'
    if os.name=='nt':
        import _winapi
        _winapi.CreateJunction(str(outside),str(notes))
    else:
        notes.symlink_to(outside,target_is_directory=True)
    assert not notes.resolve().is_relative_to(root)
    with pytest.raises(ValueError,match='project'):
        example_expression_helper(root)
    with pytest.raises(ValueError,match='project'):
        memory.find_expression_helpers(root)
    with pytest.raises(ValueError,match='project'):
        memory.run_expression_helper(root,'0'*64,{'x':1})
    assert not (outside/'memory.sqlite3').exists()


def copied_expression_interpreter(tmp_path):
    import importlib.util
    import quick_calculator
    path=tmp_path/'helper_interpreter.py'
    path.write_bytes(Path(quick_calculator.__file__).read_bytes())
    spec=importlib.util.spec_from_file_location('helper_interpreter_probe',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module,path


def test_expression_registration_rejects_interpreter_changed_during_passed_cases(tmp_path):
    import pytest,sys
    module,path=copied_expression_interpreter(tmp_path)
    root=tmp_path/'project';root.mkdir();actual=module.run_helper
    def changed(*args):
        result=actual(*args)
        path.write_bytes(path.read_bytes()+b'\n# changed after cases executed\n')
        return result
    with patch.dict(sys.modules,{'quick_calculator':module}),patch.object(module,'run_helper',side_effect=changed):
        with pytest.raises(ValueError,match='interpreter changed'):
            example_expression_helper(root)
    assert memory.find_expression_helpers(root)['helpers']==[]


def test_expression_registration_rejects_already_hot_replaced_interpreter(tmp_path):
    import pytest,sys
    module,path=copied_expression_interpreter(tmp_path)
    path.write_bytes(path.read_bytes()+b'\n# changed after module loaded\n')
    root=tmp_path/'project';root.mkdir()
    with patch.dict(sys.modules,{'quick_calculator':module}):
        with pytest.raises(ValueError,match='interpreter changed'):
            example_expression_helper(root)
    assert memory.find_expression_helpers(root)['helpers']==[]


def test_expression_invocation_rejects_interpreter_changed_during_execution(tmp_path):
    import pytest,sys
    module,path=copied_expression_interpreter(tmp_path)
    root=tmp_path/'project';root.mkdir()
    with patch.dict(sys.modules,{'quick_calculator':module}):
        record=example_expression_helper(root)
        actual=module.run_helper
        def changed(*args):
            result=actual(*args)
            path.write_bytes(path.read_bytes()+b'\n# changed during invocation\n')
            return result
        with patch.object(module,'run_helper',side_effect=changed):
            with pytest.raises(ValueError,match='changed'):
                memory.run_expression_helper(root,record['id'],{'amounts':[10],'tax':0})
    assert not (root/'.local-ai/memory.sqlite3').exists()


def test_helper_oversized_stored_cases_are_rejected_before_json_decode(tmp_path):
    import pytest,json
    record=example_expression_helper(tmp_path)
    path=next((tmp_path/'.local-ai/expression-helpers/v1/records').iterdir())
    path.write_bytes(b' '*1_000_000+path.read_bytes())
    original=json.loads
    def bounded(value,*args,**kwargs):
        assert len(value.encode() if isinstance(value,str) else value)<=memory._HELPER_RECORD_BYTES,'Oversized stored cases were decoded'
        return original(value,*args,**kwargs)
    with patch('json.loads',side_effect=bounded),pytest.raises(ValueError,match='budget|size|changed'):
        memory.run_expression_helper(tmp_path,record['id'],{'amounts':[10],'tax':0})


def test_helper_discovery_rejects_tampered_parameter_data_without_exporting_literals(tmp_path):
    import json
    record=example_expression_helper(tmp_path)
    path=next((tmp_path/'.local-ai/expression-helpers/v1/records').iterdir())
    row=json.loads(path.read_bytes());row['definition']['parameters']={'private':'PRIVATE_LITERAL_METADATA'}
    path.write_text(json.dumps(row))
    found=memory.find_expression_helpers(tmp_path)
    assert found['helpers']==[] and 'PRIVATE_LITERAL_METADATA' not in json.dumps(found)


def test_helper_discovery_and_invocation_reject_consistently_hashed_invalid_case_shape(tmp_path):
    import json,pytest
    record=example_expression_helper(tmp_path)
    path=next((tmp_path/'.local-ai/expression-helpers/v1/records').iterdir())
    row=json.loads(path.read_bytes())
    row['cases']=[{'inputs':{'amounts':[],'tax':0},'expected':0,'extra':'unsupported'}]
    row['cases_hash']=memory._helper_digest(row['cases'])
    new_id=row['id']=memory._helper_digest([str(tmp_path.resolve()),row['source_hash'],row['cases_hash'],row['runner_hash'],row['request_hash']])
    raw=memory._helper_bytes(row);path.unlink()
    (path.parent/(new_id+'-'+hashlib.sha256(raw).hexdigest()+'.json')).write_bytes(raw)
    assert memory.find_expression_helpers(tmp_path)['helpers']==[]
    with pytest.raises(ValueError):
        memory.run_expression_helper(tmp_path,new_id,{'amounts':[10],'tax':0})


def test_expression_helper_attestation_and_registration_survive_fresh_process(tmp_path):
    import subprocess,sys,json
    script="""import sys,json,project_memory as memory
root=sys.argv[1]
context=memory.verification_context({'id':'child-helper','folder':root,'brief':'Owned helper fixture'})
context.__enter__()
if sys.argv[2]=='register':
 result=memory.register_expression_helper(root,'plus_one',{'x':'number'},'x+1',[{'inputs':{'x':1},'expected':2}])
else:
 result=memory.run_expression_helper(root,sys.argv[3],{'x':10})
print(json.dumps(result))
"""
    import long_tasks
    state=tmp_path/'supervisor';state.mkdir()
    with sqlite3.connect(state/'projects.db') as conn:
        conn.execute('CREATE TABLE projects(id TEXT,folder TEXT,brief TEXT)')
        conn.execute('INSERT INTO projects VALUES(?,?,?)',('child-helper',str(tmp_path),'Owned helper fixture'))
    script=script.replace('root=sys.argv[1]','import long_tasks\nroot=sys.argv[1]\nlong_tasks.STATE=memory.Path(root)/"supervisor"')
    command=[sys.executable,'-B','-c',script,str(tmp_path)]
    first=subprocess.run(command+['register'],cwd=Path(memory.__file__).parent,capture_output=True,text=True,timeout=15,check=True)
    identifier=json.loads(first.stdout)['id']
    second=subprocess.run(command+['run',identifier],cwd=Path(memory.__file__).parent,capture_output=True,text=True,timeout=15,check=True)
    assert json.loads(second.stdout)['value']==11




@pytest.fixture
def registered_expression_project(tmp_path,monkeypatch):
    import long_tasks
    root=tmp_path/'project';root.mkdir()
    state=tmp_path/'workbench/LongTasks';state.mkdir(parents=True)
    project={'id':'a'*12,'folder':str(root),'brief':'Compute invoice totals using tested project-local arithmetic.'}
    with sqlite3.connect(state/'projects.db') as conn:
        conn.execute('CREATE TABLE projects(id TEXT,folder TEXT,brief TEXT)')
        conn.execute('INSERT INTO projects VALUES(?,?,?)',(project['id'],project['folder'],project['brief']))
    monkeypatch.setattr(long_tasks,'STATE',state)
    return root,project,state


@pytest.mark.parametrize('changed',['brief','rules','followup','deleted'])
def test_helper_reuse_binds_real_registered_original_instructions(registered_expression_project,changed):
    root,project,state=registered_expression_project
    record=example_expression_helper(root)
    assert memory.run_expression_helper(root,record['id'],{'amounts':[100],'tax':.1})['value']==110
    if changed in {'brief','deleted'}:
        with sqlite3.connect(state/'projects.db') as conn:
            if changed=='brief':conn.execute('UPDATE projects SET brief=?',('Different original task',))
            else:conn.execute('DELETE FROM projects')
    else:
        path=root/'AGENTS.md' if changed=='rules' else root/'.local-ai/USER-REQUESTS.md'
        path.write_text('New required behavior')
    with pytest.raises(ValueError,match='instructions|registered'):
        memory.run_expression_helper(root,record['id'],{'amounts':[100],'tax':.1})


def test_helper_missing_owner_never_creates_project_state(tmp_path,monkeypatch):
    import long_tasks
    state=tmp_path/'missing-registry';monkeypatch.setattr(long_tasks,'STATE',state)
    root=tmp_path/'unknown';root.mkdir()
    with pytest.raises(ValueError,match='registered'):
        example_expression_helper(root)
    assert not (root/'.local-ai').exists()


def test_helper_held_state_prevents_real_junction_swap_before_publish(registered_expression_project,monkeypatch,tmp_path):
    import _winapi,design_tools
    root,project,_=registered_expression_project
    outside=tmp_path/'outside';outside.mkdir();blocked=[]
    original=design_tools._game_rename_handle
    def raced(handle,target):
        parent=target.parent
        try:
            parent.rename(parent.with_name('moved-records'))
            _winapi.CreateJunction(str(outside),str(parent))
        except OSError:blocked.append(True)
        return original(handle,target)
    monkeypatch.setattr(design_tools,'_game_rename_handle',raced)
    assert example_expression_helper(root)['registered'] and blocked
    assert list(outside.iterdir())==[]


def test_helper_records_reject_hardlinks_without_touching_old_sqlite_sidecars(registered_expression_project,tmp_path):
    root,_,_=registered_expression_project
    notes=root/'.local-ai';notes.mkdir()
    controls=[]
    for suffix in ('','-wal','-shm','-journal'):
        control=tmp_path/('outside'+suffix);control.write_bytes(b'X'*50000)
        os.link(control,notes/('memory.sqlite3'+suffix));controls.append(control)
    record=example_expression_helper(root)
    assert memory.run_expression_helper(root,record['id'],{'amounts':[10],'tax':0})['value']==10
    assert all(path.read_bytes()==b'X'*50000 for path in controls)
    path=next((notes/'expression-helpers/v1/records').iterdir())
    outside=tmp_path/'record-alias';os.link(path,outside)
    with pytest.raises(ValueError,match='changed|registered'):
        memory.run_expression_helper(root,record['id'],{'amounts':[10],'tax':0})
    assert memory.find_expression_helpers(root)['helpers']==[]


def test_helper_registry_preserves_128_record_bound(registered_expression_project):
    root,_,_=registered_expression_project
    record=example_expression_helper(root)
    directory=root/'.local-ai/expression-helpers/v1/records'
    for number in range(127):(directory/('unknown-'+str(number))).write_bytes(b'preserve')
    with pytest.raises(ValueError,match='128'):
        memory.register_expression_helper(root,'second',{'x':'number'},'x+1',[{'inputs':{'x':1},'expected':2}])
    assert memory.run_expression_helper(root,record['id'],{'amounts':[10],'tax':0})['value']==10
    (directory/'129th').write_bytes(b'preserve')
    with pytest.raises(ValueError,match='128'):memory.find_expression_helpers(root)

@pytest.mark.parametrize('when',['before','after'])
def test_immutable_helper_records_survive_actual_process_crash_and_restart(registered_expression_project,when):
    import subprocess,sys,json
    root,project,state=registered_expression_project
    script='''import sys,json,os,long_tasks,design_tools,project_memory as memory
from pathlib import Path
root=Path(sys.argv[1]);long_tasks.STATE=Path(sys.argv[2]);when=sys.argv[3]
original=design_tools._game_rename_handle
def crash(handle,target):
 if when=='before':os._exit(73)
 original(handle,target)
 if when=='after':os._exit(74)
design_tools._game_rename_handle=crash
memory.register_expression_helper(root,'plus_one',{'x':'number'},'x+1',[{'inputs':{'x':1},'expected':2}])
'''
    command=[sys.executable,'-B','-c',script,str(root),str(state),when]
    crashed=subprocess.run(command,cwd=Path(memory.__file__).parent,capture_output=True,text=True,timeout=20)
    assert crashed.returncode==(73 if when=='before' else 74),crashed.stderr
    directory=root/'.local-ai/expression-helpers/v1'
    before={p:p.read_bytes() for p in (directory/'staging').iterdir()}
    if when=='before':assert memory.find_expression_helpers(root)['helpers']==[] and before
    else:assert len(memory.find_expression_helpers(root)['helpers'])==1
    # A separate process rebuilds from immutable bytes and recomputes examples.
    resumed='''import sys,json,long_tasks,project_memory as memory
from pathlib import Path
root=Path(sys.argv[1]);long_tasks.STATE=Path(sys.argv[2])
r=memory.register_expression_helper(root,'plus_one',{'x':'number'},'x+1',[{'inputs':{'x':1},'expected':2}])
print(json.dumps(memory.run_expression_helper(root,r['id'],{'x':9})))
'''
    result=subprocess.run([sys.executable,'-B','-c',resumed,str(root),str(state)],cwd=Path(memory.__file__).parent,
                          capture_output=True,text=True,timeout=20,check=True)
    assert json.loads(result.stdout)['value']==10
    assert all(path.read_bytes()==raw for path,raw in before.items())  # Never delete arbitrary pending bytes.
    assert not (root/'.local-ai/memory.sqlite3').exists()


def test_helper_atomic_publish_never_replaces_corrupt_destination_and_can_requalify(registered_expression_project,monkeypatch):
    root,_,_=registered_expression_project
    monkeypatch.setattr(memory.time,'time',lambda:123.0)
    record=example_expression_helper(root)
    directory=root/'.local-ai/expression-helpers/v1/records';path=next(directory.iterdir())
    path.write_bytes(b'PRIVATE EXISTING RANDOM BYTES')
    with pytest.raises(OSError):example_expression_helper(root)
    assert path.read_bytes()==b'PRIVATE EXISTING RANDOM BYTES'
    # A new immutable publication may coexist with a corrupt historical file.
    monkeypatch.setattr(memory.time,'time',lambda:124.0)
    requalified=example_expression_helper(root)
    assert requalified['id']==record['id'] and path.read_bytes()==b'PRIVATE EXISTING RANDOM BYTES'
    assert memory.run_expression_helper(root,record['id'],{'amounts':[10],'tax':0})['value']==10


def test_helper_deep_record_rejected_before_json_decode(registered_expression_project,monkeypatch):
    import json
    root,_,_=registered_expression_project
    record=example_expression_helper(root)
    path=next((root/'.local-ai/expression-helpers/v1/records').iterdir())
    path.write_bytes(b'['*100+b'0'+b']'*100)
    monkeypatch.setattr(json,'loads',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Must reject depth before decoding')))
    assert memory.find_expression_helpers(root)['helpers']==[]


def test_helper_does_not_import_persisted_pass_flag(registered_expression_project):
    import json
    root,_,_=registered_expression_project
    record=example_expression_helper(root)
    path=next((root/'.local-ai/expression-helpers/v1/records').iterdir())
    row=json.loads(path.read_bytes());row['passed']=True
    raw=memory._helper_bytes(row);path.unlink()
    (path.parent/(row['id']+'-'+hashlib.sha256(raw).hexdigest()+'.json')).write_bytes(raw)
    assert memory.find_expression_helpers(root)['helpers']==[]
    with pytest.raises(ValueError):memory.run_expression_helper(root,record['id'],{'amounts':[10],'tax':0})


def test_helper_live_owner_change_during_examples_never_publishes(registered_expression_project,monkeypatch):
    import quick_calculator
    root,project,state=registered_expression_project
    original=quick_calculator.run_helper
    def changed(*args):
        value=original(*args)
        with sqlite3.connect(state/'projects.db') as conn:conn.execute('UPDATE projects SET brief=?',('New actual original request',))
        return value
    monkeypatch.setattr(quick_calculator,'run_helper',changed)
    with memory.verification_context(project),pytest.raises(ValueError,match='instructions.*changed'):
        example_expression_helper(root)
    assert memory.find_expression_helpers(root)['helpers']==[]


def test_helper_live_owner_change_during_invocation_discards_value(registered_expression_project,monkeypatch):
    import quick_calculator
    root,project,state=registered_expression_project
    record=example_expression_helper(root);original=quick_calculator.run_helper;calls=[]
    def changed(*args):
        value=original(*args);calls.append(True)
        if len(calls)==3:
            with sqlite3.connect(state/'projects.db') as conn:conn.execute('UPDATE projects SET brief=?',('New actual original request',))
        return value
    monkeypatch.setattr(quick_calculator,'run_helper',changed)
    with memory.verification_context(project),pytest.raises(ValueError,match='instructions.*changed'):
        memory.run_expression_helper(root,record['id'],{'amounts':[10],'tax':0})
    assert len(calls)==3


def test_helper_staging_scan_is_bounded_and_preserves_unknown_crash_bytes(registered_expression_project):
    root,_,_=registered_expression_project
    example_expression_helper(root)
    staging=root/'.local-ai/expression-helpers/v1/staging'
    for number in range(128):(staging/('unknown-'+str(number))).write_bytes(b'partial preserve')
    with pytest.raises(ValueError,match='staging.*128'):
        memory.register_expression_helper(root,'new_helper',{'x':'number'},'x+1',[{'inputs':{'x':1},'expected':2}])
    assert len(list(staging.iterdir()))==128
    assert all(path.read_bytes()==b'partial preserve' for path in staging.iterdir())
    assert len(memory.find_expression_helpers(root)['helpers'])==1


# Independently reviewed adversarial checks use real child processes and TEMP-only state.
@pytest.fixture
def independent_helper_project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    conn = sqlite3.connect(state / "projects.db")
    try:
        conn.execute("create table projects(id text, folder text, brief text)")
        conn.execute("insert into projects values(?,?,?)", ("a" * 12, str(root), "Compute invoice totals accurately"))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(long_tasks, "STATE", state)
    return root, state


def independent_register_helper(root):
    return memory.register_expression_helper(root, "plus_one", {"x": "number"}, "x+1",
                                             [{"inputs": {"x": 1}, "expected": 2}])


def test_concurrent_process_registrations_publish_one_complete_record(independent_helper_project):
    root, state = independent_helper_project
    script = '''import sys,json,time,long_tasks,project_memory as m
from pathlib import Path
long_tasks.STATE=Path(sys.argv[2]);m.time.time=lambda:123.0
while not Path(sys.argv[3]).exists():time.sleep(.01)
print(json.dumps(m.register_expression_helper(Path(sys.argv[1]),'plus_one',{'x':'number'},'x+1',[{'inputs':{'x':1},'expected':2}])))
'''
    gate = state / "start"
    command = [sys.executable, "-B", "-c", script, str(root), str(state), str(gate)]
    children = [subprocess.Popen(command, cwd=Path(memory.__file__).parent,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    gate.touch()
    results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=25)
        assert child.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert results[0]["id"] == results[1]["id"]
    records = list((root / ".local-ai/expression-helpers/v1/records").iterdir())
    assert len(records) == 1
    assert memory.run_expression_helper(root, results[0]["id"], {"x": 9})["value"] == 10


def test_record_handle_prevents_real_concurrent_in_place_write(independent_helper_project):
    root, _ = independent_helper_project
    independent_register_helper(root)
    path = next((root / ".local-ai/expression-helpers/v1/records").iterdir())
    before = path.read_bytes()
    original = os.fstat
    attempted = []
    def raced(fd):
        # _helper_read has already opened its share-read-only handle here.
        result = subprocess.run([sys.executable, "-I", "-c",
                                 "import sys;open(sys.argv[1],'r+b').write(b'X')", str(path)],
                                capture_output=True, text=True, timeout=10)
        attempted.append(result.returncode)
        return original(fd)
    with patch.object(memory.os, "fstat", side_effect=raced):
        assert memory._helper_read(path) == before
    assert attempted and all(code != 0 for code in attempted)
    assert path.read_bytes() == before


def test_valid_record_replacement_during_execution_discards_computed_result(independent_helper_project):
    root, _ = independent_helper_project
    record = independent_register_helper(root)
    path = next((root / ".local-ai/expression-helpers/v1/records").iterdir())
    original = quick_calculator.run_helper
    calls = []
    def raced(*args):
        value = original(*args)
        calls.append(True)
        if len(calls) == 2:  # verified examples, then the actual invocation
            row = json.loads(path.read_bytes())
            row["created"] += 1
            raw = memory._helper_bytes(row)
            replacement = path.parent / (row["id"] + "-" + hashlib.sha256(raw).hexdigest() + ".json")
            path.unlink()
            replacement.write_bytes(raw)
        return value
    with patch.object(quick_calculator, "run_helper", side_effect=raced):
        with pytest.raises(ValueError, match="changed during invocation"):
            memory.run_expression_helper(root, record["id"], {"x": 9})
    assert len(calls) == 2


def test_preexisting_staging_junction_preserves_outside_bytes(independent_helper_project, tmp_path):
    import _winapi
    root, _ = independent_helper_project
    parent = root / ".local-ai/expression-helpers/v1"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_bytes(b"private unknown bytes")
    junction = parent / "staging"
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        with pytest.raises((OSError, ValueError)):
            independent_register_helper(root)
        assert list(outside.iterdir()) == [sentinel]
        assert sentinel.read_bytes() == b"private unknown bytes"
    finally:
        os.rmdir(junction)


def test_large_unknown_record_rejected_before_json_and_preserved(independent_helper_project):
    root, _ = independent_helper_project
    record = independent_register_helper(root)
    path = next((root / ".local-ai/expression-helpers/v1/records").iterdir())
    raw = b"{" + b" " * 250_000
    path.write_bytes(raw)
    # Record decoding must never receive the oversized payload.
    with patch.object(json, "loads", side_effect=AssertionError("oversized bytes reached JSON")):
        with pytest.raises(ValueError, match="byte budget"):
            memory._validated_expression_record(root, path)
    assert path.read_bytes() == raw
    assert not (root / ".local-ai/memory.sqlite3").exists()


@pytest.mark.parametrize("word", ["cafe\u0301marker", "caf\u00e9marker", "x\u0301marker", "na\u00efvemarker"])
def test_memory_query_preserves_combining_mark_words(tmp_path, word):
    note = memory.save(tmp_path, "note", "Unicode source", word)
    assert [row["id"] for row in memory.search(tmp_path, word, kind="note")] == [note["id"]]
    assert memory._query_words("\u0301 " + word + " other_word") == [word, "other", "word"]
