"""The person's simple task-routing choice, shared by UI and model router."""

from __future__ import annotations

import json
import os
from pathlib import Path

MODES = ("paid_saver", "medium", "floor_it")
DEFAULT = "medium"


def load(state: Path) -> str:
    """Unknown or damaged settings fall back to the established policy."""
    try:
        value = json.loads((state / "routing-mode.json").read_text(encoding="utf-8"))
        mode = value.get("mode") if isinstance(value, dict) else None
        return mode if mode in MODES else DEFAULT
    except (OSError, ValueError):
        return DEFAULT


def save(state: Path, mode: str) -> None:
    if mode not in MODES:
        raise ValueError("Choose Paid saver, Medium, or Floor it.")
    state.mkdir(parents=True, exist_ok=True)
    target = state / "routing-mode.json"
    temporary = state / f"routing-mode.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    os.replace(temporary, target)


def paid_first(mode: str, size: str, local_speed: float, paid_speed: float,
               fastest_free_speed: float, normal_paid: bool) -> bool:
    """Only Floor it changes paid priority by measured response speed."""
    if mode == "paid_saver":
        return False
    if mode == "medium":
        return normal_paid
    return paid_speed > local_speed and paid_speed >= fastest_free_speed


def free_first(mode: str, normal_free: bool, faster_than_local: bool) -> bool:
    """Saver and Floor it can use fast connected free AI for simple work."""
    return faster_than_local and (mode != "medium" or normal_free)
