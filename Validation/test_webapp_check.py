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


def test_button_calculation_replays_and_rejects_broken_handler(tmp_path):
    pytest.importorskip("playwright")
    page = PAGE.replace(
        "bill.addEventListener('input',update); update();",
        "document.getElementById('calculate').addEventListener('click',update); update();",
    ).replace('<p id="answer">', '<button id="calculate">Calculate</button><p id="answer">')
    (tmp_path / "index.html").write_text(page, encoding="utf-8")
    spec = {"path": "index.html", "cases": [
        {"set": {"bill": "80"}, "click": ["calculate"], "expect": {"answer": "96.00"}},
        {"set": {"bill": "0"}, "click": ["calculate"], "visible": ["error"]},
    ]}
    assert webapp_check.check(tmp_path, spec)["passed"]
    assert webapp_check.save_verifier(tmp_path, spec)
    saved = json.loads((tmp_path / ".local-ai/webapp-check.json").read_text())
    assert webapp_check.check(tmp_path, saved)["passed"]
    (tmp_path / "index.html").write_text(
        page.replace(".addEventListener('click',update)", ".addEventListener('click',()=>{})"),
        encoding="utf-8",
    )
    assert not webapp_check.check(tmp_path, saved)["passed"]


def test_pressed_keys_with_unchanged_hud_do_not_prove_interaction(tmp_path):
    (tmp_path/"game.html").write_text('<p id="score">SCORE: 0</p><p id="lives">3</p>')
    spec={"path":"game.html","cases":[
        {"keys":[{"key":"Space"}],"expect":{"score":"0"}},
        {"keys":[{"key":"ArrowRight"}],"expect":{"lives":"3"}}]}
    result=webapp_check.check(tmp_path,spec)
    assert result["passed"] is True
    assert result["interactions"]==[]
    assert native_agent.browser_check_evidence(tmp_path,spec,json.dumps(result))=={}


def test_real_input_effect_is_reported_and_a_noop_is_not(tmp_path):
    (tmp_path/"index.html").write_text(PAGE,encoding="utf-8")
    result=webapp_check.check(tmp_path,SPEC)
    assert result["passed"]
    assert {i["case"] for i in result["interactions"]}=={2,3}
    assert "text:answer" in result["interactions"][0]["targets"]
    assert "visibility:error" in result["interactions"][1]["targets"]
    assert native_agent.browser_check_evidence(tmp_path,SPEC,json.dumps(result))["browser_verified"]
    noop={"path":"index.html","cases":[
        {"set":{"bill":"100"},"expect":{"answer":"120.00"}},
        {"set":{"bill":"100"},"expect":{"answer":"120.00"}}]}
    result=webapp_check.check(tmp_path,noop)
    assert result["passed"] and not result["interactions"]


def test_keyboard_game_checks_replay_saved_verifier_and_detect_broken_controls(tmp_path):
    pytest.importorskip("playwright")
    source = '''<!doctype html><html><body>
<p id="position">center</p><p id="shots">0</p><p id="held">released</p>
<button id="restart">Restart</button><script>
let shots=0, held=false;
const position=document.getElementById('position'), status=document.getElementById('held');
addEventListener('keydown', e=>{
  if(e.key==='ArrowRight'){held=true;status.textContent='held';}
  if(e.code==='Space')document.getElementById('shots').textContent=String(++shots);
});
setInterval(()=>{if(held)position.textContent='right';},30);
addEventListener('keyup',()=>{held=false;status.textContent='released';});
document.getElementById('restart').onclick=()=>{
 shots=0;position.textContent='center';document.getElementById('shots').textContent='0';
};</script></body></html>'''
    page = tmp_path / "game.html"
    page.write_text(source, encoding="utf-8")
    spec = {"path": "game.html", "cases": [
        {"keys": [{"key": "ArrowRight", "hold_ms": 150}, {"key": "Space"}],
         "wait_ms": 50, "expect": {"position": "right", "shots": "1", "held": "released"}},
        {"click": ["restart"], "expect": {"position": "center", "shots": "0"}},
    ]}
    checked = webapp_check.check(tmp_path, spec)
    assert checked["passed"]
    evidence = native_agent.browser_check_evidence(tmp_path, spec, json.dumps(checked))
    assert len(evidence["browser_interactions"]) == 2
    assert webapp_check.save_verifier(tmp_path, spec)
    (tmp_path / ".local-ai/PLAN.md").write_text("- [x] Built and tested\n")
    item = {"folder": str(tmp_path), "brief": "Create a simple keyboard shooter game"}
    state = {"verify_passed": True, **evidence}
    assert native_agent.completion_issue(tmp_path, item, state) == ""
    saved = json.loads((tmp_path / ".local-ai/webapp-check.json").read_text())
    assert saved == spec
    assert webapp_check.check(tmp_path, saved)["passed"]
    # A real input regression must fail the exact persisted test.
    page.write_text(source.replace("e.key==='ArrowRight'", "e.key==='ArrowLeft'"), encoding="utf-8")
    assert "Run check_webapp" in native_agent.completion_issue(tmp_path, item, state)
    result = webapp_check.check(tmp_path, saved)
    assert not result["passed"]
    assert any("position" in message for message in result["cases"][0]["failures"])


def test_page_changed_during_check_cannot_get_passing_evidence(tmp_path, monkeypatch):
    page=tmp_path/"index.html"
    page.write_text(PAGE,encoding="utf-8")
    original=Path.read_bytes
    reads=0
    def read_bytes(path):
        nonlocal reads
        data=original(path)
        if path==page:
            reads+=1
            if reads>1:
                return data+b'<!-- changed during check -->'
        return data
    monkeypatch.setattr(Path,"read_bytes",read_bytes)
    result=webapp_check.check(tmp_path,SPEC)
    assert not result["passed"]
    assert any("changed while" in message for message in result["javascript_errors"])


@pytest.mark.parametrize("changes", [
    {"keys": "ArrowRight"}, {"keys": [{}]}, {"keys": [{"key": "Control+L"}]},
    {"keys": [{"key": "a", "hold_ms": True}]},
    {"keys": [{"key": "a", "hold_ms": 1001}]},
    {"keys": [{"key": "a", "hold_ms": -1}]},
    {"keys": [{"key": "a", "extra": "ignored"}]},
    {"wait_ms": 2001}, {"wait_ms": True}, {"wait_ms": -1},
    {"keys": [{"key": "a", "hold_ms": 1000}] * 11},
])
def test_keyboard_checks_reject_invalid_or_excessive_actions(tmp_path, changes):
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    with pytest.raises(ValueError):
        webapp_check.validate_spec(tmp_path, {
            "path": "index.html", "cases": [{"visible": ["answer"], **changes}],
        })


def test_keyboard_only_case_still_needs_an_observable_assertion(tmp_path):
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    with pytest.raises(ValueError, match="observable assertion"):
        webapp_check.validate_spec(tmp_path, {
            "path": "index.html", "cases": [{"keys": [{"key": "Space"}]}],
        })


def test_click_spec_requires_bounded_ids_and_an_assertion(tmp_path):
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    for clicks in ("calculate", ["#calculate"], ["a"] * 21):
        with pytest.raises(ValueError):
            webapp_check.validate_spec(tmp_path, {
                "path": "index.html", "cases": [{"click": clicks, "visible": ["answer"]}],
            })
    with pytest.raises(ValueError, match="observable assertion"):
        webapp_check.validate_spec(tmp_path, {
            "path": "index.html", "cases": [{"click": ["calculate"]}],
        })


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
