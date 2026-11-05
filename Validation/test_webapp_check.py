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
import project_memory
import task_context
import webapp_check


def test_project_route_optional_observer_receives_exact_served_document(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from unittest.mock import Mock
    source = tmp_path / "index.html"
    body = b"<title>Observed source</title>"
    source.write_bytes(body)
    request = SimpleNamespace(url=source.as_uri(), resource_type="document")
    route = Mock(request=request)
    observations = []
    def observed(path, loaded, digest, actual_request):
        observations.append((path, loaded, digest, actual_request))
        source.write_bytes(b"changed after fulfil")
    webapp_check.project_only_route(tmp_path, on_document=observed)(route)
    route.fulfill.assert_called_once_with(body=body, content_type="text/html; charset=utf-8")
    route.continue_.assert_not_called()
    route.abort.assert_not_called()
    assert observations == [(source, body, hashlib.sha256(body).hexdigest(), request)]
    assert source.read_bytes() != body


def test_project_route_default_and_resources_preserve_existing_routing(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    source = tmp_path / "index.html"
    source.write_text("<body>Local</body>", encoding="utf-8")
    route = Mock(request=SimpleNamespace(url=source.as_uri(), resource_type="document"))
    webapp_check.project_only_route(tmp_path)(route)
    route.continue_.assert_called_once_with()
    route.fulfill.assert_not_called()
    observer = Mock()
    for url, kind, allowed in [(source.as_uri(), "script", True), ("data:text/plain,ok", "document", True),
                               ("https://example.invalid/remote", "script", False),
                               ((tmp_path.parent / "outside.html").as_uri(), "document", False)]:
        routed = Mock(request=SimpleNamespace(url=url, resource_type=kind))
        webapp_check.project_only_route(tmp_path, on_document=observer)(routed)
        (routed.continue_ if allowed else routed.abort).assert_called_once_with()
    observer.assert_not_called()

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


@pytest.mark.parametrize("failed_case", [
    {"visible": ["form", "table"]},
    {"set": {"missing-input": "new value"}, "visible": ["expense-form"]},
    {"click": ["missing-button"], "visible": ["expense-list"]},
    {"expect": {"missing-output": "anything"}},
])
def test_failed_target_check_reports_visible_literal_ids_without_private_values(tmp_path, failed_case):
    """A tag-name mistake is diagnosable without guessing IDs or reading values."""
    (tmp_path / "index.html").write_text('''<!doctype html><body>
        <form id="expense-form"><label for="amount">Amount</label>
        <input id="amount" value="PRIVATE_INPUT_VALUE"><textarea id="notes">PRIVATE_TEXTAREA</textarea>
        <button id="add" type="button">Add expense</button></form>
        <table id="expense-list"><tr><td>PRIVATE_TABLE_DATA</td></tr></table>
        <form><button type="button">Other action</button></form>
        <output id="summary">PRIVATE_REFLECTED_VALUE</output>
        <div hidden><button id="hidden-target">HIDDEN_LABEL</button></div>
        <div aria-hidden="true"><button id="aria-hidden-target">HIDDEN_LABEL</button></div>
        <button style="visibility:hidden" id="css-hidden-target">HIDDEN_LABEL</button>
        <input type="hidden" id="hidden-input" value="PRIVATE_HIDDEN_VALUE">
        <div contenteditable id="editor">PRIVATE_EDITOR</div>
        <iframe srcdoc='<button id="frame-target">PRIVATE_FRAME</button>'></iframe>
        <div id="shadow-host"></div><script>
        localStorage.setItem('secret','PRIVATE_STORAGE');
        document.getElementById('shadow-host').attachShadow({mode:'open'}).innerHTML=
            '<button id="shadow-target">PRIVATE_SHADOW</button>';
        </script></body>''', encoding="utf-8")
    result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [failed_case]})
    assert not result["passed"]
    inventory = result["cases"][0]["target_inventory"]
    targets = {item["id"]: item for item in inventory["targets"] if item["id"]}
    assert targets["expense-form"]["tag"] == "form"
    assert targets["expense-list"]["role"] == "table"
    assert targets["amount"]["label"] == "Amount"
    assert targets["add"]["text"] == "Add expense"
    assert any(item["id"] is None and item["tag"] == "form" for item in inventory["targets"])
    assert not set(targets) & {"frame-target", "shadow-target", "hidden-target", "aria-hidden-target",
                              "css-hidden-target", "hidden-input", "editor"}
    assert "PRIVATE_" not in json.dumps(inventory)
    assert "HIDDEN_LABEL" not in json.dumps(inventory)
    assert "not selectors" in inventory["note"]
    passing = webapp_check.check(tmp_path, {"path": "index.html", "cases": [
        {"visible": ["expense-form", "expense-list"]}]})
    assert passing["passed"]
    assert "target_inventory" not in passing["cases"][0]


def test_failed_target_inventory_stays_bounded_and_compact_reports_preserve_verdict(tmp_path):
    (tmp_path / "index.html").write_text(
        "<body>" + "".join(f'<button id="b{number}">{"x" * 300}</button>' for number in range(50)),
        encoding="utf-8")
    spec = {"path": "index.html", "cases": [{"visible": ["missing"]}]}
    result = webapp_check.check(tmp_path, spec)
    inventory = result["cases"][0]["target_inventory"]
    assert inventory["truncated"]
    assert 1 <= len(inventory["targets"]) <= 16
    assert len(json.dumps(inventory)) < 3600
    # Force the existing compact path; optional hints cannot consume the
    # transport budget or turn a failed assertion into a successful one.
    expanded = {**result, "javascript_errors": ["x" * 2000] * 20}
    compact = json.loads(webapp_check.serialize_result(expanded, 4500))
    assert not compact["passed"]
    assert compact["cases"][0]["failures"] == result["cases"][0]["failures"]
    assert len(compact["cases"][0]["target_inventory"]["targets"]) <= 4
    assert len(inventory["targets"]) > 4  # Serialization did not mutate the result.


@pytest.mark.parametrize("novalidate", [False, True])
def test_failed_submit_reports_native_constraints_without_dispatching_invalid_events(tmp_path, novalidate):
    source = '''<form id="form" %s><input id="amount" type="number" min="0.01">
    <button id="add">Add</button></form><p id="error"></p><p id="events">0</p>
    <script>let n=0;const out=document.getElementById('events');
    document.addEventListener('invalid',()=>{out.textContent=String(++n)},true);
    document.getElementById('form').onsubmit=e=>{e.preventDefault()};</script>''' % (
        "novalidate" if novalidate else "")
    path = tmp_path / "index.html"
    path.write_text(source)
    spec = {"path": "index.html", "cases": [
        {"set": {"amount": "-5"}, "click": ["add"], "expect": {"error": "positive amount"}},
        {"expect": {"events": "0" if novalidate else "1"}}]}
    result = webapp_check.check(tmp_path, spec)
    assert result["passed"] is False
    assert result["cases"][1]["passed"] is True  # Observation dispatched no extra invalid event.
    failures = result["cases"][0]["failures"]
    assert "expected visible text" in failures[0].lower()
    assert '"id":"amount"' in failures[1] and '"rangeUnderflow"' in failures[1]
    assert ('"submitValidation":false' if novalidate else '"submitValidation":true') in failures[1]
    task_context.record_action(tmp_path, "check_webapp", json.dumps(result), spec)
    assert "rangeUnderflow" in json.dumps(task_context.checker_failures(tmp_path))
    assert path.read_text() == source


def test_passing_invalid_input_case_does_not_turn_constraint_into_failure(tmp_path):
    (tmp_path / "index.html").write_text('''<form novalidate>
        <input id="amount" type="number" min="1"><button id="add">Add</button></form>
        <p id="error"></p><script>document.querySelector('form').onsubmit=e=>{
        e.preventDefault();document.getElementById('error').textContent='Invalid amount'};</script>''')
    result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [
        {"set": {"amount": "-5"}, "click": ["add"], "expect": {"error": "Invalid amount"}}]})
    assert result["passed"] is True
    assert result["cases"][0]["failures"] == []

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
    def edit_between(folder, spec, **kwargs):
        nonlocal calls
        if calls:
            path.write_text(PAGE + '<!-- changed between sequences -->')
        calls += 1
        return original(folder, spec, **kwargs)
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
    prepare_existing_html_review_fixture(tmp_path, "index.html")
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
    prepare_existing_html_review_fixture(tmp_path, "index.html")
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
    prepare_existing_html_review_fixture(tmp_path, "game.html")
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
    for number in range(webapp_check.MAX_SAVED_SEQUENCES):
        spec = {"path": "index.html", "cases": [{"set": {"bill": str(number + 1)}, "visible": ["answer"]}]}
        previous = webapp_check.saved_suite(tmp_path, spec, previous)
    with pytest.raises(ValueError, match="at most 32"):
        webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [{"set": {"bill": "999"}, "visible": ["answer"]}]}, previous)


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
    prepare_existing_html_review_fixture(tmp_path, "index.html")
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


@pytest.mark.parametrize("expected, passed", [("Completed successfully", True), ("Missing result", False)])
def test_text_assertion_searches_beyond_diagnostic_preview(tmp_path, expected, passed):
    (tmp_path / "index.html").write_text(
        '<p id="result">' + 'prefix ' * 40 + 'Completed successfully</p>',
        encoding="utf-8",
    )
    result = webapp_check.check(tmp_path, {
        "path": "index.html", "cases": [{"expect": {"result": expected}}],
    })
    assert result["passed"] is passed
    assert result["cases"][0]["passed"] is passed


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
    repaired = stored["checks"][revision["saved_sequence"] - 1]["cases"]
    assert repaired[0] == bad["cases"][0]
    assert repaired[1] == {"set": {"bill": "0"}, "expect": {"error": "Invalid"}, "hidden": ["answer"]}
    assert stored["checks"][1] == incoming
    assert (tmp_path / "index.html").read_text() == source
    spec = tmp_path / ".local-ai/webapp-check.json"
    spec.write_text(spec.read_text() + " ")
    assert native_agent.browser_check_evidence(tmp_path, revision, passed_text) == {}


def test_rejected_repair_retains_original_failure_and_recovers_with_real_browser(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    source = (tmp_path / "index.html").read_bytes()
    assert webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [
        {"set": {"bill": "80"}, "expect": {"answer": "95.00"}}]}, accumulate=True)
    incoming = {"path": "index.html", "cases": [{"set": {"bill": "50"}, "expect": {"answer": "60.00"}}]}
    active = task_context.plan_steps(tmp_path, [{"phase": "Check", "title": "Verify tax",
        "check_tool": "check_webapp", "check_arguments": incoming}])["active"]
    item = {"id": "repair-regression", "folder": str(tmp_path)}
    with patch.object(long_tasks, "projects", return_value=[item]):
        failure = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", incoming))
        assert not failure["passed"] and failure["own_batch_passed"]
        assert task_context.step_status(tmp_path)["done"] == 0
        assert task_context.step_status(tmp_path)["active"]["id"] == active["id"]
        reference = failure["assertion_revision_reference"]
        assert "assertion-only" in failure["revision_guidance"]
        saved = tmp_path / ".local-ai/webapp-check.json"
        original = saved.read_bytes()
        wrong = {**reference, "cases": [{"expect": {"answer": "97.00"}}]}
        text = native_agent.execute_tool(tmp_path, "check_webapp", wrong)
        rejected = json.loads(text)
        assert rejected["revision_attempt_failed"] and not rejected["passed"]
        assert rejected["assertion_revision_reference"] == reference
        task_context.record_action(tmp_path, "check_webapp", text, wrong)
        changed = {**reference, "cases": [{"set": {"bill": "10"}, "expect": {"answer": "12.00"}}]}
        error = native_agent.execute_tool(tmp_path, "check_webapp", changed)
        assert error.startswith("Tool error:")
        task_context.record_action(tmp_path, "check_webapp", error, changed)
        checkpoint = task_context.snapshot(tmp_path)
        assert reference["suite_sha256"] in checkpoint
        assert "saved_sequence" in checkpoint and "97.00" in checkpoint
        assert "Checker request error (no new verdict)" in checkpoint
        assert saved.read_bytes() == original
        corrected = {**reference, "cases": [{"expect": {"answer": "96.00"}}]}
        text = native_agent.execute_tool(tmp_path, "check_webapp", corrected)
        assert json.loads(text)["verifier_check_passed"]
        task_context.record_action(tmp_path, "check_webapp", text, corrected)
        final = json.loads(native_agent.execute_tool(tmp_path, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert final["passed"] and final["done"] == 1
    assert not task_context.checker_failures(tmp_path, "check_webapp")
    assert (tmp_path / "index.html").read_bytes() == source


@pytest.mark.parametrize("invalidate", ["new_request", "passed"])
def test_repair_request_errors_do_not_resurrect_superseded_failure(tmp_path, invalidate):
    args = {"path": "index.html"}
    task_context.record_action(tmp_path, "check_webapp", json.dumps({"passed": False, "cases": [
        {"case": 1, "passed": False, "failures": ["Old behavior sentinel"]}]}), args)
    if invalidate == "new_request":
        (tmp_path / ".local-ai/USER-REQUESTS.md").write_text("Different requirement")
    else:
        task_context.record_action(tmp_path, "check_webapp", '{"passed":true}', args)
    task_context.record_action(tmp_path, "check_webapp", "Tool error: malformed checker arguments", args)
    summary = task_context.snapshot(tmp_path)
    assert "malformed checker arguments" in summary and "Old behavior sentinel" not in summary


def test_explicit_visibility_id_revision_replaces_wrong_targets_and_preserves_other_checks(tmp_path):
    """A checksum-bound rename must not union the broken IDs back into the suite."""
    import hashlib
    source = '''<form id="expense-form"><input id="amount" value="1"></form>
        <table id="expense-table"><tr><td>Ready</td></tr></table>
        <p id="error" hidden>Invalid</p><p id="total">1</p>
        <script>document.getElementById('amount').oninput=e=>
            document.getElementById('total').textContent=e.target.value;</script>'''
    (tmp_path / "index.html").write_text(source)
    original = {"path": "index.html", "cases": [
        {"visible": ["form", "table"], "hidden": ["error"], "expect": {"total": "1"}},
        {"set": {"amount": "2"}, "expect": {"total": "2"}}]}
    unrelated = {"path": "index.html", "viewport_width": 800,
                 "cases": [{"visible": ["expense-form"], "mobile_fit": True}]}
    webapp_check.save_verifier(tmp_path, original, accumulate=True)
    webapp_check.save_verifier(tmp_path, unrelated, accumulate=True)
    spec_file = tmp_path / ".local-ai/webapp-check.json"
    before = json.loads(spec_file.read_text())
    revision = {"path": "index.html", "saved_sequence": 1, "saved_case": 1,
                "suite_sha256": hashlib.sha256(spec_file.read_bytes()).hexdigest(),
                "cases": [{"visible": ["expense-form", "expense-table"]}]}
    item = {"id": "renamed-visible-targets", "folder": str(tmp_path)}
    with patch.object(long_tasks, "projects", return_value=[item]):
        result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", revision))
    assert result["passed"] and result["verifier_check_passed"]
    after = json.loads(spec_file.read_text())
    repaired = after["checks"][revision["saved_sequence"] - 1]
    assert after["checks"][1] == before["checks"][1]
    assert repaired["cases"][1] == original["cases"][1]
    assert repaired["cases"][0] == {**original["cases"][0], "visible": ["expense-form", "expense-table"]}
    assert (tmp_path / "index.html").read_text() == source


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


def _seed_saved_input_revision(folder):
    """Save a mistaken input and expectation without editing the real app."""
    import hashlib

    (folder / "index.html").write_text(PAGE)
    (folder / "other.html").write_text(PAGE)
    mistaken = json.loads(json.dumps(SPEC))
    mistaken["cases"][1] = {"set": {"bill": "8O"}, "expect": {"answer": "1 row"}, "hidden": ["error"]}
    webapp_check.save_verifier(folder, mistaken, accumulate=True)
    webapp_check.save_verifier(folder, {"path": "index.html", "viewport_width": 800,
        "cases": [{"expect": {"answer": "120.00"}}]}, accumulate=True)
    webapp_check.save_verifier(folder, {"path": "other.html",
        "cases": [{"expect": {"answer": "120.00"}}]}, accumulate=True)
    saved = folder / ".local-ai/webapp-check.json"
    raw = saved.read_bytes()
    arguments = {"path": "index.html", "saved_sequence": 1, "saved_case": 2,
        "suite_sha256": hashlib.sha256(raw).hexdigest(), "revise_inputs": True,
        "cases": [{"set": {"bill": "80"}, "expect": {"answer": "96.00"}}]}
    return saved, raw, arguments


def _checked_page_sha(folder):
    import hashlib

    return hashlib.sha256((folder / "index.html").read_bytes()).hexdigest()


def test_opted_in_input_and_assertion_revision_retains_other_cases_and_sequences(tmp_path):
    saved, raw, arguments = _seed_saved_input_revision(tmp_path)
    before = json.loads(raw)
    assert not webapp_check.check(tmp_path, before["checks"][0])["passed"]
    for opt_in in ({}, {"revise_inputs": False}):
        old_mode = {key: value for key, value in arguments.items() if key != "revise_inputs"}
        with pytest.raises(ValueError, match="cannot change input actions"):
            webapp_check.prepare_assertion_revision(tmp_path, {**old_mode, **opt_in})
    repaired, revision = webapp_check.prepare_assertion_revision(tmp_path, arguments)
    assert revision["replace_sequence"] == 1
    assert repaired["cases"][0] == before["checks"][0]["cases"][0]
    assert repaired["cases"][2] == before["checks"][0]["cases"][2]
    assert repaired["cases"][1] == {"set": {"bill": "80"}, "expect": {"answer": "96.00"}, "hidden": ["error"]}
    candidate = webapp_check.check(tmp_path, repaired)
    assert candidate["passed"]
    assert saved.read_bytes() == raw  # Preparing and checking a candidate never publishes it.
    assert webapp_check.save_verifier(tmp_path, repaired, accumulate=True,
        expected_sha=revision["previous_sha256"], replace_sequence=revision["replace_sequence"],
        expected_page_sha=candidate["page_sha256"])
    after = json.loads(saved.read_bytes())
    assert after["checks"][0] == repaired
    assert after["checks"][1:] == before["checks"][1:]
    assert after["request_hash"] == before["request_hash"]
    assert webapp_check.run_saved_checks(tmp_path, saved)["passed"]
    assert (tmp_path / "index.html").read_text() == PAGE


@pytest.mark.parametrize("invalid", ["stale", "request", "sequence", "case", "path", "viewport", "custom", "boolean"])
def test_input_revision_preserves_existing_reference_and_ownership_guards(tmp_path, invalid):
    saved, raw, arguments = _seed_saved_input_revision(tmp_path)
    if invalid == "stale":
        arguments["suite_sha256"] = "0" * 64
    elif invalid == "request":
        (tmp_path / ".local-ai/USER-REQUESTS.md").write_text("A new requirement")
    elif invalid == "sequence":
        arguments["saved_sequence"] = True
    elif invalid == "case":
        arguments["saved_case"] = 0
    elif invalid == "path":
        arguments["path"] = "other.html"
    elif invalid == "viewport":
        arguments["viewport_width"] = 800
    elif invalid == "custom":
        (tmp_path / ".local-ai/verify.ps1").write_text("exit 0\n")
    else:
        arguments["revise_inputs"] = "true"
    with pytest.raises(ValueError):
        webapp_check.prepare_assertion_revision(tmp_path, arguments)
    assert saved.read_bytes() == raw


@pytest.mark.parametrize("tamper", ["index", "boolean_index", "page", "viewport", "case_count", "other_case", "typed_other_case"])
def test_input_revision_save_cannot_replace_a_different_scope(tmp_path, tamper):
    saved, raw, arguments = _seed_saved_input_revision(tmp_path)
    if tamper == "typed_other_case":
        import hashlib

        initial = json.loads(raw)
        initial["checks"][0]["cases"][0]["state"] = {"game.ready": True}
        saved.write_text(json.dumps(initial), encoding="utf-8")
        raw = saved.read_bytes()
        arguments["suite_sha256"] = hashlib.sha256(raw).hexdigest()
    batch, revision = webapp_check.prepare_assertion_revision(tmp_path, arguments)
    index = revision["replace_sequence"]
    if tamper == "index":
        index = 99
    elif tamper == "boolean_index":
        index = True
    elif tamper == "page":
        batch["path"] = "other.html"
    elif tamper == "viewport":
        batch["viewport_width"] = 800
    elif tamper == "case_count":
        batch["cases"].pop()
    elif tamper == "other_case":
        batch["cases"][0]["expect"]["answer"] = "tampered"
    else:
        batch["cases"][0]["state"]["game.ready"] = 1
    with pytest.raises(ValueError):
        webapp_check.save_verifier(tmp_path, batch, accumulate=True,
            expected_sha=revision["previous_sha256"], replace_sequence=index,
            expected_page_sha=_checked_page_sha(tmp_path))
    assert saved.read_bytes() == raw


def test_input_revision_save_requires_checksum_and_rejects_another_sequence_collision(tmp_path):
    import hashlib

    saved, _, arguments = _seed_saved_input_revision(tmp_path)
    corrected, _ = webapp_check.prepare_assertion_revision(tmp_path, arguments)
    webapp_check.save_verifier(tmp_path, corrected, accumulate=True)
    raw = saved.read_bytes()
    arguments["suite_sha256"] = hashlib.sha256(raw).hexdigest()
    corrected, revision = webapp_check.prepare_assertion_revision(tmp_path, arguments)
    for keywords in ({"replace_sequence": 1}, {"accumulate": True, "replace_sequence": 1}):
        with pytest.raises(ValueError, match="checksum-bound"):
            webapp_check.save_verifier(tmp_path, corrected, **keywords)
    with pytest.raises(ValueError, match="collide"):
        webapp_check.save_verifier(tmp_path, corrected, accumulate=True,
            expected_sha=revision["previous_sha256"], replace_sequence=1,
            expected_page_sha=_checked_page_sha(tmp_path))
    assert saved.read_bytes() == raw


@pytest.mark.parametrize("checked_hash", [None, "", "not-a-sha256", "0" * 64])
def test_input_revision_save_requires_matching_checked_html_source(tmp_path, checked_hash):
    saved, raw, arguments = _seed_saved_input_revision(tmp_path)
    candidate, revision = webapp_check.prepare_assertion_revision(tmp_path, arguments)
    with pytest.raises(ValueError, match="sha256|HTML source changed"):
        webapp_check.save_verifier(tmp_path, candidate, accumulate=True,
            expected_sha=revision["previous_sha256"], replace_sequence=1,
            expected_page_sha=checked_hash)
    assert saved.read_bytes() == raw


@pytest.mark.parametrize("change_when", ["before_save", "while_staging"])
def test_input_revision_source_race_preserves_suite_and_verifier(tmp_path, monkeypatch, change_when):
    saved, raw, arguments = _seed_saved_input_revision(tmp_path)
    candidate, revision = webapp_check.prepare_assertion_revision(tmp_path, arguments)
    actual = webapp_check.check(tmp_path, candidate)
    assert actual["passed"]
    verifier = tmp_path / ".local-ai/verify.ps1"
    original_verifier = verifier.read_bytes()
    source = tmp_path / "index.html"
    if change_when == "before_save":
        source.write_text(PAGE + "\n<!-- changed after check -->")
    else:
        original_write = Path.write_bytes

        def write_and_change(path, data):
            result = original_write(path, data)
            if path == saved.with_suffix(".tmp"):
                source.write_text(PAGE + "\n<!-- changed during publication -->")
            return result

        monkeypatch.setattr(Path, "write_bytes", write_and_change)
    with pytest.raises(ValueError, match="HTML source changed"):
        webapp_check.save_verifier(tmp_path, candidate, accumulate=True,
            expected_sha=revision["previous_sha256"], replace_sequence=1,
            expected_page_sha=actual["page_sha256"])
    assert saved.read_bytes() == raw
    assert verifier.read_bytes() == original_verifier
    assert not saved.with_suffix(".tmp").exists()


def test_nonfillable_input_failure_keeps_earlier_case_outcomes(tmp_path):
    import hashlib

    source = PAGE.replace('<label for="bill">', '<label id="bill-label">')
    (tmp_path / "index.html").write_text(source)
    result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [
        {"expect": {"answer": "120.00"}},
        {"set": {"bill-label": "80"}, "expect": {"answer": "120.00"}},
        {"set": {"bill": "80"}, "expect": {"answer": "96.00"}},
    ]})
    assert not result["passed"]
    assert [case["passed"] for case in result["cases"]] == [True, False, True]
    assert result["failed_case_count"] == 1
    assert result["cases"][1]["failures"][0].startswith("Could not fill #bill-label:")
    assert result["cases"][1]["inputs"] == {"set": {"bill-label": "80"}}
    assert result["cases"][1]["target_inventory"]["targets"]
    assert result["page_sha256"] == hashlib.sha256((tmp_path / "index.html").read_bytes()).hexdigest()
    assert result["javascript_errors"] == []


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


def test_click_evidence_survives_memory_without_resetting_repeated_failure(tmp_path):
    arguments = {"path": "index.html", "cases": [{"click": "Delete", "expect": {"total": "0"}}]}
    case = {"case": 1, "passed": False, "failures": ["total expected 0, got 10"], "inputs": {"click": "Delete"}}
    verdict = {"passed": False, "cases": [case]}
    task_context.record_action(tmp_path, "check_webapp", json.dumps(verdict), arguments)
    case["click_diagnostics"] = {"replay_confirmed": True, "observations": [{
        "project_listener_count": 1, "other_listener_count": 7,
        "listeners": [{"source_path": "index.html", "line": 42, "source_preview": "PRIVATE_SOURCE_BODY"}]}]}
    task_context.record_action(tmp_path, "check_webapp", json.dumps(verdict), arguments)
    saved = task_context.checker_failures(tmp_path, "index.html")[0]
    assert saved["repeats"] == 2
    assert "1 project-file-backed listeners" in saved["details"] and "index.html:42" in saved["details"]
    assert "PRIVATE_SOURCE_BODY" not in saved["details"]
    # Optional malformed or changing observations cannot erase the actual failure.
    case["click_diagnostics"]["observations"] = [None]
    task_context.record_action(tmp_path, "check_webapp", json.dumps(verdict), arguments)
    saved = task_context.checker_failures(tmp_path, "index.html")[0]
    assert saved["repeats"] == 3 and "total expected 0" in saved["details"]


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


STORAGE_PAGE = """<!doctype html><label for="note">Note</label><input id="note">
<button id="save">Save</button><button id="clear">Clear</button><p id="saved"></p><script>
const field=document.getElementById('note'),output=document.getElementById('saved');
output.textContent=localStorage.getItem('memo')||'empty';
document.getElementById('save').onclick=()=>{localStorage.setItem('memo',field.value);output.textContent=field.value;};
document.getElementById('clear').onclick=()=>{localStorage.removeItem('memo');output.textContent='empty';};
</script>"""


def test_reload_cases_verify_storage_writes_and_deletions_in_saved_verifier(tmp_path):
    source = tmp_path / "index.html"
    source.write_text(STORAGE_PAGE, encoding="utf-8")
    spec = {"path": "index.html", "cases": [
        {"set": {"note": "first"}, "click": ["save"], "expect": {"saved": "first"}},
        {"reload": True, "expect": {"saved": "first"}},
        {"reload": True, "set": {"note": "second"}, "click": ["save"], "expect": {"saved": "second"}},
        {"reload": True, "expect": {"saved": "second"}},
        {"click": ["clear"], "expect": {"saved": "empty"}},
        {"reload": True, "expect": {"saved": "empty"}}]}
    assert webapp_check.check(tmp_path, spec)["passed"]
    assert webapp_check.save_verifier(tmp_path, spec, accumulate=True)
    report = webapp_check.run_saved_checks(tmp_path, tmp_path / ".local-ai/webapp-check.json")
    assert report["checks"][0]["input_case_count"] == 6
    assert webapp_check.resolve_saved_report(tmp_path, report)["passed"]
    def run_saved():
        with patch.object(long_tasks, "projects", return_value=[{"id": "sample", "folder": str(tmp_path), "brief": "Test storage persistence"}]):
            return native_agent.execute_tool(tmp_path, "run_verifier", {})
    assert run_saved().startswith("Exit code: 0\n")
    for mutation in [STORAGE_PAGE.replace("localStorage.setItem('memo',field.value)", "void 0"),
                     STORAGE_PAGE.replace("localStorage.removeItem('memo')", "void 0")]:
        source.write_text(mutation, encoding="utf-8")
        checked = webapp_check.check(tmp_path, spec)
        assert not checked["passed"]
        assert any(case.get("inputs", {}).get("reload") for case in checked["cases"] if not case["passed"])
        assert run_saved().startswith("Exit code: 1\n")
    source.write_text(STORAGE_PAGE, encoding="utf-8")
    assert run_saved().startswith("Exit code: 0\n")


@pytest.mark.parametrize("value", [None, 0, 1, "true", "https://example.com", {}, []])
def test_reload_is_a_boolean_not_a_navigation_target(tmp_path, value):
    (tmp_path / "index.html").write_text(STORAGE_PAGE, encoding="utf-8")
    with pytest.raises(ValueError, match="reload"):
        webapp_check.validate_spec(tmp_path, {"path": "index.html", "cases": [{"reload": value, "expect": {"saved": "empty"}}]})


def test_reload_sequences_remain_distinct_and_assertion_revision_cannot_remove_reload(tmp_path):
    import hashlib
    (tmp_path / "index.html").write_text(STORAGE_PAGE, encoding="utf-8")
    ordinary = {"path": "index.html", "cases": [{"expect": {"saved": "empty"}}]}
    reloaded = {"path": "index.html", "cases": [{"reload": True, "expect": {"saved": "empty"}}]}
    import cloud_ai
    tool = next(tool for tool in native_agent.TOOLS if tool["function"]["name"] == "check_webapp")
    response = {"choices": [{"message": {"tool_calls": [{"id": "reload", "function": {
        "name": "check_webapp", "arguments": json.dumps(reloaded)}}]}}]}
    assert cloud_ai.valid_completion(response, {"tools": [tool]})
    suite = webapp_check.saved_suite(tmp_path, ordinary)
    suite = webapp_check.saved_suite(tmp_path, reloaded, suite)
    assert len(suite["checks"]) == 2
    assert webapp_check.save_verifier(tmp_path, reloaded, accumulate=True)
    path = tmp_path / ".local-ai/webapp-check.json"
    raw = path.read_bytes()
    revision = {"path": "index.html", "saved_sequence": 1, "saved_case": 1,
                "suite_sha256": hashlib.sha256(raw).hexdigest(), "cases": [{"expect": {"saved": "empty"}}]}
    corrected, _ = webapp_check.prepare_assertion_revision(tmp_path, revision)
    assert corrected["cases"][0]["reload"] is True
    with pytest.raises(ValueError, match="input actions"):
        webapp_check.prepare_assertion_revision(tmp_path, {**revision, "cases": [{"reload": False, "expect": {"saved": "empty"}}]})
    assert path.read_bytes() == raw
    folded = webapp_check.normalize_spec({"path": "index.html", "cases": [{"reload": True}, {"expect": {"saved": "empty"}}]})
    assert folded["cases"] == reloaded["cases"]


def test_set_selects_dropdown_by_value_or_visible_label(tmp_path):
    (tmp_path / "index.html").write_text("""<!doctype html><select id="kind">
<option value="supplies">Supplies</option><option value="travel">Travel</option></select><p id="chosen"></p>
<script>const select=document.getElementById('kind'),out=document.getElementById('chosen');
select.onchange=()=>out.textContent=select.value; select.onchange();</script>""", encoding="utf-8")
    result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [
        {"set": {"kind": "travel"}, "expect": {"chosen": "travel"}},
        {"set": {"kind": "Supplies"}, "expect": {"chosen": "supplies"}}]})
    assert result["passed"]
    assert len(result["interactions"]) == 2


def test_dynamic_button_failure_reports_missing_listener_without_changing_source(tmp_path):
    source = """<button id="add">Add row</button><table><tbody id="rows"></tbody></table><p id="count">0</p>
    <script>
    document.querySelectorAll('.remove').forEach(button=>button.onclick=()=>{
        button.closest('tr').remove();document.getElementById('count').textContent='0'});
    document.getElementById('add').onclick=()=>{
        document.getElementById('rows').insertAdjacentHTML('beforeend',
            '<tr><td>New item</td><td><button class="remove">Delete</button></td></tr>');
        document.getElementById('count').textContent='1';};
    </script>"""
    path = tmp_path / "index.html"
    path.write_text(source)
    spec = {"path": "index.html", "cases": [
        {"click": ["add"], "expect": {"count": "1"}},
        {"click": [{"button": "Delete", "row_text": "New item"}], "expect": {"count": "0"}}]}
    result = webapp_check.check(tmp_path, spec)
    assert not result["passed"] and result["cases"][0]["passed"]
    diagnostic = result["cases"][1]["click_diagnostics"]
    assert diagnostic["replay_confirmed"], diagnostic
    observed = diagnostic["observations"][0]
    assert observed["project_listener_count"] == 0 and observed["scopes_checked"] >= 5, observed
    assert observed["other_listener_count"] > 0  # Automation handlers are never mislabeled as app code.
    assert observed["at"] == "before_click_attempt" and not observed["listeners"]
    assert path.read_text() == source


@pytest.mark.parametrize("binding,expected_scope,expected_type", [
    ("document.getElementById('remove').onclick=()=>{};", "button#remove", "click"),
    ("document.getElementById('rows').addEventListener('click',()=>{});", "tbody#rows", "click"),
    ("document.addEventListener('click',()=>{});", "document", "click"),
    ("window.addEventListener('click',()=>{});", "window", "click"),
    ("document.getElementById('form').onsubmit=event=>event.preventDefault();", "form#form", "submit"),
])
def test_failed_click_observes_direct_delegated_and_form_listeners(tmp_path, binding, expected_scope, expected_type):
    (tmp_path / "index.html").write_text('''<form id="form"><table><tbody id="rows"><tr>
        <td>Paper</td><td><button id="remove">Delete</button></td></tr></tbody></table></form>
        <p id="count">1</p><script>'''+binding+"</script>")
    result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [
        {"click": ["remove"], "expect": {"count": "0"}}]})
    assert not result["passed"]
    diagnostic = result["cases"][0]["click_diagnostics"]
    assert diagnostic["replay_confirmed"], diagnostic
    observed = diagnostic["observations"][0]
    assert observed["project_listener_count"] == 1, observed
    assert observed["listeners"][0]["scope"] == expected_scope
    assert observed["listeners"][0]["type"] == expected_type


def test_unrelated_descendant_listener_is_not_misreported_as_delegation(tmp_path):
    (tmp_path / "index.html").write_text('''<button id="dead">Delete</button>
        <button id="other" onclick="this.textContent='done'">Other</button><p id="count">1</p>''')
    result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [
        {"click": ["dead"], "expect": {"count": "0"}}]})
    observed = result["cases"][0]["click_diagnostics"]["observations"][0]
    assert observed["project_listener_count"] == 0, observed


def test_passing_click_does_not_replay_or_probe_listeners(tmp_path):
    (tmp_path / "index.html").write_text('''<button id="go" onclick="document.getElementById('count').textContent='0'">Delete</button><p id="count">1</p>''')
    with patch.object(webapp_check, "click_listener_observation", side_effect=AssertionError("Passing checks do not need diagnostics")) as probe:
        result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"click": ["go"], "expect": {"count": "0"}}]})
    assert result["passed"]
    probe.assert_not_called()


def test_optional_probe_failure_does_not_replace_the_failed_behavior_verdict(tmp_path):
    (tmp_path / "index.html").write_text('<button id="dead">Delete</button><p id="count">1</p>')
    with patch.object(webapp_check, "click_listener_observation", side_effect=RuntimeError("CDP unavailable")):
        result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"click": ["dead"], "expect": {"count": "0"}}]})
    assert not result["passed"] and not result["cases"][0]["passed"]
    assert "displayed '1'" in result["cases"][0]["failures"][0]
    assert result["cases"][0]["click_diagnostics"] == {"replay_confirmed": False, "unavailable": "RuntimeError"}


def test_divergent_diagnostic_replay_cannot_replace_original_failure(tmp_path):
    (tmp_path / "index.html").write_text('<button id="dead">Delete</button><p id="count">1</p>')
    def divergent(page, control, target, project_root):
        page.locator("#count").evaluate("el=>el.textContent='0'")
        return {"project_listener_count": 0}
    with patch.object(webapp_check, "click_listener_observation", side_effect=divergent):
        result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"click": ["dead"], "expect": {"count": "0"}}]})
    assert not result["passed"] and "displayed '1'" in result["cases"][0]["failures"][0]
    assert result["cases"][0]["click_diagnostics"] == {"replay_confirmed": False, "observations": []}


def test_click_accepts_an_exact_accessible_button_name_without_an_id(tmp_path):
    (tmp_path / "index.html").write_text("""<button onclick="document.getElementById('result').textContent='added'">Add expense</button><p id="result">empty</p>""")
    checked = webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"click": ["Add expense"], "expect": {"result": "added"}}]})
    assert checked["passed"] and checked["interactions"]


def test_click_does_not_choose_arbitrarily_between_duplicate_button_names(tmp_path):
    (tmp_path / "index.html").write_text("""<button onclick="document.getElementById('result').textContent='wrong'">Delete</button><button>Delete</button><p id="result">untouched</p>""")
    checked = webapp_check.check(tmp_path, {"path": "index.html", "cases": [
        {"click": ["Delete"], "expect": {"result": "untouched"}}, {"expect": {"result": "untouched"}}]})
    assert not checked["passed"] and checked["cases"][1]["passed"]
    assert "ambiguous" in " ".join(checked["cases"][0]["failures"])


def test_click_prefers_an_existing_id_over_a_different_button_name(tmp_path):
    (tmp_path / "index.html").write_text("""<button id="Save" onclick="document.getElementById('result').textContent='id'">Other</button><button onclick="document.getElementById('result').textContent='label'">Save</button><p id="result">untouched</p>""")
    assert webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"click": ["Save"], "expect": {"result": "id"}}]})["passed"]


def test_row_button_checks_deletion_and_saved_deletion_without_ids(tmp_path):
    page = tmp_path / "index.html"
    page.write_text('''<table><tbody></tbody></table><p id="count"></p><script>
        let rows=JSON.parse(localStorage.getItem('rows')||'["Paper","Fuel"]');
        function render(){document.querySelector('tbody').innerHTML=rows.map(name=>
            '<tr><td>'+name+'</td><td><button>Delete</button></td></tr>').join('');
            document.getElementById('count').textContent=String(rows.length)}
        document.addEventListener('click',e=>{if(e.target.tagName==='BUTTON'){
            rows=rows.filter(name=>name!==e.target.closest('tr').cells[0].textContent);
            localStorage.setItem('rows',JSON.stringify(rows));render()}});render();</script>''')
    spec = {"path": "index.html", "cases": [
        {"expect": {"count": "2"}, "visible": ["Paper", "Fuel"]},
        {"click": [{"button": "Delete", "row_text": "Paper"}], "expect": {"count": "1"}, "hidden": ["Paper"], "visible": ["Fuel"]},
        {"reload": True, "expect": {"count": "1"}, "hidden": ["Paper"], "visible": ["Fuel"]}]}
    checked = webapp_check.check(tmp_path, spec)
    assert checked["passed"] and checked["interactions"]
    assert webapp_check.save_verifier(tmp_path, spec, accumulate=True)
    path = tmp_path / ".local-ai/webapp-check.json"
    assert webapp_check.run_saved_checks(tmp_path, path)["passed"]
    source = page.read_text()
    page.write_text(source.replace("rows=rows.filter(name=>name!==e.target.closest('tr').cells[0].textContent);", ""))
    assert not webapp_check.run_saved_checks(tmp_path, path)["passed"]
    page.write_text(source.replace("localStorage.setItem('rows',JSON.stringify(rows));", ""))
    assert not webapp_check.run_saved_checks(tmp_path, path)["passed"]


def test_row_click_is_exact_and_rejects_duplicate_rows_and_duplicate_buttons(tmp_path):
    page = tmp_path / "index.html"
    for body in (
        '<tr><td>Paper extra</td><td><button>Delete</button></td></tr>',
        '<tr><td>Paper</td><td><button>Delete</button></td></tr>' * 2,
        '<tr><td>Paper</td><td><button>Delete</button><button>Delete</button></td></tr>',
    ):
        page.write_text('<table>' + body + '</table><p id="clicked">no</p><script>'
                        "document.addEventListener('click',()=>document.getElementById('clicked').textContent='yes')</script>")
        checked = webapp_check.check(tmp_path, {"path": "index.html", "cases": [
            {"click": [{"button": "Delete", "row_text": "Paper"}], "expect": {"clicked": "no"}}, {"expect": {"clicked": "no"}}]})
        assert not checked["passed"] and checked["cases"][1]["passed"]
        assert "ambiguous" in str(checked["cases"][0]["failures"])


@pytest.mark.parametrize("target", [{}, {"button": "Delete"}, {"button": "Delete", "row_text": " "},
    {"button": "Delete", "row_text": "Paper", "selector": "td"}, {"button": 3, "row_text": "Paper"}])
def test_row_click_descriptor_has_a_bounded_data_only_shape(tmp_path, target):
    (tmp_path / "index.html").write_text('<table></table>')
    with pytest.raises(ValueError, match="row click"):
        webapp_check.validate_spec(tmp_path, {"path": "index.html", "cases": [{"click": [target]}]})


def test_row_click_schema_and_revision_preserve_which_row_was_clicked(tmp_path):
    import hashlib

    import cloud_ai

    (tmp_path / "index.html").write_text('<table><tr><td>Paper</td><td><button>Delete</button></td></tr></table><p id="result">ready</p>')
    spec = {"path": "index.html", "cases": [{"click": [{"button": "Delete", "row_text": "Paper"}], "expect": {"result": "ready"}}]}
    tool = next(tool for tool in native_agent.TOOLS if tool["function"]["name"] == "check_webapp")
    response = {"choices": [{"message": {"tool_calls": [{"id": "row", "function": {"name": "check_webapp", "arguments": json.dumps(spec)}}]}}]}
    assert cloud_ai.valid_completion(response, {"tools": [tool]})
    assert webapp_check.save_verifier(tmp_path, spec, accumulate=True)
    path = tmp_path / ".local-ai/webapp-check.json"
    raw = path.read_bytes()
    revision = {"path": "index.html", "saved_sequence": 1, "saved_case": 1, "suite_sha256": hashlib.sha256(raw).hexdigest(),
                "cases": [{"click": [{"button": "Delete", "row_text": "Fuel"}], "expect": {"result": "ready"}}]}
    with pytest.raises(ValueError, match="input actions"):
        webapp_check.prepare_assertion_revision(tmp_path, revision)
    assert path.read_bytes() == raw


def test_failed_new_case_survives_an_unrelated_passing_check_until_source_is_fixed(tmp_path):
    page = tmp_path / "index.html"
    broken = PAGE.replace("error.hidden=n>0", "error.hidden=true")
    page.write_text(broken)
    good = {"path": "index.html", "cases": [{"set": {"bill": "80"}, "expect": {"answer": "96.00"}}]}
    invalid = {"path": "index.html", "cases": [{"set": {"bill": "0"}, "visible": ["error"]}]}
    unrelated = {"path": "index.html", "cases": [{"set": {"bill": "90"}, "expect": {"answer": "108.00"}}]}
    def run(spec):
        with patch.object(long_tasks, "projects", return_value=[{"id": "retention", "folder": str(tmp_path)}]):
            return json.loads(native_agent.execute_tool(tmp_path, "check_webapp", spec))
    initial = run(good)
    assert initial["passed"], initial
    assert "verification" not in initial and initial["verification_summary"]["passed"]
    failed = run(invalid)
    assert not failed["passed"] and failed["failed_cases_retained"]
    assert failed["verifier_check_passed"] is False
    assert page.read_text() == broken
    repeated = run(unrelated)
    assert not repeated["passed"] and not repeated["verifier_check_passed"]
    assert "verification" not in repeated and not repeated["verification_summary"]["passed"]
    assert any("visibility was wrong" in str(case["failures"]) for case in repeated["cases"])
    saved = json.loads((tmp_path / ".local-ai/webapp-check.json").read_text())
    assert len(saved["checks"]) == 3
    page.write_text(PAGE)
    fixed = run(unrelated)
    assert fixed["passed"] and fixed["verifier_check_passed"]


def test_failed_case_retention_preserves_custom_verifier(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    notes = tmp_path / ".local-ai"
    notes.mkdir()
    verifier = notes / "verify.ps1"
    verifier.write_text("# User suite\nexit 0")
    result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", {"path": "index.html", "cases": [{"expect": {"answer": "wrong"}}]}))
    assert not result["passed"] and result["failed_cases_retained"] is False
    assert verifier.read_text() == "# User suite\nexit 0"
    assert not (notes / "webapp-check.json").exists()


def test_superseded_browser_check_does_not_save_cases_under_new_user_request(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    task_context.step_status(tmp_path)
    original = webapp_check.check
    def changed_request(*args, **kwargs):
        result = original(*args, **kwargs)
        (tmp_path / ".local-ai/USER-REQUESTS.md").write_text("Use different requirements")
        return result
    with patch.object(webapp_check, "check", side_effect=changed_request):
        result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", SPEC))
    assert not result["passed"] and "instructions changed" in str(result["errors"])
    assert not (tmp_path / ".local-ai/verify.ps1").exists()
    assert not (tmp_path / ".local-ai/webapp-check.json").exists()


def test_larger_suite_reuses_process_but_isolates_storage_and_rejects_real_regression(tmp_path):
    page = tmp_path / "index.html"
    page.write_text(STORAGE_PAGE)
    for number in range(10):
        value = f"note-{number}"
        webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [
            {"expect": {"saved": "empty"}},
            {"set": {"note": value}, "click": ["save"], "expect": {"saved": value}},
            {"reload": True, "expect": {"saved": value}}]}, accumulate=True)
    path = tmp_path / ".local-ai/webapp-check.json"
    original = webapp_check.check
    browsers = []
    def observed(folder, spec, **kwargs):
        browsers.append(kwargs.get("_browser"))
        return original(folder, spec, **kwargs)
    with patch.object(webapp_check, "check", side_effect=observed):
        report = webapp_check.run_saved_checks(tmp_path, path)
    assert report["passed"] and report["case_count"] == 30 and len(report["checks"]) == 10
    assert browsers[0] is not None and all(browser is browsers[0] for browser in browsers)
    assert not browsers[0].is_connected()
    # The first assertion in every sequence proves storage isolation. The
    # last assertion proves reload persistence within that isolated sequence.
    page.write_text(STORAGE_PAGE.replace("localStorage.setItem('memo',field.value);", ""))
    failed = webapp_check.run_saved_checks(tmp_path, path)
    assert not failed["passed"] and failed["failed_case_count"] == 10


def test_late_sequence_can_be_revised_and_bound_to_full_verifier(tmp_path):
    import hashlib

    import cloud_ai

    (tmp_path / "index.html").write_text(PAGE)
    for number in range(10):
        webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [
            {"set": {"bill": str(number + 1)}, "expect": {"answer": "wrong" if number == 9 else f"{(number+1)*1.2:.2f}"}}]}, accumulate=True)
    path = tmp_path / ".local-ai/webapp-check.json"
    args = {"path": "index.html", "saved_sequence": 10, "saved_case": 1,
            "suite_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "cases": [{"expect": {"answer": "12.00"}}]}
    tool = next(t for t in native_agent.TOOLS if t["function"]["name"] == "check_webapp")
    response = {"choices": [{"message": {"tool_calls": [{"id": "late", "function": {"name": "check_webapp", "arguments": json.dumps(args)}}]}}]}
    assert cloud_ai.valid_completion(response, {"tools": [tool]})
    with patch.object(long_tasks, "projects", return_value=[{"id": "late", "folder": str(tmp_path)}]):
        result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", args))
    assert result["passed"] and result["verifier_check_passed"] and result["checked_sequence"] == 10
    task_context.record_action(tmp_path, "check_webapp", json.dumps(result), args)
    with project_memory.connection(tmp_path) as conn:
        assert "sequence 10" in conn.execute("SELECT subject FROM task_events ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_large_report_receipt_preserves_all_results_and_rejects_stale_or_redirected_data(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    for number in range(10):
        webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [
            {"set": {"bill": str(number + 1)}, "expect": {"answer": f"{(number+1)*1.2:.2f}"}}]}, accumulate=True)
    report = webapp_check.run_saved_checks(tmp_path, tmp_path / ".local-ai/webapp-check.json")
    # Even when lossy inline elision could fit, transport preserves full
    # interaction metadata through the pinned local report instead.
    soft_limit = len(json.dumps(report, ensure_ascii=True)) - 80
    soft = json.loads(webapp_check.transport_saved_report(tmp_path, report, limit=soft_limit))
    assert "report_ref" in soft
    assert webapp_check.resolve_saved_report(tmp_path, soft)["checks"] == report["checks"]
    encoded = webapp_check.transport_saved_report(tmp_path, report, limit=512)
    receipt = json.loads(encoded)
    assert len(encoded) <= 512 and receipt["report_ref"] == webapp_check.REPORT_NAME
    resolved = webapp_check.resolve_saved_report(tmp_path, receipt)
    assert resolved["passed"] and len(resolved["checks"]) == 10
    # Exercise the worker's real receipt parser without an additional child
    # browser run: this is the exact, already verified report from above.
    dispatch = native_agent.execute_tool
    def receipt_verifier(folder, name, arguments):
        if name == "run_verifier":
            return "Exit code: 0\n" + encoded
        return dispatch(folder, name, arguments)
    latest = json.loads((tmp_path / ".local-ai/webapp-check.json").read_text())["checks"][-1]
    with patch.object(native_agent, "execute_tool", side_effect=receipt_verifier):
        result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", latest))
    assert result["passed"] and result["verifier_check_passed"] and result["verified_paths"] == ["index.html"]
    target = tmp_path / ".local-ai" / webapp_check.REPORT_NAME
    raw = target.read_bytes()
    target.write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="changed after verification"):
        webapp_check.resolve_saved_report(tmp_path, receipt)
    target.write_bytes(raw)
    with patch.object(webapp_check.os, "replace", side_effect=OSError("simulated disk failure")):
        with pytest.raises(OSError, match="disk failure"):
            webapp_check.transport_saved_report(tmp_path, report, limit=512)
    assert target.read_bytes() == raw and not list(target.parent.glob("browser-report-*.tmp"))
    with pytest.raises(ValueError, match="contradicts"):
        webapp_check.resolve_saved_report(tmp_path, {**receipt, "passed": False})
    with pytest.raises(ValueError, match="no bounded local report"):
        webapp_check.resolve_saved_report(tmp_path, {**receipt, "report_ref": "../elsewhere.json"})
    (tmp_path / "index.html").write_text(PAGE + "<!-- changed -->")
    with pytest.raises(ValueError, match="HTML changed"):
        webapp_check.resolve_saved_report(tmp_path, receipt)


def test_expanded_suite_limits_reject_overflow_without_dropping_old_checks(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    for number in range(webapp_check.MAX_SAVED_SEQUENCES):
        webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [
            {"set": {"bill": str(number)}, "expect": {"answer": "any"}}]}, accumulate=True)
    path = tmp_path / ".local-ai/webapp-check.json"
    raw = path.read_bytes()
    with pytest.raises(ValueError, match="at most 32"):
        webapp_check.save_verifier(tmp_path, {"path": "index.html", "cases": [
            {"set": {"bill": "999"}, "expect": {"answer": "any"}}]}, accumulate=True)
    assert path.read_bytes() == raw
    previous = None
    for number in range(10):
        previous = webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [
            {"set": {"bill": str(number)}, "expect": {"answer": "any"}} for _ in range(12)]}, previous)
    with pytest.raises(ValueError, match="128 retained"):
        webapp_check.saved_suite(tmp_path, {"path": "index.html", "cases": [
            {"set": {"bill": "999"}, "expect": {"answer": "any"}} for _ in range(12)]}, previous)


def prepare_existing_html_review_fixture(folder, filename):
    """Record only actual file availability; browser/security gates stay unproven."""
    assert (folder / filename).is_file()
    command = f"if (Test-Path -LiteralPath '{filename}' -PathType Leaf) {{ exit 0 }} else {{ exit 1 }}"
    active = task_context.plan_steps(folder, [{"phase": "Fixture preparation",
        "title": "Confirm the existing HTML fixture is available for browser review",
        "check_command": command, "shell": "powershell"}])["active"]
    checked = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert checked["passed"] and checked["done"] == 1, checked



@pytest.mark.parametrize("bad", [
    {"cases": [{"expect": {"total-display.text": "$0.00"}}]},
    {"cases": [{"set": {"#amount": "7"}, "expect": {"total": "7"}}]},
    {"cases": [{"click": ["#submit"], "expect": {"total": "7"}}]},
    {"cases": [{"state": {"game.constructor.name": "Game"}}]},
    {"cases": [{"state": {"game.total": {"gt": 5, "lt": 3}}}]},
    {"cases": [{"keys": [{"key": "Control+R"}], "expect": {"total": "7"}}]},
    {"viewport_width": 319},
    {"cases": [{"wait_ms": 2000, "expect": {"total": "7"}}] * 6},
])
@pytest.mark.parametrize("operation", ["plan", "revise", "split"])
def test_semantic_browser_acceptance_rejected_before_any_sql_queue_mutation(tmp_path, bad, operation):
    import copy
    arguments = {"path": "future.html", "cases": [{"expect": {"total": "$0.00"}}]}
    step = {"phase": "Totals", "title": "Observe requested total", "check_tool": "check_webapp", "check_arguments": arguments}
    if operation != "plan":
        task_context.plan_steps(tmp_path, [step])
    before = copy.deepcopy(task_context.step_status(tmp_path))
    malformed = copy.deepcopy(step)
    malformed["check_arguments"].update(bad)
    with pytest.raises(ValueError):
        if operation == "plan":
            task_context.plan_steps(tmp_path, [step, malformed])
        elif operation == "revise":
            task_context.revise_step(tmp_path, before["active"]["id"], [malformed])
        else:
            task_context.plan_steps(tmp_path, [step, malformed], split_id=before["active"]["id"])
    assert task_context.step_status(tmp_path) == before
    assert not (tmp_path / "future.html").exists()
    assert not (tmp_path / ".local-ai/webapp-check.json").exists()


@pytest.mark.parametrize("field,token", [("expect", "total-display.text"), ("set", "#amount")])
def test_semantic_browser_preflight_names_wrong_case_key_not_source(tmp_path, field, token):
    arguments = {"path": "future.html", "cases": [{field: {token: "7"}, "mobile_fit": True}]}
    with pytest.raises(ValueError) as error:
        task_context.plan_steps(tmp_path, [{"phase": "Totals", "title": "Observe total", "check_tool": "check_webapp", "check_arguments": arguments}])
    message = str(error.value)
    assert f"cases[0].{field}" in message and token in message
    assert "not application source" in message
    assert "CSS" in message and ".text" in message
    assert task_context.step_status(tmp_path)["total"] == 0



def test_semantic_browser_preflight_preserves_future_page_and_normalized_adjacent_cases(tmp_path):
    import copy
    spec = {"path": "future.html", "viewport_width": 1920, "cases": [
        {"set": {"amount": "7.25"}, "click": ["Add expense"]},
        {"expect": {"total-display": "$7.25", "preview": "<input>"}},
        {"reload": True, "expect": {"total-display": "$7.25"}},
    ]}
    before = copy.deepcopy(spec)
    source_reads = []
    def protect_pages(method):
        def guarded(path, *args, **kwargs):
            if path.suffix.lower() in {".html", ".htm"}:
                source_reads.append(path)
                raise AssertionError("Planning must not read page content")
            return method(path, *args, **kwargs)
        return guarded
    # Containment resolution is allowed; source inspection and execution are not.
    with (patch.object(webapp_check, "check", side_effect=AssertionError("Planning must not run a browser")),
          patch.object(Path, "read_text", protect_pages(Path.read_text)),
          patch.object(Path, "read_bytes", protect_pages(Path.read_bytes))):
        webapp_check.validate_spec_data(webapp_check.normalize_spec(spec))
        queue = task_context.plan_steps(tmp_path, [{"phase": "Totals", "title": "Calculate requested dollars", "check_tool": "check_webapp", "check_arguments": spec}])
    assert source_reads == []
    assert queue["total"] == 1 and queue["done"] == 0
    assert queue["active"]["check_arguments"] == spec == before
    assert not (tmp_path / "future.html").exists()
    with pytest.raises(ValueError, match="HTML file must exist"):
        webapp_check.validate_spec(tmp_path, webapp_check.normalize_spec(spec))


def test_semantic_browser_preflight_bounds_bad_key_diagnostic():
    token = "amount." + "x" * 1000 + "PRIVATE_TRAILING_VALUE"
    with pytest.raises(ValueError) as error:
        webapp_check.validate_spec_data({"cases": [{"set": {token: "unrelated private value"}, "expect": {"total": "7"}}]})
    message = str(error.value)
    assert len(message) < 500 and "cases[0].set" in message
    assert "PRIVATE_TRAILING_VALUE" not in message and "unrelated private value" not in message


def test_semantic_legacy_browser_sql_gets_grounded_revise_only_focus_without_source_mutation(tmp_path):
    source = tmp_path / "index.html"
    source.write_text('<input id="amount"><button id="add">Add expense</button><output id="total-display">$0.00</output>')
    original = source.read_bytes()
    args = {"path": "index.html", "cases": [{"set": {"amount": "7"}, "click": ["add"], "expect": {"total-display": "$7.00"}}]}
    task_context.plan_steps(tmp_path, [{"phase": "Totals", "title": "Calculate the requested total", "check_tool": "check_webapp", "check_arguments": args}])
    args["cases"][0]["expect"] = {"total-display.text": "$7.00"}
    with project_memory.connection(tmp_path) as conn:
        conn.execute("UPDATE task_steps SET check_arguments=?,attempts=1", (json.dumps(args),))
    before = task_context.step_status(tmp_path)
    project = {"id": "legacy", "folder": str(tmp_path), "brief": "Build an offline expense log. Add each submitted amount to the total and preserve entries after reload."}
    state = {"step_focus_turns": 1}
    available = native_agent.acceptance_repair_tools(project, state, native_agent.TOOLS)
    scoped = native_agent.focused_step_repair_request(project, state, available, None, 64000)
    assert scoped and scoped["_native_workload"] == "active_step_checker_repair"
    assert [tool["function"]["name"] for tool in scoped["tools"]] == ["task_steps"]
    assert scoped["tools"][0]["function"]["parameters"]["properties"]["action"]["enum"] == ["revise"]
    briefing = json.dumps(scoped["messages"])
    assert "cases[0].expect" in briefing and "total-display.text" in briefing
    assert "total-display" in briefing and project["brief"] in briefing
    assert task_context.step_status(tmp_path) == before and source.read_bytes() == original



ROW_COUNT_PAGE = """<!doctype html><meta charset="utf-8"><input id="label"><button id="add">Add</button>
<table id="ledger"><thead id="head"><tr><th>Item</th><th>Action</th></tr></thead>
<tbody id="entries">
 <!-- Whitespace, comments and text are not rows. --> 
 </tbody><tfoot id="foot"></tfoot></table>
<div id="fake"></div><table><tbody id="duplicate"></tbody></table><table><tbody id="duplicate"></tbody></table>
<script>
let items=JSON.parse(localStorage.getItem('items')||'[]');
function render(){const body=document.getElementById('entries');body.replaceChildren();
 for(const name of items){const row=body.insertRow();row.insertCell().textContent=name;
 const button=document.createElement('button');button.textContent='Delete';row.insertCell().append(button);
 button.onclick=()=>{items=items.filter(item=>item!==name);save();render();};}}
function save(){localStorage.setItem('items',JSON.stringify(items));}
document.getElementById('add').onclick=()=>{items.push(document.getElementById('label').value);save();render();};
document.getElementById('fake').rows={length:0};render();
</script>"""


def row_count_workflow():
    return {"path": "index.html", "cases": [
        {"row_counts": {"entries": 0, "ledger": 1, "head": 1, "foot": 0}},
        {"set": {"label": "Alpha"}, "click": ["add"], "row_counts": {"entries": 1, "ledger": 2}},
        {"set": {"label": "Beta"}, "click": ["add"], "row_counts": {"entries": 2, "ledger": 3}},
        {"click": [{"button": "Delete", "row_text": "Alpha"}], "row_counts": {"entries": 1, "ledger": 2}},
        {"reload": True, "row_counts": {"entries": 1, "ledger": 2}},
        {"click": [{"button": "Delete", "row_text": "Beta"}], "row_counts": {"entries": 0, "ledger": 1}},
        {"reload": True, "row_counts": {"entries": 0, "ledger": 1}},
    ]}


def test_row_counts_actual_browser_add_delete_reload_and_header_semantics(tmp_path):
    source = tmp_path / "index.html"
    source.write_text(ROW_COUNT_PAGE)
    before = source.read_bytes()
    spec = row_count_workflow()
    result = webapp_check.check(tmp_path, spec)
    assert result["passed"] and result["javascript_errors"] == []
    assert result["cases"][0]["row_counts"]["entries"] == {"actual": 0, "expected": 0, "tag": "tbody"}
    assert result["cases"][0]["row_counts"]["ledger"]["actual"] == 1
    assert result["cases"][0]["row_counts"]["head"]["actual"] == 1
    assert result["cases"][0]["row_counts"]["foot"]["actual"] == 0
    assert {entry["case"] for entry in result["interactions"]} == {2, 3, 4, 6}
    assert all(set(entry["targets"]) == {"rows:entries", "rows:ledger"} for entry in result["interactions"])
    assert {"rows:entries", "rows:ledger", "rows:head", "rows:foot"} <= webapp_check.assertion_targets(spec)
    assert source.read_bytes() == before


@pytest.mark.parametrize("target,expected,actual", [("entries", 1, 0), ("missing", 0, None), ("fake", 0, None), ("duplicate", 0, None)])
def test_row_counts_wrong_missing_duplicate_or_non_table_fails_even_expected_zero(tmp_path, target, expected, actual):
    (tmp_path / "index.html").write_text(ROW_COUNT_PAGE)
    result = webapp_check.check(tmp_path, {"path": "index.html", "cases": [{"row_counts": {target: expected}}]})
    assert not result["passed"] and not result["interactions"]
    case = result["cases"][0]
    assert case["row_counts"][target]["actual"] == actual
    assert "target_inventory" in case and f"#{target}" in case["failures"][0]


@pytest.mark.parametrize("counts", [[], {"entries": True}, {"entries": -1}, {"entries": 10001}, {"entries": 1.0}, {"entries.rows.length": 1}, {"#entries": 1}, {f"table{n}": 0 for n in range(21)}])
def test_row_counts_preflight_is_exact_bounded_data_without_source_reads(counts):
    with patch.object(webapp_check, "_project_file", side_effect=AssertionError("data validation must not read files")):
        with pytest.raises(ValueError):
            webapp_check.validate_spec_data({"cases": [{"row_counts": counts}]})


def test_row_counts_adjacent_normalization_and_valid_bounds_preserve_user_data():
    spec = {"path": "future.html", "cases": [{"click": ["Add"]}, {"row_counts": {"entries": 10000}}]}
    normalized = webapp_check.normalize_spec(spec)
    assert normalized["cases"] == [{"click": ["Add"], "row_counts": {"entries": 10000}}]
    webapp_check.validate_spec_data(normalized)
    assert spec["cases"] == [{"click": ["Add"]}, {"row_counts": {"entries": 10000}}]


def test_row_counts_unchanged_passing_value_does_not_prove_input_effect(tmp_path):
    (tmp_path / "index.html").write_text('<button id="noop">Do nothing</button><table><tbody id="rows"></tbody></table>')
    spec = {"path": "index.html", "cases": [{"click": ["noop"], "row_counts": {"rows": 0}}]}
    result = webapp_check.check(tmp_path, spec)
    assert result["passed"] and not result["interactions"]


def test_row_counts_saved_verifier_replays_real_workflow_and_rejects_broken_add(tmp_path):
    source = tmp_path / "index.html"
    source.write_text(ROW_COUNT_PAGE)
    spec = row_count_workflow()
    assert webapp_check.save_verifier(tmp_path, spec, accumulate=True)
    saved = tmp_path / ".local-ai/webapp-check.json"
    report = webapp_check.run_saved_checks(tmp_path, saved)
    assert report["passed"] and report["case_count"] == 7
    source.write_text(ROW_COUNT_PAGE.replace("items.push(document.getElementById('label').value)", 'void 0'))
    broken = webapp_check.run_saved_checks(tmp_path, saved)
    assert not broken["passed"]
    assert any('Row count #entries' in failure for check in broken["checks"] for case in check["cases"] for failure in case["failures"])


def test_row_counts_suite_accumulation_and_assertion_revision_preserve_other_counts_and_inputs(tmp_path):
    (tmp_path / "index.html").write_text(ROW_COUNT_PAGE)
    first = {"path": "index.html", "cases": [{"set": {"label": "Alpha"}, "click": ["add"], "row_counts": {"entries": 1}}]}
    second = {"path": "index.html", "cases": [{"set": {"label": "Alpha"}, "click": ["add"], "row_counts": {"ledger": 2}}]}
    suite = webapp_check.saved_suite(tmp_path, second, webapp_check.saved_suite(tmp_path, first))
    assert len(suite["checks"]) == 1 and suite["checks"][0]["cases"][0]["row_counts"] == {"entries": 1, "ledger": 2}
    assert webapp_check.save_verifier(tmp_path, suite["checks"][0], accumulate=True)
    saved = tmp_path / ".local-ai/webapp-check.json"
    import hashlib
    args = {"path": "index.html", "saved_sequence": 1, "saved_case": 1,
            "suite_sha256": hashlib.sha256(saved.read_bytes()).hexdigest(), "cases": [{"row_counts": {"ledger": 3}}]}
    repaired, _metadata = webapp_check.prepare_assertion_revision(tmp_path, args)
    case = repaired["cases"][0]
    assert case["row_counts"] == {"entries": 1, "ledger": 3}
    assert case["set"] == {"label": "Alpha"} and case["click"] == ["add"]
    assert not webapp_check.check(tmp_path, repaired)["passed"]



@pytest.mark.parametrize("initial,action", [
    ('<table><tbody id="rows"></tbody></table>', "document.getElementById('rows').replaceWith(Object.assign(document.createElement('table'),{id:'rows'}))"),
    ('', "document.body.append(Object.assign(document.createElement('table'),{id:'rows'}))"),
    ('<div id="rows"></div>', "document.getElementById('rows').replaceWith(Object.assign(document.createElement('table'),{id:'rows'}))"),
    ('<table id="rows"></table><table id="rows"></table>', "document.querySelector('#rows').remove()"),
])
def test_row_counts_unknown_before_or_tag_only_change_cannot_prove_input_delta(tmp_path, initial, action):
    (tmp_path / "index.html").write_text(initial + '<button id="change">Change</button><script>'
        + "document.getElementById('change').onclick=()=>{" + action + "};</script>")
    spec = {"path": "index.html", "cases": [{"click": ["change"], "row_counts": {"rows": 0}}]}
    result = webapp_check.check(tmp_path, spec)
    assert result["passed"] and result["cases"][0]["row_counts"]["rows"]["actual"] == 0
    assert result["interactions"] == []



def seed_retarget_case(folder, *, retained_failures=False):
    import hashlib
    source = folder / "index.html"
    source.write_text(ROW_COUNT_PAGE + '<p id="status">Ready</p><script>window.game={ready:true};</script>')
    first = {"reload": False, "set": {"label": "Alpha"}, "click": ["add"], "wait_ms": 0,
        "expect": {"wrongTarget": "1 row", "status": "Ready"}, "row_counts": {"ledger": 2},
        "state": {"game.ready": True}, "visible": ["status"], "hidden": ["notCreated"], "mobile_fit": True}
    spec = {"path": "index.html", "viewport_width": 400, "cases": [first,
        {"reload": True, "row_counts": {"entries": 99 if retained_failures else 1}, "expect": {"status": "Ready"}}]}
    assert webapp_check.save_verifier(folder, spec, accumulate=True)
    if retained_failures:
        assert webapp_check.save_verifier(folder, {"path": "index.html", "cases": [{"row_counts": {"entries": 7}}]}, accumulate=True)
    saved = folder / ".local-ai/webapp-check.json"
    raw = saved.read_bytes()
    args = {"path": "index.html", "saved_sequence": 1, "saved_case": 1,
        "suite_sha256": hashlib.sha256(raw).hexdigest(),
        "retarget_assertion": {"from": "text:wrongTarget", "to": "rows:entries"},
        "cases": [{"row_counts": {"entries": 1}}]}
    return source, saved, raw, spec, args


def test_retarget_repairs_real_wrong_text_target_to_native_rows_without_source_changes(tmp_path):
    import copy
    source, saved, raw, original, args = seed_retarget_case(tmp_path)
    before_source = source.read_bytes()
    initial = webapp_check.check(tmp_path, original)
    assert not initial["passed"] and "Missing expected element #wrongTarget" in initial["cases"][0]["failures"]
    candidate, revision = webapp_check.prepare_assertion_revision(tmp_path, args)
    expected = copy.deepcopy(original)
    del expected["cases"][0]["expect"]["wrongTarget"]
    expected["cases"][0]["row_counts"]["entries"] = 1
    assert candidate == expected
    assert revision["retarget_assertion"] == args["retarget_assertion"] and revision["replace_sequence"] == 1
    actual = webapp_check.check(tmp_path, candidate)
    assert actual["passed"] and actual["cases"][0]["row_counts"]["entries"]["actual"] == 1
    assert webapp_check.save_verifier(tmp_path, candidate, accumulate=True,
        expected_sha=revision["previous_sha256"], replace_sequence=revision["replace_sequence"], expected_page_sha=actual["page_sha256"])
    suite = json.loads(saved.read_text())
    assert suite["checks"] == [expected]
    assert webapp_check.run_saved_checks(tmp_path, saved)["passed"]
    assert source.read_bytes() == before_source


def test_retarget_selected_case_can_pass_while_other_cases_and_sequences_remain_failed(tmp_path):
    source, saved, raw, original, args = seed_retarget_case(tmp_path, retained_failures=True)
    source_before = source.read_bytes()
    old = json.loads(raw)
    candidate, revision = webapp_check.prepare_assertion_revision(tmp_path, args)
    actual = webapp_check.check(tmp_path, candidate)
    assert actual["cases"][0]["passed"] and not actual["cases"][1]["passed"] and not actual["passed"]
    assert webapp_check.save_verifier(tmp_path, candidate, accumulate=True,
        expected_sha=revision["previous_sha256"], replace_sequence=revision["replace_sequence"], expected_page_sha=actual["page_sha256"])
    stored = json.loads(saved.read_text())
    assert len(stored["checks"]) == 2 and len(stored["checks"][0]["cases"]) == 2
    assert stored["checks"][0]["cases"][1] == old["checks"][0]["cases"][1]
    assert stored["checks"][1] == old["checks"][1]
    checked = webapp_check.run_saved_checks(tmp_path, saved)
    assert not checked["passed"] and checked["case_count"] == 3 and checked["failed_case_count"] == 2
    assert source.read_bytes() == source_before


def test_retarget_failed_actual_candidate_does_not_save_suite_or_verifier(tmp_path):
    source, saved, raw, _original, args = seed_retarget_case(tmp_path)
    source_before = source.read_bytes()
    verifier = tmp_path / ".local-ai/verify.ps1"
    verifier_before = verifier.read_bytes()
    args["cases"] = [{"row_counts": {"entries": 2}}]
    with patch.object(long_tasks, "projects", return_value=[{"id": "retarget", "folder": str(tmp_path)}]):
        result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", args))
    assert result["repaired_case_passed"] is False and not result["passed"] and not result.get("verifier_saved")
    assert saved.read_bytes() == raw and verifier.read_bytes() == verifier_before
    assert source.read_bytes() == source_before and task_context.step_status(tmp_path)["done"] == 0


@pytest.mark.parametrize("invalid", ["missing_from", "same", "collision", "namespace", "css", "extra_mapping", "no_replacement", "wrong_destination", "extra_assertion", "input_action", "revise_true", "revise_false", "boolean_count", "partial_reference", "hidden_contradiction"])
def test_retarget_rejects_ambiguous_waiving_or_action_changing_definitions(tmp_path, invalid):
    source, saved, raw, _original, args = seed_retarget_case(tmp_path)
    before_source = source.read_bytes()
    before_verifier = (tmp_path / ".local-ai/verify.ps1").read_bytes()
    if invalid == "missing_from": args["retarget_assertion"]["from"] = "text:missing"
    elif invalid == "same": args["retarget_assertion"]["to"] = "text:wrongTarget"
    elif invalid == "collision": args["retarget_assertion"]["to"] = "rows:ledger"; args["cases"] = [{"row_counts": {"ledger": 2}}]
    elif invalid == "namespace": args["retarget_assertion"]["from"] = "visibility:wrongTarget"
    elif invalid == "css": args["retarget_assertion"]["to"] = "rows:#entries"
    elif invalid == "extra_mapping": args["retarget_assertion"]["extra"] = True
    elif invalid == "no_replacement": args["cases"] = [{}]
    elif invalid == "wrong_destination": args["cases"] = [{"row_counts": {"head": 1}}]
    elif invalid == "extra_assertion": args["cases"][0]["expect"] = {"status": "Ready"}
    elif invalid == "input_action": args["cases"][0]["click"] = ["add"]
    elif invalid == "revise_true": args["revise_inputs"] = True
    elif invalid == "revise_false": args["revise_inputs"] = False
    elif invalid == "boolean_count": args["cases"][0]["row_counts"]["entries"] = True
    elif invalid == "partial_reference": del args["saved_case"]
    else:
        args["retarget_assertion"]["to"] = "text:notCreated"
        args["cases"] = [{"expect": {"notCreated": "Created"}}]
    with pytest.raises(ValueError):
        webapp_check.prepare_assertion_revision(tmp_path, args)
    assert saved.read_bytes() == raw and source.read_bytes() == before_source
    assert (tmp_path / ".local-ai/verify.ps1").read_bytes() == before_verifier


@pytest.mark.parametrize("source_field,source_target,destination_field,destination_target,replacement", [
    ("expect", "old", "state", "game.total", {"gte": 1}),
    ("state", "game.old", "row_counts", "items", 0),
    ("row_counts", "old", "expect", "items", ""),
])
def test_retarget_supported_namespaces_preserve_every_unrelated_field(source_field, source_target, destination_field, destination_target, replacement):
    import copy
    namespaces = {"expect": "text", "state": "state", "row_counts": "rows"}
    old = {"set": {"amount": "2"}, "click": ["Submit"], "reload": True, "wait_ms": 1,
           "keys": [{"key": "Enter"}], "expect": {"keep": "yes"}, "state": {"game.keep": True},
           "row_counts": {"keep": 3}, "visible": ["keep"], "hidden": ["absent"], "mobile_fit": True}
    old.setdefault(source_field, {})[source_target] = 1 if source_field == "row_counts" else "bad"
    expected = copy.deepcopy(old)
    del expected[source_field][source_target]
    expected.setdefault(destination_field, {})[destination_target] = replacement
    candidate = webapp_check._retarget_assertion_case(old, {destination_field: {destination_target: replacement}},
        {"from": namespaces[source_field] + ":" + source_target, "to": namespaces[destination_field] + ":" + destination_target})
    assert candidate == expected and old[source_field][source_target] == (1 if source_field == "row_counts" else "bad")


@pytest.mark.parametrize("invalid", ["stale", "request", "custom", "viewport"])
def test_retarget_preserves_existing_reference_and_ownership_guards(tmp_path, invalid):
    _source, saved, raw, _original, args = seed_retarget_case(tmp_path)
    if invalid == "stale": args["suite_sha256"] = "0" * 64
    elif invalid == "request": (tmp_path / ".local-ai/USER-REQUESTS.md").write_text("New user requirements")
    elif invalid == "custom": (tmp_path / ".local-ai/verify.ps1").write_text("exit 0")
    else: args["viewport_width"] = 800
    with pytest.raises(ValueError):
        webapp_check.prepare_assertion_revision(tmp_path, args)
    assert saved.read_bytes() == raw


@pytest.mark.parametrize("race", ["source", "source_while_staging", "request", "suite"])
def test_retarget_actual_checked_candidate_cannot_overwrite_newer_source_request_or_suite(tmp_path, monkeypatch, race):
    source, saved, raw, _original, args = seed_retarget_case(tmp_path)
    candidate, revision = webapp_check.prepare_assertion_revision(tmp_path, args)
    actual = webapp_check.check(tmp_path, candidate)
    assert actual["passed"]
    verifier = tmp_path / ".local-ai/verify.ps1"
    before_verifier = verifier.read_bytes()
    if race == "source": source.write_text(source.read_text() + "<!-- newer source -->")
    elif race == "request": (tmp_path / ".local-ai/USER-REQUESTS.md").write_text("New user request")
    elif race == "suite": saved.write_bytes(raw + b"\n")
    else:
        original_write = Path.write_bytes
        def mutate_after_staging(path, data):
            result = original_write(path, data)
            if path == saved.with_suffix(".tmp"):
                source.write_text(source.read_text() + "<!-- changed during staging -->")
            return result
        monkeypatch.setattr(Path, "write_bytes", mutate_after_staging)
    expected_saved = saved.read_bytes()
    with pytest.raises(ValueError):
        webapp_check.save_verifier(tmp_path, candidate, accumulate=True,
            expected_sha=revision["previous_sha256"], replace_sequence=revision["replace_sequence"], expected_page_sha=actual["page_sha256"])
    assert saved.read_bytes() == expected_saved and verifier.read_bytes() == before_verifier



@pytest.mark.parametrize("mapping,replacement", [
    (None, {"row_counts": {"items": 1}}),
    ({}, {"row_counts": {"items": 1}}),
    ({"from": "text:wrongTarget"}, {"row_counts": {"items": 1}}),
    ({"from": [], "to": "rows:items"}, {"row_counts": {"items": 1}}),
    ({"from": "text:wrongTarget", "to": "state:game.constructor.x"}, {"state": {"game.constructor.x": 1}}),
    ({"from": "text:wrongTarget", "to": "rows:items"}, {"row_counts": {"items": 1, "other": 0}}),
    ({"from": "text:wrongTarget", "to": "state:game.total"}, {"state": {"game.total": {}}}),
])
def test_retarget_requires_a_single_valid_typed_mapping_and_concrete_replacement(tmp_path, mapping, replacement):
    _source, saved, raw, _original, args = seed_retarget_case(tmp_path)
    args["retarget_assertion"] = mapping
    args["cases"] = [replacement]
    with pytest.raises(ValueError):
        webapp_check.prepare_assertion_revision(tmp_path, args)
    assert saved.read_bytes() == raw


def test_retarget_namespace_identity_does_not_remove_same_named_other_observables():
    old = {"expect": {"items": "wrong", "status": "Ready"}, "row_counts": {"items": 3},
           "state": {"game.items": 3}, "visible": ["items"], "hidden": ["absent"]}
    candidate = webapp_check._retarget_assertion_case(old, {"row_counts": {"entries": 3}},
        {"from": "text:items", "to": "rows:entries"})
    assert candidate == {"expect": {"status": "Ready"}, "row_counts": {"items": 3, "entries": 3},
                         "state": {"game.items": 3}, "visible": ["items"], "hidden": ["absent"]}
    assert old["expect"]["items"] == "wrong"



def seed_ordinary_partial_case(folder):
    import hashlib
    source = folder / "index.html"
    source.write_text('<input id="entry"><p id="actual">Ready</p>')
    spec = {"path": "index.html", "cases": [
        {"set": {"entry": "Alpha"}, "visible": ["wrong"], "expect": {"actual": "Ready"}, "hidden": ["notCreated"]},
        {"expect": {"bad": "oops"}},
    ]}
    assert webapp_check.save_verifier(folder, spec, accumulate=True)
    saved = folder / ".local-ai/webapp-check.json"
    raw = saved.read_bytes()
    args = {"path": "index.html", "saved_sequence": 1, "saved_case": 1,
            "suite_sha256": hashlib.sha256(raw).hexdigest(), "cases": [{"visible": ["actual"]}]}
    return source, saved, raw, spec, args


def test_ordinary_partial_assertion_repair_persists_selected_case_without_waiving_later_failure(tmp_path):
    source, saved, raw, spec, args = seed_ordinary_partial_case(tmp_path)
    before_source = source.read_bytes()
    with patch.object(long_tasks, "projects", return_value=[{"id": "partial", "folder": str(tmp_path)}]):
        result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", args))
    assert result.get("repaired_case_passed") is True and result.get("verifier_saved") is True
    assert not result["own_batch_passed"] and not result["passed"] and not result["verifier_check_passed"]
    suite = json.loads(saved.read_text())
    expected = {**spec["cases"][0], "visible": ["actual"]}
    assert suite["checks"][0]["cases"] == [expected, spec["cases"][1]]
    assert source.read_bytes() == before_source and task_context.step_status(tmp_path)["done"] == 0


def test_bound_ordinary_partial_assertion_updates_sql_definition_but_cannot_complete_bad_batch(tmp_path):
    source, saved, raw, spec, args = seed_ordinary_partial_case(tmp_path)
    before_source = source.read_bytes()
    active = task_context.plan_steps(tmp_path, [{"phase": "UI", "title": "Check requested UI", "check_tool": "check_webapp", "check_arguments": spec}])["active"]
    bound = native_agent.bind_browser_assertion_revision(tmp_path, args, active, task_context.request_identity(tmp_path))
    with patch.object(long_tasks, "projects", return_value=[{"id": "partial", "folder": str(tmp_path)}]):
        result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", bound))
        queue = task_context.step_status(tmp_path)
        assert result.get("active_step_check_aligned") is True
        assert queue["done"] == 0 and queue["active"]["check_arguments"]["cases"][0]["visible"] == ["actual"]
        assert queue["active"]["check_arguments"]["cases"][1] == spec["cases"][1]
        complete = json.loads(native_agent.execute_tool(tmp_path, "task_steps", {"action": "complete", "step_id": queue["active"]["id"]}))
    assert not complete["passed"] and task_context.step_status(tmp_path)["done"] == 0
    assert source.read_bytes() == before_source



def test_ordinary_partial_failed_selected_case_does_not_save_or_align(tmp_path):
    source, saved, raw, spec, args = seed_ordinary_partial_case(tmp_path)
    before_source = source.read_bytes()
    verifier = tmp_path / ".local-ai/verify.ps1"
    before_verifier = verifier.read_bytes()
    active = task_context.plan_steps(tmp_path, [{"phase": "UI", "title": "Check requested UI", "check_tool": "check_webapp", "check_arguments": spec}])["active"]
    args["cases"] = [{"visible": ["stillMissing"]}]
    bound = native_agent.bind_browser_assertion_revision(tmp_path, args, active, task_context.request_identity(tmp_path))
    with patch.object(long_tasks, "projects", return_value=[{"id": "partial", "folder": str(tmp_path)}]):
        result = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", bound))
    assert result["repaired_case_passed"] is False and not result.get("verifier_saved")
    assert result["active_step_check_aligned"] is False
    assert saved.read_bytes() == raw and verifier.read_bytes() == before_verifier
    assert source.read_bytes() == before_source and task_context.step_status(tmp_path)["active"] == active


@pytest.mark.parametrize("race", ["request", "source", "suite", "custom_verifier"])
def test_ordinary_partial_actual_candidate_respects_newer_request_source_suite_and_owner(tmp_path, race):
    source, saved, raw, _spec, args = seed_ordinary_partial_case(tmp_path)
    verifier = tmp_path / ".local-ai/verify.ps1"
    original_verifier = verifier.read_bytes()
    real_check = webapp_check.check
    expected = {}
    def check_then_change(folder, candidate, *extra, **kwargs):
        checked = real_check(folder, candidate, *extra, **kwargs)
        assert checked["cases"][0]["passed"] and not checked["passed"]
        if race == "request": (tmp_path / ".local-ai/USER-REQUESTS.md").write_text("New user request")
        elif race == "source": source.write_text(source.read_text() + "<!-- source changed -->")
        elif race == "suite": saved.write_bytes(raw + b"\n")
        else: verifier.write_text("# User-owned verifier\nexit 0")
        expected["suite"] = saved.read_bytes()
        expected["verifier"] = verifier.read_bytes()
        return checked
    with patch.object(webapp_check, "check", side_effect=check_then_change), patch.object(long_tasks, "projects", return_value=[{"id": "partial", "folder": str(tmp_path)}]):
        result = native_agent.execute_tool(tmp_path, "check_webapp", args)
    if not result.startswith("Tool error:"):
        parsed = json.loads(result)
        assert not parsed["passed"] and not parsed.get("verifier_saved")
    assert saved.read_bytes() == expected["suite"] and verifier.read_bytes() == expected["verifier"]
    assert task_context.step_status(tmp_path)["done"] == 0


@pytest.mark.parametrize("operation", ["plan", "revise", "split"])
@pytest.mark.parametrize("bad_path", ["/index.html", r"\index.html", "C:index.html",
    r"C:\outside.html", r"\\server\share\outside.html", "../outside.html", "page.txt"])
def test_browser_plan_path_rejects_before_sql_mutation(tmp_path, operation, bad_path):
    page = tmp_path / "index.html"
    page.write_text('<p id="status">Ready</p>')
    step = {"phase": "prepare", "title": "Check the page", "check_tool": "check_webapp",
            "check_arguments": {"path": "index.html", "cases": [{"visible": ["status"]}]}}
    queue = task_context.plan_steps(tmp_path, [step])
    with project_memory.connection(tmp_path) as conn:
        conn.execute("UPDATE task_steps SET attempts=2,evidence='Existing failure evidence',status=? WHERE id=?",
                     ("done" if operation == "plan" else "active", queue["active"]["id"]))
    def rows():
        with project_memory.connection(tmp_path) as conn:
            return [tuple(row) for row in conn.execute("SELECT * FROM task_steps ORDER BY id")]
    before = rows()
    bad = {**step, "check_arguments": {**step["check_arguments"], "path": bad_path}}
    with pytest.raises(ValueError, match="relative HTML|HTML file in this project"):
        if operation == "revise":
            task_context.revise_step(tmp_path, queue["active"]["id"], [bad])
        elif operation == "split":
            task_context.plan_steps(tmp_path, [bad, step], split_id=queue["active"]["id"])
        else:
            task_context.plan_steps(tmp_path, [bad])
    assert rows() == before
    assert page.read_text() == '<p id="status">Ready</p>'
    assert not (tmp_path / ".local-ai/webapp-check.json").exists()


def test_browser_plan_path_permits_future_contained_html_and_execution_requires_file(tmp_path):
    future = "not-created/pages/future.html"
    arguments = {"path": future, "cases": [{"visible": ["future-control"]}]}
    queue = task_context.plan_steps(tmp_path, [{"phase": "future", "title": "Check future page",
        "check_tool": "check_webapp", "check_arguments": arguments}])
    assert queue["active"]["check_arguments"] == arguments
    assert not (tmp_path / future).exists()
    assert webapp_check._project_file(tmp_path, future, must_exist=False) == (tmp_path / future).resolve()
    with pytest.raises(ValueError, match="must exist"):
        webapp_check.validate_spec(tmp_path, arguments)
    (tmp_path / "index.html").write_text('<p id="status">Ready</p>')
    assert webapp_check.validate_spec(tmp_path, {**arguments, "path": "index.html"}) == (tmp_path / "index.html").resolve()
    with pytest.raises(ValueError, match="relative HTML"):
        webapp_check.validate_spec(tmp_path, {**arguments, "path": str(tmp_path / "index.html")})


def test_browser_plan_path_rejects_resolved_symlink_escape_without_reading_source(tmp_path, monkeypatch):
    folder = tmp_path / "project"
    folder.mkdir()
    alias = folder / "alias.html"
    outside = tmp_path / "private.html"
    outside.write_text("private source must not be read")
    resolve = Path.resolve
    def redirected(path, *args, **kwargs):
        return outside if path == alias else resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", redirected)
    step = {"phase": "future", "title": "Check page", "check_tool": "check_webapp",
            "check_arguments": {"path": "alias.html", "cases": [{"visible": ["status"]}]}}
    with patch.object(Path, "read_bytes", side_effect=AssertionError("No source bytes during planning")):
        with pytest.raises(ValueError, match="HTML file in this project"):
            task_context.plan_steps(folder, [step])
    assert not (folder / ".local-ai/memory.sqlite3").exists()


def test_browser_plan_path_rejects_actual_outside_symlink(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_text("outside")
    try:
        (folder / "alias.html").symlink_to(outside)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Symlinks unavailable: {error}")
    with pytest.raises(ValueError, match="HTML file in this project"):
        task_context.plan_steps(folder, [{"phase": "check", "title": "Check page", "check_tool": "check_webapp",
            "check_arguments": {"path": "alias.html", "cases": [{"visible": ["status"]}]}}])


@pytest.mark.parametrize("bad_path", ["/index.html", r"\index.html", "C:index.html", "", "bad\x00.html"])
def test_browser_plan_path_legacy_definition_validation_is_pure_and_precise(tmp_path, bad_path):
    with patch.object(Path, "read_bytes", side_effect=AssertionError("No source reads")):
        with pytest.raises(ValueError, match="relative HTML path.*check_arguments.path"):
            task_context.validate_repeatable_check("check_webapp", {
                "path": bad_path, "cases": [{"visible": ["status"]}]})
    assert not (tmp_path / ".local-ai/memory.sqlite3").exists()


def test_browser_plan_path_absolute_inside_project_keeps_relative_only_contract(tmp_path):
    page = tmp_path / "index.html"
    page.write_text('<p id="status">Ready</p>')
    with pytest.raises(ValueError, match="relative HTML path"):
        task_context.plan_steps(tmp_path, [{"phase": "page", "title": "Check page",
            "check_tool": "check_webapp", "check_arguments": {
                "path": str(page), "cases": [{"visible": ["status"]}]}}])
    assert not (tmp_path / ".local-ai/memory.sqlite3").exists()


def test_browser_plan_path_format_only_developer_probe_keeps_pure_compatibility():
    step = {"phase": "future", "title": "Check future page", "check_tool": "check_webapp",
            "check_arguments": {"path": "future.html", "cases": [{"visible": ["future-control"]}]}}
    # The developer format probe has no project folder and never publishes SQL.
    assert len(task_context._validate_steps([step])) == 1
    with pytest.raises(ValueError, match="relative HTML path"):
        task_context._validate_steps([{**step, "check_arguments": {
            **step["check_arguments"], "path": "/index.html"}}])


# Scoped host evidence port: original accepted85552a5 regressions.

def test_persistence_metadata_budget_is_atomic_bounded_and_does_not_mutate_results():
    import copy,json
    noise=[{'target':'text:noise'+str(index),'effect':'mutation','action_case':1,'reload_case':2} for index in range(32)]
    pair=[{'target':'text:records','effect':effect,'action_case':case,'reload_case':case+1}
          for effect,case in [('creation',1),('removal',3)]]
    results=[{'path':'index.html','passed':True,'persistence_witnesses':noise},
             {'path':'index.html','passed':False,'persistence_witnesses':pair}]
    before=copy.deepcopy(results)
    selected=webapp_check._select_persistence_witnesses(results,max_items=2)
    assert len(selected)==2 and {row['effect'] for row in selected}=={'creation','removal'}
    assert all(row['sequence']==2 for row in selected)
    assert results==before
    tiny=webapp_check._select_persistence_witnesses(results,max_bytes=1)
    assert tiny==[]
    assert len(json.dumps(webapp_check._select_persistence_witnesses(results)).encode())<=24000


def test_persistence_witness_metadata_cannot_turn_real_browser_pass_into_output_error(tmp_path):
    import copy,json
    spans=''.join('<span id="n'+str(index)+'">0</span>' for index in range(32))
    (tmp_path/'index.html').write_text('<input id=seed><button id=fill>Fill</button>'+spans+"<script>function render(v){for(let i=0;i<32;i++)document.getElementById('n'+i).textContent=v;}render(localStorage.getItem('n')||'0');document.getElementById('fill').onclick=()=>{const v=document.getElementById('seed').value;localStorage.setItem('n',v);render(v);};</script>")
    first={'n'+str(index):'1' for index in range(16)}
    second={'n'+str(index):'2' for index in range(16,32)}
    spec={'path':'index.html','cases':[{'set':{'seed':'1'},'click':['fill'],'expect':first},
        {'reload':True,'expect':first},{'set':{'seed':'2'},'click':['fill'],'expect':second},
        {'reload':True,'expect':second}]}
    result=webapp_check.check(tmp_path,spec)
    assert result['passed'] and len(result['persistence_witnesses'])==32
    original=copy.deepcopy(result)
    encoded=webapp_check.serialize_result(result)
    preview=json.loads(encoded)
    assert len(encoded)<=16000 and preview['passed'] and preview['failed_case_count']==0
    assert len(preview['cases'])==4  # Optional hashes cannot crowd out actual verdicts.
    assert preview['persistence_witnesses_limited'] is True
    assert preview['persistence_witness_count']==32
    assert result==original  # Full host evidence/receipts are never truncated.
