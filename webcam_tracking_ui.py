"""A live, local view of detections on the exact webcam frame inspected."""

import base64
import io
import queue
import time
import tkinter as tk
from pathlib import Path
from tkinter import simpledialog, ttk

from PIL import Image, ImageTk

from webcam_tracking import read_json, valid_region, write_json


class TrackingView:
    def __init__(self, parent, root, generation, camera_on):
        self.parent, self.root = parent, Path(root)
        self.generation, self.camera_on = generation, camera_on
        self.pending = queue.Queue(maxsize=1)
        self.dialog = None
        self.result = None
        self.selected = None
        self.photo = None
        self.decoded = None
        self.decoded_id = None
        self.image_item = None
        self.area_mode = False
        self.region_start = None
        self.notice_until = 0
        self.config = self.root/"Media/webcam-tracking.json"
        self.was_enabled = read_json(self.config).get("enabled") is True

    def restore(self):
        """Restore the panel only while the existing camera permission is active."""
        if self.was_enabled and self.camera_on():
            self.open()

    def offer(self, result, generation):
        if generation != self.generation() or not isinstance(result, dict):
            return
        try:
            self.pending.get_nowait()
        except queue.Empty:
            pass
        try:
            self.pending.put_nowait((generation, result))
        except queue.Full:
            pass

    def notice(self, text):
        if self.dialog and self.dialog.winfo_exists():
            self.status.set(text)
            self.notice_until = time.time()+8

    def clear(self):
        self.result, self.selected, self.photo = None, None, None
        self.decoded, self.decoded_id, self.image_item = None, None, None
        self.area_mode, self.region_start = False, None
        if self.dialog and self.dialog.winfo_exists():
            self.canvas.delete("all")
            self.status.set("Waiting for a fresh camera frame…")

    def open(self):
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        self.clear()
        config = read_json(self.config)
        write_json(self.config, {**config, "enabled": True})
        dialog = self.dialog = tk.Toplevel(self.parent)
        dialog.title("Cale · Webcam AI")
        dialog.geometry("1120x850")
        dialog.minsize(780, 650)
        dialog.configure(background="#0b1119")
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Webcam AI", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(body, text="Live people & objects · Click a box, or Select area for a missed object").pack(anchor="w", pady=(4, 10))
        self.canvas = tk.Canvas(body, background="#101923", highlightthickness=0, height=540)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag_area)
        self.canvas.bind("<ButtonRelease-1>", self.finish_area)
        self.canvas.bind("<Configure>", lambda _: self.draw())
        self.status = tk.StringVar(value="Preparing local people and object detection…")
        ttk.Label(body, textvariable=self.status, wraplength=1020).pack(anchor="w", pady=(8, 4))
        self.people = tk.StringVar(value="Known people: loading…")
        ttk.Label(body, textvariable=self.people).pack(anchor="w", pady=(0, 8))
        scan_actions = ttk.Frame(body)
        scan_actions.pack(fill="x", pady=(0, 6))
        ttk.Button(scan_actions, text="Select area", command=self.begin_area).pack(side="left", padx=(0, 8))
        ttk.Button(scan_actions, text="Look closer", command=self.focus_area).pack(side="left", padx=(0, 8))
        ttk.Button(scan_actions, text="Scan whole view", command=self.whole_view).pack(side="left", padx=(0, 8))
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        for name in config.get("quick_names", [])[:4]:
            ttk.Button(actions, text="Learn " + name, command=lambda n=name: self.learn_person(n)).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Name selected", command=self.learn_selected).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Resume live", command=self.resume).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Saved names", command=self.manage).pack(side="right")
        ttk.Label(body, text="Names come from taught face samples, never a seat. Unknown faces stay unknown. "
                  "Object names need visible detail; teaching another angle adds an example.", wraplength=1020).pack(anchor="w", pady=(12, 0))
        dialog.protocol("WM_DELETE_WINDOW", self.close)
        self.session_generation = self.generation()
        self.poll()

    def close(self):
        write_json(self.config, {**read_json(self.config), "enabled": False})
        self.clear()
        if self.dialog:
            self.dialog.destroy()
        self.dialog = None

    def poll(self):
        if not self.dialog or not self.dialog.winfo_exists():
            return
        if self.session_generation != self.generation() or not self.camera_on():
            self.clear()
            self.session_generation = self.generation()
        try:
            generation, result = self.pending.get_nowait()
            if generation == self.generation() and self.camera_on() and not self.selected:
                self.result = result
                self.draw()
                names = ", ".join(result.get("known_people", [])) or "none yet"
                self.people.set("Known people: " + names + "  ·  Named objects: " + str(len(result.get("known_objects", [])))
                                + ("  ·  " + result["learning"] if result.get("learning") else ""))
        except queue.Empty:
            pass
        if self.result:
            age = time.time()-self.result.get("at", 0)
            if age > 20:
                self.clear()
                self.status.set("No fresh detections. Check that Raw camera is on.")
            elif time.time() >= self.notice_until:
                self.status.set(("Selected frame" if self.selected else "LIVE detection") +
                                f" · {age:.1f}s ago · " + str(len(self.result.get("items", []))) + " boxes")
        self.dialog.after(100, self.poll)

    def draw(self):
        if not self.result or not self.dialog or not self.dialog.winfo_exists():
            return
        try:
            token = self.result.get("frame_id")
            if self.decoded is None or self.decoded_id != token:
                raw = base64.b64decode(self.result.get("image", ""), validate=True)
                with Image.open(io.BytesIO(raw)) as original:
                    self.decoded = original.convert("RGB")
                self.decoded_id = token
            image = self.decoded
            cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
            scale = min(cw/image.width, ch/image.height)
            width, height = max(1, round(image.width*scale)), max(1, round(image.height*scale))
            image = image.resize((width, height), Image.Resampling.BILINEAR)
            if self.photo is not None and (self.photo.width(), self.photo.height()) == (width, height):
                self.photo.paste(image)
            else:
                self.photo = ImageTk.PhotoImage(image)
            self.origin = ((cw-width)/2, (ch-height)/2, width, height)
            self.canvas.delete("overlay")
            ox, oy, _, _ = self.origin
            if self.image_item is None:
                self.image_item = self.canvas.create_image(ox, oy, anchor="nw", image=self.photo)
            else:
                self.canvas.coords(self.image_item, ox, oy)
                self.canvas.itemconfigure(self.image_item, image=self.photo)
            focus = self.result.get("focus_box")
            if valid_region(focus):
                x, y, w, h = focus
                self.canvas.create_rectangle(ox+x*width, oy+y*height, ox+(x+w)*width, oy+(y+h)*height,
                                             outline="#ffe27b", dash=(6, 4), width=2, tags="overlay")
                self.canvas.create_text(ox+x*width+4, oy+y*height+4, text="CLOSE SCAN", anchor="nw",
                                        fill="#ffe27b", font=("Segoe UI", 10, "bold"), tags="overlay")
            for item in self.result.get("items", []):
                x, y, w, h = item["box"]
                color = "#ffe27b" if self.selected == item["id"] else "#55e1b4" if item.get("remembered") else "#75c9ff"
                x1, y1, x2, y2 = ox+x*width, oy+y*height, ox+(x+w)*width, oy+(y+h)*height
                self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=3, tags="overlay")
                label = item["label"] + (" · face" if item["kind"] == "face" else "" if item["id"] == "selected-area" else f" #{item['id']}")
                text = self.canvas.create_text(x1+4, max(oy+2, y1-22), text=label, fill="#071018", anchor="nw", font=("Segoe UI", 11, "bold"), tags="overlay")
                box = self.canvas.bbox(text)
                rect = self.canvas.create_rectangle(box[0]-3, box[1]-1, box[2]+3, box[3]+1, fill=color, outline="", tags="overlay")
                self.canvas.tag_raise(text, rect)
        except (ValueError, OSError, KeyError, TypeError):
            self.status.set("Waiting for a valid camera frame…")

    def select(self, event):
        if not self.result or not hasattr(self, "origin"):
            return
        ox, oy, width, height = self.origin
        x, y = (event.x-ox)/width, (event.y-oy)/height
        choices = [item for item in self.result.get("items", []) if item["box"][0] <= x <= item["box"][0]+item["box"][2]
                   and item["box"][1] <= y <= item["box"][1]+item["box"][3]]
        if choices:
            item = min(choices, key=lambda item: (item["kind"] != "face", item["box"][2]*item["box"][3]))
            self.selected = item["id"]
            self.notice("Selected " + item["label"] + ". Choose Learn or Name selected.")
            self.draw()

    def resume(self):
        self.selected = None
        self.area_mode, self.region_start = False, None
        self.notice_until = 0

    def begin_area(self):
        if not self.result:
            self.notice("Wait for a camera frame first.")
            return
        self.area_mode, self.selected = True, "drawing-area"
        self.notice("Drag a tight rectangle around the object, then choose Look closer or Name selected.")

    def point(self, event):
        ox, oy, width, height = self.origin
        return max(0., min(1., (event.x-ox)/width)), max(0., min(1., (event.y-oy)/height))

    def press(self, event):
        if self.area_mode and self.result and hasattr(self, "origin"):
            self.region_start = self.point(event)
        else:
            self.select(event)

    def drag_area(self, event):
        if self.area_mode and self.region_start:
            x, y = self.region_start
            ex, ey = self.point(event)
            ox, oy, width, height = self.origin
            self.canvas.delete("area-drag")
            self.canvas.create_rectangle(ox+x*width, oy+y*height, ox+ex*width, oy+ey*height,
                                         outline="#ffe27b", width=3, tags="area-drag")

    def finish_area(self, event):
        if not self.area_mode or not self.region_start:
            return
        x, y = self.region_start
        ex, ey = self.point(event)
        box = [min(x, ex), min(y, ey), abs(ex-x), abs(ey-y)]
        self.canvas.delete("area-drag")
        self.region_start = None
        if not valid_region(box):
            self.notice("Draw a slightly larger rectangle around the object.")
            return
        self.result["items"] = [item for item in self.result["items"] if item["id"] != "selected-area"]
        self.result["items"].append({"id": "selected-area", "kind": "object", "class": "manual", "label": "Selected area", "box": box})
        self.area_mode, self.selected = False, "selected-area"
        self.notice("Area selected. Look closer runs detection here at a larger scale.")
        self.draw()

    def focus_area(self):
        item = next((item for item in (self.result or {}).get("items", []) if item["id"] == self.selected), None)
        if not item or not valid_region(item.get("box")):
            self.notice("Use Select area and drag around the object first.")
            return
        write_json(self.config, {**read_json(self.config), "focus_box": item["box"]})
        self.resume()
        self.notice("Looking closer in the selected area. Keep the object there, or choose Scan whole view.")

    def whole_view(self):
        config = read_json(self.config)
        config.pop("focus_box", None)
        write_json(self.config, config)
        self.resume()
        self.notice("Scanning the whole view, with an extra close scan for objects held near a person.")

    def submit(self, item, name):
        if not self.result or time.time()-self.result.get("at", 0) > 18:
            self.notice("That view expired. Resume live and select again.")
            return
        request = {"frame_id": self.result["frame_id"], "id": item["id"], "name": name}
        if item["id"] == "selected-area":
            request.update(box=item["box"], category=item.get("category", ""))
        write_json(self.root/"Media/webcam-learn-request.json", request)
        self.selected = None
        self.notice("Learning " + name + "…")

    def learn_person(self, name):
        faces = [item for item in (self.result or {}).get("items", []) if item["kind"] == "face"]
        chosen = next((item for item in faces if item["id"] == self.selected), None)
        if chosen is None and len(faces) == 1:
            chosen = faces[0]
        if chosen is None:
            self.notice("Click the right face first, or have only that person in view.")
            return
        if chosen.get("remembered") and chosen.get("label") != name:
            self.notice("That face is already learned as " + chosen["label"] + ". Select " + name + "'s face; use Name selected only to correct a name.")
            return
        self.submit(chosen, name)

    def learn_selected(self):
        chosen = next((item for item in (self.result or {}).get("items", []) if item["id"] == self.selected), None)
        if chosen is None or chosen.get("class") == "person":
            self.notice("Click a face box or a specific object box first.")
            return
        if chosen["id"] == "selected-area":
            category = simpledialog.askstring("Object type", "What type of object is in this area? For example: spoon, cup, book.", parent=self.dialog)
            if not category:
                return
            chosen["category"] = category.strip().lower()
        name = simpledialog.askstring("Teach a name", "Name for this " + ("person" if chosen["kind"] == "face" else "object") + ":", parent=self.dialog)
        if name and name.strip():
            self.submit(chosen, name.strip()[:60])

    def manage(self):
        dialog = tk.Toplevel(self.dialog)
        dialog.title("Locally saved names")
        dialog.geometry("420x360")
        rows = tk.Listbox(dialog, exportselection=False)
        rows.pack(fill="both", expand=True, padx=12, pady=12)
        mapping = []
        for kind, filename in (("Person", "face-embeddings.json"), ("Object", "object-memories.json")):
            path = self.root/"Media"/filename
            for name in read_json(path):
                mapping.append((path, name))
                rows.insert("end", kind + ": " + name)
        def forget():
            selected = rows.curselection()
            if not selected:
                return
            path, name = mapping[selected[0]]
            memory = read_json(path)
            write_json(path.with_suffix(".previous.json"), memory)
            memory.pop(name, None)
            write_json(path, memory)
            rows.delete(selected[0])
            mapping.pop(selected[0])
        ttk.Button(dialog, text="Forget selected", command=forget).pack(pady=(0, 12))
