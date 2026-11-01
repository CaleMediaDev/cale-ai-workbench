"""Choose a usable Ollama context from model metadata and physical memory.

The model's native context is the ceiling. A memory estimate keeps a large
window from making a smaller PC unusable; there is no product-wide 64K cap.
"""

import ctypes
import json
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

MIN_AGENT_CONTEXT = 65536
OLLAMA = "http://127.0.0.1:11434"


def post(path, body):
    request = Request(
        OLLAMA + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=120) as response:
        return json.load(response)


def physical_ram_bytes():
    """Read total RAM without an optional package dependency."""
    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in (
                "total_phys", "available_phys", "total_page", "available_page",
                "total_virtual", "available_virtual", "extended",
            )
        ]

    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise OSError("Cannot read installed RAM")
    return memory.total_phys


def configured_context(info):
    match = re.search(r"(?m)^\s*num_ctx\s+(\d+)\s*$", info.get("parameters", ""))
    return int(match.group(1)) if match else 0


def native_context(info):
    """Read the model architecture's advertised limit from Ollama /api/show."""
    values = [
        int(value) for key, value in info.get("model_info", {}).items()
        if key.endswith(".context_length") and isinstance(value, int) and value > 0
    ]
    return max(values, default=configured_context(info))


def model_bytes(model):
    with urlopen(OLLAMA + "/api/tags", timeout=10) as response:
        models = json.load(response).get("models", [])
    names = {model, model + ":latest"}
    return next((item["size"] for item in models if item.get("name") in names), 0)


def desired_context(info, ram_bytes, weight_bytes):
    """Use the largest power-of-two window estimated to fit alongside weights.

    The 128 KiB/token allowance is intentionally conservative because Ollama's
    actual KV cache varies by architecture, parallelism and GPU placement.
    """
    native = native_context(info)
    if native < MIN_AGENT_CONTEXT:
        raise ValueError("This model has less than the 64K Hermes minimum")
    reserve = max(8 * 2**30, ram_bytes // 8)
    available = max(0, ram_bytes - weight_bytes - reserve)
    affordable = max(MIN_AGENT_CONTEXT, available // (128 * 1024))
    window = MIN_AGENT_CONTEXT
    while window * 2 <= min(native, affordable):
        window *= 2
    return min(native, window)


def sync(root, model):
    """Grow the main alias and Hermes setting together, then align quick routes."""
    root = Path(root)
    info = post("/api/show", {"model": model})
    if info.get("remote_host") or info.get("remote_model"):
        raise ValueError("A local model is required")
    ram = physical_ram_bytes()
    target = desired_context(info, ram, model_bytes(model))
    if configured_context(info) != target:
        result = post("/api/create", {
            "model": model, "from": model, "stream": False,
            "parameters": {"num_ctx": target},
        })
        if result.get("error"):
            raise RuntimeError(result["error"])
    # Align every installed route that can afford the main agent's window.
    # Smaller models remain installed but the router will exclude them.
    from model_library import CATALOG

    for candidate in dict.fromkeys(item["alias"] for item in CATALOG.values()):
        if candidate == model:
            continue
        try:
            candidate_info = post("/api/show", {"model": candidate})
            if ("tools" in candidate_info.get("capabilities", [])
                    and native_context(candidate_info) >= target
                    and desired_context(candidate_info, ram, model_bytes(candidate)) >= target
                    and configured_context(candidate_info) != target):
                post("/api/create", {
                    "model": candidate, "from": candidate, "stream": False,
                    "parameters": {"num_ctx": target},
                })
        except (OSError, ValueError):
            continue
    path = root / "data/config.yaml"
    original = path.read_text(encoding="utf-8")
    section = original.split("fallback_providers:", 1)[0]
    match = re.search(r"(?m)^  context_length: \d+[ \t]*$", section)
    if not match:
        raise ValueError("Main model context setting is missing")
    updated = original.replace(match.group(0), f"  context_length: {target}", 1)
    if updated != original:
        path.write_text(updated, encoding="utf-8")
    return target


if __name__ == "__main__":
    print(sync(sys.argv[1], sys.argv[2]))
