"""Catalog and install optional local models through Ollama's resumable store.

The app starts this module in a separate process for manual or automatic
downloads. Each profile writes a small atomic JSON status file for the UI.
"""

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from model_identity import configured_main_alias, configured_permissive_alias

ROOT = Path(__file__).resolve().parent
STANDARD_MAIN_ALIAS = "cale-main-64k"
RECOMMENDATIONS_URL = (
    "https://raw.githubusercontent.com/CaleMediaDev/"
    "cale-ai-workbench/main/models/recommendations.json"
)
CATALOG = {
    "main": {
        "label": "Main coding + optional free cloud",
        "alias": configured_main_alias(ROOT),
    },
    "fast": {
        "label": "Fast local tool model",
        "alias": "cale-fast",
        "source": "qwen3.5:9b",
        "download_gb": 7,
        "ram_gb": 24,
        "origin": "https://ollama.com/library/qwen3.5",
        "license": "Apache-2.0",
        "context": 65536,
        "context_policy": "fixed",
        "candidate": True,
        "tool_reasoning_effort": "none",
    },
    "qwen25_code7": {
        "label": "Qwen 2.5 Coder 7B (coding candidate)",
        "alias": "cale-code7", "source": "qwen2.5-coder:7b",
        "download_gb": 5, "ram_gb": 16, "min_vram_gb": 6,
        "origin": "https://ollama.com/library/qwen2.5-coder",
        "license": "Apache-2.0", "context": 32768, "context_policy": "fixed",
        "candidate": True, "coding_probe": True, "tool_reasoning_effort": "none",
    },
    "qwen25_code14": {
        "label": "Qwen 2.5 Coder 14B (coding candidate)",
        "alias": "cale-code14", "source": "qwen2.5-coder:14b",
        "download_gb": 9, "ram_gb": 24, "min_vram_gb": 11,
        "origin": "https://ollama.com/library/qwen2.5-coder",
        "license": "Apache-2.0", "context": 32768, "context_policy": "fixed",
        "candidate": True, "coding_probe": True, "tool_reasoning_effort": "none",
    },
    "gemma4_edge": {
        "label": "Gemma 4 E4B (coding candidate)",
        "alias": "cale-gemma4-edge", "source": "gemma4:e4b",
        "download_gb": 7, "ram_gb": 24, "min_vram_gb": 8,
        "origin": "https://ollama.com/library/gemma4",
        "license": "Apache-2.0", "context": 65536, "context_policy": "fixed",
        "candidate": True, "coding_probe": True, "tool_reasoning_effort": "none",
    },
    "qwen38_code": {
        "label": "Qwen 3.8 27B (24 GB GPU coding candidate)",
        "alias": "cale-qwen38-code", "source": "qwen3.8:27b",
        "download_gb": 18, "ram_gb": 32, "min_vram_gb": 22,
        "origin": "https://ollama.com/library/qwen3.8",
        "license": "Apache-2.0", "context": 65536, "context_policy": "fixed",
        "candidate": True, "coding_probe": True, "tool_reasoning_effort": "none",
    },
    "qwen35": {
        "label": "Qwen 3.5 35B (balanced local)",
        "alias": "cale-qwen35",
        "source": "qwen3.5:35b",
        "download_gb": 22,
        "ram_gb": 48,
        "origin": "https://ollama.com/library/qwen3.5",
        "license": "Apache-2.0",
        "context": 65536,
    },
    "permissive_small": {
        "label": "Huihui Qwen 3.5 9B abliterated (local)",
        "alias": "cale-qwen35-abliterated-9b",
        "source": "huihui_ai/qwen3.5-abliterated:9b",
        "download_gb": 7,
        "ram_gb": 16,
        "origin": "https://huggingface.co/huihui-ai/Huihui-Qwen3.5-9B-abliterated",
        "license": "Apache-2.0",
        "context": 65536,
        "candidate": True,
        "fallback_only": True,
        "tool_reasoning_effort": "none",
    },
    "permissive": {
        "label": "Qwen Coder Next permissive (local)",
        "alias": configured_permissive_alias(ROOT),
        "source": "huihui_ai/qwen3-coder-next-abliterated:q4_K",
        "download_gb": 52,
        "ram_gb": 96,
        "origin": "https://huggingface.co/huihui-ai/Huihui-Qwen3-Coder-Next-abliterated",
        "license": "Apache-2.0",
        "context": 65536,
    },
    "gptoss_official": {
        "label": "GPT-OSS 20B (tool-ready local)",
        "alias": "cale-gptoss",
        "source": "gpt-oss:20b",
        "download_gb": 14,
        "ram_gb": 32,
        "origin": "https://ollama.com/library/gpt-oss",
        "license": "Apache-2.0",
        "context": 65536,
    },
    "gptoss": {
        "label": "ArliAI GPT-OSS 20B (manual experimental)",
        "alias": "cale-gptoss-derestricted",
        "source": "hf.co/MaziyarPanahi/gpt-oss-20b-Derestricted-GGUF:Q4_K_M",
        "download_gb": 13,
        "ram_gb": 32,
        "origin": "https://huggingface.co/ArliAI/gpt-oss-20b-Derestricted",
        "conversion": "https://huggingface.co/MaziyarPanahi/gpt-oss-20b-Derestricted-GGUF",
        "license": "Apache-2.0",
        "context": 65536,
        "auto": False,
    },
    "llama": {
        "label": "Huihui Llama 3.3 70B (experimental)",
        "alias": "cale-llama33-abliterated",
        "source": "huihui_ai/llama3.3-abliterated:latest",
        "download_gb": 43,
        "ram_gb": 80,
        "origin": "https://huggingface.co/huihui-ai/Llama-3.3-70B-Instruct-abliterated",
        "license": "Llama 3.3 Community License",
        "context": 65536,
    },
    "glm": {
        "label": "ArliAI GLM 4.5 Air (manual experimental)",
        "alias": "cale-glm45-derestricted",
        "source": "hf.co/mradermacher/GLM-4.5-Air-Derestricted-GGUF:Q2_K",
        "download_gb": 45,
        "ram_gb": 80,
        "origin": "https://huggingface.co/ArliAI/GLM-4.5-Air-Derestricted",
        "conversion": "https://huggingface.co/mradermacher/GLM-4.5-Air-Derestricted-GGUF",
        "license": "MIT",
        "context": 65536,
        "auto": False,
    },
}
BUILTIN_PROFILES = frozenset(CATALOG)


def apply_recommendations(accepted):
    """Replace only feed-owned profiles; bundled choices remain untouched."""
    for profile in tuple(CATALOG):
        if profile not in BUILTIN_PROFILES and profile not in accepted:
            del CATALOG[profile]
    CATALOG.update(accepted)


def validated_recommendations(document):
    """Accept only bounded, official Ollama model records from the owner's feed."""
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError("Unsupported model recommendation format.")
    entries = document.get("models")
    if not isinstance(entries, dict) or len(entries) > 12:
        raise ValueError("Model recommendation count is invalid.")
    accepted = {}
    known_aliases = {item["alias"]: profile for profile, item in CATALOG.items()}
    for profile, item in entries.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,24}", profile) or (
            profile in BUILTIN_PROFILES
        ):
            continue
        if not isinstance(item, dict):
            if profile in CATALOG and profile not in BUILTIN_PROFILES:
                raise ValueError("An existing model recommendation is invalid.")
            continue
        alias = item.get("alias", "")
        source = item.get("source", "")
        origin = item.get("origin", "")
        previous = CATALOG.get(profile)
        if not (
            isinstance(alias, str)
            and isinstance(source, str)
            and re.fullmatch(r"cale-[a-z0-9-]{3,40}", alias)
            and re.fullmatch(
                r"[a-z0-9][a-z0-9._-]{1,50}:[a-z0-9][a-z0-9._-]{1,50}", source
            )
            and origin == "https://ollama.com/library/" + source.split(":", 1)[0]
            and item.get("license")
            and item.get("auto") is True
            and known_aliases.get(alias, profile) == profile
            # Replacing weights under a live profile would revoke its old
            # approval before the replacement passes. Publish a new profile
            # and alias for a new model instead.
            and (previous is None or previous.get("source") == source)
        ):
            if previous is not None and profile not in BUILTIN_PROFILES:
                raise ValueError("An existing model recommendation changed incompatibly.")
            continue
        try:
            size = int(item["download_gb"])
            ram = int(item["ram_gb"])
            context = int(item["context"])
        except (KeyError, TypeError, ValueError):
            if previous is not None and profile not in BUILTIN_PROFILES:
                raise ValueError("An existing model recommendation has invalid sizing.") from None
            continue
        if not (1 <= size <= 80 and 16 <= ram <= 256 and 64000 <= context <= 1048576):
            if previous is not None and profile not in BUILTIN_PROFILES:
                raise ValueError("An existing model recommendation has invalid sizing.")
            continue
        accepted[profile] = {
            "label": str(item.get("label", profile))[:80],
            "alias": alias,
            "source": source,
            "download_gb": size,
            "ram_gb": ram,
            "origin": origin,
            "license": str(item["license"])[:60],
            "context": context,
            "auto": True,
            "candidate": True,
            "tool_reasoning_effort": item.get("tool_reasoning_effort")
            if item.get("tool_reasoning_effort") in ("none", "low", "medium", "high")
            else None,
        }
        known_aliases[alias] = profile
    return accepted


def candidate_approved(item):
    """Only route a new recommendation after its local tool probe passes."""
    if not item.get("candidate"):
        return True
    try:
        approvals = json.loads((ROOT / "LongTasks/model-approvals.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(approvals, dict) and approvals.get(item["alias"]) == item["source"]


def approve_candidate(item):
    path = ROOT / "LongTasks/model-approvals.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        approvals = json.loads(path.read_text())
    except (OSError, ValueError):
        approvals = {}
    if not isinstance(approvals, dict):
        approvals = {}
    approvals[item["alias"]] = item["source"]
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(approvals), encoding="utf-8")
    os.replace(temporary, path)


class SlowModelError(RuntimeError):
    """A real local generation probe found this model too slow for agent work."""


class CandidateModelError(RuntimeError):
    """A model failed actual tools or coding checks; retry after a cooldown."""


def probe_tokens_per_second(result):
    """Use Ollama's decode duration, excluding model load and prompt prefill."""
    try:
        count = int(result.get("eval_count") or 0)
        duration = int(result.get("eval_duration") or 0)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if count < 32 or duration <= 0:
        return None
    return count * 1_000_000_000 / duration


def probe_tool_use(alias, reasoning_effort=None):
    """Require both a real tool call and useful decode speed before promotion."""
    request = {
        "model": alias,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": "Call multiply with a=7 and b=13. Do not answer in text.",
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "multiply",
                    "description": "Multiply two integers",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "a": {"type": "integer"},
                            "b": {"type": "integer"},
                        },
                        "required": ["a", "b"],
                    },
                },
            }
        ],
        "options": {"num_predict": 512},
    }
    if reasoning_effort == "none":
        request["think"] = False
    result = post(
        "/api/chat",
        request,
        timeout=240,
    )
    for call in result.get("message", {}).get("tool_calls", []):
        function = call.get("function", {})
        args = function.get("arguments", {})
        if (
            function.get("name") == "multiply"
            and args.get("a") == 7
            and args.get("b") == 13
        ):
            speed = probe_tokens_per_second(result)
            if speed is None:
                # Tool calls can contain too few decoded tokens for a useful
                # speed sample. Probe the already-loaded source once more.
                generation = post("/api/generate", {
                    "model": alias,
                    "prompt": "Write 80 different common English words, separated by commas. Do not explain.",
                    "stream": False,
                    "options": {"num_predict": 128, "temperature": 0},
                }, timeout=180)
                speed = probe_tokens_per_second(generation)
            if speed is None:
                raise RuntimeError("Model tool check passed, but generation speed could not be verified.")
            if speed < 5:
                raise SlowModelError(
                    f"Model generated {speed:.1f} tokens/s on this PC; needs at least 5 tokens/s."
                )
            return True
    raise CandidateModelError(
        "Model downloaded, but it did not pass the local tool-use check."
    )


def refresh_catalog(online=True):
    """Check Cale Media's public feed; use cached or bundled data offline."""
    cache = ROOT / "LongTasks/model-recommendations.json"
    bundled = ROOT / "models/recommendations.json"
    if online:
        try:
            with urlopen(
                Request(
                    RECOMMENDATIONS_URL, headers={"User-Agent": "Cale-AI-Workbench"}
                ),
                timeout=10,
            ) as response:
                raw = response.read(256_000)
            document = json.loads(raw)
            accepted = validated_recommendations(document)
            if accepted or not document.get("models"):
                cache.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_bytes(raw)
                os.replace(temporary, cache)
                apply_recommendations(accepted)
                return accepted
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    for path in (cache, bundled):
        try:
            accepted = validated_recommendations(
                json.loads(path.read_text(encoding="utf-8"))
            )
            apply_recommendations(accepted)
            return accepted
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return {}


def post(path, value, timeout=30):
    with urlopen(
        Request(
            "http://127.0.0.1:11434" + path,
            data=json.dumps(value).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=timeout,
    ) as response:
        return json.load(response)


def delete_model(alias):
    """Ollama's delete endpoint succeeds with an empty response body."""
    with urlopen(Request(
        "http://127.0.0.1:11434/api/delete",
        data=json.dumps({"model": alias}).encode(),
        headers={"Content-Type": "application/json"},
        method="DELETE",
    ), timeout=30) as response:
        response.read()


def ensure_main_standard():
    """Share main-model weights with a smaller routine context window.

    The original alias retains its full window for long requests. Rebuilding
    this metadata-only alias waits until project workers are idle.
    """
    try:
        from long_tasks import projects

        if any(task["status"] in ("queued", "working", "checking") for task in projects()):
            return False
    except Exception:
        return False
    main = CATALOG["main"]["alias"]
    try:
        info = post("/api/show", {"model": main}, timeout=10)
    except (OSError, ValueError):
        return False
    if info.get("remote_host") or info.get("remote_model") or "tools" not in info.get("capabilities", []):
        return False
    current = re.search(r"(?m)^\s*num_ctx\s+(\d+)\s*$", info.get("parameters", ""))
    if not current or int(current.group(1)) <= 65536:
        return False
    result = post("/api/create", {
        "model": STANDARD_MAIN_ALIAS, "from": main, "stream": False,
        "parameters": {"num_ctx": 65536, "num_predict": 8192},
    }, timeout=120)
    if result.get("error"):
        raise RuntimeError(result["error"])
    ready = post("/api/show", {"model": STANDARD_MAIN_ALIAS}, timeout=10)
    configured = re.search(r"(?m)^\s*num_ctx\s+(\d+)\s*$", ready.get("parameters", ""))
    if (ready.get("remote_host") or ready.get("remote_model") or
            "tools" not in ready.get("capabilities", []) or
            not configured or int(configured.group(1)) != 65536):
        raise RuntimeError("The standard-context alias did not pass local verification.")
    return True


def total_ram_gb():
    """Return physical RAM, which is the limit for optional local weights."""
    import ctypes

    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong)
            for n in (
                "total_phys",
                "available_phys",
                "total_page",
                "available_page",
                "total_virtual",
                "available_virtual",
                "extended",
            )
        ]

    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
    return memory.total_phys / 2**30


def model_store_path():
    """Find the drive Ollama uses for weights, including a custom model store."""
    configured = os.environ.get("OLLAMA_MODELS")
    store = (
        Path(configured).expanduser() if configured else Path.home() / ".ollama/models"
    )
    # disk_usage accepts an existing ancestor even before Ollama creates models/.
    return next((path for path in (store, *store.parents) if path.exists()), store)


def source_cached(item):
    """Distinguish a context-only alias update from a fresh weight download."""
    source = item.get("source")
    if not source:
        return False
    try:
        import context_window

        return context_window.model_bytes(source) > 0
    except (OSError, ValueError):
        return False


def alias_update_waits_for_tasks(item):
    """Reserve GPU and Ollama capacity for live tasks, even for a new alias.

    Installing an absent alias still ends with a tool/speed inference probe.
    That probe can evict a music model or make a task's first token stall, so
    background downloads and alias changes wait until projects are idle.
    """
    try:
        from long_tasks import projects

        return any(
            task["status"] in ("queued", "working", "checking")
            for task in projects()
        )
    except Exception:
        return True  # Preserve task capacity if task state is unavailable.


def model_ready(profile):
    """Require tool support and the context window needed by Hermes workers."""
    item = CATALOG[profile]
    info = post("/api/show", {"model": item["alias"]}, timeout=10)
    configured = re.search(r"(?m)^\s*num_ctx\s+(\d+)\s*$", info.get("parameters", ""))
    if (info.get("remote_host") or info.get("remote_model")
            or "tools" not in info.get("capabilities", []) or configured is None):
        return False
    desired = preferred_context(item, info, item["alias"])
    configured_size = int(configured.group(1))
    context_ready = (
        configured_size == desired
        if item.get("context_policy") == "fixed"
        else configured_size >= desired
    )
    return context_ready and candidate_approved(item)


def preferred_context(item, info, model):
    """Choose the largest native window this PC can hold alongside the weights.

    A missing size or architecture report keeps the catalog's tested minimum.
    It must never make an existing 64K alias unusable merely because Ollama
    omitted optional metadata.
    """
    minimum = max(8192, item["context"])
    if item.get("context_policy") == "fixed":
        return minimum
    import context_window

    if not info.get("model_info"):
        return minimum
    weights = context_window.model_bytes(model)
    if not weights:
        return minimum
    target = context_window.desired_context(
        info, context_window.physical_ram_bytes(), weights
    )
    if target < minimum:
        raise ValueError("This PC cannot fit the model's required context window.")
    return target


def catalog_signature():
    """Change when the optional download set or its model sources change."""
    options = {
        profile: {"source": item.get("source", ""), "auto": item.get("auto", True),
                  "context": item.get("context", 0),
                  "context_policy": item.get("context_policy", "native")}
        for profile, item in CATALOG.items()
        if profile != "main"
    }
    return hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:16]


def install(profile, progress=print):
    """Pull one model through Ollama and verify it advertises tool support."""
    item = CATALOG[profile]
    if profile == "main":
        raise ValueError("Use the main installer to choose the base model.")
    if total_ram_gb() + 1 < item["ram_gb"]:
        raise ValueError(
            f"This model is intended for at least {item['ram_gb']} GB RAM. Choose a smaller option."
        )
    import model_fitness

    if not model_fitness.profile_allows(item, total_ram_gb(), model_fitness.gpu_vram_gb()):
        raise ValueError("This model is likely too slow for this PC's GPU. Choose a smaller model.")
    source_was_cached = source_cached(item)
    # The Ollama store can be moved; disk space remains checked by Ollama at download time.
    progress(
        f"Downloading {item['label']}: approximately {item['download_gb']} GB. Re-running resumes interrupted downloads."
    )
    with urlopen(
        Request(
            "http://127.0.0.1:11434/api/pull",
            data=json.dumps({"model": item["source"], "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=3600,
    ) as response:
        import time

        last = 0
        for line in response:
            data = json.loads(line)
            if data.get("error"):
                raise RuntimeError(data["error"])
            if time.monotonic() - last > 2 or data.get("status") == "success":
                done, total = data.get("completed", 0), data.get("total", 0)
                progress(
                    f"{data.get('status', 'Downloading')}  {done / 1e9:.1f} / {total / 1e9:.1f} GB"
                    if total
                    else data.get("status", "Working")
                )
                last = time.monotonic()
    # Probe pulled weights before touching an alias that may already serve
    # work. Failed candidates remain in the store but cannot replace it.
    try:
        source_info = post("/api/show", {"model": item["source"]})
        if (source_info.get("remote_host") or source_info.get("remote_model")
                or "tools" not in source_info.get("capabilities", [])):
            raise RuntimeError("Model does not advertise local tool support.")
        probe_tool_use(item["source"], item.get("tool_reasoning_effort"))
        if item.get("coding_probe"):
            progress("Testing game coding accuracy and completion time")
            report = model_fitness.probe_game_coding(item["source"], ROOT)
            if not report["passed"]:
                raise CandidateModelError("Model failed the independent game-logic checks; it will not be selected.")
    except Exception:
        if not source_was_cached:
            try:
                delete_model(item["source"])
            except Exception:
                pass
        raise
    context = preferred_context(item, source_info, item["source"])
    result = post(
        "/api/create",
        {
            "model": item["alias"],
            "from": item["source"],
            "stream": False,
            "parameters": {
                "num_ctx": context,
                "num_predict": 8192,
                "temperature": 0.7,
            },
        },
        timeout=120,
    )
    if result.get("error"):
        raise RuntimeError(result["error"])
    info = post("/api/show", {"model": item["alias"]})
    if "tools" not in info.get("capabilities", []):
        raise RuntimeError(
            "Weights installed, but Ollama did not advertise tool support. This model is not ready for agent tasks."
        )
    if item.get("candidate"):
        approve_candidate(item)
    if not source_was_cached:
        record_owned_source(item["source"])
    progress(
        "Installed. Tool capability is advertised; reliability still needs task-specific testing."
    )
    return item["alias"]


def owned_sources():
    path = ROOT / "LongTasks/auto-owned-models.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return set(value) if isinstance(value, list) else set()
    except (OSError, ValueError):
        return set()


def record_owned_source(source):
    """Only sources downloaded by this app may be deleted automatically."""
    path = ROOT / "LongTasks/auto-owned-models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(sorted(owned_sources() | {source})), encoding="utf-8")
    os.replace(temporary, path)


def main_source_record():
    """Read the source tag Cale selected for its stable main alias."""
    try:
        value = json.loads((ROOT / "LongTasks/main-model-source.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record_main_source(source, owned):
    """Persist provenance separately from aliases, which Ollama may recreate."""
    path = ROOT / "LongTasks/main-model-source.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"source": source, "owned": bool(owned)}), encoding="utf-8")
    os.replace(temporary, path)


def queue_retired_main_source(source):
    """Defer deletion until all workers finish and the routine alias is rebuilt."""
    path = ROOT / "LongTasks/retired-main-sources.json"
    try:
        pending = json.loads(path.read_text())
        pending = pending if isinstance(pending, list) else []
    except (OSError, ValueError):
        pending = []
    if source not in pending:
        pending.append(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(pending), encoding="utf-8")
    os.replace(temporary, path)


def cleanup_retired_main_sources():
    """Remove only Cale-owned source tags after their replacement is ready."""
    path = ROOT / "LongTasks/retired-main-sources.json"
    try:
        pending = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(pending, list) or not pending:
        return 0
    try:
        from long_tasks import projects

        if any(task["status"] in ("queued", "working", "checking") for task in projects()):
            return 0
    except Exception:
        return 0
    current = main_source_record().get("source")
    if not isinstance(current, str) or not current:
        return 0  # No recorded replacement means there is no deletion proof.
    try:
        main = CATALOG["main"]["alias"]
        info = post("/api/show", {"model": main}, timeout=10)
        context = re.search(r"(?m)^\s*num_ctx\s+(\d+)\s*$", info.get("parameters", ""))
        if (info.get("remote_host") or info.get("remote_model") or
                "tools" not in info.get("capabilities", []) or
                context is None or int(context.group(1)) < 64000):
            return 0
        if int(context.group(1)) > 65536:
            if not ensure_main_standard():
                return 0  # Rebuild the routine alias before removing its old source.
        else:
            try:
                post("/api/show", {"model": STANDARD_MAIN_ALIAS}, timeout=10)
            except (OSError, ValueError):
                pass
            else:
                delete_model(STANDARD_MAIN_ALIAS)  # No separate routine alias is needed.
    except (OSError, RuntimeError, ValueError):
        return 0
    owned = owned_sources()
    remaining = []
    removed = 0
    for source in pending:
        if not isinstance(source, str) or source == current or source not in owned:
            continue
        try:
            delete_model(source)
            removed += 1
        except Exception:
            remaining.append(source)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(remaining), encoding="utf-8")
    os.replace(temporary, path)
    return removed


def retire_slow_model(item):
    """Remove an app alias only while idle; remove weights only if app-owned."""
    if alias_update_waits_for_tasks(item):
        return False
    alias = item["alias"]
    try:
        delete_model(alias)
    except HTTPError as error:
        # A previous pass may already have removed the alias. Ollama returns
        # 404 for that case, which is a successful final state for retirement.
        if error.code != 404:
            return False
    except Exception:
        return False
    source = item.get("source")
    if source and source in owned_sources():
        try:
            delete_model(source)
        except Exception:
            pass
    return True


def weight_blob(info):
    """Identify the actual Ollama weights behind an alias, if reported."""
    match = re.search(
        r"(?im)^FROM\s+.*?sha256[-:]([0-9a-f]{64})(?:\s|$)",
        str(info.get("modelfile", "")),
    )
    return match.group(1).lower() if match else ""


def repair_main_alias(ram=None, vram=None, *, alias=None):
    """Replace a missing, unusable or measurably slower main alias.

    The router and Hermes configuration both address this stable alias. Deleting
    it while leaving the configuration unchanged makes every worker hit a 404.
    A replacement must already be installed, tool-ready and suitable for this
    PC; an active worker may keep using an existing alias until it finishes.
    """
    import model_fitness

    ram = total_ram_gb() if ram is None else ram
    vram = model_fitness.gpu_vram_gb() if vram is None else vram
    with urlopen("http://127.0.0.1:11434/api/tags", timeout=10) as response:
        tags = json.load(response)
    sizes = {entry["name"]: entry["size"] / 2**30 for entry in tags.get("models", [])}
    # The supervisor reads Hermes' current config for each run. This module
    # may have been imported before that config changed, so use its requested
    # alias when repairing the model the worker will actually call.
    main = alias or CATALOG["main"]["alias"]
    current_size = sizes.get(main, sizes.get(main + ":latest", 0))
    current_info = {}
    current_usable = False
    if current_size and model_fitness.suitable(main, current_size, ram, vram):
        try:
            current_info = post("/api/show", {"model": main}, timeout=10)
            current_usable = (not current_info.get("remote_host")
                              and not current_info.get("remote_model")
                              and "tools" in current_info.get("capabilities", []))
        except (OSError, ValueError):
            pass  # A listed alias still needs a verified tool-capable replacement.
    if current_size and alias_update_waits_for_tasks({**CATALOG["main"], "alias": main}):
        return False
    current_blob = weight_blob(current_info) if current_usable else ""
    configured = re.search(
        r"(?m)^\s*num_ctx\s+(\d+)\s*$", current_info.get("parameters", "")
    ) if current_usable else None
    current_context = int(configured.group(1)) if configured else 65536
    candidates = []
    for profile, item in CATALOG.items():
        if profile == "main" or item.get("fallback_only") or item.get("auto") is False:
            continue
        if item.get("coding_probe") and not model_fitness.game_build_approved(item, ROOT):
            continue
        alias = item.get("alias", "")
        weight = sizes.get(alias, sizes.get(alias + ":latest", 0))
        if not weight or not model_fitness.suitable(alias, weight, ram, vram):
            continue
        try:
            if model_ready(profile):
                # Hardware and speed gates have already excluded unsuitable
                # weights. Keep the strongest qualified model for demanding
                # work even before it has accumulated three timed task calls.
                # The router still uses the small alias for quick requests.
                speed = model_fitness.measured_speed(alias)
                info = post("/api/show", {"model": alias}, timeout=10) if current_usable else {}
                candidates.append((weight, speed or 0, alias, item.get("source", ""),
                                   weight_blob(info), info))
        except (OSError, RuntimeError, ValueError):
            continue
    if not candidates:
        return False  # Keep an existing alias if no verified replacement exists.
    if current_usable:
        # A stable alias may have no direct timing history. Match its weight
        # blob to a timed catalog alias rather than guessing from model names.
        current_speeds = [speed for _, speed, _, _, blob, _ in candidates
                          if current_blob and blob == current_blob and speed > 0]
        direct_speed = model_fitness.measured_speed(main)
        # The stable alias can retain timing samples from its old weights.
        # A matching current-weight alias gives a better lower bound.
        current_speed = max([direct_speed or 0, *current_speeds])
        if not current_blob or not current_speed:
            return False
        import context_window

        faster = [candidate for candidate in candidates
                  if candidate[4] and candidate[4] != current_blob
                  and current_size * 0.8 <= candidate[0] <= current_size * 1.4
                  and candidate[1] >= current_speed * 1.5
                  and context_window.native_context(candidate[5]) >= current_context]
        if not faster:
            return False
        _, _, replacement, _, _, _ = max(faster, key=lambda candidate: (candidate[1], candidate[0]))
    else:
        _, _, replacement, _, _, _ = max(candidates)
    previous_source = main_source_record() if main == CATALOG["main"]["alias"] else {}
    result = post("/api/create", {
        "model": main, "from": replacement, "stream": False,
        "parameters": {"num_ctx": max(65536, current_context), "num_predict": 8192},
    }, timeout=180)
    if result.get("error"):
        raise RuntimeError(result["error"])
    info = post("/api/show", {"model": main}, timeout=15)
    configured = re.search(r"(?m)^\s*num_ctx\s+(\d+)\s*$", info.get("parameters", ""))
    if (info.get("remote_host") or info.get("remote_model") or
            "tools" not in info.get("capabilities", []) or
            configured is None or int(configured.group(1)) < 64000):
        raise RuntimeError("The replacement main model did not pass its tool and context checks.")
    import context_window

    context_window.sync(ROOT, main)
    if current_usable:
        ensure_main_standard()  # Refresh the routine alias from the new weights.
    replacement_source = next(
        (item.get("source", "") for item in CATALOG.values()
         if item.get("alias") == replacement), ""
    )
    if replacement_source:
        record_main_source(replacement_source, replacement_source in owned_sources())
    if (previous_source.get("owned") and previous_source.get("source")
            and previous_source["source"] != replacement_source):
        queue_retired_main_source(previous_source["source"])
    CATALOG["main"]["alias"] = main
    return True


def save_download_status(path, state, current, percent=0, metadata=None):
    """Write one UI status snapshot atomically so readers never see partial JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "status": state,
                "current": current,
                "percent": percent,
                "updated": time.time(),
                **(metadata or {}),
            }
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def auto_install(status_dir, refresh=True):
    """Install every RAM-compatible optional model, sequentially and with disk headroom.

    A Windows file lock prevents the setup process and open app from pulling the
    same model at once. Individual status files keep the task UI useful even if
    the app window closes while downloads continue.
    """
    import msvcrt

    if refresh:
        refresh_catalog()
    status_dir = Path(status_dir)
    status_dir.mkdir(parents=True, exist_ok=True)
    with (status_dir / "auto-models.lock").open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        worker_status = status_dir / "auto-model-worker.json"
        try:
            standard_status = status_dir / "optional-model-main-standard.json"
            try:
                from long_tasks import projects

                active = any(task["status"] in ("queued", "working", "checking")
                             for task in projects())
                if active:
                    save_download_status(standard_status, "waiting_tasks",
                                         "Waiting to prepare the routine context window", 0)
                else:
                    prepared = ensure_main_standard()
                    save_download_status(standard_status,
                                         "complete" if prepared else "skipped",
                                         "Routine context window ready" if prepared else
                                         "Using a faster local model instead", 100)
            except Exception as exc:
                save_download_status(standard_status, "error", str(exc), 0)
                # A full-context alias remains usable; other downloads continue.
            import model_fitness

            ram = total_ram_gb()
            vram = model_fitness.gpu_vram_gb()
            # Compare new coding candidates to the existing fast model on
            # this PC. A tiny public-fixture probe runs only while idle.
            baseline = CATALOG.get("fast")
            baseline_report = status_dir / "game-coding-baseline.json"
            try:
                if (baseline and not alias_update_waits_for_tasks(baseline) and model_ready("fast")
                        and model_fitness.profile_allows(baseline, ram, vram)):
                    if not model_fitness.game_probe_current(baseline["source"], ROOT):
                        save_download_status(baseline_report, "working", "Testing fast-model game coding", 0)
                        model_fitness.probe_game_coding(baseline["source"], ROOT)
                    save_download_status(baseline_report, "complete", "Fast model coding comparison checked", 100)
            except Exception as exc:
                save_download_status(baseline_report, "error", str(exc)[:180], 0)
            candidates = sorted(
                (
                    (profile, model)
                    for profile, model in CATALOG.items()
                    if profile != "main" and model.get("auto", True)
                ),
                key=lambda pair: pair[1]["download_gb"],
            )
            for index, (profile, model) in enumerate(candidates, 1):
                path = status_dir / f"optional-model-{profile}.json"
                if ram + 1 < model["ram_gb"]:
                    continue
                if not model_fitness.profile_allows(model, ram, vram):
                    save_download_status(path, "skipped_hardware",
                                         "This model would be too slow on this PC", 0)
                    continue
                speed = model_fitness.measured_speed(model.get("alias", profile))
                if speed is not None and speed < 5:
                    retired = retire_slow_model(model)
                    waiting = not retired and alias_update_waits_for_tasks(model)
                    status = "retired" if retired else ("waiting_tasks" if waiting else "error")
                    detail = (f"Measured {speed:.1f} tokens/s; removed slow model"
                              if retired else f"Measured {speed:.1f} tokens/s; "
                              + ("removing when idle" if waiting
                                 else "could not remove slow model; will retry"))
                    save_download_status(path, status, detail, 0)
                    continue
                try:
                    previous = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    previous = {}
                if not isinstance(previous, dict):
                    previous = {}
                try:
                    last_probe = float(previous.get("updated") or 0)
                except (TypeError, ValueError, OverflowError):
                    last_probe = 0
                if (previous.get("status") in ("skipped_slow", "skipped_incompatible")
                        and previous.get("source") == model.get("source")
                        and previous.get("ram_gb") == round(ram)
                        and previous.get("vram_gb") == round(vram, 1)
                        and 0 <= time.time() - last_probe < 7 * 86400):
                    continue  # Recheck after a week or a hardware/model change.
                try:
                    if model_ready(profile):
                        if (model.get("coding_probe")
                                and not alias_update_waits_for_tasks(model)
                                and not model_fitness.game_probe_current(model["source"], ROOT)):
                            save_download_status(path, "working", "Checking game coding accuracy and speed", 98)
                            model_fitness.probe_game_coding(model["source"], ROOT)
                        save_download_status(
                            path, "complete", "Installed and ready to use", 100
                        )
                        continue
                except Exception:
                    # Ollama may not be running yet, or this alias may be absent.
                    # The pull below will give a concrete error if it cannot connect.
                    pass
                if alias_update_waits_for_tasks(model):
                    save_download_status(
                        path, "waiting_tasks", "Waiting for active tasks before updating this model", 0
                    )
                    continue
                # Creating a wider alias for weights already in Ollama needs
                # very little disk; a new pull needs room for the whole model.
                free_gb = shutil.disk_usage(model_store_path()).free / 2**30
                required_gb = 2 if source_cached(model) else model["download_gb"] + 12
                if free_gb < required_gb:
                    save_download_status(
                        path,
                        "waiting_storage",
                        f"Needs about {required_gb} GB free to download safely.",
                        0,
                    )
                    continue
                save_download_status(
                    worker_status,
                    "working",
                    f"Downloading {model['label']}",
                    round(100 * (index - 1) / len(candidates)),
                )
                save_download_status(path, "working", "Starting automatic download", 0)

                def report(message):
                    match = re.search(
                        r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*GB", message
                    )
                    percent = (
                        min(
                            95,
                            round(95 * float(match.group(1)) / float(match.group(2))),
                        )
                        if match and float(match.group(2))
                        else 0
                    )
                    save_download_status(path, "working", message, percent)
                    save_download_status(
                        worker_status,
                        "working",
                        f"{model['label']}: {message}",
                        round(100 * (index - 1 + percent / 100) / len(candidates)),
                    )

                try:
                    install(profile, report)
                    save_download_status(
                        path, "complete", "Installed and ready to use", 100
                    )
                except SlowModelError as exc:
                    save_download_status(path, "skipped_slow", str(exc), 0, {
                        "source": model.get("source"), "ram_gb": round(ram),
                        "vram_gb": round(vram, 1),
                    })
                except CandidateModelError as exc:
                    save_download_status(path, "skipped_incompatible", str(exc), 0, {
                        "source": model.get("source"), "ram_gb": round(ram),
                        "vram_gb": round(vram, 1),
                    })
                except Exception as exc:
                    save_download_status(path, "error", str(exc), 0)
                    # One unavailable model must not block smaller or different models.
            if not alias_update_waits_for_tasks(CATALOG["main"]):
                if repair_main_alias(ram, vram):
                    save_download_status(standard_status, "complete",
                                         "Main AI switched to verified weights this PC can run", 100)
                cleanup_retired_main_sources()
            save_download_status(
                worker_status,
                "complete",
                "Automatic model checks finished",
                100,
                {"catalog": catalog_signature()},
            )
        except Exception as exc:
            save_download_status(
                worker_status, "error", str(exc), 0, {"catalog": catalog_signature()}
            )
            raise
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def repair_vision_alias() -> bool:
    """Rebuild a lost image-reader alias from verified weights already on this PC.

    This does not pull another model. The next ordinary readiness probe must
    describe an image before the UI marks image recognition ready again.
    """
    import msvcrt

    state = ROOT / "LongTasks"
    state.mkdir(parents=True, exist_ok=True)
    status = state / "vision-repair.json"
    with (state / "vision-repair.lock").open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"1")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        try:
            try:
                from long_tasks import projects
                if any(task["status"] in ("queued", "working", "checking")
                       for task in projects()):
                    save_download_status(status, "waiting_tasks", "Waiting for active tasks", 0)
                    return False
            except Exception:
                save_download_status(status, "waiting_tasks", "Waiting for task status", 0)
                return False
            try:
                current = post("/api/show", {"model": "cale-vision"}, timeout=10)
                if ("vision" in current.get("capabilities", [])
                        and not current.get("remote_host")
                        and not current.get("remote_model")):
                    save_download_status(status, "complete", "Local image reader ready", 100)
                    return True
            except (OSError, ValueError):
                pass
            sources = dict.fromkeys((
                "cale-fast", "cale-local", main_source_record().get("source", ""),
            ))
            for source in sources:
                if not source or source == "cale-vision":
                    continue
                try:
                    info = post("/api/show", {"model": source}, timeout=10)
                    if ("vision" not in info.get("capabilities", [])
                            or info.get("remote_host") or info.get("remote_model")):
                        continue
                    save_download_status(status, "working", "Restoring local image reader", 25)
                    created = post("/api/create", {
                        "model": "cale-vision", "from": source, "stream": False,
                        "parameters": {"num_ctx": 8192, "num_predict": 1200,
                                       "temperature": 0.2},
                    }, timeout=180)
                    if created.get("error"):
                        raise RuntimeError(str(created["error"]))
                    verified = post("/api/show", {"model": "cale-vision"}, timeout=10)
                    if ("vision" not in verified.get("capabilities", [])
                            or verified.get("remote_host") or verified.get("remote_model")):
                        raise RuntimeError("The restored local image reader failed verification.")
                    save_download_status(status, "complete", "Local image reader restored", 100)
                    return True
                except (OSError, ValueError, RuntimeError) as exc:
                    save_download_status(status, "error", str(exc)[:180], 0)
            save_download_status(status, "waiting_models",
                                 "Waiting for local vision-capable weights", 0)
            return False
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("profile", nargs="?", choices=list(CATALOG)[1:])
    parser.add_argument("--status-file")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--repair-vision", action="store_true")
    parser.add_argument("--status-dir")
    args = parser.parse_args()
    if args.auto:
        if args.profile:
            parser.error("Use --auto without a profile.")
        auto_install(args.status_dir or ROOT / "LongTasks")
    elif args.repair_vision:
        if args.profile:
            parser.error("Use --repair-vision without a profile.")
        repair_vision_alias()
    elif not args.profile:
        parser.error("Choose a profile or use --auto.")
    elif args.status_file:
        save_download_status(args.status_file, "working", "Starting download", 0)

        def report(message):
            match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*GB", message)
            percent = (
                min(95, round(95 * float(match.group(1)) / float(match.group(2))))
                if match and float(match.group(2))
                else 0
            )
            save_download_status(args.status_file, "working", message, percent)

        try:
            install(args.profile, report)
            from cloud_ai import select_profile

            select_profile(args.profile)
            save_download_status(
                args.status_file,
                "complete",
                "Installed and ready for the next task",
                100,
            )
        except Exception as exc:
            save_download_status(args.status_file, "error", str(exc), 0)
            raise
    else:
        install(args.profile, lambda message: print(message, flush=True))
