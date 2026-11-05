"""Scanned-PDF fallbacks keep page sampling and provenance explicit."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import file_chat
import scanned_pdf
import toolbox_mcp
import user_file_memory


def test_failed_scan_export_preserves_other_pages_and_closes_resources(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF mocked")
    pages = [Mock(), Mock(), Mock()]
    textpages = [Mock(), Mock(), Mock()]
    for page, textpage, text in zip(pages, textpages, ["Native order", "", ""]):
        page.get_textpage.return_value = textpage
        textpage.get_text_bounded.return_value = text
        page.get_size.return_value = (600, 800)
    images = [Mock(), Mock()]
    bitmaps = [Mock(), Mock()]
    for page, bitmap, image in zip(pages[1:], bitmaps, images):
        page.render.return_value = bitmap
        bitmap.to_pil.return_value = image
    images[0].save.side_effect = OSError("image export failed")
    document = Mock()
    document.__len__ = Mock(return_value=3)
    document.__getitem__ = Mock(side_effect=lambda index: pages[index])
    recognize = Mock(return_value={"description": "Readable scan"})
    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[Page 3; vision transcription]\nReadable scan" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1, 3]
    assert result["scanned_pages"] == [2, 3]
    assert result["has_scans"] is True
    assert result["partial"] is True
    recognize.assert_called_once()
    pages[0].render.assert_not_called()
    for image, bitmap in zip(images, bitmaps):
        image.close.assert_called_once_with()
        bitmap.close.assert_called_once_with()
        assert not Path(image.save.call_args.args[0]).parent.exists()
    for textpage in textpages:
        textpage.close.assert_called_once_with()
    assert [page.close.call_count for page in pages] == [1, 2, 2]
    document.close.assert_called_once_with()


def test_temp_directory_failure_preserves_native_text_and_reports_omission(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "unavailable-temp-storage.pdf"
    pdf.write_bytes(b"%PDF mocked")
    pages = [Mock(), Mock()]
    textpages = [Mock(), Mock()]
    for page, textpage, text in zip(pages, textpages, ["Native order", ""]):
        page.get_textpage.return_value = textpage
        textpage.get_text_bounded.return_value = text
    document = Mock()
    document.__len__ = Mock(return_value=2)
    document.__getitem__ = Mock(side_effect=lambda index: pages[index])
    recognize = Mock()

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }), patch.object(
        scanned_pdf.tempfile, "TemporaryDirectory",
        side_effect=OSError("temporary storage unavailable"),
    ) as temporary_directory:
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1]
    assert result["scanned_pages"] == [2]
    assert result["has_scans"] is True
    assert result["partial"] is True
    temporary_directory.assert_called_once_with(prefix="cale-pdf-")
    recognize.assert_not_called()
    for page, textpage in zip(pages, textpages):
        page.render.assert_not_called()
        textpage.close.assert_called_once_with()
    assert [page.close.call_count for page in pages] == [1, 2]
    document.close.assert_called_once_with()


def test_temp_cleanup_failure_preserves_native_text_and_reports_omission(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "cleanup-failure.pdf"
    pdf.write_bytes(b"%PDF mocked")
    pages = [Mock(), Mock()]
    textpages = [Mock(), Mock()]
    for page, textpage, text in zip(pages, textpages, ["Native order", ""]):
        page.get_textpage.return_value = textpage
        textpage.get_text_bounded.return_value = text
    pages[1].get_size.return_value = (600, 800)
    image = Mock()
    bitmap = Mock()
    bitmap.to_pil.return_value = image
    pages[1].render.return_value = bitmap
    document = Mock()
    document.__len__ = Mock(return_value=2)
    document.__getitem__ = Mock(side_effect=lambda index: pages[index])
    recognize = Mock(return_value={"description": "Transcribed scan"})
    real_temporary_directory = scanned_pdf.tempfile.TemporaryDirectory

    class CleanupFailure:
        def __init__(self, **kwargs):
            self.directory = real_temporary_directory(**kwargs)

        def __enter__(self):
            return self.directory.__enter__()

        def __exit__(self, *args):
            self.directory.__exit__(*args)
            raise OSError("temporary image could not be deleted")

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }), patch.object(scanned_pdf.tempfile, "TemporaryDirectory", CleanupFailure):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1]
    assert result["scanned_pages"] == [2]
    assert result["has_scans"] is True
    assert result["partial"] is True
    recognize.assert_called_once()
    pages[0].render.assert_not_called()
    image.close.assert_called_once_with()
    bitmap.close.assert_called_once_with()
    for textpage in textpages:
        textpage.close.assert_called_once_with()
    assert [page.close.call_count for page in pages] == [1, 2]
    document.close.assert_called_once_with()


def test_native_pdf_does_not_require_vision_worker(tmp_path):
    import sys
    from unittest.mock import Mock

    pdf = tmp_path / "native.pdf"
    pdf.write_bytes(b"%PDF mocked")
    textpage = Mock()
    textpage.get_text_bounded.return_value = "Native invoice 4817"
    page = Mock()
    page.get_textpage.return_value = textpage
    document = Mock()
    document.__len__ = Mock(return_value=1)
    document.__getitem__ = Mock(return_value=page)

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(PdfDocument=Mock(return_value=document)),
        "media_worker": None,
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert result["text"] == (
        "[Selectable PDF text]\n\n"
        "[Page 1; selectable text]\nNative invoice 4817"
    )
    assert result["pages_read"] == [1]
    assert result["scanned_pages"] == []
    assert result["has_scans"] is False
    assert result["partial"] is False
    page.render.assert_not_called()
    textpage.close.assert_called_once_with()
    page.close.assert_called_once_with()
    document.close.assert_called_once_with()


def test_unavailable_vision_worker_preserves_native_text_and_reports_omission(tmp_path):
    import sys
    from unittest.mock import Mock

    pdf = tmp_path / "mixed-without-worker.pdf"
    pdf.write_bytes(b"%PDF mocked")
    native_textpage = Mock()
    native_textpage.get_text_bounded.return_value = "Native order"
    scan_textpage = Mock()
    scan_textpage.get_text_bounded.return_value = ""
    native_page = Mock()
    native_page.get_textpage.return_value = native_textpage
    scan_page = Mock()
    scan_page.get_textpage.return_value = scan_textpage
    document = Mock()
    document.__len__ = Mock(return_value=2)
    document.__getitem__ = Mock(side_effect=lambda index: [native_page, scan_page][index])

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(PdfDocument=Mock(return_value=document)),
        "media_worker": None,
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1]
    assert result["scanned_pages"] == [2]
    assert result["has_scans"] is True
    assert result["partial"] is True
    native_page.render.assert_not_called()
    scan_page.render.assert_not_called()
    native_textpage.close.assert_called_once_with()
    scan_textpage.close.assert_called_once_with()
    native_page.close.assert_called_once_with()
    scan_page.close.assert_called_once_with()
    document.close.assert_called_once_with()


def test_text_layer_error_falls_back_to_vision_and_preserves_native_text(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "damaged-text.pdf"
    pdf.write_bytes(b"%PDF mocked")
    native_textpage = Mock()
    native_textpage.get_text_bounded.return_value = "Order A-1742"
    native_page = Mock()
    native_page.get_textpage.return_value = native_textpage
    damaged_textpage = Mock()
    damaged_textpage.get_text_bounded.side_effect = PdfiumError("unreadable text layer")
    image = Mock()
    bitmap = Mock()
    bitmap.to_pil.return_value = image
    damaged_page = Mock()
    damaged_page.get_textpage.return_value = damaged_textpage
    damaged_page.get_size.return_value = (600, 800)
    damaged_page.render.return_value = bitmap
    document = Mock()
    document.__len__ = Mock(return_value=2)
    document.__getitem__ = Mock(side_effect=lambda index: [native_page, damaged_page][index])
    recognize = Mock(return_value={"description": "INVOICE 4817"})
    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nOrder A-1742" in result["text"]
    assert "[Page 2; vision transcription]\nINVOICE 4817" in result["text"]
    assert result["pages_read"] == [1, 2]
    assert result["scanned_pages"] == [2]
    assert result["has_scans"] is True
    assert result["partial"] is False
    recognize.assert_called_once()
    native_page.render.assert_not_called()
    native_textpage.close.assert_called_once_with()
    damaged_textpage.close.assert_called_once_with()
    native_page.close.assert_called_once_with()
    assert damaged_page.close.call_count == 2
    image.close.assert_called_once_with()
    bitmap.close.assert_called_once_with()
    document.close.assert_called_once_with()


def test_native_pdf_preserves_supplementary_unicode(tmp_path):
    import sys
    from unittest.mock import Mock

    pdf = tmp_path / "unicode.pdf"
    pdf.write_bytes(b"%PDF mocked")
    text = "Name: \U00020000"
    textpage = Mock()
    # Model the range API's UCS-2 limitation versus bounded Unicode extraction.
    textpage.get_text_range.return_value = "Name: "
    textpage.get_text_bounded.return_value = text
    page = Mock()
    page.get_textpage.return_value = textpage
    document = Mock()
    document.__len__ = Mock(return_value=1)
    document.__getitem__ = Mock(return_value=page)

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(PdfDocument=Mock(return_value=document)),
        "media_worker": None,
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert result["text"] == (
        "[Selectable PDF text]\n\n[Page 1; selectable text]\n" + text
    )
    assert result["pages_read"] == [1]
    assert result["scanned_pages"] == []
    textpage.get_text_bounded.assert_called_once_with()
    textpage.get_text_range.assert_not_called()
    page.render.assert_not_called()
    textpage.close.assert_called_once_with()
    page.close.assert_called_once_with()
    document.close.assert_called_once_with()


def test_unrenderable_scan_preserves_other_pages_and_reports_omission(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "broken-scan.pdf"
    pdf.write_bytes(b"%PDF mocked")
    textpages = [Mock(), Mock(), Mock()]
    pages = [Mock(), Mock(), Mock()]
    for page, textpage, text in zip(pages, textpages, ["Native order", "", ""]):
        textpage.get_text_bounded.return_value = text
        page.get_textpage.return_value = textpage
        page.get_size.return_value = (600, 800)
    pages[1].render.side_effect = PdfiumError("cannot render page")
    image = Mock()
    bitmap = Mock()
    bitmap.to_pil.return_value = image
    pages[2].render.return_value = bitmap
    document = Mock()
    document.__len__ = Mock(return_value=3)
    document.__getitem__ = Mock(side_effect=lambda index: pages[index])
    recognize = Mock(return_value={"description": "Readable scan"})

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[Page 3; vision transcription]\nReadable scan" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1, 3]
    assert result["scanned_pages"] == [2, 3]
    assert result["has_scans"] is True
    assert result["partial"] is True
    recognize.assert_called_once()
    pages[0].render.assert_not_called()
    for textpage in textpages:
        textpage.close.assert_called_once_with()
    assert [page.close.call_count for page in pages] == [1, 2, 2]
    image.close.assert_called_once_with()
    bitmap.close.assert_called_once_with()
    document.close.assert_called_once_with()


def test_unloadable_page_preserves_other_pages_and_reports_omission(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "unloadable-page.pdf"
    pdf.write_bytes(b"%PDF mocked")
    native_textpage = Mock()
    native_textpage.get_text_bounded.return_value = "Native order"
    native_page = Mock()
    native_page.get_textpage.return_value = native_textpage
    scan_textpage = Mock()
    scan_textpage.get_text_bounded.return_value = ""
    scan_page = Mock()
    scan_page.get_textpage.return_value = scan_textpage
    scan_page.get_size.return_value = (600, 800)
    image = Mock()
    bitmap = Mock()
    bitmap.to_pil.return_value = image
    scan_page.render.return_value = bitmap

    def load_page(index):
        if index == 1:
            raise PdfiumError("cannot load page")
        return native_page if index == 0 else scan_page

    document = Mock()
    document.__len__ = Mock(return_value=3)
    document.__getitem__ = Mock(side_effect=load_page)
    recognize = Mock(return_value={"description": "Readable scan"})
    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[Page 3; vision transcription]\nReadable scan" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1, 3]
    assert result["scanned_pages"] == [2, 3]
    assert result["has_scans"] is True
    assert result["partial"] is True
    recognize.assert_called_once()
    native_page.render.assert_not_called()
    native_textpage.close.assert_called_once_with()
    scan_textpage.close.assert_called_once_with()
    native_page.close.assert_called_once_with()
    assert scan_page.close.call_count == 2
    image.close.assert_called_once_with()
    bitmap.close.assert_called_once_with()
    document.close.assert_called_once_with()


def test_scan_size_error_preserves_other_pages_and_reports_omission(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "broken-size.pdf"
    pdf.write_bytes(b"%PDF mocked")
    textpages = [Mock(), Mock(), Mock()]
    pages = [Mock(), Mock(), Mock()]
    for page, textpage, text in zip(pages, textpages, ["Native order", "", ""]):
        textpage.get_text_bounded.return_value = text
        page.get_textpage.return_value = textpage
        page.get_size.return_value = (600, 800)
    pages[1].get_size.side_effect = PdfiumError("cannot read page dimensions")
    image = Mock()
    bitmap = Mock()
    bitmap.to_pil.return_value = image
    pages[2].render.return_value = bitmap
    document = Mock()
    document.__len__ = Mock(return_value=3)
    document.__getitem__ = Mock(side_effect=lambda index: pages[index])
    recognize = Mock(return_value={"description": "Readable scan"})

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[Page 3; vision transcription]\nReadable scan" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1, 3]
    assert result["scanned_pages"] == [2, 3]
    assert result["has_scans"] is True
    assert result["partial"] is True
    recognize.assert_called_once()
    pages[0].render.assert_not_called()
    pages[1].render.assert_not_called()
    for textpage in textpages:
        textpage.close.assert_called_once_with()
    assert [page.close.call_count for page in pages] == [1, 2, 2]
    image.close.assert_called_once_with()
    bitmap.close.assert_called_once_with()
    document.close.assert_called_once_with()


def test_zero_sized_scan_preserves_other_pages_and_reports_omission(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "zero-sized-scan.pdf"
    pdf.write_bytes(b"%PDF mocked")
    textpages = [Mock(), Mock(), Mock()]
    pages = [Mock(), Mock(), Mock()]
    for page, textpage, text in zip(pages, textpages, ["Native order", "", ""]):
        textpage.get_text_bounded.return_value = text
        page.get_textpage.return_value = textpage
        page.get_size.return_value = (600, 800)
    pages[1].get_size.return_value = (0, 0)
    image = Mock()
    bitmap = Mock()
    bitmap.to_pil.return_value = image
    pages[2].render.return_value = bitmap
    document = Mock()
    document.__len__ = Mock(return_value=3)
    document.__getitem__ = Mock(side_effect=lambda index: pages[index])
    recognize = Mock(return_value={"description": "Readable scan"})

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[Page 3; vision transcription]\nReadable scan" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1, 3]
    assert result["scanned_pages"] == [2, 3]
    assert result["has_scans"] is True
    assert result["partial"] is True
    recognize.assert_called_once()
    pages[0].render.assert_not_called()
    pages[1].render.assert_not_called()
    for textpage in textpages:
        textpage.close.assert_called_once_with()
    assert [page.close.call_count for page in pages] == [1, 2, 2]
    image.close.assert_called_once_with()
    bitmap.close.assert_called_once_with()
    document.close.assert_called_once_with()


def test_nan_scan_dimensions_preserve_native_text_and_report_omission(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "nan-sized-scan.pdf"
    pdf.write_bytes(b"%PDF mocked")
    native_textpage = Mock()
    native_textpage.get_text_bounded.return_value = "Native order"
    scan_textpage = Mock()
    scan_textpage.get_text_bounded.return_value = ""
    native_page = Mock()
    native_page.get_textpage.return_value = native_textpage
    scan_page = Mock()
    scan_page.get_textpage.return_value = scan_textpage
    scan_page.get_size.return_value = (float("nan"), 800)
    # Rendering non-finite dimensions can fail during numeric conversion.
    scan_page.render.side_effect = ValueError("cannot convert float NaN to integer")
    document = Mock()
    document.__len__ = Mock(return_value=2)
    document.__getitem__ = Mock(side_effect=lambda index: [native_page, scan_page][index])
    recognize = Mock()

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1]
    assert result["scanned_pages"] == [2]
    assert result["has_scans"] is True
    assert result["partial"] is True
    native_page.render.assert_not_called()
    scan_page.render.assert_not_called()
    recognize.assert_not_called()
    native_textpage.close.assert_called_once_with()
    scan_textpage.close.assert_called_once_with()
    native_page.close.assert_called_once_with()
    assert scan_page.close.call_count == 2
    document.close.assert_called_once_with()


def test_failed_transcription_preserves_other_pages_and_reports_omission(tmp_path):
    import sys
    from unittest.mock import Mock

    class PdfiumError(Exception):
        pass

    pdf = tmp_path / "failed-transcription.pdf"
    pdf.write_bytes(b"%PDF mocked")
    pages = [Mock(), Mock(), Mock()]
    textpages = [Mock(), Mock(), Mock()]
    for page, textpage, text in zip(pages, textpages, ["Native order", "", ""]):
        page.get_textpage.return_value = textpage
        textpage.get_text_bounded.return_value = text
        page.get_size.return_value = (600, 800)
    images = [Mock(), Mock()]
    bitmaps = [Mock(), Mock()]
    for page, bitmap, image in zip(pages[1:], bitmaps, images):
        page.render.return_value = bitmap
        bitmap.to_pil.return_value = image
    document = Mock()
    document.__len__ = Mock(return_value=3)
    document.__getitem__ = Mock(side_effect=lambda index: pages[index])
    recognize = Mock(side_effect=[
        RuntimeError("vision inference failed"),
        {"description": "Readable scan"},
    ])

    with patch.dict(sys.modules, {
        "pypdfium2": SimpleNamespace(
            PdfDocument=Mock(return_value=document), PdfiumError=PdfiumError
        ),
        "media_worker": SimpleNamespace(recognize_image=recognize),
    }):
        result = scanned_pdf.read_scanned_pdf(str(pdf))

    assert "[Page 1; selectable text]\nNative order" in result["text"]
    assert "[Page 3; vision transcription]\nReadable scan" in result["text"]
    assert "[1 scanned pages were not read: 2.]" in result["text"]
    assert "[Page 2; vision transcription]" not in result["text"]
    assert result["pages_read"] == [1, 3]
    assert result["scanned_pages"] == [2, 3]
    assert result["has_scans"] is True
    assert result["partial"] is True
    assert recognize.call_count == 2
    pages[0].render.assert_not_called()
    for textpage in textpages:
        textpage.close.assert_called_once_with()
    for image, bitmap in zip(images, bitmaps):
        image.close.assert_called_once_with()
        bitmap.close.assert_called_once_with()
    for call in recognize.call_args_list:
        assert not Path(call.args[0]).parent.exists()
    assert [page.close.call_count for page in pages] == [1, 2, 2]
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
