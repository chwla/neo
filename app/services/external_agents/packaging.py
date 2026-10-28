"""Resolving each CLI's install to the files that actually execute.

``executables.stage`` needs to know two things: which file is the entrypoint, and
which tree has to travel with it. That differs per engine and is measured, not
guessed (see docs/external-agents/cli-surface.md):

* **Claude Code** is one self-contained Mach-O, ``bin/claude.exe``, linking only
  system libraries. One file.
* **Codex** on ``PATH`` is a Node wrapper that locates and spawns a native binary
  under ``@openai/codex-<platform>/vendor/<triple>/``. That whole vendor directory
  is the package: the binary runs ``codex-code-mode-host``, a bundled ``rg`` and a
  bundled ``zsh`` from paths relative to itself. Neo launches the native binary
  directly, so the wrapper -- and the Node interpreter it needs -- are out of the
  picture entirely.

An engine whose layout is not recognised is staged as a single file, which is right
for a self-contained binary and fails loudly rather than silently half-copying a
tree.
"""

from __future__ import annotations

import json
from pathlib import Path


def resolve_package(executor: str, binary: str) -> tuple[str, str | None]:
    """Return ``(entrypoint, package_root)`` for one engine's install."""

    resolved = Path(binary).expanduser()
    try:
        real = resolved.resolve(strict=True)
    except OSError:
        return str(resolved), None

    if executor == "codex":
        native, root = _codex_native(real)
        if native is not None:
            return str(native), str(root)
    return str(real), None


def _codex_native(wrapper: Path) -> tuple[Path | None, Path | None]:
    """Find the native Codex binary and its vendor root from the npm wrapper.

    The layout is declared by the platform package's own ``codex-package.json``
    (``entrypoint``, ``resourcesDir``, ``pathDir``), so it is read rather than
    assumed; a future layout change shows up as "not recognised" instead of as a
    package missing half its files.
    """

    if wrapper.is_dir():
        return None, None
    # Already the native binary: its vendor root is two levels up (bin/codex).
    if wrapper.name == "codex" and (wrapper.parent.parent / "codex-package.json").exists():
        return wrapper, wrapper.parent.parent

    package_root = wrapper.parent.parent  # .../@openai/codex/
    candidates = sorted(
        (package_root / "node_modules" / "@openai").glob("codex-*/vendor/*")
    ) + sorted((package_root / "vendor").glob("*"))
    for vendor in candidates:
        manifest = vendor / "codex-package.json"
        if not manifest.is_file():
            continue
        try:
            layout = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entry = vendor / str(layout.get("entrypoint") or "bin/codex")
        if entry.is_file():
            return entry, vendor
    return None, None


__all__ = ["resolve_package"]
