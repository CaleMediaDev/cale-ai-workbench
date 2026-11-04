"""Project documents stay local, scoped, and fresh after source edits."""

import os
from unittest.mock import patch

import file_chat
import native_agent
import project_documents
import project_memory


def test_inspector_reads_real_pdf_and_reuses_the_extraction(tmp_path):
    source = tmp_path / "research.pdf"
    stream = b"BT /F1 12 Tf 72 720 Td (The release date is November second.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n".encode())
    source.write_bytes(data)
    # A text PDF needs no vision runtime. The first call exercises the real
    # installed MarkItDown extractor; the second must read its local cache.
    with patch.object(file_chat, "ROOT", tmp_path), patch.object(
        file_chat, "document_text", wraps=file_chat.document_text,
    ) as extract:
        first = project_documents.inspect(tmp_path, "research.pdf")
        second = project_documents.inspect(tmp_path, "research.pdf", "release date")
    assert "November second" in first["text"]
    assert "November second" in second["text"]
    assert not first["cached"] and second["cached"]
    assert first["indexed"] and second["indexed"]
    assert extract.call_count == 1
    found = project_memory.search(tmp_path, "November second", kind="document")
    assert len(found) == 1 and found[0]["source_path"] == "research.pdf"
    identifier = found[0]["id"]
    before = source.stat()
    source.write_bytes(source.read_bytes().replace(b"November", b"December"))
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    try:
        project_memory.get(tmp_path, identifier)
    except ValueError as exc:
        assert "source file was removed" in str(exc)
    else:
        raise AssertionError("Edited PDF returned stale indexed text")
    assert not project_memory.search(tmp_path, "November second", kind="document")
    refreshed = project_documents.inspect(tmp_path, "research.pdf")
    assert "December second" in refreshed["text"] and refreshed["indexed"]
    assert project_memory.search(tmp_path, "December second", kind="document")


def test_inspector_paginates_and_invalidates_changed_source(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("alpha beta gamma", encoding="utf-8")
    first = project_documents.inspect(tmp_path, "notes.txt", max_chars=6)
    second = project_documents.inspect(tmp_path, "notes.txt", start_char=6, max_chars=6)
    assert first["text"] == "alpha " and first["next_start_char"] == 6
    assert second["text"] == "beta g" and second["cached"]
    source.write_text("updated beta gamma", encoding="utf-8")
    fresh = project_documents.inspect(tmp_path, "notes.txt", max_chars=7)
    assert fresh["text"] == "updated" and not fresh["cached"]


def test_inspector_rejects_source_changes_during_cache_hits(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("original document", encoding="utf-8")
    real_digest = project_documents._digest
    changed = False

    def change_after_hash(path):
        nonlocal changed
        digest = real_digest(path)
        if not changed:
            changed = True
            source.write_text("updated document", encoding="utf-8")
        return digest

    with patch.object(project_memory, "index_file") as index:
        first = project_documents.inspect(tmp_path, "notes.txt")
        assert first["text"] == "original document" and not first["cached"]
        index.reset_mock()
        with patch.object(project_documents, "_digest", side_effect=change_after_hash):
            try:
                project_documents.inspect(tmp_path, "notes.txt")
            except RuntimeError as exc:
                assert "document changed" in str(exc)
            else:
                raise AssertionError("Changed source returned stale cached text")
        index.assert_not_called()
        fresh = project_documents.inspect(tmp_path, "notes.txt")
        cached = project_documents.inspect(tmp_path, "notes.txt")

    assert changed
    assert fresh["text"] == cached["text"] == "updated document"
    assert not fresh["cached"] and cached["cached"]
    assert source.read_text(encoding="utf-8") == "updated document"


def test_inspector_rejects_source_changes_during_indexing(tmp_path):
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"original PDF source")

    def change_during_indexing(*args, **kwargs):
        source.write_bytes(b"updated PDF source")

    with patch.object(file_chat, "ROOT", tmp_path), patch.object(
        file_chat, "document_text", side_effect=["original text", "updated text"],
    ) as extract, patch.object(project_memory, "index_document_text") as index:
        first = project_documents.inspect(tmp_path, "notes.pdf")
        assert first["text"] == "original text" and not first["cached"]
        index.side_effect = change_during_indexing
        try:
            project_documents.inspect(tmp_path, "notes.pdf")
        except RuntimeError as exc:
            assert "document changed" in str(exc)
        else:
            raise AssertionError("Source edited during indexing returned stale text")
        assert extract.call_count == 1
        index.side_effect = None
        fresh = project_documents.inspect(tmp_path, "notes.pdf")
        cached = project_documents.inspect(tmp_path, "notes.pdf")

    assert fresh["text"] == cached["text"] == "updated text"
    assert not fresh["cached"] and cached["cached"]
    assert extract.call_count == 2
    assert source.read_bytes() == b"updated PDF source"


def test_inspector_does_not_cache_transient_source_text_under_original_hash(tmp_path):
    source = tmp_path / "notes.pdf"
    original = b"original PDF source"
    source.write_bytes(original)
    extraction_paths = []

    def extract_while_source_changes(path, max_scanned_pages):
        extraction_paths.append(path)
        assert max_scanned_pages == 20
        source.write_bytes(b"transient PDF source")
        try:
            return path.read_bytes().decode("utf-8")
        finally:
            source.write_bytes(original)

    with patch.object(file_chat, "ROOT", tmp_path), patch.object(
        file_chat, "document_text", side_effect=extract_while_source_changes,
    ) as extract, patch.object(project_memory, "index_document_text") as index:
        first = project_documents.inspect(tmp_path, "notes.pdf")
        cached = project_documents.inspect(tmp_path, "notes.pdf")

    assert first["text"] == cached["text"] == original.decode("utf-8")
    assert not first["cached"] and cached["cached"]
    assert extract.call_count == 1
    assert all(call.args[2] == original.decode("utf-8") for call in index.call_args_list)
    assert source.read_bytes() == original
    assert extraction_paths[0].name == source.name
    assert not extraction_paths[0].exists()


def test_inspector_rejects_source_changes_during_cache_publication(tmp_path):
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"original PDF source")
    real_replace = os.replace

    def publish_then_change_source(temporary, cache):
        result = real_replace(temporary, cache)
        source.write_bytes(b"updated PDF source")
        return result

    with patch.object(file_chat, "ROOT", tmp_path), patch.object(
        file_chat, "document_text", side_effect=["original text", "updated text"],
    ) as extract, patch.object(project_memory, "index_document_text") as index:
        with patch.object(project_documents.os, "replace", side_effect=publish_then_change_source):
            try:
                project_documents.inspect(tmp_path, "notes.pdf")
            except RuntimeError as exc:
                assert "document changed" in str(exc)
            else:
                raise AssertionError("Changed source returned stale extracted text")
        index.assert_not_called()
        fresh = project_documents.inspect(tmp_path, "notes.pdf")
        cached = project_documents.inspect(tmp_path, "notes.pdf")

    assert fresh["text"] == cached["text"] == "updated text"
    assert not fresh["cached"] and cached["cached"]
    assert extract.call_count == 2
    assert source.read_bytes() == b"updated PDF source"
    assert not list((tmp_path / ".local-ai/document-cache").glob("*.tmp"))


def test_inspector_handles_overlapping_cache_publications(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("shared document text", encoding="utf-8")
    real_replace = os.replace
    interleaved = False
    nested_results = []

    def publish_after_another_inspection(temporary, cache):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            # Publish another inspection after the first has written its
            # temporary file, but before the first moves it into the cache.
            nested_results.append(project_documents.inspect(tmp_path, "notes.txt"))
        return real_replace(temporary, cache)

    with patch.object(project_documents.os, "replace", side_effect=publish_after_another_inspection), patch.object(
        project_memory, "index_file", return_value=None,
    ):
        first = project_documents.inspect(tmp_path, "notes.txt")
        cached = project_documents.inspect(tmp_path, "notes.txt")

    assert interleaved
    assert first["text"] == nested_results[0]["text"] == cached["text"] == "shared document text"
    assert not first["cached"] and not nested_results[0]["cached"]
    assert cached["cached"]
    assert not list((tmp_path / ".local-ai/document-cache").glob("*.tmp"))


def test_inspector_preserves_extracted_line_endings_in_cache(tmp_path):
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"placeholder PDF source")
    text = "alpha\r\nbeta\rgamma\nend"
    with patch.object(file_chat, "ROOT", tmp_path), patch.object(
        file_chat, "document_text", return_value=text,
    ) as extract, patch.object(project_memory, "index_document_text"):
        first = project_documents.inspect(tmp_path, "notes.pdf", max_chars=7)
        second = project_documents.inspect(
            tmp_path, "notes.pdf", start_char=first["next_start_char"],
        )
        full = project_documents.inspect(tmp_path, "notes.pdf")

    assert not first["cached"] and second["cached"] and full["cached"]
    assert first["next_start_char"] == 7
    assert first["text"] + second["text"] == text
    assert full["text"] == text
    assert first["total_characters"] == second["total_characters"] == len(text)
    assert full["total_characters"] == len(text)
    assert second["next_start_char"] is None
    assert extract.call_count == 1


def test_old_translated_cache_is_not_reused_after_newline_fix(tmp_path):
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"PDF fixture")
    text = "alpha\r\nbeta\rgamma\nend"
    with patch.object(file_chat, "document_text", return_value=text) as extract, patch.object(project_memory, "index_document_text"):
        with patch.object(project_documents, "CACHE_VERSION", "v1"):
            project_documents.inspect(tmp_path, "notes.pdf")
        old = next((tmp_path / ".local-ai/document-cache").glob("v1-*.txt"))
        old.write_bytes(text.replace("\n", "\r\n").encode())
        fresh = project_documents.inspect(tmp_path, "notes.pdf")
    assert not fresh["cached"] and fresh["text"] == text
    assert extract.call_count == 2
    assert old.is_file()


def test_inspector_rejects_paths_outside_task_folder(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    result = native_agent.execute_tool(root, "inspect_project_file", {"path": "../outside.txt"})
    assert result.startswith("Tool error:")
    assert not (root / ".local-ai/document-cache").exists()
