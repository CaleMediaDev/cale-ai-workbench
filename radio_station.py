"""Prompt stations, a small durable local queue, and the homepage radio.

Music uses the existing durable ACE-Step worker. One song renders at a time;
playback prepares two songs ahead and repeats the last good song on underrun.
Only this module's UUID-named cache files are eligible for automatic cleanup.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
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


def database(root=ROOT):
    path = Path(root) / "LongTasks/radio.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS stations(id TEXT PRIMARY KEY,name TEXT,prompt TEXT,instrumental INTEGER)")
    conn.execute("""CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY,station TEXT,job TEXT,
        path TEXT,status TEXT,created REAL,finished REAL DEFAULT 0,render_seconds REAL DEFAULT 0,config TEXT DEFAULT '')""")
    if "config" not in {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}:
        conn.execute("ALTER TABLE tracks ADD COLUMN config TEXT DEFAULT ''")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS radio_live_tracks ON tracks(created) WHERE status!='pruned'")
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


def foreground_music_busy():
    """Do not put another radio render ahead of a live user song task."""
    import music_backend
    for path in music_backend.JOBS.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if (not str(job.get("task_id", "")).startswith("radio-")
                    and job.get("status") in ("queued", "working")
                    and music_backend._worker_running(path.stem, job)):
                return True
        except (OSError, ValueError, KeyError):
            continue
    return False


class Player:
    """A single Windows audio channel; no external player windows or packages."""
    def __init__(self):
        self.alias = "cale_radio_" + uuid.uuid4().hex
        self.opened = False
        self.volume = 65

    def command(self, text):
        if os.name != "nt":
            raise RuntimeError("Radio playback needs Windows audio.")
        buffer = ctypes.create_unicode_buffer(512)
        error = ctypes.windll.winmm.mciSendStringW(text, buffer, len(buffer), None)
        if error:
            raise RuntimeError("Windows could not play this radio track.")
        return buffer.value

    def play(self, path):
        self.stop()
        self.command(f'open "{Path(path).resolve()}" type mpegvideo alias {self.alias}')
        self.opened = True
        try:
            self.set_volume(self.volume)
            self.command(f"play {self.alias}")
        except Exception:
            self.stop()
            raise

    def busy(self):
        return self.opened and self.command(f"status {self.alias} mode") in ("playing", "paused")

    def pause(self):
        if self.opened:
            self.command(f"pause {self.alias}")

    def resume(self):
        if self.opened:
            self.command(f"resume {self.alias}")

    def set_volume(self, value):
        self.volume = max(0, min(100, int(value)))
        if self.opened:
            self.command(f"setaudio {self.alias} volume to {self.volume * 10}")

    def stop(self):
        if self.opened:
            try:
                self.command(f"close {self.alias}")
            finally:
                self.opened = False


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
        self.message = "Pick a station. Fresh three-minute tracks, made on this PC."
        self.ready = 0
        self.progress = 0
        self.retry_at = 0
        self.thread = None
        self.commands = queue.SimpleQueue()
        self.view = {"message": self.message, "ready": 0, "progress": 0, "playing": False, "paused": False}
        if not stations(self.root):
            save_station("Cale Club", "Instrumental melodic techno, electronic drums and rich synthesizers, no guitars, energetic night-drive atmosphere", root=self.root)
            save_station("Chill", "Instrumental ambient downtempo, warm synthesizers and soft electronic beats, relaxing focused atmosphere", root=self.root)
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
            self.player.stop()
            self.station = station
            self.current = None
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
            self.enabled = False
            self.paused = False
            self.player.stop()
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
        self.view = {"message": "Paused" if self.paused else self.message, "ready": self.ready,
                     "progress": self.progress, "playing": self.enabled, "paused": self.paused}

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
                self.player.stop()
            elif action == "volume":
                self.player.set_volume(value)

    def _rows(self):
        with closing(database(self.root)) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM tracks WHERE status!='pruned' ORDER BY created")]

    def _update(self, identity, status, **values):
        with closing(database(self.root)) as conn, conn:
            conn.execute("UPDATE tracks SET status=?,finished=?,render_seconds=? WHERE id=?",
                         (status, values.get("finished", 0), values.get("render_seconds", 0), identity))

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
            if not self.player.busy():
                if ready:
                    if self.current:
                        self._update(self.current["id"], "played")
                    self.current = ready.pop(0)
                    self._play_current()
                    self._update(self.current["id"], "playing")
                    self.ready = len(ready)
                    self.message = "On air · next tracks are made in the background"
                elif self.current and Path(self.current["path"]).is_file():
                    self._play_current()
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
            for row in played[:-5]:
                path = Path(row["path"])
                expected = self.root / "Radio/audio" / row["station"] / (row["id"] + ".wav")
                if path.resolve() == expected.resolve() and path.resolve().is_relative_to((self.root / "Radio/audio").resolve()):
                    path.unlink(missing_ok=True)
                    path.with_suffix(".lyrics.txt").unlink(missing_ok=True)
                    self._update(row["id"], "pruned")

    def _loop(self):
        while not self.closed.is_set():
            try:
                self.tick()
            except Exception as error:
                with self.lock:
                    self.message = "Radio will retry: " + str(error)[:100]
                    self.retry_at = time.monotonic() + 30
            self._publish()
            self.closed.wait(1)
        self.player.stop()

    def close(self):
        self.closed.set()


class Controls:
    """Compact, always-visible radio card; station prompts are one dialog away."""
    def __init__(self, parent, window, service):
        import tkinter as tk
        from tkinter import ttk

        self.window, self.service = window, service
        style = ttk.Style(window)
        style.configure("Radio.TCombobox", fieldbackground="#202d46", foreground="#edf3ff", background="#293955", arrowcolor="#edf3ff")
        style.map("Radio.TCombobox", fieldbackground=[("readonly", "#202d46")], foreground=[("readonly", "#edf3ff")])
        style.configure("Radio.TEntry", fieldbackground="#202d46", foreground="#edf3ff", insertcolor="#edf3ff")
        style.configure("Radio.TCheckbutton", background="#0b1020", foreground="#edf3ff")
        self.frame = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        self.frame.pack(fill="x", pady=(0, 12))
        line = ttk.Frame(self.frame, style="Card.TFrame")
        line.pack(fill="x")
        ttk.Label(line, text="♫  CALE RADIO", style="Card.TLabel", font=("Segoe UI", 12, "bold")).pack(side="left", padx=(0, 12))
        self.choice = ttk.Combobox(line, state="readonly", width=22, style="Radio.TCombobox")
        self.choice.pack(side="left", padx=(0, 8))
        self.choice.bind("<<ComboboxSelected>>", lambda _: self.tune())
        self.play = ttk.Button(line, text="Play", width=7, command=self.toggle)
        self.play.pack(side="left", padx=3)
        self.next = ttk.Button(line, text="Next", width=7, command=service.skip)
        self.next.pack(side="left", padx=3)
        ttk.Button(line, text="Stop", width=7, command=service.stop).pack(side="left", padx=3)
        ttk.Button(line, text="+ Station", width=10, command=self.station_dialog).pack(side="right", padx=3)
        ttk.Button(line, text="Edit", width=5, command=lambda: self.station_dialog(True)).pack(side="right", padx=3)
        self.volume = tk.DoubleVar(value=65)
        ttk.Scale(line, from_=0, to=100, length=80, variable=self.volume,
                  command=lambda value: self.set_volume(value)).pack(side="right", padx=8)
        ttk.Label(line, text="Volume", style="Card.TLabel").pack(side="right")
        self.text = tk.StringVar()
        self.details = ttk.Frame(self.frame, style="Card.TFrame")
        ttk.Label(self.details, textvariable=self.text, style="CardMuted.TLabel").pack(anchor="w", pady=(7, 3))
        self.bar = ttk.Progressbar(self.details, maximum=100, style="Cale.Horizontal.TProgressbar")
        self.bar.pack(fill="x")
        self.refresh_stations()
        self.poll()
        service.start()

    def set_volume(self, value):
        self.service.volume(value)

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
        self.text.set(state["message"] + f"  ·  {state['ready']} queued")
        self.bar.configure(value=state["progress"])
        self.play.configure(text="Resume" if state["paused"] else "Pause" if state["playing"] else "Play")
        self.next.configure(state="normal" if state["playing"] and state["ready"] else "disabled")
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
