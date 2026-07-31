# Neo memory redesign: Phase 2 mutation kernel

## Scope

Phase 2 is an isolated deterministic normalization, planning, and SQLite mutation
kernel. It accepts validated Phase 0 `MemoryCommand` objects and writes only the Phase 1
canonical schema. It is not imported by production chat, APIs, extraction, retrieval,
agent tools, jobs, UI, FTS, embeddings, or vector systems. It does not dual-write,
migrate legacy memories, or process the outbox.

## Normalization

`normalization.py` uses normalization version `neo.memory.normalization.v1` and the
authoritative contract, policy, and taxonomy versions. It canonicalizes owner UUIDs,
NFKC-normalizes and collapses whitespace in text, recursively normalizes typed JSON,
sorts object keys, and emits compact UTF-8 canonical JSON. Values retain JSON types;
booleans, numbers, arrays, and objects are not flattened into prose.

Identity consists of subject, memory type, domain, semantic slot, and cardinality.
Slots must match deterministic type-specific shapes and describe stable attributes, not
the current remembered value. Additive slots contain an opaque UUID. Domains must be a
known taxonomy key or an explicitly grounded `topic.*` key. There is no last-token
fallback; value modifiers such as `clearly` cannot become domains. A deterministic
correction predecessor supplies the domain and slot unless the validated command marks
a valid explicit change.

Canonical values and display text are positive current assertions. Negated predecessor
phrases such as `no longer` are rejected from canonical/display text, while `not only`
is permitted. Retraction wording belongs only in source/operation evidence. Metadata is
limited to approved keys and 4096 canonical JSON bytes; evidence is normalized and
bounded to 1000 characters per span. Unsupported versions, types, domains, slots, and
value schemas fail with stable codes.

## Fingerprints and request identity

Normal canonical fingerprints are SHA-256 over a versioned compact object containing
subject, type, domain, slot, and recursively case-folded normalized value. Timestamps,
confidence, similarity, and model output are absent. Normal request hashes are SHA-256
over deterministic command material.

Sensitive canonical fingerprints and sensitive/prohibited request hashes use an
injected owner-bound keyed fingerprint provider, preventing dictionary attacks and
cross-owner equality leakage. Prohibited content is classified and rejected before its
command or candidate payload is durably stored; the ledger retains only a redacted
command envelope and fixed outcome code.

## Cryptographic boundaries

`crypto.py` defines injected protocols for authenticated payload encryption/decryption,
keyed fingerprints, tombstone HMAC creation/verification, and key-version resolution.
Production defaults fail closed; Phase 2 configures no production key. The deterministic
provider lives under `tests/memory_v2` and is used only by tests and the manual harness.

Associated data canonically binds owner, type, domain, slot, record ID, schema version,
key version, and purpose. Sensitive record and candidate plaintext columns are null;
repository-visible values are ciphertext, nonce, algorithm, key version, and associated
data. Sensitive source excerpts and operation commands use the same opaque boundary.
Encryption and keyed material are completed before `BEGIN IMMEDIATE`; no provider call
occurs after the write transaction begins.

## Planner API

```python
plan_memory_mutation(
    command: MemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan
```

The frozen input snapshots contain only one owner's canonical records, candidates,
relations, tombstones, expected revisions, normalized input, source, operation ID, and
fixed time. The frozen output describes the stable outcome; record creates, updates,
deletes and preconditions; candidate decisions; relation creates; source creates and
detaches; tombstone creates/reconfirmations/deletes; and outbox intents. IDs derive from
the operation ID with UUIDv5. The planner performs no I/O, encryption, commit, model,
similarity, FTS, vector, or network call.

## Operation semantics

- `create`: creates in an empty exclusive slot; exact/typed duplicates reconfirm and add
  provenance; incompatible occupied-slot assertions require review; additive facts use
  independent entity-stable slots. Active tombstones block automatic resurrection and
  explicit reconfirmation marks the tombstone reconfirmed.
- `update`: requires target and expected revision and permits only deterministic
  same-fact refinement. It recomputes canonical/display/fingerprint, increments revision,
  and emits an upsert. Incompatible replacement is rejected.
- `replace`: validates every owner-bound predecessor and revision, inherits identity when
  required, transitions all predecessors before inserting one clean successor, creates a
  `supersedes` relation for each predecessor, attaches positive/retraction provenance,
  and emits removal/upsert events atomically.
- `supersede`: explicitly transitions predecessors to an already active compatible
  successor and cannot produce two active values in an exclusive slot.
- `merge`: accepts only explicitly selected, deterministically compatible inputs and a
  normalized result; inputs are superseded and related with `merged_from`. It never
  newline-concatenates arbitrary text.
- `archive`: requires revision, transitions active to archived, preserves history and
  provenance, and emits removal. An identical request replays its original result.
- `forget`: requires revision, changes the row to `forgotten`, removes plaintext
  canonical/display columns from normal queries by converting the payload to the opaque
  encrypted representation required by the Phase 1 row constraint, scrubs the canonical
  fingerprint, detaches provenance, creates a 30-day owner-bound tombstone, and emits
  removal plus expiry intention. The canonical row remains as non-active history; it is
  not physically removed. This is the Phase 1-compatible soft-forget representation.
- `erase_permanently`: physically deletes the canonical row and cascading provenance,
  deletes applicable tombstones and derived outbox state, redacts prior operation command
  bodies and request fingerprints that referenced the record, and emits a record-ID-only
  reconciliation request with no memory foreign key. It retains operation IDs, kinds,
  lifecycle outcomes, timestamps, and the erasure operation as minimum non-value audit
  evidence. No blocking value fingerprint remains, so recreation is allowed.
- `restore`: directly restores only an archived record into an empty compatible slot.
  Superseded/forgotten or occupied-slot direct restores fail with `invalid_restore`.
  Intentional restore-as-replacement creates a new version and supersedes the current
  active value instead of flipping a historical row active.

## Transaction coordinator

```python
MemoryMutationService(
    engine,
    *,
    owner_id,
    database_identity,
    payload_provider,
    fingerprint_provider,
    tombstone_provider,
    key_versions,
    retry_policy=None,
    failure_injector=None,
    clock=...,
).execute(command) -> MemoryCommandResult
```

The service validates owner/version/shape, derives a secure request hash, checks the
owner-scoped ledger, reads and plans outside the write lock, and precomputes crypto. It
then opens `BEGIN IMMEDIATE`, checks the ledger again, reloads an opaque whole-owner state
guard, rejects a changed plan snapshot, inserts the started operation, applies the entire
plan, marks the operation terminal, flushes, and commits once. Repository methods do not
commit. A rejection is also a durable operation plus candidate decision where permitted.
`dry_run` normalizes and plans but writes no operation or canonical row.

Failure stages cover operation start, first predecessor transition, replacement creation,
relation creation, provenance creation, tombstone creation, outbox creation, and operation
completion. Exceptions roll back the operation, records, provenance, relations,
tombstones, and outbox together. SQL text is never returned.

SQLite busy/locked conflicts, plan changes, revision conflicts, and applicable integrity
conflicts receive a bounded retry using the same operation ID. Each retry replans from a
fresh snapshot. Exhausted conflicts map to stable public codes.

## Idempotency and concurrency

The database uniqueness constraint on `(owner_id, idempotency_key)` is authoritative.
Same owner/key/hash returns the durable original result; a different hash returns
`idempotency_conflict`; owners are independent. A compact non-sensitive replay envelope
in the Phase 1 bounded `error_detail` field preserves the original active IDs, candidate
ID, revision, derived state, and fixed message even if the record later changes. Existing
operation columns preserve the remaining result fields. This avoids changing the
authoritative Phase 1 schema.

`BEGIN IMMEDIATE`, optimistic expected revisions, the opaque state guard, owner-scoped
foreign keys, idempotency uniqueness, and the partial unique active-slot index serialize
competing writers. Concurrent incompatible exclusive creates yield one active value and
one review/rejection; duplicates yield one create and one reconfirm; competing lifecycle
mutations yield one winner. Separate real SQLite connections are used in tests.

## Tombstones

Tombstone material is an HMAC of the canonical fingerprint under an injected key and is
bound again to canonical owner UUID by the provider. Tombstones contain no remembered
plaintext. Active means unexpired and not explicitly reconfirmed. They block automatic
recreation only for the same owner; explicit reconfirmation is permitted by policy and
recorded. Expired tombstones do not block recreation. Permanent erasure deletes the
matching tombstone. Phase 2 writes expiry intentions but has no expiry worker.

## Outbox

Canonical revisions atomically write pending `canonical_upsert` or `canonical_remove`
events. Forget also writes `tombstone_expiry`; permanent erasure writes a
`reconciliation_request`. The contract exposes `usage`, but no Phase 2 mutation command
changes usage, so Phase 2 emits no synthetic usage event. Keys include event kind, record
or `none`, revision or `none`, owner ID, operation ID, and deterministic label. Sensitive
events contain identity/revision/hash metadata only, never plaintext. There is no worker.

## Critical correction

Given active `create long-form cinematic YouTube videos`, a structured explicit replace
whose positive candidate is `create short Instagram reels clearly` produces exactly one
active row in `goal:video_creation:current_primary_goal`. Its canonical/display value is
exactly the positive candidate, domain is `video_creation`, and slot is inherited. The
predecessor becomes `superseded` at its next revision. One successor-to-predecessor
`supersedes` relation exists. Supporting assertion and predecessor-retraction provenance
are separate. One predecessor removal and one successor upsert are pending. Active-state
queries return only the successor; explicit history queries retain both and the lineage.
Exact idempotent replay creates no row.

## Deliberate Phase 2 decisions and deviations

- Phase 1 remains unchanged except the ORM JSON binding for a nullable sensitive command
  now uses SQL NULL (`JSON(none_as_null=True)`), matching the existing DDL check.
- The Phase 1 schema has no result-snapshot column. Exact replay uses a bounded,
  non-sensitive versioned envelope in `error_detail` instead of adding a Phase 2
  migration.
- The Phase 1 repository's defense-in-depth scan now removes canonical UUID substrings
  from classification material before Luhn checking. This prevents random UUID digit
  groups from looking like card numbers while leaving semantic values and stored payloads
  unchanged.
- Replacement updates predecessors before inserting the active successor inside the same
  transaction so the partial exclusive-slot unique index is never transiently violated.
- Soft forget retains a quarantined encrypted payload representation because the Phase 1
  payload-shape constraint requires either normal or encrypted payload fields. It is
  status-filtered, fingerprint-scrubbed, provenance-detached, and omitted from normal
  active-state reads. Permanent erase is the physical deletion operation.
- The transaction guard covers the owner's full canonical planning state. This is
  conservative for an isolated correctness kernel and can be narrowed only after later
  adapter access patterns are proven.

## Phase 3 prerequisites

Phase 3 must remain blocked until the disposable manual validation is run and reviewed.
It must also supply production authenticated-encryption/key management, define adapter
authorization and owner/profile binding, preserve structured command authority, decide
how replay envelopes evolve in a future schema revision, and design outbox consumers and
derived reconciliation. Production integration, extraction, retrieval, FTS, embeddings,
vectors, legacy migration, and UI work are intentionally unresolved and absent here.
