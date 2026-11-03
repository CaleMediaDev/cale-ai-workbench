"""Browser checks must exercise visible behavior and remain repeatable."""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import long_tasks
import native_agent
import native_browser
import task_context
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


@pytest.mark.parametrize("plan",["- [x] Build and check the page\n",
                                 "1. Build and check the page - Verified by browser cases\n"])
def test_focused_json_check_runs_generated_verifier_and_finishes(tmp_path,plan):
    (tmp_path/"index.html").write_text(PAGE,encoding="utf-8")
    notes = tmp_path / ".local-ai"
    notes.mkdir()
    (notes/"PLAN.md").write_text(plan)
    (notes/"PROGRESS.md").write_text('The requested page was built; completing its actual input checks now.')
    item={"id":"focused-test","folder":str(tmp_path),
          "brief":"Build a single-file index.html app with an input that updates the total."}
    native_agent.save_state(item,{"status":"continue","turns":1,"premature_finishes":1})
    seen=[]
    def complete(payload):
        seen.append(payload)
        assert payload["tools"]==[]
        assert payload["response_format"]=={"type":"json_object"}
        return {"choices":[{"message":{"role":"assistant","content":json.dumps(SPEC)}}]}
    with patch.object(long_tasks,"projects",return_value=[item]):
        state=native_agent.run_slice(item,complete=complete)
    assert len(seen)==1
    assert state["status"]=="done"
    assert state["verify_passed"] and state["browser_verified"]
    assert state["focused_check_calls"]==1
    assert (notes/"verify.ps1").is_file()
    native_agent.save_state(item,{**state,"status":"continue"})
    def no_inference(_):
        raise AssertionError("A fresh owned verifier should recover this handoff without a model call")
    with patch.object(long_tasks,"projects",return_value=[item]):
        recovered=native_agent.run_slice(item,complete=no_inference)
    assert recovered["status"]=="done"
    assert "recovered verified handoff" in recovered["current"]


def test_focused_json_cannot_choose_another_tool_or_run_arbitrary_text(tmp_path):
    (tmp_path/"index.html").write_text(PAGE,encoding="utf-8")
    notes = tmp_path / ".local-ai"
    notes.mkdir()
    item={"id":"focused-test","folder":str(tmp_path),
          "brief":"Build a single-file index.html app with an input that updates the total."}
    native_agent.save_state(item,{"status":"continue","turns":1,"premature_finishes":1})
    response={"choices":[{"message":{"role":"assistant","content":json.dumps({
        "tool":"run_command","command":"do something else","path":"index.html","cases":[]})}}]}
    state=native_agent.run_slice(item,complete=lambda _:response)
    assert state["status"]=="continue"
    assert "not valid argument JSON" in state["focused_check_error"]
    assert not (notes/"verify.ps1").exists()
    assert native_agent.focused_browser_check(item,state,task_context.request_identity(tmp_path)) is not None
    state=native_agent.run_slice(item,complete=lambda _:response)
    assert native_agent.focused_browser_check(item,state,task_context.request_identity(tmp_path)) is None


@pytest.mark.parametrize("case",[
    {"expect":{"answer":""}},
    {"expect":{"answer":"ok"},"hidden":["answer"]},
    {"visible":["error"],"hidden":["error"]},
])
def test_contradictory_or_vacuous_cases_are_rejected_before_browser_launch(tmp_path,case):
    (tmp_path/"index.html").write_text(PAGE,encoding="utf-8")
    with pytest.raises(ValueError):
        webapp_check.validate_spec(tmp_path,{"path":"index.html","cases":[case]})


def test_numeric_equivalence_cases_reject_superficial_decimal_string_validation(tmp_path):
    page = tmp_path / "index.html"
    source = '''<input id="people" type="number" value="4"><p id="total"></p><p id="error" hidden>Invalid count</p>
<script>const people=document.getElementById('people'),out=document.getElementById('total'),error=document.getElementById('error');
function update(){const raw=people.value,n=Number(raw);const valid=Number.isInteger(n)&&n>0;
error.hidden=valid;out.hidden=!valid;out.textContent=valid?'$'+(120/n).toFixed(2):'';}
people.oninput=update;update();</script>'''
    spec={"path":"index.html","cases":[
        {"set":{"people":"4.0"},"expect":{"total":"$30.00"}},
        {"set":{"people":"4e1"},"expect":{"total":"$3.00"}},
        {"set":{"people":"4.5"},"visible":["error"],"hidden":["total"]},
    ]}
    page.write_text(source,encoding="utf-8")
    assert webapp_check.check(tmp_path,spec)["passed"]
    superficial=source.replace('n=Number(raw)','n=parseInt(raw,10)').replace(
        'Number.isInteger(n)&&n>0',"!raw.includes('.')&&n>0")
    page.write_text(superficial,encoding="utf-8")
    result=webapp_check.check(tmp_path,spec)
    assert not result["cases"][0]["passed"] and not result["cases"][1]["passed"]
    assert result["cases"][2]["passed"]


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
        assert "'error' visibility was wrong: expected visible, observed hidden or missing" in failure["cases"][2]["failures"]
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
            with patch.object(long_tasks, "projects", return_value=[{"id": "browser-check", "folder": str(folder)}]):
                result = native_agent.execute_tool(folder, "check_webapp", SPEC)
            assert json.loads(result)["passed"] is True
        finally:
            native_browser.close()


def test_retained_browser_sequences_use_fresh_state_and_reject_earlier_regression(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    first = {"path": "index.html", "cases": [{"set": {"bill": "80"}, "expect": {"answer": "96.00"}}]}
    second = {"path": "index.html", "cases": [{"expect": {"answer": "120.00"}}]}
    assert webapp_check.save_verifier(tmp_path, first, accumulate=True)
    assert webapp_check.save_verifier(tmp_path, second, accumulate=True)
    saved = tmp_path / ".local-ai/webapp-check.json"
    result = webapp_check.run_saved_checks(tmp_path, saved)
    assert result["passed"] and len(result["checks"]) == 2
    # The newest default-only check still passes; the earlier changed input
    # must remain part of the saved proof rather than being overwritten.
    (tmp_path / "index.html").write_text(PAGE.replace("n*1.2", "n===80?1:n*1.2"))
    assert webapp_check.check(tmp_path, second)["passed"]
    assert not webapp_check.run_saved_checks(tmp_path, saved)["passed"]


def test_browser_suite_corrects_same_sequence_and_rejects_tampering_or_new_request(tmp_path):
    import hashlib

    (tmp_path / "index.html").write_text(PAGE)
    wrong = {"path": "./index.html", "cases": [{"set": {"bill": "80"}, "expect": {"answer": "wrong"}}]}
    right = {"path": "index.html", "cases": [{"set": {"bill": "80"}, "expect": {"answer": "96.00"}}]}
    assert webapp_check.save_verifier(tmp_path, wrong, accumulate=True)
    assert webapp_check.save_verifier(tmp_path, right, accumulate=True)
    path = tmp_path / ".local-ai/webapp-check.json"
    saved = json.loads(path.read_text())
    assert len(saved["checks"]) == 1 and saved["checks"][0] == right
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert webapp_check.run_saved_checks(tmp_path, path, digest)["passed"]
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="cases changed"):
        webapp_check.run_saved_checks(tmp_path, path, digest)
    (tmp_path / ".local-ai/USER-REQUESTS.md").write_text("Use the revised requirements")
    with pytest.raises(ValueError, match="instructions changed"):
        webapp_check.run_saved_checks(tmp_path, path)
    assert webapp_check.save_verifier(tmp_path, SPEC, accumulate=True)
    assert len(json.loads(path.read_text())["checks"]) == 1


def test_browser_steps_plan_before_source_and_propagate_real_interaction_evidence(tmp_path):
    item = {"id": "web-step", "folder": str(tmp_path), "brief": "Build an index.html input calculator"}
    queue = task_context.plan_steps(tmp_path, [{"phase": "Core", "title": "Implement calculation",
        "check_tool": "check_webapp", "check_arguments": SPEC}])
    (tmp_path / "index.html").write_text(PAGE)
    response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "step", "function": {
        "name": "task_steps", "arguments": json.dumps({"action": "complete", "step_id": queue["active"]["id"]})}}]}}]}
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(item, complete=lambda _: response)
    assert task_context.step_status(tmp_path)["done"] == 1
    assert state["browser_verified"] and state["browser_interactions"] and state["verify_passed"]
    assert not state["failed_checks"]


def test_browser_suite_size_limits_and_tool_syntax_errors_are_explicit(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    with pytest.raises(ValueError, match="Malformed tool argument"):
        webapp_check.normalize_spec({"path": "index.html", "cases": '[{\\"set\\":{}}]]'})
    with pytest.raises(ValueError, match="sibling lists"):
        webapp_check.validate_spec(tmp_path, {"path": "index.html", "cases": [{"expect": {"visible": ["error"]}}]})
    previous = None
    for number in range(8):
        spec = {"path": "index.html", "cases": [{"set": {"bill": str(number + 1)}, "visible": ["answer"]}]}
        previous = webapp_check.saved_suite(tmp_path, spec, previous)
    with pytest.raises(ValueError, match="at most 8"):
        webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [{"set": {"bill": "20"}, "visible": ["answer"]}]}, previous)


def test_same_input_sequence_retains_other_assertions_and_allows_explicit_corrections(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    old = {"path": "index.html", "cases": [{"expect": {"answer": "wrong"}, "hidden": ["error"]}]}
    saved = webapp_check.saved_suite(tmp_path, old)
    saved = webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [{"mobile_fit": True}]}, saved)
    case = saved["checks"][0]["cases"][0]
    assert case == {"expect": {"answer": "wrong"}, "hidden": ["error"], "mobile_fit": True}
    saved = webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [{"expect": {"answer": "120.00"}}]}, saved)
    case = saved["checks"][0]["cases"][0]
    assert case["expect"]["answer"] == "120.00" and case["hidden"] == ["error"] and case["mobile_fit"]
    saved = webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [{"visible": ["error"]}]}, saved)
    case = saved["checks"][0]["cases"][0]
    assert "hidden" not in case and case["visible"] == ["error"] and case["expect"]["answer"] == "120.00"
    assert old["cases"][0]["expect"]["answer"] == "wrong"  # Caller data was not mutated.


def test_saved_browser_suite_accepts_explicit_empty_optional_fields(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    spec = {"path": "index.html", "cases": [{"set": {}, "click": [], "keys": [],
        "visible": [], "hidden": [], "wait_ms": 0, "expect": {"answer": "120.00"}}]}
    assert webapp_check.save_verifier(tmp_path, spec, accumulate=True)
    assert webapp_check.run_saved_checks(tmp_path, tmp_path / ".local-ai/webapp-check.json")["passed"]


def test_multi_page_saved_checks_catch_old_failures_and_clear_only_verified_paths(tmp_path):
    for name in ("first.html", "second.html", "unrelated.html"):
        (tmp_path / name).write_text(PAGE)
    item = {"id": "multi-page", "folder": str(tmp_path)}
    first = {**SPEC, "path": "first.html"}
    second = {**SPEC, "path": "second.html"}
    with patch.object(long_tasks, "projects", return_value=[item]):
        assert json.loads(native_agent.execute_tool(tmp_path, "check_webapp", first))["passed"]
        assert json.loads(native_agent.execute_tool(tmp_path, "check_webapp", second))["passed"]
        (tmp_path / "first.html").write_text(PAGE.replace("n*1.2", "n*2"))
        failed = native_agent.execute_tool(tmp_path, "check_webapp", first)
        task_context.record_action(tmp_path, "check_webapp", failed, first)
        assert not json.loads(failed)["passed"]
        # The current page is fine, but its previously checked sibling is not.
        assert not json.loads(native_agent.execute_tool(tmp_path, "check_webapp", second))["passed"]
        (tmp_path / "first.html").write_text(PAGE)
        passed = native_agent.execute_tool(tmp_path, "check_webapp", second)
    verdict = json.loads(passed)
    assert verdict["passed"] and verdict["verified_paths"] == ["first.html", "second.html"]
    task_context.record_action(tmp_path, "check_webapp", '{"passed":false}', {"path": "unrelated.html"})
    task_context.record_action(tmp_path, "check_webapp", passed, second)
    assert [row["path"] for row in task_context.checker_failures(tmp_path, "")] == ["unrelated.html"]
    state = {}
    native_agent.remember_checker_verdict(state, "check_webapp", first, failed)
    native_agent.remember_checker_verdict(state, "check_webapp", {"path": "unrelated.html"}, '{"passed":false}')
    native_agent.remember_checker_verdict(state, "check_webapp", second, passed)
    assert list(state["failed_checks"].values()) == ["check_webapp unrelated.html"]


def test_direct_browser_step_check_advances_once_with_valid_json_and_keeps_source(tmp_path):
    item = {"id": "direct-web", "folder": str(tmp_path), "brief": "Build an index.html input calculator"}
    (tmp_path / "index.html").write_text(PAGE)
    original = (tmp_path / "index.html").read_bytes()
    task_context.plan_steps(tmp_path, [{"phase": "Core", "title": "Calculate",
        "check_tool": "check_webapp", "check_arguments": SPEC},
        {"phase": "Integration", "title": "Final check", "check_tool": "run_verifier"}])
    response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "check", "function": {
        "name": "check_webapp", "arguments": json.dumps(SPEC)}}]}}]}
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(item, complete=lambda _: response)
    assert task_context.step_status(tmp_path)["done"] == 1
    reply = next(message for message in state["messages"] if message.get("role") == "tool")
    assert json.loads(reply["content"])["step_progress"]["done"] == 1
    assert (tmp_path / "index.html").read_bytes() == original


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


def test_failed_visibility_reports_requested_and_observed_state(tmp_path):
    (tmp_path/"index.html").write_text('<p id="error" hidden>Invalid</p>')
    result=webapp_check.check(tmp_path,{"path":"index.html","cases":[{"visible":["error"]}]})
    message=result["cases"][0]["failures"][0]
    assert "expected visible" in message and "observed hidden" in message
