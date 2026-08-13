"""Tier 4 — derived FTS and vector indexes (plan section IDX).

These indexes are **derived**: everything in them can be rebuilt from the
canonical records, and nothing in them is authoritative.  That shapes what is
worth testing.

Two properties matter more than the rest:

* **Nothing sensitive ever gets here.** The builder is the gate, and it is the
  only gate — once a document exists it is written to a plaintext FTS table.
* **A search result is a candidate id, not an authorization.** The index is
  owner-partitioned, but the real check happens later against canonical SQL.
  These tests pin the partitioning anyway, because defence in depth is the point.

The third theme is staleness. A derived row can outlive the record it describes,
so deletes carry an expected hash and refuse to act when it doesn't match.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.memory import MemoryRecord as MemoryRecordRow
from app.services.memory.contracts import MemoryLifecycleState, Sensitivity
from app.services.memory.indexes import (
    DerivedDocumentBuilder,
    SqliteMemoryFtsIndex,
    SqliteMemoryVectorIndex,
    _cosine,
)
from app.services.memory.taxonomy import MemoryType
from tests.memory.conftest import FROZEN_NOW, OTHER_OWNER_ID, OWNER_ID
from tests.memory.doubles import FakeEmbeddingProvider
from tests.memory.factories import insert_record


@pytest.fixture
def builder() -> DerivedDocumentBuilder:
    return DerivedDocumentBuilder()


@pytest.fixture
def fts(engine) -> SqliteMemoryFtsIndex:
    return SqliteMemoryFtsIndex(engine)


@pytest.fixture
def vectors(engine) -> SqliteMemoryVectorIndex:
    return SqliteMemoryVectorIndex(engine)


@pytest.fixture
def provider() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


def stored_record(engine, **overrides) -> MemoryRecordRow:
    """Insert a record and return the ORM row the builder expects."""

    from sqlalchemy.orm import Session

    record_id = insert_record(engine, **overrides)
    with Session(engine) as session:
        return session.scalar(select(MemoryRecordRow).where(MemoryRecordRow.id == record_id))


def document_for(builder, engine, **overrides):
    return builder.build(stored_record(engine, **overrides), now=FROZEN_NOW)


class TestWhatNeverGetsIndexed:
    """The builder is the only thing standing between a record and a plaintext row."""

    def test_a_sensitive_record_produces_no_document(self, builder, engine) -> None:
        """IDX-01 / OBX-33 — the reason this check is first.

        The FTS table stores ``display_text`` verbatim so it can be searched.
        There is no encryption on that path and there cannot be — you cannot
        run a full-text query over ciphertext.  So the only protection for a
        sensitive memory is never building a document for it at all.

        The sensitivity is set on a detached row rather than inserted: a real
        sensitive record carries encrypted payloads and *no* plaintext columns,
        so it could never reach the builder with readable text anyway.  Setting
        the flag alone tests the stricter case — a row that still has plaintext
        must be refused on the flag, not merely because the text is missing.
        """

        record = stored_record(engine)
        record.sensitivity = Sensitivity.SENSITIVE.value
        assert builder.build(record, now=FROZEN_NOW) is None

    def test_an_archived_record_produces_no_document(self, builder, engine) -> None:
        """IDX-01b — a memory the user archived must stop being findable."""

        assert document_for(builder, engine, status=MemoryLifecycleState.ARCHIVED) is None

    @pytest.mark.parametrize(
        "status",
        [state for state in MemoryLifecycleState if state is not MemoryLifecycleState.ACTIVE],
    )
    def test_no_non_active_status_is_indexed(
        self, builder, engine, status: MemoryLifecycleState
    ) -> None:
        """IDX-01c — parametrised so a new lifecycle state cannot quietly leak.

        Adding a state to the enum without adding it here would leave records in
        that state indexed and recallable.  The test is over the states rather
        than over one example for exactly that reason.
        """

        assert document_for(builder, engine, status=status) is None

    def test_an_expired_record_produces_no_document(self, builder, engine, clock) -> None:
        """IDX-01d — expiry is checked at build time, not only at recall.

        A record that expired yesterday is still `active` in the table; nothing
        sweeps it the moment it lapses. If the builder didn't check, the derived
        row would outlive the fact and keep answering searches.
        """

        expired = clock.advance(days=-1)
        record = stored_record(engine, expires_at=expired)
        assert builder.build(record, now=clock.now) is None

    def test_a_record_expiring_later_is_still_indexed(self, builder, engine, clock) -> None:
        """IDX-01e — the control case, so the check isn't just always-None."""

        record = stored_record(engine, expires_at=clock.advance(days=30))
        assert builder.build(record, now=FROZEN_NOW) is not None

    def test_a_blank_display_text_produces_no_document(self, builder, engine) -> None:
        """IDX-01f — an empty document would be an unsearchable, useless row.

        Set on a detached row rather than inserted, because the schema itself
        refuses a blank display text (`ck_memory_records_payload_shape`, covered
        in `test_schema_constraints.py`). This is the builder's own guard, which
        has to hold independently — it also runs against rows that predate the
        constraint.
        """

        record = stored_record(engine)
        record.display_text = "   "
        assert builder.build(record, now=FROZEN_NOW) is None

    def test_an_oversized_display_text_produces_no_document(self, builder, engine) -> None:
        """IDX-01g — a bound on what one record can put in the index."""

        assert document_for(builder, engine, display_text="x" * 12_001) is None


class TestDocumentIdentity:
    def test_the_content_hash_is_stable_for_identical_input(self, builder, engine) -> None:
        """IDX-02 — the hash is what makes a re-index a no-op.

        If it weren't stable, every reconciliation pass would see every document
        as changed and rewrite the entire index.
        """

        record = stored_record(engine)
        first = builder.build(record, now=FROZEN_NOW)
        second = builder.build(record, now=FROZEN_NOW)
        assert first.content_hash == second.content_hash

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("display_text", "something else entirely"),
            ("memory_type", MemoryType.PREFERENCE),
            ("domain_key", "urban_sketching"),
            ("slot_key", "goal:urban_sketching:primary"),
        ],
    )
    def test_the_hash_changes_when_indexed_material_changes(
        self, builder, engine, field: str, value: str
    ) -> None:
        """IDX-03 — every field in the document must move the hash.

        A field included in the document but omitted from the hash would mean an
        edit to it never triggers a re-index, leaving the derived row describing
        the old value forever.
        """

        base = document_for(builder, engine)
        changed = document_for(builder, engine, **{field: value})
        assert base.content_hash != changed.content_hash

    def test_the_hash_covers_the_owner(self, builder, engine, other_engine) -> None:
        """IDX-03b — two profiles asserting the same fact are not one document."""

        mine = document_for(builder, engine)
        theirs = DerivedDocumentBuilder().build(
            stored_record(other_engine, owner=OTHER_OWNER_ID), now=FROZEN_NOW
        )
        assert mine.content_hash != theirs.content_hash

    def test_the_embedding_document_hashes_only_the_text(self, builder, engine) -> None:
        """IDX-05 — the two hashes answer different questions.

        The derived hash asks "has anything about this memory changed?" The
        embedding hash asks "would this embed differently?" Only the text can
        change an embedding, so a slot rename must not invalidate a vector that
        is still correct — re-embedding is the expensive operation here.
        """

        base = document_for(builder, engine)
        renamed = document_for(builder, engine, slot_key="goal:urban_sketching:primary")
        assert base.content_hash != renamed.content_hash
        assert (
            DerivedDocumentBuilder.build_embedding(base).content_hash
            == DerivedDocumentBuilder.build_embedding(renamed).content_hash
        )

    def test_the_embedding_document_is_stable(self, builder, engine) -> None:
        """IDX-04"""

        document = document_for(builder, engine)
        first = DerivedDocumentBuilder.build_embedding(document)
        second = DerivedDocumentBuilder.build_embedding(document)
        assert first.content_hash == second.content_hash
        assert first.text == document.display_text


class TestFtsIndex:
    def test_upsert_then_search_finds_the_memory(self, builder, engine, fts) -> None:
        """IDX-07"""

        document = document_for(builder, engine, display_text="improve at urban sketching")
        fts.upsert(document)
        results = fts.search(OWNER_ID, "sketching", limit=10)
        assert [item["memory_id"] for item in results] == [str(document.memory_id)]

    def test_upsert_twice_keeps_one_row(self, builder, engine, fts) -> None:
        """IDX-07b — an update in place, not a second copy.

        Every reconciliation pass re-upserts. Appending instead of replacing
        would grow the index without bound and return the same memory twice in
        one result set.
        """

        record = stored_record(engine, display_text="improve at urban sketching")
        fts.upsert(builder.build(record, now=FROZEN_NOW))
        record.display_text = "improve at watercolour sketching"
        fts.upsert(builder.build(record, now=FROZEN_NOW))
        assert len(fts.list_metadata_for_owner(OWNER_ID)) == 1

    def test_the_updated_text_is_what_is_searchable(self, builder, engine, fts) -> None:
        """IDX-07c — and the replacement really replaced."""

        record = stored_record(engine, display_text="improve at urban sketching")
        fts.upsert(builder.build(record, now=FROZEN_NOW))
        record.display_text = "improve at watercolour painting"
        fts.upsert(builder.build(record, now=FROZEN_NOW))
        assert fts.search(OWNER_ID, "painting", limit=10)
        assert fts.search(OWNER_ID, "sketching", limit=10) == []

    def test_delete_with_the_matching_hash_removes_the_row(self, builder, engine, fts) -> None:
        """IDX-08"""

        document = document_for(builder, engine)
        fts.upsert(document)
        assert fts.delete(OWNER_ID, str(document.memory_id), document.content_hash) is True
        assert fts.get_metadata(OWNER_ID, str(document.memory_id)) is None

    def test_delete_with_a_stale_hash_keeps_the_row(self, builder, engine, fts) -> None:
        """IDX-09 — the guard against a slow worker undoing a fresh write.

        Deletes arrive from a queue and can be delayed. If a memory was updated
        between the delete being enqueued and processed, an unconditional delete
        would remove the *new* row on the strength of a decision made about the
        old one. The expected hash makes the delete conditional.
        """

        document = document_for(builder, engine)
        fts.upsert(document)
        assert fts.delete(OWNER_ID, str(document.memory_id), "sha256:stale") is False
        assert fts.get_metadata(OWNER_ID, str(document.memory_id)) is not None

    def test_delete_of_an_absent_row_reports_false(self, builder, engine, fts) -> None:
        """IDX-10 — a missing row is not an error; the desired state is reached."""

        assert fts.delete(OWNER_ID, str(uuid4()), None) is False

    def test_search_is_owner_scoped(self, builder, engine, fts) -> None:
        """IDX-11 — the guarantee that matters most, at this layer.

        Both documents live in one table, separated only by a column in the
        WHERE clause. This is exactly the query that leaks if someone edits it.
        """

        mine = document_for(builder, engine, display_text="improve at urban sketching")
        theirs = document_for(
            builder,
            engine,
            owner=OTHER_OWNER_ID,
            display_text="improve at urban sketching too",
        )
        fts.upsert(mine)
        fts.upsert(theirs)
        found = {item["memory_id"] for item in fts.search(OWNER_ID, "sketching", limit=10)}
        assert found == {str(mine.memory_id)}
        assert str(theirs.memory_id) not in found

    def test_search_respects_the_limit(self, builder, engine, fts) -> None:
        """IDX-12"""

        for index in range(5):
            fts.upsert(document_for(builder, engine, display_text=f"sketching note {index}"))
        assert len(fts.search(OWNER_ID, "sketching", limit=3)) == 3

    @pytest.mark.parametrize(
        "query",
        ['"', "*", "NEAR", "a OR b", "sketch*", '"unclosed', "()", "^caret", "-minus", "AND"],
    )
    def test_fts_metacharacters_do_not_raise(self, builder, engine, fts, query: str) -> None:
        """IDX-13 — a search box is user input, and FTS5 has its own syntax.

        Passing a raw query through to `MATCH` would let a stray quote crash the
        search, or worse, let query syntax change which rows are returned. The
        implementation tokenises and re-quotes; this pins that nothing escapes.
        """

        fts.upsert(document_for(builder, engine, display_text="improve at urban sketching"))
        assert isinstance(fts.search(OWNER_ID, query, limit=10), list)

    @pytest.mark.parametrize("query", ["", "   ", "!!!", "***"])
    def test_a_query_with_no_usable_terms_returns_nothing(
        self, builder, engine, fts, query: str
    ) -> None:
        """IDX-14 — no terms means no results, not every result."""

        fts.upsert(document_for(builder, engine))
        assert fts.search(OWNER_ID, query, limit=10) == []

    def test_metadata_round_trips(self, builder, engine, fts) -> None:
        """IDX-15 — reconciliation compares against exactly these fields."""

        document = document_for(builder, engine)
        fts.upsert(document)
        metadata = fts.get_metadata(OWNER_ID, str(document.memory_id))
        assert metadata["content_hash"] == document.content_hash
        assert metadata["canonical_revision"] == document.canonical_revision
        assert metadata["derived_schema_version"] == document.schema_version

    def test_listing_metadata_is_owner_scoped(self, builder, engine, fts) -> None:
        """IDX-15b"""

        fts.upsert(document_for(builder, engine))
        fts.upsert(document_for(builder, engine, owner=OTHER_OWNER_ID))
        rows = fts.list_metadata_for_owner(OWNER_ID)
        assert {row["owner_id"] for row in rows} == {OWNER_ID}

    def test_listing_pages_deterministically(self, builder, engine, fts) -> None:
        """IDX-15c — reconciliation walks the whole index in pages.

        Without a stable order a page boundary could skip or repeat a document,
        which would make a coverage report quietly wrong.
        """

        for index in range(5):
            fts.upsert(document_for(builder, engine, display_text=f"note {index}"))
        first = fts.list_metadata_for_owner(OWNER_ID, limit=2)
        second = fts.list_metadata_for_owner(
            OWNER_ID, after_memory_id=first[-1]["memory_id"], limit=2
        )
        assert len(first) == 2
        assert not {row["memory_id"] for row in first} & {row["memory_id"] for row in second}

    @pytest.mark.parametrize("limit", [0, 1_002])
    def test_an_out_of_range_page_size_is_refused(self, fts, limit: int) -> None:
        """IDX-15d"""

        with pytest.raises(ValueError, match="fts_metadata_limit_out_of_range"):
            fts.list_metadata_for_owner(OWNER_ID, limit=limit)

    def test_clearing_an_owner_leaves_the_other_intact(self, builder, engine, fts) -> None:
        """IDX-16 — used when a profile is deleted.

        Removing one profile's derived rows must not touch another's; this is
        the destructive operation in this module.
        """

        fts.upsert(document_for(builder, engine))
        theirs = document_for(builder, engine, owner=OTHER_OWNER_ID)
        fts.upsert(theirs)
        assert fts.clear_owner(OWNER_ID) == 1
        assert fts.list_metadata_for_owner(OWNER_ID) == []
        assert len(fts.list_metadata_for_owner(OTHER_OWNER_ID)) == 1

    def test_clearing_also_empties_the_searchable_table(self, builder, engine, fts) -> None:
        """IDX-16b — two tables are involved, and both have to be cleared.

        The metadata row and the FTS5 virtual-table row are separate writes.
        Clearing only the metadata would leave the text searchable while the
        index believed it was gone.
        """

        fts.upsert(document_for(builder, engine, display_text="improve at urban sketching"))
        fts.clear_owner(OWNER_ID)
        assert fts.search(OWNER_ID, "sketching", limit=10) == []

    def test_deleting_also_empties_the_searchable_table(self, builder, engine, fts) -> None:
        """IDX-08b — the same two-table concern, on the single-row path."""

        document = document_for(builder, engine, display_text="improve at urban sketching")
        fts.upsert(document)
        fts.delete(OWNER_ID, str(document.memory_id), document.content_hash)
        assert fts.search(OWNER_ID, "sketching", limit=10) == []

    def test_health_reports_availability(self, fts) -> None:
        """IDX-17"""

        health = fts.health()
        assert health.healthy is True
        assert health.failure_code is None


class TestVectorIndex:
    def test_upsert_stores_the_vector_and_its_dimension(
        self, builder, engine, vectors, provider
    ) -> None:
        """IDX-18"""

        document = document_for(builder, engine)
        vectors.upsert(document, provider.embed(document.display_text), provider)
        metadata = vectors.get_metadata(OWNER_ID, str(document.memory_id))
        assert metadata["dimension"] == provider.dimension
        assert metadata["provider"] == "fake"
        assert metadata["model"] == "fake-embed-v1"

    def test_a_dimension_mismatch_is_refused(self, builder, engine, vectors, provider) -> None:
        """IDX-19 — the guard against silently mixing two embedding models.

        Vectors from different models are not comparable; cosine similarity
        between them is noise. Storing a wrong-length vector would corrupt
        every future ranking in a way nothing else would notice.
        """

        document = document_for(builder, engine)
        with pytest.raises(ValueError, match="embedding_dimension_mismatch"):
            vectors.upsert(document, [0.1, 0.2], provider)

    @pytest.mark.parametrize("vector", [[], [float("nan")] * 8, [float("inf")] * 8])
    def test_an_unusable_vector_is_refused(
        self, builder, engine, vectors, provider, vector
    ) -> None:
        """IDX-19b — NaN propagates through cosine and poisons the whole ranking."""

        document = document_for(builder, engine)
        with pytest.raises(ValueError, match="embedding_invalid_response"):
            vectors.upsert(document, vector, provider)

    def test_search_ranks_by_similarity(self, builder, engine, vectors) -> None:
        """IDX-20 — closest first, and the ordering is the whole point."""

        provider = FakeEmbeddingProvider(
            dimension=3,
            script={
                "near": [1.0, 0.0, 0.0],
                "far": [0.0, 1.0, 0.0],
            },
        )
        near = document_for(builder, engine, display_text="near")
        far = document_for(builder, engine, display_text="far")
        vectors.upsert(near, provider.embed("near"), provider)
        vectors.upsert(far, provider.embed("far"), provider)
        results = vectors.search([1.0, 0.0, 0.0], OWNER_ID, limit=5)
        assert [item.memory_id for item in results] == [near.memory_id, far.memory_id]
        assert results[0].score > results[1].score

    def test_search_is_owner_scoped(self, builder, engine, vectors, provider) -> None:
        """IDX-20b / IDX-25"""

        mine = document_for(builder, engine)
        theirs = document_for(builder, engine, owner=OTHER_OWNER_ID)
        vectors.upsert(mine, provider.embed("a"), provider)
        vectors.upsert(theirs, provider.embed("a"), provider)
        found = {item.memory_id for item in vectors.search(provider.embed("a"), OWNER_ID, 10)}
        assert found == {mine.memory_id}

    def test_search_respects_the_limit(self, builder, engine, vectors, provider) -> None:
        """IDX-20c"""

        for index in range(5):
            document = document_for(builder, engine, display_text=f"note {index}")
            vectors.upsert(document, provider.embed(f"note {index}"), provider)
        assert len(vectors.search(provider.embed("note 0"), OWNER_ID, 2)) == 2

    def test_delete_honours_the_expected_hash(self, builder, engine, vectors, provider) -> None:
        """IDX-23 — same staleness guard as FTS, same reason."""

        document = document_for(builder, engine)
        vectors.upsert(document, provider.embed("a"), provider)
        assert vectors.delete(OWNER_ID, str(document.memory_id), "sha256:stale") is False
        assert vectors.delete(OWNER_ID, str(document.memory_id), document.content_hash) is True

    def test_clearing_an_owner_is_scoped(self, builder, engine, vectors, provider) -> None:
        """IDX-24"""

        vectors.upsert(document_for(builder, engine), provider.embed("a"), provider)
        vectors.upsert(
            document_for(builder, engine, owner=OTHER_OWNER_ID), provider.embed("a"), provider
        )
        assert vectors.clear_owner(OWNER_ID) == 1
        assert len(vectors.list_metadata_for_owner(OTHER_OWNER_ID)) == 1

    def test_the_stored_vector_round_trips_exactly(
        self, builder, engine, vectors, provider
    ) -> None:
        """IDX-18b — stored as JSON, so precision loss would be silent.

        A vector that changes on the round-trip would make search results drift
        from the values that were embedded, with nothing to indicate why.
        """

        document = document_for(builder, engine)
        vector = [0.1, -0.25, 1e-8, 0.999999, 0.0, -1.0, 0.5, 0.3333333333333333]
        vectors.upsert(document, vector, provider)
        with engine.connect() as connection:
            from sqlalchemy import text as sql_text

            stored = connection.scalar(
                sql_text("SELECT vector_json FROM memory_vector_points WHERE memory_id = :m"),
                {"m": str(document.memory_id)},
            )
        assert json.loads(stored) == vector


class TestCosine:
    def test_identical_vectors_score_one(self) -> None:
        """IDX-21"""

        assert _cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self) -> None:
        """IDX-21b"""

        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_score_minus_one(self) -> None:
        """IDX-21c"""

        assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_the_result_is_clamped_to_the_valid_range(self) -> None:
        """IDX-21d — floating-point error can push an identical pair past 1.0.

        A score above 1.0 would sort ahead of a genuine exact match and break
        any threshold comparison written as `>= 1.0`.
        """

        value = _cosine([0.1] * 100, [0.1] * 100)
        assert -1.0 <= value <= 1.0

    def test_a_zero_vector_scores_zero_rather_than_dividing_by_zero(self) -> None:
        """IDX-21e"""

        assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0

    def test_mismatched_lengths_score_zero_rather_than_raising(self) -> None:
        """IDX-22 — pinning what it does, which is not what the plan assumed.

        I expected a mismatch to raise, since comparing vectors of different
        dimensions is meaningless. It returns 0 instead.

        On reflection that is the better behaviour *here*: this runs inside a
        search loop over every stored vector, and one row written by a
        previously-configured embedding model would otherwise abort the entire
        search rather than just failing to match. Scoring it 0 excludes it,
        which is what you want from a vector that cannot be compared.

        The mismatch is still caught where it can be acted on — `upsert` raises
        `embedding_dimension_mismatch` (IDX-19), so a wrong-dimension vector
        cannot be stored in the first place. This path only handles rows that
        predate a model change.
        """

        assert _cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0
        assert _cosine([], [1.0]) == 0


class TestOwnerPartitioning:
    def test_a_search_never_returns_another_owners_row_even_by_id(
        self, builder, engine, fts, vectors, provider
    ) -> None:
        """IDX-25 — asking directly for a row you do not own returns nothing.

        Both indexes key on `(owner_id, memory_id)`. Looking up someone else's
        memory id under your own owner id must miss, not fall back to matching
        on the memory id alone.
        """

        theirs = document_for(builder, engine, owner=OTHER_OWNER_ID)
        fts.upsert(theirs)
        vectors.upsert(theirs, provider.embed("a"), provider)
        assert fts.get_metadata(OWNER_ID, str(theirs.memory_id)) is None
        assert vectors.get_metadata(OWNER_ID, str(theirs.memory_id)) is None

    def test_a_delete_cannot_reach_another_owners_row(
        self, builder, engine, fts, vectors, provider
    ) -> None:
        """IDX-25b — and the destructive path is scoped the same way."""

        theirs = document_for(builder, engine, owner=OTHER_OWNER_ID)
        fts.upsert(theirs)
        vectors.upsert(theirs, provider.embed("a"), provider)
        assert fts.delete(OWNER_ID, str(theirs.memory_id), None) is False
        assert vectors.delete(OWNER_ID, str(theirs.memory_id), None) is False
        assert fts.get_metadata(OTHER_OWNER_ID, str(theirs.memory_id)) is not None

    def test_the_document_owner_is_a_real_uuid(self, builder, engine) -> None:
        """IDX-02b — the contract parses it, so a malformed owner cannot pass."""

        document = document_for(builder, engine)
        assert isinstance(document.owner_id, UUID)
        assert str(document.owner_id) == OWNER_ID


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
