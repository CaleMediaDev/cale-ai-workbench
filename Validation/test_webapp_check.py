"""Browser checks must exercise visible behavior and remain repeatable."""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

import native_agent
import native_browser
import webapp_check

PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width"></head>
<body><label for="bill">Bill</label><input id="bill" value="100">
<p id="answer"></p><p id="error" hidden>Bad amount</p>
<script>
const bill=document.getElementById('bill'), answer=document.getElementById('answer');
const error=document.getElementById('error');
function update(){const n=Number(bill.value); answer.textContent=n>0?'$'+(n*1.2).toFixed(2):'';
error.hidden=n>0;} bill.addEventListener('input',update); update();
</script></body></html>"""

SPEC = {"path": "index.html", "viewport_width": 375, "cases": [
    {"expect": {"answer": "120.00"}, "hidden": ["error"], "mobile_fit": True},
    {"set": {"bill": "80"}, "expect": {"answer": "96.00"}},
    {"set": {"bill": "0"}, "visible": ["error"]},
]}


def test_browser_check_catches_hidden_error_and_saved_verifier_regression():
    pytest.importorskip("playwright")
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "index.html").write_text(PAGE, encoding="utf-8")
        (folder / ".local-ai").mkdir()
        assert webapp_check.check(folder, SPEC)["passed"] is True
        assert webapp_check.save_verifier(folder, SPEC) is True
        script = folder / ".local-ai" / "verify.ps1"
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)]
        assert subprocess.run(command, cwd=folder, capture_output=True,
                              text=True, timeout=30).returncode == 0
        (folder / "index.html").write_text(PAGE.replace("error.hidden=n>0", "error.hidden=true"),
                                           encoding="utf-8")
        failure = webapp_check.check(folder, SPEC)
        assert failure["passed"] is False
        assert "'error' visibility was wrong" in failure["cases"][2]["failures"]
        assert subprocess.run(command, cwd=folder, capture_output=True,
                              text=True, timeout=30).returncode != 0


def test_browser_verifier_preserves_custom_check_and_rejects_escape():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "index.html").write_text(PAGE, encoding="utf-8")
        notes = folder / ".local-ai"
        notes.mkdir()
        verifier = notes / "verify.ps1"
        verifier.write_text("exit 0\n", encoding="utf-8")
        assert webapp_check.save_verifier(folder, SPEC) is False
        assert verifier.read_text(encoding="utf-8") == "exit 0\n"
        assert not (notes / "webapp-check.json").exists()
        with pytest.raises(ValueError, match="HTML file in this project"):
            webapp_check.validate_spec(folder, {**SPEC, "path": "../outside.html"})
        with pytest.raises(ValueError, match="observable assertion"):
            webapp_check.validate_spec(folder, {"path": "index.html", "cases": [{"set": {"bill": "1"}}]})


def test_browser_check_accepts_json_cases_and_visible_text():
    pytest.importorskip("playwright")
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "index.html").write_text(PAGE, encoding="utf-8")
        spec = {"path": "index.html", "cases": (
            '[{"set":{"bill":"80"},"expect":{"answer":"96.00"},'
            '"visible":["$96.00"]}]'), "mobile_fit": True}
        assert webapp_check.check(folder, spec)["passed"] is True


def test_invalid_letters_keep_required_number_input_and_check_empty_state():
    pytest.importorskip("playwright")
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "index.html").write_text(
            PAGE.replace('<input id="bill"', '<input type="number" id="bill"'),
            encoding="utf-8",
        )
        spec = {"path": "index.html", "cases": [
            {"set": {"bill": "abc"}, "visible": ["error"], "hidden": ["answer"]},
        ]}
        result = webapp_check.check(folder, spec)
        assert result["passed"] is True
        assert result["normalized_inputs"] == [
            "#bill: 'abc' cannot be typed into a number input; tested empty instead"
        ]


def test_offline_check_works_after_task_browser_was_opened():
    pytest.importorskip("playwright")
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "index.html").write_text(PAGE, encoding="utf-8")
        try:
            native_browser._page(folder)
            result = native_agent.execute_tool(folder, "check_webapp", SPEC)
            assert json.loads(result)["passed"] is True
        finally:
            native_browser.close()


def test_failed_text_check_reports_actual_value():
    pytest.importorskip("playwright")
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "index.html").write_text(PAGE, encoding="utf-8")
        result = webapp_check.check(folder, {
            "path": "index.html", "cases": [{"expect": {"answer": "$120.00 each"}}],
        })
        assert result["passed"] is False
        assert "displayed '$120.00'" in result["cases"][0]["failures"][0]
