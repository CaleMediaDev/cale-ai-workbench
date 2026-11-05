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
        if self.opened and self.command(f"status {self.alias} mode") == "paused":
            self.command(f"resume {self.alias}")

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
    """Two preloaded decks with an equal-power crossfade; no UI-thread audio work."""
    def __init__(self, deck_factory=AudioDeck, clock=time.monotonic):
        self.factory, self.now = deck_factory, clock
        self.active = deck_factory()
        self.active.channel = 0
        self.deck_levels = [100.0, 100.0]
        self.manual_fraction = None
        self.outgoing = self.prepared = None
        self.volume = 65
        self.paused_at = None
        self.fade_start = self.fade_seconds = 0

    @property
    def mixing(self):
        return self.outgoing is not None

    @property
    def alias(self):
        return self.active.alias

    def command(self, text):
        return self.active.command(text)

    def play(self, path):
        self.stop()
        self.active.set_volume(self.volume * self.deck_levels[self.active.channel] / 100)
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
        self.fade_seconds = max(0.1, float(seconds))
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
                self.active.set_volume(self.volume)
            raise
        return True

    def advance_mix(self):
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
        return {deck.path for deck in (self.active, self.outgoing, self.prepared) if deck is not None and deck.path}

    def busy(self):
        return self.active.busy()

    def clock(self):
        return self.active.clock()

    def pause(self):
        if self.paused_at is None:
            self.paused_at = self.now()
            for deck in (self.active, self.outgoing):
                if deck is not None:
                    deck.pause()

    def resume(self):
        if self.paused_at is not None:
            if self.mixing:
                self.fade_start += self.now()-self.paused_at
            self.paused_at = None
            for deck in (self.active, self.outgoing):
                if deck is not None:
                    deck.resume()

    def set_volume(self, value):
        self.volume = max(0, min(100, int(value)))
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
        if channel not in (0, 1):
            raise ValueError("Choose deck A or B.")
        self.deck_levels[channel] = max(0, min(100, float(value)))
        self.set_volume(self.volume)

    def stop(self):
        error = None
        for deck in (self.active, self.outgoing, self.prepared):
            if deck is not None:
                try:
                    deck.stop()
                except Exception as failure:
                    error = error or failure
        self.outgoing = self.prepared = None
        self.paused_at = None
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
        self.auto_blend = True
        self.waveforms = {}
        self.view = {"message": self.message, "ready": 0, "progress": 0, "playing": False, "paused": False}
        ensure_stations(self.root)
        self.station_catalog = stations(self.root)
        with closing(database(self.root)) as conn, conn:
            conn.execute("UPDATE tracks SET status='played' WHERE status='playing'")

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def choose(self, identity):
        # Resolve the selected row on the radio thread, not the Tk thread.
        self.commands.put(("choose_id", identity))

    def station_prompt(self, name, prompt, instrumental=True, station_id=None):
        values = _station_record(name, prompt, instrumental, station_id)
        self.commands.put(("station_prompt", values))
        return values[0]

    def _choose(self, station):
        with self.lock:
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
        self.commands.put(("toggle", None))

    def _toggle(self):
        with self.lock:
            if not self.enabled:
                if self.station:
                    self._choose(self.station)
                return
            self.paused = not self.paused
            (self.player.pause if self.paused else self.player.resume)()

    def stop(self):
        self.commands.put(("stop", None))

    def _stop(self):
        with self.lock:
            self.skip_requested = False
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
        self.commands.put(("skip", None))

    def volume(self, value):
        self.commands.put(("volume", max(0, min(100, int(float(value))))))

    def crossfade(self, value):
        self.commands.put(("crossfade", max(0, min(1, float(value)))))

    def auto_mix(self, enabled):
        self.commands.put(("auto_mix", bool(enabled)))

    def deck_volume(self, channel, value):
        if channel not in (0, 1):
            raise ValueError("Choose deck A or B.")
        self.commands.put(("deck_volume", (channel, max(0, min(100, float(value))))))

    def snapshot(self):
        return dict(self.view)

    def _publish(self):
        if time.monotonic() >= self.stats_at:
            self.stats_at = time.monotonic()+10
            self.local_tokens, self.radio_tokens = local_token_totals(self.root)
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
                     "decks": self._deck_views(), "stations": self.station_catalog,
                     "station_id": (self.station or {}).get("id")}

    def _deck_views(self):
        """Read/decode audio only on the service thread, never in Tk callbacks."""
        views = [{"channel": index, "title": "Deck "+letter, "status": "Empty", "elapsed": 0,
                  "duration": 0, "peaks": [], "level": 100, "gain": 0} for index, letter in enumerate("AB")]
        for deck in (getattr(self.player, "active", None), getattr(self.player, "outgoing", None),
                     getattr(self.player, "prepared", None)):
            channel = getattr(deck, "channel", None)
            path = getattr(deck, "path", None)
            if channel not in (0, 1) or not path:
                continue
            try:
                stat = Path(path).stat()
                identity = (path, stat.st_size, stat.st_mtime_ns)
                if identity not in self.waveforms:
                    self.waveforms[identity] = waveform_peaks(path)
                    while len(self.waveforms) > 4:
                        self.waveforms.pop(next(iter(self.waveforms)))
                peaks = self.waveforms[identity]
                elapsed, duration = deck.clock()
                views[channel].update(title=(self.station or {}).get("name", "Local track"),
                    status="Cued" if deck is getattr(self.player, "prepared", None) else "Paused" if self.paused else "Playing",
                    elapsed=elapsed, duration=duration, peaks=peaks,
                    level=getattr(self.player, "deck_levels", [100, 100])[channel], gain=getattr(deck, "volume", 0))
            except (OSError, RuntimeError, ValueError):
                views[channel]["status"] = "Audio unavailable"
        return views

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
            elif action == "skip":
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
        elapsed, duration = self.player.clock()
        if duration <= 0:
            return
        remaining = max(0, duration-elapsed)
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
            self._commands()
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
                getattr(self.player, "advance_mix", lambda: None)()
                if time.monotonic() >= next_tick:
                    self.tick()
                    next_tick = time.monotonic()+0.5
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
            self.closed.wait(0.05 if getattr(self.player, "mixing", False) else 0.15)
        self.player.stop()

    def close(self):
        self.closed.set()


def waveform_peaks(path, bins=384):
    """Bounded PCM waveform from the actual recording; unsupported audio is blank."""
    import array
    import sys
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
            peaks = []
            for _ in range(bins):
                data = audio.readframes(chunk)
                if not data:
                    break
                values = array.array({1: "B", 2: "h", 4: "i"}[width])
                values.frombytes(data)
                if sys.byteorder != "little" and width > 1:
                    values.byteswap()
                peak = max((abs(value-128) if width == 1 else abs(value) for value in values), default=0)
                peaks.append(min(1, peak/(128 if width == 1 else 2**(width*8-1))))
            return peaks
    except (OSError, EOFError, ValueError, wave.Error):
        return []


# Generated NIGHTSHIFT jogwheel raster, embedded to keep the runtime manifest stable.
_JOGWHEEL_WEBP = (
    "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"
    "AA=="
 )


class Controls:
    """A compact radio deck with real playback timing and local usage receipts."""
    def __init__(self, parent, window, service, on_make_song=None):
        import tkinter as tk
        from tkinter import ttk
        self.window, self.service, self.on_make_song = window, service, on_make_song
        self.mode = "radio"
        self.closed = False
        self.previous_volume = 65
        self.bg, self.panel, self.ink, self.muted = "#0c1117", "#171f27", "#f1f6fb", "#9cabb9"
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
        prompt_row.pack(fill="x", pady=(10, 10))
        self.prompt = tk.Entry(prompt_row, bg="#1b242e", fg=self.ink, insertbackground=self.cyan,
            font=("Segoe UI", 12), relief="flat", highlightthickness=1, highlightbackground="#384957", highlightcolor=self.cyan)
        self.prompt.pack(side="left", fill="x", expand=True, ipady=9)
        self.prompt.bind("<Return>", lambda _: self.generate())
        self.prompt.bind("<FocusIn>", self._clear_prompt_hint)
        self.prompt.bind("<FocusOut>", self._show_prompt_hint)
        self._show_prompt_hint()
        self.generate_button = self._button(prompt_row, "Start station", self.generate, accent=self.coral)
        self.generate_button.pack(side="left", fill="y", padx=(12, 0))
        station_row = tk.Frame(self.frame, bg=self.bg)
        station_row.pack(fill="x", pady=(0, 8))
        self.choice = ttk.Combobox(station_row, state="readonly", width=22, style="Radio.TCombobox", font=("Segoe UI", 10, "bold"))
        self.choice.pack(side="left")
        self.choice.bind("<<ComboboxSelected>>", lambda _: self.tune())
        self._button(station_row, "New station", self.station_dialog).pack(side="left", padx=(8, 0))
        self._button(station_row, "Edit", lambda: self.station_dialog(True)).pack(side="left", padx=8)
        self.vocals = tk.BooleanVar(value=True)
        tk.Checkbutton(station_row, text="Vocals", variable=self.vocals, bg=self.bg, fg=self.muted,
            selectcolor=self.panel, activebackground=self.bg, activeforeground=self.ink, relief="flat").pack(side="right")
        self.badge = tk.Label(station_row, text="READY", fg=self.cyan, bg="#10313a", padx=10, pady=5, font=("Segoe UI", 9, "bold"))
        self.badge.pack(side="right", padx=12)
        decks = tk.Frame(self.frame, bg=self.bg)
        decks.pack(fill="both", expand=True)
        decks.columnconfigure(0, weight=1, uniform="decks")
        decks.columnconfigure(2, weight=1, uniform="decks")
        decks.rowconfigure(0, weight=1)
        self.deck_titles, self.deck_times, self.deck_states, self.deck_buttons = [], [], [], []
        self.deck_levels, self.gain_bars = [], []
        self._load_jogwheel()
        self.deck_images = []
        self.frame.bind("<Configure>", self._resize_assets, add="+")
        for channel, color in enumerate((self.coral, self.cyan)):
            deck = tk.Frame(decks, bg=self.panel, highlightbackground=color, highlightthickness=1, padx=12, pady=9)
            deck.grid(row=0, column=channel*2, sticky="nsew")
            header = tk.Frame(deck, bg=self.panel)
            header.pack(fill="x")
            title = tk.StringVar(value="Deck "+"AB"[channel])
            timing = tk.StringVar(value="0:00 / 0:00")
            tk.Label(header, text="AB"[channel], bg=color, fg="#0b1016", font=("Segoe UI", 10, "bold"), padx=8, pady=3).pack(side="left", padx=(0, 9))
            tk.Label(header, textvariable=title, bg=self.panel, fg=self.ink, font=("Segoe UI", 14, "bold"), anchor="w").pack(side="left", fill="x", expand=True)
            tk.Label(header, textvariable=timing, bg=self.panel, fg=self.muted, font=("Consolas", 9)).pack(side="right", padx=(8,0))
            body = tk.Frame(deck, bg=self.panel)
            body.pack(fill="both", expand=True)
            platter = tk.Label(body, image=self.jogwheel, bg=self.panel)
            platter.pack(side="left", padx=(0, 12))
            self.deck_images.append(platter)
            controls = tk.Frame(body, bg=self.panel)
            controls.pack(side="left", fill="both", expand=True)
            state = tk.StringVar(value="Load a station to begin")
            tk.Label(controls, textvariable=state, bg=self.panel, fg=color, font=("Segoe UI", 10, "bold"), wraplength=170, justify="left").pack(anchor="w", pady=(2, 8))
            tk.Label(controls, text="Original audio · EQ unavailable", bg=self.panel, fg=self.muted,
                font=("Segoe UI", 8), justify="left", wraplength=150).pack(anchor="w", pady=(0, 8))
            tk.Label(controls, text="DECK LEVEL", bg=self.panel, fg=self.muted, font=("Segoe UI", 8, "bold")).pack(anchor="w")
            level = tk.DoubleVar(value=100)
            tk.Scale(controls, from_=0, to=100, orient="horizontal", variable=level, showvalue=False,
                bg=self.panel, fg=color, troughcolor="#0b1219", activebackground=color, highlightthickness=0,
                relief="flat", sliderrelief="flat", sliderlength=14, width=6,
                command=lambda value, channel=channel: self.service.deck_volume(channel, value)).pack(fill="x")
            meter = tk.Frame(controls, height=6, bg="#26323d")
            meter.pack(fill="x", pady=(8, 3))
            bar = tk.Frame(meter, bg=color)
            bar.place(relwidth=0, relheight=1)
            tk.Label(controls, text="OUTPUT GAIN", bg=self.panel, fg=self.muted, font=("Segoe UI", 7)).pack(anchor="w")
            button = self._button(deck, "Play radio", lambda channel=channel: self.deck_action(channel), accent=color)
            button.pack(side="right", pady=(10, 0))
            self.deck_titles.append(title); self.deck_times.append(timing); self.deck_states.append(state)
            self.deck_buttons.append(button); self.deck_levels.append(level); self.gain_bars.append(bar)
        master = tk.Frame(decks, bg="#111a22", padx=12, pady=12)
        master.grid(row=0, column=1, sticky="ns", padx=12)
        tk.Label(master, text="MASTER", bg="#111a22", fg=self.muted, font=("Segoe UI", 8, "bold")).pack()
        self.volume = tk.DoubleVar(value=65)
        tk.Scale(master, from_=100, to=0, variable=self.volume, orient="vertical", length=128,
            bg="#111a22", fg=self.ink, troughcolor="#060b10", activebackground=self.cyan, highlightthickness=0,
            sliderlength=16, width=9, command=self.set_volume, relief="flat", font=("Consolas", 9)).pack(pady=10)
        self.mute = self._button(master, "Mute", self.toggle_mute)
        self.mute.pack(pady=(0, 8))
        self._button(master, "Stop", service.stop).pack()
        blend = tk.Frame(self.frame, bg=self.bg)
        blend.pack(fill="x", pady=(10, 8))
        tk.Label(blend, text="A", fg=self.coral, bg=self.bg, font=("Segoe UI", 11, "bold")).pack(side="left")
        self.crossfade = tk.DoubleVar(value=0)
        self.crossfader = tk.Scale(blend, from_=0, to=1, resolution=.01, variable=self.crossfade, orient="horizontal",
            showvalue=False, bg=self.bg, troughcolor="#273440", activebackground=self.cyan, highlightthickness=0,
            sliderlength=16, width=8, relief="flat")
        self.crossfader.pack(side="left", fill="x", expand=True, padx=12)
        # Only user input changes mix mode; programmatic Auto-DJ position updates
        # must not feed back into the audio command queue.
        for sequence in ("<B1-Motion>", "<ButtonRelease-1>", "<KeyRelease>"):
            self.crossfader.bind(sequence, lambda _: self.window.after_idle(
                lambda: self.set_crossfade(self.crossfade.get())))
        tk.Label(blend, text="B", fg=self.cyan, bg=self.bg, font=("Segoe UI", 11, "bold")).pack(side="left")
        self.mix_label = tk.StringVar(value="Automatic equal-power mixing")
        tk.Label(blend, textvariable=self.mix_label, fg=self.muted, bg=self.bg, font=("Segoe UI", 9)).pack(side="left", padx=(16, 0))
        self.waves = tk.Canvas(self.frame, bg="#080d13", height=60, highlightthickness=1, highlightbackground="#26333e")
        self.waves.pack(fill="x")
        self.waves.bind("<Configure>", lambda _: self.draw_waveforms(getattr(self, "last_state", {})))
        queue_row = tk.Frame(self.frame, bg=self.panel, padx=12, pady=10)
        queue_row.pack(fill="x", pady=(10, 0))
        self.text = tk.StringVar(value="Choose a station. Tracks are created on your PC.")
        tk.Label(queue_row, text="NEXT IN RADIO", bg=self.panel, fg=self.ink, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 16))
        tk.Label(queue_row, textvariable=self.text, bg=self.panel, fg=self.muted, font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x", expand=True)
        self.next = self._button(queue_row, "Mix next", service.skip)
        self.next.pack(side="right")
        footer = tk.Frame(self.frame, bg=self.bg)
        footer.pack(fill="x", pady=(12, 0))
        self.metrics = []
        for title in ("TRACKS MADE", "RADIO TOKENS", "TOTAL LOCAL TOKENS"):
            value = tk.StringVar(value="—")
            tk.Label(footer, text=title, fg=self.muted, bg=self.bg, font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 8))
            tk.Label(footer, textvariable=value, fg=self.ink, bg=self.bg, font=("Consolas", 11, "bold")).pack(side="left", padx=(0, 24))
            self.metrics.append(value)
        self.kind = tk.StringVar(value="LOCAL AUDIO · NO ACCOUNTS")
        tk.Label(footer, textvariable=self.kind, fg=self.cyan, bg=self.bg, font=("Segoe UI", 8)).pack(side="right")
        self.bar = ttk.Progressbar(self.frame, maximum=100, style="Radio.Horizontal.TProgressbar")
        self.refresh_stations()
        self.poll()
        service.start()

    def _button(self, parent, text, command, accent=None):
        import tkinter as tk
        return tk.Button(parent, text=text, command=command, bg=accent or "#24313c", fg="#071017" if accent else self.ink,
            activebackground="#e7fafe" if accent else "#344858", activeforeground="#09121a" if accent else self.ink,
            font=("Segoe UI", 10, "bold"), relief="flat", borderwidth=0, padx=13, pady=6, cursor="hand2",
            highlightthickness=1, highlightbackground=accent or "#3b4a58", takefocus=True)

    def _load_jogwheel(self):
        import base64
        from io import BytesIO
        from PIL import Image, ImageTk
        with Image.open(BytesIO(base64.b64decode(_JOGWHEEL_WEBP))) as source:
            image = source.convert("RGBA")
            image.thumbnail((224, 224), Image.Resampling.LANCZOS)
        logo = self.service.root / "assets/cale-logo.png"
        if logo.is_file():
            with Image.open(logo) as source:
                source.thumbnail((31, 31), Image.Resampling.LANCZOS)
                image.alpha_composite(source.convert("RGBA"), ((image.width-source.width)//2, (image.height-source.height)//2))
        self.jogwheels = {}
        for size in (164, 224):
            resized = image.copy()
            resized.thumbnail((size, size), Image.Resampling.LANCZOS)
            self.jogwheels[size] = ImageTk.PhotoImage(resized, master=self.window)
        self.jogwheel = self.jogwheels[164]
        self.jog_size = 164

    def _resize_assets(self, event):
        if event.widget is not self.frame:
            return
        size = 224 if event.width >= 1200 else 164
        if size != self.jog_size:
            self.jog_size = size
            for platter in self.deck_images:
                platter.configure(image=self.jogwheels[size])

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
            self.prompt.focus_set()
            return
        if self.mode == "song":
            if self.on_make_song is not None:
                try:
                    started = self.on_make_song(prompt)
                    if started is False:
                        self.text.set("Song did not start. Your description is still here so you can retry.")
                        return
                except Exception as error:
                    self.text.set("Song could not start: "+str(error)[:100])
                    return
                self.prompt.delete(0, "end")
        else:
            try:
                self.service.station_prompt(prompt[:45], prompt, not self.vocals.get())
            except ValueError as error:
                self.text.set(str(error))
                return
            self.text.set("Starting your new station in the background...")

    def deck_action(self, channel):
        deck = self.last_state.get("decks", [{}, {}])[channel]
        if deck.get("status") == "Cued":
            self.service.skip()
        else:
            self.toggle()

    def set_crossfade(self, value):
        self.auto.set(False)
        self.service.crossfade(value)

    def draw_waveforms(self, state):
        self.waves.delete("all")
        width = max(1, self.waves.winfo_width()-70)
        for index, color in enumerate((self.coral, self.cyan)):
            deck = state.get("decks", [{}, {}])[index]
            center = 16+index*28
            self.waves.create_text(18, center, text="AB"[index], fill=color, font=("Segoe UI", 10, "bold"))
            peaks = deck.get("peaks", [])
            if not peaks:
                self.waves.create_text(52, center, text="Waveform appears when a local track is loaded", fill=self.muted, anchor="w", font=("Segoe UI", 9))
                continue
            for number, peak in enumerate(peaks):
                x = 46+width*number/max(1,len(peaks)-1)
                self.waves.create_line(x, center-peak*9, x, center+peak*9, fill=color)
            position = min(1, max(0, deck.get("elapsed",0)/max(1,deck.get("duration",0))))
            x = 46+width*position
            self.waves.create_line(x, center-11, x, center+11, fill=self.ink, width=2)

    def set_volume(self, value):
        if float(value) > 0:
            self.previous_volume = float(value)
        self.mute.configure(text="Unmute" if float(value) == 0 else "Mute")
        self.service.volume(value)

    def toggle_mute(self):
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
        state = self.service.snapshot()
        self.last_state = state
        self.text.set(state.get("message", "Ready") + f" · {state.get('ready',0)} ready" + (" · next deck cued" if state.get("next_loaded") else ""))
        if state.get("rendering"):
            self.bar.pack(fill="x", pady=(8,0))
            self.bar.configure(value=state.get("progress",0))
        else:
            self.bar.pack_forget()
        self.badge.configure(text="PAUSED" if state.get("paused") else "DJ BLEND" if state.get("blending") else "ON AIR" if state.get("on_air") else "PREPARING" if state.get("playing") else "READY")
        for value, key in zip(self.metrics, ("tracks_made", "radio_tokens", "local_tokens"), strict=True):
            number = state.get(key)
            value.set(f"{number:,}" if number is not None else "—")
        for index, deck in enumerate(state.get("decks", [{}, {}])):
            self.deck_titles[index].set(deck.get("title", "Deck "+"AB"[index])[:25])
            self.deck_states[index].set(deck.get("status", "Empty"))
            elapsed, duration = deck.get("elapsed",0), deck.get("duration",0)
            self.deck_times[index].set(f"{int(elapsed)//60}:{int(elapsed)%60:02d} / {int(duration)//60}:{int(duration)%60:02d}")
            self.gain_bars[index].place_configure(relwidth=max(0, min(1, deck.get("gain",0)/100)))
            self.deck_buttons[index].configure(text="Mix in" if deck.get("status")=="Cued" else "Resume" if state.get("paused") else "Pause mix" if state.get("blending") else "Pause" if deck.get("status")=="Playing" else "Play radio")
        automatic = state.get("auto_blend", True)
        self.auto.set(automatic)
        catalog = state.get("stations")
        if catalog is not None and catalog != self.items:
            self.refresh_stations(catalog)
        selected = next((index for index, item in enumerate(self.items) if item["id"]==state.get("station_id")), None)
        if selected is not None and self.choice.current() != selected:
            self.choice.current(selected)
        if automatic:
            self.crossfade.set(state.get("crossfade",0))
        self.mix_label.set("Automatic equal-power mixing" if automatic else "Manual A/B mix · turn Auto-DJ on to resume")
        self.crossfader.configure(state="normal" if state.get("next_loaded") or state.get("blending") else "disabled")
        self.next.configure(state="normal" if state.get("playing") and state.get("ready") and not state.get("paused") and not state.get("blending") else "disabled")
        self.draw_waveforms(state)
        self.poll_id = self.window.after(500, self.poll)

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
