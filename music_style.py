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

NEGATION = r"(?:without|no|not|avoid|exclude|skip|don'?t want|do not want)"
NEGATED_GUITAR = re.compile(
    rf"\b{NEGATION}\s+(?:any\s+)?(?:(?:electric|acoustic|distorted)\s+)?guitars?\b",
    re.I,
)
NEGATED_SINGER = re.compile(
    rf"\b{NEGATION}\s+(?:a\s+)?(male|man|guy|boy|female|woman|girl)"
    r"\s+(?:voice|vocals?|singer)\b", re.I,
)


def _clean_negations(prompt: str) -> tuple[str, bool, bool]:
    """Remove rejected sounds before ACE-Step sees words it may imitate."""
    rejected = [match.group(1).lower() for match in NEGATED_SINGER.finditer(prompt)]
    cleaned = NEGATED_SINGER.sub("", prompt)
    cleaned = NEGATED_GUITAR.sub("", cleaned)
    cleaned = re.sub(r"\s+([,.;!?])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.;")
    return cleaned, any(word in ("male", "man", "guy", "boy") for word in rejected), any(
        word in ("female", "woman", "girl") for word in rejected
    )


def vocal_intent(prompt: str, instrumental: bool = False) -> str:
    """Use the person's current request before any appended learned examples."""
    if instrumental:
        return "none"
    request, no_male, no_female = _clean_negations(
        prompt.split("Past user feedback", 1)[0]
    )
    request = request.lower()
    female = bool(re.search(r"\b(?:female|woman|women|girl|feminine)\b", request))
    male = bool(re.search(r"\b(?:male|man|men|guy|masculine)\b", request))
    if female and male:
        return "duet"
    if female:
        return "female"
    if male:
        return "male"
    if no_male and not no_female:
        return "female"
    if no_female and not no_male:
        return "male"
    if any(name in request for name in FEMALE_REFERENCES):
        return "female"
    return "unspecified"


def enhance(prompt: str, instrumental: bool = False) -> str:
    """Make vocal intent unambiguous in the local music model's caption."""
    intent = vocal_intent(prompt, instrumental)
    cleaned, _, _ = _clean_negations(prompt)
    cues = []
    if intent == "female":
        cue = "Prominent expressive female lead vocals throughout, clear sung verses and chorus"
        if any(name in cleaned.lower() for name in ("katy perry", "katy pary")):
            cue += ", bright polished dance-pop production and a catchy anthemic hook"
        cues.append(cue)
    elif intent == "male":
        cues.append("Prominent expressive male lead vocals throughout, clear sung verses and chorus")
    request = cleaned.split("Past user feedback", 1)[0].lower()
    if (re.search(r"\b(?:techno|tehno|electronic dance|dubstep|edm)\b", request)
            and not re.search(r"\b(?:guitars?|acoustic|live band|rock)\b", request)):
        cues.append(
            "Entirely electronic production with a drum machine, four-on-the-floor kick, "
            "sequenced synthesizers, synthetic bass and club textures"
        )
    additions = [cue for cue in cues if cue.lower() not in cleaned.lower()]
    return ". ".join(additions) + ". " + cleaned if additions else cleaned


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
