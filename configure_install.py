"""Apply the published template and materialize the selected local model."""

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

from ruamel.yaml import YAML


def setup_progress(root, percent, current):
    """Publish model setup progress to the desktop task card."""
    path = root / "LongTasks/install-progress.json"
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "percent": percent,
                "current": current,
                "status": "working",
                "updated": time.time(),
            }
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def configure(root, profile):
    """Install the selected main and vision models, then configure Hermes."""
    root = Path(root).resolve()
    for folder in (
        "Temp",
        "Projects",
        "LongTasks",
        "Browser-output",
        "Recovery",
        "Cloud",
    ):
        (root / folder).mkdir(exist_ok=True)
    selection = json.loads((root / "models/manifest.json").read_text())[profile]
    with urlopen(
        Request(
            "http://127.0.0.1:11434/api/pull",
            data=json.dumps({"model": selection["source"], "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=3600,
    ) as response:
        last = 0
        for line in response:
            data = json.loads(line)
            if data.get("error"):
                raise RuntimeError(data["error"])
            if time.monotonic() - last > 5 or data.get("status") == "success":
                print(
                    data.get("status"),
                    f"{data.get('completed', 0) / 1e9:.1f}/{data.get('total', 0) / 1e9:.1f} GB",
                    flush=True,
                )
                if data.get("total"):
                    setup_progress(
                        root,
                        60 + round(14 * data.get("completed", 0) / data["total"]),
                        "Downloading your main AI model",
                    )
                last = time.monotonic()
    body = {
        "model": "cale-local",
        "from": selection["source"],
        "stream": False,
        "parameters": {
            "num_ctx": max(selection["context"], 65536),
            "num_predict": 8192,
            "temperature": 1,
            "top_p": 0.95,
            "top_k": 40,
            "repeat_penalty": 1,
        },
    }
    with urlopen(
        Request(
            "http://127.0.0.1:11434/api/create",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=180,
    ) as response:
        data = json.load(response)
        if data.get("error"):
            raise RuntimeError(data["error"])
    vision_source = "qwen3.5:9b"
    setup_progress(root, 75, "Downloading the vision model")
    with urlopen(
        Request(
            "http://127.0.0.1:11434/api/pull",
            data=json.dumps({"model": vision_source, "stream": False}).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=3600,
    ) as response:
        data = json.load(response)
        if data.get("error"):
            raise RuntimeError(data["error"])
    vision = {
        "model": "cale-vision",
        "from": vision_source,
        "stream": False,
        "parameters": {"num_ctx": 8192, "num_predict": 1200, "temperature": 0.2},
    }
    with urlopen(
        Request(
            "http://127.0.0.1:11434/api/create",
            data=json.dumps(vision).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=180,
    ) as response:
        data = json.load(response)
        if data.get("error"):
            raise RuntimeError(data["error"])
    # The vision weights are also the fast, tool-capable route for short tasks.
    fast = {
        "model": "cale-fast",
        "from": vision_source,
        "stream": False,
        "parameters": {"num_ctx": 65536, "num_predict": 4096, "temperature": 0.7},
    }
    with urlopen(
        Request(
            "http://127.0.0.1:11434/api/create",
            data=json.dumps(fast).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=180,
    ) as response:
        data = json.load(response)
        if data.get("error"):
            raise RuntimeError(data["error"])
    setup_progress(root, 78, "Configuring the local AI")
    yaml = YAML()
    config = yaml.load(
        (root / "config-template.yaml")
        .read_text(encoding="utf-8")
        .replace("{{ROOT}}", root.as_posix())
    )
    # Hermes refuses agent runs below 64K. Every automatically routed model is
    # configured for at least that window, including the small local choices.
    config["model"]["context_length"] = max(selection["context"], 65536)
    config["auxiliary"]["vision"]["enabled"] = True
    config["auxiliary"]["vision"]["model"] = "cale-vision"
    (root / "Media").mkdir(exist_ok=True)
    (root / "Media/settings.json").write_text(
        json.dumps({"vision_model": "cale-vision"}), encoding="utf-8"
    )
    path = root / "data/config.yaml"
    if path.exists():
        shutil.copy2(
            path, root / "Recovery" / f"config-before-install-{int(time.time())}.yaml"
        )
    with path.open("w", encoding="utf-8") as stream:
        yaml.dump(config, stream)
    # A 64K bootstrap window satisfies Hermes; the final window follows the
    # installed model and this PC's RAM rather than staying pinned at 64K.
    import context_window

    context_window.sync(root, "cale-local")
    # Compatibility patch is MIT-licensed upstream code plus a Windows fix.
    patch = root / "windows-file-tools.patch"
    managed_git = root / "data/tools/git-2.53.0+3-win32-x64/cmd/git.exe"
    git = str(managed_git) if managed_git.exists() else shutil.which("git")
    if not git:
        raise RuntimeError(
            "Git was installed by Hermes but could not be located for the Windows compatibility patch."
        )
    if (
        subprocess.run(
            [
                git,
                "-C",
                str(root / "app"),
                "apply",
                "--ignore-space-change",
                "--check",
                str(patch),
            ],
            capture_output=True,
        ).returncode
        == 0
    ):
        subprocess.run(
            [
                git,
                "-C",
                str(root / "app"),
                "apply",
                "--ignore-space-change",
                str(patch),
            ],
            check=True,
        )
    elif subprocess.run(
        [
            git,
            "-C",
            str(root / "app"),
            "apply",
            "--ignore-space-change",
            "--reverse",
            "--check",
            str(patch),
        ],
        capture_output=True,
    ).returncode:
        raise RuntimeError("The Windows file patch did not match this Hermes version.")
    skill = root / "Toolbox/node_modules/rea-agents/skills/reverse-engineer-anything"
    if skill.exists():
        shutil.copytree(
            skill, root / "data/skills/reverse-engineer-anything", dirs_exist_ok=True
        )
    env = dict(os.environ, HERMES_HOME=str(root / "data"))
    engine = str(root / "data/bin/hermes.exe")
    boards = subprocess.run(
        [engine, "kanban", "boards", "list", "--json"],
        env=env,
        capture_output=True,
        text=True,
    )
    if "long-tasks" not in boards.stdout:
        subprocess.run(
            [engine, "kanban", "boards", "create", "long-tasks"], env=env, check=True
        )
    print("Local model and persistent task board configured.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    configure(args.root, args.profile)
