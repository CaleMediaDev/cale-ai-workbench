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
    rf"\b{NEGATION}\s+(?:(?:a|any)\s+)?(male|man|guy|boy|female|woman|girl)"
    r"\s+(?:lead\s+)?(?:voice|vocals?|singer)\b", re.I,
)
NEGATED_LEAD_VOCALS = re.compile(
    rf"\b{NEGATION}\s+(?:(?:a|any)\s+)?(?:lead\s+vocals?|singer|rap|rapping|rapper|rapped)\b", re.I,
)
VOCAL_TEXTURE = re.compile(
    r"\b(?:vocal chops?|chopped vocals?|wordless vocals?|"
    r"(?:minimal|sparse|occasional) vocals?)\b", re.I,
)
SPARSE_VOCAL_STYLE = re.compile(
    r"\b(?:minimal lyrics|nonsense vocals|repetitive (?:nonsense|vocal|hook)|"
    r"short (?:repetitive )?(?:vocal )?(?:hooks?|chants?)|vocal chops?|"
    r"vocal hooks?|very little (?:conventional )?singing)\b", re.I,
)
LEAD_VOCALS = re.compile(
    r"\b(?:lead vocals?|sung verses?|singing|singer|rap|rapping|rapper|rapped)\b", re.I,
)
EXPLICIT_LEAD = re.compile(
    r"\b(?:singer|rapper|singing|rapping|sung|rapped|"
    r"(?:male|female|lead|layered|backing|deep|gritty)\s+vocals?|"
    r"vocal\s+(?:verses?|chorus|hook)|rap\s+verses?)\b", re.I,
)
VOCAL_FREE = re.compile(
    rf"\b{NEGATION}\s+(?:any\s+)?(?:vocals|singing)\b", re.I,
)
NO_VOCALS = re.compile(
    rf"\b{NEGATION}\s+(?:any\s+)?(?:vocals|singing|lyrics)\b", re.I,
)
WHOLE_INSTRUMENTAL = re.compile(
    r"\b(?:instrumental\s+(?:song|track|beat|music|piece|version|only)|"
    r"(?:fully|entirely|all)\s+instrumental)\b", re.I,
)


def explicit_lead_vocals(prompt: str, lyrics: str = "") -> bool:
    """Recognize a requested singer or rapper, excluding negated descriptions."""
    if lyrics.strip():
        return True
    request, _, _ = _clean_negations(prompt.split("Past user feedback", 1)[0])
    return bool(EXPLICIT_LEAD.search(request))


def unambiguous_vocal_song(prompt: str, lyrics: str = "") -> bool:
    """Use only clear vocal intent for automatic correction of a music job."""
    request = prompt.split("Past user feedback", 1)[0]
    return (explicit_lead_vocals(request, lyrics)
            and not NO_VOCALS.search(request)
            and not WHOLE_INSTRUMENTAL.search(request))


def requested_bpm(prompt: str) -> int | None:
    """Extract a plausible explicit tempo for ACE-Step's numeric BPM field.

    A caption alone is a weaker hint. The model can still miss the beat, so
    this metadata never substitutes for checking the rendered audio.
    """
    match = re.search(r"\b(\d{2,3})\s*-?\s*bpm\b", prompt, re.I)
    if not match:
        return None
    value = int(match.group(1))
    return value if 40 <= value <= 240 else None


def sparse_vocal_style(prompt: str) -> bool:
    """Identify songs whose voices are short hooks rather than full verses."""
    return bool(SPARSE_VOCAL_STYLE.search(prompt))


def requires_sustained_vocals(prompt: str, lyrics: str = "",
                              instrumental: bool = False) -> bool:
    """Distinguish a lyric-driven song from sparse vocal sound design.

    A request for chops should not trigger full-length lyric writing or a
    speech-recognition threshold intended for verses and choruses.
    """
    if instrumental:
        return False
    if lyrics.strip():
        return True
    request = prompt.split("Past user feedback", 1)[0]
    if VOCAL_FREE.search(request) or WHOLE_INSTRUMENTAL.search(request):
        return False
    request, _, _ = _clean_negations(request)
    return not (VOCAL_TEXTURE.search(request) and not LEAD_VOCALS.search(request))


def _clean_negations(prompt: str) -> tuple[str, bool, bool]:
    """Remove rejected sounds before ACE-Step sees words it may imitate."""
    rejected = [match.group(1).lower() for match in NEGATED_SINGER.finditer(prompt)]
    cleaned = NEGATED_SINGER.sub("", prompt)
    cleaned = NEGATED_LEAD_VOCALS.sub("", cleaned)
    cleaned = NEGATED_GUITAR.sub("", cleaned)
    cleaned = re.sub(r"\s+([,.;!?])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.;")
    return cleaned, any(word in ("male", "man", "guy", "boy") for word in rejected), any(
        word in ("female", "woman", "girl") for word in rejected
    )


def vocal_intent(prompt: str, instrumental: bool = False) -> str:
    """Use the person's current request before any appended learned examples."""
    current_request = prompt.split("Past user feedback", 1)[0]
    if (instrumental or VOCAL_FREE.search(current_request)
            or WHOLE_INSTRUMENTAL.search(current_request)):
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
    instrumental = instrumental or intent == "none"
    cleaned, _, _ = _clean_negations(prompt)
    cues = []
    sustained = requires_sustained_vocals(cleaned, instrumental=instrumental)
    # Chops can accompany a requested lead; they do not cancel that lead.
    # Inspect only the cleaned current request, not exclusions or old feedback.
    current = cleaned.split("Past user feedback", 1)[0]
    sparse = sparse_vocal_style(current) and not re.search(r"\blead\s+vocals?\b", current, re.I)
    if sustained and intent == "female":
        cue = ("Distinct short female vocal hooks and chopped phrases between instrumental drops"
               if sparse else
               "Prominent expressive female lead vocals throughout, clear sung verses and chorus")
        if any(name in cleaned.lower() for name in ("katy perry", "katy pary")):
            cue += ", bright polished dance-pop production and a catchy anthemic hook"
        cues.append(cue)
    elif sustained and intent == "male":
        cues.append("Distinct short male vocal hooks and chopped phrases between instrumental drops"
                    if sparse else
                    "Prominent expressive male lead vocals throughout, clear sung verses and chorus")
    if not instrumental and not sustained:
        cues.append("Sparse chopped vocal syllables as rhythmic accents between instrumental sections")
    request = cleaned.split("Past user feedback", 1)[0].lower()
    if (re.search(r"\b(?:techno|tehno|electronic dance|dubstep|riddim|brostep|tearout|edm)\b", request)
            and not re.search(r"\b(?:guitars?|acoustic|live band|rock)\b", request)):
        if (re.search(r"\b(?:dubstep|riddim|brostep|tearout)\b", request)
                and not re.search(r"\b(?:four.on.the.floor|four.on.floor|4/4 kick)\b", request)):
            cues.append(
                "Entirely electronic production with halftime snare-led drums, "
                "syncopated wobble bass, sequenced synthesizers and sharp bass drops"
            )
        else:
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
