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
            re.fullmatch(r"cale-[a-z0-9-]{3,40}", alias)
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
    return approvals.get(item["alias"]) == item["source"]


def approve_candidate(item):
    path = ROOT / "LongTasks/model-approvals.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        approvals = json.loads(path.read_text())
    except (OSError, ValueError):
        approvals = {}
    approvals[item["alias"]] = item["source"]
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(approvals), encoding="utf-8")
    os.replace(temporary, path)


class SlowModelError(RuntimeError):
    """A real local generation probe found this model too slow for agent work."""


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
    raise RuntimeError(
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
    """Do not rebuild an installed alias while a project may be using it."""
    try:
        post("/api/show", {"model": item["alias"]}, timeout=10)
    except (OSError, ValueError, KeyError):
        return False  # An absent alias cannot be in use by an existing worker.
    try:
        from long_tasks import projects

        return any(
            task["status"] in ("queued", "working", "checking")
            for task in projects()
        )
    except Exception:
        return True  # Preserve a working alias if task state is unavailable.


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
    minimum = max(64000, item["context"])
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

    if not model_fitness.hardware_allows(
        item["download_gb"], total_ram_gb(), model_fitness.gpu_vram_gb()
    ):
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


def repair_main_alias(ram=None, vram=None):
    """Replace a missing or painfully slow main alias with verified local weights.

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
    main = CATALOG["main"]["alias"]
    current_size = sizes.get(main, sizes.get(main + ":latest", 0))
    if current_size and model_fitness.suitable(main, current_size, ram, vram):
        try:
            current_info = post("/api/show", {"model": main}, timeout=10)
            if (not current_info.get("remote_host")
                    and not current_info.get("remote_model")
                    and "tools" in current_info.get("capabilities", [])):
                return False
        except (OSError, ValueError):
            pass  # A listed alias still needs a verified tool-capable replacement.
    if current_size and alias_update_waits_for_tasks(CATALOG["main"]):
        return False
    candidates = []
    for profile, item in CATALOG.items():
        if profile == "main" or item.get("fallback_only") or item.get("auto") is False:
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
                candidates.append((weight, speed or 0, alias))
        except (OSError, RuntimeError, ValueError):
            continue
    if not candidates:
        return False  # Keep an existing alias if no verified replacement exists.
    _, _, replacement = max(candidates)
    previous_source = main_source_record()
    result = post("/api/create", {
        "model": main, "from": replacement, "stream": False,
        "parameters": {"num_ctx": 65536, "num_predict": 8192},
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
    replacement_source = next(
        (item.get("source", "") for item in CATALOG.values()
         if item.get("alias") == replacement), ""
    )
    if replacement_source:
        record_main_source(replacement_source, replacement_source in owned_sources())
    if (previous_source.get("owned") and previous_source.get("source")
            and previous_source["source"] != replacement_source):
        queue_retired_main_source(previous_source["source"])
    return True


def save_download_status(path, state, current, percent=0, metadata=None):
    """Write one UI status snapshot atomically so readers never see partial JSON."""
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
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
                if not model_fitness.hardware_allows(model["download_gb"], ram, vram):
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
                if (previous.get("status") == "skipped_slow"
                        and previous.get("source") == model.get("source")
                        and previous.get("ram_gb") == round(ram)
                        and previous.get("vram_gb") == round(vram, 1)
                        and 0 <= time.time() - last_probe < 7 * 86400):
                    continue  # Recheck after a week or a hardware/model change.
                try:
                    if model_ready(profile):
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
                except Exception as exc:
                    save_download_status(path, "error", str(exc), 0)
                    # One unavailable model must not block smaller or different models.
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


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("profile", nargs="?", choices=list(CATALOG)[1:])
    parser.add_argument("--status-file")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--status-dir")
    args = parser.parse_args()
    if args.auto:
        if args.profile:
            parser.error("Use --auto without a profile.")
        auto_install(args.status_dir or ROOT / "LongTasks")
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
