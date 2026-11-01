"""Download verified workbench releases and apply source updates while idle.

Only the public CaleMediaDev release feed is trusted. User projects, task state,
models, runtime folders and account credentials are never part of an update.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
RELEASES = (
    "https://api.github.com/repos/CaleMediaDev/cale-ai-workbench/releases?per_page=10"
)
MAX_ZIP = 100 * 1024 * 1024


def current_version():
    marker = (ROOT / ".cale-ai-install").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", marker)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def release_candidate(releases, installed):
    """Select the newest three-part release with a published SHA256 digest."""
    choices = []
    for release in releases:
        if release.get("draft"):
            continue
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", release.get("tag_name", ""))
        if not match:
            continue
        version = tuple(map(int, match.groups()))
        if version <= installed:
            continue
        expected_name = "Cale-AI-Workbench-" + ".".join(map(str, version)) + ".zip"
        for asset in release.get("assets", []):
            digest = asset.get("digest", "")
            if (
                asset.get("name") == expected_name
                and re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest)
                and 0 < int(asset.get("size", 0)) <= MAX_ZIP
                and asset.get("browser_download_url", "").startswith(
                    "https://github.com/CaleMediaDev/cale-ai-workbench/releases/download/"
                )
            ):
                choices.append((version, asset))
    return max(choices, default=None, key=lambda choice: choice[0])


def fetch_json(url):
    with urlopen(
        Request(url, headers={"User-Agent": "Cale-AI-Workbench"}), timeout=15
    ) as response:
        return json.load(response)


def validated_manifest(archive):
    """Accept only flat relative release paths declared by its source manifest."""
    names = set(archive.namelist())
    if "install-files.json" not in names:
        raise ValueError("Release has no install manifest.")
    manifest = json.loads(archive.read("install-files.json"))
    if not isinstance(manifest, list) or not manifest or len(manifest) > 200:
        raise ValueError("Invalid release manifest.")
    files = [*manifest, "install-files.json"]
    if len(set(files)) != len(files):
        raise ValueError("Duplicate release file.")
    protected = {
        "LongTasks",
        "Projects",
        "Cloud",
        "Media",
        "data",
        "Toolbox",
        "Updates",
        "Temp",
        "Downloads",
    }
    expanded = 0
    for name in files:
        if not isinstance(name, str):
            raise ValueError("Invalid release path.")
        pure = PurePosixPath(name)
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or ":" in name
            or name != pure.as_posix()
            or any(part in ("", ".", "..") for part in pure.parts)
            or pure.parts[0] in protected
            or name not in names
            or archive.getinfo(name).is_dir()
        ):
            raise ValueError("Unsafe or missing release file.")
        expanded += archive.getinfo(name).file_size
        if (
            archive.getinfo(name).file_size > 20 * 1024 * 1024
            or expanded > 80 * 1024 * 1024
        ):
            raise ValueError("Release contents are too large.")
    return files


def stage():
    """Download and verify a newer release; return its archive path or None."""
    choice = release_candidate(fetch_json(RELEASES), current_version())
    if not choice:
        return None
    version, asset = choice
    folder = ROOT / "Updates"
    folder.mkdir(exist_ok=True)
    destination = folder / asset["name"]
    digest = asset["digest"].split(":", 1)[1].lower()
    if (
        not destination.is_file()
        or hashlib.sha256(destination.read_bytes()).hexdigest() != digest
    ):
        temporary = destination.with_suffix(".partial")
        total = 0
        checksum = hashlib.sha256()
        with (
            urlopen(
                Request(
                    asset["browser_download_url"],
                    headers={"User-Agent": "Cale-AI-Workbench"},
                ),
                timeout=30,
            ) as response,
            temporary.open("wb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ZIP:
                    raise ValueError("Release archive is too large.")
                output.write(chunk)
                checksum.update(chunk)
        if checksum.hexdigest() != digest or total != int(asset["size"]):
            temporary.unlink(missing_ok=True)
            raise ValueError(
                "Release checksum does not match GitHub's published digest."
            )
        os.replace(temporary, destination)
    with ZipFile(destination) as archive:
        validated_manifest(archive)
    return destination, version


def active_tasks():
    """Source files may change only when no project worker is active."""
    from long_tasks import projects

    return any(
        task["status"] in ("queued", "working", "checking") for task in projects()
    )


def add_design_tools_to_config(root, backup):
    """Migrate only known local settings while preserving user preferences."""
    config = root / "data/config.yaml"
    if not config.is_file():
        return None
    original = config.read_text(encoding="utf-8")
    updated = original
    # Hermes now rejects agent windows below 64K. Older installations pinned
    # the main model to 32K even when its local weights support a larger window.
    model_block = updated.split("fallback_providers:", 1)[0]
    context = re.search(r"(?m)^  context_length: ([0-9]+)[ \t]*$", model_block)
    if context and int(context.group(1)) < 64000:
        updated = updated.replace(context.group(0), "  context_length: 65536", 1)
    if "mcp-design" not in updated:
        anchor = "  - mcp-toolbox\n"
        if anchor not in updated:
            raise RuntimeError("Could not locate the tool list in the existing config.")
        updated = updated.replace(anchor, anchor + "  - mcp-design\n", 1)
    if "  design:\n" not in updated:
        anchor = "skills:\n"
        if anchor not in updated:
            raise RuntimeError("Could not locate the MCP section in the existing config.")
        block = (
            "  design:\n"
            f"    command: '{(root / 'Toolbox/python/Scripts/python.exe').as_posix()}'\n"
            "    args:\n"
            f"    - '{(root / 'design_tools.py').as_posix()}'\n"
            "    timeout: 120\n"
            "    sampling:\n"
            "      enabled: false\n"
        )
        updated = updated.replace(anchor, block + anchor, 1)
    if updated == original:
        return None
    previous = backup / "data/config.yaml"
    previous.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config, previous)
    config.write_text(updated, encoding="utf-8")
    return config, previous


def apply(archive_path, version):
    """Keep the old source recoverable and swap only files in the manifest."""
    if active_tasks():
        raise RuntimeError("Wait for current tasks to finish before updating.")
    backup = (
        ROOT
        / "Updates/backups"
        / (".".join(map(str, current_version())) + "-" + str(int(time.time())))
    )
    backup.mkdir(parents=True)
    replaced = []
    with ZipFile(archive_path) as archive:
        files = validated_manifest(archive)
        try:
            for name in files:
                target = ROOT / name
                target.parent.mkdir(parents=True, exist_ok=True)
                previous = backup / name
                if target.is_file():
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, previous)
                staged = target.with_name(target.name + ".updating")
                staged.write_bytes(archive.read(name))
                os.replace(staged, target)
                replaced.append((target, previous))
            migration = add_design_tools_to_config(ROOT, backup)
            if migration:
                replaced.append(migration)
            marker = ROOT / ".cale-ai-install"
            marker.write_text(
                "Cale AI Workbench " + ".".join(map(str, version)), encoding="utf-8"
            )
        except Exception:
            for target, previous in reversed(replaced):
                if previous.is_file():
                    shutil.copy2(previous, target)
                else:
                    target.unlink(missing_ok=True)
            raise
    return backup


def wait_for_exit(pid):
    if os.name != "nt" or not pid:
        return
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
    if handle:
        try:
            ctypes.windll.kernel32.WaitForSingleObject(handle, 60_000)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)


def restart_router_after_update():
    """Replace the old in-memory routing code after the UI has gone idle."""
    import psutil

    pid_file = ROOT / "Cloud/router-pid.txt"
    try:
        pid = int(pid_file.read_text(encoding="utf-8"))
        process = psutil.Process(pid)
        command = process.cmdline()
        script = str((ROOT / "cloud_ai.py").resolve()).lower()
        if script in [part.lower() for part in command] and "--serve" in command:
            process.terminate()
            try:
                process.wait(timeout=10)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    except (OSError, ValueError, psutil.Error):
        pass
    from cloud_ai import ensure_router

    ensure_router()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    if not args.apply:
        result = stage()
        if result:
            print(
                json.dumps(
                    {
                        "archive": str(result[0]),
                        "version": ".".join(map(str, result[1])),
                    }
                )
            )
        return
    match = re.fullmatch(r"\d+\.\d+\.\d+", args.version or "")
    if not match:
        raise ValueError("Invalid update version.")
    wait_for_exit(args.parent_pid)
    try:
        apply(args.apply, tuple(map(int, args.version.split("."))))
        restart_router_after_update()
    except Exception as exc:
        log = ROOT / "Updates/last-error.txt"
        log.parent.mkdir(exist_ok=True)
        log.write_text(str(exc), encoding="utf-8")
        raise
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    subprocess.Popen(
        [
            str(pythonw if pythonw.is_file() else executable),
            str(ROOT / "long_tasks.py"),
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0,
    )


if __name__ == "__main__":
    main()
