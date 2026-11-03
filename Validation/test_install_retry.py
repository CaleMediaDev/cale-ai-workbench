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
    def run_fixture(self, backend: str, attempts: int = 3, no_launch: bool = False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copy2(ROOT / "Install-Retry.ps1", root / "Install-Retry.ps1")
            (root / "Install-Backend.ps1").write_text(backend, encoding="utf-8")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(root / "Install-Retry.ps1"), "-Destination", str(root / "App"),
                 "-MaxAttempts", str(attempts), "-RetryDelaySeconds", "0",
                 *(["-NoLaunch"] if no_launch else [])],
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

    def test_visual_installer_keeps_app_closed_until_setup_finishes(self):
        result, _ = self.run_fixture(
            "param([string]$Destination,[string]$Profile,[switch]$NoLaunch)\n"
            "if (-not $NoLaunch) { Write-Error 'App would open before setup is ready'; exit 8 }\n"
            "Write-Output 'App launch deferred'; exit 0\n",
            no_launch=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("App launch deferred", result.stdout)

    def test_permanent_preflight_error_does_not_repeat(self):
        result, attempts = self.run_fixture(
            "param([string]$Destination,[string]$Profile)\n"
            "$counter=Join-Path $PSScriptRoot 'attempts.txt'\n"
            "Add-Content $counter 'attempt'\n"
            "$statusDir=Join-Path $Destination 'LongTasks'\n"
            "New-Item -ItemType Directory -Path $statusDir -Force | Out-Null\n"
            "$started=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()\n"
            "$status=@{status='error';current='Setup needs attention: Not enough free disk space for the models';installer_started=$started} | ConvertTo-Json -Compress\n"
            "[IO.File]::WriteAllText((Join-Path $statusDir 'install-progress.json'),$status)\n"
            "Write-Error 'Not enough free disk space for the models'\n"
            "1..40 | ForEach-Object { Write-Output 'Other setup details' }\n"
            "exit 6\n"
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
