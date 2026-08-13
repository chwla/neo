"""Tier 1 — crypto, idempotency keys, and tombstones (CRY / IDM / TMB).

Three small modules that share one job: making a fact's identity stable without
making it readable.  Encryption keeps sensitive payloads out of the file on
disk, keyed fingerprints let us recognise a sensitive fact we already hold
without storing its plaintext, idempotency keys let a retried request be
recognised as the same request, and tombstones let a forgotten fact be
recognised and refused without keeping the fact around to compare against.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.memory.contracts import MemoryOperationKind
from app.services.memory.crypto import (
    MemoryCryptoUnavailable,
    UnavailableKeyedFingerprintProvider,
    UnavailableSensitivePayloadProvider,
    UnavailableTombstoneHMACProvider,
    build_associated_data,
)
from app.services.memory.idempotency import MemoryIdempotency
from app.services.memory.local_crypto import LocalMemoryCrypto
from app.services.memory.policy import FORGET_TOMBSTONE_DAYS
from app.services.memory.tombstones import (
    TombstoneSnapshot,
    resurrection_blocked,
    tombstone_digest,
    tombstone_expiration,
    tombstone_matches,
)
from tests.memory.conftest import FROZEN_NOW, OTHER_OWNER_ID, OWNER_ID

AAD = build_associated_data(
    owner_id=OWNER_ID,
    memory_type="goal",
    domain_key="global",
    slot_key="goal:global:primary_output",
    record_id="33333333-3333-4333-8333-333333333333",
    schema_version=1,
    key_version="local-memory-v1",
    purpose="canonical",
)


class TestPayloadEncryption:
    def test_encrypt_then_decrypt_returns_the_original(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-01"""

        payload = crypto.encrypt(b"a sensitive fact", associated_data=AAD)
        assert crypto.decrypt(payload, associated_data=AAD) == b"a sensitive fact"

    def test_decrypting_with_different_associated_data_fails(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """CRY-02 — the AAD is what binds ciphertext to the record it belongs to.

        Without this, a ciphertext could be moved from one record to another —
        or one owner to another — and would still decrypt cleanly.
        """

        payload = crypto.encrypt(b"a sensitive fact", associated_data=AAD)
        other = build_associated_data(
            owner_id=OTHER_OWNER_ID,
            memory_type="goal",
            domain_key="global",
            slot_key="goal:global:primary_output",
            record_id="33333333-3333-4333-8333-333333333333",
            schema_version=1,
            key_version="local-memory-v1",
            purpose="canonical",
        )
        with pytest.raises(Exception):  # noqa: B017 - cryptography raises InvalidTag
            crypto.decrypt(payload, associated_data=other)

    def test_a_tampered_ciphertext_fails(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-03 — AES-GCM authenticates, so corruption is detected not decoded."""

        payload = crypto.encrypt(b"a sensitive fact", associated_data=AAD)
        flipped = bytearray(payload.ciphertext)
        flipped[0] ^= 0xFF
        tampered = payload.__class__(
            ciphertext=bytes(flipped),
            algorithm=payload.algorithm,
            key_version=payload.key_version,
            nonce=payload.nonce,
            associated_data=payload.associated_data,
        )
        with pytest.raises(Exception):  # noqa: B017
            crypto.decrypt(tampered, associated_data=AAD)

    def test_a_tampered_nonce_fails(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-04"""

        payload = crypto.encrypt(b"a sensitive fact", associated_data=AAD)
        flipped = bytearray(payload.nonce)
        flipped[0] ^= 0xFF
        tampered = payload.__class__(
            ciphertext=payload.ciphertext,
            algorithm=payload.algorithm,
            key_version=payload.key_version,
            nonce=bytes(flipped),
            associated_data=payload.associated_data,
        )
        with pytest.raises(Exception):  # noqa: B017
            crypto.decrypt(tampered, associated_data=AAD)

    def test_each_encryption_uses_a_fresh_nonce(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-05 — nonce reuse under one key is what breaks GCM outright."""

        first = crypto.encrypt(b"same plaintext", associated_data=AAD)
        second = crypto.encrypt(b"same plaintext", associated_data=AAD)
        assert first.nonce != second.nonce
        assert first.ciphertext != second.ciphertext

    def test_a_short_seed_is_refused(self) -> None:
        """CRY-05b — weak key material must fail loudly at construction."""

        with pytest.raises(ValueError, match="memory_crypto_seed_too_short"):
            LocalMemoryCrypto(seed=b"too-short")


class TestKeyedDigests:
    def test_a_fingerprint_is_deterministic_per_owner(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-06a"""

        first = crypto.fingerprint(b"material", owner_id=OWNER_ID)
        second = crypto.fingerprint(b"material", owner_id=OWNER_ID)
        assert first.digest == second.digest
        assert first.key_version == second.key_version

    def test_fingerprints_differ_across_owners(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-06b — one owner must not be able to probe another's facts."""

        mine = crypto.fingerprint(b"material", owner_id=OWNER_ID)
        theirs = crypto.fingerprint(b"material", owner_id=OTHER_OWNER_ID)
        assert mine.digest != theirs.digest

    def test_fingerprints_differ_across_material(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-06c"""

        assert (
            crypto.fingerprint(b"a", owner_id=OWNER_ID).digest
            != crypto.fingerprint(b"b", owner_id=OWNER_ID).digest
        )

    def test_a_tombstone_digest_verifies(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-07a"""

        digest = crypto.create(b"material", owner_id=OWNER_ID)
        assert (
            crypto.verify(
                b"material",
                owner_id=OWNER_ID,
                digest=digest.digest,
                key_version=digest.key_version,
            )
            is True
        )

    def test_a_wrong_digest_does_not_verify(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-07b"""

        digest = crypto.create(b"material", owner_id=OWNER_ID)
        assert (
            crypto.verify(
                b"different material",
                owner_id=OWNER_ID,
                digest=digest.digest,
                key_version=digest.key_version,
            )
            is False
        )

    def test_a_mismatched_key_version_does_not_verify(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-08 — a rotated key must invalidate, not silently accept."""

        digest = crypto.create(b"material", owner_id=OWNER_ID)
        assert (
            crypto.verify(
                b"material",
                owner_id=OWNER_ID,
                digest=digest.digest,
                key_version="local-memory-v0",
            )
            is False
        )

    def test_key_versions_are_stable_and_non_empty(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-09"""

        for getter in (
            crypto.current_encryption_key_version,
            crypto.current_fingerprint_key_version,
            crypto.current_tombstone_key_version,
        ):
            assert getter()
            assert getter() == getter()

    def test_the_three_purposes_derive_different_keys(self, crypto: LocalMemoryCrypto) -> None:
        """CRY-10 — one compromised purpose must not compromise the others."""

        fingerprint = crypto.fingerprint(b"material", owner_id=OWNER_ID).digest
        tombstone = crypto.create(b"material", owner_id=OWNER_ID).digest
        assert fingerprint != tombstone


class TestUnavailableProviders:
    """CRY-11 — the fail-closed defaults, used when no key material is configured."""

    def test_encryption_is_unavailable(self) -> None:
        provider = UnavailableSensitivePayloadProvider()
        with pytest.raises(MemoryCryptoUnavailable):
            provider.encrypt(b"x", associated_data=b"y")

    def test_decryption_is_unavailable(self) -> None:
        provider = UnavailableSensitivePayloadProvider()
        with pytest.raises(MemoryCryptoUnavailable):
            provider.decrypt(None, associated_data=b"y")  # type: ignore[arg-type]

    def test_fingerprinting_is_unavailable(self) -> None:
        with pytest.raises(MemoryCryptoUnavailable):
            UnavailableKeyedFingerprintProvider().fingerprint(b"x", owner_id=OWNER_ID)

    def test_tombstone_creation_is_unavailable(self) -> None:
        with pytest.raises(MemoryCryptoUnavailable):
            UnavailableTombstoneHMACProvider().create(b"x", owner_id=OWNER_ID)

    def test_tombstone_verification_is_unavailable(self) -> None:
        with pytest.raises(MemoryCryptoUnavailable):
            UnavailableTombstoneHMACProvider().verify(
                b"x", owner_id=OWNER_ID, digest="d", key_version="v"
            )


class TestAssociatedData:
    def test_the_same_identity_produces_the_same_associated_data(self) -> None:
        """CRY-12a"""

        assert AAD == build_associated_data(
            owner_id=OWNER_ID,
            memory_type="goal",
            domain_key="global",
            slot_key="goal:global:primary_output",
            record_id="33333333-3333-4333-8333-333333333333",
            schema_version=1,
            key_version="local-memory-v1",
            purpose="canonical",
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("owner_id", OTHER_OWNER_ID),
            ("memory_type", "preference"),
            ("domain_key", "learning"),
            ("slot_key", "goal:global:current_primary_goal"),
            ("record_id", "44444444-4444-4444-8444-444444444444"),
            ("schema_version", 2),
            ("key_version", "local-memory-v2"),
            ("purpose", "display"),
        ],
    )
    def test_changing_any_component_changes_the_associated_data(
        self, field: str, value: object
    ) -> None:
        """CRY-12b — every component genuinely binds."""

        base = {
            "owner_id": OWNER_ID,
            "memory_type": "goal",
            "domain_key": "global",
            "slot_key": "goal:global:primary_output",
            "record_id": "33333333-3333-4333-8333-333333333333",
            "schema_version": 1,
            "key_version": "local-memory-v1",
            "purpose": "canonical",
        }
        base[field] = value
        assert build_associated_data(**base) != AAD

    def test_an_invalid_schema_version_is_refused(self) -> None:
        """CRY-12c"""

        with pytest.raises(ValueError, match="associated_data_schema_version_invalid"):
            build_associated_data(
                owner_id=OWNER_ID,
                memory_type="goal",
                domain_key="global",
                slot_key="goal:global:primary_output",
                record_id="33333333-3333-4333-8333-333333333333",
                schema_version=0,
                key_version="local-memory-v1",
                purpose="canonical",
            )


class TestIdempotencyKeys:
    @staticmethod
    def _all_surfaces(owner: str = OWNER_ID) -> dict[str, str]:
        return {
            "http": MemoryIdempotency.http(owner, "req-1", MemoryOperationKind.CREATE),
            "review": MemoryIdempotency.review(owner, "cand-1", 1, "accept"),
            "chat": MemoryIdempotency.chat(owner, "msg-1", "v1", "cand-key"),
            "source_change": MemoryIdempotency.source_change(owner, "msg-1", 1, "mem-1", "detach"),
            "import": MemoryIdempotency.imported(owner, "batch-1", "hash-1"),
            "agent": MemoryIdempotency.agent(owner, "call-1"),
            "maintenance": MemoryIdempotency.maintenance(owner, "run-1", "cmd-1"),
            "manual": MemoryIdempotency.manual(owner, "client-1"),
        }

    def test_every_surface_is_stable_for_stable_input(self) -> None:
        """IDM-01 — this is what makes a retry a retry rather than a second write."""

        assert self._all_surfaces() == self._all_surfaces()

    @pytest.mark.parametrize(
        ("builder", "changed"),
        [
            (
                lambda: MemoryIdempotency.http(OWNER_ID, "req-1", MemoryOperationKind.CREATE),
                lambda: MemoryIdempotency.http(OWNER_ID, "req-2", MemoryOperationKind.CREATE),
            ),
            (
                lambda: MemoryIdempotency.http(OWNER_ID, "req-1", MemoryOperationKind.CREATE),
                lambda: MemoryIdempotency.http(OWNER_ID, "req-1", MemoryOperationKind.FORGET),
            ),
            (
                lambda: MemoryIdempotency.review(OWNER_ID, "c", 1, "accept"),
                lambda: MemoryIdempotency.review(OWNER_ID, "c", 2, "accept"),
            ),
            (
                lambda: MemoryIdempotency.review(OWNER_ID, "c", 1, "accept"),
                lambda: MemoryIdempotency.review(OWNER_ID, "c", 1, "reject"),
            ),
            (
                lambda: MemoryIdempotency.chat(OWNER_ID, "m", "v1", "k"),
                lambda: MemoryIdempotency.chat(OWNER_ID, "m", "v2", "k"),
            ),
            (
                lambda: MemoryIdempotency.source_change(OWNER_ID, "m", 1, "a", "detach"),
                lambda: MemoryIdempotency.source_change(OWNER_ID, "m", 1, "b", "detach"),
            ),
            (
                lambda: MemoryIdempotency.imported(OWNER_ID, "b", "h1"),
                lambda: MemoryIdempotency.imported(OWNER_ID, "b", "h2"),
            ),
            (
                lambda: MemoryIdempotency.agent(OWNER_ID, "call-1"),
                lambda: MemoryIdempotency.agent(OWNER_ID, "call-2"),
            ),
            (
                lambda: MemoryIdempotency.maintenance(OWNER_ID, "r", "c1"),
                lambda: MemoryIdempotency.maintenance(OWNER_ID, "r", "c2"),
            ),
            (
                lambda: MemoryIdempotency.manual(OWNER_ID, "m1"),
                lambda: MemoryIdempotency.manual(OWNER_ID, "m2"),
            ),
        ],
    )
    def test_any_input_change_changes_the_key(self, builder, changed) -> None:
        """IDM-02 — a different request must not be mistaken for a retry."""

        assert builder() != changed()

    def test_surfaces_never_collide_on_the_same_material(self) -> None:
        """IDM-03 — the surface is part of the key, so an HTTP create and a chat
        create of the same fact stay distinguishable."""

        keys = list(self._all_surfaces().values())
        assert len(set(keys)) == len(keys)

    def test_every_key_fits_the_database_column(self) -> None:
        """IDM-04 — the column is 200 chars; truncation would merge two requests."""

        for key in self._all_surfaces().values():
            assert len(key) <= 200

    def test_keys_are_owner_scoped(self) -> None:
        """IDM-05 — two profiles retrying the same request id stay separate."""

        mine = self._all_surfaces(OWNER_ID)
        theirs = self._all_surfaces(OTHER_OWNER_ID)
        for surface, key in mine.items():
            assert key != theirs[surface], surface

    def test_a_malformed_owner_is_refused(self) -> None:
        """IDM-05b"""

        with pytest.raises(ValueError, match="canonical_uuid_required"):
            MemoryIdempotency.agent("not-a-uuid", "call-1")


class TestTombstones:
    @staticmethod
    def _snapshot(
        crypto: LocalMemoryCrypto,
        *,
        fingerprint: str = "sha256:abc",
        owner: str = OWNER_ID,
        created_at: datetime = FROZEN_NOW,
        reconfirmed: bool = False,
    ) -> TombstoneSnapshot:
        digest = tombstone_digest(fingerprint, owner_id=owner, provider=crypto)
        return TombstoneSnapshot(
            id="55555555-5555-4555-8555-555555555555",
            owner_id=owner,
            fingerprint_digest=digest.digest,
            fingerprint_key_version=digest.key_version,
            memory_type="goal",
            domain_key="global",
            slot_key=None,
            created_at=created_at,
            expires_at=tombstone_expiration(created_at),
            explicitly_reconfirmed=reconfirmed,
        )

    def test_a_digest_is_deterministic_and_owner_bound(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-01"""

        mine = tombstone_digest("sha256:abc", owner_id=OWNER_ID, provider=crypto)
        again = tombstone_digest("sha256:abc", owner_id=OWNER_ID, provider=crypto)
        theirs = tombstone_digest("sha256:abc", owner_id=OTHER_OWNER_ID, provider=crypto)
        assert mine.digest == again.digest
        assert mine.digest != theirs.digest

    def test_expiration_is_the_configured_window(self) -> None:
        """TMB-02"""

        assert tombstone_expiration(FROZEN_NOW) == FROZEN_NOW + timedelta(
            days=FORGET_TOMBSTONE_DAYS
        )

    def test_a_tombstone_is_active_before_expiry(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-03a"""

        snapshot = self._snapshot(crypto)
        assert snapshot.is_active(FROZEN_NOW) is True
        assert snapshot.is_active(FROZEN_NOW + timedelta(days=29)) is True

    @pytest.mark.parametrize("days", [FORGET_TOMBSTONE_DAYS, FORGET_TOMBSTONE_DAYS + 1])
    def test_a_tombstone_is_inactive_at_and_after_expiry(
        self, crypto: LocalMemoryCrypto, days: int
    ) -> None:
        """TMB-03b — the boundary is exclusive, so day 30 is already expired."""

        snapshot = self._snapshot(crypto)
        assert snapshot.is_active(FROZEN_NOW + timedelta(days=days)) is False

    def test_a_tombstone_matches_the_fact_it_was_made_for(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-04"""

        snapshot = self._snapshot(crypto)
        assert (
            tombstone_matches(
                snapshot,
                "sha256:abc",
                owner_id=OWNER_ID,
                now=FROZEN_NOW,
                provider=crypto,
            )
            is True
        )

    def test_a_tombstone_does_not_match_a_different_fact(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-05 — forgetting one fact must not block an unrelated one."""

        snapshot = self._snapshot(crypto)
        assert (
            tombstone_matches(
                snapshot,
                "sha256:different",
                owner_id=OWNER_ID,
                now=FROZEN_NOW,
                provider=crypto,
            )
            is False
        )

    def test_a_tombstone_does_not_match_across_owners(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-06 — my forgetting must not suppress your fact."""

        snapshot = self._snapshot(crypto, owner=OWNER_ID)
        assert (
            tombstone_matches(
                snapshot,
                "sha256:abc",
                owner_id=OTHER_OWNER_ID,
                now=FROZEN_NOW,
                provider=crypto,
            )
            is False
        )

    def test_an_active_tombstone_blocks_resurrection(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-07 — the whole point of forget: it does not come back by itself."""

        blocked = resurrection_blocked(
            (self._snapshot(crypto),),
            "sha256:abc",
            owner_id=OWNER_ID,
            now=FROZEN_NOW,
            explicit_reconfirmation=False,
            provider=crypto,
        )
        assert blocked is not None

    def test_an_expired_tombstone_stops_blocking(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-08 — after the window, the fact may be learned again naturally."""

        blocked = resurrection_blocked(
            (self._snapshot(crypto),),
            "sha256:abc",
            owner_id=OWNER_ID,
            now=FROZEN_NOW + timedelta(days=FORGET_TOMBSTONE_DAYS + 1),
            explicit_reconfirmation=False,
            provider=crypto,
        )
        assert blocked is None

    def test_a_reconfirmed_tombstone_stops_blocking(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-09a — the user changed their mind back."""

        blocked = resurrection_blocked(
            (self._snapshot(crypto, reconfirmed=True),),
            "sha256:abc",
            owner_id=OWNER_ID,
            now=FROZEN_NOW,
            explicit_reconfirmation=False,
            provider=crypto,
        )
        assert blocked is None

    def test_an_explicit_reconfirmation_bypasses_every_tombstone(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """TMB-09b — saying it again on purpose always wins over a past forget."""

        blocked = resurrection_blocked(
            (self._snapshot(crypto),),
            "sha256:abc",
            owner_id=OWNER_ID,
            now=FROZEN_NOW,
            explicit_reconfirmation=True,
            provider=crypto,
        )
        assert blocked is None

    def test_a_key_version_mismatch_is_handled_without_crashing(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """TMB-10 — after a key rotation, an old tombstone stops matching cleanly.

        It must not raise: an unreadable tombstone should fail open into "not a
        match" rather than taking the whole mutation down.
        """

        snapshot = self._snapshot(crypto)
        rotated = TombstoneSnapshot(
            **{
                **snapshot.__dict__,
                "fingerprint_key_version": "local-memory-v0",
            }
        )
        assert (
            tombstone_matches(
                rotated,
                "sha256:abc",
                owner_id=OWNER_ID,
                now=FROZEN_NOW,
                provider=crypto,
            )
            is False
        )

    def test_the_first_matching_tombstone_is_returned(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-07b — with several tombstones, the matching one is found."""

        unrelated = self._snapshot(crypto, fingerprint="sha256:other")
        matching = self._snapshot(crypto)
        blocked = resurrection_blocked(
            (unrelated, matching),
            "sha256:abc",
            owner_id=OWNER_ID,
            now=FROZEN_NOW,
            explicit_reconfirmation=False,
            provider=crypto,
        )
        assert blocked is matching

    def test_no_tombstones_never_blocks(self, crypto: LocalMemoryCrypto) -> None:
        """TMB-07c"""

        assert (
            resurrection_blocked(
                (),
                "sha256:abc",
                owner_id=OWNER_ID,
                now=FROZEN_NOW,
                explicit_reconfirmation=False,
                provider=crypto,
            )
            is None
        )


def test_frozen_now_is_timezone_aware() -> None:
    """A guard on the fixture itself: naive datetimes silently break comparisons."""

    assert FROZEN_NOW.tzinfo is UTC
