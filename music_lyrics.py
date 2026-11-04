"""Save editable lyrics and timed captions from an offline vocal transcription.

Stem separation and speech recognition live in existing local runtimes. This
module validates paths and turns their result into files useful for editing.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}


def prepare(source_path: str, output_dir: str) -> tuple[Path, Path]:
    """Require a fresh destination before downloading models or making stems."""
    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG song.")
    target = Path(output_dir).expanduser().resolve()
    if target == source.parent:
        raise ValueError("Choose a separate output folder for lyrics and stems.")
    if target.exists() and any(target.iterdir()):
        raise ValueError("Choose an empty output folder to protect existing files.")
    return source, target


def vocal_file(files: list[str]) -> Path:
    """Select the isolated vocal track, never the instrumental by accident."""
    vocals = [Path(name) for name in files if "vocal" in Path(name).stem.lower()
              and "no vocal" not in Path(name).stem.lower()
              and "instrument" not in Path(name).stem.lower()]
    if len(vocals) != 1 or not vocals[0].is_file():
        raise RuntimeError("Could not identify one isolated vocal track for lyric transcription.")
    return vocals[0]


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def save(source: Path, vocal: Path, target: Path, transcription: dict) -> dict:
    """Write plain text, SRT captions and raw timings without changing audio."""
    rows = []
    for segment in transcription.get("segments", []):
        words = " ".join(str(segment.get("text", "")).split())
        try:
            start = float(segment.get("start", 0))
            end = float(segment.get("end", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if (words and math.isfinite(start) and math.isfinite(end)
                and math.isfinite(start * 1000) and math.isfinite(end * 1000)
                and end > start >= 0
                and round(end * 1000) > round(start * 1000)):
            rows.append({"start": start, "end": end, "text": words})
    if not rows:
        raise RuntimeError("No clear lyrics were detected in the isolated vocal track.")
    target.mkdir(parents=True, exist_ok=True)
    text = target / "lyrics.txt"
    captions = target / "lyrics.srt"
    timings = target / "lyrics.json"
    for path in (text, captions, timings):
        if path.exists():
            raise ValueError(f"Output already exists: {path}")
    text.write_text("\n".join(row["text"] for row in rows) + "\n", encoding="utf-8")
    captions.write_text("\n\n".join(
        f"{index}\n{_srt_time(row['start'])} --> {_srt_time(row['end'])}\n{row['text']}"
        for index, row in enumerate(rows, 1)
    ) + "\n", encoding="utf-8")
    timings.write_text(json.dumps({"language": transcription.get("language"),
                                   "segments": rows}, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    return {"source": str(source), "vocal_path": str(vocal),
            "lyrics_path": str(text), "captions_path": str(captions),
            "timings_path": str(timings), "language": transcription.get("language"),
            "segments": len(rows),
            "note": "Offline speech recognition can mishear singing; review lyrics before publishing."}
