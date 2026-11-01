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
