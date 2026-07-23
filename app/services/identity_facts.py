from __future__ import annotations

import re

# These are transient states or conversational fillers, not occupations or identity facts.
# Keep this check deliberately conservative: an uncertain profile value is worse than no
# profile value because direct memory answers present it as a fact about the user.
_TRANSIENT_VALUES = frozenset(
    {
        "angry",
        "anxious",
        "bored",
        "busy",
        "confused",
        "depressed",
        "excited",
        "fine",
        "happy",
        "hungry",
        "okay",
        "ok",
        "sad",
        "sick",
        "sleepy",
        "stressed",
        "tired",
        "upset",
        "well",
    }
)
_NAME_DISALLOWED = _TRANSIENT_VALUES | {
    "a",
    "an",
    "developer",
    "engineer",
    "student",
    "teacher",
    "working",
    "learning",
    "studying",
}
_PROFILE_KEYS = frozenset(
    {"name", "age", "location", "country", "nationality", "occupation", "education", "general"}
)


def normalize_identity_value(key: str, value: str) -> str:
    """Return a display-safe canonical value for a validated identity fact."""

    cleaned = re.sub(r"\s+", " ", value).strip(" ,;.")
    if key.lower() == "name" and cleaned.islower():
        # User input commonly arrives lower-cased (for example ``iam soham``).
        # Title casing is only presentation normalization; it does not infer a name.
        # Preserve a user-supplied mixed-case name such as ``O'Neill`` unchanged.
        return " ".join(part.capitalize() for part in cleaned.split(" "))
    return cleaned


def is_durable_identity_fact(key: str, value: str) -> bool:
    """Whether an identity candidate is safe to persist and answer as user profile data."""

    normalized_key = key.strip().lower()
    cleaned = normalize_identity_value(normalized_key, value)
    lowered = cleaned.lower()
    if normalized_key not in _PROFILE_KEYS or not cleaned:
        return False

    if normalized_key == "name":
        return bool(
            re.fullmatch(r"[A-Za-z][A-Za-z' -]{1,80}", cleaned)
            and lowered not in _NAME_DISALLOWED
            and not any(word in _NAME_DISALLOWED for word in lowered.split())
        )

    if normalized_key == "occupation":
        return bool(
            len(cleaned) >= 2
            and len(cleaned) <= 120
            and lowered not in _TRANSIENT_VALUES
            and not re.match(
                r"(?:feeling|being|currently|just|very|really|so)\b", lowered
            )
        )

    if normalized_key == "age":
        return bool(cleaned.isdigit() and 0 < int(cleaned) < 130)

    return len(cleaned) >= 2
