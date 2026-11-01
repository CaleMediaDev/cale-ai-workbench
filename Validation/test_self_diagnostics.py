"""The installed regression runner stays local, bounded, and repeatable."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import self_diagnostics as diagnostics


class SelfDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "LongTasks"
        self.state.mkdir()
        (self.root / ".cale-ai-install").write_text("Cale AI Workbench 0.5.206")
        python = self.root / "Toolbox/python/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        validation = self.root / "Validation/test_long_tasks.py"
        validation.parent.mkdir()
        validation.touch()

    def test_schedules_once_and_retries_after_a_week(self):
        proc = SimpleNamespace(pid=123)
        with (patch.object(diagnostics.subprocess, "Popen", return_value=proc) as launch,
              patch("psutil.Process") as process):
            process.return_value.create_time.return_value = 100
            self.assertTrue(diagnostics.schedule(self.root, self.state, now=100))
            report = json.loads((self.state / "self-check.json").read_text())
            self.assertEqual(report["status"], "running")
            self.assertEqual(launch.call_args.args[0][0],
                             str(self.root / "Toolbox/python/Scripts/python.exe"))
        with patch.object(diagnostics, "_running", return_value=True):
            self.assertFalse(diagnostics.schedule(self.root, self.state, now=101))
        report.update(status="failed", checked_at=100)
        (self.state / "self-check.json").write_text(json.dumps(report))
        self.assertFalse(diagnostics.schedule(self.root, self.state, now=101))
        with (patch.object(diagnostics.subprocess, "Popen", return_value=proc),
              patch("psutil.Process") as process):
            process.return_value.create_time.return_value = 200
            self.assertTrue(diagnostics.schedule(
                self.root, self.state, now=101 + diagnostics.INTERVAL))

    def test_real_test_exit_is_saved_with_log(self):
        result = SimpleNamespace(returncode=1, stdout="1 failed\n", stderr="details\n")
        with patch.object(diagnostics.subprocess, "run", return_value=result) as run:
            report = diagnostics.run(self.root, self.state)
        self.assertEqual(report["status"], "failed")
        self.assertIn("1 failed", report["summary"])
        self.assertIn("details", (self.state / "self-check.log").read_text())
        self.assertEqual(run.call_args.args[0][1:], ["-m", "pytest", "-q", "Validation"])


if __name__ == "__main__":
    unittest.main()
