"""Will this model run on this computer, what will it feel like, and is it the best choice?

Pure functions: no probing, no catalog loading, no network. Everything takes a
``Machine`` and a catalog row and returns a verdict, which is what lets the whole matrix
-- Apple, NVIDIA, AMD and no graphics card at all, crossed with dense and sparse models
and every quantization -- be tested without owning any of that hardware.

Which model is *best* rests on how capable each one measured, not on its size: the
catalog carries independent measurements per goal, and ``rank`` weighs them against
whether replies would keep up and whether the model fits with room.

Every constant below is either derived from a published source or measured by
``scripts/calibrate_local_models.py`` on real hardware. See
``docs/local-models-calibration.md`` for the measurements. Nothing here is a round
number chosen because it looked about right: a recommendation that is plausible but
wrong is worse than no recommendation, because the person receiving it has no way to
tell.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from app.services.local_models.types import (
    Fit,
    FitTier,
    Machine,
    Recommendation,
    ScoreParts,
)

# --------------------------------------------------------------------------------------
# Quantization
# --------------------------------------------------------------------------------------

# Bytes one weight costs, per format. Triangulated three ways, which is why these carry
# more decimal places than a guess would deserve:
#
#   1. Block layout, from llama.cpp's ggml-common.h. A k-quant packs a 256-weight
#      super-block alongside the scales and minimums needed to dequantise it, so the
#      real cost is (block bytes / 256), not the nominal bit width. Q4_K is
#      d(2)+dmin(2)+scales(12)+qs(128) = 144 bytes / 256 = 0.5625.
#   2. llama.cpp's own published sizes for LLaMA-7B (6.738B parameters), from
#      `quantize --help`: Q4_K_M is 3.80 GiB there, which is 0.606 bytes/parameter.
#   3. Measured against real files on disk: qwen3-coder:30b at Q4_K_M came out at
#      0.608 bytes/parameter.
#
# The three agree to well under a percent. The published-size figure is used here
# because it covers every format rather than only the ones that happened to be
# installed. Note it sits ~8% above the pure block layout for the "_M" mixtures, which
# keep some tensors at higher precision -- counting a "4-bit" quant at 4 bits would
# understate a 30B model by about 1.5 GB, which is the whole margin between fitting and
# swapping.
QUANTIZATION_BYTES: dict[str, float] = {
    "Q2_K": 0.426,
    "Q3_K_M": 0.488,
    "Q4_K_M": 0.606,
    "Q5_K_M": 0.709,
    "Q6_K": 0.821,
    "Q8_0": 1.068,
    "F16": 2.000,
    "BF16": 2.000,
    # Formats some Ollama tags ship instead of Q4_K_M. Q4_0 is 4.5 bits a weight.
    # MXFP4 covers only gpt-oss's expert weights, with the rest kept wider, so the
    # figure is that model's whole-file density (13.79 GB over 20.9B) -- the larger
    # of the two published sizes, since overstating memory is the safe direction.
    "Q4_0": 0.5625,
    "MXFP4": 0.66,
}

# How much quality each format costs, as the perplexity increase over full precision
# that llama.cpp publishes for a 7B model in `quantize --help`. Lower is better; these
# are used only to rank, never shown to anyone.
#
# The shape of this curve is the point. Going from Q6_K to Q5_K_M costs almost nothing,
# and from Q5_K_M to Q4_K_M very little, but Q3_K_M and especially Q2_K fall off a
# cliff. So compressing a bigger model into the same memory is usually the right trade
# until Q3, and rarely right at Q2.
QUANTIZATION_QUALITY_COST: dict[str, float] = {
    "Q2_K": 0.8698,
    "Q3_K_M": 0.2437,
    "Q4_K_M": 0.0535,
    "Q5_K_M": 0.0142,
    "Q6_K": 0.0044,
    "Q8_0": 0.0004,
    "F16": 0.0,
    "BF16": 0.0,
    # llama.cpp's published figure for the older 4-bit format, close to Q3_K_M.
    "Q4_0": 0.2166,
    # gpt-oss was trained for MXFP4 and released in it; there is no wider original
    # to have lost anything against.
    "MXFP4": 0.0,
}

# Which versions to try, in preference order, stopping at the first that fits.
#
# The naive rule -- take the highest quality that fits -- is wrong, and measurably so.
# Q8_0 costs 76% more memory than Q4_K_M and is correspondingly slower to read, in
# exchange for 0.053 perplexity. That is below what anyone notices, so on a large model
# the trade is close to pure loss: it halves the speed and can push a better model out
# of memory entirely.
#
# So the preference depends on size, for two documented reasons that point the same way.
# Small models suffer proportionally more damage from the same quantization, and their
# memory is cheap enough that precision costs nothing worth having. Large models gain
# almost nothing above Q4_K_M -- llama.cpp's own guidance marks Q4_K_M and Q5_K_M as the
# recommended pair -- and pay for it in speed.
#
# Q2_K is always last but always present: a model that runs poorly still beats no model
# at all on a small computer, which is the situation this feature most needs to handle.
_LARGE_MODEL_LADDER: tuple[str, ...] = ("Q4_K_M", "Q3_K_M", "Q2_K")
_MEDIUM_MODEL_LADDER: tuple[str, ...] = ("Q5_K_M", "Q4_K_M", "Q3_K_M", "Q2_K")
_SMALL_MODEL_LADDER: tuple[str, ...] = (
    "Q8_0",
    "Q6_K",
    "Q5_K_M",
    "Q4_K_M",
    "Q3_K_M",
    "Q2_K",
)

# What a one-click install actually downloads is Ollama's bare tag, and the bare tag is
# one fixed build -- Q4_K_M for most of the library. A model Neo installs is therefore
# judged at that build and nothing else. Judging it on the ladder above described a file
# Neo never fetches: small models were shown at Q8_0, slower than the Q4_K_M that
# arrived, and a 32B model "fitted" at Q3_K_M on a 25 GB computer while the 20 GB
# Q4_K_M download did not -- the swap this whole module exists to prevent. The ladder
# remains for models with no tag, where someone choosing a build by hand has a choice.
OLLAMA_DEFAULT_QUANTIZATION = "Q4_K_M"

# Boundaries between those ladders, in billions of parameters.
_SMALL_MODEL_B = 3.0
_MEDIUM_MODEL_B = 7.0

# The full set, for callers that need to know every version that exists.
QUANTIZATION_LADDER: tuple[str, ...] = _SMALL_MODEL_LADDER


def ladder_for(parameters_b: float) -> tuple[str, ...]:
    """Which versions of a model this size are worth trying, best choice first."""

    if parameters_b < _SMALL_MODEL_B:
        return _SMALL_MODEL_LADDER
    if parameters_b < _MEDIUM_MODEL_B:
        return _MEDIUM_MODEL_LADDER
    return _LARGE_MODEL_LADDER


# The KV cache is stored at half precision by default in llama.cpp and Ollama.
KV_BYTES_PER_ELEMENT = 2.0

# What the runtime holds beyond weights and cache: compute buffers and the graph itself.
# Measured as the residual on llama.cpp's dense reference case -- llama3.2:3b at a 32K
# context accounted for everything except 0.23 GB. Rounded up slightly, because being
# wrong in the direction of "needs a little more" is the safe one.
RUNTIME_OVERHEAD_GB = 0.25

# --------------------------------------------------------------------------------------
# Speed
# --------------------------------------------------------------------------------------

# Published peak memory bandwidth, GB/s. Decoding is bound by reading the active weights
# once per token, so this is what sets the ceiling on how fast replies arrive.
#
# One table per accelerator, and only the machine's own accelerator's table is consulted.
# A single shared table matched processor names against graphics parts: an Intel
# "Core m3" read as an Apple M3, at twice the bandwidth its memory has.
#
# Scanned in order, so each table MUST stay most-specific-first -- "m4 max" has to be
# tested before "m4", or a Max would be judged at a third of its real bandwidth. A part
# matches as a whole token, so "a10" never matches an A100 or an A1000. Where a part ships
# in variants with different bandwidth, the *lower* figure is used: under-promising speed
# is the safe direction to be wrong in.
#
# Figures are the makers' published specifications -- for graphics cards, memory bus
# width times data rate. Deliberately incomplete: a part that is not listed falls back to
# a conservative per-vendor figure rather than being guessed at, because a fallback that
# flatters the machine produces exactly the confidently-wrong recommendation this feature
# exists to prevent. Add parts here only with a published source.
_APPLE_BANDWIDTH: tuple[tuple[str, float], ...] = (
    # M5 from Apple Newsroom (Oct 2025); M1 to M4 families from their published
    # specifications. M4 Max ships at 410 and 546 GB/s; M3 Max at 300 and 409.6.
    ("m5", 153.0),
    ("m4 max", 410.0),
    ("m4 pro", 273.0),
    ("m4", 120.0),
    ("m3 ultra", 819.3),
    ("m3 max", 300.0),
    ("m3 pro", 153.6),
    ("m3", 102.4),
    ("m2 ultra", 800.0),
    ("m2 max", 400.0),
    ("m2 pro", 200.0),
    ("m2", 100.0),
    ("m1 ultra", 800.0),
    ("m1 max", 400.0),
    ("m1 pro", 200.0),
    ("m1", 68.25),
)

_NVIDIA_BANDWIDTH: tuple[tuple[str, float], ...] = (
    # GeForce RTX 50.
    ("5090", 1792.0),
    ("5080", 960.0),
    ("5070 ti", 896.0),
    ("5070", 672.0),
    ("5060 ti", 448.0),
    ("5060", 448.0),
    ("5050", 320.0),
    # GeForce RTX 40. The 4070 Ti is 192-bit at 21 Gbps; 672 GB/s is the Ti SUPER's.
    ("4090", 1008.0),
    ("4080 super", 736.0),
    ("4080", 716.8),
    ("4070 ti super", 672.0),
    ("4070 ti", 504.0),
    ("4070 super", 504.0),
    ("4070", 504.0),
    ("4060 ti", 288.0),
    ("4060", 272.0),
    # GeForce RTX 30. The 3060 and 3050 each have a narrower-bus variant.
    ("3090 ti", 1008.0),
    ("3090", 936.0),
    ("3080 ti", 912.0),
    ("3080", 760.0),
    ("3070 ti", 608.0),
    ("3070", 448.0),
    ("3060 ti", 448.0),
    ("3060", 240.0),
    ("3050", 168.0),
    # GeForce RTX 20 and GTX 16/10.
    ("2080 ti", 616.0),
    ("2080 super", 496.0),
    ("2080", 448.0),
    ("2070 super", 448.0),
    ("2070", 448.0),
    ("2060 super", 448.0),
    ("2060", 336.0),
    ("1660 super", 336.0),
    ("1660 ti", 288.0),
    ("1660", 192.0),
    ("1650 super", 192.0),
    ("1650", 128.0),
    ("1080 ti", 484.0),
    ("1080", 320.0),
    ("1070 ti", 256.0),
    ("1070", 256.0),
    ("1060", 192.0),
    ("1050 ti", 112.0),
    ("1050", 112.0),
    # Workstation and data centre. H100 and A100 at their PCIe figures, the lower ones.
    ("rtx 6000 ada", 960.0),
    ("rtx a6000", 768.0),
    ("rtx a5000", 768.0),
    ("rtx a4000", 448.0),
    ("quadro rtx 6000", 672.0),
    ("h100", 2000.0),
    ("a100", 1555.0),
    ("v100", 900.0),
    ("l40s", 864.0),
    ("l40", 864.0),
    ("a10", 600.0),
    ("l4", 300.0),
    ("t4", 320.0),
)

# Laptop parts share desktop names but not desktop memory: a "4090 Laptop GPU" is a
# 256-bit card at 576 GB/s, not the desktop 4090's 1008. Laptop names are matched here
# only, and an unlisted one falls back rather than borrowing its desktop namesake's figure.
_NVIDIA_LAPTOP_BANDWIDTH: tuple[tuple[str, float], ...] = (
    ("4090", 576.0),
    ("4080", 432.0),
    ("4070", 256.0),
    ("4060", 256.0),
    ("4050", 192.0),
    ("3080 ti", 512.0),
    ("3080", 384.0),
    ("3070 ti", 448.0),
    ("3070", 384.0),
    ("3060", 336.0),
    ("3050 ti", 192.0),
    ("3050", 168.0),
    ("1660 ti", 288.0),
    ("1650", 128.0),
    ("1050", 112.0),
)
_LAPTOP_MARKERS: tuple[str, ...] = ("laptop", "mobile", "max-q", "max-p")

_AMD_BANDWIDTH: tuple[tuple[str, float], ...] = (
    # Radeon RX 9000, 7000 and 6000 desktop cards. Mobile parts ("7600M") are a different
    # token and fall back.
    ("9070 xt", 640.0),
    ("9070", 640.0),
    ("9060 xt", 320.0),
    ("7900 xtx", 960.0),
    ("7900 xt", 800.0),
    ("7900 gre", 576.0),
    ("7800 xt", 624.0),
    ("7700 xt", 432.0),
    ("7600 xt", 288.0),
    ("7600", 288.0),
    ("6950 xt", 576.0),
    ("6900 xt", 512.0),
    ("6800 xt", 512.0),
    ("6800", 512.0),
    ("6750 xt", 432.0),
    ("6700 xt", 384.0),
    ("6650 xt", 280.0),
    ("6600 xt", 256.0),
    ("6600", 224.0),
)

_INTEL_BANDWIDTH: tuple[tuple[str, float], ...] = (
    # Arc desktop cards.
    ("b580", 456.0),
    ("b570", 380.0),
    ("a770", 560.0),
    ("a750", 512.0),
    ("a580", 512.0),
    ("a380", 186.0),
)

GPU_BANDWIDTH_GBPS: dict[str, tuple[tuple[str, float], ...]] = {
    "metal": _APPLE_BANDWIDTH,
    "cuda": _NVIDIA_BANDWIDTH,
    "rocm": _AMD_BANDWIDTH,
    "vulkan": _INTEL_BANDWIDTH + _AMD_BANDWIDTH,
    "sycl": _INTEL_BANDWIDTH,
}

# For parts not in the table. Chosen low on purpose.
#   metal  - the slowest Apple silicon listed above.
#   cuda   - below the slowest RTX 40 card; an unlisted card is as likely to be old as new.
#   rocm   - the same reasoning.
#   cpu    - dual-channel DDR4-3200 is 2 x 3200 x 8 = 51.2 GB/s theoretical, and real
#            throughput is below that.
#   vulkan - integrated graphics reading system memory over the same bus the CPU uses, so
#   sycl     bounded by DDR rather than by the GPU; above the CPU figure only because the
#            shader cores keep more requests in flight.
FALLBACK_BANDWIDTH_GBPS: dict[str, float] = {
    "metal": 68.25,
    "cuda": 200.0,
    "rocm": 200.0,
    "vulkan": 60.0,
    "sycl": 60.0,
    "cpu": 50.0,
}

# The fraction of peak bandwidth that decoding actually reaches. Measured on this
# project's reference machine (Apple M5, 153 GB/s published) using llama3.2:3b, the only
# dense text-only model available: 36.4 tokens/sec median over seven runs against
# 1.98 GB of weights is 72.1 GB/s, or 47%. A single cold run reached 55%. Settled at the
# middle, treating the cold figure as optimistic because a cold machine is not the state
# a user will be in.
#
# Only calibratable on a dense, text-only model: a sparse model reads a few of its
# experts per token and a multimodal file carries encoders that text decoding never
# touches, so measuring either would imply a bandwidth above what the hardware can do.
DECODE_EFFICIENCY = 0.50

# --------------------------------------------------------------------------------------
# Goals
# --------------------------------------------------------------------------------------

# How much conversation each goal needs to hold. Sized to the work, not to the model's
# maximum: reserving a 128K context nobody will use would reject models that would have
# been perfectly good.
CONTEXT_TARGET: dict[str, int] = {
    "chat": 8192,
    "writing": 16384,
    "coding": 32768,
    "reasoning": 32768,
    "images": 8192,
    "offline": 8192,
}

# Tokens per second at which a goal stops feeling slow. Reading pace is around 5 tokens
# per second, so all of these are comfortably above "faster than you can read"; the
# differences reflect how much text each goal generates before you can use any of it.
# Reasoning is lowest because a model that thinks before answering is expected to pause.
SPEED_TARGET: dict[str, float] = {
    "chat": 25.0,
    "writing": 20.0,
    "coding": 20.0,
    "reasoning": 12.0,
    "images": 15.0,
    "offline": 15.0,
}

# The goal only a model that can see is any use for.
VISION_GOAL = "images"

# The goal someone raises Neo's effort for. The default effort turns thinking off, so every
# other goal judges a model the way it will actually answer -- without thinking -- and
# "Difficult problems" alone judges it thinking.
THINKING_GOAL = "reasoning"

CODING_GOAL = "coding"

# A model that lists "chat" among its goals is a general-purpose assistant, and a sensible
# choice for any of these whether or not it lists them. One that does not is a specialist
# -- a coding, reasoning or vision model -- and a first choice only for what it was built
# for. Scoring specialists a little lower was not enough: a small coding model came first
# for writing on every computer where it was the fastest thing that fit.
GENERAL_PURPOSE_GOAL = "chat"
GENERAL_GOALS = frozenset({"chat", "writing", "offline", "coding", "reasoning"})

# How fast a person reads, in the units a model generates. An average adult reads
# continuous non-fiction at around 240 words a minute (Brysbaert's 2019 meta-analysis of
# 190 studies puts silent non-fiction reading at 238 wpm), which is 4 words a second.
# English runs about three quarters of a word per unit of generation -- the widely used
# approximation of ~4 characters each.
READING_WORDS_PER_SECOND = 4.0
WORDS_PER_UNIT = 0.75
READING_PACE = READING_WORDS_PER_SECOND / WORDS_PER_UNIT  # ~5.3 a second

# Below this share of reading pace, the screen tells someone to expect to wait for longer
# answers (wording.describe_speed). A model that slow earns nothing for keeping up.
SLOW_SHARE_OF_READING = 0.4

# Quality lost to compression, as a share of the model's measured quality per unit of
# perplexity increase. Measurements are of each model as its maker released it, but what
# runs here is compressed. A quarter per unit is the weight compression always had in this
# ranking: next to nothing at the usual 4-bit build, a real loss at 2-bit.
QUALITY_LOSS_PER_PERPLEXITY = 0.25

# Boundaries on required/usable memory.
#
# Above 1.0 the model plus its cache does not fit, which is physical. The softer
# boundaries are about what is left over: at 0.70 there is room for the conversation to
# grow and for other applications to keep running; by 0.90 there is not much of either.
# No spill was reproduced on the reference machine, so these are set from headroom
# rather than from an observed cliff -- re-check them with the calibration script on a
# machine that does spill.
TIER_BOUNDARIES: tuple[tuple[FitTier, float], ...] = (
    ("comfortable", 0.70),
    ("good", 0.90),
    ("tight", 1.00),
)

# What fitting costs a model in the ranking. "comfortable" and "good" both mean it fits
# comfortably -- that is what each says on screen -- so neither costs anything. Only
# "tight" does, falling to half value at the very edge: a model that only just fits is one
# long conversation or one more open app away from slowing down, so it should be chosen
# over a model with room only when it is clearly better, not marginally. Charging for
# headroom below that point is what put an 8B model ahead of a 27B one on a 24 GB card.
FIT_COSTS_FROM = dict(TIER_BOUNDARIES)["good"]
EDGE_OF_FIT_VALUE = 0.5

# Except where the goal itself asks for room. Working with no internet is, in its own
# words, small and self-contained -- a laptop away from a desk -- so it is charged from
# the end of "comfortable", the tier that promises room to keep using other apps.
FIT_COSTS_FROM_BY_GOAL: dict[str, float] = {"offline": dict(TIER_BOUNDARIES)["comfortable"]}

# Shown beside a model whose quality is an estimate rather than a measurement.
ESTIMATED_QUALITY_CAVEAT = (
    "This model's quality has not been measured independently, so its place in this list "
    "is an estimate."
)


def bytes_per_parameter(quantization: str) -> float:
    """Memory one weight costs under ``quantization``."""

    return QUANTIZATION_BYTES.get(quantization, QUANTIZATION_BYTES["Q4_K_M"])


def shipped_quantization(model: dict[str, Any]) -> str | None:
    """The build a one-click install downloads, or ``None`` when Neo cannot install it."""

    if not model.get("ollama"):
        return None
    return str(model.get("ollama_quantization") or OLLAMA_DEFAULT_QUANTIZATION)


def weights_gb(model: dict[str, Any], quantization: str) -> float:
    """Size of this model's weights in ``quantization``.

    The downloaded build's real size when the catalog records it, since that is the
    file that gets loaded: a vision encoder or embeddings kept at higher precision
    make it larger than parameters x format, by a third on a 1B model.
    """

    recorded = model.get("ollama_weights_gb")
    if recorded and quantization == shipped_quantization(model):
        return float(recorded)
    parameters = float(model.get("parameters_b") or 0.0)
    return parameters * bytes_per_parameter(quantization)


def quality_cost(quantization: str) -> float:
    return QUANTIZATION_QUALITY_COST.get(quantization, 0.1)


def active_parameters_b(model: dict[str, Any]) -> float:
    """Parameters actually touched per token, in billions.

    A sparse model routes each token through a few of its experts, so it reads far less
    than its total size. Using the total would both understate its speed and, through
    the cache, overstate what it costs to run.
    """

    if model.get("sparse") and model.get("active_parameters_b"):
        return float(model["active_parameters_b"])
    return float(model.get("parameters_b") or 0.0)


def kv_bytes_per_token(model: dict[str, Any]) -> float:
    """Bytes of conversation cache one token costs.

    Each layer whose cache grows with the conversation keeps one key and one value
    vector per key/value head for every token:

        layers x kv_heads x (key_length + value_length) x bytes per element

    ``layers`` counts only those layers, and ``kv_heads`` and the widths are theirs.
    Many current models interleave layers whose cache does not grow: linear-attention,
    Mamba and convolution layers hold a fixed-size state, a sliding-window layer holds
    at most its window, and a layer that reuses an earlier layer's cache holds none of
    its own. Counting those at full context overstated Gemma 4 31B's cache at 8K by
    about 8 GB, enough to reject it on a 24 GB card that runs it. The catalog says how
    each row was counted.

    What this leaves out is fixed rather than growing: at most about 0.8 GB, for Gemma
    4 31B's sliding-window layers, and under 0.35 GB for every other catalog model.

    Key and value widths are read separately rather than derived as embedding/heads.
    Gemma-class models declare a key length of 512 against an embedding of 2560 over 8
    heads, where deriving it would be wrong by more than half.
    """

    layers = int(model.get("layers") or 0)
    kv_heads = int(model.get("kv_heads") or 0)
    key_length = int(model.get("key_length") or 0)
    value_length = int(model.get("value_length") or key_length)
    if not layers or not kv_heads or not key_length:
        return 0.0
    return layers * kv_heads * (key_length + value_length) * KV_BYTES_PER_ELEMENT


def required_memory_gb(model: dict[str, Any], quantization: str, context: int) -> float:
    """Total memory to load this model and hold a conversation of ``context`` tokens."""

    weights = weights_gb(model, quantization) * 1e9
    cache = kv_bytes_per_token(model) * max(0, context)
    return (weights + cache) / 1e9 + RUNTIME_OVERHEAD_GB


def _names_part(name: str, part: str) -> bool:
    """Whether ``part`` appears in ``name`` as a whole token, not inside a longer one."""

    return re.search(rf"(?<![a-z0-9]){re.escape(part)}(?![a-z0-9])", name) is not None


def bandwidth_gbps(machine: Machine) -> float:
    """This machine's memory bandwidth, from the table or a conservative fallback."""

    fallback = FALLBACK_BANDWIDTH_GBPS.get(machine.accelerator, FALLBACK_BANDWIDTH_GBPS["cpu"])
    table = GPU_BANDWIDTH_GBPS.get(machine.accelerator)
    if not table:
        # The processor itself. Its name says nothing about its memory, and matching it
        # against graphics parts is how a Core m3 became an Apple M3.
        return fallback
    # On Apple silicon the chip is the graphics, so its name is the processor's too.
    chip = machine.cpu_name if machine.accelerator == "metal" else ""
    name = (machine.gpu_name or chip).lower()
    if machine.accelerator == "cuda" and any(marker in name for marker in _LAPTOP_MARKERS):
        table = _NVIDIA_LAPTOP_BANDWIDTH
    for part, value in table:
        if _names_part(name, part):
            return value
    return fallback


def estimate_tokens_per_sec(
    model: dict[str, Any], quantization: str, machine: Machine
) -> float | None:
    """Roughly how fast this will generate.

    Decoding streams the active weights once per token, so speed is bandwidth divided by
    bytes read, scaled by how much of peak bandwidth a real decode reaches.
    """

    active = active_parameters_b(model)
    parameters = float(model.get("parameters_b") or 0.0)
    if active <= 0 or parameters <= 0:
        return None
    # The active share of the weights as they are actually stored -- the same file size
    # DECODE_EFFICIENCY was calibrated against. A vision encoder in that file is counted
    # though text decoding never reads it, which under-promises speed for those models:
    # the safe direction.
    bytes_read = weights_gb(model, quantization) * 1e9 * min(1.0, active / parameters)
    if bytes_read <= 0:
        return None
    effective = bandwidth_gbps(machine) * DECODE_EFFICIENCY * 1e9
    return effective / bytes_read


def _tier_for(ratio: float) -> FitTier:
    for tier, boundary in TIER_BOUNDARIES:
        if ratio <= boundary:
            return tier
    return "too_big"


def _reason_for(tier: FitTier, model: dict[str, Any], machine: Machine) -> str:
    """The verdict as a sentence. Never names a format or a unit of measurement."""

    name = model.get("display_name") or model.get("id") or "This model"
    if tier == "comfortable":
        return (
            f"{name} fits with room to spare, so it will stay responsive while you use other apps."
        )
    if tier == "good":
        return f"{name} fits comfortably on this computer."
    if tier == "tight":
        return (
            f"{name} fits, but only just. It will run; long conversations may slow "
            "down, and it helps to close other apps first."
        )
    if machine.usable_memory_gb <= 0:
        return (
            f"Neo could not work out how much memory is free, so it cannot promise "
            f"{name} will run here."
        )
    return f"{name} needs more memory than this computer has free."


def evaluate(
    model: dict[str, Any],
    machine: Machine,
    *,
    goal: str = "chat",
    context: int | None = None,
) -> Fit:
    """Pick the best-quality version of this model that fits, and say how it will feel."""

    target_context = context or CONTEXT_TARGET.get(goal, 8192)
    # Never promise more conversation than the model itself supports.
    model_context = int(model.get("context_length") or target_context)
    target_context = min(target_context, model_context)

    budget = machine.usable_memory_gb
    caveat = model.get("data_caveat")

    shipped = shipped_quantization(model)
    ladder = (shipped,) if shipped else ladder_for(float(model.get("parameters_b") or 0.0))
    # Nothing fitting means the shortfall is reported against the smallest version there
    # is, which is honest: quoting the memory an uncompressed copy would need would
    # overstate how far out of reach the model actually is.
    chosen = ladder[-1]
    required = required_memory_gb(model, chosen, target_context)
    if budget > 0:
        for quantization in ladder:
            candidate = required_memory_gb(model, quantization, target_context)
            if candidate <= budget:
                chosen, required = quantization, candidate
                break

    # A zero or unknown budget must not divide. It is reported as not fitting, with a
    # reason that says the scan failed rather than blaming the model.
    ratio = required / budget if budget > 0 else float("inf")
    tier = _tier_for(ratio)
    speed = estimate_tokens_per_sec(model, chosen, machine) if tier != "too_big" else None

    return Fit(
        tier=tier,
        reason=_reason_for(tier, model, machine),
        quantization=chosen,
        context=target_context,
        required_gb=required,
        tokens_per_sec=speed,
        headroom_ratio=min(ratio, 99.0),
        data_caveat=caveat,
    )


def measured_quality(model: dict[str, Any], goal: str) -> float | None:
    """How capable this model is at ``goal``, as independently measured, or ``None``.

    Quality is measured rather than inferred from size, because size predicts it badly
    across generations: Mixtral 8x7B is twice the size of Gemma 3 27B and measured a tenth
    as capable, and a size-based score recommended it first on the largest computers.
    The catalog's ``quality_source`` says where every figure comes from.
    """

    figures = model.get("quality") or {}
    if goal == VISION_GOAL:
        keys: tuple[str, ...] = ("vision",)
    elif goal == CODING_GOAL:
        keys = ("coding",)
    elif goal == THINKING_GOAL:
        # A model that cannot switch thinking off has one figure, measured thinking.
        keys = ("thinking", "general")
    else:
        keys = ("general",)
    for key in keys:
        value = figures.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return None


def _stand_in_quality(model: dict[str, Any], goal: str, models: Sequence[dict[str, Any]]) -> float:
    """Quality for a model nobody measured: no better than the weakest measured model its size.

    Deliberately pessimistic. Every older model in the catalog measured far below what its
    size suggests, so a size-based guess would put an unmeasured old model above newer
    measured ones -- the exact failure measured quality exists to fix.
    """

    size = float(model.get("parameters_b") or 0.0)
    peers: list[float] = []
    everyone: list[float] = []
    for other in models:
        value = measured_quality(other, goal)
        if value is None or (other.get("quality") or {}).get("estimated"):
            continue
        everyone.append(value)
        other_size = float(other.get("parameters_b") or 0.0)
        if size > 0 and size / 2 <= other_size <= size * 2:
            peers.append(value)
    pool = peers or everyone
    return min(pool) if pool else 0.0


def suits(model: dict[str, Any], goal: str) -> bool:
    """Whether this model is a sensible first choice for ``goal`` at all."""

    goals = model.get("goals") or []
    if goal == VISION_GOAL:
        return bool(model.get("vision"))
    return goal in goals or (GENERAL_PURPOSE_GOAL in goals and goal in GENERAL_GOALS)


def _responsiveness(tokens_per_sec: float | None, goal: str) -> float:
    """How well replies keep up, from 0 to 1.

    Zero at the pace where the screen tells someone to expect to wait, full at the
    pace where the goal stops feeling slow, and logarithmic in between because speed is
    felt in proportion: going from 3 to 6 a second matters as much as from 10 to 20. A
    straight line made a model at twice reading speed worth half of one at five times
    it, which is how a 1B model came first for chat on computers that run a 4B model
    faster than anyone reads.
    """

    if not tokens_per_sec:
        return 0.0
    floor = READING_PACE * SLOW_SHARE_OF_READING
    target = SPEED_TARGET.get(goal, 20.0)
    if tokens_per_sec <= floor:
        return 0.0
    if tokens_per_sec >= target:
        return 1.0
    return math.log(tokens_per_sec / floor) / math.log(target / floor)


def _fit_value(ratio: float, goal: str) -> float:
    """What fitting costs, from 1 down to half at the very edge. Continuous, never a cliff."""

    start = FIT_COSTS_FROM_BY_GOAL.get(goal, FIT_COSTS_FROM)
    if ratio <= start:
        return 1.0
    if ratio >= 1.0:
        return EDGE_OF_FIT_VALUE
    share = (ratio - start) / (1.0 - start)
    return 1.0 - share * (1.0 - EDGE_OF_FIT_VALUE)


def _compression_retained(quantization: str) -> float:
    """The share of measured quality that survives compression to ``quantization``."""

    return max(0.0, 1.0 - quality_cost(quantization) * QUALITY_LOSS_PER_PERPLEXITY)


def rank(
    models: list[dict[str, Any]],
    machine: Machine,
    *,
    goal: str = "chat",
    goals: Sequence[str] | None = None,
    limit: int = 25,
    include_unfit: bool = True,
) -> list[Recommendation]:
    """Score every model for this machine and goal, best first.

    A model's score is its measured quality for the goal -- as a share of the best in
    the catalog -- discounted for replies that cannot keep up, for a fit that leaves no
    room, and for a conversation it cannot hold. Multiplied rather than added, so that no
    amount of quality buys back a model too slow to use, and no amount of speed makes a
    weak model the best choice.

    Models built for something else come after every model suited to the goal, whatever
    they score: a coding model is not the answer to "help me write", however fast it is.

    Models that do not fit are kept, scored zero and sorted last, with the reason
    attached. Dropping them silently leaves someone wondering where a model they had
    heard of went, and "it needs more memory than you have" is a useful answer.

    Several ``goals`` describe one model used for all of them. It is sized for the
    longest conversation any of them needs -- a model that fits for chat but not
    for the code it was also chosen for has not fitted -- and its score is the
    average of what each goal makes of that one fit, so a model good at all of
    them beats one that is excellent at one and poor at the rest.
    """

    chosen = list(dict.fromkeys(goals)) if goals else [goal]
    context = max(CONTEXT_TARGET.get(each, 8192) for each in chosen)
    # A model that cannot read images is not a weaker choice for pictures, it is not a
    # choice. Scoring it lower was not enough: a strong text-only model outscored every
    # vision model on capability and came first for "Pictures and screenshots".
    needs_vision = VISION_GOAL in chosen

    # Every model's quality for every chosen goal, measured or stood in for, and the best
    # in the catalog. Judged against the whole catalog rather than what fits, so that a
    # model's score means the same thing on every computer.
    quality: dict[int, dict[str, float]] = {}
    estimated: set[int] = set()
    for index, model in enumerate(models):
        quality[index] = {}
        for each in chosen:
            value = measured_quality(model, each)
            if value is None:
                value = _stand_in_quality(model, each, models)
                estimated.add(index)
            quality[index][each] = value
        if (model.get("quality") or {}).get("estimated"):
            estimated.add(index)
    best = {
        each: max((quality[index][each] for index in quality), default=0.0) or 1.0
        for each in chosen
    }

    fitting: list[tuple[float, Recommendation]] = []
    unfit: list[Recommendation] = []
    for index, model in enumerate(models):
        if needs_vision and not model.get("vision"):
            continue
        fit = evaluate(model, machine, goal=chosen[0], context=context)
        if index in estimated:
            caveats = [text for text in (fit.data_caveat, ESTIMATED_QUALITY_CAVEAT) if text]
            fit = replace(fit, data_caveat=" ".join(caveats))
        if fit.tier == "too_big":
            if include_unfit:
                unfit.append(Recommendation(model=model, fit=fit, score=0.0))
            continue

        retained = _compression_retained(fit.quantization)
        capabilities, speeds, fits, reaches, scores = [], [], [], [], []
        for each in chosen:
            capability = 100.0 * quality[index][each] * retained / best[each]
            speed = _responsiveness(fit.tokens_per_sec, each)
            fit_value = _fit_value(fit.headroom_ratio, each)
            reach = min(1.0, fit.context / CONTEXT_TARGET.get(each, 8192))
            capabilities.append(capability)
            speeds.append(speed)
            fits.append(fit_value)
            reaches.append(reach)
            scores.append(capability * speed * fit_value * reach)

        count = len(chosen)
        parts = ScoreParts(
            capability=sum(capabilities) / count,
            responsiveness=100.0 * sum(speeds) / count,
            headroom=100.0 * sum(fits) / count,
            context_reach=100.0 * sum(reaches) / count,
        )
        suited = sum(1 for each in chosen if suits(model, each)) / count
        recommendation = Recommendation(
            model=model, fit=fit, score=sum(scores) / count, parts=parts
        )
        fitting.append((suited, recommendation))

    # Suited models first, then by score. Among models too slow to score at all, the more
    # capable one first, so the order still means something on the slowest computers.
    fitting.sort(
        key=lambda pair: (
            -pair[0],
            -pair[1].score,
            -pair[1].parts.capability,
            pair[1].fit.headroom_ratio,
        )
    )
    # Unfit models all score zero, so they are ordered among themselves by how close each
    # comes to fitting, which is the most useful thing to tell someone who is wondering
    # what they would need.
    unfit.sort(key=lambda item: item.fit.headroom_ratio)
    return ([item for _, item in fitting] + unfit)[:limit]
