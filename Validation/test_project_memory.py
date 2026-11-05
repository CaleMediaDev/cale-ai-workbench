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
