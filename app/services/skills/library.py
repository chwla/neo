"""Skills on disk: reading them, and getting them there.

A skill is a directory with a ``SKILL.md`` at its top level. Frontmatter names
it and says when to use it; the body is the instruction text a run loads. This
module owns that format and the three ways a skill arrives -- copied from a
folder, fetched from GitHub, or typed into Neo's own form -- and nothing else
parses ``SKILL.md``.

Everything installed is copied into the profile's own skills directory rather
than referenced where it was found. A skill the user points at today can be
moved, edited, or deleted tomorrow, and a run that silently changed behaviour
because a file moved underneath it would be the worst kind of bug to chase.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import requests
import yaml

from app.core.config import get_settings
from app.services.repos.safety import validate_repo_root
from app.services.skills import store
from app.services.skills.types import SkillError

SKILL_FILE = "SKILL.md"

#: Ceilings on what one skill may bring with it. A skill is instructions plus
#: the handful of files they reference; anything larger is a repository someone
#: pointed at by mistake, and copying it would be the visible symptom of a
#: mistake we can refuse outright.
MAX_SKILL_FILES = 20
MAX_SKILL_BYTES = 1024 * 1024

#: Frontmatter is bounded too. A ``SKILL.md`` whose header never closes would
#: otherwise be scanned to the end of a file of any size.
MAX_FRONTMATTER_CHARS = 8000

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

GITHUB_API = "https://api.github.com"
_GITHUB_URL = re.compile(
    r"\Ahttps?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"(?:/(?:tree|blob)/(?P<ref>[^/]+)(?:/(?P<path>.*))?)?/?\Z"
)
_GITHUB_SHORTHAND = re.compile(r"\A(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)(?:/(?P<path>.+))?\Z")


def slugify(name: str) -> str:
    """A directory-safe identifier for a skill name.

    Normalised to ASCII first, so ``Café Notes`` and ``Cafe Notes`` cannot
    install as two skills that read as one in the panel.
    """

    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return _SLUG_STRIP.sub("-", ascii_name.lower()).strip("-")


def skills_root() -> Path:
    root = Path(get_settings().skills_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def parse_skill_md(text: str) -> tuple[str, str, str]:
    """``(name, description, body)`` from one ``SKILL.md``.

    Every failure names what is missing. Installing a skill is a step the user
    took deliberately, and "invalid skill" would leave them guessing at which of
    four things went wrong.
    """

    match = _FRONTMATTER.match(text.lstrip("﻿"))
    if not match:
        raise SkillError(
            f"{SKILL_FILE} must start with a YAML frontmatter block fenced by --- lines."
        )
    raw, body = match.group(1), match.group(2)
    if len(raw) > MAX_FRONTMATTER_CHARS:
        raise SkillError(f"{SKILL_FILE} frontmatter is too long to be a skill header.")
    try:
        header = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillError(f"{SKILL_FILE} frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(header, dict):
        raise SkillError(f"{SKILL_FILE} frontmatter must be a mapping of keys to values.")

    name = str(header.get("name") or "").strip()
    description = str(header.get("description") or "").strip()
    if not name:
        raise SkillError(f"{SKILL_FILE} frontmatter is missing a 'name'.")
    if not description:
        # The description is what the model reads to decide whether a skill
        # applies, so a skill without one can be installed but never chosen.
        raise SkillError(f"{SKILL_FILE} frontmatter is missing a 'description'.")
    if not slugify(name):
        raise SkillError(f"Skill name '{name}' has no letters or digits to make an id from.")
    if not body.strip():
        raise SkillError(f"{SKILL_FILE} has frontmatter but no instructions under it.")
    return name, description, body.strip()


def read_skill_md(directory: Path) -> tuple[str, str, str]:
    path = directory / SKILL_FILE
    if not path.is_file():
        raise SkillError(f"No {SKILL_FILE} in {directory}.")
    try:
        return parse_skill_md(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise SkillError(f"{SKILL_FILE} is not valid UTF-8 text.") from exc


def body_text(skill: dict) -> str:
    """The instructions of an installed skill, read fresh from disk.

    Not cached and not stored in the database: the file is the skill, and a user
    who edits it expects the next run to use what they wrote.
    """

    return read_skill_md(Path(skill["directory"]))[2]


def _content_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _reserve_directory(slug: str) -> Path:
    """A fresh directory for ``slug``, refusing to overwrite an existing skill."""

    if store.get_skill_by_slug(slug):
        raise SkillError(f"A skill called '{slug}' is already installed.")
    directory = skills_root() / slug
    if directory.exists():
        # Registered nowhere but present on disk -- a half-finished install, or
        # a directory dropped in by hand. Either way it is not ours to clobber.
        raise SkillError(f"A directory for '{slug}' already exists; remove it and try again.")
    return directory


def _register(
    directory: Path,
    *,
    name: str,
    description: str,
    slug: str,
    enabled_by_default: bool,
    source_type: str,
    source_ref: str | None,
) -> dict:
    now = store.now_iso()
    return store.insert_skill(
        {
            "id": str(uuid.uuid4()),
            "slug": slug,
            "name": name,
            "description": description,
            "directory": str(directory),
            "enabled_by_default": enabled_by_default,
            "source_type": source_type,
            "source_ref": source_ref,
            "content_hash": _content_hash(directory),
            "created_at": now,
            "updated_at": now,
        }
    )


def install_from_form(
    *, name: str, description: str, instructions: str, enabled_by_default: bool = True
) -> dict:
    """A skill written in the panel, saved as a ``SKILL.md`` like any other.

    Written through the same parser it will later be read by, so a skill typed
    here and a skill fetched from GitHub cannot diverge in what they support.
    """

    slug = slugify(name)
    if not slug:
        raise SkillError("Give the skill a name with at least one letter or digit.")
    directory = _reserve_directory(slug)
    # Dumped by the YAML library rather than interpolated: a description with a
    # colon in it is ordinary English and must not become broken frontmatter.
    header = yaml.safe_dump(
        {"name": name, "description": description}, default_flow_style=False, sort_keys=False
    )
    document = f"---\n{header}---\n\n{instructions.strip()}\n"
    parsed_name, parsed_description, _ = parse_skill_md(document)
    directory.mkdir(parents=True)
    try:
        (directory / SKILL_FILE).write_text(document, encoding="utf-8")
        return _register(
            directory,
            name=parsed_name,
            description=parsed_description,
            slug=slug,
            enabled_by_default=enabled_by_default,
            source_type="ui",
            source_ref=None,
        )
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _copyable_files(source: Path) -> list[Path]:
    """The files a skill directory contributes, refusing anything oversized.

    Hidden entries are skipped: a folder someone points at may well be a git
    checkout, and ``.git`` is neither part of the skill nor small.
    """

    chosen: list[Path] = []
    total = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_symlink():
            # Copying the link would leave a skill pointing outside its own
            # directory, which is the one thing "copied in" is meant to prevent.
            continue
        if not path.is_file():
            continue
        total += path.stat().st_size
        chosen.append(path)
        if len(chosen) > MAX_SKILL_FILES:
            raise SkillError(
                f"A skill may hold at most {MAX_SKILL_FILES} files; "
                "point at the skill's own folder rather than a project."
            )
        if total > MAX_SKILL_BYTES:
            raise SkillError(
                f"A skill may hold at most {MAX_SKILL_BYTES // 1024} KB; "
                "point at the skill's own folder rather than a project."
            )
    return chosen


def install_from_folder(raw_path: str, *, enabled_by_default: bool = True) -> dict:
    """Copy a skill in from a directory on this machine."""

    try:
        source = validate_repo_root(raw_path)
    except ValueError as exc:
        # The shared validator speaks about repositories because that is its
        # usual caller. The rules are the ones we want; only the noun is wrong.
        raise SkillError(str(exc).replace("Repository", "Skill folder")) from exc

    name, description, _ = read_skill_md(source)
    slug = slugify(name)
    directory = _reserve_directory(slug)
    files = _copyable_files(source)

    directory.mkdir(parents=True)
    try:
        for path in files:
            target = directory / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        # Re-read from where it now lives rather than trusting the copy: this is
        # what proves SKILL.md survived the file caps above.
        read_skill_md(directory)
        return _register(
            directory,
            name=name,
            description=description,
            slug=slug,
            enabled_by_default=enabled_by_default,
            source_type="folder",
            source_ref=str(source),
        )
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def parse_github_url(url: str) -> tuple[str, str, str | None, str]:
    """``(owner, repo, ref, path)`` from the forms people actually paste."""

    text = url.strip()
    match = _GITHUB_URL.match(text)
    if match:
        return (
            match.group("owner"),
            match.group("repo").removesuffix(".git"),
            match.group("ref"),
            (match.group("path") or "").strip("/"),
        )
    match = _GITHUB_SHORTHAND.match(text)
    if match:
        return (
            match.group("owner"),
            match.group("repo").removesuffix(".git"),
            None,
            (match.group("path") or "").strip("/"),
        )
    raise SkillError(
        "Paste a GitHub folder URL, such as "
        "https://github.com/owner/repo/tree/main/skills/my-skill"
    )


def _github_get(url: str, *, params: dict[str, Any] | None = None) -> Any:
    try:
        response = requests.get(
            url,
            params=params,
            headers={"Accept": "application/vnd.github+json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise SkillError(f"Could not reach GitHub: {exc}") from exc
    if response.status_code == 404:
        raise SkillError("GitHub has nothing at that path. Check the URL and the branch.")
    if response.status_code == 403:
        raise SkillError("GitHub refused the request -- most likely its hourly rate limit.")
    if not response.ok:
        raise SkillError(f"GitHub answered {response.status_code} for that path.")
    try:
        return response.json()
    except ValueError as exc:
        raise SkillError("GitHub returned something that is not a folder listing.") from exc


def _github_files(
    owner: str, repo: str, ref: str | None, path: str
) -> dict[str, str]:
    """``{relative path: download url}`` for a skill folder, subdirectories included.

    Recursive because a skill is often a ``SKILL.md`` plus a ``references/`` or
    ``agents/`` folder it points at, and a copy missing those is a skill whose
    instructions reference files that are not there -- worse than refusing it,
    because nothing looks wrong until a run follows the link. Bounded by the
    same caps a local folder gets, counted across the whole tree.
    """

    found: dict[str, str] = {}
    total = 0
    # Breadth-first over an explicit queue rather than recursion, so the caps
    # below are checked against the running total across every directory.
    queue = [(path, "")]
    while queue:
        current, prefix = queue.pop(0)
        listing = _github_get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{current}",
            params={"ref": ref} if ref else None,
        )
        if isinstance(listing, dict):
            raise SkillError("That URL points at a file. Give the folder that holds SKILL.md.")
        if not isinstance(listing, list):
            raise SkillError("GitHub returned something that is not a folder listing.")
        for item in listing:
            name = str(item.get("name") or "")
            if name.startswith("."):
                continue
            relative = f"{prefix}{name}"
            if item.get("type") == "dir":
                queue.append((f"{current}/{name}".strip("/"), f"{relative}/"))
                continue
            if item.get("type") != "file" or not item.get("download_url"):
                continue
            total += int(item.get("size") or 0)
            found[relative] = str(item["download_url"])
            if len(found) > MAX_SKILL_FILES:
                raise SkillError(f"A skill may hold at most {MAX_SKILL_FILES} files.")
            if total > MAX_SKILL_BYTES:
                raise SkillError(f"A skill may hold at most {MAX_SKILL_BYTES // 1024} KB.")
    return found


def install_from_github(url: str, *, enabled_by_default: bool = True) -> dict:
    """Fetch a skill folder from a public GitHub repository.

    Only regular files directly under the named folder are taken, under the same
    caps a local folder gets. The origin and a content hash are recorded so the
    panel can say where a skill came from -- which is the whole reason to care,
    since a skill body is instructions a model will follow.
    """

    owner, repo, ref, path = parse_github_url(url)
    entries = _github_files(owner, repo, ref, path)
    if SKILL_FILE not in entries:
        raise SkillError(f"That folder has no {SKILL_FILE}.")

    downloaded: dict[str, bytes] = {}
    for relative, download_url in entries.items():
        try:
            response = requests.get(download_url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SkillError(f"Could not download {relative}: {exc}") from exc
        downloaded[relative] = response.content

    try:
        name, description, _ = parse_skill_md(downloaded[SKILL_FILE].decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SkillError(f"{SKILL_FILE} is not valid UTF-8 text.") from exc

    slug = slugify(name)
    directory = _reserve_directory(slug)
    directory.mkdir(parents=True)
    try:
        for relative, blob in downloaded.items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        return _register(
            directory,
            name=name,
            description=description,
            slug=slug,
            enabled_by_default=enabled_by_default,
            source_type="github",
            source_ref=f"{owner}/{repo}{('@' + ref) if ref else ''}/{path}".rstrip("/"),
        )
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def remove(skill: dict) -> None:
    """Forget a skill, and delete the copy Neo made of it.

    The row goes first. A directory that will not delete must not leave a skill
    listed in the panel that no run can load.
    """

    store.delete_skill(skill["id"])
    directory = Path(skill["directory"])
    if directory.is_absolute() and directory.parent == skills_root():
        shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "MAX_SKILL_BYTES",
    "MAX_SKILL_FILES",
    "SKILL_FILE",
    "body_text",
    "install_from_folder",
    "install_from_form",
    "install_from_github",
    "parse_github_url",
    "parse_skill_md",
    "read_skill_md",
    "remove",
    "skills_root",
    "slugify",
]
