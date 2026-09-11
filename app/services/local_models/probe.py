"""What this computer can actually run a model on.

Three things shape this module.

**It reports a reason, not a boolean.** "No graphics card found" sends someone to buy
hardware they already own; "your graphics driver did not respond" does not. So every
probe that fails leaves a note explaining itself, and the scan carries on. Every probe
is isolated for the same reason: one missing vendor tool must never abort the whole
scan, because a partial answer plus an explanation is useful and a stack trace is not.

**It describes the machine that will run the model, which is often not this one.** The
supported deployment runs Neo in a container and Ollama on the host, so a scan of this
process measures a Linux virtual machine with an 8 GB allocation and no graphics card
while the model will in fact run on the host's real memory and its GPU. Reporting the
container would be confidently wrong -- it recommends a 1.5B model to someone who could
run a 30B one -- so when the engine is elsewhere the engine is asked instead, through
``engine_host``, and every ``Machine`` records whose hardware it describes in ``source``.

**It never mistakes a limit for a capability.** A cgroup ceiling, a Windows adapter
field too narrow to hold the answer, an integrated card's token memory carve-out: each
reads as a small machine unless it is recognised for what it is. Those are handled
individually and named in the code, because each one silently understates a real
computer by a factor that changes the recommendation.

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
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

from app.core.config import get_settings
from app.services.local_models import engine_host
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

# A scan that established nothing is cached for seconds rather than minutes. The usual
# cause is transient -- a vendor tool still loading, an engine still starting -- and
# holding the failure for five minutes turns a blip into a stuck screen that the Check
# again button cannot fix.
FAILED_CACHE_TTL_SECONDS = 15

# Headroom left on top of what is already free. Available memory excludes what other
# programs currently hold, so this is not an allowance for the operating system -- it is
# room for the machine to grow into while a model is resident, so opening a browser tab
# does not push the model into swap.
MEMORY_RESERVE_GB = 2.0

# What fraction of unified memory to assume a GPU may use, when the operating system
# itself will not say. Apple's default wired limit has long sat near three quarters of
# physical memory; this is only ever a fallback, and using it always leaves a note.
_UNIFIED_FALLBACK_FRACTION = 0.75

# Below this, a card's reported memory is a carve-out rather than a capacity. Integrated
# Radeon graphics declare a few hundred megabytes of dedicated VRAM and then take what
# they actually need from system memory; believing the declared figure would budget half
# a gigabyte on a 32 GB laptop.
_CARVE_OUT_CEILING_GB = 2.0

# Extra places vendor tools live when they are not on PATH. WSL exposes the Windows
# driver's binaries under /usr/lib/wsl/lib, and a ROCm install puts its tools under its
# own prefix; neither is on a default PATH, and both mean the difference between
# "accelerated" and "no graphics card".
_EXTRA_TOOL_PATHS: tuple[str, ...] = (
    "/usr/lib/wsl/lib",
    "/usr/local/nvidia/bin",
    "/opt/nvidia/bin",
    "/usr/local/cuda/bin",
    "/opt/rocm/bin",
    "/usr/local/bin",
    "/usr/bin",
    r"C:\Windows\System32",
    r"C:\Program Files\NVIDIA Corporation\NVSMI",
)

# PCI vendor ids, as sysfs spells them.
_PCI_AMD = "0x1002"
_PCI_NVIDIA = "0x10de"
_PCI_INTEL = "0x8086"

# Adapters that answer a graphics query but cannot compute anything: remote-desktop
# shims, hypervisor displays and the driverless fallback Windows installs for itself.
# Left in, each one is reported as the machine's graphics card, and on a headless or
# virtualised host it is the *only* one -- so the accelerator is chosen from a device
# that has no compute units at all.
_VIRTUAL_ADAPTERS: tuple[str, ...] = (
    "microsoft basic display",
    "microsoft remote display",
    "remote desktop",
    "rdp",
    "citrix",
    "vmware svga",
    "virtualbox",
    "hyper-v video",
    "parsec",
    "displaylink",
    "usb display",
    "teradici",
    "qxl",
    "cirrus logic",
    "standard vga",
    "meta virtual",
    "aspeed",
    "matrox",
)


@dataclass
class _Scan:
    """One scan's accumulated findings. Never shared between calls."""

    notes: list[ProbeNote] = field(default_factory=list)
    # Windows answers for memory, processor and graphics in a single query, so the
    # result is kept here rather than asking three times. ``False`` means asked and
    # failed, which must not be retried; ``None`` means not yet asked.
    windows: dict | None | bool = None

    def note(self, code: str, severity: str, message: str) -> None:
        # One note per cause. A probe reached from two directions -- a GPU looked up by
        # vendor tool and again by sysfs -- must not say the same thing twice.
        if any(existing.code == code for existing in self.notes):
            return
        self.notes.append(ProbeNote(code=code, severity=severity, message=message))  # type: ignore[arg-type]

    def locate(self, program: str) -> str | None:
        """Find a vendor tool, including where installers put it but PATH does not."""

        found = shutil.which(program)
        if found:
            return found
        for directory in _EXTRA_TOOL_PATHS:
            candidate = shutil.which(program, path=directory)
            if candidate:
                return candidate
        return None

    def run(self, argv: list[str]) -> str | None:
        """Run a command and return its output, or None if it would not answer.

        Never raises. A probe that cannot run is a note, not an exception, because the
        rest of the scan is still worth having.
        """

        program = self.locate(argv[0])
        if not program:
            return None
        try:
            completed = subprocess.run(
                [program, *argv[1:]],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
                # A vendor tool that decides to prompt would otherwise inherit a
                # terminal and hang until the timeout on every single scan.
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _LOG.debug("probe %s failed: %s", argv[0], exc)
            return None
        if completed.returncode != 0:
            _LOG.debug("probe %s exited %s", argv[0], completed.returncode)
            return None
        return completed.stdout.strip() or None


# --------------------------------------------------------------------------------------
# Reading the operating system's own figures
# --------------------------------------------------------------------------------------


def _read_first_line(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError:
        return None


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            # Device-tree strings are NUL-terminated, and the NUL survives into the
            # interface if it is not trimmed here.
            return handle.read().strip().strip("\x00").strip()
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


def _cgroup_memory_limit_gb() -> float | None:
    """The ceiling this process is held to, if it is held to one.

    /proc/meminfo shows the kernel's memory, and in a container that is the host's or
    the virtual machine's -- not what this process may use. A container run with
    --memory=4g on a 64 GB host reads as 64 GB, and every recommendation made from that
    figure is a model that will be killed on load.
    """

    candidates = (
        "/sys/fs/cgroup/memory.max",  # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    )
    for path in candidates:
        raw = _read_first_line(path)
        if not raw or not raw.isdigit():
            continue  # "max" on v2 means unlimited.
        value = int(raw)
        # v1 spells "unlimited" as a number near the word size rather than as a word.
        if value <= 0 or value >= 2**62:
            continue
        return value / _GIB
    return None


def _cgroup_memory_usage_gb() -> float | None:
    """How much of a cgroup ceiling is already spoken for."""

    for path in (
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ):
        raw = _read_first_line(path)
        if raw and raw.isdigit():
            return int(raw) / _GIB
    return None


def _windows_memory_gb() -> tuple[float, float] | None:
    """Total and available memory, straight from the kernel.

    ctypes rather than a query language: this cannot be switched off, locked down by
    policy or made to fail by a broken PowerShell, and on Windows those are the ways the
    scan actually breaks. Without it a machine whose PowerShell will not run reports
    zero memory and no reason.
    """

    if platform.system() != "Windows":
        return None

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None
        return status.ullTotalPhys / _GIB, status.ullAvailPhys / _GIB
    except Exception as exc:  # noqa: BLE001 - any ctypes failure is just "unknown"
        _LOG.debug("GlobalMemoryStatusEx failed: %s", exc)
        return None


def _total_memory_gb(scan: _Scan) -> float:
    system = platform.system()
    total = 0.0

    if system == "Darwin":
        output = scan.run(["sysctl", "-n", "hw.memsize"])
        if output and output.isdigit():
            total = int(output) / _GIB
    elif system == "Windows":
        payload = _windows_payload(scan)
        if payload:
            # TotalPhysicalMemory is the installed amount; TotalVisibleMemorySize
            # excludes what the firmware reserved and is the better figure for what a
            # model may use, so the smaller of the two is the honest one.
            for key in ("visible_kb", "total_kb"):
                value = float(payload.get(key) or 0.0) * 1024 / _GIB
                if value > 0:
                    total = min(total, value) if total else value
        kernel = _windows_memory_gb()
        if not total and kernel:
            total = kernel[0]
    else:
        meminfo = _parse_meminfo()
        if "MemTotal" in meminfo:
            total = meminfo["MemTotal"]

    # A ceiling this process is held to is the real total, whatever the kernel reports.
    limit = _cgroup_memory_limit_gb()
    if limit is not None and (not total or limit < total):
        total = limit

    if total <= 0:
        scan.note(
            "memory_unknown",
            "error",
            "Neo could not read how much memory this computer has, so it cannot say "
            "which models will fit.",
        )
    return total


def _macos_available_gb(scan: _Scan) -> float | None:
    """Memory macOS would hand over without swapping, from vm_stat's page counts."""

    output = scan.run(["vm_stat"])
    if not output:
        return None
    page_size = 4096
    header = re.search(r"page size of (\d+) bytes", output)
    if header:
        page_size = int(header.group(1))
    pages = dict(re.findall(r"^(.*?):\s+(\d+)\.$", output, re.MULTILINE))

    def count(name: str) -> int:
        return int(pages.get(name, 0))

    free = count("Pages free")
    if not free and not count("Pages inactive"):
        return None
    # Free, plus what the OS would reclaim under pressure rather than swap. Purgeable
    # pages are deliberately excluded: they are already counted inside the active and
    # inactive totals, and adding them again overstates a machine by gigabytes.
    return (free + count("Pages inactive") + count("Pages speculative")) * page_size / _GIB


def _available_memory_gb(scan: _Scan, total_gb: float) -> float:
    """Memory a model could take right now, without pushing anything into swap."""

    system = platform.system()
    available: float | None = None

    if system == "Darwin":
        available = _macos_available_gb(scan)
    elif system == "Windows":
        kernel = _windows_memory_gb()
        if kernel:
            available = kernel[1]
        else:
            payload = _windows_payload(scan)
            if payload and payload.get("free_kb"):
                available = float(payload["free_kb"]) * 1024 / _GIB
    else:
        meminfo = _parse_meminfo()
        if "MemAvailable" in meminfo:
            available = meminfo["MemAvailable"]
        elif "MemFree" in meminfo:
            available = meminfo["MemFree"] + meminfo.get("Cached", 0.0)

    if available is None:
        # Better to under-promise than to invent a number: assume half is spoken for.
        available = total_gb * 0.5

    # Inside a cgroup, the kernel's idea of what is available ignores the ceiling; what
    # is left of the allowance is the smaller and truer figure.
    limit = _cgroup_memory_limit_gb()
    if limit is not None:
        used = _cgroup_memory_usage_gb()
        headroom = limit - used if used is not None else limit
        available = min(available, max(0.0, headroom))

    return min(available, total_gb) if total_gb else available


# --------------------------------------------------------------------------------------
# The processor
# --------------------------------------------------------------------------------------

# ARM's registered implementer ids. A container on Apple silicon, a Raspberry Pi and a
# Graviton server all report "aarch64" and nothing else through Python, and "aarch64" is
# not a processor a person recognises as theirs. The implementer is one byte and it is
# always there.
_ARM_IMPLEMENTERS: dict[str, str] = {
    "0x41": "ARM",
    "0x42": "Broadcom",
    "0x43": "Marvell",
    "0x46": "Fujitsu",
    "0x48": "HiSilicon",
    "0x4e": "NVIDIA",
    "0x50": "Ampere",
    "0x51": "Qualcomm",
    "0x53": "Samsung",
    "0x56": "Marvell",
    "0x61": "Apple",
    "0x69": "Intel",
    "0xc0": "Ampere",
}


def _cpuinfo_fields() -> dict[str, str]:
    """/proc/cpuinfo's first processor block, lower-cased keys."""

    fields: dict[str, str] = {}
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.partition(":")
                if not sep:
                    continue
                key = key.strip().lower()
                if key not in fields:
                    fields[key] = value.strip()
    except OSError:
        return {}
    return fields


def _linux_cpu_name() -> str:
    """The processor's name on Linux, which on ARM is not in the obvious place.

    x86 puts a marketing name in "model name". ARM does not populate that field at all,
    so the answer has to be assembled from the board, the system firmware or -- failing
    both -- the implementer id, which is the difference between showing someone "Apple"
    and showing them "aarch64".
    """

    fields = _cpuinfo_fields()
    for key in ("model name", "processor version", "cpu model"):
        value = fields.get(key, "")
        # On ARM, "processor" holds an index and "model name" is absent; a bare number
        # is not a name.
        if value and not value.isdigit():
            return value

    # Board and system firmware, in the order of how specific they are.
    for path in (
        "/sys/firmware/devicetree/base/model",
        "/sys/firmware/devicetree/base/compatible",
    ):
        value = _read_text(path)
        if value:
            return value.split("\x00")[0].replace(",", " ")
    vendor = _read_first_line("/sys/class/dmi/id/sys_vendor") or ""
    product = _read_first_line("/sys/class/dmi/id/product_name") or ""
    board = " ".join(part for part in (vendor, product) if part and "o.e.m." not in part.lower())
    if board.strip():
        return board.strip()
    for key in ("hardware", "model", "machine", "cpu part"):
        value = fields.get(key, "")
        if value and not value.isdigit():
            if key == "cpu part":
                break  # Handled below, where the implementer gives it a vendor.
            return value

    implementer = _ARM_IMPLEMENTERS.get(fields.get("cpu implementer", "").lower())
    machine = platform.machine() or "unknown"
    if implementer:
        return f"{implementer} {machine} processor"
    return ""


def _cpu_name(scan: _Scan) -> str:
    system = platform.system()
    if system == "Darwin":
        name = scan.run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if name:
            return name
        # Apple silicon dropped the brand string under some sandboxes; the chip is still
        # named in the hardware model.
        model = scan.run(["sysctl", "-n", "hw.model"])
        if model:
            return model
    elif system == "Windows":
        payload = _windows_payload(scan)
        if payload and payload.get("cpu"):
            return str(payload["cpu"]).strip()
        identifier = os.environ.get("PROCESSOR_IDENTIFIER", "").strip()
        if identifier:
            return identifier
    else:
        name = _linux_cpu_name()
        if name:
            return name

    return platform.processor() or platform.machine() or "Unknown processor"


def _cpu_cores(scan: _Scan) -> int:
    """Cores this process may actually use.

    os.cpu_count() reports the machine's, which in a container with --cpus=2 on a
    32-core host is wrong by sixteen times and feeds straight into how fast a
    CPU-only model is expected to be.
    """

    counts: list[int] = []

    quota = _read_first_line("/sys/fs/cgroup/cpu.max")  # cgroup v2: "quota period"
    if quota:
        parts = quota.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]):
            counts.append(max(1, round(int(parts[0]) / int(parts[1]))))
    v1_quota = _read_first_line("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    v1_period = _read_first_line("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if v1_quota and v1_period and v1_quota.lstrip("-").isdigit() and v1_period.isdigit():
        if int(v1_quota) > 0 and int(v1_period) > 0:
            counts.append(max(1, round(int(v1_quota) / int(v1_period))))

    # Affinity covers taskset and cpuset, which quota does not.
    if hasattr(os, "sched_getaffinity"):
        try:
            counts.append(len(os.sched_getaffinity(0)))
        except OSError:
            pass
    counts.append(os.cpu_count() or 1)
    return max(1, min(counts))


# --------------------------------------------------------------------------------------
# Where this process is running
# --------------------------------------------------------------------------------------


def _wsl() -> bool:
    """Windows Subsystem for Linux, which is a virtual machine but not a container.

    It matters separately because WSL2 takes a fixed share of the host's memory -- half
    by default -- so its figures understate the computer without any container being
    involved, and its graphics card is reached through a path no Linux install uses.
    """

    if platform.system() != "Linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    release = _read_first_line("/proc/sys/kernel/osrelease") or ""
    return "microsoft" in release.lower() or "wsl" in release.lower()


def _containerized() -> bool:
    """Whether this process is inside a container.

    It matters because a container sees the container, not the computer. On macOS and
    Windows that means a Linux virtual machine with its own memory limit and no access
    to the real graphics card, so the numbers would understate the machine badly.
    """

    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    # systemd and podman set this to the runtime's name for exactly this question.
    if os.environ.get("container"):
        return True
    if _wsl():
        # WSL's cgroup paths mention docker when Docker Desktop is installed, so this
        # has to be settled before the cgroup check below.
        return False
    for path in ("/proc/1/cgroup", "/proc/self/mountinfo"):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            continue
        markers = ("docker", "kubepods", "containerd", "lxc", "/podman")
        if any(marker in content for marker in markers):
            return True
    return False


# --------------------------------------------------------------------------------------
# Apple
# --------------------------------------------------------------------------------------


def _apple_silicon(scan: _Scan) -> bool:
    """Whether this Mac has Apple silicon, regardless of what Python was built for.

    An x86_64 Python under Rosetta reports "x86_64" for the machine it is running on,
    which would send an M-series Mac down the Intel path and out the other side with no
    graphics card at all. The kernel is asked instead.
    """

    if platform.system() != "Darwin":
        return False
    if platform.machine() == "arm64":
        return True
    if scan.run(["sysctl", "-n", "sysctl.proc_translated"]) == "1":
        return True
    return scan.run(["sysctl", "-n", "hw.optional.arm64"]) == "1"


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


def _apple_chip_name(scan: _Scan) -> str:
    """The chip as Apple names it, which is also what the bandwidth table matches on."""

    brand = scan.run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if brand:
        return brand
    # Under Rosetta the brand string is the emulated x86 one, so it must be ignored in
    # favour of the hardware model. hw.model gives "Mac15,3"; the chip is better named
    # by the GPU, which system_profiler knows.
    for entry in _macos_display_entries(scan):
        name = str(entry.get("sppci_model") or entry.get("_name") or "").strip()
        if name:
            return name
    return "Apple Silicon"


def _detect_apple(scan: _Scan, total_gb: float) -> tuple[Accelerator, list[Gpu]] | None:
    """Apple Silicon, where the GPU is the CPU's memory controller with extra steps."""

    if not _apple_silicon(scan):
        return None
    # Unified memory: the whole of it is addressable by the GPU, and how much it may
    # actually use is decided in _usable_memory_gb.
    return "metal", [Gpu(index=0, name=_apple_chip_name(scan), memory_gb=total_gb, integrated=True)]


def _macos_display_entries(scan: _Scan) -> list[dict]:
    """system_profiler's graphics inventory, or an empty list.

    Only reached on an Intel Mac or a Rosetta process: it takes a second or two to
    answer, which is worth paying to find a Radeon Pro that no other interface on macOS
    will report, and not worth paying on Apple silicon where the chip is already known.
    """

    output = scan.run(["system_profiler", "-json", "SPDisplaysDataType"])
    if not output:
        return []
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    entries = payload.get("SPDisplaysDataType")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _note_macos_graphics(scan: _Scan) -> None:
    """Name the graphics card an Intel Mac has, and say why it is not being used.

    A Mac Pro with a Radeon Pro W6800X has 32 GB of graphics memory, and the engine
    cannot use a byte of it: Ollama accelerates through Metal on Apple silicon only, and
    runs on the processor on every Intel Mac. Both halves of that need saying. Reported
    as usable, the machine gets recommendations it cannot run; left out entirely, the
    owner of a 32 GB card is told their computer has no graphics card Neo can use and
    has no way to know the limit is the engine's rather than theirs.
    """

    for entry in _macos_display_entries(scan):
        name = str(entry.get("sppci_model") or entry.get("_name") or "").strip()
        if not name or _is_virtual_adapter(name):
            continue
        scan.note(
            "gpu_unsupported",
            "info",
            f"Neo found {name}, which the local model engine cannot use on an Intel "
            "Mac. Models will run on the processor instead.",
        )
        return


# --------------------------------------------------------------------------------------
# NVIDIA
# --------------------------------------------------------------------------------------


def _detect_nvidia(scan: _Scan) -> tuple[Accelerator, list[Gpu]] | None:
    output = scan.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.total,name",
            "--format=csv,noheader,nounits",
        ]
    )
    if output is None:
        if scan.locate("nvidia-smi"):
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
    unreadable = 0
    for index, line in enumerate(output.splitlines()):
        if not line.strip():
            continue
        memory, _, name = line.partition(",")
        name = name.strip() or "NVIDIA GPU"
        try:
            # nvidia-smi reports mebibytes with --nounits.
            memory_gb = float(memory.strip()) * 1024 * 1024 / _GIB
        except ValueError:
            # "[Not Supported]" and "[N/A]" on vGPU and some laptop configurations. The
            # card is real; only its size is missing, and dropping the row here would
            # report a machine with a 4090 in it as having no graphics card.
            unreadable += 1
            gpus.append(Gpu(index=index, name=name, memory_gb=0.0, memory_unknown=True))
            continue
        gpus.append(Gpu(index=index, name=name, memory_gb=memory_gb))

    if not gpus:
        return None
    if unreadable:
        scan.note(
            "gpu_size_unknown",
            "info",
            "The graphics driver did not report how much memory this card has, so Neo "
            "has judged models against system memory instead.",
        )
    return "cuda", gpus


def _detect_tegra(scan: _Scan, total_gb: float) -> tuple[Accelerator, list[Gpu]] | None:
    """NVIDIA Jetson and other Tegra boards, which have no nvidia-smi at all.

    A Jetson runs models perfectly well on its GPU, shares one pool of memory with the
    CPU as Apple silicon does, and ships without the tool every other NVIDIA probe
    depends on -- so without this it reads as a slow ARM board with no graphics.
    """

    if platform.system() != "Linux":
        return None
    model = _read_text("/sys/firmware/devicetree/base/model") or ""
    tegra = os.path.exists("/etc/nv_tegra_release") or "tegra" in model.lower()
    if not tegra and "jetson" not in model.lower():
        return None
    name = model.split("\x00")[0].strip() or "NVIDIA Jetson"
    return "cuda", [Gpu(index=0, name=name, memory_gb=total_gb, integrated=True)]


# --------------------------------------------------------------------------------------
# Linux graphics through sysfs
# --------------------------------------------------------------------------------------


def _is_virtual_adapter(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _VIRTUAL_ADAPTERS)


def _drm_cards() -> list[str]:
    """The graphics *devices* under /sys/class/drm, and not their connectors.

    /sys/class/drm holds a directory per card and another per connector -- card0,
    card0-eDP-1, card0-DP-2 -- and every one of them has a "device" link back to the
    same card. Globbing card* therefore returns one real GPU as four or five, each
    counted again in the total.
    """

    cards = []
    for path in sorted(glob.glob("/sys/class/drm/card*")):
        if re.fullmatch(r"card\d+", os.path.basename(path)):
            device = os.path.join(path, "device")
            if os.path.exists(device):
                cards.append(device)
    return cards


def _detect_amd(scan: _Scan) -> tuple[Accelerator, list[Gpu]] | None:
    """AMD through sysfs, because no AMD vendor tool is reliably installed."""

    gpus: list[Gpu] = []
    seen: set[str] = set()
    for index, device in enumerate(_drm_cards()):
        vendor = _read_first_line(os.path.join(device, "vendor"))
        # 0x1002 is AMD. Without this check an Intel or virtual display adapter would be
        # reported as a card capable of running a model.
        if vendor != _PCI_AMD:
            continue
        # The same card can appear twice when it exposes a render node beside its
        # primary node; its PCI address is what makes it one card.
        address = os.path.basename(os.path.realpath(device))
        if address in seen:
            continue
        seen.add(address)
        total = _read_first_line(os.path.join(device, "mem_info_vram_total"))
        if not total or not total.isdigit():
            continue
        name = _read_first_line(os.path.join(device, "product_name")) or "AMD GPU"
        if _is_virtual_adapter(name):
            continue
        memory_gb = int(total) / _GIB
        # Integrated Radeon graphics declare a token carve-out and then take what they
        # need from system memory. Believing the carve-out budgets half a gigabyte on a
        # 32 GB laptop and rejects every model worth running.
        integrated = memory_gb < _CARVE_OUT_CEILING_GB
        gpus.append(Gpu(index=index, name=name, memory_gb=memory_gb, integrated=integrated))
    return ("rocm", gpus) if gpus else None


def _note_unusable_graphics(scan: _Scan) -> None:
    """Say what was found and why it is not being counted.

    Silence here is the failure mode this module exists to avoid: someone with an Arc
    A770 or an unsupported NVIDIA driver reads "no graphics card Neo can use" and
    concludes their hardware is missing rather than unsupported.
    """

    found: list[str] = []
    for device in _drm_cards():
        vendor = _read_first_line(os.path.join(device, "vendor"))
        if vendor == _PCI_INTEL:
            found.append("Intel")
        elif vendor == _PCI_NVIDIA:
            found.append("NVIDIA")
        elif vendor == _PCI_AMD:
            found.append("AMD")
    if not found:
        return

    names = sorted(set(found))
    if any(note.code == "nvidia_no_answer" for note in scan.notes):
        # The vendor tool already explained itself, and in more useful detail than this
        # would. Two explanations of one card read as two problems.
        return
    if names == ["NVIDIA"]:
        scan.note(
            "nvidia_driver_missing",
            "warning",
            "Neo found an NVIDIA graphics card but no working driver for it. Installing "
            "the NVIDIA driver would let models run on the card instead of the "
            "processor.",
        )
        return
    scan.note(
        "gpu_unsupported",
        "info",
        f"Neo found {names[0]} graphics, which the local model engine cannot use yet. "
        "Models will run on the processor instead.",
    )


# --------------------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------------------

_WINDOWS_QUERY = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "$os = Get-CimInstance Win32_OperatingSystem; "
    "$cs = Get-CimInstance Win32_ComputerSystem; "
    "$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1; "
    "$gpu = Get-CimInstance Win32_VideoController; "
    # AdapterRAM is a 32-bit field and cannot hold the size of any card worth using, so
    # the driver's own registry value is read alongside it.
    "$vram = @(Get-ChildItem "
    "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}' "
    "| ForEach-Object { (Get-ItemProperty $_.PSPath).'HardwareInformation.qwMemorySize' } "
    "| Where-Object { $_ -gt 0 }); "
    "[Console]::Out.Write((ConvertTo-Json -Compress -Depth 4 @{"
    "visible_kb = $os.TotalVisibleMemorySize; "
    "total_kb = [math]::Round($cs.TotalPhysicalMemory / 1024); "
    "free_kb = $os.FreePhysicalMemory; "
    "cpu = $cpu.Name; "
    "cores = $cpu.NumberOfLogicalProcessors; "
    "vram = @($vram); "
    "gpus = @($gpu | ForEach-Object { @{ name = $_.Name; ram = $_.AdapterRAM } })"
    "}))"
)


def _windows_payload(scan: _Scan) -> dict | None:
    """Everything Windows can tell us, in one query rather than four.

    Asked at most once per scan and remembered on the scan, including the failure: the
    query takes the better part of a second and four probes want its answer.
    """

    if platform.system() != "Windows":
        return None
    if scan.windows is False:
        return None
    if isinstance(scan.windows, dict):
        return scan.windows

    output = None
    # PowerShell 7 is named differently and is the only one present on a machine where
    # Windows PowerShell has been removed by policy.
    for shell in ("powershell", "pwsh"):
        output = scan.run([shell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_QUERY])
        if output:
            break
    if not output:
        scan.windows = False
        scan.note(
            "windows_query_failed",
            "warning",
            "Neo could not ask Windows about this computer's graphics, so it has "
            "judged models against memory alone.",
        )
        return None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        scan.windows = False
        return None
    if not isinstance(payload, dict):
        scan.windows = False
        return None
    scan.windows = payload
    return payload


def _detect_windows_gpu(scan: _Scan) -> tuple[Accelerator, list[Gpu]] | None:
    """Windows graphics, preferring the vendor tool that reports an exact size.

    nvidia-smi is asked first because it is installed with every NVIDIA driver and gives
    the real figure; Windows' own field is 32 bits wide and reports a 24 GB card as 4 GB
    or as nothing at all.
    """

    nvidia = _detect_nvidia(scan)
    if nvidia:
        return nvidia

    payload = _windows_payload(scan)
    if not payload:
        return None

    registry_vram = sorted(
        (
            float(value) / _GIB
            for value in payload.get("vram") or []
            if isinstance(value, (int, float))
        ),
        reverse=True,
    )

    gpus: list[Gpu] = []
    for index, entry in enumerate(payload.get("gpus") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip() or "Graphics card"
        # A remote-desktop or hypervisor adapter answers this query and cannot compute
        # anything; on a virtualised host it is often the only one listed.
        if _is_virtual_adapter(name):
            continue
        ram = entry.get("ram")
        memory_gb = float(ram) / _GIB if isinstance(ram, (int, float)) and ram > 0 else 0.0
        # 4 GB exactly is the 32-bit field saturating, not a 4 GB card.
        if memory_gb and abs(memory_gb - 4.0) < 0.01 and registry_vram:
            memory_gb = max(memory_gb, registry_vram[0])
        elif not memory_gb and registry_vram:
            memory_gb = registry_vram[min(index, len(registry_vram) - 1)]
        lowered = name.lower()
        integrated = any(
            marker in lowered
            for marker in ("intel", "uhd graphics", "iris", "vega 8", "radeon graphics")
        )
        gpus.append(
            Gpu(
                index=index,
                name=name,
                memory_gb=memory_gb,
                integrated=integrated,
                memory_unknown=memory_gb <= 0 and not integrated,
            )
        )

    if not gpus:
        return None

    usable = [gpu for gpu in gpus if not gpu.integrated]
    accelerator: Accelerator = "cpu"
    if any("nvidia" in gpu.name.lower() or "geforce" in gpu.name.lower() for gpu in usable):
        accelerator = "cuda"
    elif any(
        marker in gpu.name.lower() for gpu in usable for marker in ("amd", "radeon", "firepro")
    ):
        accelerator = "rocm"
    elif any("arc" in gpu.name.lower() for gpu in usable):
        accelerator = "vulkan"

    if accelerator == "cpu":
        # Integrated graphics only. Reporting them as the accelerator would promise
        # speed that will not arrive, but saying nothing reads as "no graphics card".
        scan.note(
            "gpu_unsupported",
            "info",
            f"Neo found {gpus[0].name}, which the local model engine cannot use yet. "
            "Models will run on the processor instead.",
        )
        return None

    if any(gpu.memory_unknown for gpu in usable):
        scan.note(
            "gpu_size_unknown",
            "info",
            "Windows did not report how much memory this graphics card has, so Neo has "
            "judged models against system memory instead.",
        )
    return accelerator, usable


# --------------------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------------------


def _detect_graphics(scan: _Scan, total_gb: float) -> tuple[Accelerator, list[Gpu], bool]:
    """The accelerator, the cards behind it, and whether its memory is the system's.

    Ordered by authority rather than by vendor: a probe that reports an exact size from
    the driver itself is asked before one that infers a size from a file.
    """

    system = platform.system()
    if system == "Darwin":
        probes = (lambda: _detect_apple(scan, total_gb),)
    elif system == "Windows":
        probes = (lambda: _detect_windows_gpu(scan),)
    else:
        probes = (
            lambda: _detect_nvidia(scan),
            lambda: _detect_tegra(scan, total_gb),
            lambda: _detect_amd(scan),
        )

    for attempt in probes:
        try:
            found = attempt()
        except Exception as exc:  # noqa: BLE001 - a broken probe is not a broken scan
            _LOG.debug("graphics probe raised: %s", exc)
            continue
        if found:
            accelerator, gpus = found
            unified = bool(gpus) and all(gpu.integrated for gpu in gpus)
            return accelerator, gpus, unified

    if system == "Linux":
        _note_unusable_graphics(scan)
    elif system == "Darwin":
        _note_macos_graphics(scan)
    return "cpu", [], False


def _usable_memory_gb(
    scan: _Scan,
    accelerator: str,
    gpus: list[Gpu],
    total_gb: float,
    available_gb: float,
    unified: bool,
) -> float:
    """How much memory a model may actually occupy here."""

    if unified and accelerator == "metal":
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

    if unified:
        # Any other shared-memory design -- a Jetson, an APU, integrated graphics the
        # engine can use. There is one pool, so the budget is the pool's free share and
        # not the token amount the card claims as its own.
        return max(0.0, available_gb - MEMORY_RESERVE_GB)

    sized = [gpu for gpu in gpus if gpu.memory_gb > 0]
    if sized:
        # Neither Ollama nor llama.cpp splits one model across cards by default, so two
        # cards are not one big card. Verified against Ollama's scheduling docs; taking
        # the sum here would recommend models that cannot load.
        return max(gpu.memory_gb for gpu in sized)

    return max(0.0, available_gb - MEMORY_RESERVE_GB)


# The names a container uses for the machine it is running on. They matter as a
# category of their own: that machine's home directory is mounted into the container for
# agent mode, so unlike any other remote address, its hardware *can* be established.
_CONTAINER_HOST_NAMES = (
    "host.docker.internal",
    "gateway.docker.internal",
    "host.containers.internal",
)
_LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1", "0.0.0.0")

# Where the engine runs, relative to this process.
#   this_machine    measure it directly
#   container_host  the computer this container runs on, whose home is mounted here
#   elsewhere       another computer entirely; nothing here describes it
EngineLocation = str


def _engine_location() -> tuple[str, EngineLocation]:
    """Where the engine is, read from its configured address rather than guessed.

    The three cases need separating because each admits a different source of truth, and
    conflating the last two is how one machine's hardware gets reported as another's: a
    GPU box on the network is not the host whose home directory is mounted here, and
    reading that mount to describe it would be wrong with no way to tell.
    """

    try:
        url = get_settings().ollama_url
    except Exception as exc:  # noqa: BLE001 - unconfigured settings are not a scan failure
        _LOG.debug("could not read the engine address: %s", exc)
        return "", "this_machine"

    host = (urlparse(url).hostname or "").lower()
    if not host or host in _LOOPBACK_NAMES:
        return host, "this_machine"
    if host in _CONTAINER_HOST_NAMES:
        return host, "container_host"
    return host, "elsewhere"


def _scan_local(scan: _Scan) -> Machine:
    """This process's own machine, measured directly."""

    total_gb = _total_memory_gb(scan)
    available_gb = _available_memory_gb(scan, total_gb)
    accelerator, gpus, unified = _detect_graphics(scan, total_gb)
    usable = _usable_memory_gb(scan, accelerator, gpus, total_gb, available_gb, unified)

    return Machine(
        total_memory_gb=total_gb,
        available_memory_gb=available_gb,
        cpu_name=_cpu_name(scan),
        cpu_cores=_cpu_cores(scan),
        cpu_arch=platform.machine() or "unknown",
        os_name=platform.system() or "unknown",
        accelerator=accelerator,
        gpus=tuple(gpus),
        unified_memory=unified,
        containerized=_containerized(),
        source="local",
        usable_memory_gb=usable,
        probe_notes=tuple(scan.notes),
    )


def _machine_from_engine_host(
    scan: _Scan,
    found: engine_host.HostInventory,
    local: Machine,
    host_name: str,
    location: EngineLocation,
) -> Machine:
    """The engine's own account of the computer it runs on.

    Preferred over this process's figures without hesitation: these were measured by the
    process that will load the model, with the code that will decide whether it fits.
    """

    gpus = tuple(
        Gpu(index=index, name=gpu.name, memory_gb=gpu.memory_gb, integrated=gpu.integrated)
        for index, gpu in enumerate(found.gpus)
    )
    accelerator: Accelerator = found.accelerator  # type: ignore[assignment]
    unified = found.unified_memory

    total_gb = found.total_memory_gb
    available_gb = found.free_memory_gb or 0.0
    if gpus:
        # Ollama's per-GPU total is the budget it will itself load against -- on Apple
        # silicon that is the unified memory limit, on a discrete card the card.
        usable = max(gpu.memory_gb for gpu in gpus)
        if not total_gb:
            # Never logged because no model has been loaded yet. The graphics budget is
            # known and is what fit is decided against, so the answer is still usable;
            # what is missing is only the machine's total, and inventing one would put a
            # number on screen that nobody measured.
            scan.note(
                "engine_host_memory_unknown",
                "info",
                "Neo could not read how much memory your computer has in total, so it "
                "has judged models against what your graphics can use.",
            )
    else:
        usable = max(0.0, (available_gb or total_gb * 0.5) - MEMORY_RESERVE_GB)

    if not available_gb:
        available_gb = total_gb

    if location == "container_host":
        scan.note(
            "engine_host",
            "info",
            "Neo itself is running in a container, so it has asked the local model "
            "engine about your computer instead. The figures below are your computer's, "
            "not the container's.",
        )
    else:
        scan.note(
            "engine_host",
            "info",
            f"Models will run on {host_name} rather than on this computer, so the "
            "figures below are that machine's.",
        )

    return Machine(
        total_memory_gb=total_gb or usable,
        available_memory_gb=min(available_gb, total_gb) if total_gb else usable,
        # The engine reports the chip, not the processor, and on a shared-memory design
        # those are the same part. Where they are not, the local processor name would be
        # the container's and saying nothing is the honest answer.
        cpu_name=(gpus[0].name if gpus and unified else ""),
        cpu_cores=local.cpu_cores,
        cpu_arch=local.cpu_arch,
        os_name=found.os_name or local.os_name,
        accelerator=accelerator,
        gpus=gpus,
        unified_memory=unified,
        containerized=local.containerized,
        source="engine_host",
        engine_host=host_name,
        usable_memory_gb=usable,
        probe_notes=tuple(scan.notes),
    )


def _scan_now() -> Machine:
    scan = _Scan()
    local = _scan_local(scan)

    host_name, location = _engine_location()
    if location == "this_machine":
        if local.containerized:
            # In a container with the engine inside it too: the container's limits are
            # genuinely the limits, and are worth saying so rather than implying the
            # machine is bigger than the answer.
            scan.note(
                "containerized",
                "info",
                "Neo and the local model engine are both running inside a container, "
                "so the figures below are the container's limits.",
            )
            return _with_notes(local, scan)
        return local

    # The mounted host home describes the container's own host and nothing else, so it
    # is admissible for that case alone. For any other address only a path the user
    # configured deliberately can be trusted to describe the right computer.
    found = engine_host.inventory(
        mounted_host=location == "container_host",
        this_machine=False,
    )
    if found is not None:
        return _machine_from_engine_host(scan, found, local, host_name, location)

    # The engine is on another computer and would not say what it has. Anything this
    # process can measure describes the wrong machine, so the figures are withheld
    # rather than shown with a hedge: a 30B machine reported as 8 GB is acted on, and a
    # missing number is asked about.
    if location == "container_host":
        scan.note(
            "engine_host_unknown",
            "error",
            "Models will run on your computer through the local model engine, which Neo "
            "cannot see from inside its container -- so it cannot say which models will "
            "fit. Start the engine, or run Neo directly on your computer, and check "
            "again.",
        )
    else:
        scan.note(
            "engine_host_unknown",
            "error",
            f"Models will run on {host_name}, not on this computer, and Neo cannot see "
            "what that machine has -- so it cannot say which models will fit there.",
        )
    return Machine(
        total_memory_gb=0.0,
        available_memory_gb=0.0,
        cpu_name="",
        cpu_cores=local.cpu_cores,
        cpu_arch=local.cpu_arch,
        os_name="",
        accelerator="cpu",
        gpus=(),
        unified_memory=False,
        containerized=local.containerized,
        source="unknown",
        engine_host=host_name,
        usable_memory_gb=0.0,
        probe_notes=tuple(scan.notes),
    )


def _with_notes(machine: Machine, scan: _Scan) -> Machine:
    """The same machine, carrying every note gathered since it was built."""

    return replace(machine, probe_notes=tuple(scan.notes))


_cache_lock = threading.Lock()
_cache: tuple[float, Machine] | None = None


def detect(*, fresh: bool = False) -> Machine:
    """Scan this computer. Cached briefly; pass ``fresh=True`` for the Check again path."""

    global _cache
    with _cache_lock:
        if not fresh and _cache:
            age = time.time() - _cache[0]
            # A scan that established nothing expires quickly, so that a Check again a
            # moment after starting the engine can actually find it.
            ttl = CACHE_TTL_SECONDS if _cache[1].usable_memory_gb > 0 else FAILED_CACHE_TTL_SECONDS
            if age < ttl:
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
