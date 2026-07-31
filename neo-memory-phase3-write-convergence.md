# Neo memory redesign — Phase 3 write convergence

## Scope and rollout decision

Phase 3 introduces one structured adapter/coordinator boundary over the authoritative Phase 0–2
kernel. Production routes remain on the legacy implementation by default. Canonical and shadow v2
execution is restricted to an explicit owner allowlist and an explicitly configured disposable
database root. There is no production cutover, uncontrolled dual-write, legacy-data migration,
natural-language extraction redesign, canonical recall, or derived-index worker in this phase.

This is deliberate: exposing only the controlled adapter boundary prevents production users from
encountering a partially integrated runtime while every current writer has either a structured v2
adapter path or an explicit disabled/deferred reason below.

## Re-audited mutation-path inventory

| Entry point | Current legacy behavior | Phase 3 adapter | Owner source | Idempotency source | Actor / source | Compatibility | Phase 3 state | Tests |
|---|---|---|---|---|---|---|---|---|
| `POST /conversation` with `persist` | extractor persists candidates and auto-review writes legacy rows | `ImportMemoryV2Adapter` / `CandidateReviewV2Adapter` after structured acceptance | authenticated profile owner | batch/message plus item hash | user / import or automatic extraction | typed result mapping | v2 boundary available; legacy route remains default | surface parity, architecture |
| `POST /extract-memory` with `persist` | same direct legacy candidate/review path | `CandidateReviewV2Adapter` | profile owner | candidate ID, revision, action | user / automatic extraction | explicit review outcome | extractor unchanged; v2 canonical application controlled only | candidate review |
| `POST /memory/review` | category-specific legacy mutations | `CandidateReviewV2Adapter` | profile owner | candidate ID, revision, review action | user / review | create, reconfirm, refine, replace, merge, reject, needs-review visible | enabled only for disposable adapter callers | candidate review |
| `POST /memories` | `MemoryStore.create_manual_memory` | `GenericMemoryV2Adapter.create` | profile owner | request/client mutation ID | user / manual UI or HTTP | full typed mapping | adapter enabled for disposable owners; route legacy by default | surface parity |
| `PATCH /memories/{id}` | legacy in-place edit | `GenericMemoryV2Adapter.update` or explicit `replace` | profile owner | request ID plus operation | user / HTTP | incompatible update is rejection, never silent replacement | controlled adapter only | Phase 2 plus candidate review refinement |
| generic archive | legacy lifecycle service | `GenericMemoryV2Adapter.archive` | profile owner | request/client mutation ID | user / HTTP | archived visible | controlled adapter only | lifecycle |
| generic delete | legacy deletion/tombstone | `GenericMemoryV2Adapter.forget` | profile owner | request/client mutation ID | user / HTTP | forgotten visible | controlled adapter only | lifecycle |
| permanent erase | no ordinary legacy route | `GenericMemoryV2Adapter.erase_permanently` | profile owner | explicit client mutation ID | user / manual UI | erased-permanently visible | controlled explicit adapter only | lifecycle |
| generic restore | legacy lifecycle restore | `GenericMemoryV2Adapter.restore` | profile owner | request/client mutation ID | user / HTTP | invalid restore remains rejection | controlled adapter only | lifecycle, manual |
| generic supersede | legacy lifecycle service | `GenericMemoryV2Adapter.supersede` | profile owner | request ID | user / HTTP | superseded visible | controlled adapter only | Phase 2 command coverage |
| explicit merge | legacy review merge | `GenericMemoryV2Adapter.merge` | profile owner | request/review ID | user / review | merged visible | controlled adapter only | candidate review |
| profile/identity writes | typed legacy table | `TypedMemoryV2Adapter` with `identity` taxonomy | profile owner | client mutation ID | user / manual UI | no typed-table projection | available; legacy route remains default | architecture and common typed parity |
| preferences | typed legacy table | `TypedMemoryV2Adapter` with scoped preference identity | profile owner | client mutation ID | user / manual UI | no typed-table projection | available; legacy route remains default | category correction |
| goals | typed legacy table | `TypedMemoryV2Adapter` | profile owner | client mutation ID | user / manual UI | no typed-table projection | enabled in disposable validation | surface parity, manual |
| projects | typed legacy table | `TypedMemoryV2Adapter` with additive project slot | profile owner | client mutation ID | user / manual UI | no typed-table projection | adapter available; route deferred | architecture |
| education | typed legacy table | `TypedMemoryV2Adapter` with history/current-status slot | profile owner | client mutation ID | user / manual UI | no typed-table projection | adapter available; route deferred | Phase 0 taxonomy plus architecture |
| employment | extraction/review typed legacy table | `TypedMemoryV2Adapter` | profile owner | candidate/client mutation ID | user / review | no typed-table projection | adapter available; no standalone current route | architecture |
| activities | typed legacy table and expiry job | `TypedMemoryV2Adapter`; maintenance archive proposal | profile owner | client ID or maintenance run/hash | user or maintenance | no typed-table projection | controlled adapter; job legacy by default | architecture |
| events | typed legacy table | `TypedMemoryV2Adapter` | profile owner | client mutation ID | user / manual UI | no typed-table projection | adapter available; route deferred | architecture |
| knowledge/fact | generic legacy memory row | typed/generic adapter with knowledge taxonomy | profile owner | request/import item hash | user/import | full typed result | enabled in manual import | surface parity, manual |
| sync chat | extraction calls legacy persist/review | `ChatMemoryV2Adapter` | authenticated profile owner | message ID, extraction version, candidate key | user / automatic extraction | typed result | controlled structured candidate boundary; normal chat remains legacy | sync/stream parity |
| streaming chat/generation worker | same legacy extraction after stream | same `ChatMemoryV2Adapter` method | captured profile session owner | same message-derived key | user / automatic extraction | identical failure/result | controlled structured candidate boundary; worker remains legacy | sync/stream parity |
| chat retry/rerun | source detachment then legacy re-extraction | chat adapter plus source-change coordinator | profile owner | message ID, edit revision, affected ID | user / automatic extraction | exact typed source result | canonical structured retry supported; exact v2 source row is detached | sync/stream, source change |
| message edit | directly detaches legacy sources and may archive | `MemoryV2SourceChangeCoordinator` | profile owner | message ID, edit revision, affected record and source UUID | user / chat message | preserved, needs-review, not-found, already-detached, and owner-mismatch are explicit | source-only mutation; no implicit lifecycle action | source change |
| chat deletion | broad legacy source detachment loop | same source-change coordinator per exact source | profile owner | chat deletion revision plus source/record ID | user / chat message | no broad text removal | each exact source change is persisted independently; canonical lifecycle is unchanged | source change, architecture |
| reflection | directly creates legacy reflection/candidates | structured proposal then `CandidateReviewV2Adapter` if accepted | explicit owner required | reflection run plus candidate hash | maintenance / review | proposal is not success | proposal-only for v2; legacy route default | architecture |
| aging | directly archives/decays legacy rows | `MaintenanceMemoryV2Adapter.archive_proposal` | explicit owner | run ID plus proposal hash | maintenance / maintenance | command outcome visible | adapter available; current global job deferred | architecture |
| duplicate cleanup/compression | legacy maintenance mutates | maintenance proposes merge/archive command | explicit owner | run ID plus command hash | maintenance / maintenance | no omission-as-delete | proposal-only until explicitly accepted | architecture |
| audit/tombstone repair | legacy maintenance can repair | no v2 repair adapter | explicit owner | n/a | maintenance | fail closed | intentionally disabled: diagnostics remain read-only | Phase 1 diagnostics, architecture |
| structured import/backup input | legacy bundle stores/archive-only modes | `ImportMemoryV2Adapter` | authenticated destination owner | batch ID plus item hash | user/system / import | foreign IDs/status ignored | structured accepted facts enabled in disposable mode | surface parity, manual |
| legacy DB migration | not a normal request path | none | n/a | n/a | migration | n/a | deferred to migration phase; explicitly out of Phase 3 | architecture |
| agent/tool memory mutation | no dedicated v2 tool currently | `AgentMemoryV2Adapter` | mandatory tool context owner | tool-call ID | agent / agent tool | stable typed result | adapter available; no new MCP/provider plugin exposed | architecture |
| explicit natural-language remember/forget parser | legacy `memory_intent` heuristics | structured adapter only after existing parser yields approved typed input | profile owner | request/message ID | user / direct command | kernel rejection visible | natural-language redesign deferred to Phase 4 | architecture |
| retrieval pruning/indexing APIs | separate context-memory subsystem, not canonical personal-memory authority | none | separate scope | separate subsystem | system | n/a | not a v2 personal-memory writer | static audit |

## Architecture

`adapters.py` owns surface-to-command translation. `idempotency.py` creates stable, hashed,
owner-scoped keys. `coordinator.py` validates rollout state and execution context, upgrades only the
explicit disposable database, constructs `MemoryMutationService`, executes exactly one command, and
maps its typed result through `compatibility.py`. Adapters never import v2 ORM models or the Phase 1
repository and never commit.

The coordinator API is:

```python
execute(context: MemoryV2ExecutionContext, command: MemoryCommand) \
    -> MemoryV2CoordinationResult
```

The main adapter APIs are `create`, `update`, `replace`, `merge`, `archive`, `forget`,
`erase_permanently`, `restore`, `supersede`, and exact source detachment. Specialized adapters add typed create, review
actions, shared sync/stream structured candidates, structured import, maintenance proposals, and
agent tool calls. Canonical commands delegate through `GenericMemoryV2Adapter.execute`; source-only
changes delegate through `GenericMemoryV2Adapter.detach_source`. Both paths reach the shared
coordinator and the sole `MemoryMutationService` transaction boundary.

## Feature flags and precedence

All settings use the `NEO_` prefix:

- `memory_v2_schema_enabled=false`;
- `memory_v2_shadow_mutations=false`;
- `memory_v2_canonical_writes=false`;
- `memory_v2_legacy_compatibility=true`;
- `memory_v2_enabled_owner_ids=""`;
- `memory_v2_disposable_database_root=""`.

| State | Result |
|---|---|
| schema off, writes off | legacy mode; adapters permit the existing caller to use legacy behavior |
| schema on, owner not allowlisted, writes off | schema-only/legacy behavior; no v2 call |
| schema on, allowlisted owner, shadow on | mutation service executes `dry_run`; no canonical rows |
| schema on, allowlisted owner, canonical on | canonical v2 write; legacy write is forbidden |
| incognito or memory disabled | typed `disabled` result; zero engine/repository/service calls |

Shadow/canonical without schema, simultaneous shadow and canonical, canonical without an owner
allowlist, or mutation mode without a disposable root fails settings validation. Runtime mutation
also requires `disposable=True`, file-backed SQLite, an exact path below the configured root, and an
allowlisted owner. There is no fleet-wide switch.

`memory_v2_legacy_compatibility` controls result-shape availability, not a legacy write. A canonical
v2 owner never receives an automatic legacy fallback or dual-write.

## Owner and database propagation

Every execution carries an explicit canonical owner UUID, profile ID, database identity, database
URL, guest flag, incognito flag, and memory-enabled state. Adapter context separately carries actor,
source, request, session, conversation, message, observation, and evidence metadata. The coordinator
rejects empty/default database URLs, invalid owners, profile/identity mismatch, guest/permanent
prefix mismatch, paths outside the disposable root, and an existing database bound to another
owner. The Phase 1 migration binding supplies the final database/owner check.

## Idempotency

Keys contain no candidate plaintext. A stable JSON envelope is SHA-256 hashed. Canonical-command
keys are stored in the Phase 2 operation ledger; the source-change key is returned in its narrow
result:

- HTTP: owner + request ID + operation;
- review: owner + candidate ID + revision + action;
- chat sync/stream: owner + message ID + extraction version + candidate key;
- source change: owner + message ID + edit revision + affected record + action;
- import: owner + batch ID + item hash;
- agent: owner + tool-call ID;
- maintenance: owner + run ID + proposal hash;
- manual UI: owner + client mutation ID.

Candidate proposal UUIDs are deterministically derived from the key, so canonical-command retries
serialize exactly the same request. Same-key changed content returns `idempotency_conflict`;
different owners remain independent. An exact source detachment is deliberately not a canonical
operation-ledger row: its idempotence is state-based. A repeated request performs no write and
returns the typed `already_detached` result with the current remaining-support count.

## Compatibility and transaction boundaries

The result mapper preserves every Phase 0 outcome, operation ID, active/affected IDs, revision,
rejection code, error code, and review requirement. `needs_review`, owner failures, resurrection
blocks, revision conflicts, and idempotency conflicts are never mapped to success.

Source changes use their own narrow typed result directly, so no legacy compatibility projection is
needed. It always includes the action, outcome, memory and source IDs, remaining active support
count when known, review requirement, observed memory revision, and explicit false values for
canonical mutation and canonical revision change.

`MemoryMutationService` owns the canonical transaction. Adapters and coordinator do not commit v2
tables. No legacy write runs inside or after a failed v2 call. Compatibility mapping occurs after
the mutation returns; an injected mapping failure proves the committed operation remains durable and
retryable. Phase 3 code has no model, network, embedding, vector, FTS, Qdrant, or file-processing
call inside the canonical transaction.

## Sync/stream convergence

Sync and stream call the same `ChatMemoryV2Adapter.apply_structured_candidate`. Transport affects
validation only, never source or idempotency material. Cross-transport retry returns the original
operation/result and produces one record, source, operation, and logical outbox action. Existing
extractor semantics are unchanged; only already structured candidates are accepted.

## Source edit/delete behavior

Source detachment is a narrow source-change contract, not a new Phase 0 canonical
`MemoryOperationKind`. The caller supplies an exact source UUID and target revision; the mutation
kernel derives the remaining support count from persisted SQL. It marks that source inactive with a
detachment reason and does not create an operation-ledger row, lifecycle relation, tombstone, or
outbox event. It never changes canonical status or revision.

When another active supporting source remains, the result is `action=detach_source`,
`outcome=preserved`, and `review_required=false`. Removing the final active support persists the
detachment and returns `needs_review`, but creates no review, archive, forget, delete, supersede, or
other canonical operation. Missing, already-detached, cross-owner, and revision-conflict requests
return explicit non-null outcomes. Repeated deletion is a zero-write `already_detached` result.

## Enforcement

Static tests prove that Phase 3 runtime modules import neither v2 ORM models nor the Phase 1
repository, only the coordinator references `MemoryMutationService`, no Phase 3 code references
embeddings/vectors/FTS/network clients, legacy production writers have no partial v2 imports, and
default flags remain legacy. Behavioral tests cover flags, owner/binding security, incognito,
surface parity, review parity, sync/stream replay, imports, lifecycle, source changes, and failure
injection.

## Manual validation

See `docs/manual-memory-v2-phase3.md` and run:

```bash
.venv/bin/python scripts/manual_memory_v2_phase3.py --keep
```

## Deliberate deviations and Phase 4 prerequisites

- Existing public legacy routes are not partially switched. They stay wholly legacy under default
  configuration; disposable Phase 3 callers use the explicit adapters.
- No typed legacy projection is created. Phase 5/cutover work must choose and validate projection or
  read migration behavior.
- Source-only provenance detachment is complete for exact v2 source UUIDs; canonical lifecycle
  decisions after final-source review remain outside this correction.
- Existing natural-language correction/removal heuristics are not copied into Phase 3. Phase 4 must
  emit explicit typed candidates, targets, evidence, and revisions.
- Normal chat still cannot use v2 for ordinary prompts until Phase 4 produces structured commands;
  Phase 5 must then supply canonical recall.
- Derived outbox consumers remain Phase 6 work.
- Legacy-data migration and production owner enablement require separate plans and validation.

Phase 4 is technically unblocked at the structured command boundary, but must not start until the
independent Phase 3 manual package is reviewed and accepted.
