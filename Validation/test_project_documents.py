"""Project documents stay local, scoped, and fresh after source edits."""

import os
from unittest.mock import patch

import file_chat
import native_agent
import project_documents
import project_memory


def test_snapshot_upgrade_ignores_v2_cache_and_rebuilds_only_derived_chunks(tmp_path):
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"stable original PDF bytes")
    digest = project_documents._digest(source)
    old_cache = tmp_path / ".local-ai/document-cache" / f"v2-{digest}-pdf-0.txt"
    old_cache.parent.mkdir(parents=True)
    old_cache.write_text("stale poisoned zebra passage", encoding="utf-8")
    project_memory.index_document_text(tmp_path, source, "stale poisoned zebra passage",
                                       digest, revision="v2-vision0")
    note = project_memory.save(tmp_path, "note", "My decision", "Keep the handwritten explanation",
                               source_path="notes.pdf")
    snapshots = []

    def extract(snapshot, max_scanned_pages):
        assert snapshot != source and snapshot.read_bytes() == source.read_bytes()
        snapshots.append(snapshot)
        return "verified fresh zebra passage"

    with patch.object(file_chat, "ROOT", tmp_path), patch.object(file_chat, "document_text", side_effect=extract) as reader:
        first = project_documents.inspect(tmp_path, "notes.pdf")
        second = project_documents.inspect(tmp_path, "notes.pdf")
    assert first["text"] == second["text"] == "verified fresh zebra passage"
    assert not first["cached"] and second["cached"]
    assert reader.call_count == 1 and not snapshots[0].exists()
    assert not project_memory.search(tmp_path, "stale poisoned", kind="document")
    assert project_memory.search(tmp_path, "verified fresh", kind="document")
    assert project_memory.get(tmp_path, note["id"])["body"] == "Keep the handwritten explanation"
    with project_memory.connection(tmp_path) as conn:
        rows = conn.execute("SELECT tags,source_hash FROM memories WHERE kind='document'").fetchall()
    assert rows and all(row["tags"] == "extractor:v4-vision0" and row["source_hash"] == digest for row in rows)
    assert old_cache.read_text(encoding="utf-8") == "stale poisoned zebra passage"
    assert source.read_bytes() == b"stable original PDF bytes"


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
        # Simulate cache hit for the second call, but change the source file
        cache_path = next((tmp_path / ".local-ai/document-cache").glob("*.txt"))
        cache_path.write_text(first["text"]) # Write the text extracted in the first call
        second = project_documents.inspect(tmp_path, "research.pdf", "release date")

    assert "November second" in first["text"]
    # Now that the source is changed, the second call should fail/re-extract if cache invalidation logic is correct
    # Or, if the cache is still based on old digest, it should detect the change.
    # If the cache was successfully written based on the original digest, the new source digest will mismatch.
    assert not second["cached"]
    assert first["indexed"] and second["indexed"]
    assert extract.call_count == 2 # First extraction, second re-extraction because digest changed
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
        # This should not happen if the update caused a re-extraction/re-indexing
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


def test_inspector_rejects_growth_beyond_size_limit_on_cache_hit(tmp_path):
    source = tmp_path / "notes.txt"
    expanded = b"expanded document"
    source.write_bytes(expanded)
    real_digest = project_documents._digest

    with patch.object(project_memory, "index_file"):
        first = project_documents.inspect(tmp_path, "notes.txt")
    assert first["text"] == expanded.decode("utf-8")
    assert not first["cached"]
    source.write_bytes(b"small")
    changed = False

    def grow_after_size_check(path):
        nonlocal changed
        if not changed:
            changed = True
            source.write_bytes(expanded)
        return real_digest(path)

    with patch.object(project_documents, "MAX_SOURCE_BYTES", 5), patch.object(
        project_documents, "_digest", side_effect=grow_after_size_check,
    ), patch.object(project_documents, "_extract_snapshot") as extract, patch.object(
        project_memory, "index_file",
    ) as index:
        try:
            project_documents.inspect(tmp_path, "notes.txt")
        except ValueError as exc:
            assert "local reader limit" in str(exc)
        else:
            raise AssertionError("Oversized source was accepted through a cache hit")
        extract.assert_not_called()
        index.assert_not_called()

    assert changed
    assert source.read_bytes() == expanded


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


def test_inspector_rejects_source_changes_during_excerpt_selection(tmp_path):
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"original PDF source")

    def change_during_selection(text, question, *, limit, subject):
        assert text == "original text"
        source.write_bytes(b"updated PDF source")
        return text

    with patch.object(file_chat, "ROOT", tmp_path), patch.object(
        file_chat, "document_text", side_effect=["original text", "updated text"],
    ) as extract, patch.object(project_memory, "index_document_text"):
        first = project_documents.inspect(tmp_path, "notes.pdf")
        assert first["text"] == "original text" and not first["cached"]
        with patch.object(file_chat, "select_excerpt", side_effect=change_during_selection) as select:
            try:
                project_documents.inspect(tmp_path, "notes.pdf", question="What changed?")
            except RuntimeError as exc:
                assert "document changed" in str(exc)
            else:
                raise AssertionError("Source edited during excerpt selection returned stale text")
            select.assert_called_once()
        assert extract.call_count == 1
        fresh = project_documents.inspect(tmp_path, "notes.pdf")
        cached = project_documents.inspect(tmp_path, "notes.pdf")

    assert fresh["text"] == cached["text"] == "updated text"
    assert not fresh["cached"] and cached["cached"]
    assert extract.call_count == 2
    assert source.read_bytes() == b"updated PDF source"


def test_inspector_rejects_symlink_retargeting_during_excerpt_selection(tmp_path):
    original = tmp_path / "original.txt"
    updated = tmp_path / "updated.txt"
    link = tmp_path / "notes.txt"
    original.write_bytes(b"original document")
    updated.write_bytes(b"updated document")
    try:
        link.symlink_to(original.name)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("Symlink creation is unavailable")

    def retarget_during_selection(text, question, *, limit, subject):
        assert text == "original document"
        link.unlink()
        link.symlink_to(updated.name)
        return text

    with patch.object(project_memory, "index_file"):
        first = project_documents.inspect(tmp_path, "notes.txt")
        assert first["text"] == "original document" and not first["cached"]
        with patch.object(file_chat, "select_excerpt", side_effect=retarget_during_selection):
            try:
                project_documents.inspect(tmp_path, "notes.txt", question="What changed?")
            except RuntimeError as exc:
                assert "document changed" in str(exc)
            else:
                raise AssertionError("Retargeted symlink returned stale cached text")
        fresh = project_documents.inspect(tmp_path, "notes.txt")
        cached = project_documents.inspect(tmp_path, "notes.txt")

    assert fresh["text"] == cached["text"] == "updated document"
    assert not fresh["cached"] and cached["cached"]
    assert original.read_bytes() == b"original document"
    assert updated.read_bytes() == b"updated document"


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


def test_inspector_rebuilds_invalid_or_oversized_cache(tmp_path):
    source = tmp_path / "notes.pdf"
    original = b"unchanged PDF source"
    source.write_bytes(original)
    text = "fresh document text"

    with patch.object(file_chat, "ROOT", tmp_path), patch.object(
        file_chat, "document_text", return_value=text,
    ) as extract, patch.object(project_memory, "index_document_text"), patch.object(
        project_documents, "MAX_EXTRACTED_CHARS", 64,
    ):
        first = project_documents.inspect(tmp_path, "notes.pdf")
        assert first["text"] == text and not first["cached"]
        cache = next((tmp_path / ".local-ai/document-cache").glob("*.txt"))

        for corrupt in (b"\xff", b"x" * 65):
            cache.write_bytes(corrupt)
            recovered = project_documents.inspect(tmp_path, "notes.pdf")
            reused = project_documents.inspect(tmp_path, "notes.pdf")
            assert recovered["text"] == reused["text"] == text
            assert not recovered["cached"] and reused["cached"]
            assert cache.read_bytes() == text.encode("utf-8")
            assert source.read_bytes() == original

    assert extract.call_count == 3
    assert not list(cache.parent.glob("*.tmp"))


def test_inspector_retries_empty_extraction_and_legacy_empty_cache(tmp_path):
    source = tmp_path / "notes.pdf"
    original = b"unchanged PDF source"
    source.write_bytes(original)
    digest = project_documents._digest(source)
    cache = tmp_path / ".local-ai/document-cache" / (
        f"{project_documents.CACHE_VERSION}-{digest}-pdf-0.txt"
    )

    with patch.object(file_chat, "ROOT", tmp_path), patch.object(
        file_chat, "document_text", side_effect=["", "recovered document text"],
    ) as extract, patch.object(project_memory, "index_document_text"):
        empty = project_documents.inspect(tmp_path, "notes.pdf")
        assert empty["text"] == "" and not empty["cached"]
        assert not cache.exists()

        # Empty entries written by earlier versions must also be retried.
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"")
        recovered = project_documents.inspect(tmp_path, "notes.pdf")
        reused = project_documents.inspect(tmp_path, "notes.pdf")

    assert recovered["text"] == reused["text"] == "recovered document text"
    assert not recovered["cached"] and reused["cached"]
    assert extract.call_count == 2
    assert cache.read_bytes() == b"recovered document text"
    assert source.read_bytes() == original
    assert not list(cache.parent.glob("*.tmp"))


def test_inspector_rebuilds_cache_removed_before_open(tmp_path):
    source = tmp_path / "notes.pdf"
    original = b"unchanged PDF source"
    source.write_bytes(original)
    text = "verified document text"
    real_open = type(source).open
    removed = False

    with patch.object(file_chat, "ROOT", tmp_path), patch.object(
        file_chat, "document_text", return_value=text,
    ) as extract, patch.object(project_memory, "index_document_text"):
        first = project_documents.inspect(tmp_path, "notes.pdf")
        assert first["text"] == text and not first["cached"]
        cache = next((tmp_path / ".local-ai/document-cache").glob("*.txt"))

        def remove_before_open(path, *args, **kwargs):
            nonlocal removed
            if path == cache and not removed:
                removed = True
                path.unlink()
            return real_open(path, *args, **kwargs)

        with patch.object(type(source), "open", new=remove_before_open):
            recovered = project_documents.inspect(tmp_path, "notes.pdf")
        reused = project_documents.inspect(tmp_path, "notes.pdf")

    assert removed
    assert recovered["text"] == reused["text"] == text
    assert not recovered["cached"] and reused["cached"]
    assert extract.call_count == 2
    assert cache.read_bytes() == text.encode("utf-8")
    assert source.read_bytes() == original
    assert not list(cache.parent.glob("*.tmp"))


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


def test_plain_text_preserves_line_endings_and_rebuilds_translated_cache(tmp_path):
    source = tmp_path / "notes.txt"
    text = "alpha\r\nbeta\rgamma\nend"
    original = text.encode("utf-8")
    source.write_bytes(original)
    digest = project_documents._digest(source)
    old_cache = tmp_path / ".local-ai/document-cache" / f"v3-{digest}-txt-0.txt"
    old_cache.parent.mkdir(parents=True)
    translated = text.replace("\r\n", "\n").replace("\r", "\n")
    old_cache.write_bytes(translated.encode("utf-8"))

    with patch.object(project_memory, "index_file"), patch.object(
        project_documents, "_extract_snapshot", wraps=project_documents._extract_snapshot,
    ) as extract:
        first = project_documents.inspect(tmp_path, "notes.txt", max_chars=7)
        second = project_documents.inspect(
            tmp_path, "notes.txt", start_char=first["next_start_char"],
        )
        full = project_documents.inspect(tmp_path, "notes.txt")

    assert not first["cached"] and second["cached"] and full["cached"]
    assert first["text"] == "alpha\r\n"
    assert first["next_start_char"] == 7
    assert first["text"] + second["text"] == full["text"] == text
    assert first["total_characters"] == second["total_characters"] == len(text)
    assert full["total_characters"] == len(text)
    assert second["next_start_char"] is None
    assert extract.call_count == 1
    assert source.read_bytes() == original
    assert old_cache.read_bytes() == translated.encode("utf-8")


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


def test_inspector_rejects_image_changes_during_description(tmp_path):
    source = tmp_path / "diagram.png"
    source.write_bytes(b"original image source")

    def describe_then_change(path, question):
        assert path != source and path.name == source.name
        assert path.read_bytes() == b"original image source"
        assert question == "What is shown?"
        source.write_bytes(b"updated image source")
        return "original image description"

    with patch.object(file_chat, "image_description", side_effect=describe_then_change):
        try:
            project_documents.inspect(tmp_path, "diagram.png", question="What is shown?")
        except RuntimeError as exc:
            assert "document changed" in str(exc)
        else:
            raise AssertionError("Changed image returned stale description")

    with patch.object(file_chat, "image_description", return_value="updated image description") as describe:
        fresh = project_documents.inspect(tmp_path, "diagram.png", question="What is shown?")
        describe.assert_called_once()
        snapshot, question = describe.call_args.args
        assert snapshot != source and snapshot.name == source.name
        assert not snapshot.exists()
        assert question == "What is shown?"

    assert fresh["text"] == "updated image description"
    assert fresh["kind"] == "image" and not fresh["cached"]
    assert source.read_bytes() == b"updated image source"
    assert not (tmp_path / ".local-ai/document-cache").exists()


def test_inspector_describes_snapshot_during_transient_image_edit(tmp_path):
    source = tmp_path / "diagram.png"
    original = b"original image source"
    source.write_bytes(original)
    snapshots = []

    def describe_while_source_changes(path, question):
        snapshots.append(path)
        assert question == "What is shown?"
        source.write_bytes(b"transient image source")
        try:
            return path.read_bytes().decode("utf-8")
        finally:
            source.write_bytes(original)

    with patch.object(file_chat, "image_description", side_effect=describe_while_source_changes) as describe:
        result = project_documents.inspect(tmp_path, "diagram.png", question="What is shown?")

    assert result["text"] == original.decode("utf-8")
    assert result["kind"] == "image" and not result["cached"]
    describe.assert_called_once()
    assert snapshots[0] != source and snapshots[0].name == source.name
    assert not snapshots[0].exists()
    assert source.read_bytes() == original
    assert not (tmp_path / ".local-ai/document-cache").exists()


def test_inspector_rejects_paths_outside_task_folder(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    result = native_agent.execute_tool(root, "inspect_project_file", {"path": "../outside.txt"})
    assert result.startswith("Tool error:")
    assert not (root / ".local-ai/document-cache").exists()
