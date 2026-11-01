"""Make a captioned visualizer from a completed local song.

Vocal captions prefer the words and timings actually heard by the offline
transcriber. Authored lyrics are a clearly marked fallback when those timings
are unavailable; instrumentals show a title without inventing sung words.
"""

from __future__ import annotations

import re
from pathlib import Path

import music_video


def _stamp(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, fraction = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{fraction:03}"


def caption_rows(segments: list[dict], lyrics: str, title: str,
                 instrumental: bool, duration: int) -> tuple[list[tuple[float, float, str]], str]:
    """Keep recognition timings; never label guessed timing as transcription."""
    if not instrumental:
        timed = []
        for segment in segments:
            try:
                start, end = float(segment["start"]), float(segment["end"])
            except (KeyError, TypeError, ValueError):
                continue
            words = " ".join(str(segment.get("text", "")).split())
            if words and 0 <= start < end:
                timed.append((start, end, words))
        if timed:
            return timed, "offline_transcription"
        lines = [" ".join(line.split()) for line in lyrics.splitlines()
                 if line.strip() and not re.fullmatch(r"\s*\[[^]]+\]\s*", line)]
        if lines:
            seconds = max(1.0, float(duration) / len(lines))
            return [(index * seconds, (index + 1) * seconds, line)
                    for index, line in enumerate(lines)], "estimated_from_authored_lyrics"
    caption = "Instrumental — " + title if instrumental else title
    return [(0.0, min(max(float(duration), 1.0), 7.0), caption[:100])], "title_only"


def render(source_path: str, *, segments: list[dict] | None = None,
           lyrics: str = "", title: str = "Song", instrumental: bool = False,
           duration: int = 30) -> dict:
    """Save editable SRT and a new MP4 without changing the source audio."""
    source = Path(source_path).resolve(strict=True)
    captions = source.with_name(source.stem + ".captions.srt")
    video = source.with_name(source.stem + "-video.mp4")
    if video.is_file() and video.stat().st_size > 1000 and captions.is_file():
        return {"path": str(video), "captions_path": str(captions),
                "caption_source": "existing"}
    rows, caption_source = caption_rows(
        segments or [], lyrics, title, instrumental, duration
    )
    if not captions.exists():
        captions.write_text("\n\n".join(
            f"{index}\n{_stamp(start)} --> {_stamp(end)}\n{text}"
            for index, (start, end, text) in enumerate(rows, 1)
        ) + "\n", encoding="utf-8")
    if video.exists():
        # Keep an existing video, even if it looks incomplete. Another render
        # or a user may own it; never overwrite that file on recovery.
        suffix = 2
        while video.exists():
            video = source.with_name(f"{source.stem}-video-{suffix}.mp4")
            suffix += 1
    result = music_video.make_music_video(
        str(source), str(video), captions_path=str(captions)
    )
    return {**result, "caption_source": caption_source}
