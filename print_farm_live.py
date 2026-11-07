"""Live camera observations for the page; these never authorize printer motion."""

from __future__ import annotations

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

LIVE_FEED_MAX_AGE = 15

PROMPT = """Observe this 3D printer camera image. Identify visible prints, raised debris,
loose filament, hands, tools, and the overhead print head. Distinguish flat glue haze
and scratches from raised objects. Never assume an obscured bed is empty. This is
an observation, not permission to start a printer. Return concise JSON:
{"state":"clear|occupied|obstructed|uncertain", "confidence":0.0,
"reason":"one short sentence", "objects":[{"label":"short object name",
"box":[left,top,right,bottom]}]}. Boxes use image coordinates normalized to 0..1000.
Only include boxes for objects actually visible; at most six. A print head alone
is not an object on the bed, but can obstruct the view. A hand means occupied.
Look for raised edges and shadows along the visible plate, especially a dark
print blending into a dark plate near the bottom of the frame. Report visible
objects even when the rest of the plate is cut off.
Use clear only if the entire usable bed is visibly empty."""

FINISHED_PROMPT = """The printer reports that the print finished. The part may
still be on the plate. Check the visible plate and its edges for the finished
part. Use visible evidence for object boxes. If the plate is cut off or the
part is unclear, report obstructed or uncertain."""


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
        or frame.get("awaiting_frame")
        or now - frame.get("at", 0) >= 5
        or now - frame.get("changed_at", 0) >= LIVE_FEED_MAX_AGE
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
        self.processes, self.fallback_until = {}, {}
        self.busy_printers = set()
        self.finished_printers = set()
        self.analyzing = None
        self.analysis_thread = None

    def update(self, printers, visible, *, telemetry_fresh=True):
        sources = {
            p["id"]: (p.get("camera_stream_url") or "", p.get("camera_url") or "")
            for p in printers
            if p.get("camera_stream_url") or p.get("camera_url")
        }
        finished = {
            p["id"] for p in printers
            if telemetry_fresh and p.get("online") and p.get("unload_required")
        }
        with self.lock:
            for pid, process in list(self.processes.items()):
                if (
                    not visible or sources.get(pid) != self.sources.get(pid)
                ) and process.poll() is None:
                    process.terminate()
            self.busy_printers = {
                p["id"] for p in printers
                if telemetry_fresh and p.get("online") and not p.get("idle")
            }
            for pid in self.sources.keys() | sources.keys():
                previous = self.sources.get(pid)
                if previous == sources.get(pid):
                    # An unchanged poll keeps the identity. Switching away and
                    # back to the same address creates a new one.
                    sources[pid] = previous
                else:
                    self.frames.pop(pid, None)
                    self.results.pop(pid, None)
                    self.fallback_until.pop(pid, None)
            self.sources = sources
            for pid in self.finished_printers ^ finished:
                self.results.pop(pid, None)
                if pid in self.frames:
                    # Keep the raw view while the next frame becomes eligible.
                    self.frames[pid].update(feed=object(), awaiting_frame=True)
            self.finished_printers = finished
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

    def video_frames(self, pid, source):
        url, _ = source
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
                    if self.sources.get(pid) is not source:
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

    def accept_frame(self, pid, source, url, raw):
        now = time.time()
        with self.lock:
            if self.sources.get(pid) is not source:
                return
            previous = self.frames.get(pid, {})
            feed = previous.get("feed")
            if previous.get("url") == url and previous.get("bytes") == raw:
                # A new delivery of the same image is still a frozen scene.
                previous["at"] = now
                previous.pop("awaiting_frame", None)
                return
        with Image.open(io.BytesIO(raw)) as image:
            preview = image.convert("RGB")
            preview.thumbnail((640, 360))
        signature = preview.convert("L").resize((32, 18)).tobytes()
        with self.lock:
            previous = self.frames.get(pid, {})
            if self.sources.get(pid) is not source or previous.get("feed") is not feed:
                return
            resumed = now - previous.get("changed_at", now) >= LIVE_FEED_MAX_AGE
            if previous.get("url") != url or resumed:
                self.results.pop(pid, None)
                previous = {}
            self.frames[pid] = {
                "bytes": raw,
                "preview": preview,
                "signature": signature,
                "at": now,
                "changed_at": now,
                "url": url,
                "feed": previous.get("feed") or object(),
            }

    def camera_loop(self, pid):
        while not self.stop_event.is_set():
            if not self.visible.wait(1):
                continue
            with self.lock:
                source = self.sources.get(pid)
                if source is None:
                    return
                stream_url, snapshot_url = source
                video = stream_url and time.time() >= self.fallback_until.get(pid, 0)
            delay = 1
            try:
                if video:
                    for raw in self.video_frames(pid, source):
                        self.accept_frame(pid, source, stream_url, raw)
                else:
                    self.accept_frame(pid, source, snapshot_url, self.capture(snapshot_url))
            except Exception:
                with self.lock:
                    if self.sources.get(pid) is not source:
                        continue
                    self.frames[pid] = {"error": "Camera unavailable · retrying"}
                    self.results.pop(pid, None)
                delay = 10
                if video and snapshot_url and snapshot_url != stream_url:
                    try:
                        self.accept_frame(pid, source, snapshot_url, self.capture(snapshot_url))
                        with self.lock:
                            if self.sources.get(pid) is source:
                                self.fallback_until[pid] = time.time() + 60
                        delay = 1
                    except Exception:
                        pass
            if self.stop_event.wait(delay):
                return

    def analyze(self, pid, frame):
        with self.lock:
            if frame.get("awaiting_frame") or self.frames.get(pid, {}).get("feed") is not frame["feed"]:
                return None
            finished = pid in self.finished_printers
        answer = observation(
            self.infer(
                PROMPT + "\n\n" + FINISHED_PROMPT if finished else PROMPT,
                [frame["bytes"]],
                model=self.settings().get("vision_model", "cale-vision:latest"),
            )
        )
        if finished and answer["state"] == "clear":
            answer.update(
                state="uncertain",
                confidence=0,
                reason="Unload the finished print before checking for clearance",
            )
        answer.update(
            signature=scene_signature(frame["bytes"]),
            frame=frame["bytes"],
            captured_at=frame["at"],
            checked_at=time.time(),
            url=frame["url"],
        )
        with self.lock:
            # A feed or completion change invalidates outstanding scans.
            if not self.stop_event.is_set() and self.frames.get(pid, {}).get("feed") is frame["feed"]:
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
                        if self.frames.get(pid, {}).get("feed") is frame["feed"]:
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
        self.unload_required = False
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
            and now - frame.get("at", 0) < LIVE_FEED_MAX_AGE
            and now - frame.get("changed_at", 0) < LIVE_FEED_MAX_AGE
        )
        analysis_age = now - result.get("captured_at", 0)
        overlay = (
            live_ok
            and bool(result.get("frame"))
            and 0 <= analysis_age <= 60
            and not (
                result.get("signature") and frame.get("signature")
                and scene_changed(result["signature"], frame["signature"])
            )
        )
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
                    self.canvas.winfo_width() / 2,
                    self.canvas.winfo_height() / 2,
                    text="Camera unavailable",
                    fill="#aebccc",
                )
        state = result.get("state", "waiting")
        if not live_ok:
            text, color = "AI: waiting for a live camera", "#aebccc"
        elif self.unload_required and state != "occupied":
            text, color = "Printer finished · check the bed for the print", "#ffc65c"
        elif overlay:
            labels = {
                "clear": "Bed looks clear",
                "occupied": "Object on bed",
                "obstructed": "View obstructed",
                "uncertain": "Uncertain",
            }
            text = f"AI: {labels.get(state, state)} · {result.get('confidence', 0):.0%} · {int(analysis_age)}s ago"
            color = "#49d5b6" if state == "clear" else "#ffc65c"
        elif result.get("frame"):
            text, color = "AI: waiting for an updated scan", "#aebccc"
        else:
            text, color = (
                "AI: " + result.get("reason", "Waiting for first scan"),
                "#aebccc",
            )
        if analyzing:
            text += " · analyzing…"
        self.detected.configure(text=text, foreground=color)
        self.reason.configure(text=(result.get("reason", "") if overlay and not (
            self.unload_required and state == "clear"
        ) else ""))
        self.timer = self.after(250, self.refresh)
