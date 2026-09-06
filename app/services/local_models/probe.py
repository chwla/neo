"""What this computer can actually run a model on.

Detection reports a *reason*, not a boolean. "No graphics card found" sends someone to
buy hardware they already own; "your graphics driver did not respond" does not. So every
probe that fails leaves a note explaining itself, and the scan carries on.

Every probe is isolated for the same reason. One missing vendor tool must never abort
the whole scan, because a partial answer plus an explanation is useful and a stack trace
is not.

State lives on a per-call ``_Scan`` rather than in module globals, so two scans running
at once -- the wizard mounting while the page refreshes -- cannot see each other's
half-finished results.

Standard library only. Everything here is information the operating system already
exposes, and a compiled dependency would be a poor trade for it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

from app.services.local_models.types import Accelerator, Gpu, Machine, ProbeNote

_LOG = logging.getLogger(__name__)

_GIB = 1024**3

# A vendor tool that has not answered in ten seconds is not going to. Long enough for a
# cold nvidia-smi on a laptop waking its discrete card, short enough that the wizard
# does not appear to hang.
PROBE_TIMEOUT_SECONDS = 10

# Hardware does not change, but free memory does, and the budget is computed from what
# is free. Five minutes keeps a re-opened screen instant without letting the answer go
# stale enough to matter.
CACHE_TTL_SECONDS = 300

# Headroom left on top of what is already free. Available memory excludes what other
# programs currently hold, so this is not an allowance for the operating system -- it is
# room for the machine to grow into while a model is resident, so opening a browser tab
# does not push the model into swap.
MEMORY_RESERVE_GB = 2.0

# What fraction of unified memory to assume a GPU may use, when macOS itself will not
# say. Apple's default wired limit has long sat near three quarters of physical memory;
# this is only ever a fallback, and using it always leaves a note saying so.
_UNIFIED_FALLBACK_FRACTION = 0.75


@dataclass
class _Scan:
    """One scan's accumulated findings. Never shared between calls."""

    notes: list[ProbeNote] = field(default_factory=list)

    def note(self, code: str, severity: str, message: str) -> None:
        self.notes.append(ProbeNote(code=code, severity=severity, message=message))  # type: ignore[arg-type]

    def run(self, argv: list[str]) -> str | None:
        """Run a command and return its output, or None if it would not answer.

        Never raises. A probe that cannot run is a note, not an exception, because the
        rest of the scan is still worth having.
        """

        if not shutil.which(argv[0]):
            return None
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _LOG.debug("probe %s failed: %s", argv[0], exc)
            return None
        if completed.returncode != 0:
            _LOG.debug("probe %s exited %s", argv[0], completed.returncode)
            return None
        return completed.stdout.strip() or None


def _read_first_line(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError:
        return None


def _parse_meminfo() -> dict[str, float]:
    """/proc/meminfo as gigabytes. Empty off Linux."""

    values: dict[str, float] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    # meminfo reports kibibytes.
                    values[key.strip()] = int(parts[0]) * 1024 / _GIB
    except OSError:
        return {}
    return values


def _total_memory_gb(scan: _Scan) -> float:
    system = platform.system()
    if system == "Darwin":
        output = scan.run(["sysctl", "-n", "hw.memsize"])
        if output and output.isdigit():
            return int(output) / _GIB
    elif system == "Linux":
        meminfo = _parse_meminfo()
        if "MemTotal" in meminfo:
            return meminfo["MemTotal"]
    if system != "Windows":
        scan.note(
            "memory_unknown",
            "error",
            "Neo could not read how much memory this computer has, so it cannot say "
            "which models will fit.",
        )
    return 0.0


def _available_memory_gb(scan: _Scan, total_gb: float) -> float:
    """Memory a model could take right now, without pushing anything into swap."""

    system = platform.system()
    if system == "Linux":
        meminfo = _parse_meminfo()
        if "MemAvailable" in meminfo:
            return meminfo["MemAvailable"]
    elif system == "Darwin":
        output = scan.run(["vm_stat"])
        page_size = 4096
        if output:
            header = re.search(r"page size of (\d+) bytes", output)
            if header:
                page_size = int(header.group(1))
            pages = dict(re.findall(r"^(.*?):\s+(\d+)\.$", output, re.MULTILINE))

            def count(name: str) -> int:
                return int(pages.get(name, 0))

            # Free, plus what the OS would reclaim under pressure rather than swap.
            reclaimable = (
                count("Pages free")
                + count("Pages inactive")
                + count("Pages speculative")
                + count("Pages purgeable")
            )
            if reclaimable:
                return reclaimable * page_size / _GIB
    # Better to under-promise than to invent a number: assume half is spoken for.
    return total_gb * 0.5


def _cpu_name(scan: _Scan) -> str:
    system = platform.system()
    if system == "Darwin":
        return scan.run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()
    if system == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.lower().startswith("model name"):
                        return line.partition(":")[2].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "Unknown processor"


def _containerized() -> bool:
    """Whether this process is inside a container.

    It matters because a container sees the container, not the computer. On macOS and
    Windows that means a Linux virtual machine with its own memory limit and no access
    to the real graphics card, so the numbers would understate the machine badly.
    """

    if os.path.exists("/.dockerenv"):
        return True
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as handle:
            content = handle.read()
        return "docker" in content or "kubepods" in content or "containerd" in content
    except OSError:
        return False


def _metal_budget_gb() -> float | None:
    """What macOS itself says the GPU may use, in bytes, via Metal.

    Asked rather than assumed: on Apple Silicon the GPU shares system memory, and the
    split is the operating system's to decide. Returns None rather than raising -- this
    runs on every Apple scan and a ctypes failure must not take the scan down with it.
    """

    if platform.system() != "Darwin":
        return None
    try:
        metal_path = ctypes.util.find_library("Metal")
        objc_path = ctypes.util.find_library("objc")
        if not metal_path or not objc_path:
            return None
        metal = ctypes.cdll.LoadLibrary(metal_path)
        objc = ctypes.cdll.LoadLibrary(objc_path)

        metal.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
        device = metal.MTLCreateSystemDefaultDevice()
        if not device:
            return None

        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        selector = objc.sel_registerName(b"recommendedMaxWorkingSetSize")

        objc.objc_msgSend.restype = ctypes.c_uint64
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        size = objc.objc_msgSend(ctypes.c_void_p(device), ctypes.c_void_p(selector))
        return size / _GIB if size else None
    except Exception as exc:  # noqa: BLE001 - any ctypes failure is just "unknown"
        _LOG.debug("Metal budget query failed: %s", exc)
        return None


def _detect_apple(scan: _Scan, total_gb: float) -> tuple[Accelerator, list[Gpu]] | None:
    """Apple Silicon, where the GPU is the CPU's memory controller with extra steps."""

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None
    name = scan.run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon"
    # Unified memory: the whole of it is addressable by the GPU, and how much it may
    # actually use is decided in _usable_memory_gb.
    return "metal", [Gpu(index=0, name=name, memory_gb=total_gb)]


def _detect_nvidia(scan: _Scan) -> tuple[Accelerator, list[Gpu]] | None:
    output = scan.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.total,name",
            "--format=csv,noheader,nounits",
        ]
    )
    if output is None:
        if shutil.which("nvidia-smi"):
            # The tool is installed but will not answer. That is a driver or library
            # mismatch, and saying "no graphics card" here would be a lie that costs
            # someone money.
            scan.note(
                "nvidia_no_answer",
                "warning",
                "An NVIDIA graphics tool is installed but did not respond, which "
                "usually means the driver and its libraries disagree. Neo has ignored "
                "the graphics card for now.",
            )
        return None

    gpus: list[Gpu] = []
    for index, line in enumerate(output.splitlines()):
        memory, _, name = line.partition(",")
        try:
            # nvidia-smi reports mebibytes with --nounits.
            memory_gb = float(memory.strip()) * 1024 * 1024 / _GIB
        except ValueError:
            continue
        gpus.append(Gpu(index=index, name=name.strip() or "NVIDIA GPU", memory_gb=memory_gb))
    return ("cuda", gpus) if gpus else None


def _detect_amd(scan: _Scan) -> tuple[Accelerator, list[Gpu]] | None:
    """AMD through sysfs, because no AMD vendor tool is reliably installed."""

    gpus: list[Gpu] = []
    for index, device in enumerate(sorted(glob.glob("/sys/class/drm/card*/device"))):
        vendor = _read_first_line(os.path.join(device, "vendor"))
        # 0x1002 is AMD. Without this check an Intel or virtual display adapter would be
        # reported as a card capable of running a model.
        if vendor != "0x1002":
            continue
        total = _read_first_line(os.path.join(device, "mem_info_vram_total"))
        if not total or not total.isdigit():
            continue
        name = _read_first_line(os.path.join(device, "product_name")) or "AMD GPU"
        gpus.append(Gpu(index=index, name=name, memory_gb=int(total) / _GIB))
    return ("rocm", gpus) if gpus else None


_WINDOWS_QUERY = (
    "$os = Get-CimInstance Win32_OperatingSystem; "
    "$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1; "
    "$gpu = Get-CimInstance Win32_VideoController; "
    "[Console]::Out.Write((ConvertTo-Json -Compress @{"
    "total_kb = $os.TotalVisibleMemorySize; "
    "free_kb = $os.FreePhysicalMemory; "
    "cpu = $cpu.Name; "
    "cores = $cpu.NumberOfLogicalProcessors; "
    "gpus = @($gpu | ForEach-Object { @{ name = $_.Name; ram = $_.AdapterRAM } })"
    "}))"
)


def _detect_windows(scan: _Scan) -> dict | None:
    """Everything Windows can tell us, in one PowerShell call rather than four."""

    if platform.system() != "Windows":
        return None
    output = scan.run(["powershell", "-NoProfile", "-Command", _WINDOWS_QUERY])
    if not output:
        scan.note(
            "windows_query_failed",
            "warning",
            "Neo could not ask Windows about this computer's memory and graphics, so "
            "the figures below may be incomplete.",
        )
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _usable_memory_gb(
    scan: _Scan,
    accelerator: str,
    gpus: list[Gpu],
    total_gb: float,
    available_gb: float,
    unified: bool,
) -> float:
    """How much memory a model may actually occupy here."""

    if unified:
        # An explicit wired limit is the user's own decision about their machine, and
        # they know more about it than any heuristic does. Still clamped to physical
        # memory below: the setting accepts values the machine does not have.
        wired = scan.run(["sysctl", "-n", "iogpu.wired_limit_mb"])
        if wired and wired.isdigit() and int(wired) > 0:
            return min(int(wired) * 1024 * 1024 / _GIB, total_gb)
        measured = _metal_budget_gb()
        if measured:
            return min(measured, total_gb)
        scan.note(
            "unified_budget_estimated",
            "info",
            "Neo could not ask this computer how much memory its graphics can use, so "
            "the figure below is an estimate.",
        )
        return max(0.0, total_gb * _UNIFIED_FALLBACK_FRACTION)

    if gpus:
        # Neither Ollama nor llama.cpp splits one model across cards by default, so two
        # cards are not one big card. Verified against Ollama's scheduling docs; taking
        # the sum here would recommend models that cannot load.
        return max(gpu.memory_gb for gpu in gpus)

    return max(0.0, available_gb - MEMORY_RESERVE_GB)


def _scan_now() -> Machine:
    scan = _Scan()
    system = platform.system()

    windows = _detect_windows(scan)
    if windows:
        return _machine_from_windows(scan, windows)

    total_gb = _total_memory_gb(scan)
    available_gb = _available_memory_gb(scan, total_gb)

    accelerator: Accelerator = "cpu"
    gpus: list[Gpu] = []
    unified = False

    # Each probe is tried independently; the first that answers wins, and one that
    # cannot answer has already left its own note.
    apple = _detect_apple(scan, total_gb)
    if apple:
        accelerator, gpus = apple
        unified = True
    else:
        for probe in (_detect_nvidia, _detect_amd):
            try:
                found = probe(scan)
            except Exception as exc:  # noqa: BLE001 - a broken probe is not a broken scan
                _LOG.debug("probe %s raised: %s", probe.__name__, exc)
                continue
            if found:
                accelerator, gpus = found
                break

    containerized = _containerized()
    if containerized:
        scan.note(
            "containerized",
            "warning",
            "Neo is running inside a container, so it can only see part of this "
            "computer. The figures below may be lower than what you actually have.",
        )

    usable = _usable_memory_gb(scan, accelerator, gpus, total_gb, available_gb, unified)

    return Machine(
        total_memory_gb=total_gb,
        available_memory_gb=available_gb,
        cpu_name=_cpu_name(scan),
        cpu_cores=os.cpu_count() or 1,
        cpu_arch=platform.machine() or "unknown",
        os_name=system or "unknown",
        accelerator=accelerator,
        gpus=tuple(gpus),
        unified_memory=unified,
        containerized=containerized,
        usable_memory_gb=usable,
        probe_notes=tuple(scan.notes),
    )


def _machine_from_windows(scan: _Scan, payload: dict) -> Machine:
    total_gb = float(payload.get("total_kb") or 0) * 1024 / _GIB
    available_gb = float(payload.get("free_kb") or 0) * 1024 / _GIB

    gpus: list[Gpu] = []
    for index, entry in enumerate(payload.get("gpus") or []):
        # AdapterRAM is a 32-bit field, so anything past 4 GB reads back wrong or null.
        # Reporting it would understate a big card, so the card is listed without a
        # size and the budget falls back to system memory.
        ram = entry.get("ram")
        memory_gb = float(ram) / _GIB if isinstance(ram, (int, float)) and ram else 0.0
        name = str(entry.get("name") or "Graphics card")
        gpus.append(Gpu(index=index, name=name, memory_gb=memory_gb))

    sized = [gpu for gpu in gpus if gpu.memory_gb > 0]
    if gpus and not sized:
        scan.note(
            "gpu_size_unknown",
            "info",
            "Windows did not report how much memory this graphics card has, so Neo has "
            "judged models against system memory instead.",
        )

    accelerator: Accelerator = "cuda" if any("nvidia" in g.name.lower() for g in gpus) else "cpu"
    if accelerator == "cpu":
        amd = ("amd", "radeon")
        if any(vendor in g.name.lower() for g in gpus for vendor in amd):
            accelerator = "rocm"

    usable = (
        max(gpu.memory_gb for gpu in sized)
        if sized and accelerator != "cpu"
        else max(0.0, available_gb - MEMORY_RESERVE_GB)
    )

    containerized = _containerized()
    return Machine(
        total_memory_gb=total_gb,
        available_memory_gb=available_gb,
        cpu_name=str(payload.get("cpu") or "Unknown processor"),
        cpu_cores=int(payload.get("cores") or os.cpu_count() or 1),
        cpu_arch=platform.machine() or "unknown",
        os_name="Windows",
        accelerator=accelerator,
        gpus=tuple(gpus),
        unified_memory=False,
        containerized=containerized,
        usable_memory_gb=usable,
        probe_notes=tuple(scan.notes),
    )


_cache_lock = threading.Lock()
_cache: tuple[float, Machine] | None = None


def detect(*, fresh: bool = False) -> Machine:
    """Scan this computer. Cached briefly; pass ``fresh=True`` for the Check again path."""

    global _cache
    with _cache_lock:
        if not fresh and _cache and time.time() - _cache[0] < CACHE_TTL_SECONDS:
            return _cache[1]
    machine = _scan_now()
    with _cache_lock:
        _cache = (time.time(), machine)
    return machine


def reset_cache() -> None:
    """Drop the cached scan. Used by tests and by an explicit rescan."""

    global _cache
    with _cache_lock:
        _cache = None
