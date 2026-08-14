"""Tier 6 — the workspace retrieval and context-compaction surfaces (RTV / CTX).

These are the older subsystems that predate the canonical memory layer. They ship
in the app with their own routers and their own SQLite tables, and the plan asks
for a working-order pass rather than the exhaustive treatment the canonical layer
gets.

**They do not use the profile database.** `store._connect()` reads
`get_settings().database_url` directly, which is the application database — the
`neo_memory.db` at the repository root in a normal checkout. Every test here
redirects that to a file under `tmp_path`; without it, running the suite would
write test rows into the developer's real database.

The property worth the most attention is redaction, because it is the only thing
standing between a pasted transcript and a credential stored on disk.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.context_memory import store as context_store
from app.services.context_memory.redaction import redact, redact_text
from app.services.context_memory.token_budget import compression, estimate_tokens
from app.services.memory_retrieval import store as retrieval_store
from app.services.memory_retrieval.scorer import score

RETRIEVAL = "/api/workspace-memory"
CONTEXT = "/api/context-memory"


@pytest.fixture
def database(tmp_path, monkeypatch):
    """Point both subsystems at a throwaway SQLite file.

    `_connect()` resolves the path at call time from `get_settings()`, so both
    store modules are patched rather than the settings cache — the two read it
    independently.
    """

    path = tmp_path / "workspace.db"
    patched = get_settings().model_copy(update={"database_url": f"sqlite:///{path}"})
    monkeypatch.setattr(retrieval_store, "get_settings", lambda: patched)
    monkeypatch.setattr(context_store, "get_settings", lambda: patched)
    retrieval_store.initialize_memory_retrieval_tables()
    context_store.initialize_context_memory_tables()
    return path


@pytest.fixture
def client(database):
    with TestClient(create_app()) as opened:
        yield opened


def _item(client: TestClient, **overrides) -> dict:
    payload = {
        "scope_type": "chat",
        "scope_id": "chat-1",
        "title": "Coffee setup",
        "content_text": "I use a V60 dripper for pourover",
        "memory_type": "project_note",
    }
    payload.update(overrides)
    response = client.post(f"{RETRIEVAL}/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestItemCrud:
    def test_create_read_update_delete_round_trips(self, client: TestClient) -> None:
        """RTV-03"""

        created = _item(client)
        identifier = created["id"]

        assert client.get(f"{RETRIEVAL}/items/{identifier}").status_code == 200

        patched = client.patch(
            f"{RETRIEVAL}/items/{identifier}", json={"content_text": "Now a Chemex"}
        )
        assert patched.status_code == 200
        assert patched.json()["content_text"] == "Now a Chemex"

        assert client.delete(f"{RETRIEVAL}/items/{identifier}").status_code == 204
        assert client.get(f"{RETRIEVAL}/items/{identifier}").status_code == 404

    def test_renaming_an_item_succeeds_and_changes_the_title(
        self, client: TestClient
    ) -> None:
        """RTV-09 — fixed. Renaming is the most ordinary edit to a saved note.

        This returned 500: `update_item` handed the merged row to `upsert_item`,
        which decided insert-versus-update by re-deriving a content key that
        *includes the title*. A renamed item matched nothing, took the INSERT
        branch with its own id, and hit `UNIQUE constraint failed`.

        The row is now resolved by id when one is supplied. The second assertion
        matters as much as the first: the UPDATE statement did not set `title`,
        so routing a rename through it without that change would have returned
        200 and silently kept the old name.
        """

        identifier = _item(client)["id"]

        response = client.patch(
            f"{RETRIEVAL}/items/{identifier}", json={"title": "Renamed"}
        )

        assert response.status_code == 200
        assert response.json()["title"] == "Renamed"
        assert client.get(f"{RETRIEVAL}/items/{identifier}").json()["title"] == "Renamed"

    def test_renaming_does_not_create_a_second_row(self, client: TestClient) -> None:
        """The failure mode the id lookup replaced: a rename must not fork the row."""

        identifier = _item(client)["id"]

        client.patch(f"{RETRIEVAL}/items/{identifier}", json={"title": "Renamed"})

        listed = client.get(f"{RETRIEVAL}/items").json()
        assert listed["total"] == 1
        assert listed["items"][0]["id"] == identifier

    def test_an_unknown_item_is_404(self, client: TestClient) -> None:
        """RTV-04"""

        assert client.get(f"{RETRIEVAL}/items/does-not-exist").status_code == 404

    def test_patching_an_unknown_item_is_404(self, client: TestClient) -> None:
        response = client.patch(f"{RETRIEVAL}/items/does-not-exist", json={"title": "x"})

        assert response.status_code == 404

    def test_deleting_an_unknown_item_is_404(self, client: TestClient) -> None:
        assert client.delete(f"{RETRIEVAL}/items/does-not-exist").status_code == 404

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("scope_type", "not_a_scope"),
            ("memory_type", "not_a_type"),
            ("importance", 9),
            ("confidence", 2.0),
            ("title", ""),
        ],
    )
    def test_invalid_payloads_are_rejected(
        self, client: TestClient, field: str, value: object
    ) -> None:
        """The enum guards are the reason `scope_type` can be trusted downstream."""

        payload = {
            "scope_type": "chat",
            "scope_id": "chat-1",
            "title": "t",
            "content_text": "c",
            field: value,
        }

        assert client.post(f"{RETRIEVAL}/items", json=payload).status_code == 422


class TestScopeIsolation:
    def test_a_scope_listing_returns_only_that_scope(self, client: TestClient) -> None:
        """RTV-05 / RTV-12 — scope is the isolation boundary in this subsystem.

        There is no owner here; a scope id is what separates one conversation's
        memory from another's, so a leak across scopes is this subsystem's
        equivalent of a cross-owner read.
        """

        mine = _item(client, scope_id="chat-1", title="Mine")
        _item(client, scope_id="chat-2", title="Theirs")

        listed = client.get(f"{RETRIEVAL}/scopes/chat/chat-1").json()

        assert [item["id"] for item in listed["items"]] == [mine["id"]]
        assert listed["total"] == 1

    def test_listing_can_be_filtered_by_scope(self, client: TestClient) -> None:
        _item(client, scope_id="chat-1")
        _item(client, scope_id="chat-2")

        filtered = client.get(
            f"{RETRIEVAL}/items", params={"scope_type": "chat", "scope_id": "chat-2"}
        ).json()

        assert filtered["total"] == 1

    def test_retrieval_does_not_cross_scopes(self, client: TestClient) -> None:
        """RTV-02b / RTV-12 — fixed. One chat cannot retrieve another's items.

        This previously leaked: both scope guards required an item's `scope_type`
        *and* its `scope_id` to differ before skipping it, and every pair of chats
        shares `scope_type="chat"`, so neither guard fired. Each supplied field
        now filters independently.

        The leak was easy to miss because the scorer gives same-scope items a
        +0.22 bonus, so the correct result still ranked first — the foreign item
        appeared below it. Hence the assertion on the whole result set rather
        than on the top hit.
        """

        _item(client, scope_id="chat-1", title="V60", content_text="pourover dripper")
        _item(client, scope_id="chat-2", title="Espresso", content_text="pourover machine")

        response = client.post(
            f"{RETRIEVAL}/retrieve",
            json={"scope_type": "chat", "scope_id": "chat-1", "query": "pourover"},
        )

        assert response.status_code == 200
        titles = [result["title"] for result in response.json()["results"]]
        assert titles == ["V60"], "a sibling scope's item must not appear at all"

    def test_a_scope_type_filter_alone_still_matches_across_ids(
        self, client: TestClient
    ) -> None:
        """The two filters are independent, which is what makes them correct.

        Asking for every `chat` item without naming one is a legitimate query,
        and it must still span chats. Without this, changing the guards to an
        `or` over both fields would look equally correct.
        """

        _item(client, scope_id="chat-1", title="V60", content_text="pourover dripper")
        _item(client, scope_id="chat-2", title="Espresso", content_text="pourover machine")

        response = client.post(
            f"{RETRIEVAL}/retrieve",
            json={"scope_type": "chat", "query": "pourover"},
        )

        titles = {result["title"] for result in response.json()["results"]}
        assert titles == {"V60", "Espresso"}


class TestRetrieval:
    def test_retrieval_returns_scored_results(self, client: TestClient) -> None:
        """RTV-02b"""

        _item(client, content_text="V60 dripper for pourover coffee")

        response = client.post(
            f"{RETRIEVAL}/retrieve",
            json={"scope_type": "chat", "scope_id": "chat-1", "query": "pourover coffee"},
        )

        assert response.status_code == 200
        results = response.json()["results"]
        assert results
        assert 0.0 <= results[0]["score"] <= 1.0

    def test_a_retrieval_is_recorded_in_the_audit_log(self, client: TestClient) -> None:
        """RTV-11 — every read is auditable, which is the point of a memory store."""

        _item(client)
        before = client.get(f"{RETRIEVAL}/retrievals").json()["total"]

        client.post(
            f"{RETRIEVAL}/retrieve",
            json={"scope_type": "chat", "scope_id": "chat-1", "query": "coffee"},
        )

        after = client.get(f"{RETRIEVAL}/retrievals").json()
        assert after["total"] == before + 1

    @pytest.mark.parametrize("limit", [0, 301])
    def test_the_retrievals_limit_is_bounded(self, client: TestClient, limit: int) -> None:
        """RTV-06"""

        response = client.get(f"{RETRIEVAL}/retrievals", params={"limit": limit})

        assert response.status_code == 422

    @pytest.mark.parametrize("limit", [1, 300])
    def test_the_retrievals_limit_accepts_its_extremes(
        self, client: TestClient, limit: int
    ) -> None:
        assert client.get(f"{RETRIEVAL}/retrievals", params={"limit": limit}).status_code == 200

    def test_an_unknown_retrieval_is_404(self, client: TestClient) -> None:
        assert client.get(f"{RETRIEVAL}/retrievals/nope").status_code == 404


class TestScorer:
    """RTV-09 — ranking is deterministic and bounded to 0–1."""

    def _row(self, **overrides) -> dict:
        row = {
            "id": "1",
            "title": "V60 dripper",
            "content_text": "pourover coffee setup",
            "scope_type": "chat",
            "scope_id": "chat-1",
            "tags": ["coffee"],
            "importance": 3,
            "access_count": 0,
            "memory_type": "project_note",
            "updated_at": None,
        }
        row.update(overrides)
        return row

    def test_scoring_is_deterministic(self) -> None:
        first = score(self._row(), "pourover", scope_type="chat", scope_id="chat-1", tags=[])
        second = score(self._row(), "pourover", scope_type="chat", scope_id="chat-1", tags=[])

        assert first == second

    def test_the_total_never_exceeds_one(self) -> None:
        """Every component is capped, but the sum needs its own bound.

        Maximum importance, access and tag overlap together with a full keyword
        match would otherwise push the total past 1 and make scores from
        different queries incomparable.
        """

        maxed = self._row(importance=5, access_count=1000, tags=["a", "b", "c", "d"])
        result = score(
            maxed,
            "v60 dripper pourover coffee setup",
            scope_type="chat",
            scope_id="chat-1",
            tags=["a", "b", "c", "d"],
        )

        assert 0.0 <= result["score"] <= 1.0

    def test_a_scope_match_outranks_a_type_match(self) -> None:
        exact = score(self._row(), "", scope_type="chat", scope_id="chat-1", tags=[])
        same_type = score(self._row(), "", scope_type="chat", scope_id="other", tags=[])

        assert exact["score"] > same_type["score"]

    def test_an_unparseable_timestamp_is_treated_as_ancient(self) -> None:
        """A malformed date must not raise inside ranking."""

        result = score(
            self._row(updated_at="not-a-date"), "", scope_type=None, scope_id=None, tags=[]
        )

        assert result["score_breakdown"]["recency"] == 0.0


class TestRedaction:
    """RTV-10 / CTX-05 — the one thing that must not fail open.

    Both subsystems store free text a user pasted. A transcript containing an API
    key is the ordinary case, not the adversarial one.
    """

    @pytest.mark.parametrize(
        ("text", "marker"),
        [
            ("api_key: sk-live-abcdef0123456789", "[REDACTED_CREDENTIAL]"),
            ("password = hunter2", "[REDACTED_CREDENTIAL]"),
            ("Authorization: Bearer abc.def.ghi", "[REDACTED_CREDENTIAL]"),
            ("DATABASE_URL=postgres://user:pw@host/db", "[REDACTED_ENV]"),
            ("see /Users/someone/private/notes.txt", "[REDACTED_PATH]"),
        ],
    )
    def test_each_pattern_is_replaced(self, text: str, marker: str) -> None:
        redacted = redact_text(text)

        assert marker in redacted

    def test_the_secret_value_itself_is_gone(self) -> None:
        """Asserting the marker is present is not the same as the secret being absent."""

        redacted = redact_text("api_key: sk-live-abcdef0123456789")

        assert "sk-live-abcdef0123456789" not in redacted

    def test_keys_that_name_a_secret_are_dropped_entirely(self) -> None:
        """A dict key is not text, so it is removed rather than masked."""

        safe = redact({"api_key": "sk-live-1", "note": "fine"})

        assert "api_key" not in safe
        assert safe["note"] == "fine"

    def test_redaction_recurses_through_nested_structures(self) -> None:
        safe = redact({"outer": [{"inner": "password = hunter2"}]})

        assert "hunter2" not in str(safe)

    def test_ordinary_text_is_left_alone(self) -> None:
        """Over-redaction destroys the memory it was meant to protect."""

        assert redact_text("I use a V60 dripper") == "I use a V60 dripper"

    def test_stored_content_is_redacted_before_persistence(
        self, client: TestClient, database
    ) -> None:
        """RTV-10b — asserted against the database file, not the response.

        A response can be filtered on the way out while the raw value sits on
        disk. Reading the table directly is the only way to know which one
        happened.
        """

        _item(client, content_text="my api_key: sk-live-abcdef0123456789 is here")

        connection = sqlite3.connect(database)
        try:
            stored = " ".join(
                str(row)
                for row in connection.execute(
                    "SELECT title, content_text, content_json FROM workspace_memory_items"
                ).fetchall()
            )
        finally:
            connection.close()

        assert "sk-live-abcdef0123456789" not in stored


class TestPruning:
    def test_preview_reports_candidates_without_deleting(self, client: TestClient) -> None:
        """RTV-07 — a preview that mutated would make the confirmation meaningless."""

        _item(client)
        before = client.get(f"{RETRIEVAL}/items").json()["total"]

        preview = client.post(f"{RETRIEVAL}/prune/preview", json={"stale_days": 1})

        assert preview.status_code == 200
        assert client.get(f"{RETRIEVAL}/items").json()["total"] == before

    def test_apply_requires_confirmation(self, client: TestClient) -> None:
        """RTV-08b — a destructive default is the wrong default."""

        _item(client)

        response = client.post(
            f"{RETRIEVAL}/prune/apply", json={"stale_days": 1, "confirm": False}
        )

        assert response.status_code == 400
        assert client.get(f"{RETRIEVAL}/items").json()["total"] == 1

    def test_apply_removes_exactly_the_previewed_set(self, client: TestClient) -> None:
        """RTV-08"""

        _item(client)
        previewed = {
            item["id"]
            for item in client.post(
                f"{RETRIEVAL}/prune/preview", json={"stale_days": 1}
            ).json()["candidates"]
        }

        applied = client.post(
            f"{RETRIEVAL}/prune/apply", json={"stale_days": 1, "confirm": True}
        ).json()

        assert set(applied["deleted_ids"]) == previewed
        assert applied["deleted"] == len(previewed)

    def test_protected_types_are_named_in_the_preview(self, client: TestClient) -> None:
        """The contract the user is being asked to confirm."""

        preview = client.post(f"{RETRIEVAL}/prune/preview", json={"stale_days": 1}).json()

        assert preview["protected_types"] == ["user_instruction", "safety_note"]


class TestIndexing:
    """RTV-01 — `/index` sweeps existing sources, it does not accept an item.

    I first wrote this posting a title and content, which returned 200 and
    indexed nothing: `MemoryIndexRequest` carries only a scope and a list of
    source types, and the indexer pulls from other tables. The endpoint takes
    unknown fields without complaint, so the misuse is silent — worth knowing
    for anyone wiring a caller to it.
    """

    def _summary(self, client: TestClient) -> None:
        client.post(
            f"{CONTEXT}/scopes/chat/chat-1/events",
            json={"event_type": "decision", "content": {"text": "We chose the V60"}},
        )
        compacted = client.post(
            f"{CONTEXT}/compact", json={"scope_type": "chat", "scope_id": "chat-1"}
        )
        assert compacted.status_code == 200

    def test_a_context_summary_is_swept_into_retrieval(self, client: TestClient) -> None:
        self._summary(client)

        response = client.post(
            f"{RETRIEVAL}/index",
            json={
                "scope_type": "chat",
                "scope_id": "chat-1",
                "source_types": ["context_summary"],
            },
        )

        assert response.status_code == 200
        assert client.get(f"{RETRIEVAL}/items").json()["total"] >= 1

    def test_sweeping_the_same_source_twice_does_not_duplicate(
        self, client: TestClient
    ) -> None:
        """The upsert's duplicate key is what makes a re-sweep safe to run."""

        self._summary(client)
        body = {
            "scope_type": "chat",
            "scope_id": "chat-1",
            "source_types": ["context_summary"],
        }

        client.post(f"{RETRIEVAL}/index", json=body)
        after_first = client.get(f"{RETRIEVAL}/items").json()["total"]
        client.post(f"{RETRIEVAL}/index", json=body)
        after_second = client.get(f"{RETRIEVAL}/items").json()["total"]

        assert after_first >= 1
        assert after_second == after_first

    def test_an_empty_store_indexes_nothing(self, client: TestClient) -> None:
        """With no sources to sweep, the endpoint is a no-op rather than an error."""

        response = client.post(
            f"{RETRIEVAL}/index",
            json={"scope_type": "chat", "scope_id": "chat-1", "source_types": ["context_summary"]},
        )

        assert response.status_code == 200
        assert client.get(f"{RETRIEVAL}/items").json()["total"] == 0


class TestTokenBudget:
    """CTX-03 — the estimate is deliberately not a model tokenizer."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("", 0), (None, 0), ("   ", 0), ("abcd", 1), ("abcde", 2)],
    )
    def test_the_estimate_at_its_boundaries(self, text: object, expected: int) -> None:
        assert estimate_tokens(text) == expected

    def test_the_estimate_grows_with_length(self) -> None:
        assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)

    def test_compression_handles_a_zero_denominator(self) -> None:
        """An empty summary must not divide by zero on the reporting path."""

        assert compression(100, 0) == 0.0
        assert compression(100, 50) == 2.0


class TestContextMemory:
    def _event(self, client: TestClient, **overrides) -> dict:
        payload = {"event_type": "decision", "content": {"text": "We chose the V60"}}
        payload.update(overrides)
        response = client.post(
            f"{CONTEXT}/scopes/chat/chat-1/events",
            json=payload,
        )
        assert response.status_code == 201, response.text
        return response.json()

    def test_an_event_is_created_and_listed(self, client: TestClient) -> None:
        """CTX-09"""

        self._event(client)

        events = client.get(f"{CONTEXT}/scopes/chat/chat-1/events").json()["events"]

        assert len(events) == 1
        assert events[0]["event_type"] == "decision"

    def test_events_are_scope_isolated(self, client: TestClient) -> None:
        """CTX-07"""

        self._event(client)

        other = client.get(f"{CONTEXT}/scopes/chat/chat-2/events").json()["events"]

        assert other == []

    def test_preview_summarises_without_persisting(self, client: TestClient) -> None:
        """CTX-01 — the difference between preview and compact is the write."""

        self._event(client)
        before = client.get(f"{CONTEXT}/summaries").json()["summaries"]

        response = client.post(
            f"{CONTEXT}/preview", json={"scope_type": "chat", "scope_id": "chat-1"}
        )

        assert response.status_code == 200
        after = client.get(f"{CONTEXT}/summaries").json()["summaries"]
        assert len(after) == len(before)

    def test_compact_persists_a_summary(self, client: TestClient) -> None:
        """CTX-02"""

        self._event(client)

        response = client.post(
            f"{CONTEXT}/compact", json={"scope_type": "chat", "scope_id": "chat-1"}
        )

        assert response.status_code == 200
        summaries = client.get(f"{CONTEXT}/summaries").json()["summaries"]
        assert len(summaries) == 1

    def test_an_unknown_summary_is_404(self, client: TestClient) -> None:
        """CTX-08"""

        assert client.get(f"{CONTEXT}/summaries/does-not-exist").status_code == 404

    def test_summarising_is_deterministic_for_fixed_input(self, client: TestClient) -> None:
        """CTX-06 — two previews of unchanged state must agree.

        The `safe` mode has no model in it, so determinism here is a property of
        the code rather than of a fixed model double.
        """

        self._event(client)
        body = {"scope_type": "chat", "scope_id": "chat-1"}

        first = client.post(f"{CONTEXT}/preview", json=body).json()
        second = client.post(f"{CONTEXT}/preview", json=body).json()

        assert first["summary_text"] == second["summary_text"]
        assert first["decisions"] == second["decisions"]

    def test_a_decision_event_reaches_the_summary(self, client: TestClient) -> None:
        """CTX-04 — the extractor routes an event type into the right bucket."""

        self._event(client, event_type="decision", content={"text": "We chose the V60"})

        summary = client.post(
            f"{CONTEXT}/preview", json={"scope_type": "chat", "scope_id": "chat-1"}
        ).json()

        assert "We chose the V60" in str(summary["decisions"])

    def test_a_credential_in_an_event_never_reaches_the_summary(
        self, client: TestClient
    ) -> None:
        """CTX-05 — redaction runs before summarisation, not after."""

        self._event(
            client,
            event_type="decision",
            content={"text": "we set api_key: sk-live-abcdef0123456789"},
        )

        summary = client.post(
            f"{CONTEXT}/preview", json={"scope_type": "chat", "scope_id": "chat-1"}
        ).json()

        assert "sk-live-abcdef0123456789" not in str(summary)

    @pytest.mark.parametrize("scope_type", ["not_a_scope", ""])
    def test_an_invalid_scope_type_is_rejected(
        self, client: TestClient, scope_type: str
    ) -> None:
        response = client.post(
            f"{CONTEXT}/compact", json={"scope_type": scope_type, "scope_id": "chat-1"}
        )

        assert response.status_code == 422
