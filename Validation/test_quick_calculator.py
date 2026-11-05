"""Quick arithmetic uses exact, bounded math and never evaluates code."""

from unittest.mock import patch

import chat_assistant
from quick_calculator import calculate


def test_percentage_is_exact_and_skips_the_model():
    with patch.object(chat_assistant, "local_answer") as model:
        answer = chat_assistant.answer_with_tools("What is 17.35% of 2479.80?", [])
    assert answer == "17.35% of 2479.80 = 430.2453"
    model.assert_not_called()


def test_parentheses_thousands_and_repeating_division():
    assert calculate("Calculate (1,200 + 45) * 2") == "(1,200 + 45) * 2 = 2490"
    assert calculate("What is 1/3?") == "1/3 = 1/3 (about 0.3333333333333333)"
    assert calculate("What is 2^8?") == "2^8 = 256"
    assert calculate("What is 2/0?") == "Cannot divide by zero."


def test_code_and_prose_are_not_evaluated():
    assert calculate("__import__('os').system('echo bad')") is None
    assert calculate("How much is sales tax in Michigan today?") is None
    assert calculate("1,23 + 4") is None
    assert calculate("2**1000000") is None


def test_restricted_helper_computes_json_and_exact_decimal_totals_without_executing_code():
    import quick_calculator as helper
    definition = helper.normalize_helper("invoice_total", {"amounts": "numbers", "tax": "number"},
        '{"subtotal": sum(amounts), "total": round(sum(amounts) * (1 + tax), 2)}')
    assert helper.run_helper(definition, {"amounts": [0.1, 0.2, 12.5], "tax": 0.06}) == {"subtotal": 12.8, "total": 13.57}
    summary = helper.normalize_helper("label", {"data": "object", "enabled": "boolean"},
        '{"label": get(data, "name", "Unknown"), "count": len(data)} if enabled else {"label": "Disabled", "count": 0}')
    assert helper.run_helper(summary, {"data": {"name": "Cale", "items": [1, 2]}, "enabled": True}) == {"label": "Cale", "count": 2}


def test_restricted_helper_rejects_executable_syntax_even_in_unselected_branches():
    import pytest
    import quick_calculator as helper
    for expression in ['__import__("os").system("bad")', 'value.__class__', '(lambda: 1)()',
                       '[x for x in value]', '1 if True else open("private")',
                       'get.__globals__', '(x := 1)', 'value[0:2]', 'import os',
                       'sum(value, start=0)', '{**value}', 'eval("1")']:
        with pytest.raises(ValueError):
            helper.normalize_helper("unsafe", {"value": "json"}, expression)


def test_restricted_helper_type_and_resource_budgets_reject_fast():
    import pytest
    import quick_calculator as helper
    definition = helper.normalize_helper("power", {"value": "number"}, 'value ** 1000000')
    with pytest.raises(ValueError, match="Exponent"):
        helper.run_helper(definition, {"value": 2})
    for value in [True, "2", float("inf"), [1], {"x": 1}]:
        with pytest.raises(ValueError): helper.run_helper(definition, {"value": value})
    with pytest.raises(ValueError): helper.run_helper(definition, {"value": 2, "extra": 1})
    deep = []
    for _ in range(12): deep = [deep]
    with pytest.raises(ValueError): helper.helper_json(deep)
    for expression in ['"x" * 1000000', '10 ** 20 ** 20', 'value / 0']:
        with pytest.raises(ValueError): helper.run_helper(helper.normalize_helper("bounded", {"value": "number"}, expression), {"value": 1})
    with pytest.raises(ValueError): helper.normalize_helper("bad", {"value": []}, 'value')
    with pytest.raises(ValueError): helper.normalize_helper("large", {"value": "number"}, ' + '.join(['value'] * 100))



def test_helper_case_matching_preserves_boolean_types_and_json_numeric_equivalence():
    from quick_calculator import helper_values_match
    assert helper_values_match({"total": 100}, {"total": 100.0})
    assert not helper_values_match(True, 1) and not helper_values_match([False], [0])


def test_helper_import_attestation_rejects_stale_executing_module_against_new_disk_source(tmp_path):
    import types
    import pytest
    import quick_calculator as helper
    from pathlib import Path
    trusted=Path(helper.__file__).read_bytes()
    path=tmp_path/'stale_interpreter.py'
    # Model the loader compiling old trusted code just before an update swaps
    # disk bytes. Only this copied module is executed, never a supplied helper.
    loaded=compile(trusted,str(path),'exec')
    path.write_bytes(trusted+b'\nUPDATED_IMPLEMENTATION=True\n')
    module=types.ModuleType('stale_helper_probe');module.__file__=str(path)
    exec(loaded,module.__dict__)
    assert module._HELPER_IMPORTED_IMPLEMENTATION is None
    with pytest.raises(ValueError,match='interpreter changed'):
        module.helper_interpreter_identity()


import pytest


@pytest.mark.parametrize('expression,parameters,inputs,expected',[
    ('sum([1/x,1/x,1/x])',{'x':'integer'},{'x':3},1),
    ("get({'third':1/x},'third')*x",{'x':'integer'},{'x':3},1),
    ('items[i+1]',{'items':'numbers','i':'integer'},{'items':[10,20,30],'i':1},30),
    ('round(x,digits+1)',{'x':'number','digits':'integer'},{'x':1.2345,'digits':1},1.23),
])
def test_expression_composition_preserves_exact_intermediates(expression,parameters,inputs,expected):
    import quick_calculator as calculator
    definition=calculator.normalize_helper('composed',parameters,expression)
    assert calculator.run_helper(definition,inputs)==expected

@pytest.mark.parametrize('expression,parameters,inputs',[
    ('items[i]',{'items':'numbers','i':'number'},{'items':[10,20],'i':1.0}),
    ('items[i]',{'items':'numbers','i':'boolean'},{'items':[10,20],'i':True}),
    ('items[i/2]',{'items':'numbers','i':'integer'},{'items':[10,20],'i':1}),
    ('round(x,d)',{'x':'number','d':'number'},{'x':1.234,'d':2.0}),
    ('round(x,d)',{'x':'number','d':'boolean'},{'x':1.234,'d':True}),
    ('round(x,d/2)',{'x':'number','d':'integer'},{'x':1.234,'d':3}),
    ('round(x,d+1)',{'x':'number','d':'integer'},{'x':1.234,'d':8}),
])
def test_expression_integral_composition_does_not_relax_structural_types(expression,parameters,inputs):
    import quick_calculator as calculator
    with pytest.raises(ValueError):calculator.run_helper(calculator.normalize_helper('bounded',parameters,expression),inputs)
