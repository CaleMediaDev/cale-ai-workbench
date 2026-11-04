"""Document report checks stay grounded in local source bytes and real arithmetic."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import document_check
import native_agent

SOURCE = ("Payment due: November 15, 2026\nBase fee: $1,280\n"
          "Setup credit: $180\nSupport phone: 517-555-0142\n")
REPORT = ("Final due date: November 15, 2026\nNet payable: $1,100\n"
          "Support phone: 517-555-0142\n")
SPEC = {"source_path": "terms.txt", "report_path": "summary.md", "facts": [
    {"source_text": "Payment due: November 15, 2026",
     "report_text": "Final due date: November 15, 2026"},
    {"source_text": "Support phone: 517-555-0142",
     "report_text": "Support phone: 517-555-0142"},
], "calculations": [{"operation": "subtract",
                     "source_texts": ["Base fee: $1,280", "Setup credit: $180"],
                     "report_text": "Net payable: $1,100"}]}


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".local-ai").mkdir()
    (tmp_path / "terms.txt").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "summary.md").write_text(REPORT, encoding="utf-8")
    return tmp_path


def test_document_check_saves_source_bound_read_only_verifier(tmp_path):
    folder = _project(tmp_path)
    result = document_check.check(folder, SPEC)
    assert result["passed"] is True
    assert document_check.save_verifier(folder, SPEC, result["source_sha256"]) is True
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
               str(folder / ".local-ai/verify.ps1")]
    before = (folder / "summary.md").read_bytes()
    assert subprocess.run(command, cwd=folder, capture_output=True, text=True,
                          timeout=30).returncode == 0
    assert (folder / "summary.md").read_bytes() == before

    (folder / "summary.md").write_text(REPORT.replace("$1,100", "$9,999"), encoding="utf-8")
    assert subprocess.run(command, cwd=folder, capture_output=True, text=True,
                          timeout=30).returncode != 0
    (folder / "summary.md").write_bytes(before)
    (folder / "terms.txt").write_text(SOURCE.replace("$180", "$190"), encoding="utf-8")
    assert subprocess.run(command, cwd=folder, capture_output=True, text=True,
                          timeout=30).returncode != 0


def test_document_check_rejects_wrong_amount_and_prefix_match(tmp_path):
    folder = _project(tmp_path)
    wrong = {**SPEC, "calculations": [{**SPEC["calculations"][0],
                                       "report_text": "Net payable: $9,999"}]}
    assert "differs from source arithmetic" in str(document_check.check(folder, wrong)["errors"])
    (folder / "summary.md").write_text(REPORT.replace("$1,100", "$1,1000"), encoding="utf-8")
    assert "report text was not found" in str(document_check.check(folder, SPEC)["errors"])


@pytest.mark.parametrize("source_amount,report_amount", [
    ("$1.001", "$3.00"),
    ("$1.00", "$3.001"),
])
def test_document_check_rejects_truncated_decimal_amounts(tmp_path, source_amount, report_amount):
    folder = _project(tmp_path)
    (folder / "terms.txt").write_text(
        f"Base fee: {source_amount}\nExtra fee: $2.00\n", encoding="utf-8")
    (folder / "summary.md").write_text(
        f"Total payable: {report_amount}\n", encoding="utf-8")
    spec = {"source_path": "terms.txt", "report_path": "summary.md",
            "calculations": [{"operation": "add",
                              "source_texts": [f"Base fee: {source_amount}",
                                               "Extra fee: $2.00"],
                              "report_text": f"Total payable: {report_amount}"}]}
    with pytest.raises(ValueError, match="Calculation phrases need a dollar amount"):
        document_check.check(folder, spec)


@pytest.mark.parametrize("suffix", [".50", ",000"])
@pytest.mark.parametrize("location", ["source", "report"])
def test_document_check_rejects_numeric_evidence_prefixes(tmp_path, suffix, location):
    folder = _project(tmp_path)
    if location == "source":
        (folder / "terms.txt").write_text(
            SOURCE.replace("$1,280", "$1,280" + suffix), encoding="utf-8")
        expected_error = "Calculation 1: source amounts were not found"
    else:
        (folder / "summary.md").write_text(
            REPORT.replace("$1,100", "$1,100" + suffix), encoding="utf-8")
        expected_error = "Calculation 1: report text was not found"
    result = document_check.check(folder, SPEC)
    assert result["passed"] is False
    assert expected_error in result["errors"]


@pytest.mark.parametrize("exact_total", [True, False])
def test_document_check_uses_exact_arithmetic_for_large_amounts(tmp_path, exact_total):
    folder = _project(tmp_path)
    base = "1" + "0" * 28
    total = str(int(base) + 1) if exact_total else base
    (folder / "terms.txt").write_text(
        f"Base fee: ${base}\nExtra fee: $1\n", encoding="utf-8")
    (folder / "summary.md").write_text(
        f"Total payable: ${total}\n", encoding="utf-8")
    spec = {"source_path": "terms.txt", "report_path": "summary.md",
            "calculations": [{"operation": "add",
                              "source_texts": [f"Base fee: ${base}", "Extra fee: $1"],
                              "report_text": f"Total payable: ${total}"}]}
    result = document_check.check(folder, spec)
    assert result["passed"] is exact_total
    assert result["errors"] == ([] if exact_total else [
        "Calculation 1: report amount differs from source arithmetic"])


def test_document_check_rejects_source_change_during_extraction(tmp_path):
    folder = _project(tmp_path)
    original_inspect = document_check.project_documents.inspect

    def inspect_then_change(*args, **kwargs):
        extracted = original_inspect(*args, **kwargs)
        (folder / "terms.txt").write_text(SOURCE.replace("$1,280", "$1,290"),
                                           encoding="utf-8")
        return extracted

    with patch.object(document_check.project_documents, "inspect", inspect_then_change):
        result = document_check.check(folder, SPEC)
    assert result["passed"] is False
    assert "changed while its evidence was checked" in str(result["errors"])


def test_document_check_accepts_json_cases_and_preserves_custom_verifier(tmp_path):
    folder = _project(tmp_path)
    spec = {**SPEC, "facts": '[{"source_text":"Payment due: November 15, 2026",'
                             '"report_text":"Final due date: November 15, 2026"}]',
            "calculations": '[]'}
    assert document_check.check(folder, spec)["passed"] is True
    verifier = folder / ".local-ai/verify.ps1"
    verifier.write_text("exit 0\n", encoding="utf-8")
    assert document_check.save_verifier(folder, spec, "0" * 64) is False
    assert verifier.read_text(encoding="utf-8") == "exit 0\n"
    assert not (folder / ".local-ai/document-check.json").exists()
    with pytest.raises(ValueError, match="inside this project"):
        document_check.validate_spec(folder, {**SPEC, "source_path": "../outside.txt"})


def test_native_worker_exposes_document_check_before_custom_verifier(tmp_path):
    folder = _project(tmp_path)
    project = {"folder": str(folder), "brief": "Read terms.pdf and create summary.md."}
    assert "call check_document_report" in native_agent.prompt(project)
    result = json.loads(native_agent.execute_tool(folder, "check_document_report", SPEC))
    assert result["passed"] is True
    assert result["verifier_saved"] is True
    assert (folder / ".local-ai/verify.ps1").is_file()


def test_document_report_verifier_waits_for_structured_check():
    project = {"brief": "Read terms.pdf and create summary.md with final terms."}
    args = {"path": ".local-ai/verify.ps1", "content": "exit 0"}
    assert "First inspect the source" in native_agent.document_verifier_write_issue(
        project, {}, "write_file", args)
    assert native_agent.document_verifier_write_issue(
        project, {"document_checker_attempted": True}, "write_file", args) == ""
    assert "Preserve" in native_agent.document_verifier_write_issue(
        project, {"document_checker_passed": True}, "write_file", args)
