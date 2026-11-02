"""Exercise the small Windows installer preflight without downloading models."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows installer only")
class InstallEnvironmentTests(unittest.TestCase):
    def test_packaged_launcher_uses_physical_path_and_restores_module_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder)
            family = "Test.Codex_123"
            (local / "Packages" / family).mkdir(parents=True)
            harness = local / "probe.ps1"
            harness.write_text(
                ". $env:CALE_INSTALL_HELPER\n"
                "Initialize-CalePowerShellEnvironment\n"
                "$default=Join-Path $env:LOCALAPPDATA 'CaleMedia\\AIWorkbench'\n"
                "$resolved=Get-CaleInstallDestination $default\n"
                "$explicit=Get-CaleInstallDestination (Join-Path $env:LOCALAPPDATA 'Chosen')\n"
                "$hash=(Get-FileHash -LiteralPath $env:CALE_INSTALL_HELPER -Algorithm SHA256).Hash\n"
                "@{resolved=$resolved;explicit=$explicit;hash=$hash} | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                LOCALAPPDATA=str(local),
                CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY=family,
                PSModulePath=str(local / "missing-modules"),
                CALE_INSTALL_HELPER=str(ROOT / "Install-Environment.ps1"),
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            self.assertEqual(
                Path(actual["resolved"]),
                local / "Packages" / family / "LocalCache" / "Local" / "CaleMedia" / "AIWorkbench",
            )
            self.assertEqual(Path(actual["explicit"]), local / "Chosen")
            self.assertEqual(len(actual["hash"]), 64)

    def test_already_physical_localappdata_is_not_nested_again(self):
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "Packages" / "Test.Codex_123" / "LocalCache" / "Local"
            local.mkdir(parents=True)
            env = os.environ.copy()
            env.update(
                LOCALAPPDATA=str(local),
                CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY="Test.Codex_123",
                CALE_INSTALL_HELPER=str(ROOT / "Install-Environment.ps1"),
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 ". $env:CALE_INSTALL_HELPER; Get-CaleInstallDestination"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()), local / "CaleMedia" / "AIWorkbench")


if __name__ == "__main__":
    unittest.main()
