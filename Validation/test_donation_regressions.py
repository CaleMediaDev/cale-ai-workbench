"""Maintainer-reviewed donation regression tests can be added below.

Contributors must add a focused test with each proposed fix. A test belongs here
only after the matching production change has been reviewed; the donation worker
does not execute submitted tests on a contributor's computer.
"""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import project_memory
import task_context


class CheckpointFreshnessRegressionTests(unittest.TestCase):
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
