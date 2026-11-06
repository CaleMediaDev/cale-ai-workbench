"""Install Cale's pinned, locally bundled engineering skills into Hermes.

This copies only reviewed Markdown from the release. The state file records
which exact bytes Cale owns, so source updates never overwrite a user's edits.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

SKILLS = {
    "api-and-interface-design": "software-development",
    "context-engineering": "software-development",
    "frontend-ui-engineering": "creative",
    "observability-and-instrumentation": "software-development",
    "performance-optimization": "software-development",
    "security-and-hardening": "software-development",
    "shipping-and-launch": "software-development",
}
SHARED_REFERENCES = (
    "accessibility-checklist.md",
    "definition-of-done.md",
    "performance-checklist.md",
    "security-checklist.md",
)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sources(root: Path):
    bundled = root / "vendor/agent-skills"
    if bundled.is_symlink():
        return
    for name, category in SKILLS.items():
        folder = bundled / name
        if folder.is_symlink():
            continue
        for source in sorted(folder.rglob("*")):
            if not source.is_symlink() and source.is_file():
                relative = Path(category) / name / source.relative_to(folder)
                yield source, relative
    for name in SHARED_REFERENCES:
        source = bundled / "references" / name
        if (
            not source.parent.is_symlink()
            and not source.is_symlink()
            and source.is_file()
        ):
            yield source, Path("references") / name


def sync(root: str | Path) -> dict:
    """Copy new/unchanged managed files, preserving local skill modifications."""
    root = Path(root).resolve(strict=True)
    destination = root / "data/skills"
    if not destination.is_symlink() and not destination.exists():
        destination.mkdir(parents=True, exist_ok=True)
    marker = root / "data/.cale-skill-files.json"
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    installed = updated = preserved = 0
    managed = dict(previous)
    for source, relative in _sources(root):
        key = relative.as_posix()
        data = source.read_bytes()
        target = destination / relative
        if (
            any(
                parent.is_symlink() or (parent.exists() and not parent.is_dir())
                for parent in (destination / part for part in reversed(relative.parents))
            )
            or target.is_symlink()
            or (target.exists() and not target.is_file())
        ):
            preserved += 1
            continue
        if target.exists():
            current = _hash(target.read_bytes())
            if current == _hash(data):
                managed[key] = current
                continue
            if previous.get(key) != current:
                preserved += 1
                continue
            updated += 1
        else:
            installed += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=target.name + ".", suffix=".cale-updating", delete=False
        )
        temporary = Path(temporary_file.name)
        try:
            with temporary_file:
                temporary_file.write(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        managed[key] = _hash(data)
    temporary_file = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=marker.parent,
        prefix=marker.name + ".", suffix=".tmp", delete=False
    )
    temporary = Path(temporary_file.name)
    try:
        with temporary_file:
            temporary_file.write(json.dumps(managed, indent=2, sort_keys=True))
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    return {"installed": installed, "updated": updated, "preserved": preserved}


if __name__ == "__main__":
    print(json.dumps(sync(Path(__file__).resolve().parent)))
