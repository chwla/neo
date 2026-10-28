"""Run reviewed workspace writes through the anchored helper and tooling policy."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from app.core.config import active_profile_storage_dir
from app.core.paths import install_root
from app.services import isolation
from app.services.isolation import fs_helper, safe_fs


class FSOperationRefused(safe_fs.UnsafePath):
    """The reviewed version changed or the confined operation could not finish."""


_HELPER_START = (
    "import sys; "
    "sys.path.insert(0, sys.argv.pop(1)); "
    "from app.services.isolation.fs_helper import main; "
    "raise SystemExit(main())"
)


def _profile_id() -> str:
    storage = active_profile_storage_dir.get()
    return Path(storage).name if storage else ""


def apply(
    root: str | os.PathLike[str],
    relative: str,
    *,
    replacement: bytes | None,
    expected: bytes | None,
    permission_mode: int | None = None,
) -> None:
    """Write or delete only the exact file version the caller inspected.

    macOS uses the Seatbelt launcher. Other platforms have no validated tooling
    backend yet and retain the anchored operation, as the rollout plan specifies.
    A macOS launch that starts but fails attestation never falls back to a direct
    write.
    """

    target_root = Path(root)
    if not target_root.is_absolute():
        raise FSOperationRefused("workspace root is not absolute")
    root_dev, root_ino = safe_fs.root_identity(target_root)
    if replacement is not None and len(replacement) > fs_helper.MAX_DATA_BYTES:
        raise FSOperationRefused("reviewed write is too large")
    request = {
        "operation": "write" if replacement is not None else "delete",
        "relative": relative,
        "root_dev": root_dev,
        "root_ino": root_ino,
        "expect_exists": expected is not None,
        "expected_sha256": hashlib.sha256(expected).hexdigest()
        if expected is not None else None,
        "data_b64": base64.b64encode(replacement).decode("ascii")
        if replacement is not None else None,
        "permission_mode": permission_mode,
    }

    if isolation.status()["supported"]:
        result = isolation.run(
            [sys.executable, "-I", "-B", "-c", _HELPER_START, str(install_root())],
            cwd=str(target_root),
            env={"PATH": os.defpath, "HOME": str(target_root)},
            purpose="fs_helper",
            profile_id=_profile_id(),
            workspace=str(target_root),
            timeout=45,
            stdin_text=json.dumps(request, sort_keys=True),
        )
        if result.quarantined:
            raise FSOperationRefused("confined helper cleanup failed")
        try:
            response = json.loads((result.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError, TypeError):
            raise FSOperationRefused("confined helper did not return a result") from None
        if (
            result.exit_code != 0
            or not isinstance(response, dict)
            or response.get("ok") is not True
        ):
            reason = response.get("reason") if isinstance(response, dict) else None
            raise FSOperationRefused(f"confined helper refused: {reason or 'failed'}")
        return

    response = fs_helper.apply_request(target_root, request)
    if response.get("ok") is not True:
        raise FSOperationRefused(f"anchored operation refused: {response.get('reason')}")


__all__ = ["FSOperationRefused", "apply"]
