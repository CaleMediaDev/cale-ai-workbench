"""Stale document recall must not erase or hide a concurrently refreshed version."""

import hashlib
from contextlib import contextmanager

import pytest

import project_memory as memory


def document_versions(root):
    source = root / "evidence.pdf"
    source.write_bytes(b"original synthetic document")
    memory.index_document_text(root, source, "old zebra evidence",
                               hashlib.sha256(source.read_bytes()).hexdigest(), revision="v3-vision0")
    source.write_bytes(b"updated synthetic document")
    return source, hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize("publication", ["before_lookup", "before_delete"])
def test_recall_preserves_new_document_index_published_before_stale_cleanup(tmp_path, monkeypatch, publication):
    source, digest = document_versions(tmp_path)
    original_connection = memory.connection
    calls = 0

    @contextmanager
    def concurrent_refresh(folder):
        nonlocal calls
        calls += 1
        cleanup = calls == 2

        def publish():
            memory.index_document_text(tmp_path, source, "new zebra evidence", digest, revision="v3-vision0")

        if cleanup and publication == "before_lookup":
            publish()
        with original_connection(folder) as conn:
            class PublishBeforeDelete:
                def execute(self, statement, parameters=()):
                    if statement.startswith("DELETE FROM memories WHERE id=?"):
                        publish()
                    return conn.execute(statement, parameters)

            yield PublishBeforeDelete() if cleanup and publication == "before_delete" else conn

    monkeypatch.setattr(memory, "connection", concurrent_refresh)
    result = memory.get(tmp_path, "document:evidence.pdf:1")
    assert result["body"] == "new zebra evidence"
    assert result["source_hash"] == digest
    assert memory.search(tmp_path, "zebra", kind="document")[0]["source_path"] == "evidence.pdf"


def test_search_revalidates_a_new_document_version_after_stale_hit(tmp_path, monkeypatch):
    source, digest = document_versions(tmp_path)
    original_refresh = memory._refresh_file_record
    published = False

    def concurrent_refresh(root, row, checked_documents=None):
        nonlocal published
        changed = original_refresh(root, row, checked_documents)
        if changed and not published:
            published = True
            memory.index_document_text(tmp_path, source, "new zebra evidence", digest, revision="v3-vision0")
        return changed

    monkeypatch.setattr(memory, "_refresh_file_record", concurrent_refresh)
    results = memory.search(tmp_path, "zebra", kind="document")
    assert published
    assert len(results) == 1
    assert "new" in results[0]["excerpt"]
