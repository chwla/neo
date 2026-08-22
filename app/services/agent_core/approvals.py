"""Freezing a tool call so an approval cannot be redirected.

An approval the user grants is for *these arguments*, not for this tool name.
Hashing the arguments at the moment approval is requested, and re-checking the
hash at the moment of execution, is what keeps that promise -- the loop can
change its mind between the two points, and the claim will simply fail.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_arguments(arguments: dict[str, Any]) -> str:
    """A stable text form of the arguments.

    ``sort_keys`` makes key order irrelevant, which it is. Everything else is
    preserved exactly, so ``1`` and ``"1"`` hash differently and a reordered
    nested list is a different call.
    """

    return json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)


def arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()
