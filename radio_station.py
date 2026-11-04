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


def save_station(name, prompt, instrumental=True, *, root=ROOT, station_id=None):
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise ValueError("Give your station a name, up to 60 characters.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 3000:
        raise ValueError("Describe your station's music, up to 3000 characters.")
    if type(instrumental) is not bool:
        raise ValueError("Choose instrumental or vocal music.")
    identity = station_id or uuid.uuid4().hex
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Invalid station ID")
    with closing(database(root)) as conn, conn:
        conn.execute("INSERT INTO stations VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,prompt=excluded.prompt,instrumental=excluded.instrumental",
                     (identity, name.strip(), prompt.strip(), int(instrumental)))
    return identity


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
        self.active.set_volume(self.volume)
        self.active.play(path)

    def prepare(self, path):
        path = str(Path(path).resolve())
        if self.prepared is not None and self.prepared.path == path:
            return
        if self.prepared is not None:
            self.prepared.stop()
        self.prepared = self.factory()
        try:
            self.prepared.set_volume(0)
            self.prepared.open(path)
        except Exception:
            try:
                self.prepared.stop()
            finally:
                self.prepared = None
            raise

    def blend(self, path, seconds=FADE_SECONDS):
        if self.mixing:
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
        self.advance_mix()
        return True

    def advance_mix(self):
        if not self.mixing or self.paused_at is not None:
            return
        fraction = max(0, min(1, (self.now()-self.fade_start)/self.fade_seconds))
        self.active.set_volume(self.volume*math.sin(fraction*math.pi/2))
        self.outgoing.set_volume(self.volume*math.cos(fraction*math.pi/2))
        if fraction >= 1:
            self.outgoing.stop()
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
            self.advance_mix()
            if paused is not None:
                self.paused_at = self.now()
        else:
            self.active.set_volume(self.volume)

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
        self.view = {"message": self.message, "ready": 0, "progress": 0, "playing": False, "paused": False}
        ensure_stations(self.root)
        with closing(database(self.root)) as conn, conn:
            conn.execute("UPDATE tracks SET status='played' WHERE status='playing'")

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def choose(self, identity):
        station = next((row for row in stations(self.root) if row["id"] == identity), None)
        if station is None:
            raise ValueError("Choose an existing radio station.")
        self.commands.put(("choose", station))

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

    def snapshot(self):
        return dict(self.view)

    def _publish(self):
        if time.monotonic() >= self.stats_at:
            self.stats_at = time.monotonic()+10
            self.local_tokens, self.radio_tokens = local_token_totals(self.root)
            with closing(database(self.root)) as conn:
                self.tracks_made = conn.execute("SELECT COUNT(*) FROM tracks WHERE completed=1").fetchone()[0]
        with closing(database(self.root)) as conn:
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
                     "tracks_made": self.tracks_made, "rendering": rendering}

    def _commands(self):
        while not self.commands.empty():
            action, value = self.commands.get()
            if action == "choose":
                self._choose(value)
            elif action == "toggle":
                self._toggle()
            elif action == "stop":
                self._stop()
            elif action == "skip":
                if hasattr(self.player, "prepare"):
                    self.skip_requested = not getattr(self.player, "mixing", False)
                else:
                    self.player.stop()
            elif action == "volume":
                self.player.set_volume(value)

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


class Controls:
    """A compact radio deck with real playback timing and local usage receipts."""
    def __init__(self, parent, window, service):
        import tkinter as tk
        from tkinter import ttk

        self.window, self.service = window, service
        style = ttk.Style(window)
        deck, field, ink, muted, accent = "#121c30", "#202d46", "#edf3ff", "#9baecc", "#8b7dff"
        style.configure("Radio.TCombobox", fieldbackground=deck, foreground=ink, background=field, arrowcolor=ink, borderwidth=0, padding=4)
        style.map("Radio.TCombobox", fieldbackground=[("readonly", deck)], foreground=[("readonly", ink)])
        style.configure("Radio.TEntry", fieldbackground="#202d46", foreground="#edf3ff", insertcolor="#edf3ff")
        style.configure("Radio.TCheckbutton", background="#0b1020", foreground="#edf3ff")
        style.configure("Radio.TButton", background=field, foreground=ink, padding=(10, 7), font=("Segoe UI", 10, "bold"), borderwidth=0)
        style.map("Radio.TButton", background=[("active", "#354665"), ("disabled", deck)], foreground=[("disabled", "#53627c")])
        style.configure("Radio.Play.TButton", background=accent, foreground="#ffffff", padding=(16, 8), font=("Segoe UI", 11, "bold"), borderwidth=0)
        style.map("Radio.Play.TButton", background=[("active", "#a394ff")])
        style.configure("Radio.Playback.Horizontal.TProgressbar", background=accent, troughcolor="#283650", thickness=5, borderwidth=0)
        style.configure("Radio.Build.Horizontal.TProgressbar", background="#5ce1c2", troughcolor="#283650", thickness=3, borderwidth=0)
        style.configure("Radio.Horizontal.TScale", background=deck, troughcolor="#283650", borderwidth=0)
        self.frame = tk.Frame(parent, background=deck, padx=14, pady=10, highlightbackground="#2b3954", highlightthickness=1)
        self.frame.pack(fill="x", pady=(0, 12))
        line = tk.Frame(self.frame, background=deck)
        line.pack(fill="x")
        cover = tk.Label(line, background=deck, foreground=accent, text="CALE", font=("Segoe UI", 14, "bold"))
        logo = service.root / "assets/cale-logo.png"
        if logo.is_file():
            from PIL import Image, ImageTk
            with Image.open(logo) as source:
                source.thumbnail((48, 48), Image.Resampling.LANCZOS)
                self.cover = ImageTk.PhotoImage(source.copy(), master=window)
            cover.configure(image=self.cover, text="")
        cover.pack(side="left", padx=(0, 12))
        station = tk.Frame(line, background=deck)
        station.pack(side="left")
        tk.Label(station, text="C A L E   R A D I O", background=deck, foreground=accent, font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.choice = ttk.Combobox(station, state="readonly", width=18, style="Radio.TCombobox", font=("Segoe UI", 14, "bold"))
        self.choice.pack(anchor="w")
        self.choice.bind("<<ComboboxSelected>>", lambda _: self.tune())
        self.kind = tk.StringVar(value="VOCALS · 3 MIN")
        self.badge = tk.Label(line, text="READY", background="#252642", foreground="#bdb3ff", padx=9, pady=4, font=("Segoe UI", 8, "bold"))
        self.badge.pack(side="left", padx=(12, 8))
        ttk.Button(line, text="+ Station", width=9, command=self.station_dialog, style="Radio.TButton").pack(side="right", padx=(8, 0))
        ttk.Button(line, text="Edit", width=5, command=lambda: self.station_dialog(True), style="Radio.TButton").pack(side="right")
        self.metrics = []
        for label in ("TRACKS MADE", "RADIO TOKENS", "TOTAL LOCAL TOKENS"):
            metric = tk.Frame(line, background=deck)
            metric.pack(side="right", padx=12)
            value = tk.StringVar(value="—")
            tk.Label(metric, textvariable=value, background=deck, foreground=ink, font=("Segoe UI", 13, "bold")).pack(anchor="e")
            tk.Label(metric, text=label, background=deck, foreground=muted, font=("Segoe UI", 8, "bold")).pack(anchor="e")
            self.metrics.append(value)

        transport = tk.Frame(self.frame, background=deck)
        transport.pack(fill="x", pady=(8, 0))
        self.play = ttk.Button(transport, text="Play", width=7, command=self.toggle, style="Radio.Play.TButton")
        self.play.pack(side="left")
        self.next = ttk.Button(transport, text="Mix next", width=8, command=service.skip, style="Radio.TButton")
        self.next.pack(side="left", padx=(7, 4))
        ttk.Button(transport, text="Stop", width=5, command=service.stop, style="Radio.TButton").pack(side="left")
        self.previous_volume = 65
        self.volume = tk.DoubleVar(value=65)
        ttk.Scale(transport, from_=0, to=100, length=80, variable=self.volume, style="Radio.Horizontal.TScale",
                  command=self.set_volume).pack(side="right", padx=(6, 0))
        self.mute = ttk.Button(transport, text="Mute", width=7, command=self.toggle_mute, style="Radio.TButton")
        self.mute.pack(side="right", padx=(8, 0))
        progress = tk.Frame(transport, background=deck)
        progress.pack(side="left", fill="x", expand=True, padx=(16, 8))
        time_row = tk.Frame(progress, background=deck)
        time_row.pack(fill="x")
        tk.Label(time_row, textvariable=self.kind, background=deck, foreground=muted, font=("Segoe UI", 8, "bold")).pack(side="left")
        self.clock = tk.StringVar(value="0:00 / 3:00")
        tk.Label(time_row, textvariable=self.clock, background=deck, foreground=ink, font=("Segoe UI", 9)).pack(side="right")
        self.timeline = ttk.Progressbar(progress, maximum=180, style="Radio.Playback.Horizontal.TProgressbar")
        self.timeline.pack(fill="x", pady=(4, 0))
        self.text = tk.StringVar()
        self.details = tk.Frame(self.frame, background=deck)
        tk.Label(self.details, textvariable=self.text, background=deck, foreground=muted, font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 3))
        self.bar = ttk.Progressbar(self.details, maximum=100, style="Radio.Build.Horizontal.TProgressbar")
        self.refresh_stations()
        self.poll()
        service.start()

    def set_volume(self, value):
        if float(value) > 0:
            self.previous_volume = float(value)
        self.mute.configure(text="Unmute" if float(value) == 0 else "Mute")
        self.service.volume(value)

    def toggle_mute(self):
        self.volume.set(0 if self.volume.get() > 0 else self.previous_volume)
        self.set_volume(self.volume.get())

    def refresh_stations(self):
        self.items = stations(self.service.root)
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
        state = self.service.snapshot()
        if state["playing"] and not self.details.winfo_manager():
            self.details.pack(fill="x")
        elif not state["playing"]:
            self.details.pack_forget()
        self.text.set(state["message"] + f"  ·  {state['ready']} tracks ready" + (" · next deck loaded" if state.get("next_loaded") else ""))
        if state.get("rendering"):
            self.bar.pack(fill="x")
        else:
            self.bar.pack_forget()
        self.bar.configure(value=state["progress"])
        elapsed, duration = state.get("elapsed", 0), state.get("duration", 0) or SECONDS
        def clock(value):
            return f"{int(value)//60}:{int(value)%60:02d}"
        self.clock.set(clock(elapsed)+" / "+clock(duration))
        self.timeline.configure(maximum=duration, value=min(elapsed, duration))
        self.badge.configure(text="PAUSED" if state["paused"] else "DJ BLEND" if state.get("blending") else "ON AIR" if state.get("on_air") else "BUFFERING" if state["playing"] else "READY",
                             background="#153a36" if state.get("on_air") else "#252642", foreground="#70edce" if state.get("on_air") else "#bdb3ff")
        for value, key in zip(self.metrics, ("tracks_made", "radio_tokens", "local_tokens"), strict=True):
            number = state.get(key)
            value.set(f"{number:,}" if number is not None else "—")
        index = self.choice.current()
        if 0 <= index < len(self.items):
            self.kind.set(("INSTRUMENTAL" if self.items[index]["instrumental"] else "VOCALS")+" · 3 MIN · LOCAL")
        self.play.configure(text="Resume" if state["paused"] else "Pause" if state["playing"] else "Play")
        self.next.configure(state="normal" if state["playing"] and state["ready"] and not state["paused"] and not state.get("blending") else "disabled")
        self.window.after(500, self.poll)

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
                identity = save_station(name.get(), prompt.get("1.0", "end").strip(), not vocals.get(),
                    root=self.service.root, station_id=selected["id"] if selected else None)
                self.refresh_stations()
                self.choice.current(next(i for i, row in enumerate(self.items) if row["id"] == identity))
                self.tune()
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
