"""What the computer that will actually run the model has.

Neo's own process is frequently not where inference happens. The supported deployment
puts Neo in a container and Ollama on the host, reached at ``host.docker.internal``, so
a scan of this process measures a Linux virtual machine with an 8 GB allocation and no
graphics card while the model will in fact run on the host's 32 GB and its GPU. Numbers
from the wrong machine are worse than no numbers: they recommend a 1.5B model to someone
who could comfortably run a 30B one, and they do it confidently.

So when the engine is elsewhere, ask the engine. Ollama writes its own hardware
inventory to its log at startup and before every load:

    msg="inference compute" id=0 library=Metal description="Apple M5" type=iGPU
        total="25.0 GiB" available="25.0 GiB"
    msg="system memory" total="32.0 GiB" free="19.5 GiB" free_swap="0 B"

That is the best possible source, and not merely a workaround for the container. It is
the measurement made by the process that will do the loading, using the same code that
will later decide whether the model fits -- closer to the truth than anything Neo could
compute for itself. Where the two disagree, Ollama is right by definition.

The log is reachable because the deployment already mounts the user's home directory for
agent mode, and Ollama keeps its log under it. Nothing here writes, nothing here runs a
command, and every failure returns None so that the caller can fall back and explain
itself.
"""

from __future__ import annotations

import logging
import os
import platform
import re
from dataclasses import dataclass
from datetime import datetime

from app.core.config import get_settings

_LOG = logging.getLogger(__name__)

_GIB = 1024**3

# Ollama prints sizes as a number and a unit. Both conventions appear across versions.
_SIZE_MULTIPLIERS = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
}

# Which of Ollama's GPU libraries map to which accelerator. Ollama's own spelling has
# changed case between versions ("metal" and "Metal"), so matching is case-folded.
_LIBRARY_ACCELERATORS = {
    "metal": "metal",
    "cuda": "cuda",
    "rocm": "rocm",
    "hip": "rocm",
    "vulkan": "vulkan",
    "sycl": "sycl",
    "oneapi": "sycl",
    "cpu": "cpu",
}

# Internal handles, not names to show anyone. Newer Ollama puts the readable name in
# ``description`` and a handle like MTL0 in ``name``; older versions put the readable
# name in ``name``. Preferring description and rejecting these covers both.
_HANDLE_PATTERN = re.compile(r"^(?:MTL\d+|GPU-[0-9a-f-]{8,}|[0-9]+|cpu)$", re.IGNORECASE)

# A startup prints one line per GPU at the same instant. Lines within this many seconds
# of the newest one are the same boot's inventory; anything older is a previous boot,
# possibly of a different machine, and must not be mixed in.
_RUN_WINDOW_SECONDS = 30.0

# The inventory line is written once per Ollama start, so on a long-running server it
# sits far behind the tail of a log that grows with every request. The file therefore has
# to be read from the front, which is only safe with a ceiling: 512 MB of log scanned
# line by line costs a fraction of a second and cannot exhaust memory.
_MAX_SCAN_BYTES = 512 * 1024**2

# Only these lines are parsed; the substring test runs on every line of a large file, so
# it is deliberately cheaper than the tokenizer it guards.
_WANTED = ('msg="inference compute"', 'msg="system memory"', 'msg="server config"')


@dataclass(frozen=True)
class HostGpu:
    """One accelerator, as the engine itself reported it."""

    library: str
    name: str
    memory_gb: float
    integrated: bool


@dataclass(frozen=True)
class HostInventory:
    """The engine host's hardware, as the engine measured it."""

    gpus: tuple[HostGpu, ...]
    total_memory_gb: float
    free_memory_gb: float
    os_name: str
    log_path: str
    observed_at: float

    @property
    def accelerator(self) -> str:
        """The accelerator of the largest same-library group, or "cpu"."""

        for gpu in sorted(self.gpus, key=lambda g: g.memory_gb, reverse=True):
            mapped = _LIBRARY_ACCELERATORS.get(gpu.library.lower())
            if mapped and mapped != "cpu":
                return mapped
        return "cpu"

    @property
    def unified_memory(self) -> bool:
        """Whether the GPU shares system memory, which changes what the budget means."""

        return bool(self.gpus) and all(gpu.integrated for gpu in self.gpus)


def _size_gb(value: str | None) -> float | None:
    """`"25.0 GiB"` as a number of gibibytes, or None if it is not a size."""

    if not value:
        return None
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]+)\s*", value)
    if not match:
        return None
    multiplier = _SIZE_MULTIPLIERS.get(match.group(2).upper())
    if multiplier is None:
        return None
    return float(match.group(1)) * multiplier / _GIB


def _fields(line: str) -> dict[str, str]:
    """A log line's ``key=value`` pairs, respecting double-quoted values.

    Written out rather than regexed because values contain spaces, equals signs and
    bracketed maps, and a pattern that survives all three is less legible than this.
    """

    fields: dict[str, str] = {}
    index = 0
    length = len(line)
    while index < length:
        equals = line.find("=", index)
        if equals == -1:
            break
        # Clamped to the cursor: rfind answers -1 for "no space in this key", and
        # taking that as position zero swallows every field parsed so far into the key.
        key_start = max(index, line.rfind(" ", index, equals) + 1)
        key = line[key_start:equals].strip()
        cursor = equals + 1
        if cursor < length and line[cursor] == '"':
            end = cursor + 1
            while end < length:
                if line[end] == "\\":
                    end += 2
                    continue
                if line[end] == '"':
                    break
                end += 1
            value = line[cursor + 1 : end]
            index = end + 1
        else:
            end = line.find(" ", cursor)
            end = length if end == -1 else end
            value = line[cursor:end]
            index = end + 1
        if key:
            fields[key] = value
    return fields


def _timestamp(fields: dict[str, str]) -> float:
    """The line's own clock reading. 0.0 when absent, which sorts it oldest."""

    raw = fields.get("time")
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.0


def _os_from_models_path(value: str) -> str:
    """The host's operating system, inferred from a path only that system produces."""

    if re.search(r"[A-Za-z]:[\\/]|\\\\", value):
        return "Windows"
    if "/Users/" in value:
        return "Darwin"
    if "/home/" in value or "/root/" in value or "/var/lib/" in value:
        return "Linux"
    return ""


def log_candidates(*, mounted_host: bool = True, this_machine: bool = True) -> list[str]:
    """Every place the engine's log could be, most authoritative first.

    Which places are admissible depends on where the engine is, and only the caller
    knows that. A log describes the computer it was written on, so reading this
    machine's log to answer a question about a GPU box on the network would attribute
    one machine's hardware to another -- confidently, and with nothing to notice it by.

        mounted_host  the host home directory mounted into this container. Valid only
                      when the engine runs on that same host, which is exactly what
                      host.docker.internal means.
        this_machine  paths belonging to this process's own computer. Valid only when
                      the engine runs here too.

    An explicitly configured path is always admissible: it was set deliberately, by
    someone who knows which machine it describes.
    """

    candidates: list[str] = []

    def add(path: str | None) -> None:
        if path and path not in candidates:
            candidates.append(path)

    add(os.environ.get("NEO_OLLAMA_LOG_PATH") or None)

    # The host's home directory, mounted for agent mode. Ollama's log lives under it on
    # macOS and Windows, which is exactly where Docker Desktop hides the real machine.
    roots = (get_settings().workspace_live_roots or "").split(":") if mounted_host else []
    for root in roots:
        root = root.strip()
        if not root:
            continue
        add(os.path.join(root, ".ollama", "logs", "server.log"))
        add(os.path.join(root, "AppData", "Local", "Ollama", "server.log"))
        # A mount of /Users or /home rather than of one home directory.
        try:
            for entry in sorted(os.scandir(root), key=lambda e: e.name)[:64]:
                if entry.is_dir(follow_symlinks=False):
                    add(os.path.join(entry.path, ".ollama", "logs", "server.log"))
                    add(os.path.join(entry.path, "AppData", "Local", "Ollama", "server.log"))
        except OSError:
            pass

    if this_machine:
        if platform.system() == "Windows":
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                add(os.path.join(local_appdata, "Ollama", "server.log"))
        add(os.path.join(os.path.expanduser("~"), ".ollama", "logs", "server.log"))
        add("/var/log/ollama/server.log")
    return candidates


def _scan_log(path: str) -> HostInventory | None:
    """Parse one log file. None if it is missing, unreadable or says nothing useful."""

    computes: list[tuple[float, dict[str, str]]] = []
    memory: tuple[float, dict[str, str]] | None = None
    os_name = ""
    scanned = 0

    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                scanned += len(line)
                if scanned > _MAX_SCAN_BYTES:
                    break
                if not any(marker in line for marker in _WANTED):
                    continue
                fields = _fields(line)
                message = fields.get("msg", "")
                when = _timestamp(fields)
                if message == "inference compute":
                    computes.append((when, fields))
                elif message == "system memory":
                    # Logged before every load, so the newest is the freshest reading.
                    if memory is None or when >= memory[0]:
                        memory = (when, fields)
                elif message == "server config" and not os_name:
                    match = re.search(r"OLLAMA_MODELS:(\S+)", fields.get("env", ""))
                    if match:
                        os_name = _os_from_models_path(match.group(1))
    except OSError as exc:
        _LOG.debug("engine log %s unreadable: %s", path, exc)
        return None

    if not computes and memory is None:
        return None

    gpus: list[HostGpu] = []
    observed = 0.0
    if computes:
        newest = max(when for when, _ in computes)
        observed = newest
        for when, fields in computes:
            if newest - when > _RUN_WINDOW_SECONDS:
                continue  # A previous boot, possibly of different hardware.
            library = fields.get("library", "")
            if _LIBRARY_ACCELERATORS.get(library.lower(), "cpu") == "cpu":
                continue  # Ollama lists the CPU fallback as a device; it is not a GPU.
            total = _size_gb(fields.get("total"))
            if total is None or total <= 0:
                continue
            description = fields.get("description", "").strip()
            fallback = fields.get("name", "").strip()
            if not description or _HANDLE_PATTERN.match(description):
                description = "" if _HANDLE_PATTERN.match(fallback) else fallback
            gpus.append(
                HostGpu(
                    library=library,
                    name=description or f"{library} GPU",
                    memory_gb=total,
                    # Integrated graphics share system memory, so their budget is carved
                    # out of the same pool the figures above describe.
                    integrated=fields.get("type", "").lower() == "igpu",
                )
            )

    total_gb = 0.0
    free_gb = 0.0
    if memory is not None:
        observed = max(observed, memory[0])
        total_gb = _size_gb(memory[1].get("total")) or 0.0
        free_gb = _size_gb(memory[1].get("free")) or 0.0

    if not gpus and total_gb <= 0:
        return None

    return HostInventory(
        gpus=tuple(gpus),
        total_memory_gb=total_gb,
        free_memory_gb=free_gb,
        os_name=os_name,
        log_path=path,
        observed_at=observed,
    )


def inventory(*, mounted_host: bool = True, this_machine: bool = True) -> HostInventory | None:
    """The engine host's hardware, or None if the engine has not said.

    See ``log_candidates`` for what the two arguments admit. Never raises: a caller that
    cannot get this has to explain itself to the user, and an exception here would deny
    it the chance.
    """

    for path in log_candidates(mounted_host=mounted_host, this_machine=this_machine):
        try:
            found = _scan_log(path)
        except Exception as exc:  # noqa: BLE001 - a malformed log is "no answer"
            _LOG.debug("engine log %s could not be parsed: %s", path, exc)
            continue
        if found is not None:
            return found
    return None
