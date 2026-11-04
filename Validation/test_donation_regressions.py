"""Maintainer-reviewed donation regression tests can be added below.

Contributors must add a focused test with each proposed fix. A test belongs here
only after the matching production change has been reviewed; the donation worker
does not execute submitted tests on a contributor's computer.
"""

import unittest

from task_eta import total_time


class TaskElapsedTimeRegressionTests(unittest.TestCase):
    def test_terminal_time_falls_back_for_invalid_finished_timestamp(self):
        for status in ("ready_for_review", "accepted", "stopped"):
            for finished in (None, 0, "invalid", {"bad": "timestamp"},
                             float("nan"), float("inf"), float("-inf"), 90):
                with self.subTest(status=status, finished=finished):
                    self.assertEqual(
                        total_time(100, 160, status, now=1000, finished=finished),
                        "1m 00s",
                    )
            with self.subTest(status=status, finished="valid"):
                self.assertEqual(
                    total_time(100, 160, status, now=1000, finished=130),
                    "30s",
                )
