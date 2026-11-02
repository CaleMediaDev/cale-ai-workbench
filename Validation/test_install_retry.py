"""Check first-run retries without downloading or modifying an actual install."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows installer only")
class InstallRetryTests(unittest.TestCase):
    def run_fixture(self, backend: str, attempts: int = 3):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copy2(ROOT / "Install-Retry.ps1", root / "Install-Retry.ps1")
            (root / "Install-Backend.ps1").write_text(backend, encoding="utf-8")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(root / "Install-Retry.ps1"), "-Destination", str(root / "App"),
                 "-MaxAttempts", str(attempts), "-RetryDelaySeconds", "0"],
                capture_output=True, text=True, timeout=30,
            )
            return result, (root / "attempts.txt").read_text() if (root / "attempts.txt").exists() else ""

    def test_recovers_after_transient_failure(self):
        result, attempts = self.run_fixture(
            "$counter=Join-Path $PSScriptRoot 'attempts.txt'\n"
            "Add-Content $counter 'attempt'\n"
            "if ((Get-Content $counter).Count -eq 1) { Write-Error 'Temporary network failure'; exit 9 }\n"
            "Write-Output 'Ready'; exit 0\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(attempts.count("attempt"), 2)
        self.assertIn("Retrying automatically", result.stdout)

    def test_permanent_preflight_error_does_not_repeat(self):
        result, attempts = self.run_fixture(
            "$counter=Join-Path $PSScriptRoot 'attempts.txt'\n"
            "Add-Content $counter 'attempt'\n"
            "Write-Error 'Not enough free disk space for the models'; exit 6\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(attempts.count("attempt"), 1)

    def test_stops_after_bounded_failures(self):
        result, attempts = self.run_fixture(
            "$counter=Join-Path $PSScriptRoot 'attempts.txt'\n"
            "Add-Content $counter 'attempt'\n"
            "Write-Error 'Temporary service failure'; exit 7\n", attempts=2,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(attempts.count("attempt"), 2)
        self.assertIn("after 2 attempts", result.stderr)
