"""Make a captioned visualizer from a completed local song.

Vocal captions prefer the words and timings actually heard by the offline
transcriber. Authored lyrics are a clearly marked fallback when those timings
are unavailable; instrumentals show a title without inventing sung words.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

import music_video


def _digest(path: Path) -> str:
    """Track source bytes, including edits that preserve timestamps."""
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _render_record(video: Path) -> Path:
    return video.with_name(video.name + ".cale.json")


def _saved_record(video: Path) -> dict | None:
    record = _render_record(video)
    if not record.is_file():
        return None
    try:
        value = json.loads(record.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _unique_caption_path(source: Path) -> Path:
    number = 2
    while True:
        candidate = source.with_name(f"{source.stem}.captions-{number}.srt")
        if not candidate.exists():
            return candidate
        number += 1


def _write_render_record(video: Path, source_hash: str, captions: Path) -> None:
    record = _render_record(video)
    temporary = record.with_name(record.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"source_sha256": source_hash,
                                     "captions_sha256": _digest(captions),
                                     "captions_path": str(captions)}), encoding="utf-8")
    os.replace(temporary, record)


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
    source_hash = _digest(source)
    captions = source.with_name(source.stem + ".captions.srt")
    video = source.with_name(source.stem + "-video.mp4")
    source_changed = False
    matching_source = False
    variants = [video, *sorted(
        path for path in source.parent.iterdir()
        if path.name.startswith(source.stem + "-video-")
        and path.suffix.lower() == ".mp4"
    )]
    for candidate in variants:
        record = _saved_record(candidate)
        if not record or record.get("source_sha256") != source_hash:
            continue
        remembered = record.get("captions_path")
        candidate_captions = Path(remembered) if isinstance(remembered, str) else captions
        if (candidate_captions.parent != source.parent
                or candidate_captions.suffix.lower() != ".srt"
                or not candidate_captions.is_file()):
            continue
        matching_source = True
        captions = candidate_captions
        if (record.get("captions_sha256") == _digest(captions)
                and _video_playable(candidate)):
            return {"path": str(candidate), "captions_path": str(captions),
                    "caption_source": "existing"}
    if not matching_source and video.is_file() and captions.is_file():
        record = _saved_record(video)
        if record is None:
            # Releases before render records can only use file times. New
            # renders below use hashes, so timestamp-preserved edits work too.
            source_changed = source.stat().st_mtime_ns > video.stat().st_mtime_ns
            captions_changed = captions.stat().st_mtime_ns > video.stat().st_mtime_ns
        else:
            source_changed = record.get("source_sha256") != source_hash
            captions_changed = record.get("captions_sha256") != _digest(captions)
        if not source_changed and not captions_changed and _video_playable(video):
            return {"path": str(video), "captions_path": str(captions),
                    "caption_source": "existing"}
    rows, caption_source = caption_rows(
        segments or [], lyrics, title, instrumental, duration
    )
    caption_text = "\n\n".join(
        f"{index}\n{_stamp(start)} --> {_stamp(end)}\n{text}"
        for index, (start, end, text) in enumerate(rows, 1)
    ) + "\n"
    if source_changed:
        # A fresh song needs fresh captions. Preserve the old editable SRT
        # alongside its old video. A failed video retry reuses the newly
        # generated SRT instead of making another identical caption file.
        captions = None
        for existing in sorted(
            path for path in source.parent.iterdir()
            if path.name.startswith(source.stem + ".captions-")
            and path.suffix.lower() == ".srt"
        ):
            try:
                if existing.read_text(encoding="utf-8") == caption_text:
                    captions = existing
                    break
            except OSError:
                continue
        if captions is None:
            captions = _unique_caption_path(source)
    if not captions.exists():
        captions.write_text(caption_text, encoding="utf-8")
    else:
        caption_source = "existing"
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
    if _digest(source) != source_hash:
        raise RuntimeError("The song changed while its video was being made; retry with the new audio.")
    _write_render_record(video, source_hash, captions)
    return {**result, "caption_source": caption_source}


def _video_playable(path: Path) -> bool:
    """Reject a partial MP4 left behind if an FFmpeg worker was interrupted."""
    if path.stat().st_size <= 1000:
        return False
    try:
        probe = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel",
             "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0",
             "-t", "0.25", "-f", "null", "-"],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
