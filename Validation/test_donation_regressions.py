"""Maintainer-reviewed donation regression tests can be added below.

Contributors must add a focused test with each proposed fix. A test belongs here
only after the matching production change has been reviewed; the donation worker
does not execute submitted tests on a contributor's computer.
"""

import os
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import project_memory
import task_context
from quick_calculator import calculate
from task_eta import total_time


class CheckpointFreshnessRegressionTests(unittest.TestCase):
    def test_empty_plan_checkbox_does_not_hide_next_step(self):
        with self.subTest(source="later plan item"):
            self.assertEqual(
                task_context._next_step(
                    "- [ ]   \n- [ ] Verify the current milestone\n",
                    "Next step: Review progress\n"),
                "Verify the current milestone")
        with self.subTest(source="progress fallback"):
            self.assertEqual(
                task_context._next_step(
                    "- [ ]   \n", "Next step: Review progress\n"),
                "Review progress")

    def test_equal_timestamp_priority_survives_plan_cache_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkpoint = root / ".local-ai"
            checkpoint.mkdir()
            plan = checkpoint / "PLAN.md"
            progress = checkpoint / "PROGRESS.md"
            plan.write_text("- [ ] Implement the old plan item\n", encoding="utf-8")
            progress.write_text("Next step: Verify the current milestone\n", encoding="utf-8")
            timestamp = 1_700_000_000_000_000_000
            for path in (plan, progress):
                os.utime(path, ns=(timestamp, timestamp))
            self.assertEqual(plan.stat().st_mtime_ns, progress.stat().st_mtime_ns)

            with patch("task_context.time.time", return_value=1000.0):
                initial = task_context.snapshot(root)
            expected = "Next step: Verify the current milestone"
            self.assertIn(expected, initial.splitlines())

            # Expire only the parsed plan cache; neither source file changes.
            with project_memory.connection(root) as conn:
                conn.execute("UPDATE task_documents SET updated=0 WHERE name='PLAN.md'")
            with patch("task_context.time.time", return_value=1001.0):
                refreshed = task_context.snapshot(root)
            self.assertIn(expected, refreshed.splitlines())


class CalculatorPrecisionTests(unittest.TestCase):
    def test_nested_floor_division_is_rejected_before_sibling_evaluation(self):
        for expression in (
            "1 / 0 + (2 // 1)",
            "(2 // 1) + 1 / 0",
            "1 / 0 + (2 ÷÷ 1)",
        ):
            with self.subTest(expression=expression):
                self.assertIsNone(calculate(expression))
        self.assertEqual(calculate("1 / 0 + (2 / 1)"), "Cannot divide by zero.")

    def test_unsupported_operator_is_rejected_before_evaluating_operands(self):
        for expression in ("1 // (1 / 0)", "(1 / 0) // 2", "1 // 0"):
            with self.subTest(expression=expression):
                self.assertIsNone(calculate(expression))
        self.assertEqual(calculate("1 / 0"), "Cannot divide by zero.")

    def test_leading_zero_integers_are_decimal(self):
        for expression, answer in (
            ("0012 + 3", "15"),
            ("0008 + 0009", "17"),
            ("-0012 + 3", "-9"),
            ("000 + 1", "1"),
            ("00,012 + 1", "13"),
            ("0.001 + 0002", "2.001"),
            (".001 + 0002", "2.001"),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(calculate(expression), f"{expression} = {answer}")
        for expression in ("00..12 + 1", "00,12 + 1", "0002 // 1"):
            with self.subTest(expression=expression):
                self.assertIsNone(calculate(expression))

    def test_unicode_multiplication_does_not_create_exponentiation(self):
        for expression in ("2 ×× 3", "2 ×* 3", "2 *× 3"):
            with self.subTest(expression=expression):
                self.assertIsNone(calculate(expression))
        for expression, answer in (
            ("2×3", "6"),
            ("2×-3", "-6"),
            ("2 ** 3", "8"),
            ("2 ^ 3", "8"),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(calculate(expression), f"{expression} = {answer}")

    def test_thousands_separators_require_valid_numeric_grouping(self):
        for expression in (
            "1234,567 + 1",
            "1,23,456 + 1",
            "1.234,567 + 1",
            "1,234.567,890 + 1",
            "1, 234 + 1",
        ):
            with self.subTest(expression=expression):
                self.assertIsNone(calculate(expression))
        for expression, answer in (
            ("1,234,567 + 1", "1234568"),
            ("-12,345.50 + .5", "-12345"),
            ("1,234. + 1", "1235"),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(calculate(expression), f"{expression} = {answer}")

    def test_oversized_percentage_result_falls_back(self):
        number = "9" * 60
        expression = f"{number}% of {number}"
        self.assertLessEqual(len(expression), 160)
        self.assertIsNone(calculate(expression))
        self.assertEqual(calculate("25% of $200"), "25% of $200 = $50")

    def test_long_terminating_decimal_is_exact(self):
        expression = "1 / (2^100)^2"
        result = calculate(expression)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith(expression + " = "))
        displayed = result.split(" = ", 1)[1]
        self.assertIn(".", displayed)
        self.assertNotIn("about", displayed)
        self.assertEqual(Fraction(displayed), Fraction(1, 2**200))


class TaskElapsedTimeRegressionTests(unittest.TestCase):
    def test_nonfinite_elapsed_timestamps_are_unavailable(self):
        for timestamp in (float("nan"), float("inf"), float("-inf")):
            for status in ("working", "paused", "ready_for_review", "accepted", "stopped"):
                with self.subTest(created=timestamp, status=status):
                    self.assertEqual(
                        total_time(timestamp, 160, status, now=1000), "—"
                    )
            for status in ("ready_for_review", "accepted", "stopped"):
                for finished in (None, "invalid", float("nan")):
                    with self.subTest(updated=timestamp, status=status, finished=finished):
                        self.assertEqual(
                            total_time(100, timestamp, status, now=1000, finished=finished),
                            "—",
                        )
                with self.subTest(updated=timestamp, status=status, finished=130):
                    self.assertEqual(
                        total_time(100, timestamp, status, now=1000, finished=130), "30s"
                    )

    def test_terminal_time_falls_back_for_invalid_finished_timestamp(self):
        for status in ("ready_for_review", "accepted", "stopped"):
            for finished in (None, 0, "invalid", {"bad": "timestamp"},
                             float("nan"), float("inf"), float("-inf"), 90):
                with self.subTest(status=status, finished=finished):
                    self.assertEqual(total_time(100, 160, status, now=1000, finished=finished), "1m 00s")
            self.assertEqual(total_time(100, 160, status, now=1000, finished=130), "30s")
