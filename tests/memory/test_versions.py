"""Tier 1 — the frozen version constants (plan section VER).

`versions.py` is fifteen strings and no logic, which makes it look like nothing
worth testing.  What it actually holds is the memory layer's compatibility
story: every command carries `contract_version`, every derived document carries
the builder version that produced it, and every one of those is compared for
equality somewhere.  A constant that is blank, or accidentally shares its value
with a neighbour, turns one of those equality checks into a check that always
passes — the failure is silent and the data it lets through is already written
by the time anyone notices.

So these tests are about the constants as a *set*, and they enumerate the module
rather than listing names, because the case that matters is the one where
someone adds a sixteenth constant.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.memory import versions
from app.services.memory.contracts import MEMORY_COMMAND_ADAPTER, CandidateProposal
from tests.memory import factories


def _version_constants() -> dict[str, object]:
    """Every public name in `versions.py`.

    The module is constants only — no imports, no helpers — so everything that
    isn't dunder is one of the values under test.  Collecting them this way
    means a newly added constant is covered the moment it is written.
    """

    return {name: value for name, value in vars(versions).items() if not name.startswith("_")}


class TestVersionConstants:
    def test_module_exposes_the_expected_constants(self) -> None:
        """VER-01a — the enumeration below isn't silently empty.

        Every other test in this file derives its cases from `_version_constants`.
        If a refactor moved the constants elsewhere, those tests would pass by
        iterating over nothing.  This asserts membership rather than an exact
        count: adding a sixteenth version is a normal thing to do and shouldn't
        fail a test, but losing the ones the rest of the layer imports by name
        should.
        """

        constants = _version_constants()

        assert {
            "CONTRACT_VERSION",
            "POLICY_VERSION",
            "TAXONOMY_VERSION",
            "DERIVED_DOCUMENT_VERSION",
            "EMBEDDING_DOCUMENT_VERSION",
        } <= set(constants)

    @pytest.mark.parametrize("name", sorted(_version_constants()))
    def test_every_constant_is_a_non_empty_string(self, name: str) -> None:
        """VER-01b — a blank version is a comparison that always succeeds."""

        value = _version_constants()[name]

        assert isinstance(value, str)
        assert value.strip() == value
        assert value != ""

    def test_constants_are_unique(self) -> None:
        """VER-02 — two names sharing a value make them indistinguishable.

        These are compared for equality to decide whether stored data is stale.
        If `DERIVED_DOCUMENT_VERSION` and `EMBEDDING_DOCUMENT_VERSION` ever held
        the same string, bumping one would silently invalidate both — or, worse,
        bumping one would fail to invalidate the other's documents at all.
        """

        constants = _version_constants()
        seen: dict[object, str] = {}
        collisions: list[tuple[str, str, object]] = []

        for name, value in sorted(constants.items()):
            if value in seen:
                collisions.append((seen[value], name, value))
            seen[value] = name

        assert collisions == []


class TestContractVersionGuard:
    """VER-03 — the `Literal[CONTRACT_VERSION]` annotations actually reject.

    Every command declares its version as a `Literal`, defaulted to the current
    constant.  Because it is defaulted, nothing in the ordinary construction path
    ever exercises the guard — a command built in Python always gets the right
    value.  The guard only matters for a command that arrives as a dict from
    somewhere else (a replay envelope, an HTTP body, a stored payload), which is
    exactly where a version from an older build could show up.
    """

    def test_a_command_with_a_wrong_contract_version_is_rejected(self) -> None:
        payload = factories.create_command().model_dump(mode="json")
        payload["contract_version"] = "neo.memory.contract.v0"

        with pytest.raises(ValidationError):
            MEMORY_COMMAND_ADAPTER.validate_python(payload)

    def test_the_same_payload_is_accepted_at_the_current_version(self) -> None:
        """The guard rejects for the right reason, not because the payload is bad.

        Without this, the test above would still pass if `create_command` started
        producing something unparseable for an unrelated reason.
        """

        payload = factories.create_command().model_dump(mode="json")

        assert MEMORY_COMMAND_ADAPTER.validate_python(payload) is not None

    @pytest.mark.parametrize(
        ("field", "constant"),
        [
            ("contract_version", "CONTRACT_VERSION"),
            ("taxonomy_version", "TAXONOMY_VERSION"),
            ("policy_version", "POLICY_VERSION"),
        ],
    )
    def test_each_versioned_field_on_a_proposal_is_guarded(
        self, field: str, constant: str
    ) -> None:
        """A proposal carries all three, and all three are `Literal`-guarded.

        The command adapter above only reaches `contract_version`.  Taxonomy and
        policy versions guard different things — how a slot was built, and which
        sensitivity rules classified it — so each one is pinned separately.
        """

        payload = factories.proposal().model_dump(mode="json")
        assert payload[field] == getattr(versions, constant)

        payload[field] = "neo.memory.something.v0"

        with pytest.raises(ValidationError):
            CandidateProposal.model_validate(payload)
