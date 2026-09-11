"""Value types for local model setup.

These are dataclasses rather than loose dicts on purpose. A ``Machine`` can be built
directly in a test, which is what lets the sizing math be exercised across every machine
shape -- Apple, NVIDIA, AMD, no graphics card at all -- without owning any of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# What the user is trying to do. The wizard asks this first, before anything about
# hardware, because it changes which model wins even on identical machines: a coding
# goal wants a long context, a chat goal wants replies that arrive quickly.
Goal = Literal["chat", "writing", "coding", "reasoning", "images", "offline"]
GOALS: tuple[str, ...] = ("chat", "writing", "coding", "reasoning", "images", "offline")

# How a model runs here, which is the grouping the whole screen is organised around.
# "too_big" is a real answer and is shown with its reason rather than hidden -- a model
# that silently disappears leaves the user wondering where it went.
FitTier = Literal["comfortable", "good", "tight", "too_big"]
FIT_TIERS: tuple[str, ...] = ("comfortable", "good", "tight", "too_big")

# How a model is computed here. "vulkan" and "sycl" are the integrated-graphics paths
# Ollama can use on Intel and on AMD APUs; they are slower than a discrete card but real,
# and collapsing them into "cpu" would tell someone their graphics cannot be used when it
# can.
Accelerator = Literal["metal", "cuda", "rocm", "vulkan", "sycl", "cpu"]
ACCELERATORS: tuple[str, ...] = ("metal", "cuda", "rocm", "vulkan", "sycl", "cpu")

# Whose hardware a ``Machine`` describes. Neo's supported deployment runs it in a
# container and the engine on the host, so these are routinely different computers, and a
# caller that cannot tell them apart will present a container's limits as the user's.
#   local        this process's own machine, measured directly
#   engine_host  the machine running the engine, as the engine itself reported it
#   unknown      nothing could be established; the figures are absent, not low
MachineSource = Literal["local", "engine_host", "unknown"]

Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class Gpu:
    """One graphics processor the scan found."""

    index: int
    name: str
    memory_gb: float
    # Integrated graphics take their memory from the system pool rather than having their
    # own, so a budget drawn from them competes with everything else running.
    integrated: bool = False
    # Set when the card was found but its size could not be read -- a real card with an
    # unknown capacity, which is not the same as a card with none.
    memory_unknown: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "memory_gb": round(self.memory_gb, 2),
            "integrated": self.integrated,
            "memory_unknown": self.memory_unknown,
        }


@dataclass(frozen=True)
class ProbeNote:
    """Something the scan could not determine, and why.

    The reason is the point. "No graphics card found" sends someone to buy hardware they
    already own; "your graphics driver did not respond" does not. Every probe that fails
    leaves one of these rather than silently reporting an absence.
    """

    code: str
    severity: Severity
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass(frozen=True)
class Machine:
    """What this computer can run a model on."""

    total_memory_gb: float
    available_memory_gb: float
    cpu_name: str
    cpu_cores: int
    cpu_arch: str
    os_name: str
    accelerator: Accelerator = "cpu"
    gpus: tuple[Gpu, ...] = ()
    unified_memory: bool = False
    containerized: bool = False
    # Whose machine this describes, and how it was established. A container's own limits
    # must never be presented as the user's computer when the engine runs elsewhere.
    source: MachineSource = "local"
    # Where the engine is, when it is not this process's machine. Display only.
    engine_host: str = ""
    # What a model may actually occupy here, after the operating system, the display and
    # whatever else is running take their share. Every fit decision divides by this.
    usable_memory_gb: float = 0.0
    probe_notes: tuple[ProbeNote, ...] = ()

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpus)

    @property
    def gpu_name(self) -> str:
        return self.gpus[0].name if self.gpus else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_memory_gb": round(self.total_memory_gb, 1),
            "available_memory_gb": round(self.available_memory_gb, 1),
            "cpu_name": self.cpu_name,
            "cpu_cores": self.cpu_cores,
            "cpu_arch": self.cpu_arch,
            "os_name": self.os_name,
            "accelerator": self.accelerator,
            "gpus": [gpu.as_dict() for gpu in self.gpus],
            "has_gpu": self.has_gpu,
            "gpu_name": self.gpu_name,
            "unified_memory": self.unified_memory,
            "containerized": self.containerized,
            "source": self.source,
            "engine_host": self.engine_host,
            "usable_memory_gb": round(self.usable_memory_gb, 1),
            "probe_notes": [note.as_dict() for note in self.probe_notes],
        }


@dataclass(frozen=True)
class Fit:
    """Whether a model runs here, and what running it would feel like."""

    tier: FitTier
    reason: str
    quantization: str
    context: int
    required_gb: float
    tokens_per_sec: float | None
    # required_gb / usable_memory_gb. Kept because the tier boundaries are defined on it
    # and a caller sorting by headroom should not have to recompute it.
    headroom_ratio: float = 0.0
    # Set when the catalog had to correct this row, so the UI can show that the estimate
    # rests on metadata Neo did not fully trust.
    data_caveat: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "reason": self.reason,
            "quantization": self.quantization,
            "context": self.context,
            "required_gb": round(self.required_gb, 2),
            "tokens_per_sec": round(self.tokens_per_sec, 1) if self.tokens_per_sec else None,
            "headroom_ratio": round(self.headroom_ratio, 3),
            "data_caveat": self.data_caveat,
        }


@dataclass(frozen=True)
class ScoreParts:
    """Why a model ranked where it did, in dimensions a person could argue with."""

    capability: float = 0.0
    responsiveness: float = 0.0
    headroom: float = 0.0
    context_reach: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": round(self.capability, 1),
            "responsiveness": round(self.responsiveness, 1),
            "headroom": round(self.headroom, 1),
            "context_reach": round(self.context_reach, 1),
        }


@dataclass(frozen=True)
class Recommendation:
    """One model, judged against one machine for one goal."""

    model: dict[str, Any]
    fit: Fit
    score: float
    parts: ScoreParts = field(default_factory=ScoreParts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "fit": self.fit.as_dict(),
            "score": round(self.score, 1),
            "parts": self.parts.as_dict(),
        }
