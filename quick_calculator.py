"""Exact, bounded arithmetic for short chat questions.

Only a small numeric grammar is accepted. Python code, names, function calls,
and long expressions fall back to ordinary chat rather than being evaluated.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal, localcontext
from fractions import Fraction

NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
PERCENT = re.compile(rf"(\$?{NUMBER})\s*(?:%|percent)\s+of\s+(\$?{NUMBER})", re.I)
PREFIX = re.compile(r"^(?:what(?:'s| is)|how much is|calculate|compute|evaluate)\s+", re.I)
ALLOWED = re.compile(r"[\d\s.,+*/()\-^×÷]+")


def _bounded(value: Fraction) -> Fraction:
    """Keep exact fractions small enough for an instant desktop reply."""
    if len(str(abs(value.numerator))) > 100 or len(str(value.denominator)) > 100:
        raise ValueError("Calculation exceeds the quick calculator limit.")
    return value


def _evaluate(node: ast.AST, expression: str, depth: int = 0) -> Fraction:
    if depth > 20:
        raise ValueError("Expression is too deeply nested.")
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        literal = ast.get_source_segment(expression, node)
        if not literal or len(literal) > 32:
            raise ValueError("Number is too long.")
        return _bounded(Fraction(Decimal(literal)))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand, expression, depth + 1)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
    ):
        left = _evaluate(node.left, expression, depth + 1)
        right = _evaluate(node.right, expression, depth + 1)
        if isinstance(node.op, ast.Add):
            return _bounded(left + right)
        if isinstance(node.op, ast.Sub):
            return _bounded(left - right)
        if isinstance(node.op, ast.Mult):
            return _bounded(left * right)
        if isinstance(node.op, ast.Div):
            return _bounded(left / right)
        if isinstance(node.op, ast.Pow) and right.denominator == 1 and abs(right) <= 100:
            return _bounded(left ** right.numerator)
    raise ValueError("Unsupported arithmetic expression.")


def _display(value: Fraction) -> str:
    """Show terminating decimals exactly; retain a fraction for repeating ones."""
    denominator = value.denominator
    decimal_places = 0
    for factor in (2, 5):
        exponent = 0
        while denominator % factor == 0:
            denominator //= factor
            exponent += 1
        decimal_places = max(decimal_places, exponent)
    with localcontext() as context:
        # Scaling to a power-of-ten denominator adds at most this many digits.
        context.prec = (
            len(str(abs(value.numerator))) + decimal_places
            if denominator == 1 else 16
        )
        decimal = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    if denominator == 1:
        return decimal.rstrip("0").rstrip(".") if "." in decimal else decimal
    return f"{value.numerator}/{value.denominator} (about {decimal})"


def calculate(question: str) -> str | None:
    """Return a deterministic answer for explicit arithmetic, or None."""
    expression = PREFIX.sub("", question.strip().rstrip("?"), count=1).strip()
    if not expression or len(expression) > 160:
        return None
    percent = PERCENT.fullmatch(expression)
    if percent:
        left, right = (Fraction(Decimal(part.replace(",", "").replace("$", ""))) for part in percent.groups())
        try:
            answer = _display(_bounded(left * right / 100))
        except ValueError:
            return None
        return f"{expression} = {'$' if '$' in percent.group(2) else ''}{answer}"
    if not ALLOWED.fullmatch(expression) or not any(op in expression for op in "+-*/^×÷"):
        return None
    # Validate complete numeric tokens before removing thousands separators.
    for token in re.findall(r"[\d.,]+", expression):
        if "," in token and not re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d*)?", token):
            return None
    normalized = expression.replace(",", "")
    # Chat integers are decimal, even when padded with leading zeros.
    # Match complete numeric tokens so fractional digits are never rewritten.
    normalized = re.sub(
        r"[\d.]+",
        lambda match: (match.group(0) if "." in match.group(0)
                       else match.group(0).lstrip("0") or "0"),
        normalized,
    )
    # Keep multiplication tokens separate so mixed/repeated symbols cannot form **.
    normalized = normalized.replace("×", " * ").replace("÷", "/").replace("^", "**")
    # Reject unsupported floor division before any subtree can raise an error.
    if "//" in normalized:
        return None
    try:
        tree = ast.parse(normalized, mode="eval")
        # Reject unsupported syntax before any sibling subtree is evaluated.
        allowed_nodes = (
            ast.Expression, ast.Constant, ast.UnaryOp, ast.BinOp,
            ast.UAdd, ast.USub, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
        )
        if any(
            not isinstance(node, allowed_nodes)
            or (isinstance(node, ast.Constant) and type(node.value) not in (int, float))
            for node in ast.walk(tree)
        ):
            return None
        return f"{expression} = {_display(_evaluate(tree.body, normalized))}"
    except (SyntaxError, ValueError, OverflowError):
        return None
    except ZeroDivisionError:
        return "Cannot divide by zero."


HELPER_FORMAT = 'cale-json-expression-v1'
HELPER_TYPES = frozenset({'number', 'integer', 'string', 'boolean', 'numbers', 'strings', 'array', 'object', 'json'})
_HELPER_CALLS = frozenset({'abs', 'round', 'sum', 'min', 'max', 'len', 'get'})


def helper_json(value, *, _preserve_fractions=False):
    """Copy finite plain JSON within fixed depth, node, scalar and byte limits."""
    import json
    import math
    count = 0
    def visit(item, depth):
        nonlocal count
        count += 1
        if count > 512 or depth > 8:
            raise ValueError('Helper JSON exceeds its node/depth limit.')
        if item is None or type(item) is bool:
            return item
        if type(item) in (int, float, Fraction):
            if abs(item) > 1e100 or not math.isfinite(item):
                raise ValueError('Helper numbers must be finite and bounded.')
            if isinstance(item,Fraction) and not _preserve_fractions:
                item=item.numerator if item.denominator==1 else float(item)
            return item
        if type(item) is str and len(item) <= 1000:
            return item
        if type(item) is list and len(item) <= 256:
            return [visit(child, depth + 1) for child in item]
        if type(item) is dict and len(item) <= 64 and all(type(key) is str and len(key) <= 100 for key in item):
            return {key: visit(child, depth + 1) for key, child in item.items()}
        raise ValueError('Helpers accept only bounded plain JSON values.')
    result = visit(value, 0)
    # Size the JSON representation without rounding internal rational values.
    def encode_fraction(item):
        if type(item) is Fraction:return item.numerator if item.denominator==1 else float(item)
        raise ValueError('Helpers accept only bounded plain JSON values.')
    if len(json.dumps(result, allow_nan=False, default=encode_fraction).encode('utf-8')) > 8000:
        raise ValueError('Helper JSON must be under 8 KB.')
    return result


def normalize_helper(name: str, parameters: dict, expression: str) -> dict:
    """Validate a named expression DSL, never a Python executable/code object."""
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', name):
        raise ValueError('Use one short helper name.')
    if not isinstance(parameters, dict) or not 1 <= len(parameters) <= 16:
        raise ValueError('Declare 1 to 16 typed JSON parameters.')
    for key, kind in parameters.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', key)
                or key in _HELPER_CALLS or not isinstance(kind, str) or kind not in HELPER_TYPES):
            raise ValueError('Use named parameters and supported helper JSON types.')
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 2000:
        raise ValueError('Use one expression under 2000 characters, not a script.')
    try:
        tree = ast.parse(expression.strip(), mode='eval')
    except (SyntaxError, RecursionError) as error:
        raise ValueError('Use a valid restricted expression.') from error
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
               ast.UAdd, ast.USub, ast.Not, ast.BoolOp, ast.And, ast.Or, ast.Compare,
               ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.IfExp,
               ast.List, ast.Dict, ast.Subscript, ast.Call)
    nodes = list(ast.walk(tree))
    if len(nodes) > 256 or any(not isinstance(node, allowed) for node in nodes):
        raise ValueError('Unsupported helper syntax; no attributes, imports, comprehensions or executable statements.')
    for node in nodes:
        if isinstance(node, ast.Name) and node.id not in parameters and node.id not in _HELPER_CALLS:
            raise ValueError('Expression contains an undeclared name.')
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name)
                or node.func.id not in _HELPER_CALLS or node.keywords or not 1 <= len(node.args) <= 3):
            raise ValueError('Call only the fixed helper primitives with positional arguments.')
        if isinstance(node, ast.Dict) and (any(not isinstance(key, ast.Constant) or type(key.value) is not str for key in node.keys)
                or len({key.value for key in node.keys}) != len(node.keys)):
            raise ValueError('JSON object expressions need distinct literal string keys.')
        if isinstance(node, ast.Constant):
            helper_json(node.value)
    return {'format': HELPER_FORMAT, 'name': name, 'parameters': dict(parameters), 'expression': expression.strip()}



def helper_inputs(definition: dict, inputs: dict) -> dict:
    """Validate typed JSON inputs without evaluating the expression."""
    environment = helper_json(inputs)
    if type(environment) is not dict or environment.keys() != definition['parameters'].keys():
        raise ValueError('Supply exactly the declared helper inputs.')
    checks = {'number': lambda x: type(x) in (int, float), 'integer': lambda x: type(x) is int,
              'string': lambda x: type(x) is str, 'boolean': lambda x: type(x) is bool,
              'array': lambda x: type(x) is list, 'object': lambda x: type(x) is dict,
              'numbers': lambda x: type(x) is list and all(type(v) in (int, float) for v in x),
              'strings': lambda x: type(x) is list and all(type(v) is str for v in x), 'json': lambda x: True}
    if any(not checks[kind](environment[key]) for key, kind in definition['parameters'].items()):
        raise ValueError('Helper input does not match its declared JSON type.')
    return environment


def run_helper(definition: dict, inputs: dict):
    """Interpret bounded arithmetic/JSON directly. Never eval, exec or import code."""
    definition = normalize_helper(definition['name'], definition['parameters'], definition['expression'])
    environment = helper_inputs(definition, inputs)
    operations = 0
    def number(value):
        if type(value) not in (int, float, Fraction):
            raise ValueError('Arithmetic needs numbers, not booleans or containers.')
        return _bounded(value if type(value) is Fraction else Fraction(Decimal(str(value))))
    def boolean(value):
        if type(value) is not bool:
            raise ValueError('Conditions need boolean values.')
        return value
    def integer(value):
        # Arithmetic produces exact Fractions. Structural integer positions
        # accept an integral computed result, never bools or raw float inputs.
        if type(value) is Fraction and value.denominator==1:return value.numerator
        if type(value) is int:return value
        raise ValueError('Use an integer index or round digit count.')
    def evaluate(node, depth=0):
        nonlocal operations
        operations += 1
        if operations > 512 or depth > 24:
            raise ValueError('Helper exceeds its operation/depth budget.')
        child = lambda value: evaluate(value, depth + 1)
        if isinstance(node, ast.Constant):
            if type(node.value) is float:
                # Preserve the literal's decimal spelling, not Python's rounded float.
                literal = Decimal(ast.get_source_segment(definition['expression'], node))
                # Bound scaling before constructing a potentially huge Fraction.
                if not literal.is_finite() or abs(literal.as_tuple().exponent) > 100:
                    raise ValueError('Decimal literal exceeds the helper arithmetic limit.')
                return _bounded(Fraction(literal))
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in environment: raise ValueError('Primitive names are only allowed in fixed calls.')
            return environment[node.id]
        if isinstance(node, ast.List): return helper_json([child(x) for x in node.elts],_preserve_fractions=True)
        if isinstance(node, ast.Dict): return helper_json({key.value: child(value) for key,value in zip(node.keys,node.values)},_preserve_fractions=True)
        if isinstance(node, ast.Subscript):
            target, key = child(node.value), child(node.slice)
            if type(target) is dict and type(key) is str: return target[key]
            if type(target) in (list,str): return target[integer(key)]
            raise ValueError('Index only JSON arrays/strings by integers or objects by string keys.')
        if isinstance(node, ast.UnaryOp):
            value = child(node.operand)
            if isinstance(node.op, ast.Not): return not boolean(value)
            return -number(value) if isinstance(node.op, ast.USub) else number(value)
        if isinstance(node, ast.BinOp):
            left, right = child(node.left), child(node.right)
            if isinstance(node.op, ast.Add) and type(left) is str and type(right) is str:
                return helper_json(left + right)
            left, right = number(left), number(right)
            if isinstance(node.op, ast.Add): return _bounded(left + right)
            if isinstance(node.op, ast.Sub): return _bounded(left - right)
            if isinstance(node.op, ast.Mult): return _bounded(left * right)
            if isinstance(node.op, ast.Div): return _bounded(left / right)
            if isinstance(node.op, ast.FloorDiv): return left // right
            if isinstance(node.op, ast.Mod): return _bounded(left % right)
            if isinstance(node.op, ast.Pow) and right.denominator == 1 and abs(right) <= 20:
                return _bounded(left ** right.numerator)
            raise ValueError('Exponent must be an integer between -20 and 20.')
        if isinstance(node, ast.Compare):
            left = child(node.left)
            for operator, operand in zip(node.ops, node.comparators):
                right = child(operand)
                if type(left) in (int,float,Fraction) and type(right) in (int,float,Fraction): left,right=number(left),number(right)
                elif type(left) is not type(right) or type(left) not in (str,bool,type(None)):
                    raise ValueError('Compare compatible scalar values only.')
                if isinstance(operator, ast.Eq): match = left == right
                elif isinstance(operator, ast.NotEq): match = left != right
                elif isinstance(operator, ast.Lt): match = left < right
                elif isinstance(operator, ast.LtE): match = left <= right
                elif isinstance(operator, ast.Gt): match = left > right
                else: match = left >= right
                if not match: return False
                left=right
            return True
        if isinstance(node, ast.BoolOp):
            for operand in node.values:
                value = boolean(child(operand))
                if isinstance(node.op, ast.And) and not value: return False
                if isinstance(node.op, ast.Or) and value: return True
            return isinstance(node.op, ast.And)
        if isinstance(node, ast.IfExp): return child(node.body if boolean(child(node.test)) else node.orelse)
        if isinstance(node, ast.Call):
            name=node.func.id; args=[child(x) for x in node.args]
            if name=='get' and len(args) in (2,3) and type(args[0]) is dict and type(args[1]) is str:
                return args[0].get(args[1],args[2] if len(args)==3 else None)
            if name=='len' and len(args)==1 and type(args[0]) in (str,list,dict): return len(args[0])
            if name=='abs' and len(args)==1: return abs(number(args[0]))
            if name=='round' and len(args) in (1,2):
                digits=integer(args[1]) if len(args)==2 else 0
                if not -8 <= digits <= 8: raise ValueError('Round digits must be an integer from -8 to 8.')
                return round(number(args[0]),digits)
            if name in {'sum','min','max'} and len(args)==1 and type(args[0]) is list:
                operations += len(args[0])
                if operations>512: raise ValueError('Helper exceeds its operation budget.')
                values=[number(x) for x in args[0]]
                if name=='sum': return _bounded(sum(values,Fraction(0)))
                if values: return min(values) if name=='min' else max(values)
            raise ValueError('Invalid fixed primitive arguments.')
        raise ValueError('Unsupported expression node.')
    try:
        return helper_json(evaluate(ast.parse(definition['expression'],mode='eval').body))
    except (KeyError, IndexError, TypeError, ArithmeticError, RecursionError) as error:
        raise ValueError('Helper evaluation failed for these inputs.') from error



def helper_values_match(actual, expected) -> bool:
    """JSON numbers may differ in spelling; booleans never count as numbers."""
    if type(actual) in (int, float) and type(expected) in (int, float):
        return Fraction(Decimal(str(actual))) == Fraction(Decimal(str(expected)))
    if type(actual) is not type(expected): return False
    if type(actual) is list:
        return len(actual) == len(expected) and all(helper_values_match(a,b) for a,b in zip(actual,expected))
    if type(actual) is dict:
        return actual.keys() == expected.keys() and all(helper_values_match(actual[key],expected[key]) for key in actual)
    return actual == expected



def _helper_source_bytes(path):
    with path.open('rb') as stream:
        data=stream.read(1_000_001)
    if len(data)>1_000_000:
        raise ValueError('Helper interpreter exceeds its source budget.')
    return data


def _capture_helper_implementation(module_code):
    """Attest the code object actually loaded, not only current disk bytes.

    Compiling source here never executes it. Comparing the complete module code
    catches a hot replacement or stale bytecode already present during import.
    """
    import hashlib,marshal,sys
    from pathlib import Path
    try:
        path=Path(__file__).resolve(strict=True)
        source=_helper_source_bytes(path)
        compiled=compile(source,module_code.co_filename,'exec',dont_inherit=True)
        if compiled!=module_code:
            return None
        source_hash=hashlib.sha256(source).hexdigest()
        code_hash=hashlib.sha256(marshal.dumps(module_code)).hexdigest()
        runner_hash=hashlib.sha256((source_hash+code_hash+str(sys.implementation.cache_tag)).encode()).hexdigest()
        return (path,source_hash,runner_hash)
    except (OSError,ValueError,SyntaxError,RecursionError):
        return None  # Basic calculator still works; unverified helpers do not.


def helper_interpreter_identity():
    """Reject changed interpreter source before and after helper execution."""
    import hashlib
    from pathlib import Path
    snapshot=_HELPER_IMPORTED_IMPLEMENTATION
    if (snapshot is None or Path(__file__).resolve()!=snapshot[0]
            or hashlib.sha256(_helper_source_bytes(snapshot[0])).hexdigest()!=snapshot[1]):
        raise ValueError('Helper interpreter changed; reload it before verification or reuse.')
    return snapshot[2]


# Snapshot the executing module frame, rather than reloading/execing the file.
import sys as _helper_sys
_HELPER_IMPORTED_IMPLEMENTATION=_capture_helper_implementation(_helper_sys._getframe().f_code)
del _helper_sys
