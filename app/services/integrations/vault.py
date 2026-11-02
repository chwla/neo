"""Sealing the secrets a connected account leaves behind.

An OAuth refresh token is a long-lived bearer credential for somebody's real
mailbox. It has to survive a restart, so it has to be written down; writing it
down in a column any other feature can read is what this module exists to
prevent. Everything stored here is AES-GCM sealed, and the additional
authenticated data binds the ciphertext to the profile that created it -- so a
credential row copied into another profile's database does not decrypt, it
fails. Profile isolation is enforced by the cipher, not by a WHERE clause.

Restored near-verbatim from ``app/services/tools/vault.py`` as it stood at
``19d174e^``, before the connector system was removed. Two deliberate changes:
the key is read through ``Settings`` rather than straight out of the
environment, so it is discoverable next to every other knob; and the two
functions that touched the connector store have moved to ``store.py``, which is
where database access belongs. The bookkeeping about the key itself (which key
was in use, what a reset would destroy) is in ``key_state``, imported lazily, so
importing this module still pulls in no store.

This is not the same thing as ``app.services.memory.local_crypto``, and the
difference is the key. That one derives from the owner id and password hash,
which is right for memory -- a password change should orphan it. It would be
wrong here: changing your password would silently disconnect every account you
had connected. So tokens are sealed under a machine-level key instead, and the
profile separation that the memory seed gets for free is supplied here by the
AAD.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import active_profile_storage_dir, get_base_settings

_LOG = logging.getLogger(__name__)

#: AES-GCM's standard nonce width. Twelve bytes is what the mode is specified
#: for; anything else forces an internal rehash and buys nothing.
NONCE_BYTES = 12

#: AES-256. The key is stored base64url-encoded, so a file holding anything that
#: does not decode to exactly this many bytes is a misconfiguration, not a key.
KEY_BYTES = 32


class IntegrationVaultError(ValueError):
    """A key could not be loaded, or a payload could not be sealed or opened."""


def _decode_key(value: str) -> bytes:
    normalized = value.strip()
    try:
        key = base64.urlsafe_b64decode(normalized + "=" * (-len(normalized) % 4))
    except Exception as exc:
        raise IntegrationVaultError("Integration master key is not valid base64.") from exc
    if len(key) != KEY_BYTES:
        raise IntegrationVaultError(
            f"Integration master key must decode to exactly {KEY_BYTES} bytes."
        )
    return key


def _generated_key_path() -> Path:
    """Where the key lives when it was not supplied inline.

    Read from base settings on purpose: ``get_settings()`` rewrites ``data_dir``
    per profile inside a request, and a key path built from that would be a
    different key for every profile -- which is the one thing this key must not
    be, since it is what makes a token readable after a password change.
    """

    settings = get_base_settings()
    explicit = (settings.connector_master_key_file or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if settings.data_dir:
        root = Path(settings.data_dir).expanduser().resolve()
    elif settings.database_url.startswith("sqlite:///"):
        root = (
            Path(settings.database_url.removeprefix("sqlite:///")).expanduser().resolve().parent
            / "profiles"
        )
    else:
        root = Path.cwd()
    return root / ".neo-connector-master-key"


#: Set on the command line or in the environment, never in ``.env`` by habit: a
#: deliberate, one-shot recovery. Its value must be the fingerprint the refusal
#: printed, so it cannot be left on to mint a fresh key after some later loss.
RESET_VARIABLE = "NEO_CONNECTOR_KEY_RESET"

_IS_WINDOWS = os.name == "nt"
#: Fingerprints already checked against the registry by this process.
_reconciled: set[str] = set()
_reconcile_lock = threading.Lock()


class ConnectorKeyUnavailable(IntegrationVaultError):
    """The key itself is missing, unreadable or invalid -- not one sealed row.

    Kept apart from a row that fails to open, because the remedies differ: this
    one is fixed by restoring the key or by a deliberate reset, never by
    reconnecting an account.
    """


def _windows_sid() -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argv
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=False
    )
    fields = [field.strip().strip('"') for field in completed.stdout.strip().split('","')]
    sid = fields[-1].strip('"') if fields else ""
    if completed.returncode != 0 or not sid.startswith("S-1-"):
        raise ConnectorKeyUnavailable("Neo could not determine the current Windows account.")
    return sid


def _restrict_windows_key(path: Path) -> None:
    """Remove inherited access, leaving full control to the current account only."""

    completed = subprocess.run(  # noqa: S603 - fixed argv, path chosen by Neo
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{_windows_sid()}:F"],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise ConnectorKeyUnavailable(
            f"Neo could not restrict access to the connector key at {path}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )


#: Trustees that may hold access to the key besides its owner: LocalSystem and the
#: local Administrators group, in SDDL's short and long spellings.
_WINDOWS_ALLOWED_TRUSTEES = {"SY", "BA", "S-1-5-18", "S-1-5-32-544"}
_SDDL_ACE = re.compile(r"\((A|OA|D|OD);[^;]*;[^;]*;[^;]*;[^;]*;([^)]+)\)")


def _windows_key_is_private(path: Path) -> bool:
    """Whether only this account, SYSTEM and Administrators are allowed in.

    Read as SDDL from ``icacls /save`` because that names trustees by SID; the
    human-readable output is translated (``NT-AUTORITÄT\\SYSTEM``) and would make
    the check depend on the display language.
    """

    with tempfile.TemporaryDirectory() as scratch:
        saved = Path(scratch) / "acl.txt"
        completed = subprocess.run(  # noqa: S603 - fixed argv
            ["icacls", str(path), "/save", str(saved)], capture_output=True, text=True,
            check=False,
        )
        if completed.returncode != 0 or not saved.exists():
            return False
        sddl = saved.read_bytes().decode("utf-16", errors="replace")
    allowed = _WINDOWS_ALLOWED_TRUSTEES | {_windows_sid()}
    trustees = [trustee for kind, trustee in _SDDL_ACE.findall(sddl) if kind in {"A", "OA"}]
    return bool(trustees) and all(trustee in allowed for trustee in trustees)


def _read_key_file(path: Path) -> bytes:
    """Read an existing key file, refusing one others can read. Never regenerates."""

    if path.is_symlink():
        raise ConnectorKeyUnavailable(f"The connector key at {path} is a link; refusing it.")
    try:
        if _IS_WINDOWS:
            if not _windows_key_is_private(path):
                raise ConnectorKeyUnavailable(
                    f"The connector key at {path} is readable by other Windows accounts. "
                    "Restrict it to your account (icacls /inheritance:r) and restart."
                )
        else:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise ConnectorKeyUnavailable(
                    "Integration master key file permissions are too broad "
                    f"({oct(mode)}); use 0600."
                )
        text = path.read_text(encoding="ascii")
    except ConnectorKeyUnavailable:
        # Already a specific verdict ("too broad", "other accounts"); it is also a
        # ValueError, so it must not fall into the "damaged" branch below.
        raise
    except PermissionError as exc:
        raise ConnectorKeyUnavailable(
            f"Neo cannot read the connector key at {path} ({exc.strerror}). In a "
            "container this usually means it is running as a different user than the one "
            "that created the key; run it as that user again (NEO_UID)."
        ) from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConnectorKeyUnavailable(
            f"The connector key at {path} is damaged. Restore it from a backup."
        ) from exc
    try:
        return _decode_key(text)
    except IntegrationVaultError as exc:
        raise ConnectorKeyUnavailable(
            f"The connector key at {path} is damaged ({exc}). Restore it from a backup."
        ) from exc


def _create_key_file(path: Path) -> bytes:
    """Generate a key into a file that does not exist yet; ``O_EXCL`` settles races."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii")
    try:
        # O_EXCL rather than a plain open: two workers starting at once must not
        # both generate a key, because the loser's tokens would be unreadable.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_key_file(path)
    try:
        if _IS_WINDOWS:
            # Restricted before the secret is written, so it is never readable by others.
            _restrict_windows_key(path)
        os.write(descriptor, encoded.encode("ascii"))
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    return _decode_key(encoded)


def _reconcile(key: bytes) -> None:
    """Note the key in use; a different key from last time is a replacement."""

    from app.services.integrations import key_state

    current = key_state.fingerprint(key)
    with _reconcile_lock:
        if current in _reconciled:
            return
        recorded = key_state.recorded_fingerprint()
        if recorded is None:
            key_state.record(current, kind="adopted")
        elif recorded != current:
            # An operator supplied a different key. Accepted -- it is theirs to
            # choose -- and recorded; rows sealed under the old one now fail to
            # open and are reported as needing to be reconnected.
            _LOG.warning("connector_key_replaced old=%s new=%s", recorded, current)
            key_state.record(current, kind="replaced", old=recorded)
        _reconciled.add(current)


def _missing_key(path: Path) -> bytes:
    """No key file: generate one only when nothing could have been sealed before."""

    from app.services.integrations import key_state

    with _reconcile_lock:
        recorded = key_state.recorded_fingerprint()
        counts = key_state.sealed_counts()
        expected = recorded or (key_state.orphan_token(counts) if counts else None)
        if expected is None:
            key = _create_key_file(path)
            current = key_state.fingerprint(key)
            key_state.record(current, kind="generated")
            _reconciled.add(current)
            _LOG.warning("connector_key_generated path=%s fingerprint=%s", path, current)
            return key

        requested = os.environ.get(RESET_VARIABLE, "").strip()
        if requested and hmac.compare_digest(requested, expected):
            _LOG.warning(
                "connector_key_reset old=%s path=%s unreadable_after_reset=%s",
                recorded, path, json.dumps(counts, sort_keys=True),
            )
            key = _create_key_file(path)
            current = key_state.fingerprint(key)
            key_state.record(current, kind="reset", old=recorded, counts=counts)
            key_state.mark_reconnect_required(sorted(counts))
            _reconciled.add(current)
            return key
        if requested:
            _LOG.warning("connector_key_reset_ignored reason=value_does_not_match")

    lost = sum(sum(tables.values()) for tables in counts.values())
    raise ConnectorKeyUnavailable(
        f"The connector key file is missing ({path}). It protects every profile's "
        f"connected accounts, OAuth client secrets and GitHub tokens ({lost} sealed "
        "records on this install). Restore the file from a backup and restart. Only "
        "if no backup exists: start over by setting "
        f"{RESET_VARIABLE}={expected} for one restart. That permanently destroys those "
        "records -- every account must be reconnected and every secret re-entered -- and "
        "touches nothing else (passwords, chats, notes, memories and files are unaffected)."
    )


def master_key() -> bytes:
    """The key every sealed payload is encrypted under.

    Read on every call rather than cached, so a restored or replaced key takes
    effect without a restart. A missing key is generated only on a genuine first
    run; otherwise the loss is reported with the way back, never papered over.
    """

    settings = get_base_settings()
    inline = (settings.connector_master_key or "").strip()
    if inline:
        try:
            key = _decode_key(inline)
        except IntegrationVaultError as exc:
            raise ConnectorKeyUnavailable(str(exc)) from exc
        _reconcile(key)
        return key
    key_path = _generated_key_path()
    if key_path.exists() or key_path.is_symlink():
        key = _read_key_file(key_path)
        _reconcile(key)
        return key
    return _missing_key(key_path)


def key_status() -> dict[str, str]:
    """The key's state for signed-in readiness: ``ok``, or what is wrong with it."""

    try:
        master_key()
    except ConnectorKeyUnavailable as exc:
        return {"state": "unavailable", "detail": str(exc)}
    return {"state": "ok", "detail": ""}


def scoped_aad(value: str) -> str:
    """Bind ciphertext to the active profile as well as to its record identifier.

    This is the whole of profile isolation for stored credentials. The AAD is
    covered by the GCM tag, so a row lifted from one profile's database into
    another's fails to authenticate rather than decrypting into someone else's
    live token.

    Note the consequence, which is deliberate: the scope is derived from the
    profile's resolved storage path, so moving the data directory invalidates
    every sealed token. Callers surface that as "reconnect required".
    """

    profile_root = active_profile_storage_dir.get()
    if profile_root:
        scope_source = str(Path(profile_root).expanduser().resolve())
    else:
        settings = get_base_settings()
        scope_source = settings.data_dir or settings.database_url
    scope = hashlib.sha256(scope_source.encode("utf-8")).hexdigest()
    return f"profile:{scope}:{value}"


def seal_json(value: dict[str, Any], *, aad: str) -> tuple[str, str]:
    """Seal a payload, returning ``(nonce, ciphertext)`` both base64url encoded."""

    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(master_key()).encrypt(nonce, plaintext, aad.encode("utf-8"))
    return (
        base64.urlsafe_b64encode(nonce).decode("ascii"),
        base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    )


def open_json(nonce: str, ciphertext: str, *, aad: str) -> dict[str, Any]:
    """Open a sealed payload.

    Every failure -- wrong key, wrong profile, corrupted row, truncated base64 --
    is reported as the same error with no detail. Which of them happened is
    information about the key material, and the caller's only useful response is
    the same in every case.
    """

    # Loaded outside the catch-all: a missing or damaged key is a key problem with
    # its own remedy, not one more row that "could not be decrypted".
    key = master_key()
    try:
        raw = AESGCM(key).decrypt(
            base64.urlsafe_b64decode(nonce),
            base64.urlsafe_b64decode(ciphertext),
            aad.encode("utf-8"),
        )
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise IntegrationVaultError("Stored credential could not be decrypted.") from exc
    if not isinstance(value, dict):
        raise IntegrationVaultError("Stored credential payload is invalid.")
    return value
