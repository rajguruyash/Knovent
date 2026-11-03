"""WHAT THIS MACHINE IS, and nothing it cannot find out. Detection runs a few read-only system commands (sysctl, /proc, nvidia-smi, system_profiler, PowerShell) through one injectable runner, so every
parser is tested on recorded output. Anything that cannot be determined is None, never a guess dressed up as a number. Platforms: macOS arm64 is the one this was written and run on; Linux and Windows
paths are tested against recorded output only and have NOT been run on real machines (docs/local-companion.md says so)."""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field


def run(*cmd: str, timeout: float = 5.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout  # noqa: S603 - a fixed argument list, no shell
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass
class Gpu:
    name: str
    vendor: str  # nvidia | apple | amd | intel | unknown
    vram_gb: float | None  # None when it cannot be read
    unified: bool = False  # shares the computer's memory (Apple silicon)
    usable_for_models: bool = False  # only where the memory is known and the runtimes support it


@dataclass
class Hardware:
    os: str
    os_version: str
    arch: str
    cpu: str
    cores: int | None
    ram_gb: float | None
    gpus: list[Gpu] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return asdict(self)

    @property
    def best_accelerator_gb(self) -> float | None:
        """Memory a model could live in on a GPU (dedicated VRAM, or the share of unified memory the system lets the GPU use); None if there is no usable GPU."""
        usable = [g.vram_gb for g in self.gpus if g.usable_for_models and g.vram_gb]
        return max(usable) if usable else None


def parse_nvidia_smi(text: str) -> list[Gpu]:
    """`nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits` -> one Gpu per line (memory in MiB)."""
    out = []
    for line in text.strip().splitlines():
        name, _, mem = line.rpartition(",")
        if name and mem.strip().isdigit():
            out.append(Gpu(name.strip(), "nvidia", round(int(mem) / 1024, 1), usable_for_models=True))
    return out


def parse_meminfo(text: str) -> float | None:
    m = re.search(r"^MemTotal:\s+(\d+)\s*kB", text, re.M)
    return round(int(m.group(1)) / 2**20, 1) if m else None


def parse_cpuinfo(text: str) -> tuple[str | None, int | None]:
    names = re.findall(r"^model name\s*:\s*(.+)$", text, re.M)
    return (names[0].strip() if names else None), (len(names) or None)


def parse_macos_displays(text: str) -> list[Gpu]:
    """`system_profiler SPDisplaysDataType` on an Intel Mac: a chipset name and, where it says so, its VRAM. Only NVIDIA is treated as usable; the rest are listed by name."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        name = re.search(r"Chipset Model:\s*(.+)", block)
        if not name:
            continue
        vram = re.search(r"VRAM[^:]*:\s*(\d+)\s*(MB|GB)", block)
        gb = (int(vram.group(1)) / (1024 if vram.group(2) == "MB" else 1)) if vram else None
        n = name.group(1).strip()
        vendor = "nvidia" if "nvidia" in n.lower() else "amd" if re.search(r"amd|radeon", n, re.I) else "intel" if "intel" in n.lower() else "unknown"
        out.append(Gpu(n, vendor, round(gb, 1) if gb else None, usable_for_models=False))
    return out


def parse_windows_gpus(text: str) -> list[Gpu]:
    """PowerShell `Get-CimInstance Win32_VideoController | Select Name, AdapterRAM | ConvertTo-Json`. AdapterRAM is a 32-bit field that under-reports above 4 GB, so it is never trusted:
    names only (NVIDIA memory comes from nvidia-smi where it is installed)."""
    try:
        data = json.loads(text) if text.strip() else []
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    out = []
    for d in items:
        n = str((d or {}).get("Name") or "").strip()
        if n:
            vendor = "nvidia" if "nvidia" in n.lower() else "amd" if re.search(r"amd|radeon", n, re.I) else "intel" if "intel" in n.lower() else "unknown"
            out.append(Gpu(n, vendor, None))
    return out


def apple_gpu(ram_gb: float | None, cpu: str) -> Gpu | None:
    """On Apple silicon the GPU shares the computer's memory; the system lets it use roughly two thirds of it by default (an estimate, stated as one)."""
    if ram_gb is None:
        return Gpu(cpu or "Apple GPU", "apple", None, unified=True)
    return Gpu(cpu or "Apple GPU", "apple", round(ram_gb * 0.65, 1), unified=True, usable_for_models=True)


def detect(runner: Callable[..., str] = run, system: str | None = None, machine: str | None = None) -> Hardware:
    sysname = (system or sys.platform)
    arch = machine or platform.machine()
    notes: list[str] = []
    if sysname == "darwin":
        osname, version = "macOS", platform.mac_ver()[0] or runner("sw_vers", "-productVersion").strip()
        mem = runner("sysctl", "-n", "hw.memsize").strip()
        ram = round(int(mem) / 2**30, 1) if mem.isdigit() else None
        cpu = runner("sysctl", "-n", "machdep.cpu.brand_string").strip() or platform.processor() or arch
        ncpu = runner("sysctl", "-n", "hw.ncpu").strip()
        cores = int(ncpu) if ncpu.isdigit() else os.cpu_count()
        gpus = [g] if arch == "arm64" and (g := apple_gpu(ram, cpu)) else parse_macos_displays(runner("system_profiler", "SPDisplaysDataType"))
    elif sysname.startswith("linux"):
        osname, version = "Linux", platform.release()
        ram = parse_meminfo(_read("/proc/meminfo") if runner is run else runner("cat", "/proc/meminfo"))
        cpu_name, n = parse_cpuinfo(_read("/proc/cpuinfo") if runner is run else runner("cat", "/proc/cpuinfo"))
        cpu, cores = cpu_name or platform.processor() or arch, n or os.cpu_count()
        gpus = parse_nvidia_smi(runner("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"))
        if not gpus and (lspci := runner("lspci")):
            gpus = [Gpu(m.group(1).strip(), "amd" if re.search(r"amd|radeon", m.group(1), re.I) else "intel" if "intel" in m.group(1).lower() else "unknown", None) for m in re.finditer(r"(?:VGA compatible controller|3D controller): (.+)", lspci)]
    elif sysname in ("win32", "cygwin"):
        osname, version = "Windows", platform.version()
        total = runner("powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory").strip()
        ram = round(int(total) / 2**30, 1) if total.isdigit() else None
        cpu = runner("powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Select -First 1).Name").strip() or platform.processor() or arch
        cores = os.cpu_count()
        gpus = parse_nvidia_smi(runner("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits")) or parse_windows_gpus(
            runner("powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_VideoController | Select Name | ConvertTo-Json"))
    else:
        osname, version, ram, cpu, cores, gpus = sysname, platform.release(), None, platform.processor() or arch, os.cpu_count(), []
        notes.append("this operating system is not one the companion knows how to inspect")
    if ram is None:
        notes.append("the amount of memory could not be read")
    if not any(g.usable_for_models for g in gpus):
        notes.append("no GPU the local runtimes can use was found: models would run on the CPU, which is much slower")
    return Hardware(osname, version, arch, cpu, cores, ram, gpus, notes)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""
