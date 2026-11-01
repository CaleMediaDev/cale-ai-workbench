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
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CATALOG = {
    "main": {"label": "Main coding + optional free cloud", "alias": "cale-local"},
    "permissive": {
        "label": "Qwen Coder Next permissive (local)",
        "alias": "cale-permissive",
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
        "context": 32768,
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
        "context": 32768,
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
        "context": 32768,
    },
    "glm": {
        "label": "ArliAI GLM 4.5 Air (experimental)",
        "alias": "cale-glm45-derestricted",
        "source": "hf.co/mradermacher/GLM-4.5-Air-Derestricted-GGUF:Q4_K_M",
        "download_gb": 67,
        "ram_gb": 112,
        "origin": "https://huggingface.co/ArliAI/GLM-4.5-Air-Derestricted",
        "conversion": "https://huggingface.co/mradermacher/GLM-4.5-Air-Derestricted-GGUF",
        "license": "MIT",
        "context": 32768,
    },
}


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


def model_ready(profile):
    """Check actual local Ollama state instead of trusting an old UI status file."""
    info = post("/api/show", {"model": CATALOG[profile]["alias"]}, timeout=10)
    return (
        not info.get("remote_host")
        and not info.get("remote_model")
        and "tools" in info.get("capabilities", [])
    )


def catalog_signature():
    """Change when the optional download set or its model sources change."""
    options = {
        profile: {"source": item.get("source", ""), "auto": item.get("auto", True)}
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
    result = post(
        "/api/create",
        {
            "model": item["alias"],
            "from": item["source"],
            "stream": False,
            "parameters": {
                "num_ctx": item["context"],
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
    progress(
        "Installed. Tool capability is advertised; reliability still needs task-specific testing."
    )
    return item["alias"]


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


def auto_install(status_dir):
    """Install every RAM-compatible optional model, sequentially and with disk headroom.

    A Windows file lock prevents the setup process and open app from pulling the
    same model at once. Individual status files keep the task UI useful even if
    the app window closes while downloads continue.
    """
    import msvcrt

    status_dir = Path(status_dir)
    status_dir.mkdir(parents=True, exist_ok=True)
    with (status_dir / "auto-models.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
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
            ram = total_ram_gb()
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
                # A model needs space for weights plus Ollama's temporary files.
                free_gb = shutil.disk_usage(model_store_path()).free / 2**30
                if free_gb < model["download_gb"] + 12:
                    save_download_status(
                        path,
                        "waiting_storage",
                        f"Needs about {model['download_gb'] + 12} GB free to download safely.",
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
                except Exception as exc:
                    save_download_status(path, "error", str(exc), 0)
                    # One unavailable model must not block smaller or different models.
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
