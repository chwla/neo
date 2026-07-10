from __future__ import annotations

import json
import zipfile
from io import BytesIO

from app.services.bundles import store
from app.services.bundles.checksums import sha256_bytes
from app.services.bundles.redaction import REDACTED, SENSITIVE, safe_archive_name

MAX_SIZE = 50 * 1024 * 1024
ALLOWED_PREFIXES = ("artifacts/", "patches/", "files/", "reports/")


class BundleImporter:
    def validate(self, data: bytes) -> dict:
        if len(data) > MAX_SIZE:
            raise ValueError("Bundle exceeds the 50 MiB size limit.")
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                names = archive.namelist()
                for info in archive.infolist():
                    safe_archive_name(info.filename)
                    if info.file_size > MAX_SIZE or (
                        info.filename not in {"neo_bundle.json", "checksums.json"}
                        and not info.filename.startswith(ALLOWED_PREFIXES)
                    ):
                        raise ValueError(f"Forbidden archive entry: {info.filename}")
                if "neo_bundle.json" not in names or "checksums.json" not in names:
                    raise ValueError("Bundle must contain neo_bundle.json and checksums.json.")
                manifest = json.loads(archive.read("neo_bundle.json"))
                checksums = json.loads(archive.read("checksums.json"))
                if manifest.get("schema_version") != 1:
                    raise ValueError("Unsupported bundle schema version.")
                for name, expected in checksums.items():
                    if name not in names or sha256_bytes(archive.read(name)) != expected:
                        raise ValueError(f"Checksum mismatch for {name}.")
                if _has_unredacted_secret(manifest):
                    raise ValueError("Bundle contains a possible secret field.")
        except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise ValueError("Bundle is not a readable ZIP archive.") from exc
        return {
            "valid": True,
            "manifest": manifest,
            "warnings": ["Archive-only import: records remain inert and are never executed."],
        }

    def import_archive(self, data: bytes, file_name: str) -> dict:
        result = self.validate(data)
        manifest = result["manifest"]
        entity_ids = [
            item.get("id")
            for values in manifest.get("entities", {}).values()
            if isinstance(values, list)
            for item in values
            if isinstance(item, dict) and item.get("id")
        ]
        record = store.record_import(
            file_name=file_name or "bundle.zip",
            sha256=sha256_bytes(data),
            size=len(data),
            warnings=result["warnings"],
            metadata={"manifest": manifest, "mode": "archive_only"},
            entity_ids=entity_ids,
        )
        (__import__("pathlib").Path(store.bundle_dir()) / f"import-{record['id']}.zip").write_bytes(
            data
        )
        return record


def _has_unredacted_secret(value: object, key: str = "") -> bool:
    """Reject credential-bearing values, while allowing the redaction audit flags."""
    if key in {"secrets_removed", "original_paths_redacted"}:
        return False
    if SENSITIVE.search(key):
        return value not in {None, "", REDACTED, False, True}
    if isinstance(value, dict):
        return any(_has_unredacted_secret(item, str(name)) for name, item in value.items())
    if isinstance(value, list):
        return any(_has_unredacted_secret(item, key) for item in value)
    return False
