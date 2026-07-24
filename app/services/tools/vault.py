from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from app.core.config import active_profile_storage_dir, get_base_settings
from app.services.tools import store


class ConnectorVaultError(ValueError):
    pass


def _decode_key(value: str) -> bytes:
    normalized = value.strip()
    try:
        key = base64.urlsafe_b64decode(normalized + "=" * (-len(normalized) % 4))
    except Exception as exc:
        raise ConnectorVaultError("Connector master key is not valid base64.") from exc
    if len(key) != 32:
        raise ConnectorVaultError("Connector master key must decode to exactly 32 bytes.")
    return key


def _generated_key_path() -> Path:
    explicit = os.environ.get("NEO_CONNECTOR_MASTER_KEY_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    settings = get_base_settings()
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
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ConnectorVaultError(
                f"Connector master key file permissions are too broad ({oct(mode)}); use 0600."
            )
        return _decode_key(path.read_text(encoding="ascii"))

    encoded = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    try:
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
    inline = os.environ.get("NEO_CONNECTOR_MASTER_KEY", "").strip()
    if inline:
        return _decode_key(inline)
    key_path = _generated_key_path()
    environment = os.environ.get("NEO_ENVIRONMENT", "development").strip().lower()
    if environment in {"production", "prod"} and not key_path.is_file():
        raise ConnectorVaultError(
            "Production connector encryption requires NEO_CONNECTOR_MASTER_KEY "
            "or an existing NEO_CONNECTOR_MASTER_KEY_FILE."
        )
    return _read_or_create_local_key(key_path)


def scoped_aad(value: str) -> str:
    """Bind ciphertext to the active profile as well as its record identifier."""

    profile_root = active_profile_storage_dir.get()
    if profile_root:
        scope_source = str(Path(profile_root).expanduser().resolve())
    else:
        settings = get_base_settings()
        scope_source = settings.data_dir or settings.database_url
    scope = hashlib.sha256(scope_source.encode("utf-8")).hexdigest()
    return f"profile:{scope}:{value}"


def seal_json(value: dict[str, Any], *, aad: str) -> tuple[str, str]:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ConnectorVaultError(
            "Connector encryption is unavailable; install the cryptography dependency."
        ) from exc
    nonce = os.urandom(12)
    plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(master_key()).encrypt(nonce, plaintext, aad.encode("utf-8"))
    return (
        base64.urlsafe_b64encode(nonce).decode("ascii"),
        base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    )


def open_json(nonce: str, ciphertext: str, *, aad: str) -> dict[str, Any]:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ConnectorVaultError(
            "Connector encryption is unavailable; install the cryptography dependency."
        ) from exc
    try:
        raw = AESGCM(master_key()).decrypt(
            base64.urlsafe_b64decode(nonce),
            base64.urlsafe_b64decode(ciphertext),
            aad.encode("utf-8"),
        )
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ConnectorVaultError("Connector credential could not be decrypted.") from exc
    if not isinstance(value, dict):
        raise ConnectorVaultError("Connector credential payload is invalid.")
    return value


def write_credential(
    *,
    server_id: str,
    auth_type: str,
    label: str | None,
    public_config: dict[str, Any],
    secret: dict[str, Any],
    expires_at: str | None = None,
    expected_updated_at: str | None = None,
) -> dict:
    nonce, ciphertext = seal_json(secret, aad=scoped_aad(f"connector:{server_id}"))
    payload = {
        "server_id": server_id,
        "auth_type": auth_type,
        "label": label,
        "public_config": public_config,
        "secret_nonce": nonce,
        "secret_ciphertext": ciphertext,
        "expires_at": expires_at,
        "updated_at": store.now_iso(),
    }
    if expected_updated_at is None:
        return store.upsert_connector_credential(payload)
    updated = store.replace_connector_credential(
        payload,
        expected_updated_at=expected_updated_at,
    )
    if updated is None:
        raise ConnectorVaultError(
            "Connector credentials changed during refresh; retry with the latest token."
        )
    return updated


def read_credential(server_id: str) -> tuple[dict, dict[str, Any]] | None:
    record = store.get_connector_credential(server_id)
    if record is None:
        return None
    secret = open_json(
        record["secret_nonce"],
        record["secret_ciphertext"],
        aad=scoped_aad(f"connector:{server_id}"),
    )
    return record, secret
