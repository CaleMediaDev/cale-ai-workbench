"""Small, read-only Windows hardware sampler for the desktop header.

Sampling runs off the Tk thread so a slow GPU driver cannot freeze the app.
An unavailable sensor is represented by None, never a misleading zero.
"""

from __future__ import annotations

import ctypes
import math
import os
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def rpm_color(position: float) -> str:
    """Map a position on a load gauge from green through amber to red."""
    position = max(0.0, min(1.0, position))
    stops = ((50, 211, 144), (255, 198, 75), (255, 79, 97))
    left, right, fraction = (
        (stops[0], stops[1], position * 2)
        if position <= 0.5
        else (stops[1], stops[2], (position - 0.5) * 2)
    )
    channels = (round(a + (b - a) * fraction) for a, b in zip(left, right))
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def dim_rpm_color(position: float) -> str:
    """Leave the green-to-red range faintly visible behind inactive segments."""
    bright = rpm_color(position)
    background = "#0b1020"
    channels = (
        round(int(background[index:index + 2], 16) * 0.73 + int(bright[index:index + 2], 16) * 0.27)
        for index in (1, 3, 5)
    )
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def active_segments(percent: int | None, count: int = 24) -> int:
    """Keep small nonzero readings visible on the segmented scale."""
    if percent is None:
        return 0
    return math.ceil(max(0, min(100, percent)) * count / 100)


class _FileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    @property
    def ticks(self):
        return (self.high << 32) | self.low


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("memory_load", wintypes.DWORD),
        ("total_physical", ctypes.c_ulonglong),
        ("available_physical", ctypes.c_ulonglong),
        ("total_page", ctypes.c_ulonglong),
        ("available_page", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("available_virtual", ctypes.c_ulonglong),
        ("available_extended_virtual", ctypes.c_ulonglong),
    ]


def _cpu_times():
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None
    return idle.ticks, kernel.ticks + user.ticks


def cpu_percent(previous, current):
    """Calculate CPU activity from two Windows system-time snapshots."""
    if not previous or not current:
        return None
    idle = current[0] - previous[0]
    total = current[1] - previous[1]
    if total <= 0 or idle < 0 or idle > total:
        return None
    return max(0, min(100, round(100 * (1 - idle / total))))


def _ram_percent():
    memory = _MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        return None
    return int(memory.memory_load)


def parse_gpu_utilization(output):
    """Read utilization from nvidia-smi's one-line-per-GPU CSV output."""
    values = []
    for line in output.splitlines():
        try:
            value = int(line.strip().split(",", 1)[0])
        except ValueError:
            continue
        if 0 <= value <= 100:
            values.append(value)
    return max(values) if values else None


def _gpu_percent():
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=3,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return parse_gpu_utilization(result.stdout) if result.returncode == 0 else None


def _drive_space():
    """Measure the volume holding the app, downloads, and local models."""
    total, _used, free = shutil.disk_usage(ROOT)
    if total <= 0:
        return None, None
    return round(100 * (1 - free / total)), round(free / (1024 ** 3))


def disk_transfer_speed(previous, current, elapsed):
    """Convert PC-wide disk byte counters into read/write MB per second."""
    if previous is None or current is None or elapsed <= 0:
        return None, None
    if any(current[index] < previous[index] for index in (0, 1)):
        return None, None
    return (
        max(0, (current[0] - previous[0]) / elapsed / 1_000_000),
        max(0, (current[1] - previous[1]) / elapsed / 1_000_000),
    )


def network_bandwidth(previous, current, elapsed, link_mbps):
    """Return receive/send Mbps and link utilization from byte counters."""
    if previous is None or current is None or elapsed <= 0:
        return None, None, None
    if any(current[index] < previous[index] for index in (0, 1)):
        return None, None, None
    down = max(0, (current[0] - previous[0]) * 8 / elapsed / 1_000_000)
    up = max(0, (current[1] - previous[1]) * 8 / elapsed / 1_000_000)
    load = round(min(100, 100 * (down + up) / link_mbps)) if link_mbps else None
    return down, up, load


class HardwareSampler:
    """Keep the previous CPU snapshot; GPU and RAM are sampled on demand."""

    def __init__(self):
        self.previous_cpu = None
        self.previous_disk = None
        self.previous_disk_time = None
        self.previous_net = None
        self.previous_net_time = None

    def sample(self):
        if os.name != "nt":
            return {"cpu": None, "ram": None, "gpu": None,
                    "drive": None, "drive_free_gb": None,
                    "drive_read_mbps": None, "drive_write_mbps": None,
                    "net": None, "net_down_mbps": None, "net_up_mbps": None}
        try:
            current = _cpu_times()
            cpu = cpu_percent(self.previous_cpu, current)
            self.previous_cpu = current
        except (AttributeError, OSError):
            self.previous_cpu = None
            cpu = None
        try:
            ram = _ram_percent()
        except (AttributeError, OSError):
            ram = None
        try:
            gpu = _gpu_percent()
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
            gpu = None
        try:
            drive, drive_free = _drive_space()
        except OSError:
            drive, drive_free = None, None
        try:
            import psutil

            counters = psutil.disk_io_counters()
            current_disk = (counters.read_bytes, counters.write_bytes) if counters else None
        except (ImportError, AttributeError, OSError, RuntimeError):
            current_disk = None
        now = time.monotonic()
        elapsed = now - self.previous_disk_time if self.previous_disk_time is not None else 0
        drive_read, drive_write = disk_transfer_speed(self.previous_disk, current_disk, elapsed)
        self.previous_disk, self.previous_disk_time = current_disk, now
        try:
            net_counters = psutil.net_io_counters()
            current_net = (net_counters.bytes_recv, net_counters.bytes_sent) if net_counters else None
        except (NameError, AttributeError, OSError, RuntimeError):
            current_net = None
        net_now = time.monotonic()
        try:
            link_speeds = [item.speed for item in psutil.net_if_stats().values()
                           if item.isup and item.speed > 0]
            link_mbps = max(link_speeds, default=None)
        except (NameError, AttributeError, OSError, RuntimeError):
            link_mbps = None
        net_elapsed = net_now - self.previous_net_time if self.previous_net_time is not None else 0
        net_down, net_up, net_load = network_bandwidth(
            self.previous_net, current_net, net_elapsed, link_mbps
        )
        self.previous_net, self.previous_net_time = current_net, net_now
        return {"cpu": cpu, "ram": ram, "gpu": gpu,
                "drive": drive, "drive_free_gb": drive_free,
                "drive_read_mbps": drive_read, "drive_write_mbps": drive_write,
                "net": net_load, "net_down_mbps": net_down, "net_up_mbps": net_up}
