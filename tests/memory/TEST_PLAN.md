# Neo memory layer — test plan

Goal: get the memory layer to the point where it can be trusted in real daily use.
This plan enumerates every behaviour worth pinning down, grouped by the module that
owns it, with a stable ID per case so progress can be tracked across sessions.

**Scope.** Everything under `app/services/memory/`, `app/services/memory_retrieval/`,
`app/services/context_memory/`, `app/models/memory.py`, `app/repositories/memory.py`,
`app/db/memory_migrations.py`, `app/services/memory_chat.py`, and the four memory API
routers.

**Status legend:** `[ ]` not written · `[~]` partially covered by an existing test ·
`[x]` covered and passing.

**Progress:** 1539 tests passing, 28 strict `xfail`s recording ten real defects —
**652 of 880 plan items.**

| Tier | Done | Partial | Open | Total |
|---|---:|---:|---:|---:|
| 0 — Infrastructure | 7 | 1 | 0 | 8 |
| 1 — Foundations | 196 | 0 | 0 | 196 |
| 2 — Extraction | 148 | 2 | 2 | 152 |
| 3 — Persistence | 182 | 0 | 23 | 205 |
| 4 — Derived / async | 72 | 1 | 28 | 101 |
| 5 — Recall / prompt / chat | 47 | 0 | 47 | 94 |
| 6 — Config / runtime / HTTP | 0 | 0 | 77 | 77 |
| 7 — Cross-cutting | 0 | 0 | 47 | 47 |
| **Total** | **652** | **4** | **224** | **880** |

*This table replaces a prose summary that claimed Tiers 0–3 and recall were "complete".
They are not: 3 `VER` items in Tier 1, 36 `PRE`/`COR` items in Tier 2, 23 in Tier 3, and
47 in Tier 5 were open the whole time. The item count (568) was always right; the
sentence describing it was not. A generated table is harder to overstate than a
sentence, which is the point of replacing it.*

Findings that matter most: **SCH-14** (exclusive-slot uniqueness not enforced at global
scope), **RCL-21d** (stemmer breaks `-es` plurals, so "sketches" misses "sketching"), and
**PRE-01b** ("call me X" passes the extraction gate but has no deterministic pattern).
All detailed in `decisions.md`.

**Prior coverage** (51 tests) lives in
`test_extraction_gate.py`, `test_forget_and_duplicates.py`, `test_history_redaction.py`,
`test_nested_assertions.py`, `test_semantic_duplicate.py`. Those are marked `[x]`/`[~]`
below where they already pin a case.

---

## Tier 0 — Shared test infrastructure

Not tests, but nothing below can be written without them. The previous suite's helpers
were deleted in `9071502`; these rebuild the minimum needed.

- [x] **INF-01** `conftest.py`: in-memory SQLite engine fixture with the memory schema
      migrated and an owner bound, torn down per test.
- [x] **INF-02** `conftest.py`: second-owner engine fixture, for every cross-owner test.
- [x] **INF-03** `factories.py`: builders for `ValidatedCandidateProposal`,
      `MemoryActor`, `MemorySource`, and each of the nine commands, with sane defaults.
- [x] **INF-04** `factories.py`: `insert_record(...)` writing a valid `MemoryRecord`
      directly, for tests that need a starting state without going through mutations.
- [x] **INF-05** A deterministic `KeyedFingerprintProvider` / crypto double, so
      sensitive-path tests don't depend on a real key.
- [~] **INF-06** A frozen-clock helper — many behaviours (expiry, tombstones, freshness,
      retry backoff, leases) are time-dependent and must not be wall-clock flaky.
      `conftest.FrozenClock` exists but **has no consumers yet**: every test written so
      far reads a timestamp rather than advancing one. Marked `[~]` rather than `[x]`
      so this isn't mistaken for coverage. Its consumers are in Tier 4 (outbox leases,
      maintenance sweeps) and the tombstone-expiry cases.
- [x] **INF-07** Stand-ins for the embedding path, at two seams.
      `doubles.StaticDuplicateFinder` covers the *duplicate-finder* callable the
      coordinator needs; `doubles.FakeEmbeddingProvider` is the fixed-dimension
      embedding provider the vector index needs, deriving vectors from a hash of the
      text (so identical text embeds identically) with an escape hatch for tests that
      need a specific geometry, plus a failure switch.
- [x] **INF-08** Model scripting. `doubles.scripted_model` wraps the app's own
      `FixtureExtractionModel`; `RecordingModel` captures what the model was shown;
      `UnavailableModel` scripts provider errors and timeouts. `doubles.assertion`
      and `doubles.source_span` build spans whose offsets genuinely index the message,
      so a fixture typo fails loudly instead of as a grounding rejection.

**Known duplication, deliberately not consolidated yet.** Three test files each keep a
local `CanonicalMemorySnapshot` builder — `test_forget_and_duplicates.py` (preference in an
exclusive slot), `test_extraction_coordinator.py` (knowledge in an additive slot, feeding
the override builder), and `test_correction_resolver.py`. The rule of three assumes the
three uses are the same; these are three different shapes of one contract type, so a shared
builder would need defaults none of them wants and every caller would override half of
them. Revisit when either a fourth caller appears or only one session is editing
`factories.py`, and then build it around what all four actually need.

---

## Tier 1 — Foundations (pure functions, no database)

These are cheap, fast, and catch the majority of silent-corruption bugs.

### `versions.py` — VER

- [x] **VER-01** Every version constant is a non-empty string. Parametrised over the
      module's public names rather than a hand-written list, so a newly added constant
      is covered the moment it exists.
- [x] **VER-02** Version constants are unique (no two names share a value).
- [x] **VER-03** Changing `CONTRACT_VERSION` breaks the `Literal` on `MemoryCommandBase`
      — i.e. the guard actually guards. (Assert via a wrong-version dict.) Extended to
      `taxonomy_version` and `policy_version` on `CandidateProposal`, which the command
      adapter never reaches.

### `taxonomy.py` — TAX

Domain resolution:
- [x] **TAX-01** Every alias in `DOMAIN_ALIASES` resolves to its own domain.
- [x] **TAX-02** Longest alias wins when two aliases both match the text.
- [x] **TAX-03** `explicit_domain` overrides alias matching.
- [x] **TAX-04** No alias and no grounded topic → `TaxonomyError` (fails closed; there is
      deliberately no last-token fallback).
- [x] **TAX-05** Alias matching is word-boundary bound: "scoding" does not match "coding".
- [x] **TAX-06** Alias matching is NFKC- and case-insensitive.
- [x] **TAX-07** `normalize_unknown_domain` produces `topic.<snake_case>`.
- [x] **TAX-08** An unknown topic not present in the source text is rejected
      (`unknown_domain_must_be_grounded`).
- [x] **TAX-09** A single `VALUE_ONLY_DOMAIN_TERMS` word ("briefly") cannot be a domain.
- [x] **TAX-10** A multi-word phrase containing a value-only term is allowed.
- [x] **TAX-11** `validate_domain_key` accepts known enum values and `topic.x_y` form.
- [x] **TAX-12** `validate_domain_key` rejects `topic.` with an empty tail, uppercase,
      leading/trailing underscores, and non-alphanumeric characters.
- [x] **TAX-13** Empty / whitespace / punctuation-only domain input raises.

Slot construction:
- [x] **TAX-14** IDENTITY in a non-global domain raises `identity_domain_must_be_global`.
- [x] **TAX-15** IDENTITY builds `identity:global:<key>` and is EXCLUSIVE.
- [x] **TAX-16** IDENTITY without an `identity_key` raises.
- [x] **TAX-17** PREFERENCE builds `preference:<domain>:<dimension>`, EXCLUSIVE.
- [x] **TAX-18** PREFERENCE without a dimension raises.
- [x] **TAX-19** GOAL with an `EXCLUSIVE_GOAL_ROLES` role is EXCLUSIVE, 3-part slot.
- [x] **TAX-20** GOAL with no role defaults to `independent_goal` → ADDITIVE, 4-part slot
      ending in a UUID.
- [x] **TAX-21** GOAL with an unrecognised role raises `unsupported_goal_role`.
- [x] **TAX-22** ADDITIVE goal without `entity_id` raises
      `additive_memory_requires_entity_id`.
- [x] **TAX-23** A non-UUID `entity_id` raises `entity_id_must_be_uuid`.
- [x] **TAX-24** EDUCATION/EMPLOYMENT with `current_field="current_status"` → EXCLUSIVE.
- [x] **TAX-25** EDUCATION/EMPLOYMENT with any other `current_field` raises.
- [x] **TAX-26** PROJECT/ACTIVITY/EVENT/KNOWLEDGE → `:item:<uuid>`, ADDITIVE.
- [x] **TAX-27** Slot building is deterministic: same inputs → identical slot string.
- [x] **TAX-28** Slot building never reads the remembered value (documented invariant —
      assert two different values with identical dimensions produce one slot).

Field rollup:
- [x] **TAX-29** Every `MemoryType` maps to a `MemoryField` (no type falls through).
- [x] **TAX-30** `memory_field_for_type` on an unknown string returns `MISCELLANEOUS`
      rather than raising.
- [x] **TAX-31** Only `PROJECTS` is the project-scoped field.

Predecessor inheritance:
- [x] **TAX-32** No explicit change → domain and slot are inherited verbatim.
- [x] **TAX-33** Proposing a domain without `explicit_domain_change` raises.
- [x] **TAX-34** Proposing a slot without `explicit_slot_change` raises.
- [x] **TAX-35** `explicit_domain_change=True` with no domain raises.
- [x] **TAX-36** `explicit_slot_change=True` with no slot raises.
- [x] **TAX-37** Domain change rewrites part 1 of the predecessor slot and keeps the rest.
- [x] **TAX-38** A predecessor slot whose type prefix doesn't match raises
      `invalid_predecessor_slot`.
- [x] **TAX-39** A resulting slot inconsistent with the identity raises
      `slot_does_not_match_identity`.
- [x] **TAX-40** Cardinality is always inherited, never recomputed.

### `normalization.py` — NRM

- [x] **NRM-01** `normalize_text` collapses whitespace and applies NFKC.
- [x] **NRM-02** `normalize_text` raises on empty/whitespace-only input, with the given code.
- [x] **NRM-03** `normalize_text` enforces `limit` and raises `<code>_too_long`.
- [x] **NRM-04** `canonical_json_bytes` sorts object keys recursively.
- [x] **NRM-05** `canonical_json_bytes` output is byte-identical for equal values built
      in different key orders.
- [x] **NRM-06** `canonical_json_bytes` preserves non-ASCII (no `\uXXXX` escaping).
- [x] **NRM-07** `_normalize_json` raises on a `None` value anywhere.
- [x] **NRM-08** Nested lists/dicts normalise recursively.
- [x] **NRM-09** Numbers and booleans pass through unchanged (no float coercion).
- [x] **NRM-10** Object keys are normalised and length-limited to 200.

Fingerprints:
- [x] **NRM-11** Identical facts produce identical fingerprints.
- [x] **NRM-12** `"improve at urban sketching"` and `"improve_at_urban_sketching"` share
      a fingerprint (the identity-fold regression). `[~]` slot-level only today
- [x] **NRM-13** Case variants share a fingerprint.
- [x] **NRM-14** Punctuation-only differences share a fingerprint.
- [x] **NRM-15** A different `owner_id` alone does **not** change a `sha256:` fingerprint
      (owner scoping is by column, not digest) — pin the current behaviour explicitly.
- [x] **NRM-16** Changing domain, slot, type, subject, or scope changes the fingerprint.
- [x] **NRM-17** `scope_project_id` participates in the fingerprint.
- [x] **NRM-18** SENSITIVE sensitivity produces `keyed:<version>:<digest>`.
- [x] **NRM-19** PROHIBITED raises `prohibited_content_not_persisted`.
- [x] **NRM-20** The keyed provider receives the owner id (assert on a spy).
- [x] **NRM-21** Fingerprint output shape matches the DB's `FINGERPRINT_LENGTH` bound.

Slot validation:
- [x] **NRM-22** GOAL exclusive slot must be exactly 3 parts with a known role.
- [x] **NRM-23** GOAL additive slot must be 4 parts, `independent`, UUID tail.
- [x] **NRM-24** PREFERENCE must be exclusive and 3 parts.
- [x] **NRM-25** IDENTITY must be global, exclusive, 3 parts.
- [x] **NRM-26** Additive non-goal must be `:item:<uuid>`.
- [x] **NRM-27** Exclusive non-goal must be EDUCATION/EMPLOYMENT `current_status`.
- [x] **NRM-28** A slot whose part 0 ≠ memory type raises `invalid_slot_identity`.
- [x] **NRM-29** A slot whose part 1 ≠ domain raises `invalid_slot_identity`.

Positive-value guard:
- [x] **NRM-30** Display text containing "no longer", "don't want", "used to",
      "stopped wanting", "did not want" raises `positive_current_fact_required`.
- [x] **NRM-31** "not only" is explicitly exempt.
- [x] **NRM-32** The guard is case-insensitive.
- [x] **NRM-33** Display text over 4000 chars raises.

Scope / metadata / versions:
- [x] **NRM-34** `scope_type="global"` with a `scope_project_id` raises `invalid_memory_scope`.
- [x] **NRM-35** `scope_type="project"` without a project id raises.
- [x] **NRM-36** An unsupported `value_schema_version` raises.
- [x] **NRM-37** `normalize_metadata` rejects keys outside `ALLOWED_METADATA_KEYS`.
- [x] **NRM-38** `normalize_metadata` rejects payloads over `MAX_METADATA_JSON_BYTES`.
- [x] **NRM-39** `normalize_metadata(None)` returns `{}`.
- [x] **NRM-40** `validate_command_versions` raises the right code for each of the three
      mismatched versions.
- [x] **NRM-41** Evidence text is normalised and capped at `MAX_EVIDENCE_TEXT_CHARS`.
- [x] **NRM-42** A naive `observed_at` is coerced to UTC; an aware one is untouched.

Refinement + request hash:
- [x] **NRM-43** `compatible_refinement` is True for equal values.
- [x] **NRM-44** True when the new string strictly contains the old (case-insensitively).
- [x] **NRM-45** False when the new string is shorter or unrelated.
- [x] **NRM-46** True when the new dict is a superset of the old.
- [x] **NRM-47** False when a shared dict key's value changed.
- [x] **NRM-48** False across mismatched types (str vs dict, list vs list).
- [x] **NRM-49** `operation_request_hash` is stable across equal commands and differs on
      any field change.
- [x] **NRM-50** `operation_request_hash` uses the keyed path for sensitive commands.
- [x] **NRM-51** Unknown-topic domains must be grounded in the candidate's own value,
      display text, or evidence.

### `policy.py` — POL

Extraction gate:
- [x] **POL-01** Pure-question turns are skipped. `test_extraction_gate.py`
- [x] **POL-02** First-person statements run. `test_extraction_gate.py`
- [x] **POL-03** A statement after a question still runs. `test_extraction_gate.py`
- [x] **POL-04** An explicit instruction without first person runs. `test_extraction_gate.py`
- [x] **POL-05** Each `_MEMORY_COMMAND` verb (remember / memorise / memorize / forget /
      save this / note that / call me) triggers the gate, and only at sentence start.
- [x] **POL-06** A memory verb mid-sentence ("what do you remember about…") does not.
- [x] **POL-07** Each `_CORRECTION` phrase triggers.
- [x] **POL-08** "me" alone does not trigger (documented exclusion).
- [x] **POL-09** Every `_QUESTION_OPENER` word is treated as a question opener.

Sensitivity classification:
- [x] **POL-10** Each `_PROHIBITED_PATTERN` classifies as PROHIBITED (one case each:
      password, OTP, api key/token/secret, `sk-` key, PEM private key).
- [x] **POL-11** A Luhn-valid card number is PROHIBITED.
- [x] **POL-12** A Luhn-invalid 16-digit run is not.
- [x] **POL-13** A repeated-digit run (`1111 1111 1111 1111`) is not.
- [x] **POL-14** Card detection handles spaces and hyphens, and rejects <13 / >19 digits.
- [x] **POL-15** Each `_SENSITIVE_PATTERN` classifies as SENSITIVE (diagnosis, national
      ID, bank account, street address).
- [x] **POL-16** **Regression:** "I have two cats" is NORMAL, not SENSITIVE.
- [x] **POL-17** "I have asthma" is SENSITIVE.
- [x] **POL-18** Ordinary facts are NORMAL.
- [x] **POL-19** Classification is case-insensitive.

Candidate policy:
- [x] **POL-20** Detected sensitivity escalates a proposal's declared sensitivity, never
      de-escalates it.
- [x] **POL-21** PROHIBITED → not allowed, `PROHIBITED_SENSITIVE_CONTENT`.
- [x] **POL-22** SENSITIVE without `explicit_user_request` → not allowed,
      `SENSITIVE_REQUIRES_EXPLICIT_REQUEST`.
- [x] **POL-23** SENSITIVE with explicit request → allowed.
- [x] **POL-24** The canonical value, not just display text, is scanned.

Recall / deletion / conflict / timing:
- [x] **POL-25** `can_recall_sensitivity`: PROHIBITED never, SENSITIVE only when directly
      relevant, NORMAL always.
- [x] **POL-26** `deletion_policy(FORGET)` retains a 30-day tombstone and blocks
      resurrection.
- [x] **POL-27** `deletion_policy(ERASE_PERMANENTLY)` removes provenance and audit, keeps
      no tombstone, does not block resurrection.
- [x] **POL-28** `deletion_policy` on any other operation raises.
- [x] **POL-29** Each automatic `ConflictEvidence` + deterministic target → replacement.
- [x] **POL-30** Automatic evidence + non-deterministic target → needs review.
- [x] **POL-31** `UNMARKED_INCOMPATIBLE_ASSERTION` always → needs review.
- [x] **POL-32** Explicit command / deterministic correction → `BEFORE_RESPONSE`; only the
      correction sets `use_current_turn_overlay`.
- [x] **POL-33** `AUTOMATIC_LLM` → `AFTER_TURN`, no overlay.
- [x] **POL-34** `guest_store_kind` maps the flag both ways.
- [x] **POL-35** `gate_memory_command` returns a DISABLED result under incognito.
- [x] **POL-36** …and under `memory_enabled=False`.
- [x] **POL-37** Incognito takes precedence when both are set.
- [x] **POL-38** A normal context returns `None` (no gate).
- [x] **POL-39** `MemoryCommandResult.disabled_for` rejects a non-disabled rejection code.
- [x] **POL-40** `PIN_POLICY` bypasses nothing — every `bypasses_*` flag is False.

### `contracts.py` — CON

- [x] **CON-01** `ContractModel` forbids extra fields, is frozen, strips whitespace
      (one assertion per property, on a representative model).
- [x] **CON-02** `OwnerId` canonicalises a mixed-case / braced UUID and rejects garbage.
- [x] **CON-03** `EvidenceSpan` requires start and end together.
- [x] **CON-04** `EvidenceSpan` rejects `end <= start`.
- [x] **CON-05** `EvidenceSpan` rejects negative start / zero end.
- [x] **CON-06** `CandidateProposal` rejects a `None` or blank canonical value.
- [x] **CON-07** `CandidateProposal` bounds: confidence 0–1, importance 1–10,
      `value_schema_version >= 1`.
- [x] **CON-08** `ValidatedCandidateProposal` rejects PROHIBITED sensitivity.
- [x] **CON-09** …and SENSITIVE without an explicit request.
- [x] **CON-10** `CandidateTargetHints.has_target_evidence` is True for each of the four
      evidence kinds and False when empty.
- [x] **CON-11** `CandidateGroundingSpan` enforces the 64-hex `content_hash` pattern.
- [x] **CON-12** `MemoryUpdatePatch` with no fields set raises.
- [x] **CON-13** `MemoryUpdatePatch` with an explicit `canonical_value=None` raises.
- [x] **CON-14** `MemoryUpdatePatch` distinguishes unset from explicitly-null.
- [x] **CON-15** `ReplaceMemoryCommand` requires a REPLACE-intent candidate.
- [x] **CON-16** …requires targets or target hints, unless authority is
      `GROUNDED_SAME_SLOT_ASSERTION`.
- [x] **CON-17** `SupersedeMemoryCommand` requires ≥1 predecessor.
- [x] **CON-18** `MergeMemoryCommand` requires ≥2 sources.
- [x] **CON-19** `RestoreMemoryCommand` AS_REPLACEMENT requires a candidate.
- [x] **CON-20** …ARCHIVED_ONLY forbids one.
- [x] **CON-21** `MEMORY_COMMAND_ADAPTER` discriminates all nine operations correctly.
- [x] **CON-22** …and rejects an unknown `operation` value.
- [x] **CON-23** `MemoryCommandResult` requires a rejection code for NEEDS_REVIEW /
      REJECTED / DISABLED.
- [x] **CON-24** …forbids one on any other outcome.
- [x] **CON-25** …requires an error code on FAILED and forbids it otherwise.
- [x] **CON-26** `SourceChangeResult` PRESERVED requires remaining support and no review.
- [x] **CON-27** …NEEDS_REVIEW requires zero remaining and review required.
- [x] **CON-28** …detached outcomes require a `detached_source_id`, others forbid it.
- [x] **CON-29** …not-found / owner-mismatch / revision-conflict cannot require review.
- [x] **CON-30** `PersistExtractionCandidateCommand` NEEDS_REVIEW requires outcome+code.
- [x] **CON-31** …VALIDATED forbids a decision.
- [x] **CON-32** …requires ≥1 source span.
- [x] **CON-33** `CandidateDecisionResult` requires a code for review/rejected states.
- [x] **CON-34** …requires an operation id when APPLIED.
- [x] **CON-35** Every command round-trips `model_dump(mode="json")` → re-parse.

### `crypto.py` / `local_crypto.py` — CRY

- [x] **CRY-01** `LocalMemoryCrypto` encrypt→decrypt round-trips.
- [x] **CRY-02** Decrypting with different associated data fails.
- [x] **CRY-03** A tampered ciphertext byte fails to decrypt.
- [x] **CRY-04** A tampered nonce fails.
- [x] **CRY-05** Two encryptions of the same plaintext use different nonces.
- [x] **CRY-06** `fingerprint` is deterministic per (material, owner) and differs across
      owners.
- [x] **CRY-07** Tombstone `create`/`verify` round-trip; verify rejects a wrong digest.
- [x] **CRY-08** Tombstone verify rejects a mismatched key version.
- [x] **CRY-09** Key-version getters return non-empty, stable strings.
- [x] **CRY-10** Encryption and fingerprint keys are derived distinctly (same material,
      different purpose → different key).
- [x] **CRY-11** Each `Unavailable*Provider` method raises `MemoryCryptoUnavailable`.
- [x] **CRY-12** `build_associated_data` binds owner and record identity, and changes
      when any component changes.

### `idempotency.py` — IDM

- [x] **IDM-01** Each of the eight constructors (http, review, chat, source_change,
      imported, agent, maintenance, manual) produces a stable key for stable inputs.
- [x] **IDM-02** Each varies when any input varies.
- [x] **IDM-03** Different surfaces never collide for the same material.
- [x] **IDM-04** Keys fit the DB's 200-char `idempotency_key` column.
- [x] **IDM-05** Keys are owner-scoped (owner change → key change).

### `tombstones.py` — TMB

- [x] **TMB-01** `tombstone_digest` is deterministic and owner-bound.
- [x] **TMB-02** `tombstone_expiration` is `created_at + FORGET_TOMBSTONE_DAYS`.
- [x] **TMB-03** `is_active` is True before expiry, False at/after it.
- [x] **TMB-04** `tombstone_matches` matches a re-derived digest for the same fact.
- [x] **TMB-05** …does not match a different fact.
- [x] **TMB-06** …does not match across owners.
- [x] **TMB-07** `resurrection_blocked` is True for an active, un-reconfirmed tombstone.
- [x] **TMB-08** False once expired.
- [x] **TMB-09** False once `explicitly_reconfirmed`.
- [x] **TMB-10** Key-version mismatch is handled without a crash.

---

## Tier 2 — Extraction pipeline

### `grounding.py` — GRD

- [x] **GRD-01** A span whose offsets match the message is accepted.
- [x] **GRD-02** Offsets that don't match but whose quoted text is findable are relocated.
- [x] **GRD-03** Quoted text absent from the message is rejected.
- [x] **GRD-04** Out-of-range offsets are rejected.
- [x] **GRD-05** `end <= start` is rejected.
- [x] **GRD-06** Matching is NFKC- and case-fold-tolerant.
- [x] **GRD-07** The video-verb equivalence (`_video_equivalent`) accepts the paraphrase
      it exists for, and nothing broader.
- [x] **GRD-08** `value_supported` accepts a value present in the cited span.
- [x] **GRD-09** …rejects a value the user never wrote.
- [x] **GRD-10** …handles a `None` value.
- [x] **GRD-11** `_span_hash` matches `ExtractionRequest.content_hash` for the same text.
- [x] **GRD-12** A span citing a message id not in the request is rejected.
- [x] **GRD-13** `ground_assertion` rejects a proposal with zero spans.
- [x] **GRD-14** `ground_retraction` accepts a grounded `old_value_hint`.
- [x] **GRD-15** …rejects an ungrounded one.
- [x] **GRD-16** Every rejection carries a non-empty machine-readable reason.

### `preparser.py` — PRE

One case per pattern, plus the ordering rules. All are deterministic and cheap.

- [x] **PRE-01** `_DIRECT_NAME` → identity/name candidate.
- [x] **PRE-02** `_DIRECT_AGE` → identity/age.
- [x] **PRE-03** `_DIRECT_ORIGIN` → identity/origin.
- [x] **PRE-04** `_DIRECT_EMPLOYER` → employment.
- [x] **PRE-05** `_DIRECT_OCCUPATION` → employment/occupation.
- [x] **PRE-06** `_DIRECT_PROJECT` → project. Both sides of the negative lookahead:
      "a project called Neo" is a project, "my fitness" is left to the model, because
      the projects field is a read-permission boundary.
- [x] **PRE-07** `_DIRECT_GOAL` ("I want to …") → goal. Outside a known domain the same
      pattern still matches but `deterministic` is False — the `kind` is identical either
      way, so that distinction is asserted directly.
- [x] **PRE-08** `_NOW_GOAL` ("Now I want …") → **AMBIGUOUS**, not a correction. The
      phrasing implies a predecessor but names none, so there is nothing to retract
      against. Also pins that this path skips `_video_verb` where every other goal path
      applies it.
- [x] **PRE-09** `_DIRECT_PREFERENCE` ("I prefer …") → preference.
- [x] **PRE-10** `_DOMAIN_PREFERENCE` scopes the preference to its domain.
- [x] **PRE-11** `_GLOBAL_STYLE` ("Always answer me …") → global response-style preference.
- [x] **PRE-12** `_GOAL_AND_GLOBAL_STYLE` yields both, not one — different domains and
      different slots, so neither absorbs the other.
- [x] **PRE-13** `_REMEMBER` prefix strips cleanly and marks explicit intent.
- [x] **PRE-14** `_ADDITIVE_GOALS` yields multiple independent goals, and retracts nothing.
- [x] **PRE-15** `_IMPLICIT_GOAL_CORRECTION` marks a replacement, both halves sharing one
      `correction_group`, and the retraction names the value *as stored* (`create …`, not
      the user's `make …`) or it would match nothing.
- [x] **PRE-16** `_EXPLICIT_REPLACE` marks a replacement with an old-value hint, and does
      not span sentences (the bounded character class that fixed a dropped correction).
- [x] **PRE-17** `_PREFERENCE_CORRECTION` marks a preference replacement.
- [x] **PRE-18** `_CATEGORY_CORRECTION` retargets the category — the only pattern that
      reads the conversation window. The span points at the *earlier* message, with the
      current turn attached as an additional span; an unresolvable reference is AMBIGUOUS
      rather than guessed.
- [x] **PRE-19** `_COMPOUND` corrections split into a retraction + an assertion, with
      distinct `correction_group`s per pair; one pair alone is not treated as compound.
- [x] **PRE-20** `_PURE_LOCATION_RETRACTION` retracts without asserting, and archives
      rather than forgets — the user moved, they were not misrecorded.
- [x] **PRE-21** `_CURRENT_LOCATION` asserts a durable location.
- [x] **PRE-22** `_TRANSIENT_LOCATION` ("I'm in Paris this week") does **not** persist.
- [x] **PRE-23** `_TEMPORARY` phrasing is not durable, and the exemption list rescues a
      standing preference stated with temporary wording.
- [x] **PRE-24** `_HYPOTHETICAL` ("if I were…") produces nothing.
- [x] **PRE-25** `_THIRD_PARTY` ("my brother likes…") produces nothing about the user.
- [x] **PRE-26** `_AMBIGUOUS_PRONOUN` opener ("That is my favourite") produces nothing.
- [x] **PRE-27** `_EXPLICIT_LIFECYCLE` (forget/delete) yields a retraction.
- [x] **PRE-28** `_has_multiple_statements` splits a compound turn, so one hedging word
      cannot discard a real fact stated in the next sentence.
- [x] **PRE-29** Every produced span's offsets actually index the source text.
- [x] **PRE-30** `preparse` on an empty/whitespace message returns no proposals.
- [x] **PRE-31** `preparse` is deterministic (same input twice → identical result).
- [x] **PRE-32** `deterministic_model_response` converts a `PreparseResult` into a valid
      `ModelProposalResponse` that passes the model schema.
- [x] **PRE-33** A message matching two patterns resolves by the documented precedence,
      not by dict order.
- [x] **PRE-34** `_video_verb` normalisation covers the phrasings it claims, rewrites only
      a leading verb, and folds a goal and its later retraction to one value.

### `correction_resolver.py` — COR

Candidate building:
- [x] **COR-01** An ungrounded display hint falls back to the typed value.
      `test_forget_and_duplicates.py`
- [x] **COR-02** A grounded display hint is kept. `test_forget_and_duplicates.py`
- [x] **COR-03** The same value restated later reuses its slot.
      `test_forget_and_duplicates.py`
- [x] **COR-04** Slug and prose forms share a slot. `test_forget_and_duplicates.py`
- [x] **COR-05** Case variants share a slot. `test_forget_and_duplicates.py`
- [x] **COR-06** Distinct values keep distinct slots. `test_forget_and_duplicates.py`
- [x] **COR-07** `_value_entity_id` is a pure function of the folded value — stable across
      candidate ids, shared by the slug and prose forms, distinct per domain.
- [x] **COR-08** `_entity_token` output is stable and inside `_SLOT_TOKEN_ALPHABET`. Run
      over many random UUIDs, since the Luhn false positive it prevents is data-dependent.
- [x] **COR-09** `_candidate_uuid` is deterministic per (request, proposal id), varies with
      owner and message id, and does *not* depend on the message text.
- [x] **COR-10** `_sensitivity` escalates from the policy classifier, not just the hint.
- [x] **COR-11** `_domain_for` uses the model's hint when it resolves, else the source text,
      and overrides a `global` hint for topic-specific preference wording.
- [x] **COR-12** ~~`_domain_for` fails closed when neither grounds a domain.~~ **Corrected:**
      it defaults to `global`, deliberately — a domain is an organising facet, and losing a
      durable fact over an unrecognised label is the worse outcome. `resolve_domain` does
      fail closed (TAX-04); `_domain_for` catches it. Value grounding is enforced separately
      by `ground_assertion`. See `decisions.md` 35.
- [x] **COR-13** `build_candidate` returns a reason (not a bare `None`) on every failure,
      including the success path (`candidate_normalized`).
- [x] **COR-14** A candidate built from a SENSITIVE assertion without explicit request is
      not returned as validated — and the same fact *is* accepted with one, so the guard
      keys on intent rather than rejecting all sensitive content.

Resolution:
- [x] **COR-15** Same value + same slot → RECONFIRM. `test_forget_and_duplicates.py`
- [x] **COR-16** Same value + different slot → RECONFIRM (targets the existing record).
      `test_forget_and_duplicates.py`
- [x] **COR-17** A different value → CREATE. `test_forget_and_duplicates.py`
- [x] **COR-18** ~~A refined value on an exclusive slot → REFINE, not CREATE.~~
      **Corrected:** `resolve` never returns REFINE and should not — refinement is a
      *planning* decision made against the stored record (`MemoryOutcome.REFINED`,
      PLN-03), not a resolution decision made against a snapshot list. A refinement here
      resolves to NEEDS_REVIEW. Leaves `CorrectionResolutionKind.REFINE` an unused enum
      member; flagged, not removed. See `decisions.md` 38.
- [x] **COR-19** An incompatible value on an exclusive slot with correction evidence →
      REPLACE.
- [x] **COR-20** …without correction evidence → NEEDS_REVIEW, with the occupant
      returned as context rather than replaced on an assumption.
- [x] **COR-21** An additive slot never replaces; it creates alongside.
- [x] **COR-22** Resolution against an empty existing set is always CREATE.
- [x] **COR-23** ~~Resolution ignores non-active snapshots.~~ **Corrected:** it does not —
      `resolve` never reads `status`. Hand it an archived record and it matches. The
      guarantee is the caller's (`list_active_records`, MUT-34). Pinned as a trust
      boundary. See `decisions.md` 39.
- [x] **COR-24** ~~Resolution never targets another owner's snapshot.~~ **Corrected:** it
      never reads `owner_id` either, on both the assertion and retraction paths. Same
      trust boundary as COR-23; owner scoping is held by the repository.

Retraction:
- [x] **COR-25** A value named inside a sentence retracts. `test_forget_and_duplicates.py`
- [x] **COR-26** Every duplicate of the value is retracted. `test_forget_and_duplicates.py`
- [x] **COR-27** An exact value retracts. `test_forget_and_duplicates.py`
- [x] **COR-28** One word of a longer phrase does not over-delete → NEEDS_REVIEW.
      `test_forget_and_duplicates.py`
- [x] **COR-29** An unrelated memory is never retracted. `test_forget_and_duplicates.py`
- [x] **COR-30** "Forget A and B" removes both when stored separately.
      `test_forget_and_duplicates.py`
- [x] **COR-31** A fragment of a longer match is left alone. `test_forget_and_duplicates.py`
- [x] **COR-32** A retraction whose hint matches nothing → NEEDS_REVIEW, no targets. The
      literally-hintless case is unreachable: `old_value_hint` is required with
      `min_length=1`, so the contract refuses it before any resolver runs.
- [x] **COR-33** Retraction matching is case- and punctuation-insensitive, and
      whitespace- and underscore-insensitive.
- [x] **COR-34** Retraction across memory types works when the value matches, and a type
      hint narrows the eligible set when the model supplies one.
- [x] **COR-35** ~~Retraction never crosses owners.~~ **Corrected:** `resolve_retraction`
      does not check owner; a foreign snapshot passed in is returned as a RETRACT target.
      Owner scoping is enforced before the call. Covered as a trust boundary.

### `model_schema.py` / `extraction_contracts.py` — MSC

- [x] **MSC-01** `ModelAssertionProposal` requires ≥1 source span.
- [x] **MSC-02** …bounds confidence 0–1.
- [x] **MSC-03** …rejects an empty typed value.
- [x] **MSC-04** `ModelRetractionProposal` requires an old-value hint or explicit forget.
- [x] **MSC-05** `ModelSourceSpan` rejects `end <= start` and negative offsets.
- [x] **MSC-06** `ModelProposalResponse` rejects unknown fields (the schema is `forbid`).
- [x] **MSC-07** A model response above the candidate cap is truncated, not rejected.
- [x] **MSC-08** `ExtractionRequest.content_hash` is stable, NFKC-normalised, 64-hex.
- [x] **MSC-09** `ExtractionRequest` rejects a message over `extraction_max_input_chars`.
- [x] **MSC-10** Every `ExtractionMode` / `ExtractionStatus` / `CandidateAction` value is
      reachable from the coordinator (cross-checked in EXC).
- [x] **MSC-11** Contract models are frozen and reject extras.

### `extraction.py` — EXT

Transport and provider behaviour, all against a fake transport — no live model.

- [x] **EXT-01** `FixtureExtractionModel` returns the scripted response.
- [x] **EXT-02** A response over `MAX_PROVIDER_RESPONSE_BYTES` is rejected.
- [x] **EXT-03** A connect timeout raises `ExtractionModelTimeout` with stage `connect`.
- [x] **EXT-04** A response timeout raises with stage `response`.
- [x] **EXT-05** A transport failure raises `ExtractionModelError`, not a bare `OSError`.
- [x] **EXT-06** Error messages are sanitised to `MAX_SANITIZED_ERROR_CHARS`.
- [x] **EXT-07** Error messages never contain the user's message text.
- [x] **EXT-08** `_ollama_failure_code` maps 404 / 400 / 500 / connection error to
      distinct, stable codes.
- [x] **EXT-09** A "model not found" body maps to its own code (this was a real
      deployment failure).
- [x] **EXT-10** `DirectJsonExtractionProvider` posts the expected body shape.
- [x] **EXT-11** `OllamaChatExtractionProvider` uses `format: json` in `ollama_json` mode.
- [x] **EXT-12** …uses the JSON schema in `ollama_schema` mode.
- [x] **EXT-13** `auto` mode resolves via the probe result.
- [x] **EXT-14** A non-JSON body is rejected with a clear code, not a `JSONDecodeError`.
- [x] **EXT-15** A JSON body that isn't a valid `ModelProposalResponse` is rejected.
- [x] **EXT-16** A response wrapped in markdown fences is still decoded (if supported) or
      cleanly rejected (if not) — pin whichever is true.
- [x] **EXT-17** `probe_ollama_provider` reports capabilities from a scripted `/api/tags`
      and `/api/show`.
- [x] **EXT-18** …degrades gracefully when the endpoint is unreachable.
- [x] **EXT-19** The synthetic probe input never reaches a persisted candidate.
- [x] **EXT-20** `build_extraction_model_provider` returns the fixture model when live
      extraction is disabled.
- [x] **EXT-21** …raises a configuration error for an unknown provider name.
- [x] **EXT-22** `ProviderResponseMetadata` records model, duration, and byte count.

### `extraction_coordinator.py` — EXC

The integration seam. Uses a scripted model and a real (in-memory) store.

- [x] **EXC-01** A disabled memory setting short-circuits to `DISABLED`.
- [x] **EXC-02** Incognito short-circuits to `DISABLED`.
- [x] **EXC-03** A turn failing `turn_may_contain_memory` returns `NO_ACTION` without
      calling the model (assert the model was not invoked).
- [x] **EXC-04** A deterministic preparse result skips the model entirely.
- [x] **EXC-05** A model failure degrades to `FAILED` without raising to the caller.
- [x] **EXC-06** A model timeout degrades to `FAILED`/`DEFERRED`, never a 500.
- [x] **EXC-07** Automatic candidates are capped at
      `MAX_AUTOMATIC_CANDIDATES_PER_TURN`.
- [x] **EXC-08** Explicit batches are capped at `MAX_EXPLICIT_CANDIDATES_PER_BATCH`.
- [x] **EXC-09** Exceeding the cap sets `TOO_MANY_CANDIDATES`, not a silent drop.
- [x] **EXC-10** Nested assertions are dropped. `test_nested_assertions.py`
- [x] **EXC-11** An ungrounded proposal is rejected with `UNGROUNDED_CANDIDATE`.
- [x] **EXC-12** A semantic duplicate is not stored twice. `test_semantic_duplicate.py`
- [ ] **EXC-13** …and the duplicate check is owner-scoped.
- [~] **EXC-14** …and respects the similarity threshold at both sides of the boundary.
      Covered: the finder is not consulted with nothing to compare against, and a
      failing finder never loses the memory. The threshold boundary itself still
      needs the fixed-dimension embedding fake (INF-07).
- [x] **EXC-15** A PROHIBITED candidate is never persisted, and the rejection is recorded.
- [x] **EXC-16** A SENSITIVE candidate without explicit request goes to review, redacted
      as `REDACTED_SENSITIVE_ASSERTION`.
- [~] **EXC-17** A SENSITIVE candidate with explicit request is persisted encrypted.
      The redaction and hash-suppression halves are covered; the at-rest encryption
      assertion belongs with the payload tests.
- [x] **EXC-18** A retraction resolving to one target applies a forget.
- [ ] **EXC-19** A retraction resolving to many targets forgets all of them.
      Unblocked: SCH-14 is staying pinned rather than fixed, so two active records in
      one exclusive slot is a state the store genuinely permits today. Set it up by
      direct insert and assert the retraction clears both.
- [x] **EXC-20** An unresolved retraction becomes a review item.
- [x] **EXC-21** `CurrentTurnOverrideBuilder` records candidate targets, unresolved hints,
      and final outcomes.
- [x] **EXC-22** …builds an override bound to the right owner and message.
- [x] **EXC-23** …de-duplicates repeated targets.
- [x] **EXC-24** The same request processed twice is idempotent (no duplicate records).
- [x] **EXC-25** Two different messages asserting the same fact reconfirm rather than
      duplicate.
- [x] **EXC-26** `_timing` picks foreground vs post-turn per the policy.
- [x] **EXC-27** The result's status matches what was actually persisted (no
      `APPLIED` with zero writes).
- [x] **EXC-28** Extraction never writes to another owner's store.
- [x] **EXC-29** A model returning a proposal citing an unknown message id is rejected.
- [x] **EXC-30** A model returning a proposal quoting text absent from the message is
      rejected (prompt-injection shape).

### `extraction_diagnostics.py` — EXD

- [x] **EXD-01** Diagnostics record model name, latency, proposal counts, and outcome.
- [x] **EXD-02** Diagnostics never contain raw user message text.
- [x] **EXD-03** Diagnostics are emitted on the failure path too.

### `history_redaction` — RED

- [x] **RED-01..n** Covered by `test_history_redaction.py` — re-audit once the rest of the
      suite exists to confirm every redaction site is exercised.

---

## Tier 3 — Persistence

### `db/memory_migrations.py` — MIG

- [x] **MIG-01** `upgrade_memory` on an empty database creates every table in
      `MEMORY_TABLES`.
- [x] **MIG-02** …records all three revisions in the ledger with checksums.
- [x] **MIG-03** …binds the owner in `memory_owner_bindings`.
- [x] **MIG-04** Running upgrade twice is a no-op and does not re-run migrations.
- [x] **MIG-05** Upgrading with a different `owner_id` against a bound database raises.
- [x] **MIG-06** Upgrading with a different `database_identity` raises.
- [x] **MIG-07** A tampered ledger checksum raises `MemoryMigrationError`.
- [x] **MIG-08** An unknown revision in the ledger raises.
- [x] **MIG-09** A managed table missing while its revision is recorded raises.
- [x] **MIG-10** `memory_migration_state` reports current revision and applied set.
- [x] **MIG-11** `downgrade_memory` drops every managed table and clears the ledger.
- [x] **MIG-12** Downgrade then upgrade returns to an identical schema checksum.
- [x] **MIG-13** The upgrade runs in one transaction — a mid-way failure leaves nothing
      behind.
- [x] **MIG-14** `MEMORY_CURRENT_REVISION` equals the last entry in the revision list.

### `models/memory.py` — DDL constraints — SCH

Every `CheckConstraint` and unique index deserves one rejecting case. Run against real
SQLite so the constraints are actually enforced.

- [x] **SCH-01** `_uuid_check` rejects uppercase, wrong length, missing dashes, and
      non-hex characters — on `memory_records.id` as the representative, plus one case
      per table to prove the constraint is present.
- [x] **SCH-02** `memory_records.memory_type` rejects an unknown value.
- [x] **SCH-03** …`cardinality`, `sensitivity`, `status` likewise.
- [x] **SCH-04** `confidence` outside 0–1 is rejected.
- [x] **SCH-05** `importance` outside 1–10 is rejected.
- [x] **SCH-06** `usage_count < 0` rejected.
- [x] **SCH-07** `revision < 1` rejected.
- [x] **SCH-08** Zero schema versions rejected.
- [x] **SCH-09** NORMAL payload shape: plaintext required, all crypto columns null.
- [x] **SCH-10** NORMAL with a non-null encrypted column is rejected.
- [x] **SCH-11** NORMAL with blank display text is rejected.
- [x] **SCH-12** SENSITIVE payload shape: ciphertext + algorithm + key version + both
      nonces + AAD required, plaintext null.
- [x] **SCH-13** SENSITIVE missing any one crypto column is rejected (parametrised).
- [x] **SCH-14** The active-exclusive-slot unique index blocks a second active exclusive
      record on the same slot.
- [x] **SCH-15** …but allows one once the first is superseded/archived/forgotten.
- [x] **SCH-16** …and is owner-scoped (two owners, same slot, both active is fine).
- [x] **SCH-17** …and scope-scoped (global vs project).
- [x] **SCH-18** The active-fingerprint unique index blocks a duplicate active record.
- [x] **SCH-19** …allows a duplicate once inactive.
- [x] **SCH-20** `fk_memory_records_creating_operation` rejects an operation from another
      owner (cross-owner reference).
- [x] **SCH-21** `memory_candidates` sensitive-explicit constraint: SENSITIVE requires
      `explicit_user_request`.
- [x] **SCH-22** `memory_operations` unique `(owner_id, idempotency_key)`.
- [x] **SCH-23** `memory_operations` payload shape by sensitivity.
- [x] **SCH-24** `memory_operations.status` / `outcome` / `rejection_code` / `error_code`
      enum checks.
- [x] **SCH-25** `memory_sources` excerpt shape: all-null, plaintext-only, or
      encrypted-complete; any mixed shape is rejected.
- [x] **SCH-26** `memory_sources` unique `(owner, memory_id, source_content_hash)`.
- [x] **SCH-27** `memory_sources` cascades on record delete.
- [x] **SCH-28** `memory_relations` rejects a self-relation.
- [x] **SCH-29** `memory_relations` unique on (owner, from, type, to).
- [x] **SCH-30** `memory_relations` cascades from both sides.
- [x] **SCH-31** `memory_usage_events` unique `(owner, request_id, memory_id, purpose)`.
- [x] **SCH-32** `memory_outbox` requires `memory_id` for canonical/usage kinds.
- [x] **SCH-33** …allows it null for `reconciliation_request` / `tombstone_expiry`.
- [x] **SCH-34** `memory_outbox` unique `(owner, event_idempotency_key)`.
- [x] **SCH-35** `memory_outbox.attempts >= 0`, `canonical_revision > 0` when present.
- [x] **SCH-36** `memory_outbox_deliveries` unique `(owner, event, target)`.
- [x] **SCH-37** …cascades when the event is deleted.
- [x] **SCH-38** `memory_health_state` unique `(owner, memory, target)`.
- [x] **SCH-39** …`content_hash` must be 64 lowercase hex when present.
- [x] **SCH-40** `memory_health_metrics` unique `(owner, metric_code)`, count ≥ 0.
- [x] **SCH-41** `memory_fts_documents` unique per memory; content hash is 64-hex.
- [x] **SCH-42** `memory_vector_points` unique per memory; `dimension > 0`; both hashes
      64-hex.
- [x] **SCH-43** `memory_tombstones` requires `expires_at > created_at`.
- [x] **SCH-44** `memory_tombstones` unique `(owner, digest, key_version)`.
- [x] **SCH-45** `memory_owner_bindings` unique on `database_identity`; blank identity
      rejected.
- [x] **SCH-46** Every `DERIVED_TARGET_STATES` value is accepted by both state columns.
- [x] **SCH-47** Every `OUTBOX_EVENT_KINDS` / `OUTBOX_STATES` / `RELATION_TYPES` /
      `SOURCE_ASSERTION_ROLES` value is accepted, and one invalid value each is rejected.

### `repositories/memory.py` — REP

- [x] **REP-01** Constructing against an unbound database raises `MemoryBindingError`.
- [x] **REP-02** …against a database bound to a different owner raises.
- [x] **REP-03** …against a different `database_identity` raises.
- [x] **REP-04** `_require_owned` raises on an entity from another owner.
- [x] **REP-05** `get_record` returns None for an unknown id (no raise).
- [x] **REP-06** `get_record` never returns another owner's record.
- [x] **REP-07** `_reject_prohibited_material` raises `MemoryProhibitedContentError` on a
      prohibited value reaching the repository.
- [x] **REP-08** `_validated_statuses` rejects an unknown status string (no SQL
      injection surface).
- [x] **REP-09** `eligible_records_statement` excludes inactive records.
- [x] **REP-10** …excludes expired records.
- [x] **REP-11** …respects the scope filter (global vs project).
- [x] **REP-12** `recall_filter_counts` reports inactive / expired / sensitivity /
      domain-filtered counts that sum consistently with the returned set.
- [x] **REP-13** `list_recall_eligible` respects the limit.
- [x] **REP-14** `get_recall_eligible_by_id` returns None for an ineligible record.
- [x] **REP-15** `find_recall_eligible_slot` matches an exact slot only.
- [x] **REP-16** `list_recall_eligible_for_slots` returns one record per trusted slot.
- [x] **REP-17** `active_source_ids_for_records` returns only active sources.
- [x] **REP-18** `record_recall_usage` writes one usage event per memory.
- [x] **REP-19** …is idempotent on `(request_id, memory_id, purpose)`.
- [x] **REP-20** …increments `usage_count` and sets `last_used_at`.
- [x] **REP-21** `find_active_slot` / `find_active_fingerprint` return only active rows.
- [x] **REP-22** `get_operation_by_idempotency_key` returns the prior operation.
- [x] **REP-23** `add_record` rejects a record whose owner differs.
- [x] **REP-24** `add_source` / `add_relation` / `add_outbox_event` / `add_tombstone`
      each reject cross-owner references.
- [x] **REP-25** `add_relation` rejects a reference to a nonexistent record.
- [x] **REP-26** `update_record_fields` rejects a field outside
      `_UPDATABLE_RECORD_FIELDS`.
- [x] **REP-27** …rejects metadata keys outside `ALLOWED_RECORD_METADATA_KEYS`.
- [x] **REP-28** …bumps `revision` and `updated_at`.
- [x] **REP-29** …raises `MemoryRevisionConflict` on a stale expected revision.
- [x] **REP-30** `update_candidate_decision` rejects a field outside
      `_UPDATABLE_CANDIDATE_FIELDS`.
- [x] **REP-31** …raises on a stale candidate revision.
- [x] **REP-32** `delete_tombstone` returns False for an unknown id.
- [x] **REP-33** `list_index_candidates` returns records needing (re)indexing only.
- [x] **REP-34** `get_owner_record_any_lifecycle` finds forgotten records that
      `get_record` hides.

### `planner.py` — PLN

Pure planning — given state and a command, produce the right spec set. No I/O.

- [x] **PLN-01** `plan_create` on an empty slot → one record create, one source, one
      outbox upsert.
- [x] **PLN-02** `plan_create` hitting an active exclusive slot with an equal value →
      reconfirm (update `last_confirmed_at`, no new record).
- [x] **PLN-03** …with a compatible refinement → refine update, revision bump.
- [x] **PLN-04** …with an incompatible value → `CONFLICT_REQUIRES_REPLACE` rejection.
- [x] **PLN-05** `plan_create` on an additive slot always creates.
- [x] **PLN-06** `plan_create` matching an active fingerprint → reconfirm, never a
      duplicate row.
- [x] **PLN-07** `plan_create` blocked by an active tombstone → `RESURRECTION_BLOCKED`.
- [x] **PLN-08** …allowed when the candidate is an explicit user request (reconfirmation).
- [x] **PLN-09** `plan_update` requires an expected revision.
- [x] **PLN-10** …rejects a stale revision with `REVISION_CONFLICT`.
- [x] **PLN-11** …on a missing record → `NOT_FOUND`.
- [x] **PLN-12** …on another owner's record → `CROSS_OWNER_REFERENCE`.
- [x] **PLN-13** …applies only the patched fields.
- [x] **PLN-14** …changing the canonical value recomputes the fingerprint and emits an
      outbox upsert.
- [x] **PLN-15** …changing only `pinned` does not emit a canonical upsert.
- [x] **PLN-16** `plan_replace` supersedes the target and creates the successor, with a
      `supersedes` relation.
- [x] **PLN-17** …with `GROUNDED_SAME_SLOT_ASSERTION` and no explicit target resolves the
      slot occupant.
- [x] **PLN-18** …with no resolvable target → `REPLACEMENT_TARGET_NOT_FOUND`.
- [x] **PLN-19** …with several possible targets → `AMBIGUOUS_CONFLICT`.
- [x] **PLN-20** `plan_supersede` marks every predecessor superseded and relates them to
      the successor.
- [x] **PLN-21** …rejects a successor that is not active.
- [x] **PLN-22** …rejects a predecessor equal to the successor.
- [x] **PLN-23** `plan_merge` archives/supersedes all sources and creates one record with
      `merged_from` relations.
- [x] **PLN-24** …requires all sources to be active.
- [x] **PLN-25** `plan_archive` sets ARCHIVED and emits a canonical remove.
- [x] **PLN-26** …on an already-archived record is a no-op, not an error.
- [x] **PLN-27** `plan_forget` sets FORGOTTEN, emits a remove, creates a tombstone,
      keeps provenance.
- [x] **PLN-28** …tombstone expiry is `FORGET_TOMBSTONE_DAYS` out.
- [x] **PLN-29** `plan_erase` deletes the record, sources, and relations, and creates no
      tombstone.
- [x] **PLN-30** `plan_restore` ARCHIVED_ONLY restores an archived record to ACTIVE.
- [x] **PLN-31** …refuses to restore a forgotten record → `INVALID_RESTORE`.
- [x] **PLN-32** …refuses when the slot is now occupied.
- [x] **PLN-33** `plan_restore` AS_REPLACEMENT restores and supersedes the occupant.
- [x] **PLN-34** …reconfirms the tombstone rather than leaving it blocking.
- [x] **PLN-35** Every plan carries preconditions matching the records it reads
      (`_state_guard` coverage).
- [x] **PLN-36** `mutates_canonical_state` is True exactly for the plans that change a
      record.
- [x] **PLN-37** `_plan_uuid` is deterministic per (operation id, label) so replays reuse
      ids.
- [x] **PLN-38** Every rejection path sets a rejection code and leaves the existing record
      active.
- [x] **PLN-39** A dry-run command produces a plan but marks it non-committing.

### `mutations.py` — MUT

The transactional boundary. Real SQLite, real transactions.

- [x] **MUT-01** `execute` accepts both a typed command and a raw dict.
- [x] **MUT-02** An invalid dict → `INVALID_COMMAND`, no rows written.
- [x] **MUT-03** A successful create writes record + operation + source + outbox atomically.
- [x] **MUT-04** A failure mid-apply rolls everything back (use `InjectedMutationFailure`
      at each `_inject` stage — parametrised over every stage name).
- [x] **MUT-05** Replaying the same idempotency key returns the identical result without
      writing again.
- [x] **MUT-06** A replay with a different request hash → `IDEMPOTENCY_CONFLICT`.
- [x] **MUT-07** The replay envelope round-trips a result faithfully
      (`_encode`/`_decode_replay_envelope`).
- [x] **MUT-08** A corrupt replay envelope is handled, not crashed on.
- [x] **MUT-09** `_preflight_idempotency` short-circuits before doing work.
- [x] **MUT-10** A prohibited candidate is persisted as a rejection operation and no
      record (`_persist_prohibited_rejection`).
- [x] **MUT-11** …and the rejection operation stores no prohibited text.
- [x] **MUT-12** A sensitive command encrypts the stored command payload.
- [x] **MUT-13** …and the plaintext never appears in `memory_operations`.
- [x] **MUT-14** Sensitive record payloads are encrypted, with AAD binding owner+record.
- [x] **MUT-15** `_decrypt_record` restores the exact original value.
- [x] **MUT-16** Decryption with the wrong key fails loudly rather than returning garbage.
- [x] **MUT-17** A SQLite `database is locked` error is retried per `RetryPolicy`.
- [x] **MUT-18** `RetryPolicy.delay_for_attempt` grows monotonically and is bounded.
- [x] **MUT-19** `RetryPolicy` rejects invalid construction (`__post_init__`).
- [x] **MUT-20** Retries exhaust into a FAILED result, not an exception.
- [x] **MUT-21** A concurrent revision change between plan and commit raises `_PlanChanged`
      and re-plans.
- [x] **MUT-22** Two concurrent creates on one exclusive slot: one wins, one is rejected —
      never two active rows.
- [x] **MUT-23** `dry_run=True` produces a result and writes nothing.
- [x] **MUT-24** Every operation kind writes an operation row with the right kind, actor,
      source kind, and versions.
- [x] **MUT-25** `committed_at` is set exactly on committed operations.
- [x] **MUT-26** `result_record_ids` matches the records actually touched.
- [x] **MUT-27** `detach_source` on the last active source → NEEDS_REVIEW, canonical
      unchanged.
- [x] **MUT-28** …with sources remaining → PRESERVED.
- [x] **MUT-29** …on an already-detached source → ALREADY_DETACHED.
- [x] **MUT-30** …on an unknown source → SOURCE_NOT_FOUND.
- [x] **MUT-31** …on another owner's source → OWNER_MISMATCH.
- [x] **MUT-32** …with a stale revision → REVISION_CONFLICT.
- [x] **MUT-33** …never changes `canonical_revision` in any outcome.
- [x] **MUT-34** `list_active_records` returns only this owner's active records.
- [x] **MUT-35** `candidate_status` returns None for unknown, a snapshot otherwise.
- [x] **MUT-36** `reject_candidate` sets REJECTED with `USER_REJECTED`.
- [x] **MUT-37** …is idempotent on a second call.
- [x] **MUT-38** …refuses to reject an already-applied candidate.
- [x] **MUT-39** `persist_extraction_candidate` stores a validated candidate.
- [x] **MUT-40** …returns `ALREADY_EXISTS` on a repeat.
- [x] **MUT-41** …returns `PROHIBITED` for prohibited content.
- [x] **MUT-42** …stores a needs-review candidate with its outcome and code.
- [x] **MUT-43** `_normalization_error_code` maps every `MemoryNormalizationError` code
      to a `MemoryErrorCode` (parametrised over the full code set — no `UNKNOWN` leaks).
- [x] **MUT-44** `_derived_state_for_outcome` maps every outcome.
- [x] **MUT-45** `_redacted_command` strips payload text from what gets logged.
- [x] **MUT-46** An erase removes the record and its sources, and leaves no orphan
      relation.
- [x] **MUT-47** `_erase_record` on a stale revision refuses.
- [x] **MUT-48** Owner mismatch between context and command → `OWNER_MISMATCH`, nothing
      written.

### `coordinator.py` / `adapters.py` — ADP

- [ ] **ADP-01** `MemoryExecutionContext` validates its owner id.
- [ ] **ADP-02** An invalid context raises `MemoryCoordinationError` before any DB work.
- [ ] **ADP-03** `execute` gates on incognito / disabled before building a service.
- [ ] **ADP-04** Each `GenericMemoryAdapter` verb (create, update, replace, merge,
      archive, forget, erase, restore, supersede) builds the right command — one case
      each, asserting operation kind, actor kind, source kind, and idempotency surface.
- [ ] **ADP-05** `TypedMemoryAdapter.create_typed` derives slot and cardinality correctly.
- [ ] **ADP-06** `CandidateReviewAdapter.apply` ACCEPT applies the candidate and links the
      operation.
- [ ] **ADP-07** …REJECT records the rejection.
- [ ] **ADP-08** …on an already-decided candidate is idempotent.
- [ ] **ADP-09** …uses the review idempotency surface (a repeat is a no-op).
- [ ] **ADP-10** `ChatMemoryAdapter.apply_structured_candidate` uses the chat surface.
- [ ] **ADP-11** `apply_structured_replacement` sets REPLACE intent and authority.
- [ ] **ADP-12** `ImportMemoryAdapter.accept` uses the import surface and per-item hash.
- [ ] **ADP-13** `MaintenanceMemoryAdapter.archive_proposal` uses the maintenance actor.
- [ ] **ADP-14** `AgentMemoryAdapter.create_from_tool` uses the agent surface and tool
      call id.
- [ ] **ADP-15** `structured_item_hash` is stable and order-independent.
- [ ] **ADP-16** `MemoryAdapterContext.actor` / `.source` produce valid contract models.
- [ ] **ADP-17** `_validated_candidate` rejects a proposal that can't be validated.
- [ ] **ADP-18** Every adapter refuses a cross-owner request.

### `source_changes.py` — SRC

- [ ] **SRC-01** `delete_message_source` detaches every source row for the message.
- [ ] **SRC-02** A message with no sources is a clean no-op.
- [ ] **SRC-03** A memory losing its last source is flagged for review.
- [ ] **SRC-04** Canonical state is never mutated.
- [ ] **SRC-05** The operation is owner-scoped.

---

## Tier 4 — Derived indexes and the async path

### `indexes.py` — IDX

- [x] **IDX-01** `DerivedDocumentBuilder.build` returns None for a record that shouldn't
      be indexed (inactive / sensitive / expired) — one case each.
- [x] **IDX-02** …produces a stable `content_hash` for identical input.
- [x] **IDX-03** …changes the hash when display text, type, domain, or slot changes.
- [x] **IDX-04** `build_embedding` produces a stable embedding document and hash.
- [x] **IDX-05** The embedding document differs from the FTS document (different
      versions/identity).
- [x] **IDX-06** `SqliteMemoryFtsIndex._is_available` is False without FTS5 and every
      call then raises a clear error rather than corrupting state.
- [x] **IDX-07** `upsert` inserts, then updates in place (one row per memory).
- [x] **IDX-08** `delete` with a matching expected hash removes the row and returns True.
- [x] **IDX-09** `delete` with a stale expected hash returns False and keeps the row.
- [x] **IDX-10** `delete` of an absent row returns False.
- [x] **IDX-11** `search` returns owner-scoped results only.
- [x] **IDX-12** `search` respects the limit and orders by relevance.
- [x] **IDX-13** `search` handles FTS metacharacters (`"`, `*`, `NEAR`) without raising.
- [x] **IDX-14** `search` with an empty query returns nothing.
- [x] **IDX-15** `get_metadata` / `list_metadata_for_owner` round-trip what `upsert` wrote.
- [x] **IDX-16** `clear_owner` removes only that owner's rows, returning the count.
- [x] **IDX-17** `health` reports availability.
- [x] **IDX-18** `SqliteMemoryVectorIndex.upsert` stores the vector and its dimension.
- [x] **IDX-19** …rejects a dimension mismatch against the stored provider dimension.
- [x] **IDX-20** `search` ranks by cosine similarity, owner-scoped.
- [x] **IDX-21** `_cosine` returns 1.0 for identical vectors, 0.0 for orthogonal, and
      handles a zero vector without dividing by zero.
- [x] **IDX-22** `_cosine` on mismatched lengths raises rather than truncating.
- [x] **IDX-23** Vector `delete` honours the expected hash the same way FTS does.
- [x] **IDX-24** `clear_owner` on the vector index is owner-scoped.
- [x] **IDX-25** Neither index ever returns a row for a different owner, even when asked
      directly by memory id.

### `outbox.py` — OBX

- [x] **OBX-01** `enabled_targets` reflects the FTS/vector settings.
- [x] **OBX-02** `lease_batch` leases up to the batch size and sets worker id, leased-at,
      and expiry.
- [x] **OBX-03** …skips events already leased with an unexpired lease.
- [x] **OBX-04** …reclaims an expired lease.
- [x] **OBX-05** …respects `next_attempt_at`.
- [x] **OBX-06** …is owner-scoped.
- [x] **OBX-07** Two workers leasing concurrently never get the same delivery.
- [x] **OBX-08** `_ensure_deliveries` creates one delivery row per enabled target.
- [x] **OBX-09** `process` on a `canonical_upsert` writes both derived targets.
- [x] **OBX-10** `process` on a `canonical_remove` deletes from both.
- [ ] **OBX-11** A target whose canonical record vanished → `CANONICAL_MISSING`.
- [x] **OBX-12** A record now inactive → `CANONICAL_INACTIVE`.
- [ ] **OBX-13** A record whose hash advanced → `CANONICAL_HASH_ADVANCED`, and the event
      is not applied stale.
- [x] **OBX-14** An owner-binding mismatch → `OWNER_BINDING_MISMATCH`, nothing written.
- [~] **OBX-15** A lost lease → `LEASE_LOST`, no write.
- [x] **OBX-16** Each embedding failure mode maps to its own code (timeout, unavailable,
      invalid response, dimension mismatch).
- [x] **OBX-17** Each index failure maps to its code (fts/vector upsert/delete failed).
- [x] **OBX-18** An unknown exception maps to `UNKNOWN`, never escapes.
- [x] **OBX-19** A retryable failure schedules `next_attempt_at` with backoff.
- [x] **OBX-20** `_stable_jitter` is deterministic per (event, target, attempt) and
      bounded.
- [x] **OBX-21** Attempts beyond the max move the delivery to `dead_letter`.
- [x] **OBX-22** `requeue_dead_letter` returns it to pending and returns True.
- [x] **OBX-23** …returns False for an unknown event/target.
- [x] **OBX-24** `_refresh_event_state` marks the event done only when every delivery is
      terminal.
- [x] **OBX-25** …marks it failed when any delivery dead-letters.
- [ ] **OBX-26** `_set_derived_state` / `_set_derived_failure` keep
      `memory_health_state` in step with the delivery.
- [x] **OBX-27** Processing the same event twice is idempotent.
- [ ] **OBX-28** `schedule_repair` enqueues a reconciliation request with a bounded reason.
- [ ] **OBX-29** `_queue_repair` de-duplicates an identical outstanding repair.
- [x] **OBX-30** Every processed target emits an `OutboxTargetDiagnostic` with latency and
      from/to state.
- [x] **OBX-31** Diagnostics carry no user content.
- [x] **OBX-32** `process_batch` isolates failures — one bad lease doesn't abort the rest.
- [x] **OBX-33** A sensitive record is not written to the FTS/vector index in plaintext.

### `maintenance.py` — MNT

- [x] **MNT-01** `reconcile` on a consistent store reports zero drift and changes nothing.
- [x] **MNT-02** …detects a missing FTS document and repairs it.
- [x] **MNT-03** …detects a missing vector point and repairs it.
- [ ] **MNT-04** …detects a stale hash and refreshes it.
- [x] **MNT-05** …detects a ghost derived row with no canonical record and deletes it.
- [x] **MNT-06** …detects a derived row belonging to another owner and removes it.
- [x] **MNT-07** …honours the batch limit and returns a resumable cursor.
- [x] **MNT-08** `_parse_reconciliation_checkpoint` round-trips
      `_format_reconciliation_checkpoint`.
- [x] **MNT-09** …rejects a malformed checkpoint.
- [x] **MNT-10** …rejects a checkpoint from a different version.
- [x] **MNT-11** `_next_cursor` returns `_RECONCILIATION_CURSOR_DONE` on the final page.
- [x] **MNT-12** Resuming from a cursor covers exactly the remaining records (no gap, no
      repeat) across a full multi-page run.
- [x] **MNT-13** `rebuild_owner` clears and reconstructs both indexes.
- [x] **MNT-14** …is safe to run twice.
- [x] **MNT-15** …touches only its owner.
- [x] **MNT-16** `verify_owner_rebuild` passes after a rebuild.
- [ ] **MNT-17** …fails when a document was tampered with.
- [x] **MNT-18** `coverage` reports per-target counts by state that sum to the record count.
- [x] **MNT-19** `_canonical_checksum` is stable and changes when a record changes.
- [ ] **MNT-20** `_fts_metadata_current` / `_vector_metadata_current` detect a version
      bump as stale.
- [ ] **MNT-21** `MemoryIndexMaintenance.from_settings` honours disabled targets.
- [x] **MNT-22** `PrivilegedGlobalMemoryMaintenance` refuses every method when
      `authorized=False`.
- [x] **MNT-23** …fans out across owners when authorized.
- [x] **MNT-24** `GlobalCoverageReport` aggregates per-owner reports correctly.

### `metrics.py` / `diagnostics.py` — DIA

- [ ] **DIA-01** `MemoryDerivedMetrics.record` increments existing counters and inserts
      new ones.
- [ ] **DIA-02** …rejects an unknown metric code.
- [ ] **DIA-03** …is owner-scoped.
- [ ] **DIA-04** `snapshot` returns every code, defaulting to zero.
- [ ] **DIA-05** `identify_database_owner` returns the bound owner and identity.
- [ ] **DIA-06** …raises on an unbound database.
- [ ] **DIA-07** `_require_sqlite` rejects a non-SQLite engine.
- [ ] **DIA-08** `run_sqlite_integrity_check` returns `("ok",)` on a healthy database.
- [ ] **DIA-09** `create_sqlite_backup` writes a file, and the manifest checksum matches
      it.
- [ ] **DIA-10** …the backup is restorable and byte-identical in content checksum.
- [ ] **DIA-11** `schema_checksum` is stable and changes when the schema changes.
- [ ] **DIA-12** `canonical_data_checksum` is stable, owner-scoped, and changes on a
      record change.
- [ ] **DIA-13** …is insensitive to row ordering.
- [ ] **DIA-14** `inspect_memory_invariants` is healthy on a clean store.
- [ ] **DIA-15** …reports a violation for two active exclusive records on one slot
      (inserted with constraints bypassed).
- [ ] **DIA-16** …reports orphan sources / relations / derived rows.
- [ ] **DIA-17** …reports a cross-owner row.
- [ ] **DIA-18** `MemoryInvariantReport.healthy` is False whenever violations exist.
- [ ] **DIA-19** Violations name the offending ids.

---

## Tier 5 — Recall, prompting, and chat

### `queries.py` — QRY

- [ ] **QRY-01** `MemoryQueryContext` rejects an override whose owner differs.
- [ ] **QRY-02** …rejects `explicit_sensitive_lookup` outside deterministic mode.
- [ ] **QRY-03** …bounds `maximum_records` 1–20 and `maximum_characters` 200–12000.
- [x] **QRY-04** `RecallQuery` in deterministic mode requires a selector.
- [ ] **QRY-05** …accepts each selector kind individually.
- [ ] **QRY-06** `trusted_slot_keys` is capped at 50.
- [ ] **QRY-07** `RecallScoreBreakdown` bounds every component to 0–1.
- [ ] **QRY-08** `RecallResult.canonical_ids` matches its items, in order.
- [ ] **QRY-09** Every contract model rejects extra fields.

### `recall.py` — RCL

Gating:
- [x] **RCL-01** Incognito → empty result, `GATED_INCOGNITO`.
- [x] **RCL-02** Memory disabled → `GATED_MEMORY_DISABLED`.
- [x] **RCL-03** Owner not enabled → `OWNER_NOT_ENABLED`.
- [x] **RCL-04** Database identity mismatch → `OWNER_DATABASE_MISMATCH`, and no query is
      issued.

Fetching and filtering:
- [x] **RCL-05** Inactive records are excluded and counted.
- [x] **RCL-06** Expired records are excluded and counted.
- [x] **RCL-07** SENSITIVE records are excluded unless directly relevant.
- [x] **RCL-08** …included under `explicit_sensitive_lookup` in deterministic mode.
- [x] **RCL-09** PROHIBITED never surfaces (defence in depth — insert one directly).
- [x] **RCL-10** `allowed_domains` filters and counts.
- [x] **RCL-11** `allowed_memory_types` filters.
- [x] **RCL-12** Project scope: a project-scoped record is invisible outside its project.
- [x] **RCL-13** …and a global record is visible inside a project.
- [x] **RCL-14** `MAX_LEXICAL_CANDIDATES` caps the fetch.
- [x] **RCL-15** Deterministic mode by `canonical_id` returns exactly that record.
- [x] **RCL-16** …by `slot_key` returns the slot occupant.
- [x] **RCL-17** …by `trusted_slot_keys` returns one per slot.
- [x] **RCL-18** …by `memory_type` returns that type only.
- [x] **RCL-19** `CORE_IDENTITY_SLOT_KEYS` are always reachable deterministically.

Scoring:
- [x] **RCL-20** `lexical_tokens` lowercases, splits on non-alphanumerics, and drops empties.
- [x] **RCL-21** `_stem` is idempotent and collapses the plural/verb forms it claims.
- [x] **RCL-22** BM25 ranks a term-matching record above a non-matching one.
- [x] **RCL-23** BM25 handles an empty corpus and an empty query without dividing by zero.
- [x] **RCL-24** BM25 saturates — a term repeated 50 times doesn't dominate.
- [x] **RCL-25** `_freshness` decays with the half-life and stays in 0–1.
- [x] **RCL-26** `_freshness` on a future timestamp is clamped to 1.
- [x] **RCL-27** `_aware` coerces naive timestamps to UTC.
- [x] **RCL-28** A pinned record gets at most `PIN_POLICY.max_score_boost`.
- [x] **RCL-29** A pinned record still loses to a far more relevant unpinned one
      (pinning is a boost, not a guarantee).
- [x] **RCL-30** A pinned record is still filtered by owner, status, expiry, sensitivity,
      domain, and budget (one case per `bypasses_*` flag).
- [x] **RCL-31** `USAGE_AFFECTS_RANKING=False` — usage count changes nothing.
- [x] **RCL-32** Every score component lands in 0–1, and `total` is a documented function
      of the components.
- [x] **RCL-33** Scoring is deterministic — two identical queries score identically.
- [x] **RCL-34** Ties break deterministically (not by dict/row order).

Selection:
- [x] **RCL-35** Records below `recall_min_score` are dropped with `BELOW_THRESHOLD`.
- [x] **RCL-36** The result never exceeds `maximum_records`.
- [x] **RCL-37** …nor `maximum_characters`; the overflow is `BUDGET_DROPPED`.
- [x] **RCL-38** Diversity dropping is reported as `DIVERSITY_DROPPED`.
- [x] **RCL-39** The current-turn override suppresses a record the user just changed
      (`CURRENT_TURN_SUPPRESSED`).
- [x] **RCL-40** `CURRENT_USER_MESSAGE_OVERRIDES_STORED_CONTEXT` — the override wins.
- [x] **RCL-41** An empty eligible set returns an empty result with `NO_RELIABLE_MATCH`.
- [x] **RCL-42** Diagnostic counts are internally consistent (eligible = selected +
      every drop bucket).
- [x] **RCL-43** `latency_ms` is populated and non-negative.
- [x] **RCL-44** Usage events are recorded for exactly the injected ids.

Semantic path:
- [x] **RCL-45** `_semantic_available` is False when semantic recall is disabled.
- [x] **RCL-46** …False when the embedding provider is unavailable, with
      `SEMANTIC_UNAVAILABLE` and a working lexical fallback.
- [ ] **RCL-47** A semantic hit for another owner is dropped and counted, and increments
      `semantic_wrong_owner_hit`.
- [ ] **RCL-48** A stale hit (hash advanced) is dropped, counted, and schedules a repair.
- [ ] **RCL-49** A ghost hit (no canonical record) is dropped, counted, and schedules a
      delete repair.
- [ ] **RCL-50** An inactive hit is dropped and counted.
- [ ] **RCL-51** `_schedule_repair` is best-effort — a failure there doesn't fail recall.
- [ ] **RCL-52** Hybrid scoring blends lexical and semantic and is bounded.
- [ ] **RCL-53** `lexical_available=False` degrades to semantic-only with
      `degraded_lexical=True`.
- [ ] **RCL-54** Both unavailable → empty result, both reason codes, no exception.

### `prompt.py` — PMT

- [ ] **PMT-01** `SecureMemoryPromptSerializer` emits the fixed header and
      `STABLE_MEMORY_POLICY`.
- [ ] **PMT-02** The message name is always `UNTRUSTED_MEMORY_MESSAGE_NAME`.
- [ ] **PMT-03** Serialised memory text is escaped/fenced so it cannot close the block —
      test with a record whose display text contains the header, a fence, and
      "ignore previous instructions".
- [ ] **PMT-04** `_slot_label` renders a readable label for each slot shape.
- [ ] **PMT-05** …handles a `None` / malformed slot without raising.
- [ ] **PMT-06** Output stays within the character budget.
- [ ] **PMT-07** Zero records produces no memory message at all (not an empty block).
- [ ] **PMT-08** Serialisation is deterministic for a fixed selection.
- [ ] **PMT-09** `RecallPromptOrchestrator.build` returns both the selection and the
      message.
- [ ] **PMT-10** …records usage exactly once per build.
- [ ] **PMT-11** …skips usage recording when nothing was selected.
- [ ] **PMT-12** `repository_usage_recorder` returns the recorded ids.
- [ ] **PMT-13** A usage-recording failure doesn't lose the prompt.
- [ ] **PMT-14** Sensitive text never reaches the prompt unless explicitly looked up.

### `direct_answer.py` — DAN

- [ ] **DAN-01** `_is_personal_memory_question` accepts "what's my name", "what do you
      remember about me", and rejects general questions.
- [ ] **DAN-02** `_memory_type` maps question shapes to the right type, and returns None
      when unsure.
- [ ] **DAN-03** `_trusted_slots` returns the core identity slots for an identity question.
- [ ] **DAN-04** `answer` returns None for a non-memory question (falls through to chat).
- [ ] **DAN-05** `answer` returns a value when the record exists.
- [ ] **DAN-06** …returns None (not a fabricated answer) when it doesn't.
- [ ] **DAN-07** …returns None when the context is gated (incognito/disabled).
- [ ] **DAN-08** …never returns sensitive content without an explicit lookup.
- [ ] **DAN-09** …is owner-scoped.
- [ ] **DAN-10** Disabling `direct_answer_reads_enabled` disables the path.

### `memory_chat.py` — CHT

- [ ] **CHT-01** `_BROAD_MEMORY_QUERY` matches broad recall asks and picks `BROAD` mode.
- [ ] **CHT-02** A specific question picks `SCOPED_LEXICAL`.
- [ ] **CHT-03** `context_for` carries owner, database identity, profile, request id, and
      current time.
- [ ] **CHT-04** …propagates `active_project_id`.
- [ ] **CHT-05** …propagates incognito and disabled flags.
- [ ] **CHT-06** `build_chat_memory_runtime` wires recall, prompt, and direct-answer
      consistently with settings.
- [ ] **CHT-07** A disabled memory setting yields a runtime that injects nothing.

---

## Tier 6 — Configuration, runtime, and HTTP surface

### `settings.py` / `factory.py` / `runtime.py` — RUN

- [ ] **RUN-01** `MemorySettings` defaults construct without error.
- [ ] **RUN-02** Live extraction without an endpoint raises
      `memory_live_extraction_requires_endpoint`.
- [ ] **RUN-03** …with an unknown provider raises.
- [ ] **RUN-04** An invalid `ollama_request_mode` raises.
- [ ] **RUN-05** Each numeric bound raises at both ends (input chars, recall records,
      recall chars, min score) — parametrised.
- [ ] **RUN-06** Each bound accepts its extremes.
- [ ] **RUN-07** `from_settings` derives the Ollama endpoint from `ollama_url` when the
      provider is ollama and no endpoint is set.
- [ ] **RUN-08** `from_settings` falls back to `default_model` for the extraction model
      (the deployment regression).
- [ ] **RUN-09** `vector_index_enabled` requires both the index worker and semantic recall.
- [ ] **RUN-10** `owner_is_enabled` is False for a blank owner and when disabled.
- [ ] **RUN-11** `_resolve_ollama_request_mode` resolves `auto` from probe capabilities
      and honours an explicit mode.
- [ ] **RUN-12** `_ensure_memory_schema` migrates a fresh profile database.
- [ ] **RUN-13** …is idempotent across calls.
- [ ] **RUN-14** …refuses a database bound to another owner.
- [ ] **RUN-15** `build_memory_runtime` produces a runtime whose `execution` and `context`
      carry the profile's owner and identity.
- [ ] **RUN-16** …is safe to build twice for one profile.
- [ ] **RUN-17** `build_memory_recall_dependencies` respects disabled lexical/semantic
      settings.
- [ ] **RUN-18** `build_semantic_duplicate_finder` returns None-equivalent behaviour when
      semantic is off.
- [ ] **RUN-19** …honours the threshold at both sides.
- [ ] **RUN-20** `drain_memory_outbox` processes pending events and returns a count.
- [ ] **RUN-21** …is a no-op when the worker is disabled.
- [ ] **RUN-22** …never raises out to the caller.

### `api/routes/memory.py` — API

- [ ] **API-01** `GET /memory` returns only the caller's records.
- [ ] **API-02** …shapes each record with scope, field, type, and display text.
- [ ] **API-03** `POST /memory` creates and returns 201.
- [ ] **API-04** …rejects an invalid payload with 422.
- [ ] **API-05** …rejects prohibited content with a clean 4xx, not a 500.
- [ ] **API-06** …applies `_default_slot` when no slot is given.
- [ ] **API-07** …honours project scope from the payload.
- [ ] **API-08** …a duplicate create reconfirms rather than 500-ing on the unique index.
- [ ] **API-09** `GET /memory/{id}` returns 404 for unknown and for another owner's id.
- [ ] **API-10** `PATCH /memory/{id}` updates and bumps the revision.
- [ ] **API-11** …with no fields returns 422.
- [ ] **API-12** …on a revision conflict returns a 409-shaped error.
- [ ] **API-13** `DELETE /memory/{id}` forgets and returns the outcome.
- [ ] **API-14** …on an unknown id returns 404.
- [ ] **API-15** …is idempotent on a second call.
- [ ] **API-16** `GET /memory/candidates` lists pending candidates only.
- [ ] **API-17** `POST /candidates/{id}/accept` applies and returns the record.
- [ ] **API-18** …on an unknown candidate returns 404.
- [ ] **API-19** …on an already-applied candidate is idempotent.
- [ ] **API-20** `POST /candidates/{id}/reject` rejects.
- [ ] **API-21** …is idempotent.
- [ ] **API-22** `_ensure_applied` turns a non-applied result into the right HTTP error
      for every rejection code (parametrised).
- [ ] **API-23** Every route requires a profile and fails cleanly without one.
- [ ] **API-24** No route leaks another profile's data when given its ids.
- [ ] **API-25** Error responses never contain raw memory content.

### `api/routes/memory_health.py` — HLT

- [ ] **HLT-01** Routes 404/403 when `health_routes_enabled` is False.
- [ ] **HLT-02** `_authorized_profile` rejects an unauthorized caller.
- [ ] **HLT-03** `GET /health` reports coverage and metric counts.
- [ ] **HLT-04** `POST /derived/reconcile` runs a bounded reconcile and returns a cursor.
- [ ] **HLT-05** …validates the checkpoint against
      `_RECONCILIATION_CHECKPOINT_PATTERN` and rejects a malformed one with 422.
- [ ] **HLT-06** …validates the owner token against `_UUID_TOKEN_PATTERN`.
- [ ] **HLT-07** `POST /derived/rebuild` rebuilds and reports the result.
- [ ] **HLT-08** Both mutating routes are owner-scoped.
- [ ] **HLT-09** A maintenance failure returns a clean 5xx with no stack trace.

### `memory_retrieval/` and `context_memory/` — RTV / CTX

These are the older, separate retrieval and conversation-compaction subsystems. They ship
in the app and have their own routers, so they need at least a working-order pass.

- [ ] **RTV-01** `POST /index` indexes an item and is idempotent.
- [ ] **RTV-02** `POST /retrieve` returns scored items, scope-filtered.
- [ ] **RTV-03** CRUD on `/items` round-trips create → get → patch → delete.
- [ ] **RTV-04** `GET /items/{id}` 404s for unknown.
- [ ] **RTV-05** `/scopes/{type}/{id}` returns only that scope's items.
- [ ] **RTV-06** `/retrievals` respects its limit bounds (1–300).
- [ ] **RTV-07** `prune/preview` reports what `prune/apply` would remove, and preview
      changes nothing.
- [ ] **RTV-08** `prune/apply` removes exactly the previewed set.
- [ ] **RTV-09** `scorer` ranking is deterministic and bounded.
- [ ] **RTV-10** `redaction` strips what it claims before storage.
- [ ] **RTV-11** `audit` records each retrieval.
- [ ] **RTV-12** Retrieval is owner/scope-isolated.
- [ ] **CTX-01** `POST /preview` summarises without persisting.
- [ ] **CTX-02** `POST /compact` persists a summary and returns it.
- [ ] **CTX-03** `token_budget` respects its limit at the boundary.
- [ ] **CTX-04** `extractor` pulls the expected facts from a transcript.
- [ ] **CTX-05** `redaction` removes sensitive spans before summarising.
- [ ] **CTX-06** `summarizer` is deterministic given a fixed model double.
- [ ] **CTX-07** Summaries and events are scope-isolated.
- [ ] **CTX-08** `GET /summaries/{id}` 404s for unknown.
- [ ] **CTX-09** Event creation returns 201 and appears in the scope's event list.

---

## Tier 7 — Cross-cutting properties

These are the ones that decide whether the layer is genuinely safe to use.

### Owner and profile isolation — ISO

- [ ] **ISO-01** Two profiles' databases never share a binding.
- [ ] **ISO-02** No repository method returns another owner's row, given its id directly
      (parametrised over every getter).
- [ ] **ISO-03** No mutation writes across owners, given a foreign id (parametrised over
      every command).
- [ ] **ISO-04** Recall never returns a foreign record, even via a deterministic id.
- [ ] **ISO-05** Neither derived index returns a foreign row.
- [ ] **ISO-06** The outbox never processes a foreign event.
- [ ] **ISO-07** Maintenance never touches a foreign owner.
- [ ] **ISO-08** The API never exposes a foreign record.
- [ ] **ISO-09** Fingerprints and tombstones from one owner never match another's.
- [ ] **ISO-10** A guest/ephemeral profile leaves nothing in the registered store.

### Privacy — PRV

- [ ] **PRV-01** Prohibited content never lands in any table (sweep every table after an
      attempted write).
- [ ] **PRV-02** Sensitive content is encrypted at rest in records, candidates, sources,
      and operations.
- [ ] **PRV-03** Sensitive content never appears in logs (capture logging during a full
      sensitive round-trip).
- [ ] **PRV-04** Sensitive content never appears in diagnostics or outbox payloads.
- [ ] **PRV-05** Sensitive content never reaches the derived indexes.
- [ ] **PRV-06** `erase_permanently` leaves no trace in any table (sweep by content).
- [ ] **PRV-07** `forget` leaves no recallable trace but keeps provenance.
- [ ] **PRV-08** A forgotten fact does not come back through recall, direct answer, the
      prompt, or the API.

### Concurrency — CNC

- [ ] **CNC-01** Concurrent creates on one exclusive slot leave exactly one active record.
- [ ] **CNC-02** Concurrent updates: one succeeds, the other gets a revision conflict.
- [ ] **CNC-03** Concurrent forget + update never produces a forgotten-but-updated row.
- [ ] **CNC-04** Concurrent outbox workers never double-apply an event.
- [ ] **CNC-05** Reconcile running during a mutation doesn't corrupt derived state.
- [ ] **CNC-06** The same idempotency key used concurrently produces one operation.

### End-to-end journeys — E2E

Each runs the full stack — extraction → mutation → outbox → recall → prompt.

- [ ] **E2E-01** "Remember I want to improve at urban sketching" → stored → recalled on a
      later relevant turn → appears in the prompt.
- [ ] **E2E-02** Restating the same fact later does not create a second record.
- [ ] **E2E-03** "Actually, now I want to improve at watercolour" replaces the goal;
      recall returns only the new one.
- [ ] **E2E-04** "Forget that I use a fineliner pen" removes it from recall permanently,
      and re-asserting it automatically is blocked by the tombstone.
- [ ] **E2E-05** …but explicitly re-stating it restores it.
- [ ] **E2E-06** A preference set in one domain doesn't leak into another domain's recall.
- [ ] **E2E-07** A project-scoped memory is invisible outside the project.
- [ ] **E2E-08** An identity fact ("my name is …") is answerable directly without the LLM.
- [ ] **E2E-09** A sensitive fact stated without an explicit request is not stored and is
      surfaced for review.
- [ ] **E2E-10** …with an explicit request is stored encrypted and recalled only on a
      direct question.
- [ ] **E2E-11** A prohibited fact is never stored and the user is told so.
- [ ] **E2E-12** Asking "what do you remember?" creates no new records (the duplicate
      regression, end to end).
- [ ] **E2E-13** Incognito: nothing is written, nothing is recalled, and the store is
      byte-identical afterwards (canonical checksum).
- [ ] **E2E-14** Memory disabled: same.
- [ ] **E2E-15** Kill the process mid-mutation (simulated): the store is consistent and
      the outbox catches up on restart.
- [ ] **E2E-16** Drop both derived indexes, run `rebuild_owner`, and recall returns
      identical results to before.
- [ ] **E2E-17** A cold start with an empty store answers "I don't have that" rather than
      erroring.
- [ ] **E2E-18** Fifty facts across every type: recall stays within budget and returns the
      most relevant.

### Performance guardrails — PRF

Not benchmarks — regression tripwires with generous bounds.

- [ ] **PRF-01** Recall over 1,000 records completes under a fixed budget.
- [ ] **PRF-02** Recall issues a bounded number of queries (no N+1 over records).
- [ ] **PRF-03** A single mutation issues a bounded number of statements.
- [ ] **PRF-04** Reconcile over 1,000 records stays within its batch limit per call.
- [ ] **PRF-05** The prompt never exceeds `MAX_RECALL_CONTEXT_CHARS`, even with
      pathological display text.

---

## Counts

| Tier | Area | Cases |
|---|---|---|
| 0 | Infrastructure | 8 |
| 1 | Foundations | 196 |
| 2 | Extraction | 152 |
| 3 | Persistence | 205 |
| 4 | Derived / async | 101 |
| 5 | Recall / prompt / chat | 94 |
| 6 | Config / runtime / HTTP | 77 |
| 7 | Cross-cutting | 47 |
| | **Total** | **880** |

23 of those are already covered by the existing 51 tests (several existing tests pin the
same case from different angles, and several plan entries are parametrised into more than
one test, so the two counts don't line up one-to-one). Tier 1 and Tier 3 carry most of the
risk and most of the value per hour.

## Suggested order

1. **Tier 0** — nothing else can be written without it.
2. **Tier 1** — pure, fast, and where silent data corruption starts.
3. **Tier 3 SCH + REP + MIG** — the constraints that make everything above enforceable.
4. **Tier 3 PLN + MUT** — the transactional core.
5. **Tier 5 RCL** — recall correctness is what the user actually experiences.
6. **Tier 2 EXC** — the seam where the model meets the store.
7. **Tier 4** — the async path; slow to debug in production, cheap to test here.
8. **Tier 6 + 7** — the surface and the guarantees.
