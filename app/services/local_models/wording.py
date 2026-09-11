"""Turning the numbers into sentences a non-technical person can act on.

This module is the feature, not decoration around it. A recommendation that reads
"18.9 GB, Q4_K_M, ~38 tok/s" is unusable by the people this was built for, and every one
of those figures has a plain equivalent that carries the same decision without the
vocabulary.

The prose is generated here rather than in the browser so that there is one source of
truth: the API, the wizard and any future command line all say the same thing about the
same model.

Nothing in this module may emit the words this feature exists to hide -- compression
formats, graphics memory, cache internals, or a rate measured in units nobody outside
the field uses. A test walks every string the API returns and fails on any of them.
"""

from __future__ import annotations

from typing import Any

from app.services.local_models.sizing import bytes_per_parameter
from app.services.local_models.types import Fit, Machine

# An average adult reads continuous non-fiction prose at around 240 words per minute
# (Brysbaert's 2019 meta-analysis of 190 studies puts silent non-fiction reading at
# 238 wpm), which is 4 words a second.
READING_WORDS_PER_SECOND = 4.0

# English text runs about three quarters of a word per unit of generation -- the widely
# used approximation of ~4 characters each. Used only to convert a generation rate into
# a reading rate, so the exact figure matters less than the comparison it supports.
WORDS_PER_UNIT = 0.75

# Assumed download speed, in megabytes per second. Ookla's global median for fixed
# broadband has been near 100 Mbit/s; 50 Mbit/s is roughly 6 MB/s and is deliberately
# pessimistic, because a download that finishes sooner than promised is a good surprise
# and the reverse is not.
DOWNLOAD_MB_PER_SECOND = 6.0


def describe_speed(tokens_per_sec: float | None) -> str:
    """How fast replies arrive, in terms of reading rather than a generation rate."""

    if not tokens_per_sec:
        return "Neo could not work out how fast this would be on your computer."

    reading_pace = READING_WORDS_PER_SECOND / WORDS_PER_UNIT  # ~5.3 units per second
    ratio = tokens_per_sec / reading_pace

    if ratio >= 4.0:
        return "Replies appear much faster than you can read them."
    if ratio >= 1.5:
        return "Replies appear faster than you can read them."
    if ratio >= 0.9:
        return "Replies arrive at about reading speed."
    if ratio >= 0.4:
        return "Replies come in slower than reading speed, but steadily."
    return "Replies come in slowly. Expect to wait for longer answers."


def describe_speed_short(tokens_per_sec: float | None) -> str:
    """The speed verdict as a fragment, for a list row rather than a sentence."""

    if not tokens_per_sec:
        return "Speed unknown"

    reading_pace = READING_WORDS_PER_SECOND / WORDS_PER_UNIT
    ratio = tokens_per_sec / reading_pace
    if ratio >= 4.0:
        return "Very fast"
    if ratio >= 1.5:
        return "Faster than reading"
    if ratio >= 0.9:
        return "Reading speed"
    if ratio >= 0.4:
        return "A little slow"
    return "Slow"


def download_gb(model: dict[str, Any], fit: Fit) -> float:
    """How much has to come down the wire: the compressed weights, not the full model."""

    parameters = float(model.get("parameters_b") or 0.0)
    return parameters * 1e9 * bytes_per_parameter(fit.quantization) / 1e9


def _download_wait(size_gb: float) -> str:
    minutes = size_gb * 1000 / DOWNLOAD_MB_PER_SECOND / 60
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        return f"about {round(minutes)} minutes"
    hours = minutes / 60
    return f"about {hours:.0f} hours" if hours >= 2 else "about an hour and a half"


def describe_download(model: dict[str, Any], fit: Fit) -> str:
    """Download size as a size *and* a wait, because the wait is the real cost."""

    size_gb = download_gb(model, fit)
    if size_gb <= 0:
        return "Neo could not work out how large this download would be."
    if size_gb < 1:
        return f"{round(size_gb * 1000)} MB download, {_download_wait(size_gb)}."
    return f"{size_gb:.0f} GB download, {_download_wait(size_gb)} on a typical connection."


def describe_download_short(model: dict[str, Any], fit: Fit) -> str:
    size_gb = download_gb(model, fit)
    if size_gb <= 0:
        return "Size unknown"
    if size_gb < 1:
        return f"{round(size_gb * 1000)} MB download"
    return f"{size_gb:.0f} GB download"


def describe_fit(fit: Fit) -> str:
    """The one-line verdict. ``Fit.reason`` is already written as a sentence."""

    return fit.reason


def describe_compression(fit: Fit) -> str:
    """What was traded away to make it fit, without naming the format.

    The thresholds follow the published quality cost of each format: the top of the
    ladder is indistinguishable from the original, the middle is a fair trade, and the
    bottom is a real loss that someone deserves to be warned about.
    """

    quantization = fit.quantization
    if quantization in ("F16", "BF16", "Q8_0"):
        return "Full quality, as the makers released it."
    if quantization in ("Q6_K", "Q5_K_M"):
        return "Made slightly smaller to fit, with no noticeable difference in quality."
    if quantization == "Q4_K_M":
        return "Made smaller to fit comfortably. The difference in quality is hard to notice."
    if quantization == "Q3_K_M":
        return "Made considerably smaller to fit. Answers may be a little less reliable."
    return "Compressed as far as it goes to fit this computer. Expect some quality loss."


def describe_machine(machine: Machine) -> str:
    """The scan result as a verdict first and specifications second.

    Someone opening this screen wants to know whether their computer is up to it. The
    numbers are there for the person who cares, after the answer.
    """

    if machine.usable_memory_gb <= 0:
        return (
            "Neo could not work out what this computer has available, so it cannot say "
            "which models will run well here."
        )

    memory = round(machine.total_memory_gb)
    usable = round(machine.usable_memory_gb)

    if machine.unified_memory and machine.gpu_name:
        chip = machine.gpu_name
        # On a shared-memory design the budget is always a fraction of the total, so the
        # two being equal means the total was never established and the budget stood in
        # for it. Naming it as the machine's memory would then understate the computer.
        if memory > usable:
            opening = f"You have {_article(chip)} {chip} with {memory} GB of memory"
        else:
            opening = f"You have {_article(chip)} {chip}"
    elif machine.has_gpu:
        opening = f"You have {_article(machine.gpu_name)} {machine.gpu_name}"
    else:
        opening = f"You have {memory} GB of memory and no graphics card Neo can use"

    verdict = _capacity_verdict(usable)
    sentence = f"{opening}, and about {usable} GB of that can be used for AI. {verdict}"

    if not machine.has_gpu:
        sentence += " Without a graphics card, replies will be slower than you may be used to."
    return sentence


def _capacity_verdict(usable_gb: float) -> str:
    """What that budget means, in terms of what it can actually do."""

    if usable_gb >= 40:
        return "That is a lot -- you can run the largest models people run at home."
    if usable_gb >= 20:
        return "That is a strong setup: you can run large, capable models."
    if usable_gb >= 10:
        return "That is comfortable for most mid-sized models."
    if usable_gb >= 5:
        return "That is enough for small and mid-sized models."
    return "That is enough for compact models -- quick, if less knowledgeable."


# Letters whose *names* open on a vowel sound. An initialism is read out letter by
# letter, so "NVIDIA" takes "an" while "Nvidia" would take "a" -- and graphics cards are
# named in initialisms almost without exception.
_VOWEL_SOUND_LETTERS = frozenset("AEFHILMNORSX")


def _article(phrase: str) -> str:
    """ "a" or "an", by how the name is said rather than how it is spelled."""

    word = phrase.strip().split(" ")[0] if phrase.strip() else ""
    if len(word) >= 2 and word[:2].isupper() and word[:2].isalpha():
        return "an" if word[0] in _VOWEL_SOUND_LETTERS else "a"
    return "an" if word[:1].lower() in "aeiou" else "a"


def plain(model: dict[str, Any], fit: Fit) -> dict[str, str]:
    """Everything the interface needs to describe one recommendation, in prose."""

    return {
        "fit": describe_fit(fit),
        "speed": describe_speed(fit.tokens_per_sec),
        "download": describe_download(model, fit),
        "compression": describe_compression(fit),
        "short": f"{describe_speed_short(fit.tokens_per_sec)} · "
        f"{describe_download_short(model, fit)}",
    }
