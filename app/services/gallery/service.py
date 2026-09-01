"""The gallery: every image Neo has seen, kept once and findable later.

An item is a thin layer over a ``workspace_files`` row. That row already owns the
bytes, the sha256 and the soft-delete flag, so the deduplication the gallery
promises -- upload the same photo twice, get one entry -- is inherited rather
than reimplemented: ``WorkspaceFilesService.import_bytes`` returns the existing
row on a hash match, and enrolling that row a second time records a new
*appearance* instead of a new item.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.core.config import get_settings
from app.services.files.service import WorkspaceFilesService
from app.services.gallery import images, store
from app.services.gallery.types import GalleryItemUpdate


class GalleryError(ValueError):
    """The request cannot be satisfied as asked."""


class GalleryService:
    def __init__(self, thumbnails_root: Path | None = None) -> None:
        settings = get_settings()
        store.initialize_gallery_tables()
        self.files = WorkspaceFilesService()
        self.thumbnails_root = Path(thumbnails_root or settings.gallery_thumbnails_dir)
        self.thumbnail_max_px = settings.gallery_thumbnail_max_px
        self.describe_max_px = settings.gallery_describe_max_px
        self.auto_describe = settings.gallery_auto_describe
        self.default_allow_duplicates = settings.gallery_allow_duplicates

    ALLOW_DUPLICATES_KEY = "allow_duplicates"

    def allow_duplicates(self) -> bool:
        """Whether the same image may be enrolled more than once.

        Stored per profile, because it is a preference about this person's
        gallery rather than a property of the deployment.
        """

        stored = store.get_preference(self.ALLOW_DUPLICATES_KEY)
        if stored is None:
            return self.default_allow_duplicates
        return stored == "1"

    def set_allow_duplicates(self, allowed: bool) -> bool:
        store.set_preference(self.ALLOW_DUPLICATES_KEY, "1" if allowed else "0")
        return allowed

    # ------------------------------------------------------------------ ingest

    def enrol(
        self,
        file_id: str,
        *,
        origin: str = "upload",
        chat_id: int | None = None,
        message_id: int | None = None,
        project_id: str | None = None,
        role: str = "user",
        describe: bool | None = None,
    ) -> dict:
        """Bring a stored file into the gallery, or note that it appeared again.

        Returns the item either way. Enrolling a file that is already in the
        gallery does not re-describe it, re-embed it, or overwrite a caption the
        user has edited -- it only records where it was seen.
        """

        record = self.files.get(file_id)
        existing = store.get_item_by_file(file_id, include_deleted=True)
        if existing:
            if existing["deleted"]:
                # Re-sharing something that was deleted brings it back rather than
                # colliding with the unique index on file_id.
                store.update_item(existing["id"], {"deleted": False})
            self._note_appearance(existing["id"], chat_id, message_id, project_id, role)
            return store.get_item(existing["id"]) or existing

        content = self._read_bytes(file_id)
        probe = images.probe(content)
        item_id = str(uuid.uuid4())
        thumbnail_path = self._write_thumbnail(item_id, content)
        now = store.now_iso()
        item = store.insert_item(
            {
                "id": item_id,
                "file_id": file_id,
                # Until the model has looked, the filename is the only honest
                # title. It is replaced on the first successful describe.
                "title": record.get("display_name"),
                "tags": [],
                "width": probe.width,
                "height": probe.height,
                "image_format": probe.image_format,
                "thumbnail_path": thumbnail_path,
                "phash": images.perceptual_hash(content),
                "origin": origin,
                "description_status": "pending",
                "created_at": now,
                "updated_at": now,
            }
        )
        self._note_appearance(item_id, chat_id, message_id, project_id, role)
        if describe if describe is not None else self.auto_describe:
            self.request_description(item_id)
        return store.get_item(item_id) or item

    def enrol_upload(
        self,
        *,
        original_filename: str,
        content: bytes,
        mime_type: str | None = None,
        origin: str = "upload",
        chat_id: int | None = None,
        message_id: int | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Import bytes and enrol them in one step.

        Validates the pixels before writing anything: a file that is not a
        readable image is refused here rather than becoming a workspace file that
        the gallery then cannot describe.
        """

        settings = get_settings()
        if not content:
            raise GalleryError("Empty files cannot be uploaded.")
        if len(content) > settings.gallery_image_max_bytes:
            limit = settings.gallery_image_max_bytes
            raise GalleryError(f"Image exceeds the {limit}-byte upload limit.")
        images.probe(content)
        record = self.files.import_bytes(
            original_filename=original_filename,
            content=content,
            mime_type=mime_type,
            deduplicate=not self.allow_duplicates(),
        )
        return self.enrol(
            record["id"],
            origin=origin,
            chat_id=chat_id,
            message_id=message_id,
            project_id=project_id,
        )

    def _note_appearance(
        self,
        item_id: str,
        chat_id: int | None,
        message_id: int | None,
        project_id: str | None,
        role: str,
    ) -> None:
        if chat_id is None and message_id is None and project_id is None:
            return
        store.record_appearance(
            {
                "id": str(uuid.uuid4()),
                "item_id": item_id,
                "chat_id": chat_id,
                "message_id": message_id,
                "project_id": project_id,
                "role": role,
                "seen_at": store.now_iso(),
            }
        )

    # -------------------------------------------------------------------- read

    def get(self, item_id: str) -> dict:
        item = store.get_item(item_id)
        if not item:
            raise LookupError("Gallery item not found.")
        return item

    def detail(self, item_id: str) -> dict:
        item = self.get(item_id)
        return {"item": item, "appearances": store.list_appearances(item_id)}

    def list(self, **filters) -> tuple[list[dict], int]:
        return store.list_items(**filters)

    def image_bytes(self, item_id: str) -> bytes:
        return self._read_bytes(self.get(item_id)["file_id"])

    def image_path(self, item_id: str) -> Path:
        return self.files.download_path(self.get(item_id)["file_id"])

    def thumbnail_path(self, item_id: str) -> Path:
        """The cached thumbnail, regenerated if it has gone missing."""

        item = self.get(item_id)
        stored = item.get("thumbnail_path")
        if stored and Path(stored).is_file():
            return Path(stored)
        path = self._write_thumbnail(item_id, self._read_bytes(item["file_id"]))
        store.update_item(item_id, {"thumbnail_path": path})
        return Path(path)

    def attachments_for_prompt(self, item_ids: list[str]) -> tuple[list[str], list[dict]]:
        """Base64 payloads for ``LLMMessage.images``, plus the items they came from.

        Downscaled to what the model will actually consume. Items that cannot be
        read are skipped rather than raising: one unreadable attachment must not
        cost the user their whole message.
        """

        payloads: list[str] = []
        resolved: list[dict] = []
        for item_id in item_ids:
            try:
                item = self.get(item_id)
                content = self._read_bytes(item["file_id"])
                payloads.append(images.for_vision(content, self.describe_max_px))
                resolved.append(item)
            except (LookupError, images.ImageError, OSError):
                continue
        return payloads, resolved

    # ------------------------------------------------------------------ mutate

    def update(self, item_id: str, patch: GalleryItemUpdate) -> dict:
        self.get(item_id)
        fields = patch.model_dump(exclude_none=True)
        if not fields:
            return self.get(item_id)
        # Any hand edit marks the item, so a later re-describe knows not to
        # overwrite words the user chose.
        fields["user_edited"] = True
        updated = store.update_item(item_id, fields)
        if updated:
            self._reembed(item_id)
        return updated or self.get(item_id)

    def delete(self, item_id: str, *, purge: bool = False) -> None:
        """Soft delete by default, matching how workspace files behave.

        ``purge`` destroys the derived data and the bytes as well. It is the only
        way to reclaim the disk, so it is offered explicitly rather than being
        what an accidental click does.
        """

        item = store.get_item(item_id, include_deleted=True)
        if not item:
            raise LookupError("Gallery item not found.")
        if not purge:
            store.update_item(item_id, {"deleted": True})
            self._deindex_memory(item_id)
            return
        thumbnail = item.get("thumbnail_path")
        if thumbnail:
            Path(thumbnail).unlink(missing_ok=True)
        try:
            self.files.download_path(item["file_id"]).unlink(missing_ok=True)
        except (LookupError, OSError):
            pass
        from app.services.files import store as files_store

        files_store.update_file(item["file_id"], {"deleted": True})
        self._deindex_memory(item_id)
        store.delete_item(item_id)

    def request_description(self, item_id: str, *, force: bool = False) -> dict:
        """Hand the item to the describer. Never blocks the caller."""

        self.get(item_id)
        from app.services.gallery.describe import schedule_description

        schedule_description(item_id, force=force)
        return self.get(item_id)

    # ----------------------------------------------------------------- private

    def _read_bytes(self, file_id: str) -> bytes:
        return self.files.download_path(file_id).read_bytes()

    def _write_thumbnail(self, item_id: str, content: bytes) -> str:
        self.thumbnails_root.mkdir(parents=True, exist_ok=True)
        path = self.thumbnails_root / f"{item_id}.webp"
        path.write_bytes(images.make_thumbnail(content, self.thumbnail_max_px))
        return str(path)

    def _reembed(self, item_id: str) -> None:
        """Keep the vector aligned with the words after an edit. Never fatal."""

        try:
            from app.services.gallery.vectors import GalleryVectorIndex

            GalleryVectorIndex().index_item(item_id)
        except Exception:
            # An unavailable embedder must not make a caption un-editable.
            pass

    def _deindex_memory(self, item_id: str) -> None:
        try:
            from app.services.gallery.memory_sync import forget_item

            forget_item(item_id)
        except Exception:
            pass
