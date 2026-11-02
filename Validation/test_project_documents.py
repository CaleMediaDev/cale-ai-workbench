"""Project documents stay local, scoped, and fresh after source edits."""

import os
from pathlib import Path
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


def test_inspector_rejects_paths_outside_task_folder(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    result = native_agent.execute_tool(root, "inspect_project_file", {"path": "../outside.txt"})
    assert result.startswith("Tool error:")
    assert not (root / ".local-ai/document-cache").exists()
