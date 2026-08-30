from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread

import requests

from app.core.config import get_settings
from app.db.memory_migrations import upgrade_memory
from app.db.session import build_engine
from app.services.memory.adapters import (
    ChatMemoryAdapter,
    GenericMemoryAdapter,
    MemoryAdapterContext,
)
from app.services.memory.contracts import ActorKind, EvidenceSpan, SourceKind
from app.services.memory.coordinator import MemoryExecutionContext, MemoryMutationCoordinator
from app.services.memory.extraction import (
    OllamaCapabilities,
    OllamaRequestMode,
    build_extraction_model_provider,
    probe_ollama_provider,
)
from app.services.memory.extraction_coordinator import MemoryExtractionCoordinator
from app.services.memory.local_crypto import LocalMemoryCrypto
from app.services.memory.runtime import build_semantic_duplicate_finder
from app.services.memory.settings import MemorySettings
from app.services.profile_accounts import (
    database_identity_for_profile,
    database_url_for,
    memory_key_material_for_profile,
)


@dataclass(frozen=True)
class MemoryRuntime:
    profile: dict
    settings: MemorySettings
    coordinator: MemoryMutationCoordinator
    adapter: GenericMemoryAdapter
    chat_adapter: ChatMemoryAdapter
    extraction: MemoryExtractionCoordinator

    @property
    def execution(self) -> MemoryExecutionContext:
        profile_id = str(self.profile["id"])
        guest = bool(self.profile.get("is_guest"))
        return MemoryExecutionContext(
            owner_id=str(self.profile["owner_id"]),
            database_identity=database_identity_for_profile(profile_id, guest=guest),
            database_url=database_url_for(profile_id, guest=guest),
            profile_id=profile_id,
            is_guest=guest,
            is_incognito=bool(self.profile.get("incognito")),
            memory_enabled=self.settings.enabled,
        )

    def context(
        self,
        *,
        source_kind: SourceKind,
        source_id: str | None,
        request_id: str,
        conversation_id: str | None = None,
        message_id: str | None = None,
        session_id: str | None = None,
        observed_at: datetime | None = None,
        evidence: tuple[EvidenceSpan, ...] = (),
    ) -> MemoryAdapterContext:
        return MemoryAdapterContext(
            execution=self.execution,
            actor_kind=ActorKind.USER,
            actor_id=str(self.profile["id"]),
            source_kind=source_kind,
            source_id=source_id,
            request_id=request_id,
            session_id=session_id,
            conversation_id=conversation_id,
            message_id=message_id,
            observed_at=observed_at,
            evidence=evidence,
        )


_verified_memory_schemas: set[tuple[str, str, str]] = set()
_verified_memory_schemas_lock = Lock()

_EXTRACTION_LOG = logging.getLogger("neo.memory.extraction")
_negotiated_ollama_modes: dict[tuple[str, str], tuple[OllamaRequestMode, object | None]] = {}
_negotiated_ollama_lock = Lock()
#: Endpoint/model pairs whose probe is already running, so a burst of turns
#: starts one background negotiation between them rather than one each.
_probing_ollama_keys: set[tuple[str, str]] = set()


def _capability_cache_path() -> Path | None:
    """Where the negotiated mode is remembered between processes.

    Resolved the same way the profile store is (``profile_accounts._root``):
    ``data_dir`` when set, otherwise beside the configured SQLite database.
    ``data_dir`` alone is not enough -- it is unset in an ordinary local run,
    which would leave the cache with nowhere to write and the probe running
    on every restart exactly as before.

    ``None`` only when there is genuinely nowhere durable, in which case the
    in-process cache is the only one and behaviour is what it was.
    """
    settings = get_settings()
    directory: Path | None = None
    if settings.data_dir:
        directory = Path(settings.data_dir).expanduser()
    else:
        database_url = settings.database_url or ""
        if database_url.startswith("sqlite:///"):
            directory = Path(database_url.removeprefix("sqlite:///")).expanduser().parent
    if directory is None:
        return None
    # Resolved so a relative database URL (the default) does not scatter the
    # cache wherever the process happens to be started from, and kept beside
    # the profile store rather than loose in the working tree.
    try:
        directory = (directory.resolve() / "profiles").expanduser()
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return directory / "ollama_capabilities.json"


def _ollama_build_id(endpoint: str) -> str:
    """The Ollama build the answer was negotiated against, best effort.

    A rebuilt or upgraded server can genuinely accept different formats, so
    the version is part of the cache key. An unreachable server returns "" --
    which simply means the stored answer is reused, and reusing a slightly
    stale mode is a far smaller problem than re-running the probe.
    """
    # The configured endpoint is the chat URL itself, so the version lives a
    # level up -- asking `<endpoint>/api/version` always 404s and silently
    # recorded an empty build, which would never invalidate on an upgrade.
    root = endpoint.rstrip("/")
    for suffix in ("/api/chat", "/api/generate"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    try:
        response = requests.get(f"{root}/api/version", timeout=2)
        response.raise_for_status()
        return str(response.json().get("version") or "")
    except Exception:
        return ""


def _read_persisted_mode(
    key: tuple[str, str],
) -> tuple[OllamaRequestMode, object | None] | None:
    path = _capability_cache_path()
    if path is None or not path.exists():
        return None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        entry = stored[f"{key[0]}|{key[1]}"]
        # Only a build we can actually read, and that actually differs,
        # invalidates the entry. An unreadable version is the *expected*
        # answer while a probe is saturating the server, and treating that as
        # staleness started another probe -- a loop that kept every turn slow.
        current = _ollama_build_id(key[0])
        recorded = entry.get("build")
        if current and recorded and recorded != current:
            return None
        mode = OllamaRequestMode(entry["mode"])
        fields = entry.get("capabilities")
        return mode, (OllamaCapabilities(**fields) if fields else None)
    except Exception:
        # A corrupt or unreadable cache must never be worse than no cache.
        return None


def _write_persisted_mode(
    key: tuple[str, str], resolved: tuple[OllamaRequestMode, object | None]
) -> None:
    path = _capability_cache_path()
    if path is None:
        return
    mode, capabilities = resolved
    try:
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(stored, dict):
            stored = {}
        stored[f"{key[0]}|{key[1]}"] = {
            "mode": mode.value,
            "build": _ollama_build_id(key[0]),
            "capabilities": asdict(capabilities) if is_dataclass(capabilities) else None,
        }
        path.write_text(json.dumps(stored, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        # Losing the cache costs one probe next time; failing the turn costs
        # the user their message.
        _EXTRACTION_LOG.debug("ollama_capability_cache_write_failed", exc_info=True)


def _resolve_ollama_request_mode(
    settings: MemorySettings,
) -> tuple[OllamaRequestMode, object | None]:
    """Choose the request format the configured Ollama build actually accepts.

    ``auto`` previously collapsed to ``ollama_schema`` and the capability probe
    was never run, so a server that rejects JSON-schema ``format`` (returning
    HTTP 400 ``failed to parse grammar``) failed *every* model-backed extraction
    silently.  Probe once per endpoint/model and reuse the answer; an explicitly
    configured mode is still honoured exactly as written.

    The probe itself is slow enough that *when* it runs matters as much as
    whether it does, so it never runs here: the first caller gets JSON mode --
    the same answer an inconclusive probe yields -- and the real negotiation
    happens on a background thread, persisted so it is paid once per machine
    rather than once per process.
    """

    configured = settings.ollama_request_mode
    if configured == "ollama_json":
        return OllamaRequestMode.JSON, None
    if configured == "ollama_schema":
        return OllamaRequestMode.SCHEMA, None
    if settings.extraction_provider != "ollama":
        return OllamaRequestMode.SCHEMA, None

    key = (settings.extraction_endpoint, settings.extraction_model)
    cached = _negotiated_ollama_modes.get(key)
    if cached is not None:
        return cached
    with _negotiated_ollama_lock:
        existing = _negotiated_ollama_modes.get(key)
        if existing is not None:
            return existing
        # Six sequential model calls, a 300s warmup budget, and this lock
        # held throughout. A stack dump of a stalled server found exactly
        # this on the critical path of a bare "hi", with every concurrent
        # turn queued behind it.
        #
        # Nothing about answering the user depends on the result: it decides
        # how a *later* memory extraction frames its request, and JSON mode
        # is already the documented fallback whenever the probe is
        # inconclusive. So the caller gets that fallback now and the
        # negotiation happens off the reply path, once per machine.
        persisted = _read_persisted_mode(key)
        if persisted is not None:
            _negotiated_ollama_modes[key] = persisted
            return persisted
        should_start = key not in _probing_ollama_keys
        if should_start:
            _probing_ollama_keys.add(key)
    if should_start:
        _start_background_probe(settings, key)
    return OllamaRequestMode.JSON, None


def _start_background_probe(settings: MemorySettings, key: tuple[str, str]) -> None:
    """Negotiate the extraction format without anyone waiting on the answer."""

    def run() -> None:
        resolved: tuple[OllamaRequestMode, object | None] | None = None
        try:
            probe = probe_ollama_provider(
                settings.extraction_endpoint,
                model=settings.extraction_model,
                requested_mode=OllamaRequestMode.AUTO,
                connect_timeout_seconds=settings.extraction_connect_timeout_seconds,
                response_timeout_seconds=settings.extraction_response_timeout_seconds,
                warmup_timeout_seconds=settings.extraction_warmup_timeout_seconds,
            )
        except Exception:
            # An unreachable provider must not permanently pin a mode, so
            # this result is deliberately neither cached nor persisted.
            _EXTRACTION_LOG.exception("memory_extraction_capability_probe_failed")
        else:
            if probe.selected_request_mode is None:
                _EXTRACTION_LOG.warning(
                    "memory_extraction_capability_probe_inconclusive code=%s",
                    probe.sanitized_failure_code,
                )
            else:
                resolved = (probe.selected_request_mode, probe.capabilities)
                _EXTRACTION_LOG.info(
                    "memory_extraction_mode_negotiated mode=%s schema=%s json=%s",
                    probe.selected_request_mode.value,
                    probe.capabilities.schema_format_supported,
                    probe.capabilities.json_format_supported,
                )
        # Persist before clearing the in-flight marker, so "no longer
        # probing" means the answer is durable rather than merely decided --
        # otherwise a restart in that window probes all over again.
        if resolved is not None:
            _write_persisted_mode(key, resolved)
        with _negotiated_ollama_lock:
            _probing_ollama_keys.discard(key)
            if resolved is not None:
                _negotiated_ollama_modes[key] = resolved

    Thread(target=run, name="neo-ollama-capability-probe", daemon=True).start()



def _ensure_memory_schema(database_url: str, owner_id: str, database_identity: str) -> None:
    """Run the memory migration check once per process for each profile database.

    A runtime is built several times per chat turn, and each build otherwise
    created an engine and opened a write-capable connection to the very database
    the chat worker is writing to.  The schema cannot change underneath a running
    process, so verifying it once removes both the latency and that contention.
    The key includes the owner binding, so a database reached with a different
    identity is still validated rather than silently accepted.
    """

    key = (database_url, owner_id, database_identity)
    if key in _verified_memory_schemas:
        return
    with _verified_memory_schemas_lock:
        if key in _verified_memory_schemas:
            return
        engine = build_engine(database_url)
        try:
            upgrade_memory(
                engine,
                owner_id=owner_id,
                database_identity=database_identity,
            )
        finally:
            engine.dispose()
        _verified_memory_schemas.add(key)


def build_memory_runtime(profile: dict) -> MemoryRuntime:
    profile_id = str(profile["id"])
    guest = bool(profile.get("is_guest"))
    settings = MemorySettings.from_settings(get_settings())
    database_url = database_url_for(profile_id, guest=guest)
    owner_id = str(profile["owner_id"])
    database_identity = database_identity_for_profile(profile_id, guest=guest)
    _ensure_memory_schema(database_url, owner_id, database_identity)
    crypto = LocalMemoryCrypto(seed=memory_key_material_for_profile(profile_id, guest=guest))
    coordinator = MemoryMutationCoordinator(
        flags=settings,
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
    )
    generic = GenericMemoryAdapter(coordinator)
    chat = ChatMemoryAdapter(coordinator)
    model = None
    if settings.extraction_enabled and settings.live_extraction_model_enabled:
        request_mode, capabilities = _resolve_ollama_request_mode(settings)
        model = build_extraction_model_provider(
            settings.extraction_provider,
            settings.extraction_endpoint,
            model=settings.extraction_model,
            connect_timeout_seconds=settings.extraction_connect_timeout_seconds,
            response_timeout_seconds=settings.extraction_response_timeout_seconds,
            ollama_request_mode=request_mode,
            ollama_capabilities=capabilities,
            two_stage=settings.two_stage_extraction_enabled,
        )
    extraction = MemoryExtractionCoordinator(
        chat,
        model=model,
        duplicate_finder=build_semantic_duplicate_finder(
            database_url=database_url,
            owner_id=owner_id,
            database_identity=database_identity,
            flags=settings,
            settings=get_settings(),
        ),
        duplicate_threshold=get_settings().memory_semantic_duplicate_threshold,
    )
    return MemoryRuntime(profile, settings, coordinator, generic, chat, extraction)
