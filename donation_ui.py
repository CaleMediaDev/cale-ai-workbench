"""Small, plain-language donation controls shared by the desktop header/dialog."""

import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

import chatgpt_auth
import donation
import github_account


class Controls:
    """Show the token counter and a three-button opt-in connection flow."""

    def __init__(self, header, window, service, connect_github, connect_chatgpt):
        self.window = window
        self.service = service
        self.connect_github = connect_github
        self.connect_chatgpt = connect_chatgpt
        self.popup = None
        self.checked_at = 0
        self.last_counter = 0
        self.probing = False
        self.events = queue.Queue()
        self.login = ""
        self.chat_ready = False
        self.fingerprint = ""
        frame = ttk.Frame(header)
        frame.pack(side="right", padx=(12, 10))
        ttk.Button(frame, text="Donate my ChatGPT", command=self.open).pack()
        self.counter = tk.StringVar(value="Total tokens donated: 0")
        ttk.Label(frame, textvariable=self.counter, style="Muted.TLabel").pack(pady=(3, 0))

    def refresh(self):
        """Refresh cheap local state; account network probes stay off the UI thread."""
        now = time.monotonic()
        if now - self.last_counter >= 5:
            self.last_counter = now
            try:
                self.counter.set(f"Total tokens donated: {donation.total_tokens(self.service.root):,}")
            except Exception:
                self.counter.set("Total tokens donated: checking…")
        if not self.popup or not self.popup.winfo_exists():
            return
        try:
            while True:
                self.login, self.chat_ready, self.fingerprint = self.events.get_nowait()
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
                                     chatgpt_auth.account_fingerprint()))
                except Exception:
                    self.events.put(("", False, ""))
            threading.Thread(target=probe, daemon=True).start()
        self.github_text.set("Connected · " + self.login if self.login else "Not connected yet")
        self.chat_text.set("Connected · plan permission approved" if self.chat_ready else "Connect and approve plan permission")
        self.github_button.configure(text="Reconnect GitHub" if self.login else "1. Connect GitHub")
        self.chat_button.configure(text="Reconnect ChatGPT" if self.chat_ready else "2. Connect ChatGPT")
        enabled = donation.settings(self.service.root)["enabled"]
        self.enable_button.configure(text="Stop donating" if enabled else "3. Start donating when idle",
                                     state="normal" if enabled or (self.login and self.chat_ready) else "disabled")
        self.state_text.set(("On · " + self.service.message) if enabled else "Off · your allowance is not used for contributions.")

    def toggle(self):
        """Only this explicit action authorizes background plan use."""
        enabled = donation.settings(self.service.root)["enabled"]
        if not enabled and not (self.login and self.chat_ready):
            return
        donation.set_enabled(not enabled, self.service.root,
                             chatgpt_account=self.fingerprint, github_account=self.login)
        self.service.activity()
        self.service.message = "Starts after two minutes away from Cale." if not enabled else "Off."
        self.refresh()

    def open(self):
        """Explain the allowance use before enabling; reuse existing sign-in flows."""
        self.service.activity()
        if self.popup and self.popup.winfo_exists():
            self.popup.lift()
            return
        self.popup = popup = tk.Toplevel(self.window)
        popup.title("Donate my ChatGPT")
        popup.geometry("650x590")
        popup.minsize(620, 570)
        outer = ttk.Frame(popup, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Help make Cale better", font=("Segoe UI", 23, "bold")).pack(anchor="w")
        ttk.Label(outer, text=(
            "Let Cale use your ChatGPT/Codex allowance or available credits while you're away. "
            "Only public Cale source code is sent. Proposed improvements go to GitHub "
            "for the owner's review—nothing merges automatically."),
            wraplength=585, justify="left").pack(anchor="w", pady=(10, 14))
        self.github_text = tk.StringVar(value="Checking your connection…")
        self.chat_text = tk.StringVar(value="Checking your connection…")
        self.state_text = tk.StringVar()
        self.github_button = ttk.Button(outer, text="1. Connect GitHub", command=self.connect_github)
        self.github_button.pack(anchor="w", ipady=4)
        ttk.Label(outer, textvariable=self.github_text, style="Muted.TLabel").pack(anchor="w", pady=(4, 12))
        self.chat_button = ttk.Button(outer, text="2. Connect ChatGPT", command=self.connect_chatgpt)
        self.chat_button.pack(anchor="w", ipady=4)
        ttk.Label(outer, textvariable=self.chat_text, style="Muted.TLabel").pack(anchor="w", pady=(4, 14))
        self.enable_button = ttk.Button(outer, text="3. Start donating when idle", command=self.toggle,
                                        style="Accent.TButton", state="disabled")
        self.enable_button.pack(anchor="w", ipady=5)
        ttk.Label(outer, textvariable=self.state_text, wraplength=585).pack(anchor="w", pady=(10, 10))
        ttk.Label(outer, text=(
            "One AI proposal attempt per day. Your tasks and chat come first. "
            "Returning to Cale stops further work; a request already in flight may still use tokens. "
            "Local-only, free-cloud-only and offline modes pause donations."),
            style="Muted.TLabel", wraplength=585, justify="left").pack(anchor="w")
        ttk.Label(outer, textvariable=self.counter).pack(anchor="w", pady=(14, 3))
        ttk.Label(outer, text="Counts reported input and output tokens, including reviews with no code change.",
                  style="Muted.TLabel", wraplength=585).pack(anchor="w")
        ttk.Button(outer, text="View contributions on GitHub ↗",
                   command=lambda: webbrowser.open(donation.REVIEW_URL)).pack(anchor="w", pady=(14, 0))
        self.checked_at = 0
        self.refresh()
