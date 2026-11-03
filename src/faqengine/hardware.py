"""What this machine can train on. Shown before and during training, because on a small laptop it decides everything."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys

import torch


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _meminfo() -> dict[str, float]:
    out = {}
    try:
        for line in open("/proc/meminfo"):
            k, _, v = line.partition(":")
            out[k] = float(v.split()[0]) / 2**20  # kB -> GB
    except OSError:
        pass
    return out


def cpu_name() -> str:
    if sys.platform == "darwin":
        return _run("sysctl", "-n", "machdep.cpu.brand_string").strip() or platform.processor()
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def memory() -> dict:
    """total, free (what a new process could use without swapping) and swap in use, in GB; None where unknown."""
    if sys.platform == "darwin":
        total = _run("sysctl", "-n", "hw.memsize").strip()
        vm = _run("vm_stat")
        page = re.search(r"page size of (\d+) bytes", vm)
        pages = {k: int(v) for k, v in re.findall(r"Pages (free|inactive|speculative|purgeable):\s+(\d+)", vm)}
        free = (pages.get("free", 0) + pages.get("inactive", 0) + pages.get("speculative", 0) + pages.get("purgeable", 0)) * int(page.group(1)) / 2**30 if page else None
        swap = re.search(r"used = ([\d.]+)M", _run("sysctl", "-n", "vm.swapusage"))
        return {"total_gb": int(total) / 2**30 if total.isdigit() else None, "free_gb": free, "swap_used_gb": float(swap.group(1)) / 1024 if swap else None}
    m = _meminfo()
    if m:
        return {"total_gb": m.get("MemTotal"), "free_gb": m.get("MemAvailable"), "swap_used_gb": (m.get("SwapTotal", 0) - m.get("SwapFree", 0)) or 0.0}
    return {"total_gb": None, "free_gb": None, "swap_used_gb": None}


def accelerator() -> str:
    if torch.cuda.is_available():
        return f"NVIDIA GPU: {torch.cuda.get_device_name(0)}"
    if torch.backends.mps.is_available():
        return "Apple GPU (Metal)"
    return "none (CPU only)"


def hardware(path: str = ".") -> dict:
    from .encoder import pick_device

    mem = memory()
    disk_free = shutil.disk_usage(path).free / 2**30
    warnings = []
    if disk_free < 2:
        warnings.append(f"Only {disk_free:.1f} GB of disk is free. Training needs room for the model (about 0.1 GB) and exports.")
    if mem["free_gb"] is not None and mem["free_gb"] < 1.5:
        warnings.append(f"Only {mem['free_gb']:.1f} GB of memory is free; close other applications, or training will be slow.")
    if mem["swap_used_gb"] is not None and mem["swap_used_gb"] > 1.5:
        warnings.append(f"{mem['swap_used_gb']:.1f} GB of swap is in use, which makes training slower and less predictable.")
    return {
        "cpu": cpu_name(), "cores": os.cpu_count(), "ram_gb": mem["total_gb"], "ram_free_gb": mem["free_gb"], "swap_used_gb": mem["swap_used_gb"],
        "disk_free_gb": disk_free, "accelerator": accelerator(), "device": str(pick_device()), "torch": torch.__version__,
        "python": platform.python_version(), "system": f"{platform.system()} {platform.machine()}", "warnings": warnings,
    }
