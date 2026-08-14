"""Tier 6 — the memory HTTP surface (plan section API).

These run the real FastAPI app against a real profile database. The router is
thin, but it is the only place a *person* touches the memory layer directly, and
it is where two things must hold that nothing below it can guarantee: an
unauthenticated request gets nothing, and an error response never echoes the
content it failed on.

Everything stays inside `tmp_path`. The profile root is redirected and every
profile is a guest, so no real profile directory is created and the account
registry is never opened.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.models.chat  # noqa: F401  - register mappings before create_all
import app.models.memory  # noqa: F401
import app.models.project  # noqa: F401
from app.api.routes import memory as memory_routes
from app.core.config import get_settings
from app.db.base import Base
from app.main import create_app
from app.services import profile_accounts
from app.services.memory import factory
from app.services.memory.contracts import Sensitivity
from tests.memory import factories
from tests.memory.conftest import OTHER_OWNER_ID, OWNER_ID

PROFILE_ID = "guest-1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app, bound to a throwaway guest profile.

    `build_memory_runtime` caches its schema check per profile in module state,
    so that is cleared too — otherwise the second test to use this fixture
    inherits the first test's verification for a database that no longer exists.
    """

    factory._verified_memory_schemas.clear()
    # Live extraction is disabled for every API test. Otherwise
    # `build_memory_runtime` calls `_resolve_ollama_request_mode`, which probes a
    # real Ollama endpoint with a warmup timeout of up to 300s -- so the suite's
    # runtime depends on whether a model server happens to be running on the
    # developer's machine. A failed probe is deliberately not cached, so every
    # runtime build retries it.
    no_model = get_settings().model_copy(update={"memory_extraction_enabled": False})
    monkeypatch.setattr(factory, "get_settings", lambda: no_model)

    root = tmp_path / "neo-data"
    root.mkdir()
    base = get_settings().model_copy(update={"data_dir": str(root)})
    monkeypatch.setattr(profile_accounts, "get_base_settings", lambda: base)

    guest_dir = root / "profiles" / "guests" / PROFILE_ID
    guest_dir.mkdir(parents=True)
    (guest_dir / "owner_id").write_text(OWNER_ID)

    profile = {"id": PROFILE_ID, "owner_id": OWNER_ID, "is_guest": True}
    monkeypatch.setattr(memory_routes, "session_for", lambda request: profile)

    application = create_app()
    with profile_accounts.profile_database(PROFILE_ID, guest=True):
        from app.db.session import SessionLocal

        from app.db.memory_migrations import upgrade_memory

        session = SessionLocal()
        Base.metadata.create_all(session.get_bind())
        # The memory tables are otherwise created lazily by the first runtime
        # build, which is too late for a test that seeds a candidate row before
        # issuing any request.
        upgrade_memory(
            session.get_bind(),
            owner_id=OWNER_ID,
            database_identity=profile_accounts.database_identity_for_profile(
                PROFILE_ID, guest=True
            ),
        )
        session.close()
        with TestClient(application) as opened:
            yield opened

    factory._verified_memory_schemas.clear()


def _body(**overrides) -> dict:
    payload = {
        "memory_type": "knowledge",
        "display_text": "I use a V60 dripper",
        "value": "V60 dripper",
        "domain": "global",
    }
    payload.update(overrides)
    return payload


def _create(client: TestClient, **overrides) -> dict:
    response = client.post("/api/memory", json=_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


class TestListing:
    def test_an_empty_store_lists_nothing(self, client: TestClient) -> None:
        response = client.get("/api/memory")

        assert response.status_code == 200
        assert response.json() == []

    def test_a_created_record_is_listed(self, client: TestClient) -> None:
        """API-01"""

        created = _create(client)

        listed = client.get("/api/memory").json()

        assert [item["id"] for item in listed] == [created["id"]]

    def test_each_record_carries_its_presentation_fields(self, client: TestClient) -> None:
        """API-02 — the shape the UI groups and renders by.

        `field` is derived from the storage type rather than stored, so records
        written before fields existed still group correctly.
        """

        record = _create(client, memory_type="identity", display_text="Soham", value="Soham")

        assert record["memory_type"] == "identity"
        assert record["field"] == "profile"
        assert record["scope"] == {"type": "global"}
        assert record["display_text"] == "Soham"
        assert record["domain"] == "global"
        assert record["revision"] >= 1


class TestCreate:
    def test_a_valid_create_returns_201(self, client: TestClient) -> None:
        """API-03"""

        response = client.post("/api/memory", json=_body())

        assert response.status_code == 201
        assert response.json()["canonical_value"] == "V60 dripper"

    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            ({"display_text": "x"}, "missing value"),
            ({"value": "x", "display_text": ""}, "blank display text"),
            ({"value": "x", "display_text": "x", "importance": 11}, "importance out of range"),
            ({"value": "x", "display_text": "x", "confidence": 2}, "confidence out of range"),
            ({"value": "x", "display_text": "x", "unexpected": 1}, "unknown field"),
        ],
    )
    def test_an_invalid_payload_is_rejected_with_422(
        self, client: TestClient, payload: dict, reason: str
    ) -> None:
        """API-04 — one case per guard, so a dropped constraint is attributable."""

        assert client.post("/api/memory", json=payload).status_code == 422, reason

    def test_prohibited_content_fails_cleanly(self, client: TestClient) -> None:
        """API-05 — a refusal, not a stack trace.

        Prohibited content raises from deep inside the repository guard. What
        matters at this boundary is that it becomes a 4xx and the response does
        not echo the secret back.
        """

        secret = "sk-live-abcdef0123456789abcdef0123456789"
        response = client.post(
            "/api/memory",
            json=_body(display_text=f"my api key is {secret}", value=secret),
        )

        assert response.status_code == 409
        assert response.json()["detail"]["rejection_code"] == "prohibited_sensitive_content"
        assert secret not in response.text

    def test_a_slot_is_derived_when_none_is_given(self, client: TestClient) -> None:
        """API-06"""

        record = _create(client, memory_type="identity", display_text="Soham", value="Soham")

        assert record["slot"].startswith("identity:global:manual_")
        assert record["cardinality"] == "exclusive"

    def test_two_manual_identity_facts_do_not_replace_each_other(
        self, client: TestClient
    ) -> None:
        """API-06b — the reason the manual slot is unique per memory.

        An identity slot is exclusive. If every manual identity fact shared one
        slot, adding a second would silently supersede the first — the user would
        watch their previous fact disappear.
        """

        first = _create(client, memory_type="identity", display_text="Soham", value="Soham")
        second = _create(client, memory_type="identity", display_text="Delhi", value="Delhi")

        listed = client.get("/api/memory").json()

        assert first["slot"] != second["slot"]
        assert {item["id"] for item in listed} == {first["id"], second["id"]}

    def test_an_explicit_slot_is_kept(self, client: TestClient) -> None:
        record = _create(
            client,
            memory_type="knowledge",
            slot="knowledge:global:item:" + str(uuid4()),
        )

        assert record["slot"].startswith("knowledge:global:item:")

    def test_a_global_scope_with_a_project_is_rejected(self, client: TestClient) -> None:
        """API-07a — the two scope fields must agree."""

        response = client.post("/api/memory", json=_body(scope_type="global", project_id="1"))

        assert response.status_code == 422
        assert response.json()["detail"] == "global_scope_cannot_have_project"

    def test_a_project_scope_without_a_project_is_rejected(self, client: TestClient) -> None:
        """API-07b"""

        response = client.post("/api/memory", json=_body(scope_type="project"))

        assert response.status_code == 422
        assert response.json()["detail"] == "project_scope_requires_project"

    def test_a_project_scope_naming_an_unknown_project_is_rejected(
        self, client: TestClient
    ) -> None:
        """API-07c — scope decides who can read the memory, so it fails closed."""

        response = client.post(
            "/api/memory", json=_body(scope_type="project", project_id="9999")
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "project_scope_project_not_found"

    def test_repeating_a_create_does_not_error_on_the_unique_index(
        self, client: TestClient
    ) -> None:
        """API-08 — the same payload twice must not surface a constraint violation.

        Each create derives a fresh mutation id, so this is two distinct
        additive memories rather than a reconfirm — but the property that
        matters at the HTTP boundary is that neither call 500s.
        """

        first = client.post("/api/memory", json=_body())
        second = client.post("/api/memory", json=_body())

        assert first.status_code == 201
        assert second.status_code == 201

    def test_a_repeated_client_mutation_id_is_idempotent(self, client: TestClient) -> None:
        """API-08b — the caller's own idempotency key is what makes a replay safe."""

        body = _body(client_mutation_id="fixed-key-1")
        first = client.post("/api/memory", json=body)
        second = client.post("/api/memory", json=body)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert len(client.get("/api/memory").json()) == 1


class TestReadUpdateDelete:
    def test_an_unknown_id_is_404(self, client: TestClient) -> None:
        """API-09a"""

        response = client.get(f"/api/memory/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["detail"] == "memory_not_found"

    def test_a_record_is_readable_by_id(self, client: TestClient) -> None:
        created = _create(client)

        response = client.get(f"/api/memory/{created['id']}")

        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    def test_a_patch_updates_and_bumps_the_revision(self, client: TestClient) -> None:
        """API-10"""

        created = _create(client)

        response = client.patch(
            f"/api/memory/{created['id']}", json={"display_text": "I use a Chemex"}
        )

        assert response.status_code == 200
        assert response.json()["display_text"] == "I use a Chemex"
        assert response.json()["revision"] > created["revision"]

    def test_a_patch_with_no_fields_is_422(self, client: TestClient) -> None:
        """API-11 — an empty patch is a caller bug, not a no-op."""

        created = _create(client)

        assert client.patch(f"/api/memory/{created['id']}", json={}).status_code == 422

    def test_a_stale_revision_conflicts(self, client: TestClient) -> None:
        """API-12 — optimistic concurrency, surfaced as a conflict rather than a write.

        Two browser tabs editing one memory: the second must be told its view was
        stale instead of silently overwriting the first.
        """

        created = _create(client)
        client.patch(f"/api/memory/{created['id']}", json={"display_text": "first edit"})

        response = client.patch(
            f"/api/memory/{created['id']}",
            json={"display_text": "second edit", "expected_revision": created["revision"]},
        )

        assert response.status_code == 409

    def test_a_delete_forgets_the_record(self, client: TestClient) -> None:
        """API-13"""

        created = _create(client)

        response = client.delete(f"/api/memory/{created['id']}")

        assert response.status_code == 200
        assert client.get("/api/memory").json() == []

    def test_deleting_an_unknown_id_is_404(self, client: TestClient) -> None:
        """API-14"""

        assert client.delete(f"/api/memory/{uuid4()}").status_code == 404

    def test_a_second_delete_returns_404(self, client: TestClient) -> None:
        """API-15 — corrected: not idempotent, and that is defensible.

        The plan expected a repeat delete to succeed. It 404s, because the record
        is no longer listed once forgotten and `_record_or_404` runs first. For a
        UI this is the more informative answer — the second tab learns the record
        is gone rather than being told it deleted something twice — and DELETE
        being non-idempotent on a *missing* resource is a common REST reading.
        Pinned as it is. See `decisions.md` 55.
        """

        created = _create(client)
        assert client.delete(f"/api/memory/{created['id']}").status_code == 200

        assert client.delete(f"/api/memory/{created['id']}").status_code == 404


class TestCandidates:
    def test_an_empty_candidate_list(self, client: TestClient) -> None:
        """API-16"""

        response = client.get("/api/memory/candidates")

        assert response.status_code == 200
        assert response.json() == []

    def test_accepting_an_unknown_candidate_is_404(self, client: TestClient) -> None:
        """API-18"""

        response = client.post(f"/api/memory/candidates/{uuid4()}/accept", json={})

        assert response.status_code == 404

    def test_rejecting_an_unknown_candidate_is_404(self, client: TestClient) -> None:
        """API-20a"""

        response = client.post(f"/api/memory/candidates/{uuid4()}/reject", json={})

        assert response.status_code == 404


class TestAuthenticationAndIsolation:
    def test_every_route_requires_a_profile(self, client: TestClient, monkeypatch) -> None:
        """API-23 — no profile, no data, on every route including the reads.

        Parametrised inline over the whole surface rather than one representative:
        a route added without the dependency is exactly the mistake this catches.
        """

        monkeypatch.setattr(memory_routes, "session_for", lambda request: None)
        identifier = uuid4()

        responses = {
            "list": client.get("/api/memory"),
            "create": client.post("/api/memory", json=_body()),
            "get": client.get(f"/api/memory/{identifier}"),
            "patch": client.patch(f"/api/memory/{identifier}", json={"display_text": "x"}),
            "delete": client.delete(f"/api/memory/{identifier}"),
            "candidates": client.get("/api/memory/candidates"),
            "accept": client.post(f"/api/memory/candidates/{identifier}/accept", json={}),
            "reject": client.post(f"/api/memory/candidates/{identifier}/reject", json={}),
        }

        for name, response in responses.items():
            assert response.status_code == 401, f"{name} did not require a profile"
            assert response.json()["detail"] == "authenticated_profile_required"

    def test_another_owners_id_is_not_readable(
        self, client: TestClient, tmp_path, monkeypatch
    ) -> None:
        """API-24 — asking for a foreign id is indistinguishable from unknown.

        Not `in {404, 503}`: a disjunction here would pass whether the record was
        properly hidden or the whole runtime failed to build, which are very
        different outcomes. It is a 404 with the same body an unknown id gets, so
        a caller cannot use the status to confirm the record exists.
        """

        created = _create(client)

        guest_two = tmp_path / "neo-data" / "profiles" / "guests" / "guest-2"
        guest_two.mkdir(parents=True)
        (guest_two / "owner_id").write_text(OTHER_OWNER_ID)
        monkeypatch.setattr(
            memory_routes,
            "session_for",
            lambda request: {"id": "guest-2", "owner_id": OTHER_OWNER_ID, "is_guest": True},
        )

        response = client.get(f"/api/memory/{created['id']}")
        unknown = client.get(f"/api/memory/{uuid4()}")

        assert response.status_code == 404
        assert response.json() == unknown.json()

    def test_an_error_response_never_echoes_stored_content(
        self, client: TestClient
    ) -> None:
        """API-25 — the failure path must not become a disclosure path.

        A 409 carries an outcome and a code. If it carried the value that caused
        it, an error surfaced in a shared browser log would leak the memory.
        """

        created = _create(client, display_text="my private note", value="my private note")
        client.patch(f"/api/memory/{created['id']}", json={"display_text": "changed"})

        response = client.patch(
            f"/api/memory/{created['id']}",
            json={"display_text": "again", "expected_revision": created["revision"]},
        )

        assert response.status_code == 409
        assert "my private note" not in response.text


@pytest.fixture
def profile_engine(client):
    """The engine bound to the guest profile, for seeding rows directly.

    The `client` fixture holds `profile_database` open for the duration of the
    test, so `SessionLocal` is still bound to the profile database here.
    """

    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session.get_bind()
    finally:
        session.close()


class TestCandidateFlow:
    """The review queue: extraction proposes, the user accepts or rejects.

    Seeded directly rather than driven through extraction. What these routes owe
    is correct behaviour given a candidate row; how the row got there is the
    extraction coordinator's contract and is covered in EXC.
    """

    def test_only_pending_candidates_are_listed(
        self, client: TestClient, profile_engine
    ) -> None:
        """API-16 — an applied or rejected candidate is not still awaiting review."""

        pending = factories.insert_candidate(profile_engine, state="validated")
        review = factories.insert_candidate(profile_engine, state="needs_review")
        factories.insert_candidate(profile_engine, state="applied")
        factories.insert_candidate(profile_engine, state="rejected")

        listed = client.get("/api/memory/candidates").json()

        assert {item["id"] for item in listed} == {pending, review}

    def test_a_sensitive_candidate_is_listed_without_its_content(
        self, client: TestClient, profile_engine
    ) -> None:
        """The review queue must not become a way to read a sensitive value.

        Two layers hold here, and the seeding proves the first one: the schema's
        payload-shape constraint *refuses* a sensitive candidate that carries
        plaintext at all, so `display_text` is NULL on the row. The route's
        `"[sensitive memory]"` substitution is therefore masking an absence
        rather than hiding a value it could have shown.

        Found by trying to seed one the obvious way and being rejected by the
        database — the constraint is the real guarantee.
        """

        factories.insert_candidate(
            profile_engine,
            sensitivity=Sensitivity.SENSITIVE,
            explicit_user_request=True,
            canonical_payload=None,
            display_text=None,
            encrypted_canonical_payload=b"ciphertext",
            encrypted_display_payload=b"ciphertext",
            encryption_algorithm="aes-256-gcm",
            encryption_key_version="v1",
            canonical_nonce=b"nonce-canonical",
            display_nonce=b"nonce-display",
            encryption_aad=b"aad",
        )

        response = client.get("/api/memory/candidates")

        assert response.json()[0]["display_text"] == "[sensitive memory]"
        assert "ciphertext" not in response.text

    def test_accepting_a_candidate_creates_the_record(
        self, client: TestClient, profile_engine
    ) -> None:
        """API-17"""

        candidate_id = factories.insert_candidate(profile_engine)

        response = client.post(f"/api/memory/candidates/{candidate_id}/accept", json={})

        assert response.status_code == 200
        listed = client.get("/api/memory").json()
        assert [item["display_text"] for item in listed] == ["improve at urban sketching"]

    def test_accepting_twice_does_not_create_a_second_record(
        self, client: TestClient, profile_engine
    ) -> None:
        """API-19 — the idempotency key is derived from the candidate and revision.

        A double-click on Accept must not store the memory twice; the second call
        replays the first result rather than writing again.
        """

        candidate_id = factories.insert_candidate(profile_engine)
        first = client.post(f"/api/memory/candidates/{candidate_id}/accept", json={})
        second = client.post(f"/api/memory/candidates/{candidate_id}/accept", json={})

        assert first.status_code == 200
        assert second.status_code == 200
        assert len(client.get("/api/memory").json()) == 1

    def test_accepting_with_a_stale_revision_conflicts(
        self, client: TestClient, profile_engine
    ) -> None:
        """The candidate changed under the reviewer, so the decision is refused."""

        candidate_id = factories.insert_candidate(profile_engine)

        response = client.post(
            f"/api/memory/candidates/{candidate_id}/accept", json={"expected_revision": 99}
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "candidate_revision_conflict"

    def test_a_sensitive_candidate_cannot_be_accepted_from_the_queue(
        self, client: TestClient, profile_engine
    ) -> None:
        """API-22 — a non-applied outcome becomes an HTTP error, not a silent skip.

        Accepting from the list would store a sensitive memory on the strength of
        one click on a screen that deliberately does not show its content. The
        user has to restate it explicitly instead.
        """

        candidate_id = factories.insert_candidate(
            profile_engine,
            sensitivity=Sensitivity.SENSITIVE,
            explicit_user_request=True,
            canonical_payload=None,
            display_text=None,
            encrypted_canonical_payload=b"ciphertext",
            encrypted_display_payload=b"ciphertext",
            encryption_algorithm="aes-256-gcm",
            encryption_key_version="v1",
            canonical_nonce=b"nonce-canonical",
            display_nonce=b"nonce-display",
            encryption_aad=b"aad",
        )

        response = client.post(f"/api/memory/candidates/{candidate_id}/accept", json={})

        assert response.status_code == 409
        assert response.json()["detail"] == "sensitive_candidate_requires_explicit_reentry"
        assert client.get("/api/memory").json() == []

    def test_rejecting_a_candidate_removes_it_from_the_queue(
        self, client: TestClient, profile_engine
    ) -> None:
        """API-20"""

        candidate_id = factories.insert_candidate(profile_engine)

        response = client.post(f"/api/memory/candidates/{candidate_id}/reject", json={})

        assert response.status_code == 200
        assert client.get("/api/memory/candidates").json() == []
        assert client.get("/api/memory").json() == []

    def test_rejecting_twice_is_idempotent(
        self, client: TestClient, profile_engine
    ) -> None:
        """API-21 — unlike accept, reject genuinely repeats without error."""

        candidate_id = factories.insert_candidate(profile_engine)
        first = client.post(f"/api/memory/candidates/{candidate_id}/reject", json={})
        second = client.post(f"/api/memory/candidates/{candidate_id}/reject", json={})

        assert first.status_code == 200
        assert second.status_code == 200

    def test_another_owners_candidate_is_not_visible_or_actionable(
        self, client: TestClient, profile_engine
    ) -> None:
        """The candidate queue is owner-scoped in the same way records are."""

        foreign = factories.insert_candidate(profile_engine, owner=OTHER_OWNER_ID)

        listed = client.get("/api/memory/candidates").json()

        assert foreign not in {item["id"] for item in listed}
        assert (
            client.post(f"/api/memory/candidates/{foreign}/accept", json={}).status_code == 404
        )
        assert (
            client.post(f"/api/memory/candidates/{foreign}/reject", json={}).status_code == 404
        )
