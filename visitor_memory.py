"""Local, self-declared visitor names; no face or voice identity matching."""

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PEOPLE = ROOT / "Media/known-visitors.json"
INTRO = re.compile(r"^(?:i am|i'm|my name is)\s+([A-Za-z][A-Za-z'-]*(?: [A-Za-z][A-Za-z'-]*)?)[.!? ]*$", re.I)
NOT_NAMES = {"making", "working", "trying", "looking", "using", "going", "doing", "here", "back", "fine", "good", "ready"}


def name_from_intro(phrase):
    match = INTRO.fullmatch(str(phrase).strip())
    if not match or match.group(1).split()[0].lower() in NOT_NAMES:
        return None
    return match.group(1).strip().title()


def remember_intro(phrase):
    """Save only a name a person volunteered, not camera images or biometrics."""
    name = name_from_intro(phrase)
    if not name:
        return None
    try:
        people = json.loads(PEOPLE.read_text(encoding="utf-8"))
        if not isinstance(people, dict):
            people = {}
    except (OSError, ValueError):
        people = {}
    people[name] = {"introduced_at": time.time(), "source": "spoken introduction"}
    PEOPLE.parent.mkdir(exist_ok=True)
    PEOPLE.write_text(json.dumps(people, indent=2), encoding="utf-8")
    return name


def list_visitors():
    try:
        people = json.loads(PEOPLE.read_text(encoding="utf-8"))
        return {"names": sorted(people) if isinstance(people, dict) else []}
    except (OSError, ValueError):
        return {"names": []}


def forget_visitor(name):
    """Remove a voluntarily supplied name and its local face examples."""
    from face_identity import EMBEDDINGS, _known

    name = str(name)
    try:
        people = json.loads(PEOPLE.read_text(encoding="utf-8"))
        if not isinstance(people, dict):
            people = {}
    except (OSError, ValueError):
        people = {}
    removed = people.pop(name, None) is not None
    examples = _known()
    removed = examples.pop(name, None) is not None or removed
    PEOPLE.parent.mkdir(exist_ok=True)
    PEOPLE.write_text(json.dumps(people, indent=2), encoding="utf-8")
    EMBEDDINGS.write_text(json.dumps(examples), encoding="utf-8")
    return {"name": name, "forgotten": removed}
