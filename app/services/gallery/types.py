from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: Where an image came from. ``generated`` has no producer yet -- Neo creates no
#: images today -- but it is in the vocabulary so that when one arrives the
#: schema, the filters and the UI do not have to change to accommodate it.
Origin = Literal["chat_attachment", "paste", "upload", "generated"]
DescriptionStatus = Literal["pending", "ready", "failed", "skipped"]


class GalleryAppearance(BaseModel):
    id: str
    item_id: str
    chat_id: int | None = None
    message_id: int | None = None
    agent_session_id: str | None = None
    project_id: str | None = None
    role: str = "user"
    seen_at: str


class GalleryItem(BaseModel):
    id: str
    file_id: str
    title: str | None = None
    caption: str | None = None
    ocr_text: str | None = None
    alt_text: str | None = None
    tags: list[str] = Field(default_factory=list)
    width: int | None = None
    height: int | None = None
    image_format: str | None = None
    phash: str | None = None
    origin: str = "upload"
    description_status: str = "pending"
    description_model: str | None = None
    description_error: str | None = None
    described_at: str | None = None
    user_edited: bool = False
    pinned: bool = False
    deleted: bool = False
    created_at: str
    updated_at: str


class GalleryItemUpdate(BaseModel):
    """What a person may change by hand.

    Deliberately excludes the derived fields: ocr_text is the model's transcript
    and phash is computed from pixels, so neither is meaningful to hand-edit.
    """

    title: str | None = Field(default=None, max_length=300)
    caption: str | None = Field(default=None, max_length=8000)
    alt_text: str | None = Field(default=None, max_length=1000)
    tags: list[str] | None = Field(default=None, max_length=40)
    pinned: bool | None = None


class GalleryEnrolRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=64)
    origin: Origin = "upload"
    chat_id: int | None = None
    message_id: int | None = None
    project_id: str | None = Field(default=None, max_length=64)
    role: str = Field(default="user", max_length=24)


class GallerySearchRequest(BaseModel):
    query: str = Field(default="", max_length=500)
    chat_id: int | None = None
    project_id: str | None = Field(default=None, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=20)
    since: str | None = None
    until: str | None = None
    limit: int = Field(default=12, ge=1, le=100)
    include_score_breakdown: bool = False


class GallerySearchHit(BaseModel):
    item: GalleryItem
    score: float
    appearances: list[GalleryAppearance] = Field(default_factory=list)
    score_breakdown: dict[str, float] | None = None
