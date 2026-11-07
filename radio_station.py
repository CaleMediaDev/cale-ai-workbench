"""Prompt stations, a small durable local queue, and the homepage radio.

Music uses the existing durable ACE-Step worker. One song renders at a time;
playback prepares two songs ahead and repeats the last good song on underrun.
Only this module's UUID-named cache files are eligible for automatic cleanup.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECONDS = 180
FADE_SECONDS = 8
PRELOAD_SECONDS = 15
LEGACY_CLUB = "Instrumental melodic techno, electronic drums and rich synthesizers, no guitars, energetic night-drive atmosphere"
BRAND_LYRICS = "Original lyrics about Cale Media, local SEO and marketing, helping local businesses get found. Sing the name Cale Media in a memorable chorus. Clear lead vocals through verses and choruses."
DEFAULT_STATIONS = (
    ("Cale Club", "Melodic techno club anthem, 128 BPM, electronic drums, rich synthesizers and powerful bass, no guitars. " + BRAND_LYRICS, False),
    ("Cale Country", "Upbeat modern country song, warm lead singer, acoustic guitar, steel guitar, live drums, big singalong chorus. " + BRAND_LYRICS, False),
    ("Cale Rap", "Heavy-bass hip hop, 94 BPM, confident rapped verses, tight drums and a catchy sung chorus. " + BRAND_LYRICS, False),
    ("Cale Pop", "Bright dance pop, punchy electronic drums, sparkling synths, expressive lead singer and a huge catchy chorus. " + BRAND_LYRICS, False),
    ("Cale Rock", "Energetic anthemic rock, driving electric guitars, live drums, strong sung lead vocals and a big chorus. " + BRAND_LYRICS, False),
    ("Chill", "Instrumental ambient downtempo, warm synthesizers and soft electronic beats, relaxing focused atmosphere", True),
)


def completed_songs(root=ROOT):
    """Expose completed maker audio to the same decks; never copy or re-render it."""
    folder=Path(root)/"LongTasks/music-jobs"
    if not folder.is_dir():return []
    songs=[];seen=set()
    receipts=[]
    for path in folder.glob("*.json"):
        try:receipts.append((path.stat().st_mtime,path))
        except OSError:continue
    recent=[path for _,path in sorted(receipts,reverse=True)[:128]]
    for receipt in recent:
        try:
            if receipt.stat().st_size>256000:continue
            value=json.loads(receipt.read_text(encoding="utf-8"))
            if not isinstance(value,dict) or value.get("status")!="complete" or str(value.get("task_id","")).startswith("radio-"):continue
            path=Path(value["output_path"]).resolve()
            if path in seen or not path.is_file() or path.suffix.lower() not in {".wav",".mp3",".flac",".m4a",".ogg"}:continue
            seen.add(path)
            title=" ".join(str(value.get("prompt") or path.stem).split())[:70]
            songs.append({"id":receipt.stem,"path":str(path),"title":title})
            if len(songs)>=12:break
        except (OSError,ValueError,KeyError,TypeError):continue
    return songs


def database(root=ROOT):
    path = Path(root) / "LongTasks/radio.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS stations(id TEXT PRIMARY KEY,name TEXT,prompt TEXT,instrumental INTEGER)")
    conn.execute("""CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY,station TEXT,job TEXT,
        path TEXT,status TEXT,created REAL,finished REAL DEFAULT 0,render_seconds REAL DEFAULT 0,config TEXT DEFAULT '',completed INTEGER NOT NULL DEFAULT 0)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    if "config" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN config TEXT DEFAULT ''")
        conn.commit()
    if "completed" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE tracks SET completed=1 WHERE status IN ('ready','playing','played') OR finished>0")
        # Older players erased finish timestamps on playback/pruning. Recover
        # only successes proved by their exact durable music-job receipt.
        for row in conn.execute("SELECT id,job FROM tracks WHERE completed=0 AND status='pruned'").fetchall():
            job = row["job"]
            if not isinstance(job, str) or len(job) != 32 or any(c not in "0123456789abcdef" for c in job):
                continue
            try:
                receipt = json.loads((Path(root)/"LongTasks/music-jobs"/(job+".json")).read_text(encoding="utf-8"))
                if isinstance(receipt, dict) and receipt.get("status") == "complete":
                    conn.execute("UPDATE tracks SET completed=1 WHERE id=?", (row["id"],))
            except (OSError, ValueError):
                pass
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS radio_live_tracks ON tracks(created) WHERE status!='pruned'")
    conn.execute("CREATE INDEX IF NOT EXISTS radio_queue ON tracks(station,config,status)")
    return conn


def stations(root=ROOT):
    with closing(database(root)) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM stations ORDER BY rowid")]


def _station_record(name, prompt, instrumental=True, station_id=None):
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise ValueError("Give your station a name, up to 60 characters.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 3000:
        raise ValueError("Describe your station's music, up to 3000 characters.")
    if type(instrumental) is not bool:
        raise ValueError("Choose instrumental or vocal music.")
    identity = station_id or uuid.uuid4().hex
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Invalid station ID")
    return identity, name.strip(), prompt.strip(), int(instrumental)


def save_station(name, prompt, instrumental=True, *, root=ROOT, station_id=None):
    values = _station_record(name, prompt, instrumental, station_id)
    with closing(database(root)) as conn, conn:
        conn.execute("INSERT INTO stations VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,prompt=excluded.prompt,instrumental=excluded.instrumental", values)
    return values[0]


def ensure_stations(root=ROOT):
    """Add built-ins and migrate only the exact old instrumental Club preset."""
    existing = stations(root)
    club = DEFAULT_STATIONS[0]
    for row in existing:
        if row["name"] == club[0] and row["prompt"] == LEGACY_CLUB and row["instrumental"] == 1:
            save_station(*club, root=root, station_id=row["id"])
    names = {row["name"].casefold() for row in existing}
    for name, prompt, instrumental in DEFAULT_STATIONS:
        if name.casefold() not in names:
            save_station(name, prompt, instrumental, root=root)


def local_token_totals(root=ROOT):
    """Read reported text-token usage; audio has no fabricated token count."""
    path = Path(root) / "LongTasks/usage.db"
    if not path.is_file():
        return None, None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=0.25)) as conn:
            row = conn.execute("""SELECT COALESCE(SUM(total_tokens),0),
                COALESCE(SUM(CASE WHEN task_id LIKE 'radio-%' THEN total_tokens ELSE 0 END),0)
                FROM usage_events WHERE source='ollama'""").fetchone()
            return tuple(row)
    except sqlite3.Error:
        return None, None


def foreground_music_busy():
    """Do not put another radio render ahead of a live user song task."""
    import music_backend
    for path in music_backend.JOBS.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(job, dict):
                continue
            if (not str(job.get("task_id", "")).startswith("radio-")
                    and job.get("status") in ("queued", "working")
                    and music_backend._worker_running(path.stem, job)):
                return True
        except (OSError, ValueError, KeyError):
            continue
    return False


class AudioDeck:
    """One pre-loadable Windows audio channel, owned by the radio worker."""
    def __init__(self):
        self.alias = "cale_radio_" + uuid.uuid4().hex
        self.opened = False
        self.path = None
        self.volume = 65

    def command(self, text):
        if os.name != "nt":
            raise RuntimeError("Radio playback needs Windows audio.")
        buffer = ctypes.create_unicode_buffer(512)
        error = ctypes.windll.winmm.mciSendStringW(text, buffer, len(buffer), None)
        if error:
            raise RuntimeError("Windows could not play this radio track.")
        return buffer.value

    def open(self, path):
        self.stop()
        self.command(f'open "{Path(path).resolve()}" type mpegvideo alias {self.alias}')
        self.opened = True
        self.path = str(Path(path).resolve())
        try:
            self.set_volume(self.volume)
            self.command(f"set {self.alias} time format milliseconds")
        except Exception:
            self.stop()
            raise

    def start(self):
        self.command(f"play {self.alias}")

    def play(self, path):
        self.open(path)
        self.start()

    def busy(self):
        return self.opened and self.command(f"status {self.alias} mode") in ("playing", "paused")

    def clock(self):
        if not self.opened:
            return 0, 0
        return (int(self.command(f"status {self.alias} position"))/1000,
                int(self.command(f"status {self.alias} length"))/1000)

    def pause(self):
        if self.opened and self.command(f"status {self.alias} mode") == "playing":
            self.command(f"pause {self.alias}")

    def resume(self):
        if self.opened:
            mode = self.command(f"status {self.alias} mode")
            if mode == "paused":
                self.command(f"resume {self.alias}")
            elif mode == "stopped":
                self.command(f"play {self.alias}")

    def seek(self, seconds):
        if not self.opened:
            return
        from music_edit import _finite

        position = _finite(seconds, "Cue position", 0, self.clock()[1])
        playing = self.command(f"status {self.alias} mode") == "playing"
        self.command(f"seek {self.alias} to {round(position*1000)}")
        if playing:
            self.command(f"play {self.alias}")

    def set_volume(self, value):
        self.volume = max(0, min(100, float(value)))
        if self.opened:
            self.command(f"setaudio {self.alias} volume to {round(self.volume * 10)}")

    def stop(self):
        if self.opened:
            try:
                self.command(f"close {self.alias}")
            finally:
                self.opened = False
                self.path = None


class Player:
    """Automatic radio crossfades or four manual decks, owned by the audio thread."""
    def __init__(self, deck_factory=AudioDeck, clock=time.monotonic):
        self.factory, self.now = deck_factory, clock
        self.active = deck_factory()
        self.active.channel = 0
        self.deck_levels = [100.0] * 4
        self.manual_fraction = None
        self.outgoing = self.prepared = None
        self.volume = 65
        self.paused_at = None
        self.fade_start = self.fade_seconds = 0
        self.dj_settings = [{"tone": (0.,0.,0.), "speed": 1., "trim": 0., "filter": 0.,
                             "effect": (0., 0., 0.), "effect_depth": 0.} for _ in range(4)]
        self.loops = {}
        self.studio_mode = False
        self.studio_decks = {}
        self.studio_paused = set()
        self.studio_started = set()
        self.studio_positions = {}
        self.channel_sides = [-1, 1, -1, 1]
        self.channel_muted = set()
        self.cue_points = [0.] * 4
        self.hot_cues = [[None] * 8 for _ in range(4)]
        self.loop_lengths = [4.] * 4
        self.loop_starts = {}
        self.track_bpms = {}
        self.track_beat_offsets = {}
        self._global_pause_channels = None
        self.samples = {}
        self.sample_volume = 80.
        self.rolls = {}
        self.studio_fade = None

    @property
    def mixing(self):
        return self.outgoing is not None or self.studio_fade is not None

    @property
    def alias(self):
        return self.active.alias

    def command(self, text):
        return self.active.command(text)

    def play(self, path):
        self.stop()
        self.active.set_volume(self.volume * self.deck_levels[self.active.channel] / 100)
        self._apply_dj(self.active)
        self.active.play(path)

    def prepare(self, path):
        path = str(Path(path).resolve())
        if self.prepared is not None and self.prepared.path == path:
            return
        if self.prepared is not None:
            previous = self.prepared
            self.prepared = None
            previous.stop()
        self.prepared = self.factory()
        try:
            self.prepared.channel = 1 - self.active.channel
            self.prepared.set_volume(0)
            self._apply_dj(self.prepared)
            self.prepared.open(path)
        except Exception:
            try:
                self.prepared.stop()
            finally:
                self.prepared = None
            raise

    def blend(self, path, seconds=FADE_SECONDS):
        if self.mixing or self.paused_at is not None:
            return False
        # Validate before starting audio or transferring ownership of the decks.
        fade_seconds = max(0.1, float(seconds))
        self.prepare(path)
        # Keep the outgoing channel intact if the new channel cannot start.
        try:
            self.prepared.start()
        except Exception:
            try:
                self.prepared.stop()
            finally:
                self.prepared = None
            raise
        self.outgoing, self.active = self.active, self.prepared
        self.prepared = None
        self.fade_start = self.now()
        self.fade_seconds = fade_seconds
        try:
            self.advance_mix()
        except Exception:
            # Starting playback is not enough: initial gain setup can fail too.
            # Restore the original channel before reporting a failed transition.
            failed = self.active
            self.active = self.outgoing
            self.outgoing = None
            try:
                failed.stop()
            finally:
                self.active.set_volume(self.volume*self.deck_levels[self.active.channel]/100)
            raise
        return True

    def enable_dj(self, root):
        from music_edit import DJDeck
        replacement=DJDeck(root)
        if not self.active.path:
            channel=self.active.channel
            self.active.stop();self.active=replacement;self.active.channel=channel
            self._apply_dj(self.active)
        self.factory=lambda:DJDeck(root)

    def _apply_dj(self, deck):
        settings=self.dj_settings[deck.channel]
        if hasattr(deck,"set_tone"):
            deck.set_tone(*settings["tone"]);deck.set_speed(settings["speed"])
        if hasattr(deck, "set_trim"):
            deck.set_trim(settings["trim"])
            deck.set_filter(settings["filter"])
        if hasattr(deck, "set_effect"):
            deck.set_effect(settings["effect"], settings["effect_depth"])

    def loaded_decks(self):
        if self.studio_mode:
            return list(self.studio_decks.values())
        return [deck for deck in (self.active, self.outgoing, self.prepared) if deck is not None]

    def enter_studio(self):
        """Keep audible radio decks alive when the user starts a manual mix."""
        if self.studio_mode:
            return
        position = self.crossfade_position
        for deck in self.loaded_decks():
            if not deck.path:
                continue
            if deck.channel in self.studio_decks:
                deck.stop()
                continue
            self.studio_decks[deck.channel] = deck
            if deck is self.prepared:
                self.studio_paused.add(deck.channel)
            else:
                self.studio_started.add(deck.channel)
                if self.paused_at is not None:
                    self.studio_paused.add(deck.channel)
        self.outgoing = self.prepared = None
        self.paused_at = None
        self.manual_fraction = position
        self.studio_mode = True

    def load_deck(self, channel, path):
        from music_edit import _source

        if channel not in range(4):
            raise ValueError("Choose deck 1, 2, 3 or 4.")
        path = _source(str(path))
        replacement = self.factory()
        replacement.channel = channel
        try:
            self._apply_dj(replacement)
            replacement.open(path)
        except Exception:
            replacement.stop()
            raise
        self.enter_studio()
        self.studio_fade = None
        previous = self.studio_decks.get(channel)
        self.studio_decks[channel] = replacement
        self.studio_paused.add(channel)
        self.studio_started.discard(channel)
        self.studio_positions[channel] = 0.
        if self._global_pause_channels is not None:
            self._global_pause_channels.discard(channel)
        self.cue_points[channel] = 0.
        self.hot_cues[channel] = [None] * 8
        self.loops.pop(channel, None)
        self.loop_starts.pop(channel, None)
        self.rolls.pop(channel, None)
        if previous is not None:
            previous.stop()
        self._studio_gains()

    def mix_studio_song(self, path, seconds=FADE_SECONDS):
        """Blend a finished song into a free deck without stopping a manual mix."""
        if self.mixing:
            raise ValueError("Wait for the current blend to finish.")
        available = [channel for channel in range(4) if channel not in self.studio_decks
                     or channel in self.studio_paused or not self.studio_decks[channel].busy()]
        if not available:
            raise ValueError("All four decks are playing. Pause or eject one before loading another song.")
        outgoing = [(channel, deck.volume) for channel, deck in self.studio_decks.items()
                    if channel in self.studio_started and channel not in self.studio_paused and deck.busy()]
        channel = available[0]
        fade_seconds = max(.1, float(seconds))
        self.load_deck(channel, path)
        audible = {c: self.deck_levels[c] for c, gain in outgoing if gain > 0}
        self.deck_levels[channel] = 0. if audible else 100.
        self.channel_sides[channel] = 0
        self.channel_muted.discard(channel)
        self._studio_gains()  # Silence the incoming deck before native playback starts.
        self.toggle_deck(channel)
        if audible:
            self.studio_fade = {"incoming": channel, "outgoing": audible,
                                "started": self.now(), "seconds": fade_seconds}
            self.advance_mix()
        return channel

    def _studio_gains(self):
        position = self.manual_fraction if self.manual_fraction is not None else .5
        for channel, deck in self.studio_decks.items():
            side = self.channel_sides[channel]
            gain = (math.cos(position*math.pi/2) if side < 0 else
                    math.sin(position*math.pi/2) if side > 0 else 1.)
            if channel in self.channel_muted:
                gain = 0.
            deck.set_volume(self.volume*self.deck_levels[channel]/100*gain)
        for (channel, _), deck in self.samples.items():
            side = self.channel_sides[channel]
            gain = (math.cos(position*math.pi/2) if side < 0 else
                    math.sin(position*math.pi/2) if side > 0 else 1.)
            deck.set_volume(0 if channel in self.channel_muted else
                            self.volume*self.deck_levels[channel]/100*gain*self.sample_volume/100)

    def play_sample(self, channel, slot, path):
        self.enter_studio()
        previous = self.samples.pop((channel, slot), None)
        if previous is not None:
            previous.stop()
        deck = self.factory()
        deck.channel = channel
        self._apply_dj(deck)
        try:
            deck.open(path)
            self.samples[channel, slot] = deck
            self._studio_gains()
            deck.start()
        except Exception:
            self.samples.pop((channel, slot), None)
            deck.stop()
            raise

    def _reap_samples(self):
        for identity, deck in list(self.samples.items()):
            if not deck.busy():
                try:
                    deck.stop()
                finally:
                    self.samples.pop(identity, None)

    def seek_deck(self, channel, position):
        deck = next((deck for deck in self.loaded_decks() if deck.channel == channel), None)
        if deck is None or not hasattr(deck, "seek"):
            return
        duration = deck.clock()[1]
        position = max(0., min(max(0., duration-.01), float(position)))
        if self.studio_mode and channel not in self.studio_started:
            self.studio_positions[channel] = position
        else:
            deck.seek(position)

    def deck_clock(self, deck):
        elapsed, duration = deck.clock()
        if self.studio_mode and deck.channel not in self.studio_started:
            elapsed = self.studio_positions.get(deck.channel, 0.)
        return elapsed, duration

    def toggle_deck(self, channel):
        self.enter_studio()
        deck = self.studio_decks.get(channel)
        if deck is None:
            raise ValueError("Load a song into this deck first.")
        if channel in self.studio_started and not deck.busy():
            path = deck.path
            deck.open(path)
            self.studio_started.discard(channel)
            self.studio_positions[channel] = 0.
        if channel not in self.studio_started:
            deck.start()
            self.studio_started.add(channel)
            deck.seek(self.studio_positions.get(channel, 0.))
            self.studio_paused.discard(channel)
        elif channel in self.studio_paused:
            deck.resume()
            self.studio_paused.remove(channel)
        else:
            deck.pause()
            self.studio_paused.add(channel)
        if self.paused_at is not None and channel not in self.studio_paused:
            self._global_pause_channels = None
            self.paused_at = None
            self.studio_fade = None
        self._studio_gains()

    def performance_control(self, channel, action, value):
        """Transport and pad actions operate on the same four audible voices."""
        from music_edit import _finite

        self.enter_studio()
        deck = self.studio_decks.get(channel)
        if deck is None:
            raise ValueError("Load a song into this deck first.")
        position, duration = self.deck_clock(deck)
        bpm = self.track_bpms.get(deck.path, 120.)
        beat_seconds = 60/bpm
        if action == "cue_set":
            self.cue_points[channel] = position
        elif action == "hotcue":
            if not isinstance(value, dict) or type(value.get("slot")) is not int or value["slot"] not in range(8):
                raise ValueError("Choose a hot cue from 1 to 8.")
            slot = value["slot"]
            if value.get("clear"):
                self.hot_cues[channel][slot] = None
            elif self.hot_cues[channel][slot] is None:
                self.hot_cues[channel][slot] = position
            else:
                self.seek_deck(channel, self.hot_cues[channel][slot])
                if channel in self.studio_paused:
                    self.toggle_deck(channel)
        elif action == "beat_jump":
            self.seek_deck(channel, position + _finite(value, "Beat jump", -128, 128)*beat_seconds)
        elif action == "loop_length":
            beats = _finite(value, "Loop beats", .25, 32)
            self.loop_lengths[channel] = beats
            if channel in self.loops:
                path, start, _ = self.loops[channel]
                self.loops[channel] = (path, start, min(duration-.01, start+beats*beat_seconds))
        elif action == "loop_in":
            self.loop_starts[channel] = position
        elif action == "loop_out":
            start = self.loop_starts.get(channel)
            if start is None or position-start < .1:
                raise ValueError("Set loop IN, then move forward before setting OUT.")
            self.loops[channel] = (deck.path, start, position)
        elif action == "sync":
            other = max((candidate for candidate in self.studio_decks.values()
                         if candidate.channel != channel and candidate.channel in self.studio_started
                         and candidate.channel not in self.studio_paused and candidate.path in self.track_bpms
                         and candidate.busy()), key=lambda candidate: candidate.volume, default=None)
            if other is None or deck.path not in self.track_bpms:
                raise ValueError("Sync is ready when both songs have an analyzed BPM and the other deck is playing.")
            target_bpm = self.track_bpms[other.path] * self.dj_settings[other.channel]["speed"]
            speed = _finite(target_bpm/bpm, "Synced playback speed", .8, 1.2)
            self.dj_control(channel, "speed", speed)
            other_offset = self.track_beat_offsets.get(other.path, 0.)
            own_offset = self.track_beat_offsets.get(deck.path, 0.)
            phase = (self.deck_clock(other)[0]-other_offset)*self.track_bpms[other.path]/60 % 1
            self.seek_deck(channel, own_offset+(math.floor((position-own_offset)/beat_seconds)+phase)*beat_seconds)
        elif action == "bpm_scale":
            if value not in (.5, 2) or deck.path not in self.track_bpms:
                raise ValueError("Wait for the analyzed BPM before adjusting it.")
            self.track_bpms[deck.path] = _finite(self.track_bpms[deck.path]*value, "BPM", 30, 360)
        elif action == "roll_on":
            if channel in self.rolls:
                return
            beats = _finite(value, "Roll beats", .25, 32)
            self.rolls[channel] = {"path": deck.path, "position": position, "started": self.now(),
                                  "rate": self.dj_settings[channel]["speed"],
                                  "playing": channel in self.studio_started and channel not in self.studio_paused,
                                  "loop": self.loops.get(channel)}
            self.loops[channel] = (deck.path, position, min(duration-.01, position+beats*beat_seconds))
            if channel in self.studio_paused:
                self.toggle_deck(channel)
        elif action == "roll_off":
            roll = self.rolls.pop(channel, None)
            if roll is not None and roll["path"] == deck.path:
                self.loops.pop(channel, None)
                if roll["loop"] is not None:
                    self.loops[channel] = roll["loop"]
                if roll["playing"]:
                    self.seek_deck(channel, roll["position"]+(self.now()-roll["started"])*roll["rate"])
                else:
                    if channel not in self.studio_paused:
                        self.toggle_deck(channel)
                    self.seek_deck(channel, roll["position"])
        elif action == "eject":
            self.studio_fade = None
            self.loops.pop(channel, None)
            self.studio_paused.discard(channel)
            self.studio_started.discard(channel)
            self.studio_positions.pop(channel, None)
            if self._global_pause_channels is not None:
                self._global_pause_channels.discard(channel)
            self.rolls.pop(channel, None)
            self.studio_decks.pop(channel)
            deck.stop()
        else:
            raise ValueError("Unknown performance control.")

    def dj_control(self, channel, action, value=None):
        if channel not in range(4):raise ValueError("Choose deck 1, 2, 3 or 4.")
        from music_edit import _finite
        decks=[d for d in self.loaded_decks() if d.channel==channel]
        if action=="tone":
            if not isinstance(value,(tuple,list)) or len(value)!=3:raise ValueError("Choose bass, mid and high settings.")
            self.dj_settings[channel]["tone"]=tuple(_finite(v,"Equalizer",-12,12) for v in value)
            for deck in decks:
                if hasattr(deck,"set_tone"):deck.set_tone(*self.dj_settings[channel]["tone"])
        elif action=="speed":
            self.dj_settings[channel]["speed"]=_finite(value,"Playback speed",.8,1.2)
            for deck in decks:
                if hasattr(deck,"set_speed"):deck.set_speed(self.dj_settings[channel]["speed"])
        elif action in {"trim", "filter"}:
            minimum, maximum = (-12, 12) if action == "trim" else (-1, 1)
            self.dj_settings[channel][action] = _finite(value, action.title(), minimum, maximum)
            for deck in decks:
                method = getattr(deck, "set_"+action, None)
                if method:
                    method(self.dj_settings[channel][action])
        elif action == "effect":
            if not isinstance(value, dict) or not isinstance(value.get("tone"), (list, tuple)) or len(value["tone"]) != 3:
                raise ValueError("Choose a three-band tone effect and depth.")
            tone = tuple(_finite(item, "Effect tone", -12, 12) for item in value["tone"])
            depth = _finite(value.get("depth"), "Effect depth", 0, 1)
            self.dj_settings[channel].update(effect=tone, effect_depth=depth)
            for deck in decks:
                if hasattr(deck, "set_effect"):
                    deck.set_effect(tone, depth)
        elif action == "toggle":
            self.toggle_deck(channel)
        elif action == "seek":
            self.seek_deck(channel, _finite(value, "Playhead", 0, 86400))
        elif action == "mute":
            self.enter_studio()
            if channel in self.channel_muted:
                self.channel_muted.remove(channel)
            else:
                self.channel_muted.add(channel)
            self._studio_gains()
        elif action == "assign":
            self.enter_studio()
            if value not in (-1, 0, 1):
                raise ValueError("Choose crossfader side A, THRU or B.")
            self.channel_sides[channel] = value
            self._studio_gains()
        elif action in {"cue_set", "hotcue", "beat_jump", "loop_length", "loop_in", "loop_out",
                        "sync", "eject", "bpm_scale", "roll_on", "roll_off"}:
            self.performance_control(channel, action, value)
        elif action=="cue":
            if self.studio_mode:
                if channel in self.studio_started and channel not in self.studio_paused:
                    self.studio_decks[channel].pause()
                self.studio_paused.add(channel)
                self.seek_deck(channel, self.cue_points[channel])
            else:
                for deck in decks:
                    if hasattr(deck,"seek"):deck.seek(0)
        elif action=="loop":
            if channel in self.loops:self.loops.pop(channel)
            else:
                deck=next((d for d in decks if d.path and hasattr(d,"seek")),None)
                if deck:
                    position,duration=self.deck_clock(deck)
                    seconds = self.loop_lengths[channel]*60/self.track_bpms.get(deck.path, 120.) if self.studio_mode else 8
                    margin = .01 if self.studio_mode else .1
                    if duration-position>(.1 if self.studio_mode else 1):
                        self.loops[channel]=(deck.path,position,min(duration-margin,position+seconds))
        else:raise ValueError("Unknown DJ control.")

    def advance_loops(self):
        if self.paused_at is not None:return
        for channel,(path,start,end) in list(self.loops.items()):
            if self.studio_mode and channel in self.studio_paused:
                continue
            deck=next((d for d in self.loaded_decks() if d.channel==channel and d.path==path),None)
            if deck is None:self.loops.pop(channel,None)
            elif deck.clock()[0]>=end:deck.seek(start)

    def advance_mix(self):
        self._reap_samples()
        self.advance_loops()
        if self.studio_mode:
            if self.studio_fade is not None and self.paused_at is None:
                fade = self.studio_fade
                fraction = max(0., min(1., (self.now()-fade["started"])/fade["seconds"]))
                self.deck_levels[fade["incoming"]] = 100*math.sin(fraction*math.pi/2)
                for channel, level in fade["outgoing"].items():
                    self.deck_levels[channel] = level*math.cos(fraction*math.pi/2)
                    if fraction >= 1:
                        self.studio_decks[channel].pause()
                        self.studio_paused.add(channel)
                if fraction >= 1:
                    self.studio_fade = None
            self._studio_gains()
            return
        if not self.mixing or self.paused_at is not None:
            return
        fraction = max(0, min(1, (self.now()-self.fade_start)/self.fade_seconds))
        if self.manual_fraction is not None:
            fraction = self.manual_fraction if self.active.channel == 1 else 1-self.manual_fraction
        self.active.set_volume(self.volume*self.deck_levels[self.active.channel]/100*math.sin(fraction*math.pi/2))
        self.outgoing.set_volume(self.volume*self.deck_levels[self.outgoing.channel]/100*math.cos(fraction*math.pi/2))
        if fraction >= 1:
            try:
                self.outgoing.stop()
            finally:
                self.outgoing = None

    def protected_paths(self):
        return {deck.path for deck in self.loaded_decks() if deck.path}

    def busy(self):
        if self.studio_mode:
            return any(deck.busy() for deck in self.studio_decks.values())
        return self.active.busy()

    def clock(self):
        if self.studio_mode:
            deck = next(iter(self.studio_decks.values()), None)
            return self.deck_clock(deck) if deck is not None else (0, 0)
        return self.active.clock()

    def pause(self):
        if self.studio_mode:
            if self._global_pause_channels is None:
                self._global_pause_channels = self.studio_started - self.studio_paused
            for channel in self._global_pause_channels - self.studio_paused:
                self.studio_decks[channel].pause()
                self.studio_paused.add(channel)
            if self.paused_at is None:
                self.paused_at = self.now()
            return
        if self.paused_at is None:
            paused_at = self.now()
            # Publish the pause only after every idempotent deck command
            # succeeds, so a partial failure can retry the same intent.
            for deck in (self.active, self.outgoing):
                if deck is not None:
                    deck.pause()
            self.paused_at = paused_at

    def resume(self):
        if self.studio_mode:
            for channel in (self._global_pause_channels or set()) & self.studio_paused:
                self.studio_decks[channel].resume()
                self.studio_paused.remove(channel)
            self._global_pause_channels = None
            if self.studio_fade is not None and self.paused_at is not None:
                self.studio_fade["started"] += self.now()-self.paused_at
            self.paused_at = None
            return
        if self.paused_at is not None:
            # Keep the pause state retryable if an audio command fails.
            # AudioDeck.resume is a no-op for decks already playing.
            for deck in (self.active, self.outgoing):
                if deck is not None:
                    deck.resume()
            if self.mixing:
                self.fade_start += self.now()-self.paused_at
            self.paused_at = None

    def set_volume(self, value):
        self.volume = max(0, min(100, int(value)))
        if self.studio_mode:
            self._studio_gains()
            return
        if self.mixing:
            # A paused fade still applies the saved point on its gain curve.
            paused = self.paused_at
            if paused is not None:
                self.fade_start += self.now()-paused
                self.paused_at = None
            try:
                self.advance_mix()
            finally:
                if paused is not None:
                    self.paused_at = self.now()
        else:
            self.active.set_volume(self.volume*self.deck_levels[self.active.channel]/100)

    @property
    def crossfade_position(self):
        if self.studio_mode:
            return self.manual_fraction if self.manual_fraction is not None else .5
        if not self.mixing:
            return float(self.active.channel)
        if self.manual_fraction is not None:
            return self.manual_fraction
        instant = self.paused_at if self.paused_at is not None else self.now()
        progress = max(0, min(1, (instant-self.fade_start)/self.fade_seconds))
        return progress if self.active.channel == 1 else 1-progress

    def set_crossfade(self, value):
        """A/B are physical deck slots, including after an automatic transition."""
        self.manual_fraction = max(0, min(1, float(value)))
        self.set_volume(self.volume)

    def set_auto_mix(self, enabled):
        position = self.crossfade_position
        self.manual_fraction = None if enabled else position
        if enabled and self.mixing:
            progress = position if self.active.channel == 1 else 1-position
            instant = self.paused_at if self.paused_at is not None else self.now()
            self.fade_start = instant-progress*self.fade_seconds
        self.set_volume(self.volume)

    def set_deck_volume(self, channel, value):
        if channel not in range(4):
            raise ValueError("Choose deck 1, 2, 3 or 4.")
        self.deck_levels[channel] = max(0, min(100, float(value)))
        self.studio_fade = None  # A manual fader move takes over from automation.
        self.set_volume(self.volume)

    def stop(self):
        self.loops.clear()
        self.studio_fade = None
        error = None
        decks = self.loaded_decks()
        decks.extend(self.samples.values())
        if self.active not in decks:
            decks.append(self.active)
        for deck in decks:
            if deck is not None:
                try:
                    deck.stop()
                except Exception as failure:
                    error = error or failure
        self.outgoing = self.prepared = None
        self.paused_at = None
        self.studio_decks.clear()
        self.studio_started.clear()
        self.studio_paused.clear()
        self.studio_positions.clear()
        self.studio_mode = False
        self._global_pause_channels = None
        self.samples.clear()
        self.rolls.clear()
        if error is not None:
            raise error


class Service:
    """All render/poll/play work stays off Tk's UI thread. Opt-in per launch."""
    def __init__(self, root=ROOT, player=None):
        self.root = Path(root)
        self.player = player or Player()
        self.lock = threading.RLock()
        self.station = None
        self.enabled = False
        self.paused = False
        self.suspended = True
        self.closed = threading.Event()
        self.current = None
        self.repeating = False
        self.skip_requested = False
        self.message = "Pick a station. Fresh three-minute tracks, made on this PC."
        self.ready = 0
        self.progress = 0
        self.retry_at = 0
        self.thread = None
        self.stats_at = 0
        self.local_tokens = self.radio_tokens = None
        self.tracks_made = 0
        self.commands = queue.SimpleQueue()
        self.command_ready = threading.Event()
        self.auto_blend = True
        self.waveforms = {}
        self.waveform_pending = False
        self.waveform_results = queue.SimpleQueue()
        self.dj_setup = {"status":"idle","percent":0,"message":"Preparing DJ sound"}
        self.dj_thread = None
        self.dj_attempts = 0
        self.dj_retry_at = 0
        self.dj_enabled = False
        self.manual_mode = False
        self.pending_song = None
        self.song_library = []
        self.view = {"message": self.message, "ready": 0, "progress": 0, "playing": False, "paused": False}
        ensure_stations(self.root)
        self.station_catalog = stations(self.root)
        with closing(database(self.root)) as conn, conn:
            conn.execute("UPDATE tracks SET status='played' WHERE status='playing'")

    def enable_dj(self):
        if self.dj_thread is not None and self.dj_thread.is_alive():return
        if self.dj_setup["status"]=="ready":return
        self.dj_attempts+=1
        def setup():
            from music_edit import ensure_dj_runtime
            def progress(percent,message):self.dj_setup={"status":"working","percent":percent,"message":message}
            try:
                ensure_dj_runtime(self.root,progress)
                self.dj_setup={"status":"ready","percent":100,"message":"DJ mixer ready · local sound"}
            except Exception as exc:
                self.dj_retry_at=time.monotonic()+30
                self.dj_setup={"status":"error","percent":0,"message":str(exc)}
        self.dj_thread=threading.Thread(target=setup,daemon=True);self.dj_thread.start()

    def enqueue(self, action, value=None):
        self.commands.put((action, value))
        self.command_ready.set()

    def dj_control(self,channel,action,value=None):self.enqueue("dj_control",(channel,action,value))

    def play_song(self,job_id):self.enqueue("play_song",job_id)

    def load_deck(self, channel, path):
        self.enqueue("studio_load", (channel, str(path)))

    def load_song(self, channel, job_id):
        self.enqueue("studio_song", (channel, job_id))

    def studio_crossfade(self, value):
        self.enqueue("studio_crossfade", max(0., min(1., float(value))))

    def sample(self, channel, slot):
        self.enqueue("studio_sample", (channel, slot))

    def sample_volume(self, value):
        self.enqueue("sample_volume", max(0., min(100., float(value))))

    def _song_library(self):
        return completed_songs(self.root)

    def _load_song(self,job_id):
        song=next((song for song in self._song_library() if song["id"]==job_id),None)
        if song is None:raise ValueError("That song is no longer available. Choose another finished song.")
        return self._mix_saved_track(song)

    def _mix_saved_track(self,song):
        track={"id":"song-"+song["id"],"path":song["path"],"title":song["title"]}
        if getattr(self.player, "studio_mode", False):
            if self.player.mixing:
                self.pending_song = song
                self.message = "Queued your song after this blend"
                return "queued_after_transition"
            if self.paused:
                self.player.resume()
            self.player.mix_studio_song(track["path"], FADE_SECONDS)
            self.current = track
            self.manual_mode, self.enabled, self.paused, self.auto_blend = True, True, False, False
            self.pending_song = None
            self.message = ("Blending your song · " if self.player.mixing else "Playing your song · ")+song["title"]
            return "playing"
        if self.enabled and (self.current or {}).get("path")==track["path"]:
            if self.paused:self.player.resume();self.paused=False
            return "playing"
        if self.enabled and self.current and self.player.busy():
            if self.paused:self.player.resume();self.paused=False
            if getattr(self.player,"mixing",False):
                self.pending_song=song;self.message="Queued your song after this blend"
                return "queued_after_transition"
            self.player.set_auto_mix(True);self.auto_blend=True
            if not self.player.blend(track["path"],FADE_SECONDS):raise ValueError("This deck is not ready to mix yet.")
            self._update(self.current["id"],"played")
            self.current=track
        else:
            self.player.play(track["path"]);self.current=track;self.enabled=True;self.paused=False
            self.manual_mode=True;self.pending_song=None
            self.station={"id":"saved-songs","name":"Your songs","prompt":"","instrumental":False}
        self.repeating=False;self.message="Playing your song · "+song["title"]
        return "playing"

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def choose(self, identity):
        # Resolve the selected row on the radio thread, not the Tk thread.
        self.enqueue("choose_id", identity)

    def station_prompt(self, name, prompt, instrumental=True, station_id=None):
        values = _station_record(name, prompt, instrumental, station_id)
        self.enqueue("station_prompt", values)
        return values[0]

    def _choose(self, station):
        with self.lock:
            self.manual_mode=False;self.pending_song=None
            if self.current:
                self._update(self.current["id"], "played")
            try:
                self.player.stop()
            finally:
                self.station = station
                self.current = None
                self.repeating = False
                self.skip_requested = False
                self.ready = self.progress = 0
                self.enabled, self.paused = True, False
                self.retry_at = 0
                self.message = "Tuning in · preparing your first track…"

    def toggle(self):
        self.enqueue("toggle")

    def _toggle(self):
        with self.lock:
            if not self.enabled:
                if self.manual_mode:
                    self.message="Choose another finished song or pick a radio station."
                    return
                if self.station:
                    self._choose(self.station)
                return
            paused = not self.paused
            (self.player.pause if paused else self.player.resume)()
            self.paused = paused

    def stop(self):
        self.enqueue("stop")

    def _stop(self):
        with self.lock:
            self.skip_requested = False
            self.pending_song = None
            self.enabled = False
            self.paused = False
            try:
                self.player.stop()
            finally:
                if self.current:
                    self._update(self.current["id"], "played")
                    self.current = None
                self.message = "Radio stopped. Your stations are saved; the current render can finish in cache."

    def skip(self):
        self.enqueue("skip")

    def volume(self, value):
        self.enqueue("volume", max(0, min(100, int(float(value)))))

    def crossfade(self, value):
        self.enqueue("crossfade", max(0, min(1, float(value))))

    def auto_mix(self, enabled):
        self.enqueue("auto_mix", bool(enabled))

    def deck_volume(self, channel, value):
        if channel not in range(4):
            raise ValueError("Choose deck 1, 2, 3 or 4.")
        self.enqueue("deck_volume", (channel, max(0, min(100, float(value)))))

    def snapshot(self):
        return dict(self.view)

    def _publish(self):
        if time.monotonic() >= self.stats_at:
            self.stats_at = time.monotonic()+10
            self.local_tokens, self.radio_tokens = local_token_totals(self.root)
            self.song_library=self._song_library()
            with closing(database(self.root)) as conn:
                self.tracks_made = conn.execute("SELECT COUNT(*) FROM tracks WHERE completed=1").fetchone()[0]
        with closing(database(self.root)) as conn:
            self.station_catalog = [dict(row) for row in conn.execute("SELECT * FROM stations ORDER BY rowid")]
            rendering = bool(conn.execute("SELECT 1 FROM tracks WHERE status='queued' LIMIT 1").fetchone())
            if self.station:
                signature = hashlib.sha256((self.station["prompt"]+str(self.station["instrumental"])).encode()).hexdigest()
                self.ready = conn.execute("SELECT COUNT(*) FROM tracks WHERE station=? AND config=? AND status='ready'",
                                          (self.station["id"], signature)).fetchone()[0]
        elapsed, duration, on_air = 0, 0, False
        try:
            elapsed, duration = getattr(self.player, "clock", lambda: (0, 0))()
            on_air = bool(self.enabled and self.current and not self.paused and self.player.busy())
        except (RuntimeError, ValueError):
            pass
        decks = self._deck_views()
        if getattr(self.player, "studio_mode", False):
            on_air = any(deck["status"] == "Playing" and deck["gain"] > 0 for deck in decks)
            on_air = on_air or any(deck.busy() and deck.volume > 0 for deck in self.player.samples.values())
        self.view = {"message": "Paused" if self.paused else self.message, "ready": self.ready,
                     "progress": self.progress, "playing": self.enabled, "paused": self.paused,
                     "on_air": on_air, "elapsed": elapsed, "duration": duration,
                     "blending": bool(getattr(self.player, "mixing", False)),
                     "next_loaded": getattr(self.player, "prepared", None) is not None,
                     "local_tokens": self.local_tokens, "radio_tokens": self.radio_tokens,
                     "tracks_made": self.tracks_made, "rendering": rendering,
                     "auto_blend": self.auto_blend, "station_name": (self.station or {}).get("name", "Choose a station"),
                     "master_volume": getattr(self.player, "volume", 65),
                     "crossfade": getattr(self.player, "crossfade_position", 0),
                     "decks": decks[:2], "studio_decks": decks, "stations": self.station_catalog,
                     "songs":self.song_library,"dj_setup":dict(self.dj_setup),
                     "dj_settings":[dict(settings) for settings in getattr(self.player,"dj_settings",[])],
                     "dj_controls_ready":[self.dj_enabled and all(hasattr(d,"set_tone") for d in
                        self._audio_decks()
                        if getattr(d,"channel",None)==channel) for channel in range(4)],
                     "dj_loops":list(getattr(self.player,"loops",{})),
                     "studio_mode": getattr(self.player, "studio_mode", False),
                     "channel_muted": list(getattr(self.player, "channel_muted", [])),
                     "channel_sides": list(getattr(self.player, "channel_sides", [-1, 1, -1, 1])),
                     "cue_points": list(getattr(self.player, "cue_points", [0.] * 4)),
                     "hot_cues": [list(cues) for cues in getattr(self.player, "hot_cues", [[None] * 8 for _ in range(4)])],
                     "loop_lengths": list(getattr(self.player, "loop_lengths", [4.] * 4)),
                     "active_samples": list(getattr(self.player, "samples", {})),
                     "sample_volume": getattr(self.player, "sample_volume", 80.),
                     "active_rolls": list(getattr(self.player, "rolls", {})),
                     "station_id": (self.station or {}).get("id")}

    def _audio_decks(self):
        reader = getattr(self.player, "loaded_decks", None)
        if reader is not None:
            return reader()
        return [deck for deck in (getattr(self.player, "active", None), getattr(self.player, "outgoing", None),
                                  getattr(self.player, "prepared", None)) if deck is not None]

    def _deck_views(self):
        """Publish timing immediately while the waveform worker reads audio."""
        try:
            identity, peaks, bpm, offset = self.waveform_results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.waveform_pending = False
            self.waveforms[identity] = {"peaks": peaks, "bpm": bpm, "offset": offset}
            while len(self.waveforms) > 4:
                removed = next(iter(self.waveforms))
                self.waveforms.pop(removed)
        levels = list(getattr(self.player, "deck_levels", []))
        levels = (levels+[100.] * 4)[:4]
        views = [{"channel": index, "title": "Deck "+str(index+1), "status": "Empty", "elapsed": 0,
                  "duration": 0, "peaks": [], "bpm": None, "level": levels[index], "gain": 0} for index in range(4)]
        tempos = getattr(self.player, "track_bpms", {})
        offsets = getattr(self.player, "track_beat_offsets", {})
        known = {identity[0] for identity in self.waveforms}
        known.update(getattr(deck, "path", None) for deck in self._audio_decks())
        for path in set(tempos)-known:
            tempos.pop(path)
            offsets.pop(path, None)
        studio = getattr(self.player, "studio_mode", False)
        for deck in self._audio_decks():
            channel = getattr(deck, "channel", None)
            path = getattr(deck, "path", None)
            if channel not in range(4) or not path:
                continue
            try:
                stat = Path(path).stat()
                identity = (path, stat.st_size, stat.st_mtime_ns)
                if identity not in self.waveforms and not self.waveform_pending and not self.closed.is_set():
                    threading.Thread(target=self._read_waveform, args=(identity,), daemon=True).start()
                    self.waveform_pending = True
                analysis = self.waveforms.get(identity, {})
                bpm = analysis.get("bpm")
                if bpm:
                    tempos.setdefault(path, bpm)
                    offsets[path] = analysis["offset"]
                clock = getattr(self.player, "deck_clock", lambda value: value.clock())
                elapsed, duration = clock(deck)
                status = ("Loaded" if channel not in self.player.studio_started else
                          "Paused" if channel in self.player.studio_paused else
                          "Playing" if deck.busy() else "Finished") if studio else (
                          "Cued" if deck is getattr(self.player, "prepared", None) else "Paused" if self.paused else "Playing")
                title = Path(path).stem if studio else (
                    (self.current or {}).get("title") if path==(self.current or {}).get("path") and (self.current or {}).get("title")
                    else (self.station or {}).get("name", "Local track"))
                views[channel].update(title=title, status=status,
                    elapsed=elapsed, duration=duration, peaks=analysis.get("peaks", []),
                    bpm=tempos.get(path),
                    level=levels[channel], gain=getattr(deck, "volume", 0))
            except (OSError, RuntimeError, ValueError):
                views[channel]["status"] = "Audio unavailable"
        return views

    def _read_waveform(self, identity):
        """Complete one optional waveform without touching the live decks."""
        peaks = []
        bpm, offset = None, 0.
        try:
            peaks = waveform_peaks(identity[0])
            bpm, offset, fallback = audio_analysis(identity[0], need_waveform=not peaks)
            peaks = peaks or fallback
        finally:
            self.waveform_results.put((identity, peaks, bpm, offset))

    def _commands(self):
        while not self.commands.empty():
            action, value = self.commands.get()
            if action == "choose":
                self._choose(value)
            elif action == "choose_id":
                station = next((row for row in stations(self.root) if row["id"] == value), None)
                if station is None:
                    raise ValueError("Choose an existing radio station.")
                self._choose(station)
            elif action == "station_prompt":
                identity, name, prompt, instrumental = value
                save_station(name, prompt, bool(instrumental), root=self.root, station_id=identity)
                self._choose({"id": identity, "name": name, "prompt": prompt, "instrumental": instrumental})
            elif action == "toggle":
                self._toggle()
            elif action == "stop":
                self._stop()
            elif action == "dj_control":
                self.player.dj_control(*value)
                if getattr(self.player, "studio_mode", False):
                    self.manual_mode, self.enabled = True, True
                    self.paused = self.player.paused_at is not None
                    self.auto_blend = False
                    channel, control, _ = value
                    if control in {"toggle", "cue", "eject"}:
                        status = ("ejected" if control == "eject" else
                                  "paused" if channel in self.player.studio_paused else "playing")
                        self.message = f"Deck {channel+1} {status}"
            elif action in {"studio_load", "studio_song"}:
                channel, path = value
                if action == "studio_song":
                    song = next((song for song in self._song_library() if song["id"] == path), None)
                    if song is None:
                        raise ValueError("That saved song is unavailable.")
                    path = song["path"]
                self.player.load_deck(channel, path)
                self.manual_mode, self.enabled = True, True
                self.paused = self.player.paused_at is not None
                self.auto_blend = False
                self.message = f"Deck {channel+1} loaded · press Play when ready"
            elif action == "studio_crossfade":
                self.player.enter_studio()
                self.player.set_crossfade(value)
                self.manual_mode = True
                self.auto_blend = False
            elif action == "studio_sample":
                channel, slot = value
                if channel not in range(4) or slot not in range(8):
                    raise ValueError("Choose one of the eight sampler pads.")
                self.player.play_sample(channel, slot, sampler_sound(self.root, slot))
                self.manual_mode, self.enabled, self.paused = True, True, False
                self.auto_blend = False
            elif action == "sample_volume":
                self.player.sample_volume = value
                if self.player.studio_mode:
                    self.player._studio_gains()
            elif action == "play_song":
                self._load_song(value)
            elif action == "skip":
                getattr(self.player,"loops",{}).clear()
                if hasattr(self.player, "prepare"):
                    self.auto_blend = True
                    self.player.set_auto_mix(True)
                    self.skip_requested = not getattr(self.player, "mixing", False)
                else:
                    self.player.stop()
            elif action == "volume":
                self.player.set_volume(value)
            elif action == "deck_volume":
                self.player.set_deck_volume(*value)
            elif action == "auto_mix":
                if value and getattr(self.player, "studio_mode", False):
                    station = next((item for item in self.station_catalog
                                    if item["id"] == (self.station or {}).get("id")), self.station_catalog[0])
                    self._choose(station)
                self.auto_blend = value
                self.player.set_auto_mix(value)
            elif action == "crossfade":
                if getattr(self.player, "mixing", False) or getattr(self.player, "prepared", None) is not None:
                    self.auto_blend = False
                    self.player.set_crossfade(value)
                    self.skip_requested = not getattr(self.player, "mixing", False)

    def _rows(self):
        with closing(database(self.root)) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM tracks WHERE status!='pruned' ORDER BY created")]

    def _update(self, identity, status, **values):
        with closing(database(self.root)) as conn, conn:
            conn.execute("UPDATE tracks SET status=?,finished=COALESCE(?,finished),render_seconds=COALESCE(?,render_seconds),completed=MAX(completed,?) WHERE id=?",
                         (status, values.get("finished"), values.get("render_seconds"), int(status in ("ready", "playing", "played")), identity))

    def tick(self):
        try:
            self._tick()
        finally:
            self._publish()

    def _play_current(self):
        """A broken cache file must not trap the station on the same song."""
        try:
            self.player.play(self.current["path"])
        except Exception:
            self._update(self.current["id"], "error")
            self.current = None
            raise

    def _prepare_transition(self, ready):
        """Stage the next recording before EOF, then overlap both live decks."""
        if not self.current or not hasattr(self.player, "prepare") or getattr(self.player, "mixing", False):
            return
        channel=getattr(getattr(self.player,"active",None),"channel",None)
        if channel in getattr(self.player,"loops",{}) and not self.skip_requested:return
        elapsed, duration = self.player.clock()
        if duration <= 0:
            return
        rate=getattr(getattr(self.player,"active",None),"speed",1.)
        remaining = max(0, (duration-elapsed)/max(.1,rate))
        if not ready:
            self.skip_requested = False
        use_fresh = (self.repeating or self.skip_requested) and bool(ready)
        if remaining > PRELOAD_SECONDS and not ready:
            return
        target = ready[0] if ready else self.current
        try:
            self.player.prepare(target["path"])
            if not self.auto_blend and not self.skip_requested:
                return
            if remaining > FADE_SECONDS and not use_fresh:
                return
            if not self.player.blend(target["path"], max(0.1, min(FADE_SECONDS, remaining))):
                return
        except Exception:
            if target["id"] != self.current["id"]:
                self._update(target["id"], "error")
                ready.remove(target)
            self.message = "On air · skipping an unplayable buffered track"
            return  # The current deck keeps playing after a preload/start error.
        if target["id"] != self.current["id"]:
            self._update(self.current["id"], "played")
            self.current = ready.pop(0)
            self._update(self.current["id"], "playing")
            self.repeating = False
        else:
            self.repeating = True
        self.skip_requested = False
        self.message = ("On air · DJ blend into the next track" if not self.repeating else
                        "On air · blending a cached repeat while the next song finishes")

    def _tick(self):
        import music_backend

        with self.lock:
            if (self.dj_setup["status"]=="error" and self.dj_thread is not None
                    and self.dj_attempts<3 and time.monotonic()>=self.dj_retry_at
                    and not (self.root/"LongTasks/offline-network.json").is_file()):
                self.enable_dj()
            if self.dj_setup["status"]=="ready" and not self.dj_enabled and hasattr(self.player,"enable_dj"):
                try:self.player.enable_dj(self.root);self.dj_enabled=True
                except (OSError,RuntimeError,AttributeError) as exc:
                    self.dj_retry_at=time.monotonic()+30
                    self.dj_setup={"status":"error","percent":0,"message":"DJ sound needs repair · "+str(exc)}
            self._commands()
            if self.pending_song and not self.paused and not getattr(self.player,"mixing",False):
                song=self.pending_song;self.pending_song=None;self._mix_saved_track(song)
            if self.manual_mode:
                if getattr(self.player, "studio_mode", False):
                    return
                if self.enabled and not self.paused and not self.player.busy():
                    self.enabled=False;self.current=None;self.player.stop();self.message="Finished · choose another song or radio station"
                return  # Playing a saved song never starts another music render.
            rows = self._rows()
            # Reattach durable jobs after a restart without starting duplicates.
            for row in rows:
                if row["status"] == "ready" and not Path(row["path"]).is_file():
                    self._update(row["id"], "error")
                    continue
                if row["status"] != "queued":
                    continue
                job = music_backend.music_job_status(row["job"])
                if job.get("status") == "complete" and Path(row["path"]).is_file():
                    self._update(row["id"], "ready", finished=time.time(), render_seconds=time.time()-row["created"])
                elif job.get("status") in ("complete", "error", "stopped", "paused"):
                    self._update(row["id"], "error")
                    self.retry_at = time.monotonic() + 30
                    self.message = "Music needs a moment · retrying automatically."
                elif self.station and row["station"] == self.station["id"]:
                    self.progress = int(job.get("progress", 0))
            if not self.enabled or not self.station or self.paused:
                return
            if self.suspended:
                self.message = "Waiting for setup or update to finish…"
                return
            rows = self._rows()
            signature = hashlib.sha256((self.station["prompt"]+str(self.station["instrumental"])).encode()).hexdigest()
            ready = [r for r in rows if r["station"] == self.station["id"] and r["config"] == signature and r["status"] == "ready" and Path(r["path"]).is_file()]
            self.ready = len(ready)
            if self.player.busy():
                self._prepare_transition(ready)
                self.ready = len(ready)
                if not getattr(self.player, "mixing", False) and self.message.startswith("On air · DJ blend"):
                    self.message = "On air · next tracks are made in the background"
            if not self.player.busy():
                if ready:
                    if self.current:
                        self._update(self.current["id"], "played")
                    self.current = ready.pop(0)
                    self.repeating = False
                    self.skip_requested = False
                    self._play_current()
                    self._update(self.current["id"], "playing")
                    self.ready = len(ready)
                    self.message = "On air · next tracks are made in the background"
                elif self.current and Path(self.current["path"]).is_file():
                    self._play_current()
                    self.repeating = True
                    self.message = "On air · repeating this track while the next one finishes"
                else:
                    if self.current:
                        self._update(self.current["id"], "error")
                        self.current = None
                        self.repeating = False
                    self.message = "Buffering your first three-minute track…"
            if not any(r["status"] == "queued" for r in rows) and len(ready) < 2 and time.monotonic() >= self.retry_at:
                if foreground_music_busy():
                    if not self.current:
                        self.message = "Waiting for your current song render · radio will start automatically"
                    return
                identity = uuid.uuid4().hex
                path = self.root / "Radio/audio" / self.station["id"] / (identity + ".wav")
                path.parent.mkdir(parents=True, exist_ok=True)
                request = self.station["prompt"] + "\nCreate a fresh original three-minute radio track. No ads or spoken DJ announcements."
                # Serialize admission across windows. A second service may
                # have read the queue before the first one inserted its job.
                with closing(database(self.root)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("SELECT 1 FROM tracks WHERE status='queued' LIMIT 1").fetchone():
                        return
                    job = music_backend.start_music_job(request, str(path), duration_seconds=SECONDS,
                        instrumental=bool(self.station["instrumental"]), task_id="radio-"+self.station["id"], make_video=False)
                    conn.execute("INSERT INTO tracks(id,station,job,path,status,created,config) VALUES(?,?,?,?,?,?,?)",
                        (identity, self.station["id"], job["job_id"], str(path), "queued", time.time(), signature))
                self.progress = 0
            # Bound disk use; never delete an upload, song task, or active track.
            played = [r for r in rows if r["status"] in ("played", "error", "cancelled") or
                      r["status"] == "ready" and (r["station"] != self.station["id"] or r["config"] != signature)]
            protected = getattr(self.player, "protected_paths", lambda: set())()
            for row in played[:-5]:
                path = Path(row["path"])
                if str(path.resolve()) in protected:
                    continue  # An outgoing fade and preloaded deck still own these files.
                expected = self.root / "Radio/audio" / row["station"] / (row["id"] + ".wav")
                if path.resolve() == expected.resolve() and path.resolve().is_relative_to((self.root / "Radio/audio").resolve()):
                    path.unlink(missing_ok=True)
                    path.with_suffix(".lyrics.txt").unlink(missing_ok=True)
                    self._update(row["id"], "pruned")

    def _loop(self):
        next_tick = 0
        while not self.closed.is_set():
            try:
                # Clear before draining so a command arriving during the drain
                # or a playback tick also wakes the next wait.
                self.command_ready.clear()
                with self.lock:
                    self._commands()
                if time.monotonic() >= next_tick:
                    self.tick()
                    next_tick = time.monotonic()+0.5
                # Let queued recovery commands stop failing decks before mixing.
                getattr(self.player, "advance_mix", lambda: None)()
            except Exception as error:
                with self.lock:
                    self.message = "Radio will retry: " + str(error)[:100]
                    self.retry_at = time.monotonic() + 30
                    next_tick = time.monotonic()+1
                try:
                    self._publish()
                except Exception:
                    # Publishing uses the same database that may have failed
                    # above. Leave the last snapshot intact and retry next tick.
                    pass
            quick_tick = getattr(self.player, "mixing", False) or getattr(self.player, "loops", None)
            self.command_ready.wait(0.05 if quick_tick else 0.15)
        self.player.stop()

    def close(self):
        self.closed.set()
        self.command_ready.set()


def waveform_peaks(path, bins=384):
    """Bounded PCM waveform from the actual recording; unsupported audio is blank."""
    import wave
    bins = max(1, min(512, int(bins)))
    try:
        if Path(path).stat().st_size > 128*1024*1024:
            return []
        with wave.open(str(path), "rb") as audio:
            width, channels, frames, rate = audio.getsampwidth(), audio.getnchannels(), audio.getnframes(), audio.getframerate()
            if width not in (1, 2, 4) or channels not in (1, 2) or rate <= 0 or frames/rate > 600:
                return []
            chunk = max(1, math.ceil(frames/bins))
            if chunk*channels*width > 1024*1024:
                return []
            try:
                import numpy as np
            except ImportError:
                np = None
                import array
                import sys
            scale = 2**(width*8-1)
            peaks = []
            for _ in range(bins):
                data = audio.readframes(chunk)
                if not data:
                    break
                if np is None:
                    values = array.array({1: "B", 2: "h", 4: "i"}[width], data)
                    if sys.byteorder != "little" and width > 1:
                        values.byteswap()
                    low, high = min(values), max(values)
                else:
                    values = np.frombuffer(data, dtype={1: "u1", 2: "<i2", 4: "<i4"}[width])
                    low, high = int(values.min()), int(values.max())
                offset = 128 if width == 1 else 0
                peaks.append(max(abs(low-offset), abs(high-offset))/scale)
            return peaks
    except (OSError, EOFError, ValueError, wave.Error):
        return []


def audio_analysis(path, *, need_waveform=False):
    """Estimate tempo and beat offset, with a bounded waveform for compressed audio."""
    import subprocess
    import imageio_ffmpeg
    import numpy as np

    peaks = []
    rate = 4000 if need_waveform else 8000
    seconds = 600 if need_waveform else 60
    try:
        result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(path),
            "-t", str(seconds), "-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not 0 < len(result.stdout) <= seconds*rate*4:
            return None, 0., peaks
        samples = np.frombuffer(result.stdout, dtype="<f4")
        if need_waveform and len(samples) < seconds*rate:
            peaks = [float(np.max(np.abs(chunk))) for chunk in np.array_split(samples, min(384, len(samples)))]
        stride = rate//100
        count = min(len(samples)//stride, 6000)
        energy = np.sqrt(np.mean(samples[:count*stride].reshape(count, stride)**2, axis=1))
        onset = np.maximum(0., np.diff(energy))
        if len(onset) < 800 or float(onset.max()) < .003:
            return None, 0., peaks
        pulses = onset.copy()
        onset -= float(onset.mean())
        length = 1 << (2*len(onset)-1).bit_length()
        spectrum = np.fft.rfft(onset, length)
        correlation = np.fft.irfft(spectrum*np.conjugate(spectrum), length)[:len(onset)]
        low, high = 33, 101
        lag = low + int(np.argmax(correlation[low:high]))
        if correlation[0] <= 0 or correlation[lag]/correlation[0] < .1:
            return None, 0., peaks
        # Refine the integer lag; even a few milliseconds per beat drift over a song.
        left, center, right = correlation[lag-1:lag+2]
        curvature = left-2*center+right
        refined = lag+max(-.5, min(.5, .5*(left-right)/curvature)) if curvature < 0 else float(lag)
        bins = (np.arange(len(pulses))+1) % refined
        phases = np.bincount(bins.astype(int), weights=pulses, minlength=math.ceil(refined))
        offset = int(np.argmax(phases))/100
        return round(float(6000/refined), 1), offset, peaks
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return None, 0., peaks


def sampler_sound(root, slot):
    """Create a small deterministic local drum/synth hit for a performance pad."""
    import tempfile
    import wave
    import numpy as np

    if slot not in range(8):
        raise ValueError("Choose a sampler pad from 1 to 8.")
    folder = Path(root)/"Radio/sampler"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/f"cale-pad-v1-{slot}.wav"
    if target.is_file():
        return target
    rate = 24000
    seconds = (.45, .22, .12, .18, .32, .15, .6, .7)[slot]
    time_axis = np.arange(round(rate*seconds))/rate
    noise = np.random.default_rng(7300+slot).uniform(-1., 1., len(time_axis))
    if slot == 0:
        sound = np.sin(2*np.pi*(46*time_axis+5*(1-np.exp(-time_axis*35))))*np.exp(-time_axis*9)
    elif slot in (1, 2, 3, 5):
        filtered = noise-np.roll(noise, 1)
        decay = (22, 50, 30, 35)[(1, 2, 3, 5).index(slot)]
        sound = filtered*np.exp(-time_axis*decay)
        if slot == 1:
            sound += .4*np.sin(2*np.pi*180*time_axis)*np.exp(-time_axis*30)
        elif slot == 3:
            sound *= 1+.6*np.sin(2*np.pi*55*time_axis)
    else:
        pitch = (140 if slot == 4 else 880 if slot == 6 else 220)
        phase = pitch*time_axis + (180*time_axis**2 if slot == 7 else 0)
        sound = (np.sin(2*np.pi*phase)+.3*np.sin(2*np.pi*phase*2.8))*np.exp(-time_axis*7)
    sound *= .7/max(.01, float(np.max(np.abs(sound))))
    pcm = np.repeat(np.rint(sound*32767).astype("<i2"), 2)
    descriptor, name = tempfile.mkstemp(prefix=".pad-", suffix=".wav", dir=folder)
    os.close(descriptor)
    staged = Path(name)
    try:
        with wave.open(str(staged), "wb") as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(pcm.tobytes())
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    return target


# Generated controller artwork and MIT-licensed Phosphor icon rasters.
_CONTROLLER_ASSETS = {
    'knob': (
        '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'
    ),
    'jog': (
        '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'
        'ZpkYZcrzPKW8l/L8tHN2dnJycrR/fLi7s7e/d3japVyhxEFUpuV8byGBks251ZzLmq251vzc3FwWQwwxEGRjO89T3uv0+nnufr/fPTnrdHqcW0Fg'
        'GzNbLkBCgLHNRy1JlgEbMxMvQCAEBmGQB8jnSoD5f////9YAVlA4II4kAQAwNgSdASogAyADPikSiEMhoSEhojNKIDAFCWluc43SzxX/8y1/YP/V'
        'kx3b+r5m8HP5vNfynv3PTvZr/8vnhd+f+n3ejpk4lycbqai+052JhG64/QH+K5rPE/5b/n/7f+QnEDdc/YA/i/8o/xn9o9lvAGoAcYXPL/vv7F+w'
        'j+d/2j/w/4b1qvX//n/uN50Psn9oerb9ofnvn/r3z/sd8/bo/R3tuee09JO8K/zjzAOsc/eH1AP7n1AHSD8dPyP9z/oH+x/Lnzr9Kvxv3F/u3zBX'
        'z/d/9X/1/7j1Q/n/46/df4j/M/9//J/u192P9X/wfdL60/r39l/2f9L+7/wF/kn88/0X9z/dL/I/uV683gRcx+7nqL+5323/X/5H90P8b+7H2A97'
        'P9P64fwf/B/7HuB/07+y/7D/D/ut/j////zPjg8Tr9N/4/3G+AX+ff3f/ff5393P8N///uJ/1P/R/sP9v+2Puk+uP/F/qf9r/7f8x////h+hX8s/'
        'qX+o/vX+b/6v+F////m+7L/6/9T/qfIb9q//z/s/+V8n367/7n8x/3//945J3d3d3d3d3d3QvAolSjyvJe0jguJaVlNvWSuvPX7nUOOxaArRQNic'
        'crxC1bqA5xtzYgRh1cr39eJM40g2nJ43rQaPKOV53d3d3d3d3d3d3c6ZlxFQ273/uTabUX+hc/7hu97wdDKe3U1OTYSzmKv0EZzXWCMkEVzAyePR'
        'nr01rhzMjOboTkHRKD2+4GicKmhhEeaLNVtct/e2hoFlOlQdOWcLIH5DUxJphmIDVI2x4WYMDK87u7u7u7u7u7u7u7u52gCTfLoq8j//8sBM/+6n'
        '/1i2E/8xSzxrtqIgte6Eovm85yx8XAOQe5S019K/xOWpgEICy/2l5cwTSzp+4aQgVQtRVcZdpxsVghOukJdPXkEaDqv9lzGpR0n6U9OUDYvrKd6B'
        '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'
        'gVtkAAAAAAAAAA=='
    ),
    'fader': (
        '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'
    ),
    'eject': (
        'iVBORw0KGgoAAAANSUhEUgAAAFAAAABQCAYAAACOEfKtAAAER0lEQVR4nO2b7U3bQBiAz8ovBIgwQWGCmgkaJiBMUGeCphM0nYB0gjgTECZINqg7'
        'AXQCggDxB0Sfk3JCqsD32uev2PdIp3tPMnf3Pjn7bCcEyuOEF+iIF+iIF+iIF+hI4wTe398PgiD4ShhuiibR5fX1db6/v79SDaIxAh8eHkKqC8pA'
        'pbPq9XqjnZ2dG9UAGiFQy2N1LVl5fZpWOHbNsad7e3sJzVqpXWBWeQb+phESaxWYV56Bv61dYm0CXeUZ6KNWibUILEqegb5qk1i5QJLtI/CahPs0'
        'bcwpGn1bY+Nmd3f3hH7XxJURUCpDy3t8fFwShhQbI1ZUrADhkVJqRrGRIPE0qFBiZQLzyjM0VWIlAjPKmyMvUu+AxFjJTufKJJYusCh5hqZJLFVg'
        '0fIMTZJYqkASXVINlB2xPAN9x0ogkQ9xwQuIc8JSKE0gCc6oImUnszwDY8RKIBFixhhRF05AKRwSm1FFyk5ueQbGilWNEgNKoZDQjCpSdv6QUEjt'
        'BKeovs6ulFKfKTYKlxhQCiOLPC7u+sXpmtiZOiUGlEKoS56hLokBxRlew08Q8oPQRinyDFkkcuxPdueJcsRZICsvUrJHrFLlGRAjlggjVmKsHHAS'
        '2DR5hiol5hYolUcyd0zwqCp5hqenp6Pn5+eEcQ9o2sgtMZfALPJIYMDkEpqVwzz1i9sVcyhNYmaBTCpSWyDPwHxLlZhJINeVIZO5JEyFYxohz5BF'
        'IsedsjuvlBCxwM0klkyiT/NDOKZR8gyb+VslcsyaY06l8xcJpFPR9xgc10h5BnIQSySH40Cw8YkEMnCsBA/sDJxp+dcBN/0DxCwJbcyRGCkLIoEM'
        'esugfcI0RgwYqy2ABREpy0bIYlizGA4JU7EKRJ70E2sdSLSeUV5gCoUIpBP9WHRL2Dl4/Dxk8awJP8QqUMMqXNHRF8LOwMK5YvUNCVORChwgcEnY'
        'GRBoPX01IoEayc7VIsR3FGKBGq6FQz6ZKeEnShv5GwTBmGvfglhEJoGGzSkdIrNPc+shlzW5JJJT9n9yCfS84QU64gU64gU6kksgu/EZVUhpEwm7'
        '7xV1JjIJ1PeC7FYX7Fp9mq2D3PTL1O/Se0CNWCDyplTfKF3gFxLH1FZEAjllh3w6l4SdgZV4zim9IExFJJDV95sqpHSJhFV4Qp2KVaD+gvrl5eWa'
        'sHP0er1j23+FWgVuHtuWhJ2Dy5b1jYwXmEIlAiWDNJUicvMCHXPzAh1zcxYIY3arhHrr4O4ipJpS3qUqga3FC3SkEIF04r8XTsEqUMMqXNDRGWFn'
        'YOEU970wz8IhHa6QeECz9ZDrHbmKfqYnEqhBYkTHUzputURy1PLGyIuVALFADRL1SpwwwBnN1kFuV+Q2QZ515RkyCTQwkP7FakjYGrQ05K0JM5FL'
        'oOcNL9ARL9ARL9ARL9ARL9ARL9ARL9ARL9ARL9ARL9ARL9ARL9CRf5OkrW+Cz5rIAAAAAElFTkSuQmCC'
    ),
    'folder-open': (
        'iVBORw0KGgoAAAANSUhEUgAAAFAAAABQCAYAAACOEfKtAAAENElEQVR4nO2YcTLrUBSHT/CPwejbQd4K5K3geStQK3jpCtQKsAK1ArUCtYNaAV0B'
        'ViBvMPyD98Wooe5Nmp5UcvV+M2fORTT5fb23SW8gHhVeoBIvUIkXqMQLVOIFKvEClXiBSrxAJV6gEi9QiReoxAtUUkjg7e1tND8/v8pQzeLi4inN'
        'eQIql7u7u53n5+dYREIplz6vu7eystIXRwkoK4RrIO+Y4bpMl87y8vI23TkyBbJkD2mxfA3nfDxssrQvxSGsApEX0c6oL4MZn8zNzbWWlpZ6/OgE'
        'WQI7tC2qCpxZ0laBNzc3/SAIfjOsCieWdJ0FOrGkJxU4IFybPhG8boe2Ro1LX6YAGRKupccbdEJP+FVhJhLIiU95dlsXBRV/xn6APAltk0x9KUhl'
        'AlN4xmw+PT11Oc8qP1YOuf6Qqy8FqFRgyv39ffj4+NhjuEZVCrkS7v4/gwLLuXKBQ2q0pFtI7MqY1EZgSh2WNNlOyNZkOBa1EphS9ZIumq12Aoew'
        'pGPO02TYoMomItsq/ROcs1C22gqcJmVm8wJHKJrNCxyhaLZSBfL7xsPDwxp30nWpMVxnLCKhmLkkd1fewfGXbGycmjY2AspIUYE8guwgrs3/TOND'
        'vy70Edl6L7IUgdwxD2mxzABkTxYWFn4NJaoFctwux+0wnCX6fFv5Q9cJpDeYfRcc952XrREyb7IN1lMJRF4sIofULHLELIxVAjmmxzEb/GrmGDqY'
        'WCD2m9x5r/lxJsHBy6bDxAIDtsIZ7lM2DpDcpjsJH0/ptVvz4WAPgbsagasMI8oIz0s/h7d6F0HgGS0330QC4VJEQrEzYPZZT153XrfULhjaeMsX'
        'UEZyBOaxzQk6dCdh9rVp+5SNt3xTETic3uIoCLyghWLhfb6AMqIQOODdiehOgrz02s8oGx/yTUNgixN0xVEQ2KFtUTY+5CtdIF9vfvB/CUMnQeAF'
        'LRQLo/kCysgkAnm8eXm4ZOgkyItoZ5QRU75SBcKH6e0aCOyKyF/Kxqd8pQocnd6uQeZrrr/B0IgpX2kCTdPbJfhe3yTDMUMbR8y+WEYoTSDHvuyP'
        'MXSSvOVry1eKQN65f8y+BkNnIa91+WblK0UgGKe3KzD7YsneGLbmK0UgxxmntyuQNXNjmL9Z86kFZk1vF+D6G9xArhnauGL2hWJBLRCOOEEsjjLG'
        '8j0gX5tupAyBvzjBOd1JyJm5fCEzn1bgFS8eiqOMsXGam08r8IATtOlOwvJNr32fspGbTyvwnEooJ+EGEpGxwdDI+41TG1qB35kBsy+iZ+IF2tlG'
        'YIeeiRdoYZzlm5IlsIfADYazyIDZF9FzsQrkDhVL9gPmd6aFwK6MgVVgyozOwiPkxTImmQJTXmfiOrf8UL43CZOlh7yuFCBXoCcbL1CJF6jEC1Ti'
        'BSrxApV4gUq8QCVeoBIvUIkXqOQ/yw2vb+zh4loAAAAASUVORK5CYII='
    ),
    'pause': (
        'iVBORw0KGgoAAAANSUhEUgAAAFAAAABQCAYAAACOEfKtAAACmUlEQVR4nO3cwW0TYRiE4W/lU5RESgfQAaGCrDtwCXSQErArgA4ogVCB1xVACU4H'
        'kWyfzazEYebE7P4Lp3mk1f45ZD15Y1/dVTRJwEYJ2CgBGyVgowRslICNErBRAjZKwEazA16v14fL5fIFx76q3tc8A56zu7+/H2oGvP4Gv/+MY1/z'
        'HKvq5fb2dtd13RvOk3W4Jjufz48YvseLPuDHZnjWGHFbE2DDN9w+1TKOq9VqfXNzc6yJ5gb8idsjrsUg4hoRhzL8eed9x3FJw93d3Rr3STpck5xO'
        'px7vvD2Oi0KQAwL2ZcCGARuecFwUNqyxYagJ5gTcYvxnHBeHd4C1B5+AK26LQ8AdAm5rAmswS0BlDWYJqKzBLAGVNZgloLIGswRU1mCWgMoazBJQ'
        'WYNZAiprMEtAZQ1mCaiswSwBlTWYJaCyBrMEVNZgloDKGswSUFmDWQIqazBLQGUNZgmorMEsAZU1mCWgsgazBFTWYJaAyhrMElBZg1kCKmswS0Bl'
        'DWYJqKzBLAGVNZgloLIGswRU1mCWgMoazBJQWYNZAiprMEtAZQ1mCaiswSwBlTWYJaCyBrMEVNZgloDKGswSUFmDWQIqazBLQGUNZgmorMEsAZU1'
        'mCWgsgazBFTWYJaAyhrMElBZg1kCKmswS0BlDWYJqKzBLAGVNZghYI+AexwXhfE/MH6D419hw4ANTzgu7SP+ib9wt00OOMI7YHyRD7gWg4BrBBzK'
        '8C++9gTPO+D1+5pobsBH3F5wvcPVDON3GL+tCbDhK27PuJbwulqt+v/2xTsj/NHjVz9tq2qDa1ZIPOOA2xbxhpoBH+W+8PsNH+dXXONXP43PeMN5'
        'sg5XNEjARgnYKAEbJWCjBGyUgI0SsFECNkrARr8B1nz4byPzG28AAAAASUVORK5CYII='
    ),
    'play': (
        'iVBORw0KGgoAAAANSUhEUgAAAFAAAABQCAYAAACOEfKtAAADXklEQVR4nO2c3W3bMBCAySQv+QO6QbNBs0nTDewJam+gbKBskExQeQNngioTVNlA'
        'ARLkKXA/qnah/NiRRcoiqfuAA495/MAT7yJDWglWiEBLRKAlItASEWiJCLREBFrSqcDHx8dzrfV3UrVYLGYnJyc5aVR0IvD5+fns5eXlF+k5UWeO'
        'yMvT09O5igTnAhH05enp6TfpmVpPenx8fKm1LsmDRhNOoWxTlp/ERhBdInBCWd+wDRZNOAWBf1jOVHPmSqkpInPW4NCEUxC4YNkaTmNydHR0xVqy'
        'DQZvBC4pEDjl+ZiRB4FvAlfM9/f3x4eHh4XyHF8FVpfM3t5edVuz9RZNOMWVwBoFMse+9o4hCKxAYsZNPdaeXTLBCDQg0fSOCSKv2HpBUAJr5MQY'
        'kWbtlVAFrqguGd1jWYcucFXWE07jDdudowmn7FpgjV56x5gEVnAadzoSRidwSYHAnYyEsQqs4PmYHRwcTLss66gFGpDY6UioCaf4JrBGgUznI+GQ'
        'BK645jROeUaW5NYMUaDiJJre0clIOEiBNXLCaiQcusAV1SWjW5S1CFyyLOutR0JNOCVUgTW2GglF4Bo4jY1GQhG4mYLS3tg7isBmmBf/Kes7RGBD'
        'KOUf3NQZ6StEYEMo5RmlfEH6ChG4BZTxO1/v/mCLCLQkVoFSwpbIJWKHtDEtuad0R5TuXK1BBK4BcZfm1FG6Jdu1iMA3IO6WF1Ej+WfCliDugZdP'
        'o48uik2IwH9cIS75rFw/YugC74gRz7qctRWDFGjKldOWIC5la8UQBd5QrhMEluTWDEngpz1dG6IXiLQHlhRxieqAqAUib0ZPN2na07UhVoH3mleU'
        'POsy8k6JTiCnrtEI5opoBCJuqxHMFcELRJzp6cwvCq5VD4QusPUI5opQBd4RI05dztorQQlclmuCuJStFwQjEHkzxI0QWLL1hhAEdjKCucJbgUh7'
        'YOlsBHOFlwKR10tP1wbfBN5rejpak4w8CLwRyKnb6Qjmii4EFkqpr0QjEHerOXXIy9kGRxcCU5Ymn34yPZ0Rd60CxrlAxJiPj+Wkm05h7yOYK5wL'
        'NCw/f5eRfiP+g9xbloTWZK4iQROdQTmfI+2CVHHaMso1J42KTgUOARFoiQi0RARaIgItEYGWiEBL/gJC7ftgH3hIBgAAAABJRU5ErkJggg=='
    ),
    'speaker-high': (
        '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'
        'VsCS/Av35AWr8PotxgAAAABJRU5ErkJggg=='
    ),
    'speaker-slash': (
        'iVBORw0KGgoAAAANSUhEUgAAAFAAAABQCAYAAACOEfKtAAAJzUlEQVR4nO2c7XHTShSGj4A/DDAxFRAqiFOBpQqSVIBcQZQKYlcQpwKUCvCtwHIF'
        'mAowFSAGGPgB+D7HNxaytSut7Pjrjp+ZnXPW2j17zuvVh22IJwdW4iDgihwEXJGDgCtyEHBFDgKuyF4K+OPHj+Pfv39f4jZp48ePH3efPn06li2w'
        'dwJ++/YtFJG3tIzJZJJiLl68eJHIhllaQHZBCyO880PM2kGkxvfv329wQzEzfv78+WvsRvFotaCI8z9//rz1PK9BVwtLHz161H727Fmf7lrgzdJT'
        '9h1uk1bGKSKOsBujloBfv371EW6AWwAhg3WcQotvWBnryqGMWgJy/YlF5A2tAMmnFBk85A5AvOvJZNIRRxi72wKyAxNEauEaoYAHEZE4er3TU9aX'
        'GjBvtwUs24E5xlwPTxEyxa8NazQRYsD8Bt1aMG/nBWxi3tOqGCFi4NUUkfiXmB5tKXZeQIUiQ1l4DrNQS0Ti9jAq4NLshYAKxYbiJmLM9bCNLYVr'
        'q4/QA9yV2BsBFUR03TGVIhIrluprqwr0BaGPcI1wfH8EVFwLh1tEjLBG2IF9hDnDLePO87w+Ir3DN8Kx/RJQqSFiGxFjMUCMUMovCdO5CF16qu+l'
        'gCStz2yJiJzQqpgKIQYQcYRZjPGJds4cPSb/SwEVEncWkbHGInm9gYgRAp3T1X4f4Xr0U7pT1i0g67+ZTCbnrNGgq08RXS+3vokHEVBhYRVxhPuK'
        'ZoVxKUkFiKNja7FOARHvLSaUHMRLnzx5cso3TmOxUCkgQRo/f/6s3FkK35g0MT1aKcRcSsR1CYh4Tcx7mgndiYFn2YlWATUoCV0z8ZzuOqj9kW9d'
        'AhK3Q9xrXCPE7RP3AreAUUDEC5l0Q9AG3XVS+u4uQqE+Ywe4Rsh5LQIqHL8g1z7uHAUBEU933oAJDbqbwFlECvUZN8A1Qt5LCag1Y2yn8BRip1xy'
        'XnsLeRYEJMk+g85wN0nlpxWF3NYioELsDrGvca0Qv0v8juTwaBkM0DvpZ9xtUCkiRfoUOcA1Qv4BBSayJOzEEeaEZoVfAF/n78oeLaMqwQ1Q9ZGv'
        'NL8qAe9/W2mwxohuAT3+69evEWsc0TXCGl3W6Mg9uyag0qbAWAyQfOkZwrX0JfmnuHOws5qYtzS1Gsf6QxhjQ/lvrBGdi4AvcafUFfCKACNsKcTo'
        'YU5oy2IVkRz7xD/DnYO8/qGwc9w5eF0/4XxkToPuHBwLmJPIAowfS8kHAmJld+RaAtoWXIQ4CXFauEtjW4vXVRAVMYvPa0MEP+e1lO4cjI0wN7QC'
        'zEuZ99pbmMecUEp2IdwxLxTYhoAfaA3aK5oV1kqJEZDoiG4B1vA5fsy4cVlOiBFLybdFzO8yvyMLEF/XP8ItwJzsNPZoGUzSpAa4RpgYMDGRCoiT'
        'EKeFW4AYQ8/zIqyOMSY4gzFahFVEFxAwwtzQrCzeWRXmxVIi/GzOVgQkhk+CTXwdd8TLZYy53pwyLsWvDWvoKT9mftk6V7xJPWwGc0IpOY2JN70O'
        'bk1AAcb5jBvgVuH8acVElRj5nGbwWukdn+Nd5nS2KqBSVVyOVUUcYU5oRtiBc1oozJlgbNwxJ5ybROE+CQ5wjVB8QPGJVECchDgt3ALEGBLDlxwk'
        'GoqbiDFJt7G1YY0Ic0OzcUrsETaDOWOx3OxmdXj4GRTuU/gA1wiTAiYlUgFxEuK0cAsQY0gMXxYg2R7mklbFUiKSk09OA1wj5BWQVyI5mJMwp4Vb'
        'gPFDxu+OgAoixlJy58txi4gR1hly8slpgGuEvALySiQHcxLmtHALMH7I+N0SUKkhYhsRY3GEnDrkdI1rZPZYIjnI5SPmWMzcsX7o4WSwiM8iA1wj'
        'FB9QfCIVECchTgu3ADGGxPDFAsf17pdIyQU/R5siYnEAMd5jmjQjxJnTQmHOBGOEPLvU0ZmbROE+hQ9wjTApYFIiFRAnIU4LtwAxhsTwpQTGOIvI'
        '2IB4iZSg37LwLcxHXCPEGBLDlxwOc7rM2U0BFcapiCPcVzQrjKv8tEKcc8a9w7VxxfweNoPdF0rJkwFrbv9BugqKaDJeYx3RtcKYUhHJx+f4ANcI'
        'QrzkeIqbwdqxlFyLZ9fMnRZQoRAnEWGMEKeMmxNiBnHGYt7NtwgfYeeghs/EauCa+MCcJlZ2XkCFeD7xBrhVWD+t3F/T+rgntCnkYvwOEbFDKTl9'
        '4RYBI2w9ASFi646wpZBoD9OkFSDp2gIqDkXNsIqoaI18G90gjzEijHipAGt9xByLBWJPr3+44tEyNDgHB7hrg8SXElChsAhzQ6siRpw2tjYOa3wi'
        '9rHc49EyNiEgjEjgFFubmvnVFlFPc35Ues8aDbo2rojbw07xaBnsDn10+Iy7bhJOgQsSTfGdqSmgckuxEdYJdp/G9sUC+nwh3jE5pHSnzAmokGSf'
        'AWe4a4VkUtYJSGhE1wly85mjRdahzRqxVIB4oVRcY8m5y+WnIzkKAhKoycCERI/oboKIAm+xlSwpoFIpIrETYrdwbXzirGkyJsXPKAioIGKIiD0G'
        'b0rEmOSuWG8uuUUo0mfMALc21BOwexKxQM1jMT8nTrHNNwqoEFB3YoeEz+hughFNd4paIysKmDI3sMUndp/jtlpvmRdhC1gFnMHC+qNME7c2989b'
        'Hck9vJbBWOu/GFAocmkBFY3PfKOIWiPHE44f0c3zgXx8Xk/xC1QKuCokpXf2Hu4bmis9irzCzrGqgPeMEeSUOCn+HCoiJpa/b/gdYyPT2BlrF3AG'
        'yUWYG5orCclf5JOvEpA3q+uVfGmao/TTSh02JqCCiLbTxAhjU8Zmp5yDgIHnecdS8Thyz4OI6NE2CkXqNbVP4i26rkSIeOsioN4piR+Km4i1P60s'
        '4tG2AkX2MJc0V2LE+weR3uEb4dhUQIEa8VcS0aNtDW4u+vcQVJgjupUgUMrYBq4RjmcCKogYi9vNy/qYUsVWBVQosomJ5e+db2kWBVSIH4ubiG1E'
        'jKUmWxdQofBlHnUKEKcgIK9p7ETc3qDaIu6EgDPYLRHmhrYUiFUQUOF1JxEZV/lfuxbZKQEVRKz1qJOHeUYBFY6piCPcVzQrjOsSoyOO7JyACkUs'
        '86ij8wKKT8QCMSvfHI4PieGLIzsp4AwK7mEuaU5QfEDxiZRAzFIROdYlRkcc2WkBFU4750cdig8oPpEKbA/kzP/CTeSYYyldJ3ZeQEV3DSaWipsA'
        'nCLACFvJvYix/L0mfqCFrvNn7IWACrtDbwI93Dc0E58o/lhqoj8kCdS58+bZGwFnsBsjzA0tA3G/YM5dTt+HZu8EVHTX8ON9hHBNTsMxP/Z3lt1B'
        'q7KXAu4SBwFX5CDgihwEXJGDgCtyEHBF/gX5C4Cr/iJIcAAAAABJRU5ErkJggg=='
    ),
}


class ControllerKnob:
    """A draggable, keyboard-accessible rotary control using the real knob raster."""
    def __init__(self, parent, window, image, variable, minimum, maximum, step, command, default=0):
        import tkinter as tk

        self.window, self.source, self.variable = window, image, variable
        self.minimum, self.maximum, self.step = minimum, maximum, step
        self.command, self.default = command, default
        self.dragging = False
        self.widget = tk.Canvas(parent, width=44, height=44, bg="#303032", highlightthickness=0,
                                takefocus=True, cursor="hand2")
        self.widget.bind("<Configure>", self.draw)
        self.widget.bind("<ButtonPress-1>", self.press)
        self.widget.bind("<B1-Motion>", self.drag)
        self.widget.bind("<ButtonRelease-1>", self.release)
        self.widget.bind("<Double-Button-1>", lambda _: self.change(self.default))
        self.widget.bind("<MouseWheel>", self.wheel)
        self.widget.bind("<Left>", lambda _: self.change(self.variable.get()-self.step))
        self.widget.bind("<Right>", lambda _: self.change(self.variable.get()+self.step))
        self.widget.bind("<Up>", lambda _: self.change(self.variable.get()+self.step))
        self.widget.bind("<Down>", lambda _: self.change(self.variable.get()-self.step))
        self.widget.bind("<Home>", lambda _: self.change(self.default))
        self.trace_id = self.variable.trace_add("write", lambda *_: self.draw())
        self.widget.bind("<Destroy>", self.destroy)

    def destroy(self, event):
        if event.widget is self.widget:
            self.variable.trace_remove("write", self.trace_id)

    def draw(self, _event=None):
        from PIL import Image, ImageTk

        size = max(8, min(self.widget.winfo_width(), self.widget.winfo_height()))
        fraction = (self.variable.get()-self.minimum)/(self.maximum-self.minimum)
        image = self.source.resize((size, size), Image.Resampling.LANCZOS)
        image = image.rotate(135-270*fraction, resample=Image.Resampling.BICUBIC)
        self.photo = ImageTk.PhotoImage(image, master=self.window)
        self.widget.delete("all")
        self.widget.create_image(self.widget.winfo_width()/2, self.widget.winfo_height()/2, image=self.photo)

    def change(self, value):
        if str(self.widget.cget("state")) == "disabled":
            return "break"
        value = self.minimum + round((float(value)-self.minimum)/self.step)*self.step
        self.variable.set(max(self.minimum, min(self.maximum, value)))
        self.command(self.variable.get())
        return "break"

    def press(self, event):
        self.widget.focus_set()
        self.dragging = True
        self.anchor = (event.y_root, self.variable.get())

    def drag(self, event):
        if self.dragging:
            sensitivity = (self.maximum-self.minimum)/100
            if event.state & 1:
                sensitivity /= 5
            self.change(self.anchor[1]+(self.anchor[0]-event.y_root)*sensitivity)

    def release(self, _event):
        self.dragging = False

    def wheel(self, event):
        return self.change(self.variable.get()+self.step*(1 if event.delta > 0 else -1))


class ControllerFader:
    """A real draggable value, with a metal fader cap matching the controller."""
    def __init__(self, parent, window, image, variable, minimum, maximum, command,
                 *, horizontal=False, reversed_scale=False, background="#303032"):
        import tkinter as tk

        self.window, self.source, self.variable = window, image, variable
        self.minimum, self.maximum, self.command = minimum, maximum, command
        self.horizontal, self.reversed_scale = horizontal, reversed_scale
        self.dragging = False
        self.widget = tk.Canvas(parent, width=40, height=110, bg=background,
                                highlightthickness=0, takefocus=True, cursor="hand2")
        self.widget.bind("<Configure>", self.draw)
        self.widget.bind("<ButtonPress-1>", self.press)
        self.widget.bind("<B1-Motion>", self.drag)
        self.widget.bind("<ButtonRelease-1>", self.release)
        self.widget.bind("<Left>", lambda _: self.change(self.variable.get()-(self.maximum-self.minimum)/100))
        self.widget.bind("<Right>", lambda _: self.change(self.variable.get()+(self.maximum-self.minimum)/100))
        self.widget.bind("<Up>", lambda _: self.change(self.variable.get()+(self.maximum-self.minimum)/100))
        self.widget.bind("<Down>", lambda _: self.change(self.variable.get()-(self.maximum-self.minimum)/100))
        self.widget.bind("<MouseWheel>", self.wheel)
        self.trace_id = self.variable.trace_add("write", lambda *_: self.draw())
        self.widget.bind("<Destroy>", self.destroy)

    def destroy(self, event):
        if event.widget is self.widget:
            self.variable.trace_remove("write", self.trace_id)

    def draw(self, _event=None):
        from PIL import Image, ImageTk

        width, height = self.widget.winfo_width(), self.widget.winfo_height()
        if min(width, height) < 2:
            return
        self.widget.delete("all")
        fraction = (self.variable.get()-self.minimum)/(self.maximum-self.minimum)
        fraction = max(0., min(1., fraction))
        if self.reversed_scale:
            fraction = 1-fraction
        if self.horizontal:
            self.widget.create_line(9, height/2, width-9, height/2, fill="#080808", width=5)
            cap = self.source.rotate(90, expand=True).resize((max(8, round(height*.5)), max(12, height-4)), Image.Resampling.LANCZOS)
            x, y = 9+(width-18)*fraction, height/2
        else:
            self.widget.create_line(width/2, 9, width/2, height-9, fill="#080808", width=5)
            for index in range(9):
                y_tick = 9+(height-18)*index/8
                self.widget.create_line(width*.1, y_tick, width*.3, y_tick, fill="#85858a")
                self.widget.create_line(width*.7, y_tick, width*.9, y_tick, fill="#85858a")
            cap = self.source.resize((max(8, width-3), max(8, round(width*.32))), Image.Resampling.LANCZOS)
            x, y = width/2, 9+(height-18)*(1-fraction)
        self.photo = ImageTk.PhotoImage(cap, master=self.window)
        self.widget.create_image(x, y, image=self.photo)

    def change(self, value):
        if str(self.widget.cget("state")) == "disabled":
            return "break"
        self.variable.set(max(self.minimum, min(self.maximum, float(value))))
        self.command(self.variable.get())
        return "break"

    def press(self, event):
        self.widget.focus_set()
        self.dragging = True
        self.drag(event)

    def drag(self, event):
        if not self.dragging:
            return
        size = self.widget.winfo_width() if self.horizontal else self.widget.winfo_height()
        coordinate = event.x if self.horizontal else event.y
        fraction = max(0., min(1., (coordinate-9)/max(1, size-18)))
        if not self.horizontal:
            fraction = 1-fraction
        if self.reversed_scale:
            fraction = 1-fraction
        self.change(self.minimum+fraction*(self.maximum-self.minimum))

    def release(self, event):
        self.drag(event)
        self.dragging = False

    def wheel(self, event):
        return self.change(self.variable.get()+(self.maximum-self.minimum)/100*(1 if event.delta > 0 else -1))


class ControllerSurface:
    """Four mixer channels and two selectable deck banks on one hardware-style board."""
    WIDTH, HEIGHT = 1260, 658
    CHANNEL_COLORS = ("#3289ff", "#24d5a0", "#a085ff", "#ffae45")
    PAD_COLORS = ("#ff4b52", "#ffb335", "#4f94ff", "#ffe448", "#29ed69", "#ed4cff", "#ae64ff", "#dc59f7")
    LOOP_BEATS = (.25, .5, 1, 2, 4, 8, 16, 32)
    SAMPLE_NAMES = ("KICK", "SNARE", "HAT", "CLAP", "TOM", "SHAKER", "BELL", "RISE")

    def __init__(self, parent, window, service, owner):
        import base64
        from io import BytesIO
        import tkinter as tk
        from PIL import Image

        self.window, self.service, self.owner = window, service, owner
        self.assets = {name: Image.open(BytesIO(base64.b64decode(data))).convert("RGBA")
                       for name, data in _CONTROLLER_ASSETS.items()}
        self.canvas = tk.Canvas(parent, width=1260, height=530, background="#161617", highlightthickness=0)
        self.canvas.pack(fill="x")
        self.elements, self.controls, self.icon_buttons = [], {}, []
        self.selected = [0, 1]
        self.focus_channel = 0
        self.pad_modes = ["hotcue", "hotcue"]
        self.vinyl = [True, True]
        self.jump_beats = [4, 4]
        self.state = {}
        self.hint = None
        self.last_size = None
        self.resize_id = None
        self.photos = {}
        self.icon_cache = {}
        self.variable_controls = {}
        self.jog_drag = {}
        self.tone_values = [[tk.DoubleVar(value=0) for _ in range(3)] for _ in range(4)]
        self.speed_values = [tk.DoubleVar(value=100) for _ in range(4)]
        self.deck_levels = [tk.DoubleVar(value=100) for _ in range(4)]
        self.trim_values = [tk.DoubleVar(value=0) for _ in range(4)]
        self.filter_values = [tk.DoubleVar(value=0) for _ in range(4)]
        self.dj_widgets = [[] for _ in range(4)]
        self.knobs, self.faders, self.loop_buttons = [], [], [None]*4
        self.bank_titles = [tk.StringVar(value="Load a song"), tk.StringVar(value="Load a song")]
        self.bank_times = [tk.StringVar(value="0:00"), tk.StringVar(value="0:00")]
        self.bank_bpms = [tk.StringVar(value="— BPM"), tk.StringVar(value="— BPM")]
        self.bank_numbers = [tk.StringVar(value="DECK 1"), tk.StringVar(value="DECK 2")]
        self.bank_rates = [tk.DoubleVar(value=100), tk.DoubleVar(value=100)]
        self.bank_pads = [[], []]
        self.mode_buttons = [[], []]
        self.deck_selectors = {}
        self.meters = {}
        self.meter_levels = {}
        self.pitch_faders = []
        self.fx_tone = "FLAT"
        self.fx_sends = [tk.DoubleVar(value=0), tk.DoubleVar(value=0)]
        self.fx_depth = tk.DoubleVar(value=.5)
        self._build_mixer()
        self._build_bank(0)
        self._build_bank(1)
        self.canvas.bind("<Configure>", self.schedule_resize)
        self.canvas.bind("<MouseWheel>", self.canvas_wheel)
        self.configure_binding = self.window.bind("<Configure>", self.schedule_resize, add="+")
        self.canvas.bind("<Destroy>", self.destroy, add="+")
        self.window.after_idle(self.resize)

    def _place(self, widget, x, y, width, height, font_size=None):
        item = self.canvas.create_window(x, y, window=widget, width=width, height=height)
        self.elements.append((item, widget, x, y, width, height, font_size))
        return widget

    def label(self, text, x, y, width=60, height=18, *, size=8, background="#303032", color="#e7e7ea", bold=False):
        import tkinter as tk

        options = {"textvariable": text} if isinstance(text, tk.Variable) else {"text": text}
        widget = tk.Label(self.canvas, **options, bg=background, fg=color,
                          font=("Bahnschrift", size, "bold" if bold else "normal"))
        return self._place(widget, x, y, width, height, size)

    def button(self, name, label, x, y, width, height, command, *, color="#2b2b2e", icon=None, size=8):
        import tkinter as tk

        widget = tk.Button(self.canvas, text=label, command=command, bg=color, fg="#f5f5f7",
                           activebackground="#54545a", activeforeground="#ffffff", relief="raised", bd=1,
                           highlightthickness=1, highlightbackground="#505056", highlightcolor="#399dff",
                           font=("Bahnschrift", size, "bold"), padx=1, pady=0, cursor="hand2", takefocus=True)
        self._place(widget, x, y, width, height, size)
        self.controls[name] = widget
        widget.bind("<Enter>", lambda _event, n=name: self.describe(n))
        widget.bind("<Leave>", self.clear_hint)
        if icon:
            widget.configure(compound="left" if label else "none")
            self.icon_buttons.append((widget, icon, min(width, height)*.55))
        return widget

    def knob(self, name, x, y, variable, low, high, step, command, *, default=0, channel=None, background="#303032"):
        knob = ControllerKnob(self.canvas, self.window, self.assets["knob"], variable, low, high, step, command, default)
        knob.widget.configure(background=background)
        self._place(knob.widget, x, y, 39, 39)
        self.controls[name] = knob.widget
        knob.widget.bind("<Enter>", lambda _event, n=name: self.describe(n))
        knob.widget.bind("<Leave>", self.clear_hint)
        self.knobs.append(knob)
        self.variable_controls[str(variable)] = knob
        if channel is not None:
            self.dj_widgets[channel].append(knob.widget)
        return knob

    def fader(self, name, x, y, width, height, variable, low, high, command, **options):
        fader = ControllerFader(self.canvas, self.window, self.assets["fader"], variable, low, high, command, **options)
        self._place(fader.widget, x, y, width, height)
        self.controls[name] = fader.widget
        fader.widget.bind("<Enter>", lambda _event, n=name: self.describe(n))
        fader.widget.bind("<Leave>", self.clear_hint)
        self.faders.append(fader)
        self.variable_controls[str(variable)] = fader
        return fader

    def _build_mixer(self):
        import tkinter as tk

        for column, channel in enumerate((2, 0, 1, 3)):
            x = 545+column*56
            self.label("TRIM", x, 63, size=7)
            self.knob(f"trim-{channel}", x, 89, self.trim_values[channel], -12, 12, .5,
                      lambda value, c=channel: self.control(c, "trim", value), channel=channel)
            for row, (name, index) in enumerate((("HIGH", 2), ("MID", 1), ("LOW", 0))):
                self.label(name, x, 115+row*43, size=7)
                self.knob(f"eq-{channel}-{name}", x, 140+row*43, self.tone_values[channel][index], -12, 12, .5,
                          lambda _value, c=channel: self.apply_tone(c), channel=channel)
            self.label("FILTER", x, 249, size=7)
            self.knob(f"filter-{channel}", x, 276, self.filter_values[channel], -1, 1, .025,
                      lambda value, c=channel: self.control(c, "filter", value), channel=channel)
            self.label(str(channel+1), x, 432, width=22, size=10, color=self.CHANNEL_COLORS[channel], bold=True)
            self.loop_buttons[channel] = self.button(f"loop-{channel}", "LOOP", x, 409, 44, 20,
                lambda c=channel: self.control(c, "loop"), color="#4d421c", size=7)
            self.fader(f"level-{channel}", x-3, 489, 33, 103, self.deck_levels[channel], 0, 100,
                       lambda value, c=channel: self.service.deck_volume(c, value))
            self.button(f"mute-{channel}", "MUTE", x, 557, 43, 20,
                        lambda c=channel: self.control(c, "mute"), size=7)
            for index, (label, side) in enumerate((("A", -1), ("T", 0), ("B", 1))):
                self.button(f"assign-{channel}-{side}", label, x-15+index*15, 583, 15, 17,
                            lambda c=channel, s=side: self.control(c, "assign", s), size=7)
        self.label("MAIN", 772, 64, width=45, size=7)
        self.master = tk.DoubleVar(value=65)
        self.knob("master", 772, 94, self.master, 0, 100, 1, self.service.volume, default=65)
        self.master_mute = self.button("master-mute", "", 772, 145, 31, 27, self.owner.toggle_mute, icon="speaker-high")
        self.label("SAMPLER", 772, 246, width=58, size=7)
        self.sample_level = tk.DoubleVar(value=80)
        self.sample_knob = self.knob("sample-level", 772, 276, self.sample_level, 0, 100, 1,
                  lambda value: self.service.sample_volume(value), default=80)
        self.label("LOOP BEATS", 488, 64, width=68, size=7)
        self.loop_value = tk.DoubleVar(value=4)
        self.knob("loop-beats", 488, 94, self.loop_value, 0, 7, 1,
                  lambda value: self.control(self.focus_channel, "loop_length", self.LOOP_BEATS[round(value)]), default=4)
        self.label("BEAT JUMP", 488, 246, width=68, size=7)
        self.jump_value = tk.DoubleVar(value=2)
        self.knob("jump-beats", 488, 276, self.jump_value, 0, 5, 1, self.set_jump, default=2)
        self.label("TONE FX", 630, 310, width=180, size=8, bold=True)
        for index, title in enumerate(("BASS", "VOCAL", "AIR", "FLAT")):
            self.button("preset-"+title.lower(), title, 545+index*56, 329, 49, 19,
                        lambda title=title: self.preset(title), color="#243448", size=7)
        for bank, x in enumerate((559, 699)):
            self.fader(f"fx-send-{bank}", x, 367, 48, 35, self.fx_sends[bank], 0, 1,
                       lambda _value: self.apply_effect(), horizontal=False)
            self.label("A FX" if bank == 0 else "B FX", x, 390, width=48, size=7)
        self.knob("fx-depth", 630, 367, self.fx_depth, 0, 1, .025,
                  lambda _value: self.apply_effect(), default=.5)
        self.label("DEPTH", 630, 394, width=48, size=7)
        self.crossfade = tk.DoubleVar(value=0)
        self.crossfader = self.fader("crossfade", 630, 618, 192, 29, self.crossfade, 0, 1,
                                     self.crossfade_changed, horizontal=True)
        self.label("A", 516, 617, width=15, size=10, bold=True)
        self.label("B", 745, 617, width=15, size=10, bold=True)
        self.label("CALE AI  /  LOCAL MIXER", 630, 648, width=280, size=9, bold=True)
        self.button("stop-all", "STOP", 772, 616, 42, 29, self.service.stop, color="#4b2629", size=7)
        self.button("auto-dj", "AUTO DJ", 488, 617, 53, 29, self.toggle_auto, color="#17394e", size=7)

    def _build_bank(self, bank):
        offset = 0 if bank == 0 else 810
        background = "#1d1d1f"
        self.label("CALE" if bank == 0 else "AI / RADIO", offset+196, 52, width=218,
                   height=32, size=22, background=background, bold=True)
        self.button(f"load-{bank}", "LOAD", offset+58, 55, 70, 27,
                    lambda b=bank: self.load_file(b), icon="folder-open")
        self.button(f"saved-{bank}", "SONG", offset+335, 55, 56, 27, lambda b=bank: self.load_saved(b), size=8)
        self.button(f"eject-{bank}", "", offset+398, 55, 27, 27,
                    lambda b=bank: self.bank_control(b, "eject"), icon="eject")
        for index, channel in enumerate((bank, bank+2)):
            self.deck_selectors[channel] = self.button(f"deck-{channel}", str(channel+1), offset+407, 143+index*40,
                29, 30, lambda b=bank, c=channel: self.select_deck(b, c), color=self.CHANNEL_COLORS[channel], size=11)
        self.button(f"vinyl-{bank}", "VINYL", offset+403, 220, 48, 24,
                    lambda b=bank: self.toggle_vinyl(b), color="#194777", size=7)
        self.pitch_faders.append(self.fader(f"pitch-{bank}", offset+400, 330, 33, 163, self.bank_rates[bank], 80, 120,
                   lambda value, b=bank: self.pitch(b, value), reversed_scale=True, background=background))
        self.label("TEMPO", offset+400, 426, width=57, size=7, background=background)
        self.label(self.bank_numbers[bank], offset+198, 249, width=64, size=7, background="#000000", bold=True)
        self.label(self.bank_bpms[bank], offset+198, 270, width=85, height=25, size=13, background="#000000", bold=True)
        self.label(self.bank_times[bank], offset+198, 297, width=72, size=10, background="#000000")
        self.label(self.bank_titles[bank], offset+196, 455, width=327, height=19, size=9, background=background)
        self.button(f"sync-{bank}", "SYNC", offset+46, 479, 60, 28,
                    lambda b=bank: self.bank_control(b, "sync"), color="#1a3d5a", size=8)
        self.button(f"cue-{bank}", "CUE", offset+46, 526, 60, 34,
                    lambda b=bank: self.bank_control(b, "cue"), color="#303032", size=10)
        cue = self.controls[f"cue-{bank}"]
        cue.bind("<Shift-ButtonPress-1>", lambda _event: "break")
        cue.bind("<Shift-ButtonRelease-1>", lambda _event, b=bank: self.save_cue(b))
        self.button(f"play-{bank}", "", offset+46, 582, 60, 51,
                    lambda b=bank: self.bank_control(b, "toggle"), icon="play", color="#183a25")
        for index, (title, mode) in enumerate((("HOT CUE", "hotcue"), ("LOOP", "loop"), ("ROLL", "roll"), ("SAMPLER", "sample"))):
            widget = self.button(f"mode-{bank}-{mode}", title, offset+113+index*57, 488, 53, 23,
                lambda b=bank, mode=mode: self.set_pad_mode(b, mode), color="#273a52" if index == 0 else "#303032", size=7)
            self.mode_buttons[bank].append((mode, widget))
        for slot, color in enumerate(self.PAD_COLORS):
            x, y = offset+116+(slot%4)*56, 540+(slot//4)*46
            pad = self.button(f"pad-{bank}-{slot}", str(slot+1), x, y, 47, 35,
                lambda b=bank, slot=slot: self.pad(b, slot), color=color, size=9)
            pad.configure(fg="#111114", activebackground="#f5f5ff", activeforeground="#111114")
            pad.bind("<ButtonPress-1>", lambda _event, b=bank, slot=slot: self.pad_press(b, slot))
            pad.bind("<ButtonRelease-1>", lambda event, b=bank, slot=slot: self.pad_release(b, slot, event))
            pad.bind("<KeyPress-space>", lambda _event, b=bank, slot=slot: self.pad_key(b, slot, True))
            pad.bind("<KeyRelease-space>", lambda _event, b=bank, slot=slot: self.pad_key(b, slot, False))
            self.bank_pads[bank].append(pad)
        for name, title, x, y, action, value in (
            ("in", "IN", 355, 532, "loop_in", None), ("out", "OUT", 405, 532, "loop_out", None),
            ("back", "− BEAT", 355, 579, "beat_jump", -1), ("forward", "+ BEAT", 405, 579, "beat_jump", 1),
            ("bpm-half", "BPM ÷2", 355, 487, "bpm_scale", .5), ("bpm-double", "BPM ×2", 405, 487, "bpm_scale", 2)):
            self.button(f"{name}-{bank}", title, offset+x, y, 44, 25,
                lambda b=bank, a=action, v=value: self.bank_control(b, a, v), size=7)

    def schedule_resize(self, _event=None):
        if self.resize_id is None:
            self.resize_id = self.window.after_idle(self.resize)

    def resize(self):
        from PIL import Image, ImageTk

        self.resize_id = None
        if not self.canvas.winfo_exists():
            return
        width = max(1, self.canvas.winfo_width())
        if width < 100:
            return
        top = self.canvas.winfo_rooty()-self.window.winfo_rooty()
        siblings = self.canvas.master.pack_slaves()
        reserve = 12
        for widget in siblings[siblings.index(self.canvas)+1:]:
            packed = widget.pack_info()
            padding = packed.get("pady", 0)
            padding = sum(map(int, padding)) if isinstance(padding, tuple) else 2*int(padding)
            reserve += widget.winfo_reqheight()+padding+2*int(packed.get("ipady", 0))
        available = max(270, self.window.winfo_height()-top-reserve)
        scale = min((width-8)/self.WIDTH, available/self.HEIGHT)
        geometry = (width, round(scale, 4))
        if geometry == self.last_size:
            return
        self.last_size = geometry
        self.scale = scale
        self.offset = (width-self.WIDTH*scale)/2
        self.canvas.configure(height=round(self.HEIGHT*scale))
        self.canvas.delete("chassis")
        for left, right, color in ((0, 449, "#1d1d1f"), (449, 813, "#303032"), (813, 1260, "#1d1d1f")):
            self.canvas.create_rectangle(self.offset+left*scale, 0, self.offset+right*scale, self.HEIGHT*scale,
                                         fill=color, outline="#4d4d52", tags="chassis")
        jog_size = max(100, round(356*scale))
        self.photos["jog"] = ImageTk.PhotoImage(self.assets["jog"].resize((jog_size, jog_size), Image.Resampling.LANCZOS), master=self.window)
        for bank, x in enumerate((198, 1008)):
            tag = f"jog-{bank}"
            self.canvas.create_image(self.offset+x*scale, 274*scale, image=self.photos["jog"], tags=("chassis", tag))
            self.canvas.tag_bind(tag, "<ButtonPress-1>", lambda event, b=bank: self.jog_press(b, event))
            self.canvas.tag_bind(tag, "<B1-Motion>", lambda event, b=bank: self.jog_move(b, event))
            self.canvas.tag_bind(tag, "<ButtonRelease-1>", lambda event, b=bank: self.jog_release(b, event))
        for item, widget, x, y, element_width, height, font_size in self.elements:
            self.canvas.coords(item, self.offset+x*scale, y*scale)
            self.canvas.itemconfigure(item, width=max(1, round(element_width*scale)), height=max(1, round(height*scale)))
            if font_size is not None:
                weight = "bold" if widget.winfo_class() == "Button" or "bold" in str(widget.cget("font")) else "normal"
                # Pixel sizes track the physical board; a fixed point minimum
                # clips hardware labels on shorter Windows displays.
                widget.configure(font=("Bahnschrift", -max(7, round(font_size*scale*1.34)), weight))
        for widget, name, icon_size in self.icon_buttons:
            size = max(10, round(icon_size*scale))
            photo = self.cached_icon(name, size)
            widget.configure(image=photo)
            widget.controller_icon = photo
        self.canvas.tag_lower("chassis")
        self.canvas.delete("meters")
        self.meters.clear()
        for column, channel in enumerate((2, 0, 1, 3)):
            x = 565+column*56
            self.meters[channel] = [self.canvas.create_rectangle(self.offset+(x-1)*scale, (533-index*7)*scale,
                self.offset+(x+3)*scale, (537-index*7)*scale, fill="#252527", outline="", tags="meters") for index in range(12)]
        self.meter_levels.clear()
        self.refresh(self.state)

    def destroy(self, event):
        if event.widget is self.canvas:
            if self.resize_id is not None:
                self.window.after_cancel(self.resize_id)
                self.resize_id = None
            self.window.unbind("<Configure>", self.configure_binding)

    def set_value(self, variable, value):
        control = self.variable_controls.get(str(variable))
        if not (control and control.dragging) and variable.get() != value:
            variable.set(value)

    def deck(self, channel):
        decks = self.state.get("studio_decks", self.state.get("decks", []))
        return decks[channel] if channel < len(decks) else {"status": "Empty", "elapsed": 0, "duration": 0}

    def control(self, channel, action, value=None):
        self.focus_channel = channel
        self.service.dj_control(channel, action, value)

    def save_cue(self, bank):
        self.bank_control(bank, "cue_set")
        return "break"

    def bank_control(self, bank, action, value=None):
        channel = self.selected[bank]
        if self.deck(channel).get("status", "Empty") == "Empty":
            self.owner.dj_text.set("Load a local file or a finished song into this deck first.")
            return
        if action == "beat_jump":
            value *= self.jump_beats[bank]
        if action in {"speed", "sync"}:
            ready = self.state.get("dj_controls_ready", [])
            if channel >= len(ready) or not ready[channel]:
                self.owner.dj_text.set("Tempo controls become available when local DJ sound is ready.")
                return
        self.control(channel, action, value)

    def apply_tone(self, channel):
        self.control(channel, "tone", [value.get() for value in self.tone_values[channel]])

    def select_deck(self, bank, channel):
        self.selected[bank] = channel
        self.focus_channel = channel
        self.refresh(self.state)

    def set_jump(self, value):
        bank = 0 if self.focus_channel in (0, 2) else 1
        self.jump_beats[bank] = (1, 2, 4, 8, 16, 32)[round(value)]

    def pitch(self, bank, value):
        channel = self.selected[bank]
        self.speed_values[channel].set(value)
        self.control(channel, "speed", value/100)

    def crossfade_changed(self, value):
        self.owner.auto.set(False)
        self.service.studio_crossfade(value)

    def toggle_auto(self):
        enabled = not self.state.get("auto_blend", True) or self.state.get("studio_mode", False)
        self.owner.auto.set(enabled)
        self.service.auto_mix(enabled)

    def preset(self, title):
        self.fx_tone = title
        self.apply_effect()

    def apply_effect(self):
        tone = {"BASS": (6, 0, -2), "VOCAL": (-3, 5, 1), "AIR": (-2, 0, 6), "FLAT": (0, 0, 0)}[self.fx_tone]
        for channel in range(4):
            self.service.dj_control(channel, "effect", {"tone": tone, "depth": self.fx_sends[channel % 2].get()*self.fx_depth.get()})
        for title in ("BASS", "VOCAL", "AIR", "FLAT"):
            self.controls["preset-"+title.lower()].configure(bg="#286098" if title == self.fx_tone else "#243448")

    def describe(self, name):
        descriptions = {
            "cue": "CUE returns to the saved cue and pauses. Shift-click saves the current position.",
            "pad": "HOT CUE: click to save/recall; Shift-click clears. LOOP: select length. ROLL: hold. SAMPLER: play a hit.",
            "sync": "Match analyzed tempo and beat phase to the other playing deck (within ±20%).",
            "vinyl": "VINYL seeks while dragging the platter. BEND nudges tempo until you release it.",
            "fx": "Tone FX sends apply to left/right bank channels. DEPTH blends the selected tone effect with your EQ.",
            "preset": "Choose BASS, VOCAL, AIR or FLAT; raise an FX send and DEPTH to hear it.",
            "assign": "A and B follow the crossfader. T means THRU, independent of the crossfader.",
            "level": "Channel volume. Drag the fader or use arrow keys; a manual move takes over from Auto-DJ.",
            "trim": "Channel input trim from −12 to +12 dB. Double-click a knob to reset; Shift-drag adjusts finely.",
            "filter": "Turn left to cut high frequencies; right to cut lows. Center restores the full spectrum.",
            "load": "Load a local audio file into this bank's selected deck.",
            "saved": "Load the selected Your Songs track into this bank's selected deck.",
            "deck": "Switch this bank's platter, tempo, transport and pads between its two decks.",
            "loop": "LOOP toggles a beat loop. IN/OUT mark custom boundaries. LOOP BEATS targets the last touched deck.",
            "jump": "BEAT JUMP chooses jump length for the last touched bank. The −/+ buttons and wheel move that many beats.",
            "auto": "Return to automatic radio playback; manual decks stop when the station starts.",
        }
        key = name.split("-")[0]
        self.hint = descriptions.get(key)
        if self.hint:
            self.owner.dj_text.set(self.hint)

    def clear_hint(self, _event=None):
        if self.hint:
            self.hint = None
            self.owner.dj_text.set(self.state.get("dj_setup", {}).get("message", ""))

    def load_file(self, bank):
        from tkinter import filedialog

        path = filedialog.askopenfilename(parent=self.window, title="Load a song into deck "+str(self.selected[bank]+1),
            filetypes=[("Audio", "*.wav *.mp3 *.flac *.m4a *.ogg"), ("All files", "*.*")])
        if path:
            self.service.load_deck(self.selected[bank], path)

    def load_saved(self, bank):
        identity = self.owner.song_choices.get(self.owner.song_picker.get())
        if identity:
            self.service.load_song(self.selected[bank], identity)
        else:
            self.owner.dj_text.set("Choose a finished track under Your Songs, then press SONG on a deck.")

    def set_pad_mode(self, bank, mode):
        self.pad_modes[bank] = mode
        self.refresh(self.state)

    def pad(self, bank, slot, clear=False):
        channel = self.selected[bank]
        mode = self.pad_modes[bank]
        if mode == "sample":
            self.service.sample(channel, slot)
        elif mode == "hotcue":
            self.bank_control(bank, "hotcue", {"slot": slot, "clear": clear})
        elif mode == "loop":
            self.bank_control(bank, "loop_length", self.LOOP_BEATS[slot])
            if channel not in self.state.get("dj_loops", []):
                self.bank_control(bank, "loop")

    def pad_press(self, bank, slot):
        if self.pad_modes[bank] == "roll":
            self.bank_control(bank, "roll_on", self.LOOP_BEATS[slot])

    def pad_release(self, bank, slot, event):
        if self.pad_modes[bank] == "roll":
            self.bank_control(bank, "roll_off")
        elif event.state & 1 and self.pad_modes[bank] == "hotcue":
            self.pad(bank, slot, clear=True)
            self.bank_pads[bank][slot].configure(relief="raised")
            return "break"

    def pad_key(self, bank, slot, pressed):
        if self.pad_modes[bank] == "roll":
            self.bank_control(bank, "roll_on" if pressed else "roll_off", self.LOOP_BEATS[slot] if pressed else None)
        elif not pressed:
            self.pad(bank, slot)
        return "break"

    def toggle_vinyl(self, bank):
        self.vinyl[bank] = not self.vinyl[bank]
        self.controls[f"vinyl-{bank}"].configure(text="VINYL" if self.vinyl[bank] else "BEND")

    def jog_press(self, bank, event):
        channel = self.selected[bank]
        x = self.offset+(198 if bank == 0 else 1008)*self.scale
        y = 274*self.scale
        self.jog_drag[bank] = {"angle": math.atan2(event.y-y, event.x-x), "position": self.deck(channel).get("elapsed", 0),
                               "speed": self.speed_values[channel].get()/100, "sent": 0.}
        self.focus_channel = channel

    def jog_move(self, bank, event):
        drag = self.jog_drag.get(bank)
        if drag is None:
            return
        x = self.offset+(198 if bank == 0 else 1008)*self.scale
        angle = math.atan2(event.y-274*self.scale, event.x-x)
        difference = (angle-drag["angle"]+math.pi) % (2*math.pi)-math.pi
        drag["angle"] = angle
        drag["position"] += difference*4/(2*math.pi)
        if time.monotonic()-drag["sent"] < .04:
            return
        drag["sent"] = time.monotonic()
        if self.vinyl[bank]:
            self.bank_control(bank, "seek", max(0., drag["position"]))
        else:
            self.bank_control(bank, "speed", max(.8, min(1.2, drag["speed"]+difference*.08)))

    def jog_release(self, bank, _event):
        drag = self.jog_drag.pop(bank, None)
        if drag is not None:
            self.bank_control(bank, "seek" if self.vinyl[bank] else "speed",
                              max(0., drag["position"]) if self.vinyl[bank] else drag["speed"])

    def jog_wheel(self, bank, event):
        direction = 1 if event.delta > 0 else -1
        self.bank_control(bank, "beat_jump", direction)
        return "break"

    def canvas_wheel(self, event):
        if not hasattr(self, "scale"):
            return
        x = (event.x-self.offset)/self.scale
        y = event.y/self.scale
        if 95 <= y <= 453 and (20 <= x <= 376 or 830 <= x <= 1186):
            return self.jog_wheel(0 if x < 449 else 1, event)

    def refresh(self, state):
        self.state = state
        settings = state.get("dj_settings", [])
        ready = state.get("dj_controls_ready", [])
        for channel in range(4):
            for widget in self.dj_widgets[channel]:
                widget.configure(state="normal" if channel < len(ready) and ready[channel] else "disabled")
            if channel < len(settings):
                values = settings[channel]
                for variable, value in zip(self.tone_values[channel], values.get("tone", (0, 0, 0)), strict=True):
                    self.set_value(variable, value)
                self.set_value(self.speed_values[channel], values.get("speed", 1)*100)
                self.set_value(self.trim_values[channel], values.get("trim", 0))
                self.set_value(self.filter_values[channel], values.get("filter", 0))
            looping = channel in state.get("dj_loops", [])
            self.loop_buttons[channel].configure(bg="#a28a25" if looping else "#4d421c")
            self.controls[f"mute-{channel}"].configure(bg="#94532c" if channel in state.get("channel_muted", []) else "#303032")
            sides = state.get("channel_sides", [-1, 1, -1, 1])
            for side in (-1, 0, 1):
                self.controls[f"assign-{channel}-{side}"].configure(bg="#387d99" if sides[channel] == side else "#303032")
            deck = self.deck(channel)
            self.set_value(self.deck_levels[channel], deck.get("level", 100))
            peaks = deck.get("peaks", [])
            position = min(1., max(0., deck.get("elapsed", 0)/max(1., deck.get("duration", 0))))
            level = peaks[min(len(peaks)-1, int(position*len(peaks)))] if peaks else 0.
            gain = level*deck.get("gain", 0)/100
            count = round(max(0., min(1., (20*math.log10(max(1e-5, gain))+42)/42))*12) if deck.get("status") == "Playing" else 0
            if self.meter_levels.get(channel) != count:
                for index, item in enumerate(self.meters.get(channel, [])):
                    color = "#ef5349" if index > 10 else "#f3ca38" if index > 8 else "#34d168"
                    self.canvas.itemconfigure(item, fill=color if index < count else "#252527")
                self.meter_levels[channel] = count
        for bank, channel in enumerate(self.selected):
            deck = self.deck(channel)
            self.bank_titles[bank].set(str(deck.get("title", "Load a song"))[:46] if deck.get("status") != "Empty" else "Load a song")
            elapsed = int(deck.get("elapsed", 0))
            self.bank_times[bank].set(f"{elapsed//60:02d}:{elapsed%60:02d}")
            bpm = deck.get("bpm")
            self.bank_bpms[bank].set(f"≈ {bpm*self.speed_values[channel].get()/100:.1f}" if bpm else "— BPM")
            self.bank_numbers[bank].set("DECK "+str(channel+1))
            for candidate in (bank, bank+2):
                self.deck_selectors[candidate].configure(bg=self.CHANNEL_COLORS[candidate] if candidate == channel else "#303032")
            if not self.pitch_faders[bank].dragging:
                self.set_value(self.bank_rates[bank], self.speed_values[channel].get())
            self.pitch_faders[bank].widget.configure(state="normal" if channel < len(ready) and ready[channel] else "disabled")
            playing = deck.get("status") == "Playing"
            icon = "pause" if playing else "play"
            size = max(12, round(29*getattr(self, "scale", 1.)))
            photo = self.cached_icon(icon, size)
            self.controls[f"play-{bank}"].configure(image=photo, bg="#245b38" if playing else "#183a25")
            self.controls[f"play-{bank}"].controller_icon = photo
            for mode, button in self.mode_buttons[bank]:
                button.configure(bg="#286098" if mode == self.pad_modes[bank] else "#303032")
            cues = state.get("hot_cues", [[None]*8 for _ in range(4)])[channel]
            for slot, pad in enumerate(self.bank_pads[bank]):
                mode = self.pad_modes[bank]
                label = self.SAMPLE_NAMES[slot] if mode == "sample" else str(self.LOOP_BEATS[slot])+"B" if mode in {"loop", "roll"} else str(slot+1)
                active = ((mode == "hotcue" and cues[slot] is not None)
                          or (mode == "roll" and channel in state.get("active_rolls", []))
                          or (mode == "sample" and (channel, slot) in state.get("active_samples", [])))
                pad.configure(text=label, relief="sunken" if active else "raised")
        if not self.crossfader.dragging:
            self.set_value(self.crossfade, state.get("crossfade", 0))
        if not self.sample_knob.dragging:
            self.set_value(self.sample_level, state.get("sample_volume", 80))
        self.set_value(self.master, state.get("master_volume", self.master.get()))
        icon = "speaker-slash" if self.master.get() == 0 else "speaker-high"
        size = max(10, round(16*getattr(self, "scale", 1.)))
        photo = self.cached_icon(icon, size)
        self.master_mute.configure(image=photo, bg="#6d2b2b" if self.master.get() == 0 else "#303032")
        self.master_mute.controller_icon = photo
        lengths = state.get("loop_lengths", [4.] * 4)
        length = lengths[self.focus_channel]
        self.set_value(self.loop_value, min(range(8), key=lambda index: abs(self.LOOP_BEATS[index]-length)))
        for name in ("preset-bass", "preset-vocal", "preset-air", "preset-flat", "fx-send-0", "fx-send-1", "fx-depth"):
            self.controls[name].configure(state="normal" if any(ready) else "disabled")
        self.draw_lcd()

    def cached_icon(self, name, size):
        from PIL import Image, ImageTk

        identity = (name, size)
        if identity not in self.icon_cache:
            # Window resizing must not accumulate every historical icon size.
            if len(self.icon_cache) > 24:
                self.icon_cache.clear()
            self.icon_cache[identity] = ImageTk.PhotoImage(
                self.assets[name].resize((size, size), Image.Resampling.LANCZOS), master=self.window)
        return self.icon_cache[identity]

    def draw_lcd(self):
        """Real track progress and cached audio amplitude, never random decoration."""
        if not hasattr(self, "scale"):
            return
        self.canvas.delete("lcd")
        scale = self.scale
        for bank, x in enumerate((198, 1008)):
            deck = self.deck(self.selected[bank])
            progress = max(0., min(1., deck.get("elapsed", 0)/max(.01, deck.get("duration", 0))))
            cx, cy, radius = self.offset+x*scale, 274*scale, 55*scale
            self.canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, outline="#184170", width=max(1, 2*scale), tags="lcd")
            if progress:
                self.canvas.create_arc(cx-radius, cy-radius, cx+radius, cy+radius, start=90, extent=-360*progress,
                                       style="arc", outline="#3d94ff", width=max(1, 3*scale), tags="lcd")
            peaks = deck.get("peaks", [])
            peak_max = max(.03, max(peaks, default=0.))
            for index in range(32 if peaks else 0):
                start = int(index*len(peaks)/32)
                end = max(start+1, int((index+1)*len(peaks)/32))
                amplitude = max(peaks[start:end], default=0.)/peak_max
                px = cx+(index-15.5)*2*scale
                height = max(1., amplitude*12)*scale
                self.canvas.create_line(px, 316*scale-height/2, px, 316*scale+height/2,
                                        fill="#27c3b1" if index/32 > progress else "#378fff", width=max(1, scale), tags="lcd")


class Controls:
    """A compact radio deck with real playback timing and local usage receipts."""
    def __init__(self, parent, window, service, on_make_song=None):
        import tkinter as tk
        from tkinter import ttk
        self.window, self.service, self.on_make_song = window, service, on_make_song
        self.mode = "radio"
        self.closed = False
        self.previous_volume = 65
        self.bg, self.panel, self.ink, self.muted = "#1d1d1f", "#303032", "#f1f1f4", "#aaaab3"
        self.cyan, self.coral = "#16d7e7", "#ff7482"
        self.frame = tk.Frame(parent, bg=self.bg, padx=14, pady=8)
        self.frame.pack(fill="both", expand=True)
        self.frame.bind("<Destroy>", self._destroy, add="+")
        style = ttk.Style(window)
        for name, color in (("Radio", self.cyan), ("Radio.Coral", self.coral)):
            style.configure(name+".Horizontal.TProgressbar", background=color, troughcolor="#26323d", thickness=4, borderwidth=0)
        style.configure("Radio.TCombobox", fieldbackground=self.panel, foreground=self.ink,
                        background="#24313b", arrowcolor=self.cyan, borderwidth=0, padding=6)
        style.map("Radio.TCombobox", fieldbackground=[("readonly", self.panel)], foreground=[("readonly", self.ink)])
        style.configure("Radio.TEntry", fieldbackground=self.panel, foreground=self.ink, insertcolor=self.ink)
        style.configure("Radio.TCheckbutton", background=self.bg, foreground=self.ink)
        style.configure("Radio.TButton", padding=(12, 8), background="#26323d", foreground=self.ink)
        top = tk.Frame(self.frame, bg=self.bg)
        top.pack(fill="x")
        self.radio_tab = self._button(top, "AI Radio", lambda: self.set_mode("radio"), accent=self.cyan)
        self.radio_tab.pack(side="left", padx=(0, 8))
        self.song_tab = self._button(top, "Make a song", lambda: self.set_mode("song"))
        self.song_tab.pack(side="left")
        self.auto = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="Auto-DJ blend", variable=self.auto, command=lambda: self.service.auto_mix(self.auto.get()),
            bg=self.bg, fg=self.ink, activebackground=self.bg, activeforeground=self.cyan, selectcolor="#17404a",
            font=("Segoe UI", 10), relief="flat", cursor="hand2").pack(side="right")
        prompt_row = tk.Frame(self.frame, bg=self.bg)
        prompt_row.pack(fill="x", pady=(6, 6))
        self.prompt = tk.Entry(prompt_row, bg="#1b242e", fg=self.ink, insertbackground=self.cyan,
            font=("Segoe UI", 12), relief="flat", highlightthickness=1, highlightbackground="#384957", highlightcolor=self.cyan)
        self.prompt.pack(side="left", fill="x", expand=True, ipady=5)
        self.prompt.bind("<Return>", lambda _: self.generate())
        self.prompt.bind("<FocusIn>", self._clear_prompt_hint)
        self.prompt.bind("<FocusOut>", self._show_prompt_hint)
        self._show_prompt_hint()
        self.generate_button = self._button(prompt_row, "Start station", self.generate, accent=self.coral)
        self.generate_button.pack(side="left", fill="y", padx=(12, 0))
        # Song acknowledgements belong to the song maker. The radio's periodic
        # status updates must not erase a launch result or an actionable error.
        self.song_notice = tk.StringVar(value="Describe your song, then choose Make song.")
        self.song_notice_label = tk.Label(self.frame, textvariable=self.song_notice, bg=self.bg,
            fg=self.cyan, anchor="w", justify="left", wraplength=900, font=("Segoe UI", 10))
        station_row = tk.Frame(self.frame, bg=self.bg)
        station_row.pack(fill="x", pady=(0, 8))
        self.choice = ttk.Combobox(station_row, state="readonly", width=22, style="Radio.TCombobox", font=("Segoe UI", 10, "bold"))
        self.choice.pack(side="left")
        self.choice.bind("<<ComboboxSelected>>", lambda _: self.tune())
        self._button(station_row, "New station", self.station_dialog).pack(side="left", padx=(8, 0))
        self._button(station_row, "Edit", lambda: self.station_dialog(True)).pack(side="left", padx=8)
        self.vocals = tk.BooleanVar(value=True)
        self.vocals_toggle = tk.Checkbutton(station_row, text="Vocals", variable=self.vocals, bg=self.bg, fg=self.muted,
            selectcolor=self.panel, activebackground=self.bg, activeforeground=self.ink, relief="flat")
        self.vocals_toggle.pack(side="right")
        self.badge = tk.Label(station_row, text="READY", fg=self.cyan, bg="#10313a", padx=10, pady=5, font=("Segoe UI", 9, "bold"))
        self.badge.pack(side="right", padx=12)
        self.controller = ControllerSurface(self.frame, window, service, self)
        self.dj_widgets = self.controller.dj_widgets
        self.tone_values = self.controller.tone_values
        self.speed_values = self.controller.speed_values
        self.deck_levels = self.controller.deck_levels
        self.loop_buttons = self.controller.loop_buttons
        self.volume = self.controller.master
        self.mute = self.controller.master_mute
        self.crossfade = self.controller.crossfade
        self.crossfader = self.controller.crossfader.widget
        self.waves = tk.Canvas(self.frame, bg="#080d13", height=60, highlightthickness=1, highlightbackground="#26333e")
        self.waves.pack(fill="x")
        self.last_state = None
        self.wave_rows = {}
        self.waves.bind("<Configure>", lambda _: self.draw_waveforms(self.last_state or {}))
        queue_row = tk.Frame(self.frame, bg=self.panel, padx=12, pady=4)
        queue_row.pack(fill="x", pady=(5, 0))
        self.text = tk.StringVar(value="Choose a station. Tracks are created on your PC.")
        self.queue_title = tk.StringVar(value="NEXT IN RADIO")
        tk.Label(queue_row, textvariable=self.queue_title, bg=self.panel, fg=self.ink, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 16))
        tk.Label(queue_row, textvariable=self.text, bg=self.panel, fg=self.muted, font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x", expand=True)
        self.next = self._button(queue_row, "Mix next", service.skip)
        self.next.pack(side="right")
        footer = tk.Frame(self.frame, bg=self.bg)
        footer.pack(fill="x", pady=(6, 0))
        self.metrics = []
        for title in ("TRACKS MADE", "RADIO TOKENS", "TOTAL LOCAL TOKENS"):
            value = tk.StringVar(value="—")
            tk.Label(footer, text=title, fg=self.muted, bg=self.bg, font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 8))
            tk.Label(footer, textvariable=value, fg=self.ink, bg=self.bg, font=("Consolas", 11, "bold")).pack(side="left", padx=(0, 24))
            self.metrics.append(value)
        self.kind = tk.StringVar(value="LOCAL AUDIO · NO ACCOUNTS")
        tk.Label(footer, textvariable=self.kind, fg=self.cyan, bg=self.bg, font=("Segoe UI", 8)).pack(side="right")
        library=tk.Frame(self.frame,bg=self.bg);library.pack(fill="x",pady=(8,0))
        tk.Label(library,text="YOUR SONGS",bg=self.bg,fg=self.muted,font=("Segoe UI",8,"bold")).pack(side="left",padx=(0,8))
        self.song_choices={}
        self.song_picker=ttk.Combobox(library,state="readonly",style="Radio.TCombobox",width=28)
        self.song_picker.pack(side="left",fill="x",expand=True)
        self._button(library,"Play / mix",self.play_finished_song).pack(side="left",padx=(8,0))
        self._button(library,"Reset mixer",self.reset_mixer).pack(side="left",padx=(8,0))
        self.dj_retry=self._button(library,"Fix DJ sound",lambda:getattr(self.service,"enable_dj",lambda:None)())
        self.dj_text=tk.StringVar(value="Preparing DJ sound in the background")
        tk.Label(self.frame,textvariable=self.dj_text,bg=self.bg,fg=self.muted,anchor="w",font=("Segoe UI",8)).pack(fill="x",pady=(4,0))
        self.bar = ttk.Progressbar(self.frame, maximum=100, style="Radio.Horizontal.TProgressbar")
        self.refresh_stations()
        self.frame.bind("<Map>", lambda _: self.refresh_view())
        self.poll()
        getattr(service,"enable_dj",lambda:None)()
        service.start()

    def apply_dj_slider(self,channel,rate):
        if rate:self.service.dj_control(channel,"speed",self.speed_values[channel].get()/100)
        else:self.service.dj_control(channel,"tone",[value.get() for value in self.tone_values[channel]])

    def reset_mixer(self):
        for channel in range(4):
            for value in self.tone_values[channel]:value.set(0)
            self.speed_values[channel].set(100)
            self.service.dj_control(channel,"tone",(0,0,0))
            self.service.dj_control(channel,"speed",1.)
            self.service.dj_control(channel, "trim", 0.)
            self.service.dj_control(channel, "filter", 0.)
        for send in self.controller.fx_sends:
            send.set(0.)
        self.controller.fx_depth.set(.5)
        self.controller.preset("FLAT")

    def play_finished_song(self):
        job_id=self.song_choices.get(self.song_picker.get())
        if job_id:self.service.play_song(job_id)
        else:self.dj_text.set("Finished songs appear here automatically. Make a song above first.")

    def _button(self, parent, text, command, accent=None):
        import tkinter as tk
        return tk.Button(parent, text=text, command=command, bg=accent or "#24313c", fg="#071017" if accent else self.ink,
            activebackground="#e7fafe" if accent else "#344858", activeforeground="#09121a" if accent else self.ink,
            font=("Segoe UI", 10, "bold"), relief="flat", borderwidth=0, padx=13, pady=6, cursor="hand2",
            highlightthickness=1, highlightbackground=accent or "#3b4a58", takefocus=True)

    def _destroy(self, event):
        if event.widget is self.frame:
            self.closed = True
            if getattr(self, "poll_id", None):
                self.window.after_cancel(self.poll_id)

    def set_mode(self, mode):
        self.mode = mode
        self.radio_tab.configure(bg=self.cyan if mode=="radio" else "#24313c", fg="#071017" if mode=="radio" else self.ink)
        self.song_tab.configure(bg=self.cyan if mode=="song" else "#24313c", fg="#071017" if mode=="song" else self.ink)
        self.generate_button.configure(text="Make song" if mode=="song" else "Start station",
            state="disabled" if mode=="song" and self.on_make_song is None else "normal")
        if mode == "song":
            self.vocals_toggle.pack_forget()
            self.song_notice_label.pack(fill="x", before=self.choice.master, pady=(0, 10))
        else:
            self.vocals_toggle.pack(side="right", before=self.badge)
            self.song_notice_label.pack_forget()
        self._clear_prompt_hint()
        self.prompt.focus_set()

    def _clear_prompt_hint(self, _event=None):
        if getattr(self, "hint_visible", False):
            self.prompt.delete(0, "end")
            self.hint_visible = False
            self.prompt.configure(fg=self.ink)

    def _show_prompt_hint(self, _event=None):
        if not self.prompt.get():
            self.hint_visible = True
            self.prompt.insert(0, "Describe your next song..." if self.mode=="song" else "Describe your station: style, mood, vocals...")
            self.prompt.configure(fg=self.muted)

    def generate(self):
        prompt = "" if getattr(self, "hint_visible", False) else self.prompt.get().strip()
        if not prompt:
            self.text.set("Describe what you want to hear first.")
            if self.mode == "song":
                self.song_notice.set("Describe what you want to hear first.")
            self.prompt.focus_set()
            return
        if self.mode == "song":
            if self.on_make_song is None:
                self.text.set("Song creation is not connected here. Your description is kept.")
                self.song_notice.set(self.text.get())
                return
            if self.on_make_song is not None:
                try:
                    started = self.on_make_song(prompt)
                    if started is False:
                        self.text.set("Song did not start. Your description is still here so you can retry.")
                        self.song_notice.set(self.text.get())
                        return
                except Exception as error:
                    self.text.set("Song could not start: "+str(error)[:100])
                    self.song_notice.set(self.text.get())
                    return
                self.prompt.delete(0, "end")
                self.song_notice.set("Song started. Follow its progress in Home → My tasks.")
        else:
            try:
                self.service.station_prompt(prompt[:45], prompt, not self.vocals.get())
            except ValueError as error:
                self.text.set(str(error))
                return
            self.text.set("Starting your new station in the background...")

    def deck_action(self, channel):
        deck = (self.last_state or {}).get("decks", [{}, {}])[channel]
        if deck.get("status") == "Cued":
            self.service.skip()
        else:
            self.toggle()

    def set_crossfade(self, value):
        self.auto.set(False)
        self.service.crossfade(value)

    def draw_waveforms(self, state):
        width = max(1, self.waves.winfo_width()-70)
        for index, color in enumerate((self.coral, self.cyan)):
            deck = state.get("decks", [{}, {}])[index]
            center = 16+index*28
            peaks = deck.get("peaks", [])
            tag = f"wave-{index}"
            geometry = (width, peaks)
            previous_geometry, previous_x = self.wave_rows.get(index, (None, None))
            if previous_geometry != geometry:
                self.waves.delete(tag)
                self.waves.create_text(18, center, text="AB"[index], fill=color,
                                       font=("Segoe UI", 10, "bold"), tags=tag)
                if peaks:
                    for number, peak in enumerate(peaks):
                        x = 46 + width * number / max(1, len(peaks)-1)
                        self.waves.create_line(x, center-peak*9, x, center+peak*9, fill=color, tags=tag)
                    self.waves.create_line(46, center-11, 46, center+11, fill=self.ink, width=2,
                                           tags=(tag, tag+"-head"))
                else:
                    self.waves.create_text(52, center, text="Waveform appears when a local track is loaded",
                                           fill=self.muted, anchor="w", font=("Segoe UI", 9), tags=tag)
                previous_x = None
            if not peaks:
                self.wave_rows[index] = (geometry, None)
                continue
            position = min(1, max(0, deck.get("elapsed",0)/max(1,deck.get("duration",0))))
            x = 46+width*position
            if previous_x != x:
                self.waves.coords(tag+"-head", x, center-11, x, center+11)
            self.wave_rows[index] = (geometry, x)

    def set_volume(self, value):
        if float(value) > 0:
            self.previous_volume = float(value)
        self.mute.configure(text="Unmute" if float(value) == 0 else "Mute")
        self.service.volume(value)

    def toggle_mute(self):
        if self.volume.get() > 0:
            self.previous_volume = self.volume.get()
        self.volume.set(0 if self.volume.get() > 0 else self.previous_volume)
        self.set_volume(self.volume.get())

    def refresh_stations(self, items=None):
        self.items = list(items if items is not None else getattr(self.service, "station_catalog", []))
        self.choice.configure(values=[s["name"] for s in self.items])
        if self.items and self.choice.current() < 0:
            self.choice.current(0)

    def tune(self):
        index = self.choice.current()
        if index >= 0:
            self.service.choose(self.items[index]["id"])

    def toggle(self):
        if not self.service.enabled:
            self.tune()
        else:
            self.service.toggle()

    def poll(self):
        if self.closed:
            return
        if self.frame.winfo_ismapped():
            self.refresh_view()
        self.poll_id = self.window.after(500, self.poll)

    def refresh_view(self):
        if self.closed:
            return
        state = self.service.snapshot()
        if state == self.last_state:
            return
        self.last_state = state
        setup=state.get("dj_setup",{})
        if self.controller.hint is None:
            self.dj_text.set(setup.get("message","Preparing DJ sound")+(" · "+str(setup.get("percent",0))+"%" if setup.get("status")=="working" else ""))
        if setup.get("status")=="error":
            if not self.dj_retry.winfo_manager():self.dj_retry.pack(side="left",padx=(8,0))
        else:self.dj_retry.pack_forget()
        choices={str(i+1)+". "+song["title"]:song["id"] for i,song in enumerate(state.get("songs",[]))}
        self.song_choices=choices;self.song_picker.configure(values=tuple(choices))
        if self.song_picker.get() not in choices:self.song_picker.set(next(iter(choices),""))
        self.controller.refresh(state)
        manual = state.get("studio_mode", False)
        self.next.configure(state="disabled" if manual else "normal")
        self.queue_title.set("MIXER" if manual else "NEXT IN RADIO")
        self.text.set(state.get("message", "Ready") + ("" if manual else
            f" · {state.get('ready',0)} ready" + (" · next deck cued" if state.get("next_loaded") else "")))
        if state.get("rendering"):
            self.bar.pack(fill="x", pady=(8,0))
            self.bar.configure(value=state.get("progress",0))
        else:
            self.bar.pack_forget()
        self.badge.configure(text="PAUSED" if state.get("paused") else "DJ BLEND" if state.get("blending") else
                             "ON AIR" if state.get("on_air") else "LOADED" if state.get("studio_mode") else
                             "PREPARING" if state.get("playing") else "READY")
        for value, key in zip(self.metrics, ("tracks_made", "radio_tokens", "local_tokens"), strict=True):
            number = state.get(key)
            value.set(f"{number:,}" if number is not None else "—")
        automatic = state.get("auto_blend", True)
        self.auto.set(automatic)
        catalog = state.get("stations")
        if catalog is not None and catalog != self.items:
            self.refresh_stations(catalog)
        selected = next((index for index, item in enumerate(self.items) if item["id"]==state.get("station_id")), None)
        if selected is not None and self.choice.current() != selected:
            self.choice.current(selected)
        self.next.configure(state="normal" if state.get("playing") and state.get("ready") and not state.get("paused") and not state.get("blending") else "disabled")
        self.draw_waveforms(state)

    def station_dialog(self, editing=False):
        import tkinter as tk
        from tkinter import messagebox, ttk

        index = self.choice.current()
        selected = self.items[index] if editing and index >= 0 else None
        dialog = tk.Toplevel(self.window)
        dialog.title("Your radio station")
        dialog.transient(self.window)
        box = ttk.Frame(dialog, padding=20)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Name your station").pack(anchor="w")
        name = ttk.Entry(box, width=52, style="Radio.TEntry")
        name.pack(fill="x", pady=(4, 12))
        ttk.Label(box, text="What should it sound like?").pack(anchor="w")
        prompt = tk.Text(box, height=5, width=55, wrap="word", background="#202d46", foreground="#edf3ff", insertbackground="#edf3ff", font=("Segoe UI", 11), relief="flat", padx=8, pady=8)
        prompt.pack(fill="both", expand=True, pady=(4, 10))
        vocals = tk.BooleanVar(value=bool(selected and not selected["instrumental"]))
        ttk.Checkbutton(box, text="Include singing / vocals", variable=vocals, style="Radio.TCheckbutton").pack(anchor="w")
        ttk.Label(box, text="Three-minute tracks · local generation · audio only", style="Muted.TLabel").pack(anchor="w", pady=10)
        if selected:
            name.insert(0, selected["name"])
            prompt.insert("1.0", selected["prompt"])
        def save():
            try:
                self.service.station_prompt(name.get(), prompt.get("1.0", "end").strip(), not vocals.get(),
                    station_id=selected["id"] if selected else None)
                dialog.destroy()
            except ValueError as error:
                messagebox.showerror("Station", str(error), parent=dialog)
        ttk.Button(box, text="Save & play", command=save, style="Accent.TButton").pack(anchor="e")
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.update_idletasks()
        x = self.window.winfo_rootx() + (self.window.winfo_width()-dialog.winfo_reqwidth())//2
        y = self.window.winfo_rooty() + (self.window.winfo_height()-dialog.winfo_reqheight())//2
        dialog.geometry(f"+{max(0,x)}+{max(0,y)}")
        name.focus_set()
