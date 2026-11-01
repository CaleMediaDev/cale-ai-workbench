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
    """Floor it favors the route expected to finish first, including latency."""
    if mode == "paid_saver":
        return False
    if mode == "medium":
        return normal_paid
    output = {"quick": 100, "balanced": 1000, "deep": 3000}.get(size, 1000)
    paid_time = 2.0 + output / max(paid_speed, 1)
    local_time = output / max(local_speed, 1)
    free_time = (2.0 + output / fastest_free_speed
                 if fastest_free_speed > 0 else float("inf"))
    return paid_time < min(local_time, free_time)


def free_first(mode: str, normal_free: bool, faster_than_local: bool) -> bool:
    """Saver and Floor it can use fast connected free AI for simple work."""
    return faster_than_local and (mode != "medium" or normal_free)
