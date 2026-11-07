"""Native Print Farm page with per-user account connection and one-click approval."""

from __future__ import annotations

import ipaddress
import math
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

from print_farm import BED_CLEAR_MAX_AGE, PRINTER_TELEMETRY_MAX_AGE, Service, digest
from print_farm_live import CameraTile, LiveCameras


class PrinterCard(tk.Frame):
    """Show printer telemetry beside its existing camera controls."""

    SURFACE = "#0e1725"
    MUTED = "#9cadc4"
    FILAMENT = {
        "black": ("#080b11", "#526075", "#a4b6c9"),
        "white": ("#f5f8ff", "#aebfd6", "#334765"),
    }
    STATUS = {
        "printing": ("Printing", "#50eaff"),
        "starting": ("Starting", "#50eaff"),
        "ready": ("Standby", "#78e6ab"),
        "complete": ("Print complete", "#78e6ab"),
        "fault": ("Needs attention", "#ffc879"),
        "offline": ("Offline", "#9cadc4"),
        "disconnected_busy": ("Connection lost", "#ffc879"),
        "stale": ("Signal delayed", "#ffc879"),
    }

    def __init__(self, parent, live, printer_id, settings, check_bed):
        super().__init__(parent, bg=self.SURFACE, highlightthickness=1,
                         highlightbackground="#2a3d58")
        self.last_state = None
        self.progress = None
        self.signal = self.STATUS["offline"][1]
        self.accent = tk.Frame(self, height=3, bg=self.MUTED)
        self.accent.pack(fill="x")
        body = tk.Frame(self, bg=self.SURFACE)
        body.pack(fill="both", expand=True, padx=13, pady=(11, 9))

        head = tk.Frame(body, bg=self.SURFACE)
        head.pack(fill="x")
        self.icon = tk.Canvas(head, width=34, height=34, bg="#16263c", highlightthickness=0)
        self.icon.pack(side="left", padx=(0, 9))
        self.icon.create_line(8, 25, 8, 9, 26, 9, 26, 25, 8, 25,
                              fill=self.signal, width=2, tags="printer")
        self.icon.create_line(9, 14, 25, 14, 17, 14, 17, 20,
                              fill=self.signal, tags="printer")
        self.icon.create_line(12, 22, 22, 22, fill=self.signal, tags="printer")
        self.name = tk.Label(head, bg=self.SURFACE, fg="#f0f7ff", anchor="w",
                             font=("Segoe UI", 13, "bold"))
        self.name.pack(side="left", fill="x", expand=True)
        ttk.Button(head, text="⚙ Settings", command=settings).pack(side="right", padx=(5, 0))

        telemetry = tk.Frame(body, bg=self.SURFACE)
        telemetry.pack(fill="x", pady=(5, 0))
        self.percent = tk.Label(telemetry, bg=self.SURFACE, fg=self.signal, anchor="w",
                                font=("Segoe UI", 31, "bold"), padx=0, pady=0)
        self.percent.pack(side="left")
        readings = tk.Frame(telemetry, bg=self.SURFACE)
        readings.pack(side="right")
        self.status = tk.Label(readings, anchor="e", bg="#16263c", fg=self.signal,
                               font=("Segoe UI", 10, "bold"), padx=8, pady=3)
        self.status.pack(anchor="e")
        self.material = tk.Label(readings, anchor="e", bg=self.SURFACE, fg=self.MUTED,
                                 font=("Segoe UI", 9), padx=0)
        self.material.pack(anchor="e")
        self.meter = tk.Canvas(body, height=10, width=1, bg=self.SURFACE, highlightthickness=0)
        self.meter.pack(fill="x", pady=(1, 9))
        self.meter.bind("<Configure>", self.draw_meter)

        metrics = tk.Frame(body, bg=self.SURFACE)
        metrics.pack(fill="x", pady=(0, 8))
        self.metrics = []
        for index, title in enumerate(("◷  TIME LEFT", "↑  NOZZLE", "▱  BED")):
            metrics.columnconfigure(index, weight=1, uniform="printer-metrics")
            cell = tk.Frame(metrics, bg="#142238", padx=5, pady=3)
            cell.grid(row=0, column=index, sticky="ew", padx=(0, 3) if index < 2 else 0)
            tk.Label(cell, text=title, fg=self.MUTED, bg="#142238",
                     font=("Segoe UI", 8, "bold")).pack()
            value = tk.Label(cell, fg="#e4f5ff", bg="#142238", font=("Segoe UI", 10, "bold"))
            value.pack()
            self.metrics.append(value)

        self.camera = CameraTile(body, live, printer_id, width=640, height=185)
        self.camera.canvas.configure(width=240)
        self.camera.pack(fill="x")
        bed_row = tk.Frame(body, bg=self.SURFACE)
        bed_row.pack(fill="x", pady=(8, 0))
        self.bed = tk.Label(bed_row, bg="#16263c", fg=self.MUTED, anchor="w",
                            font=("Segoe UI", 9, "bold"), padx=8, pady=4)
        self.check_bed = ttk.Button(bed_row, text="◎ Check bed", command=check_bed, state="disabled")
        self.check_bed.pack(side="right", padx=(4, 0))
        self.bed.pack(side="left", fill="x", expand=True)
        self.bed_note = tk.Label(body, bg=self.SURFACE, fg=self.MUTED, anchor="w",
                                 justify="left", font=("Segoe UI", 9))
        body.bind("<Configure>", lambda event: self.bed_note.configure(wraplength=max(120, event.width - 8)))

    def draw_meter(self, _event=None):
        self.meter.delete("segments")
        width = self.meter.winfo_width()
        if width <= 1:
            return
        pitch = width / 20
        for index in range(20):
            left = index * pitch
            right = (index + 1) * pitch - 2
            self.meter.create_rectangle(left, 1, right, 9, fill="#233349",
                                        width=0, tags="segments")
            filled = min(right, width * (self.progress or 0) / 100)
            if filled > left:
                self.meter.create_rectangle(left, 1, filled, 9, fill=self.signal,
                                            width=0, tags="segments")

    @staticmethod
    def read_number(raw, maximum):
        if isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and 0 <= value <= maximum else None

    @staticmethod
    def bed_observation_state(printer, now):
        bed = printer.get("bed") or {}
        state = bed.get("state")
        if state in {"clear", "blocked", "out_of_view", "unknown"}:
            if not printer.get("online") or not printer.get("idle"):
                return None
            sampled = bed.get("checked_at") if state == "unknown" else bed.get("captured_at")
            if type(sampled) not in (int, float) or not 0 <= now - sampled < BED_CLEAR_MAX_AGE:
                return "expired"
        return state

    def render(self, printer, *, telemetry_stale=False, bed_check=None):
        detail = printer.get("detail", "")
        if telemetry_stale:
            state = "stale"
        elif not printer.get("online"):
            state = "disconnected_busy" if printer.get("busy_unconfirmed") else "offline"
        elif detail == "Printer error":
            state = "fault"
        elif detail.startswith("Finished"):
            state = "complete"
        elif detail == "Starting print":
            state = "starting"
        else:
            state = "ready" if printer.get("idle") else "printing"
        progress = (self.read_number(printer.get("progress"), 100)
                    if state in {"printing", "complete"} else None)
        seconds = (self.read_number(printer.get("seconds_remaining"), 604800)
                   if state == "printing" else None)
        eta = "—"
        if seconds:
            hours, minutes = divmod(math.ceil(seconds / 60), 60)
            eta = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
        temperatures = []
        for field in ("nozzle_c", "bed_c"):
            temp = (
                self.read_number(printer.get(field), 500)
                if printer.get("online") and not telemetry_stale else None
            )
            temperatures.append("—" if temp is None else f"{temp:.0f}°C")
        bed_state = None if telemetry_stale else self.bed_observation_state(printer, time.time())
        if state == "complete" and bed_state == "clear":
            state, progress = "ready", None
        self.camera.unload_required = state == "complete"
        if state == "complete" and bed_state in {None, "unchecked", "expired"}:
            bed_state = "unload_required"
        bed_label, bed_color = {
            "clear": ("✓  Build plate clear", "#78e6ab"),
            "blocked": ("!  Build plate occupied", "#ffc879"),
            "out_of_view": ("◇  Bed out of view", "#ffc879"),
            "unknown": ("◇  Build plate unverified", self.MUTED),
            "expired": ("◇  Bed check expired", self.MUTED),
            "unload_required": ("!  Unload finished print", "#ffc879"),
            "queued": ("◇  Check queued", self.MUTED),
            "checking": ("◎  Checking bed…", "#50eaff"),
            "skipped_busy": ("◇  Skipped · printer busy", self.MUTED),
            "skipped_offline": ("◇  Skipped · offline", self.MUTED),
            "skipped_camera_changed": ("◇  Camera changed", self.MUTED),
        }.get(bed_check or bed_state, ("◇  Bed check pending", self.MUTED))
        bed_note = ""
        if not bed_check and bed_state in {"blocked", "out_of_view", "unknown", "unload_required"}:
            note = (printer.get("bed") or {}).get("reason")
            if isinstance(note, str) and note != "Checked before the next print":
                bed_note = note
            if bed_state == "unload_required":
                bed_note = "Cale automatically watches for removal. Keep the whole plate visible."
        name = printer.get("hostname") or printer["id"]
        color = str(printer.get("color") or "").strip()
        readings = (eta, *temperatures)
        can_check = (
            not bed_check and state in {"ready", "complete"}
            and bool(printer.get("idle") and printer.get("camera_url"))
        )
        shown = (state, progress, bed_label, bed_note, name, color, readings, can_check)
        if self.last_state == shown:
            return
        self.last_state = shown
        self.progress = progress
        label, self.signal = self.STATUS[state]
        filament, edge, icon_surface = self.FILAMENT.get(
            color.casefold(), (self.MUTED, "#2a3d58", "#16263c")
        )
        self.configure(highlightbackground=edge, highlightcolor=edge)
        self.name.configure(text=name)
        self.percent.configure(text="—" if progress is None else f"{progress:.0f}%",
                               fg=self.signal)
        self.status.configure(text="●  " + label.upper(), fg=self.signal)
        self.accent.configure(bg=filament)
        self.icon.configure(bg=icon_surface)
        self.icon.itemconfigure("printer", fill=filament)
        self.material.configure(text=(color.upper() + " FILAMENT") if color else "LOCAL PRINTER")
        self.bed.configure(text=bed_label, fg=bed_color)
        if bed_note:
            self.bed_note.configure(text=bed_note)
            self.bed_note.pack(fill="x", pady=(3, 0))
        else:
            self.bed_note.pack_forget()
        self.check_bed.configure(state="normal" if can_check else "disabled")
        for label, value in zip(self.metrics, readings):
            label.configure(text=value)
        self.draw_meter()


class Controls:
    def __init__(self, parent, window, root, service=None):
        self.parent, self.window = parent, window
        self.service = service or Service(root)
        self.live = LiveCameras(self.service.settings)
        self.camera_tiles = {}
        self.printer_cards = {}
        self.printer_data = {}
        self.order_signature = None
        self.grid_columns = 0
        self.events = queue.Queue()
        self.busy = False
        self.last_render = ""
        style = ttk.Style(window)
        style.configure(
            "PrintFarm.TLabelframe",
            background="#0b1016",
            bordercolor="#29364a",
            relief="solid",
        )
        style.configure(
            "PrintFarm.TLabelframe.Label",
            background="#0b1016",
            foreground="#aebccc",
            font=("Segoe UI", 10, "bold"),
        )
        self.frame = ttk.Frame(parent, padding=(8, 8, 8, 0))
        self.frame.pack(fill="both", expand=True)
        heading = ttk.Frame(self.frame)
        heading.pack(fill="x", pady=(0, 8))
        ttk.Label(heading, text="Print Farm", font=("Segoe UI", 26, "bold")).pack(
            side="left"
        )
        self.toggle = ttk.Button(
            heading, text="Start automatic printing", command=self.toggle_printing
        )
        self.toggle.pack(side="right", padx=(8, 0))
        self.connect_button = ttk.Button(
            heading, text="Connect eBay account", command=self.connect
        )
        self.connect_button.pack(side="right")
        self.account_button = ttk.Button(
            heading, text="Disconnect", command=self.disconnect
        )
        self.status = tk.StringVar(value="Connecting to your local print service…")
        ttk.Label(self.frame, textvariable=self.status, wraplength=1000).pack(
            fill="x", pady=(0, 8)
        )
        shell = ttk.Frame(self.frame)
        shell.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(shell, highlightthickness=0, background="#0b1016")
        scroll = ttk.Scrollbar(shell, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas, padding=(0, 0, 8, 0))
        item = self.canvas.create_window((0, 0), anchor="nw", window=self.content)
        self.content.bind(
            "<Configure>",
            lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )

        def resize(event):
            self.canvas.itemconfigure(item, width=event.width)
            self.layout_printers(event.width)

        self.canvas.bind("<Configure>", resize)
        self.wheel_binding = self.window.bind("<MouseWheel>", self.scroll_page, add="+")
        top = ttk.Frame(self.content)
        top.pack(fill="x")
        self.fleet_status = ttk.Label(top, text="PRINTERS", font=("Segoe UI", 10, "bold"))
        self.fleet_status.pack(side="left")
        ttk.Button(top, text="Add printer", command=self.add_printer).pack(side="right")
        ttk.Button(top, text="Refresh files", command=self.refresh_files).pack(
            side="right", padx=8
        )
        self.printers_frame = ttk.Frame(self.content)
        self.printers_frame.pack(fill="x", pady=(8, 14))
        self.no_printers = ttk.Label(
            self.printers_frame,
            text="No printers connected. Add your printer and its bed camera to begin.",
        )
        ttk.Label(
            self.content, text="SALES & PRINT QUEUE", font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", pady=(0, 8))
        self.orders_frame = ttk.Frame(self.content, padding=8)
        self.orders_frame.pack(fill="both", expand=True)
        self.service.start()
        self.parent.bind("<Destroy>", self.on_destroy, add="+")
        self.tick()

    def on_destroy(self, event):
        if event.widget is self.parent:
            if self.wheel_binding:
                self.window.unbind("<MouseWheel>", self.wheel_binding)
            self.live.stop()
            self.service.stop()

    def toggle_printing(self):
        settings = self.service.settings()
        values = {"enabled": not settings.get("enabled")}
        if values["enabled"] and not settings.get("enabled_since"):
            values["enabled_since"] = time.time()
        self.service.configure(values)
        self.status.set(
            "Automatic printing enabled"
            if values["enabled"]
            else "Paused. Active prints continue; no new prints will start."
        )

    def background(self, fn, done):
        if self.busy:
            return
        self.busy = True
        self.connect_button.configure(state="disabled")

        def run():
            try:
                self.events.put((done, fn(), None))
            except Exception as exc:
                self.events.put((done, None, str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def connect(self):
        from print_farm_account import connect

        self.status.set("Opening eBay sign-in…")
        self.service.configure({"enabled": False})

        def done(record):
            self.service.configure(
                {
                    "account_mode": "connected",
                    "account_scope": record["id"],
                    "enabled_since": time.time(),
                }
            )
            self.status.set(
                "eBay connected. Add your printers, then start automatic printing."
            )

        self.background(
            lambda: connect(
                self.service.root,
                on_status=lambda text: self.events.put(("status", text, None)),
                cancelled=self.service.stop_event.is_set,
            ),
            done,
        )

    def disconnect(self):
        from print_farm_account import disconnect

        self.service.configure({"enabled": False, "account_mode": "disconnected"})
        self.background(
            lambda: disconnect(self.service.root),
            lambda _: self.status.set("eBay disconnected. Printing paused."),
        )

    def refresh_files(self):
        self.service.last_inventory = 0
        self.service.proposal_retry.clear()
        self.service.wake.set()
        self.status.set("Refreshing saved printer files…")

    def add_printer(self, existing=None):
        dialog = tk.Toplevel(self.window)
        dialog.title("Print Farm · Printer")
        dialog.geometry("620x520")
        panel = ttk.Frame(dialog, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="Connect a printer", font=("Segoe UI", 19, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            panel,
            text="Creality K1 SE · saved PLA G-code · LAN connection",
            wraplength=560,
        ).pack(anchor="w", pady=(4, 14))
        values = {}
        for field, label in [
            ("ip", "Printer LAN IP"),
            ("hostname", "Printer hostname (as shown on the printer)"),
            ("color", "Loaded PLA color: black or white"),
            ("camera_url", "Live camera JPEG snapshot URL"),
            ("camera_stream_url", "Video stream URL (optional, RTSP or HTTP)"),
        ]:
            ttk.Label(panel, text=label).pack(anchor="w", pady=(6, 2))
            values[field] = tk.StringVar(value=(existing or {}).get(field, ""))
            ttk.Entry(panel, textvariable=values[field]).pack(fill="x")
        result = tk.StringVar()
        ttk.Label(panel, textvariable=result, wraplength=560).pack(fill="x", pady=8)

        def save():
            printer = {k: v.get().strip() for k, v in values.items()}
            try:
                address = ipaddress.ip_address(printer["ip"])
                if not address.is_private or address.version != 4:
                    raise ValueError("Use the printer’s local IPv4 address.")
                if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", printer["hostname"]):
                    raise ValueError("Enter the exact printer hostname.")
                if printer["color"].lower() not in ("black", "white"):
                    raise ValueError(
                        "This driver currently supports black or white PLA."
                    )
                if not printer["camera_url"].startswith(("http://", "https://")):
                    raise ValueError(
                        "Enter the live JPEG snapshot URL for the bed camera."
                    )
                if printer["camera_stream_url"]:
                    video = urlsplit(printer["camera_stream_url"])
                    if (
                        video.scheme not in ("rtsp", "http", "https")
                        or not video.hostname
                        or video.username
                        or video.password
                    ):
                        raise ValueError(
                            "Use an RTSP or HTTP video URL without embedded credentials."
                        )
            except ValueError as exc:
                result.set(str(exc))
                return
            printer.update(
                id=(existing or {}).get("id", "printer-" + digest(printer["ip"])[:10]),
                driver="creality_k1_se",
                color=printer["color"].lower(),
            )
            settings = self.service.settings()
            printers = [
                p for p in settings.get("printers", []) if p["id"] != printer["id"]
            ]
            if any(p["ip"] == printer["ip"] for p in printers):
                result.set("This IP is already registered to another printer.")
                return
            self.service.configure({"printers": printers + [printer]})
            self.refresh_files()
            dialog.destroy()

        ttk.Button(panel, text="Save printer", command=save).pack(anchor="e")

    def scroll_page(self, event):
        if self.parent.winfo_ismapped():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

    def layout_printers(self, width):
        columns = 1 if width < 680 else 2 if width < 1280 else 4
        self.grid_columns = columns
        for i in range(4):
            self.printers_frame.columnconfigure(
                i,
                weight=1 if i < columns else 0,
                uniform="cameras" if i < columns else "",
            )
        for index, pid in enumerate(self.printer_data):
            card = self.printer_cards[pid]
            card.grid(
                row=index // columns,
                column=index % columns,
                sticky="nsew",
                padx=(0, 8),
                pady=(0, 8),
            )

    def render(self, snapshot, *, telemetry_stale=False, bed_checks=None):
        printers = snapshot.get("printers", [])
        online = sum(bool(printer.get("online")) for printer in printers)
        busy = sum(bool(printer.get("online") and not printer.get("idle")) for printer in printers)
        self.fleet_status.configure(
            text="PRINTERS    ◇ SIGNAL DELAYED" if telemetry_stale
            else f"PRINTERS    ● {online}/{len(printers)} ONLINE    ◇ {busy} BUSY"
        )
        previous_ids = tuple(self.printer_data)
        self.printer_data = {p["id"]: p for p in printers}
        for pid in list(self.printer_cards):
            if pid not in self.printer_data:
                self.printer_cards.pop(pid).destroy()
                self.camera_tiles.pop(pid, None)
        if not printers:
            self.no_printers.grid(row=0, column=0, sticky="w", pady=16)
        else:
            self.no_printers.grid_remove()
        for printer in printers:
            pid = printer["id"]
            if pid not in self.printer_cards:
                card = PrinterCard(
                    self.printers_frame, self.live, pid,
                    lambda key=pid: self.add_printer(self.printer_data.get(key)),
                    lambda key=pid: self.service.request_bed_check(key),
                )
                self.printer_cards[pid] = card
                self.camera_tiles[pid] = card.camera
            self.printer_cards[pid].render(
                printer, telemetry_stale=telemetry_stale, bed_check=(bed_checks or {}).get(pid)
            )
        if previous_ids != tuple(self.printer_data):
            self.layout_printers(self.canvas.winfo_width())
        rows = snapshot.get("orders", [])
        order_signature = digest([snapshot.get("connected"), rows])
        if order_signature == self.order_signature:
            return
        self.order_signature = order_signature
        for child in self.orders_frame.winfo_children():
            child.destroy()
        if not rows:
            ttk.Label(
                self.orders_frame,
                text="No unfulfilled paid sales waiting to print."
                if snapshot.get("connected")
                else "Connect your eBay account. Your sales and print queue stay separate from other users.",
                wraplength=800,
                font=("Segoe UI", 12),
            ).pack(anchor="w", pady=24)
        labels = {
            "approval": "Needs your OK",
            "queued": "Waiting for printer",
            "printing": "Printing",
            "completed": "Complete",
            "held": "Needs inspection",
            "needs_file": "Needs a print file",
        }
        for row in rows[:200]:
            card = ttk.LabelFrame(
                self.orders_frame,
                style="PrintFarm.TLabelframe",
                text=labels.get(row["status"], row["status"]),
                padding=12,
            )
            card.pack(fill="x", pady=(0, 10))
            head = ttk.Frame(card)
            head.pack(fill="x")
            ttk.Label(
                head, text=row["title"], wraplength=850, font=("Segoe UI", 12, "bold")
            ).pack(side="left", fill="x", expand=True)
            if row["status"] == "approval":
                button = ttk.Button(head, text="OK")

                def approve(r=row, b=button):
                    self.service.approve(r["key"], r["token"])
                    b.configure(state="disabled", text="Queued")
                    self.status.set(
                        "File approved. Starting when a compatible printer has a clear bed."
                    )

                button.configure(command=approve)
                button.pack(side="right", padx=10, ipadx=18, ipady=6)
            ttk.Label(
                card,
                text=f"Order {row['order_id']} · {row['quantity']} purchased · {row.get('completed', 0)}/{row.get('total', '?')} prints complete",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(5, 0))
            if row.get("filename"):
                ttk.Label(
                    card,
                    text=f"{row['filename']}  ·  {round(row['confidence'] * 100)}% file match",
                    wraplength=980,
                ).pack(anchor="w", pady=(5, 0))
            if (row.get("proposal") or {}).get("historical"):
                ttk.Label(
                    card,
                    text="Imported from before this automation was enabled; review remaining prints once.",
                    style="Muted.TLabel",
                ).pack(anchor="w")
            ttk.Label(card, text=row.get("reason", ""), wraplength=980).pack(
                anchor="w", pady=(4, 0)
            )

    def tick(self):
        if not self.parent.winfo_exists():
            return
        try:
            while True:
                done, value, error = self.events.get_nowait()
                if done == "status":
                    self.status.set(value)
                    continue
                self.busy = False
                self.connect_button.configure(state="normal")
                if error:
                    self.status.set(error)
                    messagebox.showerror("Print Farm", error, parent=self.window)
                else:
                    done(value)
        except queue.Empty:
            pass
        snapshot = self.service.snapshot
        now = time.time()
        telemetry_stale = bool(snapshot.get("printers")) and not (
            0 <= now - snapshot.get("printer_telemetry_at", 0) <= PRINTER_TELEMETRY_MAX_AGE
        )
        self.live.update(
            snapshot.get("printers", []),
            bool(self.parent.winfo_ismapped()) and self.window.state() != "iconic",
            telemetry_fresh=not telemetry_stale,
        )
        self.toggle.configure(
            text="Pause automatic printing"
            if self.service.settings().get("enabled")
            else "Start automatic printing"
        )
        if snapshot.get("connected"):
            self.connect_button.configure(text="Reconnect eBay")
            if not self.account_button.winfo_manager():
                self.account_button.pack(side="right", padx=8)
        else:
            self.connect_button.configure(text="Connect eBay account")
            self.account_button.pack_forget()
        bed_checks = self.service.bed_check_states()
        signature = digest([
            {
                k: v
                for k, v in snapshot.items()
                if k not in ("checked_at", "ebay_checked_at", "printer_telemetry_at")
            },
            telemetry_stale,
            bed_checks,
            [PrinterCard.bed_observation_state(printer, now) for printer in snapshot.get("printers", [])],
        ])
        if signature != self.last_render:
            self.render(snapshot, telemetry_stale=telemetry_stale, bed_checks=bed_checks)
            self.last_render = signature
            if not self.busy:
                self.status.set(snapshot.get("status", ""))
        self.window.after(1000, self.tick)
