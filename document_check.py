"""Check a local document report against extracted evidence and saved source bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import project_documents

MAX_REPORT_BYTES = 5_000_000
MAX_FACTS = 12
MAX_CALCULATIONS = 6


def _project_path(folder: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Use a relative project file path")
    target = (folder / name).resolve()
    if not target.is_relative_to(folder.resolve()) or not target.is_file():
        raise ValueError("Choose an existing file inside this project")
    return target


def _phrase(value: str) -> str:
    if not isinstance(value, str) or not 3 <= len(value.strip()) <= 240:
        raise ValueError("Evidence and report text must be 3 to 240 characters")
    return " ".join(value.split())


def _present(text: str, phrase: str) -> bool:
    normalized = " ".join(text.split())
    phrase = _phrase(phrase)
    boundary = r"(?![^\W_])"
    if phrase[-1].isdigit():
        boundary = r"(?![^\W_]|[.,][0-9])"
    return bool(re.search(r"(?<![^\W_])" + re.escape(phrase) +
                          boundary, normalized))


def _money(text: str) -> Decimal:
    match = re.search(
        r"(-?)\$\s*(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?!\d|[.,]\d)\b",
        text,
    )
    if not match or (match.group(1) and match.group(2).startswith("-")):
        raise ValueError("Calculation phrases need a dollar amount")
    return Decimal((match.group(1) + match.group(2)).replace(",", ""))


def normalize_spec(spec: dict) -> dict:
    """Handle JSON-encoded arrays from smaller function-calling models."""
    if not isinstance(spec, dict):
        raise ValueError("Document check must be an object")
    result = dict(spec)
    for field in ("facts", "calculations"):
        if isinstance(result.get(field), str):
            try:
                result[field] = json.loads(result[field])
            except ValueError as exc:
                raise ValueError(f"{field} must be a JSON array") from exc
    return result


def validate_spec(folder: Path, spec: dict) -> tuple[Path, Path]:
    """Bound paths and fact lists before inspecting a potentially large source."""
    source = _project_path(folder, spec.get("source_path"))
    report = _project_path(folder, spec.get("report_path"))
    if source == report:
        raise ValueError("Source and report must be different files")
    if report.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("Report is too large to check")
    facts = spec.get("facts", [])
    calculations = spec.get("calculations", [])
    if not isinstance(facts, list) or not isinstance(calculations, list):
        raise ValueError("facts and calculations must be arrays")
    if len(facts) > MAX_FACTS or len(calculations) > MAX_CALCULATIONS or not (facts or calculations):
        raise ValueError("Give 1 to 12 facts or up to 6 calculations")
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {"source_text", "report_text"}:
            raise ValueError("Each fact needs source_text and report_text")
        _phrase(fact["source_text"])
        _phrase(fact["report_text"])
    for calculation in calculations:
        if (not isinstance(calculation, dict)
                or set(calculation) != {"operation", "source_texts", "report_text"}
                or not isinstance(calculation["operation"], str)
                or calculation["operation"] not in {"add", "subtract"}
                or not isinstance(calculation["source_texts"], list)
                or len(calculation["source_texts"]) != 2):
            raise ValueError("A calculation needs add or subtract, two source_texts, and report_text")
        for phrase in calculation["source_texts"]:
            _phrase(phrase)
            _money(phrase)
        _phrase(calculation["report_text"])
        _money(calculation["report_text"])
    return source, report


def _source_contains(folder: Path, source_name: str, excerpt: dict, phrase: str) -> bool:
    if _present(str(excerpt.get("text", "")), phrase):
        return True
    focused = project_documents.inspect(folder, source_name, question=phrase)
    return _present(str(focused.get("text", "")), phrase)


def check(folder: Path, requested: dict) -> dict:
    """Check actual source evidence, report text, and simple money arithmetic."""
    spec = normalize_spec(requested)
    source, report = validate_spec(folder, spec)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    source_name = source.relative_to(folder.resolve()).as_posix()
    extracted = project_documents.inspect(folder, source_name)
    output = report.read_text(encoding="utf-8-sig", errors="replace")
    errors = []
    for number, fact in enumerate(spec.get("facts", []), 1):
        if not _source_contains(folder, source_name, extracted, fact["source_text"]):
            errors.append(f"Fact {number}: source evidence was not found")
        if not _present(output, fact["report_text"]):
            errors.append(f"Fact {number}: report text was not found")
    for number, calculation in enumerate(spec.get("calculations", []), 1):
        phrases = calculation["source_texts"]
        if not all(_source_contains(folder, source_name, extracted, phrase) for phrase in phrases):
            errors.append(f"Calculation {number}: source amounts were not found")
            continue
        left, right = (_money(phrase) for phrase in phrases)
        with localcontext() as context:
            # Dollar operands have at most two decimal places. Allow for
            # aligning their scales and an additional carry without rounding.
            context.prec = max(len(left.as_tuple().digits),
                               len(right.as_tuple().digits)) + 3
            expected = left + right if calculation["operation"] == "add" else left - right
        if _money(calculation["report_text"]) != expected:
            errors.append(f"Calculation {number}: report amount differs from source arithmetic")
        if not _present(output, calculation["report_text"]):
            errors.append(f"Calculation {number}: report text was not found")
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha256:
        errors.append("Source document changed while its evidence was checked")
    return {"passed": not errors, "errors": errors,
            "source_sha256": source_sha256}


def save_verifier(folder: Path, requested: dict, source_sha256: str) -> bool:
    """Save a read-only, source-bound check without replacing a custom verifier."""
    spec = normalize_spec(requested)
    validate_spec(folder, spec)
    notes = folder / ".local-ai"
    notes.mkdir(parents=True, exist_ok=True)
    verifier = notes / "verify.ps1"
    marker = "# Generated by Cale check_document_report"
    existing = verifier.read_text(encoding="utf-8-sig") if verifier.is_file() else ""
    if existing and not existing.startswith(marker):
        return False
    record = {"spec": spec, "source_sha256": source_sha256}
    target = notes / "document-check.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(temporary, target)

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = (
        marker + "\n$ErrorActionPreference = 'Stop'\n"
        f"$python = {literal(sys.executable)}\n"
        "if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction Stop).Source }\n"
        f"& $python {literal(str(Path(__file__).resolve()))} --folder "
        "(Split-Path -Parent $PSScriptRoot) --spec "
        "(Join-Path $PSScriptRoot 'document-check.json')\n"
        "if ($LASTEXITCODE -ne 0) { exit 1 }\n"
    )
    script_temp = verifier.with_suffix(".tmp")
    script_temp.write_text(script, encoding="utf-8-sig")
    os.replace(script_temp, verifier)
    return True


def verify_saved(folder: Path, record: dict) -> dict:
    """Recheck unchanged source bytes and every report claim without writing."""
    spec = normalize_spec(record["spec"])
    source, report = validate_spec(folder, spec)
    errors = []
    if hashlib.sha256(source.read_bytes()).hexdigest() != record.get("source_sha256"):
        errors.append("Source document changed after its evidence was checked")
    output = report.read_text(encoding="utf-8-sig", errors="replace")
    for number, fact in enumerate(spec.get("facts", []), 1):
        if not _present(output, fact["report_text"]):
            errors.append(f"Fact {number}: report text was not found")
    for number, calculation in enumerate(spec.get("calculations", []), 1):
        if not _present(output, calculation["report_text"]):
            errors.append(f"Calculation {number}: report text was not found")
    return {"passed": not errors, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    spec_file = args.spec.resolve()
    if not spec_file.is_relative_to(folder / ".local-ai"):
        raise ValueError("Document check spec must be in this project's .local-ai folder")
    result = verify_saved(folder, json.loads(spec_file.read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
