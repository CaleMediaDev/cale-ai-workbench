"""Keep local agent models within hardware and observed speed limits."""

import re
import os
import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def gpu_vram_gb():
    """Return usable NVIDIA VRAM; unknown GPUs get a conservative CPU path."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        values = [int(line.strip()) for line in result.stdout.splitlines()
                  if re.fullmatch(r"\s*\d+\s*", line)]
        return sum(values) / 1024 if values else 0.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def hardware_allows(weights_gb, ram_gb, vram_gb):
    """Avoid severe CPU offload; leave headroom for context and the desktop."""
    if weights_gb <= 0 or ram_gb < weights_gb + 8:
        return False
    return weights_gb <= max(8.0, vram_gb * 2.0)


def measured_speed(alias):
    """Return an established median, or None until three real calls exist."""
    import usage_history

    return usage_history.recent_model_speed(alias)


def suitable(alias, weights_gb, ram_gb, vram_gb, min_tokens_per_second=5.0):
    if not hardware_allows(weights_gb, ram_gb, vram_gb):
        return False
    speed = measured_speed(alias)
    return speed is None or speed >= min_tokens_per_second
