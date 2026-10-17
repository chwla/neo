from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SourceType = Literal[
    "manual",
    "research_report",
    "chat",
    "web_source",
    "memory_candidate",
    "project",
    "agent_run",
]

#: How a note's content is held. ``markdown``: the body is the content (legacy and
#: API-written notes). ``rich``: the editor's document is the content and the body
#: is a lossy Markdown projection of it. ``encrypted``: only ciphertext is stored.
ContentFormat = Literal["markdown", "rich", "encrypted"]


class Note(BaseModel):
    id: str
    title: str
    body: str
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_type: SourceType | None = None
    source_id: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    pinned: bool = False
    archived: bool = False
    deleted: bool = False
    created_at: str
    updated_at: str

    content_format: ContentFormat = "markdown"
    doc: dict[str, Any] | None = None
    cipher: dict[str, Any] | None = None
    lock_key_version: int | None = None
    revision: int = 0
    meta_revision: int = 0
    meta_updated_at: str | None = None
    folder_id: str | None = None
    deleted_at: str | None = None
    checklist_total: int = 0
    checklist_done: int = 0
    has_images: bool = False
    has_files: bool = False
    has_sketches: bool = False
    has_tables: bool = False
    has_links: bool = False
    cover_file_id: str | None = None
    preview: str = ""
    excerpt: str = ""
    open_conflicts: int = 0


class NoteListItem(Note):
    snippet: str | None = None


class NoteSearchResult(NoteListItem):
    pass


class NoteCreate(BaseModel):
    title: str | None = None
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    source_type: SourceType | None = "manual"
    source_id: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    # Editor-created notes arrive with their document and a write id, so a
    # create replayed after a crash returns the note it already made.
    doc: dict[str, Any] | None = None
    folder_id: str | None = None
    write_id: str | None = Field(default=None, max_length=160)


class NoteUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None
    summary: str | None = None
    source_type: SourceType | None = None
    source_id: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    source_metadata: dict[str, Any] | None = None
    base_revision: int | None = None


class NoteContentWrite(BaseModel):
    """A whole-content write from the editor, applied only on top of ``base_revision``."""

    base_revision: int = Field(ge=0)
    write_id: str = Field(min_length=1, max_length=160)
    format: Literal["rich", "encrypted"]
    title: str = Field(default="", max_length=2000)
    body: str = ""
    doc: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    cipher: dict[str, Any] | None = None
    lock_key_version: int | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=2000)
    # A locked note that still held its original Markdown is now a document:
    # its current ciphertext (that Markdown) is kept as the original version.
    keep_original: bool = False


class NoteTag(BaseModel):
    tag: str
    count: int


class NoteLink(BaseModel):
    id: str
    note_id: str
    link_type: str
    target_id: str | None = None
    target_url: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class NoteFolder(BaseModel):
    id: str
    name: str
    parent_id: str | None = None
    position: float = 0
    created_at: str
    updated_at: str
    count: int = 0


class NoteFolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    parent_id: str | None = None


class NoteFolderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    parent_id: str | None = None
    move_to_root: bool = False
    position: float | None = None


class NoteVersionSummary(BaseModel):
    id: str
    note_id: str
    kind: str
    content_format: ContentFormat
    title: str
    revision: int
    created_at: str


class NoteVersion(NoteVersionSummary):
    body: str | None = None
    doc: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    cipher: dict[str, Any] | None = None
    lock_key_version: int | None = None


class NoteConflict(BaseModel):
    id: str
    note_id: str
    reason: Literal["stale", "deleted"]
    base_revision: int
    server_revision: int
    write_id: str | None = None
    content_format: ContentFormat
    title: str
    body: str | None = None
    doc: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    cipher: dict[str, Any] | None = None
    lock_key_version: int | None = None
    attachment_ids: list[str] = Field(default_factory=list)
    created_at: str
    resolved_at: str | None = None
    resolution: str | None = None


class ConflictResolution(BaseModel):
    resolution: Literal["keep_mine", "use_other", "save_copy"]
    base_revision: int | None = None
    write_id: str | None = Field(default=None, max_length=160)


class VersionRestore(BaseModel):
    base_revision: int = Field(ge=0)
    write_id: str = Field(min_length=1, max_length=160)


class NoteSettings(BaseModel):
    sort_by: Literal["edited", "created", "title"] = "edited"
    sort_order: Literal["desc", "asc"] = "desc"
    group_by_date: bool = True
    new_note_style: Literal["title", "heading", "subheading", "body"] = "title"
    checked_items: Literal["manual", "auto"] = "manual"
    view: Literal["list", "gallery"] = "list"


class NoteSettingsUpdate(BaseModel):
    sort_by: Literal["edited", "created", "title"] | None = None
    sort_order: Literal["desc", "asc"] | None = None
    group_by_date: bool | None = None
    new_note_style: Literal["title", "heading", "subheading", "body"] | None = None
    checked_items: Literal["manual", "auto"] | None = None
    view: Literal["list", "gallery"] | None = None


class SmartFolder(BaseModel):
    id: str
    name: str
    rules: dict[str, Any]
    position: float = 0
    created_at: str
    updated_at: str
    count: int = 0


class SmartFolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    rules: dict[str, Any]


class SmartFolderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    rules: dict[str, Any] | None = None


class TagRename(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class NoteSidebar(BaseModel):
    folders: list[NoteFolder]
    counts: dict[str, int]
    tags: list[NoteTag]
    smart_folders: list[SmartFolder] = Field(default_factory=list)
