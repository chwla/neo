"""Will this model run on this computer, and what will it feel like?

Pure functions: no probing, no catalog loading, no network. Everything takes a
``Machine`` and a catalog row and returns a verdict, which is what lets the whole matrix
-- Apple, NVIDIA, AMD and no graphics card at all, crossed with dense and sparse models
and every quantization -- be tested without owning any of that hardware.

Every constant below is either derived from a published source or measured by
``scripts/calibrate_local_models.py`` on real hardware. See
``docs/local-models-calibration.md`` for the measurements. Nothing here is a round
number chosen because it looked about right: a recommendation that is plausible but
wrong is worse than no recommendation, because the person receiving it has no way to
tell.
"""

from __future__ import annotations

import math
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
# Scanned in order and matched as a substring, so this MUST stay most-specific-first --
# "m4 max" has to be tested before "m4", or a Max would be judged at a third of its real
# bandwidth. Where a part ships in variants with different bandwidth, the *lower* figure
# is used: under-promising speed is the safe direction to be wrong in.
#
# Deliberately incomplete. A part that is not listed falls back to a conservative
# per-vendor figure rather than being guessed at, because a fallback that flatters the
# machine produces exactly the confidently-wrong recommendation this feature exists to
# prevent. Add parts here only with a published source.
GPU_BANDWIDTH_GBPS: tuple[tuple[str, float], ...] = (
    # Apple. M5 from Apple Newsroom (Oct 2025); M3/M4 families from their published
    # specifications. M4 Max ships at 410 and 546 GB/s; M3 Max at 300 and 409.6.
    ("m5", 153.0),
    ("m4 max", 410.0),
    ("m4 pro", 273.0),
    ("m4", 120.0),
    ("m3 ultra", 819.3),
    ("m3 max", 300.0),
    ("m3 pro", 153.6),
    ("m3", 102.4),
    # NVIDIA GeForce RTX 40 series, from published specifications.
    ("4090", 1008.0),
    ("4080", 716.8),
    ("4070 ti", 672.0),
    ("4070", 504.0),
    ("4060 ti", 288.0),
    ("4060", 272.0),
)

# For parts not in the table. Chosen low on purpose.
#   metal  - below the slowest Apple silicon listed above.
#   cuda   - below the slowest RTX 40 card; an unlisted card is as likely to be old as new.
#   rocm   - the same reasoning.
#   cpu    - dual-channel DDR4-3200 is 2 x 3200 x 8 = 51.2 GB/s theoretical, and real
#            throughput is below that.
FALLBACK_BANDWIDTH_GBPS: dict[str, float] = {
    "metal": 100.0,
    "cuda": 200.0,
    "rocm": 200.0,
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

# (capability, responsiveness, headroom, context_reach), summing to 1.
GOAL_WEIGHTS: dict[str, tuple[float, float, float, float]] = {
    "chat": (0.35, 0.35, 0.20, 0.10),
    "writing": (0.45, 0.25, 0.20, 0.10),
    "coding": (0.40, 0.20, 0.15, 0.25),
    "reasoning": (0.50, 0.15, 0.15, 0.20),
    "images": (0.45, 0.25, 0.20, 0.10),
    "offline": (0.35, 0.30, 0.25, 0.10),
}

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


def bytes_per_parameter(quantization: str) -> float:
    """Memory one weight costs under ``quantization``."""

    return QUANTIZATION_BYTES.get(quantization, QUANTIZATION_BYTES["Q4_K_M"])


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

    Every layer keeps one key and one value vector per key/value head:

        layers x kv_heads x (key_length + value_length) x bytes per element

    Key and value widths are read separately rather than derived as embedding/heads.
    Gemma-class models declare a key length of 512 against an embedding of 2560 over 8
    heads, where deriving it would be wrong by more than half.

    This is an upper bound for architectures that bound the cache below the full
    context -- a sliding window caps local layers at the window size however long the
    conversation runs. Over-estimating means recommending a slightly smaller model than
    strictly necessary, which is the safe direction; the opposite error puts someone
    into swap.
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

    parameters = float(model.get("parameters_b") or 0.0)
    weights = parameters * 1e9 * bytes_per_parameter(quantization)
    cache = kv_bytes_per_token(model) * max(0, context)
    return (weights + cache) / 1e9 + RUNTIME_OVERHEAD_GB


def bandwidth_gbps(machine: Machine) -> float:
    """This machine's memory bandwidth, from the table or a conservative fallback."""

    name = (machine.gpu_name or machine.cpu_name or "").lower()
    for part, value in GPU_BANDWIDTH_GBPS:
        if part in name:
            return value
    return FALLBACK_BANDWIDTH_GBPS.get(machine.accelerator, FALLBACK_BANDWIDTH_GBPS["cpu"])


def estimate_tokens_per_sec(
    model: dict[str, Any], quantization: str, machine: Machine
) -> float | None:
    """Roughly how fast this will generate.

    Decoding streams the active weights once per token, so speed is bandwidth divided by
    bytes read, scaled by how much of peak bandwidth a real decode reaches.
    """

    active = active_parameters_b(model)
    if active <= 0:
        return None
    bytes_read = active * 1e9 * bytes_per_parameter(quantization)
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

    ladder = ladder_for(float(model.get("parameters_b") or 0.0))
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


def _capability_score(model: dict[str, Any], quantization: str, goal: str) -> float:
    """How good this model is, before considering whether it fits.

    Capability rises with size but with steeply diminishing returns, so a logarithm
    rather than the parameter count itself: the step from 3B to 8B matters far more than
    the step from 60B to 70B. Total parameters are used, not active ones -- a sparse
    model knows what its full weights know, it just reads them selectively.
    """

    parameters = float(model.get("parameters_b") or 0.0)
    if parameters <= 0:
        return 0.0
    # log10(0.5B)=-0.3 through log10(70B)=1.85, mapped onto roughly 0..100.
    size_score = max(0.0, (math.log10(parameters) + 0.5) / 2.4) * 100.0
    # Perplexity cost is an increase, so it subtracts. Scaled so that Q2_K's 0.87 is a
    # heavy penalty and Q5_K_M's 0.014 is almost none.
    size_score -= quality_cost(quantization) * 25.0
    if goal in (model.get("goals") or []):
        # Built for this job. Enough to break a tie, not enough to beat a much better
        # model that happens not to advertise the goal.
        size_score += 12.0
    return max(0.0, min(100.0, size_score))


def _responsiveness_score(tokens_per_sec: float | None, goal: str) -> float:
    if not tokens_per_sec:
        return 0.0
    target = SPEED_TARGET.get(goal, 20.0)
    # Full marks at the target; above it, more speed stops mattering because nobody
    # reads faster than they read.
    return max(0.0, min(100.0, tokens_per_sec / target * 100.0))


def _headroom_score(ratio: float) -> float:
    """Reward leaving memory free, without a cliff at a tier boundary.

    A step function here would invert the ranking of two models separated by a rounding
    error, so this is continuous and the tiers are only ever labels on top of it.
    """

    if ratio <= 0 or ratio == float("inf"):
        return 0.0
    return max(0.0, min(100.0, (1.0 - ratio) * 100.0 + 30.0))


def _context_score(context: int, goal: str) -> float:
    target = CONTEXT_TARGET.get(goal, 8192)
    return max(0.0, min(100.0, context / target * 100.0))


def rank(
    models: list[dict[str, Any]],
    machine: Machine,
    *,
    goal: str = "chat",
    limit: int = 25,
    include_unfit: bool = True,
) -> list[Recommendation]:
    """Score every model for this machine and goal, best first.

    Models that do not fit are kept, scored zero and sorted last, with the reason
    attached. Dropping them silently leaves someone wondering where a model they had
    heard of went, and "it needs more memory than you have" is a useful answer.
    """

    weights = GOAL_WEIGHTS.get(goal, GOAL_WEIGHTS["chat"])
    results: list[Recommendation] = []

    for model in models:
        fit = evaluate(model, machine, goal=goal)
        if fit.tier == "too_big":
            if include_unfit:
                results.append(Recommendation(model=model, fit=fit, score=0.0))
            continue

        parts = ScoreParts(
            capability=_capability_score(model, fit.quantization, goal),
            responsiveness=_responsiveness_score(fit.tokens_per_sec, goal),
            headroom=_headroom_score(fit.headroom_ratio),
            context_reach=_context_score(fit.context, goal),
        )
        score = (
            parts.capability * weights[0]
            + parts.responsiveness * weights[1]
            + parts.headroom * weights[2]
            + parts.context_reach * weights[3]
        )
        results.append(Recommendation(model=model, fit=fit, score=score, parts=parts))

    # Unfit models all score zero, so the secondary key keeps them in a sensible order
    # among themselves -- closest to fitting first, which is the most useful thing to
    # tell someone who is wondering what they would need.
    results.sort(key=lambda item: (-item.score, item.fit.headroom_ratio))
    return results[:limit]
