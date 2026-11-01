"""Estimate useful musical features from an existing recording, entirely locally.

These are listening aids for the agent, not authoritative music notation. A short
excerpt or a drum-heavy mix can make key and chord estimates uncertain.
"""

from __future__ import annotations

from pathlib import Path

SUPPORTED = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}
NOTES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MAJOR = (0, 4, 7)
MINOR = (0, 3, 7)
MAX_SECONDS = 180
KEY_PROFILES = {
    "major": (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88),
    "minor": (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17),
}


def _best_chord(chroma):
    """Return the best major/minor triad, or None for weak pitch evidence."""
    import numpy as np

    strength = np.asarray(chroma, dtype=float)
    if not np.isfinite(strength).all() or strength.sum() < 0.01:
        return None
    scores = []
    for root in range(12):
        for quality, intervals in (("major", MAJOR), ("minor", MINOR)):
            selected = sum(strength[(root + interval) % 12] for interval in intervals)
            scores.append((float(selected / strength.sum()), f"{NOTES[root]} {quality}"))
    score, name = max(scores)
    return name if score >= 0.35 else None


def _best_key(chroma):
    """Correlate pitch classes with standard major/minor key profiles."""
    import numpy as np

    strength = np.asarray(chroma, dtype=float)
    if not np.isfinite(strength).all() or strength.sum() < 0.01:
        return None
    choices = []
    for quality, profile in KEY_PROFILES.items():
        for root in range(12):
            similarity = np.corrcoef(strength, np.roll(profile, root))[0, 1]
            choices.append((float(similarity), f"{NOTES[root]} {quality}"))
    choices.sort(reverse=True)
    score, name = choices[0]
    return name if score >= 0.3 and score - choices[1][0] >= 0.08 else None


def analyze_music(source_path: str) -> dict:
    """Measure tempo and approximate harmony in the first three minutes."""
    import librosa
    import numpy as np

    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in SUPPORTED:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG audio file.")
    duration = float(librosa.get_duration(path=str(source)))
    if duration <= 0:
        raise ValueError("The audio file has no playable duration.")
    signal, sample_rate = librosa.load(str(source), sr=22050, mono=True, duration=MAX_SECONDS)
    if len(signal) == 0 or float(np.max(np.abs(signal))) < 0.001:
        raise ValueError("The audio is silent or too quiet to analyze.")

    # Harmonic separation reduces drum energy in the chord estimate.
    harmonic = librosa.effects.harmonic(signal)
    chroma = librosa.feature.chroma_stft(y=harmonic, sr=sample_rate, hop_length=512)
    global_chroma = np.mean(chroma, axis=1)
    tempo, beat_frames = librosa.beat.beat_track(y=signal, sr=sample_rate, hop_length=512)
    bpm = float(np.asarray(tempo).reshape(-1)[0]) if len(beat_frames) >= 2 else None

    segment_seconds = 4
    frames_per_segment = round(segment_seconds * sample_rate / 512)
    changes = []
    for start in range(0, chroma.shape[1], frames_per_segment):
        name = _best_chord(np.mean(chroma[:, start:start + frames_per_segment], axis=1))
        if name and (not changes or changes[-1]["chord"] != name):
            changes.append({"at_seconds": round(start * 512 / sample_rate, 1), "chord": name})

    return {
        "source": str(source),
        "duration_seconds": round(duration, 2),
        "analyzed_seconds": round(len(signal) / sample_rate, 2),
        "tempo_bpm": round(bpm, 1) if bpm else None,
        # A single repeated chord does not establish a song's key.
        "estimated_key": _best_key(global_chroma) if len({item["chord"] for item in changes}) >= 3 else None,
        "dominant_chord": _best_chord(global_chroma),
        "chord_changes": changes,
        "peak_dbfs": round(20 * float(np.log10(max(np.max(np.abs(signal)), 1e-9))), 1),
        "note": "Tempo may be half/double time; chords are approximate and are not a full transcription.",
    }
