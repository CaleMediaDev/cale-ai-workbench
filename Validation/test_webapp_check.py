"""Browser checks must exercise visible behavior and remain repeatable."""

import json
import subprocess
import tempfile
from decimal import Decimal
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

CANVAS_STATE_PAGE = """<!doctype html><canvas id="canvas"></canvas><script>
window.game={player:{x:300},bullets:[],playing:true,empty:null,label:'ready'};
window.addEventListener('keydown',e=>{
 if(e.code==='ArrowRight')game.player.x+=24;
 if(e.code==='Space')game.bullets.push({x:game.player.x});
});</script>"""


def test_nontext_presence_mistake_still_fails_and_suggests_visible_without_rewriting_page(tmp_path):
    source = '<canvas id="canvas"></canvas><p id="status">exists</p>'
    page = tmp_path / "index.html"
    page.write_text(source)
    failed = webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"expect": {"canvas": "exists"}}]})
    assert failed["passed"] is False
    assert "use visible=['canvas']" in str(failed["cases"][0]["failures"])
    assert page.read_text() == source
    assert webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"visible": ["canvas"]}]})["passed"]
    assert webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"expect": {"status": "exists"}}]})["passed"]


def test_canvas_live_state_records_real_key_effects_and_rejects_broken_controls(tmp_path):
    path = tmp_path / "index.html"
    path.write_text(CANVAS_STATE_PAGE)
    spec = {"path": "index.html", "cases": [
        {"state": {"game.player.x": 300, "game.playing": True, "game.empty": None, "game.label": "ready"}},
        {"keys": [{"key": "ArrowRight"}], "state": {"game.player.x": {"gt": 300, "lte": 324}}},
        {"keys": [{"key": "Space"}], "state": {"game.bullets.length": {"gte": 1}}}]}
    result = webapp_check.check(tmp_path, spec)
    assert result["passed"]
    assert [item["targets"] for item in result["interactions"]] == [
        ["state:game.player.x"], ["state:game.bullets.length"]]
    assert native_agent.browser_check_evidence(tmp_path, spec, json.dumps(result))["browser_verified"]
    unchanged = webapp_check.check(tmp_path, {"path": "index.html", "cases": [
        {"keys": [{"key": "Space"}], "state": {"game.player.x": 300}}]})
    assert unchanged["passed"] and not unchanged["interactions"]
    assert webapp_check.save_verifier(tmp_path, spec, accumulate=True)
    path.write_text(CANVAS_STATE_PAGE.replace("game.player.x+=24", "game.player.x+=0"))
    failed = webapp_check.run_saved_checks(tmp_path, tmp_path / ".local-ai/webapp-check.json")
    assert not failed["passed"]
    assert "game.player.x" in str(failed)


def test_canvas_state_rejects_missing_accessors_and_incorrect_types_without_calling_getters(tmp_path):
    source = CANVAS_STATE_PAGE.replace("</script>", """
Object.defineProperty(game,'trap',{get(){game.player.x=999;return 42}});
Object.defineProperty(game,'infinite',{value:Infinity});</script>""")
    (tmp_path / "index.html").write_text(source)
    spec = {"path": "index.html", "cases": [{"state": {
        "game.missing": None, "game.trap": 42, "game.playing": 1, "game.infinite": 0}},
        {"state": {"game.player.x": 300}}]}
    result = webapp_check.check(tmp_path, spec)
    assert not result["passed"] and len(result["cases"][0]["failures"]) == 4
    assert result["cases"][1]["passed"]  # The accessor never ran.


@pytest.mark.parametrize("state", [[], {"game.x()": 1}, {"game.__proto__.x": 1},
    {"game.constructor.name": "Object"}, {"game.player.x": {"gt": True}},
    {"game.player.x": {"eq": 300}}, {"game.player.x": []},
    {"game.player.x": float('nan')}, {"game.player.x": {}}, {"game": 1},
    {"game.player.x": {"gt": 300, "lte": 300}}, {"game.player.x": {"gte": 300, "lt": 200}},
    {"game.player.x": 10**1000}])
def test_canvas_state_spec_rejects_expressions_and_unbounded_or_coerced_values(tmp_path, state):
    (tmp_path / "index.html").write_text(CANVAS_STATE_PAGE)
    with pytest.raises(ValueError):
        webapp_check.validate_spec(tmp_path, {"path": "index.html", "cases": [{"state": state}]})


def test_saved_canvas_state_assertions_merge_without_dropping_other_targets(tmp_path):
    (tmp_path / "index.html").write_text(CANVAS_STATE_PAGE)
    old = webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [{"state": {
        "game.player.x": 300, "game.bullets.length": 0}}]})
    merged = webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [{"state": {
        "game.player.x": {"gte": 300}}}]}, old)
    assert merged["checks"][0]["cases"][0]["state"] == {
        "game.player.x": {"gte": 300}, "game.bullets.length": 0}


def test_canvas_state_assertions_are_saved_and_executed_as_sqlite_step_checks(tmp_path):
    (tmp_path / "index.html").write_text(CANVAS_STATE_PAGE)
    arguments = {"path": "index.html", "cases": [
        {"keys": [{"key": "ArrowRight"}], "state": {"game.player.x": {"gt": 300}}},
        {"keys": [{"key": "Space"}], "state": {"game.bullets.length": {"gte": 1}}}]}
    queue = task_context.plan_steps(tmp_path, [{"phase": "Controls", "title": "Movement and shooting",
        "check_tool": "check_webapp", "check_arguments": arguments}])
    step = queue["active"]
    assert step["check_arguments"] == arguments
    with patch.object(long_tasks, "projects", return_value=[{"id": "canvas", "folder": str(tmp_path)}]):
        checked = json.loads(native_agent.execute_tool(tmp_path, "task_steps", {
            "action": "complete", "step_id": step["id"]}))
    assert checked["passed"] and checked["done"] == 1
    assert checked["check_verdict"]["verifier_check_passed"] is True
    assert {target for item in checked["browser_evidence"]["browser_interactions"]
            for target in item["targets"]} == {"state:game.player.x", "state:game.bullets.length"}


def test_completion_only_browser_prompt_waits_until_sqlite_implementation_units_finish(tmp_path):
    (tmp_path / "index.html").write_text(CANVAS_STATE_PAGE)
    item = {"folder": str(tmp_path), "brief": "Build an offline HTML canvas space shooter game"}
    state = {"premature_finishes": 1}
    assert native_agent.focused_browser_check(item, state, task_context.request_identity(tmp_path))
    task_context.plan_steps(tmp_path, [{"phase": "Build", "title": "Implement controls",
        "check_tool": "check_webapp", "check_arguments": {"path": "index.html", "cases": [
            {"keys": [{"key": "ArrowRight"}], "state": {"game.player.x": {"gt": 300}}}]}}])
    assert native_agent.focused_browser_check(item, state, task_context.request_identity(tmp_path)) is None
    # This only restores the normal tools; unfinished units still block handoff.
    assert "SQLite step queue" in native_agent.completion_issue(tmp_path, item, state)


def complete_browser_unit(folder, arguments):
    queue = task_context.plan_steps(folder, [{"phase": "Controls", "title": "Check input behavior",
        "check_tool": "check_webapp", "check_arguments": arguments}])
    with patch.object(long_tasks, "projects", return_value=[{"id": "coverage", "folder": str(folder)}]):
        result = native_agent.execute_tool(folder, "task_steps", {
            "action": "complete", "step_id": queue["active"]["id"]})
    return json.loads(result)


def test_unchanged_hud_cannot_complete_input_step_but_static_scaffolding_can(tmp_path):
    for name, actions in (("behavior", {"keys": [{"key": "ArrowRight"}]}), ("scaffold", {})):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "index.html").write_text('<p id="score">0</p>')
        result = complete_browser_unit(folder, {"path": "index.html", "cases": [
            {**actions, "expect": {"score": "0"}}]})
        assert result["passed"] is (not actions)
        assert result["done"] == (0 if actions else 1)
        if actions:
            assert result["check_summary"]["input_effect_proven"] is False
            assert "Input-step coverage failed" in result["check_summary"]["errors"][0]
            assert "Input-step coverage failed" in task_context.snapshot(folder)


def test_automatic_browser_steps_preserve_input_effect_and_real_browser_evidence(tmp_path):
    for name, html, cases, passes in (
        ("weak", '<p id="score">0</p>', [{"keys": [{"key": "ArrowRight"}], "expect": {"score": "0"}}], False),
        ("real", PAGE, [{"set": {"bill": "200"}, "expect": {"answer": "240.00"}},
                        {"set": {"bill": "50"}, "expect": {"answer": "60.00"}}], True),
    ):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "index.html").write_text(html)
        item = {"id": "automatic-browser", "folder": str(folder)}
        task_context.plan_steps(folder, [{"phase": "Input", "title": "Verify behavior", "check_tool": "check_webapp",
            "check_arguments": {"path": "index.html", "cases": cases}}])
        state = {"status": "running", "turns": 1}
        native_agent.save_state(item, state)
        with patch.object(long_tasks, "projects", return_value=[item]):
            assert native_agent.advance_checked_steps(item, state, [])
        assert task_context.step_status(folder)["done"] == (1 if passes else 0)
        if passes:
            assert state["browser_verified"] and state["browser_interactions"] and state["verify_passed"]
        else:
            assert state["browser_verified"] is False and state["verify_passed"] is False
            assert "Input-step coverage failed" in task_context.snapshot(folder)


def test_invariant_input_step_can_use_fresh_same_page_saved_positive_control(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    item = {"id": "positive", "folder": str(tmp_path)}
    with patch.object(long_tasks, "projects", return_value=[item]):
        seeded = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", SPEC))
    assert seeded["passed"] and seeded["interactions"]
    invariant = {"path": "index.html", "cases": [{"set": {"bill": "100"}, "expect": {"answer": "120.00"}}]}
    result = complete_browser_unit(tmp_path, invariant)
    assert result["passed"] and result["done"] == 1
    assert result["check_summary"]["input_effect_proven"] is True


def test_another_pages_positive_control_cannot_cover_static_input_checks(tmp_path):
    (tmp_path / "other.html").write_text(PAGE)
    (tmp_path / "index.html").write_text('<p id="score">0</p>')
    with patch.object(long_tasks, "projects", return_value=[{"id": "other", "folder": str(tmp_path)}]):
        seed = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", {**SPEC, "path": "other.html"}))
    assert seed["passed"]
    weak = {"path": "index.html", "cases": [{"keys": [{"key": "ArrowRight"}], "expect": {"score": "0"}}]}
    result = complete_browser_unit(tmp_path, weak)
    assert not result["passed"] and result["done"] == 0


def test_unrelated_target_on_same_page_cannot_cover_weak_movement_check(tmp_path):
    (tmp_path / "index.html").write_text(PAGE + '<p id="score">0</p>')
    with patch.object(long_tasks, "projects", return_value=[{"id": "mixed", "folder": str(tmp_path)}]):
        seed = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", SPEC))
    assert seed["passed"] and seed["interactions"]
    weak = {"path": "index.html", "cases": [{"keys": [{"key": "ArrowRight"}], "expect": {"score": "0"}}]}
    result = complete_browser_unit(tmp_path, weak)
    assert not result["passed"] and result["done"] == 0


def test_stale_or_malformed_retained_coverage_does_not_pass_input_step(tmp_path):
    import hashlib

    path = tmp_path / "index.html"
    path.write_text('<p id="score">0</p>')
    old = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(path.read_text() + '<!-- changed -->')
    result = {"passed": True, "page_sha256": old, "interactions": [], "cases": [],
        "retained_interaction_coverage": [{"path": "../outside.html", "interaction_case_count": 1},
            {"path": "index.html", "page_sha256": old, "interaction_case_count": 1}]}
    args = {"path": "index.html", "cases": [{"keys": [{"key": "ArrowRight"}], "expect": {"score": "0"}}]}
    assert not json.loads(native_agent.browser_step_effect(tmp_path, args, json.dumps(result)))["passed"]


def test_direct_checker_cannot_auto_advance_weak_input_acceptance(tmp_path):
    (tmp_path / "index.html").write_text('<p id="score">0</p>')
    args = {"path": "index.html", "cases": [{"keys": [{"key": "ArrowRight"}], "expect": {"score": "0"}}]}
    task_context.plan_steps(tmp_path, [{"phase": "Controls", "title": "Movement",
        "check_tool": "check_webapp", "check_arguments": args}])
    item = {"id": "direct", "folder": str(tmp_path), "brief": "Build an offline HTML game"}
    response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "check", "function": {
        "name": "check_webapp", "arguments": json.dumps(args)}}]}}]}
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(item, complete=lambda _: response)
    assert task_context.step_status(tmp_path)["done"] == 0
    assert state["failed_checks"] and not state.get("browser_verified")


def test_saved_interaction_receipt_rejects_impossible_counts_and_changed_html(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    webapp_check.save_verifier(tmp_path, SPEC, accumulate=True)
    spec = tmp_path / ".local-ai/webapp-check.json"
    report = webapp_check.run_saved_checks(tmp_path, spec)
    assert report["passed"] and webapp_check.resolve_saved_report(tmp_path, report)["passed"]
    report["checks"][0]["input_case_count"] += 1
    with pytest.raises(ValueError, match="interaction counts"):
        webapp_check.resolve_saved_report(tmp_path, report)
    report["checks"][0]["input_case_count"] -= 1
    target = report["checks"][0]["interaction_targets"]
    report["checks"][0]["interaction_targets"] = ["state:unrelated.x"]
    with pytest.raises(ValueError, match="interaction targets"):
        webapp_check.resolve_saved_report(tmp_path, report)
    report["checks"][0]["interaction_targets"] = target
    (tmp_path / "index.html").write_text(PAGE + '<!-- changed -->')
    with pytest.raises(ValueError, match="HTML changed"):
        webapp_check.resolve_saved_report(tmp_path, report)


def test_saved_sequences_cannot_combine_interactions_from_different_html_versions(tmp_path):
    path = tmp_path / "index.html"
    path.write_text(PAGE)
    webapp_check.save_verifier(tmp_path, SPEC, accumulate=True)
    webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [
        {"set": {"bill": "50"}, "expect": {"answer": "60.00"}}]}, accumulate=True)
    original = webapp_check.check
    calls = 0
    def edit_between(folder, spec):
        nonlocal calls
        if calls:
            path.write_text(PAGE + '<!-- changed between sequences -->')
        calls += 1
        return original(folder, spec)
    with patch.object(webapp_check, "check", side_effect=edit_between):
        report = webapp_check.run_saved_checks(tmp_path, tmp_path / ".local-ai/webapp-check.json")
    assert not report["passed"]
    assert "HTML source changed between saved sequences" in report["errors"][0]


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
    {"expect":{"answer":"ok"},"hidden":["answer"]},
    {"visible":["error"],"hidden":["error"]},
])
def test_contradictory_visibility_cases_are_rejected_before_browser_launch(tmp_path,case):
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


def test_hidden_case_can_correct_prior_empty_text_assertion_without_dropping_other_targets(tmp_path):
    (tmp_path / "index.html").write_text('<p id="answer" hidden>stale total</p><p id="error">Invalid</p>')
    old = {"path": "index.html", "cases": [{"expect": {"answer": "", "error": "Invalid"}}]}
    saved = webapp_check.saved_suite(tmp_path, old)
    corrected = webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [{"hidden": ["answer"]}]}, saved)
    case = corrected["checks"][0]["cases"][0]
    assert case == {"expect": {"error": "Invalid"}, "hidden": ["answer"]}
    assert webapp_check.check(tmp_path, corrected["checks"][0])["passed"]


def test_named_numeric_examples_match_labels_and_decimal_values_without_model_review(tmp_path):
    (tmp_path / "index.html").write_text('<label for="count">Quantity</label><input id="count" type="number"><p id="error">Invalid</p>')
    initial = {"path": "index.html", "cases": [{"set": {"count": "0"}, "visible": ["error"]}]}
    assert webapp_check.save_verifier(tmp_path, initial, accumulate=True)
    request = "Quantity must be whole: zero and 1.5 are invalid."
    missing = webapp_check.missing_numeric_boundaries(tmp_path, request)
    assert [(item["input"], item["value"]) for item in missing] == [("count", "1.5")]
    assert missing[0]["quote"] in request
    extra = {"path": "index.html", "cases": [{"set": {"count": "1.5000"}, "visible": ["error"]}]}
    assert webapp_check.save_verifier(tmp_path, extra, accumulate=True)
    assert webapp_check.missing_numeric_boundaries(tmp_path, request) == []


def test_numeric_example_check_does_not_guess_ambiguous_or_nonnumeric_requirements(tmp_path):
    (tmp_path / "index.html").write_text('''<label for="bill">Bill amount</label><input id="bill" type="number">
<label for="tip">Tip amount</label><input id="tip" type="number"><input id="password" type="password"><p id="error">Invalid</p>''')
    assert webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [{"visible": ["error"]}]}, accumulate=True)
    assert webapp_check.missing_numeric_boundaries(tmp_path, "Amount zero is invalid.") == []
    assert webapp_check.missing_numeric_boundaries(tmp_path, "Bill and tip zero are invalid.") == []
    assert webapp_check.missing_numeric_boundaries(tmp_path, "Password shorter than 8 characters is invalid.") == []
    assert webapp_check.missing_numeric_boundaries(tmp_path, "A 375 pixel viewport must not be invalid.") == []
    assert webapp_check.missing_numeric_boundaries(tmp_path, "Bill zero is not invalid; do not reject 0.") == []
    assert webapp_check.missing_numeric_boundaries(tmp_path, "Bill below 10 is invalid.") == []
    assert webapp_check.missing_numeric_boundaries(tmp_path, "Bill 0 is valid but 4.5 is invalid.") == []
    assert webapp_check.missing_numeric_boundaries(tmp_path, "Bill zero is invalid.")[0]["input"] == "bill"
    (tmp_path / ".local-ai/verify.ps1").write_text('Write-Output "Custom acceptance suite"\nexit 0\n')
    assert webapp_check.missing_numeric_boundaries(tmp_path, "Bill zero is invalid.") == []


def test_missing_numeric_case_blocks_handoff_until_actual_browser_case_is_added(tmp_path):
    source = PAGE.replace('n>0', 'n>0&&Number.isInteger(n)').replace('<input id="bill"', '<input id="bill" type="number"')
    (tmp_path / "index.html").write_text(source)
    item = {"id": "coverage", "folder": str(tmp_path),
            "brief": "Build an index.html app with a Bill input. Bill zero and 4.5 are invalid."}
    with patch.object(long_tasks, "projects", return_value=[item]):
        initial = native_agent.execute_tool(tmp_path, "check_webapp", SPEC)
        assert json.loads(initial)["passed"]
        state = {"verify_passed": True, **native_agent.browser_check_evidence(tmp_path, SPEC, initial)}
        (tmp_path / ".local-ai/PLAN.md").write_text('- [x] Built and verified\n')
        (tmp_path / ".local-ai/PROGRESS.md").write_text('App built and existing checks passed; checking coverage.')
        issue = native_agent.completion_issue(tmp_path, item, state)
        assert '"value": "4.5"' in issue and 'no retained browser test' in issue
        assert not native_agent.automatic_handoff_ready(tmp_path, item, state)
        extra = {"path": "index.html", "cases": [{"set": {"bill": "4.5"}, "visible": ["error"], "hidden": ["answer"]}]}
        assert json.loads(native_agent.execute_tool(tmp_path, "check_webapp", extra))["verifier_check_passed"]
        assert native_agent.completion_issue(tmp_path, item, state) == ''
        assert native_agent.automatic_handoff_ready(tmp_path, item, state)
    assert (tmp_path / "index.html").read_text() == source


def test_numeric_boundary_scan_reads_late_followup_and_requires_observed_input(tmp_path):
    (tmp_path / "index.html").write_text('<label for="age">Age</label><input id="age" type="number"><p id="error">Invalid</p>')
    assert webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [
        {"expect": {"error": "-1 is invalid"}}]}, accumulate=True)
    request = "Earlier details. " * 9000 + "Age -1 and 2e1 are invalid."
    missing = webapp_check.missing_numeric_boundaries(tmp_path, request)
    assert {Decimal(item["value"]) for item in missing} == {Decimal(-1), Decimal(20)}


def test_browser_setup_then_observation_is_coalesced_without_reordering(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    original = {"path": "index.html", "cases": [
        {"set": {"bill": "80"}}, {"expect": {"answer": "96.00"}},
        {"set": {"bill": "0"}}, {"visible": ["error"]}]}
    normalized = webapp_check.normalize_spec(original)
    assert len(normalized["cases"]) == 2
    result = webapp_check.check(tmp_path, original)
    assert result["passed"] and len(result["cases"]) == 2
    assert len(original["cases"]) == 4
    assert webapp_check.save_verifier(tmp_path, original, accumulate=True)
    assert webapp_check.run_saved_checks(tmp_path, tmp_path / ".local-ai/webapp-check.json")["passed"]
    for cases in ([{"set": {"bill": "80"}}],
                  [{"set": {"bill": "80"}}, {"click": ["calculate"]}, {"expect": {"answer": "96.00"}}]):
        with pytest.raises(ValueError, match="observable assertion"):
            webapp_check.validate_spec(tmp_path, webapp_check.normalize_spec({"path": "index.html", "cases": cases}))


def test_empty_expectation_checks_actual_cleared_text_and_never_passes_vacuously(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    cleared = {"path": "index.html", "cases": [{"set": {"bill": "0"}, "expect": {"answer": ""}, "visible": ["error"]}]}
    assert webapp_check.check(tmp_path, cleared)["passed"]
    stale = {"path": "index.html", "cases": [{"expect": {"answer": ""}}]}
    assert not webapp_check.check(tmp_path, stale)["passed"]
    assert not webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"expect": {"missing": ""}}]})["passed"]
    (tmp_path / "index.html").write_text('<p id="answer" hidden>' + ' ' * 500 + 'stale total</p>')
    assert not webapp_check.check(tmp_path, stale)["passed"]
    (tmp_path / "index.html").write_text('<p id="answer" hidden>  </p>')
    both = {"path": "index.html", "cases": [{"expect": {"answer": ""}, "hidden": ["answer"]}]}
    assert webapp_check.check(tmp_path, both)["passed"]
    assert webapp_check.save_verifier(tmp_path, both, accumulate=True)
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


def test_saved_case_revision_preserves_actions_other_assertions_and_real_evidence(tmp_path):
    source = '''<label for="bill">Bill</label><input id="bill" type="number" value="100">
<p id="answer"></p><p id="error" hidden>Invalid</p><script>
const bill=document.getElementById('bill'),out=document.getElementById('answer'),err=document.getElementById('error');
function update(){const n=Number(bill.value),valid=n>0;out.hidden=!valid;err.hidden=valid;
if(valid)out.textContent='$'+(n*1.2).toFixed(2);}bill.addEventListener('input',update);update();</script>'''
    (tmp_path / "index.html").write_text(source)
    bad = {"path": "index.html", "cases": [
        {"set": {"bill": "80"}, "expect": {"answer": "96.00"}},
        {"set": {"bill": "0"}, "expect": {"answer": "", "error": "Invalid"}}]}
    assert webapp_check.save_verifier(tmp_path, bad, accumulate=True)
    incoming = {"path": "index.html", "cases": [{"set": {"bill": "50"}, "expect": {"answer": "60.00"}}]}
    item = {"id": "assertion-repair", "folder": str(tmp_path)}
    with patch.object(long_tasks, "projects", return_value=[item]):
        failed_text = native_agent.execute_tool(tmp_path, "check_webapp", incoming)
        failed = json.loads(failed_text)
        assert not failed["passed"] and failed["cases"][0]["inputs"] == {"set": {"bill": "0"}}
        assert failed["cases"][0]["saved_sequence"] == 1 and failed["cases"][0]["case"] == 2
        task_context.record_action(tmp_path, "check_webapp", failed_text, incoming)
        for _ in range(105):
            task_context.record_action(tmp_path, "read_file", "Unrelated contents", {"path": "index.html"})
        checkpoint = task_context.snapshot(tmp_path)
        assert '"bill": "0"' in checkpoint and "saved_sequence=1" in checkpoint
        assert failed["suite_sha256"] in checkpoint
        revision = failed["assertion_revision_reference"]
        assert revision == {"path": "index.html", "saved_sequence": 1, "saved_case": 2,
                            "suite_sha256": failed["suite_sha256"], "cases": [{"hidden": ["answer"]}]}
        passed_text = native_agent.execute_tool(tmp_path, "check_webapp", revision)
        passed = json.loads(passed_text)
        assert passed["passed"] and passed["verifier_check_passed"]
        assert native_agent.browser_check_evidence(tmp_path, revision, passed_text)["browser_verified"]
        task_context.record_action(tmp_path, "check_webapp", passed_text, revision)
        assert "assertion sequence 1, case 2): Completed" in task_context.snapshot(tmp_path)
    stored = json.loads((tmp_path / ".local-ai/webapp-check.json").read_text())
    repaired = stored["checks"][-1]["cases"]
    assert repaired[0] == bad["cases"][0]
    assert repaired[1] == {"set": {"bill": "0"}, "expect": {"error": "Invalid"}, "hidden": ["answer"]}
    assert stored["checks"][0] == incoming
    assert (tmp_path / "index.html").read_text() == source
    spec = tmp_path / ".local-ai/webapp-check.json"
    spec.write_text(spec.read_text() + " ")
    assert native_agent.browser_check_evidence(tmp_path, revision, passed_text) == {}


def test_case_revision_rejects_stale_input_edits_custom_verifiers_and_changed_instructions(tmp_path):
    import hashlib

    (tmp_path / "index.html").write_text(PAGE)
    webapp_check.save_verifier(tmp_path, SPEC, accumulate=True)
    spec = tmp_path / ".local-ai/webapp-check.json"
    raw = spec.read_bytes()
    revision = {"path": "index.html", "saved_sequence": 1, "saved_case": 2,
                "suite_sha256": hashlib.sha256(raw).hexdigest(), "cases": [{"expect": {"answer": "96.00"}}]}
    for update in ({"suite_sha256": "0" * 64}, {"saved_sequence": True}, {"saved_case": 0},
                   {"cases": [{"set": {"bill": "10"}, "expect": {"answer": "12.00"}}]}):
        with pytest.raises(ValueError):
            webapp_check.prepare_assertion_revision(tmp_path, {**revision, **update})
        assert spec.read_bytes() == raw
    batch, context = webapp_check.prepare_assertion_revision(tmp_path, revision)
    repeated_inputs = {**revision, "cases": [{"set": {"bill": "80"}, "expect": {"answer": "96.00"}}]}
    repeated_batch, _ = webapp_check.prepare_assertion_revision(tmp_path, repeated_inputs)
    assert repeated_batch["cases"][1]["set"] == {"bill": "80"}
    (tmp_path / ".local-ai/USER-REQUESTS.md").write_text('New user instructions')
    with pytest.raises(ValueError, match="instructions changed"):
        webapp_check.save_verifier(tmp_path, batch, accumulate=True, expected_sha=context["previous_sha256"])
    assert spec.read_bytes() == raw
    (tmp_path / ".local-ai/USER-REQUESTS.md").write_text('')
    script = tmp_path / ".local-ai/verify.ps1"
    script.write_text('exit 0\n')
    with pytest.raises(ValueError, match="custom verifier"):
        webapp_check.prepare_assertion_revision(tmp_path, revision)
    assert script.read_text() == 'exit 0\n'


def test_case_revision_does_not_claim_success_if_verifier_ownership_changes(tmp_path):
    import hashlib

    (tmp_path / "index.html").write_text(PAGE)
    webapp_check.save_verifier(tmp_path, SPEC, accumulate=True)
    spec = tmp_path / ".local-ai/webapp-check.json"
    raw = spec.read_bytes()
    arguments = {"path": "index.html", "saved_sequence": 1, "saved_case": 2,
                 "suite_sha256": hashlib.sha256(raw).hexdigest(), "cases": [{"expect": {"answer": "96.00"}}]}
    check = webapp_check.check
    def changed_owner(*args, **kwargs):
        result = check(*args, **kwargs)
        (tmp_path / ".local-ai/verify.ps1").write_text('Write-Output "User custom checks"\nexit 0\n')
        return result
    with patch.object(webapp_check, "check", side_effect=changed_owner):
        result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", arguments))
    assert not result["passed"] and not result["verifier_saved"]
    assert "ownership changed" in result["errors"][0] and spec.read_bytes() == raw


def test_coalesced_failure_keeps_actual_inputs_in_durable_diagnostic(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    arguments = {"path": "index.html", "cases": [
        {"set": {"bill": "80"}}, {"expect": {"answer": "wrong"}},
        {"set": {"bill": "10"}}, {"expect": {"answer": "also wrong"}}]}
    result = webapp_check.check(tmp_path, arguments)
    task_context.record_action(tmp_path, "check_webapp", json.dumps(result), arguments)
    detail = task_context.checker_failures(tmp_path, "index.html")[0]["details"]
    assert '"bill": "80"' in detail and '"bill": "10"' in detail
    assert result["cases"][1]["inputs"] == {"set": {"bill": "10"}}


def test_large_browser_failures_return_valid_json_without_changing_verdict(tmp_path):
    (tmp_path / "index.html").write_text('<p id="answer">' + 'a' * 200 + '</p>')
    arguments = {"path": "index.html", "cases": [{"expect": {"answer": 'b' * 200}} for _ in range(12)]}
    result = webapp_check.check(tmp_path, arguments)
    assert not result["passed"] and len(result["cases"]) == 12
    # Include realistically repetitive verifier diagnostics to cross the
    # transport limit. Compaction must retain an explicit failing verdict.
    result["verification"] = 'long diagnostic ' * 2000
    encoded = webapp_check.serialize_result(result)
    compact = json.loads(encoded)
    assert len(encoded) <= 16000 and not compact["passed"] and compact["diagnostics_limited"]
    assert compact["failed_case_count"] == 12 and compact["cases"]


def test_default_failed_case_does_not_borrow_inputs_from_another_sequence(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    failed = webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"expect": {"answer": "wrong"}}]})
    assert failed["cases"][0]["inputs"] == {}
    current = {"path": "index.html", "cases": [{"set": {"bill": "80"}, "expect": {"answer": "96.00"}}]}
    task_context.record_action(tmp_path, "check_webapp", json.dumps(failed), current)
    detail = task_context.checker_failures(tmp_path, "index.html")[0]["details"]
    assert "inputs={}" in detail and '"bill": "80"' not in detail


def test_large_saved_report_keeps_parseable_case_references_and_rejects_stale_data(tmp_path):
    import hashlib

    (tmp_path / "index.html").write_text(PAGE)
    for number in range(8):
        webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [
            {"set": {"bill": str(number + 1)}, "expect": {"answer": "wrong"}}]}, accumulate=True)
    spec = tmp_path / ".local-ai/webapp-check.json"
    original = spec.read_bytes()
    report = {"passed": False, "suite_sha256": hashlib.sha256(original).hexdigest(),
        "case_count": 8, "failed_case_count": 8, "verified_paths": [], "checks": [
            {"path": "index.html", "passed": False, "case_count": 1, "failed_case_count": 1,
             "javascript_errors": ['large error ' * 200] * 3,
             "cases": [{"case": 1, "passed": False, "inputs": {"set": {"bill": '8' * 2000}},
                        "failures": ['large failure ' * 100] * 20}] if number < 4 else []}
            for number in range(8)], "errors": []}
    encoded = webapp_check.serialize_saved_report(report)
    assert len(encoded) <= 10000 and not json.loads(encoded)["passed"]
    # Tighten the transport budget enough to elide duplicate path strings.
    compact = json.loads(webapp_check.serialize_saved_report(json.loads(encoded), len(encoded) - 40))
    assert compact["paths_in_spec"] and "path" not in compact["checks"][0]
    resolved = webapp_check.resolve_saved_report(tmp_path, compact)
    assert len(resolved["checks"]) == 8 and resolved["checks"][0]["path"] == "index.html"
    assert resolved["checks"][0]["cases"][0]["case"] == 1
    assert resolved["failed_case_count"] == 8
    spec.write_bytes(original + b' ')
    with pytest.raises(ValueError, match="changed after verification"):
        webapp_check.resolve_saved_report(tmp_path, compact)
    spec.write_bytes(original)
    (tmp_path / ".local-ai/USER-REQUESTS.md").write_text('New requirements')
    with pytest.raises(ValueError, match="current instructions"):
        webapp_check.resolve_saved_report(tmp_path, compact)
    (tmp_path / ".local-ai/USER-REQUESTS.md").write_text('')
    compact["passed"] = True
    with pytest.raises(ValueError, match="contradictory"):
        webapp_check.resolve_saved_report(tmp_path, compact)


def test_unicode_saved_reports_are_ascii_safe_bounded_and_keep_exact_paths(tmp_path):
    import hashlib

    name = '模型.html'
    (tmp_path / name).write_text(PAGE, encoding="utf-8")
    for number in range(8):
        webapp_check.save_verifier(tmp_path, {"path": name, "cases": [
            {"set": {"bill": str(number + 1)}, "expect": {"answer": "wrong"}}]}, accumulate=True)
    raw = (tmp_path / ".local-ai/webapp-check.json").read_bytes()
    report = {"passed": False, "suite_sha256": hashlib.sha256(raw).hexdigest(), "checks": [
        {"path": name, "passed": False, "javascript_errors": ['错误' * 150] * 3,
         "cases": [{"case": 1, "passed": False, "inputs": {}, "failures": ['错误' * 150] * 20}]
         if number < 4 else []} for number in range(8)]}
    encoded = webapp_check.serialize_saved_report(report)
    assert encoded.isascii() and len(encoded) <= 10000
    resolved = webapp_check.resolve_saved_report(tmp_path, json.loads(encoded))
    assert all(check["path"] == name for check in resolved["checks"])
    assert not resolved["passed"] and resolved["checks"][0]["cases"]
