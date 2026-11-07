"""Native Print Farm page with per-user account connection and one-click approval."""

from __future__ import annotations

import ipaddress
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

from print_farm import Service, digest
from print_farm_live import CameraTile, LiveCameras


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
        ttk.Label(
            self.frame,
            text="Sales import automatically. File matches above 50% start when a compatible printer is clear. Matches at 50% or below wait for OK.",
            style="Muted.TLabel",
            wraplength=1050,
        ).pack(fill="x")
        ttk.Label(
            self.frame,
            text="After someone removes a finished print, AI checks the empty bed and starts the next queued job. Keep Cale and this PC running.",
            style="Muted.TLabel",
            wraplength=1050,
        ).pack(fill="x", pady=(2, 16))
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
        ttk.Label(top, text="PRINTERS", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(top, text="Add printer", command=self.add_printer).pack(side="right")
        ttk.Button(top, text="Refresh files", command=self.refresh_files).pack(
            side="right", padx=8
        )
        ttk.Label(
            self.content,
            text="LIVE CAMERAS · AI adapts to scene changes. AI overlay shows the inspected frame.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 0))
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
        columns = 1 if width < 680 else 2 if width < 1450 else 4
        self.grid_columns = columns
        for i in range(4):
            self.printers_frame.columnconfigure(
                i,
                weight=1 if i < columns else 0,
                uniform="cameras" if i < columns else "",
            )
        for index, pid in enumerate(self.printer_data):
            card = self.printer_cards[pid][0]
            card.grid(
                row=index // columns,
                column=index % columns,
                sticky="nsew",
                padx=(0, 8),
                pady=(0, 8),
            )

    def render(self, snapshot):
        printers = snapshot.get("printers", [])
        previous_ids = tuple(self.printer_data)
        self.printer_data = {p["id"]: p for p in printers}
        for pid in list(self.printer_cards):
            if pid not in self.printer_data:
                self.printer_cards.pop(pid)[0].destroy()
                self.camera_tiles.pop(pid, None)
        if not printers:
            self.no_printers.grid(row=0, column=0, sticky="w", pady=16)
        else:
            self.no_printers.grid_remove()
        for printer in printers:
            pid = printer["id"]
            if pid not in self.printer_cards:
                card = ttk.LabelFrame(
                    self.printers_frame, style="PrintFarm.TLabelframe", padding=10
                )
                detail = ttk.Label(card, font=("Segoe UI", 11, "bold"))
                detail.pack(anchor="w")
                tile = CameraTile(card, self.live, pid, width=640, height=240)
                tile.pack(fill="x", pady=(8, 4))
                self.camera_tiles[pid] = tile
                bed = ttk.Label(card)
                bed.pack(anchor="w", pady=(4, 0))
                ttk.Button(
                    card,
                    text="Settings",
                    command=lambda key=pid: self.add_printer(
                        self.printer_data.get(key)
                    ),
                ).pack(anchor="w", pady=(8, 0))
                self.printer_cards[pid] = (card, detail, bed)
            card, detail, bed = self.printer_cards[pid]
            card.configure(text=printer.get("hostname", pid))
            progress = (
                f" · {printer.get('progress', 0):.0f}%"
                if printer.get("online") and not printer.get("idle")
                else ""
            )
            detail.configure(text=printer.get("detail", "Checking…") + progress)
            bed.configure(
                text="Start check: "
                + printer.get("bed", {}).get("state", "unchecked").replace("_", " ")
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
        self.live.update(
            snapshot.get("printers", []),
            bool(self.parent.winfo_ismapped()) and self.window.state() != "iconic",
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
        signature = digest(
            {
                k: v
                for k, v in snapshot.items()
                if k not in ("checked_at", "ebay_checked_at")
            }
        )
        if signature != self.last_render:
            self.render(snapshot)
            self.last_render = signature
            if not self.busy:
                self.status.set(snapshot.get("status", ""))
        self.window.after(1000, self.tick)
