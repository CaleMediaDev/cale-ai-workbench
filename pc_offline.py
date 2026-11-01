"""Launch the local Windows network toggle with a visible UAC prompt."""

import ctypes
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "LongTasks/offline-network.json"
SCRIPT = ROOT / "pc_offline.ps1"


def offline() -> bool:
    """Treat a saved adapter record as a request to stay local-only."""
    return STATE.is_file()


def status() -> str:
    """Report whether the saved switch actually has physical adapters down.

    The router stays local whenever a switch record exists, even if this check
    is uncertain. The UI must not call that uncertain state verified offline.
    """
    if not offline():
        return "online"
    try:
        record = json.loads(STATE.read_text(encoding="utf-8-sig"))
        if (not isinstance(record, dict) or record.get("phase") != "offline"
                or not record.get("names")):
            return "unverified"
        powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-Command",
             "ConvertTo-Json -InputObject @(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' } | Select-Object -ExpandProperty Name) -Compress"],
            capture_output=True, text=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            return "unverified"
        active = json.loads(result.stdout)
        return "physical_off" if active == [] else "unverified"
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return "unverified"


def toggle() -> str:
    """Request elevation for a real PC-wide network change."""
    if os.name != "nt":
        raise RuntimeError("PC-wide offline mode is available on Windows only.")
    mode = "online" if offline() else "offline"
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    arguments = f'-NoProfile -ExecutionPolicy Bypass -File "{SCRIPT}" -Mode {mode}'
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(powershell), arguments, str(ROOT), 1)
    if result <= 32:
        raise RuntimeError("Windows did not start the network switch. Administrator approval may have been declined.")
    return mode
