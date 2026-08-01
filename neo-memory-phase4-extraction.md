# Neo memory v2 Phase 4: extraction and correction planning

## Scope and architecture

Phase 4 converts trusted user-authored language into typed, grounded candidate proposals. It does
not add recall, prompt serialization, direct-answer integration, search workers, legacy-data
migration, or production cutover.

The write path is:

```text
trusted user source
  -> bounded deterministic pre-parser
  -> explicit grammar or bounded extraction-model request
  -> strict schema validation
  -> exact speaker/span grounding
  -> durability, sensitivity, and taxonomy validation
  -> owner-bound deterministic correction resolution
  -> typed candidate decision
  -> Phase 3 chat adapter/coordinator
  -> Phase 2 MemoryMutationService
```

Only `MemoryV2ExtractionCoordinator` invokes an extraction provider. Extraction modules do not
import ORM models and cannot write canonical tables. The model sees a bounded conversation input,
not owner IDs, database state, configuration secrets, arbitrary system prompts, or unbounded
history. It can suggest local proposal IDs, facts, retractions, correction groups, spans, type,
domain, slot, durability, confidence, and sensitivity. It cannot choose canonical IDs,
predecessor IDs, lifecycle state, database actions, or policy exceptions.

## Timing policy

The foreground path handles only bounded unambiguous grammars: explicit remember and lifecycle
commands, direct approved assertions, explicit replacement, and deterministic linked correction.
It performs no model call. If structure or target resolution is uncertain, it returns review or
defers; it never guesses.

General extraction is post-turn. It may call the configured model after response generation and
can propose at most four automatic candidates. Timeout or malformed output is isolated from the
chat response and produces no generic fallback mutation. When the bounded pre-parser already has
an exact, durable positive span, malformed model output may persist only a non-recallable
`needs_review` candidate carrying the failure code and unresolved-domain/slot markers; it can
never create or suppress a canonical record. Explicit batches may exceed four up to
the policy limit of 50, with independent candidate IDs, validation, persistence, and results.
Provider connections, response reads, and local-model warm-up have separate bounded settings.
Their defaults are 5, 120, and 300 seconds respectively; the old eight-second all-stage deadline
is no longer used by the Phase 4 live path. Transport failures are not retried.

## Versioned contracts and strict model schema

`ExtractionRequest` carries trusted owner context, request/conversation/session/message IDs, the
current user message, a maximum 12-message/12,000-character supporting window, explicit intent,
incognito and memory state, extraction mode, contract/taxonomy/policy versions, candidate limit,
and an NFKC content hash. `ModelExtractionInput` deliberately omits owner and database context.

The model response schema is version 1 and `extra="forbid"` throughout. Assertions, retractions,
exclusions, exact source spans, subject, type/domain/slot hints, typed value, durability,
confidence, sensitivity hint, correction group, and explicit cross-taxonomy-change signals are
typed. Unknown fields, schema versions, operation-like fields, missing spans, duplicate local IDs,
and outputs above 128 KB are rejected. Plain JSON, surrounding whitespace, and one exact
`json`-labelled code fence are accepted. Prose surrounding an object and arbitrary first-brace
searching are forbidden. A single schema-repair retry is allowed; timeout and transport errors
are not retried. Repaired output receives the same full grounding and policy checks.

## Bounded pre-parser

The pre-parser recognizes explicit remember/batch/lifecycle commands, direct video goals,
scoped and global preferences, explicit and implicit replacements, preference correction, pure
location creation/retraction, additive goals, and `not only`. The residence grammar is limited to
clear first-person forms such as `I live in <location>`, `I currently live in <location>`, and
`My current city is <location>`; visiting and current-moment locations are excluded. The two exact
category references `That is a goal, not a preference` and `What I said is a goal, not a
preference` may resolve only against a bounded recent user assertion. It marks questions,
temporary states,
hypotheticals, third-party subjects, and ambiguous pronouns. Its grammar is intentionally small:
it segments evidence and supplies safe hints but does not infer missing facts or act as a broad
regex fallback.

The critical sentence, without words such as `correction` or `replace`, yields linked old/new
spans. `make` is normalized to `create` for deterministic matching. The new value remains
`create short Instagram reels clearly`; the adverb is neither a domain nor a preference signal.

## Grounding

Every proposal must cite one to four exact spans from the current authorized input. Grounding
checks the message ID, role, offsets, NFKC-normalized quote, and value support. Only user-role
messages are authoritative. Assistant, system, tool, wrong-message, impossible-offset, and
hallucinated-value proposals are rejected. Similarity is never evidence. Persisted grounding uses
source provenance and hashes; diagnostic telemetry does not store raw transcripts or reasoning.

## Durability and sensitivity

Temporary, hypothetical, question, third-party, assistant, ambiguous-subject, and uncertain
proposals do not become active. Stable identity, preferences, ongoing goals/projects, recurring
activity, and explicit durable knowledge can proceed. A model's durability label is only a hint
validated against deterministic source markers.

Prohibited content is detected before pre-parsing or model invocation and again during candidate
admission and mutation. It creates no candidate, operation, provenance, canonical record, or
outbox row. Its public pre-parser result and override are empty and diagnostics contain only a
stable reason code. Sensitive classification evaluates both grounded spans and the trusted current
user message, so a model cannot downgrade sensitivity by selecting a narrow span. Sensitive
content requires trusted explicit user intent; candidate, canonical, operation, and provenance
payloads use the approved encryption path. Plaintext display columns and operator-visible output
remain null or use the fixed `[sensitive memory]` redaction. Raw-output hashes are withheld for
sensitive results.

The Phase 0 mutation contract supports typed `expires_at` updates, but Phase 4 does not invent an
expiry from vague language. A genuinely time-bounded fact without an exact application-supplied
expiry is rejected/reviewed rather than persisted permanently. Automatic date interpretation is
left disabled until a typed date-resolution boundary is approved.

## Taxonomy and correction resolution

Phase 0 `MemoryType`, domain resolution, slot builders, and cardinality rules are authoritative.
Video editing, YouTube creation, reels, and short-form video normalize to `video_creation`.
Topic-specific advice stays domain-scoped; only genuinely global wording produces a global
response-style preference. There is no last-token domain fallback.

The resolver receives normalized candidates and owner-visible canonical snapshots from an
explicit Phase 3 query. It compares subject, type, domain, slot, normalized old value, and grounded
target language. It returns create, reconfirm, replace, pure retract, needs-review, or reject. The
model cannot supply target IDs. Vector similarity, timestamps, and confidence never select a
predecessor. A linked correction inherits the matched exclusive predecessor slot and replaces all
active conflicts in that slot. Missing or ambiguous target relationships produce a review
candidate rather than an unrelated active fact.

For the critical correction, deterministic resolution finds the old video goal, passes its
trusted ID/revision to one Phase 3 replacement call, leaves one active replacement, marks the
predecessor superseded, creates the lineage relation, and retains the negated clause only as
retraction/source evidence.

## Candidate lifecycle and current-turn override

Grounded candidates persist in `memory_candidates_v2` as `validated` or `needs_review`. Applied
candidates transition to `applied` in the same Phase 2 operation and link to the operation ID.
Policy rejection prevents prohibited persistence; ambiguous candidates remain non-recallable and
separate from canonical mutation. Deterministic candidate UUIDs make repeated and sync/stream
processing idempotent.

The grounded malformed-model fallback returns a typed review decision with the proposed memory
type, null proposed domain/slot hints when unresolved, explicit `domain_unresolved` and
`slot_unresolved` booleans, and the stable model-failure reason. Its normalized SQL routing fields
exist only to store the non-recallable candidate; the unresolved decision fields and grounding
evidence remain authoritative until review.

Every result includes a fail-closed `CurrentTurnOverride` with separate fields for records and
slots explicitly authorized for suppression, records merely considered as candidate targets,
unresolved conflict slots, a prompt-safe positive assertion, a redacted sensitive assertion,
sensitivity, review status, confidence, and deterministic contradiction status. Compatibility
`contradicted_*` fields are validated aliases of `suppressed_*`; they cannot acquire broader
meaning.

Suppression is derived only after the final typed mutation outcome. Create, exact duplicate,
reconfirmation, and compatible refinement suppress nothing. An applied deterministic replacement
suppresses only predecessor IDs and their affected slots. An applied pure retraction suppresses
the retracted IDs and slots. Review ambiguity can expose candidate-target and unresolved-slot
metadata but is structurally forbidden from authorizing suppression. Rejected, ignored, failed,
prohibited, incognito, and memory-disabled results publish empty overrides. Sensitive results
never place plaintext in the positive assertion. The override remains data only; Phase 4 neither
recalls nor injects memory.

## Provider and failure boundary

`ExtractionModelProvider` is the transport protocol boundary. `DirectJsonExtractionProvider`
expects the HTTP response body itself to be the extraction schema.
`OllamaChatExtractionProvider` sends the configured model, bounded system instruction, bounded
model input, `stream=false`, temperature zero, a stage-specific response timeout, and an explicit
structured-output mode. `ollama_schema` sends the authoritative JSON schema object in Ollama's
`format` field. `ollama_json` sends `format="json"` but still subjects the response to the complete
strict Pydantic schema, grounding, owner, taxonomy, sensitivity, and correction policy. A
synthetic capability probe prefers schema mode and may select JSON mode only after the schema
probe fails and JSON succeeds. `think=false`, seed, `num_predict`, and `keep_alive` are emitted
only when individually supported; the system instruction always prohibits reasoning output.
The provider recognizes only the Ollama chat envelope
with assistant output in `message.content`; thinking/reasoning fields are ignored. Provider
selection is explicit as `direct_json` or `ollama`. Unknown providers, envelopes, partial/NDJSON
streams, empty content, model-not-found responses, HTTP errors, and timeouts fail closed. Future
providers implement the same boundary rather than sharing an assumed JSON shape. There is no
provider dependency in the mutation kernel.

The Ollama probe calls `/api/version` and `/api/tags`, verifies the exact configured model, warms
it with a constant synthetic message, and audits the complete production schema format, JSON
format, `think`, seed,
`num_predict`, and `keep_alive`. Probe input contains no user memory and the probe has no
persistence dependency. Ollama error envelopes are mapped to stable sanitized codes. Durable
diagnostics never store provider error text; the manual probe may print a maximum 240-character,
allowlisted message only after rejecting user, sensitive, and secret content.

Assistant content is stripped only of surrounding whitespace or one documented exact JSON fence,
then JSON-parsed and strictly schema-validated before grounding. Malformed JSON/schema, unknown
values, missing/bad spans, hallucination, invalid taxonomy,
ambiguous correction, timeout, transport failure, and excessive candidates produce stable typed
diagnostics with no unexpected active mutation. Review persistence occurs only where grounded
evidence is sufficient. No legacy broad extractor is invoked as fallback.

## Feature flags and production isolation

The following settings all default to `false`:

- `memory_v2_extraction_enabled`
- `memory_v2_foreground_commands_enabled`
- `memory_v2_post_turn_extraction_enabled`
- `memory_v2_live_extraction_model_enabled`

Extraction also requires existing schema/canonical-write gates and explicit disposable owner
binding. Invalid combinations fail closed; live mode additionally requires an endpoint and an
explicit supported provider. Incognito
and memory-disabled requests perform zero model, candidate, or mutation calls. Production remains
on the legacy path and a v2 disposable owner uses only the v2 coordinator, never both extractors.
The legacy extractor remains intact for eventual Phase 5+ migration planning.

## Observability

Bounded structured diagnostics contain request/owner/message IDs, extractor/model/prompt versions,
provider kind, HTTP status, recognized envelope shape, content presence and byte length,
response-content hash for non-sensitive output, JSON parse result, schema validation result,
stable schema error codes, sanitized provider error code, timeout stage, latency, parse outcome,
proposal and accepted/rejected/review counts,
stable reason codes,
operation IDs, explicitly suppressed IDs, candidate-target IDs, and unresolved conflict slots.
Raw user text, sensitive plaintext, secrets, credentials, encryption keys, complete transcripts,
provider output, and hidden reasoning are excluded. Sentinel regressions scan serialized results,
diagnostics, captured stdout/stderr, application logs, all v2 SQL tables, and retained manual
artifacts.

## Validation and deliberate boundaries

Automated Phase 4 tests use deterministic providers and cover corrections, grounding, failures,
security, timing, idempotency, candidate transitions, flags, and static architecture. The manual
fixture and optional live-provider procedure are documented in
`docs/manual-memory-v2-phase4.md`.

Deliberate boundaries are: no heuristic expiry-date parsing; no multi-language free-form grammar
beyond model-assisted typed proposals; no semantic/vector predecessor matching; no candidate
review UI; and no recall consumption of current-turn overrides. These are safe fail-closed limits,
not generic fallbacks.

## Phase 5 prerequisites

Phase 5 remains blocked until independent Phase 4 manual validation confirms extraction quality,
the critical correction, review routing, sensitive/prohibited behavior, and disposable isolation.
After that approval, Phase 5 may consume current-turn overrides, implement owner-scoped canonical
recall and prompt serialization, migrate direct-answer consumers, and define recall budgets. No
Phase 5 code is included here.
