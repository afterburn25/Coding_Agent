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
    vendor: str = ""
    shared_memory_mb: int = 0     # Windows shared GPU memory — NOT VRAM

    @property
    def total_vram_gb(self) -> float:
        return round(self.total_vram_mb / 1024, 2)

    @property
    def free_vram_gb(self) -> float:
        return round(self.free_vram_mb / 1024, 2)

    @property
    def shared_memory_gb(self) -> float:
        return round(self.shared_memory_mb / 1024, 2)


@dataclass(slots=True)
class HardwareSnapshot:
    platform: str
    total_ram_gb: float
    available_ram_gb: float
    gpus: list[GPUInfo] = field(default_factory=list)
    nvidia_smi_available: bool = False
    cpu_name: str = ""
    cpu_logical_cores: int = 0
    pagefile_gb: float = 0.0
    uptime_seconds: float | None = None
    load_1m: float | None = None

    @property
    def total_vram_gb(self) -> float:
        return round(sum(g.total_vram_mb for g in self.gpus) / 1024, 2)

    @property
    def free_vram_gb(self) -> float:
        return round(sum(g.free_vram_mb for g in self.gpus) / 1024, 2)

    @property
    def ram_used_percent(self) -> float:
        if not self.total_ram_gb:
            return 0.0
        used = max(0.0, self.total_ram_gb - self.available_ram_gb)
        return round(used / self.total_ram_gb * 100, 1)

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "total_ram_gb": self.total_ram_gb,
            "available_ram_gb": self.available_ram_gb,
            "ram_used_percent": self.ram_used_percent,
            "total_vram_gb": self.total_vram_gb,
            "free_vram_gb": self.free_vram_gb,
            "nvidia_smi_available": self.nvidia_smi_available,
            "cpu_name": self.cpu_name,
            "cpu_logical_cores": self.cpu_logical_cores,
            "pagefile_gb": self.pagefile_gb,
            "uptime_seconds": self.uptime_seconds,
            "load_1m": self.load_1m,
            "gpus": [asdict(g) | {"total_vram_gb": g.total_vram_gb, "free_vram_gb": g.free_vram_gb, "shared_memory_gb": g.shared_memory_gb} for g in self.gpus],
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
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return True, []
        return True, parse_nvidia_smi_csv(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        return True, []


def _gpu_vendor(name: str) -> str:
    n = name.lower()
    if "nvidia" in n or "geforce" in n or "quadro" in n or " rtx" in n or "gtx" in n:
        return "nvidia"
    if "amd" in n or "radeon" in n:
        return "amd"
    if "intel" in n or "arc" in n or "iris" in n or "uhd" in n:
        return "intel"
    return "other"


def _detect_gpus_registry() -> list[GPUInfo]:
    """Windows display-adapter registry read: HardwareInformation.qwMemorySize
    carries true 64-bit dedicated VRAM (Win32_VideoController.AdapterRAM is a
    capped DWORD). Shared system memory comes from DedicatedVideoMemory's
    sibling value. Works for NVIDIA/AMD/Intel without vendor tools."""
    if os.name != "nt":
        return []
    import winreg
    gpus: list[GPUInfo] = []
    key_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as root:
            idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, idx)
                except OSError:
                    break
                idx += 1
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name, _ = winreg.QueryValueEx(k, "DriverDesc")
                        try:
                            mem, _ = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")
                        except OSError:
                            try:
                                mem, _ = winreg.QueryValueEx(k, "HardwareInformation.MemorySize")
                            except OSError:
                                mem = 0
                        shared = 0
                        try:
                            shared, _ = winreg.QueryValueEx(k, "HardwareInformation.SharedSystemMemory")  # noqa: SLF001
                        except OSError:
                            pass
                        if not mem:
                            continue
                        gpus.append(GPUInfo(
                            index=len(gpus),
                            name=str(name),
                            total_vram_mb=int(mem) // (1024 * 1024),
                            used_vram_mb=0,
                            free_vram_mb=int(mem) // (1024 * 1024),
                            vendor=_gpu_vendor(str(name)),
                            shared_memory_mb=int(shared) // (1024 * 1024) if shared else 0,
                        ))
                except OSError:
                    continue
    except OSError:
        return []
    return gpus


def _cpu_info() -> tuple[str, int]:
    try:
        name = platform.processor() or ""
        if os.name == "nt":
            import winreg
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                ) as k:
                    name = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
            except OSError:
                pass
        return name, os.cpu_count() or 0
    except Exception:
        return "", os.cpu_count() or 0


def _pagefile_windows() -> int:
    if os.name != "nt":
        return 0
    import ctypes
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
    s = MEMORYSTATUSEX(); s.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
        return 0
    return max(0, int(s.ullTotalPageFile) - int(s.ullTotalPhys))


def _uptime_seconds() -> float | None:
    try:
        if os.name == "nt":
            import ctypes
            get_tick = getattr(ctypes.windll.kernel32, "GetTickCount64", None)
            if get_tick is not None:
                return round(get_tick() / 1000, 1)
            return None
        text = open("/proc/uptime", "r", encoding="utf-8").read()
        return round(float(text.split()[0]), 1)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _load_1m() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        return None


def gpu_backend_for(gpus: list[GPUInfo]) -> str:
    """Best llama.cpp GPU backend guess for the installed stack (Vulkan
    runtime is bundled; CUDA only when NVIDIA tooling exists)."""
    vendors = {g.vendor for g in gpus}
    if "nvidia" in vendors:
        return "vulkan"  # bundled runtime is Vulkan; works for all vendors
    if vendors & {"amd", "intel"}:
        return "vulkan"
    return "cpu"


def detect_hardware() -> HardwareSnapshot:
    total_ram, available_ram = _detect_memory()
    smi, gpus = _detect_nvidia()
    if not gpus:
        # Non-NVIDIA or nvidia-smi missing: registry fallback gives real
        # dedicated VRAM for any vendor's driver.
        gpus = _detect_gpus_registry()
    else:
        # Enrich nvidia-smi rows with vendor + shared memory from registry.
        reg = {g.name: g for g in _detect_gpus_registry()}
        for g in gpus:
            g.vendor = _gpu_vendor(g.name)
            match = reg.get(g.name)
            if match:
                g.shared_memory_mb = match.shared_memory_mb
    cpu_name, cores = _cpu_info()
    return HardwareSnapshot(
        platform=f"{platform.system()} {platform.release()}".strip(),
        total_ram_gb=total_ram,
        available_ram_gb=available_ram,
        gpus=gpus,
        nvidia_smi_available=smi,
        cpu_name=cpu_name,
        cpu_logical_cores=cores,
        pagefile_gb=round(_pagefile_windows() / (1024 ** 3), 2),
        uptime_seconds=_uptime_seconds(),
        load_1m=_load_1m(),
    )


def hardware_profile(disk_path: str | os.PathLike | None = None) -> "object":
    """Normalized install-time hardware profile for the tier planner.

    Dedicated VRAM only; shared Windows GPU memory is reported separately and
    never counts. Multi-GPU VRAM stays per-device (no pooling).
    """
    from ..models.tiers import HardwareProfile

    snap = detect_hardware()
    cpu_name, cores = _cpu_info()
    disk_gb = 0.0
    if disk_path is not None:
        try:
            usage = shutil.disk_usage(str(disk_path))
            disk_gb = round(usage.free / (1024 ** 3), 2)
        except OSError:
            disk_gb = 0.0
    primary = snap.gpus[0] if snap.gpus else None
    return HardwareProfile(
        gpu_name=primary.name if primary else "",
        gpu_vendor=primary.vendor if primary else "",
        dedicated_vram_gb=primary.total_vram_gb if primary else 0.0,
        shared_gpu_memory_gb=primary.shared_memory_gb if primary else 0.0,
        gpu_count=len(snap.gpus),
        gpu_vram_gb_list=[g.total_vram_gb for g in snap.gpus],
        total_ram_gb=snap.total_ram_gb,
        available_ram_gb=snap.available_ram_gb,
        cpu_logical_cores=cores,
        cpu_name=cpu_name,
        disk_free_gb=disk_gb,
        gpu_backend=gpu_backend_for(snap.gpus),
        pagefile_gb=round(_pagefile_windows() / (1024 ** 3), 2),
    )


def hardware_fingerprint(snap: HardwareSnapshot | None = None) -> str:
    """Stable GPU+RAM identity used to detect hardware changes across
    restarts (GPU upgrade → offer model-stack reevaluation)."""
    import hashlib
    snap = snap or detect_hardware()
    parts = [f"ram:{snap.total_ram_gb:.1f}"]
    for g in sorted(snap.gpus, key=lambda x: x.index):
        parts.append(f"gpu{g.index}:{g.name}:{g.total_vram_mb}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
