"""Generated document verification must retain the validated check definitions."""

import json
import re
import subprocess
import sys
from pathlib import Path

import document_check


def test_generated_document_verifier_rejects_changed_calculation_definition(tmp_path):
    (tmp_path / "terms.txt").write_text("Base fee: $100\nCredit: $20\n", encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("Net payable: $80\n", encoding="utf-8")
    spec = {"source_path": "terms.txt", "report_path": "report.md", "calculations": [{
        "operation": "subtract", "source_texts": ["Base fee: $100", "Credit: $20"],
        "report_text": "Net payable: $80"}]}
    checked = document_check.check(tmp_path, spec)
    assert checked["passed"]
    assert document_check.save_verifier(tmp_path, spec, checked["source_sha256"])
    saved = tmp_path / ".local-ai/document-check.json"
    script = (tmp_path / ".local-ai/verify.ps1").read_text(encoding="utf-8-sig")
    command = [sys.executable, str(Path(document_check.__file__).resolve()),
               "--folder", str(tmp_path), "--spec", str(saved)]
    # Execute the Python entry point used by the generated PowerShell script,
    # retaining its optional attestation without changing execution policy.
    attestation = re.search(r"--spec-sha256\s+([a-f0-9]{64})", script)
    if attestation:
        command += ["--spec-sha256", attestation.group(1)]
    original = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert original.returncode == 0, original.stderr
    assert json.loads(original.stdout)["passed"]
    original_definition = saved.read_bytes()
    record = json.loads(original_definition)
    record["spec"]["calculations"][0]["report_text"] = "Net payable: $999"
    report.write_text("Net payable: $999\n", encoding="utf-8")
    saved.write_text(json.dumps(record), encoding="utf-8")
    assert not document_check.check(tmp_path, record["spec"])["passed"]
    changed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert changed.returncode != 0, changed.stdout
    assert "definition changed" in changed.stdout + changed.stderr
    saved.write_bytes(original_definition)
    report.write_text("Net payable: $80\n", encoding="utf-8")
    restored = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["passed"]
