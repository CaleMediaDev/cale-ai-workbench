"""Selected files answer small questions locally without creating tasks."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import chat_assistant
import file_chat


def test_small_file_question_routes_to_chat_and_build_work_to_task(tmp_path):
    report = tmp_path / "report.txt"
    report.write_text("Revenue increased in June.", encoding="utf-8")
    paths = [str(report)]
    assert chat_assistant.route_with_files("What's in this file?", [], paths) == "file_answer"
    assert chat_assistant.route_with_files("Summarize this file", [], paths) == "file_answer"
    assert chat_assistant.route_with_files("Build an app from this file", [], paths) == "task"
    assert chat_assistant.route_with_files("What's the status?", [], paths) == "status"
    assert chat_assistant.route_with_files("What's in this folder?", [], [str(tmp_path)]) == "task"


def test_file_answer_uses_read_only_local_excerpt(tmp_path):
    report = tmp_path / "report.txt"
    report.write_text("Revenue increased in June.", encoding="utf-8")
    result = {"choices": [{"message": {"content": "Revenue rose in June [1]."}}]}
    with patch("cloud_ai.post", return_value=result) as post:
        answer = file_chat.answer_from_files("What changed?", [str(report)])
    assert "Revenue rose in June" in answer
    assert "[1] report.txt" in answer
    assert post.call_args.args[0].startswith("http://127.0.0.1:11434/")
    payload = post.call_args.args[1]
    assert "Revenue increased in June" in payload["messages"][1]["content"]
    assert "untrusted data" in payload["messages"][0]["content"]
    assert payload["reasoning_effort"] == "none"


@pytest.mark.parametrize("malformed", [
    {"choices": None},
    {"choices": [{"message": None}]},
    {"choices": [{"message": []}]},
    {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": 123}}]},
])
def test_null_response_fields_fall_back_to_next_local_model(tmp_path, malformed):
    report = tmp_path / "report.txt"
    report.write_text("Revenue increased in June.", encoding="utf-8")
    valid = {"choices": [{"message": {"content": "Revenue rose in June [1]."}}]}
    with (
        patch("cloud_ai.FAST_MODEL", "first-local-model"),
        patch("cloud_ai.LOCAL_MODEL", "second-local-model"),
        patch("cloud_ai.post", side_effect=[malformed, valid]) as post,
        patch("cloud_ai.retry_derestricted", side_effect=lambda payload, result, model: result),
    ):
        answer = file_chat.answer_from_files("What changed?", [str(report)])
    assert "Revenue rose in June [1]." in answer
    assert "Files read: [1] report.txt" in answer
    assert [call.args[1]["model"] for call in post.call_args_list] == [
        "first-local-model", "second-local-model",
    ]


def test_image_question_uses_local_vision_description(tmp_path):
    image = tmp_path / "garden.png"
    image.write_bytes(b"fake image for mocked vision")
    with (
        patch.object(file_chat, "image_description", return_value="A garden with flowers.") as see,
        patch("cloud_ai.post", return_value={"choices": [{"message": {"content": "Flowers in a garden [1]."}}]}),
    ):
        answer = file_chat.answer_from_files("What is shown?", [str(image)])
    see.assert_called_once_with(Path(image), "What is shown?")
    assert "Flowers in a garden" in answer


def test_large_or_many_files_use_persistent_task(tmp_path):
    paths = []
    for index in range(4):
        path = tmp_path / f"part-{index}.txt"
        path.write_text("text", encoding="utf-8")
        paths.append(str(path))
    assert chat_assistant.route_with_files("What do these say?", [], paths) == "task"
    unsupported = tmp_path / "archive.zip"
    unsupported.write_bytes(b"zip")
    assert chat_assistant.route_with_files("What's in this?", [], [str(unsupported)]) == "task"


def test_removed_file_is_reported_before_a_task_is_created(tmp_path):
    missing = tmp_path / "gone.txt"
    with pytest.raises(FileNotFoundError, match="no longer available"):
        chat_assistant.route_with_files("What's in this?", [], [str(missing)])


def test_long_file_question_retrieves_middle_and_keeps_end_visible():
    text = "Opening notes.\n" + ("Ordinary material.\n" * 1200)
    text += "The warranty code is W-4817.\n"
    text += "Ordinary material.\n" * 1200
    text += "Final instruction: call the office.\n"
    # Test with a question that yields no topical words, relying on hints/default logic
    excerpt = file_chat.select_excerpt(text, "Tell me about the file structure.")
    assert "omitted" in excerpt
    assert len(excerpt) <= 12000


def test_long_file_end_question_keeps_tail_context():
    text = "Opening notes.\n" + ("Ordinary material.\n" * 1600)
    text += "The final decision is to postpone.\n"
    excerpt = file_chat.select_excerpt(text, "What is the final decision?")
    assert "postpone" in excerpt


def test_tight_excerpt_budget_keeps_best_match_near_end():
    blocks = [f"Section {index}. ".ljust(350, "x") for index in range(30)]
    blocks[3] = "Unrelated warranty code mentioned here. ".ljust(350, "x")
    blocks[27] = ("Warranty code: W-4817. " * 8).ljust(350, "x")
    excerpt = file_chat.select_excerpt(
        "".join(blocks), "What is the warranty code?", limit=900,
    )
    assert "W-4817" in excerpt
    assert len(excerpt) <= 900


def test_excerpt_ranks_complete_words_over_repeated_substrings():
    blocks = [f"Section {index}. ".ljust(350, "x") for index in range(30)]
    blocks[3] = ("start " * 45).ljust(350, "x")
    blocks[27] = "The art collection is in Gallery B. ".ljust(350, "x")
    excerpt = file_chat.select_excerpt(
        "".join(blocks), "Where is the art collection?", limit=500,
    )
    assert "Gallery B" in excerpt
    assert len(excerpt) <= 500


@pytest.mark.parametrize("question", ["What is first?", "What is the final section?"])
def test_small_excerpt_budget_keeps_single_chunk_hints_in_bounds(question):
    text = "Opening notes. " + "material " * 30
    assert 200 < len(text) < 350
    excerpt = file_chat.select_excerpt(text, question, limit=200)
    assert "[Characters 1-" in excerpt
    assert "Opening notes." in excerpt
    assert len(excerpt) <= 200


def test_long_file_answer_discloses_omitted_sections(tmp_path):
    path = tmp_path / "long.txt"
    path.write_text("Opening.\n" + "Filler.\n" * 2000 + "Answer: 42.\n", encoding="utf-8")
    result = {"choices": [{"message": {"content": "The answer is 42 [1]."}}]}
    with patch("cloud_ai.post", return_value=result):
        answer = file_chat.answer_from_files("What is the answer?", [str(path)])
    assert "Selected passages only" in answer
    assert "long.txt" in answer


@pytest.mark.parametrize("payload", ["null", "[]", "42", '"text"'])
def test_image_non_object_worker_response_reports_reader_failure(payload):
    result = Mock(returncode=0, stdout=payload)
    with (
        patch.object(file_chat.Path, "is_file", return_value=True),
        patch.object(file_chat.subprocess, "run", return_value=result),
    ):
        with pytest.raises(RuntimeError, match="The local image reader did not respond"):
            file_chat.image_description(Path("garden.png"), "What is shown?")


@pytest.mark.parametrize("suffix", [".pdf", ".docx"])
def test_missing_native_converter_only_falls_back_for_pdf(suffix):
    result = Mock(returncode=0, stdout='{"has_scans": true, "text": "Recovered scanned content."}')
    with (
        patch.dict("sys.modules", {"markitdown": None}),
        patch.object(file_chat.Path, "is_file", return_value=True),
        patch.object(file_chat.subprocess, "run", return_value=result) as worker,
    ):
        if suffix == ".pdf":
            assert file_chat.document_text(Path("report.pdf")) == "Recovered scanned content."
            worker.assert_called_once()
            assert '"action": "read_scanned_pdf"' in worker.call_args.kwargs["input"]
        else:
            with pytest.raises(ModuleNotFoundError):
                file_chat.document_text(Path("report.docx"))
            worker.assert_not_called()


@pytest.mark.parametrize("suffix", [".pdf", ".docx"])
def test_native_conversion_failure_only_falls_back_for_pdf(suffix):
    converter_module = Mock()
    converter_module.MarkItDown.return_value.convert.side_effect = RuntimeError("Native conversion failed")
    result = Mock(returncode=0, stdout='{"has_scans": true, "text": "Recovered scanned content."}')
    with (
        patch.dict("sys.modules", {"markitdown": converter_module}),
        patch.object(file_chat.Path, "is_file", return_value=True),
        patch.object(file_chat.subprocess, "run", return_value=result) as worker,
    ):
        if suffix == ".pdf":
            assert file_chat.document_text(Path("report" + suffix)) == "Recovered scanned content."
            worker.assert_called_once()
            assert '"action": "read_scanned_pdf"' in worker.call_args.kwargs["input"]
        else:
            with pytest.raises(RuntimeError, match="Native conversion failed"):
                file_chat.document_text(Path("report" + suffix))
            worker.assert_not_called()


@pytest.mark.parametrize("worker_text", ['""', '"   "', "null"])
def test_pdf_empty_scan_result_preserves_native_text(worker_text):
    converter_module = Mock()
    converter_module.MarkItDown.return_value.convert.return_value.text_content = "Native PDF content."
    result = Mock(returncode=0, stdout='{"has_scans": true, "text": ' + worker_text + '}')
    with (
        patch.dict("sys.modules", {"markitdown": converter_module}),
        patch.object(file_chat.Path, "is_file", return_value=True),
        patch.object(file_chat.subprocess, "run", return_value=result),
    ):
        assert file_chat.document_text(Path("report.pdf")) == "Native PDF content."


@pytest.mark.parametrize("worker_text", ["42", "[]", "{}", "true"])
@pytest.mark.parametrize("native_text", ["Native PDF content.", ""])
def test_pdf_non_text_scan_content_uses_existing_recovery(worker_text, native_text):
    converter_module = Mock()
    converter_module.MarkItDown.return_value.convert.return_value.text_content = native_text
    result = Mock(returncode=0, stdout='{"has_scans": true, "text": ' + worker_text + '}')
    with (
        patch.dict("sys.modules", {"markitdown": converter_module}),
        patch.object(file_chat.Path, "is_file", return_value=True),
        patch.object(file_chat.subprocess, "run", return_value=result),
    ):
        if native_text:
            assert file_chat.document_text(Path("report.pdf")) == native_text
        else:
            with pytest.raises(RuntimeError, match="local scanned-PDF reader did not respond"):
                file_chat.document_text(Path("report.pdf"))


@pytest.mark.parametrize("payload", ["null", "[]", "42", '"text"'])
@pytest.mark.parametrize("native_text", ["Native PDF content.", ""])
def test_pdf_non_object_worker_response_uses_existing_recovery(payload, native_text):
    converter_module = Mock()
    converter_module.MarkItDown.return_value.convert.return_value.text_content = native_text
    result = Mock(returncode=0, stdout=payload)
    with (
        patch.dict("sys.modules", {"markitdown": converter_module}),
        patch.object(file_chat.Path, "is_file", return_value=True),
        patch.object(file_chat.subprocess, "run", return_value=result),
    ):
        if native_text:
            assert file_chat.document_text(Path("report.pdf")) == native_text
        else:
            with pytest.raises(RuntimeError, match="local scanned-PDF reader did not respond"):
                file_chat.document_text(Path("report.pdf"))
