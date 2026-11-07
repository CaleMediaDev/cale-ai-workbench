"""Live camera observations for the page; these never authorize printer motion."""

from __future__ import annotations

import hashlib
import io
import math
import subprocess
import threading
import time
import tkinter as tk
import urllib.parse
from tkinter import ttk

from PIL import Image, ImageTk

import ffmpeg_runtime
from print_farm import AI_LOCK, camera_frame, local_ai

PROMPT = """Observe this 3D printer camera image. Identify visible prints, raised debris,
loose filament, hands, tools, and the overhead print head. Distinguish flat glue haze
and scratches from raised objects. Never assume an obscured bed is empty. This is
an observation, not permission to start a printer. Return concise JSON:
{"state":"clear|occupied|obstructed|uncertain", "confidence":0.0,
"reason":"one short sentence", "objects":[{"label":"short object name",
"box":[left,top,right,bottom]}]}. Boxes use image coordinates normalized to 0..1000.
Only include boxes for objects actually visible; at most six. A print head alone
is not an object on the bed, but can obstruct the view. A hand means occupied.
Use clear only if the entire usable bed is visibly empty."""


def observation(answer):
    """Validate model output before drawing; never invent a box or clear state."""
    state = answer.get("state", "uncertain")
    if state not in {"clear", "occupied", "obstructed", "uncertain"}:
        state = "uncertain"
    score = answer.get("confidence", 0)
    if (
        type(score) not in (int, float)
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):
        score, state = 0, "uncertain"
    boxes = []
    objects = answer.get("objects", [])
    for item in objects[:6] if isinstance(objects, list) else []:
        if not isinstance(item, dict):
            continue
        box = item.get("box")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(
                type(v) not in (int, float)
                or not math.isfinite(v)
                or not 0 <= v <= 1000
                for v in box
            )
        ):
            continue
        if box[0] >= box[2] or box[1] >= box[3]:
            continue
        boxes.append({"label": str(item.get("label", "Object"))[:50], "box": box})
    return {
        "state": state,
        "confidence": score,
        "reason": str(answer.get("reason", "No reliable observation"))[:180],
        "objects": boxes,
    }


def scene_signature(raw):
    with Image.open(io.BytesIO(raw)) as image:
        return image.convert("L").resize((32, 18)).tobytes()


def scene_changed(before, after):
    if not before or not after or len(before) != len(after):
        return True
    differences = [abs(a - b) for a, b in zip(before, after)]
    return (
        sum(differences) / len(differences) >= 4
        or sum(d > 20 for d in differences) / len(differences) >= 0.02
    )


def scan_due(frame, result, busy, now):
    if (
        not frame.get("bytes")
        or now - frame.get("at", 0) >= 5
        or now - frame.get("changed_at", 0) >= 15
    ):
        return False
    if not result:
        return True
    elapsed = now - result.get("checked_at", 0)
    if result.get("state") == "unavailable":
        return elapsed >= 30
    changed = scene_changed(result.get("signature"), frame.get("signature"))
    # Scene-change scans are display-only; the dispatch check always takes new frames.
    return elapsed >= (8 if changed else 20 if busy else 45)


class LiveCameras:
    def __init__(self, settings, capture=camera_frame, infer=local_ai):
        self.settings, self.capture, self.infer = settings, capture, infer
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.visible = threading.Event()
        self.sources, self.workers, self.frames, self.results = {}, {}, {}, {}
        self.stream_ids, self.processes = set(), {}
        self.snapshot_urls, self.fallback_until = {}, {}
        self.busy_printers = set()
        self.analyzing = None
        self.analysis_thread = None

    def update(self, printers, visible):
        sources = {
            p["id"]: p.get("camera_stream_url") or p.get("camera_url", "")
            for p in printers
            if p.get("camera_stream_url") or p.get("camera_url")
        }
        with self.lock:
            for pid, process in list(self.processes.items()):
                if (
                    not visible or sources.get(pid) != self.sources.get(pid)
                ) and process.poll() is None:
                    process.terminate()
            self.busy_printers = {
                p["id"] for p in printers if p.get("online") and not p.get("idle")
            }
            self.snapshot_urls = {p["id"]: p.get("camera_url") for p in printers}
            self.stream_ids = {p["id"] for p in printers if p.get("camera_stream_url")}
            for pid, url in sources.items():
                if self.sources.get(pid) != url:
                    self.frames.pop(pid, None)
                    self.results.pop(pid, None)
                    self.fallback_until.pop(pid, None)
            self.sources = sources
            self.frames = {k: v for k, v in self.frames.items() if k in sources}
            self.results = {k: v for k, v in self.results.items() if k in sources}
        if visible:
            self.visible.set()
        else:
            self.visible.clear()
        for pid in sources:
            if pid not in self.workers or not self.workers[pid].is_alive():
                worker = threading.Thread(
                    target=self.camera_loop, args=(pid,), daemon=True
                )
                self.workers[pid] = worker
                worker.start()
        if self.analysis_thread is None:
            self.analysis_thread = threading.Thread(
                target=self.analysis_loop, daemon=True
            )
            self.analysis_thread.start()

    def stop(self):
        self.stop_event.set()
        self.visible.set()
        with self.lock:
            for process in self.processes.values():
                if process.poll() is None:
                    process.terminate()

    def snapshot(self, pid):
        with self.lock:
            return (
                dict(self.frames.get(pid, {})),
                dict(self.results.get(pid, {})),
                self.analyzing == pid,
            )

    def video_frames(self, pid, url):
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme not in {"rtsp", "http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Invalid camera video URL")
        options = (
            ["-rtsp_transport", "tcp", "-timeout", "10000000"]
            if parsed.scheme == "rtsp"
            else ["-rw_timeout", "10000000"]
        )
        command = [
            str(ffmpeg_runtime.executable()),
            "-hide_banner",
            "-loglevel",
            "error",
            *options,
            "-i",
            url,
            "-an",
            "-vf",
            "fps=4,scale=960:-2",
            "-threads",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "5",
            "pipe:1",
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self.lock:
            self.processes[pid] = process
        buffer = b""
        try:
            while not self.stop_event.is_set() and self.visible.is_set():
                with self.lock:
                    if self.sources.get(pid) != url:
                        return
                chunk = process.stdout.read1(65536)
                if not chunk:
                    raise OSError("Video stream ended")
                buffer += chunk
                if len(buffer) > 2_000_000:
                    raise ValueError("Oversized video frame")
                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9", max(start, 0))
                    if start < 0 or end < 0:
                        break
                    yield buffer[start : end + 2]
                    buffer = buffer[end + 2 :]
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            process.stdout.close()
            with self.lock:
                if self.processes.get(pid) is process:
                    self.processes.pop(pid, None)

    def accept_frame(self, pid, url, raw):
        now = time.time()
        fingerprint = hashlib.sha256(raw).hexdigest()
        with Image.open(io.BytesIO(raw)) as image:
            preview = image.convert("RGB")
            preview.thumbnail((640, 360))
        with self.lock:
            previous = self.frames.get(pid, {})
            changed = (
                now
                if previous.get("hash") != fingerprint
                else previous.get("changed_at", now)
            )
            if self.sources.get(pid) == url:
                self.frames[pid] = {
                    "bytes": raw,
                    "preview": preview,
                    "signature": preview.convert("L").resize((32, 18)).tobytes()
                    if now - previous.get("signature_at", 0) >= 1
                    else previous.get("signature"),
                    "signature_at": now
                    if now - previous.get("signature_at", 0) >= 1
                    else previous.get("signature_at"),
                    "at": now,
                    "changed_at": changed,
                    "hash": fingerprint,
                    "url": url,
                }

    def camera_loop(self, pid):
        while not self.stop_event.is_set():
            if not self.visible.wait(1):
                continue
            with self.lock:
                url = self.sources.get(pid)
            if not url:
                return
            delay = 1
            try:
                with self.lock:
                    video = (
                        pid in self.stream_ids
                        and time.time() >= self.fallback_until.get(pid, 0)
                    )
                    snapshot_url = self.snapshot_urls.get(pid) or url
                if video:
                    for raw in self.video_frames(pid, url):
                        self.accept_frame(pid, url, raw)
                else:
                    self.accept_frame(pid, url, self.capture(snapshot_url))
            except Exception:
                delay = 10
                fallback = False
                if video and snapshot_url != url:
                    try:
                        self.accept_frame(pid, url, self.capture(snapshot_url))
                        self.fallback_until[pid] = time.time() + 60
                        fallback, delay = True, 1
                    except Exception:
                        pass
                if not fallback:
                    with self.lock:
                        if self.sources.get(pid) == url:
                            self.frames[pid] = {
                                "error": "Camera unavailable · retrying",
                                "url": url,
                            }
            if self.stop_event.wait(delay):
                return

    def analyze(self, pid, frame):
        answer = observation(
            self.infer(
                PROMPT,
                [frame["bytes"]],
                model=self.settings().get("vision_model", "cale-vision:latest"),
            )
        )
        answer.update(
            signature=scene_signature(frame["bytes"]),
            frame=frame["bytes"],
            captured_at=frame["at"],
            checked_at=time.time(),
            url=frame["url"],
        )
        with self.lock:
            # An edited camera must never show results from its previous address.
            if not self.stop_event.is_set() and self.sources.get(pid) == frame["url"]:
                self.results[pid] = answer
        return answer

    def analysis_loop(self):
        while not self.stop_event.is_set():
            if not self.visible.wait(1):
                continue
            now = time.time()
            with self.lock:
                candidates = [
                    (self.results.get(pid, {}).get("checked_at", 0), pid, dict(frame))
                    for pid, frame in self.frames.items()
                    if scan_due(
                        frame, self.results.get(pid, {}), pid in self.busy_printers, now
                    )
                ]
            if not candidates or not AI_LOCK.acquire(blocking=False):
                self.stop_event.wait(1)
                continue
            try:
                checked, pid, frame = min(candidates, key=lambda item: item[0])
                with self.lock:
                    self.analyzing = pid
                try:
                    self.analyze(pid, frame)
                except Exception:
                    with self.lock:
                        if self.sources.get(pid) == frame["url"]:
                            self.results[pid] = {
                                "state": "unavailable",
                                "reason": "Local AI unavailable · retrying",
                                "checked_at": time.time(),
                            }
                finally:
                    with self.lock:
                        self.analyzing = None
            finally:
                AI_LOCK.release()
                self.stop_event.wait(1)


class CameraTile(ttk.Frame):
    def __init__(self, parent, live, pid, width=330, height=185):
        super().__init__(parent)
        self.live, self.pid, self.size = live, pid, (width, height)
        self.overlay = tk.BooleanVar(value=False)
        self.photo = None
        self.image_item = None
        self.last_key = None
        self.canvas = tk.Canvas(
            self,
            width=min(width, 300) if width <= 640 else width,
            height=height,
            background="#101821",
            highlightthickness=0,
        )
        self.canvas.pack(fill="x")
        self.canvas.bind("<Configure>", self.resize)
        self.caption = ttk.Label(self, text="Connecting camera…", style="Muted.TLabel")
        self.caption.pack(anchor="w", pady=(4, 2))
        self.detected = ttk.Label(
            self,
            text="AI: waiting for a live frame",
            wraplength=width,
            font=("Segoe UI", 10, "bold"),
        )
        self.detected.pack(anchor="w")
        self.reason = ttk.Label(self, text="", wraplength=width, style="Muted.TLabel")
        self.reason.pack(anchor="w", pady=(2, 4))
        buttons = ttk.Frame(self)
        buttons.pack(fill="x")
        self.mode_button = ttk.Button(
            buttons,
            text="AI overlay",
            command=lambda: self.overlay.set(not self.overlay.get()),
        )
        self.mode_button.pack(side="left")
        ttk.Button(buttons, text="Enlarge", command=self.enlarge).pack(side="right")
        self.timer = self.after(100, self.refresh)
        self.bind("<Destroy>", self.cleanup, add="+")

    def resize(self, event):
        height = max(90, min(self.size[1], int(event.width * 9 / 16)))
        if abs(self.canvas.winfo_height() - height) > 1:
            self.canvas.configure(height=height)
        self.detected.configure(wraplength=max(140, event.width))
        self.reason.configure(wraplength=max(140, event.width))

    def cleanup(self, event):
        if event.widget is self and self.timer:
            self.after_cancel(self.timer)
            self.timer = None

    def enlarge(self):
        window = tk.Toplevel(self.winfo_toplevel())
        window.title("Print Farm · " + self.pid + " camera")
        window.configure(background="#0b1016")
        tile = CameraTile(window, self.live, self.pid, 960, 540)
        tile.pack(fill="both", expand=True, padx=16, pady=16)
        tile.overlay.set(self.overlay.get())

    def refresh(self, force=False):
        if not self.winfo_exists():
            return
        if not force and (
            not self.winfo_ismapped() or self.winfo_toplevel().state() == "iconic"
        ):
            self.timer = self.after(750, self.refresh)
            return
        frame, result, analyzing = self.live.snapshot(self.pid)
        now = time.time()
        live_ok = (
            bool(frame.get("bytes"))
            and now - frame.get("at", 0) < 15
            and now - frame.get("changed_at", 0) < 15
        )
        overlay = self.overlay.get()
        self.mode_button.configure(text="Live camera" if overlay else "AI overlay")
        raw = (
            result.get("frame")
            if overlay and live_ok
            else frame.get("bytes")
            if live_ok
            else None
        )
        captured = result.get("captured_at", 0) if overlay else frame.get("at", 0)
        age = max(0, int(now - captured))
        caption = (
            (
                f"AI inspected frame · {age}s ago"
                if overlay
                else f"{'LIVE' if age < 5 else 'DELAYED'} · {age}s ago"
            )
            if raw
            else frame.get(
                "error",
                "Waiting for a changing camera frame"
                if frame.get("bytes")
                else "Connecting camera…",
            )
        )
        self.caption.configure(text=caption)
        key = (
            hash(raw) if raw else None,
            overlay,
            result.get("checked_at") if overlay else None,
            self.canvas.winfo_width(),
            self.canvas.winfo_height(),
        )
        if key != self.last_key:
            self.last_key = key
            self.canvas.delete("detection")
            if raw:
                cached = frame.get("preview") if not overlay else None
                picture = (
                    cached.copy()
                    if cached is not None and self.size[0] <= 640
                    else Image.open(io.BytesIO(raw)).convert("RGB")
                )
                picture.thumbnail(
                    (
                        max(1, min(self.size[0], self.canvas.winfo_width())),
                        max(1, self.canvas.winfo_height()),
                    )
                )
                if (
                    self.photo is not None
                    and (self.photo.width(), self.photo.height()) == picture.size
                ):
                    self.photo.paste(picture)
                else:
                    self.photo = ImageTk.PhotoImage(picture, master=self)
                offset = max(0, (self.canvas.winfo_width() - picture.width) / 2)
                if self.image_item is None:
                    self.canvas.delete("all")
                    self.image_item = self.canvas.create_image(
                        offset, 0, anchor="nw", image=self.photo
                    )
                else:
                    self.canvas.coords(self.image_item, offset, 0)
                    self.canvas.itemconfigure(self.image_item, image=self.photo)
                if overlay:
                    for item in result.get("objects", []):
                        x1, y1, x2, y2 = item["box"]
                        coords = (
                            offset + x1 * picture.width / 1000,
                            y1 * picture.height / 1000,
                            offset + x2 * picture.width / 1000,
                            y2 * picture.height / 1000,
                        )
                        self.canvas.create_rectangle(
                            *coords, outline="#ffc65c", width=2, tags="detection"
                        )
                        self.canvas.create_text(
                            coords[0] + 3,
                            max(10, coords[1] + 10),
                            anchor="w",
                            text=item["label"],
                            fill="#ffc65c",
                            font=("Segoe UI", 10, "bold"),
                            tags="detection",
                        )
            else:
                self.photo = None
                self.image_item = None
                self.canvas.delete("all")
                self.canvas.create_text(
                    self.size[0] / 2,
                    self.size[1] / 2,
                    text="Camera unavailable"
                    if not live_ok
                    else "Waiting for AI inspection…",
                    fill="#aebccc",
                )
        state = result.get("state", "waiting")
        age_ai = max(0, int(now - result.get("captured_at", now)))
        if not live_ok:
            text, color = "AI: waiting for a live camera", "#aebccc"
        elif result.get("frame"):
            labels = {
                "clear": "Bed looks clear",
                "occupied": "Object on bed",
                "obstructed": "View obstructed",
                "uncertain": "Uncertain",
            }
            text = f"AI: {labels.get(state, state)} · {result.get('confidence', 0):.0%} · {age_ai}s ago"
            if age_ai > 60:
                text = "AI: previous observation · " + str(age_ai) + "s ago"
            color = "#49d5b6" if state == "clear" and age_ai <= 60 else "#ffc65c"
        else:
            text, color = (
                "AI: " + result.get("reason", "Waiting for first scan"),
                "#aebccc",
            )
        if analyzing:
            text += " · analyzing…"
        self.detected.configure(text=text, foreground=color)
        self.reason.configure(text=result.get("reason", "") if live_ok else "")
        self.timer = self.after(250, self.refresh)
