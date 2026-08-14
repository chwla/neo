"""Tier 6 — the derived-state health and maintenance routes (plan section HLT).

These three routes are the operator's controls for reconstructible state: report
coverage, reconcile drift, rebuild from canonical. They are the only memory
routes that can do bulk work, so the interesting properties are the refusals —
who may call them, what they will not accept as input, and what they say when
the machinery underneath fails.

`_maintenance_for_profile` re-derives the owner binding from the database's own
migration ledger and refuses if it disagrees with the caller's profile. That is
the isolation guarantee here, and it is checked before any work starts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.models.chat  # noqa: F401  - register mappings before create_all
import app.models.memory  # noqa: F401
import app.models.project  # noqa: F401
from app.api.routes import memory_health as health_routes
from app.core.config import get_settings
from app.db.base import Base
from app.main import create_app
from app.services import profile_accounts
from app.services.memory import factory
from app.services.memory.settings import MemorySettings
from tests.memory.conftest import OTHER_OWNER_ID, OWNER_ID

PROFILE_ID = "guest-1"
HEALTH = "/api/memory/health"
RECONCILE = "/api/memory/derived/reconcile"
REBUILD = "/api/memory/derived/rebuild"


def _call(client: TestClient, method: str, path: str):
    """GET takes no body; POST needs one. Keeps the route tables parametrisable."""

    if method == "get":
        return client.get(path)
    return client.post(path, json={})


class StubEmbeddingProvider:
    """Stands in for `OllamaEmbeddingProvider` so no test opens a socket.

    `_maintenance_for_profile` builds a real provider and hands `provider.health`
    to maintenance, which calls it during `coverage()`. That is an HTTP GET to
    the configured Ollama endpoint — so without this, two tests in this file
    reached out to `127.0.0.1:11434`, and their result depended on whether a
    model server happened to be running.

    They still *passed* either way, because the provider swallows connection
    errors and reports unhealthy. That is the dangerous shape: green on the
    error path, and no signal that the intended path was never taken.
    """

    # `ValidatedMemoryEmbeddingProvider` reads both off the wrapped provider and
    # refuses to construct without them.
    provider_name = "stub"

    def __init__(self, *, model_name: str = "stub-embed", base_url: str = "", timeout: float = 1.0):
        self.model_name = model_name
        self.base_url = base_url
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        return [0.1] * 8

    def health(self) -> bool:
        return True


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A guest profile whose database lives entirely under tmp_path."""

    factory._verified_memory_schemas.clear()
    monkeypatch.setattr(health_routes, "OllamaEmbeddingProvider", StubEmbeddingProvider)
    root = tmp_path / "neo-data"
    root.mkdir()
    base = get_settings().model_copy(update={"data_dir": str(root)})
    monkeypatch.setattr(profile_accounts, "get_base_settings", lambda: base)

    guest_dir = root / "profiles" / "guests" / PROFILE_ID
    guest_dir.mkdir(parents=True)
    (guest_dir / "owner_id").write_text(OWNER_ID)

    record = {"id": PROFILE_ID, "owner_id": OWNER_ID, "is_guest": True}
    monkeypatch.setattr(health_routes, "session_for", lambda request: record)
    yield record
    factory._verified_memory_schemas.clear()


@pytest.fixture
def client(profile):
    application = create_app()
    with profile_accounts.profile_database(PROFILE_ID, guest=True):
        from app.db.session import SessionLocal

        session = SessionLocal()
        Base.metadata.create_all(session.get_bind())
        session.close()
        # The health routes read the memory migration ledger directly, so the
        # schema has to exist before the first request rather than being created
        # lazily by a runtime build.
        from app.db.memory_migrations import upgrade_memory

        engine_session = SessionLocal()
        upgrade_memory(
            engine_session.get_bind(),
            owner_id=OWNER_ID,
            database_identity=profile_accounts.database_identity_for_profile(
                PROFILE_ID, guest=True
            ),
        )
        engine_session.close()
        with TestClient(application) as opened:
            yield opened


class TestAuthorization:
    @pytest.mark.parametrize(
        ("method", "path"),
        [("get", HEALTH), ("post", RECONCILE), ("post", REBUILD)],
    )
    def test_every_route_requires_a_profile(
        self, client: TestClient, monkeypatch, method: str, path: str
    ) -> None:
        """HLT-02 — these can rebuild an entire index, so anonymous is never enough."""

        monkeypatch.setattr(health_routes, "session_for", lambda request: None)

        response = _call(client, method, path)

        assert response.status_code == 401
        assert response.json()["detail"] == "authenticated_profile_required"

    @pytest.mark.parametrize(
        ("method", "path"),
        [("get", HEALTH), ("post", RECONCILE), ("post", REBUILD)],
    )
    def test_a_disabled_flag_hides_every_route(
        self, client: TestClient, monkeypatch, method: str, path: str
    ) -> None:
        """HLT-01 — 404, not 403: a disabled control should not advertise itself.

        `health_routes_enabled` is currently hardcoded True in `from_settings`,
        so the only way to exercise this is to substitute the flags. The guard is
        real and worth pinning even though nothing sets it today — see
        `decisions.md` 58.
        """

        monkeypatch.setattr(
            health_routes.MemorySettings,
            "from_settings",
            staticmethod(lambda _settings: MemorySettings(health_routes_enabled=False)),
        )

        response = _call(client, method, path)

        assert response.status_code == 404
        assert response.json()["detail"] == "memory_health_routes_disabled"

    @pytest.mark.parametrize(
        ("method", "path"),
        [("get", HEALTH), ("post", RECONCILE), ("post", REBUILD)],
    )
    def test_a_profile_without_an_owner_is_refused(
        self, client: TestClient, monkeypatch, method: str, path: str
    ) -> None:
        """A blank owner cannot be scoped to anything, so it is treated as disabled."""

        monkeypatch.setattr(
            health_routes,
            "session_for",
            lambda request: {"id": PROFILE_ID, "owner_id": "", "is_guest": True},
        )

        response = _call(client, method, path)

        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("method", "path"),
        [("post", RECONCILE), ("post", REBUILD)],
    )
    def test_disabling_reconciliation_hides_the_mutating_routes(
        self, client: TestClient, monkeypatch, method: str, path: str
    ) -> None:
        """The read stays available when the write half is switched off."""

        monkeypatch.setattr(
            health_routes.MemorySettings,
            "from_settings",
            staticmethod(lambda _settings: MemorySettings(reconciliation_enabled=False)),
        )

        response = _call(client, method, path)

        assert response.status_code == 404
        assert response.json()["detail"] == "memory_reconciliation_disabled"


class TestCoverage:
    def test_health_reports_coverage(self, client: TestClient) -> None:
        """HLT-03"""

        response = client.get(HEALTH)

        assert response.status_code == 200
        body = response.json()
        # Named fields rather than "the body is non-empty": the point of this
        # route is that an operator can see per-target drift, and a truthiness
        # check would pass on any dict at all.
        assert body["owner_id"] == OWNER_ID
        assert body["canonical_active_eligible_count"] == 0
        for field in (
            "fts_current_count",
            "fts_missing_count",
            "fts_stale_count",
            "vector_current_count",
            "vector_missing_count",
            "vector_stale_count",
            "pending_outbox_count",
        ):
            assert body[field] == 0, field

    def test_the_health_route_is_not_shadowed_by_the_record_route(
        self, client: TestClient
    ) -> None:
        """`GET /memory/health` and `GET /memory/{memory_id}` both match this path.

        The health router is mounted first in `create_app`, which is the only
        reason `health` is not parsed as a memory id. Asserting it here means a
        reordering of `include_router` calls fails a test rather than quietly
        turning this route into a 422 on an invalid UUID.
        """

        response = client.get(HEALTH)

        assert response.status_code == 200


class TestReconcile:
    def test_a_bounded_reconcile_runs(self, client: TestClient) -> None:
        """HLT-04"""

        response = client.post(RECONCILE, json={"dry_run": True, "limit": 10})

        assert response.status_code == 200
        assert isinstance(response.json(), dict)

    @pytest.mark.parametrize(
        "checkpoint",
        [
            "not-a-checkpoint",
            "v1:bad",
            "'; DROP TABLE memory_records; --",
            "00000000-0000-4000-8000",
            "v1:-:-",
        ],
    )
    def test_a_malformed_checkpoint_is_rejected_by_the_contract(
        self, client: TestClient, checkpoint: str
    ) -> None:
        """HLT-05 — the checkpoint is caller-supplied and reaches a query builder.

        Rejecting at the pydantic pattern means a malformed value never becomes a
        cursor, and the SQL-shaped case is included deliberately: this is the one
        free-text field on these routes.
        """

        response = client.post(RECONCILE, json={"checkpoint": checkpoint})

        assert response.status_code == 422

    @pytest.mark.parametrize(
        "checkpoint",
        [
            "11111111-1111-4111-8111-111111111111",
            "v1:-:-:-",
            "v1:11111111-1111-4111-8111-111111111111:-:!",
        ],
    )
    def test_well_formed_checkpoints_are_accepted(
        self, client: TestClient, checkpoint: str
    ) -> None:
        """HLT-06 — the UUID token pattern, exercised through the shapes it composes.

        The plan describes this as validating "the owner token". There is no
        owner token on the wire: `_UUID_TOKEN_PATTERN` exists only as a component
        of the checkpoint grammar, so that is where it is tested.
        """

        response = client.post(RECONCILE, json={"checkpoint": checkpoint})

        assert response.status_code == 200

    @pytest.mark.parametrize("limit", [0, 1_001])
    def test_the_limit_is_bounded(self, client: TestClient, limit: int) -> None:
        """An unbounded reconcile is a full table scan on the user's machine."""

        assert client.post(RECONCILE, json={"limit": limit}).status_code == 422

    def test_unknown_fields_are_rejected(self, client: TestClient) -> None:
        assert client.post(RECONCILE, json={"unexpected": True}).status_code == 422


class TestRebuild:
    def test_a_rebuild_runs_and_reports(self, client: TestClient) -> None:
        """HLT-07"""

        response = client.post(REBUILD, json={})

        assert response.status_code == 200
        assert isinstance(response.json(), dict)


class TestOwnerScoping:
    @pytest.mark.parametrize(
        ("method", "path", "detail"),
        [
            ("get", HEALTH, "memory_health_health_unavailable"),
            ("post", RECONCILE, "memory_reconciliation_unavailable"),
            ("post", REBUILD, "memory_rebuild_unavailable"),
        ],
    )
    def test_a_binding_mismatch_refuses_before_any_work(
        self, client: TestClient, monkeypatch, method: str, path: str, detail: str
    ) -> None:
        """HLT-08 — the ledger is re-read and compared to the caller's profile.

        A profile claiming an owner the database was not bound to is the
        cross-profile case. `_maintenance_for_profile` raises before building any
        index object, so nothing is read or written on the way to the refusal.
        """

        monkeypatch.setattr(
            health_routes,
            "session_for",
            lambda request: {"id": PROFILE_ID, "owner_id": OTHER_OWNER_ID, "is_guest": True},
        )

        response = _call(client, method, path)

        assert response.status_code == 503
        # The refusal reason is deliberately not disclosed: the caller learns the
        # route is unavailable, not that they named an owner the database rejects.
        assert response.json()["detail"] == detail
        assert "binding" not in response.text
        assert OTHER_OWNER_ID not in response.text


class TestFailureSurface:
    @pytest.mark.parametrize(
        ("method", "path", "detail"),
        [
            ("get", HEALTH, "memory_health_health_unavailable"),
            ("post", RECONCILE, "memory_reconciliation_unavailable"),
            ("post", REBUILD, "memory_rebuild_unavailable"),
        ],
    )
    def test_a_maintenance_failure_is_a_clean_503(
        self, client: TestClient, monkeypatch, method: str, path: str, detail: str
    ) -> None:
        """HLT-09 — a fixed code, and nothing from the exception.

        Maintenance touches SQL and an embedding provider; both produce
        exceptions whose text can contain paths, queries, or provider responses.
        The route replaces all of it with one stable code.
        """

        def _explode(*args, **kwargs):
            raise RuntimeError("sqlite error: /Users/someone/private/path/neo.db is locked")

        monkeypatch.setattr(health_routes, "_maintenance_for_profile", _explode)

        response = _call(client, method, path)

        assert response.status_code == 503
        assert response.json()["detail"] == detail
        assert "sqlite" not in response.text
        assert "private/path" not in response.text
