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
    if isinstance(node, ast.BinOp):
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
    try:
        tree = ast.parse(normalized, mode="eval")
        return f"{expression} = {_display(_evaluate(tree.body, normalized))}"
    except (SyntaxError, ValueError, OverflowError):
        return None
    except ZeroDivisionError:
        return "Cannot divide by zero."
