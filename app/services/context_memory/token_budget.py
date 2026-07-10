from __future__ import annotations


def estimate_tokens(value: object) -> int:
    """A stable rough estimate; this is deliberately not a model tokenizer."""
    text = str(value or "").strip()
    return 0 if not text else max(1, (len(text) + 3) // 4)


def compression(before: int, after: int) -> float:
    return round((before / after) if after else 0.0, 2)
