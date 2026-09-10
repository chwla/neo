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
where database access belongs. What is left is only cryptography, so nothing
here imports a store and no import cycle is possible.

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
import json
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import active_profile_storage_dir, get_base_settings

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


def _read_or_create_local_key(path: Path) -> bytes:
    """Read the key file, generating one on first use.

    Refusing a world- or group-readable key file is the point of the mode check:
    a key anybody on the machine can read is not protecting anything, and a
    silent downgrade to "encrypted, sort of" is worse than a loud failure.
    """

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise IntegrationVaultError(
                f"Integration master key file permissions are too broad ({oct(mode)}); use 0600."
            )
        return _decode_key(path.read_text(encoding="ascii"))

    encoded = base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii")
    try:
        # O_EXCL rather than a plain open: two workers starting at once must not
        # both generate a key, because the loser's tokens would be unreadable.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_or_create_local_key(path)
    try:
        os.write(descriptor, encoded.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return _decode_key(encoded)


def master_key() -> bytes:
    """The key every sealed payload is encrypted under.

    Deliberately uncached. Reading a 44-byte file costs nothing next to the HTTP
    round trip that always follows, and a cache would mean a rotated key needed a
    restart to take effect.
    """

    settings = get_base_settings()
    inline = (settings.connector_master_key or "").strip()
    if inline:
        return _decode_key(inline)
    key_path = _generated_key_path()
    # Generating a key on demand is right for a laptop and wrong for a server:
    # a container with no persistent volume would mint a fresh one on every
    # deploy and silently disconnect every account. Read from the environment
    # rather than Settings because this names the deployment, not a Neo feature.
    environment = os.environ.get("NEO_ENVIRONMENT", "development").strip().lower()
    if environment in {"production", "prod"} and not key_path.is_file():
        raise IntegrationVaultError(
            "Production integration encryption requires NEO_CONNECTOR_MASTER_KEY "
            "or an existing NEO_CONNECTOR_MASTER_KEY_FILE."
        )
    return _read_or_create_local_key(key_path)


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

    try:
        raw = AESGCM(master_key()).decrypt(
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
