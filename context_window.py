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
    model_info = info.get("model_info", {})
    architecture = model_info.get("general.architecture")
    native = model_info.get(f"{architecture}.context_length")
    if architecture:
        if isinstance(native, int) and not isinstance(native, bool) and native > 0:
            return native
        raise ValueError("Cannot determine native context limit for model architecture")
    values = [
        int(value) for key, value in model_info.items()
        if key.endswith(".context_length") and isinstance(value, int)
        and not isinstance(value, bool) and value > 0
    ]
    if not values:
        raise ValueError("Cannot determine native context limit from model metadata")
    if len(set(values)) != 1:
        raise ValueError("Cannot determine native context limit from ambiguous model metadata")
    return values[0]


def model_bytes(model):
    with urlopen(OLLAMA + "/api/tags", timeout=10) as response:
        models = json.load(response).get("models", [])
    names = {model, model + ":latest"}
    size = next((item["size"] for item in models if item.get("name") in names), None)
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("Cannot determine model weight size for memory budget")
    return size


def desired_context(info, ram_bytes, weight_bytes):
    """Use the native window when it fits, otherwise a conservative power of two.

    The 128 KiB/token allowance is intentionally conservative because Ollama's
    actual KV cache varies by architecture, parallelism and GPU placement.
    """
    native = native_context(info)
    if native < MIN_AGENT_CONTEXT:
        raise ValueError("This model has less than the 64K Hermes minimum")
    for name, value in (("ram_bytes", ram_bytes), ("weight_bytes", weight_bytes)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer byte count")
    reserve = max(8 * 2**30, ram_bytes // 8)
    available = max(0, ram_bytes - weight_bytes - reserve)
    affordable = available // (128 * 1024)
    if affordable < MIN_AGENT_CONTEXT:
        raise ValueError("Insufficient memory for the 64K Hermes minimum")
    if affordable >= native:
        return native
    window = MIN_AGENT_CONTEXT
    while window * 2 <= min(native, affordable):
        window *= 2
    return min(native, window)


def sync(root, model):
    """Grow the main alias and Hermes setting together.

    Optional aliases keep their smaller windows. The router uses them only
    when the actual request fits, so a short task remains fast on modest GPUs.
    """
    root = Path(root)
    info = post("/api/show", {"model": model})
    if info.get("remote_host") or info.get("remote_model"):
        raise ValueError("A local model is required")
    ram = physical_ram_bytes()
    target = desired_context(info, ram, model_bytes(model))
    path = root / "data/config.yaml"
    original = path.read_text(encoding="utf-8")
    model_section = re.search(
        r"(?ms)^model:[ \t]*(?:#[^\n]*)?\n(?P<body>.*?)(?=^[^\s#]|\Z)",
        original,
    )
    match = re.search(
        r"(?m)^  context_length: \d+(?=[ \t]*(?:#[^\n]*)?$)", model_section.group("body")
    ) if model_section else None
    if not match:
        raise ValueError("Main model context setting is missing")
    start = model_section.start("body") + match.start()
    end = model_section.start("body") + match.end()
    updated = original[:start] + f"  context_length: {target}" + original[end:]
    if configured_context(info) != target:
        result = post("/api/create", {
            "model": model, "from": model, "stream": False,
            "parameters": {"num_ctx": target},
        })
        if result.get("error"):
            raise RuntimeError(result["error"])
    if updated != original:
        path.write_text(updated, encoding="utf-8")
    return target


if __name__ == "__main__":
    print(sync(sys.argv[1], sys.argv[2]))
