"""What a skill is, on the wire.

A skill is a directory holding ``SKILL.md`` -- YAML frontmatter naming it and
saying when to use it, then a body of instructions. That is the Anthropic Agent
Skills layout on purpose: a skill someone already wrote works here unmodified,
and one written here works elsewhere.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Where a skill came from. ``ui`` is one written in Neo's own form; the other
#: two record an origin the user can be shown when deciding whether to keep it.
SourceType = str


class SkillCreate(BaseModel):
    """A skill written by hand in the panel."""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=1)
    enabled_by_default: bool = True


class SkillInstallFolder(BaseModel):
    """A skill copied in from a directory on this machine."""

    path: str = Field(min_length=1)
    enabled_by_default: bool = True


class SkillInstallGithub(BaseModel):
    """A skill fetched from a public GitHub repository."""

    url: str = Field(min_length=1)
    enabled_by_default: bool = True


class SkillUpdate(BaseModel):
    """Everything about an installed skill that the panel may change.

    The body is deliberately absent: editing instructions belongs in the file,
    and a skill installed from GitHub whose body Neo rewrote would no longer be
    the thing its recorded source says it is.
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=1024)
    enabled_by_default: bool | None = None


class SkillError(ValueError):
    """A skill could not be installed, and the reason is worth showing."""


__all__ = [
    "SkillCreate",
    "SkillError",
    "SkillInstallFolder",
    "SkillInstallGithub",
    "SkillUpdate",
    "SourceType",
]
