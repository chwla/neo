"""Smart folders: notes gathered by rules rather than filed by hand.

A smart folder holds no notes. It is a set of rules (tags, when a note was
made or edited, checklists, attachments, folders, pinned, locked) that match
all or any, evaluated whenever it is opened, as in Apple Notes. Rules are a
closed vocabulary validated here and compiled to SQL with parameters only:
nothing a client sends becomes SQL text.

Dates are the person's: "today" is their today, so a list request carries
the browser's offset from UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

MAX_RULES = 20


class TagRule(BaseModel):
    field: Literal["tags"]
    op: Literal["includes_any", "includes_all", "excludes", "has_any", "has_none"]
    value: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("value")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        cleaned = []
        for tag in value:
            tag = tag.strip().lower().lstrip("#").replace(" ", "-")[:40]
            if tag and tag not in cleaned:
                cleaned.append(tag)
        return cleaned


class DateRule(BaseModel):
    field: Literal["created", "edited"]
    op: Literal["today", "yesterday", "last_days", "before", "after"]
    #: A number of days for last_days; a YYYY-MM-DD day for before and after.
    value: int | str | None = None

    @field_validator("value")
    @classmethod
    def _value(cls, value: int | str | None, info) -> int | str | None:
        op = info.data.get("op")
        if op == "last_days":
            if not isinstance(value, int) or not 1 <= value <= 3650:
                raise ValueError("last_days needs a number of days from 1 to 3650.")
        elif op in ("before", "after"):
            try:
                datetime.strptime(str(value), "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("before and after need a day as YYYY-MM-DD.") from exc
        return value


class ChecklistRule(BaseModel):
    field: Literal["checklists"]
    op: Literal["has", "has_unchecked", "all_checked", "none"]


class AttachmentRule(BaseModel):
    field: Literal["attachments"]
    op: Literal["any", "images", "files", "sketches", "none"]


class FolderRule(BaseModel):
    field: Literal["folder"]
    op: Literal["in", "not_in"]
    #: Folder ids; "default" is the Notes folder.
    value: list[str] = Field(min_length=1, max_length=50)


class FlagRule(BaseModel):
    field: Literal["pinned", "locked"]
    op: Literal["is"]
    value: bool


Rule = Annotated[
    TagRule | DateRule | ChecklistRule | AttachmentRule | FolderRule | FlagRule,
    Field(discriminator="field"),
]


class SmartRules(BaseModel):
    match: Literal["all", "any"] = "all"
    rules: list[Rule] = Field(min_length=1, max_length=MAX_RULES)


def _day_start(now: datetime, tz_offset_minutes: int, days_back: int = 0) -> str:
    """The start of a local day, as the UTC ISO time notes are stored in."""

    zone = timezone(timedelta(minutes=tz_offset_minutes))
    local = now.astimezone(zone) - timedelta(days=days_back)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).isoformat()


def _date_clause(rule: DateRule, now: datetime, tz: int) -> tuple[str, list[Any]]:
    column = "n.created_at" if rule.field == "created" else "n.updated_at"
    if rule.op == "today":
        return f"{column} >= ?", [_day_start(now, tz)]
    if rule.op == "yesterday":
        return f"{column} >= ? AND {column} < ?", [_day_start(now, tz, 1), _day_start(now, tz)]
    if rule.op == "last_days":
        return f"{column} >= ?", [
            (now - timedelta(days=int(rule.value))).astimezone(UTC).isoformat()
        ]
    zone = timezone(timedelta(minutes=tz))
    day = datetime.strptime(str(rule.value), "%Y-%m-%d").replace(tzinfo=zone)
    if rule.op == "before":
        return f"{column} < ?", [day.astimezone(UTC).isoformat()]
    return f"{column} >= ?", [(day + timedelta(days=1)).astimezone(UTC).isoformat()]


def _tag_clause(rule: TagRule) -> tuple[str, list[Any]]:
    has = "EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = n.id{extra})"
    if rule.op == "has_any":
        return has.format(extra=""), []
    if rule.op == "has_none":
        return "NOT " + has.format(extra=""), []
    if not rule.value:
        raise ValueError("Choose at least one tag.")
    marks = ",".join("?" * len(rule.value))
    if rule.op == "includes_any":
        return has.format(extra=f" AND t.tag IN ({marks})"), list(rule.value)
    if rule.op == "excludes":
        return "NOT " + has.format(extra=f" AND t.tag IN ({marks})"), list(rule.value)
    return (
        f"(SELECT COUNT(DISTINCT t.tag) FROM note_tags t "
        f"WHERE t.note_id = n.id AND t.tag IN ({marks})) = ?",
        [*rule.value, len(rule.value)],
    )


_CHECKLIST = {
    "has": "n.checklist_total > 0",
    "has_unchecked": "n.checklist_total > n.checklist_done",
    "all_checked": "n.checklist_total > 0 AND n.checklist_done = n.checklist_total",
    "none": "n.checklist_total = 0",
}
_ATTACHMENTS = {
    "any": "(n.has_images = 1 OR n.has_files = 1 OR n.has_sketches = 1)",
    "images": "n.has_images = 1",
    "files": "n.has_files = 1",
    "sketches": "n.has_sketches = 1",
    "none": "(n.has_images = 0 AND n.has_files = 0 AND n.has_sketches = 0)",
}


def _folder_clause(rule: FolderRule) -> tuple[str, list[Any]]:
    ids = [folder for folder in rule.value if folder != "default"]
    parts = []
    if "default" in rule.value:
        parts.append("n.folder_id IS NULL")
    if ids:
        parts.append(f"n.folder_id IN ({','.join('?' * len(ids))})")
    clause = "(" + " OR ".join(parts) + ")"
    return (clause if rule.op == "in" else f"NOT {clause}"), ids


def compile_rules(
    rules: SmartRules, *, now: datetime | None = None, tz_offset_minutes: int = 0
) -> tuple[str, list[Any]]:
    """A WHERE fragment over notes ``n`` and its parameters."""

    now = now or datetime.now(UTC)
    tz = max(-14 * 60, min(14 * 60, int(tz_offset_minutes)))
    clauses: list[str] = []
    params: list[Any] = []
    for rule in rules.rules:
        if isinstance(rule, TagRule):
            sql, values = _tag_clause(rule)
        elif isinstance(rule, DateRule):
            sql, values = _date_clause(rule, now, tz)
        elif isinstance(rule, ChecklistRule):
            sql, values = _CHECKLIST[rule.op], []
        elif isinstance(rule, AttachmentRule):
            sql, values = _ATTACHMENTS[rule.op], []
        elif isinstance(rule, FolderRule):
            sql, values = _folder_clause(rule)
        else:
            column = "n.pinned = 1" if rule.field == "pinned" else "n.content_format = 'encrypted'"
            sql, values = (column if rule.value else f"NOT ({column})"), []
        clauses.append(f"({sql})")
        params.extend(values)
    joiner = " AND " if rules.match == "all" else " OR "
    return "(" + joiner.join(clauses) + ")", params
