"""Small, read-only Windows hardware sampler for the desktop header.

Sampling runs off the Tk thread so a slow GPU driver cannot freeze the app.
An unavailable sensor is represented by None, never a misleading zero.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes


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
    if total <= 0:
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
            values.append(int(line.strip().split(",", 1)[0]))
        except ValueError:
            continue
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


class HardwareSampler:
    """Keep the previous CPU snapshot; GPU and RAM are sampled on demand."""

    def __init__(self):
        self.previous_cpu = None

    def sample(self):
        if os.name != "nt":
            return {"cpu": None, "ram": None, "gpu": None}
        try:
            current = _cpu_times()
            cpu = cpu_percent(self.previous_cpu, current)
            self.previous_cpu = current
        except (AttributeError, OSError):
            cpu = None
        try:
            ram = _ram_percent()
        except (AttributeError, OSError):
            ram = None
        try:
            gpu = _gpu_percent()
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            gpu = None
        return {"cpu": cpu, "ram": ram, "gpu": gpu}
