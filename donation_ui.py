"""Small, plain-language donation controls shared by the desktop header/dialog."""

import queue
import re
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

import chatgpt_auth
import donation
import github_account


class ChatActivity:
    """Turn bounded, factual worker events into quiet main-chat updates.

    Only the Tk caller invokes the sink. No account checks, database access or
    model calls occur here; worker-provided prose never becomes chat content.
    """

    def __init__(self, service, reply, clock=time.monotonic):
        self.service, self.reply, self.clock = service, reply, clock
        self.seen = {}
        self.urls = {}
        self.pending_reports = {}
        self.titles = {}
        self.resources = {}
        self.pending = {}
        self.last_progress = float("-inf")

    @staticmethod
    def report_url(value):
        if not isinstance(value, str) or not re.fullmatch(
                r"https://github\.com/CaleMediaDev/cale-ai-workbench/pull/[1-9][0-9]{0,9}", value):
            return None
        return value

    @staticmethod
    def remember(cache, key):
        cache[key] = True
        while len(cache) > 512:
            cache.pop(next(iter(cache)))

    @staticmethod
    def public_title(value):
        """Quote public job context as data, never as links or speaker lines."""
        if not isinstance(value, str):
            return ""
        value = re.sub(r"https?://\S+", "[link omitted]", value[:512], flags=re.I)
        value = re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]", " ", value)
        return " ".join(value.split()).replace('"', "'")[:72]

    def poll(self):
        """Drain once without blocking; batch six lanes instead of six voices."""
        drain = getattr(self.service, "drain_events", None)
        if not callable(drain):
            return  # Older service instances remain usable during development.
        try:
            events = drain()
        except Exception:
            return  # A worker channel must not break/reschedule the Tk poll.
        if not isinstance(events, list):
            return
        phases = {"reviewing", "checking", "submitting", "submitted", "no_change",
                  "waiting", "error", "training", "trained"}
        for event in events[:64]:
            if not isinstance(event, dict) or not isinstance(event.get("phase"), str) or event["phase"] not in phases:
                continue
            run, phase = event.get("run_id"), event["phase"]
            if not isinstance(run, str) or not run or len(run) > 128:
                continue
            title = self.public_title(event.get("job_title"))
            if title:
                self.titles[run] = title
                while len(self.titles) > 512:
                    self.titles.pop(next(iter(self.titles)))
            resource = event.get("resource")
            if isinstance(resource, str) and resource in {"local", "free_cloud", "chatgpt", "cpu"}:
                self.resources[run] = resource
                while len(self.resources) > 512:
                    self.resources.pop(next(iter(self.resources)))
            elif "resource" in event:
                self.resources.pop(run, None)
            if phase == "submitted":
                url = self.report_url(event.get("pr_url"))
                if url and url not in self.urls:
                    self.pending_reports[url] = self.titles.get(run, "")
                    while len(self.pending_reports) > 128:
                        self.pending_reports.pop(next(iter(self.pending_reports)))
                    self.pending.pop(run, None)
                continue
            key = (run, phase, self.resources.get(run))
            if key in self.seen:
                continue
            self.remember(self.seen, key)
            self.pending[run] = phase
            while len(self.pending) > 64:
                self.pending.pop(next(iter(self.pending)))
        count = 0
        reports = list(self.pending_reports)[:64]
        if reports:
            # Up to 64 validated results remain one compact chat message. The
            # producer drains at most six concurrent lanes in normal operation.
            noun = "a proposed improvement" if len(reports) == 1 else f"{len(reports)} proposed improvements"
            try:
                self.reply(f"I just sent {noun} to GitHub for review:\n" + "\n".join(
                    (f'“{self.pending_reports[url]}” — ' if self.pending_reports[url] else "") + url for url in reports))
            except Exception:
                return 0  # Retain this bounded batch for the next Tk tick.
            for url in reports:
                self.remember(self.urls, url)
                self.pending_reports.pop(url, None)
            self.last_progress = self.clock()
            count += 1
        if self.pending and self.clock() - self.last_progress >= 20:
            current = list(self.pending.values())
            descriptions = {
                "reviewing": "I'm reviewing public Cale code",
                "checking": "I'm checking proposed code changes before sending them for review",
                "submitting": "I'm sending a proposed improvement to GitHub for review",
                "training": "I'm training a small specialist on reviewed public data",
                "trained": "I recorded a local specialist experiment; it is not activated",
                "no_change": "I finished a review without a reliable code change; nothing was sent",
                "waiting": "I'm waiting for an enabled AI source or GitHub to become available",
                "error": "I paused after a contribution error; no successful report is confirmed",
            }
            lines = []
            for phase in descriptions:
                if phase not in current:
                    continue
                runs = [run for run, value in self.pending.items() if value == phase]
                labels = list(dict.fromkeys(self.titles[run] for run in runs if self.titles.get(run)))
                areas = ", ".join(f'“{label}”' for label in labels[:2])
                if len(labels) > 2:
                    areas += f" and {len(labels) - 2} other areas"
                if phase == "reviewing":
                    line = ("I'm reviewing " + (areas or "public Cale code") if len(runs) == 1 else
                            f"I'm working on {len(runs)} reviews" + (" for " + areas if areas else ""))
                elif phase == "checking" and len(runs) > 1:
                    line = f"I'm checking proposed code changes from {len(runs)} reviews before sending them for review"
                    line += " · " + areas if areas else ""
                else:
                    line = descriptions[phase] + (" · " + areas if areas else "")
                names = {"local": "local AI", "free_cloud": "free cloud AI", "chatgpt": "ChatGPT", "cpu": "local CPU"}
                sources = list(dict.fromkeys(self.resources[run] for run in runs if self.resources.get(run) in names))
                if sources:
                    source_text = ", ".join(names[source] for source in sources)
                    line += (" (" + source_text + ")" if phase in {"waiting", "error"} else " using " + source_text)
                lines.append(line)
            try:
                self.reply(". ".join(lines) + ".")
            except Exception:
                return count
            self.pending.clear()
            self.last_progress = self.clock()
            count += 1
        return count


class Controls:
    """Show resource choices, intensity, reported tokens and accepted-fix rankings."""

    def __init__(self, header, window, service, connect_github, connect_chatgpt, connect_free=None, on_activity=None):
        self.window = window
        self.service = service
        self.chat_activity = ChatActivity(service, on_activity) if on_activity else None
        self.connect_github = connect_github
        self.connect_chatgpt = connect_chatgpt
        self.connect_free = connect_free or (lambda: webbrowser.open("https://openrouter.ai"))
        self.popup = None
        self.checked_at = 0
        self.last_counter = 0
        self.probing = False
        self.events = queue.Queue()
        self.login = ""
        self.chat_ready = False
        self.fingerprint = ""
        self.free_fingerprint = ""
        self.local_ready = False
        self.leaderboard_popup = None
        frame = ttk.Frame(header)
        frame.pack(side="right", padx=(12, 10))
        ttk.Button(frame, text="Donate AI power", command=self.open).pack()
        self.counter = tk.StringVar(value="Total tokens donated: 0")
        self.breakdown = tk.StringVar(value="Local: 0 · Free cloud: 0 · ChatGPT: 0")
        ttk.Label(frame, textvariable=self.counter, style="Muted.TLabel").pack(pady=(3, 0))

    def refresh(self):
        """Refresh cheap local state; account network probes stay off the UI thread."""
        activity = getattr(self, "chat_activity", None)
        if activity:
            activity.poll()
        now = time.monotonic()
        if now - self.last_counter >= 5:
            self.last_counter = now
            try:
                self.counter.set(f"Total tokens donated: {donation.total_tokens(self.service.root):,}")
                totals = donation.source_totals(self.service.root)
                self.breakdown.set(f"Local: {totals.get('local', 0):,} · Free cloud: {totals.get('free_cloud', 0):,} · ChatGPT: {totals.get('chatgpt', 0):,}")
            except Exception:
                self.counter.set("Total tokens donated: checking…")
        if not self.popup or not self.popup.winfo_exists():
            return
        try:
            while True:
                self.login, self.chat_ready, self.fingerprint, self.free_fingerprint, self.local_ready = self.events.get_nowait()
                self.chat_ready = bool(self.chat_ready and self.fingerprint)
                self.probing = False
        except queue.Empty:
            pass
        if not self.probing and now - self.checked_at >= 5:
            self.checked_at = now
            self.probing = True
            def probe():
                try:
                    self.events.put((github_account.account(), bool(chatgpt_auth.status().get("sharing")),
                                     chatgpt_auth.account_fingerprint(), donation.free_fingerprint(), donation.local_available()))
                except Exception:
                    self.events.put(("", False, "", "", False))
            threading.Thread(target=probe, daemon=True).start()
        self.github_text.set("Connected · " + self.login if self.login else "Not connected yet")
        self.chat_text.set("Connected · plan permission approved" if self.chat_ready else "Connect and approve plan permission")
        self.free_text.set("Free routes connected" if self.free_fingerprint else "Connect a free route or use an existing provider in Supercharge")
        self.local_text.set("CPU specialist training selected; Ollama is optional" if self.model_training.get() else
                            "Local AI is available" if self.local_ready else "Waiting for Ollama and a local model")
        import small_training as donation_models
        training = donation_models.summary(self.service.root)
        if training is not None:
            self.model_stats.set(f"CPU experiments: {training['experiments']} · worker time: {training['worker_seconds']:.1f}s · "
                                 f"screened candidates: {training['screened_candidates']} · separate from token totals")
        self.github_button.configure(text="Reconnect GitHub" if self.login else "1. Connect GitHub")
        self.chat_button.configure(text="Reconnect ChatGPT" if self.chat_ready else "2. Connect ChatGPT")
        enabled = donation.settings(self.service.root)["enabled"]
        selected = [key for key, variable in self.source_vars.items() if variable.get()]
        self.enable_button.configure(text="Stop donating" if enabled else ("Start donating continuously" if self.intensity.get() in {"medium", "full_blast"} else "Start donating when idle"),
                                     state="normal" if enabled or self.ready_to_start() else "disabled")
        self.state_text.set(("On · " + self.service.status_message()) if enabled else "Off · donations use no AI until you start them.")
        self.intensity_text.set(donation.intensity_description(self.intensity.get()))
        for key, button in self.intensity_buttons.items():
            button.configure(style="SelectedMode.TButton" if key == self.intensity.get() else "Mode.TButton")
        if "chatgpt" in selected:
            self.chat_row.pack(fill="x", before=self.power_row, pady=(3, 8))
        else:
            self.chat_row.pack_forget()
        if "free_cloud" in selected:
            self.free_row.pack(fill="x", before=self.power_row, pady=(3, 8))
        else:
            self.free_row.pack_forget()

    def ready_to_start(self):
        """Require only the accounts/runtimes for the resources actually selected."""
        selected = [key for key, value in self.source_vars.items() if value.get()]
        return bool(self.login and selected and ("local" not in selected or self.local_ready or self.model_training.get())
                    and ("chatgpt" not in selected or self.chat_ready)
                    and ("free_cloud" not in selected or self.free_fingerprint))

    def choose_configuration(self):
        """Resource/intensity changes stop the old grant until explicitly restarted."""
        if not self.source_vars["local"].get():
            self.model_training.set(False)
        donation.set_enabled(False, self.service.root, sources=[key for key, value in self.source_vars.items() if value.get()],
                             intensity=self.intensity.get(), model_training=self.model_training.get())
        self.service.cancel.set()
        self.service.activity()
        self.service.next_attempt = 0
        self.refresh()

    def toggle(self):
        """Only this explicit action authorizes background plan use."""
        enabled = donation.settings(self.service.root)["enabled"]
        if not enabled and not self.ready_to_start():
            return
        donation.set_enabled(not enabled, self.service.root,
                             chatgpt_account=self.fingerprint, github_account=self.login,
                             free_account=self.free_fingerprint,
                             sources=[key for key, value in self.source_vars.items() if value.get()], intensity=self.intensity.get(),
                             model_training=self.model_training.get())
        if enabled:
            self.service.cancel.set()
        self.service.activity()
        self.service.next_attempt = 0
        self.service.message = (("Continuous donations enabled." if self.intensity.get() in {"medium", "full_blast"} else "Starts when Cale is idle at your chosen intensity.") if not enabled else "Off.")
        self.refresh()

    def open(self):
        """Explain the allowance use before enabling; reuse existing sign-in flows."""
        self.service.activity()
        if self.popup and self.popup.winfo_exists():
            self.popup.lift()
            return
        self.popup = popup = tk.Toplevel(self.window)
        popup.title("Donate AI power")
        popup.geometry(f"700x{min(720, popup.winfo_screenheight() - 100)}")
        popup.minsize(620, 540)
        # The installed desktop uses larger fonts/padding than a plain Tk
        # preview. Keep the review link visible and let short screens scroll.
        footer = ttk.Frame(popup, padding=(24, 10, 24, 16))
        footer.pack(side="bottom", fill="x")
        ttk.Button(footer, text="View contributions on GitHub ↗",
                   command=lambda: webbrowser.open(donation.REVIEW_URL)).pack(side="left")
        ttk.Button(footer, text="Leaderboard", command=self.open_leaderboard).pack(side="right")
        canvas = tk.Canvas(popup, highlightthickness=0, borderwidth=0,
                           background=ttk.Style(popup).lookup("TFrame", "background"))
        scrollbar = ttk.Scrollbar(popup, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(fill="both", expand=True)
        outer = ttk.Frame(canvas, padding=24)
        content = canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(content, width=event.width))
        def scroll(event):
            self.service.activity()
            canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
            return "break"
        popup.bind("<MouseWheel>", scroll, add="+")
        ttk.Label(outer, text="Help make Cale better", font=("Segoe UI", 23, "bold")).pack(anchor="w")
        ttk.Label(outer, text=(
            "Contribute local AI, free cloud, or your ChatGPT allowance while away—or continuously in Full Blast. "
            "Only public Cale source code is used for code reviews. Optional CPU experiments use reviewed public datasets. Proposed contributions go to GitHub "
            "for the owner's review—nothing merges automatically."),
            wraplength=585, justify="left").pack(anchor="w", pady=(10, 14))
        self.github_text = tk.StringVar(value="Checking your connection…")
        self.chat_text = tk.StringVar(value="Checking your connection…")
        self.state_text = tk.StringVar()
        config = donation.settings(self.service.root)
        sources_row = ttk.Frame(outer)
        sources_row.pack(fill="x", pady=(0, 10))
        self.source_vars = {key: tk.BooleanVar(value=key in config["sources"]) for key in donation.SOURCES}
        self.model_training = tk.BooleanVar(value=config["model_training"])
        for key, label in (("local", "Local compute"), ("free_cloud", "Free cloud"), ("chatgpt", "ChatGPT plan / credits")):
            ttk.Checkbutton(sources_row, text=label, variable=self.source_vars[key],
                            command=self.choose_configuration).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(outer, text="Also train small specialist models on CPU (Local compute required)",
                        variable=self.model_training, command=self.choose_configuration).pack(anchor="w", pady=(0, 6))
        ttk.Label(outer, text="Public pinned data only · up to 2 CPU threads / 5 minutes per experiment · no ChatGPT tokens. "
                  "Weak results stay local; screened candidates need owner review and never activate automatically.",
                  wraplength=585, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.model_stats = tk.StringVar(value="CPU experiments: not started · separate from token totals")
        ttk.Label(outer, textvariable=self.model_stats, wraplength=585, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.local_text = tk.StringVar()
        ttk.Label(outer, textvariable=self.local_text, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Style(popup).configure("Donation.Connect.TButton", padding=(12, 6), font=("Segoe UI", 10, "bold"))
        self.github_row = ttk.Frame(outer)
        self.github_row.pack(fill="x", pady=(0, 8))
        self.github_button = ttk.Button(self.github_row, text="Connect GitHub", command=self.connect_github,
                                        style="Donation.Connect.TButton")
        self.github_button.pack(side="left")
        ttk.Label(self.github_row, textvariable=self.github_text, style="Muted.TLabel", wraplength=350).pack(side="left", padx=(10, 0))
        self.chat_row = ttk.Frame(outer)
        self.chat_button = ttk.Button(self.chat_row, text="Connect ChatGPT", command=self.connect_chatgpt,
                                     style="Donation.Connect.TButton")
        self.chat_button.pack(side="left")
        ttk.Label(self.chat_row, textvariable=self.chat_text, style="Muted.TLabel", wraplength=350).pack(side="left", padx=(10, 0))
        self.free_text = tk.StringVar()
        self.free_row = ttk.Frame(outer)
        ttk.Button(self.free_row, text="Connect free cloud", command=self.connect_free,
                   style="Donation.Connect.TButton").pack(side="left")
        ttk.Label(self.free_row, textvariable=self.free_text, style="Muted.TLabel", wraplength=350).pack(side="left", padx=(10, 0))
        self.power_row = ttk.Frame(outer)
        self.power_row.pack(fill="x", pady=(10, 6))
        self.intensity = tk.StringVar(value=config["intensity"])
        self.intensity_buttons = {}
        def select_intensity(key):
            self.intensity.set(key)
            self.choose_configuration()
        for key, label in (("slow", "Slow"), ("medium", "Medium"), ("full_blast", "Full Blast")):
            self.intensity_buttons[key] = ttk.Button(self.power_row, text=label,
                                                    command=lambda selected=key: select_intensity(selected))
            self.intensity_buttons[key].pack(side="left", padx=(0, 8))
        self.intensity_text = tk.StringVar()
        ttk.Label(outer, textvariable=self.intensity_text, style="Muted.TLabel", wraplength=585).pack(anchor="w", pady=(0, 12))
        self.enable_button = ttk.Button(outer, text="Start donating when idle", command=self.toggle,
                                        style="Accent.TButton", state="disabled")
        self.enable_button.pack(anchor="w", ipady=5)
        ttk.Label(outer, textvariable=self.state_text, wraplength=585).pack(anchor="w", pady=(10, 10))
        ttk.Label(outer, text=(
            "Slow pauses for your tasks. Medium runs continuously, one review at a time. "
            "Full Blast runs up to six parallel reviews, with shared account-limit cooldowns and duplicate-work protection. "
            "A request already in flight may still use tokens. Local mode blocks cloud donations; offline pauses all contributions."),
            style="Muted.TLabel", wraplength=585, justify="left").pack(anchor="w")
        ttk.Label(outer, textvariable=self.counter).pack(anchor="w", pady=(14, 3))
        ttk.Label(outer, textvariable=self.breakdown, style="Muted.TLabel", wraplength=585).pack(anchor="w", pady=(0, 3))
        ttk.Label(outer, text="Counts reported input and output tokens, including reviews with no code change.",
                  style="Muted.TLabel", wraplength=585).pack(anchor="w")
        self.checked_at = 0
        self.refresh()

    def open_leaderboard(self):
        """Show public GitHub-authoritative accepted fixes, including a cached view."""
        self.service.activity()
        if self.leaderboard_popup and self.leaderboard_popup.winfo_exists():
            self.leaderboard_popup.lift()
            return
        self.leaderboard_popup = dialog = tk.Toplevel(self.window)
        dialog.title("Cale contribution leaderboard")
        dialog.geometry("700x600")
        dialog.minsize(620, 450)
        outer = ttk.Frame(dialog, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Accepted fixes leaderboard", font=("Segoe UI", 21, "bold")).pack(anchor="w")
        ttk.Label(outer, text="One accepted contribution (code or model) = one point. Open or rejected proposals do not count.",
                  wraplength=600, style="Muted.TLabel").pack(anchor="w", pady=(6, 14))
        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x")
        style = ttk.Style(dialog)
        style.configure("Donation.Treeview", background="#151d31", fieldbackground="#151d31",
                        foreground="#edf3ff", rowheight=32, font=("Segoe UI", 11))
        style.configure("Donation.Treeview.Heading", background="#202d46", foreground="#edf3ff",
                        font=("Segoe UI", 10, "bold"))
        style.map("Donation.Treeview", background=[("selected", "#4f63c8")], foreground=[("selected", "white")])
        table_shell = ttk.Frame(outer)
        table_shell.pack(fill="both", expand=True)
        table = ttk.Treeview(table_shell, columns=("rank", "login", "fixes"), show="headings", height=9,
                             style="Donation.Treeview")
        scroll = ttk.Scrollbar(table_shell, orient="vertical", command=table.yview)
        scroll.pack(side="right", fill="y")
        table.configure(yscrollcommand=scroll.set)
        for column, text, width in (("rank", "Rank", 65), ("login", "Contributor", 350), ("fixes", "Accepted contributions", 140)):
            table.heading(column, text=text)
            table.column(column, width=width, anchor="w")
        table.pack(fill="both", expand=True)
        for rank, color in (("1", "#ffd17d"), ("2", "#c8d8ed"), ("3", "#e4b18c")):
            table.tag_configure(rank, foreground=color)
        notice = tk.StringVar(value="Checking GitHub merge records…")
        ttk.Label(footer, textvariable=notice, wraplength=600, style="Muted.TLabel").pack(anchor="w", pady=10)
        events = queue.Queue()
        loading = [False]
        def display(value):
            table.delete(*table.get_children())
            for rank, row in enumerate(value.get("rows", []), 1):
                table.insert("", "end", values=(rank, "@" + row["login"], row["accepted"]), tags=(str(rank),))
            notice.set(value.get("notice") or ("Checking GitHub merge records…" if not value.get("checked_at") else
                       "No accepted fixes yet · be the first contributor." if not value.get("rows")
                       and value.get("complete") else "Verified from GitHub merged PRs." if value.get("complete")
                       else "Partial history · refresh again to load older accepted fixes."))
        def refresh():
            if loading[0]:
                return
            loading[0] = True
            def worker():
                try:
                    events.put(donation.refresh_leaderboard(self.service.root, force=True))
                except Exception:
                    events.put({**donation.leaderboard(self.service.root), "notice": "GitHub unavailable · showing cached counts."})
            threading.Thread(target=worker, daemon=True).start()
        ttk.Button(footer, text="Refresh accepted fixes", command=refresh).pack(anchor="w")
        display(donation.leaderboard(self.service.root))
        refresh()
        def poll():
            if not dialog.winfo_exists():
                return
            try:
                while True:
                    display(events.get_nowait())
                    loading[0] = False
            except queue.Empty:
                pass
            dialog.after(500, poll)
        poll()
