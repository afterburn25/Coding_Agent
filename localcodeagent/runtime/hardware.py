from __future__ import annotations

import csv
import io
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class GPUInfo:
    index: int
    name: str
    total_vram_mb: int
    used_vram_mb: int
    free_vram_mb: int
    utilization_percent: int | None = None
    temperature_c: int | None = None

    @property
    def total_vram_gb(self) -> float:
        return round(self.total_vram_mb / 1024, 2)

    @property
    def free_vram_gb(self) -> float:
        return round(self.free_vram_mb / 1024, 2)


@dataclass(slots=True)
class HardwareSnapshot:
    platform: str
    total_ram_gb: float
    available_ram_gb: float
    gpus: list[GPUInfo] = field(default_factory=list)
    nvidia_smi_available: bool = False

    @property
    def total_vram_gb(self) -> float:
        return round(sum(g.total_vram_mb for g in self.gpus) / 1024, 2)

    @property
    def free_vram_gb(self) -> float:
        return round(sum(g.free_vram_mb for g in self.gpus) / 1024, 2)

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "total_ram_gb": self.total_ram_gb,
            "available_ram_gb": self.available_ram_gb,
            "total_vram_gb": self.total_vram_gb,
            "free_vram_gb": self.free_vram_gb,
            "nvidia_smi_available": self.nvidia_smi_available,
            "gpus": [asdict(g) | {"total_vram_gb": g.total_vram_gb, "free_vram_gb": g.free_vram_gb} for g in self.gpus],
        }


def _memory_windows() -> tuple[int, int]:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _memory_linux() -> tuple[int, int]:
    values: dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key in {"MemTotal", "MemAvailable", "MemFree"}:
                values[key] = int(rest.strip().split()[0]) * 1024
    return values.get("MemTotal", 0), values.get("MemAvailable", values.get("MemFree", 0))


def _memory_posix() -> tuple[int, int]:
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        total = page * os.sysconf("SC_PHYS_PAGES")
        avail = page * os.sysconf("SC_AVPHYS_PAGES")
        return int(total), int(avail)
    except (AttributeError, OSError, ValueError):
        return 0, 0


def _detect_memory() -> tuple[float, float]:
    try:
        if os.name == "nt":
            total, avail = _memory_windows()
        elif platform.system().lower() == "linux":
            total, avail = _memory_linux()
        else:
            total, avail = _memory_posix()
    except OSError:
        total, avail = 0, 0
    gib = 1024 ** 3
    return round(total / gib, 2), round(avail / gib, 2)


def parse_nvidia_smi_csv(text: str) -> list[GPUInfo]:
    gpus: list[GPUInfo] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5:
            continue
        cells = [x.strip() for x in row]
        try:
            util = int(float(cells[5])) if len(cells) > 5 and cells[5] not in {"N/A", "[N/A]", ""} else None
            temp = int(float(cells[6])) if len(cells) > 6 and cells[6] not in {"N/A", "[N/A]", ""} else None
            gpus.append(GPUInfo(
                index=int(cells[0]),
                name=cells[1],
                total_vram_mb=int(float(cells[2])),
                used_vram_mb=int(float(cells[3])),
                free_vram_mb=int(float(cells[4])),
                utilization_percent=util,
                temperature_c=temp,
            ))
        except ValueError:
            continue
    return gpus


def _detect_nvidia() -> tuple[bool, list[GPUInfo]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False, []
    query = (
        "index,name,memory.total,memory.used,memory.free,"
        "utilization.gpu,temperature.gpu"
    )
    try:
        proc = subprocess.run(
            [exe, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return True, []
        return True, parse_nvidia_smi_csv(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        return True, []


def detect_hardware() -> HardwareSnapshot:
    total_ram, available_ram = _detect_memory()
    smi, gpus = _detect_nvidia()
    return HardwareSnapshot(
        platform=f"{platform.system()} {platform.release()}".strip(),
        total_ram_gb=total_ram,
        available_ram_gb=available_ram,
        gpus=gpus,
        nvidia_smi_available=smi,
    )
