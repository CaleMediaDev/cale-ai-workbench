"""Install Cale's pinned, locally bundled engineering skills into Hermes.

This copies only reviewed Markdown from the release. The state file records
which exact bytes Cale owns, so source updates never overwrite a user's edits.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    for name, category in SKILLS.items():
        folder = bundled / name
        for source in sorted(folder.rglob("*")):
            if source.is_file() and not source.is_symlink():
                relative = Path(category) / name / source.relative_to(folder)
                yield source, relative
    for name in SHARED_REFERENCES:
        yield bundled / "references" / name, Path("references") / name


def sync(root: str | Path) -> dict:
    """Copy new/unchanged managed files, preserving local skill modifications."""
    root = Path(root).resolve(strict=True)
    destination = root / "data/skills"
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
        if target.is_symlink():
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
        temporary = target.with_name(target.name + ".cale-updating")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        managed[key] = _hash(data)
    temporary = marker.with_name(marker.name + ".tmp")
    temporary.write_text(json.dumps(managed, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, marker)
    return {"installed": installed, "updated": updated, "preserved": preserved}


if __name__ == "__main__":
    print(json.dumps(sync(Path(__file__).resolve().parent)))
