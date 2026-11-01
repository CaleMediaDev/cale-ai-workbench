"""Launch the local Windows network toggle with a visible UAC prompt."""

import ctypes
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "LongTasks/offline-network.json"
SCRIPT = ROOT / "pc_offline.ps1"


def offline() -> bool:
    """Treat a saved adapter record as a request to stay local-only."""
    return STATE.is_file()


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
