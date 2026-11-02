"""Turn a requested singer description into explicit ACE-Step style cues.

The generator's prompt is a music caption, so short vocal descriptions work
better than relying on a singer's name alone. This does not clone a voice or
claim to detect the rendered singer's gender from audio.
"""

from __future__ import annotations

import re

FEMALE_REFERENCES = (
    "katy perry", "katy pary", "taylor swift", "ariana grande", "adele", "dua lipa",
    "lady gaga", "rihanna", "billie eilish", "beyonce", "beyoncé",
    "miley cyrus", "olivia rodrigo", "sabrina carpenter", "chappell roan",
)


def vocal_intent(prompt: str, instrumental: bool = False) -> str:
    """Use the person's current request before any appended learned examples."""
    if instrumental:
        return "none"
    request = prompt.split("Past user feedback", 1)[0].lower()
    female = bool(re.search(r"\b(?:female|woman|women|girl|feminine)\b", request))
    male = bool(re.search(r"\b(?:male|man|men|guy|masculine)\b", request))
    if female and male:
        return "duet"
    if female:
        return "female"
    if male:
        return "male"
    if any(name in request for name in FEMALE_REFERENCES):
        return "female"
    return "unspecified"


def enhance(prompt: str, instrumental: bool = False) -> str:
    """Make vocal intent unambiguous in the local music model's caption."""
    intent = vocal_intent(prompt, instrumental)
    cues = []
    if intent == "female":
        cue = "Prominent expressive female lead vocals throughout, clear sung verses and chorus"
        if any(name in prompt.lower() for name in ("katy perry", "katy pary")):
            cue += ", bright polished dance-pop production and a catchy anthemic hook"
        cues.append(cue)
    elif intent == "male":
        cues.append("Prominent expressive male lead vocals throughout, clear sung verses and chorus")
    request = prompt.split("Past user feedback", 1)[0].lower()
    if (re.search(r"\b(?:techno|tehno|electronic dance|dubstep|edm)\b", request)
            and not re.search(r"\b(?:guitar|acoustic|live band|rock)\b", request)):
        cues.append(
            "Entirely electronic production with a drum machine, four-on-the-floor kick, "
            "sequenced synthesizers, synthetic bass and club textures; no live band instruments"
        )
    additions = [cue for cue in cues if cue.lower() not in prompt.lower()]
    return prompt.rstrip(" .") + ". " + ". ".join(additions) + "." if additions else prompt


def feedback_direction(note: str, original_request: str) -> str:
    """Turn common music complaints into positive production instructions.

    Do not feed the model the words of a rejected instrument or voice: music
    models can latch onto those terms despite negation.
    """
    complaint = note.lower()
    request = original_request.lower()
    cues = []
    if (re.search(r"\b(?:male|man|guy|boy)\s+(?:voice|vocal|singer)\b", complaint)
            and vocal_intent(original_request) == "female"):
        cues.append("Solo expressive female lead singer with a bright, clear sung chorus")
    if (re.search(r"\b(?:guitars?|acoustic|live band)\b", complaint)
            and re.search(r"\b(?:techno|tehno|edm|electronic dance|dubstep)\b", request)):
        cues.append("Strictly electronic club production: synthesized kick, sequenced synth bass, "
                    "drum machine percussion and layered synthesizer leads")
    return ". ".join(cues)


def learned_directions(guidance: str, original_request: str) -> str:
    """Apply only feedback that can be translated into concrete music cues."""
    cues = []
    for line in guidance.splitlines():
        if line.startswith("- Improve: "):
            direction = feedback_direction(line.removeprefix("- Improve: "), original_request)
            if direction and direction not in cues:
                cues.append(direction)
    return ". ".join(cues)
