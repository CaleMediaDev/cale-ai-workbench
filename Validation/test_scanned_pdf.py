"""Scanned-PDF fallbacks keep page sampling and provenance explicit."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import file_chat
import scanned_pdf
import toolbox_mcp
import user_file_memory


def test_failed_scan_export_closes_image_resources(tmp_path):
    import sys
    from unittest.mock import Mock

    import pytest

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF mocked")
    textpage = Mock()
    textpage.get_text_range.return_value = ""
    image = Mock()
    image.save.side_effect = OSError("image export failed")
    bitmap = Mock()
    bitmap.to_pil.return_value = image
    page = Mock()
    page.get_textpage.return_value = textpage
    page.get_size.return_value = (600, 800)
    page.render.return_value = bitmap
    document = Mock()
    document.__len__ = Mock(return_value=1)
    document.__getitem__ = Mock(return_value=page)
    recognize = Mock()
    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(PdfDocument=Mock(return_value=document)),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        with pytest.raises(OSError, match="image export failed"):
            scanned_pdf.read_scanned_pdf(str(pdf))

    image.close.assert_called_once_with()
    bitmap.close.assert_called_once_with()
    textpage.close.assert_called_once_with()
    assert page.close.call_count == 2
    document.close.assert_called_once_with()
    recognize.assert_not_called()


def test_zero_scan_budget_preserves_native_text_without_transcription(tmp_path):
    import sys
    from unittest.mock import Mock

    pdf = tmp_path / "mixed.pdf"
    pdf.write_bytes(b"%PDF mocked")
    native_textpage = Mock()
    native_textpage.get_text_range.return_value = "Order A-1742"
    scanned_textpage = Mock()
    scanned_textpage.get_text_range.return_value = ""
    native_page = Mock()
    native_page.get_textpage.return_value = native_textpage
    scanned_page = Mock()
    scanned_page.get_textpage.return_value = scanned_textpage
    document = Mock()
    document.__len__ = Mock(return_value=2)
    document.__getitem__ = Mock(side_effect=[native_page, scanned_page])
    recognize = Mock()

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(PdfDocument=Mock(return_value=document)),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf), max_pages=0)

    assert scanned_pdf.sampled_pages(2, 0) == []
    assert scanned_pdf.sampled_pages(2, -1) == []
    assert result["pages_read"] == [1]
    assert result["scanned_pages"] == [2]
    assert result["has_scans"] is True
    assert result["partial"] is True
    assert "[Page 1; selectable text]\nOrder A-1742" in result["text"]
    assert "1 scanned pages were not read: 2." in result["text"]
    native_page.render.assert_not_called()
    scanned_page.render.assert_not_called()
    recognize.assert_not_called()
    native_textpage.close.assert_called_once_with()
    scanned_textpage.close.assert_called_once_with()
    native_page.close.assert_called_once_with()
    scanned_page.close.assert_called_once_with()
    document.close.assert_called_once_with()


def test_page_selection_spans_long_document():
    assert scanned_pdf.sampled_pages(1, 6) == [0]
    assert scanned_pdf.sampled_pages(10, 4) == [0, 3, 6, 9]
    assert scanned_pdf.sampled_pages(30, 50)[-1] == 29


def test_empty_selectable_pdf_uses_local_scan_reader(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF fake for mocked extractor")
    runtime = tmp_path / "Media/python/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"")
    payload = {"has_scans": True, "text": "[Unverified transcription]\n[Page 1]\nINVOICE 4817"}
    process = SimpleNamespace(stdout=json.dumps(payload) + "\n", returncode=0)
    with patch.object(file_chat, "ROOT", tmp_path), patch(
        "markitdown.MarkItDown"
    ) as converter, patch.object(file_chat.subprocess, "run", return_value=process) as run:
        converter.return_value.convert.return_value.text_content = ""
        assert "INVOICE 4817" in file_chat.document_text(pdf)
    request = json.loads(run.call_args.kwargs["input"])
    assert request == {"action": "read_scanned_pdf", "path": str(pdf), "max_pages": 6}


def test_text_pdf_keeps_existing_extraction_when_no_scans(tmp_path):
    pdf = tmp_path / "ordinary.pdf"
    runtime = tmp_path / "Media/python/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"")
    process = SimpleNamespace(
        stdout=json.dumps({"has_scans": False, "text": "PDFium text"}) + "\n", returncode=0
    )
    with patch.object(file_chat, "ROOT", tmp_path), patch(
        "markitdown.MarkItDown"
    ) as converter, patch.object(file_chat.subprocess, "run", return_value=process) as run:
        converter.return_value.convert.return_value.text_content = "Selectable text"
        assert file_chat.document_text(pdf) == "Selectable text"
    run.assert_called_once()


def test_mixed_pdf_keeps_native_text_and_reads_scanned_page(tmp_path):
    pdf = tmp_path / "mixed.pdf"
    runtime = tmp_path / "Media/python/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"")
    combined = "[Page 1; selectable text]\nOrder A-1742\n[Page 2; vision transcription]\nINVOICE 4817"
    process = SimpleNamespace(
        stdout=json.dumps({"has_scans": True, "text": combined}) + "\n", returncode=0
    )
    with patch.object(file_chat, "ROOT", tmp_path), patch(
        "markitdown.MarkItDown"
    ) as converter, patch.object(file_chat.subprocess, "run", return_value=process):
        converter.return_value.convert.return_value.text_content = "Order A-1742"
        assert file_chat.document_text(pdf) == combined


def test_scanned_pdf_is_indexed_and_agent_tool_uses_same_fallback(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"scan")
    text = "[Unverified local vision transcription]\n[Page 1]\nGarden invoice 4817"
    with patch.object(file_chat, "document_text", return_value=text) as reader:
        assert "Garden invoice" in user_file_memory._text(pdf)
        reader.assert_called_with(pdf, max_scanned_pages=12)
        with patch.object(toolbox_mcp, "BOX", tmp_path):
            result = toolbox_mcp.document_to_text(str(pdf))
    assert result["text"] == text
    assert Path(result["full_text_file"]).read_text(encoding="utf-8") == text
