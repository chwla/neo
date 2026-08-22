from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.core.paths import is_within, same_path
from app.services.files.types import WorkspaceFile
from app.services.repos import store
from app.services.repos.safety import in_container, validate_browse_path, validate_live_root
from app.services.repos.service import RepoWorkspaceService
from app.services.repos.types import RepoFile, RepoRegisterRequest, RepoStats, WorkspaceRepo

router = APIRouter(prefix="/repos", tags=["repos"])


def _service() -> RepoWorkspaceService:
    return RepoWorkspaceService()


def _stats(repo: dict) -> RepoStats:
    metadata = repo.get("metadata", {})
    return RepoStats(
        file_count=repo["file_count"],
        indexed_file_count=repo["indexed_file_count"],
        total_bytes=repo["total_bytes"],
        ignored_files=metadata.get("ignored_files", 0),
        ignored_dirs=metadata.get("ignored_dirs", 0),
        unsupported_files=metadata.get("unsupported_files", 0),
    )


@router.post("/register", status_code=201)
def register_repo(request: RepoRegisterRequest) -> dict:
    try:
        repo = _service().register(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo": WorkspaceRepo.model_validate(repo), "stats": _stats(repo)}


#: Enough to pick from without turning the dialog into a file manager. A root
#: holding more than this is not a project folder, and paging it would only make
#: that harder to notice.
_MAX_LISTED_FOLDERS = 200


def _folders_under(root: str) -> list[dict]:
    """The immediate subdirectories of one directory, for the folder browser.

    One level deep and directories only: this answers "what is in here?", and the
    browser asks it again for each level the user descends into. Linked entries
    are skipped for the same reason ``validate_repo_root`` refuses them.

    Dotted entries are listed like any other. ``.dotfiles``, ``.vscode`` and
    ``.config`` are ordinary folders to a coding agent, and hiding them by name
    would make a real project unreachable -- there is no typed path field left to
    fall back on.
    """

    from pathlib import Path

    from app.core.paths import is_reparse_point

    base = Path(root).expanduser()
    if not base.is_dir():
        return []
    found: list[dict] = []
    try:
        children = sorted(base.iterdir(), key=lambda entry: entry.name.lower())
    except OSError:
        return []
    for child in children:
        if len(found) >= _MAX_LISTED_FOLDERS:
            break
        try:
            if not child.is_dir() or is_reparse_point(child):
                continue
            is_git = (child / ".git").exists()
        except OSError:
            continue
        found.append({"name": child.name, "path": str(child), "is_git": is_git})
    return found


def _configured_roots() -> list[str]:
    from app.core.config import get_settings
    from app.core.paths import split_roots

    return [part for part in split_roots(get_settings().workspace_live_roots) if part.strip()]


def _live_repos_by_path() -> dict[str, str]:
    """Live repositories keyed by the folder they were opened from."""

    repos, _total = store.list_repos(limit=200)
    return {
        repo["original_path"]: repo["id"]
        for repo in repos
        if repo.get("access") == store.LIVE
    }


@router.get("/roots")
def list_roots() -> dict:
    """Where a folder may be opened from, and which ones were opened recently."""

    repos, _total = store.list_repos(limit=200)
    recent = [
        {"path": repo["original_path"], "name": repo["name"], "repo_id": repo["id"]}
        for repo in repos
        if repo.get("access") == store.LIVE
    ]
    return {
        "roots": _configured_roots(),
        "recent": recent[:10],
        "containerized": in_container(),
    }


@router.get("/browse")
def browse_folders(path: str | None = None) -> dict:
    """List the directories under a configured root, one level at a time.

    The typed path field this replaces asked a question the user could not
    answer: inside a container the only real paths are the mounted ones, and
    nothing in the browser reveals them. The server knows them, so it lists them.

    ``can_attach`` is decided by running the real attach check against the
    current directory rather than by re-deriving the rules here. That keeps one
    source of truth -- a root reports itself as browse-only because
    ``validate_live_root`` refuses it, not because this route knows about roots.
    """

    from pathlib import Path

    roots = _configured_roots()
    target: Path | None = None

    if path is None:
        # One root is the ordinary case, and making the user click through a
        # single-item list to reach it is a step for nothing.
        if len(roots) == 1:
            try:
                target = validate_browse_path(roots[0])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        try:
            target = validate_browse_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    attached = _live_repos_by_path()

    if target is None:
        # Either nothing is configured, or several roots are -- both are shown as
        # a list of roots rather than a directory listing.
        entries = [
            {
                "name": root,
                "path": root,
                "is_git": False,
                "attached_repo_id": attached.get(root),
            }
            for root in roots
        ]
        return {
            "path": None,
            "parent": None,
            "roots": roots,
            "entries": entries,
            "can_attach": False,
            "attach_blocked_reason": (
                "No workspace root is configured." if not roots else "Choose a workspace root."
            ),
            "containerized": in_container(),
        }

    entries = [
        {**folder, "attached_repo_id": attached.get(folder["path"])}
        for folder in _folders_under(str(target))
    ]

    can_attach = True
    blocked: str | None = None
    try:
        validate_live_root(str(target))
    except ValueError as exc:
        can_attach = False
        blocked = str(exc)

    # A root has no parent the browser may show: navigating above it would leave
    # the configured tree, which is the one thing this endpoint must not permit.
    at_root = any(same_path(target, Path(root).expanduser().resolve()) for root in roots)
    parent = None if at_root else str(target.parent)

    return {
        "path": str(target),
        "parent": parent,
        "roots": roots,
        "entries": entries,
        "can_attach": can_attach,
        "attach_blocked_reason": blocked,
        "containerized": in_container(),
    }


@router.get("")
def list_repos(
    project_id: str | None = None,
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    repos, total = store.list_repos(
        project_id=project_id,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return {"repos": [WorkspaceRepo.model_validate(item) for item in repos], "total": total}


@router.get("/{repo_id}")
def read_repo(repo_id: str) -> dict:
    try:
        repo = _service().get(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"repo": WorkspaceRepo.model_validate(repo), "stats": _stats(repo)}


@router.get("/{repo_id}/files")
def list_repo_files(
    repo_id: str,
    q: str | None = None,
    extension: str | None = None,
    language: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        _service().get(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    items, total = store.list_repo_files(
        repo_id,
        q=q,
        extension=extension,
        language=language,
        limit=limit,
        offset=offset,
    )
    return {"files": [RepoFile.model_validate(item) for item in items], "total": total}


@router.get("/{repo_id}/files/{repo_file_id}")
def read_repo_file(repo_id: str, repo_file_id: str) -> dict:
    try:
        mapping, file_item = _service().get_file(repo_id, repo_file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "repo_file": RepoFile.model_validate(mapping),
        "file": WorkspaceFile.model_validate(file_item),
    }


@router.delete("/{repo_id}", status_code=204)
def delete_repo(repo_id: str) -> None:
    try:
        _service().soft_delete(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
