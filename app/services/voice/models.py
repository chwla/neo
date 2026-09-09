"""Which transcription models may be downloaded, and getting one onto the disk.

The registry is an allowlist, and that is its main job. ``faster_whisper`` resolves a
bare name like ``small`` to a Hugging Face repository, but it will equally accept any
repository id or filesystem path a caller hands it -- so a download endpoint that
forwarded the browser's string would let anyone with a session pull arbitrary code and
data onto the machine, and write it wherever they liked. Nothing outside this table is
installable.

Sizes are approximate on purpose: they exist to set an expectation before somebody
commits to a download, and quoting them from a table works offline, where asking the
network how big the file is does not.
"""

from __future__ import annotations

import io
import shutil
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from tqdm.auto import tqdm


# Deliberately short. A long menu of models is a research tool, not a dictation
# setting, and every extra entry is one more configuration somebody has to have an
# opinion about before they can talk to their computer.
@dataclass(frozen=True)
class VoiceModel:
    id: str
    label: str
    detail: str
    approx_bytes: int
    english_only: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "detail": self.detail,
            "approx_bytes": self.approx_bytes,
            "approx_mb": round(self.approx_bytes / (1024 * 1024)),
            "english_only": self.english_only,
        }


CATALOG: tuple[VoiceModel, ...] = (
    VoiceModel(
        id="small",
        label="Standard",
        detail="The best balance of accuracy and speed. Understands many languages.",
        approx_bytes=484 * 1024 * 1024,
        english_only=False,
    ),
    VoiceModel(
        id="small.en",
        label="Standard (English only)",
        detail="Slightly more accurate than Standard if you only ever dictate English.",
        approx_bytes=484 * 1024 * 1024,
        english_only=True,
    ),
    VoiceModel(
        id="base.en",
        label="Compact (English only)",
        detail="A third of the size and noticeably faster, at some cost in accuracy.",
        approx_bytes=145 * 1024 * 1024,
        english_only=True,
    ),
    VoiceModel(
        id="tiny.en",
        label="Minimal (English only)",
        detail="For older machines. Expect mistakes on names and technical words.",
        approx_bytes=75 * 1024 * 1024,
        english_only=True,
    ),
)

DEFAULT_MODEL_ID = "small"

_BY_ID = {model.id: model for model in CATALOG}


def get(model_id: str) -> VoiceModel | None:
    """The catalogue entry, or None. The only way a model id becomes trusted."""

    return _BY_ID.get((model_id or "").strip())


def is_allowed(model_id: str) -> bool:
    return get(model_id) is not None


def directory_for(root: Path, model_id: str) -> Path:
    """Where huggingface_hub puts this model under ``root``.

    Derived from the catalogue id rather than from anything a caller sent, so the path
    cannot be steered outside the model root.
    """

    return root / f"models--Systran--faster-whisper-{model_id}"


def installed_bytes(root: Path, model_id: str) -> int:
    """How much of this model is on disk. Zero when none of it is."""

    directory = directory_for(root, model_id)
    if not directory.is_dir():
        return 0
    # Symlinks are skipped rather than followed. The hub cache stores each file once
    # under blobs/ and links to it from snapshots/, so counting both reports twice the
    # real size -- which would drive the progress bar to 100% at the half way point.
    return sum(
        item.stat().st_size
        for item in directory.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def is_installed(root: Path, model_id: str) -> bool:
    """Whether the model is usable, not merely whether a folder exists.

    A download interrupted half way leaves a directory full of ``.incomplete`` files;
    treating that as installed would turn a network blip into a permanently broken
    microphone that reports itself ready. The weights file is what decides.
    """

    directory = directory_for(root, model_id)
    if not directory.is_dir():
        return False
    weights = [
        item
        for item in directory.rglob("model.bin")
        if item.is_file() and not item.name.endswith(".incomplete")
    ]
    if not weights:
        return False
    # A stub or a truncated file is not a model. The smallest catalogue entry is 75 MB,
    # so anything under a megabyte is certainly a failed download.
    return any(item.stat().st_size > 1024 * 1024 for item in weights)


def remove(root: Path, model_id: str) -> bool:
    """Delete a model, including the wreckage of a failed download."""

    directory = directory_for(root, model_id)
    if not directory.is_dir():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


# -- downloading -------------------------------------------------------------

# One download at a time, tracked per model so a second click is answered with the
# progress of the first rather than starting a competing download of the same file.
_active: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


class AlreadyDownloading(RuntimeError):
    """A download of this model is already running."""


def active_state(model_id: str) -> dict[str, Any] | None:
    with _lock:
        state = _active.get(model_id)
        return dict(state) if state else None


def cancel(model_id: str) -> bool:
    """Ask a running download to stop.

    Aborting the browser's request does not stop it: the download runs in a server-side
    thread that knows nothing about that connection, so it needs its own signal -- the
    same reason the local-model installer carries a cancel endpoint.
    """

    with _lock:
        state = _active.get(model_id)
        if not state:
            return False
        state["cancel"].set()
        return True


class _ProgressTqdm(tqdm):
    """A real tqdm that draws nowhere and reports bytes to a shared counter.

    Subclassing rather than duck-typing is deliberate. ``huggingface_hub``'s xet
    backend calls a wide and undocumented slice of the tqdm API -- ``set_postfix_str``,
    ``format_dict``, ``set_transfer_postfix_str`` -- and a hand-written stand-in fails
    on whichever one it has not implemented yet, differently in each release. Inheriting
    gets the whole surface for free and leaves exactly one method to override.

    Output goes to a throwaway buffer rather than being disabled, because a disabled
    tqdm short-circuits ``update`` and would report nothing at all.

    This exists because polling the destination directory does not work: with the xet
    backend the bytes are staged outside the model folder and only appear at the end,
    so a 484 MB download sat at 0% for two minutes and then jumped to done -- exactly
    the hung-looking screen that streaming progress is meant to prevent.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._counter = kwargs.pop("_counter", None)
        kwargs["file"] = io.StringIO()
        kwargs["leave"] = False
        kwargs["disable"] = False
        super().__init__(*args, **kwargs)
        total = kwargs.get("total") or 0
        # Only the per-file bars are summed. The xet backend also opens aggregate bars
        # measured in the same bytes, and counting both would halve the reported
        # percentage.
        if self._counter is not None and total and kwargs.get("unit") == "B":
            with self._counter["lock"]:
                self._counter["total"] += total

    def update(self, n: float = 1) -> Any:
        if self._counter is not None and self.unit == "B":
            with self._counter["lock"]:
                self._counter["done"] += n or 0
        return super().update(n)


# The files a CTranslate2 Whisper model actually consists of. Mirrors what
# faster-whisper asks for; kept here because this calls snapshot_download directly in
# order to get progress, which faster-whisper's own wrapper disables.
_ALLOW_PATTERNS = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]

# Resolved from the catalogue id, never from caller input.
_REPO_PREFIX = "Systran/faster-whisper-"


def download(root: Path, model_id: str) -> Iterator[dict[str, Any]]:
    """Fetch a model, yielding progress events until it is usable."""

    model = get(model_id)
    if model is None:
        yield {"type": "error", "message": f"'{model_id}' is not a model Neo can install."}
        return

    if is_installed(root, model_id):
        yield {"type": "done", "message": f"{model.label} is ready.", "percent": 100}
        return

    cancel_event = threading.Event()
    with _lock:
        if model_id in _active:
            yield {
                "type": "error",
                "code": "already_downloading",
                "message": "That download is already running.",
            }
            return
        _active[model_id] = {"cancel": cancel_event, "percent": 0}

    counter: dict[str, Any] = {"done": 0, "total": 0, "lock": threading.Lock()}
    result: dict[str, Any] = {}

    def work() -> None:
        try:
            from huggingface_hub import snapshot_download

            snapshot_download(
                f"{_REPO_PREFIX}{model_id}",
                cache_dir=str(root),
                allow_patterns=_ALLOW_PATTERNS,
                tqdm_class=partial(_ProgressTqdm, _counter=counter),
            )
            result["ok"] = True
        except Exception as exc:  # reported to the user, not raised into the stream
            result["error"] = str(exc)

    try:
        root.mkdir(parents=True, exist_ok=True)
        yield {"type": "progress", "message": f"Downloading {model.label}", "percent": 0}

        thread = threading.Thread(target=work, name=f"voice-download-{model_id}", daemon=True)
        thread.start()

        last = -1
        while thread.is_alive():
            thread.join(timeout=0.4)
            if cancel_event.is_set():
                break
            with counter["lock"]:
                done, total = counter["done"], counter["total"]
            # Fall back to the catalogue's estimate until the first bar exists, so the
            # bar moves from the first chunk rather than after the first file.
            denominator = total or model.approx_bytes
            percent = min(99, int(done * 100 / denominator)) if denominator else 0
            if percent != last:
                last = percent
                with _lock:
                    if model_id in _active:
                        _active[model_id]["percent"] = percent
                yield {
                    "type": "progress",
                    "message": f"Downloading {model.label}",
                    "percent": percent,
                    "downloaded_bytes": int(done),
                    "total_bytes": int(denominator),
                }

        if cancel_event.is_set():
            # The worker cannot be interrupted mid-request, so the partial download is
            # cleaned up instead -- leaving it would make the model look installed to a
            # later check that only asked whether the directory existed.
            remove(root, model_id)
            yield {"type": "cancelled", "message": "Download cancelled."}
            return

        thread.join()
        if result.get("error"):
            remove(root, model_id)
            yield {"type": "error", "message": _friendly_error(result["error"])}
            return

        if not is_installed(root, model_id):
            remove(root, model_id)
            yield {
                "type": "error",
                "message": "The download finished but the model is incomplete. Try again.",
            }
            return

        yield {
            "type": "done",
            "message": f"{model.label} is ready.",
            "percent": 100,
            "installed_bytes": installed_bytes(root, model_id),
        }
    finally:
        with _lock:
            _active.pop(model_id, None)


def _friendly_error(detail: str) -> str:
    """A sentence somebody can act on, rather than a traceback.

    The raw text is kept out of the interface on purpose: it is usually a stack of
    library internals, and the two causes that actually happen -- no network, no disk
    space -- are worth naming plainly.
    """

    lowered = detail.lower()
    if "connection" in lowered or "network" in lowered or "resolve" in lowered:
        return "Could not reach the download server. Check your connection and try again."
    if "space" in lowered or "disk" in lowered:
        return "There is not enough free disk space for this model."
    return "The download failed. Try again."
